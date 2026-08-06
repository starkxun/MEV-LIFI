#!/usr/bin/env python3
"""
sui_pool_price.py —— Sui 上多个 DEX 的可成交价对比 + 成本地板

`pool_price.py` 的 Sui 版本。回答同一个问题:

    同一个币在 Sui 不同 DEX 上差多少?这个差够不够覆盖成本地板?

**为什么先做这个,而不是直接跑 sui-mev 机器人**:
那个机器人要自建 Sui 全节点(DBSimulator 直读 RocksDB),是持续开销。
花钱之前,先用几乎零成本的方式量一下**价差分布**——
如果价差本身就压在成本地板以下,后面的投入根本不用谈。

这是你在跨链上验证过的顺序:**先量成本地板,再看有没有机会。**

数据来源:
  · 池子清单 —— sui-mev 仓库自带的 crates/dex-indexer/data/*.txt
    (含 token 类型、decimals、费率,不用自己猜地址)
  · 链上状态 —— Sui JSON-RPC

⚠️ **Sui 公共 fullnode 的 JSON-RPC 已废弃**(`fullnode.mainnet.sui.io` 直接返回
   `Method not found … migrate to gRPC or GraphQL`)。实测下面两个第三方端点还能用。
   而且它们会拦默认 User-Agent,必须带 UA —— 这两个坑都踩过了。

用法:
    python3 sui_pool_price.py                      # 默认 SUI/USDC
    python3 sui_pool_price.py --base USDC --quote SUI
    python3 sui_pool_price.py --min-tvl 50000      # 只看有深度的池
    python3 sui_pool_price.py --repo ~/sui-mev
    python3 sui_pool_price.py --json out.json
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import requests

# 实测可用(2026-08-06)。官方 fullnode 的 JSON-RPC 已废弃,别用。
ENDPOINTS = [
    "https://sui-mainnet-endpoint.blockvision.org",
    "https://rpc-mainnet.suiscan.xyz",
]

# 常见币的类型标识。Sui 上同一个符号可能有多个发行方,这里只登记验证过的。
KNOWN = {
    "SUI":  "0x2::sui::SUI",
    "USDC": "0x5d4b302506645c37ff133b98c4b50a5ae14841659738d6d733d59d0d217a93bf::coin::COIN",
    "USDT": "0xc060006111016b8a020ad5b33834984a437aaa7d3c74c18e09a95d48aceab08c::coin::COIN",
}

# 每家 DEX 的价格怎么读、费率分母是多少。
# **这些是实测对象字段推出来的**,不是抄文档 —— 见脚本头部说明。
DEX_SPEC = {
    "cetus":      {"kind": "clmm", "fee_key": "fee_rate",  "fee_den": 1_000_000},
    "turbos":     {"kind": "clmm", "fee_key": "fee",       "fee_den": 1_000_000},
    "flowx_clmm": {"kind": "clmm", "fee_key": "fee_rate",  "fee_den": 1_000_000},
    "kriya_clmm": {"kind": "clmm", "fee_key": "fee_rate",  "fee_den": 1_000_000},
    "kriya_amm":  {"kind": "amm",  "fee_key": None,        "fee_den": 1_000_000},
    "blue_move":  {"kind": "amm",  "fee_key": None,        "fee_den": 1_000_000},
    # aftermath 是多资产池、deepbook 是订单簿,定价方式不同,本版跳过
}

Q64 = 2 ** 64
_S = requests.Session()
_S.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})


def rpc(method, params, retries=4):
    """
    带端点轮换和重试。两个坑:
      1. 默认 User-Agent 会被 403(已在 Session 里设好)
      2. 单个端点会抽风,轮换比重试同一个有效
    """
    delay = 1.0
    last = None
    for attempt in range(retries):
        for ep in ENDPOINTS:
            try:
                r = _S.post(ep, json={"jsonrpc": "2.0", "id": 1,
                                      "method": method, "params": params}, timeout=30)
                if r.status_code == 200:
                    d = r.json()
                    if "result" in d:
                        return d["result"]
                    last = str(d.get("error"))[:120]
                else:
                    last = f"HTTP {r.status_code}"
            except (requests.RequestException, ValueError) as e:
                last = f"{type(e).__name__}"
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"RPC {method} 失败: {last}")


def load_registry(repo, want_a, want_b):
    """
    从 sui-mev 自带的池子清单里挑出目标交易对。

    格式:  dex|pool_id|[{token_type,decimals},…]|{额外信息}
    只要**恰好两种币且正是目标对**的池 —— 多资产池(aftermath)定价方式不同。
    """
    data_dir = Path(repo) / "crates" / "dex-indexer" / "data"
    if not data_dir.exists():
        raise RuntimeError(f"找不到池子清单目录: {data_dir}\n"
                           f"用 --repo 指向 sui-mev 仓库根目录")
    out = []
    for f in sorted(data_dir.glob("*_pools.txt")):
        dex = f.stem.replace("_pools", "")
        if dex not in DEX_SPEC:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            parts = line.split("|")
            if len(parts) < 4:
                continue
            try:
                toks = json.loads(parts[2])
                extra = json.loads(parts[3])
            except ValueError:
                continue
            if len(toks) != 2:
                continue
            types = [t["token_type"] for t in toks]
            if {types[0], types[1]} != {want_a, want_b}:
                continue
            out.append({"dex": dex, "pool": parts[1], "tokens": toks, "extra": extra})
    return out


def fee_bps(p):
    """把各家五花八门的费率字段统一成 bps。"""
    spec = DEX_SPEC[p["dex"]]
    e = p["extra"]
    if not isinstance(e, dict):
        return None
    inner = next(iter(e.values())) if e else {}
    if not isinstance(inner, dict):
        return None
    if p["dex"] == "kriya_amm":
        # LP 费 + 协议费都要算
        tot = (inner.get("lp_fee_percent", 0) or 0) + (inner.get("protocol_fee_percent", 0) or 0)
        return tot / spec["fee_den"] * 10_000
    k = spec["fee_key"]
    if k and inner.get(k) is not None:
        return inner[k] / spec["fee_den"] * 10_000
    return None


def price_from_pool(p, fields, base_type, quote_type):
    """
    算出「1 个 quote 值多少 base」。

    CLMM: sqrt_price 是 Q64 定点数 → price_raw = (sqrt/2^64)^2 = token1/token0
    AMM : 直接用储备比

    **decimals 换算是这里最容易错的地方** —— SUI 是 9 位、USDC 是 6 位,
    差 1000 倍。方向搞反不会报错,只会让价格变成倒数(这个坑本项目栽过两次)。
    """
    t0, t1 = p["tokens"][0], p["tokens"][1]
    d0, d1 = int(t0["decimals"]), int(t1["decimals"])
    kind = DEX_SPEC[p["dex"]]["kind"]

    if kind == "clmm":
        sq = fields.get("sqrt_price")
        if sq is None:
            return None, None
        raw = (int(sq) / Q64) ** 2           # token1 每 token0(原始单位)
        px_1_per_0 = raw * 10 ** (d0 - d1)   # 换成人类单位
        # 深度参考:CLMM 用两边储备(字段名各家不同)
        r0 = fields.get("coin_a") or fields.get("reserve_x")
        r1 = fields.get("coin_b") or fields.get("reserve_y")
    else:
        r0 = fields.get("token_x")
        r1 = fields.get("token_y")
        if r0 is None or r1 is None or int(r0) == 0:
            return None, None
        px_1_per_0 = (int(r1) / 10 ** d1) / (int(r0) / 10 ** d0)

    if not px_1_per_0 or px_1_per_0 <= 0:
        return None, None

    # 统一成「1 quote = ? base」
    if t0["token_type"] == base_type:
        px = 1 / px_1_per_0            # token0=base,原式是 quote/base,取倒数
    else:
        px = px_1_per_0

    # TVL 折成 base 计价(能读到储备才有)
    tvl = None
    try:
        if r0 is not None and r1 is not None:
            a0 = int(r0) / 10 ** d0
            a1 = int(r1) / 10 ** d1
            if t0["token_type"] == base_type:
                tvl = a0 + a1 * px
            else:
                tvl = a1 + a0 * px
    except (TypeError, ValueError):
        pass
    return px, tvl


def main():
    p = argparse.ArgumentParser(description="Sui 多 DEX 可成交价 + 成本地板")
    p.add_argument("--repo", default=str(Path.home() / "sui-mev"),
                   help="sui-mev 仓库路径(用它自带的池子清单)")
    p.add_argument("--base", default="USDC", help="计价币")
    p.add_argument("--quote", default="SUI", help="标的币")
    p.add_argument("--min-tvl", type=float, default=1000,
                   help="TVL 下限(base 计价),挡掉僵尸池")
    p.add_argument("--outlier-x", type=float, default=10.0,
                   help="价格偏离中位数超过这个倍数即判为坏数据(未初始化的池)")
    p.add_argument("--json", help="导出结果")
    args = p.parse_args()

    bt = KNOWN.get(args.base.upper(), args.base)
    qt = KNOWN.get(args.quote.upper(), args.quote)

    pools = load_registry(args.repo, bt, qt)
    if not pools:
        print(f"清单里没有 {args.base}/{args.quote} 的两币池", file=sys.stderr)
        return 1

    print(f"\n从池子清单找到 {len(pools)} 个 {args.base}/{args.quote} 池,正在读链上状态…",
          file=sys.stderr)

    # 批量读,减少往返
    ids = [x["pool"] for x in pools]
    objs = {}
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        res = rpc("sui_multiGetObjects", [chunk, {"showContent": True, "showType": True}])
        for o in res or []:
            d = (o or {}).get("data")
            if d:
                objs[d["objectId"]] = d
        time.sleep(0.2)

    rows = []
    for x in pools:
        d = objs.get(x["pool"])
        if not d:
            continue
        f = (d.get("content") or {}).get("fields", {})
        px, tvl = price_from_pool(x, f, bt, qt)
        if px is None:
            continue
        rows.append({"dex": x["dex"], "pool": x["pool"], "price": px,
                     "tvl": tvl, "fee_bps": fee_bps(x)})

    if not rows:
        print("没有能解出价格的池", file=sys.stderr)
        return 1

    # ---- 不变量检查:未初始化的池会给出天文数字 ----
    # 实测:某个 flowx_clmm 池 sqrt_price 接近 0,取倒数后价格算出 1.8e22,
    # TVL 算出 2.95e14 —— 它同时通过了 TVL 下限,然后把价差统计毒成
    # 2.7e26 bps。**这是 wick_scan.py 里那个"100% 回弹率"的同类**:
    # 极端值不先剔掉,后面所有统计都是废的。
    #
    # 判据:和全体中位价偏离超过 outlier_x 倍的,判为坏数据。
    # 用中位数不用均值 —— 均值本身就会被这种值拖走。
    med_px = statistics.median([r["price"] for r in rows])
    for r in rows:
        r["bad"] = not (med_px / args.outlier_x <= r["price"] <= med_px * args.outlier_x)
    bad = [r for r in rows if r["bad"]]

    live = [r for r in rows if not r["bad"] and (r["tvl"] or 0) >= args.min_tvl]
    rows.sort(key=lambda r: (r["bad"], -(r["tvl"] or 0)))

    print()
    print("=" * 84)
    print(f"Sui 多 DEX 可成交价   1 {args.quote} = ? {args.base}   "
          f"(TVL 下限 {args.min_tvl:,.0f} {args.base})")
    print("=" * 84)
    print(f"{'DEX':13} {'价格':>13} {'费率bps':>9} {'TVL':>16}  池")
    print("-" * 84)
    for r in rows:
        if r["bad"]:
            dead = "  ✗坏数据(价格偏离中位 >%gx)" % args.outlier_x
        elif r not in live:
            dead = "  ✗低于TVL下限"
        else:
            dead = ""
        fee = f"{r['fee_bps']:.1f}" if r["fee_bps"] is not None else "?"
        tvl = f"{r['tvl']:,.0f}" if r["tvl"] else "?"
        print(f"{r['dex']:13} {r['price']:>13.6f} {fee:>9} {tvl:>16}  {r['pool'][:14]}…{dead}")

    print("-" * 84)
    if bad:
        print(f"⚠ 剔除 {len(bad)} 个坏数据池(未初始化 / sqrt_price≈0,"
              f"取倒数后价格是天文数字)。全体中位价 {med_px:.6f}")
    if len(live) < 2:
        print("存活池不足 2 个,没法比价差。放宽 --min-tvl 再看。")
        print("=" * 84)
        return 0

    v = [r["price"] for r in live]
    lo_r = min(live, key=lambda r: r["price"])
    hi_r = max(live, key=lambda r: r["price"])
    disp = (max(v) - min(v)) / min(v) * 10_000

    print(f">> 存活 {len(live)} 个池,价格 {min(v):.6f} ~ {max(v):.6f}")
    print(f"   **最大价差 {disp:.2f} bps**  ({lo_r['dex']} 买 → {hi_r['dex']} 卖)")

    # ---- 成本地板:这才是关键 ----
    f_lo, f_hi = lo_r["fee_bps"], hi_r["fee_bps"]
    print()
    if f_lo is None or f_hi is None:
        print("   ⚠ 有池子的费率读不到,算不了成本地板")
    else:
        floor = f_lo + f_hi
        net = disp - floor
        print(f">> 成本地板(双边手续费): {f_lo:.1f} + {f_hi:.1f} = **{floor:.1f} bps**")
        print(f"   净收益 = 价差 − 地板 = {disp:.2f} − {floor:.1f} = **{net:+.2f} bps**")
        print()
        if net > 0:
            print(f"   ⚠ 名义为正,但**还没扣**:价格冲击(你那笔的滑点)、gas、")
            print(f"     以及被别人抢先的概率。别当成机会。")
        else:
            print(f"   → 光是手续费就吃掉了价差,**这个时点不成立**。")

    # 最便宜的一对(不一定是价差最大的那对)
    best = None
    for a in live:
        for b in live:
            if a is b or a["fee_bps"] is None or b["fee_bps"] is None:
                continue
            sp = (b["price"] - a["price"]) / a["price"] * 10_000
            n = sp - a["fee_bps"] - b["fee_bps"]
            if best is None or n > best[0]:
                best = (n, a, b, sp)
    if best and best[1] is not lo_r:
        n, a, b, sp = best
        print(f"\n>> 扣完费率后最优的一对:{a['dex']} 买 → {b['dex']} 卖")
        print(f"   价差 {sp:.2f} − 费率 {a['fee_bps']:.1f}+{b['fee_bps']:.1f} = {n:+.2f} bps")

    print()
    print("注意:以上都是**中间价**,不含你那笔的价格冲击。")
    print("     单次观察不能下结论 —— 要连续采样看分布(参考 watch_probe.py)。")
    print("=" * 84)

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print(f"已导出 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
