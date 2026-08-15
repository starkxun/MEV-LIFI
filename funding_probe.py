#!/usr/bin/env python3
"""
funding_probe.py —— 资金费 carry 探针(只读公开行情,不需要 API key)

配套教学文档:docs/week_3/资金费对冲教学.md

这个脚本回答一个问题:**做资金费 carry,哪些币真的付得够多、而且付得稳?**

    python3 funding_probe.py floor                 # 手续费地板是多少
    python3 funding_probe.py rates --limit 100     # 短窗口快照(会骗人,见 --help)
    python3 funding_probe.py history BTC_USDC_PERP # 单个币的长窗口稳定性
    python3 funding_probe.py screen                # ★ 主命令:长窗口筛选

    自己根据交易所筛选：
    python3 funding_probe.py pair-screen --venues Binance,Bybit
    挑一个看实时状态：
    python3 carry_watch.py --coin SOXL --interval 15 --rounds 5
    确认合约规格和最小下单量：
    python3 carry_watch.py --coin SOXL --info

⚠️ 为什么有 `rates` 又有 `screen`:
   `rates` 用短窗口,快但**会骗人** —— 实测 4 天窗口把 BILL 年化算成 +126%,
   长窗口真值是 +13.57%(高估 9.3 倍);而且 5 个"符号 100% 稳定"的候选里,
   **3 个在长窗口下符号是反的**。
   `rates` 保留下来是为了让你能亲手复现这个陷阱,不是给你做决策用的。
"""

import argparse
import gzip
import http.client
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from datetime import datetime

API = "https://api.backpack.exchange/api/v1"

# Backpack Tier 1(默认档)费率,单位 bps。来源:官方 VIP Program 文档。
# ⚠️ **这一组只对 Backpack 有效**,是「照抄公示」不是实测。
#    币安/Bybit 的费率见 lib/fees.py —— 那边是从真实账单反推的,
#    而且**代币化股票和加密不一样**,不能混用。
FEES = {
    "spot_maker": 8, "spot_taker": 10,
    "perp_maker": 2, "perp_taker": 5,
}

sys.path.insert(0, str(Path(__file__).parent))
from lib import fees as FEEMOD  # noqa: E402


# 同一次运行里重复拉的大接口(Bybit tickers 单次 600KB,live 和 universe 都要)
_CACHE = {}

# 连接被对端 RST(Errno 104)几乎只发生在这些 500KB+ 的全市场接口上。
# 三个应对:开 gzip 把包压到 ~1/8、每次重试都换新连接、退避拉长并加抖动。
_RETRIABLE = (OSError, http.client.HTTPException, json.JSONDecodeError)


def get(url, n=5, cache=False):
    if cache and url in _CACHE:
        return _CACHE[url]
    last = None
    for i in range(n):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "research/1.0",
                "Accept-Encoding": "gzip",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            d = json.loads(raw)
            if cache:
                _CACHE[url] = d
            return d
        except urllib.error.HTTPError as e:
            # 4xx 是「这个币在这个所没有」之类的确定性错误,重试没意义;
            # 只有 429/418(限频)和 5xx 值得等一会儿再来。
            last = e
            if e.code not in (429, 418) and e.code < 500:
                break
        except _RETRIABLE as e:
            last = e
        if i == n - 1:
            break
        time.sleep(min(8.0, 2 ** i) * (0.7 + random.random() * 0.6))
    raise RuntimeError(f"{url.split('?')[0]} 失败: {type(last).__name__}: {last}") from last


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
    print("=== Backpack Tier 1 费率(官方文档,bps,**未实测**)===")
    print("    ⚠️ 只适用于 Backpack 单所现货+永续。币安/Bybit 跨所请看 lib/fees.py")
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


BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers?category=linear"


def venue_universe(name):
    """某个所的 USDT 永续清单 + 24h 成交额。"""
    if name == "Binance":
        d = get("https://fapi.binance.com/fapi/v1/ticker/24hr")
        return {x["symbol"]: float(x["quoteVolume"]) for x in d
                if x["symbol"].endswith("USDT")}
    if name == "Bybit":
        d = get(BYBIT_TICKERS, cache=True)
        return {x["symbol"]: float(x.get("turnover24h") or 0)
                for x in d["result"]["list"] if x["symbol"].endswith("USDT")}
    if name == "Bitget":
        d = get("https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES")
        return {x["symbol"]: float(x.get("usdtVolume") or 0) for x in d.get("data", [])}
    if name == "OKX":
        d = get("https://www.okx.com/api/v5/market/tickers?instType=SWAP")
        return {x["instId"].replace("-USDT-SWAP", "USDT"): float(x.get("volCcy24h") or 0)
                for x in d.get("data", []) if x["instId"].endswith("-USDT-SWAP")}
    raise ValueError(name)


def cmd_pairscreen(args):
    """
    按【你实际能交易的两个所】筛长窗口稳定的跨所 carry。

    ⚠️ 和 `screen` 的区别:
       `screen` 扫的是 **Backpack** 的绝对费率(单所口径),
       筛出来的 `XXX_USDC_PERP` 在币安/Bybit 上根本不存在。
       这条命令扫的是**两个所都有的币**,算的是跨所价差。
    """
    a, b = [v.strip() for v in args.venues.split(",")]
    print(f"拉 {a} / {b} 的合约清单 …")
    # ★ 同时抓当前实时费率 —— 「历史稳定」和「现在有没有机会」必须并排看
    # 每个接口单独 try —— 之前四个挤在一个 try 里,任何一个抖一下整列「当前净」就没了。
    live = {}
    BNP = BYT = {}
    if {a, b} != {"Binance", "Bybit"}:
        # 实时段只实现了这两个所的口径,别拿别的所的符号方向硬套
        print(f"  ⚠️ 实时费率暂只支持 Binance/Bybit,{a}/{b} 只出历史")
    else:
        try:
            BNP = {x["symbol"]: x
                   for x in get("https://fapi.binance.com/fapi/v1/premiumIndex")}
        except Exception as e:
            print(f"  ⚠️ 币安实时费率拉取失败:{e}")
        try:
            BYT = {x["symbol"]: x
                   for x in get(BYBIT_TICKERS, cache=True)["result"]["list"]}
        except Exception as e:
            print(f"  ⚠️ Bybit 实时费率拉取失败:{e}")
    ivh = {}
    if BNP and BYT:
        try:
            # 这个接口只列出**非 8h** 的币,拿不到就全按 8h,误差有限,不该拖垮整块
            ivh = {x["symbol"]: float(x["fundingIntervalHours"])
                   for x in (get("https://fapi.binance.com/fapi/v1/fundingInfo") or [])}
        except Exception as e:
            print(f"  ⚠️ 币安结算间隔拉取失败:{e},一律按 8h 年化")
    for sym in set(BNP) & set(BYT):
        ba = float(BNP[sym]["lastFundingRate"]) * (8760 / ivh.get(sym, 8.0)) * 100
        ya = float(BYT[sym]["fundingRate"]) * (8760 / 8) * 100
        live[sym] = ba - ya if a == "Binance" else ya - ba
    if not live and BNP and BYT:
        print("  ⚠️ 实时费率不可用,本次只出历史 —— 「当前净」一列会全是 —")
    ua, ub = venue_universe(a), venue_universe(b)
    common = {s for s in set(ua) & set(ub)
              if min(ua[s], ub[s]) >= args.min_turnover}
    common = sorted(common, key=lambda s: -min(ua[s], ub[s]))[:args.top]
    print(f"  {a} {len(ua)} 个 / {b} {len(ub)} 个 → "
          f"**两所都有且双边成交额 ≥ ${args.min_turnover/1e6:.0f}M 的 {len(common)} 个**\n")

    def hist(venue, sym):
        v = VENUES[venue]
        coin = sym[:-4]
        try:
            raw = get(v["url"](coin))
            rows = v["rows"](raw)
        except Exception:
            return None
        if not rows or len(rows) < 30:
            return None
        return sorted(((v["ts"](x), v["rate"](x)) for x in rows), key=lambda t: t[0])

    out = []
    for i, sym in enumerate(common):
        pa, pb = hist(a, sym), hist(b, sym)
        if not pa or not pb:
            continue
        t0, t1 = max(pa[0][0], pb[0][0]), min(pa[-1][0], pb[-1][0])
        sa = [p for p in pa if t0 <= p[0] <= t1]
        sb = [p for p in pb if t0 <= p[0] <= t1]
        if len(sa) < 20 or len(sb) < 20:
            continue
        days = (t1 - t0) / 86400000
        ha, hb = len(sa) // 2, len(sb) // 2
        net = annualise(sa) - annualise(sb)
        n_new = annualise(sa[ha:]) - annualise(sb[hb:])
        n_old = annualise(sa[:ha]) - annualise(sb[:hb])
        flip = n_new * n_old < 0
        ratio = max(abs(n_new), abs(n_old)) / max(min(abs(n_new), abs(n_old)), 1e-9)
        out.append({"s": sym, "days": days, "net": net, "nn": n_new, "no": n_old,
                    "flip": flip, "ratio": ratio,
                    "turn": min(ua[sym], ub[sym])})
        if (i + 1) % 10 == 0:
            print(f"  …{i+1}/{len(common)}", flush=True)
    out.sort(key=lambda r: -abs(r["net"]))

    # ★ 回本天数:以前这张表只报净年化,不报「要多久才够付手续费」,
    #   于是 +0.86% 和 +15% 看起来只是数字大小之分。加上之后才看得出
    #   前者需要 570 天回本 —— 根本不是一个量级的东西。
    for r in out:
        r["cls"] = FEEMOD.asset_class(r["s"], auto=False)
        r["fee"], r["fee_ok"], _ = FEEMOD.roundtrip(a, b, r["cls"], maker=args.maker)
        base = r["live"] if r.get("live") is not None else r["net"]
        r["pb"] = (r["fee"] / (abs(base) / 100 / 365 * 1e4)) if base else float("inf")

    print(f"\n{'币种':<15}{'类':>7}{'天':>5}{'历史净':>9}{'倍':>6}"
          f"{'★当前净':>10}{'费bps':>7}{'回本天':>8}{'双边额':>9}  历史判定")
    print("-" * 92)
    for r in out[:args.show]:
        v = "🔴反号" if r["flip"] else ("🔴不稳" if r["ratio"] > 3 else
                                       "⚠️偏" if r["ratio"] > 1.8 else "✅稳")
        cur = live.get(r["s"])
        r["live"] = cur
        cs = f"{cur:>+10.2f}" if cur is not None else f"{'—':>10}"
        pb = f"{r['pb']:>8.1f}" if r["pb"] < 9999 else f"{'∞':>8}"
        print(f"{r['s']:<15}{r['cls']:>7}{r['days']:>5.0f}{r['net']:>+9.2f}{r['ratio']:>6.1f}"
              f"{cs}{r['fee']:>7.1f}{pb}{r['turn']/1e6:>8,.0f}M  {v}")

    ok = [r for r in out if not r["flip"] and r["ratio"] <= 1.8
          and abs(r["net"]) >= args.min_net]
    print(f"\n=== 通过(不反号 + 两半≤1.8倍 + |净|≥{args.min_net}%)===")
    if not ok:
        print("  一个都没有。")
    for r in ok:
        short = a if r["net"] > 0 else b
        long_ = b if r["net"] > 0 else a
        print(f"  {r['s']:<15} 净 {r['net']:+7.2f}%   做空 {short} / 做多 {long_}   "
              f"{r['days']:.0f} 天样本   双边额 ${r['turn']/1e6:,.0f}M")
        print(f"  {'':<15} {r['cls']}  往返 {r['fee']:.1f} bps  "
              f"→ 按当前费率回本 {r['pb']:.1f} 天"
              + ("" if r['pb'] < 30 else "   🔴 回本期比样本可信度还长"))
    print(f"\n  {len(ok)}/{len(out)} 通过")

    # ★★ 筛选目标 vs 使用目标 的自检
    if live:
        import statistics as _st
        pv = [abs(live[r["s"]]) for r in ok if r["s"] in live]
        fv = [abs(live[r["s"]]) for r in out if r not in ok and r["s"] in live]
        if pv and fv:
            mp, mf = _st.median(pv), _st.median(fv)
            print(f"\n{'='*72}")
            print(f"=== ⚠️ 筛选器自检:稳定性筛选是不是把机会筛掉了 ===")
            print(f"  通过的 {len(pv)} 个   当前 |净| 中位 {mp:>7.2f}%   最大 {max(pv):>7.2f}%")
            print(f"  淘汰的 {len(fv)} 个   当前 |净| 中位 {mf:>7.2f}%   最大 {max(fv):>7.2f}%")
            if mf > mp * 2:
                print(f"\n  🔴 **淘汰组的当前机会是通过组的 {mf/max(mp,0.01):.0f} 倍**")
                print(f"     说明「两半比值 ≤ {1.8}」这个判据正在**系统性筛掉机会**:")
                print(f"       两半比值小 = 这个价差一直没怎么变")
                print(f"       一直没变的价差 = 一直是那个小数,现在也小")
                print(f"     **而机会恰恰来自「变了」。**")
                print(f"\n     → 这个筛选适合回答「历史上哪个方向稳」,")
                print(f"       **不适合回答「现在该做哪个」**。两者负相关。")
            else:
                print(f"\n  ✅ 两组当前机会量级接近,筛选没有明显反向选择。")
    print(f"\n⚠️ 通过 ≠ 可以下单。这只说明【历史上】这个方向稳定,")
    print(f"   当前瞬时值可能完全不同 —— 看上面「当前净」那一列。")
    if args.out:
        json.dump(out, open(args.out, "w"), ensure_ascii=False, indent=1)
        print(f"\n已存 {args.out}")
    return 0


def cmd_compare(args):
    """
    比对多天的 screen 快照 —— 第 3 步「名单稳不稳」用这个。

    判据不是"今天筛出几个",而是**"同一批币能不能连续多天留在名单里"**。
    一个每天换一半的名单,说明判据太松,不能拿去下单。
    """
    import glob
    import os
    files = sorted(glob.glob(args.pattern))
    if len(files) < 2:
        print(f"至少要 2 个快照才能比。当前匹配到 {len(files)} 个:{files}")
        return 1
    snaps = []
    for f in files:
        rows = json.load(open(f))
        ok = {r["sym"] for r in rows
              if not r["flip"] and r["pos"] >= args.min_consistency
              and r["payback_days"] <= args.max_payback}
        snaps.append((os.path.basename(f), ok))
    print(f"=== {len(snaps)} 个快照 ===")
    for name, ok in snaps:
        print(f"  {name:<32} {len(ok):>3} 个通过")

    allsym = set().union(*[s for _, s in snaps])
    n = len(snaps)
    print(f"\n=== 每个币出现在几个快照里(共 {n} 个)===")
    counts = {sym: sum(1 for _, ok in snaps if sym in ok) for sym in allsym}
    for sym, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        bar = "█" * c + "·" * (n - c)
        tag = "  ★ 全程在榜" if c == n else ("  ⚠️ 只出现一次" if c == 1 else "")
        print(f"  {sym:<22} {bar}  {c}/{n}{tag}")

    always = [s for s, c in counts.items() if c == n]
    once = [s for s, c in counts.items() if c == 1]
    print(f"\n=== 判定 ===")
    print(f"  全程在榜  {len(always)}/{len(allsym)}  {always}")
    print(f"  只来一次  {len(once)}/{len(allsym)}")
    churn = 1 - len(always) / max(len(allsym), 1)
    print(f"  换手率    {churn*100:.0f}%")
    if churn > 0.5:
        print(f"\n  🔴 换手超过一半 —— **判据太松,不要拿这个名单下单**。")
        print(f"     回去把 --min-consistency 提高(比如 85),或延长观察期。")
    elif always:
        print(f"\n  ✅ 有 {len(always)} 个币全程在榜,可以进第 4 步(小额测单腿)。")
    return 0


# ══════════════════════════════════════════════════════════════════
#   跨所资金费差 —— taoli.tools 排名页上真正有意思的结构
# ══════════════════════════════════════════════════════════════════
#
# 排名页的 `1Y` 列是「下次资金费 × 一年次数」的**单点外推**,
# 实测:COTI 下次 +0.6898%(8H 间隔)→ 0.6898 × 1095 = +755.3%,完全对上。
# 它回答"接下来这一次付多少",不回答"通常付多少"。
#
# 但同一个币在不同所的费率差是真实结构:高的做空、低的做多,
# 两条腿都是永续(手续费比现货+永续便宜一半),天然 delta 中性。
# 这个命令就是查:**那个价差是常态,还是今天碰巧。**

VENUES = {
    "Binance": {
        "url": lambda c: f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={c}USDT&limit=1000",
        "rows": lambda d: d if isinstance(d, list) else [],
        "rate": lambda x: float(x["fundingRate"]),
        "ts":   lambda x: int(x["fundingTime"]),
    },
    "Bybit": {
        "url": lambda c: f"https://api.bybit.com/v5/market/funding/history?category=linear&symbol={c}USDT&limit=200",
        "rows": lambda d: d.get("result", {}).get("list", []),
        "rate": lambda x: float(x["fundingRate"]),
        "ts":   lambda x: int(x["fundingRateTimestamp"]),
    },
    "Bitget": {
        "url": lambda c: f"https://api.bitget.com/api/v2/mix/market/history-fund-rate?symbol={c}USDT&productType=USDT-FUTURES&pageSize=100",
        "rows": lambda d: d.get("data", []) or [],
        "rate": lambda x: float(x["fundingRate"]),
        "ts":   lambda x: int(x["fundingTime"]),
    },
    "KuCoin": {
        "url": lambda c: f"https://api-futures.kucoin.com/api/v1/contract/funding-rates?symbol={c}USDTM&from={int(time.time()*1000)-90*86400000}&to={int(time.time()*1000)}",
        "rows": lambda d: d.get("data", []) or [],
        "rate": lambda x: float(x["fundingRate"]),
        "ts":   lambda x: int(x["timepoint"]),
    },
    "OKX": {
        "url": lambda c: f"https://www.okx.com/api/v5/public/funding-rate-history?instId={c}-USDT-SWAP&limit=100",
        "rows": lambda d: d.get("data", []) or [],
        "rate": lambda x: float(x.get("realizedRate") or x.get("fundingRate")),
        "ts":   lambda x: int(x["fundingTime"]),
    },
}


def venue_stats(name, coin):
    v = VENUES[name]
    try:
        raw = get(v["url"](coin))
    except Exception as e:
        return {"venue": name, "err": str(e)[:60]}
    try:
        rows = v["rows"](raw)
    except Exception:
        return {"venue": name, "err": "解析失败"}
    if not rows or len(rows) < 10:
        return {"venue": name, "err": f"只有 {len(rows) if rows else 0} 条"}
    pts = sorted(((v["ts"](x), v["rate"](x)) for x in rows), key=lambda t: t[0])
    return {"venue": name, "pts": pts}


def annualise(pts):
    """
    年化,**不依赖结算间隔**。

    ⚠️ 早期版本用「均值 × (8760/中位间隔)」,在**合约中途改过间隔**时会算错。
       实测 SKHYNIX 在币安从 8H 改成了 4H:0/8/16 点各 71 期,
       4/12/20 点只有 29 期 —— 中位间隔算出 4h,于是早期那批 8H 的点
       被整整放大了一倍。

    正确做法:**把窗口内收到的费率直接加总,除以窗口天数,再乘 365。**
    这个算法对间隔变化、缺失结算、不规则结算全都免疫。
    """
    if len(pts) < 2:
        return 0.0
    days = (pts[-1][0] - pts[0][0]) / 86400000
    if days <= 0:
        return 0.0
    return sum(r for _, r in pts) / days * 365 * 100


def summarise(name, pts):
    """把一段 (时间戳, 费率) 点列算成年化等指标。"""
    ts = [t for t, _ in pts]
    fr = [r for _, r in pts]
    gaps = [(ts[i + 1] - ts[i]) / 3.6e6 for i in range(len(ts) - 1)]
    iv = statistics.median(gaps) if gaps else 8.0
    half = len(pts) // 2
    return {
        "venue": name, "n": len(fr), "days": (ts[-1] - ts[0]) / 86400000, "iv": iv,
        "ann": annualise(pts),
        "pos": sum(1 for x in fr if x > 0) / len(fr) * 100,
        "a_recent": annualise(pts[half:]),
        "a_old": annualise(pts[:half]),
    }


def cmd_cross(args):
    coin = args.coin.upper()
    print(f"=== {coin} 跨所资金费(各所拉满可得历史)===\n")
    raw = []
    for name in VENUES:
        r = venue_stats(name, coin)
        if "err" in r:
            print(f"  {name:<9} —— {r['err']}")
            continue
        raw.append(r)
    if len(raw) < 2:
        print("\n  可比较的所少于 2 个,无法算跨所价差。")
        return 1

    print("  各所原始可得历史:")
    for r in raw:
        d = summarise(r["venue"], r["pts"])
        print(f"    {d['venue']:<9} {d['n']:>4} 期 / {d['days']:>5.0f} 天 / "
              f"{d['iv']:>4.1f}h  年化 {d['ann']:>+8.2f}%")

    # ★ 对齐到公共时间窗 —— 否则拿 83 天均值减 16 天均值,
    #   算出来的不是价差,是两段不同时期的差
    t0 = max(r["pts"][0][0] for r in raw)
    t1 = min(r["pts"][-1][0] for r in raw)
    win_days = (t1 - t0) / 86400000
    print(f"\n  ★ 对齐到公共窗口:{win_days:.0f} 天(取各所都覆盖到的那一段)")
    if win_days < 7:
        print(f"  🔴 公共窗口只有 {win_days:.0f} 天 —— 太短,不足以判断。")

    rows = []
    for r in raw:
        seg = [p for p in r["pts"] if t0 <= p[0] <= t1]
        if len(seg) < 10:
            print(f"    {r['venue']:<9} 窗口内只有 {len(seg)} 期,剔除")
            continue
        rows.append(summarise(r["venue"], seg))
    print()
    for r in rows:
        print(f"    {r['venue']:<9} {r['n']:>4} 期  年化 {r['ann']:>+8.2f}%  "
              f"同号 {r['pos']:>3.0f}%  近半 {r['a_recent']:>+7.1f}  远半 {r['a_old']:>+7.1f}")
    if len(rows) < 2:
        print("\n  公共窗口内可比较的所少于 2 个。")
        return 1

    hi = max(rows, key=lambda r: r["ann"])
    lo = min(rows, key=lambda r: r["ann"])
    spread = hi["ann"] - lo["ann"]
    print(f"\n{'='*70}")
    print(f"=== 跨所价差 ===")
    print(f"  做空 {hi['venue']}(收 {hi['ann']:+.2f}%)")
    print(f"  做多 {lo['venue']}(付 {lo['ann']:+.2f}%)")
    print(f"  **净年化 {spread:+.2f}%**")

    # 稳定性:用近半 / 远半分别算价差,方向要一致
    s_recent = hi["a_recent"] - lo["a_recent"]
    s_old = hi["a_old"] - lo["a_old"]
    print(f"\n  近半价差 {s_recent:+.2f}%   远半价差 {s_old:+.2f}%")
    if s_recent * s_old <= 0:
        print(f"  🔴 **两半价差反号 —— 方向不稳定,不要做**")
    else:
        ratio = max(abs(s_recent), abs(s_old)) / max(min(abs(s_recent), abs(s_old)), 1e-9)
        if ratio > 3:
            print(f"  🔴 两半相差 {ratio:.1f} 倍 —— 价差集中在其中一段,**不是常态**")
        elif ratio > 1.8:
            print(f"  ⚠️ 两半相差 {ratio:.1f} 倍 —— 偏不稳,再观察几天")
        else:
            print(f"  ✅ 两半方向一致,量级接近({ratio:.1f} 倍)")

    # 成本:两条腿都是永续。**不能用 Backpack 的费率**(以前就是这么错的)——
    # 币安/Bybit 各自不同,而且代币化股票和加密还要再分。
    acls = FEEMOD.asset_class(args.coin, auto=False)
    perp_rt_t, ok_t, det_t = FEEMOD.roundtrip("Binance", "Bybit", acls, maker=False)
    perp_rt,   ok_m, det_m = FEEMOD.roundtrip("Binance", "Bybit", acls, maker=True)
    print(f"\n  两条腿都是永续,往返 4 笔手续费({args.coin} → {acls}):")
    print(f"    吃单 {det_t}   {FEEMOD.fee_note(ok_t)}")
    print(f"    挂单 {det_m}   {FEEMOD.fee_note(ok_m)}")
    if spread != 0:
        for tag, rt in (("吃单", perp_rt_t), ("挂单", perp_rt)):
            d = rt / (abs(spread) / 100 / 365 * 1e4)
            print(f"    按净年化 {spread:+.2f}% 算,{tag}回本 {d:.1f} 天")
    print(f"\n  ⚠️ 各所样本长度不同(见上表 `天`),短的那个所主导了不确定性。")
    print(f"     另外这只算了资金费,**没算两个所各自的爆仓/ADL 风险**。")
    return 0


def volume_profile(coin, days=40):
    """
    从 1 小时 K 线统计「成交量按 UTC 小时的分布」。

    为什么要这个:SKHYNIX 这类代币化股票,标的有交易时段,
    但**我不知道它跟的是哪个市场的时段**(韩国交易所?某个 ADR?)。

    **猜错市场时间会让整个分析作废,所以不猜** ——
    改为让数据自己说:标的开市时,永续的成交量一定放大。
    **成交量的形状,就是开市时间。**
    """
    url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={coin}USDT"
           f"&interval=1h&limit={min(days*24, 1000)}")
    try:
        kl = get(url)
    except Exception as e:
        return None, str(e)[:60]
    if not isinstance(kl, list) or not kl:
        return None, "没有 K 线数据"
    from datetime import datetime, timezone
    byhour = {}
    for k in kl:
        # [开盘时间, 开, 高, 低, 收, 成交量, 收盘时间, 成交额, ...]
        h = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).hour
        rng = (float(k[2]) - float(k[3])) / float(k[4]) * 100 if float(k[4]) else 0
        byhour.setdefault(h, {"vol": [], "rng": []})
        byhour[h]["vol"].append(float(k[7]))     # 成交额(USDT)
        byhour[h]["rng"].append(rng)             # 该小时振幅 %
    return byhour, None


def cmd_session(args):
    """用成交量剖面找出标的的交易时段,再和资金费剖面叠起来看。"""
    coin = args.coin.upper()
    print(f"=== {coin} 交易时段探测(不靠猜,用成交量形状)===\n")
    byhour, err = volume_profile(coin, args.days)
    if err:
        print(f"  拉 K 线失败:{err}"); return 1

    vols = {h: statistics.mean(v["vol"]) for h, v in byhour.items()}
    rngs = {h: statistics.mean(v["rng"]) for h, v in byhour.items()}
    vmax = max(vols.values()) or 1
    med = statistics.median(vols.values())

    # 资金费剖面(同一个所)
    fr_slot = {}
    r = venue_stats("Binance", coin)
    if "err" not in r:
        from datetime import datetime, timezone
        tmp = {}
        for t, rate in r["pts"]:
            tmp.setdefault(datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour,
                           []).append(rate)
        fr_slot = {h: statistics.mean(v) * 365 * 100 for h, v in tmp.items()}

    print(f"  {'UTC':>4}{'平均成交额':>14}{'振幅%':>8}{'资金费年化%':>12}   成交量分布")
    print("  " + "-" * 74)
    for h in range(24):
        if h not in vols:
            continue
        v = vols[h]
        bar = "█" * max(1, int(v / vmax * 30))
        hot = " ← 开市" if v > med * 1.8 else ""
        f = f"{fr_slot[h]:>+12.2f}" if h in fr_slot else f"{'—':>12}"
        print(f"  {h:>2}:00{v:>14,.0f}{rngs[h]:>8.2f}{f}   {bar}{hot}")

    busy = sorted([h for h, v in vols.items() if v > med * 1.8])
    print(f"\n  === 判读 ===")
    if busy:
        print(f"  成交量显著放大的时段(UTC):{busy}")
        print(f"  → 这几个小时就是**标的开市**的时段(北京时间 +8)")
        print(f"  → 对应北京时间:{[(h+8) % 24 for h in busy]}")
    else:
        print(f"  没有明显的时段差异 —— 可能不是股票类标的,或样本不够")
    if fr_slot:
        best = max(fr_slot, key=lambda h: fr_slot[h])
        print(f"\n  资金费最高的结算点:UTC {best}:00  ({fr_slot[best]:+.2f}%)")
        print(f"  该时点{'在' if best in busy else '**不在**'}开市时段内")
        if best not in busy:
            print(f"  ⚠️ 高资金费出现在**休市后**,而不是开市中 ——")
            print(f"     说明它更可能是「标的不动、永续乱跑」造成的,")
            print(f"     而不是真实的多空失衡。**这种费率的持续性要打折看。**")
    print(f"\n  ⚠️ 成交额单位是 USDT,来自币安 1 小时 K 线,{args.days} 天样本。")
    return 0


def cmd_hours(args):
    """
    按 UTC 小时 / 工作日-周末 拆解资金费。

    动机:SKHYNIX 这类**代币化股票**永续,标的有交易时段。
    币安页面直接挂了警告:「基础资产在其主要市场的非交易时段内,
    资金费率的波动可能会较大」;Bybit 更写明周末可能只允许减仓。

    所以要问:**这个 carry 的收益,是不是主要来自标的休市的时段?**
    如果是,那它的性质完全不同 —— 休市时段恰恰流动性最差、限制最多。

    **不写死市场时间**,只把数据按小时摊开,让形状自己说话。
    """
    from datetime import datetime, timezone
    coin = args.coin.upper()
    names = [args.venue] if args.venue else list(VENUES)
    for name in names:
        if name not in VENUES:
            print(f"未知交易所 {name},可选:{list(VENUES)}"); continue
        r = venue_stats(name, coin)
        if "err" in r:
            print(f"\n=== {name} ===\n  拉不到数据:{r['err']}")
            continue
        pts = r["pts"]
        ts = [t for t, _ in pts]
        gaps = [(ts[i + 1] - ts[i]) / 3.6e6 for i in range(len(ts) - 1)]
        iv = statistics.median(gaps) if gaps else 8.0
        if iv <= 0:
            iv = 8.0
        ann = 8760 / iv
        print(f"\n{'='*66}")
        print(f"=== {name}  {coin}  ({len(pts)} 期 / {iv:.0f}h 间隔 / "
              f"{(ts[-1]-ts[0])/86400000:.0f} 天)===")

        byhour, byday = {}, {"工作日": [], "周末": []}
        for t, rate in pts:
            d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
            byhour.setdefault(d.hour, []).append(rate)
            byday["周末" if d.weekday() >= 5 else "工作日"].append((t, rate))

        # 每个 UTC 小时槽每天最多触发一次 → 该槽单独年化 = 均值 × 365
        print(f"\n  按 UTC 小时(该槽单独年化,假设每日发生一次):")
        print(f"  {'UTC时':>6}{'期数':>6}{'年化%':>10}   分布")
        slot = {h: statistics.mean(vs) * 365 * 100 for h, vs in byhour.items()}
        hi = max(abs(x) for x in slot.values()) or 1
        for h in sorted(byhour):
            a = slot[h]
            bar = "█" * max(1, int(abs(a) / hi * 34))
            print(f"  {h:>4}:00{len(byhour[h]):>6}{a:>+10.2f}   {bar}")

        wd = byday["工作日"]; we = byday["周末"]
        if len(wd) > 1 and len(we) > 1:
            # 工作日/周末各自的点不连续,用「总和 ÷ 实际覆盖天数」
            awd = sum(r for _, r in wd) / (len({datetime.fromtimestamp(t/1000, tz=timezone.utc).date() for t, _ in wd}) or 1) * 365 * 100
            awe = sum(r for _, r in we) / (len({datetime.fromtimestamp(t/1000, tz=timezone.utc).date() for t, _ in we}) or 1) * 365 * 100
            print(f"\n  工作日  {len(wd):>4} 期   年化 {awd:>+8.2f}%")
            print(f"  周末    {len(we):>4} 期   年化 {awe:>+8.2f}%")
            if awd != 0:
                ratio = awe / awd
                print(f"  周末 / 工作日 = {ratio:.2f} 倍", end="")
                if ratio > 1.5:
                    print("   🔴 **收益主要来自周末** —— 而周末可能只允许减仓")
                elif ratio < 0.67:
                    print("   ✅ 收益主要来自工作日(标的开市),这是好事")
                else:
                    print("   ✅ 两者接近,没有明显的时段依赖")
    print(f"\n  ⚠️ 间隔 {iv:.0f}h 意味着一天只有 {24/iv:.0f} 个采样点,")
    print(f"     小时分辨率有限 —— 看趋势,别抠单个小时的数。")
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

    c = sub.add_parser("compare", help="比对多天快照,看名单稳不稳(第 3 步用)")
    c.add_argument("pattern", nargs="?", default="shadow/funding_2*.json")
    c.add_argument("--min-consistency", type=float, default=70)
    c.add_argument("--max-payback", type=float, default=30)
    c.set_defaults(func=cmd_compare)

    ps = sub.add_parser("pair-screen",
                        help="★ 按【你能交易的两个所】筛长窗口稳定的跨所 carry")
    ps.add_argument("--venues", default="Binance,Bybit",
                    help="两个交易所,逗号分隔(默认 Binance,Bybit)")
    ps.add_argument("--top", type=int, default=30, help="按流动性取前 N 个分析")
    ps.add_argument("--show", type=int, default=25)
    ps.add_argument("--min-turnover", type=float, default=30e6,
                    help="**双边**都要达到的 24h 成交额")
    ps.add_argument("--min-net", type=float, default=3.0, help="通过所需的最小净年化 %%")
    ps.add_argument("--maker", action="store_true",
                    help="按挂单费率算成本(默认吃单)。⚠️ maker 那一列大多是"
                         "公示费率没实测,只有 Bybit 代币化股票的 0 bps 是真的")
    ps.add_argument("--out", default="shadow/pair_screen.json")
    ps.set_defaults(func=cmd_pairscreen)

    x = sub.add_parser("cross", help="★ 跨所资金费差(排名页上真正有意思的结构)")
    x.add_argument("coin", help="币种,如 ARC / SKHYNIX / LINK")
    x.set_defaults(func=cmd_cross)

    hr = sub.add_parser("hours", help="★ 按小时/周末拆解(代币化股票必看)")
    hr.add_argument("coin")
    hr.add_argument("--venue", help="只看某个所,默认全部")
    hr.set_defaults(func=cmd_hours)

    se = sub.add_parser("session", help="★ 用成交量形状探测标的交易时段")
    se.add_argument("coin")
    se.add_argument("--days", type=int, default=40)
    se.set_defaults(func=cmd_session)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
