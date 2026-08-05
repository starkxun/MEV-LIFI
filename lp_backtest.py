#!/usr/bin/env python3
"""
lp_backtest.py —— 「收费站套利法」的证伪工具

假设(来自共学群分享的《收费站套利法》):
    在缺少稳定币直连池的新币上,建一个高费率、区间在现价下方的单边池,
    机器人来回穿越时反复交手续费,你就赚了。

这个脚本用真实历史数据去验它。核心是把收益拆成**两项相反的力**:

    净收益 = 手续费收入  −  逆向选择损失(存货亏损)

原文只写了第一项。但机器人**只在跟你交易对它有利时才路由过来** ——
它拿走的正是你报价与外部市场的错价。这个损失有正式名字叫 LVR
(loss versus rebalancing),是 LP 生意的主要成本,不是"风险之一"。

单边挂在现价下方 = 一张限价买单。它成交的时刻,正是币在往下砸的时刻。
所以"存货亏损"这一项在新币上通常很凶 —— 脚本会把它单独算出来。

用法:
    # 先看有哪些高费率池可测
    python3 lp_backtest.py --list --chain BNB

    # 回测某个池:10000 USDT,区间放在建仓价的 -20% ~ -2%,跑 168 小时
    python3 lp_backtest.py --chain BNB --pool 0x46104643... \\
        --capital 10000 --range-lo -20 --range-hi -2 --hours 168

    # 扫多个区间宽度,看哪种设置最优(或者全都亏)
    python3 lp_backtest.py --chain BNB --pool 0x... --sweep

口径声明:
  · 手续费按「我的流动性 / 池子活跃流动性」分摊。subgraph 的 liquidity 是
    当前 tick 的活跃流动性,这是近似 —— 真实 V3 流动性按 tick 分布。
  · 只在价格落在我的区间内时才计费(区间外不赚钱,这点脚本严格执行)。
  · 不含建池/撤池/换回稳定币的 gas,用 --gas 单独给。
  · **subgraph 的 TVL 不可信**(实测比链上真实余额虚高 3~6 倍),
    所以脚本不用 TVL 做任何计算,只用价格、成交额、手续费、流动性。
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.graph import GraphClient

# 已验证:都有 poolHourDatas,标准 Uniswap schema
CHAINS = {
    "BNB":    {"id": "G5MUbSBM7Nsrm9tH2tGQUiAF4SZDGf2qeo1xPLYjKr7K",
               "name": "Uniswap V3 BNB"},
    "BNB-V4": {"id": "EAq1nJKgjnuKH6Gj4RFjCW7LcL7E2uipbncdwV7TTWkX",
               "name": "uniswap-v4-bnb"},
    "BAS":    {"id": "GqzP4Xaehti8KSfQmv3ZctFSjnSUYZ4En5NRsiTbvZpz",
               "name": "Uniswap V3 Base"},
    "ARB":    {"id": "Fo8QBLpEGfXHWkGMD3jSM4vVLk4JxvxxQD3v3U4fsrbh",
               "name": "uniswap-v3-arbitrum"},
}

STABLES = {"USDT", "USDC", "BUSD", "USDT.Z", "DAI", "FDUSD"}


# ============================================================
# Uniswap V3 集中流动性数学
# ============================================================
# 价格 P = token1 / token0。区间 [Pa, Pb],流动性 L。
#
#   P <= Pa : 全是 token0
#   P >= Pb : 全是 token1
#   中间     : 两者都有
#
# 这套公式是精确的,不是近似 —— 仓位构成完全由 (L, Pa, Pb, P) 决定。

def amounts(L, P, Pa, Pb):
    sa, sb, sp = sqrt(Pa), sqrt(Pb), sqrt(max(P, 1e-30))
    if P <= Pa:
        return L * (1 / sa - 1 / sb), 0.0
    if P >= Pb:
        return 0.0, L * (sb - sa)
    return L * (1 / sp - 1 / sb), L * (sp - sa)


def liquidity_for(amount, is_token1, P, Pa, Pb):
    """给定单边投入,反解 L。"""
    sa, sb, sp = sqrt(Pa), sqrt(Pb), sqrt(max(P, 1e-30))
    if is_token1:                       # 只投 token1,要求 P >= Pb
        span = sb - sa if P >= Pb else sp - sa
        return amount / span if span > 0 else 0.0
    span = (1 / sa - 1 / sb) if P <= Pa else (1 / sp - 1 / sb)
    return amount / span if span > 0 else 0.0


# ============================================================
# 数据
# ============================================================

POOL_Q = '{pool(id:"%s"){feeTier liquidity token0{symbol decimals} token1{symbol decimals} token0Price token1Price}}'
HOUR_Q = """{poolHourDatas(first:1000, orderBy:periodStartUnix, orderDirection:asc,
  where:{pool:"%s", periodStartUnix_gte:%d, periodStartUnix_lt:%d}){
  periodStartUnix token1Price liquidity volumeUSD feesUSD txCount}}"""

LIST_Q = """{pools(first:%d, where:{feeTier_gte:"%d", volumeUSD_gt:"100000"},
  orderBy:volumeUSD, orderDirection:desc){
  id feeTier token0{symbol} token1{symbol} volumeUSD feesUSD
  totalValueLockedUSD txCount createdAtTimestamp}}"""


def fetch_hours(g, sid, pool, start, end):
    out, cur = [], start
    while cur < end:
        rows = g.query_id(sid, HOUR_Q % (pool.lower(), cur, end))["poolHourDatas"]
        if not rows:
            break
        out.extend(rows)
        nxt = int(rows[-1]["periodStartUnix"]) + 1
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    return out


def do_list(g, sid, min_fee):
    pools = g.query_id(sid, LIST_Q % (25, min_fee))["pools"]
    print(f"\n{'池对':24} {'费率':>7} {'累计成交':>17} {'累计费':>13} "
          f"{'TVL':>12} {'费/成交':>8}  地址")
    print("-" * 116)
    for p in pools:
        v = float(p["volumeUSD"] or 0)
        f = float(p["feesUSD"] or 0)
        t = float(p["totalValueLockedUSD"] or 0)
        pair = f"{p['token0']['symbol'][:10]}/{p['token1']['symbol'][:10]}"
        # 数据自洽性检查:手续费/成交额 应该 ≈ 费率。差太远说明数据有问题
        implied = f / v * 100 if v else 0
        flag = ""
        if t > 0 and v / t > 100_000:
            flag = "  ⚠成交额/TVL 过高,疑似刷量"
        print(f"{pair:24} {int(p['feeTier'])/10000:>6.2f}% ${v:>16,.0f} "
              f"${f:>12,.0f} ${t:>11,.0f} {implied:>7.3f}%  {p['id'][:12]}…{flag}")
    print("\n提示:TVL 极小而成交额极大的池基本是刷量,回测它们没有意义。")


# ============================================================
# 回测
# ============================================================

def backtest(rows, meta, capital, lo_pct, hi_pct, gas):
    """
    rows: 逐小时数据(已按时间升序)
    返回一个 dict,包含手续费收入、存货盈亏、净值。
    """
    t0, t1 = meta["token0"], meta["token1"]
    fee_rate = int(meta["feeTier"]) / 1_000_000

    # 判断哪边是稳定币 —— 我们单边投入的就是它
    s0 = t0["symbol"].upper() in STABLES
    s1 = t1["symbol"].upper() in STABLES
    if s0 == s1:
        return {"err": f"无法判断稳定币边({t0['symbol']}/{t1['symbol']}),"
                       f"这个池不适合单边收费站玩法"}
    stable_is_t1 = s1
    stable = t1 if stable_is_t1 else t0
    risk = t0 if stable_is_t1 else t1

    d0, d1 = int(t0["decimals"]), int(t1["decimals"])

    # ---- 单位:V3 的 liquidity 是**原始整数单位**(含 decimals)----
    # subgraph 的 token1Price 是人类可读价;直接拿它算出的 L 和池子的
    # liquidity 差 10^(d1-d0) 个量级,份额会被算成 ~0,手续费全变 0。
    # 所以内部一律用 raw,只在显示时换回人类单位。
    def to_raw(p_human):
        return p_human * 10 ** (d1 - d0)

    P0h = float(rows[0]["token1Price"])         # token1 per token0(人类可读)
    if P0h <= 0:
        return {"err": "起始价格为 0"}
    P0 = to_raw(P0h)

    # 区间:相对起始价的百分比。收费站要求区间落在"买入方向"
    # · 稳定币是 token1 → 我们买 token0,区间要在现价**下方**(P >= Pb)
    # · 稳定币是 token0 → 我们买 token1,区间要在现价**上方**(P <= Pa)
    if stable_is_t1:
        Pa, Pb = P0 * (1 + lo_pct / 100), P0 * (1 + hi_pct / 100)
    else:
        Pa, Pb = P0 * (1 - hi_pct / 100), P0 * (1 - lo_pct / 100)
    if Pa >= Pb or Pa <= 0:
        return {"err": f"区间非法 [{Pa:.6g}, {Pb:.6g}]"}

    d_stable = d1 if stable_is_t1 else d0
    d_risk = d0 if stable_is_t1 else d1
    capital_raw = capital * 10 ** d_stable

    L = liquidity_for(capital_raw, stable_is_t1, P0, Pa, Pb)
    if L <= 0:
        return {"err": "反解流动性失败,可能区间方向不对"}

    a0, a1 = amounts(L, P0, Pa, Pb)
    init_stable = (a1 if stable_is_t1 else a0) / 10 ** d_stable
    init_risk = (a0 if stable_is_t1 else a1) / 10 ** d_risk

    fees = 0.0
    in_range_h = 0
    crossings = 0
    prev_in = None
    px_series = []

    for r in rows:
        Ph = float(r["token1Price"] or 0)
        if Ph <= 0:
            continue
        P = to_raw(Ph)
        px_series.append(Ph if stable_is_t1 else 1 / Ph)
        inside = Pa <= P <= Pb
        if prev_in is not None and inside != prev_in:
            crossings += 1
        prev_in = inside
        if not inside:
            continue
        in_range_h += 1
        # 我的份额 = 我的 L /(池子活跃 L + 我的 L)
        Lpool = float(r["liquidity"] or 0)
        share = L / (Lpool + L) if (Lpool + L) > 0 else 1.0
        vol = float(r["volumeUSD"] or 0)
        fees += vol * fee_rate * share

    Pendh = float(rows[-1]["token1Price"])
    Pend = to_raw(Pendh)
    e0, e1 = amounts(L, Pend, Pa, Pb)
    end_stable = (e1 if stable_is_t1 else e0) / 10 ** d_stable
    end_risk = (e0 if stable_is_t1 else e1) / 10 ** d_risk

    # 把风险资产按**结束时价格**折成稳定币 —— 这是你撤池后真能换回的
    # 注意这里用人类可读价(stable per risk),不是 raw
    px_risk_end = Pendh if stable_is_t1 else 1 / Pendh
    px_risk_0 = P0h if stable_is_t1 else 1 / P0h
    end_value = end_stable + end_risk * px_risk_end

    inventory_pnl = end_value - capital       # 不含手续费的仓位盈亏
    net = inventory_pnl + fees - gas

    return {
        "stable": stable["symbol"], "risk": risk["symbol"],
        "fee_rate": fee_rate, "P0": px_risk_0, "Pend": px_risk_end,
        "Pa": Pa, "Pb": Pb, "L": L,
        # 区间也要换回人类可读的 stable-per-risk 方向,否则和建仓价不同量纲
        "Pa_h": (Pa / 10 ** (d1 - d0)) if stable_is_t1 else 1 / (Pb / 10 ** (d1 - d0)),
        "Pb_h": (Pb / 10 ** (d1 - d0)) if stable_is_t1 else 1 / (Pa / 10 ** (d1 - d0)),
        "capital": capital, "fees": fees,
        "inventory_pnl": inventory_pnl, "gas": gas, "net": net,
        "end_stable": end_stable, "end_risk": end_risk,
        "end_value": end_value,
        "hours": len(rows), "in_range_h": in_range_h, "crossings": crossings,
        "px_min": min(px_series) if px_series else 0,
        "px_max": max(px_series) if px_series else 0,
        "init_risk": init_risk, "init_stable": init_stable,
    }


def show(r, label=""):
    if "err" in r:
        print(f"  ✗ {r['err']}")
        return
    s, k = r["stable"], r["risk"]
    print(f"\n{'='*78}")
    print(f"回测结果 {label}   {k}/{s}   费率 {r['fee_rate']*100:.2f}%")
    print(f"{'='*78}")
    print(f"建仓价 {r['P0']:,.8g} {s}/{k}    结束价 {r['Pend']:,.8g}"
          f"  ({(r['Pend']/r['P0']-1)*100:+.1f}%)")
    print(f"区间   [{r['Pa_h']:,.8g}, {r['Pb_h']:,.8g}]"
          f"   期间价格 {r['px_min']:,.6g} ~ {r['px_max']:,.6g}")
    print(f"时长   {r['hours']} 小时,其中 {r['in_range_h']} 小时在区间内"
          f"({r['in_range_h']/max(r['hours'],1)*100:.0f}%),穿越 {r['crossings']} 次")
    print("-" * 78)
    print(f"投入        {r['capital']:>14,.2f} {s}(单边)")
    print(f"撤池拿回     {r['end_stable']:>14,.2f} {s} + {r['end_risk']:,.6g} {k}"
          f"  折合 {r['end_value']:,.2f} {s}")
    print("-" * 78)
    print(f"① 手续费收入   {r['fees']:>+14,.2f} {s}"
          f"   ({r['fees']/r['capital']*10000:+.1f} bps)")
    print(f"② 存货盈亏     {r['inventory_pnl']:>+14,.2f} {s}"
          f"   ({r['inventory_pnl']/r['capital']*10000:+.1f} bps)  ← 逆向选择在这里")
    print(f"③ gas         {-r['gas']:>+14,.2f} {s}")
    print(f"{'─'*78}")
    print(f"   净收益      {r['net']:>+14,.2f} {s}"
          f"   **{r['net']/r['capital']*10000:+.1f} bps**")
    print(f"{'='*78}")
    if r["fees"] > 0 and r["inventory_pnl"] < 0:
        cover = r["fees"] / abs(r["inventory_pnl"])
        print(f">> 手续费覆盖了 {cover*100:.1f}% 的存货亏损"
              f"{'—— 覆盖得住' if cover >= 1 else '—— **覆盖不住**'}")
    if r["in_range_h"] == 0:
        print(">> 价格从没进过区间:一分钱手续费没赚到,资金白占着。")
        print("   这是收费站最常见的失败模式 —— 不是亏钱,是根本没开张。")


def main():
    p = argparse.ArgumentParser(description="高费率单边 LP(收费站套利法)历史回测")
    p.add_argument("--chain", default="BNB", choices=list(CHAINS))
    p.add_argument("--pool", help="池地址")
    p.add_argument("--list", action="store_true", help="列出可测的高费率池")
    p.add_argument("--min-fee", type=int, default=10000, help="--list 的费率下限")
    p.add_argument("--capital", type=float, default=10000, help="单边投入的稳定币数量")
    p.add_argument("--range-lo", type=float, default=-20,
                   help="区间下沿,相对建仓价的百分比,默认 -20")
    p.add_argument("--range-hi", type=float, default=-2,
                   help="区间上沿,默认 -2")
    p.add_argument("--hours", type=int, default=168, help="回测时长(小时)")
    p.add_argument("--since", help="起始时间 ISO8601,不给就从 --hours 前算起")
    p.add_argument("--gas", type=float, default=5, help="建池+撤池+换回的 gas 估算")
    p.add_argument("--sweep", action="store_true", help="扫多组区间设置")
    args = p.parse_args()

    g = GraphClient()
    sid = CHAINS[args.chain]["id"]

    if args.list:
        do_list(g, sid, args.min_fee)
        return 0
    if not args.pool:
        p.error("要么 --list,要么给 --pool")

    meta = g.query_id(sid, POOL_Q % args.pool.lower())["pool"]
    if not meta:
        print(f"这个 subgraph 里没有池 {args.pool}", file=sys.stderr)
        return 1

    now = int(time.time())
    if args.since:
        start = int(datetime.fromisoformat(
            args.since.replace("Z", "+00:00")).timestamp())
        end = start + args.hours * 3600
    else:
        start, end = now - args.hours * 3600, now
    start -= start % 3600

    rows = fetch_hours(g, sid, args.pool, start, end)
    rows = [r for r in rows if float(r["token1Price"] or 0) > 0]
    if len(rows) < 2:
        print(f"窗口内只有 {len(rows)} 个小时点,不够回测。"
              f"换个时间窗或换个更活跃的池。", file=sys.stderr)
        return 1

    print(f"\n池 {args.pool}  {meta['token0']['symbol']}/{meta['token1']['symbol']}"
          f"  费率 {int(meta['feeTier'])/10000:.2f}%")
    print(f"窗口 {datetime.fromtimestamp(int(rows[0]['periodStartUnix']), timezone.utc):%Y-%m-%d %H:%M}"
          f" ~ {datetime.fromtimestamp(int(rows[-1]['periodStartUnix']), timezone.utc):%Y-%m-%d %H:%M} UTC"
          f"   {len(rows)} 个小时点")

    if args.sweep:
        print("\n区间扫描(负数=现价下方):")
        print(f"{'区间':>16} {'在区间小时':>11} {'穿越':>6} {'手续费':>12} "
              f"{'存货盈亏':>13} {'净收益bps':>11}")
        print("-" * 78)
        for lo, hi in [(-5, -1), (-10, -2), (-20, -2), (-30, -5),
                       (-50, -10), (-20, -10), (-40, -20)]:
            r = backtest(rows, meta, args.capital, lo, hi, args.gas)
            if "err" in r:
                print(f"{f'[{lo}%,{hi}%]':>16}  ✗ {r['err'][:44]}")
                continue
            print(f"{f'[{lo}%,{hi}%]':>16} {r['in_range_h']:>11} {r['crossings']:>6} "
                  f"{r['fees']:>+12,.2f} {r['inventory_pnl']:>+13,.2f} "
                  f"{r['net']/r['capital']*10000:>+11.1f}")
        print("-" * 78)
        print("要是所有区间设置的净收益都是负的,这个池的这段行情就是**证伪**了。")
    else:
        show(backtest(rows, meta, args.capital, args.range_lo,
                      args.range_hi, args.gas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
