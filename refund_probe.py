#!/usr/bin/env python3
"""
refund_probe.py —— 量「构建者事后 refund」,把清算生意里最大的未知数变成一个数

背景([`docs/week_2/研究人教学.md`](docs/week_2/研究人教学.md) 2026-08-08 修正):
清算机器人 0xf0570ec4 把链上可见毛机会 **100%** 报价给了区块构建者,合约自留 0。
那它到底赚不赚钱,就完全取决于构建者的**事后 refund**。

群友那份 PDF 的结论是"refund 链上不可观测,所以不能算 ROI"。**这个结论下早了。**
它只查了一个 EOA(0x8d64)的普通入账就收手。实际上:

  · 这台机器人有 **17 个** 签名者 EOA,refund 可能打给任何一个;
  · builder refund 是**链上 ETH 转账**,不是链下结算。

关键识别规则(本脚本的核心):

    **一笔 refund,它的 from 就是该区块自己的 fee recipient。**

    构建者在自己出的块里给搜索者打钱 —— 这个签名几乎不可能伪造,
    也不需要维护任何"已知构建者地址表"(那种表一定过期)。
    普通转账、交易所提现、自有资金调拨都不会满足这个条件。

实测结果(2026-08-08,0xf0570ec4):

    17 个 EOA 收到的 builder refund 合计 **6.474503 ETH / 19 笔**,
    **全部集中在 2025-01-30 ~ 2025-02-03**(合约上线头 3 天),之后 18 个月为 0。
    同期(最近 40 笔)竞价支出 656.22 WETH,refund **0**。

    → 成本模型里「构建者 refund」这一项,**保守场景(=0)才是有实测支撑的那个**。

用法:
    python3 refund_probe.py --bot 0xf0570ec48d03171a80ff796dceadf0d385a00004
    python3 refund_probe.py --bot 0x... --bid-sample 40 --json data/refund.json

⚠️ 这个脚本只能证明「**这些地址**没收到 refund」。它证明不了:
   · refund 打给了一个和机器人没有链上关联的地址
   · 或者压根走了链下结算(法币/CEX/场外)
   没查到 ≠ 不存在。报告时请写「未发现」,不要写「不存在」。
"""

import argparse
import collections
import json
import sys
import time
from pathlib import Path

from actor_profile import ankr_key, rpc


def all_txs(multi, addr, cap):
    """把一个地址的交易翻页拉完(descOrder,最新在前)。"""
    got, page = [], None
    while len(got) < cap:
        p = {"blockchain": ["eth"], "address": [addr], "pageSize": 100, "descOrder": True}
        if page:
            p["pageToken"] = page
        r = rpc(multi, "ankr_getTransactionsByAddress", p) or {}
        batch = r.get("transactions") or []
        if not batch:
            break
        got += batch
        page = r.get("nextPageToken")
        if not page:
            break
        time.sleep(0.15)
    return got


def find_callers(multi, bot, cap):
    """机器人合约的签名者 EOA 全集 —— refund 可能打给其中任何一个。"""
    txs = all_txs(multi, bot, cap)
    c = collections.Counter((t.get("from") or "").lower()
                            for t in txs if (t.get("to") or "").lower() == bot)
    return c, txs


def scan_refunds(eth, multi, eoas, cap, miner_cache):
    """
    找 builder refund:入账 ETH 且 **from == 该区块的 fee recipient**。

    不维护构建者地址白名单 —— 那种表必然过期,而且新构建者会漏。
    用「付款人就是出块人」这个结构性特征,自证且不会过时。
    """
    hits, checked = [], 0
    for i, a in enumerate(sorted(eoas), 1):
        for t in all_txs(multi, a, cap):
            if (t.get("to") or "").lower() != a:
                continue
            v = int(t.get("value") or "0x0", 16)
            if not v:
                continue
            bn = t["blockNumber"]
            if bn not in miner_cache:
                b = rpc(eth, "eth_getBlockByNumber", [bn, False]) or {}
                miner_cache[bn] = (b.get("miner") or "").lower()
            checked += 1
            if (t.get("from") or "").lower() == miner_cache[bn]:
                hits.append({"block": int(bn, 16), "ts": int(t["timestamp"], 16),
                             "builder": t["from"].lower(), "eoa": a,
                             "eth": v / 1e18, "hash": t["hash"]})
        print(f"  [{i}/{len(eoas)}] {a}", file=sys.stderr, flush=True)
    return hits, checked


def total_bids(eth, txs, bot, n, miner_cache):
    """样本内付给 block.coinbase 的原生 ETH 合计(trace 实测,日志里看不见)。"""
    tot, done, span = 0.0, 0, []
    for t in txs:
        if done >= n:
            break
        if (t.get("to") or "").lower() != bot:
            continue
        bn = t["blockNumber"]
        if bn not in miner_cache:
            b = rpc(eth, "eth_getBlockByNumber", [bn, False]) or {}
            miner_cache[bn] = (b.get("miner") or "").lower()
        m = miner_cache[bn]
        try:
            tr = rpc(eth, "trace_transaction", [t["hash"]], retries=2) or []
        except RuntimeError:
            continue
        tot += sum(int(x["action"].get("value") or "0x0", 16) for x in tr
                   if x.get("type") == "call"
                   and (x["action"].get("to") or "").lower() == m) / 1e18
        span.append(int(t["timestamp"], 16))
        done += 1
    return tot, done, (min(span), max(span)) if span else (0, 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True, help="机器人执行合约地址")
    ap.add_argument("--cap", type=int, default=800, help="每个地址最多翻多少笔")
    ap.add_argument("--bid-sample", type=int, default=40, help="算竞价合计用多少笔")
    ap.add_argument("--json", help="导出")
    args = ap.parse_args()

    key = ankr_key()
    if not key:
        sys.exit("需要 .env 里的 ANKR_KEY")
    eth, multi = f"https://rpc.ankr.com/eth/{key}", f"https://rpc.ankr.com/multichain/{key}"
    bot = args.bot.lower()
    mc = {}

    print("=" * 78)
    print(f"refund 探测   {bot}")
    print("=" * 78)

    callers, bot_txs = find_callers(multi, bot, args.cap)
    print(f"签名者 EOA: {len(callers)} 个(合约样本 {len(bot_txs)} 笔)")
    print("扫这些 EOA 的 ETH 入账…", file=sys.stderr)
    hits, checked = scan_refunds(eth, multi, set(callers), args.cap, mc)

    print(f"\n入账 {checked} 笔,其中 **付款人 == 该块出块人** 的有 {len(hits)} 笔:\n")
    if hits:
        hits.sort(key=lambda h: h["block"])
        print(f"  {'block':>10}  {'日期':16}  {'builder':14} {'→EOA':14} {'ETH':>11}")
        for h in hits:
            print(f"  {h['block']:>10}  "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(h['ts'])):16}  "
                  f"{h['builder'][:12]:14} {h['eoa'][:12]:14} {h['eth']:>11.6f}")
        tot = sum(h["eth"] for h in hits)
        by = collections.Counter()
        for h in hits:
            by[h["builder"]] += h["eth"]
        print(f"\n  refund 合计 {tot:.6f} ETH")
        for b, v in by.most_common():
            print(f"    {b}  {v:.6f} ETH")
        t0, t1 = min(h["ts"] for h in hits), max(h["ts"] for h in hits)
        print(f"  时间跨度 {time.strftime('%Y-%m-%d', time.gmtime(t0))} ~ "
              f"{time.strftime('%Y-%m-%d', time.gmtime(t1))}"
              f"  ({(t1 - t0) / 86400:.1f} 天)")
        quiet = (time.time() - t1) / 86400
        if quiet > 30:
            print(f"  ⚠️ **距今已 {quiet:.0f} 天没有新的 refund。**")
    else:
        tot = 0.0
        print("  没有。")

    print(f"\n对照:最近 {args.bid_sample} 笔的竞价支出(trace 实测)")
    bid, n, (s0, s1) = total_bids(eth, bot_txs, bot, args.bid_sample, mc)
    print(f"  竞价合计 {bid:.4f} ETH / {n} 笔   "
          f"({time.strftime('%Y-%m-%d', time.gmtime(s0))} ~ "
          f"{time.strftime('%Y-%m-%d', time.gmtime(s1))})")
    recent = [h for h in hits if h["ts"] >= s0]
    print(f"  同期 refund {sum(h['eth'] for h in recent):.6f} ETH / {len(recent)} 笔")
    print(f"\n  → 成本模型「构建者 refund」建议取值:"
          f"**{sum(h['eth'] for h in recent) / bid * 100 if bid else 0:.1f}%**(同期实测)")
    print("     没查到 ≠ 不存在 —— 只能说明**这些地址**没收到,详见文件头 ⚠️")
    print("=" * 78)

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"bot": bot, "callers": dict(callers), "refunds": hits,
             "refund_total_eth": tot, "bid_sample_n": n, "bid_total_eth": bid,
             "bid_window": [s0, s1]}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"已导出 {args.json}")


if __name__ == "__main__":
    main()
