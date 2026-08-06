---
name: mev-cost-probe
description: "算一条链上套利路径的 token 口径真实 bps 成本门槛。跨链搬砖、同链 DEX、三角套利都适用。"
version: 1.0.0
author: starkxun
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [MEV, Arbitrage, LI.FI, Cost-Model, DeFi]
prerequisites:
  commands: [python3]
---

# MEV 成本探针

调用 `cost_probe.py` 算出一条路径**扣掉所有摩擦后的真实 bps 门槛**。

项目根目录:`~/MEV-LIFI`(下面所有命令都在这里跑)

## 三条铁律(每次回答都必须遵守)

1. **只认到手,不认标价。** 同种资产转移用 token 数量算,**永远不要**引用
   `fromAmountUSD` / `toAmountUSD` 得出成本结论。这两个字段实测会给出符号相反的答案。
2. **屏幕价差 ≠ 净收益。** 报出门槛时必须说明还有哪些成本没算进去。
3. **可复现才是策略。** 单次报价只是一个采样点,不要用一次结果下"这条路成立"的结论。

## 怎么选模式

| 情况 | 命令 |
|---|---|
| 起点和终点是**同种资产**(USDC→USDC) | 直接单程 |
| 起点和终点**不同资产**(USDC→WETH) | **必须加 `--roundtrip`** |
| 同链 DEX / 三角 | `--from-chain` 和 `--to-chain` 填同一条链,加 `--roundtrip` |

**为什么跨资产必须 roundtrip**:10000 USDC 减 5.34 WETH 没有意义。
只有闭环回到起点资产,减法才重新成立,才能得到零价格假设的成本。
脚本在跨资产单程模式下会拒绝给门槛 —— 这是设计,不是 bug,不要绕过它。

## 命令

```bash
# 同资产单程,扫多个规模看成本曲线
python3 cost_probe.py --from-chain ARB --to-chain BAS --token USDC \
    --amounts 100,1000,10000,100000

# 跨资产往返闭环
python3 cost_probe.py --from-chain ARB --to-chain BAS \
    --from-token USDC --to-token WETH --amounts 1000,10000,100000 --roundtrip

# 同链三角
python3 cost_probe.py --from-chain ARB --to-chain ARB \
    --from-token USDC --to-token WETH --amounts 10000 --roundtrip

# 顺手入证据表
... --csv evidence.csv
```

链可以填 key(`ARB` `BAS` `OPT` `POL` `ETH`)或链 ID(`42161` `8453`)。
**永远传多个 `--amounts`** —— 单一规模看不出成本形态。

## 怎么读输出

```
· 硬成本 25.00 bps —— 同种资产数量相减,零价格假设,可信
· 账外   0.02 bps —— gas 折算用了标价,是估算
```

汇报时**必须保持这两栏分开**,不要合并成一个数字。用户需要知道结论有多硬。

成本形态有两种,脚本会自己判别:

- **摊薄型** — 门槛随规模下降,gas 被摊薄,直到撞上比例费地板 → 规模大些更划算
- **冲击型** — 门槛随规模上升,深度不够 → **最优规模在小端**,放大规模只会更糟

## 必须主动提示用户的三件事

1. **路由会随规模切换。** 见到不同规模走了不同 tool 时要指出来。实测过
   10 万规模从 `eco`(7 秒)换成 `polymerStandard`(1080 秒),门槛一模一样但
   延迟涨了 150 倍 —— **成本没变不等于风险没变**。
2. **跨资产门槛是活的,而且少量采样会严重低估它。**
   ARB.USDC⇄BAS.WETH 往返闭环,98 次观测后中位 **60.45 bps**,区间 [51.90, 73.31]。
   而最早 2–3 次采样只报出 22–43 bps —— **低估了一半**。
   所以:报跨资产门槛时必须说明是几次观测;单次结果不要当成"这条路的成本"。
3. **上游数据会骗人。** 脚本会报 `toAmountMin > toAmount` 这类不变量违反。
   见到警告要转述给用户,不要吞掉。

## 报告门槛时必须带的限定词

实测 ARB→BAS USDC 的 25 bps **全部是 `LIFI Fixed Fee`**(`percentage: 0.0025`),
底层桥收费接近 0。所以:

- ✅ 说「**通过 LI.FI** 走这条路,门槛 25 bps」
- ❌ 不要说「跨链搬 USDC 的成本是 25 bps」

**这是聚合器的定价决策,不是物理成本** —— 可议价(LI.FI 对大流量/套利场景有优惠费率),
也可以绕开(直接调桥就没有这一层)。

判别方法:如果某项成本**完全不随规模变化**(100 和 100,000 规模下都是 25.00 bps),
那它几乎一定是固定费率而非市场成本。市场成本(滑点、深度)不可能这么规整。

## 还没算进门槛的成本

回答里要提醒:当前门槛**不含**失败交易概率、资金占用成本、
延迟期间价差消失的概率。这三项是完整成本模型待补的部分。

## 比较多条候选路径

`cost_probe.py` 用的是 `/quote`,它只返回**它认为最好的一条**,而且判据基本只有
到手金额 —— **它不给延迟定价**。

要看全部候选和「成本 vs 延迟」的取舍,用 `route_compare.py`:

```bash
python3 route_compare.py --from-chain ARB --to-chain BAS --token USDC --amount 10000
python3 route_compare.py ... --window 3      # 我的价差窗口只有 3 秒
```

实测同一条路 15 条候选里 **11 条被严格支配**(到手不更多还更慢),
而剩下的构成一条真实的取舍曲线:

```
Eco        9,975.00     7s   ← 最便宜
Relay      9,974.03     3s   ← 多付 0.97 bps,快 4 秒
AcrossV4   9,974.00     1s   ← 多付 1.00 bps,快 6 秒
```

**延迟溢价约 0.17~0.38 bps/秒。** 用户的价差窗口越短,越值得多付。
`/quote` 不知道用户的窗口,所以这个决定它替不了 —— 要主动问用户窗口有多长。

## 边界

- LI.FI 报价接口只读免 key,这个脚本**不会发起任何交易**。
- **限速按端点分:** `/quote` 和 `/advanced/routes` 是 **75 次 / 2 小时**;
  `/chains` `/token` 这类是 100 次 / 60 秒。前者没有 ratelimit 响应头,查不到剩余额度。
  脚本自带节流和 429 重试,**不要并发绕过、不要换 Key 绕过**。
  一次 `--amounts 100,1000,10000,100000` 就是 4 次 quote,往返模式翻倍 —— 心里要有数。
- 报不出价(`✗`)是**结论**不是故障 —— 说明这条路在这个规模上不成立。
