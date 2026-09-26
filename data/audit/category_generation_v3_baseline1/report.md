# Generation v3 —— 五类创作真实 LLM 基线

- protocol: `haiguitang-v2` / prompt: `haiguitang-generation-v3` / model: `ds`
- session_seed: `20260928` / corpus: `keyword2-vocab-v2` / requested difficulty: `(不指定)`
- 总耗时: 2350.5s / token usage: {'input_tokens': 78262, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 299264, 'output_tokens': 91227}

## 汇总(只做事实统计, 不评分)

| 类别 | attempts | accepted | requested=observed 命中 | observed 分布 | 失败分类 |
|---|---|---|---|---|---|
| logic(逻辑) | 5 | 3 | 1 | logic:1, brainstorm:2 | gen_fail:2 |
| suspense(悬疑) | 4 | 3 | 2 | emotion:1, suspense:2 | gen_fail:1 |
| horror(恐怖) | 4 | 3 | 3 | horror:3 | gen_fail:1 |
| emotion(情感) | 5 | 3 | 2 | brainstorm:1, emotion:2 | gen_fail:2 |
| brainstorm(脑洞) | 3 | 3 | 2 | brainstorm:2, emotion:1 | - |

### stage 级调用/失败(技术瓶颈定位)

| stage | calls | errors | output_tokens | latency_s |
|---|---|---|---|---|
| puzzle.story | 21 | 2 | 19769 | 1740.0 |
| puzzle.surface | 19 | 0 | 2505 | 196.1 |
| puzzle.structure | 19 | 0 | 25889 | 187.9 |
| puzzle.review | 20 | 0 | 38762 | 153.6 |
| puzzle.safety | 18 | 0 | 1695 | 35.2 |
| puzzle.truth_audit | 18 | 0 | 2607 | 37.4 |

## logic / 逻辑

### logic-03

请求类型：logic / 逻辑
随机关键词：合影 / 自律

【汤面】
小王拿出一张与邻居大爷的合影，坚称是案发当晚拍的。警方只看了一眼照片，就斩钉截铁地说：这照片肯定是清晨拍的。没有人提到拍照的具体时间，他们凭什么这么肯定？

【汤底】
小王想为自己伪造不在场证明。小区的邻居大爷以极度自律闻名：几十年每天清晨六点准时出门晨练、买早点，晚上九点后从不出门，全小区都拿他当"活闹钟"。案发那天清晨，小王在大爷买早点时和他合了影；当晚作案后，他把这张照片谎称为当晚所拍。警方一看照片里大爷穿着晨练服、手里还拎着豆浆油条，又深知大爷雷打不动的作息，立刻断定照片只能摄于清晨——不在场证明随之崩塌。

【核心答案】
照片里大爷穿着晨练服、拎着豆浆油条，而大爷几十年只在清晨六点出门晨练买早点、晚上从不出门，所以照片只能是清晨拍的。

【最终观察】
primary_category: logic
categories: ['logic', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 358, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1429}

### logic-04

请求类型：logic / 逻辑
随机关键词：狂风暴雨 / 床

【汤面】
暴雨下了一整夜，男人却一直守在"床"边不肯离开，风雨越大，他守得越紧。天亮之后，他才回到家里，倒头睡了整整一天。

【汤底】
这个"床"不是睡觉的床，而是河床。男人是水文站的值班员，暴雨夜正是一年里最关键的时刻：他必须守在河边，测水位、记录流量变化，判断会不会发生洪水。风雨越大，他越要盯在河床边。天亮时水势平稳、数据记录完毕，他才放心回家，倒头补了一觉。

【核心答案】
"床"不是睡觉的床，而是河床：他是水文站值班员，暴雨夜必须守在河边测水位、记流量、判断洪水，天亮水势平稳才放心回家补觉。

【最终观察】
primary_category: brainstorm
categories: ['brainstorm', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 310, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1456}

### logic-05

请求类型：logic / 逻辑
随机关键词：火山 / 重口味

【汤面】
客人尝了一口菜，立刻被咸得皱起眉头，连连喝水，一口也吃不下去。做菜的男子接过碗尝了尝，一脸疑惑地说："这口味很淡啊，我平时就是这么吃的。"

【汤底】
男子从小住在活火山脚下，火山常年散发出含硫的"臭鸡蛋"气味。长年被这气味熏着，他的嗅觉和味觉严重钝化，尝什么都觉得寡淡无味。为了让食物"尝起来正常"，他做菜时盐、辣椒、酱料越放越多——在他自己的感知里，这已经是刚刚好的清淡口味。所以客人觉得重口味到受不了，他却一脸无辜：不是他怪，是火山悄悄偷走了他的味觉。

【核心答案】
男子长住活火山脚下，被含硫气味熏得嗅觉味觉钝化，做菜时越放越咸，自己却尝不出，只觉得清淡。

【最终观察】
primary_category: brainstorm
categories: ['brainstorm', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 340, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1179}

## suspense / 悬疑

### suspense-06

请求类型：suspense / 悬疑
随机关键词：衣柜 / 仪式

【汤面】
每年同一天，她都锁上房门，在旧衣柜前点起蜡烛，剪下自己的一缕头发放进柜里，对着柜子低声说话。家人和邻居在门外听见，都说她在搞邪教仪式。柜子里，其实什么人也没有。

【汤底】
小时候她与双胞胎妹妹玩捉迷藏，妹妹藏进旧衣柜，她因赌气故意拖延没去找，等想起来时妹妹已在柜中窒息身亡。此后每年妹妹忌日，她都关起门对着衣柜点蜡烛、剪下一缕头发放进柜中，喃喃自语，家人和邻居都以为她入了邪教在搞诡异仪式。其实那是她的赎罪：她把头发当作"自己"藏进衣柜，替妹妹续上那局永远没结束的捉迷藏——这次换她来藏，等妹妹来找。

【核心答案】
衣柜里是她幼年捉迷藏时被自己赌气拖延、窒息而死的双胞胎妹妹;每年忌日她剪发藏进柜中,是用捉迷藏替妹妹续上那局永远没结束的游戏,也是赎罪。

【最终观察】
primary_category: emotion
categories: ['emotion', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 364, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1477}

### suspense-07

请求类型：suspense / 悬疑
随机关键词：旋律 / 禁足

【汤面】
每天傍晚六点，隔壁照例传来女孩练琴的琴声，一拍不差。老邻居听了大半年，忽然发现：这琴声从来没有弹错过一个音——连同一个错音的位置都从不出现。她越想越不对劲，报了警。

【汤底】
女孩被母亲长期禁足在家，日夜练琴备战钢琴比赛，一次她趁母亲外出偷偷溜出门，却在路上出了车祸身亡。母亲因监护失职害怕被追责，隐瞒了死讯，每天傍晚在琴上播放女儿练琴的录音，让邻居以为女孩还在家里练琴。但录音是固定的， melody 从不弹错一个音、永远一模一样，老邻居终于起疑报了警。警方破门时，发现的只有遗像和一台循环播放的旧录音机。

【核心答案】
女孩早已车祸身亡，母亲为隐瞒死讯，每天傍晚播放女儿练琴的固定录音，琴声从不弹错才露了馅。

【最终观察】
primary_category: suspense
categories: ['suspense', 'horror', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 362, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1442}

### suspense-09

请求类型：suspense / 悬疑
随机关键词：冻死 / 布偶

【汤面】
寒夜里，搜寻队的呼喊声一遍遍喊着女孩的名字，越来越近。她却把自己缩得更深，屏住呼吸，一动不敢动——这地方她从没来过，根本不认得。怀里那只奶奶缝的布偶，她攥了一整夜，始终没有拆开。

【汤底】
女孩被拐后遭遗弃在城郊的废弃大棚，骗子吓唬她"走出这条路就有人杀你全家"，于是她把搜寻队也当成威胁，每次听见呼喊都躲起来。奶奶在外套上缝了只布偶，里面藏着写有家庭住址的布条，叮嘱"实在回不了家再拆开"。她以为自己就在家附近，一直不肯拆，最终在寒夜里冻死。警方起初只当布偶是普通玩具，几天后复查遗物拆开才发现布条——其实她有整整三天自救机会。

【核心答案】
她被拐后遭恐吓、把搜寻队也当成威胁，听见呼喊反而躲得更深；而奶奶缝在布偶里、写着家庭住址的布条她至死没拆，白白错过三天自救机会、冻死。

【最终观察】
primary_category: suspense
categories: ['suspense', 'emotion']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 357, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1525}

## horror / 恐怖

### horror-11

请求类型：horror / 恐怖
随机关键词：人工心脏 / 指引

【汤面】
男人深夜独自走进医院发来的"指引"指定的废弃病区，里面空无一人。他看到成排的同款人工心脏摆在那里，每一台都贴着一张姓名牌。他凑近去看，发现牌上的名字他一个都不认识——但今晚的指引还没结束。

【汤底】
男人的心脏被换成了"人工心脏"，医院发给他一个"指引"App，声称必须按时按指引做远程校准，否则心脏会停。指引的指令越来越怪：不许告诉家人、深夜独自出门。他照做，走进了废弃病区，那里摆满同款人工心脏，每台都贴着病人姓名。真相是：医院在濒死病人身上秘密试验未获批的人工心脏，靠"指引"远程操控心率采集数据；一旦试验有暴露风险，就把病人诱到无人处，远程停搏灭口——姓名牌全是先他一步"校准完毕"的病人，而今晚，轮到他了。

【核心答案】
医院在濒死病人身上秘密试验人工心脏，用"指引"App 远程操控收集数据；把他引到废弃病区是为了远程停搏灭口，姓名牌上的都是前一个"校准完毕"的试验者。

【最终观察】
primary_category: horror
categories: ['horror', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 387, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1349}

### horror-12

请求类型：horror / 恐怖
随机关键词：赌博 / 柠檬

【汤面】
男人夜里投宿山中客栈，被拉去凑一桌牌局。规则古怪：每人手里必须一直捏着一片柠檬，绝不允许离手或上桌。牌友们的手冰凉冰凉的，从不吃柠檬，却一个劲地劝他："把柠檬也押上来吧。"天亮时，牌友们全都不见了。

【汤底】
男人夜里投宿山中客栈，被拉去凑一桌牌局。规则古怪：每人手里必须一直捏着一片柠檬，柠檬绝不允许离手或上桌。牌友们手冰凉、从不吃柠檬，而且一直劝他"把柠檬也押上来"。天亮时牌友们全部消失，真相是：这客栈多年前失火，烧死的赌客夜夜回来打牌，只想赢到一个活人替死鬼凑齐最后一桌。柠檬辟邪，鬼魂一碰就发黑——他们的柠檬早已烂尽，再也抓不住活物；男人手里的柠檬始终新鲜，只要他不肯松手押上，鬼就永远拿不走他。

【核心答案】
牌友是多年前客栈大火烧死的赌客鬼魂，想赢活人替死；柠檬辟邪，他们碰不得，只要男人不把柠檬押上就安全。

【最终观察】
primary_category: horror
categories: ['horror', 'suspense', 'brainstorm']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 405, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1373}

### horror-13

请求类型：horror / 恐怖
随机关键词：小吃摊 / 心脏

【汤面】
巷口那个不收钱的摊子只卖莲子汤，老板娘却盯着每位客人，汤不喝完就不让走。那晚一个醉汉喝完一碗还嫌不够，连着又舀了两碗。老板娘没有阻拦，只是低下头，把火熄了。

【汤底】
深夜巷口的小吃摊只卖莲子汤，不收钱，但老板娘坚持要客人把汤喝完才能走。真相：老板娘曾抱着心跳衰竭的孩子死在这条巷子里，母子未能超生。孩子生来没有心跳，只能靠吸取活人的心跳维系；她熬的汤会在入口时悄悄带走客人"一拍心跳"，不收钱正是等价交换。一位醉酒的常客贪嘴连喝三碗，第二天再没醒来——从此，巷口的摊子再也没有人见过。

【核心答案】
摊子是鬼摊：老板娘与死婴靠莲子汤吸走客人一拍心跳维生，醉汉连喝三碗等于被抽走三拍，次日便死了。

【最终观察】
primary_category: horror
categories: ['horror', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 354, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1340}

## emotion / 情感

### emotion-14

请求类型：emotion / 情感
随机关键词：尝试 / 惊奇

【汤面】
爸爸夹起一筷子烧糊发苦的菜，突然瞪大眼睛惊呼："太好吃了！你怎么做的？"紧接着，全家人都抢着往自己碗里夹，吃得心满意足。妈妈在一旁笑着说："明天我再改良改良。"

【汤底】
妈妈多年前一场大病失去了味觉和嗅觉，从此做饭全凭记忆和手感去"尝试"，越做越没把握，一度想放弃下厨。爸爸为了让她不丢掉这份坚持，每天都配合表演：再咸再糊也第一个动筷，吃得津津有味，还大声表现出"惊奇"，几十年如一日。全家也心照不宣地陪演，让这顿饭一直热热闹闹地做下去、吃下去。爸爸的夸张不是口味奇怪，而是一场温柔的守护。

【核心答案】
妈妈多年前失去味觉嗅觉,厨艺早已没把握;爸爸的夸张惊呼和全家抢食都是陪演,只为让她别放弃下厨。

【最终观察】
primary_category: brainstorm
categories: ['brainstorm', 'emotion']
difficulty: easy

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 348, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1289}

### emotion-15

请求类型：emotion / 情感
随机关键词：吵闹 / 排风扇

【汤面】
每天傍晚，儿子都把排风扇开到最大，还拿着锅铲拼命敲打锅碗，家里吵得震天响。奶奶坐在厨房里，却安静得像什么都没发生。女儿实在忍不住了："爸，你能不能小点声？"

【汤底】
奶奶患了阿尔茨海默症，记忆停在了几十年前全家挤在老厨房做饭的日子——只有听到锅铲声、排风扇的轰鸣和人声鼎沸，她才安静安稳；一旦家里太静，她就惊慌哭闹。于是每天傍晚，儿子故意把排风扇开到最大、锅碗敲得震天响，还常叫亲戚回来吃饭，陪她"回到"那个全家都在的黄昏。女儿一直嫌吵，后来才明白：这份吵闹，是父亲给奶奶开的药。

【核心答案】
儿子故意制造吵闹，是在哄患阿尔茨海默症的奶奶——她一听到锅碗声和排风扇轰鸣就安稳，太安静反而惊慌。

【最终观察】
primary_category: emotion
categories: ['emotion', 'brainstorm']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 352, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1139}

### emotion-18

请求类型：emotion / 情感
随机关键词：AO3 / 下毒

【汤面】
奶奶去世后，她的连载小说突然更新了，结局还完整填上了那个投毒悬案的答案。深夜里，爷爷独自在电脑前，反复搜索"下毒"和"毒药原理"。可谁都知道，他连打字都不会。

【汤底】
奶奶生前在 AO3 上连载小说，患眼疾后无法再写作，临终前最放不下那个没写完的投毒悬案结局。爷爷不会打字，却偷偷注册了她的账号，一个键一个键学，替她把小说完结——对外仍署她的名。他深夜搜索"下毒""毒药原理"，是因为老读者留言说原稿里的投毒情节写错了，他必须查清楚才能替奶奶改对那个结局。

【核心答案】
奶奶病后无法写完连载,不会打字的爷爷偷偷学会打字,用她的账号替她补完了结局;深夜搜毒药是为了改对她小说里的投毒情节。

【最终观察】
primary_category: emotion
categories: ['emotion', 'suspense', 'brainstorm']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 344, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1309}

## brainstorm / 脑洞

### brainstorm-19

请求类型：brainstorm / 脑洞
随机关键词：牌 / 邋遢

【汤面】
男人每天胡子拉碴、衣衫破旧地出门，邻居都以为他遭了变故，好心劝过他好几次。直到有一天，路人在一家鬼屋大门口的荣誉牌前停下——牌子上那张吓人成绩最佳的"招牌邋遢鬼"照片，和他本人一模一样。

【汤底】
男人不是自甘堕落，他是本地鬼屋的招牌演员。为了演好"邋遢鬼"这个角色，他每天上班前故意不洗头、不刮胡子，把新衣服撕破弄脏，所以出门总是一副邋遢样，邻居还以为他遭遇了变故。他其实是鬼屋的"头牌"——吓人成绩最好，照片被挂在大门口的荣誉牌上。路人看见牌子上的照片和他本人一模一样，误会才解开。

【核心答案】
他不是遭了变故，他是鬼屋的招牌演员，邋遢是他上班前刻意做成的行头，门口荣誉牌上那张最佳吓人照片就是他。

【最终观察】
primary_category: brainstorm
categories: ['brainstorm', 'emotion', 'logic']
difficulty: easy

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 359, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1270}

### brainstorm-20

请求类型：brainstorm / 脑洞
随机关键词：八音盒 / 护工

【汤面】
阿良端着早饭冲进病房，却先扑向床头那个八音盒，慌忙把发条上满，听到旋律重新响起才松了口气。床上的人从头到尾没睁过眼，他还是替对方掖好被角，轻声说"别急，今天还长着呢"。家属每月付他一大笔钱，只叮嘱一件事：旋律一停，马上上发条。

【汤底】
在未来，人临终前可以把一生的记忆录进八音盒：发条上满、旋律走完一遍，就等于逝者重新"活"了一天。护工在一家特殊机构上班，照顾的"病人"其实都已去世，床头上只有八音盒。家属付钱请护工每天准时上发条、铺床、盖被、在旁边陪着，让逝者把"这一天"好好过完。旋律一旦停下，逝者的"一天"就中断了，所以上发条比喂饭还急。

【核心答案】
八音盒里存着逝者一生的记忆，上满发条走完一遍旋律，就等于让逝者重新"活"一天；护工在特殊机构照顾的都是已故之人。

【最终观察】
primary_category: brainstorm
categories: ['brainstorm', 'emotion', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 385, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1506}

### brainstorm-21

请求类型：brainstorm / 脑洞
随机关键词：推理 / 110

【汤面】
深夜，110接线员接到一个电话，对头却反过来向他提问："什么时间？什么地点？几个人？"对方一本正经地做着记录，最后还向他郑重交办了几件事，才挂断电话。据说这位老人每晚都会准时打来。

【汤底】
老人退休前是110接警员，如今患了阿尔茨海默症，记忆停在了上班的那些年。他每晚"到岗"，凭着几十年的肌肉记忆拨打110，像接警一样向接线员提问、做"案情记录"，最后郑重交办。接线的年轻警员都认得这位带过自己的老师傅，不忍戳破，也不能按恶意拨打110处理，于是顺着他的流程一一应答，让他把"这一班"安心值完。

【核心答案】
老人退休前是110接警员，患阿尔茨海默症后记忆停在上班的年代，每晚"到岗"拨打110，按接警流程提问、记录、交办；年轻接线员都是他的后辈，不忍戳破。

【最终观察】
primary_category: emotion
categories: ['emotion', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 349, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1433}

---

共 15 道 accepted。requested != observed 是**合法状态**, 报告原样呈现, 不改分类、不挑题。