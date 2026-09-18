# vendor 本地 patch 登记

`vendor/douyin_fetcher/` 是**拷进仓库的 vendored source**(不是运行时从
pip 动态安装的包)。它承载抖音 Webcast 的传输层, 有些地方必须按我们的
运行环境修正 —— 这份文件登记每一处本地改动。

## 为什么直接改 vendor 而不是在子类里 override

曾经考虑过在 `CallbackFetcher`(我们自己的子类)里复制整段
`_connectWebSocket()`。**不采用**, 因为:

- 那段代码包含 URL 拼装、参数组合、签名方式 —— 上游一改, 我们就得**维护
  两份完整实现**, 而且不会有任何信号提示我们漏跟了上游;
- 覆盖单个方法而复制它的全部内容, 是"看起来隔离、实际耦合更深"。

所以: **直接改 vendor, 并在这份文件里逐条登记**。升级 vendor 时按这份
清单逐条检查是否需要重放。

---

## patch 3 — 本地生成 WS bootstrap(当前生效路径)

**文件**: `vendor/douyin_fetcher/ws_bootstrap.py`(新增) +
`liveMan.py::_local_bootstrap` / `_connectWebSocket`

**改动**: 新增纯函数 `generate_ws_bootstrap(room_id, user_unique_id,
now_ms, rng)`, 按外部当前实现的做法**在本地**构造 `cursor` /
`internal_ext`(以 `sec << 32` 为高位 + 随机低位, 时间戳取当前毫秒)。
`_connectWebSocket()` 每次连接重新生成并使用它。

**原因**: 上游写死的 2024-07 值能连上、能收 chat/member/like/social, 但
**收不到 Gift**。见 patch 1 的对照说明。

**证据来源与边界**: 做法来自外部实现 `JaneEyre3007/douyin-js` 的
`genCursorInternalExt()`(其 README 称"本地还原 signature / cursor /
internal_ext, 直接连接 WebSocket")。
⚠️ 这是**外部当前实现采用的做法, 不是抖音官方协议定义**。我们采用它是
为了做一个**单变量实验**: 只换 cursor/internal_ext 的来源, 其余
(room / WS host / signature / handler / proto / 礼物操作)全不变。

**升级 vendor 时**: 若上游已自带本地生成, 删除本 patch 跟随上游。

---

## patch 1 — 动态 WS bootstrap(/im/fetch, **已降级为诊断**)

**文件**: `vendor/douyin_fetcher/liveMan.py`
**改动**:
- 新增 `_fetch_bootstrap_state()`: 连接前请求
  `/webcast/im/fetch/`(protobuf), 解析 `Response.cursor`(:2) /
  `Response.internalExt`(:5) / `liveCursor`(:11)。
- `_connectWebSocket()` 每次连接**重新调用**它(不做跨连接缓存),
  失败才回退到原有的硬编码常量。
- `__init__` 里新增 `self.user_unique_id`(原先是硬编码在 WS URL 里的
  字面量, 现在 bootstrap 与 WS 两处共用, 提到一处定义)。

**原因**: 上游的 `cursor` / `internal_ext` / `first_req_ms` / `fetch_time` /
`wrds_v` 全是**写死的 2024-07 状态**(`t-1721106114633` ≈ 2024-07-16)。
那等于告诉服务端"从那时开始收"。`Response` 自带 cursor/internalExt, 说明
这是会话状态, 本该由服务端在响应里推进。

**为什么走 `/webcast/im/fetch/` 而不是 `/webcast/room/web/enter/`**:
第一版从 `/enter` 的 JSON 里翻 `data.room.cursor`, 那是**猜 JSON 层级** ——
离线测试只能证明"如果它返回这两个字段我们会用", 没证明真实响应真的带。
外部可行的同源实现走的是 `/im/fetch/` + protobuf `LiveResponse`, 而我们
自己的 `Response` proto 已经有那三个字段, 不需要新解析结构。

**⚠️ 已降级(见 patch 3)**: 实测在本项目环境里该 endpoint **恒返回
HTTP 200 + 空 body**(试过 protobuf/json/不带 resp_content_type、空
cursor、d-1 cursor、带 internal_src, 全部 len=0; 返回头还是
`application/json`)。很可能是 `a_bogus` 的签名范围与它校验的参数集不一致。

**不要继续在这里投入。** 它保留为诊断/对照代码, **不再是生产 dynamic 的
前置条件**。生产走 patch 3 的本地生成。

**上游基线**: 本仓库 vendored 的版本, 未见版本号标注。
对照实现: `cv-cat/DouYin_Spider`(master, 2026-09 观察)。
`opedium/douyin-live-proto` 的 payload reference 亦印证
Response 携带 cursor/internalExt。

**升级 vendor 时**: 检查上游是否已经自带动态 bootstrap。若有, 删除本
patch 改为跟随上游; 若无, 重放到新版 `_connectWebSocket()`。

---

## patch 2 — 原始 method 探针

**文件**: `danmaku.py`(我们自己的文件, 非 vendor; 登记在此是因为它与
patch 1 同属"B smoke 前的诊断能力")

**改动**: `_wsOnMessage()` 在 `handlers.get(msg.method)` **之前**统计
`method_counts` / `unhandled_method_counts` / `parse_error_counts`;
新增 `method_summary()` 与 `log_method_summary()`(断开时自动打一次)。
**只含方法名与计数, 不含 payload/昵称**。

**原因**: 原先 `if fn is None: continue` 是**无痕丢弃**。Gift 没落盘时
无法区分"服务端没给"与"给了但我们没注册"。探针让四种可能一眼可辨:
服务端没给 / 分发表问题 / 解析问题 / ingest 问题。

---

## 未登记即未修改

除以上两处, `vendor/douyin_fetcher/` 保持上游原样。
新增改动请在此追加 patch 条目(编号、文件、改动、原因、上游基线)。
