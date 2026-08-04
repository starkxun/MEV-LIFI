#!/usr/bin/env python3
"""
pool_price.py —— 从链上 AMM 池读**可成交价**,给证据表的「屏幕价差」列换个硬来源

证据表里「屏幕价差」原本填的是 LI.FI 的 `priceUSD` —— 那是**标价**(聚合报价),
不是你真能成交的价。铁律 1 说别信 xxxUSD 字段,那一列却正好在用它。

这个脚本直接读各家 AMM 池的链上状态,算出真实兑换比。
**不需要 API key**,公共 RPC 就能读。

支持的池型:
  · univ3      Uniswap V3 及其分叉  getPool(a,b,uint24) + slot0()
  · slipstream Aerodrome 集中流动性(Uni V3 数学)getPool(a,b,int24) + slot0()
  · solidly    Aerodrome v2(Velodrome 系)getPool(a,b,bool) + getReserves()

用法:
    pip install web3
    python3 pool_price.py --chains ARB,BAS --base USDC --quote WETH
    python3 pool_price.py --chains ARB,BAS --base USDC --quote WETH --min-tvl 500000
    python3 pool_price.py ... --json
    python3 pool_price.py ... --rpc BAS=https://你的alchemy端点

三个已经踩过的坑,脚本都做了防护:

坑 A:**「有合约」不等于「是那个合约」。**
      Base 上通用 Uniswap Factory 地址 0x1F98…4 也有字节码,但 getPool 返回空。
      所以 factory 地址必须逐链登记,不能假设多链同地址。

坑 B:**不能拿不同费率档/不同场所的价格直接相减。**
      同一条链上不同池子的中间价能差二三十 bps,跨档相减会凭空造出一个
      和真实门槛同量级的"价差",看着像边缘机会,其实不存在。

坑 C:**僵尸池的价格是有毒的。**
      实测 Aerodrome 的 USDC/WETH **stable** 池只有 3 个 WETH,
      报价 5,563 USDC/WETH(偏离 3 倍)—— 因为 stable 曲线是给相关资产用的,
      这个池基本没人动,价格早不新鲜。用 --min-tvl 把这类池挡掉。
"""

import argparse
import json
import sys
import time

import requests

# ---- 逐链登记的场所。地址都实测过有合约且调用返回合理结果 ----
VENUES = {
    "ARB": [
        {"name": "Uniswap V3", "kind": "univ3",
         "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
         "params": [100, 500, 3000, 10000]},
    ],
    "BAS": [
        {"name": "Uniswap V3", "kind": "univ3",
         "factory": "0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
         "params": [100, 500, 3000, 10000]},
        {"name": "Aerodrome", "kind": "solidly",
         "factory": "0x420DD381b31aEf6683db6B902084cB0FFECe40Da",
         "params": [False, True]},          # volatile / stable
        {"name": "Slipstream", "kind": "slipstream",
         "factory": "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A",
         "params": [1, 50, 100, 200, 2000]},
    ],
    "ETH": [
        {"name": "Uniswap V3", "kind": "univ3",
         "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
         "params": [100, 500, 3000, 10000]},
    ],
    "OPT": [
        {"name": "Uniswap V3", "kind": "univ3",
         "factory": "0x1F98431c8aD98523631AE4a59f267346ea31F984",
         "params": [100, 500, 3000, 10000]},
    ],
}

RPCS = {
    "ARB": "https://arb1.arbitrum.io/rpc",
    "BAS": "https://mainnet.base.org",
    "ETH": "https://eth.llamarpc.com",
    "OPT": "https://mainnet.optimism.io",
}

SEL = {
    "univ3_getpool": "0x1698ee82",       # getPool(address,address,uint24)
    "slip_getpool": "0x28af8d0b",        # getPool(address,address,int24)
    "solidly_getpool": "0x79bc57d5",     # getPool(address,address,bool)
    "slot0": "0x3850c7bd",
    "reserves": "0x0902f1ac",            # getReserves()
    "balanceof": "0x70a08231",
}


def eth_call(w3, to, data, retries=5):
    """
    带退避重试的 eth_call。公共 RPC(尤其 mainnet.base.org)很容易 429,
    一次限速就让脚本崩掉是最不该发生的 —— 和 chainkit.run_loop 同一条原则。
    要跑得密就用 --rpc 换成自己的节点。
    """
    from web3 import Web3
    delay = 0.8
    for i in range(retries):
        try:
            return w3.eth.call({"to": Web3.to_checksum_address(to), "data": data})
        except Exception as e:
            msg = str(e)
            transient = any(x in msg for x in
                            ("429", "Too Many Requests", "502", "503", "timeout",
                             "Timeout", "Connection"))
            if not transient or i == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2
    return b""


def pad_addr(a):
    return a[2:].lower().rjust(64, "0")


def pad_int(n):
    if isinstance(n, bool):
        n = 1 if n else 0
    if n < 0:
        n = (1 << 256) + n
    return hex(n)[2:].rjust(64, "0")


def get_token(chain, sym, retries=4):
    """token 元数据走 LI.FI。同样要重试 —— 实测这里会被 Connection reset 打断。"""
    delay = 1.0
    last = None
    for i in range(retries):
        try:
            r = requests.get("https://li.quest/v1/token",
                             params={"chain": chain, "token": sym}, timeout=25)
            r.raise_for_status()
            d = r.json()
            return {"address": d["address"], "decimals": int(d["decimals"]),
                    "priceUSD": float(d["priceUSD"]), "symbol": d["symbol"]}
        except (requests.RequestException, ValueError, KeyError) as e:
            last = e
            if i == retries - 1:
                break
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"取 {chain}/{sym} 元数据失败: {last}")


def balance_of(w3, token, holder):
    r = eth_call(w3, token, SEL["balanceof"] + pad_addr(holder))
    return int.from_bytes(bytes(r), "big") if r else 0


def price_from_slot0(w3, pool, base, quote):
    """Uni V3 数学:sqrtPriceX96 → quote per base。"""
    s0 = eth_call(w3, pool, SEL["slot0"])
    if not s0 or len(bytes(s0)) < 32:
        return None
    sq = int.from_bytes(bytes(s0)[:32], "big")
    if sq == 0:
        return None
    base_is_t0 = int(base["address"], 16) < int(quote["address"], 16)
    raw = (sq / 2 ** 96) ** 2                 # token1 / token0
    shift = 10 ** (base["decimals"] - quote["decimals"])
    return raw * shift if base_is_t0 else (1 / raw) * shift


def price_from_reserves(w3, pool, base, quote):
    """
    Solidly volatile 池:恒定乘积,中间价 = 储备比。

    注意 stable 池用的是 x³y+y³x=k,中间价**不等于**储备比。
    这里统一按储备比算,所以 stable 池的价格只能当粗略参考 ——
    真正挡住它的是 --min-tvl(这种池通常也没什么钱)。
    """
    r = eth_call(w3, pool, SEL["reserves"])
    if not r or len(bytes(r)) < 64:
        return None
    b = bytes(r)
    r0 = int.from_bytes(b[0:32], "big")
    r1 = int.from_bytes(b[32:64], "big")
    if r0 == 0 or r1 == 0:
        return None
    base_is_t0 = int(base["address"], 16) < int(quote["address"], 16)
    rb, rq = (r0, r1) if base_is_t0 else (r1, r0)
    return (rq / 10 ** quote["decimals"]) / (rb / 10 ** base["decimals"])


def read_chain(w3, chain, base, quote):
    from web3 import Web3
    out = []
    for venue in VENUES.get(chain.upper(), []):
        for prm in venue["params"]:
            if venue["kind"] == "univ3":
                data = SEL["univ3_getpool"]
                label = f"fee{prm}"
            elif venue["kind"] == "slipstream":
                data = SEL["slip_getpool"]
                label = f"ts{prm}"
            else:
                data = SEL["solidly_getpool"]
                label = "stable" if prm else "volatile"
            data += pad_addr(base["address"]) + pad_addr(quote["address"]) + pad_int(prm)

            try:
                res = eth_call(w3, venue["factory"], data)
            except Exception:
                continue
            # 空返回 = 这个地址上没有该方法,或者根本不是 factory(坑 A)
            if not res or len(bytes(res)) < 32:
                continue
            if int.from_bytes(bytes(res), "big") == 0:
                continue
            pool = Web3.to_checksum_address(bytes(res)[-20:])

            # 两个 helper 返回的是 quote per base(数学上自然的方向)
            raw_px = (price_from_reserves(w3, pool, base, quote)
                      if venue["kind"] == "solidly"
                      else price_from_slot0(w3, pool, base, quote))
            if not raw_px or raw_px <= 0:
                continue
            # 全脚本统一成 **base per quote**(即"1 WETH = 多少 USDC"),
            # 和 LI.FI 的 lifi_mark 同方向 —— 单位不统一就会算出天文数字的 bps。
            px = 1 / raw_px

            # 统一 TVL 口径:直接读池子持有的两种 token 数量。
            # 这样 CL 池和恒定乘积池才可比 —— L(liquidity)是不可比的。
            qb = balance_of(w3, base["address"], pool) / 10 ** base["decimals"]
            qq = balance_of(w3, quote["address"], pool) / 10 ** quote["decimals"]
            tvl = qb + qq * px          # quote 数量 × (base per quote) → base 计价

            out.append({"venue": venue["name"], "label": label, "pool": pool,
                        "price": px, "tvl_base": tvl,
                        "amt_base": qb, "amt_quote": qq})
    return out


def main():
    p = argparse.ArgumentParser(description="链上多场所可成交价 + 跨链价差")
    p.add_argument("--chains", default="ARB,BAS")
    p.add_argument("--base", default="USDC", help="报价分母")
    p.add_argument("--quote", default="WETH", help="计价资产")
    p.add_argument("--min-tvl", type=float, default=200_000,
                   help="TVL 下限(base 计价),挡掉僵尸池。默认 20 万")
    p.add_argument("--rpc", action="append", default=[])
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    try:
        from web3 import Web3
    except ImportError:
        print("需要 web3: pip install web3", file=sys.stderr)
        return 1

    rpc_over = dict(x.split("=", 1) for x in args.rpc)
    chains = [c.strip().upper() for c in args.chains.split(",") if c.strip()]

    result = {}
    for chain in chains:
        url = rpc_over.get(chain) or RPCS.get(chain)
        if not url:
            print(f"{chain}: 没有 RPC,用 --rpc {chain}=<url>", file=sys.stderr)
            continue
        w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
        b, q = get_token(chain, args.base), get_token(chain, args.quote)
        pools = read_chain(w3, chain, b, q)
        if not pools:
            print(f"{chain}: 没找到 {args.base}/{args.quote} 的池", file=sys.stderr)
            continue
        live = [x for x in pools if x["tvl_base"] >= args.min_tvl]
        result[chain] = {"block": w3.eth.block_number, "all": pools,
                         "live": live, "base": b, "quote": q,
                         "lifi_mark": q["priceUSD"] / b["priceUSD"]}

    if not result:
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0

    bs, qs = args.base, args.quote
    print()
    print("=" * 78)
    print(f"链上多场所可成交价   {bs}/{qs}   (TVL 下限 {args.min_tvl:,.0f} {bs})")
    print("=" * 78)

    for chain, r in result.items():
        print(f"\n【{chain}】 block {r['block']:,}")
        for x in sorted(r["all"], key=lambda z: -z["tvl_base"]):
            dead = "" if x in r["live"] else "  ✗僵尸池,已剔除"
            print(f"   {x['venue']:<12} {x['label']:<9} "
                  f"1 {qs} = {x['price']:>10,.2f} {bs}   "
                  f"TVL {x['tvl_base']:>13,.0f} {bs}{dead}")
        if not r["live"]:
            print("   ⚠ 全部低于 TVL 下限,这条链没有可信价格")
            continue
        v = [x["price"] for x in r["live"]]
        disp = (max(v) - min(v)) / min(v) * 10_000
        print(f"   → 存活 {len(r['live'])} 个池,价格 {min(v):,.2f} ~ {max(v):,.2f}"
              f"  **场所间离散 {disp:.2f} bps**")
        print(f"   → LI.FI 标价 {r['lifi_mark']:,.2f}"
              f"(与最深池差 {(r['lifi_mark']/max(r['live'], key=lambda z:z['tvl_base'])['price']-1)*10_000:+.2f} bps)")

    if len(result) == 2:
        (c1, r1), (c2, r2) = list(result.items())
        if not r1["live"] or not r2["live"]:
            print("\n有链没有可信价格,跳过跨链比较")
            print("=" * 78)
            return 0
        d1 = max(r1["live"], key=lambda z: z["tvl_base"])
        d2 = max(r2["live"], key=lambda z: z["tvl_base"])
        sp = (d2["price"] - d1["price"]) / d1["price"] * 10_000
        mark = (r2["lifi_mark"] - r1["lifi_mark"]) / r1["lifi_mark"] * 10_000
        v1 = [x["price"] for x in r1["live"]]
        v2 = [x["price"] for x in r2["live"]]
        disp = max((max(v1)-min(v1))/min(v1), (max(v2)-min(v2))/min(v2)) * 10_000

        print("\n" + "-" * 78)
        print(f">> 跨链价差 {c1}→{c2}(各取最深池)")
        print(f"   {c1} {d1['venue']} {d1['label']}: {d1['price']:,.2f}")
        print(f"   {c2} {d2['venue']} {d2['label']}: {d2['price']:,.2f}")
        print(f"   价差 {sp:+.2f} bps    |    LI.FI 标价价差 {mark:+.2f} bps")
        print("-" * 78)
        if abs(sp) < disp:
            print(f"⚠ 这个跨链价差({abs(sp):.2f} bps)**小于链内场所间离散度"
                  f"({disp:.2f} bps)**。")
            print("  意思是:同一条链上不同池子的差,比两条链之间的差还大。")
            print("  此时「这条链的价格」根本不是单一数字,跨链价差这个概念本身就不稳。")
            print("  别拿它当套利信号 —— 换个池子比,结论就翻了。")
        print("\n注意:以上都是**中间价**,不含你那笔的滑点。")
        print("     净收益 = 可成交价差 − cost_probe 算出的真实门槛。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
