#!/usr/bin/env python3
"""
cost_model.py —— 完整成本模型:把共学文档第 5 节的公式变成可算的东西

    净收益 = 屏幕价差
            − 桥费 / 协议费
            − 交易滑点
            − gas
            − 延迟风险(执行期间价差消失的概率 × 损失)
            − 失败交易成本(失败概率 × 已花 gas + 占用资金)
            − 资金占用 / 机会成本

**这个脚本的核心不是把七项加起来,而是给每一项标上证据等级。**

    [实测]  有数据、有样本量,可复现
    [公式]  由可观测的输入算出来(比如资金占用 = 延迟 × 链上真实利率)
    [待定]  测不了,老实空着

> **参数测不出来的成本模型没有价值。**
> 很多人会在这一步填"失败概率假设 5%",然后算出一个精确到小数点后两位的
> 净收益 —— 那是把猜测包装成计算。整个项目的方法论都在反对这个,
> 不该在最后一步破功。

数据来源:
  · 桥费/滑点/gas/延迟  ← cost_probe.py(LI.FI /quote 实测)
  · 延迟风险            ← delay_risk.py 产出的 JSON(链上 swap 秒级实测)
  · 资金占用利率        ← Aave V3 链上真实存款利率(不是拍脑袋的"假设年化 X%")
  · 失败概率            ← **没有**,需要真实执行才能测

用法:
    # 先跑一次延迟风险(可以缓存复用)
    python3 delay_risk.py --hours 24 --json delay_risk.json

    # 再算完整成本
    python3 cost_model.py --from-chain ARB --to-chain BAS --token USDC --amount 10000
    python3 cost_model.py --from-chain ARB --to-chain BAS \\
        --from-token USDC --to-token WETH --amount 10000 --roundtrip

    # 带上你观测到的屏幕价差,直接给净收益判断
    python3 cost_model.py ... --spread 12.5
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cost_probe as cp

# Aave V3 Pool + USDC。用它的**存款利率**当资金占用的机会成本下界 ——
# 理由:那是你不做套利时,同一笔钱躺着能拿到的、链上可查的真实收益。
# 比"假设年化 10%"硬得多。
AAVE = {
    "ARB": ("https://arb1.arbitrum.io/rpc", "0x794a61358D6845594F94dc1DB02A252b5b4814aD",
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
    "BAS": ("https://mainnet.base.org", "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"),
}
SEC_PER_YEAR = 365 * 24 * 3600


def aave_supply_apy(chain):
    """
    读 Aave V3 的 currentLiquidityRate(存款年化,ray 定点数 1e27)。

    getReserveData 返回结构里第 3 个 32 字节字就是它:
        word0 configuration / word1 liquidityIndex / **word2 currentLiquidityRate**
    """
    if chain.upper() not in AAVE:
        return None
    rpc, pool, asset = AAVE[chain.upper()]
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 25}))
        sel = Web3.to_hex(Web3.keccak(text="getReserveData(address)"))[:10]
        data = sel + asset[2:].lower().rjust(64, "0")
        r = w3.eth.call({"to": Web3.to_checksum_address(pool), "data": data})
        b = bytes(r)
        if len(b) < 96:
            return None
        return int.from_bytes(b[64:96], "big") / 1e27
    except Exception:
        return None


def load_delay_risk(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def delay_risk_at(dr, seconds, pct="adverse_p95"):
    """
    取对应延迟的风险值。实测只有几个离散的 horizon,中间用**线性插值**;
    超出最大 horizon 就外推最后一段的斜率,并标记为外推。

    用 95% 分位而不是中位数 —— 成本模型该用保守值,
    而延迟风险的分布是**长尾**的(中位常常是 0,尾部才咬人)。
    """
    if not dr or not dr.get("horizons"):
        return None, "无数据"
    hs = sorted((int(k), v) for k, v in dr["horizons"].items() if v.get(pct) is not None)
    if not hs:
        return None, "无数据"
    if seconds <= hs[0][0]:
        return hs[0][1][pct], f"实测@{hs[0][0]}s"
    for i in range(1, len(hs)):
        t0, v0 = hs[i - 1]
        t1, v1 = hs[i]
        if seconds <= t1:
            w = (seconds - t0) / (t1 - t0) if t1 > t0 else 0
            return v0[pct] + w * (v1[pct] - v0[pct]), f"插值@{t0}~{t1}s"
    # 外推
    t0, v0 = hs[-2] if len(hs) >= 2 else hs[-1]
    t1, v1 = hs[-1]
    if t1 > t0:
        slope = (v1[pct] - v0[pct]) / (t1 - t0)
        return v1[pct] + slope * (seconds - t1), f"⚠外推(>{t1}s)"
    return v1[pct], f"实测@{t1}s"


def main():
    p = argparse.ArgumentParser(description="完整成本模型,每项带证据等级")
    p.add_argument("--from-chain", required=True)
    p.add_argument("--to-chain", required=True)
    p.add_argument("--token")
    p.add_argument("--from-token")
    p.add_argument("--to-token")
    p.add_argument("--amount", type=float, default=10000)
    p.add_argument("--roundtrip", action="store_true")
    p.add_argument("--address", default=cp.DEFAULT_ADDRESS)
    p.add_argument("--delay-risk", default="delay_risk.json",
                   help="delay_risk.py 产出的 JSON")
    p.add_argument("--spread", type=float,
                   help="你观测到的屏幕价差(bps)。给了才能算净收益")
    p.add_argument("--capital-rate", type=float,
                   help="资金机会成本年化(小数)。不给则读 Aave 链上真实存款利率")
    p.add_argument("--json", help="导出")
    args = p.parse_args()

    if args.token:
        args.from_token = args.from_token or args.token
        args.to_token = args.to_token or args.token
    if not args.from_token:
        p.error("必须给 --token 或 --from-token")
    args.to_token = args.to_token or args.from_token
    args.amounts = str(args.amount)

    # ---- 1. 从 LI.FI 拿硬成本 / gas / 延迟 ----
    from_meta = cp.get_token(args.from_chain, args.from_token)
    try:
        r = cp.probe_size(args, args.amount, from_meta)
    except cp.NoQuote as e:
        print(f"报不出价:{e}\n—— 这条路在这个规模上不成立,本身就是结论。",
              file=sys.stderr)
        return 1

    sym = from_meta["symbol"]
    same = args.roundtrip or args.from_token.upper() == args.to_token.upper()

    # ---- 2. 延迟风险 ----
    dr = load_delay_risk(args.delay_risk)
    dv, dsrc = delay_risk_at(dr, r.duration)

    # ---- 3. 资金占用 ----
    rate = args.capital_rate
    rate_src = "命令行指定"
    if rate is None:
        rate = aave_supply_apy(args.from_chain)
        rate_src = f"Aave V3 {args.from_chain} USDC 存款 APY"
    if rate is None:
        rate, rate_src = 0.0, "读取失败,按 0 处理"
    capital_bps = rate * (r.duration / SEC_PER_YEAR) * 10_000

    # ---- 输出 ----
    path = f"{args.from_chain}.{args.from_token} → {args.to_chain}.{args.to_token}"
    if args.roundtrip:
        path += f" → {args.from_chain}.{args.from_token}"

    print()
    print("=" * 82)
    print(f"完整成本模型   {path}")
    print(f"规模 {args.amount:,.0f} {sym}   路由 {r.tools}   耗时 {r.duration}s")
    print("=" * 82)
    print(f"{'成本项':22} {'bps':>10}  证据")
    print("-" * 82)

    rows = []
    if same:
        rows.append(("桥费/协议费 + 滑点", r.hard_bps,
                     f"[实测] token 口径,零价格假设"))
    else:
        rows.append(("桥费/协议费 + 滑点", None,
                     "[无法定义] 跨资产单程,加 --roundtrip"))
    rows.append(("gas + 未含费用", r.offchain_bps,
                 f"[实测] 原生币折算,用了标价"))
    rows.append(("延迟风险", dv,
                 f"[实测] {dsrc} 不利侧95%分位" if dv is not None
                 else "[缺] 先跑 delay_risk.py"))
    rows.append(("资金占用", capital_bps,
                 f"[公式] {r.duration}s × {rate*100:.2f}% ({rate_src})"))
    rows.append(("失败交易成本", None,
                 "[待定] 需真实执行才能测失败概率"))

    total = 0.0
    for name, v, ev in rows:
        if v is None:
            print(f"{name:22} {'—':>10}  {ev}")
        else:
            total += v
            print(f"{name:22} {v:>10.2f}  {ev}")

    print("-" * 82)
    # **主成本项缺失时绝对不能打印合计。**
    # 第一版会在跨资产单程时照样输出「合计 2.46 bps」—— 而那一项里
    # 最大的桥费/滑点是「无法定义」的。一个漏掉大头的合计,比没有合计危险得多:
    # 它看起来像个可用的数字。
    if not same:
        print("合计:**拒绝给出** —— 主成本项(桥费/滑点)在跨资产单程下无法定义。")
        print("      加 --roundtrip 让路径闭合回起点,减法才重新成立。")
        print("=" * 82)
        return 0
    print(f"{'合计(不含待定项)':22} {total:>10.2f}  bps")
    print("=" * 82)

    if args.spread is not None:
        net = args.spread - total
        print(f"\n>> 净收益 = 屏幕价差 {args.spread:.2f} − 成本 {total:.2f} = "
              f"**{net:+.2f} bps**")
        if net <= 0:
            print("   → 不成立。而且这还**没扣失败交易成本**,实际更差。")
        else:
            print(f"   ⚠ 名义为正 {net:.2f} bps,但**失败交易成本还没算**。")
            print(f"     只要失败概率 × 单次失败损失 > {net:.2f} bps,就依然是负的。")
    else:
        print(f"\n>> 屏幕价差必须 > **{total:.2f} bps** 才可能为正")
        print(f"   (而且这是**下界** —— 失败交易成本还没算进去)")

    # 诚实提醒
    print("\n口径声明:")
    if dr and dr.get("hourly_vol_median") is not None:
        print(f"  · 延迟风险来自 {dr.get('title','?')},"
              f"样本逐小时波动中位 {dr['hourly_vol_median']:.2f} bps")
        print(f"    **平静时段的数;行情剧烈时会显著放大。**")
    print(f"  · 资金占用在 {r.duration}s 这个量级上"
          f"{'几乎可忽略' if capital_bps < 0.1 else '不可忽略'}"
          f"({capital_bps:.3f} bps)——"
          f"它只在资金锁几天(比如有挑战期的桥)时才咬人。")
    print(f"  · 失败交易成本是**唯一测不了**的一项。别拿假设值填 ——")
    print(f"    要么真实小额执行去测,要么就承认这个模型有个已知缺口。")
    print("=" * 82)

    if args.json:
        out = {"path": path, "amount": args.amount, "duration_s": r.duration,
               "tools": r.tools,
               "items": [{"name": n, "bps": v, "evidence": e} for n, v, e in rows],
               "total_bps": total, "spread_bps": args.spread,
               "net_bps": (args.spread - total) if args.spread is not None else None}
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"已导出 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
