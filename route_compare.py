#!/usr/bin/env python3
"""
route_compare.py —— 比较 /advanced/routes 的全部候选路径,把「成本 vs 延迟」摊开

为什么需要它:

`/quote` 只返回**它认为最好的一条**,而它的判据基本只有一个 —— **到手金额**。
它**不给延迟定价**。

但对套利来说延迟就是成本:价差可能在你资金在途时消失。
实测同一条 ARB→BAS USDC 路径的候选:

    Eco         9,975.00 USDC      7 秒   ← CHEAPEST
    Polymer     9,975.00 USDC   1080 秒   ← 到手一模一样,慢 154 倍
    AcrossV4    9,974.00 USDC      1 秒   ← 贵 1 bps,快 6 秒

`/quote` 给你 Eco。但如果你的价差窗口只有 3 秒,**你应该主动多付 1 bps 走 AcrossV4**。
这个决定 `/quote` 替你做不了,因为它不知道你的窗口有多长。

本脚本干三件事:
  1. 把所有候选按 token 口径算成本,和耗时并排
  2. 标出**被严格支配**的路径(到手不更多、还更慢 —— 任何情况下都不该选)
  3. 算**延迟溢价**:比最便宜那条快 1 秒,要多付多少 bps

用法:
    python3 route_compare.py --from-chain ARB --to-chain BAS --token USDC --amount 10000
    python3 route_compare.py --from-chain ARB --to-chain BAS \\
        --from-token USDC --to-token WETH --amount 10000
    python3 route_compare.py ... --window 30      # 我的价差窗口只有 30 秒
    python3 route_compare.py ... --json out.json

    # 同链往返不变量检查 —— 揪出虚报产出的报价源
    python3 route_compare.py --sanity --chain BAS --base USDC --quote WETH --amount 1000

注意 `/advanced/routes` 的限速和 `/quote` 一样紧(未认证约 75 次/2 小时),
别拿它当轮询接口用。
"""

import argparse
import json
import sys
import time

import requests

BASE = "https://li.quest/v1"
DEFAULT_ADDRESS = "0x552008c0f6870c2f77e5cC1d2eb9bdff03e30Ea0"


def get_token(chain, sym, retries=4):
    """token 元数据。和项目里其它脚本一样:走网络就要重试。"""
    delay = 1.0
    last = None
    for i in range(retries):
        try:
            r = requests.get(f"{BASE}/token",
                             params={"chain": chain, "token": sym}, timeout=25)
            r.raise_for_status()
            d = r.json()
            return {"address": d["address"], "decimals": int(d["decimals"]),
                    "symbol": d["symbol"], "chainId": int(d["chainId"])}
        except (requests.RequestException, ValueError, KeyError) as e:
            last = e
            if i < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"取 {chain}/{sym} 失败: {last}")


def fetch_routes(fb, tb, raw_amount, address, retries=4):
    payload = {
        "fromChainId": fb["chainId"], "toChainId": tb["chainId"],
        "fromTokenAddress": fb["address"], "toTokenAddress": tb["address"],
        "fromAmount": str(int(raw_amount)), "fromAddress": address,
    }
    delay = 1.0
    last = None
    for i in range(retries):
        try:
            r = requests.post(f"{BASE}/advanced/routes", json=payload, timeout=60)
            if r.status_code == 429:
                wait = float(r.headers.get("ratelimit-reset", delay) or delay)
                print(f"  [限速] 等 {wait:.0f}s…", file=sys.stderr)
                time.sleep(min(wait, 65))
                continue
            r.raise_for_status()
            return r.json().get("routes", [])
        except (requests.RequestException, ValueError) as e:
            last = e
            if i < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise RuntimeError(f"拉取候选路径失败: {last}")


def parse_route(rt, fb, tb):
    steps = rt.get("steps", [])
    tools = " → ".join(
        s.get("toolDetails", {}).get("name") or s.get("tool", "?") for s in steps)
    dur = sum(s.get("estimate", {}).get("executionDuration", 0) or 0 for s in steps)

    gas = sum(float(g.get("amountUSD") or 0)
              for s in steps for g in s.get("estimate", {}).get("gasCosts", []))
    # included=False 的费用是另外掏钱的,必须加;included=True 已在 toAmount 里扣掉
    extra_fee = sum(float(f.get("amountUSD") or 0)
                    for s in steps for f in s.get("estimate", {}).get("feeCosts", [])
                    if f.get("included") is False)

    frm = int(rt["fromAmount"]) / 10 ** fb["decimals"]
    to = int(rt["toAmount"]) / 10 ** tb["decimals"]
    to_min = int(rt["toAmountMin"]) / 10 ** tb["decimals"]

    return {
        "tools": tools, "duration": dur, "to": to, "to_min": to_min, "from": frm,
        "gas_usd": gas, "extra_fee_usd": extra_fee,
        "tags": rt.get("tags") or [],
        "steps": len(steps),
        # 保底缓冲:预期到手和最低到手之差。为负说明上游数据自相矛盾。
        "buffer_bps": (to - to_min) / to * 10_000 if to else 0,
    }


def lifi_fee_in_input(rt, fb):
    """
    取出 LI.FI 自己那笔从**输入端**扣的服务费(人类可读单位)。

    只剥离 LI.FI 的抽成 —— DEX 协议费(如 Sushi Protocol Fee)是**真实成本**,
    不能剥。剥错了就是把亏损伪装成盈利。
    """
    tot = 0.0
    for s in rt.get("steps", []):
        for f in s.get("estimate", {}).get("feeCosts", []):
            name = (f.get("name") or "").upper()
            if "LIFI" not in name and "LI.FI" not in name:
                continue
            if not f.get("included"):
                continue
            tok = f.get("token", {}) or {}
            if (tok.get("symbol") or "").upper() != fb["symbol"].upper():
                continue      # 不是从输入端扣的,跳过
            tot += int(f.get("amount", 0)) / 10 ** int(tok.get("decimals", fb["decimals"]))
    return tot


def zero_fee_out(rt, fb, tb):
    """
    把 LI.FI 抽成加回去,得到"零费率口径"的产出。

    LI.FI 是先从输入里扣走服务费,再拿剩下的去换。所以零费率产出 ≈
        toAmount × fromAmount / (fromAmount − fee)
    (小规模下价格冲击近似线性,这个外推是合理的)
    """
    frm = int(rt["fromAmount"]) / 10 ** fb["decimals"]
    to = int(rt["toAmount"]) / 10 ** tb["decimals"]
    fee = lifi_fee_in_input(rt, fb)
    if fee <= 0 or frm - fee <= 0:
        return to, frm
    return to * frm / (frm - fee), frm


def sanity_roundtrip(chain, base_sym, quote_sym, amount, address, top=6):
    """
    同链买卖往返 —— 一个能**定位坏数据源**的不变量。

    原理:在同一条链上买入后立刻卖出,必然亏掉双边 DEX 成本。
    **任何正值都意味着报价体系自相矛盾**,不是赚钱机会。

    这比"toAmountMin > toAmount"那种单腿内部检查强在:
    按(买腿路由 × 卖腿路由)拆开之后,能看出是**哪个源**在虚报产出。

    方法来自共学群一份 120 轮实测(2026-08-06),他们据此揪出 fly/Magpie
    在 Base 上自报买卖价交叉 +44.5 bps —— 等于"低买高卖自己"。
    """
    b = get_token(chain, base_sym)
    q = get_token(chain, quote_sym)

    buy_raw = int(round(amount * 10 ** b["decimals"]))
    buy_routes = fetch_routes(b, q, buy_raw, address)
    if not buy_routes:
        raise RuntimeError(f"{chain} 上 {base_sym}→{quote_sym} 无路由")

    # 用买腿产出的中位数作为卖腿的输入,再把各卖腿的汇率外推到每个买腿
    mid = sorted(int(r["toAmount"]) for r in buy_routes)[len(buy_routes) // 2]
    sell_routes = fetch_routes(q, b, mid, address)
    if not sell_routes:
        raise RuntimeError(f"{chain} 上 {quote_sym}→{base_sym} 无路由")

    buys, sells = [], []
    for r in buy_routes[:top]:
        z, frm = zero_fee_out(r, b, q)
        buys.append({
            "tool": r["steps"][0].get("toolDetails", {}).get("name")
                    or r["steps"][0].get("tool", "?"),
            "rate_now": (int(r["toAmount"]) / 10 ** q["decimals"]) / frm,
            "rate_zero": z / frm,
        })
    for r in sell_routes[:top]:
        z, frm = zero_fee_out(r, q, b)
        sells.append({
            "tool": r["steps"][0].get("toolDetails", {}).get("name")
                    or r["steps"][0].get("tool", "?"),
            "rate_now": (int(r["toAmount"]) / 10 ** b["decimals"]) / frm,
            "rate_zero": z / frm,
        })

    pairs = []
    for x in buys:
        for y in sells:
            pairs.append({
                "buy": x["tool"], "sell": y["tool"],
                "now_bps": (x["rate_now"] * y["rate_now"] - 1) * 10_000,
                "zero_bps": (x["rate_zero"] * y["rate_zero"] - 1) * 10_000,
            })
    return b, q, buys, sells, pairs


def run_sanity(args):
    b, q, buys, sells, pairs = sanity_roundtrip(
        args.chain, args.base, args.quote, args.amount, args.address, args.top)

    print()
    print("=" * 88)
    print(f"同链往返不变量检查   {args.chain}   "
          f"{b['symbol']} → {q['symbol']} → {b['symbol']}   规模 {args.amount:,.0f} {b['symbol']}")
    print("=" * 88)
    print("原理:同链买完立刻卖,必然亏掉双边成本。**任何正值都说明报价体系自相矛盾。**")
    print()

    print(f"{'买腿路由':22} {'卖腿路由':22} {'现状bps':>10} {'零费率bps':>11}   判定")
    print("-" * 88)
    for p in sorted(pairs, key=lambda x: -x["zero_bps"]):
        flag = "  ⚠ 越过零线,数据不自洽" if p["zero_bps"] > 0 else ""
        print(f"{p['buy'][:22]:22} {p['sell'][:22]:22} "
              f"{p['now_bps']:>10.2f} {p['zero_bps']:>11.2f}{flag}")

    bad = [p for p in pairs if p["zero_bps"] > 0]
    print("-" * 88)
    if not bad:
        print(">> 全部配对都为负 —— 报价体系自洽,没有发现虚报产出的源。")
    else:
        print(f">> ⚠ {len(bad)}/{len(pairs)} 个配对越过零线。")
        # 按工具归因:某个工具只要出现就越线,它就是嫌疑源
        involved = {}
        for p in pairs:
            for role, t in (("买", p["buy"]), ("卖", p["sell"])):
                d = involved.setdefault(t, {"n": 0, "pos": 0})
                d["n"] += 1
                if p["zero_bps"] > 0:
                    d["pos"] += 1
        print(f"\n   按工具归因(该工具参与的配对里有多少越线):")
        for t, d in sorted(involved.items(), key=lambda x: -x[1]["pos"] / x[1]["n"]):
            ratio = d["pos"] / d["n"] * 100
            mark = "  ← 嫌疑源" if ratio > 60 else ""
            print(f"     {t[:26]:26} {d['pos']:>3}/{d['n']:<3} ({ratio:>3.0f}%){mark}")
        print("\n   判读:某个工具**参与的配对几乎全部越线**,而不含它的配对都正常 ——")
        print("         那就是它在虚报 toAmount。这类源应当隔离(**显式记录,不要静默剔除**)。")
        print("   注意:虚报产出的源会**赢得路由竞争**,所以它更容易出现在 /quote 的结果里。")

    print("\n口径说明:")
    print("  现状bps  = 含 LI.FI 服务费的真实往返损耗(必然很负,双边各抽一次)")
    print("  零费率bps = 把 LI.FI 抽成加回去(只剥它自己的,DEX 协议费是真实成本不剥)")
    print("             —— 用这一列判断自洽性,因为费率会把所有配对一起压低,掩盖异常")
    print("=" * 88)
    return 0


def mark_dominated(rows):
    """
    严格支配:存在另一条路,到手 >= 且耗时 <=,且至少一项更好。
    被支配的路**任何情况下都不该选** —— 不管你的延迟偏好是什么。
    """
    for a in rows:
        a["dominated_by"] = None
        for b in rows:
            if b is a:
                continue
            if (b["to"] >= a["to"] and b["duration"] <= a["duration"]
                    and (b["to"] > a["to"] or b["duration"] < a["duration"])):
                a["dominated_by"] = b["tools"]
                break
    return rows


def main():
    p = argparse.ArgumentParser(
        description="比较 /advanced/routes 的全部候选:成本 vs 延迟")
    p.add_argument("--from-chain")
    p.add_argument("--to-chain")
    p.add_argument("--token", help="同资产简写")
    p.add_argument("--from-token")
    p.add_argument("--to-token")
    p.add_argument("--amount", type=float, default=10000)
    p.add_argument("--address", default=DEFAULT_ADDRESS)
    p.add_argument("--window", type=float,
                   help="你的价差窗口(秒)。给了就筛掉来不及的路径")
    p.add_argument("--top", type=int, default=12, help="最多显示几条")
    p.add_argument("--json", help="导出原始解析结果")
    p.add_argument("--sanity", action="store_true",
                   help="同链往返不变量检查:揪出虚报产出的报价源")
    p.add_argument("--chain", help="--sanity 模式用:在哪条链上做往返")
    p.add_argument("--base", default="USDC", help="--sanity 模式的计价资产")
    p.add_argument("--quote", default="WETH", help="--sanity 模式的标的资产")
    args = p.parse_args()

    if args.sanity:
        if not args.chain:
            p.error("--sanity 需要 --chain,比如 --sanity --chain BAS")
        return run_sanity(args)

    if args.token:
        args.from_token = args.from_token or args.token
        args.to_token = args.to_token or args.token
    if not (args.from_chain and args.to_chain):
        p.error("需要 --from-chain 和 --to-chain(或用 --sanity --chain 做往返检查)")
    if not args.from_token:
        p.error("必须给 --token 或 --from-token")
    args.to_token = args.to_token or args.from_token

    fb = get_token(args.from_chain, args.from_token)
    tb = get_token(args.to_chain, args.to_token)
    raw = int(round(args.amount * 10 ** fb["decimals"]))

    routes = fetch_routes(fb, tb, raw, args.address)
    if not routes:
        print("没有可用候选路径 —— 这条路当前不通,本身就是结论。", file=sys.stderr)
        return 1

    rows = [parse_route(r, fb, tb) for r in routes]
    same_asset = fb["symbol"].upper() == tb["symbol"].upper()
    best_to = max(r["to"] for r in rows)

    for r in rows:
        # 同资产才能算绝对的 token 口径成本(零价格假设)
        r["cost_bps"] = ((r["from"] - r["to"]) / r["from"] * 10_000
                         if same_asset else None)
        # 跨资产时用"比最优候选少收了多少"做相对比较 —— 同输入同输出币,可比
        r["vs_best_bps"] = (best_to - r["to"]) / best_to * 10_000

    rows.sort(key=lambda r: -r["to"])
    mark_dominated(rows)

    print()
    print("=" * 92)
    print(f"候选路径比较   {args.from_chain}.{fb['symbol']} → "
          f"{args.to_chain}.{tb['symbol']}   规模 {args.amount:,.0f} {fb['symbol']}")
    print(f"共 {len(rows)} 条候选" + (f"(显示前 {args.top} 条)" if len(rows) > args.top else ""))
    print("=" * 92)

    hdr = f"{'到手':>14} "
    hdr += f"{'成本bps':>9} " if same_asset else f"{'比最优差':>9} "
    hdr += f"{'耗时':>7} {'gas$':>8} {'步':>3}  路径"
    print(hdr)
    print("-" * 92)

    shown = rows[:args.top]
    for r in shown:
        flag = ""
        if r["dominated_by"]:
            flag = " ✗被支配"
        elif "CHEAPEST" in r["tags"]:
            flag = " ←最便宜"
        elif "FASTEST" in r["tags"]:
            flag = " ←最快"
        if args.window and r["duration"] > args.window:
            flag += " ⏱超窗口"
        metric = r["cost_bps"] if same_asset else r["vs_best_bps"]
        print(f"{r['to']:>14,.4f} {metric:>9.2f} {r['duration']:>6,d}s "
              f"{r['gas_usd']:>8.4f} {r['steps']:>3}  {r['tools'][:34]:34}{flag}")

    dominated = [r for r in rows if r["dominated_by"]]
    live = [r for r in rows if not r["dominated_by"]]

    print("-" * 92)
    print(f">> {len(dominated)}/{len(rows)} 条被严格支配"
          f"(到手不更多、还更慢 —— 任何延迟偏好下都不该选)")

    # ---- 延迟溢价:核心产出 ----
    cheapest = max(live, key=lambda r: r["to"])
    faster = [r for r in live if r["duration"] < cheapest["duration"]]
    if faster:
        print()
        print(f">> 延迟溢价(相对最便宜的 {cheapest['tools'][:24]},"
              f"{cheapest['duration']}s):")
        print(f"   {'快多少':>8} {'多付bps':>9} {'每省1秒的代价':>15}  路径")
        for r in sorted(faster, key=lambda x: x["duration"]):
            saved = cheapest["duration"] - r["duration"]
            extra = (cheapest["to"] - r["to"]) / cheapest["to"] * 10_000
            per_s = extra / saved if saved else 0
            print(f"   {saved:>7,d}s {extra:>9.2f} {per_s:>15.3f}  {r['tools'][:30]}")
        print("   → 你的价差窗口越短,越值得多付这几个 bps。"
              "`/quote` 不知道你的窗口,所以它替你做不了这个决定。")
    else:
        print("\n>> 最便宜的那条同时也是最快的 —— 这次没有取舍。")

    if args.window:
        ok = [r for r in live if r["duration"] <= args.window]
        print()
        if ok:
            b = max(ok, key=lambda r: r["to"])
            m = b["cost_bps"] if same_asset else b["vs_best_bps"]
            print(f">> 窗口 {args.window:g}s 内可行的最优路径:{b['tools']}")
            print(f"   到手 {b['to']:,.4f}  {'成本' if same_asset else '比最优差'} "
                  f"{m:.2f} bps  耗时 {b['duration']}s")
            lost = (cheapest["to"] - b["to"]) / cheapest["to"] * 10_000
            if lost > 0:
                print(f"   为了赶上窗口,比最便宜那条多付 {lost:.2f} bps")
        else:
            print(f">> ⚠ 没有任何候选能在 {args.window:g}s 内完成 —— "
                  f"最快的也要 {min(r['duration'] for r in live)}s。")
            print("   这条路配不上你的价差窗口,不是成本问题,是速度问题。")

    bad = [r for r in rows if r["buffer_bps"] < 0]
    if bad:
        print(f"\n⚠ {len(bad)} 条候选的 toAmountMin > toAmount(保底值大于预期值,"
              f"违反不变量),这些路径的保底数据不可信:")
        for r in bad[:3]:
            print(f"   {r['tools'][:40]}  缓冲 {r['buffer_bps']:.2f} bps")

    if same_asset:
        print(f"\n注意:成本 bps 是 token 口径(同资产数量相减,零价格假设)。")
        print(f"      这里面 LI.FI 服务费通常占大头,而它是**可议价的定价决策**,"
              f"不是物理成本。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n已导出 {args.json}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
