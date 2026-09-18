#!/usr/bin/env python
# coding: utf-8
"""
抖音直播弹幕抓取 —— 只抓弹幕, 落库 JSONL。

用法:
    uv run danmaku.py <live_id> [--out danmaku.jsonl] [--all]

    live_id: 直播间号, 即 https://live.douyin.com/813110862078 里的 813110862078
    --out:   输出文件, 默认 danmaku.jsonl
    --all:   同时抓礼物/进场/点赞/关注/表情, 默认只抓弹幕

设计:
    - 子类化 DouyinLiveWebFetcher, 覆盖 _parseXxxMsg, 不动原库源码
    - 只保留需要的消息类型, 其余静音(修掉 RoomRankMessage 刷屏)
    - 静音心跳包/连接提示的 print, 避免日志噪音
    - 每条消息立即 flush, 方便实时 tail
    - 断线自动重连
"""

import argparse
import contextlib
import gzip
import io
import json
import os
import sys
import threading
import time
from datetime import datetime

import websocket  # noqa: E402

# 原库整体放在 vendor/douyin_fetcher/ (liveMan.py 依赖同级的
# ac_signature / protobuf / *.js, 不能拆散), 这里把它加进搜索路径。
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor", "douyin_fetcher")
sys.path.insert(0, _VENDOR)

from liveMan import DouyinLiveWebFetcher  # noqa: E402
import liveMan  # noqa: E402

# 原库 generateSignature() 的 script_file 默认是相对名 'sign.js', 靠 cwd 找。
# 我们把原库挪进了 vendor/, 所以这里换成绝对路径, 免得改变进程 cwd。
_orig_generate_signature = liveMan.generateSignature


def _generate_signature_abs(wss, script_file=os.path.join(_VENDOR, "sign.js")):
    return _orig_generate_signature(wss, script_file)


liveMan.generateSignature = _generate_signature_abs

from protobuf.douyin import (  # noqa: E402
    ChatMessage,
    GiftMessage,
    LikeMessage,
    MemberMessage,
    SocialMessage,
    EmojiChatMessage,
    ControlMessage,
    PushFrame,
    Response,
)


@contextlib.contextmanager
def _suppress_stdout():
    """吞掉原库的 print 噪音(心跳/连接提示/RoomRank 刷屏)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield


class DanmakuFetcher(DouyinLiveWebFetcher):
    """只把需要的事件写成 JSONL, 其余丢弃。"""

    def __init__(self, live_id, out_path, keep_all=False,
                 interaction_enabled=False):
        super().__init__(live_id, abogus_file=os.path.join(_VENDOR, "a_bogus.js"))
        self.out_path = out_path
        self.keep_all = keep_all
        #: Step 11: Like/Gift 是否**解析并交给业务回调**。
        #: 与 `keep_all` 解耦 —— 见 `story.config` 里那条注释。
        #: 这个基类自己没有业务回调, 它是给 `CallbackFetcher` 用的开关。
        self.interaction_enabled = interaction_enabled
        self._fp = None
        self._counts = {}
        #: **永久终止**标志 —— 见 `terminate()`。与 `stop()` 是两件事:
        #: `stop()` 只关当前 socket(允许重连), 这个置位后 `start()` 的
        #: 重连循环**不再**继续。
        self._terminated = threading.Event()

    # ---- 落库 ----
    def _emit(self, kind, user_id=None, user_name=None, content=None, extra=None):
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "user_id": str(user_id) if user_id is not None else None,
            "user_name": user_name,
            "content": content,
        }
        if extra:
            rec.update(extra)
        self._fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fp.flush()
        self._counts[kind] = self._counts.get(kind, 0) + 1
        print(f"[{kind}] {user_name}: {content}" if content
              else f"[{kind}] {user_name}", flush=True)

    # ---- Step 11: 业务钩子(基类 no-op, 由 CallbackFetcher 覆盖) ----
    def _on_like(self, msg):
        """一条点赞。基类什么都不做 —— 只落库的用法不受影响。"""

    def _on_gift(self, msg):
        """一个礼物。基类什么都不做。"""

    # ---- 覆盖消息解析 ----
    def _parseChatMsg(self, payload):
        m = ChatMessage().parse(payload)
        self._emit("chat", m.user.id, m.user.nick_name, m.content)

    def _parseGiftMsg(self, payload):
        # Step 11: 同 `_parseLikeMsg` —— 业务与落库解耦。
        if not (self.keep_all or self.interaction_enabled):
            return
        m = GiftMessage().parse(payload)
        if self.keep_all:
            self._emit("gift", m.user.id, m.user.nick_name,
                       f"{m.gift.name}x{m.combo_count}",
                       {"gift_name": m.gift.name,
                        "gift_id": str(getattr(m, "gift_id", "") or ""),
                        "combo_count": m.combo_count,
                        "repeat_count": getattr(m, "repeat_count", 0),
                        "total_count": getattr(m, "total_count", 0)})
        self._on_gift(m)

    def _parseLikeMsg(self, payload):
        # Step 11: 业务开关与落库开关**分开**。想收礼物不该被迫开全量落库。
        if not (self.keep_all or self.interaction_enabled):
            return
        m = LikeMessage().parse(payload)
        # 落库仍只在 keep_all 时做(那是存储策略, 归 keep_all 管)。
        if self.keep_all:
            self._emit("like", m.user.id, m.user.nick_name, None,
                       {"count": m.count, "total": m.total,
                        "msg_id": str(getattr(getattr(m, "common", None),
                                              "msg_id", 0) or "")})
        self._on_like(m)

    def _parseMemberMsg(self, payload):
        if not self.keep_all:
            return
        m = MemberMessage().parse(payload)
        self._emit("member", m.user.id, m.user.nick_name)

    def _parseSocialMsg(self, payload):
        if not self.keep_all:
            return
        m = SocialMessage().parse(payload)
        self._emit("social", m.user.id, m.user.nick_name, "关注了主播")

    def _parseEmojiChatMsg(self, payload):
        if not self.keep_all:
            return
        m = EmojiChatMessage().parse(payload)
        u = getattr(m, "user", None)
        self._emit("emoji", getattr(u, "id", None),
                   getattr(u, "nick_name", None),
                   getattr(m, "default_content", None))

    # 明确静音, 避免父类 print 整坨 protobuf
    def _parseRankMsg(self, payload):        # 注意: 父类方法名是 _parseRankMsg
        pass

    def _parseRoomStatsMsg(self, payload):
        pass

    def _parseRoomUserSeqMsg(self, payload):
        pass

    def _parseFansclubMsg(self, payload):
        pass

    def _parseRoomMsg(self, payload):
        pass

    def _parseRoomStreamAdaptationMsg(self, payload):
        pass

    def _parseControlMsg(self, payload):
        m = ControlMessage().parse(payload)
        if m.status == 3:
            print(">>> 直播间已结束", flush=True)
            # 下播是**终止**: 只 `stop()` 的话重连循环下一轮又会连上,
            # 抖音会把同一批弹幕重发 —— 见 `terminate()` 的说明。
            with _suppress_stdout():
                self.terminate()

    # ---- 覆盖消息分发: 去掉噪音类型 + 让异常可见 ----
    def _wsOnMessage(self, ws, message):
        package = PushFrame().parse(message)
        response = Response().parse(gzip.decompress(package.payload))

        if response.need_ack:
            ack = PushFrame(
                log_id=package.log_id,
                payload_type="ack",
                payload=response.internal_ext.encode("utf-8"),
            ).SerializeToString()
            ws.send(ack, websocket.ABNF.OPCODE_BINARY)

        handlers = {
            "WebcastChatMessage": self._parseChatMsg,
            "WebcastControlMessage": self._parseControlMsg,
        }
        if self.keep_all:
            handlers.update({
                "WebcastGiftMessage": self._parseGiftMsg,
                "WebcastLikeMessage": self._parseLikeMsg,
                "WebcastMemberMessage": self._parseMemberMsg,
                "WebcastSocialMessage": self._parseSocialMsg,
                "WebcastEmojiChatMessage": self._parseEmojiChatMsg,
            })

        for msg in response.messages_list:
            fn = handlers.get(msg.method)
            if fn is None:
                continue  # 排行榜/统计/心跳等一律忽略
            try:
                fn(msg.payload)
            except Exception as e:
                # 不再静默吞异常, 便于排障
                print(f"!!! 解析 {msg.method} 失败: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)

    # ---- 静音父类噪音(心跳/连接提示/关闭) ----
    def _wsOnOpen(self, ws):
        print(">>> WebSocket 已连接", flush=True)
        threading.Thread(target=self._sendHeartbeat, daemon=True).start()

    def _sendHeartbeat(self):
        while True:
            try:
                hb = PushFrame(payload_type="hb").SerializeToString()
                self.ws.send(hb, websocket.ABNF.OPCODE_PING)
            except Exception:
                break
            time.sleep(5)

    def _wsOnError(self, ws, error):
        print(f"!!! WebSocket error: {error}", file=sys.stderr, flush=True)

    def _wsOnClose(self, ws, *args):
        # 父类会调 get_room_status()(需 a_bogus, 易失败/被风控), 这里不调
        print(">>> WebSocket 已断开", file=sys.stderr, flush=True)

    # ---- 生命周期 ----
    def terminate(self) -> None:
        """**永久终止** —— 置位后 `start()` 的重连循环不再继续。

        与 `stop()` 的区别(这个区别很关键, 不要合并):

        - `stop()` 语义是"**中止本次连接**" —— 它只做 `self.ws.close()`,
          让当前 `run_forever()` 返回, 外层重连循环随后照常重连。
          原库自己在错误路径上就调它(`liveMan.py` 的 `_connectWebSocket`
          except 分支), 所以**绝不能**把它改成永久退出: 否则一次普通
          网络抖动就会让弹幕永远不再重连。
        - `terminate()` 语义是"**结束这个抓取器**" —— 下播(status=3)、
          watchdog 淘汰、进程正常退出时用。

        `terminate()` 内部仍会调一次 `stop()`: 光置标志位只能让循环在
        **下一轮**发现, 当前那次 `run_forever()` 还阻塞在 socket 上,
        必须关掉它才能立刻返回。
        """
        self._terminated.set()
        try:
            self.stop()          # 关掉当前 socket, 让 run_forever 立即返回
        except Exception:
            pass

    def start(self):
        self._fp = open(self.out_path, "a", encoding="utf-8")
        delay = 3
        warned = False
        try:
            # 用 `_terminated.is_set()` 而不是 `while True`: 否则下播之后
            # `stop()` 关掉 socket -> run_forever 返回 -> 睡几秒 -> **又连**,
            # 抖音会把同一批弹幕原样重发, 一次下播变成无限重连 + 无限重放。
            while not self._terminated.is_set():
                err = None
                try:
                    super().start()
                except KeyboardInterrupt:
                    raise
                except AttributeError as e:
                    # 原库在 room_id 解析失败时抛: 'NoneType' has no attribute 'group'
                    # 通常是: 房间号填错 / 未开播 / 页面改版
                    err = ("无法解析房间信息", e)
                except Exception as e:
                    err = ("连接异常", e)

                if err is not None:
                    kind, e = err
                    if not warned:
                        print(f"\n!!! {kind}: {type(e).__name__}: {e}", flush=True)
                        if "NoneType" in str(e):
                            print(f"    房间号 {self.live_id} 解析失败 —— 请确认:",
                                  flush=True)
                            print("      1) 房间号是否正确(见直播间链接末尾的数字)", flush=True)
                            print("      2) 主播是否正在开播", flush=True)
                            print("    未开播时会持续等待, 开播后自动连上。", flush=True)
                            print("    (如不需要等待, 按 Ctrl-C 退出)\n", flush=True)
                        warned = True
                else:
                    print(">>> 连接已关闭", flush=True)

                # 退避也要可中断: 否则下播后最长还要干等 60 秒才退出,
                # 期间日志看起来像"卡住了"。`wait()` 会被 `terminate()` 立刻唤醒。
                if self._terminated.wait(delay):
                    break
                delay = min(int(delay * 1.5), 60)   # 退避, 最多 60 秒
                if warned and delay == 60:
                    print(f">>> 已等待中, 每 60 秒重试一次...", flush=True)
        except KeyboardInterrupt:
            print("\n>>> 收到 Ctrl-C, 退出", flush=True)
        finally:
            if self._fp:
                self._fp.close()
            total = sum(self._counts.values())
            print(f">>> 共写入 {total} 条 -> {self.out_path}", flush=True)
            if self._counts:
                print(">>> 分类: " + ", ".join(
                    f"{k}={v}" for k, v in sorted(self._counts.items())), flush=True)


def main():
    ap = argparse.ArgumentParser(description="抖音直播弹幕抓取(仅弹幕/可选全部)")
    ap.add_argument("live_id", help="直播间号, 如 813110862078")
    ap.add_argument("--out", default=os.path.join("data", "danmaku.jsonl"),
                    help="输出 JSONL 文件, 默认为 data/danmaku.jsonl")
    ap.add_argument("--all", action="store_true",
                    help="同时抓礼物/进场/点赞/关注/表情")
    args = ap.parse_args()

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    print(f">>> 直播间 {args.live_id} -> {out}", flush=True)
    DanmakuFetcher(args.live_id, out, keep_all=args.all).start()


if __name__ == "__main__":
    main()
