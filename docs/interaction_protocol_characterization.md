# 互动协议特征化(Step 12A — 外部证据)

> **这份文档不含任何真实直播采样。**
>
> 它记录的是从**我们自己仓库里的 proto** 与**外部同源实现 + 其 issue** 能
> 确认的协议事实, 以及**仍然未知**的部分。
>
> 真实采集(Step 12B)**只完成了 Like 的一半**(见 §5.5); Gift 仍是 0 样本。
> 本文档里出现的一切**观察性描述**都标注了
> 来源; 凡是没有来源的, 一律列在"仍未知"里 —— **不得**把它们补成看起来
> 像实测的样子。
>
> 配套证据文件: `tests/fixtures/interaction_protocol_external_evidence.json`
> (同样只记来源与观察, 不是 live sample)。

---

## 1. 为什么需要这一步

Step 13 要把点赞/礼物换算成 Summon。换算规则本身很简单("100 赞 = 1",
"任意真实礼物单位 = 1"), 难的是**"一个真实礼物单位"到底是什么**。

最容易写错、而且**错了不会报错**的一种实现是:

```python
每收到一条 GiftMessage:
    summon += 1
```

这个写法在真实环境里会**重复计数**。下面第 4 节的 Issue #88 是这条结论的
直接外部证据。

---

## 2. 已确认(有来源)

### 2.1 我们自己的 proto(`vendor/douyin_fetcher/protobuf/douyin.proto`)

**Like 有两个独立的计数字段** —— `LikeMessage`:

| 字段 | 编号 | 类型 |
|---|---|---|
| `common` | 1 | `Common` |
| `count` | 2 | `uint64` |
| `total` | 3 | `uint64` |
| `user` | 5 | `User` |

**Gift 有六个可能用于计量的字段** —— `GiftMessage`:

| 字段 | 编号 | 类型 |
|---|---|---|
| `giftId` | 2 | `uint64` |
| `groupCount` | 4 | `uint64` |
| `repeatCount` | 5 | `uint64` |
| `comboCount` | 6 | `uint64` |
| `repeatEnd` | 9 | `uint32` |
| `groupId` | 11 | `uint64` |
| `logId` | 16 | `string` |
| `totalCount` | 29 | `uint64` |
| `traceId` | 35 | `string` |

**两层 msg_id 是两个不同的字段**:

| 位置 | 字段 | 编号 | 类型 |
|---|---|---|---|
| 外层 envelope `Message` | `msgId` | 3 | `int64` |
| 内层 `Common`(各 payload 的 `common`) | `msgId` | 2 | `uint64` |

⚠️ 还有**第三个**容易混淆的:`Common` 自己也有 `logId`(编号 12), 与
`GiftMessage.logId`(编号 16)不是同一个东西。

> 这条直接决定了采集口径: 当前实现同时采 `envelope_msg_id` 与
> `common_msg_id`, **不假设两者相等, 也不预判哪个是幂等主键**。见
> `danmaku.py::_wsOnMessage` 与 `story/ingest.py::InteractionEvent`。

### 2.2 外部同源实现: `cv-cat/DouYin_Spider`

- 同样是 `PushFrame → gzip → Response/LiveResponse → messagesList` 的结构;
- 同样按 `WebcastGiftMessage` / `WebcastLikeMessage` 分发;
- Like 同样有 `count` / `total`;
- Gift 同样有 `giftId` / `comboCount` / `user` / `gift`;
- **它把礼物打印成 `礼物名 x comboCount`** —— 即 `comboCount` 被实际用作
  面向显示的 "xN" 数量。

**但它的 Gift proto 比我们的简化**: 它只保留 `giftId / comboCount / user /
toUser / gift`, 没有 `repeatCount / totalCount / repeatEnd / groupId /
logId / traceId`。**我们反而拥有更多做可靠计量所需的原始字段。**

### 2.3 外部 issue: `cv-cat/DouYin_Spider` Issue #88(2026-09-15)

> 报告内容: **直播间只送了一个小心心, 但程序收到了两条相同的小心心礼物消息。**

该 issue 至今 open、无作者回复。它的实现只是"识别并打印 GiftMessage",
**没有解决业务级幂等**。

这条证据独立印证了两个冻结原则:

```text
GiftMessage 数量 ≠ 真实礼物单位数
```

```text
因此绝对不能: 每收到一条 GiftMessage 就 summon += 1
```

---

## 3. 已确认的推论(由上面推出, 不是新观察)

1. **`GiftMessage`-per-unit 明确不安全** —— 依据 §2.3。
2. **`comboCount` 至少是"面向显示的数量"** —— 依据 §2.2 的实现用法。
3. **两层 msg_id 都必须采** —— 依据 §2.1; 哪一个是稳定幂等键尚无定论。

---

## 4. 仍未知(必须等真实采集 Step 12B)

以下每一条都**不能用猜测代替**, 因为猜错的后果是重复计费或漏计:

| # | 未知项 | 为什么重要 |
|---|---|---|
| 1 | `combo` / `repeat` / `total` **哪个是累计绝对值** | 决定 accumulator 用哪个字段算增量 |
| 2 | combo 更新是否 **1→2→3**(而非只报最终值) | 决定"新增单位数"能否由相邻差推出 |
| 3 | `group_id` / `trace_id` 在**一次 combo 内**是否稳定 | 决定能否用它们做 combo 级归并 |
| 4 | `envelope_msg_id` vs `common_msg_id` 的**稳定性与 replay 行为** | 决定幂等主键选哪个 |
| 5 | **非 combo 礼物**各计数字段的真实形态 | 单发礼物与连击礼物可能完全不同 |
| 6 | **重连是否重放 gift**, 以及哪些 ID 保持不变 | 决定去重窗口要多大 |

### 关于 §2.2 那条 `xN` 的边界(重要)

`comboCount` 被用作显示 "xN" **不足以证明**:

```text
comboCount == 本消息新增礼物数
```

若 combo 连击产生 `1 / 2 / 3`, 那个程序只会打印 `x1 / x2 / x3` —— 它
**根本没有判断**应该计:

```text
3            (最新值 = 累计)
还是
1+2+3 = 6    (逐步相加)
```

再加上 §2.3 的重复消息问题, **外部实现不能替我们解决 accumulator**。

---

## 5. 对实现的约束(Step 13A 起生效)

```text
允许(13A):
  - Like: 用 total 的 high-water 增量换算(算法见下)
  - SummonLedger: earn(n) / reserve / commit / release 通用机制
  - Gift: 只到 raw InteractionEvent -> 保留 / 日志 / no-op

禁止(直到 12B 完成):
  - GiftMessage 到一条 -> earned +1
  - combo_count  -> earned
  - repeat_count -> earned
  - total_count  -> earned
```

第 5 节最后四条不是"暂时简化", 而是**正确性要求** —— §2.3 已经证明
"一条消息 = 一个单位"这个前提是错的。

---

## 5.5 Step 12B 进展(真实采集, 部分完成)

### 12B-Like: **部分完成**

一次真实直播(约 75 秒, 单用户, 无重连)采到 6 条 `WebcastLikeMessage`:

| # | count | total |
|---|---|---|
| 1 | 2 | 2 |
| 2 | 11 | 13 |
| 3 | 11 | 24 |
| 4 | 13 | 37 |
| 5 | 9 | 46 |
| 6 | 5 | 51 |

**已实测(仅限这段样本)**:

```text
sum(count) == 最后一条 total == 51
total 严格单调递增
envelope_msg_id == common_msg_id (6/6)
每条 msg_id 互不相同
```

**准确结论(不要写成"Like 语义已完全确定")**:

> 在**单用户、无重连**的这段真实样本里, `count` 表现为批次增量,
> `total` 严格递增, 且 `sum(count) = 最后 total`。
> **多用户口径与重连/reset 行为仍未确认。**

⚠️ 两种解释同时兼容这组数据, 无法区分:

```text
解释 A: total 是全房间累计 —— 这段时间只有一个人在点, 所以恰好等于他的 count 和
解释 B: total 是该用户累计 —— 所以也恰好等于他的 count 和
```

**因此 13A 的实现保持不变**(单个 `Like.total` high-water)。
**不要**现在改成按 `user_id` 分桶求和: 若 `total` 本来就是全房间值,
A 到 80、B 下一条到 90 时真实总量只是 90, 按用户累加会错算成 170。

判定方法(下一次 Like smoke): **两个账号交替** ——

```text
初始静置 -> A 点一小段 -> B 点一小段 -> A 再点一小段
逐条记录 user_id / count / total
```

- 切到 B 后 `total` 继续沿 A 之后的数字增长 -> 倾向 room-wide;
- B 的 `total` 回到自己的较小累计、切回 A 又恢复 A 的累计 -> 倾向 per-user。

### 12B-Gift: **未完成**

```text
实际发送过礼物(小心心): 是
收到 WebcastGiftMessage 数量: 0
```

排查结论(措辞已收紧):

- **只能说**: 在**已识别为 `WebcastGiftMessage`** 的那条路径上, 没有落盘、
  没有解析记录 —— `gift` 零痕迹, 日志里零 gift 字样, 唯一的"解析失败"是
  前一天 17:34 的房间号问题。
- ⚠️ **不能说**"帧根本没进来"。当轮的 `_wsOnMessage()` 对未知
  `msg.method` 是 `if fn is None: continue` —— **无痕丢弃**, 而且当时
  **没有** pre-handler method 计数器。所以如果抖音换了礼物 method 名、
  或发来一个我们没有注册的 Gift 相关 method, 它会静默消失, 我们**看不见**。
  → 这正是 **method 探针是 B smoke 的必要前提**、而不是可选优化的原因。
- **已确认的**: 同一次运行里 `member`(21 条)与 `like`(6 条)正常落盘 ->
  对**这些**类型, handler 注册与分发路径工作正常。
- **连接层有一个**已确认的缺陷: `_connectWebSocket()` 里的
  `cursor` / `internal_ext` / `first_req_ms` / `fetch_time` / `wrds_v`
  全是写死的 **2024-07** 状态(`t-1721106114633` ≈ 2024-07-16)。

> ⚠️ **不要把"写死 cursor 是真实缺陷"与"它就是 Gift 收不到的原因"
> 混为一谈。**
>
> 前者已确认(那个 2024 状态本就该退出生产);
> 后者**尚无证据** —— Like/Member 正常而 Gift 缺失, 也可能是参数组合、
> 服务端路由、消息订阅差异、房间条件等。cursor 通常涉及恢复位置/会话
> 状态, 不能仅凭"低频 Gift 不见了"就推导它按消息类型过滤。

### `/im/fetch/` 路线已放弃(实测空 body)

本环境实测: `/webcast/im/fetch/` **恒返回 HTTP 200 + 空 body**。
试过的变体(全部 len=0, 返回头都是 `application/json`):

```text
resp_content_type=protobuf / json / 不带
cursor 为空 / 为 d-1 形式
internal_ext 为空 / 带 internal_src:dim
```

即: 请求被接受, 但服务端不按我们要求回数据。很可能是 `a_bogus` 的
**签名范围**与该 endpoint 校验的参数集不一致。

**决定: 不继续在这里投入**(追签名/cookie parity 的性价比已不划算),
改走下面的本地生成路线。`_fetch_bootstrap_state()` 保留为诊断代码,
**不再是生产 dynamic 的前置条件**。

### 本地生成 bootstrap(当前 B 路径)

外部当前实现 `JaneEyre3007/douyin-js` 的 `genCursorInternalExt()`
**不经过任何 HTTP bootstrap**, 直接取 `Date.now()` 在**本地**构造:

```text
sec    = floor(now_ms / 1000)
base   = sec << 32
r      = base + random16
h      = base + random32
wrds_v = base + random16

cursor       = t-{now_ms}_r-{r}_d-1_u-1_h-{h}
internal_ext = internal_src:dim
               |wss_push_room_id:{room_id}
               |wss_push_did:{user_unique_id}
               |first_req_ms:{now_ms}
               |fetch_time:{now_ms}
               |seq:1
               |wss_info:0-{now_ms}-0-0
               |wrds_v:{wrds_v}
```

其 README 称"本地还原 signature / cursor / internal_ext, 直接连接
WebSocket"。

> ⚠️ **这是"外部当前实现采用的做法", 不是"抖音官方协议定义"。**
> 我们采用它是为了做一个**单变量实验**, 而不是因为我们知道这是官方算法。
> 见 `vendor/douyin_fetcher/ws_bootstrap.py` 的边界说明。

**旁证**: 另有旧实现同样直接使用这种时间戳形态的 cursor/internal_ext
而非 HTTP bootstrap(例如 `fetch_time` 与 cursor 使用同一个毫秒值)。

### 下一步的 A/B(单变量)

```text
A: 旧 fallback cursor/internal_ext(2024-07 写死值)   <- 已有 A: 0 Gift
B: local-generated(JaneEyre 算法)                     <- 本轮
其余: room / WS host / signature / handler / proto / 礼物操作 全部不变
```

- A:0 / B:有 Gift -> 强证据: 旧 stale bootstrap 影响了收到的消息集合;
- 两边仍 0 -> bootstrap 假设**降级**, 转去查 WS host / `signature` 参数 /
  身份与订阅条件。**仍然不是动 Gift accumulator 的理由。**

有效性判据: 日志必须显示
`【bootstrap】本次连接使用 local-generated now_ms=...`。

### method 探针的第一个真实结果(旧 fallback 连接, 838 帧 / 952 消息)

完整 method 表(按次数):

```text
WebcastMemberMessage                319
WebcastChatMessage                  157
WebcastRoomUserSeqMessage           143
WebcastRoomStreamAdaptationMessage  124
WebcastRoomStatsMessage             123
WebcastLikeMessage                   28
WebcastRoomRankMessage               34
WebcastInRoomBannerMessage            9
WebcastRanklistHourEntranceMessage    6
WebcastGiftSortMessage                2   <- Gift-family, 无 handler
WebcastResidentGuestMessage           2
WebcastSocialMessage                  2
WebcastLowPcuGuideMessage             1
WebcastLowPcuGuideChatMessage         1
WebcastControlMessage                 1
WebcastGiftMessage                    0
WebcastLightGiftMessage               0
parse_errors: {} (空)
```

三个直接结论:

1. **`WebcastGiftMessage = 0`**: 在这条旧 bootstrap 连接里, 小心心没有以
   普通 Gift method 出现。
2. **`WebcastGiftSortMessage × 2` 是 Gift-family 里唯一出现的** —— 但它
   **基本可以从"打赏本体候选"里排除**: 公开 proto 显示它的结构只有
   `sort_type` / `scene_config` 一类字段, 被描述为"礼物排序/展示模式更新";
   另一个实现也注释为"调整礼物列表展示优先级"。它的最大价值是
   **证明 pre-handler probe 必须存在**(没有它我们根本不知道有这个类型)。
   它只记录次数, **不进入 gift accounting 候选**。
3. **新纳入观察的类型: `WebcastLightGiftMessage`**(当前公开 proto 里描述为
   轻礼物/快捷礼物, 结构含 `gift_info` / `count`, 与普通
   `WebcastGiftMessage` 是**两个独立 method**)。我们自己的 proto 目前
   **没有**这个定义。所以**不能先验假定**小心心一定走普通
   `WebcastGiftMessage`。

### method 分布推断法的教训

> ⚠️ 早先基于"257 条四类吃满"得出"没有未知 method"的推断**方法是不可靠
> 的** —— 那时只有 4 种 method 且恰好加总相等, 就差点把"没有别的类型"
> 当成结论。真正的答案必须来自**全量 method 计数**(现在的探针), 而不是
> "已知类型之和 == 总数"这种巧合。下面这一轮 952 条里有 **10+ 种** method,
> 其中 `GiftSortMessage` 就是"已知四类"完全看不见的。

### 下一轮 B smoke 看什么

**不要只盯 `WebcastGiftMessage`。** method probe 本来就是全量的, 所以
**无需提前实现任何新 parser**; 测完看整个 method 表里有没有:

```text
WebcastGiftMessage
WebcastLightGiftMessage
WebcastGiftPlayEventMessage
WebcastGiftUpdateMessage
以及任何其他新出现的 Gift* / 陌生 method
```

`GiftSortMessage` 继续记录次数, 但不进入 accounting 候选。

- 若普通 Gift 或 LightGift 出现 -> stale bootstrap 假设得到很强支持;
- 若仍全部为 0 -> 下一层优先查 **真实 `user_unique_id`、WS signature
  参数/SDK version、host/身份订阅**, 而不是继续折腾 cursor。

### 12B 当前状态(未关闭项)

- **12B-Gift 未关闭**: 旧 bootstrap 下没有普通 Gift、没有 LightGift;
  GiftSort 不是打赏本体。
- **12B-Like 未关闭**: 仍缺真正的双用户 A→B→A(room-wide/per-user 未判定);
  重连/reset 也未判定。
- **13B(Gift accumulator)仍禁止实现**。
- **Step 14 继续锁死**。

---

## 6. Step 12B 需要什么

只需要**一小段真实样本**回答第 4 节的六个差分问题, 不必从零研究协议:

```text
一次真实直播, keep_all=True 落一份 data/danmaku.jsonl
  - 含若干次点赞(观察 total 是否单调、是否 replay)
  - 含至少一次 combo 连击礼物(观察 combo/repeat/total 的逐条变化)
  - 含至少一次单发礼物(观察非 combo 形态)
  - 最好覆盖一次断线重连(观察 replay 与 ID 稳定性)
```

拿到之后:

```text
Step 12B characterization
  -> Step 13B Gift delta accumulator
  -> 合并宣布 Step 13 完成
```

---

## 7. 采集口径(当前实现, 供 12B 使用)

`keep_all=True` 时 gift 记录包含:

```text
gift_id / gift_name
combo_count / repeat_count / total_count
repeat_end / group_count
group_id
log_id / trace_id
envelope_msg_id / common_msg_id
```

`group_count` 仅作**观察**保留, 不赋任何业务语义。

Like 记录包含 `count / total / envelope_msg_id / common_msg_id`。
