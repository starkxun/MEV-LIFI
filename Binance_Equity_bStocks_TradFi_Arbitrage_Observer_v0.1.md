# Binance 美股 / bStocks / TradFi Perp 套利探测器 v0.1

> **项目定位**：先证明 alpha 是否存在，再决定是否开发自动执行器。  
> **阶段路线**：Observer → Shadow/Paper Trade → 小额执行。  
> **文档日期**：2026-08-11  
> **建议语言**：Python 3.12（第一版）；后续如确有延迟需求，再将关键行情/执行模块迁移至 Rust/Go。  

---

## 0. 一句话目标

构建一个持续采集并统一比较以下市场价格的套利观察系统：

1. Binance Direct Stocks（真实美股/ETF交易接口）
2. Binance bStocks（代币化美股）
3. Binance TradFi Stock Perpetuals（股票永续）
4. BNB Chain 上 bStocks 的 DEX 流动性

系统**第一阶段不自动下单**，只回答三个问题：

- 有没有扣除全部成本后仍为正的套利机会？
- 机会持续多久：50 ms、500 ms、5 s、30 s，还是更久？
- 普通 API 用户是否来得及成交，还是只有做市商/内部流动性提供者能吃到？

---

# 1. 为什么值得做

Binance 目前把几类原本割裂的市场放到了同一生态中：

```text
                    ┌────────────────────┐
                    │   US Direct Stock  │
                    │   NVDA / TSLA ...  │
                    └─────────┬──────────┘
                              │
                      mint / redeem 1:1
                              │
                              ▼
                    ┌────────────────────┐
                    │      bStocks       │
                    │   tokenized equity │
                    └──────┬───────┬─────┘
                           │       │
                         CEX      BSC
                           │       │
                           │       ▼
                           │    DEX Pools
                           │
                           ▼
                    ┌────────────────────┐
                    │  TradFi Stock Perp│
                    │   USDT perpetual   │
                    └────────────────────┘
```

当前官方能力中，最重要的是：

- Stocks Trading API 已正式提供 `/sapi/v1/equity/*`
- Direct Stocks 有实时 quote WebSocket
- bStocks 与对应股票可进行 mint/redeem
- bStocks 是 BNB Smart Chain 上的 BEP-20 资产
- USDⓈ-M Futures API 可直接读取 TradFi Perp book ticker、mark price、funding
- Binance 还提供 TradFi underlying trading session 信息

这使得我们能程序化研究：

```text
Stock ↔ bStock
bStock ↔ Perp
Stock ↔ Perp
CEX bStock ↔ DEX bStock
DEX A ↔ DEX B
Stock → mint → bStock → withdraw → DEX
DEX → deposit → redeem → Stock
```

但注意：**不是每条路径都天然可执行**。  
项目的任务就是把“看起来有价差”过滤成“真实可成交净套利”。

---

# 2. 项目成功标准

这个项目不是以“机器人写完了”为成功，而以得到清晰的市场结论为成功。

## 2.1 最低成功标准

跑满至少：

```text
72 小时连续采集
+
至少 2 个完整美股交易日
+
至少 1 个周末
```

得到：

- 每个标的的价差时间序列
- 每类套利机会次数
- 毛价差分布
- 净价差分布
- 机会持续时间分布
- 可成交深度
- 理论 PnL
- shadow execution PnL
- 不同市场时段表现

---

## 2.2 值得继续开发执行器的标准

建议至少满足以下其中一种情况：

### A 类：高频但小利润

```text
net edge >= 15 bp
机会 >= 50 次 / 天
median lifetime >= 500 ms
可执行 notional >= 500 USDT
```

### B 类：低频但大利润

```text
net edge >= 40 bp
机会 >= 3 次 / 天
median lifetime >= 2 s
可执行 notional >= 1,000 USDT
```

### C 类：结构性机会

例如：

```text
周末/盘前/盘后
corporate action
资金费率结算前后
mint/redeem 暂停恢复
DEX 流动性失衡
大幅单边行情
```

这些窗口反复出现，并且存在明显、可解释、可执行的 edge。

---

# 3. 明确不做什么

v0.1 **不做**：

- 不直接实盘自动下单
- 不做预测美股涨跌
- 不赌周末 bStock 一定回归周五收盘价
- 不默认可以裸空 Direct Stock
- 不拿 last price 计算套利
- 不把 mark price 当成交价
- 不假设所有 bStock 都有足够 DEX 流动性
- 不硬编码 token contract / perp symbol
- 不在缺少真实手续费、滑点、gas 的情况下报告“净利润”

---

# 4. 第一版研究标的

不要硬编码成“只有这几只”。

程序启动后应从官方 API 动态获取：

```text
Direct Stocks universe
Tokenized asset universe
TradFi Perp universe
```

然后做交集。

优先观察高波动、高关注度、容易产生 crypto-native 交易需求的股票，例如：

```text
NVDA
TSLA
COIN
MSTR
PLTR
SPY
QQQ
```

**最终使用哪些标的必须由 API 当时返回的实际可交易列表决定。**

输出：

```json
{
  "underlying": "NVDA",
  "direct_stock": true,
  "bstock": true,
  "tradfi_perp": true,
  "onchain_pool": true,
  "enabled": true
}
```

---

# 5. 系统架构

```text
                         ┌─────────────────┐
                         │ Symbol Registry │
                         └────────┬────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
        ▼                         ▼                         ▼
┌────────────────┐       ┌────────────────┐        ┌─────────────────┐
│ Stock Collector│       │ Perp Collector │        │ Onchain Collector│
│ Binance Equity │       │ Binance Futures│        │ BNB Chain / DEX │
└───────┬────────┘       └───────┬────────┘        └────────┬────────┘
        │                        │                          │
        └─────────────┬──────────┴─────────────┬────────────┘
                      ▼                        ▼
              ┌───────────────┐        ┌───────────────┐
              │ Market State  │        │ Raw Storage   │
              │ latest quotes │        │ tick/events   │
              └───────┬───────┘        └───────────────┘
                      │
                      ▼
              ┌────────────────┐
              │ Opportunity    │
              │ Engine         │
              └───────┬────────┘
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
      Stock/bS      bS/Perp      CEX/DEX
          │           │            │
          └───────────┼────────────┘
                      ▼
              ┌────────────────┐
              │ Cost & Risk    │
              │ Model          │
              └───────┬────────┘
                      ▼
              ┌────────────────┐
              │ Opportunity DB │
              └───────┬────────┘
                      ▼
              ┌────────────────┐
              │ Analyzer       │
              │ Report / CSV   │
              └────────────────┘
```

---

# 6. 模块一：Symbol Registry

## 6.1 目的

解决最容易埋坑的问题：

```text
NVDA
NVDAB
NVDAUSDT
某个 BSC token address
```

这几个名字并不应该靠人工字符串拼接。

建立统一对象：

```python
InstrumentMapping(
    underlying="NVDA",
    stock_symbol="NVDA",
    bstock_symbol=None,
    bstock_contract=None,
    perp_symbol=None,
    multiplier=None,
    stock_enabled=False,
    bstock_enabled=False,
    perp_enabled=False,
    dex_enabled=False,
)
```

---

## 6.2 数据来源

Direct Stocks：

```text
GET /sapi/v1/equity/market/exchangeInfo
GET /sapi/v1/equity/market/tokenized-assets
```

TradFi Perp：

```text
GET /fapi/v1/exchangeInfo
```

对 futures universe 做筛选，不要默认：

```text
underlying + "USDT"
```

就是正确合约。

BNB Chain：

- 从官方/可信来源确认 bStock contract address
- 再查询 DEX Factory / Subgraph / RPC
- 建立 token → pool 映射

---

# 7. 模块二：Direct Stock Collector

## 7.1 行情禁止使用 REST 轮询作为主数据源

官方 REST：

```text
GET /sapi/v1/equity/market/quote
```

存在 server-side cache，不适合做套利主行情。

主行情使用 Stocks WebSocket：

```text
wss://nbstream.binance.com/equity
```

单标的：

```text
/ws/NVDA@quote
```

多标的：

```text
/stream?streams=NVDA@quote/TSLA@quote/COIN@quote
```

主要字段：

```text
bid price
ask price
bid size
ask size
event timestamp
quote timestamp
```

---

## 7.2 还必须订阅市场状态

订阅：

```text
calendar
{SYMBOL}@tradingStatus
{SYMBOL}@tradability
```

因为：

```text
PRE_MARKET
MARKET_OPEN
POST_MARKET
OVERNIGHT
MARKET_CLOSED
HALT
LULD
SSR
```

都可能改变价格意义和可执行性。

每一条 opportunity 必须带：

```text
stock_market_session
stock_trading_status
stock_buy_allowed
stock_sell_allowed
```

---

# 8. 模块三：bStock 数据

bStock 有两部分：

## 8.1 Binance 内部 bStock

若存在相应 Binance spot / tokenized trading quotation：

采集：

```text
best bid
best ask
bid qty
ask qty
timestamp
```

必须使用**实际可成交 bid/ask**。

不能用：

```text
last price
24h ticker close
指数价
展示页价格
```

来判断套利。

---

## 8.2 Mint / Redeem 状态

核心 API：

```text
POST /sapi/v1/equity/tokenized/mint
POST /sapi/v1/equity/tokenized/redeem

GET /sapi/v1/equity/tokenized/convert-status
GET /sapi/v1/equity/tokenized/history
```

Observer 阶段不需要真的频繁 mint/redeem。

但要记录：

```text
mint_available
redeem_available
estimated_conversion_latency
historical_conversion_latency
corporate_action_pause
```

后续 paper/small-live 阶段通过极小规模 conversion 测量真实延迟。

---

# 9. 模块四：TradFi Perp Collector

需要三种数据。

## 9.1 可成交价

REST fallback：

```text
GET /fapi/v1/ticker/bookTicker
```

主数据优先 WebSocket book ticker / depth。

记录：

```text
perp_bid
perp_bid_qty
perp_ask
perp_ask_qty
```

---

## 9.2 Mark / Index / Funding

```text
GET /fapi/v1/premiumIndex
GET /fapi/v1/fundingRate
GET /fapi/v1/fundingInfo
```

记录：

```text
mark_price
index_price
last_funding_rate
next_funding_time
funding_interval
funding_cap
funding_floor
```

注意：

> funding 只进入持仓收益模型，不能直接被当成瞬时可兑现利润。

---

## 9.3 TradFi market session

```text
GET /fapi/v1/tradingSchedule
```

以及：

```text
tradingSession WebSocket
```

需要明确：

```text
PRE_MARKET
REGULAR
AFTER_MARKET
OVERNIGHT
NO_TRADING
```

---

# 10. 模块五：BNB Chain / DEX Collector

第一版只做 BNB Chain。

不要一上来接十个 DEX。

## v0.1

先接：

```text
PancakeSwap V3
```

如果发现 bStock 流动性不在那里，再扩展：

```text
PancakeSwap V2
DEX aggregator
其他实际有 bStock 流动性的协议
```

---

## 10.1 DEX 不能只存 spot price

对于某个测试 notional：

```text
100 USDT
500 USDT
1,000 USDT
5,000 USDT
10,000 USDT
```

分别请求真实 quote。

保存：

```text
amount_in
amount_out
effective_price
price_impact
pool_fee
estimated_gas
block_number
block_timestamp
```

套利计算要用：

```text
effective executable price
```

而不是：

```text
sqrtPriceX96 推导出来的瞬时 mid
```

---

## 10.2 测试方向

两边都测：

```text
USDT → bStock
bStock → USDT
```

必要时：

```text
USDC → bStock
bStock → USDC
```

---

# 11. 统一 Market State

所有 collector 只负责提供行情。

统一内存状态：

```python
MarketState(
    symbol="NVDA",

    stock_bid=...,
    stock_ask=...,
    stock_bid_qty=...,
    stock_ask_qty=...,

    bstock_bid=...,
    bstock_ask=...,
    bstock_bid_qty=...,
    bstock_ask_qty=...,

    perp_bid=...,
    perp_ask=...,
    perp_bid_qty=...,
    perp_ask_qty=...,
    perp_mark=...,
    perp_index=...,
    funding_rate=...,
    next_funding_time=...,

    dex_sell_effective_price={100: ..., 500: ..., 1000: ...},
    dex_buy_effective_price={100: ..., 500: ..., 1000: ...},

    stock_session="REGULAR",
    perp_session="REGULAR",

    stock_ts=...,
    bstock_ts=...,
    perp_ts=...,
    dex_block_ts=...,
)
```

---

# 12. 最重要：Staleness Filter

不要拿不同时间的价格做“幽灵套利”。

例如：

```text
Stock quote: 10:00:00.100
bStock quote: 10:00:02.000
```

已经差 1.9 秒。

定义：

```python
age_stock_ms
age_bstock_ms
age_perp_ms
age_dex_ms
```

以及：

```python
cross_market_skew_ms =
    max(timestamp) - min(timestamp)
```

第一版建议：

```text
Stock ↔ bStock:
cross_market_skew <= 500 ms

bStock ↔ Perp:
cross_market_skew <= 300 ms

CEX ↔ DEX:
DEX 按最新 block / pending simulation 单独处理
```

这些数字只是初始实验阈值，最终要用数据调整。

---

# 13. 策略 A：Direct Stock → bStock

目标：

```text
Stock ask < bStock bid
```

理论毛 edge：

```text
gross_edge =
    bstock_bid / stock_ask - 1
```

路径：

```text
BUY Stock
→ mint bStock
→ SELL bStock
```

---

## 13.1 净利润

```text
gross_profit
= bstock_sell_proceeds
- stock_buy_cost
```

扣除：

```text
stock commission
bStock spot fee
slippage
inventory cost
conversion latency risk
rounding
currency/quote conversion cost
```

得到：

```text
net_profit
net_edge_bps
```

---

## 13.2 关键风险

最大的风险不是手续费，而是：

```text
BUY Stock 成交
↓
mint 过程中
↓
bStock bid 消失
```

所以 Observer 必须统计：

```text
spread lifetime
spread decay after trigger
conversion latency
```

---

# 14. 策略 B：bStock → Direct Stock

理论条件：

```text
bstock_ask < stock_bid
```

理论路径：

```text
BUY bStock
→ redeem Stock
→ SELL Stock
```

但这条路径**不能默认永远可执行**。

需要确认：

```text
redeem 是否可用
redeem 延迟
Direct Stock 当前 sell 是否可用
是否已有库存
是否涉及不可用的短售能力
```

第一版只计算：

```text
inventory-backed arb
```

也就是：

> 如果账户本来就有可卖 Stock inventory，则可以评估。

**不要把未知的裸空能力计入可执行套利。**

---

# 15. 策略 C：bStock ↔ TradFi Perp

这是第一版重点。

## 15.1 Perp overpriced

条件：

```text
perp_bid > bstock_ask
```

构造：

```text
LONG bStock
SHORT Perp
```

gross basis：

```text
basis =
    perp_bid / bstock_ask - 1
```

---

## 15.2 持仓收益

如果持有：

```text
T hours
```

则：

```text
Expected PnL
=
basis convergence
+ received funding
- paid funding
- bStock fee
- perp fee
- slippage
- financing / inventory cost
```

---

## 15.3 不要把 basis 全部视为可赚

需要记录：

```text
entry basis
minimum basis after 1m
minimum basis after 5m
minimum basis after 30m
basis at next funding
basis after funding
```

统计：

```text
mean reversion half-life
```

---

# 16. 策略 D：Direct Stock ↔ TradFi Perp

条件：

```text
perp_bid > stock_ask
```

结构：

```text
LONG Stock
SHORT Perp
```

这条路径很有研究价值，因为不需要 mint/redeem。

缺点：

- 股票交易时段和永续 24/7 时段不同
- 股票腿可能无法在周末/特定 session 立即执行
- 股息、corporate action、funding 都可能影响 basis

因此把它分成：

```text
RTH basis
Extended-hours basis
Overnight basis
Market-closed basis
Weekend basis
```

分别统计。

---

# 17. 策略 E：Binance bStock ↔ BSC DEX

这是最 crypto-native 的方向。

## 17.1 CEX cheap / DEX expensive

```text
CEX bStock ask < DEX effective sell price
```

路径：

```text
BUY bStock on Binance
→ withdraw BSC
→ SELL on DEX
```

这里的主要问题是 withdrawal latency。

因此不能简单地看到价差后才：

```text
买 → 提币 → 卖
```

真正成熟的做法往往需要：

```text
预先在 CEX 和链上都放库存
```

然后：

```text
CEX BUY
+
DEX SELL
```

近同步执行。

事后再 rebalance。

---

## 17.2 DEX cheap / CEX expensive

```text
DEX buy effective price < CEX bStock bid
```

路径：

```text
BUY DEX
+
SELL CEX inventory
```

然后：

```text
链上 bStock → deposit Binance
```

做库存恢复。

---

# 18. 策略 F：DEX ↔ DEX

如果同一个 bStock 在多个池子存在：

```text
Pool A
Pool B
Pool C
```

直接计算：

```text
DEX A → DEX B
```

这一类最接近传统链上套利。

必须考虑：

```text
pool fee
price impact
gas
router overhead
MEV competition
sandwich/backrun risk
transaction revert
```

若以后要实盘，应尽量使用：

```text
atomic transaction
```

而不是两笔独立交易。

---

# 19. 周末价差必须单独处理

这是整个项目最容易产生假 alpha 的地方。

例如：

```text
Friday Stock close = 100
Saturday bStock = 105
Saturday Perp = 105.2
```

不能说：

```text
bStock 比真实股票贵 5%
所以有 5% 套利
```

因为此时你并不能保证能以 100 买到 underlying stock。

所以：

## Stock market open

计算：

```text
Executable Arbitrage
```

## Stock market closed

计算：

```text
Indicative Basis / Price Discovery
```

数据库必须区分：

```text
opportunity_type = EXECUTABLE
```

和：

```text
opportunity_type = INDICATIVE
```

---

# 20. Corporate Action Filter

bStocks 有 dividend / split / multiplier 机制。

TradFi perp funding 历史里也可能出现与股票 dividend 相关的特殊 funding 类型。

因此遇到：

```text
dividend
split
reverse split
merger
symbol change
halt
corporate action processing
```

必须：

```text
disable normal arbitrage signal
```

直到 multiplier / symbol / basis 正确归一化。

否则非常容易产生巨大“假价差”。

---

# 21. Multiplier Normalization

不要默认：

```text
1 bStock = 1 raw display share
```

永远不变。

系统统一维护：

```text
normalized_stock_price
normalized_bstock_price
normalized_perp_price
```

所有 comparison 必须在相同经济单位下。

例如：

```python
normalized_bstock_price =
    raw_bstock_price / multiplier
```

具体归一方式必须以 API 当前 multiplier 定义为准。

任何 multiplier 变化：

```text
→ 暂停 signal
→ 更新 registry
→ 重建 normalization
→ 再开启 signal
```

---

# 22. 可成交深度模型

不要只研究 1 美元的价差。

设测试规模：

```python
NOTIONAL_BUCKETS = [
    100,
    500,
    1000,
    2500,
    5000,
    10000,
]
```

对每个套利机会分别计算：

```text
edge_100
edge_500
edge_1000
edge_2500
edge_5000
edge_10000
```

这样最后能回答：

> 这个 alpha 是只有 $30 能吃，还是 $5,000 也能吃？

---

# 23. Cost Model

建立统一函数：

```python
estimate_cost(
    strategy,
    symbol,
    notional,
    side_a,
    side_b,
)
```

返回：

```python
CostBreakdown(
    stock_fee,
    bstock_fee,
    perp_fee,
    dex_fee,
    gas_cost,
    slippage,
    withdrawal_fee,
    funding_cost,
    conversion_cost,
    safety_buffer,
)
```

---

## 23.1 Safety Buffer

Observer 不要把理论边缘全部算成利润。

先加入：

```text
5–10 bp safety buffer
```

再根据真实 shadow fill error 调整。

---

# 24. Opportunity Engine

每次任意市场 quote 更新：

```text
update MarketState
↓
staleness check
↓
session check
↓
tradability check
↓
multiplier normalization
↓
generate candidate routes
↓
quantity-aware executable price
↓
cost model
↓
net edge
↓
persist if threshold hit
```

---

## 24.1 Candidate

```python
Opportunity(
    id=...,
    symbol="NVDA",
    strategy="BSTOCK_PERP",
    direction="LONG_BSTOCK_SHORT_PERP",

    detected_at=...,

    stock_bid=None,
    stock_ask=None,
    bstock_bid=...,
    bstock_ask=...,
    perp_bid=...,
    perp_ask=...,

    notional=1000,

    gross_edge_bps=31.2,
    estimated_cost_bps=12.4,
    safety_buffer_bps=5.0,
    net_edge_bps=13.8,

    max_executable_notional=...,

    stock_session="REGULAR",
    perp_session="REGULAR",

    cross_market_skew_ms=...,
)
```

---

# 25. Opportunity Lifetime Tracker

发现 opportunity 后，不只存一条记录。

持续追踪直到：

```text
net_edge <= 0
```

或者：

```text
net_edge < trigger_threshold
```

记录：

```text
detected_at
peak_edge_bps
peak_time
last_profitable_at
ended_at
lifetime_ms
```

再记录 edge path：

```text
T+50ms
T+100ms
T+250ms
T+500ms
T+1s
T+2s
T+5s
T+10s
```

这是判断你是否有机会真正成交的核心指标。

---

# 26. Shadow Execution

Observer 之后不要立刻实盘。

做模拟：

```text
信号出现
↓
模拟 API 延迟
↓
T + 50 / 100 / 200 / 500 ms
↓
读取当时真实 book
↓
模拟成交
```

得到：

```text
theoretical_edge
vs
realistic_fill_edge
```

---

## 26.1 latency profile

模拟不同基础设施：

```text
50 ms
100 ms
200 ms
500 ms
1 s
```

最终会得到：

```text
如果延迟 50 ms：策略盈利
如果延迟 200 ms：勉强
如果延迟 500 ms：全没了
```

那就知道是否值得继续卷 latency。

---

# 27. 数据库设计

v0.1 推荐：

```text
PostgreSQL
+
TimescaleDB（可选）
```

本地轻量实验也可以：

```text
DuckDB + Parquet
```

如果目标是先跑 3–7 天，我建议：

```text
raw tick → Parquet
opportunity → PostgreSQL / DuckDB
```

---

## 27.1 raw_quotes

```sql
timestamp
receive_timestamp
venue
market_type
symbol
bid
ask
bid_qty
ask_qty
source_event_timestamp
market_session
sequence_id
```

---

## 27.2 perp_state

```sql
timestamp
symbol
bid
ask
mark_price
index_price
funding_rate
next_funding_time
funding_interval_hours
session
```

---

## 27.3 dex_quotes

```sql
timestamp
block_number
dex
pool
token_in
token_out
amount_in
amount_out
effective_price
price_impact_bps
gas_estimate
```

---

## 27.4 opportunities

```sql
opportunity_id
strategy
symbol
direction
detected_at
ended_at
lifetime_ms

notional
gross_edge_bps
fee_bps
slippage_bps
gas_bps
safety_buffer_bps
net_edge_bps

stock_session
perp_session
cross_market_skew_ms

executable
reason_not_executable
```

---

# 28. 日志

任何 signal 都要能复盘。

例如：

```text
2026-08-11T14:32:01.412Z
NVDA
BSTOCK_PERP
LONG_BSTOCK_SHORT_PERP

bStock ask: 182.11
Perp bid:   182.48

Gross: 20.3 bp
Fees:   8.0 bp
Slip:   2.4 bp
Buffer: 5.0 bp

Net:    4.9 bp

Notional: $1,000
Lifetime: 812 ms
Result: BELOW_EXECUTION_THRESHOLD
```

---

# 29. Dashboard / Report

第一版不需要花时间写漂亮网页。

每天生成：

```text
reports/YYYY-MM-DD.md
reports/YYYY-MM-DD.csv
```

内容：

## Market Summary

```text
数据覆盖率
断线次数
每个 venue latency
market session
```

## Opportunity Summary

```text
strategy
count
median gross edge
median net edge
p95 net edge
max edge
median lifetime
p95 lifetime
median executable size
```

---

# 30. 最重要的统计图

至少画：

1. `net_edge_bps` histogram
2. `opportunity lifetime` histogram
3. edge vs notional
4. edge vs market session
5. edge vs time-of-day
6. bStock/Stock basis time series
7. Perp/bStock basis time series
8. funding vs basis
9. theoretical PnL vs shadow PnL
10. opportunity count by symbol

---

# 31. Market Regime 标签

每条数据附加：

```text
US_RTH
PRE_MARKET
POST_MARKET
OVERNIGHT
WEEKEND
HOLIDAY
HALT
FUNDING_WINDOW
HIGH_VOL
NORMAL_VOL
```

以后分析时非常重要。

你可能最终发现：

```text
平时没 alpha
但是周一开盘前 15 分钟经常有
```

这才是值得做的结果。

---

# 32. 风控层

虽然 Observer 不下单，也要从第一天按未来实盘设计。

配置：

```yaml
risk:
  max_notional_per_trade: 1000
  max_symbol_exposure: 2500
  max_total_exposure: 5000

  min_net_edge_bps: 15
  max_cross_market_skew_ms: 500

  max_quote_age_ms: 1000
  stop_on_multiplier_change: true
  stop_on_market_halt: true
  stop_on_conversion_disabled: true
```

---

# 33. Kill Switch 条件

未来执行器一旦出现：

```text
WebSocket stale
API error burst
timestamp drift
exchange status abnormal
stock halt
tradability change
mint/redeem disabled
corporate action
multiplier change
chain RPC stale
gas spike
DEX simulation revert
inventory imbalance
unknown order state
```

立即：

```text
STOP NEW ORDERS
```

而不是继续赌。

---

# 34. API Key 安全

Observer 第一阶段：

```text
尽可能只使用 public market data
```

必须用账户 API 时：

```text
单独 API key
最小权限
禁止提现权限
IP whitelist
密钥只放环境变量/secret manager
禁止写进代码/Git
```

`.env`：

```text
BINANCE_API_KEY=
BINANCE_API_SECRET=
BSC_RPC_URL=
```

`.gitignore`：

```text
.env
data/
logs/
*.db
```

---

# 35. 推荐项目目录

```text
equity-arb-observer/
│
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
│
├── config/
│   ├── symbols.yaml
│   ├── strategy.yaml
│   └── risk.yaml
│
├── src/
│   ├── main.py
│   │
│   ├── registry/
│   │   ├── instruments.py
│   │   └── discovery.py
│   │
│   ├── collectors/
│   │   ├── binance_stock_ws.py
│   │   ├── binance_stock_rest.py
│   │   ├── binance_bstock.py
│   │   ├── binance_perp_ws.py
│   │   ├── binance_perp_rest.py
│   │   ├── bsc_rpc.py
│   │   └── pancake_v3.py
│   │
│   ├── market/
│   │   ├── state.py
│   │   ├── normalization.py
│   │   ├── sessions.py
│   │   └── freshness.py
│   │
│   ├── strategies/
│   │   ├── stock_bstock.py
│   │   ├── bstock_perp.py
│   │   ├── stock_perp.py
│   │   ├── cex_dex.py
│   │   └── dex_dex.py
│   │
│   ├── engine/
│   │   ├── opportunity.py
│   │   ├── costs.py
│   │   ├── depth.py
│   │   └── lifetime.py
│   │
│   ├── shadow/
│   │   ├── executor.py
│   │   └── latency.py
│   │
│   ├── storage/
│   │   ├── models.py
│   │   ├── parquet.py
│   │   └── repository.py
│   │
│   ├── reports/
│   │   ├── daily.py
│   │   └── plots.py
│   │
│   └── utils/
│       ├── clock.py
│       ├── retry.py
│       └── logging.py
│
├── tests/
│   ├── test_normalization.py
│   ├── test_costs.py
│   ├── test_opportunity.py
│   ├── test_staleness.py
│   └── test_shadow_execution.py
│
├── scripts/
│   ├── discover_symbols.py
│   ├── inspect_bstocks.py
│   ├── inspect_perps.py
│   └── generate_report.py
│
├── data/
├── logs/
└── reports/
```

---

# 36. Python 技术栈

推荐：

```text
Python 3.12
asyncio
aiohttp
websockets
httpx
pydantic
orjson
web3.py
eth-abi
pandas / polars
duckdb
pyarrow
SQLAlchemy
matplotlib
pytest
structlog
tenacity
```

行情主循环尽量：

```text
asyncio + event driven
```

不要：

```text
while True:
    requests.get(...)
    sleep(1)
```

---

# 37. 时间同步

套利系统必须严格处理时钟。

保存两个 timestamp：

```text
exchange_event_ts
local_receive_ts
```

计算：

```text
transport_delay =
local_receive_ts - exchange_event_ts
```

系统机器开启：

```text
NTP / chrony
```

若 drift 超阈值：

```text
disable opportunity generation
```

---

# 38. 第一阶段具体开发顺序

## Day 1：市场映射

完成：

```text
Stocks universe discovery
bStock mapping
TradFi perp discovery
```

输出：

```text
data/instruments.json
```

验收：

```text
程序能够告诉我：
NVDA 是否有 stock
是否有 bStock
是否有 perp
对应 symbol 分别是什么
```

---

## Day 2：三路 Binance 行情

完成：

```text
Stock WebSocket
bStock quote
Perp WebSocket
funding / mark / index
market session
```

终端实时显示：

```text
NVDA

Stock   182.10 / 182.12
bStock  182.16 / 182.18
Perp    182.20 / 182.22

Stock-bStock
bStock-Perp
Stock-Perp
```

---

## Day 3：存储 + staleness

完成：

```text
raw tick persist
latest MarketState
freshness check
cross-market timestamp skew
```

连续跑 6 小时不能：

```text
数据错位
内存泄漏
频繁断线
重复 event
```

---

## Day 4：Opportunity Engine

实现：

```text
stock → bStock
bStock → stock
bStock ↔ perp
stock ↔ perp
```

暂时：

```text
不接 DEX
不下单
```

---

## Day 5：Cost Model + Lifetime

完成：

```text
gross edge
estimated net edge
lifetime tracking
notional buckets
```

第一份真正有意义的报告应该在这里出来。

---

## Day 6：接 BSC / PancakeSwap

完成：

```text
contract mapping
pool discovery
quote simulation
gas estimate
```

加入：

```text
CEX ↔ DEX
DEX ↔ DEX
```

---

## Day 7：Shadow Executor

模拟：

```text
50 ms
100 ms
200 ms
500 ms
1000 ms
```

的成交结果。

输出：

```text
signal edge
shadow fill edge
edge decay
```

---

# 39. 观察期

代码写完后先跑：

```text
3–7 天
```

至少覆盖：

```text
RTH
盘前
盘后
overnight
周末
一次 funding settlement
```

不要两小时没机会就宣布失败，也不要看到一次 2% 的“价差”就宣布发现 alpha。

---

# 40. 最终决策树

```text
              跑完 3–7 天
                    │
                    ▼
             有净正 edge？
              /           \
            否             是
            │               │
            ▼               ▼
        这条线停       lifetime 足够？
                         /        \
                       否          是
                       │            │
                       ▼            ▼
                需要卷 latency   深度足够？
                                  /     \
                                否       是
                                │         │
                                ▼         ▼
                           小资金价值   Paper Trade
                                            │
                                            ▼
                                      shadow PnL > 0？
                                         /     \
                                       否       是
                                       │         │
                                       ▼         ▼
                                    停止     小额实盘
```

---

# 41. 小额实盘前必须满足

必须同时满足：

```text
[ ] API 资格与地区合规确认
[ ] 至少 7 天稳定 observer 数据
[ ] 至少 100 个 shadow opportunities
[ ] 扣费用后 shadow PnL > 0
[ ] 无 unresolved order-state bug
[ ] 无时间戳错位
[ ] fee model 已用真实账户数据校准
[ ] mint/redeem 延迟已实测
[ ] withdrawal/deposit 延迟已实测
[ ] kill switch 已测试
[ ] inventory accounting 已测试
```

初始：

```text
单笔风险 notional <= 100 USDT
```

而不是直接上千刀。

---

# 42. 第一版最值得优先验证的三个假设

按优先级：

## H1：bStock ↔ TradFi Perp

```text
是否长期存在 >15–30 bp 的可成交 basis？
```

优点：

- 都在 Binance
- 数据获取简单
- 不需要跨链
- 不需要等待 mint/redeem
- 最容易先证明有没有 alpha

**优先级：★★★★★**

---

## H2：Direct Stock ↔ bStock

```text
RTH / extended hours 是否存在 conversion-arb？
```

重点不只是价差，而是：

```text
价差寿命 vs mint/redeem latency
```

**优先级：★★★★☆**

---

## H3：Binance bStock ↔ BSC DEX

```text
链上流动性失衡是否经常超过 transfer / inventory 成本？
```

如果存在，这条线最符合 crypto MEV / arbitrage 技术栈。

**优先级：★★★★☆**

---

# 43. 暂时不要优先做的方向

## 周末 Stock close ↔ bStock

不是严格套利。

只能研究：

```text
price discovery
```

---

## Predictive arbitrage

例如：

```text
用 Perp 推断周一股票开盘
```

这是预测策略，不是无风险套利。

---

## 直接 HFT 拼纳秒

当前连 edge 都没证明，不要先优化 Rust、kernel bypass、colocation。

顺序必须是：

```text
Alpha
>
Execution
>
Latency optimization
```

---

# 44. 我希望项目最终回答的表格

最终生成类似：

| Strategy | Opportunities/day | Median Net Edge | P95 Edge | Median Lifetime | P95 Size | Shadow PnL | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| Stock → bStock |  |  |  |  |  |  |  |
| bStock → Stock |  |  |  |  |  |  |  |
| bStock ↔ Perp |  |  |  |  |  |  |  |
| Stock ↔ Perp |  |  |  |  |  |  |  |
| CEX ↔ DEX |  |  |  |  |  |  |  |
| DEX ↔ DEX |  |  |  |  |  |  |  |

最后只做：

```text
GO
NO-GO
NEEDS-LATENCY
NEEDS-CAPITAL
```

四种结论。

---

# 45. 给 Codex 的第一轮任务

下面这段可以直接喂给 Codex：

```text
你现在负责实现一个名为 equity-arb-observer 的研究型套利探测项目。

目标不是立即交易，而是验证 Binance Direct Stocks、bStocks、TradFi Stock Perpetuals 之间是否存在普通 API 用户能够捕捉的可执行套利。

严格遵守以下原则：

1. 第一阶段禁止实盘下单。
2. 所有市场比较必须使用 best bid / best ask，不得使用 last price 代替成交价格。
3. 不允许硬编码 bStock、股票和 TradFi Perp 的 symbol 映射；必须通过官方 API 动态发现。
4. 每个行情事件同时保存 exchange timestamp 和 local receive timestamp。
5. 必须实现 staleness filter，禁止拿时间错位的行情生成套利信号。
6. 所有套利信号必须扣除 fee、slippage 和 safety buffer。
7. 必须按 100、500、1000、2500、5000、10000 USDT 多个 notional 计算可执行 edge。
8. 必须区分 REGULAR、PRE_MARKET、POST_MARKET、OVERNIGHT、WEEKEND / CLOSED。
9. 市场关闭时，Stock 与 24/7 产品的价差默认标记为 INDICATIVE，而不是 EXECUTABLE。
10. 需要记录 opportunity lifetime，而不是仅记录发现时的一瞬间价差。
11. 所有代码必须可测试、模块化，并对断线自动重连。
12. API key 不允许进入 Git；Observer 尽量使用 public market data。
13. 遇到 multiplier 变化、corporate action、halt、tradability change，暂停对应套利信号。
14. 暂时不要做网页前端，优先把数据和逻辑做对。
15. 不要未经验证就假设某个 token contract、symbol、fee 或 endpoint 存在；应以当前官方 API 响应和官方文档为准。

第一阶段只实现：
A. 项目骨架；
B. Instrument/Symbol Registry；
C. Binance Stocks WebSocket collector；
D. Binance TradFi Perp collector；
E. MarketState；
F. staleness filter；
G. Stock/bStock、bStock/Perp、Stock/Perp 的 opportunity calculator；
H. Parquet/DuckDB 数据保存；
I. CLI 实时输出；
J. pytest 单元测试。

暂时不要：
- 下单；
- mint/redeem；
- 提币；
- 接 DEX；
- 做 GUI。

开发完成后必须：
1. 给出项目目录；
2. 给出启动方式；
3. 给出配置示例；
4. 实际运行 public endpoints；
5. 展示至少一个真实 symbol 的实时 stock/perp 数据；
6. 如果 bStock 行情接口与预期不同，不要伪造结果，应记录问题并根据最新 Binance 官方文档调整；
7. 输出第一版数据样本；
8. 运行 pytest；
9. 总结当前哪些套利路径已经能够被真实数据计算，哪些还不能。

以“数据正确性 > 可复现性 > 稳定性 > 性能”为优先顺序。
```

---

# 46. 第二轮 Codex 任务

第一轮稳定后再给：

```text
在现有 equity-arb-observer 基础上增加 BNB Chain bStock DEX 观察能力。

要求：

1. 先从可信来源验证 bStock 的 BNB Chain contract address，不得根据 ticker 猜地址。
2. 动态发现实际存在流动性的 PancakeSwap V3/V2 池。
3. 对 100 / 500 / 1000 / 2500 / 5000 / 10000 USDT 分别做真实 quote simulation。
4. 计算 effective execution price，而不是只读取 pool mid price。
5. 加入 pool fee、price impact、gas。
6. 加入 Binance CEX bStock ↔ DEX 的双向机会检测。
7. 暂时不广播链上交易。
8. 保存 block number、block timestamp、quote time。
9. 对链上行情和 CEX 行情执行 freshness / block-staleness 检查。
10. 最终输出 CEX/DEX opportunity lifetime、edge 和 executable size。
```

---

# 47. 第三轮 Codex 任务

只有数据证明值得继续才做：

```text
实现 Shadow Execution Engine。

对每一个真实套利信号，在不下单的情况下模拟：

T + 50ms
T + 100ms
T + 200ms
T + 500ms
T + 1000ms

时刻重新读取/重建可成交价格，并计算如果在该延迟下发送订单，策略还能剩多少 edge。

报告：

theoretical edge
shadow fill edge
edge decay
fillable size
expected PnL

按 strategy、symbol、market session 分组。

目标是回答：
普通云服务器 + Binance API 是否来得及吃到这些机会。
```

---

# 48. 官方资料基线

> API 和产品变化很快，Codex 每次开始实现前都应重新核对最新官方文档。

Binance Stocks Trading API：

- [Stocks Trading Introduction](https://developers.binance.com/en/docs/products/stocks/introduction)
- [Stocks Trading Change Log](https://developers.binance.com/en/docs/products/stocks/change-log)
- [Stocks Trading General Info](https://developers.binance.com/en/docs/products/stocks/general-info)
- [Stocks WebSocket General Info](https://developers.binance.com/en/docs/products/stocks/websocket-streams-general-info)

Binance USDⓈ-M Futures：

- [USDⓈ-M Futures Market Data REST API](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [USDⓈ-M Futures WebSocket Market Streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)

bStocks：

- [Binance bStocks Guide](https://www.binance.com/en/academy/articles/what-are-bstocks-a-guide-to-tokenized-stocks-on-binance)
- [Binance bStocks Landing Page](https://www.binance.com/en/bstocks-landing)

---

# 49. 当前官方能力核对（2026-08-11）

截至本文档编写时，官方资料显示：

- Stocks Trading API 于 2026-07-20 初始发布。
- `/sapi/v1/equity/market/exchangeInfo` 可获取股票交易规则和可交易 symbol。
- `/sapi/v1/equity/market/tokenized-assets` 可获取 tokenized assets 与 underlying/multiplier 信息。
- `/sapi/v1/equity/tokenized/mint` 与 `/redeem` 已存在。
- Stocks WebSocket `{SYMBOL}@quote` 提供 best bid / ask，单 symbol 推送有约 200 ms throttle。
- Stocks WebSocket 有 `calendar`、`tradingStatus`、`tradability`。
- bStocks 官方说明为 1:1 underlying backing，并可在 BNB Smart Chain 自托管。
- bStocks 与 underlying stock 的转换官方说明为符合资格用户可 1:1 转换，conversion 本身不收 conversion fee；实际交易手续费、网络费等仍需进入成本模型。
- Binance Futures 可通过 `/fapi/v1/ticker/bookTicker` 获取最佳买卖价。
- `/fapi/v1/premiumIndex` 可获取 mark/index/funding 信息。
- `/fapi/v1/fundingRate` 可获取 funding history。
- `/fapi/v1/tradingSchedule` 和 `tradingSession` stream 可用于识别 TradFi underlying 的交易时段。

这些接口和规则都必须在真正运行前再次动态确认。

---

# 50. 合规与账户资格

bStocks 并不是所有地区都能使用。

程序启动前建议单独实现：

```text
eligibility_check
```

如果账户/司法辖区没有产品资格：

```text
不要绕过限制
不要让程序尝试下单
```

即使只能访问 public market data，仍然可以做研究型 Observer。

真正进入 paper/small-live 前，要重新确认：

```text
Direct Stocks eligibility
bStocks eligibility
TradFi Perp eligibility
API permissions
当地法律与税务要求
```

---

# 51. 最终建议

这个项目第一周只问一个问题：

> **Binance 新形成的 Stock / bStock / TradFi Perp / BSC 四层市场之间，到底有没有普通开发者能够吃到的价差？**

不要先问：

```text
机器人能赚多少钱？
```

先用数据回答：

```text
edge 多大？
多久消失？
多大资金还能成交？
什么时间出现？
谁在把它吃掉？
```

如果答案是：

```text
30–80 bp
持续 1–10 秒
有 $1k–$10k 深度
每天反复出现
```

那这条线值得迅速推进执行器。

如果答案是：

```text
只有 2–5 bp
几十毫秒消失
手续费后为负
```

就及时停掉，把精力转去更低效的新市场。

**先证明市场低效，再写交易机器人。**
