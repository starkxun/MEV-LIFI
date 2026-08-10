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

## 第 9.5 课:动手连上去看看(已实测)

上面那些是文档说的。**下面是真连上去之后看到的。**

```bash
python3 svr_listen.py --seconds 90
```

> [`svr_listen.py`](../../svr_listen.py) **在代码层面就不可能出价** ——
> 它没有实现 `solver_submitSolverOperation`,也不读私钥。只订阅,只收。

### 结论一:匿名就能订阅

```
[16:53:12] ✅ WebSocket 已建立(未鉴权)
[16:53:12] → 已发送订阅请求
[16:53:14] 推送 #1  ...
```

**不需要押金、不需要注册、不需要任何 header。** permissionless 是真的,
而且连"先押 0.1 ETH"都不是订阅的前置条件 —— 押金是**出价**才需要的。

> 这一步值得单独跑,因为它把"文档说 permissionless"变成了"我连上了"。
> 很多集成卡在这里:文档写得漂亮,实际要邮件申请。**这个没有。**

### 结论二:推送里到底有什么

90 秒收到 15 帧,结构是:

```json
{
  "auction_id": "579bc6f7-fa13-4243-b191-b65e03b79159",
  "partial_user_operation": {
    "chainId": "0x38",
    "control": "0x7d50b32444609a9b53bcf208c159c8d0d0767835",
    "from":    "0x0000000000000000000000000000000000000000",   ← 留给你填
    "gas":     "0x7a120",
    "deadline":"0x6dc3da6",
    "to":      "...",
    "userOpHash": "...",
    "hints": {
      "aggregator":  "0x10cAD61aF7b534F18DB2E39e9b8515a78B116433",
      "medianPrice": "0xdc966b5422be7bed000",
      "rawReport":   "...",
      "forwardData": "0x6fadcf72..."
    }
  }
}
```

### 结论三:medianPrice 的小数位 —— 我在这里错了一次

第一批样本全是 BNB Chain 的喂价,除以 1e18 得到:

```
0x290a47E6…  0x682b866f16cba44000  ÷1e18  =  $1,921.60   ← ETH/USD
0x10cAD61a…  0xdc966b5422be7bed000 ÷1e18  =  $65,105.96  ← BTC/USD
0x494aE7aF…  0x20b8c331c3fe6e6000  ÷1e18  =  $603.61     ← BNB/USD
```

数字很漂亮,和链上一致,**我就写下了"medianPrice 是 18 位小数"。错了。**

抓到 Arbitrum 的推送后,同样除以 1e18 全变成 $0.00:

```
聚合器                      raw              decimals()  正确解法
0xa5E1a369…(Aave SVR ETH)  0x2cac671dc0     8           ÷1e8  = $1,918.71  ✅
0x0b6eaC11…(另一个 ETH/USD) 0x6803a88da8c5…  18          ÷1e18 = $1,918.73  ✅
0xE7c522c6…(BTC/USD)       0x5ea9af16f00    8           ÷1e8  = $65,051.80 ✅
0x62619470…(ARB/USD)       0x796dac         8           ÷1e8  = $0.0796    ✅
```

> ### 🔑 正确的规则
> **`价格 = medianPrice / 10 ** aggregator.decimals()`**
> —— 小数位要**从那个聚合器自己链上读**,不能假设。
> 同一条链上 8 位和 18 位的喂价是混着的
> (上表里 `0xa5E1a369` 是 8 位、`0x0b6eaC11` 是 18 位,都是 ETH/USD)。

**我为什么会错:第一批样本恰好全是 18 位的。**
这是本文档第三次栽在"样本不够宽"上(前两次见第 10 课、第 9.5 课结论五)。
区别是这次错误留下了痕迹 —— 除出来是 $0.00,一眼就看得出不对。

> **可迁移的一条:让错误"显眼"比让代码"聪明"更重要。**
> 如果我当初写的是"猜一个能让数字落在合理区间的小数位",
> Arbitrum 那批会被猜成某个看起来正常的数,错误就永远藏住了。

### 结论四:最关键的一条 —— 你拿到的不是价格,是那笔交易

`forwardData` 以 `0x6fadcf72` 开头。查一下这是什么:

```python
Web3.keccak(text="forward(address,bytes)")[:4]  →  0x6fadcf72
```

**这就是 Chainlink Forwarder 的方法选择器** —— 也正是我们在第 5 课查
"谁在更新聚合器"时,链上看到的那个方法。

所以推送给你的东西是:

```
forwardData = forward(聚合器地址, 预言机报告)
              ↑
              **这笔预言机更新交易的完整 calldata,而它还没上链**
```

> ### 🔑 这才是 SVR 拍卖的本质
> 不是"你比别人早看到价格",而是 —— **拍卖方把即将上链的那笔预言机更新
> 交易交给你,让你出价买"紧跟在它后面"的位置。**
>
> 所有参与者拿到的是**同一份**数据、**同一时刻**。
> 所以竞争维度只剩一个:**你能算出这次更新值多少钱,而且敢出多高。**

### 结论五:推送节奏和覆盖范围

```
15 条 / 66 秒  =  每 4.7 秒一条
```

比单个喂价的更新频率(约 90 秒)快得多,因为它覆盖**整条链上所有喂价**。

90 秒的样本里 **15 帧全部是 BNB Chain(chainId 0x38)**。

当时我差点写下"订阅只推 BNB"。**忍住了,先跑长的:**

```bash
python3 svr_listen.py --seconds 900 --out shadow/svr_feed_long.jsonl
```

结果完全不同:

```
BNB Chain  ×15
Base       ×7
Arbitrum   ×5      ← 出现了
0x8f = 143 ×3      ← Monad
```

**订阅是全局的,一条连接覆盖所有 SVR 链。** 90 秒里全是 BNB 纯属巧合。

> ### 🔑 这是第 10 课那条教训的第二次应用
> 90 秒样本 → "只推 BNB"(错)
> 15 分钟样本 → "全局推送,4 条链"(对)
>
> **同一个项目里,我因为短样本已经错过两次**(14 天窗口算错 39 倍、
> 用笔数判断市场大小)。第三次忍住了,是因为在写文档时逼自己
> 标注"这个结论的样本有多大" —— **把样本量写进结论旁边,
> 是防止自己过度推断的最便宜的办法。**

这也意味着:**你写一个 solver,天然就能同时覆盖 Arbitrum / Base / BNB / Monad。**
集成是链无关的 —— 这是第 11 课"新市场上线"那条路唯一实打实的价值。

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

---

## 第 12 课:写 solver 合约 —— 以及一次差点被误导的接口核实

合约在 [`contracts/SvrLiquidatorSolver.sol`](../../contracts/SvrLiquidatorSolver.sol),
**已编译通过,未部署、未押金、未实盘。**

### 先核实接口,别照文档抄

写合约之前必须确认:文档给的函数签名,链上真的是这个吗?

**方法一:扫真实合约的字节码找选择器。**

Solidity 编译出来的分发表里,每个 external 函数的 4 字节选择器会以
`PUSH4`(操作码 `0x63`)的形式出现。扫出来和候选签名的 keccak 前 4 字节比对:

```
atlasSolverCall(address,address,address,uint256,bytes,bytes) = 0x024181a6
  0x72004a0dc826…  ✅ 有        ← 真实 solver 合约
  0xd351755a32de…  ✅ 有
  0x7eda748f4278…  ✅ 有
```

**三个独立的真实 solver 合约都有这个选择器 —— 文档给的签名是对的。**

### 然后我差点被这个方法误导

同样的方法去核实 Aave 的 `liquidationCall`:

```
flashLoanSimple(address,address,uint256,bytes,uint16) = 0x42b0b77c  ✅ 存在
liquidationCall(address,address,address,uint256,bool) = 0x00a718a9  🔴 找不到
```

**但链上明明有几千笔清算。** 我当时的第一反应是"签名一定变了",
差点去改合约。

### 换个方法:eth_call 试探

拿一个**健康的**仓位去调用它,看会发生什么:

```
0x00a718a9  →  revert 数据 0x930bb771
0x5534b55b  →  revert 数据 0x(空)        ← 另一个候选签名
```

`0x930bb771` 是什么?反查一下:

```python
Web3.keccak(text="HealthFactorNotBelowThreshold()")[:4]  →  0x930bb771
```

**正是"健康度没跌破阈值,不能清算"** —— 这是拿健康仓位去清算时
本该报的业务错误。

> ### 🔑 两种 revert 的区别,是这一课的核心
>
> ```
> 函数存在,被业务逻辑挡下   →  revert 带 4 字节 custom error(或错误字符串)
> 函数根本不存在             →  revert 数据为 0x(空),命中 fallback
> ```
>
> **所以 `0x00a718a9` 是对的,字节码扫描给了个假阴性。**
> 原因大概率是代理 / 模块化分发 —— Aave V3 较新版本把 Pool 拆开了,
> 函数不在我扫的那个实现合约里,但调用能被正确路由过去。
>
> **可迁移的一条:核实一个函数存不存在,`eth_call` 试探比扫字节码可靠。**
> 扫字节码只能证明"有",不能证明"没有"。

### 合约做了什么

```
Atlas 中标后调 atlasSolverCall
  ① 校验 msg.sender == Atlas          ← 没这行,任何人都能驱动你的合约
  ② 校验 solverOpFrom == owner        ← 押金记在 solverFrom 名下,
                                         不校验别人能用你的押金
  ③ 出价封顶 maxBidWei                ← 估值程序算错时的最后一道闸
  ④ bidToken 必须是 address(0)        ← 实测 SVR 就是原生币;
                                         遇到别的币种直接 revert,不猜
  ⑤ 闪电贷 → liquidationCall → 变现 → 还贷
  ⑥ 把中标价付给执行环境
  ⑦ 调 Atlas.reconcile 结清 gas
```

### 补上了那个 bot 缺的东西

我们之前反编译过一个真实清算 bot(`0xf0570Ec4…`),
发现它**完全没有滑点保护**。本合约在两处强制下限:

```solidity
// ★ 滑点保护 1:清算实际拿到的抵押物不能少于预期
if (collOut < p.minCollateralOut) revert CollateralShortfall(collOut, p.minCollateralOut);

// ★ 滑点保护 2:变现换回来的钱不能少于预期
if (debtBack < p.minDebtBack)     revert ProceedsShortfall(debtBack, p.minDebtBack);
```

**两处不满足就整笔回滚,只损失 gas。宁可不赚,不能亏。**

在 Atlas 里这个设计代价特别低 —— 因为 metacall 本来就是原子的,
revert 之后预言机更新照样生效,只是你没中标那次机会。

### 编译

```bash
solc --optimize --optimize-runs 200 --bin --hashes contracts/SvrLiquidatorSolver.sol
```

输出里最该看的一行:

```
024181a6: atlasSolverCall(address,address,address,uint256,bytes,bytes)
```

**和链上真实 solver 合约的选择器一模一样** —— 说明 Atlas 能找到我们的回调。

### 还没做的(诚实清单)

- [ ] 没部署、没押金、没实盘
- [ ] `borrow(uint256)` 没用上 —— Atlas 允许在 metacall 里借原生币,
      现在是靠合约里预存余额付中标价,是**刻意的简化**
- [ ] 没写 fork 测试。下一步该用 `forge test --fork-url` 在
      历史区块上重放一笔真实清算
- [ ] 链下估值程序还没写 —— 收到推送后算"这次更新值多少钱、该出多少价"
      **这才是真正的竞争点**,合约只是执行器

---

## 第 13 课:fork 测试 —— 在历史区块上重放真实清算

光编译通过不算数。**要在真实的历史状态上跑一遍。**

```bash
export ARB_RPC_URL="https://rpc.ankr.com/arbitrum/$ANKR_KEY"
forge test --match-contract SvrLiquidatorSolverTest -vv
```

测试在 [`test/SvrLiquidatorSolver.t.sol`](../../test/SvrLiquidatorSolver.t.sol)。

### 一个必须先想清楚的时序问题

真实交易里 **[预言机更新 + 清算] 在同一个区块**(468923284)。

所以 fork 到前一块(468923283)时,**价格还是旧的,仓位是健康的** ——
直接跑清算一定失败。

测试因此分两步:

```
① 先证明"此刻不可清算"   ← 确认 fork 点和参数都对
② 再把价格打下去          ← 复现"预言机更新之后"
③ 然后跑完整流程
```

**第 ① 步不是形式主义。** 如果 fork 点选错、参数抄错,后面全部无意义。

跑出来:

```
HF at fork block: 1.000338471421177782
```

**距离清算线只差 0.034%。** 这个数字本身就说明了清算生意的性质 ——
一次预言机跳动就够了,不需要暴跌。

然后用 `vm.mockCall` 把 WBTC 价格下调 12%:

```
HF after -12% price: 0.880297854990716955
```

### 结果:能对上账

```
solver USDC after flow:  26,166.684579
bid received by EE:      0.01 ETH
```

拆开看:

```
毛清算奖励            $26,354.93   ← 真实交易的数(7% 清算奖励)
闪电贷费 0.05%        $   188.25   ← 376,499.06 × 0.0005
────────────────────────────────
净                    $26,166.68   ✅ 和测试输出分毫不差
```

> **闪电贷费第一次被算出了具体金额。** 之前成本模型里这一项一直是估的。
> 在这笔交易的规模上,它是 $188.25 —— 占毛奖励的 0.71%。

### 这个测试证明了什么、没证明什么

**证明了:**
- 那个仓位在那个区块确实处在清算边缘(HF 1.0003)
- `liquidationCall` 接受我们编的参数并成功执行
- 闪电贷借得出、还得上(premium 0.05% 被数字验证)
- 中标价确实付给了执行环境
- **滑点保护真的会拦** —— 把 `minCollateralOut` 设成 10 倍,整笔回滚,
  且**中标价没有被付出去**

**没证明:**
- 变现用的是 MockRouter,**真实 DEX 的滑点没有测**。
  $26,166 这个利润里,"抵押物能按预言机价卖掉"是我给的假设,不是实测。
- 没跑通真实的 Atlas metacall(见下)

### 踩到的两个坑

**坑 1:`vm.prank` 被提前消耗**

```solidity
// ❌ 错的
vm.prank(ATLAS);
vm.expectRevert(abi.encodeWithSelector(..., solver.maxBidWei()));   // ← 这次调用吃掉了 prank
solver.atlasSolverCall(...);                                         // ← 变成普通调用者
```

报错是 `NotAtlas() != BidTooHigh(...)`,一开始看不懂。

**`vm.prank` 只对下一次外部调用生效**,而 `solver.maxBidWei()` 就是一次外部调用。
修法:先读进局部变量。

**坑 2:`Atlas.reconcile` 报 `WrongPhase()`**

流程跑到最后一步 revert,错误码 `0xe2586bcc`。反查:

```python
Web3.keccak(text="WrongPhase()")[:4]  →  0xe2586bcc
```

**这不是我们合约的 bug** —— `reconcile` 要求处于真实 metacall 的正确阶段,
而我们是裸调的。要真跑通得把 userOp + solverOp + 签名 + bundler
整套搭起来,超出本测试范围。

从 trace 看,**在它之前的每一步都成功了**:

```
flashLoanSimple  ✅
liquidationCall  ✅
swap             ✅
还闪电贷          ✅  (FlashLoan 事件已发)
付中标价          ✅  0xEE::fallback{value: 0.01 ether}
reconcile        🔴  ← 只有这一步
```

所以在测试里 mock 掉它,专注验证主线。

> ### 🔑 可迁移的一条
> **测试失败时,先看它失败在第几步。**
> 「最后一步失败」和「第一步失败」是完全不同的信息 ——
> 前者说明主线是通的,只差环境;后者说明方向就错了。
> 我如果只看到 "custom error 0xe2586bcc" 就去改合约,会把一个
> **本来是对的实现**改坏。**`-vvvv` 看完整调用栈,是这一步的唯一办法。**

### 还差什么

- [ ] 真实 DEX 变现路径(现在是 MockRouter)
- [ ] 完整 Atlas metacall(userOp + solverOp + EIP-712 签名 + bundler)
- [ ] 链下估值程序 —— **收到推送后算该出多少价,这才是真正的竞争点**

---

## 第 14 课:链下估值 —— 拍卖里真正比的东西

合约只是执行器。**拍卖比的是这个**:推送到达的那一刻,
算出「这次预言机更新值多少钱、该出多少价」。

脚本:[`svr_valuer.py`](../../svr_valuer.py)(只算不出价,代码里没有
`solver_submitSolverOperation`,也不读私钥)

### 第一步:把聚合器映射到 Aave 资产

推送里给的是**聚合器**地址,而 `getSourceOfAsset` 返回的是**代理**。
差一层,得多走一步:

```
Aave.getSourceOfAsset(WETH) → 代理 0xbd41b154…
代理.aggregator()           → 聚合器 0xa5e1a369…   ← 推送里出现的是这个
```

```bash
python3 svr_valuer.py build-map --chain ARB
```

```
  WETH   agg 0xa5e1a369…  dec=8   清算奖励 5.0%
  WBTC   agg 0xe7c522c6…  dec=8   清算奖励 7.0%
  LINK   agg 0x4c76f02e…  dec=8   清算奖励 10.0%
  ARB    agg 0xb72359b2…  dec=8   清算奖励 10.0%
  AAVE   agg 0xc1720a82…  dec=8   清算奖励 10.0%
  EURS   agg 0x1ce4eeea…  dec=8   清算奖励 7.5%

  DAI/USDC/wstETH/weETH/GHO/rETH…  (适配器,无 aggregator() —— 跳过)
```

**稳定币和 LST 用的是复合/上限适配器,没有 `aggregator()`。**
脚本如实跳过并打印出来,不假装能处理 —— 这类资产的价格更新
不会以我们能识别的形式出现在推送里。

### 第二步:离线重放验证

```bash
python3 svr_valuer.py replay shadow/svr_feed_long.jsonl
```

```
  ARB WETH  $ 1,918.5316 (+0.078% vs 链上)   无机会
  ARB tBTC  $65,014.5853 (+0.087% vs 链上)   无机会
  ARB ARB   $     0.0796 (-0.009% vs 链上)   无机会
  ARB LINK  $     8.2192 (-0.499% vs 链上)   无机会

=== 260 帧 ===
  链不支持        203  (78.1%)   ← BNB/Monad,我们没有那边的仓位名单
  非 Aave 喂价     32  (12.3%)
  Aave 喂价        25  ( 9.6%)
  无机会           25  ( 9.6%)
```

**260 帧里只有 25 帧和我们相关,而这 25 帧一个机会都没产生。**
这不是失败,**这就是这门生意的形状** —— 和第 10 课测出的
「80% 的钱集中在 5 天」完全一致。

### 第三步:必须能证明"没机会"不等于"坏了"

平静期 replay 永远输出「0 个机会」。**这时候你没法区分
「确实没机会」和「代码悄悄坏了」。**

所以加了自检命令,注入一次必然触发的合成更新:

```bash
python3 svr_valuer.py simulate --symbol WETH --pct -15
```

```
合成推送:ARB WETH  $1,917.03 → $1,629.48 (-15.0%)

  ★ 53 个仓位跌破清算线
    毛奖励 $ 28,964.95
    成本   $    101.45  (闪电贷 5bps + 滑点 30bps + gas $0.07)
    净     $ 28,863.50
    **建议出价 $ 25,977.15**  (留 10% 利润率)
      0x270d1c8c…  HF 1.0244 → 0.8707  债务 $40,145
      0x11650b27…  HF 1.0320 → 0.8772  债务 $9,892
```

> ### 🔑 可迁移的一条
> **任何"平时不输出"的监控,都必须配一个"必然触发"的自检入口。**
> 否则你永远不知道它是在安静地工作,还是已经死了。
> 这条比本课其他所有内容都通用。

### 一个意外的交叉验证

```
建议出价 $25,977.15  ÷  毛奖励 $28,964.95  =  89.7%
```

**和市场实测的回收率 92.3% 几乎一致。**

我们的出价公式是「毛奖励 − 成本,再留 10% 利润率」独立推出来的,
落点却和真实市场的清算价重合。这说明:

> **市场上那批 solver 的出价逻辑,和我们推的是同一个** ——
> 都是「扣掉成本后,留一个很薄的利润率」。
> 而 92.3% 这个数字的含义就是:**竞争已经把利润率压到 8% 左右。**

### 成本参数的来源(每一项都标了)

```python
FLASHLOAN_BPS     = 5     # ✅ 实测:fork 测试里 $188.25 / $376,499
GAS_USD           = 0.07  # ✅ 实测:gasUsed 1,789,466 @ Arbitrum
SWAP_SLIPPAGE_BPS = 30    # ⚠️ **假设,没有实测**。真实 DEX 滑点未验证
TARGET_MARGIN     = 0.10  # 目标利润率,市场实际清算在 ~8%
```

**滑点那一项是整个模型里唯一没有实测支撑的数。** 它也恰好是
fork 测试里用 MockRouter 绕过去的那一项 —— 两处空白是同一个。

### 已知的近似(和 P0 是同一个)

```python
new_hf = hf * ratio     # 一阶近似:HF ∝ 抵押物价格
```

**只在「抵押物是这个资产、债务是稳定币」时成立。**
现在它把 WETH 的价格变动套用到了名单里**所有**仓位上,
不管人家抵押的是不是 WETH —— 所以 53 这个数字**偏高**。

要消除得用 `eth_call` 逐个模拟 `liquidationCall`。这是 P1 的事。

---

## 全流程速查

```bash
# 1. 搞清楚 Aave 读哪个喂价(有没有上 SVR)
python3 svr_probe.py feeds --chains ARB OPT BASE

# 2. 看拍卖场地和门槛
python3 svr_probe.py atlas
python3 svr_probe.py solvers --days 7

# 3. 解剖一笔真实清算
python3 svr_probe.py anatomy <txhash>

# 4. 连上拍卖推送(只读)
python3 svr_listen.py --seconds 900 --out shadow/svr_feed_long.jsonl

# 5. 建映射 + 估值
python3 svr_valuer.py build-map --chain ARB
python3 svr_valuer.py replay shadow/svr_feed_long.jsonl
python3 svr_valuer.py simulate --symbol WETH --pct -15    # 自检
python3 svr_valuer.py watch --seconds 600

# 6. 合约:编译 + 历史区块重放
export ARB_RPC_URL="https://rpc.ankr.com/arbitrum/$ANKR_KEY"
forge test -vv
```

**到这里,除了「真的出价」之外的每一步都跑通了。**
没跑的那一步不是技术问题 —— 是第 10 课算出来的:
Arbitrum 拍卖残留一年约 $57k,由 3 个 solver 分,Top1 占 84.6%。
