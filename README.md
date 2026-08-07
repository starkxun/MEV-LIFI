# MEV-LIFI

> 链上套利残酷共学 · 个人作战仓库　|　Starkxun　|　2026-08-05 → 08-26

用 LI.FI 报价接口,把"屏幕上的价差"逐项扣成"到手的净收益",判断一条套利路径**到底成不成立**。

核心工具是 [`cost_probe.py`](cost_probe.py) —— 输入任意路径,输出 **token 口径的真实 bps 门槛**。

---

## 三条铁律

1. **只认到手,不认标价。** 同种资产转移用 token 数量算,永远别信 `xxxUSD` 字段。
2. **屏幕价差 ≠ 净收益。** 扣完 gas / 桥费 / 滑点 / 失败交易 / 资金占用,还剩多少?
3. **可复现才是策略,复现不了只是噪声。**

> 铁律 1 不是口号,是用真实数据劈出来的。同一笔 ARB→BAS 的 USDC:
> 美元口径说"赚了 12 bps",token 口径说"亏了 25 bps" —— **连正负号都反。**

---

## 快速开始

```bash
pip install requests

# 一条 ARB→BAS 的 USDC 搬砖,扫四个规模
python3 cost_probe.py --from-chain ARB --to-chain BAS --token USDC \
    --amounts 100,1000,10000,100000
```

输出:

```
           规模              到手        硬成本     账外bps       真实门槛      耗时  路径
       (USDC)          (USDC)      (bps)    (gas等)      (bps)     (秒)
------------------------------------------------------------------------------
       100.00         99.7500      25.00      2.33      27.33       7  eco
     1,000.00        997.5000      25.00      0.23      25.23       7  eco
    10,000.00      9,975.0000      25.00      0.02      25.02       7  eco
   100,000.00     99,750.0000      25.00      0.00      25.00   1,080  polymerStandard
==============================================================================
>> 真实门槛(token 口径): 最低 25.00 bps @ 规模 100,000 USDC
   含义: BAS 上的价格必须比 ARB 高 25.00 bps 才刚好回本。
```

LI.FI 报价接口**只读、免 key、不需要钱包里有钱**。这个脚本不会发起任何交易。

---

## `cost_probe.py` 用法

| 参数 | 说明 |
|---|---|
| `--from-chain` / `--to-chain` | 链,填 key(`ARB`)或 ID(`42161`)。两者相同 = 同链 DEX 交易 |
| `--token` | 同资产路径的简写,等于同时设 from/to |
| `--from-token` / `--to-token` | 跨资产路径分别指定 |
| `--amounts` | 逗号分隔的规模列表,扫出成本曲线 |
| `--roundtrip` | **往返闭环** —— 跨资产路径唯一诚实的算法 |
| `--csv` | 追加进《证据记录表》(共学文档第 4 节 schema) |
| `--dump` | 存原始 JSON,方便自己翻字段 |

```bash
# 单程同资产 —— 经典稳定币搬砖
python3 cost_probe.py --from-chain ARB --to-chain BAS --token USDC --amounts 100,1000,10000

# 跨资产 —— 脚本会拒绝给门槛,并提示你上 roundtrip
python3 cost_probe.py --from-chain ARB --to-chain BAS \
    --from-token USDC --to-token WETH --amounts 10000

# 往返闭环
python3 cost_probe.py --from-chain ARB --to-chain BAS \
    --from-token USDC --to-token WETH --amounts 1000,10000,100000 --roundtrip

# 同链 DEX / 三角
python3 cost_probe.py --from-chain ARB --to-chain ARB \
    --from-token USDC --to-token WETH --amounts 10000 --roundtrip

# 导出证据记录表
python3 cost_probe.py --from-chain ARB --to-chain BAS --token USDC \
    --amounts 1000,10000 --csv evidence.csv
```

---

## 三个设计要点

### 1. 硬成本 vs 账外成本,永远分栏

```
· 硬成本 25.00 bps —— 同种资产数量相减,零价格假设,可信
· 账外   0.02 bps —— gas/未含费用原生币付,折算用了标价,是估算
```

被搬运的资产可以直接相减,**不需要任何价格**;但 gas 是原生币,折算躲不掉价格。
两者混成一个数字的那一刻,你就不知道自己的结论有多硬。

### 2. 往返闭环:铁律 1 在任意资产对上的推广

跨资产单程无法做减法(10000 USDC 减 5.34 WETH 等于什么?)。
把路径闭合回起点,减法就重新成立:

```
10000 USDC(ARB) → 5.34 WETH(BAS) → 9957.78 USDC(ARB)
闭环成本 = 42.2 bps          ← 依然零价格假设
```

**套利本来就是环** —— 三角是环,搬砖回本是环,清算(借币→清算→卖抵押物→还币)也是环。

### 3. 成本有两种形态,不能用同一套话术

- **摊薄型**:门槛随规模**下降**。固定成本(gas)被摊薄,直到撞上比例费地板。
- **冲击型**:门槛随规模**上升**。深度不够,滑点和路由降级越吃越狠 → **最优规模在小端**。

脚本会自动判别并给出对应解释,不会拿摊薄型的话术去描述冲击型的曲线。

---

## 比较候选路径:成本 vs 延迟

`cost_probe.py` 走 `/quote`,它只返回**它认为最好的一条**,判据基本只有到手金额 ——
**它不给延迟定价**。而对套利来说延迟就是成本(资金在途时价差可能消失)。

`route_compare.py` 走 `/advanced/routes`,把全部候选摊开:

```bash
python3 route_compare.py --from-chain ARB --to-chain BAS --token USDC --amount 10000
python3 route_compare.py ... --window 3     # 我的价差窗口只有 3 秒
```

实测同一条路 15 条候选,**11 条被严格支配**(到手不更多、还更慢),剩下的构成真实取舍:

```
Eco        9,975.00     7s   ← 最便宜
Relay      9,974.03     3s   ← 多付 0.97 bps,快 4 秒
AcrossV4   9,974.00     1s   ← 多付 1.00 bps,快 6 秒
Polymer    9,975.00  1,080s  ← 到手和 Eco 一样,慢 154 倍 → 被支配
```

**延迟溢价约 0.17~0.38 bps/秒。** 你的价差窗口越短,越值得多付这几个 bps ——
而 `/quote` 不知道你的窗口有多长,所以这个决定它替你做不了。

## The Graph 历史数据

`cost_probe.py` 只能告诉你**当前**一条路径的真实成本门槛;要判断价差是否可复现,还需要把过去一段时间的池子价格拉出来。这个项目用 [`lib/graph.py`](lib/graph.py) 封装 The Graph 查询,给 `price_history.py` 和 `lp_backtest.py` 复用。

[`lib/graph.py`](lib/graph.py) 主要做三件事:

- 自动读取 `THE_GRAPH_KEY`(环境变量优先,其次项目根 `.env`)。
- 统一处理 The Graph gateway 的 GraphQL 请求、认证、超时和重试。
- 维护一个已验证的 subgraph 注册表 `SUBGRAPHS`,用 `ARB` / `BAS` / `ETH` 这样的项目链名映射到真实 subgraph ID。

这里的“注册表”不是链上合约,也不是系统注册表,只是本仓库里的一张可信数据源清单。每个 ID 都要确认能查通、schema 匹配、同步健康,并且价格和 RPC 直读交叉验证一致。原因很现实:The Graph 上同名 Uniswap V3 subgraph 很多,有的用 Messari schema(`liquidityPool`),有的缺 `poolHourDatas` 时序实体,ID 不能靠猜。

```bash
# 准备 API key
echo 'THE_GRAPH_KEY=你的key' >> .env

# 验证注册表里的 subgraph 是否可用
python3 lib/graph.py

# 从 The Graph 网络 subgraph 里搜索当前可用 ID
python3 lib/graph.py discover uniswap
```

---

## 已有结论

2026-08-01 实测快照:

| 路径 | 规模 | 真实门槛 | 形态 | 判断 |
|---|---|---|---|---|
| ARB→BAS USDC(单程) | 1k–100k | **25 bps** 地板 | 摊薄型 | 稳定币间几乎不可能有 25bps 的真实可成交价差 → **不成立** |
| ARB.USDC ⇄ BAS.WETH(往返) | 1k | **中位 60.45 bps** | 冲击型 | 规模放大到 100k 恶化到 150+ bps |
| ARB.USDC ⇄ ARB.WETH(同链往返) | 1k | ~30 bps(仅 1 次观测) | 冲击型 | 样本不足,待补 |

> ⚠️ **2026-08-05 更正**:上面跨链往返那行原先写的是「~29–43 bps」,那是**基于 2–3 个采样点**的概括,错了。
> 连续监控 48.5 小时、98 次观测后的真实分布:
>
> ```
> 规模 1,000    n=98   中位 60.45   区间 [51.90, 73.31]   标准差 2.94
> 规模 10,000   n=98   中位 60.84   区间 [44.89, 70.52]   标准差 3.54
> 前半 vs 后半中位差 +1.17 bps  → 估计已收敛
> ```
>
> **真实门槛是 60 bps 量级,不是 30 bps 量级。** 早期那个 22.59 bps 的低点是极端值,不代表常态。
> 这正是铁律 3 说的:一个数字是采样,一个分布才是观察 —— 我自己就在这上面栽了一次。
>
> 另外这条路 **60% 的观测伴随上游数据异常**(stargateV2 返回 `toAmountMin > toAmount`),
> 不影响门槛计算(用的是 `toAmount`),但整体可信度要打折。

稳定币那 25 bps 是固定费率,已用 98 次观测确认**饱和**(标准差 0.01 bps,前后半段中位差 0.00);
跨资产的数字虽然中位数已收敛,但单次报价仍在 ±10 bps 内跳,**不能当常量引用**。

**"不成立"本身就是有价值的结论** —— 它把时间从红海里省下来。

> ### ⚠️ 结论的适用范围:那 25 bps 是 LI.FI 的服务费,不是物理成本
>
> 2026-08-06 查了费用明细,ARB→BAS 的 10,000 USDC:
>
> ```
> feeCosts:  LIFI Fixed Fee   25.00 USDC   pct=0.0025   included=true
>            (没有别的费用项)
> gasCosts:  $0.0296
> ```
>
> **桥本身收费接近 0,25 bps 一分不少全是 LI.FI 抽的服务费。** 所以准确的结论是:
>
> - ✅ **通过 LI.FI** 搬 USDC,门槛 25 bps → 这条路不成立
> - ❌ 跨链搬 USDC 的**物理成本**是 25 bps ← **不能这么推广**
>
> 三个直接后果:
>
> 1. **门槛可议价。** 发起人文章明说「默认新用户抽点 25 bps,会根据调用量提供优惠」,
>    并且在推套利场景的专属费率。抽点降到 5 bps,整个可行性判断就翻转。
>
>    > **但这个逃生口已经被外部数据堵上了**(2026-08-06,**非本人测量**):
>    > 群内一份 120 轮实测把费率**扣到 0** 重算,WETH 中位 −1.2 bps、
>    > USDT 中位 +6.4 bps,**120 轮里一轮都没到 +10 bps**。
>    > 即"即使不抽也不成立"。
>    > 适用范围:平静行情、2 小时窗口、3 条 L2、$1,000 规模、基于报价非成交。
>    > 详见 [外部结论存档](docs/week_2/外部结论-零费率实测.md)。
> 2. **绕开聚合器直接调桥,这 25 bps 根本不存在。** 本项目的成本模型量的是
>    「走 LI.FI 的成本」,不是「跨链的成本」。
> 3. **但方法论没白做** —— 正因为坚持 token 口径分栏,才能一眼看出这 25 bps 是
>    **纯比例费、完全不随规模摊薄**,从而判断它是费率而非市场成本。
>    美元口径混算是看不出这一点的。

三个观察上的坑,细节见 [通用成本探针笔记](docs/week_1/通用成本探针.md):

- **`feeCosts[].included`**:`true` 表示已从 `toAmount` 扣掉,再加一次就多算 25 bps,正好把门槛翻倍。
- **路由会随规模切换**:10 万规模时 `eco` → `polymerStandard`,门槛不变但耗时 7s → 1080s。**成本没变不等于风险没变。**
- **上游数据会违反不变量**:实测 stargateV2 返回 `toAmountMin > toAmount`(保底值大于预期值)。API 返回 200 不代表数据自洽。

---

## 目录结构

```
cost_probe.py           通用成本探针(主工具)
watch_probe.py          持续监控:定期跑探针、落 JSONL、门槛变化告警
route_compare.py        比较 /advanced/routes 全部候选:成本 vs 延迟、支配关系、延迟溢价
delay_risk.py           实测延迟风险:链上 swap 秒级数据 → T 秒后价差跑多远
cost_model.py           完整成本模型:七项逐个算,每项带证据等级
make_evidence.py        监控历史 → 证据记录表(自动列刷新,人工判断保留)
li_fi_cost_probe.py     Day-0 原版,保留不动 —— 和上面的 diff 就是学习证明
lib/chainkit.py         采集骨架(复用自 onChainListen):checkpoint / 去重 / 循环容错
lib/graph.py            The Graph 查询客户端 + 已验证 subgraph 注册表
hermes/skills/          三个 Hermes 技能,装到 ~/.hermes/skills/mev-lifi/
evidence.csv            证据记录表(--csv 生成)
watch/*.jsonl           门槛历史(watch_probe 生成)
共学.md                 21 天作战手册:路线图 / 成本模型 / 打卡模板
docs/week_1/
  ├── 机会结构.md              七类套利机会:价差从哪来、谁在抢、天然成本
  ├── 深度如何决定最优规模.md    AMM 定价 / 流动性深度 / 价格冲击 / 区块确认
  ├── LIFI系统端点.md          六个端点分别给你什么原始数据
  ├── 通用成本探针.md          本脚本的四个坑 + 实测结论
  └── 通用成本探针-问答补充.md  探针用途 / 服务器数据关系 / 60 bps 打卡写法
docs/week_2/
  ├── Hermes配置指南.md        从"能启动"配到"能干活"
  ├── 研究工作流.md            五环节工作流 + onChainListen 复用了什么
  ├── Agent工作流教学.md       复用判断 / 监控工程 / 告警设计 / 给 Agent 立纪律
  ├── 证据记录表教学.md         三类列 / 分布不填单点 / 「可复现」问的是什么
  ├── 收费站套利法-问答补充.md  V3 LP / 费率档 / 刷量 / 回测输出的追问整理
  ├── 收费站套利法-验证.md      LP 收费站回测:LVR / 区间扫描 / 为什么不成立
  ├── 接针是什么.md             针与接针概念 / 深针频率与回弹率扫描
  ├── 外部结论-零费率实测.md    群内 120 轮实测存档:零费率仍无机会 / fly 报价污染 / 成本地板准则
  ├── 从数据到结论教学.md       三天数据收口:四道检验 / 日内周期证伪 / 异常归因
  ├── 原子套利.md               Solana 实测拆解 + Sui 可行性待验清单
  ├── 成本模型教学.md           七项怎么从"拍脑袋"变成"可算" / 证据等级
  ├── 待办.md                   Week 2 剩余任务 + 本周攒下的方法论
  └── 服务器拉取数据.md         tmux 长跑攒观测:参数取值理由 / 查看 / 限制
```

## 持续监控

```bash
# 跑一次(给 hermes cron 用);退出码 10 = 有告警
python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC \
    --amounts 1000,10000 --alert-below 20

# 前台每 10 分钟一轮
python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC \
    --amounts 1000 --interval 600

# 历史统计(不发新请求)
python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC --history
```

一次报价只是一个采样点,连续采样才能区分**可复现的机会**和**一次性噪声**(铁律 3)。

## Hermes 技能

```bash
cp -r hermes/skills/* ~/.hermes/skills/mev-lifi/   # 安装
hermes skills list | grep mev                      # 验证
```

| 技能 | 干什么 |
|---|---|
| `mev-cost-probe` | 算任意路径的真实 bps 门槛 |
| `mev-spread-watch` | 持续监控 + 历史统计 |
| `mev-evidence-log` | 按证据表 schema 存档 |

技能文件里最值钱的不是命令,是**约束 Agent 别犯错**的纪律 —— 比如「永远不要引用
`fromAmountUSD` 得出成本结论」「没有历史数据不要填『可复现』列」。
你踩过的每个坑,都该变成技能里的一行约束。

---

## 进度

- [x] **课前** 跑通 `/quote`,建立 token 口径直觉,算出 ARB→BAS = 25 bps
- [x] **Week 1** 机会结构地图 / 深度与最优规模 / LI.FI 端点系统化 / **通用成本探针**
- [ ] **Week 2** Hermes 研究工作流、证据记录表填真实数据、完整成本模型(补失败概率 / 资金占用 / 延迟风险)
- [ ] **Week 3** 快速三查、最小原型、结业三件套

**差异化主线**:不做拥挤的稳定币跨链搬砖,往 **跨链清算 / 长尾链清算 / 经济漏洞型 MEV** 走。

---

## 免责声明

本仓库是学习与研究记录,**不是投资建议**,也不承诺任何盈利。
所有脚本只调用只读报价接口,不发起交易。真实执行前请先跑最坏情况分析,用亏得起的规模验证。
