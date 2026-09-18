# 互动协议特征化(Step 12A — 外部证据)

> **这份文档不含任何真实直播采样。**
>
> 它记录的是从**我们自己仓库里的 proto** 与**外部同源实现 + 其 issue** 能
> 确认的协议事实, 以及**仍然未知**的部分。
>
> 真实采集(Step 12B)尚未发生。本文档里出现的一切**观察性描述**都标注了
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
