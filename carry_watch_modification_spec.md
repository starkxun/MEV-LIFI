# carry_watch.py 修改任务书

> 目标：把当前 `carry_watch.py` 从“按高 carry 持续时间估算收益”的 **window-based scanner**，升级为以真实资金费结算事件为核心的 **settlement-centric logger / backtester**。
>
> 本轮修改重点不是增加自动交易能力，而是修正统计口径和经济模型，使后续 2–4 周采样能够真正回答：
>
> **“跨所 funding carry 在真实手续费、真实结算、真实基差波动和真实执行成本下，是否具有可持续、可扩容的正期望？”**
>
> **约束：**
>
> - 保持脚本只读，不新增任何下单能力；
> - 不接入私有 API key；
> - 优先使用公开行情 / funding / 合约信息端点；
> - 如果某项数据无法通过公开 API 获得，要明确标记为 `unknown / unavailable`，不要静默猜测；
> - 所有影响经济结论的假设必须显式落盘；
> - 旧输出可以保留兼容，但不能继续用错误口径给出“可做 / 不可做”的结论。

---

# 一、当前版本存在的核心问题

## P0-1：把 funding 当成连续利息累计，模型口径错误

当前 `cmd_scan_report()` 使用：

```python
cap = thr / 365 / 24 / 60 * 100 * med
```

来估算一个高 carry 窗口持续 `med` 分钟能够获得多少 bps。

这相当于假设：

> funding 收益随着持仓分钟数连续线性累积。

但实际永续资金费是**按结算时点离散发生**的。

因此：

- 一个 40% 年化窗口持续 3 小时，但没有跨过结算点，实际 funding 可能为 0；
- 一个只持续 30 分钟的高费率窗口，如果恰好覆盖结算时点，反而可能收到完整一期；
- “窗口持续多久”不能直接换算成“实际能收到多少 funding”。

### 必须修改

把核心统计对象从：

```text
高 carry 窗口持续时间
```

改成：

```text
funding settlement event
```

真正需要统计的是：

```text
一次机会期间：
- 跨过了几个结算点
- 每个结算点两边实际 / 最终 funding rate 是多少
- 每次结算理论能收到多少净 funding
```

### 旧逻辑处理

以下指标可以继续保留作为辅助观察：

```text
窗口数量
窗口持续时间
窗口峰值 carry
```

但它们**不能再直接换算成 funding 收益**。

---

# 二、重新定义核心数据模型

建议引入以下概念。

## 1. Snapshot

每次扫描记录：

```json
{
  "ts": "...",
  "symbol": "SOXLUSDT",

  "binance": {
    "bid": 0,
    "ask": 0,
    "bid_qty": 0,
    "ask_qty": 0,
    "mark": 0,
    "index": 0,
    "funding_rate": 0,
    "next_funding_ms": 0,
    "funding_interval_h": 0
  },

  "bybit": {
    "bid": 0,
    "ask": 0,
    "bid_qty": 0,
    "ask_qty": 0,
    "mark": 0,
    "index": 0,
    "funding_rate": 0,
    "next_funding_ms": 0,
    "funding_interval_h": 0
  },

  "basis_mark_bps": 0,
  "entry_short_binance_bps": 0,
  "entry_short_bybit_bps": 0,

  "net_funding_per_interval": {
    "short_binance_long_bybit": 0,
    "short_bybit_long_binance": 0
  }
}
```

注意：

**不要只保存年化。原始 per-settlement funding rate 必须落盘。**

年化只是展示字段，不是经济计算的原始数据。

---

## 2. Settlement Event

新增专门的结算事件记录。

建议格式：

```json
{
  "event": "funding_settlement",
  "symbol": "SOXLUSDT",
  "venue": "Bybit",
  "settlement_ts": "...",

  "previous_next_funding_ms": 0,
  "new_next_funding_ms": 0,

  "rate_before": 0,
  "rate_after": 0,

  "mark_before": 0,
  "mark_after": 0,

  "estimated_settlement_rate": 0,

  "source": "public_market_api",
  "confidence": "estimated"
}
```

如果公开 API 能取得官方历史 funding：

```json
"actual_settlement_rate": ...
"confidence": "confirmed"
```

优先使用官方历史 funding endpoint 对结算事件做回填确认。

---

# 三、P0：修正 funding 结算逻辑

## P0-2：不要用“结算后 snapshot 的 funding rate”代表刚刚结算的 rate

当前 tracker 中逻辑大致为：

```python
if prev and nx > prev:
    rate = snaps[v]["rate"]
```

问题：

`nextFundingTime` 跳变后，当前 `fundingRate` 很可能已经代表**下一期预测值**，不一定等于刚刚结算的最终费率。

### 修改要求

每个 venue / symbol 至少缓存：

```python
last_snapshot
last_rate_before_settlement
last_mark_before_settlement
last_next_funding_ms
```

检测到：

```python
new_next_funding_ms > old_next_funding_ms
```

时：

1. 认为刚跨过一个 settlement；
2. 优先调用官方 funding history endpoint 获取刚结算的真实 rate；
3. 如果查不到：
   - 使用结算前最后一个 snapshot 的 rate；
   - 标记 `confidence = estimated`；
4. 不允许直接拿结算后的新预测 rate 当作刚结算 rate。

---

# 四、P0：修正 funding 收益计算

## P0-3：用结算时名义价值，不要长期固定使用开仓 notional

当前 tracker：

```python
track["notional"] = qty * entry_price
got = track["notional"] * rate
```

这会把 position value 固定在开仓价。

### 修改要求

理论 funding 应按：

```python
settlement_notional = abs(qty) * settlement_mark_price
funding_cash = settlement_notional * funding_rate
```

然后按方向确定收付：

```text
正 funding：
short 收
long 付

负 funding：
short 付
long 收
```

请实现通用函数，例如：

```python
def funding_cashflow(qty, side, mark_price, funding_rate):
    ...
```

并写单元测试覆盖：

```text
long + positive funding  -> negative cashflow
short + positive funding -> positive cashflow
long + negative funding  -> positive cashflow
short + negative funding -> negative cashflow
```

---

# 五、P0：完全重写 scan_report 的“机会价值”计算

目前错误逻辑：

```text
carry × 窗口分钟数
```

必须废弃。

## 新 report 至少输出 3 组统计

### A. Window Statistics

只描述“信号存在多久”：

```text
threshold: |annualized carry| >= 10 / 20 / 40%

- window_count
- windows_per_day
- median_duration
- p75_duration
- p90_duration
- max_duration
```

明确标注：

```text
duration ≠ funding earned
```

---

### B. Settlement Capture Statistics

核心统计：

对于每个 threshold：

```text
满足阈值的窗口中：
- 跨过 ≥1 次结算的比例
- 跨过 ≥2 次结算的比例
- 平均跨过多少次结算
- 结算时净 funding 的 median / p25 / p75 / p90
- 每个 opportunity 实际可捕获 funding bps
```

例：

```text
≥20% 年化信号：
  窗口数                 83
  跨过至少 1 次结算       19 / 83 = 22.9%
  跨过至少 2 次结算        4 / 83 = 4.8%

  实际捕获净 funding:
    median  1.7 bps
    p75     4.2 bps
    p90     9.1 bps
```

这才是真正判断“8 bps 成本能不能覆盖”的数据。

---

### C. Full Trade Economics

最终不要只算 funding。

对每一个 shadow opportunity 计算：

```text
Expected / simulated trade PnL
=
settled funding
+ basis PnL
+ entry spread
+ exit spread
- trading fees
- estimated slippage
```

统一换算为：

```text
bps on one-leg notional
```

---

# 六、P0：手续费模型不能再硬编码成“往返固定 8 bps”

当前：

```python
FEES = {
    "Binance": {"maker": 2.0, "taker": 5.0},
    "Bybit":   {"maker": 2.0, "taker": 5.5},
}
```

这个模型只能作为默认 fallback。

## 修改要求

增加 fee profile 配置层。

例如：

```python
FEE_PROFILES = {
    "default": {
        "Binance": {...},
        "Bybit": {...}
    },

    "tradfi": {
        ...
    }
}
```

支持 CLI：

```bash
--fee-profile default
--fee-profile tradfi
```

以及手动覆盖：

```bash
--binance-maker-bps
--binance-taker-bps
--bybit-maker-bps
--bybit-taker-bps
```

### 更重要

输出中必须区分：

```text
assumed_fee_bps
actual_fee_bps
```

对于 shadow scan：

```text
actual_fee_bps = unknown
```

对于用户手工录入的真实成交：

允许传入真实 fee。

---

# 七、P0：不要再假设 exit spread = entry spread 的镜像

当前：

```python
cost = fee - entry_bps * 2
```

隐含假设：

```text
未来平仓时的基差 / crossing cost
≈
开仓时成本的镜像
```

这个假设不成立。

## 修改

`evaluate()` 只应该计算：

```text
entry executable spread
entry fee
current funding edge
```

不要在实时 snapshot 阶段直接给出“完整往返成本”。

可以保留：

```python
entry_cost_bps
```

但以下字段删除或改名：

```text
total_cost_bps
payback_days
```

建议变成：

```text
fee_assumption_roundtrip_bps
entry_edge_bps
funding_needed_to_cover_fees_bps
```

其中 `payback_days` 如果保留，必须明显标记：

```text
theoretical_continuous_payback_days
```

并且不能作为交易判据。

更推荐直接移除“回本天数”作为主指标。

---

# 八、P1：基差模型统一使用 mark price

当前存在口径错位：

```text
历史 basis：
Kline close / last trade

实时 basis：
mark price
```

这会导致 z-score 的历史分布和实时值不属于同一个统计量。

## 修改要求

优先拉取：

```text
Binance historical mark price
Bybit historical mark price
```

计算：

```python
basis_mark_bps = (binance_mark - bybit_mark) / reference * 1e4
```

历史和实时必须统一。

如果某交易所无法获得足够历史 mark：

允许 fallback 到 last price，但：

```json
"basis_source": "last_price"
```

必须显式记录。

不要把 last-price 历史分布和 mark-price 当前值混在一起。

---

# 九、P1：废弃“11 分钟极差 → 7 bps/day → sqrt(T)”模型

当前文档把：

```text
SOXL 11 分钟 25 个采样
range ≈ 7.1 bps
```

外推为：

```text
基差日内噪音 ≈ 7 bps/day
noise = 7 * sqrt(T)
```

这个结论不应继续使用。

## 新统计方法

针对每个 symbol，持续采集 mark basis：

```text
B_t
```

计算不同 horizon：

```text
ΔB_1h
ΔB_8h
ΔB_1d
ΔB_3d
ΔB_7d
```

分别统计：

```text
mean
median
std
MAD
p05
p10
p25
p75
p90
p95
max adverse move
```

方向必须区分：

如果策略是：

```text
short Binance / long Bybit
```

则重点统计：

```text
basis 扩大造成的 adverse move
```

如果：

```text
short Bybit / long Binance
```

则 adverse 方向相反。

## 增加均值回归诊断

至少输出：

```text
lag-1 autocorrelation
lag-8h autocorrelation
lag-1d autocorrelation
```

可选增加：

```text
half-life estimate
```

但不要强行假设 OU / random walk。

先让数据决定：

```text
stationary / mean-reverting / persistent / regime-shifting
```

---

# 十、P1：流动性过滤必须两边都做

当前 bulk scan 主要使用：

```text
Bybit turnover24h
```

作为流动性过滤。

这不足以说明跨所双边都能执行。

## 修改要求

至少记录：

```text
Binance best bid qty
Binance best ask qty
Bybit best bid qty
Bybit best ask qty
```

并对给定测试名义金额计算：

```text
top_of_book_fillable
```

CLI 增加：

```bash
--test-notional 100
--test-notional 1000
--test-notional 10000
```

如果只有 L1 行情：

至少判断：

```python
available_notional = price * qty
```

两边都足够才算 executable。

更进一步：

如果公开 orderbook depth endpoint 可用，增加：

```text
VWAP @ $100
VWAP @ $1k
VWAP @ $10k
```

输出：

```text
entry_slippage_bps
exit_slippage_proxy_bps
```

---

# 十一、P1：不能对所有 tokenized stocks 共用一个硬编码交易时段

当前：

```python
OPEN_HOURS = {...}
```

是全局固定值。

不同底层资产可能属于：

```text
美股
ETF
韩国股票
其他 TradFi underlying
```

其主市场活跃时段不同。

## 修改要求

不要再把所有币统一分成同一套 `开市 / 休市`。

建议：

### 最低实现

按 symbol 建配置：

```python
SESSION_CONFIG = {
    "SOXLUSDT": ...,
    "SKHYNIXUSDT": ...,
}
```

### 更通用实现

如果无法可靠获得底层市场时间：

直接取消人为 `开市/休市` 分类，改成按 UTC hour 做统计：

```text
hour_00
hour_01
...
hour_23
```

最终从数据中看：

```text
哪个 UTC hour basis 更稳定
哪个 UTC hour spread 更宽
哪个 UTC hour liquidity 更差
```

这种方法比写死 session 更可靠。

---

# 十二、P1：真实 PnL 模型重新定义

旧公式：

```text
总收益
=
basis PnL
+ funding
- fee
```

方向是对的，但需要更完整。

统一定义：

```text
Trade PnL
=
Σ settled funding cashflow
+ mark-to-mark basis PnL
+ realized execution spread
- trading fees
- slippage
```

对于已平仓 shadow trade：

```text
Realized PnL
=
Σ funding
+ qty × (entry_basis - exit_basis)
- entry_fee
- exit_fee
- slippage
```

所有结果同时输出：

```text
USDT
bps
annualized capital return（仅作为展示）
```

**不要用年化作为核心筛选指标。**

---

# 十三、P1：Opportunity 不再定义为“年化高于阈值”

增加一个真正的 shadow entry / exit 机制。

## Shadow Entry

一个 candidate 至少同时满足：

```text
1. net funding direction > 0
2. 两边都满足最低流动性
3. executable entry spread 不超过阈值
4. 距离下一结算点在允许窗口内
5. funding signal 在结算前保持稳定
```

建议支持：

```bash
--entry-before-settlement-min 60
--min-net-funding-bps 1
--max-entry-cost-bps 5
```

注意：

筛选最好直接使用：

```text
下一次 settlement 的净 funding bps
```

而不是年化。

例如：

```text
Bybit funding = +0.035%
Binance funding = +0.001%
方向：short Bybit / long Binance

next settlement net = 3.4 bps
```

这比：

```text
年化 37.2%
```

更接近真实经济意义。

---

# 十四、P1：Shadow Exit 规则

至少实现 3 套离线可比较策略：

## Strategy A：固定结算次数

```text
持有 1 次 funding
持有 2 次 funding
持有 3 次 funding
```

## Strategy B：funding edge 消失

例如：

```text
连续 N 次 snapshot
next settlement net funding <= 0
→ exit
```

## Strategy C：basis mean reversion / stop

例如：

```text
达到 basis profit target
或
basis adverse move 超过风险阈值
```

这三套不要先判断谁对。

全部 shadow 回放，然后比较：

```text
win rate
median trade bps
mean trade bps
p05
max drawdown
holding time
settlement count
```

---

# 十五、P2：增加真正的 Go / No-Go 报告

最终 `scan-report` 不要再输出：

```text
20% 年化以上 = 可做
```

而是输出类似：

```text
=== Business Viability Report ===

采样：
  18 天
  37 个 symbol
  25,920 个 snapshot
  412 个 settlement event

可执行机会：
  128

按 1 次结算退出：
  median PnL      +0.8 bps
  mean PnL        -0.3 bps
  win rate        47%
  p05            -12.4 bps
  fee-adjusted EV -0.3 bps
  => NO-GO

按 2 次结算退出：
  median PnL      +3.7 bps
  mean PnL        +2.1 bps
  win rate        61%
  p05            -15.1 bps
  fee-adjusted EV +2.1 bps
  => WATCH

按 3 次结算退出：
  ...
```

---

# 十六、建议定义正式 Go / No-Go 标准

先不要把阈值写死成 20% 年化。

建议使用实际 trade distribution。

## 进入下一阶段资金验证的最低要求

满足全部条件才算 `GO FOR SMALL-SCALE LIVE TEST`：

```text
1. 至少 2–4 周连续数据
2. 至少 100 个独立 funding settlement opportunity
3. 扣除手续费后：
   mean trade EV > 0
4. median trade EV > 0
5. P25 不应严重为负
6. 结果不能只由 1–2 个极端 outlier 贡献
7. 至少 5 个不同 symbol 有正贡献
8. 不同日期 / 不同市场状态下结果方向一致
```

进入真正扩容前还要增加：

```text
9. 用 $100 / $1k / $10k 名义重新计算 orderbook capacity
10. 加上 taker fallback 情景
11. 加上单腿失败情景
12. 加上 funding 突然反号情景
```

---

# 十七、风险表述修正

当前文档里：

```text
“基差是这个策略唯一的真实风险来源”
```

这句话需要修改。

在纯 delta-neutral 两腿价格 PnL 中，basis change 是主要市场风险。

但真实策略还包括：

```text
- 两腿非原子成交 / 单腿风险
- API / 网络 / VPS 故障
- maker 未成交 → taker fallback
- 流动性枯竭
- funding 突然变化 / 反号
- 两边 funding settlement 时间不一致
- 交易所规则变化
- ADL / 强平 / 保证金规则差异
- tokenized stock / TradFi 特殊规则
- corporate action / 特殊 funding 调整
```

修改文档表述为：

> **在两腿都已成功建立且不考虑执行和交易所事件的前提下，短期价格 PnL 的主要剩余风险是跨所 basis 变化。**

不要再写“唯一风险”。

---

# 十八、代码结构建议

不要继续把所有逻辑塞在一个文件里。

建议最低拆成：

```text
carry_watch.py
    CLI 入口

venues/
    binance.py
    bybit.py

models.py
    Snapshot
    SettlementEvent
    ShadowTrade

funding.py
    settlement detection
    funding cashflow

basis.py
    basis calculation
    horizon statistics

fees.py
    fee profiles

scanner.py
    full market snapshot

backtest.py
    shadow entry / exit
    trade replay

report.py
    settlement report
    viability report
```

如果为了保持当前项目简单，也可以暂时保留单文件，但至少把函数分组。

---

# 十九、必须增加的测试

请 不要只“改到能运行”，必须补测试。

## Test 1：funding 方向

```text
positive funding:
  short 收
  long 付
```

## Test 2：negative funding

```text
negative funding:
  short 付
  long 收
```

## Test 3：结算点 detection

模拟：

```text
nextFundingTime:
16:00
→
00:00
```

必须只触发一次 settlement event。

## Test 4：不能使用结算后的新 rate 回填旧 settlement

模拟：

```text
15:59:50 rate = 0.035%
16:00:10 rate = 0.002%
```

如果没有历史 API：

刚刚 16:00 的 estimated settlement rate 应取：

```text
0.035%
```

不是：

```text
0.002%
```

## Test 5：position notional

```text
qty = 10
entry = 100
settlement mark = 120
```

funding position value 应使用：

```text
1200
```

不是：

```text
1000
```

## Test 6：window 不跨 settlement

高 carry 持续 5 小时：

```text
但没有经过 settlement timestamp
```

理论 captured funding：

```text
0
```

## Test 7：窗口很短但跨 settlement

高 carry 仅持续 10 分钟：

```text
15:55 → 16:05
settlement = 16:00
```

应该捕获 1 次 settlement。

## Test 8：basis PnL

验证：

```python
qty * (entry_basis - exit_basis)
```

方向正确。

## Test 9：不同 fee profile

确保：

```text
default
tradfi
manual override
```

结果不同且可追踪。

---

# 二十、最终 CLI 建议

希望最后可以这样跑：

## 全市场采样

```bash
python carry_watch.py \
  --scan \
  --interval 60 \
  --record-above 0 \
  --test-notional 1000
```

注意：

`--record-above 0`

建议默认记录所有满足流动性门槛的币，避免只保存高 carry 数据导致 selection bias。

---

## 生成 settlement-centric 报告

```bash
python carry_watch.py \
  --scan-report \
  --pattern "shadow/scan_*.jsonl"
```

输出：

```text
Window statistics
Settlement capture statistics
Funding distribution
Basis horizon statistics
Execution cost
Shadow trade PnL
Business viability
```

---

# 二十一、数据采样必须避免 selection bias

这一点很重要。

当前：

```python
if abs(net) < args.record_above:
    continue
```

会导致数据库只保存“看起来有机会”的时刻。

后面你就无法知道：

```text
正常状态是什么
机会出现概率是多少
机会前后怎么演化
低 carry 如何转成高 carry
```

## 修改

推荐：

```text
对于达到流动性门槛的 symbol：
全部保存简化 snapshot
```

高 carry 只是增加：

```json
"is_candidate": true
```

而不是：

```text
低于 threshold 就完全不保存
```

如果担心磁盘：

可以：

```text
正常状态 60s
candidate 状态 10s
```

做动态采样。

---

# 二十二、优先级

## P0 —— 在开始 2–4 周采样前必须完成

```text
[ ] funding 改成 settlement event 模型
[ ] 修正跨结算点判断
[ ] 修正 settlement funding rate 取值
[ ] funding 使用 settlement mark notional
[ ] 废弃 window_duration × annualized carry 收益算法
[ ] 废弃固定 8 bps 作为唯一成本
[ ] 去掉 entry spread × 2 的 exit 假设
[ ] scan-report 改成 settlement capture
[ ] 所有 liquid symbols 都落盘，避免 selection bias
```

## P1 —— 最好同步完成

```text
[ ] historical / realtime basis 统一 mark price
[ ] horizon basis distribution
[ ] 双边流动性
[ ] test notional / depth / slippage
[ ] symbol-specific session 或 UTC-hour statistics
[ ] shadow entry / exit
[ ] fee profiles
```

## P2 —— 有 2–4 周数据后做

```text
[ ] business viability report
[ ] symbol-level edge
[ ] capacity curve
[ ] fee tier sensitivity
[ ] maker / taker sensitivity
[ ] failure scenario stress test
```

---

# 二十三、需要同步修改原研究文档的结论

请同时修正项目 Markdown 文档。

当前以下结论不能继续作为确定结论：

```text
“20% 年化附近是门槛”
“3% 要 72.5 天”
“5% 要 26.1 天”
“高 carry 只持续小时级，所以收不回手续费”
“基差噪音 = 7 bps × sqrt(T)”
“基差是唯一真实风险来源”
```

统一改成：

```text
这些是第一笔实盘形成的 preliminary hypothesis，
不能用于决定资金投入。
```

现阶段可靠结论只有：

```text
1. 跨所双腿实盘执行已经验证可行；
2. 非原子性和单腿风险真实存在；
3. 短期 PnL 明显受到跨所 basis 变化影响；
4. funding edge 的可盈利性必须按真实 settlement event 重新统计；
5. 当前样本不足以判断商业可行性；
6. 下一阶段需要 2–4 周 settlement-centric shadow data。
```

---

# 二十四、完成标准

修改完以后，不要只告诉我“代码已更新”。

必须提供：

```text
1. 修改了哪些函数 / 文件
2. 每项修改对应解决什么问题
3. 新旧数据结构差异
4. 所有测试结果
5. 一份最小运行示例
6. 一份模拟 scan-report 输出
7. 哪些数据仍然只能估算，不能确认
8. 是否还有任何会影响 Go / No-Go 判断的已知偏差
```

并在最后明确回答：

```text
当前版本是否已经可以开始连续跑 2–4 周？
```

只有 P0 全部完成并通过测试，答案才能是：

```text
YES
```

否则必须是：

```text
NO
```

---

# 二十五、执行指令

请直接阅读当前项目中的：

```text
carry_watch.py
第一笔实盘记录 Markdown
```

按照本任务书执行修改。

要求：

- 不新增下单能力；
- 不碰任何 API key；
- 保持现有实盘经验记录；
- 修改代码前先列出实现方案；
- 修改后运行测试；
- 如果发现本任务书中的假设与交易所公开接口实际行为冲突，以真实接口行为为准，但必须把差异写出来；
- 不要为了“让结果看起来能赚钱”调整模型；
- 所有经济性结论必须从原始 settlement event 数据推导；
- 目标不是证明这门生意可行，而是建立一个能够可靠判定 **GO / NO-GO** 的观测系统。
