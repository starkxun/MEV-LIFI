#!/usr/bin/env python3
"""
p1_simulate.py —— P1 模拟验证(只读,不发交易)

P0 的预测建立在一个粗糙的一阶近似上:

    HF_new ≈ HF_now × (P_new / P_now)

它假设「抵押物全是随该喂价波动的资产、债务全是稳定币」。
真实仓位往往不是 —— 混着稳定币抵押物的仓位跌得慢得多。
**所以 P0 会高估将被清算的数量。**

P1 分两步消除它:

  A. 精确拆解 —— 把仓位拆到每个资产,自己算 HF,
     再和 `getUserAccountData` 的 HF 对账。
     **对不上就说明我的拆解有错**,这是外部可验证量。

  B. 模拟清算 —— 用 `eth_call` 调 liquidationCall,看 revert 内容。
     关键洞察:**Aave 的健康度检查发生在代币转账之前**,所以用一个
     没钱的地址去模拟,revert 原因就能区分:

        revert HealthFactorNotBelowThreshold  → Aave 拒绝,仓位健康
        revert Collateral/Currency 类错误      → Aave 拒绝,参数不对
        revert 在 ERC20 转账处                → **Aave 全部检查通过了**
        revert 数据为空                        → 函数不存在(签名错了)

     不需要给自己凭空塞钱,也不需要状态覆盖。

    python3 p1_simulate.py --chain OPT
    python3 p1_simulate.py --chain OPT --limit 20 --drop 10
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
CHAIN = {"OPT": {"slug": "optimism"}, "ARB": {"slug": "arbitrum"}}

# 用一个绝对没有余额、也没有授权的地址去模拟。
# 用零地址会被某些 RPC 特殊处理,所以用一个明显是"燃烧"的地址。
PROBE = "0x000000000000000000000000000000000000dEaD"

_CONN = {}


def key():
    for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith("ANKR_KEY="):
            return ln.split("=", 1)[1].strip()
        if ln.startswith("SUI_RPC=") and "ankr.com" in ln:
            return ln.split("=", 1)[1].strip().rstrip("/").split("/")[-1]
    raise RuntimeError(".env 里找不到 Ankr key")


def w3(chain):
    from web3 import Web3
    if chain not in _CONN:
        _CONN[chain] = Web3(Web3.HTTPProvider(
            f"https://rpc.ankr.com/{CHAIN[chain]['slug']}/{key()}",
            request_kwargs={"timeout": 60}))
    return _CONN[chain]


def retry(chain, fn, n=4):
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            # 业务层 revert 不该重试 —— 那是结果,不是故障
            if "revert" in str(e).lower() or "0x" == str(e)[:2]:
                raise
            if i == n - 1:
                raise
            time.sleep(1.2 * (i + 1))
            _CONN.pop(chain, None)


def sel(sig):
    from web3 import Web3
    return Web3.to_hex(Web3.keccak(text=sig))[:10]


def call(chain, to, data, frm=None):
    from web3 import Web3
    tx = {"to": Web3.to_checksum_address(to), "data": data}
    if frm:
        tx["from"] = Web3.to_checksum_address(frm)
    return retry(chain, lambda: w3(chain).eth.call(tx))


# ── Aave v3.4 用 custom error,4 字节选择器。运行时算,不写死 ──────
ERRORS = [
    "HealthFactorNotBelowThreshold()", "CollateralCannotBeLiquidated()",
    "SpecifiedCurrencyNotBorrowedByUser()", "InvalidAmount()",
    "ReserveInactive()", "ReserveFrozen()", "ReservePaused()",
    "PriceOracleSentinelCheckFailed()", "MustNotLeaveDust()",
    "LiquidationGracePeriodNotExpired()", "ZeroAddressNotValid()",
    "CollateralCannotCoverNewBorrow()", "InvalidLiquidationGracePeriod()",
]


def error_table():
    from web3 import Web3
    return {Web3.to_hex(Web3.keccak(text=e))[:10]: e for e in ERRORS}


# ══════════════════════════════════════════════════════════════════
#   A. 精确拆解
# ══════════════════════════════════════════════════════════════════
def load_reserves(chain):
    """储备清单 + 每个资产的价格、清算阈值、清算奖励、小数位。"""
    raw = call(chain, AAVE_POOL, sel("getReservesList()")).hex()
    n = int(raw[64:128], 16)
    assets = ["0x" + raw[128 + i * 64 + 24: 128 + (i + 1) * 64] for i in range(n)]

    ap = "0x" + call(chain, AAVE_POOL, sel("ADDRESSES_PROVIDER()")).hex()[-40:]
    oracle = "0x" + call(chain, ap, sel("getPriceOracle()")).hex()[-40:]
    dp = "0x" + call(chain, ap, sel("getPoolDataProvider()")).hex()[-40:]

    out = []
    for a in assets:
        cfg = int(call(chain, AAVE_POOL,
                       sel("getConfiguration(address)") + a[2:].rjust(64, "0")).hex(), 16)
        try:
            px = int(call(chain, oracle,
                          sel("getAssetPrice(address)") + a[2:].rjust(64, "0")).hex(), 16) / 1e8
        except Exception:
            px = 0.0
        try:
            h = call(chain, a, sel("symbol()")).hex()
            ln = int(h[64:128], 16)
            sym = bytes.fromhex(h[128:128 + ln * 2]).decode()
        except Exception:
            sym = a[:8]
        out.append({
            "asset": a, "symbol": sym,
            # ReserveConfigurationMap 位域(Aave V3)
            "decimals": (cfg >> 48) & 0xFF,
            "ltv": (cfg & 0xFFFF) / 1e4,
            "liq_threshold": ((cfg >> 16) & 0xFFFF) / 1e4,
            "liq_bonus": ((cfg >> 32) & 0xFFFF) / 1e4,
            "price": px,
        })
    return out, oracle, dp


_EMODE = {}


def emode_threshold(chain, cat):
    """
    取 eMode 类别的清算阈值。

    ⚠️ `getEModeCategoryData(uint8)` 返回的是**动态结构体**,
       ABI 编码第一个字是偏移量 0x20,不是数据。
       按静态字段解会整体错位一格 —— 我第一版就是这么错的,
       解出 "LTV 0.0032" 这种荒谬值。
       **荒谬到一眼能看出来是运气好;错位后落在合理区间才是真危险。**
    """
    if cat in _EMODE:
        return _EMODE[cat]
    h = call(chain, AAVE_POOL,
             sel("getEModeCategoryData(uint8)") + format(cat, "064x")).hex()
    off = int(h[0:64], 16)
    base = 64 if off == 32 else 0          # 有偏移量就跳过第一个字
    ltv = int(h[base:base + 64], 16) / 1e4
    lt = int(h[base + 64:base + 128], 16) / 1e4
    _EMODE[cat] = {"ltv": ltv, "liq_threshold": lt}
    return _EMODE[cat]


def decompose(chain, user, reserves, dp):
    """把一个仓位拆到每个资产,并自己算 HF。

    **必须处理 eMode** —— 开了 eMode 的仓位用类别阈值(可高到 0.95),
    而不是各资产自己的阈值(0.79~0.83)。忽略它会把 HF 算低 17%,
    也就是**把一个健康仓位误判成可清算**。
    """
    bm = int(call(chain, AAVE_POOL,
                  sel("getUserConfiguration(address)") + user[2:].lower().rjust(64, "0")).hex(), 16)
    coll, debt = [], []
    for i, r in enumerate(reserves):
        is_borrow = (bm >> (2 * i)) & 1
        is_coll = (bm >> (2 * i + 1)) & 1
        if not (is_borrow or is_coll):
            continue
        d = call(chain, dp, sel("getUserReserveData(address,address)")
                 + r["asset"][2:].rjust(64, "0") + user[2:].lower().rjust(64, "0")).hex()
        # 返回值 0:aToken 余额 1:稳定债 2:浮动债 …
        a_bal = int(d[0:64], 16) / 10 ** r["decimals"]
        s_debt = int(d[64:128], 16) / 10 ** r["decimals"]
        v_debt = int(d[128:192], 16) / 10 ** r["decimals"]
        if is_coll and a_bal > 0:
            coll.append({**r, "amount": a_bal, "usd": a_bal * r["price"]})
        if is_borrow and (s_debt + v_debt) > 0:
            debt.append({**r, "amount": s_debt + v_debt,
                         "usd": (s_debt + v_debt) * r["price"]})
    cat = int(call(chain, AAVE_POOL,
                   sel("getUserEMode(address)") + user[2:].lower().rjust(64, "0")).hex(), 16)
    if cat:
        lt = emode_threshold(chain, cat)["liq_threshold"]
        for c in coll:
            c["liq_threshold_used"] = lt
    else:
        for c in coll:
            c["liq_threshold_used"] = c["liq_threshold"]

    num = sum(c["usd"] * c["liq_threshold_used"] for c in coll)
    den = sum(d["usd"] for d in debt)
    hf = num / den if den > 0 else float("inf")
    return {"user": user, "collateral": coll, "debt": debt, "emode": cat,
            "hf_computed": hf, "debt_usd": den,
            "collateral_usd": sum(c["usd"] for c in coll)}


def onchain_hf(chain, user):
    b = bytes(call(chain, AAVE_POOL,
                   sel("getUserAccountData(address)") + user[2:].lower().rjust(64, "0")))
    raw = int.from_bytes(b[160:192], "big")
    return float("inf") if raw >= 2 ** 200 else raw / 1e18


def project_exact(pos, drop_pct, stable={"USDC", "USDT", "USDC.e", "DAI", "USD₮0",
                                         "sUSD", "LUSD", "GHO", "FRAX", "MAI"}):
    """
    只把**非稳定币**的价格打下去,稳定币不动。
    这才是 P0 那个一阶近似缺的东西。
    """
    r = 1 + drop_pct / 100.0
    num = sum(c["usd"] * (1 if c["symbol"] in stable else r) * c["liq_threshold_used"]
              for c in pos["collateral"])
    den = sum(d["usd"] * (1 if d["symbol"] in stable else r) for d in pos["debt"])
    return num / den if den > 0 else float("inf")


# ══════════════════════════════════════════════════════════════════
#   B. 模拟清算
# ══════════════════════════════════════════════════════════════════
def simulate(chain, user, coll_asset, debt_asset, amount_raw, errs):
    """
    eth_call 模拟 liquidationCall,靠 revert 内容分类。

    用一个没有余额、没有授权的地址发起 —— 因为 Aave 的健康度检查
    在代币转账**之前**,所以:
        Aave 层的错误  → 这笔清算不成立
        转账层的错误   → **Aave 全部检查已通过**
    """
    data = (sel("liquidationCall(address,address,address,uint256,bool)")
            + coll_asset[2:].rjust(64, "0")
            + debt_asset[2:].rjust(64, "0")
            + user[2:].lower().rjust(64, "0")
            + format(amount_raw, "064x")
            + format(0, "064x"))
    try:
        call(chain, AAVE_POOL, data, frm=PROBE)
        return "无 revert(异常)", None
    except Exception as e:
        m = str(e)
        # 抓 4 字节 custom error
        for h, name in errs.items():
            if h in m:
                return "AAVE_拒绝", name
        if "0x" in m and "execution reverted" in m.lower():
            # 有 revert 数据但不认识 —— 大概率是 ERC20 层
            return "通过AAVE检查", m[:90]
        if "execution reverted" in m.lower():
            return "空revert", m[:90]
        return "其他", m[:90]


def main():
    p = argparse.ArgumentParser(description="P1 模拟验证(只读)")
    p.add_argument("--chain", default="OPT", choices=["OPT", "ARB"])
    p.add_argument("--limit", type=int, default=25, help="只跑名单里最危险的前 N 个")
    p.add_argument("--drop", type=float, default=10.0, help="精确推演的跌幅 %%")
    p.add_argument("--out", default="shadow/p1_simulation.json")
    args = p.parse_args()

    ch = args.chain
    data = json.loads((ROOT / "shadow" / "positions.json").read_text(encoding="utf-8"))
    if data.get("chain") and data["chain"] != ch:
        raise SystemExit(f"名单是 {data['chain']} 的,不是 {ch}。先跑 p0_shadow.py --refresh --chain {ch}")
    positions = data["positions"][:args.limit]
    errs = error_table()

    print(f"加载 {ch} 储备清单 …")
    reserves, oracle, dp = load_reserves(ch)
    print(f"  {len(reserves)} 个资产,DataProvider {dp}\n")

    print(f"{'='*78}")
    print(f"=== A. 精确拆解 + 对账(前 {len(positions)} 个仓位)===")
    print(f"{'='*78}")
    rows, mismatch = [], 0
    for i, p0 in enumerate(positions):
        u = p0["user"]
        try:
            d = decompose(ch, u, reserves, dp)
            hf_chain = onchain_hf(ch, u)
        except Exception as e:
            print(f"  {u[:12]}…  拆解失败 {str(e)[:50]}")
            continue
        err = (abs(d["hf_computed"] - hf_chain) / hf_chain * 100
               if hf_chain not in (0, float("inf")) else 0)
        ok = err < 1.0
        if not ok:
            mismatch += 1
        cs = "+".join(f"{c['symbol']}" for c in d["collateral"]) or "无"
        ds = "+".join(f"{x['symbol']}" for x in d["debt"]) or "无"
        d["hf_onchain"] = hf_chain
        d["hf_err_pct"] = err
        d["hf_projected"] = project_exact(d, -args.drop)
        d["hf_naive"] = hf_chain * (1 - args.drop / 100)
        rows.append(d)
        em = f" eMode={d['emode']}" if d.get("emode") else ""
        print(f"  {u[:12]}…  抵押[{cs:<18}] 债务[{ds:<12}]{em:<9} "
              f"HF 我算 {d['hf_computed']:.4f} / 链上 {hf_chain:.4f} "
              f"{'✅' if ok else f'🔴 差 {err:.1f}%'}")

    print(f"\n  对账:{len(rows)-mismatch}/{len(rows)} 一致(误差 <1%)")
    if mismatch:
        print(f"  🔴 {mismatch} 个对不上 —— 拆解逻辑有问题,后面的推演不可信")

    print(f"\n{'='*78}")
    print(f"=== 一阶近似 vs 精确推演(跌 {args.drop}%)===")
    print(f"{'='*78}")
    n_naive = sum(1 for r in rows if r["hf_naive"] < 1)
    n_exact = sum(1 for r in rows if r["hf_projected"] < 1)
    print(f"  P0 一阶近似预测可清算:  {n_naive} 个")
    print(f"  P1 精确推演预测可清算:  {n_exact} 个")
    if n_naive:
        print(f"  → 一阶近似**高估了 {n_naive-n_exact} 个**"
              f"({(n_naive-n_exact)/n_naive*100:.0f}%)"
              if n_naive >= n_exact else
              f"  → 一阶近似**低估了 {n_exact-n_naive} 个**")
    print(f"\n  差异最大的几个:")
    for r in sorted(rows, key=lambda x: -abs(x["hf_naive"] - x["hf_projected"]))[:6]:
        cs = "+".join(c["symbol"] for c in r["collateral"])
        print(f"    {r['user'][:12]}…  近似 {r['hf_naive']:.4f}  精确 {r['hf_projected']:.4f}"
              f"   抵押[{cs}]")

    print(f"\n{'='*78}")
    print(f"=== B. 模拟清算(eth_call,不发交易)===")
    print(f"{'='*78}")
    verdict = Counter()
    for r in rows:
        if not r["collateral"] or not r["debt"]:
            verdict["无抵押或无债务"] += 1
            continue
        c = max(r["collateral"], key=lambda x: x["usd"])
        d = max(r["debt"], key=lambda x: x["usd"])
        amt = int(d["amount"] * 0.5 * 10 ** d["decimals"])   # closeFactor 上限一般 50%
        kind, detail = simulate(ch, r["user"], c["asset"], d["asset"], amt, errs)
        verdict[kind] += 1
        r["sim"] = {"kind": kind, "detail": detail,
                    "collateral": c["symbol"], "debt": d["symbol"]}
        mark = {"AAVE_拒绝": "  ", "通过AAVE检查": "★ "}.get(kind, "? ")
        print(f"  {mark}{r['user'][:12]}…  HF {r['hf_onchain']:.4f}  "
              f"{d['symbol']}→{c['symbol']}  {kind}"
              + (f"  [{detail}]" if kind == "AAVE_拒绝" else ""))

    print(f"\n  === 判定汇总 ===")
    tot = sum(verdict.values())
    for k, v in verdict.most_common():
        print(f"    {k:<16} {v:>3}  ({v/tot*100:>5.1f}%)")
    passed = verdict.get("通过AAVE检查", 0)
    print(f"\n  **模拟通过率 {passed}/{tot} = {passed/tot*100:.1f}%**")
    print(f"  → 这是「失败交易概率」的第一个估计:失败 {100-passed/tot*100:.1f}%")
    print(f"  ⚠️ 注意:这是**当前价格**下的通过率。名单里多数仓位现在还健康,")
    print(f"     所以低通过率是预期内的,不代表策略失败。")

    (ROOT / args.out).write_text(json.dumps(
        {"chain": ch, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
         "drop_pct": args.drop, "rows": rows}, ensure_ascii=False, indent=1,
        default=str), encoding="utf-8")
    print(f"\n已存 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
