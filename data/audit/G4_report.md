# G4 报告 —— 题型分布 HARD→SOFT + 统一默认直播题源

上一笔的 vocabulary 随机组合已验收通过。这一笔是**真正的 G4**:

* **A/B/C** —— 题型分布从硬门降为偏好(生成侧 + 池子侧)
* **D** —— keyword vocabulary 的 seed 级 safety 窄修复
* **E** —— 自由生成链补上直播安全硬门
* **§一~§九** —— 统一默认直播题源(keyword2 为主, curated opt-in)

---

## 交付

| | |
|---|---|
| 基线 | `7411277a4f1eecb92bca13e90d5b53f84521e34a` |
| commit | `f789775`(A/B/C/E) → `6d0f4b4`(D) → `08c187d`(§一~§九) → `b6720b8`(回归+变异+报告) → `6c5dc59`(CI 修复) |
| main | `6c5dc59ae96de0287cbbb03d3672858eed691385` |
| exact-head CI | `offline-tests` on `6c5dc59` = **success** |
| 新增 LLM 调用 | **0**(全部离线) |
| 回归 | 23 个离线套件全绿 |
| 装配冒烟 | exit=124 / 0 Traceback / 题就位 / banner 可见 |

### CI 迭代记录(如实记)

第一次推的 `b6720b8` **CI 红了**, 挂在 `离线套件 g4_source`。本地、
干净 clone、`uv run` 三种方式**全绿** —— 差异在平台。

根因: banner 测试为了拿到**真实渲染出来的**那几行, 跑的是真的
`run()`(在 `_build_source` 上截断)。`run()` 在 banner 之后、截断点
之前会起 `RenderServer`, 三次迭代都用默认端口 8765。Windows 的
`SO_REUSEADDR` 语义宽松, 连着 bind 看不出来; **Linux 上第二次 bind
撞 TIME_WAIT 直接失败**。

修法两条: 每次迭代换端口(`18700+idx`), 并在 `finally` 里
`dr.server.stop()` —— 测试起的服务测试自己收掉。`6c5dc59` 转绿。

这条值得留在报告里, 因为它是**同类问题的第二次**: 上一笔里
`test_g3_session_seed_reproducible_end_to_end` 也是"两次运行共享了
一份状态"而本地看不出来。测试里的**隐式共享资源**(tmpdir、端口)
是一个反复出现的失败模式。

---

## A. 生成链: 题型分布 HARD → SOFT

`structure_original_idea()` 的 ④ 与 `gen_spec()` 的 ⑤ 原本都是

```python
xbad = cross_puzzle_gate(...)
if xbad:
    return _bail("跨题重复: ...")     # / continue
```

现在只写 metrics:

```python
if xbad:
    m["diversity_signals"] = list(xbad)
```

理由不是"宽松", 是**成本形状**: 一稿要跑完出稿 + 审稿 + truth audit
三次昂贵调用, 全跑完才因为"recent 10 里 death 已有 2 道"把稿子扔掉。
而那道题**本身完全合格**。观众那边看到的是现场生成反复失败、回落兜底。

两条链**同源同口径**(落同一个 `diversity_signals` 键), 因为 classic
链同样存在"生成完因题型 quota 直接丢稿"的路径。

**没有放宽的**: `validate_spec` / schema / truth audit / mechanism
一致性 / `too_similar`。

## B. 池子: 两遍选择

`pool._passes()` 是显式阶梯:

| pool_kind | passes |
|---|---|
| curated | `(False, True)` |
| generated | `(False, True)` |
| 未知(替身/老实例) | `(False,)` 行为不变 |

消灭的形状:

```
stock > 0
  但 recent-10 把 mechanism/domain/death 配额占满
  -> playable == 0
  -> 补池狂补, 补进来的被同一个窗口挡住
  -> 一路补到硬上限, 观众还在等
```

`playable_count()` 与 `pop_next()` 共用**同一份** `_passes()`, 所以
"一个说能播、一个交付 None" 不可能再出现。

新增 `diversity_reject_count`: **Pass 2 真正起作用的次数**。没有它,
"接了两遍" 与 "接了两遍但没生效" 在测试上不可区分 —— 本项第一版正是
如此(见下面的变异记录)。

### ⚠️ 一处第一版写错并当场被测试抓下

我最初给 curated 写了 `(False,)`, 想当然地以为"收紧就是只留一遍"。
后果立刻可见: `curated: 配额占满仍可播(Pass2)` 从绿变红 —— 因为
`(False,)` 把 **Pass 2 整条删掉了**, 于是 H4-E 治过的病("一道都播不
出来")复发。两遍是**前后关系**, 删掉后一遍不是"更严格"。

## C. Blueprint 只做生成偏好

classic 链的 ⑤ 与 A 同一处理。Blueprint 继续用于"想生成什么", 但成品
合格时不再因 observed signature 没落在 target 类型上被丢。narrator
truthfulness / mechanism consistency / schema / 真实逻辑错误一律不放松。

## D. vocabulary seed 级 safety(窄)

新增 `_SHOCK_MARKERS`: 以**严重伤害 / 重口暴力 / 性暴力 / 自伤 / 毒品
本身作为冲击点**的词不进词库。

```
碎尸 分尸 尸块 运尸 抛尸 藏尸 焚尸
砍手 砍断 砍死 截肢 断手 断脚 挖眼 割喉 割腕 剁 肢解
虐待 虐杀 折磨致死 拷打
上吊 跳楼 割脉 自缢 服毒
贩毒 制毒 毒瘾 吸食
性侵 性虐 猥亵 迷奸 诱奸 娼妓
```

**刻意窄**。下面是**保留**的(逐个断言过):

```
死亡 尸体 棺材 凶杀 杀人 遗书 祭奠
手枪 毒药 埋葬 精神病 黑人抬棺 砷中毒 血迹 爆炸 打猎
```

判据是"这个词**除了冲击感还有没有别的信息**", 不是"题目悲不悲惨"。
`死亡`/`凶杀`/`遗书` 是海龟汤的**事实材料**; `碎尸`/`砍手` 的全部
内容就是那个伤害动作。

### 过滤统计(只记数量与类别, 不落词面)

```
raw_rows            3729
raw_token_count     8123
valid_token_count   7112
unique_token_count  1134     (v1: 1144)

rejected_by_reason:
    length     324
    sentence   500
    charset     15
    unsafe     129
    shock       43   ← 本轮新增这一类
```

新增 `reject_reason()`: 与 `is_valid_keyword` **同序**的类别归属。
分开之后报告能写"挡住了 43 个 shock", 而不是含糊的"过滤了 968 个词"。
产物只存 `{类别: 计数}`, **不存词面**(§D: 不要在报告里大段复现这些词)。

版本: `CORPUS_VERSION` 与 `KEYWORD_SEED_VERSION` 同步
`keyword2-vocab-v1` → `keyword2-vocab-v2`(sampler 一行没改, bump 的
是**词表口径**)。

### 两层是分工, 不是重复

```
这一层 seed 级       不让明显不合适的**入口**出现
livestream_safe 成品级  不管入口多干净, 成品必须过直播安全判断
```

只有入口过滤 → "普通词拼出重口题"漏出去(这正是本轮补 E 的原因);
只有成品门 → 白烧 A+B+审稿+audit 四次调用才拒掉。

## E. 自由生成链的直播安全硬门

`_QUALITY_CHECK_FIELDS` 八项 → 九项, 第 9 位 `livestream_safe`。
`_quality_check_contract` 的切片 `[:8]` → `[:9]`。curated-v5 契约
**不受影响**(它走 `_CURATED_HARD_CHECK_FIELDS`, 根本不看这个元组)。

死亡作为普通剧情事实 → true; 拿重口 / 极端伤害当噱头 → false。
判据措辞与 curated 侧**共用** `_TOOL_CHECK` 的那一份 schema description。

### ⚠️ **位置是契约的一部分**

第一版我把 `livestream_safe` **append 到元组末尾**。切片 `[:9]` 于是
取到的是 `story_reconstruction`(题型四问的第一项), 而
`livestream_safe` 留在切片外。生产后果是**每一道自由生成的题都判不合格**
—— 代码在索要一个它根本没问过模型的字段。

`test_llm` 的 `qc_ok()` 立刻红了(`quality_checks 未全过(story_reconstruction)`),
我才发现。现在它紧跟四项"好不好玩", 并且在常量旁边写了注释说明
"位置是契约的一部分"。

---

## §一~§九: 统一默认直播题源

| | |
|---|---|
| 默认题源 | keyword2 generated |
| curated | **OFF**(opt-in) |
| 取题顺序 | generated → curated → keyword2 现场生成 |
| 现场生成 | keyword2(与后台补池**同一条链**) |
| kill-switch | `--no-keyword-seed` → prefetch 与 live **同时**回 classic |

### 一 / 二 / 三 / 四: 顺序与 opt-in

* `prefer_curated` 默认 `False`
* 新增 `--curated`; `--no-curated` 保留为兼容参数
* **两者写同一个 dest** → "最后写的赢" 是唯一规则, 不会出现两个 flag
  打架而代码只读其中一个
* 开启后 generated **仍排前** —— curated 是补充题源, 不是默认主产品

### 四: live 与 prefetch 共用一份骨架

抽出 `keyword_seed.keyword_spec()`, 两条路径**都**调它:

```
draw 2 independent keywords
  -> Stage A -> Stage B
  -> generated Reviewer -> truth audit -> validate -> safety/dup 硬门
```

两份实现会在"哪里写 metrics / 哪里判让路 / 哪里补 provenance"上漂,
漂了以后 live 与 prefetch 出的题就不是同一种东西了。

### 五: 冷启动 prewarm

`Director._prewarm()`:

* `playable >= 1` → **0 次生成**(§九-9)
* `playable == 0` → 最多 `pool_prewarm_max_rounds` 轮 / 90 秒内取**一道**
* 拿到一道**立即**结束(§九-10), 不补到 target=5
* 失败 **不阻止启动**(§九-11)—— 按 emergency fallback

目标是"至少 1 道", 不是补到 target。补满要 3~5 轮、每轮几十秒 ——
那是"开播前先静默三分钟", 对直播不可接受。

直接调 `_generate_one_inner` 而不是 `on_tick`: 后者带 latch / 退避 /
deadline 一整层"该不该现在开始"的判定, 而那些判定的前提(相位稳定)
在 `engine.start()` 之前**不成立**。绕过调度层, 复用执行层。

### 八: provenance

| 来源 | 标签 |
|---|---|
| 后台补池 | `keyword2_pool` |
| 现场生成 | `keyword2_live` |
| 外部题 | `curated` |
| classic 现场 | `live_generate` |
| 引擎兜底 | (引擎自己的) |

不再有模糊的 `pool`。

### 六 / 七: 不动的东西

`pool_min_size=2` / `pool_target_size=5` / `reveal_target=7` **未调整**
(§六: 先实播观察, 不凭感觉扩大池子)。

curated compiler / dataset / ledger / LazyCurator / H4-E 两遍**全部保留**
—— 关闭的是"默认播放 / 默认审题", 不是删除。`neurostellar/haiguitang`
的 `input` 仍然作为 keyword vocabulary 的**离线构建来源**。

---

## ⚠️ 顺带修掉的一个既有缺陷: banner 根本打不出来

§二 要求 banner 打印 `题源模式 / curated: OFF`。写完之后我发现**整个
banner 都看不到**。

根因: `setup_logging()` 为了在 Windows 控制台正确写中文, 把控制台句柄
**重新 open** 成了 UTF-8 流:

```python
console = open(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)
```

交给 logging 用的是**这一份**; 而 `run()` 里的 `print()` 写的是
`sys.stdout` —— **同一个 fd 的另一个 Python 对象**, 而且那个是带缓冲的。
于是 banner 要等**进程退出**才 flush, 那时日志已经滚完几十行;
`| head` / 重定向时更彻底 —— 整个 banner 消失。

**在干净基线上复现过**(`grep -c "竖屏 AI" = 0`), 所以不是本轮引入的。

修法: banner 走模块级 `_console`(与日志**同一个** fd)并**显式 flush**。
顺序因此是确定的, 而且 banner 变成**可断言**的了 —— `test_g4_source`
里那条 banner 测试就是靠这个把 §二 钉住的。

---

## 回归

### 改写了 11 条钉住旧契约的断言

G4 反转了若干产品决定, 所以有些测试的**结论**必须跟着反。每一条都记了
"为什么反"与"仍然守住什么":

| 文件 | 条 | 从 → 到 |
|---|---|---|
| test_pool | [2a] | 分布冲突挑不出 → **能挑出**, 但记 `diversity_reject_count` |
| test_pool | [2b] | 池==gate → gate 只决定哪一遍; 反证换成静态准入 |
| test_pool | [2c] | 全被挡→None → **Pass 2 照样出题** |
| test_pool | [L1-1] | playable=0(纯 diversity) → 改用 `too_similar` 证两指标不等 |
| test_pool | [L1-2] | 同上 |
| test_pool | [C6-A] | 6-dark 挡交付 → **退化为偏好**, 但仍然被计算 |
| test_pool | [H4-E2] | generated 仍挡 → **与 curated 同阶梯** |
| test_pool | [H4-E8] | generated hard quota 不变 → 同阶梯; `_soft_diversity` 属性语义未变 |
| test_llm | P0-1 | 跨题重复不返回 → **照常交付**; 新增 `too_similar` 仍硬的反证 |
| test_prefetch | [G2-14] | 配额墙硬拒 → 只记录; 断言换到 `diversity_signals` |
| test_curated_compile / test_curated_v2 / test_solve_ux | | 字段数 8→9 / 12→13 |

### test_prefetch 的 `_blocked_pool` 夹具

它原来靠"同一个 signature 撞配额"造出 `stock>0, playable=0`。G4-B 之后
那条路**不再让 playable 归零** —— 夹具会**静默失去触发条件**, 变得
"看起来在测 L1-A, 其实池子完全健康", 而测试仍然全绿。

改成用 `too_similar`(identity, 两遍都挡, 且同样依赖当前窗口)。
这是本轮最容易踩的坑, 所以在那段代码上写了长注释。

---

## 变异

18 条, **全部红**。

| # | 变异 | 变红的套件 |
|---|---|---|
| M1 | generated 回单遍 | test_pool |
| M2 | Pass 2 仍走完整门 | test_pool |
| M3 | `gen_spec` 重新硬拒 | test_llm |
| M4 | Stage B 重新 bail | test_prefetch |
| M5 | `livestream_safe` 不进切片 | test_solve_ux |
| M6 | `prefer_curated` 默认回 True | test_g4_source |
| M7 | 取题顺序回 curated 优先 | test_g4_source |
| M8 | live 现场生成回 classic | test_g4_source |
| M9 | prewarm 不判 playable | test_g4_source |
| M10 | prewarm 补到 target | test_g4_source |
| M11 | prewarm 上限失效 | test_g4_source |
| M12 | shock 过滤整条删掉 | test_keyword_seed |
| M13 | shock 过滤扩到普通死亡词 | test_keyword_seed |
| M14 | banner 不打题源模式 | test_g4_source |
| M15 | source 标签退回 `pool` | test_pool |
| M16 | curated 默认仍然加载 | test_g4_source |
| M17 | `--curated` 不接线 | test_g4_source |

### ⚠️ M9 第一版**没有红** —— 这是本轮唯一一次, 值得记

`test_prewarm_skipped_when_playable` 用的是 `mkcfg(d)` 的默认值, 而
那个 helper 默认 `pool_prewarm_max_rounds=0`(**关掉预热**, 免得离线
用例被拖慢)。于是"0 次生成"在**任何实现下**都成立 —— 这条测试测的是
空气。把阈值改成 `>= 99999`(永远不跳过), 它照样全绿。

修法两件事一起做:

1. 显式把预热**打开**(`pool_prewarm_max_rounds=3`);
2. 补一条**反证**: 同样开着预热, `playable=0` 时**必须**发一次。

两条合起来才能区分"跳过了"与"根本没跑"。

这与 H4-F 的 M2 / G2 的 M7 / G3 的 M1、G4 上一笔的 M8/M9/M11 是同一类:
**断言写得很像在测那个机制, 但执行路径根本没走到**。变异实验的价值
就在这里 —— 它不验证"代码对不对", 它验证"测试有没有在测东西"。

---

## 明确没做(§六 / §七)

* 没调 `pool_min_size` / `pool_target_size` / `reveal_target`
* 没删 curated compiler / dataset / ledger
* 没改 `KEYWORD_IDEA_SYSTEM`(Stage A prompt 未动)
* 没改 Stage B 的 schema(仍无 puzzle/answer 可写字段)
* 没改 G3 的随机组合逻辑
* 没改 curated-v5
* `QUALITY_POLICY_VERSION` / `RIDDLE_PROMPT_VERSION` **未 bump**
  (bump 会隔离盘上全部存量题)
