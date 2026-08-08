#!/usr/bin/env python3
"""
actor_profile.py —— 给一个链上地址做画像:策略 / 基础设施 / 运气 怎么拆

共学 Week 2 任务:「研究人而不只是案例」。核心是那句归因拆分:

    哪些能力来自**策略**,哪些来自**基础设施**,哪些只是某个**时间窗口的运气**。

**一笔交易分不出这三样。** 它们的签名是统计性的,必须看分布:

    运气      →  极少数交易贡献绝大部分利润(集中度高、基尼大)
    基础设施  →  每笔都稳定地为速度付费(优先费/利润 比高)、失败率有特征
    策略      →  单笔经济学稳定(中位≈均值)、路径重复

所以这个脚本先算分布,再让你挑代表性的单笔去还原。

**挑单笔时挑中位数附近的,不要挑最赚的那笔** —— 最赚的那笔恰恰是运气,
拿它还原会写出一篇好看但没有代表性的报告。

用法:
    # Solana(免 key,公共 RPC)
    python3 actor_profile.py --chain sol --address J7GR6XoJ...Tx6Nf

    # Ethereum(用 .env 里的 Ankr key,和 SUI_RPC 同一个)
    python3 actor_profile.py --chain eth --address 0x6b75d8af...9a80

    # 批量跑榜单
    python3 actor_profile.py --chain eth --from-file 套利地址.md --classify --sample 20 --limit 20

    python3 actor_profile.py --chain sol --address ... --sample 100 --json out.json

⚠️ **这个脚本量的是"这个地址的钱怎么变的",不是"这个策略赚不赚钱"。**
   利润可能落到别的地址(机器人合约、独立收款钱包),量出来会偏低。
   看到系统性为负时,先怀疑口径而不是下结论 —— 我在 Solana 那次
   就因为只看原生 SOL 漏了 WSOL,得出过符号相反的结论。

⚠️⚠️ **2026-08-08 修正:只扫 Transfer 日志算利润是错的,而且会错成反的。**

   第一版的 ETH 分支把「WETH 的 Transfer 净变化」当利润、把「gas 成本」当
   "为速度付费"。在 0xf0570ec4 这类清算机器人上,两个都错:

     · 它给区块构建者的钱走 `block.coinbase.call{value:}("")` ——
       **原生 ETH 转账,不发任何 event**,只扫日志永远看不见。
     · 它解包 WETH 用的 `WETH9.withdraw()` **只发 Withdrawal,不发 Transfer**
       (deposit 同理只发 Deposit),所以这笔流出也看不见。

   两个盲点叠加,Transfer 净变化恰好 **恒等于付给构建者的竞价**:

       MM → 合约   +G          (Transfer,看得见)
       合约 → MM   −f          (Transfer,看得见)
       withdraw(T)             (Withdrawal,看不见)   T = coinbaseTip
       合约 → 收款  −(S−T)      (Transfer,看得见)    S = G−f
       ─────────────────────────────────────────────
       净变化 = G − f − (S−T) = T   ← 脚本把「竞价」当成了「利润」

   已在 tx 0xefe4f89f…e52762d 上 wei 级验证:Transfer 净变化
   0.536620432662676339 WETH,trace 实测付给 block.coinbase 的也是
   0.536620432662676339 ETH,差值为 0。

   现在的做法:
     · 竞价 —— 用 trace_transaction 实测转给 block.coinbase 的原生 ETH
     · WETH 净变化 —— Transfer delta **加上** Deposit / Withdrawal
     · 落袋 —— 单独统计转给收款地址(--receiver)的 WETH
     · gas 单独一栏,**不再叫"为速度付费"**
"""

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

import requests

SOL_RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

# topic0 一律现算,不凭记忆抄 —— 这个项目已经在这上面栽过三次。
def _topic0(sig):
    from eth_hash.auto import keccak
    return "0x" + keccak(sig.encode()).hex()

TRANSFER = _topic0("Transfer(address,address,uint256)")
# WETH9 的 deposit/withdraw **不发 Transfer**,只发这两个 —— 漏了它们,
# WETH 净变化就是错的(见模块 docstring 的 2026-08-08 修正)。
WITHDRAWAL = _topic0("Withdrawal(address,uint256)")
DEPOSIT = _topic0("Deposit(address,uint256)")

_S = requests.Session()
_S.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})


def ankr_key():
    """Ankr 的 key 在 .env 里,和 SUI_RPC 复用同一个 —— 实测同 key 通吃多链。"""
    p = Path(__file__).parent / ".env"
    if not p.exists():
        return None
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln.startswith("ANKR_KEY="):
            return ln.split("=", 1)[1].strip()
        if ln.startswith("SUI_RPC=") and "ankr.com" in ln:
            return ln.split("=", 1)[1].strip().rstrip("/").split("/")[-1]
    return None


def rpc(url, method, params, retries=4):
    delay = 1.0
    last = None
    for _ in range(retries):
        try:
            r = _S.post(url, json={"jsonrpc": "2.0", "id": 1,
                                   "method": method, "params": params}, timeout=60)
            if r.status_code == 200:
                d = r.json()
                if "result" in d:
                    return d["result"]
                last = str(d.get("error"))[:120]
            else:
                last = f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError) as e:
            last = type(e).__name__
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"{method} 失败: {last}")


# ============================================================
# Solana
# ============================================================

def sol_profile(addr, sample, max_pages=6):
    sigs, before = [], None
    for _ in range(max_pages):
        p = [addr, {"limit": 1000}]
        if before:
            p[1]["before"] = before
        r = rpc(SOL_RPC, "getSignaturesForAddress", p)
        if not r:
            break
        sigs += r
        before = r[-1]["signature"]
        if len(r) < 1000:
            break
    if not sigs:
        raise RuntimeError("没有交易记录")

    ok = [s for s in sigs if not s.get("err")]
    ts = [s["blockTime"] for s in sigs if s.get("blockTime")]
    span_h = (max(ts) - min(ts)) / 3600 if len(ts) > 1 else 0

    profits, fees, tips, skipped = [], [], [], []
    for s in [x["signature"] for x in ok][:sample]:
        try:
            r = rpc(SOL_RPC, "getTransaction",
                    [s, {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"}])
        except RuntimeError:
            continue
        if not r:
            continue
        meta, msg = r["meta"], r["transaction"]["message"]
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
        if addr not in keys:
            continue
        i = keys.index(addr)
        nat = (meta["postBalances"][i] - meta["preBalances"][i]) / 1e9
        pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances", [])}
        post = {b["accountIndex"]: b for b in meta.get("postTokenBalances", [])}
        wsol = 0.0
        for j in set(pre) | set(post):
            a, b = pre.get(j, {}), post.get(j, {})
            if (b.get("owner") or a.get("owner")) != addr:
                continue
            if (b.get("mint") or a.get("mint")) != WSOL:
                continue
            wsol += (float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
                     - float((a.get("uiTokenAmount") or {}).get("uiAmount") or 0))
        # **纯转账要剔除。** 实测这个地址有一笔送出 3.4 SOL、无任何 token 变化、
        # 只调了 System Program —— 那是资金归集,不是套利行为。
        # 一笔就把 60 笔的合计从 +3.07 翻成 −0.34。
        progs = set()
        for ins in msg.get("instructions", []):
            pid = ins.get("programId")
            if pid:
                progs.add(pid)
        for inner in (meta.get("innerInstructions") or []):
            for ins in inner.get("instructions", []):
                pid = ins.get("programId")
                if pid:
                    progs.add(pid)
        boring = {"11111111111111111111111111111111",
                  "ComputeBudget111111111111111111111111111111"}
        if not (progs - boring) and abs(wsol) < 1e-9:
            skipped.append(("纯转账", nat))
            continue

        fee = meta["fee"] / 1e9
        # 原生 SOL 的净支出里,超出手续费的部分基本就是给区块构建者的小费
        tips.append(max(0.0, -nat - fee))
        fees.append(fee)
        profits.append(nat + wsol)   # 原生 + WSOL,两个都要算(只看一个会得出反的结论)
        time.sleep(0.03)

    return {"total": len(sigs), "ok": len(ok), "span_h": span_h,
            "profits": profits, "fees": fees, "tips": tips, "unit": "SOL",
            "skipped": skipped}


# ============================================================
# Ethereum(Ankr Advanced API)
# ============================================================

def _miner_of(eth, blk_hex, cache):
    """区块的 fee recipient。缓存 —— 同一区块可能有多笔样本。"""
    if blk_hex not in cache:
        b = rpc(eth, "eth_getBlockByNumber", [blk_hex, False]) or {}
        cache[blk_hex] = (b.get("miner") or "").lower()
    return cache[blk_hex]


def _builder_bid(eth, txhash, miner):
    """
    实测这笔交易转给 `block.coinbase` 的原生 ETH。

    **必须走 trace。** 机器人给构建者的钱是 `block.coinbase.call{value:}("")`,
    原生转账不产生任何 log,receipt 里一个字都没有。第一版只扫日志,
    于是把这笔钱整个漏掉,还把它错记成了利润(见模块 docstring)。

    返回 (bid_wei, 来源)。trace 不可用时返回 (None, "unavailable") ——
    **返回 None 而不是 0**,免得"测不到"被下游当成"确实没付"。
    """
    if not miner:
        return None, "no-miner"
    try:
        tr = rpc(eth, "trace_transaction", [txhash], retries=2)
    except RuntimeError:
        return None, "unavailable"
    if tr is None:
        return None, "unavailable"
    bid = 0
    for t in tr:
        if t.get("type") != "call":
            continue
        act = t.get("action") or {}
        if (act.get("to") or "").lower() == miner:
            bid += int(act.get("value") or "0x0", 16)
    return bid, "trace"


def eth_profile(addr, sample, key, receiver=None):
    """
    以太坊画像。**合约和 EOA 要分开处理:**

      · EOA   —— 看它**发出**的交易
      · 合约  —— 看**发给它**的交易,利润从 receipt 日志里算它自己的 token 净变化

    第一版只写了 EOA 分支,遇到机器人合约直接报"没有一笔是该地址发出的" ——
    而榜单上的地址**几乎全是合约**(机器人不会用 EOA 直接干活)。

    `receiver` —— 机器人常把利润转到独立收款地址,合约自己净留 0。
    给了就单独统计「转给收款地址的 WETH」,那才是落袋数。
    """
    multi = f"https://rpc.ankr.com/multichain/{key}"
    eth = f"https://rpc.ankr.com/eth/{key}"
    a = addr.lower()
    rcv = (receiver or "").lower() or None

    code = rpc(eth, "eth_getCode", [a, "latest"]) or "0x"
    is_contract = len(code) > 2

    got, page = [], None
    while len(got) < sample * 3:
        p = {"blockchain": ["eth"], "address": [a], "pageSize": 100, "descOrder": True}
        if page:
            p["pageToken"] = page
        r = rpc(multi, "ankr_getTransactionsByAddress", p)
        batch = (r or {}).get("transactions") or []
        if not batch:
            break
        got += batch
        page = (r or {}).get("nextPageToken")
        if not page:
            break
        time.sleep(0.2)

    rel = ([t for t in got if (t.get("to") or "").lower() == a] if is_contract
           else [t for t in got if (t.get("from") or "").lower() == a])
    if not rel:
        raise RuntimeError(f"抓到 {len(got)} 笔,但没有一笔与该地址{'相关' if is_contract else '发出'}")

    ts = [int(t["timestamp"], 16) for t in rel if t.get("timestamp")]
    span_h = (max(ts) - min(ts)) / 3600 if len(ts) > 1 else 0
    okn = sum(1 for t in rel if t.get("status") == "0x1")

    profits, fees, tips, skipped, callers = [], [], [], [], {}
    bids, payouts, retained, no_trace = [], [], [], 0
    miners, two_way = {}, False
    for t in rel[:sample]:
        callers[(t.get("from") or "").lower()] = \
            callers.get((t.get("from") or "").lower(), 0) + 1
        if not is_contract and (t.get("input") or "0x") in ("0x", ""):
            skipped.append(("纯转账", int(t.get("value") or "0x0", 16) / 1e18))
            continue
        rc = rpc(eth, "eth_getTransactionReceipt", [t["hash"]])
        if not rc:
            continue
        gas_used = int(rc.get("gasUsed") or "0x0", 16)
        gas_price = int(rc.get("effectiveGasPrice") or t.get("gasPrice") or "0x0", 16)
        fee = gas_used * gas_price / 1e18

        # 从日志里算该地址的每种 token 净变化。**这比调 token-transfers API 准**,
        # 因为它就是这笔交易实际发生的事,不依赖第三方索引。
        deltas = {}
        wd = dp = 0          # WETH 的 withdraw / deposit —— 这两个不发 Transfer
        pay = 0              # 转给收款地址的 WETH
        for lg in (rc.get("logs") or []):
            tp = lg.get("topics") or []
            if not tp:
                continue
            tok = lg["address"].lower()
            try:
                data = int(lg["data"], 16)
            except (ValueError, KeyError):
                data = None

            # WETH9 的 deposit/withdraw 只发 Deposit/Withdrawal,**没有 Transfer**。
            # 漏了它们,WETH 净变化就不是余额变化,而是一个没有意义的中间量。
            if tok == WETH and data is not None and len(tp) >= 2:
                who = ("0x" + tp[1][-40:]).lower()
                if who == a and tp[0] == WITHDRAWAL:
                    wd += data
                elif who == a and tp[0] == DEPOSIT:
                    dp += data

            if len(tp) < 3 or tp[0] != TRANSFER or data is None:
                continue
            frm = ("0x" + tp[1][-40:]).lower()
            to = ("0x" + tp[2][-40:]).lower()
            if frm == a:
                deltas[tok] = deltas.get(tok, 0) - data
                if tok == WETH and rcv and to == rcv:
                    pay += data
            if to == a:
                deltas[tok] = deltas.get(tok, 0) + data
                # 收款地址**同时是交易对手**时(0xf0570ec4 的收款地址就是
                # Bebop 做市商 Wintermute),单向求和会把 RFQ 换币腿当成利润。
                # 必须取净额 —— 实测最大一笔 141.41 WETH 其实是抵押品卖出腿。
                if tok == WETH and rcv and frm == rcv:
                    pay -= data
                    two_way = True

        # WETH **余额**变化 = Transfer 净额 + deposit − withdraw。
        # 旧版只有第一项,于是这个数恒等于给构建者的竞价(见 docstring)。
        weth = (deltas.get(WETH, 0) + dp - wd) / 1e18

        miner = _miner_of(eth, rc.get("blockNumber"), miners)
        bid_wei, src = _builder_bid(eth, t["hash"], miner)
        if bid_wei is None:
            no_trace += 1
        bids.append(None if bid_wei is None else bid_wei / 1e18)
        payouts.append(pay / 1e18)
        retained.append(weth)

        others = {k: v for k, v in deltas.items() if k != WETH and abs(v) > 0}
        fees.append(fee)
        # tips 现在只装**给构建者的竞价**。旧版往这里塞 gas 成本,
        # 于是"为速度付费"算出来永远 ≈0 —— 那是在拿 gas 除以 gas。
        tips.append(0.0 if bid_wei is None else bid_wei / 1e18)
        # 分布分析用**毛机会** = 合约自留 + 竞价,也就是这笔清算到底抓到多大的肉。
        # 不能用"自留",转发型合约每笔都是 0,分布退化成一条零线。
        profits.append(weth + (0.0 if bid_wei is None else bid_wei / 1e18))
        if others:
            skipped.append((f"另有 {len(others)} 种 token 变化未计价", 0.0))
        time.sleep(0.06)

    return {"total": len(rel), "ok": okn, "span_h": span_h,
            "profits": profits, "fees": fees, "tips": tips, "unit": "WETH",
            "skipped": skipped, "callers": callers, "is_contract": is_contract,
            "bids": bids, "payouts": payouts, "retained": retained,
            "no_trace": no_trace, "receiver": rcv, "rcv_two_way": two_way}


# ============================================================
# 行为分类:先判断这是什么类型的玩家,再决定要不要做利润画像
# ============================================================

# 事件签名(topic0)。**只用 topic0 就够分类**,不需要 ABI、不需要解码参数、
# 不需要 trace —— 这是这套方法能便宜跑起来的关键。
# 事件签名(topic0)。**全部用 Web3.keccak 从函数签名现算出来的,不是抄的。**
#
# 三个踩过的坑:
#   1. `FlashLoan(...)` **同名不同签名** —— Aave V2/V3/Balancer 三个版本
#      参数不同,topic0 完全不同。只记一个会漏掉三分之二。
#   2. 我第一版把 Aave V3 的 FlashLoan 标成了 Balancer,把 Balancer 的 Swap
#      标成了 Curve —— **标错不会报错,只会静默错判**。
#   3. 我还编过一个哈希(`0x2b3d9c1b9e2b1a0e0e3a…`,那个递增规律一眼假)。
#      **签名一律现算,不要凭记忆写。**
TOPICS = {
    # ---- 清算 ----
    "0xe413a321e8681d831f4dbccbca790d2952b56f977908e45be37335533e005286": "aave_liquidation",
    "0x298637f684da70674f26509b10f07ec2fbc77a335ab1e7d6215a4b2484d8bb52": "comp_liquidation",
    "0x8a729c3fdc874f157ba233b169485178027323530eb084ad9d406294fc568b9a": "morpho_liquidation",
    # ---- 闪电贷(三个版本都要)----
    "0xefefaba5e921573100900a3ad9cf29f222d995fb3b6045797eaea7521bd8d6f0": "flashloan_aave_v3",
    "0x631042c832b07452973831137f2d73e395028b44b250dedc5abb0ee766e168ac": "flashloan_aave_v2",
    "0x0d7d75e01ab95780d3cd1c8ec0dd6c2ce19e3a20427eec8bf53283b6fb8e95f0": "flashloan_balancer",
    "0xbdbdb71d7860376ba52b25a5028beea23581364a40522f6bcfb86bb1f2dca633": "flashloan_univ3",
    # ---- DEX ----
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822": "univ2_swap",
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67": "univ3_swap",
    "0x2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b": "balancer_swap",
    "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140": "curve_swap",
    "0xd013ca23e77a65003c2c659c5442c00c805371b7fc1ebd4c206c41d1536bd90b": "curve_swap_u",
    "0xe7525d00e88ec2fe4949364ebcdf61f80247212b853f121457da56e0df239589": "aggregator_swap",
    "0xd6d4f5681c246c9f42c203e287975af1601f8df8035a9251f79aab5c8f09e2f8": "1inch_swap",
    "0xe9f1b7666102fca89be763a0bc043734cb801a6ec8f7d57b8ecf79bf27f8c9d7": "0x_fill",
    # ---- aToken 动作(清算时抵押物转移的痕迹)----
    "0x458f5fa412d0f69b08dd84872b0215675cc67bc1d5b6fd93300a1c3878b86196": "atoken_mint",
    "0x4cf25bc1d991c17529c25213d3cc0cda295eeaad5f13f361969b12ea48015f90": "atoken_burn",
    "0x4beccb90f994c31aced7a23b5611020728a23d8ec5cddd1a3e9d97b96fda8666": "atoken_transfer",
}
SWAPS = {"univ2_swap", "univ3_swap", "balancer_swap", "curve_swap",
         "curve_swap_u", "aggregator_swap", "1inch_swap", "0x_fill"}
FLASH = {"flashloan_aave_v3", "flashloan_aave_v2", "flashloan_balancer",
         "flashloan_univ3"}
LIQ = {"aave_liquidation", "comp_liquidation", "morpho_liquidation"}
# aToken 的 mint/burn/transfer:清算时抵押物易手一定会留下这些痕迹。
# **它是"发生了借贷协议侧动作"的证据**,哪怕 swap 走的是我没覆盖的场所。
LENDING = {"atoken_mint", "atoken_burn", "atoken_transfer"}


def classify(addr, key, sample=25):
    """
    给一个地址做行为分类。**不算利润,只看它在干什么。**

    为什么要先做这一步:那份 Dune 榜单排序依据是"付了多少优先费",
    里面混着代币合约(USDT/USDC 本身)、散户机器人服务(费是用户付的)、
    前端路由、做市商、三明治机器人。**直接对它们算"套利利润"是没有意义的。**
    """
    multi = f"https://rpc.ankr.com/multichain/{key}"
    eth = f"https://rpc.ankr.com/eth/{key}"
    a = addr.lower()

    code = rpc(eth, "eth_getCode", [a, "latest"]) or "0x"
    is_contract = len(code) > 2

    r = rpc(multi, "ankr_getTransactionsByAddress",
            {"blockchain": ["eth"], "address": [a],
             "pageSize": max(sample, 30), "descOrder": True})
    txs = (r or {}).get("transactions", []) or []
    if not txs:
        return {"addr": a, "err": "无交易"}

    # 合约看"发给它的",EOA 看"它发出的"
    rel = [t for t in txs if (t.get("to") or "").lower() == a] if is_contract \
        else [t for t in txs if (t.get("from") or "").lower() == a]
    if not rel:
        rel = txs

    ev = {}
    venues = {}
    ok = n = 0
    logs_per = []
    blocks = {}
    for t in rel[:sample]:
        rc = rpc(eth, "eth_getTransactionReceipt", [t["hash"]])
        if not rc:
            continue
        n += 1
        if rc.get("status") == "0x1":
            ok += 1
        blocks[rc.get("blockNumber")] = blocks.get(rc.get("blockNumber"), 0) + 1
        lg = rc.get("logs", []) or []
        logs_per.append(len(lg))
        for x in lg:
            t0 = (x.get("topics") or ["?"])[0]
            name = TOPICS.get(t0)
            if name:
                ev[name] = ev.get(name, 0) + 1
            venues[x["address"].lower()] = venues.get(x["address"].lower(), 0) + 1
        time.sleep(0.06)

    if not n:
        return {"addr": a, "err": "拿不到 receipt"}

    swaps = sum(v for k, v in ev.items() if k in SWAPS)
    flash = sum(v for k, v in ev.items() if k in FLASH)
    liq = sum(v for k, v in ev.items() if k in LIQ)
    lending = sum(v for k, v in ev.items() if k in LENDING)
    # 认不出的事件占比 —— 高说明字典覆盖不足,结论要打折
    known = sum(ev.values())
    unknown_ratio = 1 - known / max(sum(logs_per), 1)
    # 同区块内出现多次 = 三明治的签名(前后夹一笔受害交易)
    multi_blk = sum(1 for c in blocks.values() if c >= 2)

    # **判断顺序很重要:强信号在前。**
    # 第一版把"没有 swap"排在最前面,结果一个做了 7 次闪电贷的地址被判成
    # 「非交易类」—— 闪电贷是比 swap 更强的行为信号,不该被它挡住。
    kind, why = "未知", []
    if flash > 0 and liq > 0:
        kind = "闪电贷清算"
        why.append(f"{flash} 次闪贷 + {liq} 次清算")
    elif liq > 0:
        kind = "清算"
        why.append(f"{liq} 次清算事件")
    elif flash > 0 and swaps > 0:
        kind = "套利/清算(用闪电贷)"
        why.append(f"{flash} 次闪贷,每笔 {swaps/n:.1f} 次 swap")
    elif flash > 0:
        kind = "闪电贷(用途待查)"
        why.append(f"{flash} 次闪贷但无 swap —— 值得单独看")
    elif not is_contract and swaps == 0:
        kind = "EOA·非交易"
        why.append("不是合约且无 swap")
    elif lending > 0:
        kind = "借贷协议交互"
        why.append(f"{lending} 次 aToken 动作")
    elif swaps == 0 and n >= 10 and unknown_ratio < 0.5:
        kind = "非交易类"
        why.append("无 swap 且事件基本都认识")
    elif swaps == 0 and n >= 10:
        # **"没有 swap" ≠ "没做交易"** —— 可能只是走了我字典没覆盖的场所。
        # 实测那个闪电贷清算地址就是这样被误判成「非交易类」的。
        kind = "未知(事件覆盖不足)"
        why.append(f"无已知 swap,但 {unknown_ratio*100:.0f}% 事件不认识")
    elif multi_blk / max(len(blocks), 1) > 0.3:
        kind = "疑似三明治"
        why.append(f"{multi_blk}/{len(blocks)} 个区块里出现≥2次")
    elif swaps >= n * 1.5:
        kind = "多跳交易(疑似套利)"
        why.append(f"平均每笔 {swaps/n:.1f} 次 swap")
    elif swaps > 0:
        kind = "单跳交易(路由/做市/散户)"
        why.append(f"平均每笔 {swaps/n:.1f} 次 swap")

    return {"addr": a, "is_contract": is_contract, "n": n,
            "success": ok / n * 100, "swaps_per_tx": swaps / n,
            "flash": flash, "liq": liq, "lending": lending,
            "unknown_ratio": unknown_ratio, "multi_block": multi_blk,
            "blocks": len(blocks), "logs_per_tx": sum(logs_per) / n,
            "events": ev, "kind": kind, "why": "; ".join(why)}


def show_classify(rows):
    print()
    print("=" * 100)
    print("行为分类  ——  先判断是什么类型,再决定要不要做利润画像")
    print("=" * 100)
    print(f"{'标签':24} {'类型':22} {'成功率':>7} {'swap/笔':>8} "
          f"{'闪贷':>5} {'同块≥2':>7}  依据")
    print("-" * 100)
    for lab, c in rows:
        if c.get("err"):
            print(f"{lab[:24]:24} {'✗ ' + c['err']:22}")
            continue
        print(f"{lab[:24]:24} {c['kind'][:22]:22} {c['success']:>6.0f}% "
              f"{c['swaps_per_tx']:>8.1f} {c['flash']:>5} "
              f"{c['multi_block']:>3}/{c['blocks']:<3}  {c['why'][:34]}")
    print("=" * 100)
    hi_unknown = [l for l, c in rows if not c.get("err")
                  and c.get("unknown_ratio", 0) > 0.5]
    if hi_unknown:
        print(f"⚠ 这些地址超过一半的事件不在字典里,分类可能不准:"
              f"{', '.join(hi_unknown[:4])}")
        print("   —— 「没有 swap」不等于「没做交易」,可能只是走了没覆盖的场所。")
    worth = [l for l, c in rows if not c.get("err")
             and ("套利" in c["kind"] or "清算" in c["kind"])]
    if worth:
        print(f">> 值得做利润画像的:{', '.join(worth)}")
        print(f"   跑 `python3 actor_profile.py --chain eth --address <地址>`")
    else:
        print(">> 这批里没有明确的套利/清算型。换一批地址,或放大 --sample。")
    print("   注意:三明治、做市、路由都不是套利 —— 对它们算「套利利润」没有意义。")


# ============================================================
# 归因分析
# ============================================================

def concentration(p):
    """
    利润集中度 —— 判「策略 vs 运气」的关键。

    只用**为正**的那部分算贡献占比:负利润不"贡献"利润,
    混进去会让分母失真(总和可能接近 0 甚至为负,占比变成无意义的大数)。
    """
    pos = sorted([x for x in p if x > 0], reverse=True)
    if not pos:
        return None
    tot = sum(pos)
    n = len(pos)
    out = {"n_pos": n, "total_pos": tot, "share": {}}
    for k in (1, 2, 3, 5, 10, 20):
        if k <= n:
            out["share"][k] = sum(pos[:k]) / tot * 100
    cum = g = 0.0
    for x in sorted(pos):
        cum += x
        g += cum
    out["gini"] = 1 - 2 * g / (n * tot) if tot else 0
    return out


def show(name, r):
    p = r["profits"]
    if not p:
        print(f"\n{name}: 没算出可用的盈亏样本")
        return None
    u = r["unit"]
    n = len(p)
    tot, med, mean = sum(p), statistics.median(p), sum(p) / n
    pos = [x for x in p if x > 0]

    print()
    print("=" * 80)
    print(f"地址画像   {name}")
    print("=" * 80)
    print(f"活动:  抓到 {r['total']:,} 笔  成功 {r['ok']:,} "
          f"({r['ok']/r['total']*100:.1f}%)  跨度 {r['span_h']:.1f}h"
          # 低频地址用「笔/小时」会被 .0f 截成 0,看着像没活动。
          # 这里按量级换单位 —— 之前就是这个坑,把 1.2 笔/**天** 读成了 1.2 笔/**小时**。
          + ((f"  ≈{r['total']/r['span_h']:.1f} 笔/小时"
              if r["total"] / r["span_h"] >= 1
              else f"  ≈{r['total']/r['span_h']*24:.2f} 笔/天")
             if r["span_h"] > 0 else ""))
    print(f"样本:  分析了 {n} 笔")
    print()
    print(f"盈亏:  合计 {tot:+.4f} {u}   中位 {med:+.6f}   均值 {mean:+.6f}")
    print(f"       为正 {len(pos)}/{n}   最大 {max(p):+.6f}   最小 {min(p):+.6f}")
    # 均值/中位这个判据**只在两者都为正时有意义**。
    # 均值为负时算出 −0.8× 还说"分布较均匀",是纯粹的胡说。
    if med > 0 and mean > 0:
        ratio = mean / med
        print(f"       **均值/中位 = {ratio:.1f}×**"
              + ("  → 右偏严重,收入靠尾部" if ratio > 3 else "  → 分布较均匀"))
    elif med > 0 >= mean:
        print(f"       中位为正但均值为负 —— **少数几笔大亏抵消了大量小赚**,"
              f"先去查那几笔是什么")
    else:
        print(f"       中位 ≤ 0,均值/中位判据不适用")

    sk = r.get("skipped") or []
    # skipped 里混了两类东西,不能都叫"纯转账" —— 分开报,否则
    # "已剔除 6 笔纯转账"会让人以为样本被砍了,其实那 6 笔都算进来了。
    pure = [x for x in sk if x[0] == "纯转账"]
    uncounted = [x for x in sk if x[0] != "纯转账"]
    if pure:
        print(f"       (已剔除 {len(pure)} 笔纯转账/归集,合计 "
              f"{sum(x[1] for x in pure):+.4f} {u} —— 那不是套利行为)")
    if uncounted:
        print(f"       ({len(uncounted)} 笔另含未计价 token(非 WETH),"
              f"**这些笔仍在样本内**,只是非 WETH 部分没折算)")

    c = concentration(p)
    if c:
        print()
        print("利润集中度(判「策略 vs 运气」):")
        for k, v in sorted(c["share"].items()):
            print(f"   最赚的 {k:>2} 笔({k/c['n_pos']*100:>4.1f}% 的盈利交易)"
                  f"  → {v:>5.1f}% 的利润")
        print(f"   基尼系数 {c['gini']:.3f}   (0=均匀 1=全集中在一笔)")

    # ---- 竞价 / 落袋 / gas 分开报 ----
    # 旧版把 gas 当"为速度付费",分母又是被污染的"利润",算出来恒 ≈0%。
    bids = [b for b in (r.get("bids") or []) if b is not None]
    if bids:
        gas = sum(r["fees"][:len(p)])
        bid_sum = sum(bids)
        pay_sum = sum(r.get("payouts") or [])
        # 毛机会 = 合约自留 + 竞价。**不能把 pay_sum 算进来** ——
        # 收款地址若同时是做市商,那一栏是换币流水,加进分母会把
        # "为速度付费" 稀释成 2.3% 这种假象(真实值是 99.99%)。
        gross = sum(r.get("retained") or []) + bid_sum
        kept = sum(r.get("retained") or [])
        print()
        print("毛机会去哪了(trace 实测,不靠日志推断):")
        print(f"   付给区块构建者  {bid_sum:>12.4f} {u}   非零 {sum(1 for b in bids if b > 0)}/{len(bids)} 笔")
        print(f"   合约自留        {kept:>12.4f} {u}")
        if r.get("receiver"):
            print(f"   净流向收款地址  {pay_sum:>12.4f} {u}   ({r['receiver']})")
            if r.get("rcv_two_way"):
                print(f"                   ^ ⚠️ 该地址与机器人**双向**转 WETH,"
                      f"说明它是交易对手(做市商)而不是纯收款方。")
                print(f"                     这一栏是换币流水,**不能当利润读** —— "
                      f"实测最大一笔 141.41 WETH 是抵押品卖出腿。")
        print(f"   gas(EOA 支付)  {gas:>12.4f} ETH")
        if gross > 0:
            print(f"   **为速度付费占毛机会 {bid_sum / gross * 100:.1f}%**"
                  f"   (毛机会 = 自留 + 竞价 = {gross:.4f} {u})")
        if r.get("no_trace"):
            print(f"   ⚠️ {r['no_trace']} 笔拿不到 trace,竞价按缺失处理(**不是按 0**)")
        if bid_sum > 0 and abs(kept) < bid_sum * 0.01:
            print()
            print(f"   ⚠️ **自留 ≈ 0,毛机会几乎全额报给了构建者。**")
            print(f"      真实收益取决于构建者事后 refund —— 那是链下的,"
                  f"链上测不到,别把毛机会当利润记。")

    print()
    print("判读:")
    if c:
        t3 = sum(v for k, v in c["share"].items() if k == 3) or c["share"].get(2, 0)
        if t3 > 60:
            print(f"   · 最赚的 3 笔占 {t3:.0f}% —— **高度集中,收入靠偶发大机会(运气成分重)**")
        elif t3 > 35:
            print(f"   · 最赚的 3 笔占 {t3:.0f}% —— 中等集中,平时靠小单维持、靠尾部赚钱")
        else:
            print(f"   · 最赚的 3 笔只占 {t3:.0f}% —— **分布均匀,像可重复的策略**")
    if len(pos) / n > 0.9:
        print(f"   · {len(pos)}/{n} 笔为正 —— 策略本身有效,不是碰运气进场")
    if r["ok"] / r["total"] < 0.3:
        print(f"   · 成功率仅 {r['ok']/r['total']*100:.1f}% —— "
              f"**失败成本必须≈0 才玩得起**(原子/回滚型)")

    # 给下一步:挑代表性单笔
    order = sorted(range(n), key=lambda i: p[i])
    mid = order[n // 2]
    print()
    print(f"下一步:还原**中位数那笔**(样本内第 {mid+1} 笔,盈亏 {p[mid]:+.6f} {u})")
    print(f"        —— 别挑最赚的那笔,那笔恰恰是运气,不代表这个人平时在干什么。")
    print("=" * 80)
    return {"n": n, "total": tot, "median": med, "mean": mean,
            "pos": len(pos), "concentration": c}


def load_addrs(path):
    """
    从 markdown 表格里抽地址,去重并**按优先费降序**排。

    排序很重要:不排的话 --limit 取到的是文件里的行序(实测是按地址字母序),
    跑出来一堆 0xff… 开头的小角色,而榜首那几个反而没跑到。
    """
    out, seen = [], set()
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 2 or not re.fullmatch(r"0x[0-9a-fA-F]{40}", cells[0]):
            continue
        a = cells[0].lower()
        if a in seen:
            continue
        seen.add(a)
        try:
            fee = float(cells[2]) if len(cells) > 2 else 0.0
        except ValueError:
            fee = 0.0
        out.append((a, cells[1], fee))
    out.sort(key=lambda x: -x[2])
    return [(a, lab) for a, lab, _ in out]


def main():
    p = argparse.ArgumentParser(description="链上地址画像:策略/基础设施/运气 归因")
    p.add_argument("--chain", choices=["sol", "eth"], default="eth")
    p.add_argument("--address")
    p.add_argument("--from-file", help="从 markdown 表格批量读地址")
    p.add_argument("--limit", type=int, default=3, help="批量模式最多跑几个")
    p.add_argument("--sample", type=int, default=60, help="每个地址分析多少笔")
    p.add_argument("--classify", action="store_true",
                   help="只做行为分类(不算利润)。**先跑这个再决定深挖谁**")
    p.add_argument("--receiver", help="机器人的利润收款地址(合约自己常净留 0,"
                                     "不给这个就量不到落袋数)")
    p.add_argument("--json", help="导出")
    args = p.parse_args()

    targets = []
    if args.from_file:
        targets = load_addrs(args.from_file)[:args.limit]
        print(f"从 {args.from_file} 读到 {len(load_addrs(args.from_file))} 个唯一地址,"
              f"跑前 {len(targets)} 个", file=sys.stderr)
    elif args.address:
        targets = [(args.address, args.address)]
    else:
        p.error("要么 --address,要么 --from-file")

    key = ankr_key() if args.chain == "eth" else None
    if args.chain == "eth" and not key:
        print("需要 Ankr key。在 .env 里加 ANKR_KEY=<key>,"
              "或复用已有的 SUI_RPC(同 key 通吃多链)。", file=sys.stderr)
        return 1

    if args.classify:
        if args.chain != "eth":
            print("--classify 目前只支持以太坊", file=sys.stderr)
            return 1
        rows = []
        for addr, label in targets:
            try:
                rows.append((label, classify(addr, key, args.sample)))
            except RuntimeError as e:
                rows.append((label, {"err": str(e)[:40]}))
        show_classify(rows)
        if args.json:
            Path(args.json).write_text(
                json.dumps({l: c for l, c in rows}, indent=2, ensure_ascii=False),
                encoding="utf-8")
            print(f"\n已导出 {args.json}")
        return 0

    out = {}
    for addr, label in targets:
        try:
            r = (sol_profile(addr, args.sample) if args.chain == "sol"
                 else eth_profile(addr, args.sample, key, args.receiver))
        except RuntimeError as e:
            print(f"\n{label}: {e}", file=sys.stderr)
            continue
        res = show(f"{label}  ({addr[:10]}…)", r)
        if res:
            out[addr] = {"label": label, **res}

    if args.json and out:
        Path(args.json).write_text(json.dumps(out, indent=2, ensure_ascii=False,
                                              default=str), encoding="utf-8")
        print(f"\n已导出 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
