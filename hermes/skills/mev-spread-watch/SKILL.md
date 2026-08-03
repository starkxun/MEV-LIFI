---
name: mev-spread-watch
description: "持续监控一条套利路径的成本门槛,落 JSONL 历史,门槛跌破阈值或路由切换时告警。"
version: 1.0.0
author: starkxun
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [MEV, Monitoring, Arbitrage, Cron, DeFi]
prerequisites:
  commands: [python3]
---

# 成本门槛持续监控

`watch_probe.py` 定期跑成本探针,把每次结果按 JSONL 存下来,变化时告警。

项目根目录:`~/MEV-LIFI`

## 为什么要盯而不是看一眼

一次报价只是**一个采样点**。跨资产路径的门槛实测在 22–43 bps 之间跳动,
因为 LI.FI 会实时换路由。只有连续采样才能回答:

- 这条路**平时**是什么水平?(中位数,不是某一次的运气)
- 什么时候会变好?有没有规律?
- 那个"好数字"是**可复现的机会**还是**一次性噪声**?

最后一条正是铁律 3。没有历史,就没法区分 edge 和噪声。

## 命令

```bash
# 跑一次(给 cron 用,每次拉起一个新进程)
python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC \
    --amounts 1000,10000 --alert-below 20

# 前台每 10 分钟一轮
python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC \
    --amounts 1000 --interval 600

# 往返闭环同样能盯
python3 watch_probe.py --from-chain ARB --to-chain BAS \
    --from-token USDC --to-token WETH --amounts 1000 --roundtrip --interval 900

# 看历史统计(不发新请求,可以随时跑)
python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC --history
```

参数和 `cost_probe.py` 一致,额外多了:

| 参数 | 作用 |
|---|---|
| `--interval N` | >0 每 N 秒一轮;省略=跑一次退出(cron 模式) |
| `--max-rounds N` | 最多跑几轮,0=无限 |
| `--alert-below X` | 门槛跌破 X bps 告警 —— 你真正在等的机会窗口 |
| `--alert-delta X` | 门槛单轮变化超过 X bps 告警,默认 10 |
| `--history` | 只打印统计,不发请求 |

## 输出

- `watch/<路径标识>.jsonl` —— 每轮一行,门槛/路由/耗时/告警全存着
- stdout —— 人类可读的一行摘要
- **退出码 10 = 触发了告警**(cron 里可以据此决定要不要通知用户)

## 三种告警分别意味着什么

| 告警 | 现实含义 |
|---|---|
| 门槛跌破阈值 | 机会窗口可能开了 —— 但**还要验真实性和可执行性**,别直接下结论 |
| 门槛突变 | 路由或深度出事了,可能是机会也可能是陷阱 |
| 路由切换 | **成本没变不等于风险没变**。要同时看 `duration_s` |
| 上游数据异常 | LI.FI 返回了自相矛盾的数据,这轮的数字要打折扣 |

## 配成定时任务

```bash
hermes cron create
```

建议:稳定币路径 15–30 分钟一次就够(它的 25 bps 是固定费率,很稳);
跨资产路径可以密一些(它是活的),但注意 LI.FI 未认证限速 100 次/60 秒。

## 汇报纪律

- 报告告警时,**必须同时给出历史区间**(跑 `--history`)。
  "现在 22 bps" 没有意义;"现在 22 bps,历史中位 29,最低 22" 才有意义。
- **不要因为一次跌破阈值就说"这条路成立"。** 门槛只是成本的一半,
  另一半是屏幕价差,那要独立观测。净收益 = 屏幕价差 − 真实成本。
- 告警里的路由切换要连着耗时一起报。
