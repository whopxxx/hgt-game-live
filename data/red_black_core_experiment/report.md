# 红汤 / 黑汤 **汤底** 创意基线 —— 原始结果

## 这个实验**没有**做什么

| 没有做 | 说明 |
|---|---|
| 不写汤面 | 本轮完全不生成 puzzle |
| 不做结构化 | 没有 facts / solve_atoms / fair_clues / discovery_beats / completion / signature |
| 不跑 Reviewer | 没有 pass/fix/rewrite, 没有 quality_checks, 没有 truth audit |
| 不评分不排名 | 没有任何自动分类器给这 20 条打分或排序 |
| 不筛选 | 20 条全部原样保留, 不删除 / 不重抽 |
| 不接生产 | 不进 Stage B, 不入池, 不写 played / pool_used / archive |

本文件只陈述**客观事实**。哪几条好、红黑两种哪边更强 —— 
**由人直接读下面 20 条原始故事来判断**, 报告不替读者下结论。

## 事实

- base SHA: `62b069b1ec1abbe3a027df19f293f6b041e58ad5`
- branch: `experiment/red-black-core-stories`
- 生成时 HEAD: `62b069b1ec1abbe3a027df19f293f6b041e58ad5`
- model(请求): `deepseek-v4.1-flash`
- model(返回体): `['deepseek-v4.1-flash']`
- temperature: `0.8` / max_tokens: `2600`
- seed: red=`20260921` black=`20260922`
- 红 10 / 黑 10, 共 `20` 条
- 成功 `20` / 技术失败 `0`
- 原始结果: `data/red_black_core_experiment/raw.jsonl`
- 逐条全文: `data/red_black_core_experiment/raw_report.md`

## 实际使用的 Prompt(原文)

### 红汤 system —— 94 字 (`02e6ed12`)

```text
你在为海龟汤设计隐藏故事。
只写事情真正发生了什么, 不要写谜面(writer 之后会从故事里截取)。
偏红汤: 真相本身要有分量, 值得最后揭晓 —— 不是普通事故, 也不是单纯的悲剧。
```

### 黑汤 system —— 98 字 (`d4fe029f`)

```text
你在为海龟汤设计隐藏故事。
只写事情真正发生了什么, 不要写谜面(writer 之后会从故事里截取)。
偏黑汤: 真相揭晓后应让人明显不安或后背发凉 —— 靠关系、意图或世界规则, 不靠血腥描写。
```

### user(两类共用)

```text
写一个完整的故事。可以围绕「{subject}」, 也可以不围绕。
```

### 输出 schema

```json
{
  "type": "object",
  "properties": {
    "story": {
      "type": "string",
      "description": "完整故事全文。",
      "minLength": 1
    }
  },
  "required": [
    "story"
  ]
}
```

## 逐条索引

| id | type | 主题 | 技术失败 | 字数 | 试次 |
|---|---|---|---|---|---|
| `red-01` | red | 一间储物间 |  | 1342 | 1 |
| `red-02` | red | 一台旧相机 |  | 750 | 1 |
| `red-03` | red | 一次门诊复诊 |  | 1982 | 3 |
| `red-04` | red | 一次家访 |  | 1497 | 1 |
| `red-05` | red | 一位老邻居 |  | 1465 | 2 |
| `red-06` | red | 一部电梯 |  | 1469 | 1 |
| `red-07` | red | 一次过户 |  | 1473 | 2 |
| `red-08` | red | 一场暴雨 |  | 1446 | 1 |
| `red-09` | red | 一座小岛 |  | 1629 | 3 |
| `red-10` | red | 一部公交车 |  | 1204 | 2 |
| `black-01` | black | 一位新同事 |  | 1856 | 2 |
| `black-02` | black | 一份体检报告 |  | 3228 | 1 |
| `black-03` | black | 一台旧相机 |  | 1302 | 2 |
| `black-04` | black | 一趟搬家 |  | 1272 | 1 |
| `black-05` | black | 一次采访 |  | 1321 | 2 |
| `black-06` | black | 一个直播间 |  | 1645 | 3 |
| `black-07` | black | 一间洗衣房 |  | 1614 | 1 |
| `black-08` | black | 一部公交车 |  | 2132 | 1 |
| `black-09` | black | 一间值班室 |  | 1461 | 1 |
| `black-10` | black | 一个快递驿站 |  | 1654 | 1 |

## 模型自发的行为(客观计数, 不含褒贬)

这些是**模型在没有要求的情况下自己做的**, 记下来供人读原文时参照。
本轮**没有**因此删改任何一条。

- **自加标题**(首行 `《…》`): 11/20 条 —— `red-02`, `red-04`, `red-06`, `red-08`, `black-01`, `black-02`, `black-04`, `black-06`, `black-07`, `black-08`, `black-10`
- **提到「谜面/汤底」等元文本**: 0/20 条 —— 无
- **以问句结尾**: 0/20 条 —— 无
- **自然段数**: 最少 11, 最多 75

## 下一步(本轮**不做**)

只有在人确认这批汤底方向对了之后, 下一轮才做:

> 完整汤底 -> 截取 1~3 句极短汤面
