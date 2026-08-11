#!/usr/bin/env python3
"""
funding_probe.py —— 资金费 carry 探针(只读公开行情,不需要 API key)

配套教学文档:docs/week_3/资金费对冲教学.md

这个脚本回答一个问题:**做资金费 carry,哪些币真的付得够多、而且付得稳?**

    python3 funding_probe.py floor                 # 手续费地板是多少
    python3 funding_probe.py rates --limit 100     # 短窗口快照(会骗人,见 --help)
    python3 funding_probe.py history BTC_USDC_PERP # 单个币的长窗口稳定性
    python3 funding_probe.py screen                # ★ 主命令:长窗口筛选

⚠️ 为什么有 `rates` 又有 `screen`:
   `rates` 用短窗口,快但**会骗人** —— 实测 4 天窗口把 BILL 年化算成 +126%,
   长窗口真值是 +13.57%(高估 9.3 倍);而且 5 个"符号 100% 稳定"的候选里,
   **3 个在长窗口下符号是反的**。
   `rates` 保留下来是为了让你能亲手复现这个陷阱,不是给你做决策用的。
"""

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime

API = "https://api.backpack.exchange/api/v1"

# Backpack Tier 1(默认档)费率,单位 bps。来源:官方 VIP Program 文档
FEES = {
    "spot_maker": 8, "spot_taker": 10,
    "perp_maker": 2, "perp_taker": 5,
}


def get(url, n=3):
    for i in range(n):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception:
            if i == n - 1:
                raise
            time.sleep(1.5 * (i + 1))


def interval_hours():
    """结算间隔。别写死 8 小时 —— Backpack 实测是 1 小时。"""
    h = get(f"{API}/fundingRates?symbol=SOL_USDC_PERP&limit=6")
    ts = [datetime.fromisoformat(x["intervalEndTimestamp"]) for x in h]
    return abs((ts[0] - ts[1]).total_seconds()) / 3600


def pull_history(sym, max_pages=12, per=1000):
    """分页拉满历史。API 单次上限实测 1000 条。"""
    out = []
    for p in range(max_pages):
        try:
            d = get(f"{API}/fundingRates?symbol={sym}&limit={per}&offset={p*per}")
        except Exception:
            break
        if not d:
            break
        out += d
        if len(d) < per:
            break
    return [float(x["fundingRate"]) for x in out]


# ══════════════════════════════════════════════════════════════════
def cmd_floor(args):
    """手续费地板 —— 一切判断的起点。"""
    iv = interval_hours()
    print("=== Backpack Tier 1 费率(官方文档,bps)===")
    for k, v in FEES.items():
        print(f"  {k:<12} {v}")
    mm = (FEES["spot_maker"] + FEES["perp_maker"]) * 2
    tt = (FEES["spot_taker"] + FEES["perp_taker"]) * 2
    print(f"\n=== 往返手续费地板(开仓 + 平仓,现货腿 + 永续腿)===")
    print(f"  全 maker   {mm} bps")
    print(f"  全 taker   {tt} bps")
    print(f"\n  ⚠️ Backpack 对**所有 taker 单**有 100ms 减速带,只有 postOnly 免疫。")
    print(f"     所以 taker 的真实成本 > {tt} bps,还要加上那 100ms 里的盘口漂移。")
    print(f"\n=== 要靠资金费赚回 {mm} bps,需要多少年化 ===")
    print(f"  结算间隔 {iv:.0f} 小时,一年 {8760/iv:.0f} 次\n")
    for days in (1, 3, 7, 30, 90, 365):
        need = mm / 1e4 / (days / 365) * 100
        print(f"  持仓 {days:>3} 天回本  →  需要年化 {need:>8.2f}%")
    return 0


def cmd_rates(args):
    """短窗口快照。**会骗人**,保留是为了让你亲手复现这个陷阱。"""
    iv = interval_hours()
    mp = get(f"{API}/markPrices")
    syms = [m["symbol"] for m in mp if m["symbol"].endswith("_PERP")][:args.top]
    rows = []
    for s in syms:
        try:
            h = get(f"{API}/fundingRates?symbol={s}&limit={args.limit}")
        except Exception:
            continue
        fr = [float(x["fundingRate"]) for x in h]
        if len(fr) < 20:
            continue
        rows.append({"sym": s, "n": len(fr),
                     "ann": statistics.mean(fr) * (8760 / iv) * 100,
                     "pos": sum(1 for x in fr if x > 0) / len(fr) * 100})
    rows.sort(key=lambda r: -abs(r["ann"]))
    print(f"⚠️ 短窗口({args.limit} 期 ≈ {args.limit*iv/24:.1f} 天)快照 —— **不要拿它做决策**\n")
    print(f"{'币种':<22}{'年化%':>10}{'同号%':>8}")
    print("-" * 42)
    for r in rows[:args.show]:
        print(f"{r['sym']:<22}{r['ann']:>+10.2f}{r['pos']:>7.0f}%")
    print(f"\n跑 `screen` 看这些数字在长窗口下还剩多少。")
    return 0


def analyse(sym, iv, floor_bps):
    fr = pull_history(sym)
    if len(fr) < 200:
        return None
    half = len(fr) // 2
    mean = statistics.mean(fr)
    a_recent = statistics.mean(fr[:half]) * (8760 / iv) * 100
    a_old = statistics.mean(fr[half:]) * (8760 / iv) * 100
    per_day = abs(mean) * 1e4 * (24 / iv)
    return {
        "sym": sym, "n": len(fr), "days": len(fr) * iv / 24,
        "ann": mean * (8760 / iv) * 100,
        "pos": sum(1 for x in fr if x > 0) / len(fr) * 100,
        "a_recent": a_recent, "a_old": a_old,
        "flip": a_recent * a_old < 0,
        "payback_days": floor_bps / per_day if per_day > 0 else float("inf"),
    }


def cmd_history(args):
    iv = interval_hours()
    floor = (FEES["spot_maker"] + FEES["perp_maker"]) * 2
    r = analyse(args.symbol, iv, floor)
    if not r:
        print(f"{args.symbol} 历史不足 200 期"); return 1
    print(f"=== {r['sym']} ===")
    print(f"  样本      {r['n']} 期 = {r['days']:.0f} 天")
    print(f"  年化      {r['ann']:+.2f}%")
    print(f"  同号占比  {r['pos']:.0f}%")
    print(f"  近半 {r['a_recent']:+.2f}%   远半 {r['a_old']:+.2f}%"
          + ("   ⚠️ 两半反号 —— 不可用" if r["flip"] else "   ✅ 方向一致"))
    print(f"  回本天数  {r['payback_days']:.1f} 天(要赚回 {floor} bps 往返手续费)")
    return 0


def cmd_screen(args):
    """★ 主命令:长窗口筛选,这才是能用来做决策的。"""
    iv = interval_hours()
    floor = (FEES["spot_maker"] + FEES["perp_maker"]) * 2
    mp = get(f"{API}/markPrices")
    syms = [m["symbol"] for m in mp if m["symbol"].endswith("_PERP")][:args.top]
    print(f"长窗口筛选 {len(syms)} 个永续(每个拉满历史,可能要几分钟)…\n")
    rows = []
    for i, s in enumerate(syms):
        r = analyse(s, iv, floor)
        if r:
            rows.append(r)
        if (i + 1) % 10 == 0:
            print(f"  …{i+1}/{len(syms)}", flush=True)
    rows.sort(key=lambda r: -abs(r["ann"]))

    print(f"\n{'币种':<22}{'期数':>7}{'天':>6}{'年化%':>9}{'同号%':>7}"
          f"{'近半':>9}{'远半':>9}{'回本天':>8}")
    print("-" * 82)
    for r in rows[:args.show]:
        mark = " ⚠️反号" if r["flip"] else ""
        print(f"{r['sym']:<22}{r['n']:>7}{r['days']:>6.0f}{r['ann']:>+9.2f}"
              f"{r['pos']:>6.0f}%{r['a_recent']:>+9.1f}{r['a_old']:>+9.1f}"
              f"{r['payback_days']:>8.1f}{mark}")

    # 四问过滤器里的「③ 成本」那一关
    ok = [r for r in rows
          if not r["flip"] and r["pos"] >= args.min_consistency
          and r["payback_days"] <= args.max_payback]
    print(f"\n{'='*82}")
    print(f"=== 通过长窗口筛选的(同号≥{args.min_consistency}%、两半不反号、"
          f"{args.max_payback} 天内回本)===")
    if not ok:
        print("  一个都没有。")
    for r in ok:
        print(f"  {r['sym']:<22} 年化 {r['ann']:+7.2f}%  同号 {r['pos']:.0f}%  "
              f"{r['days']:.0f} 天样本  回本 {r['payback_days']:.1f} 天")
    print(f"\n  {len(ok)}/{len(rows)} 通过")
    print(f"\n⚠️ 通过 ≠ 可以下单。还有一项没测:**单腿发生率**。")
    print(f"   它只能靠小额真跑测出来 —— 按我们自己的规矩,")
    print(f"   有待定项就不出「总收益」这个数。")
    if args.out:
        json.dump(rows, open(args.out, "w"), ensure_ascii=False, indent=1)
        print(f"\n已存 {args.out}")
    return 0


def main():
    p = argparse.ArgumentParser(description="资金费 carry 探针(只读公开行情)")
    sub = p.add_subparsers(dest="cmd")

    f = sub.add_parser("floor", help="手续费地板 + 回本所需年化")
    f.set_defaults(func=cmd_floor)

    r = sub.add_parser("rates", help="短窗口快照(会骗人,仅供复现陷阱)")
    r.add_argument("--limit", type=int, default=100)
    r.add_argument("--top", type=int, default=40)
    r.add_argument("--show", type=int, default=18)
    r.set_defaults(func=cmd_rates)

    h = sub.add_parser("history", help="单个币的长窗口稳定性")
    h.add_argument("symbol")
    h.set_defaults(func=cmd_history)

    s = sub.add_parser("screen", help="★ 长窗口筛选(决策用这个)")
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--show", type=int, default=25)
    s.add_argument("--min-consistency", type=float, default=70)
    s.add_argument("--max-payback", type=float, default=30)
    s.add_argument("--out", default="shadow/funding_screen.json")
    s.set_defaults(func=cmd_screen)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
