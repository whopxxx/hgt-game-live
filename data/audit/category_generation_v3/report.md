# Generation v3 —— 五类创作真实 LLM 基线

- protocol: `haiguitang-v2` / prompt: `haiguitang-generation-v3` / model: `ds`
- session_seed: `20260926` / corpus: `keyword2-vocab-v2` / requested difficulty: `(不指定)`
- 总耗时: 1472.8s / token usage: {'input_tokens': 26788, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 27648, 'output_tokens': 18023}

## 汇总(只做事实统计, 不评分)

| 类别 | attempts | accepted | requested=observed 命中 | observed 分布 | 失败分类 |
|---|---|---|---|---|---|
| logic(逻辑) | 3 | 3 | 2 | logic:2, suspense:1 | - |
| suspense(悬疑) | 5 | 3 | 2 | suspense:2, emotion:1 | gen_fail:2 |
| horror(恐怖) | 6 | 1 | 0 | suspense:1 | gen_fail:5 |
| emotion(情感) | 5 | 3 | 3 | emotion:3 | gen_fail:2 |
| brainstorm(脑洞) | 3 | 3 | 1 | emotion:2, brainstorm:1 | - |

**shortfall(如实暴露, 不补样本)**: {'horror': 2}

## logic / 逻辑

### logic-01

请求类型：logic / 逻辑
随机关键词：勇气 / 光芒

【汤面】
敌机来袭的那个夜晚，灯塔看守人做的第一件事，竟是亲手熄灭了灯塔的灯。第二天，全城都在骂他是个贪生怕死的懦夫，他没有辩解一个字。后来，军方却悄悄给他戴上了一枚勇气勋章。

【汤底】
战争时期，港口的灯塔每晚都亮着，敌机的轰炸机其实一直借着这道光，在夜里准确找到港口的位置。灯塔看守人发现了这一点，便趁敌机来袭的那晚故意熄灭了灯，港口因此躲过了轰炸。可不知情的市民只看到他在危险中熄了灯，都骂他是贪生怕死的懦夫；只有军方知道真相，悄悄授予他勇气勋章。他也不辩解——说出熄灯的原因，就等于告诉敌人那道光有多重要。

【核心答案】
灯塔的光其实是敌机夜里定位港口的坐标，看守人主动熄灯让港口躲过轰炸，因此获勋，也因为不能泄露原因而沉默。

【最终观察】
primary_category: logic
categories: ['logic', 'suspense', 'emotion']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 4057, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 128, 'output_tokens': 1269}

### logic-02

请求类型：logic / 逻辑
随机关键词：中毒 / 打嗝

【汤面】
几个月来，她每天只往窗台的花盆里吐进一两口呛出来的咖啡。她自己安然无恙，可那盆花却一天天枯萎，最后莫名其妙地死透了。

【汤底】
丈夫长期在妻子的咖啡里下慢性毒药，剂量是按她每天喝完一整杯计算的。妻子喝咖啡时总会被烫得打嗝，呛出来的那一两口，她习惯吐进窗台的花盆里。几个月后，花盆里的花莫名其妙枯死了。她因此起疑，把咖啡送去化验，才发现自己早就被下了毒——而救了她一命的，正是那每天打的嗝。

【核心答案】
丈夫在咖啡里下慢性毒，剂量按整杯算；她每杯都被烫得打嗝、呛出一两口吐进花盆，花先被毒死，她才起疑化验。

【最终观察】
primary_category: suspense
categories: ['suspense', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 4033, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 128, 'output_tokens': 1417}

### logic-03

请求类型：logic / 逻辑
随机关键词：遗迹 / 工作

【汤面】
老人守着一栋四处漏雨的老宅，屋顶破了个洞，雨水一滴一滴落在床边。有人出钱要帮他修，他一口回绝；劝他搬走，他也不肯。他从不外出上班，却似乎从没为钱发过愁。

【汤底】
老人的老宅破旧漏雨，他一直想攒钱翻修，却始终舍不得离开。后来整片老宅被认定为文物遗迹，政府规定必须原样保存、不许改动任何一处。政府干脆聘请他当遗迹看护员，住在这里维持原状。于是他既不能修屋顶，也不许翻新任何东西——他的家就是遗迹，"什么都不动"恰恰是他的本职工作，所以他才会拒绝一切修缮，也从不离开去"上班"。

【核心答案】
老宅被列为文物遗迹，他被政府聘为看护员，维持原状就是他的工作，所以不许修、也不能走。

【最终观察】
primary_category: logic
categories: ['logic', 'brainstorm']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 4059, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 128, 'output_tokens': 1234}

## suspense / 悬疑

### suspense-04

请求类型：suspense / 悬疑
随机关键词：保鲜 / 地球仪

【汤面】
丈夫平静地向警方描述妻子独自环球旅行的行程，语气笃定，路线和日期都背得出来。可警察在她家里看到：书桌上她每晚都要转动的地球仪落了一层薄灰，冰箱里还放着她前一晚做好的饭菜。他解释说，那是她出发前特意留下来的。

【汤底】
丈夫报案称妻子独自去环球旅行了。真相是两人争吵中他失手将妻子推倒致死，慌乱之下把遗体藏进了自家冰柜，想用低温掩盖事实。警方发现冰箱里还有她前一天准备好的饭菜、她每晚都会转动着规划旅程的地球仪落了灰——她根本没出过门，时间线对不上，丈夫的谎言随之崩塌。

【核心答案】
妻子从未出门——丈夫争吵中失手致她死亡，遗体藏在自家冰柜，所谓环球旅行是他编造的谎言。

【最终观察】
primary_category: suspense
categories: ['suspense', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 342, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1485}

### suspense-06

请求类型：suspense / 悬疑
随机关键词：故障 / 手印

【汤面】
她这周第三次报修"电路故障"，电工检查了所有线路，什么问题都没找到。她却仍一口咬定家里有故障，还神秘兮兮地指着阁楼检修口让他看——那上面有一枚新鲜的手印，可奇怪的是，手印是在盖板的内侧。

【汤底】
女子知道涉案潜逃的丈夫一直藏在她家阁楼里。她既不敢继续窝藏，又不忍亲手报警，于是连日谎报家中"电路故障"，盼着维修工上门时能自己发现阁楼里的人。检修口内侧那枚新鲜手印，正是丈夫以为妻子已睡、半夜从阁楼探头查看动静时按下的。她反复报修，其实是借外人的手揭发丈夫。

【核心答案】
丈夫涉案潜逃藏在阁楼，妻子不敢继续窝藏又不忍亲手报警，反复报修"电路故障"，是想借电工上门发现他；内侧手印是丈夫半夜从阁楼探头时按下的。

【最终观察】
primary_category: suspense
categories: ['suspense', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 4057, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 128, 'output_tokens': 1448}

### suspense-08

请求类型：suspense / 悬疑
随机关键词：再遇 / 汤面

【汤面】
一位女顾客走进一家面馆，与掌勺的老板素不相识。端上来的那碗汤却让她僵住了——汤面的葱花，摆出的正是她从小到大的小名。那是只有爸爸才会这样摆的，可她爸爸十几年前就死了。她什么也没问，把汤喝完，起身离开。

【汤底】
父亲当年替人担保欠下巨债，追债人放话要对他妻儿不利。他趁邻镇一场火灾"失踪"，被认定身亡，从此隐姓埋名在千里外的小面馆掌勺。女儿多年后偶然走进这家店，看见一碗汤的表面用葱花摆出了她小名的图案——那是父亲哄她喝汤时的独门习惯，她由此认出了他。父亲装作不识，她也没有揭穿：只要他继续"死着"，家人就安全。她默默喝完汤离开，此后每年父亲"忌日"，店里都会收到一份匿名汇款。

【核心答案】
面馆老板正是"死去"多年的父亲:他当年为躲债假死隐姓埋名,葱花摆小名是父女间的独门习惯,她认出后选择不揭穿。

【最终观察】
primary_category: emotion
categories: ['emotion', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: fix
model: deepseek-v4.1-flash
usage: {'input_tokens': 378, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1453}

## horror / 恐怖

### horror-10

请求类型：horror / 恐怖
随机关键词：英语 / 家人们

【汤面】
女孩每晚直播教英语，镜头前总亲热地喊观众"家人们"。那晚她在黑板上写下例句"they are not my family"，直播随即中断——此后再也没有人见过这个女孩。

【汤底】
女孩每晚直播教英语，总把观众喊作"家人们"。真相是：她幼年被拐卖，买主冒充她的"父母"，还逼她直播教英语赚钱。因为"家人"看得懂中文，她多年偷偷在英文例句里夹带求救暗号，可观众一直没看懂，还以为是教学风格。那晚"家人"终于发现了她写的"they are not my family"，直播从此中断，世上再没人见过这个女孩——她喊了多年的"家人们"，只有屏幕外那些人才是真的。

【核心答案】
她自幼被拐卖，买主冒充"父母"逼她直播赚钱；她多年在英文例句里藏求救暗号，"家人们"实为监视者，暗号被发现后她消失了。

【最终观察】
primary_category: suspense
categories: ['suspense', 'emotion']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 4063, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 128, 'output_tokens': 1522}

## emotion / 情感

### emotion-15

请求类型：emotion / 情感
随机关键词：东西 / 细节

【汤面】
奶奶去世后，儿女回家整理遗物，却看见年迈的父亲每天仍把杯柄一一转向右边，椅子推到同样的角度，连门缝都卡着固定的宽度。他们劝他："妈不在了，不用再这样了。"父亲只是摇头，继续把每样东西摆回原位。

【汤底】
奶奶中年失明后，爷爷几十年如一日，每天把家里所有东西摆回原位：杯柄统一朝右、门缝留固定宽度、椅子永远推到同一角度——全是为了让奶奶能凭记忆和手指，在"一成不变的细节"里独自安全地生活。奶奶去世后，儿女发现父亲依然每天照做，以为他糊涂了。其实爷爷是想，等自己也老了看不清的那天，能靠这些熟悉的细节，还在这个和奶奶一起生活过的家里找到方向。

【核心答案】
奶奶失明后，爷爷几十年把每样东西摆回原位，好让奶奶凭记忆独自安全生活；奶奶走后他照旧摆，是想等自己老眼昏花时还能靠这些细节认路。

【最终观察】
primary_category: emotion
categories: ['emotion', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 363, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1293}

### emotion-16

请求类型：emotion / 情感
随机关键词：瓜农 / 黑暗

【汤面】
瓜田夜夜进贼，瓜农躺在床上听得清清楚楚，却从不出门抓。院里那盏灯坏了大半年，他一直不肯修。更怪的是，他偏偏把最甜的几个瓜全搬到了田角。

【汤底】
夜里进瓜田的"贼"不是外人，是瓜农多年不归的儿子。儿子当年和他大吵一架摔门出走，如今想家又拉不下脸，只敢趁黑来摘瓜。瓜农从第一晚就听出了那熟悉的脚步声，却一直装作不知道——他故意不修院子里那盏"坏了"的灯，还悄悄把最甜的瓜挪到田角，好让儿子摘得顺当、常来。他要的从来不是瓜，是儿子能一次次回来。

【核心答案】
夜里的"贼"是瓜农离家出走后拉不下脸的儿子，老人听出脚步声，故意不修灯、把甜瓜挪到田角，好让他常回来摘。

【最终观察】
primary_category: emotion
categories: ['emotion']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 4063, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 128, 'output_tokens': 1365}

### emotion-19

请求类型：emotion / 情感
随机关键词：监视 / 超生

【汤面】
小时候，每当他被允许走到院子里玩，家里的某个大人总会放下手里的活，隔着窗户一直盯着他。他跑到哪，那双眼睛就跟到哪，一次都没漏过。他始终不明白，为什么全家人都不许他出大门半步。

【汤底】
他是超生的第二个孩子。当年一旦被查出，不但要交巨额罚款，孩子还可能被带走在别处寄养，所以家里死活不许他踏出家门半步。他从小被全家人轮流"监视"，连到院子里玩都有人隔着窗户盯着，一直以为家人嫌他是累赘，委屈了二十年。直到成年后翻出一张当年的"值班表"——从天不亮到深夜，每个人干完农活都抢着站岗，为的是计划生育干部一来，就能第一时间让他躲进地窖。所谓监视，从头到尾都是守护。

【核心答案】
他是超生的第二个孩子，家人轮流盯着、不许他出大门，是为了计划生育干部一来就能让他躲进地窖——那不是监视，是守护。

【最终观察】
primary_category: emotion
categories: ['emotion', 'logic', 'suspense']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 366, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1404}

## brainstorm / 脑洞

### brainstorm-20

请求类型：brainstorm / 脑洞
随机关键词：小熊 / 笑

【汤面】
奶奶的葬礼上，孙子抱着一只毛绒小熊按下了开关，忽然放声大笑起来。宾客们愣了一下，随后整个灵堂里的人都跟着笑了。没有人解释为什么，大家边笑边流泪。

【汤底】
奶奶重病时知道自己撑不了多久，就提前给每个孙辈缝了一只录音小熊，里面录着她说好的笑话和告别——她想让自己的葬礼上有人笑，而不是只剩哭声。葬礼上，孙子抱着小熊按下了开关，听到奶奶用虚弱的声音讲完那个她在病床上练了几十遍的笑话，忍不住大笑起来。宾客认出这是奶奶留下的安排，先是愣住，随后也笑着流着泪，一起笑了起来。

【核心答案】
小熊是奶奶病中录下自己讲笑话的遗物，孙子按下开关放出笑声，宾客听懂这是她最后的安排，便陪着笑也陪着哭。

【最终观察】
primary_category: emotion
categories: ['emotion', 'brainstorm']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 331, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1308}

### brainstorm-21

请求类型：brainstorm / 脑洞
随机关键词：器官捐献 / 鱼雷

【汤面】
海峡对岸的海滩上，一条鱼雷破水而出，冲上沙滩。守在岸边的一群医生却没有人逃跑或报警，反而扑上去拆开弹体，有人当场喜极而泣。

【汤底】
海岛医院里有位病人脑死亡，他生前登记了器官捐献。可台风切断了轮渡和直升机，他的心脏必须在几小时内送到海峡对岸的接收医院，否则就会失活。岛上恰好有一条当年守岛部队留下的废弃鱼雷发射管，直通对岸——医生们拆掉弹头，把装着供体心脏的恒温箱塞进雷体，用压缩空气把这条"鱼雷"发射了出去。所以那条被发射的鱼雷里没有炸药，装的是救命的器官，人们才会欢呼落泪。

【核心答案】
那不是武器：鱼雷里装的是待移植的供体心脏。台风断了航运，医生借废弃鱼雷发射管把心脏射过海峡，岸边等着的医生都是接收方人员。

【最终观察】
primary_category: brainstorm
categories: ['brainstorm', 'emotion', 'logic']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 355, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1513}

### brainstorm-22

请求类型：brainstorm / 脑洞
随机关键词：婚礼 / 神秘符号

【汤面】
婚礼上没有致辞，也没有人朗读宣誓。宾客只看见新娘一直拉着新郎的手，在他掌心一遍遍地画着谁也看不懂的符号。新郎盯着那些符号，突然泣不成声。

【汤底】
新郎在婚礼前一周因事故突然失聪，谁都不敢提。新娘瞒着所有人，偷偷去聋哑学校学会了手语。婚礼上没有致辞、没有宣誓朗读，新娘只是拉着新郎的手，在他掌心一下一下地比划。宾客看不懂，只看见新娘不停画着"神秘符号"，而新郎泣不成声——那些符号，是用他此刻唯一能"听见"的方式，说出的结婚誓言。

【核心答案】
新郎婚礼前意外失聪，新娘偷偷学了手语、在他掌心比划，那些"符号"就是只用他能"听见"的方式说出的结婚誓言。

【最终观察】
primary_category: emotion
categories: ['emotion', 'brainstorm']
difficulty: medium

【生产结果】
protocol_version: haiguitang-v2
prompt_version: haiguitang-generation-v3
review_decision: pass
model: deepseek-v4.1-flash
usage: {'input_tokens': 321, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 3840, 'output_tokens': 1312}

---

共 13 道 accepted。requested != observed 是**合法状态**, 报告原样呈现, 不改分类、不挑题。