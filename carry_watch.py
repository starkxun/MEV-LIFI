#!/usr/bin/env python3
"""
carry_watch.py —— 跨所资金费 carry 的实时记录器

⚠️ **只读公开行情,不需要 API key,代码里没有任何下单能力。**
   用的全是各所的公开行情端点 —— 结构上就不可能动你的钱。

它做三件事:

  1. 每隔 N 秒同时抓两个所的盘口 + 资金费,落一条 JSONL
  2. 算出**两个开仓方向**各自的:资金费净年化、开仓价差成本、回本天数
  3. 划算的时候在终端标出来

为什么要它:
  你手工在 taoli.tools 下单时,屏幕上的数字**转瞬即逝**。
  事后想算「我实际成交的价差 vs 当时屏幕上的价差」,没有记录就算不了。
  而那个差,加上单腿发生率,是我们成本模型里**最后两个「待定」项**。

    python3 carry_watch.py --coin SKHYNIX
    python3 carry_watch.py --coin SKHYNIX --interval 10 --alert-days 2
    python3 carry_watch.py --coin SKHYNIX --info      # 只看合约规格(最小下单量等)
"""

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

# 手续费(bps)。VIP0 默认档,**你升级了要自己改这里**。
# 一条腿开+平 = 2 笔,两条腿 = 4 笔。
FEES = {
    "Binance": {"maker": 2.0, "taker": 5.0},
    "Bybit":   {"maker": 2.0, "taker": 5.5},
}


def get(url, n=3, timeout=15):
    last = None
    for i in range(n):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except Exception as e:
            last = e
            if i < n - 1:
                time.sleep(1.0 * (i + 1))
    raise last


# ── 各所的行情适配器 ───────────────────────────────────────────
def snap_binance(coin):
    s = f"{coin}USDT"
    bt = get(f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={s}")
    pi = get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={s}")
    return {
        "venue": "Binance",
        "bid": float(bt["bidPrice"]), "ask": float(bt["askPrice"]),
        "bid_qty": float(bt["bidQty"]), "ask_qty": float(bt["askQty"]),
        "mark": float(pi["markPrice"]), "index": float(pi["indexPrice"]),
        "rate": float(pi["lastFundingRate"]),
        "next_ms": int(pi["nextFundingTime"]),
    }


def snap_bybit(coin):
    d = get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={coin}USDT")
    t = d["result"]["list"][0]
    return {
        "venue": "Bybit",
        "bid": float(t["bid1Price"]), "ask": float(t["ask1Price"]),
        "bid_qty": float(t.get("bid1Size") or 0), "ask_qty": float(t.get("ask1Size") or 0),
        "mark": float(t["markPrice"]), "index": float(t["indexPrice"]),
        "rate": float(t["fundingRate"]),
        "next_ms": int(t["nextFundingTime"]),
    }


SNAP = {"Binance": snap_binance, "Bybit": snap_bybit}


# ── 结算间隔 ──────────────────────────────────────────────────
def interval_binance(coin):
    """
    ⚠️ Binance 的 fundingInfo **只列出非 8 小时的合约**。
       查不到 ≠ 出错,而是「就是默认的 8 小时」。
       这个默认值不写清楚,年化会直接算错一倍。
    """
    try:
        for x in get("https://fapi.binance.com/fapi/v1/fundingInfo"):
            if x["symbol"] == f"{coin}USDT":
                return float(x["fundingIntervalHours"])
    except Exception:
        pass
    return 8.0


def interval_bybit(coin):
    try:
        d = get(f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={coin}USDT")
        return float(d["result"]["list"][0]["fundingInterval"]) / 60.0
    except Exception:
        return 8.0


def contract_info(coin):
    """最小下单量 / 步长 / 最大杠杆 —— 决定「最少要充多少钱」。"""
    out = {}
    try:
        d = get(f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={coin}USDT")
        t = d["result"]["list"][0]
        out["Bybit"] = {
            "min_qty": float(t["lotSizeFilter"]["minOrderQty"]),
            "qty_step": float(t["lotSizeFilter"]["qtyStep"]),
            "max_lev": float(t["leverageFilter"]["maxLeverage"]),
            "tick": float(t["priceFilter"]["tickSize"]),
        }
    except Exception as e:
        out["Bybit"] = {"err": str(e)[:50]}
    try:
        d = get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        for sym in d["symbols"]:
            if sym["symbol"] != f"{coin}USDT":
                continue
            f = {x["filterType"]: x for x in sym["filters"]}
            out["Binance"] = {
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "qty_step": float(f["LOT_SIZE"]["stepSize"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0)),
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
            }
            break
    except Exception as e:
        out["Binance"] = {"err": str(e)[:50]}
    return out


# ── 基差的历史分布 ────────────────────────────────────────────
def basis_history(coin, hours=1000):
    """
    用两所的 1 小时 K 线算历史基差分布。

    **为什么必须有这个**:开仓成本不是「此刻价差多少」,而是
    「此刻价差相对它自己的常态偏到哪」。

    你做空 A、做多 B,就是**做空基差 (A-B)**:
        基差此刻偏【低】 → 你在低卖高买,而且回归时继续亏  ← 坏时机
        基差此刻偏【高】 → 你在高卖低买,回归时还再赚一笔  ← 好时机

    同样的资金费,进场时机不同能差十几个 bps。
    """
    try:
        bn = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={coin}USDT"
                 f"&interval=1h&limit={min(hours,1000)}")
        by = get(f"https://api.bybit.com/v5/market/kline?category=linear"
                 f"&symbol={coin}USDT&interval=60&limit={min(hours,1000)}")
    except Exception as e:
        return None, str(e)[:60]
    BN = {int(k[0]): float(k[4]) for k in bn}
    BY = {int(k[0]): float(k[4]) for k in by["result"]["list"]}
    common = sorted(set(BN) & set(BY))
    if len(common) < 100:
        return None, f"共同小时只有 {len(common)}"
    rows = []
    for t in common:
        rows.append((t, (BN[t] - BY[t]) / BY[t] * 1e4))
    return rows, None


def basis_stats(rows, open_hours):
    """按开市/休市分别给出基差分布。"""
    op = [v for t, v in rows
          if datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour in open_hours]
    cl = [v for t, v in rows
          if datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour not in open_hours]
    def st(xs):
        if len(xs) < 10:
            return None
        return {"n": len(xs), "mean": statistics.mean(xs),
                "sd": statistics.pstdev(xs), "sorted": sorted(xs)}
    return {"开市": st(op), "休市": st(cl), "全部": st([v for _, v in rows])}


def basis_verdict(cur, st):
    """当前基差在历史分布里的位置 → 是不是好的进场时机。"""
    if not st:
        return None
    z = (cur - st["mean"]) / st["sd"] if st["sd"] else 0
    xs = st["sorted"]
    pct = sum(1 for x in xs if x < cur) / len(xs) * 100
    return {"z": z, "pct": pct, "mean": st["mean"], "sd": st["sd"], "n": st["n"]}


# ── 核心计算 ──────────────────────────────────────────────────
def evaluate(a, b, iv_a, iv_b, maker=True):
    """
    a、b 两个所的快照 → 两个开仓方向各自的经济性。

    做空一条腿:资金费为正时**收钱**
    做多一条腿:资金费为正时**付钱**
    """
    ann_a = a["rate"] * (8760 / iv_a) * 100
    ann_b = b["rate"] * (8760 / iv_b) * 100
    kind = "maker" if maker else "taker"
    # 一条腿开+平 2 笔,两条腿共 4 笔
    fee = (FEES[a["venue"]][kind] + FEES[b["venue"]][kind]) * 2

    out = []
    for short, long_ in ((a, b), (b, a)):
        # 开仓:在 short 腿以买一卖出、在 long 腿以卖一买入
        mid = (short["bid"] + long_["ask"]) / 2
        entry_bps = (short["bid"] - long_["ask"]) / mid * 1e4
        net_ann = ((ann_a if short is a else ann_b)
                   - (ann_b if long_ is b else ann_a))
        # 出场时价差大致对称,按同样幅度再算一次
        cost = fee - entry_bps * 2          # 负的 entry_bps 是成本
        # cost < 0 = 开仓拿到的价差已经盖过手续费,进场当场就赚
        # **这种情况没有「回本天数」可言** —— 早期版本让它算成负数,
        # 于是「负数 ≤ 阈值」意外通过判据,把净年化 +0.2% 的行标成了好时机。
        if cost <= 0:
            payback = 0.0
        elif net_ann > 0:
            payback = cost / (net_ann / 365 * 100)
        else:
            payback = float("inf")
        out.append({
            "short": short["venue"], "long": long_["venue"],
            "net_ann": net_ann, "entry_bps": entry_bps,
            "fee_bps": fee, "total_cost_bps": cost, "payback_days": payback,
        })
    return {"ann": {a["venue"]: ann_a, b["venue"]: ann_b}, "dirs": out}


def main():
    p = argparse.ArgumentParser(description="跨所 carry 实时记录器(只读,无下单能力)")
    p.add_argument("--coin", default="SKHYNIX")
    p.add_argument("--venues", default="Binance,Bybit")
    p.add_argument("--interval", type=int, default=20, help="采样间隔(秒)")
    p.add_argument("--alert-days", type=float, default=3.0,
                   help="回本天数低于此值才可能标 ★")
    p.add_argument("--min-carry", type=float, default=15.0,
                   help="净年化低于此值不标 ★(默认 15%%)")
    p.add_argument("--taker", action="store_true", help="按吃单费率算(默认挂单)")
    p.add_argument("--out", default=None, help="JSONL 落盘路径")
    p.add_argument("--rounds", type=int, default=0, help="跑几轮后退出,0=一直跑")
    p.add_argument("--info", action="store_true", help="只打印合约规格然后退出")
    args = p.parse_args()

    coin = args.coin.upper()
    vs = [v.strip() for v in args.venues.split(",")]
    if len(vs) != 2 or any(v not in SNAP for v in vs):
        print(f"--venues 需要两个,且必须在 {list(SNAP)} 里"); return 1

    print(f"=== {coin} 合约规格 ===")
    info = contract_info(coin)
    for v, d in info.items():
        if "err" in d:
            print(f"  {v:<9} 读不到:{d['err']}"); continue
        print(f"  {v:<9} 最小下单 {d['min_qty']:<10g} 步长 {d['qty_step']:<10g} "
              f"价格精度 {d['tick']:g}"
              + (f"  最大杠杆 {d['max_lev']:g}x" if "max_lev" in d else "")
              + (f"  最小名义 ${d['min_notional']:g}" if d.get("min_notional") else ""))

    iv = {}
    for v in vs:
        iv[v] = interval_binance(coin) if v == "Binance" else interval_bybit(coin)
    print(f"\n=== 结算间隔 ===")
    for v in vs:
        print(f"  {v:<9} {iv[v]:g} 小时  →  一年 {8760/iv[v]:.0f} 次")
    print(f"  ⚠️ Binance 的 fundingInfo 只列非 8 小时合约,查不到就是默认 8 小时。")

    if args.info:
        try:
            a, b = SNAP[vs[0]](coin), SNAP[vs[1]](coin)
            # 两条腿必须**同时**开得出来 → 约束是两所最小量里**较大**的那个
            qtys = {v: info[v]["min_qty"] for v in vs if "min_qty" in info[v]}
            mn = max(qtys.values())
            who = max(qtys, key=qtys.get)
            px = (a["mark"] + b["mark"]) / 2
            notional = mn * px
            # 还要满足各所的最小名义金额
            for v in vs:
                need = info[v].get("min_notional", 0)
                if need and notional < need:
                    mn = need / px
                    notional = need
                    who = f"{v}(最小名义 ${need:g})"
            print(f"\n=== 最小一笔要多少钱(现价 ${px:,.2f})===")
            for v, q in qtys.items():
                print(f"  {v:<9} 最小 {q:g} 个 = ${q*px:,.2f}")
            print(f"  → 两条腿要同时开,**取较大者**:{mn:g} 个 = "
                  f"名义 ${notional:,.2f} / 每条腿   (受限于 {who})")
            for lev in (2, 3, 5):
                print(f"  {lev}x 杠杆 → 每条腿保证金 ${notional/lev:,.2f},"
                      f"两条腿共 ${notional/lev*2:,.2f}")
            print(f"\n  ⚠️ 这是**开得起**的下限,不是**该充**的量。")
            print(f"     保证金只够刚好开仓 = 一点波动就爆。建议留 5 倍以上余量。")
        except Exception as e:
            print(f"  取现价失败:{str(e)[:60]}")
        return 0

    # 基差历史 —— 用来判断「此刻是不是好的进场点」
    OPEN_HOURS = {0, 1, 2, 3, 4, 5, 13, 14}      # 由 funding_probe.py session 实测得出
    brows, berr = basis_history(coin)
    bstats = basis_stats(brows, OPEN_HOURS) if brows else {}
    if berr:
        print(f"\n  ⚠️ 基差历史拉取失败:{berr},将跳过时机判断")
    else:
        print(f"\n=== 基差历史({len(brows)} 小时)===")
        for k in ("开市", "休市", "全部"):
            v = bstats.get(k)
            if v:
                print(f"  {k:<5} {v['n']:>5} 小时   均值 {v['mean']:>+7.1f} bps   "
                      f"标准差 {v['sd']:>5.1f}")
        print(f"  基差 = (Binance − Bybit) / Bybit")

    out = ROOT / (args.out or f"shadow/carry_{coin}_{datetime.now(timezone.utc):%Y%m%d}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n落盘 {out}")
    print(f"每 {args.interval}s 采样一次,按 {'吃单' if args.taker else '挂单'} 费率计算")
    print(f"回本 < {args.alert_days} 天就标 ★\n")

    n, best = 0, None
    try:
        while True:
            n += 1
            try:
                a = SNAP[vs[0]](coin)
                b = SNAP[vs[1]](coin)
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] 抓取失败 {str(e)[:60]}")
                time.sleep(args.interval)
                if args.rounds and n >= args.rounds:
                    break
                continue

            ev = evaluate(a, b, iv[vs[0]], iv[vs[1]], maker=not args.taker)

            # 当前基差 + 它在历史里的位置
            cur_basis = (a["mark"] - b["mark"]) / b["mark"] * 1e4 if a["venue"] == "Binance" \
                else (b["mark"] - a["mark"]) / a["mark"] * 1e4
            h = datetime.now(timezone.utc).hour
            seg = "开市" if h in OPEN_HOURS else "休市"
            bv = basis_verdict(cur_basis, bstats.get(seg)) if bstats else None
            rec_extra = {"basis_bps": cur_basis, "session": seg,
                         "basis_z": bv["z"] if bv else None,
                         "basis_pct": bv["pct"] if bv else None}

            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "coin": coin, "snap": {a["venue"]: a, b["venue"]: b},
                   "interval_h": iv, "eval": ev}
            rec.update(rec_extra)
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            # ★ 必须**同时**满足:carry 够大 + 回本够快。
            #   只满足一个的不标 —— 尤其是「开仓即赚但没有 carry」那种,
            #   那是价差套利的机会,不是 carry 的机会。
            good = [d for d in ev["dirs"]
                    if d["net_ann"] >= args.min_carry
                    and d["payback_days"] <= args.alert_days]
            d = max(ev["dirs"], key=lambda x: x["net_ann"])
            # 做空 Binance = 做空基差 → 基差偏【高】才是好时机
            timing = ""
            if bv:
                shorting_binance = d["short"] == "Binance"
                fav = (bv["z"] > 0.5) if shorting_binance else (bv["z"] < -0.5)
                bad = (bv["z"] < -0.5) if shorting_binance else (bv["z"] > 0.5)
                timing = ("  ✅时机好" if fav else "  🔴时机差" if bad else "  ·时机中性")
            star = "★" if good else " "
            if best is None or d["net_ann"] > best:
                best = d["net_ann"]
            eta = min((s["next_ms"] for s in (a, b)), default=0)
            mins = max(0, (eta - time.time() * 1000) / 60000)
            print(f"{star}[{datetime.now():%H:%M:%S}] "
                  f"{ev['ann'][vs[0]]:>+7.1f}% / {ev['ann'][vs[1]]:>+7.1f}%  →  "
                  f"空{d['short'][:3]} 净 {d['net_ann']:>+7.1f}%  "
                  f"开仓 {d['entry_bps']:>+5.1f}bps  "
                  f"成本 {d['total_cost_bps']:>5.1f}bps  "
                  + (f"回本  即赚  " if d["payback_days"] == 0
                     else "回本   ——   " if d["payback_days"] == float("inf")
                     else f"回本 {d['payback_days']:>5.1f}天  ")
                  + 
                  f"基差 {cur_basis:>+6.1f}bps"
                  + (f"({bv['z']:>+4.1f}σ)" if bv else "")
                  + timing, flush=True)

            if args.rounds and n >= args.rounds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止")
    print(f"\n采样 {n} 次,最好的净年化 {best:+.1f}%" if best is not None else "")
    print(f"记录在 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
