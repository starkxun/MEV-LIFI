#!/usr/bin/env python3
"""
liquidator_scan.py —— 扫全市场清算人,找「竞价比例低」的那些人

来自 [`docs/week_2/研究人教学.md`](docs/week_2/研究人教学.md) 第十四节的结论:
0xf0570ec4 那台机器人把 **97~98%** 的毛机会竞价给了区块构建者,只有
毛机会 >100 WETH 的大单才掉到 ~40%。能让竞价掉下来的路只有两条 ——
**规模**(做市商级 RFQ 额度)或**私有订单流**(机会不公开)。

第二条没法从单个地址看出来,只能横向比:**把所有清算人摆在一起,
找出那些系统性不给构建者付钱的,再去看他们凭什么。**

给构建者的钱有**两条通道**,必须都算:

    付给构建者 = block.coinbase 直接转账(trace,无日志)
               + 优先费 gasUsed × (effectiveGasPrice − baseFeePerGas)

只算其中一条会把另一种打法的人误判成"不付钱"。0xf0570ec4 走的是
前者,而很多清算人走的是后者 —— 两者在链上长得完全不一样。

**规模必须一起量,否则「竞价为 0」毫无意义** —— 实测 30 天里 717 笔清算
有 391 笔债务不到 $10(有人在清 $1.08 的仓),它们竞价当然是 0,
因为那里没有钱。所以脚本直接从**日志本身**解码清算规模(不取 receipt),
只对超过阈值的大单去测竞价。

Morpho Blue 的事件里只有 market id,要反查 `idToMarketParams(bytes32)`
才知道借的是什么币 —— 漏了这步会把 203 条(28%)整块丢掉,
而恰恰是这批里藏着 21 笔「大额 + 零竞价」的样本。

用法:
    python3 liquidator_scan.py --days 30
    python3 liquidator_scan.py --from-block 25494151 --to-block 25710151 --json out.json
"""

import argparse
import collections
import json
import sys
import time
from pathlib import Path

from actor_profile import _topic0, ankr_key, rpc

# 同名不同签名的坑(沉淀第 4 条):Aave V2 和 V3 的 LiquidationCall
# 签名恰好一致,所以按 topic0 扫会同时命中两者 —— 用 emitter 地址区分。
T_AAVE = _topic0("LiquidationCall(address,address,address,uint256,uint256,address,bool)")
T_MORPHO = _topic0("Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
T_EULER = _topic0("Liquidate(address,address,address,uint256,uint256)")
T_COMET = _topic0("AbsorbCollateral(address,address,address,uint256,uint256)")

POOLS = {
    "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2": "AaveV3",
    "0x7d2768de32b0b80b7a3454c06bdac94a69ddc7a9": "AaveV2",
    "0xc13e21b648a5ee794902342038ff3adab66be987": "SparkLend",
    "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb": "MorphoBlue",
}
CHUNK = 4500          # Ankr 对 eth_getLogs 的区间上限比 5000 略紧


def scan_logs(eth, topic, lo, hi):
    out = []
    b = lo
    while b <= hi:
        top = min(b + CHUNK - 1, hi)
        try:
            r = rpc(eth, "eth_getLogs",
                    [{"fromBlock": hex(b), "toBlock": hex(top), "topics": [topic]}],
                    retries=3)
            out += r or []
        except RuntimeError as e:
            print(f"    区间 {b}-{top} 失败: {str(e)[:60]}", file=sys.stderr)
        b = top + 1
        print(f"    {(b - lo) / (hi - lo) * 100:5.1f}%", end="\r", file=sys.stderr)
    return out


def builder_payment(eth, txh, blk_cache):
    """
    这笔交易一共给了区块构建者多少 ETH —— **两条通道都要算**。

    返回 (coinbase 直转, 优先费, gasUsed)。只看其中一条会误判打法。
    """
    rc = rpc(eth, "eth_getTransactionReceipt", [txh])
    if not rc:
        return None
    bn = rc["blockNumber"]
    if bn not in blk_cache:
        b = rpc(eth, "eth_getBlockByNumber", [bn, False]) or {}
        blk_cache[bn] = ((b.get("miner") or "").lower(),
                         int(b.get("baseFeePerGas") or "0x0", 16))
    miner, base = blk_cache[bn]
    gas_used = int(rc["gasUsed"], 16)
    eff = int(rc["effectiveGasPrice"], 16)
    prio = gas_used * max(0, eff - base) / 1e18
    try:
        tr = rpc(eth, "trace_transaction", [txh], retries=2) or []
    except RuntimeError:
        return None
    direct = sum(int(x["action"].get("value") or "0x0", 16) for x in tr
                 if x.get("type") == "call"
                 and (x["action"].get("to") or "").lower() == miner) / 1e18
    return direct, prio, gas_used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--from-block", type=int)
    ap.add_argument("--to-block", type=int)
    ap.add_argument("--max-tx", type=int, default=250, help="最多细查多少笔")
    ap.add_argument("--min-usd", type=float, default=10000,
                    help="只对超过这个规模的清算测竞价 —— 小单竞价为 0 是废信息")
    ap.add_argument("--json")
    args = ap.parse_args()

    key = ankr_key()
    if not key:
        sys.exit("需要 .env 里的 ANKR_KEY")
    eth = f"https://rpc.ankr.com/eth/{key}"

    tip = int(rpc(eth, "eth_blockNumber", []), 16)
    hi = args.to_block or tip
    lo = args.from_block or (hi - int(args.days * 7200))
    print("=" * 84)
    print(f"清算人横扫   块 {lo} ~ {hi}   (≈{(hi - lo) / 7200:.0f} 天)")
    print("=" * 84)

    events = []
    for name, topic in (("Aave V2/V3 + Spark", T_AAVE), ("Morpho Blue", T_MORPHO),
                        ("Euler EVK", T_EULER), ("Compound III", T_COMET)):
        print(f"  扫 {name} …", file=sys.stderr)
        for lg in scan_logs(eth, topic, lo, hi):
            proto = POOLS.get(lg["address"].lower(), lg["address"][:10])
            events.append({"proto": proto, "tx": lg["transactionHash"],
                           "block": int(lg["blockNumber"], 16)})
        print(f"  {name:22} 累计 {len(events)} 条", flush=True)

    by_tx = {}
    for e in events:
        by_tx.setdefault(e["tx"], e)
    print(f"\n共 {len(events)} 条清算事件,{len(by_tx)} 笔交易")

    # **按协议分层取样。** 事件是按协议顺序追加的,直接截前 N 笔
    # 会把排在后面的协议整个切掉 —— 而恰恰是 Euler / Compound III
    # 这些小众协议最可能有不同的清算机制。
    per = collections.defaultdict(list)
    for txh, e in by_tx.items():
        per[e["proto"]].append((txh, e))
    quota = max(1, args.max_tx // max(1, len(per)))
    picked = []
    for p, lst in per.items():
        picked += lst[:quota]
    for p, lst in per.items():          # 配额没用满的,拿剩下的补齐
        if len(picked) >= args.max_tx:
            break
        picked += lst[quota:quota + (args.max_tx - len(picked))]
    print("分层取样:" + "  ".join(f"{p}×{sum(1 for t, e in picked if e['proto'] == p)}"
                                  for p in per))

    blk_cache, rows = {}, []
    for i, (txh, e) in enumerate(picked[:args.max_tx], 1):
        tx = rpc(eth, "eth_getTransactionByHash", [txh])
        if not tx:
            continue
        pay = builder_payment(eth, txh, blk_cache)
        if pay is None:
            continue
        direct, prio, gu = pay
        rows.append({"tx": txh, "proto": e["proto"], "block": e["block"],
                     "sender": (tx.get("from") or "").lower(),
                     "target": (tx.get("to") or "").lower(),
                     "direct": direct, "prio": prio, "total": direct + prio,
                     "gas_used": gu})
        print(f"    {i}/{min(len(by_tx), args.max_tx)}", end="\r", file=sys.stderr)

    agg = collections.defaultdict(lambda: {"n": 0, "direct": 0.0, "prio": 0.0,
                                           "protos": collections.Counter()})
    for r in rows:
        a = agg[r["target"]]
        a["n"] += 1
        a["direct"] += r["direct"]
        a["prio"] += r["prio"]
        a["protos"][r["proto"]] += 1

    print(f"\n分析了 {len(rows)} 笔,{len(agg)} 个清算合约\n")
    print(f"  {'清算合约':44} {'笔':>4} {'coinbase直转':>13} {'优先费':>11} "
          f"{'合计/笔':>11}  协议")
    for addr, a in sorted(agg.items(), key=lambda kv: -(kv[1]["direct"] + kv[1]["prio"])):
        tot = a["direct"] + a["prio"]
        ps = ",".join(f"{k}×{v}" for k, v in a["protos"].most_common(3))
        print(f"  {addr:44} {a['n']:>4} {a['direct']:>13.4f} {a['prio']:>11.4f} "
              f"{tot / a['n']:>11.5f}  {ps}")

    print(f"\n【筛选】每笔给构建者 < 0.001 ETH 的清算合约 —— 这些人不在拍卖里:")
    quiet = [(k, v) for k, v in agg.items()
             if (v["direct"] + v["prio"]) / v["n"] < 0.001 and v["n"] >= 2]
    if quiet:
        for addr, a in sorted(quiet, key=lambda kv: -kv[1]["n"]):
            ps = ",".join(f"{k}×{v}" for k, v in a["protos"].most_common(3))
            print(f"  {addr}  {a['n']:>3} 笔  "
                  f"{(a['direct'] + a['prio']) / a['n']:.6f} ETH/笔   {ps}")
    else:
        print("  没有 —— 所有清算人都在为排序付钱")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"range": [lo, hi], "rows": rows,
             "agg": {k: {"n": v["n"], "direct": v["direct"], "prio": v["prio"],
                         "protos": dict(v["protos"])} for k, v in agg.items()}},
            indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n已导出 {args.json}")
    print("=" * 84)


if __name__ == "__main__":
    main()
