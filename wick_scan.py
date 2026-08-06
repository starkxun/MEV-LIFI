#!/usr/bin/env python3
"""
wick_scan.py —— 扫描历史「针」:出现频率 + 回弹率

回答「接针」策略最核心的两个问题:

    1. 深针多久出现一次?        → 决定你的钱要空等多久(资金占用)
    2. 扎完之后价格回不回来?    → 决定你接到的是便宜货还是归零盘

这两个数都是**链上历史**,不用挂服务器等 —— `poolHourDatas` 里每小时的
open/high/low/close 一直躺在那儿,什么时候拉都在。

**为什么小时最低价就能判断"会不会被填"**:
AMM 和交易所挂单不一样 —— 交易所可能价格跳过你的挂单成交,
但 AMM 的价格是沿着曲线连续移动的,**只要价格穿过你的区间,就必然吃掉你的单**。
所以「这一小时最低价跌破了我的挂单价」= 一定成交。

用法:
    # 扫一批池子(默认按成交额取前 N 个)
    python3 wick_scan.py --chain BNB --days 30 --depth 10

    # 扫指定池
    python3 wick_scan.py --chain BNB --pool 0x65d5... --days 30 --depth 5,10,20,30

    # 改回弹判定窗口(默认 6 小时内回到扎针前的价位算回弹)
    python3 wick_scan.py --chain BNB --days 30 --depth 10 --recover-hours 12
"""

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.graph import GraphClient
from lp_backtest import CHAINS, STABLES

TOP_POOLS_Q = """
{
  pools(first: %d, orderBy: %s, orderDirection: desc) {
    id feeTier volumeUSD totalValueLockedUSD
    token0 { symbol } token1 { symbol }
  }
}
"""

# 排序字段按优先级试。**同一套 schema,不同 subgraph 支持的排序不一样** ——
# 实测 BNB 那个 subgraph 用 orderBy:volumeUSD 会稳定返回
# "bad indexers"(索引器没有这个排序索引 / 查询太贵),
# 但 orderBy:totalValueLockedUSD 完全正常;Base 那个则两个都行。
# 所以"验证过 schema 能用"不等于"任意查询都能用",要逐个查询验。
ORDER_FALLBACKS = ["volumeUSD", "totalValueLockedUSD", "id"]


def discover_pools(g, sid, top):
    """按成交额取池;该 subgraph 不支持时自动降级到 TVL 排序。"""
    last = None
    for field in ORDER_FALLBACKS:
        try:
            pools = g.query_id(sid, TOP_POOLS_Q % (top, field))["pools"]
            if pools:
                return pools, field
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"池发现失败(所有排序字段都不可用): {last}")

HOURS_Q = """
{
  poolHourDatas(first: 1000, orderBy: periodStartUnix, orderDirection: asc,
    where: {pool: "%s", periodStartUnix_gte: %d}) {
    periodStartUnix open high low close volumeUSD txCount
  }
}
"""


def fetch_hours(g, sid, pool, start):
    """分页拉小时数据。单次上限 1000 条,30 天 = 720 条,通常一次够。"""
    out, cursor = [], start
    while True:
        rows = g.query_id(sid, HOURS_Q % (pool.lower(), cursor))["poolHourDatas"]
        if not rows:
            break
        out.extend(rows)
        if len(rows) < 1000:
            break
        cursor = int(rows[-1]["periodStartUnix"]) + 1
        time.sleep(0.15)
    return out


def stable_side(t0, t1):
    """
    判断哪边是稳定币,决定价格方向。

    **这是本项目栽过两次的坑**:token0/token1 的顺序由合约地址大小决定,
    没有任何语义。方向搞反不会报错,只会让所有数字变成倒数 ——
    「跌 20%」会显示成「涨 25%」,而你看不出来。
    """
    if t0 in STABLES and t1 not in STABLES:
        return "t0"        # 价格 = token1Price?不:raw = token1/token0,
                           # token0 是稳定币时 raw 就是 币/USDT,要取倒数
    if t1 in STABLES and t0 not in STABLES:
        return "t1"        # token1 是稳定币,raw = USDT/币,方向已经对
    return None


def to_quote(o, h, l, c, side):
    """统一成「稳定币 / 币」。side=t0 表示稳定币在 token0,需要取倒数。"""
    if side == "t1":
        return o, h, l, c
    vals = [o, h, l, c]
    if min(vals) <= 0:
        return None
    o2, h2, l2, c2 = [1 / x for x in vals]
    # 取倒数之后最高最低会互换
    return o2, l2, h2, c2


def scan_pool(rows, depths, recover_hours, side,
              max_wick=90.0, min_hour_vol=1000.0, min_hour_tx=3):
    """
    找出所有下影线,并判断有没有回弹。

    下影线 = (实体下沿 − 最低价) / 实体下沿
      实体下沿 = min(开盘, 收盘) —— 用它而不是开盘,是为了排除"单边阴跌",
      我们要找的是**扎下去又弹回来**,不是**一路跌下去**。

    回弹 = 扎针之后 recover_hours 小时内,最高价有没有回到扎针前的实体下沿。
    """
    series = []
    for r in rows:
        v = to_quote(*[float(r[k]) for k in ("open", "high", "low", "close")], side)
        if v is None:
            continue
        o, h, l, c = v
        if min(o, h, l, c) <= 0:
            continue
        series.append({"ts": int(r["periodStartUnix"]), "o": o, "h": h,
                       "l": l, "c": c,
                       "vol": float(r["volumeUSD"] or 0),
                       "tx": int(r["txCount"] or 0)})

    res = {d: {"count": 0, "recovered": 0, "events": []} for d in depths}
    rejected = 0
    for i, bar in enumerate(series):
        body_lo = min(bar["o"], bar["c"])
        if body_lo <= 0:
            continue
        wick = (body_lo - bar["l"]) / body_lo * 100

        # ---- 不变量检查:把坏数据挡在统计之外 ----
        # 实测第一版扫出「针深 100.0%,成交价 7.7e-07,毛收益 +334 亿%」——
        # 那不是针,是某小时的 low 掉到接近 0 的脏数据。
        # 判别信号很明确:**回弹率 100%**。真策略不可能百发百中,
        # 一旦看到 100% 就该怀疑数据而不是庆祝。
        bad = (
            wick >= max_wick or                    # 深到不合理
            bar["l"] <= 0 or
            bar["l"] < body_lo / 1e3 or            # 最低价比实体低 3 个数量级
            bar["vol"] < min_hour_vol or           # 那一小时几乎没成交
            bar["tx"] < min_hour_tx                # 那一小时几乎没笔数
        )
        if bad and wick >= min(depths):
            rejected += 1
            continue

        fut = series[i + 1: i + 1 + recover_hours]
        if not fut:
            continue

        # 回弹判定必须用**收盘价**,不能用最高价。
        # 第一版用 max(high) 判定,结果每个深度都是 100% 回弹 ——
        # 那不是策略神,是判据烂:最高价只是盘中碰一下,你根本卖不到。
        # 在一个来回震荡的币上,"某小时最高价超过某位置"几乎必然成立。
        best_close = max(x["c"] for x in fut)
        exit_close = fut[-1]["c"]          # 老老实实持有到窗口末尾再卖

        for d in depths:
            if wick < d:
                continue
            res[d]["count"] += 1
            ok = best_close >= body_lo     # 收盘价回到扎针前
            if ok:
                res[d]["recovered"] += 1
            res[d]["events"].append({
                "ts": bar["ts"], "wick": wick, "fill": bar["l"],
                "before": body_lo, "best_close": best_close,
                "exit_close": exit_close, "recovered": ok,
                # 上限:在针尖买入、在窗口内最好的收盘价卖出(仍然要择时,做不到)
                "gross_best": (best_close - bar["l"]) / bar["l"] * 100,
                # 现实:在针尖买入、窗口末尾无脑卖出(不择时,可复现)
                "gross_hold": (exit_close - bar["l"]) / bar["l"] * 100,
            })
    return res, len(series), rejected


def main():
    p = argparse.ArgumentParser(description="扫描历史深针:频率 + 回弹率")
    p.add_argument("--chain", default="BNB", choices=list(CHAINS))
    p.add_argument("--pool", help="只扫这一个池;不给则扫成交额前 N 个")
    p.add_argument("--top", type=int, default=25, help="扫多少个池")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--depth", default="5,10,20,30",
                   help="针深度阈值(%%),逗号分隔")
    p.add_argument("--recover-hours", type=int, default=6,
                   help="扎针后多少小时内回到原位算回弹")
    p.add_argument("--min-vol", type=float, default=100_000,
                   help="池累计成交额下限")
    p.add_argument("--max-wick", type=float, default=90.0,
                   help="超过这个深度(%%)判定为脏数据,剔除")
    p.add_argument("--min-hour-vol", type=float, default=1000.0,
                   help="扎针那小时的最低成交额,低于此判定为噪声")
    p.add_argument("--min-hour-tx", type=int, default=3,
                   help="扎针那小时的最低成交笔数")
    args = p.parse_args()

    depths = sorted(float(x) for x in args.depth.split(",") if x.strip())
    sid = CHAINS[args.chain]["id"]
    g = GraphClient()
    start = int(time.time()) - args.days * 86400

    if args.pool:
        meta = g.query_id(sid, '{pool(id:"%s"){id feeTier volumeUSD '
                               'totalValueLockedUSD token0{symbol} token1{symbol}}}'
                          % args.pool.lower())["pool"]
        pools = [meta] if meta else []
    else:
        pools, order_by = discover_pools(g, sid, args.top)
        if order_by != "volumeUSD":
            print(f"(该 subgraph 不支持按成交额排序,已降级为 {order_by})",
                  file=sys.stderr)

    if not pools:
        print("没找到池", file=sys.stderr)
        return 1

    print(f"\n链 {args.chain}  窗口 {args.days} 天  "
          f"回弹判定 {args.recover_hours} 小时内")
    print("=" * 88)

    total = {d: {"count": 0, "recovered": 0, "hours": 0} for d in depths}
    total_rejected = 0
    skipped = 0
    best_events = []

    for pl in pools:
        t0, t1 = pl["token0"]["symbol"], pl["token1"]["symbol"]
        side = stable_side(t0, t1)
        if side is None:
            skipped += 1          # 两边都是稳定币,或都不是 → 没有统一计价口径
            continue
        if float(pl["volumeUSD"] or 0) < args.min_vol:
            continue
        try:
            rows = fetch_hours(g, sid, pl["id"], start)
        except Exception as e:
            print(f"  {t0}/{t1} 拉取失败: {str(e)[:60]}", file=sys.stderr)
            continue
        if len(rows) < 24:
            continue
        res, nbars, rej = scan_pool(rows, depths, args.recover_hours, side,
                                    args.max_wick, args.min_hour_vol,
                                    args.min_hour_tx)
        total_rejected += rej

        line = f"{t0}/{t1}".ljust(22)[:22]
        line += f" fee{int(pl['feeTier'])/10000:.2f}%".ljust(11)
        line += f"{nbars:>5}h  "
        for d in depths:
            c, rc = res[d]["count"], res[d]["recovered"]
            line += f"≥{d:g}%:{c:>3}"
            if c:
                line += f"({rc/c*100:>3.0f}%回) "
            else:
                line += "        "
        print(line)

        for d in depths:
            total[d]["count"] += res[d]["count"]
            total[d]["recovered"] += res[d]["recovered"]
            total[d]["hours"] += nbars
            for e in res[d]["events"]:
                best_events.append((d, f"{t0}/{t1}", e))

    print("=" * 88)
    print("汇总")
    print(f"{'针深度':>8} {'出现次数':>9} {'覆盖小时':>10} {'平均多久一次':>14} "
          f"{'回弹率':>9} {'持有中位收益':>13}")
    for d in depths:
        t = total[d]
        if not t["hours"]:
            continue
        freq = t["hours"] / t["count"] if t["count"] else float("inf")
        rr = t["recovered"] / t["count"] * 100 if t["count"] else 0
        freq_s = f"{freq:,.0f} 小时" if t["count"] else "从未出现"
        holds = sorted(e["gross_hold"] for dd, _nm, e in best_events if dd == d)
        med = f"{holds[len(holds)//2]:+.1f}%" if holds else "—"
        print(f"  ≥{d:g}%   {t['count']:>9,} {t['hours']:>10,} {freq_s:>14} "
              f"{rr:>8.0f}% {med:>12}")

    if skipped:
        print(f"\n跳过 {skipped} 个池(没有稳定币计价方,无法统一价格口径)")
    if total_rejected:
        print(f"剔除 {total_rejected} 根「针」—— 深度>{args.max_wick:g}% 或该小时"
              f"成交额<{args.min_hour_vol:,.0f}/笔数<{args.min_hour_tx},判定为脏数据")

    deep = sorted([x for x in best_events if x[0] == depths[-1]],
                  key=lambda x: -x[2]["wick"])[:8]
    if deep:
        print(f"\n最深的几根针(≥{depths[-1]:g}%):")
        print(f"{'时间(UTC)':13} {'池':18} {'针深':>7} {'成交价':>11} "
              f"{'上限收益':>9} {'持有到期':>9} 回弹")
        for _, name, e in deep:
            ts = datetime.fromtimestamp(e["ts"], timezone.utc)
            print(f"{ts:%m-%d %H:%M}  {name[:18]:18} {e['wick']:>6.1f}% "
                  f"{e['fill']:>11.6g} {e['gross_best']:>+8.1f}% "
                  f"{e['gross_hold']:>+8.1f}% {'✓' if e['recovered'] else '✗'}")

    print("\n『上限收益』= 针尖买入 + 窗口内最优收盘价卖出 —— 需要完美择时,做不到。")
    print("『持有到期』= 针尖买入 + 窗口末尾无脑卖出 —— 这才是可复现的口径。")
    print("还要扣:资金空等的机会成本、gas、以及接到真归零盘的那些次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
