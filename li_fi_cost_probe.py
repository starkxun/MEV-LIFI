#!/usr/bin/env python3
"""
LI.FI 成本探针 —— 链上套利残酷共学 · 课前准备 Day 0
--------------------------------------------------
目标:亲手看清一笔"跨链搬砖"到底要吃掉多少成本,
      从而算出"屏幕上的价差要多大才刚好回本"。

用法:
    pip install requests
    python li_fi_cost_probe.py

改这几个变量,试不同的链、资产、规模,感受成本怎么变。
LI.FI 报价接口是只读的,不需要 API key,不需要钱包里有钱。
文档: https://docs.li.fi/api-reference/introduction
"""

import requests

BASE = "https://li.quest/v1"

# ---- 你要试的这笔"搬砖" ----------------------------------
# chain 可以填链 ID(数字)或 LI.FI 的 chain key(如 'ARB','BAS')
FROM_CHAIN = "ARB"        # 从 Arbitrum
TO_CHAIN   = "BAS"        # 搬到 Base
FROM_TOKEN = "USDC"
TO_TOKEN   = "USDC"
FROM_AMOUNT_HUMAN = 10_000       # 搬 1 万 USDC,试试改成 100 / 100000 看成本占比怎么变
DECIMALS = 6                     # USDC 是 6 位小数
# 报价需要一个 fromAddress,随便一个合法地址即可(只读,不动你的钱)
FROM_ADDRESS = "0x552008c0f6870c2f77e5cC1d2eb9bdff03e30Ea0"
# ----------------------------------------------------------


def get_quote():
    params = {
        "fromChain": FROM_CHAIN,
        "toChain": TO_CHAIN,
        "fromToken": FROM_TOKEN,
        "toToken": TO_TOKEN,
        "fromAmount": str(FROM_AMOUNT_HUMAN * 10**DECIMALS),
        "fromAddress": FROM_ADDRESS,
    }
    r = requests.get(f"{BASE}/quote", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def usd(x):
    try:
        return f"${float(x):,.4f}"
    except (TypeError, ValueError):
        return "?"


def main():
    q = get_quote()
    est = q.get("estimate", {})

    frm = int(est.get("fromAmount", 0)) / 10**DECIMALS
    to  = int(est.get("toAmount", 0)) / 10**DECIMALS
    to_min = int(est.get("toAmountMin", 0)) / 10**DECIMALS
    print(f"\n== token 真相(别信美元)==")
    print(f"发出   : {frm:,.4f} {FROM_TOKEN}")
    print(f"到手   : {to:,.4f} {TO_TOKEN}   (最坏情况 toAmountMin: {to_min:,.4f})")
    print(f">> 真实成本: {frm - to:,.4f} {FROM_TOKEN}  =  {(frm-to)/frm*10_000:,.1f} bps")
    print(f">> 滑点缓冲: {to - to_min:,.4f} {TO_TOKEN}  (到手与保底之差)")

    tool = q.get("tool", "?")
    from_usd = float(est.get("fromAmountUSD", 0) or 0)
    to_usd   = float(est.get("toAmountUSD", 0) or 0)

    gas_usd = sum(float(g.get("amountUSD", 0) or 0) for g in est.get("gasCosts", []))
    fee_usd = sum(float(f.get("amountUSD", 0) or 0) for f in est.get("feeCosts", []))

    # LI.FI 已经把 gas + 桥费/协议费的影响算进了 toAmountUSD,
    # 所以最诚实的"总成本"就是:你投进去的美元价值 - 你到手的美元价值。
    total_cost = from_usd - to_usd
    cost_bps = (total_cost / from_usd * 10_000) if from_usd else 0

    print("=" * 56)
    print(f"路径工具(bridge): {tool}   用时约 {est.get('executionDuration','?')} 秒")
    print(f"投入   : {usd(from_usd)}  ({FROM_AMOUNT_HUMAN} {FROM_TOKEN} @ {FROM_CHAIN})")
    print(f"到手   : {usd(to_usd)}  ({TO_TOKEN} @ {TO_CHAIN})")
    print("-" * 56)
    print(f"  其中 gas 成本 : {usd(gas_usd)}")
    print(f"  其中 费用成本 : {usd(fee_usd)}   (桥费 + 协议费)")
    print(f"  滑点缓冲       : toAmountMin 比 toAmount 少的部分(自己去 JSON 里看)")
    print("=" * 56)
    print(f">> 净成本(投入-到手): {usd(total_cost)}  =  {cost_bps:,.1f} bps")
    print(f">> 也就是说:目标链上的价差 至少要 > {cost_bps:,.1f} bps")
    print(f"   (还没算你自己的执行延迟、失败交易、资金占用)才可能赚钱。")
    print("=" * 56)
    print("\n想深挖?把整个 JSON 打印出来,找找这些字段:")
    print("  estimate.feeCosts[] / gasCosts[] / toAmountMin / slippage / includedSteps[]")


if __name__ == "__main__":
    main()