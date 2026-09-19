#!/usr/bin/python
# coding:utf-8

# @FileName:    liveMan.py
# @Time:        2024/1/2 21:51
# @Author:      bubu
# @Project:     douyinLiveWebFetcher

import codecs
import gzip
import hashlib
import random
import re
import string
import subprocess
import threading
import time
import execjs
import urllib.parse
from contextlib import contextmanager
from unittest.mock import patch

import requests
import websocket
from py_mini_racer import MiniRacer

from ac_signature import get__ac_signature
from ws_bootstrap import generate_ws_bootstrap
from protobuf.douyin import *

from urllib3.util.url import parse_url


def execute_js(js_file: str):
    """
    执行 JavaScript 文件
    :param js_file: JavaScript 文件路径
    :return: 执行结果
    """
    with open(js_file, 'r', encoding='utf-8') as file:
        js_code = file.read()
    
    ctx = execjs.compile(js_code)
    return ctx


@contextmanager
def patched_popen_encoding(encoding='utf-8'):
    original_popen_init = subprocess.Popen.__init__
    
    def new_popen_init(self, *args, **kwargs):
        kwargs['encoding'] = encoding
        original_popen_init(self, *args, **kwargs)
    
    with patch.object(subprocess.Popen, '__init__', new_popen_init):
        yield


def generateSignature(wss, script_file='sign.js'):
    """
    出现gbk编码问题则修改 python模块subprocess.py的源码中Popen类的__init__函数参数encoding值为 "utf-8"
    """
    params = ("live_id,aid,version_code,webcast_sdk_version,"
              "room_id,sub_room_id,sub_channel_id,did_rule,"
              "user_unique_id,device_platform,device_type,ac,"
              "identity").split(',')
    wss_params = urllib.parse.urlparse(wss).query.split('&')
    wss_maps = {i.split('=')[0]: i.split("=")[-1] for i in wss_params}
    tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
    param = ','.join(tpl_params)
    md5 = hashlib.md5()
    md5.update(param.encode())
    md5_param = md5.hexdigest()
    
    with codecs.open(script_file, 'r', encoding='utf8') as f:
        script = f.read()
    
    ctx = MiniRacer()
    ctx.eval(script)
    
    try:
        signature = ctx.call("get_sign", md5_param)
        return signature
    except Exception as e:
        print(e)
    
    # 以下代码对应js脚本为sign_v0.js
    # context = execjs.compile(script)
    # with patched_popen_encoding(encoding='utf-8'):
    #     ret = context.call('getSign', {'X-MS-STUB': md5_param})
    # return ret.get('X-Bogus')


def generateMsToken(length=182):
    """
    产生请求头部cookie中的msToken字段，其实为随机的107位字符
    :param length:字符位数
    :return:msToken
    """
    random_str = ''
    base_str = string.ascii_letters + string.digits + '-_'
    _len = len(base_str) - 1
    for _ in range(length):
        random_str += base_str[random.randint(0, _len)]
    return random_str


class DouyinLiveWebFetcher:
    
    def __init__(self, live_id, abogus_file='a_bogus.js',
                 login_cookie=None):
        """
        直播间弹幕抓取对象
        :param live_id: 直播间的直播id，打开直播间web首页的链接如：https://live.douyin.com/261378947940，
                        其中的261378947940即是live_id
        :param login_cookie: 登录态 Cookie 串(可选)。**不要**从命令行传入,
                            只从环境变量经 Config 透传 —— 见 ws_cookie.py。
                            None/空 = 游客态(与历史行为等价)。
        """
        self.abogus_file = abogus_file
        self.__ttwid = None
        self.__room_id = None
        #: WS handshake 的登录态 Cookie(凭据!)。绝不打日志/进异常/落库。
        self._login_cookie = login_cookie or None
        #: WS handshake 用的匿名身份 cookie 链。**惰性**获取: `ttwid` 属性
        #: 走网络(见下), 而 `__ac_nonce` / `__ac_signature` 上游只有在
        #: `build_webcast_url()` 那条路径上才取。连接时若还没取过, 就现取
        #: 一次; 取失败只是少两个字段, 不该让连接构建炸掉。
        #: 缓存住是因为 `__ac_nonce` 本来就是会话级的, 每次连接重新取会多
        #: 打一次 HTTP, 而且会让 A/B 多出一个非受控变量。
        self._ws_ac_nonce = None
        self._ws_ac_signature = None
        #: WS bootstrap 状态(动态获取)。None = 还没取过。
        #: 见 `_fetch_bootstrap_state`。
        self.__bootstrap = None
        #: 原来这个值是以字面量硬编码在 WS URL 里的; 现在 bootstrap 也要用,
        #: 提到一处定义, 避免两处各写一份(改一处忘一处)。
        self.user_unique_id = "7319483754668557238"
        self.session = requests.Session()
        self.live_id = live_id
        self.host = "https://www.douyin.com/"
        self.live_url = "https://live.douyin.com/"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
        self.headers = {
            'User-Agent': self.user_agent
        }
    
    def start(self):
        self._connectWebSocket()
    
    def stop(self):
        self.ws.close()
    
    @property
    def ttwid(self):
        """
        产生请求头部cookie中的ttwid字段，访问抖音网页版直播间首页可以获取到响应cookie中的ttwid
        :return: ttwid
        """
        if self.__ttwid:
            return self.__ttwid
        headers = {
            "User-Agent": self.user_agent,
        }
        try:
            response = self.session.get(self.live_url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            print("【X】Request the live url error: ", err)
        else:
            self.__ttwid = response.cookies.get('ttwid')
            return self.__ttwid
    
    @property
    def room_id(self):
        """
        根据直播间的地址获取到真正的直播间roomId，有时会有错误，可以重试请求解决
        :return:room_id
        """
        if self.__room_id:
            return self.__room_id
        url = self.live_url + self.live_id
        headers = {
            "User-Agent": self.user_agent,
            "cookie": f"ttwid={self.ttwid}&msToken={generateMsToken()}; __ac_nonce=0123407cc00a9e438deb4",
        }
        try:
            response = self.session.get(url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            print("【X】Request the live room url error: ", err)
        else:
            match = re.search(r'roomId\\":\\"(\d+)\\"', response.text)
            if match is None or len(match.groups()) < 1:
                print("【X】No match found for roomId")
            
            self.__room_id = match.group(1)
            
            return self.__room_id
    
    def get_ac_nonce(self):
        """
        获取 __ac_nonce
        """
        resp_cookies = self.session.get(self.host, headers=self.headers).cookies
        return resp_cookies.get("__ac_nonce")
    
    def get_ac_signature(self, __ac_nonce: str = None) -> str:
        """
        获取 __ac_signature
        """
        __ac_signature = get__ac_signature(self.host[8:], __ac_nonce, self.user_agent)
        self.session.cookies.set("__ac_signature", __ac_signature)
        return __ac_signature
    
    def get_a_bogus(self, url_params: dict):
        """
        获取 a_bogus
        """
        url = urllib.parse.urlencode(url_params)
        ctx = execute_js(self.abogus_file)
        _a_bogus = ctx.call("get_ab", url, self.user_agent)
        return _a_bogus
    
    def get_room_status(self):
        """
        获取直播间开播状态:
        room_status: 2 直播已结束
        room_status: 0 直播进行中
        """
        msToken = generateMsToken()
        nonce = self.get_ac_nonce()
        signature = self.get_ac_signature(nonce)
        url = ('https://live.douyin.com/webcast/room/web/enter/?aid=6383'
               '&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&enter_from=page_refresh'
               '&cookie_enabled=true&screen_width=5120&screen_height=1440&browser_language=zh-CN&browser_platform=Win32'
               '&browser_name=Edge&browser_version=140.0.0.0'
               f'&web_rid={self.live_id}'
               f'&room_id_str={self.room_id}'
               '&enter_source=&is_need_double_stream=false&insert_task_id=&live_reason=&msToken=' + msToken)
        query = parse_url(url).query
        params = {i[0]: i[1] for i in [j.split('=') for j in query.split('&')]}
        a_bogus = self.get_a_bogus(params)  # 计算a_bogus,成功率不是100%，出现失败时重试即可
        url += f"&a_bogus={a_bogus}"
        headers = self.headers.copy()
        headers.update({
            'Referer': f'https://live.douyin.com/{self.live_id}',
            'Cookie': f'ttwid={self.ttwid};__ac_nonce={nonce}; __ac_signature={signature}',
        })
        resp = self.session.get(url, headers=headers)
        data = resp.json().get('data')
        if data:
            room_status = data.get('room_status')
            user = data.get('user')
            user_id = user.get('id_str')
            nickname = user.get('nickname')
            print(f"【{nickname}】[{user_id}]直播间：{['正在直播', '已结束'][bool(room_status)]}.")
    
    def _local_bootstrap(self, now_ms: int) -> dict:
        """本地生成 cursor / internal_ext(Step 12B 的单变量实验)。

        包装 `ws_bootstrap.generate_ws_bootstrap`, 注入 room / 身份 / 时钟。
        **只做这一件事** —— 不碰任何其他连接参数。
        """
        return generate_ws_bootstrap(
            room_id=self.room_id,
            user_unique_id=self.user_unique_id,
            now_ms=now_ms)

    def _fetch_bootstrap_state(self):
        """⚠️ **实验/诊断用, 不再是生产路径**(Step 12B 起)。

        实测在本项目环境里, `/webcast/im/fetch/` **恒返回 HTTP 200 + 空
        body**(试过 protobuf/json/不带 resp_content_type、空 cursor、
        d-1 cursor、带 internal_src, 全部 len=0), 返回头还是
        `application/json` —— 说明请求被接受但服务端不按我们要求回数据,
        很可能是 `a_bogus` 的签名范围与该 endpoint 校验的参数集不一致。

        而外部当前实现提供了**完全不经它**的本地生成路径
        (`ws_bootstrap.generate_ws_bootstrap`), 所以生产改走那条。
        本函数保留下来只为将来诊断/对照, **不再作为 dynamic 的前置条件**。

        原说明: 连接 WS 之前, 取本次直播的 cursor / internal_ext。

        ## 为什么必须动态取

        原来 `_connectWebSocket()` 里的这组值全是写死的 2024-07 状态:

            cursor=d-1_u-1_fh-...t-1721106114633_r-1
            |first_req_ms:1721106114541|fetch_time:1721106114633|...
            wrds_v:7392094459690748497

        (1721106114633 ms ~ 2024-07-16)。`Response` 自带
        `cursor`(:2) / `internalExt`(:5) / `liveCursor`(:11), 说明这是一套
        **会话状态**, 本该由服务端在响应里推进 —— 写死几个月前的值等于
        告诉服务端"我要从那时开始收"。

        ## 为什么走 /webcast/im/fetch/ 而不是 /webcast/room/web/enter/

        我第一版从 `/webcast/room/web/enter/` 的 JSON 里翻
        `data.room.cursor`, 那是**猜** JSON 层级 —— 本地测试只能证明
        "如果它返回这两个字段, 我们会用", **没有**证明真实响应真的带。

        同源外部实现的做法是:

            GET /webcast/im/fetch/  (resp_content_type=protobuf)
              -> 解析 **LiveResponse protobuf**
              -> frame.cursor / frame.internalExt
              -> 用于 WebSocket URL

        我们自己的 `Response` 就是那个结构(cursor/internalExt/liveCursor
        都在), 所以**不需要引入新的解析结构**, 直接复用。

        ## 失败即回退

        拿不到就返回 None, 调用方回退旧常量 —— 不会因为这个改动连不上。
        """
        try:
            msToken = generateMsToken()
            nonce = self.get_ac_nonce()
            signature = self.get_ac_signature(nonce)
            url = ('https://live.douyin.com/webcast/im/fetch/?aid=6383'
                   '&app_name=douyin_web&live_id=1&device_platform=web'
                   '&language=zh-CN&enter_from=page_refresh'
                   '&cookie_enabled=true&screen_width=1536&screen_height=864'
                   '&browser_language=zh-CN&browser_platform=Win32'
                   '&browser_name=Mozilla'
                   '&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)'
                   '%20AppleWebKit/537.36%20(KHTML,%20like%20Gecko)'
                   '%20Chrome/126.0.0.0%20Safari/537.36'
                   '&browser_online=true&tz_name=Asia/Shanghai'
                   '&cursor=&internal_ext='
                   '&host=https://live.douyin.com&aid=6383&live_id=1'
                   '&did_rule=3&endpoint=live_pc&support_wrds=1'
                   f'&user_unique_id={self.user_unique_id}'
                   '&im_path=/webcast/im/fetch/&identity=audience'
                   '&need_persist_msg_count=15&insert_task_id=&live_reason='
                   f'&room_id={self.room_id}&heartbeatDuration=0'
                   '&resp_content_type=protobuf&version_code=180800'
                   '&webcast_sdk_version=1.0.14-beta.0'
                   '&update_version_code=1.0.14-beta.0&compress=gzip'
                   '&msToken=' + msToken)
            query = parse_url(url).query
            params = {i[0]: i[1] for i in [j.split('=') for j in query.split('&')]}
            a_bogus = self.get_a_bogus(params)
            url += f"&a_bogus={a_bogus}"
            headers = self.headers.copy()
            headers.update({
                'Referer': f'https://live.douyin.com/{self.live_id}',
                'Cookie': f'ttwid={self.ttwid};__ac_nonce={nonce}; '
                          f'__ac_signature={signature}',
                'Accept': 'application/x-protobuf',
            })
            resp = self.session.get(url, headers=headers)
            body = resp.content or b""
            if not body:
                print("【bootstrap】/im/fetch 返回空 body, 回退旧常量")
                return None
            # push 路径的 body 是 "PushFrame(内含 gzip 的 Response)"。
            # fetch 直接回 Response; 这里两种都试, 免得依赖具体一种。
            frame = None
            for parse in ("direct", "pushframe"):
                try:
                    if parse == "direct":
                        frame = Response().parse(body)
                    else:
                        pkg = PushFrame().parse(body)
                        if pkg.payload:
                            frame = Response().parse(gzip.decompress(pkg.payload))
                    if frame is not None and (frame.cursor or frame.internal_ext):
                        break
                except Exception:
                    frame = None
            if frame is None:
                print("【bootstrap】/im/fetch body 解析不出 Response, 回退旧常量")
                return None
            cursor = frame.cursor or ""
            # ⚠️ **只接受 internal_ext**。`liveCursor`(:11) 是**另一个字段**,
            # 外部可行实现也只用 internalExt。把 liveCursor 塞进
            # `internal_ext=` 会凭空造一个我们没有证据支持的协议假设。
            # 它可以被采样记录(供 12B 观察), 但不能冒充。
            internal_ext = frame.internal_ext or ""
            if frame.live_cursor:
                print(f"【bootstrap】观察到 live_cursor(仅记录, 不冒充 "
                      f"internal_ext): {str(frame.live_cursor)[:40]}")
            # ---- 严格要求**两者同时**存在 ----
            # 只有一个时, 另一个会悄悄用 2024 fallback -> URL 变成新旧混搭。
            # 那种连接不能算 dynamic, 也不能拿来比较 A/B。
            if not (cursor and internal_ext):
                print(f"【bootstrap】cursor/internal_ext 不齐 "
                      f"(cursor={'有' if cursor else '无'}, "
                      f"internal_ext={'有' if internal_ext else '无'}) "
                      f"-> 整组回退, 本次不算 dynamic")
                return None
            print(f"【bootstrap】cursor/internal_ext 取自 /im/fetch "
                  f"(cursor={'有' if cursor else '无'}, "
                  f"internal_ext={'有' if internal_ext else '无'})")
            return {"cursor": str(cursor), "internal_ext": str(internal_ext)}
        except Exception as err:
            print(f"【bootstrap】取 bootstrap 状态失败, 回退旧常量: {err}")
            return None

    def _build_ws_cookie_header(self) -> str:
        """组装 WS handshake 的 Cookie header 值(匿名 / 登录态)。

        为什么放在传输层: 这是**认证职责**。Engine / Director 不该知道
        "cookie 长什么样", 它们只负责把"有没有登录态"传下来。

        匿名链保留 ttwid / __ac_nonce / __ac_signature —— 这三个是服务端
        认身份用的字段。登录态存在时, 登录 cookie 里的同名项**优先**
        (例如登录 cookie 自带 ttwid 时, 用登录的那个, 不产生重复 name)。

        `__ac_nonce` / `__ac_signature` 是**惰性**取的: 只在还没取过时现取
        一次并缓存。取失败静默降级(少这两个字段), 不让连接构建抛异常 ——
        否则一个签名端点抽风就能让整场直播连不上, 比"认证不完整"糟得多。

        ⚠️ 返回值是凭据, 只交给 WebSocketApp; 不要打日志/进异常/落库。
        """
        from ws_cookie import build_ws_cookie_header
        if self._ws_ac_nonce is None:
            try:
                self._ws_ac_nonce = self.get_ac_nonce()
            except Exception:
                self._ws_ac_nonce = ""
        if self._ws_ac_signature is None:
            try:
                self._ws_ac_signature = self.get_ac_signature(self._ws_ac_nonce)
            except Exception:
                self._ws_ac_signature = ""
        base = {
            "ttwid": self.ttwid,
            "__ac_nonce": self._ws_ac_nonce,
            "__ac_signature": self._ws_ac_signature,
        }
        return build_ws_cookie_header(base, self._login_cookie)

    def _connectWebSocket(self):
        """
        连接抖音直播间websocket服务器，请求直播间数据
        """
        # ---- WS bootstrap 状态: 优先**动态取**, 取不到才回退旧常量 ----
        #
        # 原来这里写死了 2024-07 的 cursor/internal_ext(wrds_v 等), 等于
        # 告诉服务端"从几个月前开始收"。`Response` 自带
        # cursor(:2)/internalExt(:5)/liveCursor(:11), 说明这是会话状态,
        # 本该由服务端在响应里推进。
        #
        # 回退值是**保命**用的: 万一详情接口拿不到状态, 也还能连上(至少
        # 与改动前行为一致), 不会因为这个改动把直播搞挂。
        # ---- Step 12B: 本地生成 bootstrap(单变量实验) ----
        #
        # 换掉那组**写死的 2024-07 值**(`t-1721106114633`)。旧值能连上,
        # 能收 chat/member/like/social, 但**收不到 Gift**。
        #
        # 本地生成的做法来自外部当前实现(JaneEyre3007/douyin-js 的
        # `genCursorInternalExt`)—— 它不经任何 HTTP bootstrap, 直接取
        # `Date.now()` 构造这两个串。见 `ws_bootstrap.py` 的说明。
        #
        # ⚠️ 这是**单变量实验**: 只有 cursor/internal_ext 的来源变了,
        # room / WS host / signature / handler / proto / 礼物操作全不动。
        #
        # 每次连接重新生成(时间戳本来就该是新的; 不做跨连接缓存 —— 缓存会
        # 让"重连拿到新值"这件事失效, 也会让实验结果真假难辨)。
        now_ms = int(time.time() * 1000)
        boot = self._local_bootstrap(now_ms)
        self.__bootstrap = boot
        mode = "local-generated"
        print(f"【bootstrap】本次连接使用 {mode} now_ms={now_ms}"
              f"   <<< B-smoke 有效性判据(应为 local-generated)", flush=True)
        cursor = boot["cursor"]
        internal_ext = boot["internal_ext"]
        wss = ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               f"&cursor={cursor}"
               f"&internal_ext={internal_ext}"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id={self.user_unique_id}&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0")

        signature = generateSignature(wss)
        wss += f"&signature={signature}"

        headers = {
            "cookie": self._build_ws_cookie_header(),
            'user-agent': self.user_agent,
        }
        self.ws = websocket.WebSocketApp(wss,
                                         header=headers,
                                         on_open=self._wsOnOpen,
                                         on_message=self._wsOnMessage,
                                         on_error=self._wsOnError,
                                         on_close=self._wsOnClose)
        try:
            self.ws.run_forever()
        except Exception:
            self.stop()
            raise
    
    def _sendHeartbeat(self):
        """
        发送心跳包
        """
        while True:
            try:
                heartbeat = PushFrame(payload_type='hb').SerializeToString()
                self.ws.send(heartbeat, websocket.ABNF.OPCODE_PING)
                print("【√】发送心跳包")
            except Exception as e:
                print("【X】心跳包检测错误: ", e)
                break
            else:
                time.sleep(5)
    
    def _wsOnOpen(self, ws):
        """
        连接建立成功
        """
        print("【√】WebSocket连接成功.")
        threading.Thread(target=self._sendHeartbeat).start()
    
    def _wsOnMessage(self, ws, message):
        """
        接收到数据
        :param ws: websocket实例
        :param message: 数据
        """
        
        # 根据proto结构体解析对象
        package = PushFrame().parse(message)
        response = Response().parse(gzip.decompress(package.payload))
        
        # 返回直播间服务器链接存活确认消息，便于持续获取数据
        if response.need_ack:
            ack = PushFrame(log_id=package.log_id,
                            payload_type='ack',
                            payload=response.internal_ext.encode('utf-8')
                            ).SerializeToString()
            ws.send(ack, websocket.ABNF.OPCODE_BINARY)
        
        # 根据消息类别解析消息体
        for msg in response.messages_list:
            method = msg.method
            try:
                {
                    'WebcastChatMessage': self._parseChatMsg,  # 聊天消息
                    'WebcastGiftMessage': self._parseGiftMsg,  # 礼物消息
                    'WebcastLikeMessage': self._parseLikeMsg,  # 点赞消息
                    'WebcastMemberMessage': self._parseMemberMsg,  # 进入直播间消息
                    'WebcastSocialMessage': self._parseSocialMsg,  # 关注消息
                    'WebcastRoomUserSeqMessage': self._parseRoomUserSeqMsg,  # 直播间统计
                    'WebcastFansclubMessage': self._parseFansclubMsg,  # 粉丝团消息
                    'WebcastControlMessage': self._parseControlMsg,  # 直播间状态消息
                    'WebcastEmojiChatMessage': self._parseEmojiChatMsg,  # 聊天表情包消息
                    'WebcastRoomStatsMessage': self._parseRoomStatsMsg,  # 直播间统计信息
                    'WebcastRoomMessage': self._parseRoomMsg,  # 直播间信息
                    'WebcastRoomRankMessage': self._parseRankMsg,  # 直播间排行榜信息
                    'WebcastRoomStreamAdaptationMessage': self._parseRoomStreamAdaptationMsg,  # 直播间流配置
                }.get(method)(msg.payload)
            except Exception:
                pass
    
    def _wsOnError(self, ws, error):
        print("WebSocket error: ", error)
    
    def _wsOnClose(self, ws, *args):
        self.get_room_status()
        print("WebSocket connection closed.")
    
    def _parseChatMsg(self, payload):
        """聊天消息"""
        message = ChatMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        content = message.content
        print(f"【聊天msg】[{user_id}]{user_name}: {content}")
    
    def _parseGiftMsg(self, payload):
        """礼物消息"""
        message = GiftMessage().parse(payload)
        user_name = message.user.nick_name
        gift_name = message.gift.name
        gift_cnt = message.combo_count
        print(f"【礼物msg】{user_name} 送出了 {gift_name}x{gift_cnt}")
    
    def _parseLikeMsg(self, payload):
        '''点赞消息'''
        message = LikeMessage().parse(payload)
        user_name = message.user.nick_name
        count = message.count
        print(f"【点赞msg】{user_name} 点了{count}个赞")
    
    def _parseMemberMsg(self, payload):
        '''进入直播间消息'''
        message = MemberMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gender = ["女", "男"][message.user.gender]
        print(f"【进场msg】[{user_id}][{gender}]{user_name} 进入了直播间")
    
    def _parseSocialMsg(self, payload):
        '''关注消息'''
        message = SocialMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        print(f"【关注msg】[{user_id}]{user_name} 关注了主播")
    
    def _parseRoomUserSeqMsg(self, payload):
        '''直播间统计'''
        message = RoomUserSeqMessage().parse(payload)
        current = message.total
        total = message.total_pv_for_anchor
        print(f"【统计msg】当前观看人数: {current}, 累计观看人数: {total}")
    
    def _parseFansclubMsg(self, payload):
        '''粉丝团消息'''
        message = FansclubMessage().parse(payload)
        content = message.content
        print(f"【粉丝团msg】 {content}")
    
    def _parseEmojiChatMsg(self, payload):
        '''聊天表情包消息'''
        message = EmojiChatMessage().parse(payload)
        emoji_id = message.emoji_id
        user = message.user
        common = message.common
        default_content = message.default_content
        print(f"【聊天表情包id】 {emoji_id},user：{user},common:{common},default_content:{default_content}")
    
    def _parseRoomMsg(self, payload):
        message = RoomMessage().parse(payload)
        common = message.common
        room_id = common.room_id
        print(f"【直播间msg】直播间id:{room_id}")
    
    def _parseRoomStatsMsg(self, payload):
        message = RoomStatsMessage().parse(payload)
        display_long = message.display_long
        print(f"【直播间统计msg】{display_long}")
    
    def _parseRankMsg(self, payload):
        message = RoomRankMessage().parse(payload)
        ranks_list = message.ranks_list
        print(f"【直播间排行榜msg】{ranks_list}")
    
    def _parseControlMsg(self, payload):
        '''直播间状态消息'''
        message = ControlMessage().parse(payload)
        
        if message.status == 3:
            print("直播间已结束")
            self.stop()
    
    def _parseRoomStreamAdaptationMsg(self, payload):
        message = RoomStreamAdaptationMessage().parse(payload)
        adaptationType = message.adaptation_type
        print(f'直播间adaptation: {adaptationType}')
