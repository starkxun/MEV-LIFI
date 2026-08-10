#!/usr/bin/env python3
"""
svr_probe.py —— Chainlink SVR / Atlas 探针(只读,不发任何交易)

把「今天怎么把 SVR 这套东西查清楚的」固化成可重复执行的命令。
每个子命令对应教学文档 docs/week_3/SVR与Atlas教学.md 里的一节。

    python3 svr_probe.py feeds              # Aave 到底读哪个喂价
    python3 svr_probe.py atlas              # Atlas 合约身份 + 押金门槛
    python3 svr_probe.py solvers --days 7   # 谁在中标,出价多少
    python3 svr_probe.py anatomy <txhash>   # 解剖一笔 SVR 清算

设计原则(和项目里其他脚本一致):
  1. 地址一律现算或现查,不写死推测值
  2. 拿不到的数据就说拿不到,不用估算值冒充实测
  3. 每个数字都能追到一次链上调用
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).parent

# ── 各链常量。Aave V3 Pool 在多条链上是同一个地址(确定性部署)
AAVE_POOL = "0x794a61358D6845594F94dc1DB02A252b5b4814aD"
CHAINS = {
    "ARB":  {"slug": "arbitrum", "blocktime": 0.25,
             "weth": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
             "atlas": "0x8ad1ae9d97c79aa68a0a151e83ff3942f68f86c1"},
    "OPT":  {"slug": "optimism", "blocktime": 2.0,
             "weth": "0x4200000000000000000000000000000000000006",
             "atlas": None},     # Optimism 没上 SVR
    "BASE": {"slug": "base", "blocktime": 2.0,
             "weth": "0x4200000000000000000000000000000000000006",
             "atlas": None},     # 有 SVR,但 Atlas 地址未查证 → 不猜
}

# 公开 Chainlink ETH/USD 代理,用来做「Aave 读的是不是这个」的对照
PUBLIC_ETHUSD = {
    "ARB":  "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",
    "OPT":  "0x13e3Ee699D1909E989722E753853AE30b17e08c5",
    "BASE": "0x71041dddad3595F9CEd3DcCFBe3D1F4b0a16Bb70",
}

MAX_LOG_RANGE = 9000        # Ankr 硬上限,实测出来的


def key():
    for ln in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith("ANKR_KEY="):
            return ln.split("=", 1)[1].strip()
        if ln.startswith("SUI_RPC=") and "ankr.com" in ln:
            return ln.split("=", 1)[1].strip().rstrip("/").split("/")[-1]
    raise RuntimeError(".env 里找不到 Ankr key(ANKR_KEY= 或 SUI_RPC=)")


_conn = {}


def w3(chain):
    from web3 import Web3
    if chain not in _conn:
        _conn[chain] = Web3(Web3.HTTPProvider(
            f"https://rpc.ankr.com/{CHAINS[chain]['slug']}/{key()}",
            request_kwargs={"timeout": 60}))
    return _conn[chain]


def retry(chain, fn, n=4):
    """Ankr 公共端点会 reset,重连而不是硬失败。"""
    from web3 import Web3
    for i in range(n):
        try:
            return fn()
        except Exception:
            if i == n - 1:
                raise
            time.sleep(1.2 * (i + 1))
            _conn[chain] = Web3(Web3.HTTPProvider(
                f"https://rpc.ankr.com/{CHAINS[chain]['slug']}/{key()}",
                request_kwargs={"timeout": 60}))


def sel(sig):
    from web3 import Web3
    return Web3.to_hex(Web3.keccak(text=sig))[:10]


def call(chain, to, data):
    from web3 import Web3
    return retry(chain, lambda: w3(chain).eth.call(
        {"to": Web3.to_checksum_address(to), "data": data}))


def addr_of(chain, to, sig):
    """调一个返回 address 的 view 方法。"""
    return "0x" + call(chain, to, sel(sig)).hex()[-40:]


def uint_of(chain, to, sig, arg=None):
    d = sel(sig) + (arg[2:].lower().rjust(64, "0") if arg else "")
    return int(call(chain, to, d).hex(), 16)


def string_of(chain, to, sig):
    h = call(chain, to, sel(sig)).hex()
    n = int(h[64:128], 16)
    return bytes.fromhex(h[128:128 + n * 2]).decode()


def latest_round(chain, feed):
    """latestRoundData() → (价格, 更新时间戳)。第 2、4 个返回值。"""
    b = bytes(call(chain, feed, sel("latestRoundData()")))
    ans = int.from_bytes(b[32:64], "big")
    if ans >= 2 ** 255:
        ans -= 2 ** 256
    return ans / 1e8, int.from_bytes(b[96:128], "big")


# ══════════════════════════════════════════════════════════════════
def cmd_feeds(args):
    """
    问 Aave 自己:你读的是哪个喂价?

    这一步是整件事的起点。**喂价地址不能从「Chainlink 官方 ETH/USD 代理」
    倒推** —— 协议随时可以换源,而 SVR 喂价和普通喂价的接口一模一样
    (Chainlink 官方文档明确说明),光看接口分辨不出来。
    """
    print("=== Aave V3 实际读取的 ETH/USD 喂价 ===\n")
    print("调用链: Pool.ADDRESSES_PROVIDER() → .getPriceOracle() → .getSourceOfAsset(WETH)\n")
    for ch in args.chains:
        c = CHAINS[ch]
        try:
            ap = addr_of(ch, AAVE_POOL, "ADDRESSES_PROVIDER()")
            orc = addr_of(ch, ap, "getPriceOracle()")
            src = "0x" + call(ch, orc, sel("getSourceOfAsset(address)")
                              + c["weth"][2:].lower().rjust(64, "0")).hex()[-40:]
        except Exception as e:
            print(f"  {ch:<5} 读取失败: {str(e)[:60]}\n")
            continue
        pub = PUBLIC_ETHUSD[ch].lower()
        same = src == pub
        px, ts = latest_round(ch, src)
        age = int(time.time()) - ts
        print(f"  {ch}")
        print(f"    AaveOracle       {orc}")
        print(f"    Aave 读的喂价     {src}")
        print(f"    公开 Chainlink   {pub}")
        print(f"    {'✅ 同一个 —— 这条链没上 SVR' if same else '🔴 不是同一个 —— 这就是 SVR 喂价'}")
        print(f"    当前 ${px:,.4f}  {age}s 前更新")
        if not same:
            ppx, pts = latest_round(ch, pub)
            print(f"    对照·公开代理 ${ppx:,.4f}  {int(time.time())-pts}s 前更新"
                  f"   → 差 {(px-ppx)/ppx*1e4:+.1f} bps")
            try:
                agg = addr_of(ch, src, "aggregator()")
                pagg = addr_of(ch, pub, "aggregator()")
                ls = len(retry(ch, lambda: w3(ch).eth.get_code(
                    __import__("web3").Web3.to_checksum_address(agg))))
                lp = len(retry(ch, lambda: w3(ch).eth.get_code(
                    __import__("web3").Web3.to_checksum_address(pagg))))
                print(f"    SVR 聚合器 {agg}  ({ls} 字节)")
                print(f"    公开聚合器 {pagg}  ({lp} 字节)")
                print(f"    → 字节码差 {ls-lp:+d} 字节。**但函数选择器完全相同**,")
                print(f"       所以不能靠接口区分 SVR —— 只能靠「Aave 指向别处」这个事实。")
            except Exception:
                pass
        print()
    print("解读:")
    print("  Aave 指向的地址 ≠ 公开代理  →  这条链上该资产走 SVR")
    print("  两者一致                    →  没上 SVR,和以前一样是公开抢跑")


# ══════════════════════════════════════════════════════════════════
def cmd_atlas(args):
    """Atlas 合约身份 + 押金门槛。"""
    ch = args.chain
    at = CHAINS[ch]["atlas"]
    if not at:
        print(f"{ch} 没有已查证的 Atlas 地址。")
        print("找法:扫该链 Aave Pool 的 LiquidationCall,看 tx.to 是不是 Pool 本身;")
        print("      不是的话,对那个合约调 name(),Atlas 会返回 'Atlas ETH' 之类。")
        return 1
    print(f"=== Atlas on {ch}: {at} ===\n")
    for f in ["name()", "version()"]:
        try:
            print(f"  {f:<22} {string_of(ch, at, f)}")
        except Exception:
            pass
    for f in ["VERIFICATION()", "SIMULATOR()", "FACTORY_LIB()"]:
        try:
            print(f"  {f:<22} {addr_of(ch, at, f)}")
        except Exception:
            pass
    print()
    bt = CHAINS[ch]["blocktime"]
    for f, note in [("ESCROW_DURATION()", "解绑后要等的区块数"),
                    ("SCALE()", "比例基数"),
                    ("bondedTotalSupply()", "全网押金总额(ETH)"),
                    ("totalSupply()", "未绑定 atlETH(ETH)")]:
        try:
            v = uint_of(ch, at, f)
            if "Supply" in f:
                print(f"  {f:<22} {v/1e18:,.4f} ETH")
            elif "ESCROW" in f:
                print(f"  {f:<22} {v} 区块  ≈ {v*bt:.0f} 秒 ({note})")
            else:
                print(f"  {f:<22} {v}  ({note})")
        except Exception:
            print(f"  {f:<22} 读不到")
    print("\n押金怎么查:balanceOfBonded(address) —— 注意地址要用 **solverFrom**")
    print("(即 SolverTxResult 的第 2 个 indexed 字段),不是发交易的 EOA,")
    print("也不是 LiquidationCall 里的 liquidator(那是执行环境)。")
    return 0


# ══════════════════════════════════════════════════════════════════
SOLVER_TX_RESULT = "SolverTxResult(address,address,address,address,uint256,bool,bool,uint256)"


def cmd_solvers(args):
    """扫 Atlas 的 SolverTxResult,看谁在中标、出价多少。"""
    from web3 import Web3
    ch = args.chain
    at = CHAINS[ch]["atlas"]
    if not at:
        print(f"{ch} 没有已查证的 Atlas 地址"); return 1
    topic = Web3.to_hex(Web3.keccak(text=SOLVER_TX_RESULT))
    head = retry(ch, lambda: w3(ch).eth.block_number)
    blocks = int(args.days * 86400 / CHAINS[ch]["blocktime"])
    start = head - blocks
    print(f"扫 {args.days} 天 ({start} → {head}),约 {blocks//MAX_LOG_RANGE} 次请求 …")
    rows, b, t0, skipped = [], start, time.time(), 0
    while b < head:
        e = min(b + MAX_LOG_RANGE, head)
        try:
            for l in retry(ch, lambda: w3(ch).eth.get_logs({
                    "address": Web3.to_checksum_address(at), "topics": [topic],
                    "fromBlock": b, "toBlock": e})):
                d = l["data"].hex()
                rows.append({"blk": l["blockNumber"], "tx": l["transactionHash"].hex(),
                             "solverTo": "0x" + l["topics"][1].hex()[-40:],
                             "solverFrom": "0x" + l["topics"][2].hex()[-40:],
                             "control": "0x" + l["topics"][3].hex()[-40:],
                             "bid": int(d[64:128], 16),
                             "success": bool(int(d[192:256], 16))})
        except Exception:
            skipped += 1
        b = e + 1
    print(f"  {len(rows)} 条,{time.time()-t0:.0f}s"
          + (f",{skipped} 段失败" if skipped else ""))
    if not rows:
        return 0
    ok = [r for r in rows if r["success"]]
    print(f"\n  success {len(ok)}/{len(rows)}"
          f"  ← 落败的出价**不上链**,所以这里看到的全是赢家")
    c = Counter(r["solverFrom"] for r in ok)
    n = max(len(ok), 1)
    print(f"\n=== 中标分布(按 solverFrom = 真正的 solver 身份)===")
    print(f"  {len(c)} 个 solver")
    for a, v in c.most_common(10):
        try:
            bonded = uint_of(ch, at, "balanceOfBonded(address)", a) / 1e18
            bs = f"押 {bonded:.4f} ETH"
        except Exception:
            bs = ""
        print(f"    {a}  {v:>6} ({v/n*100:>5.1f}%)  {bs}")
    print(f"  Top1 {c.most_common(1)[0][1]/n*100:.1f}%"
          f"   Top3 {sum(v for _, v in c.most_common(3))/n*100:.1f}%")
    bids = sorted(r["bid"] / 1e18 for r in ok)
    print(f"\n=== 出价(bidAmount,原生币)===")
    print(f"  中位 {statistics.median(bids):.6f}   均值 {statistics.mean(bids):.6f}"
          f"   最大 {max(bids):.6f}")
    print(f"  非零出价 {sum(1 for x in bids if x>0)}/{len(bids)}")
    ctl = Counter(r["control"] for r in ok)
    print(f"\n=== dAppControl(业务线)===")
    for a, v in ctl.most_common(5):
        print(f"    {a}  ×{v}")
    if args.save:
        json.dump(rows, open(args.save, "w"))
        print(f"\n已存 {args.save}")
    return 0


# ══════════════════════════════════════════════════════════════════
def cmd_anatomy(args):
    """解剖一笔清算:还了多少债、拿了多少抵押、毛奖励多少、出价多少。"""
    from web3 import Web3
    ch = args.chain
    h = args.tx if args.tx.startswith("0x") else "0x" + args.tx
    rc = retry(ch, lambda: w3(ch).eth.get_transaction_receipt(h))
    tx = retry(ch, lambda: w3(ch).eth.get_transaction(h))
    LC = "0x" + Web3.keccak(text="LiquidationCall(address,address,address,uint256,uint256,address,bool)").hex()
    STR = "0x" + Web3.keccak(text=SOLVER_TX_RESULT).hex()
    print(f"=== {h} ===")
    print(f"  发起 EOA  {tx['from']}")
    print(f"  目标合约  {tx['to']}")
    gas = rc["gasUsed"] * rc["effectiveGasPrice"] / 1e18
    print(f"  gasUsed {rc['gasUsed']:,}  实付 {gas:.6f} 原生币")
    print(f"  {len(rc['logs'])} 条日志,{len(set(l['address'].lower() for l in rc['logs']))} 个合约")

    def meta(a):
        try:
            return string_of(ch, a, "symbol()"), uint_of(ch, a, "decimals()")
        except Exception:
            return a[:8], 18

    gross_total = 0.0
    for l in rc["logs"]:
        if not l["topics"] or "0x" + l["topics"][0].hex() != LC:
            continue
        ca = "0x" + l["topics"][1].hex()[-40:]
        da = "0x" + l["topics"][2].hex()[-40:]
        d = l["data"].hex()
        debt, coll = int(d[:64], 16), int(d[64:128], 16)
        cs, cd = meta(ca); ds, dd = meta(da)
        dv, cv = debt / 10 ** dd, coll / 10 ** cd
        # 清算奖励参数在 reserve configuration 的 bit 32..47,10500 = 1.05
        lb = ((uint_of(ch, AAVE_POOL, "getConfiguration(address)", ca) >> 32) & 0xFFFF) / 10000.0
        implied = dv / cv if cv else 0
        gross = cv * implied * lb - dv
        gross_total += gross
        print(f"\n  ── LiquidationCall ──")
        print(f"    还 {dv:,.6f} {ds}   拿 {cv:.6f} {cs}")
        print(f"    清算奖励参数 {(lb-1)*100:.1f}%")
        print(f"    隐含成交价 {implied:,.4f} {ds}/{cs}")
        print(f"    反推预言机价 {implied*lb:,.4f}   ← 和当日真实价对一下,能校验解码对不对")
        print(f"    毛清算奖励 {gross:,.2f} {ds}")

    bid = None
    for l in rc["logs"]:
        if l["topics"] and "0x" + l["topics"][0].hex() == STR:
            bid = int(l["data"].hex()[64:128], 16) / 1e18
            print(f"\n  ── SolverTxResult ──")
            print(f"    solverTo   0x{l['topics'][1].hex()[-40:]}")
            print(f"    solverFrom 0x{l['topics'][2].hex()[-40:]}  ← 真正的 solver 身份")
            print(f"    bidAmount  {bid:.6f}   ← 交给 Aave+Chainlink 的钱")
    if bid is not None and gross_total > 0:
        print(f"\n  ⚠️ 毛奖励以债务币计价、出价以原生币计价,")
        print(f"     要比较得自己折算 —— 本脚本不替你猜汇率。")
    elif bid is None:
        print(f"\n  没有 SolverTxResult → 这笔**不是**走 Atlas 拍卖的,是普通清算。")
    return 0


def main():
    p = argparse.ArgumentParser(description="Chainlink SVR / Atlas 探针(只读)")
    sub = p.add_subparsers(dest="cmd")

    f = sub.add_parser("feeds", help="Aave 到底读哪个喂价")
    f.add_argument("--chains", nargs="+", default=["ARB", "OPT", "BASE"])
    f.set_defaults(func=cmd_feeds)

    a = sub.add_parser("atlas", help="Atlas 合约身份 + 押金门槛")
    a.add_argument("--chain", default="ARB")
    a.set_defaults(func=cmd_atlas)

    s = sub.add_parser("solvers", help="谁在中标,出价多少")
    s.add_argument("--chain", default="ARB")
    s.add_argument("--days", type=float, default=7)
    s.add_argument("--save")
    s.set_defaults(func=cmd_solvers)

    n = sub.add_parser("anatomy", help="解剖一笔清算")
    n.add_argument("tx")
    n.add_argument("--chain", default="ARB")
    n.set_defaults(func=cmd_anatomy)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
