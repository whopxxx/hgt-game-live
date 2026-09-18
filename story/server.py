#!/usr/bin/env python
# coding: utf-8
"""stdlib HTTP + 手写 WebSocket 渲染服务。

零新依赖(不引入 FastAPI/uvicorn)。服务器只做两件事:
    1. 发 3 个静态文件
    2. 以 4Hz + 阶段变更时立即推 JSON 快照

帧只服务端->客户端, 不打掩码, opcode=1(文本)。快照 ~2-4KB, 无需分片。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger("story.server")

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class StateHub:
    """向所有浏览器广播快照。线程安全。"""

    def __init__(self):
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._last: str = "{}"

    def add(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.add(sock)

    def remove(self, sock: socket.socket) -> None:
        with self._lock:
            self._clients.discard(sock)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            self._last = data
            clients = list(self._clients)
        frame = _encode_text_frame(data)
        dead = []
        for c in clients:
            try:
                c.sendall(frame)
            except Exception:
                dead.append(c)
        if dead:
            with self._lock:
                for c in dead:
                    self._clients.discard(c)
                    try:
                        c.close()
                    except Exception:
                        pass

    def last(self) -> str:
        with self._lock:
            return self._last


def _encode_text_frame(text: str) -> bytes:
    """单帧文本。不支持分片(快照 < 64KB 足够)。"""
    payload = text.encode("utf-8")
    n = len(payload)
    if n < 126:
        header = struct.pack("!BB", 0x81, n)
    elif n < 65536:
        header = struct.pack("!BBH", 0x81, 126, n)
    else:
        header = struct.pack("!BBQ", 0x81, 127, n)
    return header + payload


class _Handler(BaseHTTPRequestHandler):
    hub: StateHub = None            # 由 Server 注入
    debug_default: bool = False

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):     # 静音默认访问日志
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/ws":
            self._handle_ws()
        elif path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/app.js":
            self._serve_file("app.js", "application/javascript; charset=utf-8")
        elif path == "/style.css":
            self._serve_file("style.css", "text/css; charset=utf-8")
        elif path == "/state":
            body = (self.hub.last() if self.hub else "{}").encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body)
        elif path == "/health":
            self._respond(200, "text/plain; charset=utf-8", b"ok")
        else:
            self._respond(404, "text/plain; charset=utf-8", b"not found")

    # ------------------------------------------------------------------
    def _serve_file(self, name: str, ctype: str) -> None:
        fp = os.path.join(_WEB_DIR, name)
        if not os.path.isfile(fp):
            self._respond(404, "text/plain; charset=utf-8",
                          f"missing {name}".encode())
            return
        with open(fp, "rb") as f:
            body = f.read()
        self._respond(200, ctype, body)

    def _respond(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _handle_ws(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            self._respond(400, "text/plain; charset=utf-8", b"not a websocket")
            return
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        sock = self.connection
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self.hub.add(sock)
        log.info("浏览器接入 (当前 %d 个)", self.hub.client_count())
        # 立刻推一次当前状态
        try:
            sock.sendall(_encode_text_frame(self.hub.last()))
        except Exception:
            pass
        # 阻塞读, 用于感知断开(浏览器不发消息)
        try:
            while True:
                data = sock.recv(1024)
                if not data:
                    break
                # 收到 close 帧(0x88)则退出
                if data[:1] == b"\x88":
                    break
        except Exception:
            pass
        finally:
            self.hub.remove(sock)
            log.info("浏览器断开 (当前 %d 个)", self.hub.client_count())


class RenderServer:
    def __init__(self, hub: StateHub, host: str, port: int):
        self.hub = hub
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler = type("Handler", (_Handler,), {"hub": self.hub})
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as e:
            raise RuntimeError(
                f"无法绑定 {self.host}:{self.port} —— 端口可能被占用 ({e})"
            ) from e
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True, name="render-server")
        self._thread.start()
        log.info("渲染服务已启动: http://%s:%d/", self.host, self.port)

    def stop(self) -> None:
        if self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
