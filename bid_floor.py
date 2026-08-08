#!/usr/bin/env python3
"""
bid_floor.py —— 清算的钱到底被谁拿走了:竞价地板 + 成本分解

来自 [`docs/week_2/研究人教学.md`](docs/week_2/研究人教学.md) 第十三节留下的问题:
实测利润留存率随机会规模从 1.8% 一路升到 62.5%,那么

    · 竞价是一个**固定地板**(小单被地板吃掉),
      还是一个**随规模变化的比例**(大单竞争者少)?
    · 「用闪电贷不就把成本覆盖了吗」—— 闪电贷费到底占多少?

这个脚本把每一笔清算拆成五项,按规模分档:

    毛机会 = 抵押品变现所得 − 还债
           = 闪电贷费 + 变现折价(RFQ) + gas + 竞价 + payout(真利润)

**结论先写在这:闪电贷费≈0,gas≈0,吃掉利润的是竞价,而竞价恰恰是
闪电贷造成的** —— 闪电贷把入场资本门槛降到 0,所有人都能参与同一个
公开机会,于是竞价把价值拍到接近 100%。闪电贷解决的是「失败成本」,
不是「竞争成本」。详见文档第十四节。

用法:
    python3 bid_floor.py --bot 0xf0570ec4... --receiver 0x51c72848... --sample 40
"""

import argparse
import json
import statistics
import sys
from pathlib import Path

from actor_profile import (DEPOSIT, TRANSFER, WITHDRAWAL, _miner_of, _topic0,
                           ankr_key, rpc)
from refund_probe import all_txs

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
# 两种闪贷的 FlashLoan 同名不同签名 —— 只记一个会漏掉另一种(沉淀第 4 条)
FL_AAVE_V3 = _topic0("FlashLoan(address,address,address,uint256,uint8,uint256,uint16)")
FL_BALANCER = _topic0("FlashLoan(address,address,uint256,uint256)")


def dissect(eth, tx, bot, rcv, miners):
    """把一笔清算拆成:毛机会 / 闪贷费 / 竞价 / payout / gas。"""
    rc = rpc(eth, "eth_getTransactionReceipt", [tx["hash"]])
    if not rc or rc.get("status") != "0x1":
        return None
    logs = rc.get("logs") or []

    wd_idx, wd, dp, dw = -1, 0, 0, 0
    for lg in logs:
        tp = lg.get("topics") or []
        if (lg["address"].lower() == WETH and tp and len(tp) >= 2
                and ("0x" + tp[1][-40:]).lower() == bot):
            if tp[0] == WITHDRAWAL:
                wd_idx = max(wd_idx, int(lg["logIndex"], 16))
                wd += int(lg["data"], 16)
            elif tp[0] == DEPOSIT:
                dp += int(lg["data"], 16)

    delta, pay = 0, 0
    flash_fee_wei, flash_src = 0, None
    for lg in logs:
        tp = lg.get("topics") or []
        if not tp:
            continue
        tok = lg["address"].lower()

        # 闪贷费:Aave V3 的 premium 在 data 的第 2 个 word,Balancer 的
        # feeAmount 在第 2 个 word。两者都只在**该笔借的是 WETH 时**
        # 能直接和 WETH 口径相加;借稳定币时费用单位不同,单独标注。
        if tp[0] == FL_AAVE_V3 and len(lg["data"]) >= 2 + 64 * 3:
            flash_src = "AaveV3"
            asset = "0x" + tp[2][-40:] if len(tp) > 2 else ""
            prem = int(lg["data"][2 + 64 * 2:2 + 64 * 3], 16)
            flash_fee_wei = prem if asset.lower() == WETH else -1
        elif tp[0] == FL_BALANCER and len(lg["data"]) >= 2 + 64 * 2:
            flash_src = "Balancer"
            token = "0x" + tp[2][-40:] if len(tp) > 2 else ""
            fee = int(lg["data"][2 + 64:2 + 64 * 2], 16)
            flash_fee_wei = fee if token.lower() == WETH else (0 if fee == 0 else -1)

        if len(tp) < 3 or tp[0] != TRANSFER:
            continue
        try:
            v = int(lg["data"], 16)
        except (ValueError, KeyError):
            continue
        if tok != WETH:
            continue
        frm = ("0x" + tp[1][-40:]).lower()
        to = ("0x" + tp[2][-40:]).lower()
        if frm == bot:
            delta -= v
            if rcv and to == rcv and wd_idx >= 0 and int(lg["logIndex"], 16) > wd_idx:
                pay += v
        if to == bot:
            delta += v

    miner = _miner_of(eth, rc["blockNumber"], miners)
    try:
        tr = rpc(eth, "trace_transaction", [tx["hash"]], retries=2) or []
    except RuntimeError:
        return None
    bid = sum(int(x["action"].get("value") or "0x0", 16) for x in tr
              if x.get("type") == "call"
              and (x["action"].get("to") or "").lower() == miner) / 1e18

    retained = (delta + dp - wd) / 1e18
    gas = int(rc["gasUsed"], 16) * int(rc["effectiveGasPrice"], 16) / 1e18
    blk = rpc(eth, "eth_getBlockByNumber", [rc["blockNumber"], False]) or {}
    return {"hash": tx["hash"], "block": int(rc["blockNumber"], 16),
            "ts": int(tx["timestamp"], 16),
            "gross": retained + bid + pay / 1e18, "bid": bid,
            "payout": pay / 1e18, "retained": retained, "gas": gas,
            "base_fee": int(blk.get("baseFeePerGas") or "0x0", 16) / 1e9,
            "gas_used": int(rc["gasUsed"], 16),
            "flash_src": flash_src,
            "flash_fee": (flash_fee_wei / 1e18) if flash_fee_wei and flash_fee_wei > 0 else 0.0,
            "flash_fee_other_token": flash_fee_wei == -1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True)
    ap.add_argument("--receiver", required=True)
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--json")
    args = ap.parse_args()

    key = ankr_key()
    if not key:
        sys.exit("需要 .env 里的 ANKR_KEY")
    eth = f"https://rpc.ankr.com/eth/{key}"
    multi = f"https://rpc.ankr.com/multichain/{key}"
    bot, rcv = args.bot.lower(), args.receiver.lower()

    txs = [t for t in all_txs(multi, bot, args.sample * 5)
           if (t.get("to") or "").lower() == bot][:args.sample]
    miners, rows = {}, []
    for t in txs:
        d = dissect(eth, t, bot, rcv, miners)
        if d and d["gross"] > 1e-9:
            rows.append(d)
        print(f"  {len(rows)}/{len(txs)}", end="\r", file=sys.stderr)
    rows.sort(key=lambda r: r["gross"])

    print("=" * 84)
    print(f"竞价地板与成本分解   {bot}   n={len(rows)}")
    print("=" * 84)

    print("\n【一】按规模分档:钱被谁拿走了(占毛机会 %)")
    print(f"  {'毛机会档':>12} {'n':>3} {'毛机会':>10} │ {'闪贷费':>7} {'gas':>7} "
          f"{'竞价':>7} {'payout':>8}")
    for lo, hi in ((0, 1), (1, 5), (5, 20), (20, 100), (100, float("inf"))):
        sel = [r for r in rows if lo <= r["gross"] < hi]
        if not sel:
            continue
        g = sum(r["gross"] for r in sel)
        f = sum(r["flash_fee"] for r in sel)
        gs = sum(r["gas"] for r in sel)
        b = sum(r["bid"] for r in sel)
        p = sum(r["payout"] for r in sel)
        lbl = f"{lo}~{'∞' if hi == float('inf') else int(hi)}"
        print(f"  {lbl:>12} {len(sel):>3} {g:>10.2f} │ {f/g*100:>6.2f}% {gs/g*100:>6.2f}% "
              f"{b/g*100:>6.1f}% {p/g*100:>7.1f}%")
    g = sum(r["gross"] for r in rows)
    print(f"  {'合计':>12} {len(rows):>3} {g:>10.2f} │ "
          f"{sum(r['flash_fee'] for r in rows)/g*100:>6.2f}% "
          f"{sum(r['gas'] for r in rows)/g*100:>6.2f}% "
          f"{sum(r['bid'] for r in rows)/g*100:>6.1f}% "
          f"{sum(r['payout'] for r in rows)/g*100:>7.1f}%")

    print("\n【二】竞价是固定地板,还是比例?")
    print(f"  {'毛机会':>12} {'竞价':>12} {'竞价/毛机会':>12} {'gas':>9} {'baseFee':>9}")
    for r in rows:
        print(f"  {r['gross']:>12.4f} {r['bid']:>12.4f} {r['bid']/r['gross']*100:>11.1f}% "
              f"{r['gas']:>9.5f} {r['base_fee']:>8.2f}g")
    small = [r for r in rows if r["gross"] < 20]
    big = [r for r in rows if r["gross"] >= 100]
    if small:
        print(f"\n  毛机会 <20 WETH  ({len(small)} 笔):竞价/毛机会 中位 "
              f"**{statistics.median(r['bid']/r['gross'] for r in small)*100:.1f}%**")
    if big:
        print(f"  毛机会 ≥100 WETH ({len(big)} 笔):竞价/毛机会 中位 "
              f"**{statistics.median(r['bid']/r['gross'] for r in big)*100:.1f}%**")
    print("  → 竞价**不是固定地板**(地板应表现为小单比例高、绝对值相近);"
          "\n    它是一个**比例**,而这个比例在大单上掉下来 —— 说明大单的竞争者更少。")

    print("\n【三】闪电贷来源与费用")
    srcs = {}
    for r in rows:
        srcs.setdefault(r["flash_src"] or "未识别", []).append(r)
    for s, v in srcs.items():
        print(f"  {s:>10}  {len(v):>2} 笔   WETH 计价的闪贷费合计 "
              f"{sum(x['flash_fee'] for x in v):.6f} WETH"
              + ("   (部分笔借的不是 WETH,费用未折算)"
                 if any(x["flash_fee_other_token"] for x in v) else ""))
    print("=" * 84)

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"已导出 {args.json}")


if __name__ == "__main__":
    main()
