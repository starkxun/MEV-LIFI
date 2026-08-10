# SVR 与 Atlas:从零把这套东西查清楚

> 假设你会看清算合约、懂健康度和清算奖励,但**从没听说过 SVR 和 Atlas**。
> 这份文档带你把它们从零查明白,**每个数字都给出可以自己跑的命令**。
> 配套脚本:[`svr_probe.py`](../../svr_probe.py)(只读,不发任何交易)

---

## 第 0 课:为什么要学这个

如果你只知道传统的清算玩法,你脑子里的模型大概是:

```
预言机推新价格 → 某个仓位健康度跌破 1 → 谁先发交易谁清算 → 赚清算奖励
```

**这个模型在 Aave 的主要市场上已经过时了。**

2026 年 3 月起,Aave 在 Arbitrum 和 Base 上把喂价换成了 **SVR 喂价**,
官方数据:**89% 的清算奖励被协议回收了**,不再归 searcher。

更要命的是:**你看不出来。** SVR 喂价和普通喂价长得一模一样,
你按老办法盯 Chainlink 官方的 ETH/USD 代理,会发现自己盯的是一个
**Aave 根本不读的地址** —— 这正是我们这个项目最初踩的坑,
白测了 180 天的数据。

所以这一课的第一个目标就是:**学会问协议"你到底读哪个喂价"。**

---

## 第 1 课:OEV 是什么

先建立概念。MEV 你可能听过,**OEV(Oracle Extractable Value)是它的一个子集**:

> **由预言机更新这个动作本身创造出来的、可被提取的价值。**

具体到清算:

```
ETH 从 $2,000 跌到 $1,900
        ↓
预言机把新价格写上链   ← 就是这个瞬间
        ↓
一批仓位的健康度同时跌破 1
        ↓
清算奖励(比如债务的 5%)凭空出现,谁抢到归谁
```

**关键点:这个价值不是清算人创造的,是预言机更新创造的。**
清算人只是跑得快而已。

这就带出一个很自然的质疑 ——

> 既然是预言机更新创造的价值,凭什么全归跑得快的人?
> **借出这笔钱、承担坏账风险的是协议啊。**

SVR 就是这个质疑的产物。

---

## 第 2 课:SVR 的架构 —— 双聚合器

SVR = **S**mart **V**alue **R**ecapture(智能价值回收),Chainlink + Aave 的联合机制。

**以前只有一条路:**

```
Chainlink 节点网络(Data DON)
        │
        └──→ 公开内存池 ──→ 聚合器合约 ──→ 所有人同时看见
                                              ↓
                                        谁快谁抢到清算
```

**SVR 之后是两条路,同一份价格报告发两遍:**

```
Chainlink Data DON
        │
        ├──→ 公开内存池 ──→ 【标准聚合器】     (和以前一样,谁都能看)
        │
        └──→ 私有通道   ──→ 【SVR 聚合器】     (先不公开)
                                  ↓
                        searcher 竞价:
                   「让我的清算跟在这次价格更新后面,
                     我愿意交出利润的 XX%」
                                  ↓
                        出价最高的赢,他的清算
                        和价格更新打包进同一个区块
```

**Aave 的合约读的是【SVR 聚合器】。**

所以当公开世界看到价格变了的时候,拍卖赢家已经把清算做完了。

**兜底机制**:如果私有通道失败或超时,SVR 喂价会回落到公开价格
(有个可配置的延迟)。Aave 文档说这个延迟在 L2 上**可以短到 10 秒,
但他们选择保持和以太坊一致的参数**。

> **这就是漏给普通 searcher 的那部分** —— 也是唯一还能靠老办法抢的部分。

---

## 第 3 课:动手 —— 问 Aave 你读哪个喂价

**这是整份文档最重要的一个操作。**

### 为什么不能查文档

因为协议随时可以换源,而且**文档往往滞后**。唯一可靠的是问链上。

### 调用链

Aave 的价格源要穿三层才能问到:

```
Pool.ADDRESSES_PROVIDER()      → PoolAddressesProvider 地址
    .getPriceOracle()          → AaveOracle 地址
        .getSourceOfAsset(WETH)→ ★ 这才是真正读的喂价
```

### 跑一下

```bash
python3 svr_probe.py feeds --chains ARB OPT
```

实际输出:

```
  ARB
    AaveOracle       0xb56c2f0b653b2e0b10c9b928c8580ac5df02c7c7
    Aave 读的喂价     0xbd41b1548a5a06544cbcf87c0c54864312842c00
    公开 Chainlink   0x639fe6ab55c921f74e7fac1ee960c0b6293ba612
    🔴 不是同一个 —— 这就是 SVR 喂价
    当前 $1,924.6200  518s 前更新
    对照·公开代理 $1,924.6467  1177s 前更新   → 差 -0.1 bps

  OPT
    Aave 读的喂价     0x13e3ee699d1909e989722e753853ae30b17e08c5
    公开 Chainlink   0x13e3ee699d1909e989722e753853ae30b17e08c5
    ✅ 同一个 —— 这条链没上 SVR
```

### 怎么读这个结果

| 现象 | 含义 |
|---|---|
| Aave 读的 ≠ 公开代理 | 这条链的这个资产**走 SVR**,清算被拍卖 |
| 两者一致 | 没上 SVR,还是老规矩,拼延迟 |

注意 Arbitrum 那行的更新时间:**SVR 518 秒前更新,公开代理 1177 秒前** ——
**SVR 更新更勤**。实测中位间隔 SVR 90 秒、公开 120 秒。

> ### 🔑 可迁移的一条
> **任何"协议依赖外部数据"的场景,都要问协议本身而不是问数据提供方。**
> 我们最初的错误就是从"Chainlink 官方 ETH/USD 代理是哪个"倒推,
> 得到一个 Aave 根本不用的地址,然后在上面测了 180 天。

---

## 第 4 课:为什么不能靠接口认出 SVR

我当时的第一反应是:"SVR 肯定有特殊方法,我把两个合约的函数选择器 diff 一下。"

**做了,结果是:两个聚合器各 53 个选择器,完全一样。**

差异只有字节码长度:

```
SVR 聚合器   23,186 字节
公开聚合器   22,337 字节
            ────────
差           +849 字节
```

一度让我怀疑自己是不是认错了地址。后来 Chainlink 文档给了答案:

> *"Chainlink SVR Feeds maintain the **same aggregator interface** and data
> structure as standard Chainlink Price Feeds."*

**接口一致是设计如此** —— 这样协议接入时只要改个地址,不用改代码。

> ### 🔑 可迁移的一条
> **"我查不出差异"有两种可能:我方法不对,或者设计上就没差异。**
> 分清这两种,靠的是找到设计意图的说明,而不是继续加大力气查。
> 我当时差点因为查不出差异而推翻一个正确的结论。

**所以判据只能是:Aave 指向的地址 ≠ 公开代理。** 没有别的办法。

---

## 第 5 课:L2 上完全不一样 —— Atlas 登场

以太坊上,SVR 的私有通道用的是 Flashbots MEV-Share。

**但 L2 没有公开内存池,也没有 builder 竞价** —— 排序器说了算。
所以 Chainlink 换了一套东西。Aave 治理文档写得很清楚:

> *"Instead of using Flashbots… it uses Chainlinkʼs native infrastructure,
> partially from **Atlas**, a system acquired by Chainlink from Fastlane…
> settlement/ordering of transactions happens in a **smart contract layer** now
> (akin to a multicall/ERC4337 setup), **without the need for a private mempool**."*

翻译:

```
以太坊:  拍卖在链下(Flashbots)→ 赢家的 bundle 由 builder 打包
L2:     拍卖在链下,但**结算和排序在一个智能合约里完成**
        这个合约就是 Atlas
```

### 怎么找到它(不靠文档)

我是这么找到的:

```
1. 扫 Aave Pool 的 LiquidationCall 事件
2. 看这些交易的 tx.to 是不是 Aave Pool 本身
3. 发现有一批不是 → 对那个合约调 name()
4. 返回 "Atlas ETH"  ← 找到了
```

跑一下:

```bash
python3 svr_probe.py atlas
```

```
=== Atlas on ARB: 0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1 ===
  name()                 Atlas ETH
  VERIFICATION()         0xac116abb948e26b023c9c4815ab001845fbf54ff
  SIMULATOR()            0x57fa2abf1dc109c5f7ea2fb6a72358d2c624971d
  ESCROW_DURATION()      128 区块  ≈ 32 秒
  bondedTotalSupply()    13.6462 ETH        ← 全网 solver 押金总额
```

**全网所有 solver 押的钱加起来只有 13.6 ETH(约 $26,000)。**
这个数字本身就说明门槛有多低。

---

## 第 6 课:Atlas 的四个角色 —— 我在这栽了两次

这是整套系统最容易搞混的地方,**我在同一天错了两次**,所以单独一课。

```
┌─────────────────────────────────────────────────────────┐
│  ① solver(solverFrom)                                  │
│     真正竞价的身份。押金记在这个地址下。                    │
│     ← 你要成为的就是这个                                  │
├─────────────────────────────────────────────────────────┤
│  ② solver 合约(solverTo)                               │
│     实现 atlasSolverCall 回调的合约,执行你的清算逻辑        │
├─────────────────────────────────────────────────────────┤
│  ③ bundler                                              │
│     把 metacall 交易实际发上链的人 = tx.from              │
│     **可以不是你**                                        │
├─────────────────────────────────────────────────────────┤
│  ④ 执行环境(ExecutionEnvironment)                       │
│     Atlas 给每次执行创建的沙箱合约。                        │
│     Aave 的 LiquidationCall 里记录的 liquidator 是**它**   │
└─────────────────────────────────────────────────────────┘
```

### 我的两次错误

**错误一:把 bundler 数当参与者数。**

我看到 Atlas 的交易由 **10 个不同 EOA** 发出,就写下"没有单一垄断者"。
后来解出 `SolverTxResult` 事件才发现:**7 天里真正中标的 solver 只有 3 个,
Top1 占 84.6%。** 那 10 个是 bundler。

**错误二:查押金查错了地址。**

我用 bundler 的 EOA 和 `LiquidationCall` 里的 liquidator 去查
`balanceOfBonded`,全返回 0,于是写"押金无法验证"。
换成 `solverFrom` 就出来了:

```
0x83eb6513…  balanceOfBonded = 0.2617 ETH
0xb4a3c10c…  balanceOfBonded = 0.0893 ETH
0x35666ffa…  balanceOfBonded = 0.1249 ETH
```

> ### 🔑 可迁移的一条
> **读不到数据时,先怀疑"我查的是不是同一个东西",再怀疑方法名。**
> 两次错误同一个病根:把系统里的**不同角色**当成了同一个。
> 看到"很多地址"先问清楚它们在系统里扮演什么,别把角色数当参与者数。

---

## 第 7 课:动手 —— 解剖一笔真实的 SVR 清算

拿 160 天里**最大的一笔**当教材:

```bash
python3 svr_probe.py anatomy 0x3125e7348f9f3f3c2f59d208148278513845738b01291bd608ebc70bce58c841
```

```
  发起 EOA  0x12cDc19f5B8B570747115F2fccAe1Cd14318D44A
  目标合约  0x8ad1aE9D97C79aA68A0a151E83ff3942f68F86C1   ← Atlas,不是 Aave Pool
  gasUsed 1,789,466  实付 0.000036 原生币
  46 条日志,16 个合约

  ── LiquidationCall ──
    还 376,499.058704 USDC   拿 5.566116 WBTC
    清算奖励参数 7.0%
    隐含成交价 67,641.2468 USDC/WBTC
    反推预言机价 72,376.1341
    毛清算奖励 26,354.93 USDC

  ── SolverTxResult ──
    solverFrom 0xfaed98a5f7b49fb00ee1ee444c4616de52198c91   ← 真正的 solver
    bidAmount  11.018461                                     ← 交出去的钱
```

### 这里面有个自检技巧,一定要学会

```
隐含成交价 = 还的债 ÷ 拿到的抵押 = 376,499.06 ÷ 5.566116 = 67,641.25
反推预言机价 = 隐含价 × (1 + 清算奖励) = 67,641.25 × 1.07 = 72,376.13
```

**然后去对一下当天 BTC 的真实价格。** 如果对得上,说明你的解码、
小数位、清算奖励参数全都取对了。如果差很远,说明某一步错了。

> ### 🔑 可迁移的一条
> **解码链上数据时,永远找一个"外部可验证的量"来自检。**
> 这里就是币价 —— 它来自完全独立的信息源。
> 光看自己解出来的数是否"看起来合理",发现不了系统性错误
> (比如小数位差 10^12,数字照样"看起来像个价格")。

### 钱怎么分的

```
毛清算奖励        26,354.93 USDC
出价              11.018461 ETH  ≈ $21,177(按 ETH $1,922 折算)
gas                    $0.07
──────────────────────────────
solver 净得        ≈ $5,178      回收率 ≈ 80%
```

**这是 160 天里最大的一笔生意,solver 赚了约 $5,178。**

---

## 第 8 课:钱到底怎么分 —— 实测

别用官方口径的百分比套单笔。**自己算。**

```bash
python3 svr_probe.py solvers --days 7
```

### 三笔小额清算的完整账(实测)

| 还债 | 拿到 | 毛奖励 | **出价** | gas | **solver 净得** | 回收率 |
|---|---|---|---|---|---|---|
| 2,836.60 USDC | 1.6163 WETH | $141.83 | $131.05 | $0.05 | **$10.73** | 92.4% |
| 133.41 USDC | 0.0730 WETH | $6.67 | $6.08 | $0.06 | **$0.54** | 91.1% |
| 70.58 USDC | 0.0012 WBTC | $4.94 | $4.44 | $0.06 | **$0.45** | 89.8% |

**实测 92.3%,官方口径 89.2% —— 对上了。**

### 一个重要观察:落败的出价不上链

7 天扫到 1,931 条 `SolverTxResult`,**全部 `success = true`**。

因为拍卖在链下跑,**只有赢家的交易会上链**。这意味着:

```
✅ 没中标 = 不花钱
❌ 但也意味着你无法从链上看到竞争有多激烈
```

这是个好消息(失败零成本),也是个坏消息(你看不到对手的出价分布,
只能看到成交价)。

---

## 第 9 课:怎么真的成为一个 solver

这一节是流程,**没有实盘验证过,按官方文档整理 + 链上核实的部分标注了**。

### 门槛(来自 [Chainlink SVR Searcher Onboarding: Atlas](https://docs.chain.link/data-feeds/svr-feeds/searcher-onboarding-atlas))

| 项 | 要求 | 核实状态 |
|---|---|---|
| **准入** | 无申请、无白名单、无 KYC | 文档 |
| **押金** | Base/Arbitrum **0.1 ETH**,调 `depositAndBond` | ✅ 链上看到实际押 0.089~0.26 ETH |
| **锁定期** | `ESCROW_DURATION()` | ✅ 链上 = 128 块 ≈ 32 秒 |
| **合约** | 实现 `atlasSolverCall` 回调,SDK 有 `SolverBase` 基类 | 文档 |
| **行情** | WebSocket `wss://svr-bid-endpoint.chain.link/ws/solver` | 文档,未连过 |
| **出价** | JSON-RPC `solver_submitSolverOperation`,EIP-712 签名 | 文档 |
| **排名** | *"determined by bid competitiveness, **not reputation**"* | 文档 |

### 完整流程

```
1. 写 solver 合约
   └ 实现 atlasSolverCall(solverOpFrom, executionEnvironment,
                          bidToken, bidAmount, solverOpData, forwardedData)
   └ 合约里做:闪电贷 → liquidationCall → 卖掉抵押物 → 付 bidAmount
   └ 执行结束前必须调 Atlas 的 reconcile

2. 押金
   └ 调 Atlas.depositAndBond,押 0.1 ETH
   └ gas 从押金余额里自动扣

3. 订阅行情
   └ 连 wss://svr-bid-endpoint.chain.link/ws/solver
   └ 发 solver_subscribe(["userOperations"])
   └ 收到的通知里带 **aggregator 地址 + median price**

4. 估值 + 出价
   └ 用推来的新价格算:哪些仓位可清算、能赚多少
   └ 决定出价 → solver_submitSolverOperation

5. 中标了 Atlas 会调你的 atlasSolverCall
```

### 第 3 步最反直觉,单独强调

> **Chainlink 把新价格直接推给你,还附带 median price。**

这意味着在这个赛道里,**"抢先看到价格"这件事不存在了** ——
所有参与者同时收到同一份推送。

```
        抢公开清算              SVR 拍卖
信息    自己抢先看见      →    推给你,带 median price
胜负    延迟,赢家通吃    →    出价高低
落败    亏 gas           →    不花钱
核心    基础设施          →    **估值准确度**
```

**门槛从"基础设施"变成了"算得准"。**

---

## 第 10 课:这门生意值多少钱 —— 兼一堂取样课

这一课的教训比数字重要。

### 我第一次算错了 39 倍

用 **14 天窗口**扫 Arbitrum,得到:

```
38 笔清算,毛奖励 $716.57  →  年化 $18,682  →  solver 一年 $1,439
```

我在同一段里写了"14 天是平静期,这是严重低估的下界,不能当结论用",
**然后在下一节的结论表里直接把它当结论用了。**

### 扫 160 天之后

```
3,896 笔清算,毛奖励 $316,293  →  年化 $721,542

单笔中位  $ 0.03
单笔均值  $81.18       ← 差 2,700 倍
最大一笔  $26,352
```

**低估了 39 倍。**

### 为什么会差这么多

```
金额最大  1 天  占全年 35.4%
金额最大  5 天  占全年 80.0%
金额最大 10 天  占全年 90.2%
笔数最多一天    911 笔
```

**一年的生意压在 5 天里。我那 14 天里一个暴发日都没有。**

> ### 🔑 可迁移的一条(本文最重要的一条)
> **分布越偏,窗口取样越危险。**
>
> 判断这类市场的规模,**样本必须长到能覆盖至少一次暴发**,
> 否则你测的是"平时",而平时恰恰不是这门生意。
>
> 而且要警惕一个心理陷阱:**加了免责声明不等于没犯错。**
> 我明知样本不可靠还是把数字写进了结论 —— 正确做法是
> 要么别出这个数,要么去取够样本再出。

### 另一个同源错误:用笔数当价值

我还判断过"SVR 没覆盖的市场都很小",依据是**笔数**:

```
Optimism  2.4 笔/天   vs   Arbitrum  2.7 笔/天   → "差不多大"
```

按 Aave 官方**金额**统计:

```
Optimism  $0.61M/年,永不上 SVR,100% 归 searcher
Arbitrum  ~$60k/年 solver 残留(已被 SVR 吃掉 89%)
```

**Optimism 的可得池子是 Arbitrum 的 10 倍。**

同一天,同一个病根:**中位 $0.03、均值 $81 的分布下,任何按笔数的判断都会错。**

---

## 第 11 课:新市场上线初期真的更好赚吗

有个很有吸引力的假说:**新市场刚上线时 searcher 少,回收率低,
所以提前做好集成、第一天就进场,能吃到红利。**

这个假说**在两份 Aave 官方文档里都找不到**。但不用猜 ——
Base/Arbitrum 就是 2026-03 上线的,回测就行。

### 实测结果:假说成立

```
第 0 天    1 个 solver     ← 场上只有一个人
第 1 个月   7 个
第 2 个月  22 个
第 3 个月  34 个
第 4 个月  43 个   ← 峰值
第 6 个月   4 个   ← 大批退出
```

回收率确实在爬:`25% → 45% → 74% → 80% → 93%`

### 但钱不在爬坡期

```
爬坡期(周 3–12,2.3 个月)  回收 74.8%  毛奖励 $ 48,520  solver 残留 $12,217
成熟期(周 13+)             回收 93.6%  毛奖励 $227,281  solver 残留 $14,527
```

**爬坡期只占全期毛奖励的 17.6%;周 13 一周就占 76%,而那时回收率已经 93.6%。**

而暴发周的最大赢家 `0x35666ffa…` 是**第 79 天(≈周 11)才首次中标**的
—— 它不是第一天到场的,是**暴发前两周**到场的。

> ### 🔑 可迁移的一条
> **"先发优势"要看红利期和收获期重不重合。**
> 这里不重合:先发优势期有窗口没有量,有量的时候窗口已经关了。
> 判断任何"抢先进入"的机会,都要问一句:**在我领先的那段时间里,
> 到底有多少交易量?**

---

## 附:命令速查

```bash
# Aave 到底读哪个喂价(判断有没有上 SVR)
python3 svr_probe.py feeds --chains ARB OPT BASE

# Atlas 合约身份 + 押金门槛
python3 svr_probe.py atlas --chain ARB

# 谁在中标、出价多少、押了多少
python3 svr_probe.py solvers --chain ARB --days 7 --save shadow/solvers.json

# 解剖一笔清算(自动判断是不是走 Atlas 拍卖)
python3 svr_probe.py anatomy <txhash>
```

### 关键地址(截至 2026-08-10,**用上面的命令自己复核**)

```
Aave V3 Pool(多链同址)   0x794a61358D6845594F94dc1DB02A252b5b4814aD
Arbitrum AaveOracle       0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7
Arbitrum SVR ETH/USD      0xbd41b1548a5a06544cbcf87c0c54864312842c00
Arbitrum 公开 ETH/USD     0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612
Arbitrum Atlas            0x8ad1aE9D97C79aA68A0a151E83ff3942f68F86C1
```

### 关键事件签名

```
LiquidationCall(address,address,address,uint256,uint256,address,bool)
  topics: [sig, collateralAsset, debtAsset, user]
  data:   debtToCover, liquidatedCollateralAmount, liquidator, receiveAToken

SolverTxResult(address,address,address,address,uint256,bool,bool,uint256)
  topics: [sig, solverTo, solverFrom, dAppControl]
  data:   bidToken, bidAmount, executed, success, result
       ↑ solverFrom 才是 solver 身份,押金记在它名下

AnswerUpdated(int256,uint256,uint256)
  topics: [sig, current, roundId]     ← 两个都是 indexed!
  data:   updatedAt                   ← data 里只有 32 字节,别解错位置
```

---

## 这一课学到的方法,比 SVR 本身更值钱

回头看,这次调查真正可迁移的是四条:

1. **问协议本身,不问文档** —— 协议随时换源,文档滞后
2. **分清系统里的角色** —— 别把 bundler 数当参与者数
3. **解码要有外部自检量** —— 用币价校验解码,而不是"看起来合理"
4. **偏态分布下,窗口取样必须覆盖极端事件** —— 否则你测的是"平时"

这四条和 SVR 一点关系都没有,**但它们是这次调查里唯一不会过期的东西。**
