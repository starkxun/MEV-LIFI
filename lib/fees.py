#!/usr/bin/env python3
"""
fees.py —— 手续费:每个数字都要标明出处

═══ 为什么要单独一个模块 ═══

2026-08-15,先从 10 笔真实账单反推费率,再去两个所的官方费率页对账,
发现三件事:

  ① **同一个交易所,代币化股票(TradFi)和加密永续的费率不一样**
       Bybit  TradFi taker 2.75 bps  /  加密 taker 5.50 bps  (整 50% 折扣)
       币安   TradFi taker 4.00 bps  /  加密 taker 5.00 bps

  ② **我们脚本和文档里一直用的「8 bps 往返」是拍脑袋的**
       加密双边吃单实测 21.0 bps  → 低估 163%

  ③ ★ **TradFi 永续双边挂单 = 0 手续费**
       币安 TradFi 挂单方 0.0000%(官方表)
       Bybit TradFi 挂单实测 0 USDT
       → 往返 (0 + 0) × 2 = **0.00 bps**

  ①②③ 里最值钱的是 ③:8 bps 那个「成本地板」在 TradFi + 挂单这一档
  **根本不存在**。之前被成本否掉的一些方向要重算。

═══ 两种证据,不能互相替代 ═══

  账单实测  →  你**实际付了**多少,含账户等级/活动/折扣的真实结果
  官方费率表 →  规则是什么,**包括你还没交易过的格子**

  账单能验证「这张表对我这个账户成立」,费率表能填上账单没覆盖的空格。
  两者对账:4 个有账单的格子,和官方表 **4/4 完全一致**。

  所以这一版每个数字都带出处标记,调用方一眼能看出可信度:

      ✅✅ 账单 + 官方表        最高
      ✅   只有账单
      📖   只有官方费率表
      ⚠️   都没有,猜的         ← 出现这个就别拿去做决策

═══ 资产类别怎么判 ═══

不硬编码币种清单(会过期)。币安 exchangeInfo 的 `underlyingSubType`
带 `TradFi` 标记,直接问交易所:

    SNDKUSDT     EQUITY      ['TradFi']            ← 代币化美股
    SKHYNIXUSDT  KR_EQUITY   ['TradFi']            ← 代币化韩股
    HK0700USDT   HK_EQUITY   ['TradFi']            ← 代币化港股
    XAUUSDT      COMMODITY   ['TradFi']            ← 代币化黄金
    OPENAIUSDT   PREMARKET   ['Pre-IPO','TradFi']  ← 未上市
    HYPEUSDT     COIN        ['DeFi']              ← 加密

816 个合约里 162 个是 TradFi,占 20%。**不分类是不行的。**
"""
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
CACHE = ROOT / "shadow" / "asset_class.json"

# ── 费率表(bps,单腿单边)──────────────────────────────────────────
# 核对日期 2026-08-15。**费率会变,活动会过期,定期重查。**
FEES = {
    ("Binance", "crypto"): {"maker": 2.00, "taker": 5.00},
    ("Binance", "tradfi"): {"maker": 0.00, "taker": 4.00},
    ("Bybit",   "crypto"): {"maker": 2.00, "taker": 5.50},
    ("Bybit",   "tradfi"): {"maker": 0.00, "taker": 2.75},
}

# ── 每个数字的出处 ────────────────────────────────────────────────
# bill = 真实成交账单反推   table = 交易所官方费率页
SOURCES = {
    ("Binance", "crypto", "maker"): ("table", "U本位合约 挂单 0.0200%"),
    ("Binance", "crypto", "taker"): ("bill+table", "0.0500% / HYPE ×1"),
    ("Binance", "tradfi", "maker"): ("table", "TradFi合约 挂单方 0.0000% ⏳活动期"),
    ("Binance", "tradfi", "taker"): ("bill+table", "0.0400% / SNDK×2 SOXL×1"),
    ("Bybit",   "crypto", "maker"): ("table", "合约 Maker 0.0200%"),
    ("Bybit",   "crypto", "taker"): ("bill+table", "0.0550% / HYPE ×2"),
    ("Bybit",   "tradfi", "maker"): ("bill", "SNDK 限价单账单 = 0 USDT"),
    ("Bybit",   "tradfi", "taker"): ("bill", "SNDK×2 SOXL×1(= 5.50 的整 50%)"),
}
MARK = {"bill+table": "✅✅", "bill": "✅", "table": "📖", None: "⚠️"}
RANK = {"bill+table": 3, "bill": 2, "table": 1, None: 0}

# 币安持有 BNB 可打 9 折(吃单)。默认不启用 —— 你没 BNB 就别算进去。
BNB_TAKER_DISCOUNT = 0.90

CLASSES = ("crypto", "tradfi")
_cache = None


def _load_cache():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE.read_text())
        except Exception:
            _cache = {}
    return _cache


def _save_cache():
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(_cache, ensure_ascii=False, indent=1, sort_keys=True))
    except Exception:
        pass


def refresh_classes(timeout=25):
    """拉一次币安 exchangeInfo,把全市场的 tradfi/crypto 归类缓存下来。"""
    req = urllib.request.Request("https://fapi.binance.com/fapi/v1/exchangeInfo",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    c = _load_cache()
    n = 0
    for s in data.get("symbols", []):
        base = s.get("baseAsset")
        if not base:
            continue
        sub = s.get("underlyingSubType") or []
        c[base.upper()] = "tradfi" if "TradFi" in sub else "crypto"
        n += 1
    c["_fetched"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_cache()
    return n


def asset_class(coin, auto=True):
    """
    'tradfi' / 'crypto'。

    拿不到分类时返回 'crypto' —— 这是**保守**的一侧:crypto 的 maker 和
    taker 都 ≥ tradfi 的对应值,未知时按 crypto 算只会高估成本。
    """
    coin = coin.upper()
    c = _load_cache()
    if coin in c:
        return c[coin]
    if auto:
        try:
            refresh_classes()
        except Exception:
            pass
        return _load_cache().get(coin, "crypto")
    return "crypto"


def leg_fee(venue, cls, kind, bnb=False):
    """单腿单边 → (bps, 出处标签)"""
    row = FEES.get((venue, cls))
    if row is None:
        raise KeyError(f"没有 {venue}/{cls} 的费率,先补进 FEES")
    f = row[kind]
    if bnb and venue == "Binance" and kind == "taker":
        f *= BNB_TAKER_DISCOUNT
    src = SOURCES.get((venue, cls, kind), (None, "未知"))[0]
    return f, src


def roundtrip(venue_a, venue_b, cls, maker=False, bnb=False):
    """
    两条腿开+平 = 4 笔 → (往返 bps, 是否全部有账单支撑, 明细字符串)

    第二个返回值只在**两条腿都有真实账单**时为 True。
    只有官方费率表撑着的(📖)返回 False —— 表是规则,账单才是事实。
    """
    kind = "maker" if maker else "taker"
    fa, sa = leg_fee(venue_a, cls, kind, bnb)
    fb, sb = leg_fee(venue_b, cls, kind, bnb)
    total = (fa + fb) * 2
    detail = (f"({venue_a} {fa:.2f}{MARK[sa]} + {venue_b} {fb:.2f}{MARK[sb]})"
              f" × 2 = {total:.2f} bps")
    return total, (RANK[sa] >= 2 and RANK[sb] >= 2), detail


def mixed_roundtrip(maker_venue, taker_venue, cls, bnb=False):
    """一条腿挂单、另一条腿吃单 → (往返 bps, 全账单?, 明细)"""
    fm, sm = leg_fee(maker_venue, cls, "maker")
    ft, st = leg_fee(taker_venue, cls, "taker", bnb)
    total = (fm + ft) * 2
    detail = (f"({maker_venue}挂 {fm:.2f}{MARK[sm]} + {taker_venue}吃 {ft:.2f}{MARK[st]})"
              f" × 2 = {total:.2f} bps")
    return total, (RANK[sm] >= 2 and RANK[st] >= 2), detail


def fee_note(all_billed):
    return "✅ 两腿均有真实账单" if all_billed else "📖 含仅凭官方费率表的格子"


def describe(bnb=False):
    print("═" * 74)
    print("手续费表(bps,单腿单边)　核对于 2026-08-15")
    print("  ✅✅ 账单+官方表　✅ 只有账单　📖 只有官方表　⚠️ 没依据")
    print("═" * 74)
    print(f"  {'交易所':8} {'类别':8} {'maker':>10} {'taker':>10}   出处")
    for (v, cls) in FEES:
        cells, srcs = [], []
        for k in ("maker", "taker"):
            f, s = leg_fee(v, cls, k, bnb)
            cells.append(f"{f:5.2f}{MARK[s]}")
            srcs.append(SOURCES.get((v, cls, k), (None, "未知"))[1])
        print(f"  {v:8} {cls:8} {cells[0]:>10} {cells[1]:>10}   {srcs[0]}")
        print(f"  {'':8} {'':8} {'':>10} {'':>10}   {srcs[1]}")
    print()
    print("  往返(两腿 × 开平):")
    for cls in CLASSES:
        t, ok, d = roundtrip("Binance", "Bybit", cls, maker=False, bnb=bnb)
        print(f"    {cls:7} 双边吃单  {d}   {fee_note(ok)}")
        t, ok, d = mixed_roundtrip("Bybit", "Binance", cls, bnb)
        print(f"    {cls:7} 混  合    {d}   {fee_note(ok)}")
        t, ok, d = roundtrip("Binance", "Bybit", cls, maker=True)
        star = "   ★★★ 零手续费" if t == 0 else ""
        print(f"    {cls:7} 双边挂单  {d}   {fee_note(ok)}{star}")
        print()
    print("  🔑 以前一律按 8.0 bps 往返算。真相是:")
    print("     加密双边吃单 21.0 bps —— 低估 163%")
    print("     **TradFi 双边挂单 0.00 bps —— 那个「成本地板」在这一档不存在**")
    print()
    print("  ⏳ 币安 TradFi 挂单 0% 是【活动期】费率,会过期。")
    print("     挂单的代价是可能挂不上 → 单腿风险。零手续费不等于零成本。")
    print("═" * 74)


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:]]
    if "refresh" in args:
        print(f"已缓存 {refresh_classes()} 个合约的资产分类 → {CACHE}")
        args.remove("refresh")
    describe(bnb="bnb" in args)
    for coin in [a for a in args if a not in ("bnb",)]:
        print(f"  {coin.upper():12} → {asset_class(coin, auto=False)}")
