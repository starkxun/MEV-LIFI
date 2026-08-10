#!/usr/bin/env python3
"""
p0_shadow.py —— P0 影子模式:只看不做

回答一个问题,而且只回答这一个:

    **在 OPT 预言机更新后、ARB 预言机更新前的那 ~34 秒里,
      我能不能算出哪些仓位即将可清算?**

**这个脚本不发任何交易,不需要私钥,不碰钱。** 它只做三件事:

  1. 维护一份 Arbitrum 上 Aave V3 的高危仓位名单(链上直读)
  2. 盯着 Optimism 的 Chainlink 喂价
  3. 每当 OPT 价格变动,用**新价格**推算 ARB 上哪些仓位会跌破清算线,记录下来

跑几天之后,拿记录去对:**预测的那些仓位,后来真的被清算了吗?**
那个命中率就是成本模型里最后一个「待定」项 —— 失败交易概率 —— 的第一个估计。

---

⚠️ **一个必须说清楚的近似**

`getUserAccountData` 只返回**聚合后**的抵押/债务(美元计价),不告诉你
抵押物具体是什么币。所以推算新健康度时,这里用的是一阶近似:

    HF_new ≈ HF_now × (P_new / P_now)

**这个近似只在「抵押物全是随该喂价波动的资产、债务是稳定币」时成立。**
真实仓位可能混着多种抵押物、或者借的也是波动资产 —— 那样 HF 的变化会更小。

所以本脚本的预测是**偏激进**的:它会高估"将要被清算"的数量。
P1 阶段用 `eth_call` 逐个模拟才能消除这个近似 —— **P0 的目的是先证明
"能提前看见",不是精确定价。**

---

用法:
    # 建/更新高危名单(扫链上 Borrow 事件 + 逐个查健康度)
    python3 p0_shadow.py --refresh --days 3

    # 单次评估:如果 ETH 跌 x%,谁会被清算
    python3 p0_shadow.py --what-if -5

    # 影子模式:盯着 OPT 预言机,持续记录预测
    python3 p0_shadow.py --watch --interval 30
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.chainkit import append_jsonl, eprint, run_loop, utcnow

ROOT = Path(__file__).parent
STATE = ROOT / "shadow"
POSITIONS = STATE / "positions.json"
PREDICTIONS = STATE / "predictions.jsonl"

AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"     # Arbitrum / Optimism V3 同址

# ⚠️ 喂价地址不能凭"这是 Chainlink 官方 ETH/USD 代理"就用 ——
#    必须问 Aave 自己读的是哪个:
#      Pool.ADDRESSES_PROVIDER() → .getPriceOracle() → .getSourceOfAsset(WETH)
#    验证脚本:--verify-feeds
#
# Arbitrum:Aave 自 2026-03 起改读 **Chainlink SVR 喂价**(不是公开代理)。
#   SVR = Smart Value Recapture,把清算 OEV 通过拍卖回收给协议。
#   接口与普通喂价完全相同(官方文档明确说明),所以**光看接口分辨不出来**,
#   只能靠"Aave 指向的地址 ≠ 公开代理地址"来识别。
FEED_ARB_PUBLIC = "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612"   # 公开代理,Aave 已不读
FEED_ARB        = "0xbd41b1548a5a06544cbcf87c0c54864312842c00"   # SVR,Aave 实际读这个

# Optimism:未上 SVR(Aave 官方明确"不推荐"),Aave 读的就是公开代理。已验证一致。
FEED_OPT = "0x13e3Ee699D1909E989722E753853AE30b17e08c5"

# 各链 Aave V3 的实际喂价源,--verify-feeds 会重新核对
AAVE_FEED = {"ARB": FEED_ARB, "OPT": FEED_OPT}

BORROW_TOPIC = None      # 启动时由 keccak 现算,不写死


def key():
    p = ROOT / ".env"
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith("ANKR_KEY="):
            return ln.split("=", 1)[1].strip()
        if ln.startswith("SUI_RPC=") and "ankr.com" in ln:
            return ln.split("=", 1)[1].strip().rstrip("/").split("/")[-1]
    raise RuntimeError(".env 里找不到 Ankr key")


def w3(chain):
    from web3 import Web3
    slug = {"ARB": "arbitrum", "OPT": "optimism"}[chain]
    return Web3(Web3.HTTPProvider(f"https://rpc.ankr.com/{slug}/{key()}",
                                  request_kwargs={"timeout": 60}))


def sel(sig):
    from web3 import Web3
    return Web3.to_hex(Web3.keccak(text=sig))[:10]


def latest_price(chain, feed):
    """读喂价的最新价格和更新时间。latestRoundData() 的第 2、4 个返回值。"""
    from web3 import Web3
    c = w3(chain)
    r = c.eth.call({"to": Web3.to_checksum_address(feed),
                    "data": sel("latestRoundData()")})
    b = bytes(r)
    ans = int.from_bytes(b[32:64], "big")
    if ans >= 2 ** 255:
        ans -= 2 ** 256
    return ans / 1e8, int.from_bytes(b[96:128], "big")


def refresh_positions(days=3, hf_max=3.0):
    """
    重建高危名单。两步:
      1. 扫最近 N 天的 Borrow 事件,收集借款人地址
      2. 逐个调 getUserAccountData 拿**链上真实**健康度

    **为什么不用现成的 subgraph**:试过 `aave-liquidation-risk-arbitrum`,
    它返回的 15 个"高危"仓位 `healthFactor` **全部是写死的 1.0000**,
    `debt` 是没归一化的原始 wei,8 个抽查里 5 个链上根本没有债务。
    **第三方索引数据必须交叉验证** —— 这次验证救了整个名单。
    """
    from web3 import Web3
    c = w3("ARB")
    pool = Web3.to_checksum_address(AAVE_POOL)
    topic = Web3.to_hex(Web3.keccak(
        text="Borrow(address,address,address,uint256,uint8,uint256,uint16)"))

    head = c.eth.block_number
    BPD, CHUNK = 345_600, 10_000        # Arbitrum ~0.25s 出块;单次上限 1 万块
    start = head - int(days * BPD)
    users, n, t0 = set(), 0, time.time()
    b = start
    while b < head:
        e = min(b + CHUNK, head)
        try:
            for l in c.eth.get_logs({"address": pool, "topics": [topic],
                                     "fromBlock": b, "toBlock": e}):
                users.add("0x" + l["topics"][2].hex()[-40:])
        except Exception:
            pass                         # 单片失败不影响整体
        b = e + 1
        n += 1
        if n % 40 == 0:
            eprint(f"[refresh] {n} 片, {len(users)} 个借款人, {time.time()-t0:.0f}s")
    eprint(f"[refresh] 扫完 {days} 天,{len(users)} 个借款人,开始查健康度")

    s = sel("getUserAccountData(address)")
    out = []
    for i, u in enumerate(sorted(users)):
        try:
            r = c.eth.call({"to": pool, "data": s + u[2:].lower().rjust(64, "0")})
            bb = bytes(r)
            debt = int.from_bytes(bb[32:64], "big") / 1e8
            hf_raw = int.from_bytes(bb[160:192], "big")
            # hf 为天文数字 = 没有债务(Aave 用 uint256 最大值表示)
            if debt <= 0 or hf_raw >= 2 ** 200:
                continue
            hf = hf_raw / 1e18
            if hf > hf_max:
                continue
            out.append({"user": u,
                        "collateral": int.from_bytes(bb[0:32], "big") / 1e8,
                        "debt": debt,
                        "liq_threshold": int.from_bytes(bb[96:128], "big") / 100,
                        "hf": hf})
        except Exception:
            continue
        if i % 150 == 0 and i:
            eprint(f"[refresh] …{i}/{len(users)}")

    out.sort(key=lambda x: x["hf"])
    STATE.mkdir(exist_ok=True)
    px, _ = latest_price("ARB", FEED_ARB)
    POSITIONS.write_text(json.dumps(
        {"built_at": utcnow(), "eth_price": px, "days": days,
         "scanned_borrowers": len(users), "positions": out},
        ensure_ascii=False, indent=2), encoding="utf-8")
    eprint(f"[refresh] 名单已存:{len(out)} 个有债务且 HF<{hf_max} 的仓位")
    return out


def load_positions():
    if not POSITIONS.exists():
        raise RuntimeError("还没有名单,先跑 --refresh")
    return json.loads(POSITIONS.read_text(encoding="utf-8"))


def project(positions, ratio):
    """
    价格变成 ratio 倍之后,谁会跌破清算线。

    一阶近似:HF ∝ 抵押物价值 ∝ 价格。见文件头的说明 ——
    **这会高估将被清算的数量**,因为真实仓位可能混着稳定币抵押物。
    """
    hit = []
    for p in positions:
        new_hf = p["hf"] * ratio
        if new_hf < 1.0:
            hit.append({**p, "new_hf": new_hf})
    return hit


def drop_to_liquidate(hf):
    """这个仓位要跌多少才会被清算。HF×(1−d)=1 → d = 1 − 1/HF"""
    return (1 - 1 / hf) * 100 if hf > 0 else 0


def cmd_whatif(pct):
    data = load_positions()
    pos = data["positions"]
    ratio = 1 + pct / 100
    hit = project(pos, ratio)
    print()
    print("=" * 78)
    print(f"假设 ETH {'跌' if pct < 0 else '涨'} {abs(pct):g}%  "
          f"(名单建于 {data['built_at'][:16]},{len(pos)} 个仓位)")
    print("=" * 78)
    print(f"  会跌破清算线的:{len(hit)} 个仓位")
    if hit:
        print(f"  合计可清算债务:${sum(x['debt'] for x in hit):,.0f}")
        print(f"\n{'user':44} {'现HF':>7} {'新HF':>7} {'债务$':>12}")
        for x in hit[:15]:
            print(f"{x['user']:44} {x['hf']:>7.4f} {x['new_hf']:>7.4f} {x['debt']:>12,.0f}")
    print()
    print("  各档位所需跌幅:")
    for d in (1, 2, 3, 5, 8, 12, 20):
        h = project(pos, 1 - d / 100)
        print(f"    跌 {d:>2}%  →  {len(h):>3} 个仓位  ${sum(x['debt'] for x in h):>12,.0f}")
    print("=" * 78)
    print("⚠ 一阶近似(HF ∝ 价格),**偏激进** —— 真实仓位可能混着稳定币抵押物。")
    print("  P1 阶段用 eth_call 逐个模拟才能消除这个近似。")


def cmd_watch(args):
    data = load_positions()
    pos = data["positions"]
    base_px = data["eth_price"]
    STATE.mkdir(exist_ok=True)
    eprint(f"[watch] 名单 {len(pos)} 个仓位,建单时 ETH=${base_px:,.2f}")
    last = {"opt": None, "arb": None}

    def once():
        opt_px, opt_ts = latest_price("OPT", FEED_OPT)
        arb_px, arb_ts = latest_price("ARB", FEED_ARB)
        # 核心:用 **OPT 的价格** 推算 ARB 上的仓位
        ratio_opt = opt_px / base_px
        ratio_arb = arb_px / base_px
        hit_opt = project(pos, ratio_opt)
        hit_arb = project(pos, ratio_arb)
        # OPT 已经看到、但 ARB 还没反映的 —— **这就是那 34 秒的窗口**
        ahead = [x for x in hit_opt
                 if x["user"] not in {y["user"] for y in hit_arb}]
        rec = {"ts": utcnow(), "opt_px": opt_px, "arb_px": arb_px,
               "spread_bps": (opt_px - arb_px) / arb_px * 10_000,
               "opt_feed_ts": opt_ts, "arb_feed_ts": arb_ts,
               "n_liquidatable_by_opt": len(hit_opt),
               "n_liquidatable_by_arb": len(hit_arb),
               "n_ahead": len(ahead),
               "ahead": [{"user": x["user"], "hf": x["hf"],
                          "new_hf": x["new_hf"], "debt": x["debt"]}
                         for x in ahead[:20]]}
        append_jsonl(PREDICTIONS, rec)
        moved = (last["opt"] != opt_px) or (last["arb"] != arb_px)
        last["opt"], last["arb"] = opt_px, arb_px
        flag = ""
        if ahead:
            flag = f"  ★ OPT 已见 {len(ahead)} 个可清算,ARB 还没反映"
        if moved or ahead:
            print(f"[{rec['ts'][11:19]}] OPT ${opt_px:,.2f}  ARB ${arb_px:,.2f}  "
                  f"差 {rec['spread_bps']:+.1f}bps  "
                  f"可清算 OPT={len(hit_opt)} ARB={len(hit_arb)}{flag}", flush=True)

    run_loop(once, interval=args.interval, max_rounds=args.max_rounds,
             label="p0-shadow")


def verify_feeds():
    """
    问 Aave 自己:你读的是哪个喂价?

    **这一步是 P0 最早版本漏掉的,直接导致整条 ARB 腿盯错了对象。**
    教训:喂价地址不能从"Chainlink 官方 ETH/USD 代理"倒推,
    必须从协议本身查出来 —— 协议随时可以换源,而且 SVR 喂价和
    普通喂价的接口一模一样(官方文档明确说明),光看接口分辨不出。
    """
    from web3 import Web3
    WETH = {"ARB": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
            "OPT": "0x4200000000000000000000000000000000000006"}
    print("=== Aave V3 实际读取的 ETH/USD 喂价 ===\n")
    bad = 0
    for ch, expect in AAVE_FEED.items():
        c = w3(ch)
        ap = "0x" + c.eth.call({"to": Web3.to_checksum_address(AAVE_POOL),
                                "data": sel("ADDRESSES_PROVIDER()")}).hex()[-40:]
        orc = "0x" + c.eth.call({"to": Web3.to_checksum_address(ap),
                                 "data": sel("getPriceOracle()")}).hex()[-40:]
        src = "0x" + c.eth.call({
            "to": Web3.to_checksum_address(orc),
            "data": sel("getSourceOfAsset(address)")
                    + WETH[ch][2:].lower().rjust(64, "0")}).hex()[-40:]
        ok = src == expect.lower()
        bad += 0 if ok else 1
        px, ts = latest_price(ch, src)
        age = int(time.time()) - ts
        print(f"  {ch}  Aave 读 {src}")
        print(f"      脚本用 {expect.lower()}   {'✅ 一致' if ok else '🔴 不一致,脚本盯错了!'}")
        print(f"      当前 ${px:,.4f}  {age}s 前更新")
        if ch == "ARB":
            ppx, pts = latest_price(ch, FEED_ARB_PUBLIC)
            print(f"      对照·公开代理 ${ppx:,.4f}  {int(time.time())-pts}s 前更新"
                  f"  → 差 {(px-ppx)/ppx*1e4:+.1f} bps")
    print()
    if bad:
        print("🔴 有链盯错了喂价,预测全部无效 —— 先改常量再跑。")
    else:
        print("✅ 全部一致。")
    return 1 if bad else 0


def main():
    p = argparse.ArgumentParser(description="P0 影子模式:只记录,不发交易")
    p.add_argument("--verify-feeds", action="store_true",
                   help="核对 Aave 实际读的喂价与脚本常量是否一致(跑 --watch 前先跑这个)")
    p.add_argument("--refresh", action="store_true", help="重建高危仓位名单")
    p.add_argument("--days", type=float, default=3, help="--refresh 扫多少天的 Borrow")
    p.add_argument("--hf-max", type=float, default=3.0, help="名单只留 HF 低于此值的")
    p.add_argument("--what-if", type=float, metavar="PCT",
                   help="单次评估:价格变动 PCT%% 时谁会被清算(负数=跌)")
    p.add_argument("--watch", action="store_true", help="持续影子监控")
    p.add_argument("--interval", type=int, default=30, help="--watch 的轮询间隔(秒)")
    p.add_argument("--max-rounds", type=int, default=0)
    args = p.parse_args()

    if args.verify_feeds:
        return verify_feeds()
    if args.refresh:
        refresh_positions(args.days, args.hf_max)
        return 0
    if args.what_if is not None:
        cmd_whatif(args.what_if)
        return 0
    if args.watch:
        cmd_watch(args)
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
