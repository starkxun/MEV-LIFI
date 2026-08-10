#!/usr/bin/env python3
"""
oracle_lag.py —— 验证「跨链预言机时差」这条边是否真实存在

论文《Signals and Spoils》(2026-06) 声称:同一个 Chainlink 喂价,
**Optimism 上的更新是其它链的领先指标**,平均提前 19~27 秒,
Arbitrum 上真假阳性比高达 **498:1**。

如果成立,这就是一条**公开可读、不需要任何私下关系**的信息边 ——
正是「小而中立的清算 searcher 能不能存在」这个问题的关键。

**所以这个脚本的唯一目的是:自己复现一遍,而不是相信论文。**

---

方法:

  1. 各链读同一个喂价的 `AnswerUpdated` 事件(聚合器发出)
     事件签名: AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
  2. 找参考链上的**显著价格变动**(≥ --min-move bps)
  3. 量目标链**多久之后才穿过同一个价位**(阈值穿越法)
  4. 统计:领先比例、平均/中位领先秒数

两个必须小心的地方:

  · **不能用「配对更新」的方法。** 两条链更新频率差好几倍(实测 ARB 每 61s、
    OPT 每 176s),按"价格最接近"配对时容差内有多个候选,会挑到时间上很远
    的那条 —— 第一版就这么跑出了 74s 平均领先(论文只有 19s),**是方法产生的假象**。
  · **各链出块时间差异巨大**(Arbitrum ~0.25s / Optimism ~2s / 以太坊 ~12s)。
    所以要用**区块时间戳**,不能用区块高度。

用法:
    python3 oracle_lag.py                          # 默认 ETH/USD,过去 12 小时
    python3 oracle_lag.py --hours 24 --feed BTC
    python3 oracle_lag.py --chains OPT,ARB --hours 6
    python3 oracle_lag.py --json oracle_lag.json
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# AnswerUpdated(int256 indexed current, uint256 indexed roundId, uint256 updatedAt)
ANSWER_UPDATED = "0x0559884fd3a460db3073b7fc896cc77986f16e378210ded43186175bf646fc5f"

# 代理地址。**每一个都用 description() 现场验证过是对应的喂价**,不是抄的。
# 脚本启动时还会再验一遍 —— 地址会被 Chainlink 换,不能假设永远有效。
FEEDS = {
    "ETH": {
        "ETH":  "0x5f4eC3Df9cbd43714FE2740f5E3616155c5b8419",
        "ARB":  "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
        "OPT":  "0x13e3Ee699D1909E989722E753853AE30b17e08c5",
        "BASE": "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
    },
}

# 各链大致出块时间(秒),只用来估算扫描范围,不参与计算
BLOCK_TIME = {"ETH": 12.0, "ARB": 0.25, "OPT": 2.0, "BASE": 2.0}
SLUG = {"ETH": "eth", "ARB": "arbitrum", "OPT": "optimism", "BASE": "base"}

_S = requests.Session()
_S.headers.update({"Content-Type": "application/json"})


def ankr_key():
    p = Path(__file__).parent / ".env"
    if not p.exists():
        raise RuntimeError("没有 .env")
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith("ANKR_KEY="):
            return ln.split("=", 1)[1].strip()
        if ln.startswith("SUI_RPC=") and "ankr.com" in ln:
            return ln.split("=", 1)[1].strip().rstrip("/").split("/")[-1]
    raise RuntimeError(".env 里找不到 Ankr key")


def rpc(chain, key, method, params, retries=4):
    url = f"https://rpc.ankr.com/{SLUG[chain]}/{key}"
    delay = 1.0
    last = None
    for _ in range(retries):
        try:
            r = _S.post(url, json={"jsonrpc": "2.0", "id": 1,
                                   "method": method, "params": params}, timeout=60)
            if r.status_code == 200:
                d = r.json()
                if "result" in d:
                    return d["result"]
                last = str(d.get("error"))[:110]
            else:
                last = f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError) as e:
            last = type(e).__name__
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"{chain} {method} 失败: {last}")


def decode_string(h):
    if not h or len(h) < 130:
        return None
    try:
        n = int(h[2 + 64:2 + 128], 16)
        return bytes.fromhex(h[2 + 128:2 + 128 + n * 2]).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def verify_feed(chain, key, proxy, want):
    """
    **先验证再使用。** Chainlink 会换聚合器地址,而且同一个符号在不同链上
    可能指向不同的喂价。不验就用,等于把整个结论建在一个假设上。

    ⚠️ **必须枚举历史聚合器,不能只用当前那个。**
    Chainlink 用 phase 机制轮换底层聚合器:代理地址不变,但事件是由
    **当时那个聚合器**发出的。实测三条链各有 2 个 phase ——
    只扫当前的,回溯到 2026-06-05 时 Optimism 会得到「0 次更新」,
    而同期 Arbitrum 有 344 次。**那不是数据缺失,是扫错了合约。**
    """
    desc = decode_string(rpc(chain, key, "eth_call",
                             [{"to": proxy, "data": "0x7284e416"}, "latest"]))
    dec = rpc(chain, key, "eth_call", [{"to": proxy, "data": "0x313ce567"}, "latest"])
    cur = rpc(chain, key, "eth_call", [{"to": proxy, "data": "0x245a7bfc"}, "latest"])
    ok = desc and want.upper() in desc.upper().replace(" ", "")

    # 枚举全部 phase 的聚合器
    aggs = []
    pid = rpc(chain, key, "eth_call", [{"to": proxy, "data": "0x58303b10"}, "latest"])
    try:
        pid = int(pid, 16) if pid else 0
    except ValueError:
        pid = 0
    for i in range(1, pid + 1):
        d = "0xc1597304" + hex(i)[2:].rjust(64, "0")
        a = rpc(chain, key, "eth_call", [{"to": proxy, "data": d}, "latest"])
        if a and int(a, 16):
            aggs.append("0x" + a[-40:])
        time.sleep(0.02)
    if not aggs and cur:
        aggs = ["0x" + cur[-40:]]

    return {"desc": desc, "decimals": int(dec, 16) if dec else None,
            "aggregator": "0x" + cur[-40:] if cur else None,
            "aggregators": aggs, "phases": pid, "ok": bool(ok)}


def block_at_time(chain, key, target_ts):
    """
    二分查找某个时间戳对应的区块号。

    各链出块时间差 48 倍(ARB 0.25s vs ETH 12s),按平均出块时间估算会差很远,
    尤其回溯几个月时。二分要 ~20 次请求,但准确。
    """
    hi = int(rpc(chain, key, "eth_blockNumber", []), 16)
    lo = 1
    hi_ts = int(rpc(chain, key, "eth_getBlockByNumber", [hex(hi), False])["timestamp"], 16)
    if target_ts >= hi_ts:
        return hi
    for _ in range(60):
        if lo >= hi - 1:
            break
        mid = (lo + hi) // 2
        blk = rpc(chain, key, "eth_getBlockByNumber", [hex(mid), False])
        if not blk:
            lo = mid + 1
            continue
        t = int(blk["timestamp"], 16)
        if t < target_ts:
            lo = mid
        else:
            hi = mid
        time.sleep(0.02)
    return lo


def fetch_updates(chain, key, aggs, hours, chunk_blocks=10000,
                  start_block=None, end_block=None):
    """
    拉 AnswerUpdated 事件,并取每个事件所在区块的时间戳。

    区块时间戳要单独查(getLogs 不带),所以按区块号去重后批量取 ——
    同一区块里的多个事件共用一个时间戳。
    """
    if start_block is not None and end_block is not None:
        start, head = start_block, end_block
    else:
        head = int(rpc(chain, key, "eth_blockNumber", []), 16)
        span = int(hours * 3600 / BLOCK_TIME[chain])
        start = max(0, head - span)

    logs = []
    b = start
    while b < head:
        e = min(b + chunk_blocks, head)
        try:
            # address 传数组 = 一次查多个聚合器,覆盖所有 phase
            got = rpc(chain, key, "eth_getLogs",
                      [{"address": aggs if isinstance(aggs, list) else [aggs],
                        "topics": [ANSWER_UPDATED],
                        "fromBlock": hex(b), "toBlock": hex(e)}])
            logs += got or []
        except RuntimeError:
            pass          # 单片失败不影响整体,继续
        b = e + 1
        time.sleep(0.03)

    # 批量取区块时间戳
    blocks = sorted({int(l["blockNumber"], 16) for l in logs})
    ts = {}
    for bn in blocks:
        try:
            blk = rpc(chain, key, "eth_getBlockByNumber", [hex(bn), False])
            if blk:
                ts[bn] = int(blk["timestamp"], 16)
        except RuntimeError:
            continue
        time.sleep(0.02)

    out = []
    for l in logs:
        bn = int(l["blockNumber"], 16)
        if bn not in ts:
            continue
        # topic1 = current price(int256,indexed)
        price = int(l["topics"][1], 16)
        if price >= 2 ** 255:
            price -= 2 ** 256
        out.append({"ts": ts[bn], "price": price, "block": bn,
                    "tx": l["transactionHash"]})
    out.sort(key=lambda x: x["ts"])
    return out


class StepPrice:
    """
    阶梯价格序列:t 时刻这条链"看到"的价格 = t 之前最后一次发布的值。

    预言机和 AMM 一样是阶梯函数 —— 两次发布之间,链上价格是恒定的。
    清算判定用的就是这个值,所以必须按阶梯取,不能插值。
    """

    def __init__(self, updates):
        self.ts = [u["ts"] for u in updates]
        self.px = [u["price"] for u in updates]

    def at(self, t):
        import bisect
        i = bisect.bisect_right(self.ts, t) - 1
        return self.px[i] if i >= 0 else None

    def first_cross_below(self, level, t_from, t_until):
        """从 t_from 起,第一次价格 ≤ level 的时刻。"""
        for i, t in enumerate(self.ts):
            if t < t_from or t > t_until:
                continue
            if self.px[i] <= level:
                return t
        return None

    def first_cross_above(self, level, t_from, t_until):
        for i, t in enumerate(self.ts):
            if t < t_from or t > t_until:
                continue
            if self.px[i] >= level:
                return t
        return None


def crossing_lead(ref_updates, tgt_updates, min_move_bps, max_wait):
    """
    **阈值穿越法** —— 这才是论文真正测的东西,也是清算真正关心的。

    对参考链上每一次**显著价格变动**(幅度 ≥ min_move_bps),
    看目标链**多久之后才穿过同一个价位**。

    为什么不用「配对更新」:两条链更新频率差好几倍(实测 ARB 每 61s、
    OPT 每 176s)。按"价格最接近"配对时,容差内有多个候选,
    会挑到时间上很远的那条,**系统性夸大领先量**
    —— 第一版就是这么跑出 74s 平均领先的(论文只有 19s)。

    穿越法没有这个问题:它问的是一个有确定答案的问题
    ——「目标链什么时候也认为价格到了这个水平」。
    """
    tgt = StepPrice(tgt_updates)
    out = []
    for i in range(1, len(ref_updates)):
        prev, cur = ref_updates[i - 1], ref_updates[i]
        if prev["price"] <= 0:
            continue
        move = (cur["price"] - prev["price"]) / prev["price"] * 10_000
        if abs(move) < min_move_bps:
            continue
        t0 = cur["ts"]
        # 目标链在这一刻看到的价格
        seen = tgt.at(t0)
        if seen is None:
            continue
        level = cur["price"]
        if move < 0:
            # 参考链跌到 level;目标链已经在这个水平之下 → 它更早,不算领先
            if seen <= level:
                out.append({"ts": t0, "move_bps": move, "lead": 0.0, "already": True})
                continue
            t1 = tgt.first_cross_below(level, t0, t0 + max_wait)
        else:
            if seen >= level:
                out.append({"ts": t0, "move_bps": move, "lead": 0.0, "already": True})
                continue
            t1 = tgt.first_cross_above(level, t0, t0 + max_wait)
        if t1 is None:
            # 等待窗口内目标链始终没到 → 记为截尾,不当成"领先 max_wait"
            out.append({"ts": t0, "move_bps": move, "lead": None, "already": False})
        else:
            out.append({"ts": t0, "move_bps": move, "lead": t1 - t0, "already": False})
    return out


def main():
    p = argparse.ArgumentParser(description="验证跨链预言机发布时差")
    p.add_argument("--feed", default="ETH", help="喂价符号(目前只登记了 ETH)")
    p.add_argument("--chains", default="OPT,ARB,BASE,ETH",
                   help="第一个是参考链(论文说 Optimism 领先)")
    p.add_argument("--hours", type=float, default=12)
    p.add_argument("--since", help="回溯到某个时刻,ISO8601 如 2026-06-05T00:00:00Z。"
                                   "给了就用 --since 起、持续 --hours 小时")
    p.add_argument("--min-move", type=float, default=5.0,
                   help="参考链上多大幅度算「显著变动」(bps),默认 5")
    p.add_argument("--time-window", type=int, default=300,
                   help="配对时的时间窗口(秒)")
    p.add_argument("--json", help="导出")
    args = p.parse_args()

    feed = FEEDS.get(args.feed.upper())
    if not feed:
        print(f"没有登记 {args.feed} 的喂价地址", file=sys.stderr)
        return 1
    chains = [c.strip().upper() for c in args.chains.split(",") if c.strip()]
    key = ankr_key()

    # ---- 1. 先验证地址 ----
    print("验证喂价地址…", file=sys.stderr)
    meta = {}
    for c in chains:
        if c not in feed:
            print(f"  {c}: 没登记地址,跳过", file=sys.stderr)
            continue
        try:
            m = verify_feed(c, key, feed[c], args.feed + "/USD")
        except RuntimeError as e:
            print(f"  {c}: 验证失败 {e}", file=sys.stderr)
            continue
        flag = "✓" if m["ok"] else "✗ 描述对不上!"
        print(f"  {c:5} {m['desc']:12} decimals={m['decimals']} "
              f"phases={m['phases']} aggs={len(m['aggregators'])} {flag}",
              file=sys.stderr)
        if m["ok"]:
            meta[c] = m

    if len(meta) < 2:
        print("可用链不足 2 条", file=sys.stderr)
        return 1

    # ---- 2. 拉更新 ----
    win = None
    if args.since:
        t0 = int(datetime.fromisoformat(
            args.since.strip().replace("Z", "+00:00")).timestamp())
        t1 = t0 + int(args.hours * 3600)
        print(f"回溯窗口 {datetime.fromtimestamp(t0, timezone.utc):%Y-%m-%d %H:%M} ~ "
              f"{datetime.fromtimestamp(t1, timezone.utc):%Y-%m-%d %H:%M} UTC",
              file=sys.stderr)
        win = {}
        for c in meta:
            b0 = block_at_time(c, key, t0)
            b1 = block_at_time(c, key, t1)
            win[c] = (b0, b1)
            print(f"  {c}: 区块 {b0:,} ~ {b1:,} ({b1-b0:,} 块)", file=sys.stderr)

    series = {}
    for c in meta:
        print(f"拉 {c} 的 AnswerUpdated…", file=sys.stderr)
        try:
            if win:
                series[c] = fetch_updates(c, key, meta[c]["aggregators"], args.hours,
                                          start_block=win[c][0], end_block=win[c][1])
            else:
                series[c] = fetch_updates(c, key, meta[c]["aggregators"], args.hours)
        except RuntimeError as e:
            print(f"  {c}: {e}", file=sys.stderr)
            continue
        print(f"  {c}: {len(series[c])} 次更新", file=sys.stderr)

    live = {c: v for c, v in series.items() if len(v) >= 3}
    if len(live) < 2:
        print("有效数据不足", file=sys.stderr)
        return 1

    ref = chains[0] if chains[0] in live else list(live)[0]

    print()
    print("=" * 84)
    print(f"跨链预言机发布时差   {args.feed}/USD   参考链 = {ref}")
    print(f"窗口 {args.hours} 小时   方法:阈值穿越"
          f"(参考链变动 ≥{args.min_move:g} bps,目标链最多等 {args.time_window}s)")
    print("=" * 84)

    print(f"{'链':6} {'更新次数':>8} {'平均间隔':>10}")
    for c, v in live.items():
        gaps = [v[i]["ts"] - v[i-1]["ts"] for i in range(1, len(v))]
        med = statistics.median(gaps) if gaps else 0
        print(f"{c:6} {len(v):>8} {med:>8.0f}s")

    print()
    print(f"{'对比':16} {'事件数':>7} {'参考链领先':>11} {'平均领先':>10} "
          f"{'中位领先':>10} {'最大领先':>10}")
    print("-" * 84)
    results = {}
    for c, v in live.items():
        if c == ref:
            continue
        ev = crossing_lead(live[ref], v, args.min_move, args.time_window)
        if len(ev) < 3:
            print(f"{ref}→{c:11} {len(ev):>7}  显著变动太少(调小 --min-move)")
            continue
        leads = [e["lead"] for e in ev if e["lead"] is not None and not e["already"]]
        already = sum(1 for e in ev if e["already"])
        censored = sum(1 for e in ev if e["lead"] is None)
        pos = [x for x in leads if x > 0]
        results[c] = {
            "n": len(ev), "already_ahead": already, "censored": censored,
            "measured": len(leads),
            "lead_pct": len(pos) / len(ev) * 100,
            "mean_lead": statistics.mean(pos) if pos else 0,
            "median_lead": statistics.median(pos) if pos else 0,
            "max_lead": max(pos) if pos else 0}
        r = results[c]
        print(f"{ref}→{c:11} {r['n']:>7} {len(pos):>6} ({r['lead_pct']:>3.0f}%) "
              f"{r['mean_lead']:>9.1f}s {r['median_lead']:>9.1f}s {r['max_lead']:>9.0f}s")
        print(f"{'':16} {'':>7}   其中 {already} 次目标链本来就更靠前、"
              f"{censored} 次窗口内没穿过(截尾,未计入)")

    print("-" * 84)
    print("论文声称(《Signals and Spoils》2026-06,2025-10-10 单日):")
    print("   Arbitrum  TP 498 : FP 1   66.5% 提前   平均 19.40s")
    print("   Base      TP 414 : FP 0   63.5% 提前   平均 26.65s")
    print("   Ethereum  TP 188 : FP 155 76.6% 提前   平均 26.95s")
    print()

    # ---- 判据 ----
    if results:
        print(">> 判读(停止条件:领先比例 ≥60% 且中位领先 ≥5s 才算复现):")
        for c, r in results.items():
            # 用**中位数**判,不用平均 —— 平均会被截尾附近的大值拉高
            med = r["median_lead"]
            if r["lead_pct"] >= 60 and med >= 5:
                print(f"   {ref}→{c}: **复现** —— {r['lead_pct']:.0f}% 领先,"
                      f"中位 {med:.0f}s(平均 {r['mean_lead']:.0f}s)")
            elif r["lead_pct"] >= 50:
                print(f"   {ref}→{c}: 弱信号 —— {r['lead_pct']:.0f}% 领先,"
                      f"中位只有 {med:.0f}s")
            else:
                print(f"   {ref}→{c}: **没复现** —— 只有 {r['lead_pct']:.0f}% 领先")
            if r["censored"]:
                print(f"        ⚠ {r['censored']}/{r['n']} 次在 {args.time_window}s 内"
                      f"目标链**始终没穿过** —— 这些没计入,"
                      f"所以上面的领先量是**低估**")
        print()
        print("   注意:论文数据取自 2025-10-10 单日(剧烈行情)。")
        print("        平静时段更新稀疏,配对数少、结论弱 —— 这是下界不是常态。")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"feed": args.feed, "ref": ref, "hours": args.hours,
             "counts": {c: len(v) for c, v in live.items()},
             "results": results}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n已导出 {args.json}")
    print("=" * 84)
    return 0


if __name__ == "__main__":
    sys.exit(main())
