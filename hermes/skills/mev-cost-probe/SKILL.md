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
2. **跨资产门槛是活的。** 同一条路两次报价实测在 22–43 bps 之间跳。
   不要把跨资产的数字当常量引用,要说"现跑现看"。
3. **上游数据会骗人。** 脚本会报 `toAmountMin > toAmount` 这类不变量违反。
   见到警告要转述给用户,不要吞掉。

## 还没算进门槛的成本

回答里要提醒:当前门槛**不含**失败交易概率、资金占用成本、
延迟期间价差消失的概率。这三项是完整成本模型待补的部分。

## 边界

- LI.FI 报价接口只读免 key,这个脚本**不会发起任何交易**。
- 未认证限速 100 次/60 秒,脚本自带节流和 429 重试,不要并发绕过。
- 报不出价(`✗`)是**结论**不是故障 —— 说明这条路在这个规模上不成立。
