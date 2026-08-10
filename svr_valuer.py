#!/usr/bin/env python3
"""
svr_valuer.py —— SVR 拍卖的链下估值程序(只算不出价)

这是整条链路的最后一块。合约只是执行器,**拍卖里真正比的是这个**:
推送到达的那一刻,算出「这次预言机更新值多少钱、该出多少价」。

    python3 svr_valuer.py build-map                    # 建 聚合器→Aave资产 映射
    python3 svr_valuer.py replay shadow/svr_feed_long.jsonl   # 用抓到的历史帧离线跑
    python3 svr_valuer.py watch --seconds 600          # 接实时推送

⚠️ 全程只读、只算、只记录。**没有实现出价**,代码里不存在
   solver_submitSolverOperation,也不读私钥。

── 估值链路 ────────────────────────────────────────────────────
  推送到达
    → hints.aggregator  查出这是哪个 Aave 资产
    → hints.medianPrice / 10**decimals()  得到新价格   ← 小数位必须现查
    → 和链上当前价比,得到 ratio
    → 用 ratio 重算高危仓位的健康度
    → 跌破 1 的:估算毛清算奖励
    → 减去成本,乘上目标利润率,得到出价建议
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
ENDPOINT = "wss://svr-bid-endpoint.chain.link/ws/solver"

CHAIN_IDS = {"0xa4b1": "ARB", "0x2105": "BASE", "0x38": "BNB", "0x1": "ETH"}
SLUG = {"ARB": "arbitrum", "BASE": "base", "OPT": "optimism"}

MAP_FILE = ROOT / "shadow" / "aggregator_map.json"
DECISIONS = ROOT / "shadow" / "valuations.jsonl"

# ── 成本参数。来源标注在旁边,不是拍脑袋 ──────────────────────
FLASHLOAN_BPS = 5          # Aave 闪电贷 0.05%,fork 测试里实测 $188.25 / $376,499
GAS_USD = 0.07             # 实测:gasUsed 1,789,466 @ Arbitrum ≈ $0.07
SWAP_SLIPPAGE_BPS = 30     # ⚠️ **这个是假设,没有实测**。真实 DEX 滑点未验证。
TARGET_MARGIN = 0.10       # 想留多少利润率。市场实际清算在 ~8%(回收率 92.3%)


def key():
    for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith("ANKR_KEY="):
            return ln.split("=", 1)[1].strip()
        if ln.startswith("SUI_RPC=") and "ankr.com" in ln:
            return ln.split("=", 1)[1].strip().rstrip("/").split("/")[-1]
    raise RuntimeError(".env 里找不到 Ankr key")


_c = {}


def w3(chain):
    from web3 import Web3
    if chain not in _c:
        _c[chain] = Web3(Web3.HTTPProvider(
            f"https://rpc.ankr.com/{SLUG[chain]}/{key()}", request_kwargs={"timeout": 60}))
    return _c[chain]


def rt(chain, fn, n=4):
    from web3 import Web3
    for i in range(n):
        try:
            return fn()
        except Exception:
            if i == n - 1:
                raise
            time.sleep(1.2 * (i + 1))
            _c[chain] = Web3(Web3.HTTPProvider(
                f"https://rpc.ankr.com/{SLUG[chain]}/{key()}", request_kwargs={"timeout": 60}))


def sel(sig):
    from web3 import Web3
    return Web3.to_hex(Web3.keccak(text=sig))[:10]


def call(chain, to, data):
    from web3 import Web3
    return rt(chain, lambda: w3(chain).eth.call(
        {"to": Web3.to_checksum_address(to), "data": data}))


def addr_of(chain, to, sig):
    return "0x" + call(chain, to, sel(sig)).hex()[-40:]


def uint_of(chain, to, sig, arg=None):
    return int(call(chain, to, sel(sig) + (arg[2:].lower().rjust(64, "0") if arg else "")).hex(), 16)


def str_of(chain, to, sig):
    h = call(chain, to, sel(sig)).hex()
    n = int(h[64:128], 16)
    return bytes.fromhex(h[128:128 + n * 2]).decode()


# ══════════════════════════════════════════════════════════════
def cmd_build_map(args):
    """
    建「聚合器地址 → Aave 资产」映射。

    推送里给的是**聚合器**地址,而 Aave 的 getSourceOfAsset 返回的是**代理**。
    所以要多走一步:代理.aggregator() 才能对上。
    """
    chain = args.chain
    ap = addr_of(chain, AAVE_POOL, "ADDRESSES_PROVIDER()")
    orc = addr_of(chain, ap, "getPriceOracle()")
    raw = call(chain, AAVE_POOL, sel("getReservesList()")).hex()
    n = int(raw[64:128], 16)
    assets = ["0x" + raw[128 + i * 64 + 24: 128 + (i + 1) * 64] for i in range(n)]
    print(f"{chain}: {n} 个储备资产,逐个解析…\n")

    out = {}
    for a in assets:
        try:
            sym = str_of(chain, a, "symbol()")
        except Exception:
            sym = a[:8]
        try:
            src = "0x" + call(chain, orc, sel("getSourceOfAsset(address)")
                              + a[2:].lower().rjust(64, "0")).hex()[-40:]
        except Exception:
            print(f"  {sym:<10} 读不到价格源"); continue
        try:
            agg = addr_of(chain, src, "aggregator()")
            dec = uint_of(chain, agg, "decimals()")
        except Exception:
            # 有些是 capped / 复合适配器,没有 aggregator()
            print(f"  {sym:<10} {src}  (适配器,无 aggregator() —— 跳过)")
            continue
        try:
            lb = ((uint_of(chain, AAVE_POOL, "getConfiguration(address)", a) >> 32) & 0xFFFF) / 10000.0
        except Exception:
            lb = 1.0
        out[agg.lower()] = {"asset": a, "symbol": sym, "proxy": src,
                            "decimals": dec, "liq_bonus": lb}
        print(f"  {sym:<10} agg {agg}  dec={dec}  清算奖励 {(lb-1)*100:.1f}%")

    MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
    old[chain] = out
    MAP_FILE.write_text(json.dumps(old, indent=1))
    print(f"\n{len(out)} 条映射已存 {MAP_FILE}")
    return 0


def load_map():
    if not MAP_FILE.exists():
        raise SystemExit("没有映射文件,先跑:python3 svr_valuer.py build-map")
    return json.loads(MAP_FILE.read_text())


def load_positions():
    p = ROOT / "shadow" / "positions.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text())
    return d.get("positions", d) if isinstance(d, dict) else d


# ══════════════════════════════════════════════════════════════
def value_update(chain, agg, raw_price, amap, positions, cur_price_cache):
    """
    核心估值:一次预言机更新值多少钱?

    返回 None 表示「这次更新和我们无关」—— 绝大多数推送都是这个结果,
    这本身就是重要信息:**大部分推送不产生机会。**
    """
    info = amap.get(chain, {}).get(agg.lower())
    if not info:
        return None                      # 不是 Aave 用的喂价

    new_px = int(raw_price, 16) / 10 ** info["decimals"]

    # 和链上当前价比。**小数位从映射里取,是 build-map 时链上现查的。**
    ck = (chain, agg.lower())
    if ck not in cur_price_cache:
        try:
            b = bytes(call(chain, info["proxy"], sel("latestRoundData()")))
            ans = int.from_bytes(b[32:64], "big")
            cur_price_cache[ck] = ans / 10 ** info["decimals"]
        except Exception:
            cur_price_cache[ck] = None
    cur = cur_price_cache[ck]
    if not cur or cur <= 0:
        return None

    ratio = new_px / cur
    hits = []
    for p in positions:
        hf = p.get("hf")
        debt = p.get("debt_usd") or p.get("debt") or 0
        if not hf or hf >= 100:
            continue
        # ⚠️ 一阶近似:HF ∝ 抵押物价格。
        #    只在「抵押物是这个资产、债务是稳定币」时成立,**偏激进**。
        #    要消除得用 eth_call 逐个模拟 liquidationCall(P1 的事)。
        new_hf = hf * ratio
        if hf >= 1.0 > new_hf:
            hits.append({"user": p.get("user"), "hf": hf, "new_hf": new_hf, "debt": debt})

    if not hits:
        return {"chain": chain, "symbol": info["symbol"], "new_px": new_px,
                "cur_px": cur, "ratio": ratio, "n_hits": 0, "gross": 0.0, "bid": 0.0}

    # 毛清算奖励 ≈ 可清算债务 × 清算奖励率
    # closeFactor:Aave V3 在 HF > 0.95 时只让清算 50%
    gross = 0.0
    for h in hits:
        cf = 0.5 if h["new_hf"] > 0.95 else 1.0
        gross += h["debt"] * cf * (info["liq_bonus"] - 1)

    cost = gross * (FLASHLOAN_BPS + SWAP_SLIPPAGE_BPS) / 10000 + GAS_USD
    net = gross - cost
    bid = max(0.0, net * (1 - TARGET_MARGIN))

    return {"chain": chain, "symbol": info["symbol"], "new_px": new_px, "cur_px": cur,
            "ratio": ratio, "n_hits": len(hits), "gross": gross, "cost": cost,
            "net": net, "bid": bid, "hits": hits[:5]}


def report(v, tag=""):
    if v is None:
        return
    d = (v["ratio"] - 1) * 100
    head = (f"{tag}{v['chain']} {v['symbol']:<8} ${v['new_px']:>12,.4f} "
            f"({d:+.3f}% vs 链上)")
    if v["n_hits"] == 0:
        print(f"  {head}   无机会")
        return
    print(f"  {head}")
    print(f"     ★ {v['n_hits']} 个仓位跌破清算线")
    print(f"       毛奖励 ${v['gross']:>10,.2f}")
    print(f"       成本   ${v['cost']:>10,.2f}  (闪电贷 {FLASHLOAN_BPS}bps + 滑点 {SWAP_SLIPPAGE_BPS}bps + gas ${GAS_USD})")
    print(f"       净     ${v['net']:>10,.2f}")
    print(f"       **建议出价 ${v['bid']:>10,.2f}**  (留 {TARGET_MARGIN*100:.0f}% 利润率)")
    for h in v["hits"]:
        print(f"         {h['user'][:14]}…  HF {h['hf']:.4f} → {h['new_hf']:.4f}  债务 ${h['debt']:,.0f}")


# ══════════════════════════════════════════════════════════════
def cmd_replay(args):
    """用抓到的历史帧离线跑估值 —— 不用连网就能验证逻辑。"""
    amap = load_map()
    positions = load_positions()
    print(f"仓位名单 {len(positions)} 个,映射覆盖 "
          f"{sum(len(v) for v in amap.values())} 个聚合器\n")
    cache = {}
    stat = Counter()
    n_frames = 0
    for line in open(args.file, encoding="utf-8"):
        try:
            msg = json.loads(line)["msg"]
        except Exception:
            continue
        p = msg.get("params", {})
        p = p.get("result", p) if isinstance(p, dict) else p
        if not isinstance(p, dict) or "partial_user_operation" not in p:
            continue
        n_frames += 1
        u = p["partial_user_operation"]
        ch = CHAIN_IDS.get(u.get("chainId"))
        if ch not in SLUG:
            stat["链不支持"] += 1
            continue
        h = u.get("hints", {})
        v = value_update(ch, h.get("aggregator", ""), h.get("medianPrice", "0x0"),
                         amap, positions, cache)
        if v is None:
            stat["非 Aave 喂价"] += 1
            continue
        stat["Aave 喂价"] += 1
        stat["有机会" if v["n_hits"] else "无机会"] += 1
        report(v)
    print(f"\n=== {n_frames} 帧 ===")
    for k, n in stat.most_common():
        print(f"  {k:<14} {n:>5}  ({n/max(n_frames,1)*100:>5.1f}%)")
    print(f"\n**绝大多数推送不产生机会 —— 这本身就是这门生意的形状。**")
    return 0


def cmd_simulate(args):
    """
    注入一次合成的价格更新,验证估值管道真的会报警。

    **为什么需要这个**:平静期 replay 跑出来永远是「0 个机会」,
    这时候你没法区分「确实没机会」和「代码悄悄坏了」。
    这条命令给一个必然触发的输入,如果它也报 0,说明管道有问题。
    """
    amap = load_map()
    positions = load_positions()
    ch = args.chain
    # 找到该资产的聚合器
    agg = None
    for a, info in amap.get(ch, {}).items():
        if info["symbol"].upper() == args.symbol.upper():
            agg = a; break
    if not agg:
        print(f"映射里没有 {args.symbol}。可用:"
              f"{[i['symbol'] for i in amap.get(ch,{}).values()]}")
        return 1
    info = amap[ch][agg]
    b = bytes(call(ch, info["proxy"], sel("latestRoundData()")))
    cur = int.from_bytes(b[32:64], "big") / 10 ** info["decimals"]
    new = cur * (1 + args.pct / 100)
    raw = hex(int(new * 10 ** info["decimals"]))
    print(f"合成推送:{ch} {info['symbol']}  ${cur:,.4f} → ${new:,.4f} ({args.pct:+.1f}%)\n")
    v = value_update(ch, agg, raw, amap, positions, {})
    report(v)
    if v and v["n_hits"] == 0:
        print(f"\n  ⚠️ {args.pct:+.1f}% 都没触发。要么名单里没有该资产抵押的仓位,")
        print(f"     要么管道有问题。试更大的跌幅确认。")
    return 0


def cmd_watch(args):
    import asyncio
    import websockets

    amap = load_map()
    positions = load_positions()
    cache = {}
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)

    async def run():
        print(f"连 {ENDPOINT},监听 {args.seconds}s。只算不出价。\n")
        t0 = time.time()
        stat = Counter()
        async with websockets.connect(ENDPOINT, open_timeout=20, ping_interval=20,
                                      max_size=8 * 1024 * 1024) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                      "method": "solver_subscribe",
                                      "params": ["userOperations"]}))
            while time.time() - t0 < args.seconds:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(1.0, args.seconds - (time.time() - t0)))
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("id") == 1 or "error" in msg:
                    print(f"  订阅响应: {str(msg)[:160]}")
                    continue
                p = msg.get("params", {})
                p = p.get("result", p) if isinstance(p, dict) else p
                if not isinstance(p, dict) or "partial_user_operation" not in p:
                    continue
                u = p["partial_user_operation"]
                ch = CHAIN_IDS.get(u.get("chainId"))
                if ch not in SLUG:
                    stat["链不支持"] += 1
                    continue
                h = u.get("hints", {})
                v = value_update(ch, h.get("aggregator", ""), h.get("medianPrice", "0x0"),
                                 amap, positions, cache)
                if v is None:
                    stat["非 Aave 喂价"] += 1
                    continue
                stat["有机会" if v["n_hits"] else "无机会"] += 1
                report(v, tag=f"[{time.strftime('%H:%M:%S')}] ")
                with open(DECISIONS, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"t": time.time(), **{k: x for k, x in v.items()
                                                             if k != "hits"}}) + "\n")
        print(f"\n=== {time.time()-t0:.0f}s ===")
        for k, n in stat.most_common():
            print(f"  {k:<14} {n:>5}")
        print(f"\n决策记录 → {DECISIONS}")

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def main():
    p = argparse.ArgumentParser(description="SVR 拍卖链下估值(只算不出价)")
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("build-map", help="建 聚合器→Aave资产 映射")
    b.add_argument("--chain", default="ARB")
    b.set_defaults(func=cmd_build_map)

    r = sub.add_parser("replay", help="用抓到的历史帧离线估值")
    r.add_argument("file")
    r.set_defaults(func=cmd_replay)

    m = sub.add_parser("simulate", help="注入合成价格更新,自检管道")
    m.add_argument("--chain", default="ARB")
    m.add_argument("--symbol", default="WETH")
    m.add_argument("--pct", type=float, default=-15.0, help="价格变动百分比")
    m.set_defaults(func=cmd_simulate)

    w = sub.add_parser("watch", help="接实时推送估值")
    w.add_argument("--seconds", type=int, default=600)
    w.set_defaults(func=cmd_watch)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
