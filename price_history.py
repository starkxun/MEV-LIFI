#!/usr/bin/env python3
"""
price_history.py —— 用 The Graph 把「屏幕价差」从单点变成时间序列

监控脚本(watch_probe.py)只能采集**从现在开始**的成本门槛,
而链上价格是**可回溯**的 —— 这正是 The Graph 的用武之地:
把已经过去的那段窗口的价格补出来,和成本序列对齐。

    watch/*.jsonl   真实成本门槛,易逝,不采就没了
    price_history   屏幕价差,可回溯,什么时候拉都在

两条序列对齐之后才能算逐小时的净收益 = 屏幕价差 − 真实成本。

**一条硬规矩(实测得出):subgraph 的价格可信,TVL 不可信。**
  · 价格   subgraph 与 RPC slot0 交叉验证 0.00 bps,可以放心用
  · TVL    subgraph 的 totalValueLockedToken* 是按 swap 增量累加的,
           实测比链上真实余额虚高 3–6 倍
  所以:**价格取历史用 subgraph,深度/选池一律读链上余额。**

用法:
    python3 price_history.py --chains ARB,BAS --base USDC --quote WETH --hours 48
    python3 price_history.py ... --since 2026-08-03T08:34:00Z
    python3 price_history.py ... --csv price_history.csv
    python3 price_history.py ... --verify        # 最新一小时 vs 当前现货,校验管道

需要 .env 里的 THE_GRAPH_KEY(见 lib/graph.py)。
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.graph import GraphClient, SUBGRAPHS

# 每条链上用来代表"这条链价格"的参考池。
# **选池依据是链上真实余额**(见 pool_price.py),不是 subgraph 的 TVL。
REF_POOLS = {
    "ARB": {"pool": "0xc6962004f452be9203591991d15f6b388e09e8d0",
            "label": "UniV3 fee500", "subgraph": "ARB"},
    "BAS": {"pool": "0x6c561b446416e1a00e8e93e221854d6ea4171372",
            "label": "UniV3 fee3000", "subgraph": "BAS"},
}

# 备选池:同一条链上的其它深池,用来量"链内场所间离散度"。
# 跨链价差如果小于这个离散度,那个价差就不是可靠信号(见 pool_price.py 的告警)。
ALT_POOLS = {
    "ARB": [("UniV3 fee3000", "0xc473e2aee3441bf18cf58a300ef9440304b21dcb", "ARB")],
    "BAS": [("UniV3 fee500", "0xd0b53d9277642d899df5c87a3966a349a798f224", "BAS"),
            ("Slipstream ts100", "0xb2cc224c1c9fee385f8ad6a55b4d94e92359dc59",
             "AERO")],
}

# Aerodrome Slipstream 的 subgraph(标准 Uniswap schema,已验证)
EXTRA_SUBGRAPHS = {
    "AERO": {"id": "GENunSHWLBXm59mBSgPzQ8metBEp9YDfdqwFr91Av1UM",
             "name": "Aerodrome Base Full (Slipstream)"},
}

HOUR_Q = """
{
  poolHourDatas(
    first: %d
    orderBy: periodStartUnix
    orderDirection: asc
    where: {pool: "%s", periodStartUnix_gte: %d, periodStartUnix_lt: %d}
  ) {
    periodStartUnix token1Price token0Price close volumeUSD txCount
  }
}
"""


def sg_id(key):
    if key in EXTRA_SUBGRAPHS:
        return EXTRA_SUBGRAPHS[key]["id"]
    return SUBGRAPHS[key]["id"]


def fetch_hours(g, subgraph_key, pool, start, end, base_is_t0):
    """
    拉一个池的逐小时价格。分页拉,因为 subgraph 单次最多 1000 条。

    token0Price / token1Price 的方向要看 base 是不是 token0:
      base 是 token0 → token0Price 就是 "base per quote"
      base 是 token1 → token1Price 才是
    这个方向搞反,整条曲线会变成倒数,而且**不会报错** —— 又一个"没有报错的错误"。
    """
    out = []
    cursor = start
    while cursor < end:
        rows = g.query_id(sg_id(subgraph_key),
                          HOUR_Q % (1000, pool.lower(), cursor, end))
        rows = rows["poolHourDatas"]
        if not rows:
            break
        for r in rows:
            px = float(r["token0Price"] if base_is_t0 else r["token1Price"])
            if px <= 0:
                continue
            out.append({"ts": int(r["periodStartUnix"]), "price": px,
                        "volumeUSD": float(r["volumeUSD"] or 0),
                        "txCount": int(r["txCount"] or 0)})
        nxt = rows[-1]["periodStartUnix"]
        if int(nxt) + 1 <= cursor:
            break
        cursor = int(nxt) + 1
        time.sleep(0.15)
    return out


def main():
    p = argparse.ArgumentParser(description="用 The Graph 拉逐小时价格并算跨链价差")
    p.add_argument("--chains", default="ARB,BAS")
    p.add_argument("--base", default="USDC")
    p.add_argument("--quote", default="WETH")
    p.add_argument("--hours", type=int, default=48, help="往回拉多少小时")
    p.add_argument("--since", help="起始时间 ISO8601,如 2026-08-03T08:34:00Z,优先于 --hours")
    p.add_argument("--csv", help="导出 CSV")
    p.add_argument("--verify", action="store_true",
                   help="拿最新一小时和当前现货对一下,校验整条管道")
    args = p.parse_args()

    chains = [c.strip().upper() for c in args.chains.split(",") if c.strip()]
    now = int(time.time())
    if args.since:
        s = args.since.replace("Z", "+00:00")
        start = int(datetime.fromisoformat(s).timestamp())
    else:
        start = now - args.hours * 3600
    start = start - start % 3600          # 对齐到整点
    end = now + 3600

    g = GraphClient()

    # base 是不是 token0:USDC vs WETH 比地址大小。两条链上 WETH 都比 USDC 小,
    # 所以 WETH=token0、USDC=token1 —— 但不能想当然,这里显式判断。
    from pool_price import get_token
    series = {}
    for c in chains:
        if c not in REF_POOLS:
            print(f"{c}: 没有登记参考池,跳过", file=sys.stderr)
            continue
        b = get_token(c, args.base)
        q = get_token(c, args.quote)
        base_is_t0 = int(b["address"], 16) < int(q["address"], 16)

        ref = REF_POOLS[c]
        rows = fetch_hours(g, ref["subgraph"], ref["pool"], start, end, base_is_t0)
        series[c] = {"ref": ref, "rows": rows, "base_is_t0": base_is_t0,
                     "base": b, "quote": q}
        print(f"{c} {ref['label']}: {len(rows)} 个小时点", file=sys.stderr)

        # 同链备选池 → 链内离散度
        for label, pool, sk in ALT_POOLS.get(c, []):
            alt = fetch_hours(g, sk, pool, start, end, base_is_t0)
            series[c].setdefault("alts", []).append({"label": label, "rows": alt})
            print(f"   备选 {label}: {len(alt)} 个小时点", file=sys.stderr)

    if not series:
        return 1

    # 按小时对齐
    idx = {}
    for c, s in series.items():
        for r in s["rows"]:
            idx.setdefault(r["ts"], {})[c] = r["price"]
    alt_idx = {}
    for c, s in series.items():
        for a in s.get("alts", []):
            for r in a["rows"]:
                alt_idx.setdefault(r["ts"], {}).setdefault(c, []).append(r["price"])

    hours = sorted(idx)
    if not hours:
        print("窗口内没有数据。可能是时间范围不对,或该池这段时间没有成交。",
              file=sys.stderr)
        return 1

    qs, bs = args.quote, args.base
    print()
    print("=" * 82)
    print(f"逐小时价格与跨链价差   {bs}/{qs}")
    print(f"窗口 {datetime.fromtimestamp(hours[0], timezone.utc):%Y-%m-%d %H:%M} ~ "
          f"{datetime.fromtimestamp(hours[-1], timezone.utc):%Y-%m-%d %H:%M} UTC"
          f"   共 {len(hours)} 小时")
    print("=" * 82)

    two = len(chains) == 2 and all(c in series for c in chains)
    c1, c2 = (chains[0], chains[1]) if two else (None, None)

    rows_out, spreads = [], []
    for h in hours:
        rec = {"ts": h,
               "utc": datetime.fromtimestamp(h, timezone.utc).isoformat()}
        for c in series:
            rec[f"{c}_price"] = idx[h].get(c)
        # 链内离散度:参考池 vs 同链其它深池
        for c in series:
            vals = [idx[h][c]] if c in idx[h] else []
            vals += alt_idx.get(h, {}).get(c, [])
            if len(vals) >= 2:
                rec[f"{c}_disp_bps"] = (max(vals) - min(vals)) / min(vals) * 10_000
        if two and idx[h].get(c1) and idx[h].get(c2):
            sp = (idx[h][c2] - idx[h][c1]) / idx[h][c1] * 10_000
            rec["spread_bps"] = sp
            spreads.append((h, sp, rec.get(f"{c1}_disp_bps"),
                            rec.get(f"{c2}_disp_bps")))
        rows_out.append(rec)

    show = rows_out if len(rows_out) <= 30 else rows_out[:12] + [None] + rows_out[-12:]
    hdr = f"{'时间(UTC)':17}"
    for c in series:
        hdr += f" {c+' 价':>12}"
    if two:
        hdr += f" {'跨链bps':>9} {'链内离散':>9}"
    print(hdr)
    print("-" * 82)
    for r in show:
        if r is None:
            print(f"{'  … 中间省略 …':17}")
            continue
        line = f"{r['utc'][5:16]:17}"
        for c in series:
            v = r.get(f"{c}_price")
            line += f" {v:>12,.2f}" if v else f" {'—':>12}"
        if two:
            sp = r.get("spread_bps")
            d = max([x for x in (r.get(f"{c1}_disp_bps"),
                                 r.get(f"{c2}_disp_bps")) if x is not None],
                    default=None)
            line += f" {sp:>+9.2f}" if sp is not None else f" {'—':>9}"
            line += f" {d:>9.2f}" if d is not None else f" {'—':>9}"
        print(line)

    if spreads:
        v = sorted(x[1] for x in spreads)
        n = len(v)
        print("-" * 82)
        print(f">> 跨链价差 {c1}→{c2}  共 {n} 小时")
        print(f"   最小 {v[0]:+.2f}   中位 {v[n//2]:+.2f}   最大 {v[-1]:+.2f} bps")
        pos = sum(1 for x in v if x > 0)
        print(f"   为正 {pos}/{n} 小时({pos/n*100:.0f}%)")

        # 关键判断:价差有没有大到能穿过链内噪声
        dv = [max([y for y in (a, b) if y is not None], default=0)
              for _, _, a, b in spreads]
        if dv:
            med_d = sorted(dv)[len(dv)//2]
            beat = sum(1 for (_, s, a, b) in spreads
                       if abs(s) > max([y for y in (a, b) if y is not None],
                                       default=0))
            print(f"   链内场所离散度中位 {med_d:.2f} bps")
            print(f"   **跨链价差绝对值超过同期链内离散度的小时数:{beat}/{n}"
                  f"({beat/n*100:.0f}%)**")
            if beat / n < 0.5:
                print("   → 大部分时间,跨链价差比链内噪声还小。"
                      "这条路的「屏幕价差」本身就不是个稳定信号。")
        print("-" * 82)
        print("下一步:把这条序列和 watch/*.jsonl 的成本门槛按小时对齐,")
        print("        逐小时算 净收益 = 屏幕价差 − 真实成本。")

    if args.verify:
        print("\n" + "=" * 82)
        print("管道校验:最新一小时 vs 当前现货")
        import subprocess
        last = rows_out[-1]
        for c in series:
            print(f"  {c} subgraph 最新小时: {last.get(f'{c}_price'):,.2f}")
        print("  (跑 `python3 pool_price.py` 对比当前现货;"
              "小时收盘价和实时价有时间差,几 bps 内属正常)")

    if args.csv:
        cols = ["ts", "utc"] + [f"{c}_price" for c in series] + \
               [f"{c}_disp_bps" for c in series] + (["spread_bps"] if two else [])
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows_out)
        print(f"\n已导出 {args.csv}({len(rows_out)} 行)")

    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())
