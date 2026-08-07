#!/usr/bin/env python3
"""
delay_risk.py —— 把「延迟风险」从一句话变成一个可算的数

成本模型里最含糊的一项:

    净收益 = 屏幕价差 − … − 延迟风险(执行期间价差消失的概率 × 损失) − …

大多数人到这一项就开始拍脑袋(「假设 2 bps」)。**但它可以实测。**

关键数据源:The Graph 的 `swaps` 实体带 `timestamp` + `sqrtPriceX96` ——
**每一笔成交都是一个精确到秒的价格快照**。有了它就能回答:

    我这条路要跑 T 秒,这 T 秒里价格/价差通常会跑多远?

两种模式:

  · **单池模式**(--pool):量一个池子自己的价格在 T 秒内的漂移。
    适合回答"我在目标链卖出时,价格还是不是我看到的那个"。

  · **价差模式**(--spread,默认):量**两条链之间的价差**在 T 秒内的漂移。
    **这才是套利真正关心的** —— 你赚的是价差,不是单边价格。

一个容易忽略的性质:**AMM 的价格只在有人成交时才变**,两笔成交之间价格恒定。
所以 swap 序列就是完整的价格路径,不需要插值 —— 只要取
「t+T 时刻之前最后一笔成交的价格」即可。

用法:
    # 默认:ARB vs BAS 的 WETH/USDC 价差,过去 24 小时
    python3 delay_risk.py

    # 单个池子的价格漂移
    python3 delay_risk.py --pool BAS --hours 24

    # 指定要评估的延迟(秒),对应 cost_probe 报出的 executionDuration
    python3 delay_risk.py --horizons 7,30,300,1080,1859

    # 导出给成本模型用
    python3 delay_risk.py --json delay_risk.json
"""

import argparse
import bisect
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.graph import GraphClient

Q96 = 2 ** 96

# 每条链上用来代表价格的参考池。选池依据是**链上真实余额**(见 pool_price.py),
# 不是 subgraph 的 TVL —— 后者实测虚高 3~6 倍。
POOLS = {
    "ARB": {"pool": "0xc6962004f452be9203591991d15f6b388e09e8d0",
            "label": "UniV3 fee500", "d0": 18, "d1": 6, "base_is_t0": False},
    "BAS": {"pool": "0x6c561b446416e1a00e8e93e221854d6ea4171372",
            "label": "UniV3 fee3000", "d0": 18, "d1": 6, "base_is_t0": False},
}

SWAPS_Q = """
{
  swaps(first: 1000, orderBy: timestamp, orderDirection: desc,
        where: {pool: "%s", timestamp_lte: %d, timestamp_gte: %d}) {
    timestamp sqrtPriceX96 amountUSD
  }
}
"""


def fetch_series(g, chain, start, end, max_pages=30):
    """
    拉一个池子在窗口内的全部 swap,转成 (时间戳, 价格) 序列。

    分页往回翻 —— subgraph 单次最多 1000 条,活跃池一小时就可能超。
    """
    meta = POOLS[chain]
    out = {}
    cursor = end
    for _ in range(max_pages):
        rows = g.query(chain, SWAPS_Q % (meta["pool"], cursor, start))["swaps"]
        if not rows:
            break
        # rows 是**降序**(新→旧)。同一秒可能有多笔成交(实测 200 笔里有 33 个
        # 这样的时间戳),该保留的是**那一秒最后的价格**,也就是降序里第一次
        # 遇到的那笔。所以已存在的键不要覆盖。
        for s in rows:
            sq = int(s["sqrtPriceX96"])
            if sq <= 0:
                continue
            if int(s["timestamp"]) in out:
                continue
            # (sqrt/2^96)^2 = token1/token0(原始单位),再按 decimals 换算
            raw = (sq / Q96) ** 2
            px = raw * 10 ** (meta["d0"] - meta["d1"])
            if meta["base_is_t0"]:
                px = 1 / px if px else 0
            if px > 0:
                out[int(s["timestamp"])] = px
        nxt = int(rows[-1]["timestamp"]) - 1
        if nxt <= start or len(rows) < 1000:
            break
        cursor = nxt
        time.sleep(0.15)
    return sorted(out.items())


class StepSeries:
    """
    阶梯函数取值器。**AMM 价格在两笔成交之间是恒定的**,
    所以 t 时刻的价格 = t 之前最后一笔成交的价格。

    (第一版我写成了"t 之后第一笔成交",那是错的 ——
     会把还没发生的价格当成当前价,相当于偷看未来。)
    """

    def __init__(self, pairs):
        self.ts = [t for t, _ in pairs]
        self.px = [p for _, p in pairs]

    def at(self, t):
        i = bisect.bisect_right(self.ts, t) - 1
        return self.px[i] if i >= 0 else None

    def span(self):
        return (self.ts[0], self.ts[-1]) if self.ts else (0, 0)

    def __len__(self):
        return len(self.ts)


def quantiles(vals, qs=(0.5, 0.75, 0.9, 0.95, 0.99)):
    if not vals:
        return {}
    v = sorted(vals)
    n = len(v)
    return {q: v[min(n - 1, int(n * q))] for q in qs}


def measure(series, horizons, sample_step=None):
    """
    对每个起点 t,量 T 秒后的变动。

    返回两套数:
      · abs_move —— |变动|,回答"通常跑多远"
      · adverse  —— 只取**对你不利**的一侧。套利里价格朝有利方向跑不是风险,
                    所以真正该进成本模型的是这一侧。
                    这里不知道你的方向,所以取"负向变动"的绝对值,
                    等价于假设你持有多头。
    """
    ts = series.ts
    if not ts:
        return {}
    step = sample_step or max(1, len(ts) // 2000)   # 太密就抽样,别把自己算死
    out = {}
    for T in horizons:
        absm, adv = [], []
        for i in range(0, len(ts), step):
            t0 = ts[i]
            p0 = series.px[i]
            if t0 + T > ts[-1]:
                break
            p1 = series.at(t0 + T)
            if p1 is None or p0 <= 0:
                continue
            d = (p1 - p0) / p0 * 10_000
            absm.append(abs(d))
            adv.append(max(0.0, -d))
        out[T] = {"n": len(absm), "abs": quantiles(absm), "adverse": quantiles(adv),
                  "abs_mean": statistics.mean(absm) if absm else 0,
                  "adverse_mean": statistics.mean(adv) if adv else 0}
    return out


def build_spread(sa, sb, grid_step=10):
    """
    两条链的价差序列:spread(t) = (P_b(t) − P_a(t)) / P_a(t)。

    两边的成交时刻对不齐,所以在**公共时间网格**上取值。
    grid_step 越小越精细,但计算量线性上升;10 秒对分钟级的延迟够用。
    """
    lo = max(sa.span()[0], sb.span()[0])
    hi = min(sa.span()[1], sb.span()[1])
    pairs = []
    t = lo
    while t <= hi:
        pa, pb = sa.at(t), sb.at(t)
        if pa and pb and pa > 0:
            pairs.append((t, (pb - pa) / pa * 10_000))   # 直接存 bps
        t += grid_step
    return pairs


def measure_spread(pairs, horizons, grid_step=10):
    """
    价差的漂移。注意这里单位已经是 bps,所以是**差值**不是比值 ——
    价差从 +5 bps 变成 −3 bps,漂移就是 8 bps。

    ⚠️ **T 必须 ≥ 网格步长,否则结果是伪零。**
    第一版没做这个检查,于是 grid=10 时 T=7 的那一行**全部是 0.00**:
    因为 t0+7 落在 t0 和 t0+10 之间,取"≤t0+7 的最后一点"就是 t0 自己,
    差值恒等于 0。而它还报 n=8631,看起来像个充分采样的结果 ——
    **这比报错危险得多。**
    """
    ts = [t for t, _ in pairs]
    vs = [v for _, v in pairs]
    idx = {t: i for i, t in enumerate(ts)}
    out = {}
    for T in horizons:
        if T < grid_step:
            out[T] = {"n": 0, "abs": {}, "adverse": {},
                      "abs_mean": 0, "adverse_mean": 0,
                      "unmeasurable": f"T({T}s) < 网格({grid_step}s)"}
            continue
        absm, adv = [], []
        for i, t0 in enumerate(ts):
            j = idx.get(t0 + T)
            if j is None:
                k = bisect.bisect_right(ts, t0 + T) - 1
                if k < 0 or ts[k] < t0:
                    continue
                j = k
            d = vs[j] - vs[i]
            absm.append(abs(d))
            adv.append(max(0.0, -d))     # 价差缩小 = 对套利不利
        out[T] = {"n": len(absm), "abs": quantiles(absm), "adverse": quantiles(adv),
                  "abs_mean": statistics.mean(absm) if absm else 0,
                  "adverse_mean": statistics.mean(adv) if adv else 0}
    return out


def realized_vol(series, bucket=3600):
    """按小时算已实现波动,用来判断这段样本是平静还是剧烈。"""
    out = {}
    for i in range(1, len(series)):
        h = series.ts[i] // bucket * bucket
        r = (series.px[i] - series.px[i - 1]) / series.px[i - 1] * 10_000
        out.setdefault(h, []).append(r)
    return {h: statistics.pstdev(v) for h, v in out.items() if len(v) > 2}


def main():
    p = argparse.ArgumentParser(description="实测延迟风险:T 秒后价格/价差跑多远")
    p.add_argument("--pool", help="单池模式,给链名(ARB/BAS)。不给则用价差模式")
    p.add_argument("--chains", default="ARB,BAS", help="价差模式的两条链")
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--horizons", default="7,30,60,300,1080,1859",
                   help="要评估的延迟(秒),对应 cost_probe 的 executionDuration")
    p.add_argument("--grid", type=int, default=10, help="价差模式的时间网格(秒)")
    p.add_argument("--json", help="导出结果给成本模型用")
    args = p.parse_args()

    horizons = sorted(int(x) for x in args.horizons.split(",") if x.strip())
    end = int(time.time())
    start = end - args.hours * 3600
    g = GraphClient()

    if args.pool:
        chain = args.pool.upper()
        print(f"拉 {chain} {POOLS[chain]['label']} 的 swap…", file=sys.stderr)
        ser = StepSeries(fetch_series(g, chain, start, end))
        if len(ser) < 50:
            print(f"样本太少({len(ser)}),放大 --hours", file=sys.stderr)
            return 1
        res = measure(ser, horizons)
        vols = realized_vol(ser)
        title = f"单池价格漂移  {chain} {POOLS[chain]['label']}"
        span = ser.span()
        nobs = len(ser)
    else:
        c1, c2 = [c.strip().upper() for c in args.chains.split(",")]
        print(f"拉 {c1} 和 {c2} 的 swap…", file=sys.stderr)
        sa = StepSeries(fetch_series(g, c1, start, end))
        sb = StepSeries(fetch_series(g, c2, start, end))
        if len(sa) < 50 or len(sb) < 50:
            print(f"样本太少({c1}:{len(sa)} {c2}:{len(sb)}),放大 --hours", file=sys.stderr)
            return 1
        # 网格必须细于最小的 horizon,否则那些行是伪零。自动收紧。
        grid = min(args.grid, min(horizons)) if horizons else args.grid
        grid = max(1, grid)
        if grid != args.grid:
            print(f"(最小 horizon 是 {min(horizons)}s,网格自动从 {args.grid}s "
                  f"收紧到 {grid}s,否则短延迟那几行会是伪零)", file=sys.stderr)
        pairs = build_spread(sa, sb, grid)
        if len(pairs) < 100:
            print("价差序列太短", file=sys.stderr)
            return 1
        res = measure_spread(pairs, horizons, grid)
        vols = realized_vol(sa)
        title = f"跨链价差漂移  {c1} → {c2}"
        span = (pairs[0][0], pairs[-1][0])
        nobs = len(pairs)
        spv = [v for _, v in pairs]
        print(f"\n价差本身:中位 {statistics.median(spv):+.2f} bps  "
              f"区间 [{min(spv):+.2f}, {max(spv):+.2f}]", file=sys.stderr)

    print()
    print("=" * 84)
    print(f"延迟风险实测   {title}")
    print(f"窗口 {datetime.fromtimestamp(span[0], timezone.utc):%m-%d %H:%M} ~ "
          f"{datetime.fromtimestamp(span[1], timezone.utc):%m-%d %H:%M} UTC"
          f"  ({(span[1]-span[0])/3600:.1f}h,{nobs:,} 个观测点)")
    print("=" * 84)

    print(f"{'延迟':>7} {'样本':>7} │ {'不利侧(该进成本模型)':^30} │ {'绝对变动(参考)':^18}")
    print(f"{'(秒)':>7} {'':>7} │ {'中位':>7}{'75%':>8}{'90%':>8}{'95%':>7} │ {'中位':>8}{'95%':>9}")
    print("-" * 84)
    for T in horizons:
        r = res.get(T)
        if r and r.get("unmeasurable"):
            print(f"{T:>7} {'不可测':>7} │ {r['unmeasurable']:^30} │")
            continue
        if not r or r["n"] < 20:
            print(f"{T:>7} {'样本不足':>7}")
            continue
        a, b = r["adverse"], r["abs"]
        print(f"{T:>7} {r['n']:>7} │ {a[0.5]:>7.2f}{a[0.75]:>8.2f}{a[0.9]:>8.2f}{a[0.95]:>7.2f} │ "
              f"{b[0.5]:>8.2f}{b[0.95]:>9.2f}")

    print("-" * 84)
    print("怎么用:找到你那条路的 executionDuration,查对应行的**不利侧 95% 分位**,")
    print("        那就是「延迟风险」这一项该填的保守值。")

    if vols:
        v = sorted(vols.values())
        n = len(v)
        print(f"\n行情背景:逐小时已实现波动 中位 {v[n//2]:.2f} bps "
              f"(最低 {v[0]:.2f} / 最高 {v[-1]:.2f})")
        if v[-1] < v[n // 2] * 3:
            print("  ⚠ 这段样本**没有剧烈行情**。延迟风险在行情剧烈时会显著放大,")
            print("    上面的数字是**平静时段的下界**,不能当最坏情况用。")

    print("=" * 84)

    if args.json:
        out = {"title": title, "window_start": span[0], "window_end": span[1],
               "n_obs": nobs, "mode": "pool" if args.pool else "spread",
               "grid_step": grid if not args.pool else 1,
               "horizons": {str(T): {"n": r["n"],
                                     "adverse_p50": r["adverse"].get(0.5),
                                     "adverse_p75": r["adverse"].get(0.75),
                                     "adverse_p90": r["adverse"].get(0.9),
                                     "adverse_p95": r["adverse"].get(0.95),
                                     "abs_p50": r["abs"].get(0.5),
                                     "abs_p95": r["abs"].get(0.95)}
                            for T, r in res.items()
                            if r["n"] >= 20 and not r.get("unmeasurable")},
               "hourly_vol_median": statistics.median(vols.values()) if vols else None}
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"已导出 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
