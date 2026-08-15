#!/usr/bin/env python3
"""
carry_watch.py —— 跨所资金费 carry 的实时记录器

⚠️ **只读公开行情,不需要 API key,代码里没有任何下单能力。**
   用的全是各所的公开行情端点 —— 结构上就不可能动你的钱。

它做三件事:

  1. 每隔 N 秒同时抓两个所的盘口 + 资金费,落一条 JSONL
  2. 算出**两个开仓方向**各自的:资金费净年化、开仓价差成本、回本天数
  3. 划算的时候在终端标出来

为什么要它:
  你手工在 taoli.tools 下单时,屏幕上的数字**转瞬即逝**。
  事后想算「我实际成交的价差 vs 当时屏幕上的价差」,没有记录就算不了。
  而那个差,加上单腿发生率,是我们成本模型里**最后两个「待定」项**。

    python3 carry_watch.py --coin SKHYNIX
    python3 carry_watch.py --coin SKHYNIX --interval 10 --alert-days 2
    python3 carry_watch.py --coin SKHYNIX --info      # 只看合约规格(最小下单量等)
    python3 carry_watch.py --coin SKHYNIX --interval 30 --alert-days 3 --min-carry 15

   挂服务器跑：
   tmux new -s scan -d 'python3 carry_watch.py --scan --interval 60 \
  --min-turnover 30000000 --record-above 20 \
  >> logs/scan_$(date +%F).log 2>&1' 

  挂服务器监控某笔交易(开仓价用**成交均价**,不是下单时的盘口价)：
  tmux new -d -s sndk "python3 carry_watch.py --coin SNDK --venues Binance,Bybit --short Binance:1645.77 --long Bybit:1645.35 --qty 0.01 --carry-floor 0 --carry-strikes 5 --interval 60 --target 0.0222 --out shadow/sndk_track.jsonl"

  手续费不再写死 8 bps:按币种自动查 lib/fees.py(代币化股票 13.5 / 加密 21 bps
  吃单往返,均为实测)。要手动覆盖用 --fee-roundtrip。看费率表:python3 lib/fees.py
"""

import argparse
import json
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent

# 手续费搬到 lib/fees.py 了。原因见那个文件的开头:
# 代币化股票和加密永续的费率**不一样**,以前这里的两层字典没有这一维,
# 结果所有回本天数都偏乐观(股票低估 69%、加密低估 163%)。
sys.path.insert(0, str(ROOT))
from lib import fees as FEEMOD  # noqa: E402


class Blocked(Exception):
    """地域封锁(HTTP 451 / 403)—— 重试没有意义,必须换机器。"""


def get(url, n=3, timeout=15):
    last = None
    for i in range(n):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            # 451 Unavailable For Legal Reasons / 403 —— 交易所按服务器 IP 所在地
            # 做合规封锁。**重试多少次都没用**,只能换一台在允许地区的机器。
            if e.code in (451, 403):
                host = url.split("/")[2]
                raise Blocked(f"{host} 返回 HTTP {e.code} —— 该地区被封锁")
            last = e
            if i < n - 1:
                time.sleep(1.0 * (i + 1))
        except Exception as e:
            last = e
            if i < n - 1:
                time.sleep(1.0 * (i + 1))
    raise last


# ── 各所的行情适配器 ───────────────────────────────────────────
def snap_binance(coin):
    s = f"{coin}USDT"
    bt = get(f"https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol={s}")
    pi = get(f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={s}")
    return {
        "venue": "Binance",
        "bid": float(bt["bidPrice"]), "ask": float(bt["askPrice"]),
        "bid_qty": float(bt["bidQty"]), "ask_qty": float(bt["askQty"]),
        "mark": float(pi["markPrice"]), "index": float(pi["indexPrice"]),
        "rate": float(pi["lastFundingRate"]),
        "next_ms": int(pi["nextFundingTime"]),
    }


def snap_bybit(coin):
    d = get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={coin}USDT")
    t = d["result"]["list"][0]
    return {
        "venue": "Bybit",
        "bid": float(t["bid1Price"]), "ask": float(t["ask1Price"]),
        "bid_qty": float(t.get("bid1Size") or 0), "ask_qty": float(t.get("ask1Size") or 0),
        "mark": float(t["markPrice"]), "index": float(t["indexPrice"]),
        "rate": float(t["fundingRate"]),
        "next_ms": int(t["nextFundingTime"]),
    }


SNAP = {"Binance": snap_binance, "Bybit": snap_bybit}


# ── 结算间隔 ──────────────────────────────────────────────────
def interval_binance(coin):
    """
    ⚠️ Binance 的 fundingInfo **只列出非 8 小时的合约**。
       查不到 ≠ 出错,而是「就是默认的 8 小时」。
       这个默认值不写清楚,年化会直接算错一倍。
    """
    try:
        for x in get("https://fapi.binance.com/fapi/v1/fundingInfo"):
            if x["symbol"] == f"{coin}USDT":
                return float(x["fundingIntervalHours"])
    except Exception:
        pass
    return 8.0


def interval_bybit(coin):
    try:
        d = get(f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={coin}USDT")
        return float(d["result"]["list"][0]["fundingInterval"]) / 60.0
    except Exception:
        return 8.0


def contract_info(coin):
    """最小下单量 / 步长 / 最大杠杆 —— 决定「最少要充多少钱」。"""
    out = {}
    try:
        d = get(f"https://api.bybit.com/v5/market/instruments-info?category=linear&symbol={coin}USDT")
        t = d["result"]["list"][0]
        out["Bybit"] = {
            "min_qty": float(t["lotSizeFilter"]["minOrderQty"]),
            "qty_step": float(t["lotSizeFilter"]["qtyStep"]),
            "max_lev": float(t["leverageFilter"]["maxLeverage"]),
            "tick": float(t["priceFilter"]["tickSize"]),
        }
    except Exception as e:
        out["Bybit"] = {"err": str(e)[:50]}
    try:
        d = get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        for sym in d["symbols"]:
            if sym["symbol"] != f"{coin}USDT":
                continue
            f = {x["filterType"]: x for x in sym["filters"]}
            out["Binance"] = {
                "min_qty": float(f["LOT_SIZE"]["minQty"]),
                "qty_step": float(f["LOT_SIZE"]["stepSize"]),
                "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 0)),
                "tick": float(f["PRICE_FILTER"]["tickSize"]),
            }
            break
    except Exception as e:
        out["Binance"] = {"err": str(e)[:50]}
    return out


# ── 基差的历史分布 ────────────────────────────────────────────
def basis_history(coin, hours=1000):
    """
    用两所的 1 小时 K 线算历史基差分布。

    **为什么必须有这个**:开仓成本不是「此刻价差多少」,而是
    「此刻价差相对它自己的常态偏到哪」。

    你做空 A、做多 B,就是**做空基差 (A-B)**:
        基差此刻偏【低】 → 你在低卖高买,而且回归时继续亏  ← 坏时机
        基差此刻偏【高】 → 你在高卖低买,回归时还再赚一笔  ← 好时机

    同样的资金费,进场时机不同能差十几个 bps。
    """
    try:
        bn = get(f"https://fapi.binance.com/fapi/v1/klines?symbol={coin}USDT"
                 f"&interval=1h&limit={min(hours,1000)}")
        by = get(f"https://api.bybit.com/v5/market/kline?category=linear"
                 f"&symbol={coin}USDT&interval=60&limit={min(hours,1000)}")
    except Exception as e:
        return None, str(e)[:60]
    BN = {int(k[0]): float(k[4]) for k in bn}
    BY = {int(k[0]): float(k[4]) for k in by["result"]["list"]}
    common = sorted(set(BN) & set(BY))
    if len(common) < 100:
        return None, f"共同小时只有 {len(common)}"
    rows = []
    for t in common:
        rows.append((t, (BN[t] - BY[t]) / BY[t] * 1e4))
    return rows, None


def basis_stats(rows, open_hours):
    """按开市/休市分别给出基差分布。"""
    op = [v for t, v in rows
          if datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour in open_hours]
    cl = [v for t, v in rows
          if datetime.fromtimestamp(t / 1000, tz=timezone.utc).hour not in open_hours]
    def st(xs):
        if len(xs) < 10:
            return None
        return {"n": len(xs), "mean": statistics.mean(xs),
                "sd": statistics.pstdev(xs), "sorted": sorted(xs)}
    return {"开市": st(op), "休市": st(cl), "全部": st([v for _, v in rows])}


# ── 资金费历史体检 ────────────────────────────────────────────
def funding_history(coin, venue, limit=200):
    """历史已结算资金费 → [(结算时刻ms, 费率)],按时间升序。"""
    if venue == "Binance":
        d = get(f"https://fapi.binance.com/fapi/v1/fundingRate"
                f"?symbol={coin}USDT&limit={min(limit,1000)}")
        return sorted((int(x["fundingTime"]), float(x["fundingRate"])) for x in d)
    if venue == "Bybit":
        d = get(f"https://api.bybit.com/v5/market/funding/history"
                f"?category=linear&symbol={coin}USDT&limit={min(limit,200)}")
        return sorted((int(x["fundingRateTimestamp"]), float(x["fundingRate"]))
                      for x in d["result"]["list"])
    raise ValueError(venue)


def history_check(coin, vs, iv_a, cur_net, tol_ms=300_000):
    """
    ★ 开仓前的历史体检。净年化统一按 (vs[0] − vs[1]) 定向。

    **为什么必须有这个:实时监控只报「此刻」。**

    VELVET 那次,此刻净 +13.2%,而 33 天真实均值是 **−10.6%** —— 连符号
    都是反的;+13% 只是当天拉盘(24h +14.7%)的副产品。光看实时数据,
    工具没有任何办法区分「结构性价差」和「一天的假象」。

    这和 BILL 那次(4 天窗口 +126% vs 344 天 +13.57%)是同一个错误,
    区别只在于:那次是人肉发现的,这次要让工具自己拦下来。
    """
    try:
        A = funding_history(coin, vs[0])
        B = funding_history(coin, vs[1])
    except Exception as e:
        return None, [f"⚠️ 资金费历史拉取失败:{str(e)[:60]} —— **体检没做**"]
    if len(A) < 6 or len(B) < 6:
        return None, [f"⚠️ 历史期数不足(={len(A)}/{len(B)}),**体检没做**"]

    bt = sorted(B)
    com = []
    for t, ra in A:                      # 两所结算时刻可能差几十秒,按 ±5 分钟配对
        m = [y for y, _ in bt if abs(y - t) <= tol_ms]
        if m:
            com.append((t, ra, dict(bt)[m[0]]))
    if len(com) < 6:
        return None, ["⚠️ 两所结算时刻对不齐,**体检没做**"]

    per_day = 24.0 / iv_a
    k = per_day * 365                    # 一年多少期
    def stat(rows):
        nets = [(a - b) * k * 100 for _, a, b in rows]
        n = len(nets)
        m = statistics.mean(nets)
        sd = statistics.pstdev(nets) if n > 1 else 0.0
        # 标准误 & t 值:长期 carry 到底和 0 有没有区别。
        # 比「sd 是均值的几倍」讲究 —— 那个判据没考虑样本量,
        # 样本越多它越容易报警,方向是反的。
        se = sd / (n ** 0.5) if n > 1 else float("inf")
        return {"n": n, "days": n / per_day, "mean": m, "sd": sd, "se": se,
                "t": (m / se) if se else 0.0,
                "neg": sum(1 for x in nets if x < 0) / n * 100,
                "a": statistics.mean([a for _, a, _ in rows]) * k * 100,
                "b": statistics.mean([b for _, _, b in rows]) * k * 100}

    wins = {}
    for tag, d in (("1天", 1), ("7天", 7), ("30天", 30)):
        n = int(round(per_day * d))
        if n >= 3 and len(com) >= n:
            wins[tag] = stat(com[-n:])
    wins["全部"] = stat(com)

    ref = wins.get("30天") or wins["全部"]
    warn = []
    if ref["days"] < 14:
        warn.append(f"⚠️ 可比历史只有 {ref['days']:.0f} 天 —— 样本本身就不够")
    if cur_net is not None and ref["mean"] != 0:
        if cur_net * ref["mean"] < 0:
            warn.append(f"🔴 **当前净 {cur_net:+.1f}% 与 {ref['days']:.0f} 天均值 "
                        f"{ref['mean']:+.1f}% 符号相反** —— 你看到的很可能是短期假象")
        elif abs(cur_net) > abs(ref["mean"]) * 3:
            warn.append(f"🔴 **当前净 {cur_net:+.1f}% 是长期均值 {ref['mean']:+.1f}% 的 "
                        f"{abs(cur_net/ref['mean']):.1f} 倍** —— 均值回归会吃掉它")
    if ref["neg"] >= 25:
        warn.append(f"🔴 {ref['neg']:.0f}% 的结算期净值为**负**(你在付钱)")
    t = ref["t"]
    if t <= -2:
        warn.append(f"🔴 长期净年化 {ref['mean']:+.1f}% ± {ref['se']:.1f} "
                    f"(t={t:.1f})—— **显著为负**,这个方向长期是亏的")
    elif abs(t) < 2:
        warn.append(f"🔴 长期净年化 {ref['mean']:+.1f}% ± {ref['se']:.1f} "
                    f"(t={t:.1f})—— **和 0 没有统计区别**,不构成 carry")
    if "1天" in wins and "7天" in wins and wins["1天"]["mean"] * wins["7天"]["mean"] < 0:
        warn.append("🔴 1 天和 7 天窗口**符号相反** —— 历史自身不自洽")
    return wins, warn


def basis_verdict(cur, st):
    """当前基差在历史分布里的位置 → 是不是好的进场时机。"""
    if not st:
        return None
    z = (cur - st["mean"]) / st["sd"] if st["sd"] else 0
    xs = st["sorted"]
    pct = sum(1 for x in xs if x < cur) / len(xs) * 100
    return {"z": z, "pct": pct, "mean": st["mean"], "sd": st["sd"], "n": st["n"]}


# ── 核心计算 ──────────────────────────────────────────────────
def evaluate(a, b, iv_a, iv_b, maker=True, cls="crypto", fee_override=None):
    """
    a、b 两个所的快照 → 两个开仓方向各自的经济性。

    做空一条腿:资金费为正时**收钱**
    做多一条腿:资金费为正时**付钱**

    cls: 'crypto' / 'tradfi' —— 两者费率差最多 2 倍,必须分开算。
         拿不到分类时按 crypto(保守,只会高估成本)。
    """
    ann_a = a["rate"] * (8760 / iv_a) * 100
    ann_b = b["rate"] * (8760 / iv_b) * 100
    # 一条腿开+平 2 笔,两条腿共 4 笔
    if fee_override is not None:
        fee, fee_measured = fee_override, False
    else:
        fee, fee_measured, _ = FEEMOD.roundtrip(a["venue"], b["venue"], cls, maker)

    out = []
    for short, long_ in ((a, b), (b, a)):
        # 开仓:在 short 腿以买一卖出、在 long 腿以卖一买入
        mid = (short["bid"] + long_["ask"]) / 2
        entry_bps = (short["bid"] - long_["ask"]) / mid * 1e4
        net_ann = ((ann_a if short is a else ann_b)
                   - (ann_b if long_ is b else ann_a))
        # 出场时价差大致对称,按同样幅度再算一次
        cost = fee - entry_bps * 2          # 负的 entry_bps 是成本
        # cost < 0 = 开仓拿到的价差已经盖过手续费,进场当场就赚
        # **这种情况没有「回本天数」可言** —— 早期版本让它算成负数,
        # 于是「负数 ≤ 阈值」意外通过判据,把净年化 +0.2% 的行标成了好时机。
        if cost <= 0:
            payback = 0.0
        elif net_ann > 0:
            payback = cost / (net_ann / 365 * 100)
        else:
            payback = float("inf")
        out.append({
            "short": short["venue"], "long": long_["venue"],
            "net_ann": net_ann, "entry_bps": entry_bps,
            "fee_bps": fee, "total_cost_bps": cost, "payback_days": payback,
        })
    return {"ann": {a["venue"]: ann_a, b["venue"]: ann_b}, "dirs": out,
            "fee_bps": fee, "fee_measured": fee_measured, "cls": cls}


# ══════════════════════════════════════════════════════════════════
#   持仓跟踪 + 平仓提醒
# ══════════════════════════════════════════════════════════════════
def parse_leg(s):
    """'Bybit:143.50' → ('Bybit', 143.50)"""
    v, px = s.split(":")
    v = v.strip()
    if v not in SNAP:
        raise ValueError(f"未知交易所 {v},可选 {list(SNAP)}")
    return v, float(px)


def track_pnl(snaps, short_v, short_px, long_v, long_px, qty):
    """
    浮动盈亏 = 数量 × (开仓价差 − 当前价差)

    推导:
        空腿盈亏 = qty × (开仓价 − 标记价)
        多腿盈亏 = qty × (标记价 − 开仓价)
        相加 → qty × [(空开 − 多开) − (空标 − 多标)]
             = qty × (开仓价差 − 当前价差)

    **所以整个仓位的盈亏只由基差变化决定,和价格本身无关。**
    这就是对冲的数学本质。
    """
    entry_sp = short_px - long_px
    now_sp = snaps[short_v]["mark"] - snaps[long_v]["mark"]
    return qty * (entry_sp - now_sp), entry_sp, now_sp


def alert(msg, path):
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print(f"\a🚨🚨🚨 {msg}", flush=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ══════════════════════════════════════════════════════════════════
#   多币种铺开扫描 —— 回答「机会窗口一天几次、每次多久」
# ══════════════════════════════════════════════════════════════════
#
# 效率关键:**用批量端点,不要一个币一个请求。**
#   Binance /fapi/v1/premiumIndex        (不带 symbol)→ 全部资金费+标记价
#   Binance /fapi/v1/ticker/bookTicker   (不带 symbol)→ 全部盘口
#   Bybit   /v5/market/tickers           (不带 symbol)→ 全部,含成交额
# 三个请求覆盖全市场,60 秒一轮完全不会触发限流。

def preflight():
    """
    开跑前逐个测端点,把「地区封锁」和「网络抖动」分开。

    ⚠️ 实测:美国 IP 的服务器访问币安会拿到 **HTTP 451**
       (Unavailable For Legal Reasons)。这是按**服务器所在地**的
       合规封锁,重试无效,只能换一台在允许地区的机器。
       症状是日志里一直刷「抓取失败 HTTP Error 451」。
    """
    tests = [
        ("Binance 资金费", "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"),
        ("Binance 盘口", "https://fapi.binance.com/fapi/v1/ticker/bookTicker?symbol=BTCUSDT"),
        ("Bybit 行情", "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT"),
    ]
    print("=== 端点自检 ===")
    blocked, failed = [], []
    for name, url in tests:
        try:
            get(url, n=1, timeout=12)
            print(f"  {name:<16} ✅")
        except Blocked as e:
            print(f"  {name:<16} 🔴 {e}")
            blocked.append(name)
        except Exception as e:
            print(f"  {name:<16} ⚠️ {str(e)[:60]}")
            failed.append(name)
    if blocked:
        print(f"\n🔴 **{len(blocked)} 个端点被地区封锁,这台机器跑不了。**")
        print(f"   HTTP 451 是按【服务器 IP 所在地】的合规封锁,和你人在哪无关。")
        print(f"   换一台在允许地区的机器再跑 —— 重试、改 UA、加延迟都没用。")
        return False
    if failed:
        print(f"\n⚠️ {len(failed)} 个端点这次没通,但不是封锁,可能只是抖动。继续。")
    print()
    return True


def bulk_snapshot(min_turnover):
    """一轮抓全市场。返回 {symbol: {...}}。"""
    bn_pi = get("https://fapi.binance.com/fapi/v1/premiumIndex")
    bn_bt = get("https://fapi.binance.com/fapi/v1/ticker/bookTicker")
    by = get("https://api.bybit.com/v5/market/tickers?category=linear")
    BT = {x["symbol"]: x for x in bn_bt}
    BN = {x["symbol"]: x for x in bn_pi if x["symbol"] in BT}
    BY = {x["symbol"]: x for x in by["result"]["list"]}
    out = {}
    for sym in set(BN) & set(BY):
        if not sym.endswith("USDT"):
            continue
        y = BY[sym]
        try:
            turn = float(y.get("turnover24h") or 0)
            if turn < min_turnover:
                continue
            out[sym] = {
                # ⚠️ **原始 per-settlement 费率必须落盘**,年化只是展示字段。
                #    年化 = rate × (8760/间隔),是派生量;反推会丢失间隔信息。
                "bn_rate": float(BN[sym]["lastFundingRate"]),
                "bn_mark": float(BN[sym]["markPrice"]),
                "bn_next": int(BN[sym]["nextFundingTime"]),
                "bn_bid": float(BT[sym]["bidPrice"]), "bn_ask": float(BT[sym]["askPrice"]),
                "bn_bq": float(BT[sym]["bidQty"]), "bn_aq": float(BT[sym]["askQty"]),
                "by_rate": float(y["fundingRate"]),
                "by_mark": float(y["markPrice"]),
                "by_next": int(y["nextFundingTime"]),
                "by_bid": float(y["bid1Price"]), "by_ask": float(y["ask1Price"]),
                "by_bq": float(y.get("bid1Size") or 0), "by_aq": float(y.get("ask1Size") or 0),
                "turnover": turn,
            }
        except Exception:
            continue
    return out


def load_spot_symbols():
    """
    两所的现货可交易清单 —— 判断「现货+永续」这条腿能不能做。

    ⚠️ 我们前两周只测了【跨所价差】= |rate_A − rate_B|,
       漏掉了【单所绝对费率】= rate_A 本身。
       两个所同向高费率时(实测 SNDK 两边都 -47%),
       跨所差几乎为零,但**现货买入 + 永续做空能吃满 47%**。
       实测:≥20% 的币,跨所口径 5 个,绝对值口径 8 个。
    """
    bn, by = set(), set()
    try:
        d = get("https://api.binance.com/api/v3/exchangeInfo")
        bn = {x["symbol"] for x in d["symbols"]
              if x.get("status") == "TRADING" and x["symbol"].endswith("USDT")}
    except Exception:
        pass
    try:
        d = get("https://api.bybit.com/v5/market/tickers?category=spot")
        by = {x["symbol"] for x in d["result"]["list"] if x["symbol"].endswith("USDT")}
    except Exception:
        pass
    return bn, by


def load_intervals():
    """各币的结算间隔。Binance 只列非 8h 的,查不到就是 8h。"""
    bn = {}
    try:
        for x in get("https://fapi.binance.com/fapi/v1/fundingInfo"):
            bn[x["symbol"]] = float(x["fundingIntervalHours"])
    except Exception:
        pass
    by = {}
    try:
        d = get("https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
        for x in d["result"]["list"]:
            by[x["symbol"]] = float(x.get("fundingInterval", 480)) / 60.0
    except Exception:
        pass
    return bn, by


def cmd_scan(args):
    """
    铺开扫描:**记录所有达到流动性门槛的币**,而不是只记高 carry 的。

    ⚠️ 早期版本用 `if abs(net) < record_above: continue` 过滤后才落盘,
       这会造成**选择性偏差** —— 数据库里只有"看起来有机会"的时刻,
       事后无法回答:
         · 正常状态是什么样
         · 机会出现的概率是多少
         · 低 carry 怎么演变成高 carry
       现在全部落盘,高 carry 只是多打一个 is_candidate 标记。

    ⚠️ 落盘的是**原始 per-settlement 费率 + 结算时点**,不是年化。
       年化是派生量,分析阶段再算 —— 因为资金费是**离散结算**的,
       "持有 N 分钟 × 年化"这种连续累计的算法是错的。
    """
    if not preflight():
        return 2
    bn_iv, by_iv = load_intervals()
    bn_spot, by_spot = load_spot_symbols()
    out = ROOT / (args.out or f"shadow/scan_{datetime.now(timezone.utc):%Y%m%d}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"=== 铺开扫描(settlement-centric)===")
    print(f"  流动性门槛  Bybit 24h 成交额 > ${args.min_turnover/1e6:,.0f}M")
    print(f"  落盘范围    **全部达标币种**(避免选择性偏差)")
    print(f"  候选标记    |净年化| > {args.record_above}% 记 is_candidate")
    print(f"  采样间隔    {args.interval}s(每轮 3 个批量请求)")
    print(f"  现货清单    币安 {len(bn_spot)} 个 / Bybit {len(by_spot)} 个"
          f"(用于判断现货+永续能不能做)")
    print(f"  落盘        {out}\n")
    n = 0
    try:
        while True:
            n += 1
            try:
                snap = bulk_snapshot(args.min_turnover)
            except Blocked as e:
                print(f"\n🔴 [{datetime.now():%H:%M:%S}] {e}")
                print(f"   这是地区封锁,继续跑只会刷屏。**退出,换机器。**")
                return 2
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] 抓取失败 {str(e)[:60]}", flush=True)
                time.sleep(args.interval)
                continue
            rows = []
            for sym, d in snap.items():
                iv_b = bn_iv.get(sym, 8.0)
                iv_y = by_iv.get(sym, 8.0)
                ba = d["bn_rate"] * (8760 / iv_b) * 100
                ya = d["by_rate"] * (8760 / iv_y) * 100
                net_ann = ba - ya
                # ★ 单所绝对费率 —— 现货+永续吃的是这个,不是跨所差
                if abs(ba) >= abs(ya):
                    abs_ann, abs_v = ba, "Binance"
                else:
                    abs_ann, abs_v = ya, "Bybit"
                has_spot = (sym in bn_spot) if abs_v == "Binance" else (sym in by_spot)
                # ★ 下一次结算的净 funding(bps)—— 比年化更接近真实经济意义
                #   注意:两边间隔可能不同,所以"下一次"未必同时发生
                net_next_bps = (d["bn_rate"] - d["by_rate"]) * 1e4
                basis = (d["bn_mark"] - d["by_mark"]) / d["by_mark"] * 1e4
                e_short_bn = (d["bn_bid"] - d["by_ask"]) / d["by_ask"] * 1e4
                e_short_by = (d["by_bid"] - d["bn_ask"]) / d["bn_ask"] * 1e4
                rows.append({
                    "s": sym,
                    "br": d["bn_rate"], "yr": d["by_rate"],      # 原始费率
                    "bi": iv_b, "yi": iv_y,                       # 结算间隔(小时)
                    "bn": d["bn_next"], "yn": d["by_next"],       # 下次结算时点(ms)
                    "bm": d["bn_mark"], "ym": d["by_mark"],       # 标记价
                    "bb": d["bn_bid"], "ba_": d["bn_ask"],
                    "yb": d["by_bid"], "ya_": d["by_ask"],
                    "bbq": d["bn_bq"], "baq": d["bn_aq"],         # 盘口量(双边流动性)
                    "ybq": d["by_bq"], "yaq": d["by_aq"],
                    "t": round(d["turnover"] / 1e6, 1),
                    "net_ann": round(net_ann, 2),
                    "abs_ann": round(abs_ann, 2),      # 单所绝对费率(带符号)
                    "abs_v": abs_v,                    # 哪个所
                    "spot": has_spot,                  # 该所有没有现货,能不能做现货+永续
                    "sbn": sym in bn_spot, "sby": sym in by_spot,
                    "net_next_bps": round(net_next_bps, 3),
                    "basis": round(basis, 2),
                    "e_sbn": round(e_short_bn, 2), "e_sby": round(e_short_by, 2),
                    "cand": abs(net_ann) >= args.record_above,
                })
            rows.sort(key=lambda r: -abs(r["net_ann"]))
            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "n": len(rows), "rows": rows}
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            cand = [r for r in rows if r["cand"]]
            big = sum(1 for r in rows if abs(r["net_ann"]) >= 20)
            bigabs = sum(1 for r in rows if abs(r["abs_ann"]) >= 20 and r["spot"])
            top = sorted(rows, key=lambda r: -abs(r["abs_ann"]))[:3]
            print(f"[{datetime.now():%H:%M:%S}] 全量 {len(rows)}  "
                  f"跨所≥20%: {big}  单所≥20%且有现货: {bigabs}   "
                  + "  ".join(f"{r['s'][:-4]}{r['abs_ann']:+.0f}%@{r['abs_v'][:3]}"
                              + ("" if r["spot"] else "(无现货)") for r in top), flush=True)
            if args.rounds and n >= args.rounds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止")
    print(f"\n采样 {n} 轮,记录在 {out}")
    return 0


def _pct(xs, q):
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(int(q / 100 * len(xs)), len(xs) - 1)
    return xs[i]


def cmd_scan_report(args):
    """
    settlement-centric 报告。

    ⚠️ **早期版本用 `年化 × 窗口分钟数` 估算能收多少资金费 —— 那是错的。**
       资金费按结算时点**离散**发生:
         · 高 carry 持续 5 小时但没跨结算点 → 实收 0
         · 只持续 10 分钟但跨了结算点     → 实收完整一期
       所以窗口时长**不能**换算成收益。

    现在的做法:从 next_funding_ms 的跳变检测真实结算事件,
    每次结算取**结算前最后一个快照的费率**(结算后的 rate 已经是下一期预测)。
    """
    import glob
    from collections import defaultdict
    files = sorted(glob.glob(str(ROOT / (args.pattern or "shadow/scan_*.jsonl"))))
    snaps = []
    for f in files:
        for l in open(f, encoding="utf-8"):
            try:
                d = json.loads(l)
            except Exception:
                continue
            if "rows" in d:                      # 新格式
                snaps.append(d)
    if len(snaps) < 10:
        print(f"新格式快照只有 {len(snaps)} 轮,先跑一段时间再来"); return 1
    snaps.sort(key=lambda r: r["ts"])
    t0 = datetime.fromisoformat(snaps[0]["ts"]); t1 = datetime.fromisoformat(snaps[-1]["ts"])
    hours = (t1 - t0).total_seconds() / 3600
    step = hours * 3600 / max(len(snaps) - 1, 1)
    syms = sorted({r["s"] for d in snaps for r in d["rows"]})
    print(f"=== settlement-centric 报告 ===")
    print(f"  {len(snaps)} 轮 / {len(syms)} 个 symbol / {hours:.1f} 小时"
          f"({t0:%m-%d %H:%M} → {t1:%m-%d %H:%M} UTC),间隔约 {step:.0f}s\n")

    # 按 symbol 重排时间序列
    series = defaultdict(list)
    for d in snaps:
        for r in d["rows"]:
            series[r["s"]].append((d["ts"], r))

    # ── ① 真实结算事件 ──────────────────────────────────
    events = defaultdict(list)      # sym -> [(idx, venue, settled_rate)]
    for sym, seq in series.items():
        for i in range(1, len(seq)):
            prev, cur = seq[i - 1][1], seq[i][1]
            for venue, nk, rk in (("Binance", "bn", "br"), ("Bybit", "yn", "yr")):
                if cur.get(nk, 0) > prev.get(nk, 0):
                    # ★ 用【结算前】最后一个快照的费率,不是结算后的新预测
                    events[sym].append((i, venue, prev[rk]))
    tot_ev = sum(len(v) for v in events.values())
    print(f"=== ① 结算事件 ===")
    print(f"  检测到 {tot_ev} 次(covering {len(events)} 个 symbol)")
    if tot_ev == 0:
        print(f"  🔴 采样时长 {hours:.1f}h 还没跨过任何结算点(间隔 4~8h),")
        print(f"     **至少要跑满 8 小时**才能开始统计。\n")

    # ── ② 窗口统计(只描述信号,不换算收益)────────────────
    print(f"\n=== ② 窗口统计(duration ≠ funding earned)===")
    for thr in (10, 20, 40):
        wins, cur = [], {}
        for i, d in enumerate(snaps):
            hit = {r["s"]: r["net_ann"] for r in d["rows"] if abs(r["net_ann"]) >= thr}
            for s_ in hit:
                cur.setdefault(s_, i)
            for s_ in list(cur):
                if s_ not in hit:
                    wins.append((s_, cur[s_], i - 1)); del cur[s_]
        for s_, b in cur.items():
            wins.append((s_, b, len(snaps) - 1))
        if not wins:
            print(f"  ≥{thr}%   无窗口"); continue
        durs = sorted((e - b + 1) * step / 60 for _, b, e in wins)
        print(f"  ≥{thr}%   {len(wins)} 个窗口 / {len({w[0] for w in wins})} 个币   "
              f"{len(wins)/max(hours/24,1e-9):.1f} 个/天")
        print(f"          时长 中位 {_pct(durs,50):.0f}分  p75 {_pct(durs,75):.0f}分  "
              f"p90 {_pct(durs,90):.0f}分  最长 {max(durs):.0f}分")

    # ── ③ 结算捕获统计(核心)─────────────────────────────
    print(f"\n=== ③ 结算捕获(这才是能不能覆盖成本的依据)===")
    if tot_ev == 0:
        print(f"  数据不足,跳过。")
    else:
        for thr in (10, 20, 40):
            wins, cur = [], {}
            for i, d in enumerate(snaps):
                hit = {r["s"]: r["net_ann"] for r in d["rows"] if abs(r["net_ann"]) >= thr}
                for s_ in hit:
                    cur.setdefault(s_, (i, hit[s_]))
                for s_ in list(cur):
                    if s_ not in hit:
                        wins.append((s_, cur[s_][0], i - 1, cur[s_][1])); del cur[s_]
            for s_, (b, sgn) in cur.items():
                wins.append((s_, b, len(snaps) - 1, sgn))
            if not wins:
                continue
            caps, ncross = [], []
            for s_, b, e, sgn in wins:
                short_bn = sgn > 0          # net_ann>0 → 空 Binance、多 Bybit
                got = 0.0; k = 0
                for idx, venue, rate in events.get(s_, []):
                    if not (b < idx <= e):
                        continue
                    k += 1
                    if venue == "Binance":
                        got += rate * 1e4 * (1 if short_bn else -1)
                    else:
                        got += rate * 1e4 * (-1 if short_bn else 1)
                caps.append(got); ncross.append(k)
            c1 = sum(1 for k in ncross if k >= 1); c2 = sum(1 for k in ncross if k >= 2)
            print(f"\n  ≥{thr}%  {len(wins)} 个窗口")
            print(f"        跨过 ≥1 次结算  {c1}/{len(wins)} = {c1/len(wins)*100:.0f}%")
            print(f"        跨过 ≥2 次结算  {c2}/{len(wins)} = {c2/len(wins)*100:.0f}%")
            print(f"        平均跨过 {sum(ncross)/len(ncross):.2f} 次")
            nz = [c for c, k in zip(caps, ncross) if k > 0]
            if nz:
                print(f"        实收净 funding(跨过结算的窗口,bps):")
                print(f"          中位 {_pct(nz,50):+.2f}   p75 {_pct(nz,75):+.2f}   "
                      f"p90 {_pct(nz,90):+.2f}   最大 {max(nz):+.2f}")
                print(f"        vs 假设往返手续费 {args.fee_roundtrip} bps  "
                      + ("✅ 中位能覆盖" if _pct(nz,50) > args.fee_roundtrip else "🔴 中位覆盖不了"))

    print(f"\n{'='*66}")
    print(f"⚠️ 口径声明")
    print(f"   · 手续费按 {args.fee_roundtrip} bps 往返(assumed,非实测)")
    print(f"   · 未计入:滑点、单腿失败、平仓价差、基差变化")
    print(f"   · 「实收 funding」是从结算前快照的费率推的,标记为 estimated;")
    print(f"     交易所官方历史 funding 可以回填确认,本版未做")
    print(f"   · **本报告只回答「资金费够不够付手续费」,不构成 GO/NO-GO**")
    return 0


def main():
    p = argparse.ArgumentParser(description="跨所 carry 实时记录器(只读,无下单能力)")
    p.add_argument("--coin", default="SKHYNIX")
    p.add_argument("--venues", default="Binance,Bybit")
    p.add_argument("--interval", type=int, default=20, help="采样间隔(秒)")
    p.add_argument("--alert-days", type=float, default=3.0,
                   help="回本天数低于此值才可能标 ★")
    p.add_argument("--min-carry", type=float, default=15.0,
                   help="净年化低于此值不标 ★(默认 15%%)")
    p.add_argument("--taker", action="store_true", help="按吃单费率算(默认挂单)")
    p.add_argument("--out", default=None, help="JSONL 落盘路径")
    p.add_argument("--rounds", type=int, default=0, help="跑几轮后退出,0=一直跑")
    p.add_argument("--info", action="store_true", help="只打印合约规格然后退出")
    p.add_argument("--force", action="store_true",
                   help="资金费历史体检不通过时仍然继续(默认拒绝)")
    p.add_argument("--scan", action="store_true",
                   help="★ 铺开扫描全市场(而不是单个币)")
    p.add_argument("--scan-report", action="store_true", help="分析扫描结果")
    p.add_argument("--pattern", help="--scan-report 的文件匹配")
    p.add_argument("--fee-roundtrip", type=float, default=None,
                   help="手动覆盖往返手续费 bps。默认 None = 按币种自动查 "
                        "lib/fees.py 的实测表(代币化股票 13.5 / 加密 21)")
    p.add_argument("--min-turnover", type=float, default=30e6, help="流动性门槛(USDT)")
    p.add_argument("--record-above", type=float, default=20.0,
                   help="标记 is_candidate 的门槛(%%)。**不再用于过滤落盘**")
    # ── 持仓跟踪 ──
    g = p.add_argument_group("持仓跟踪(填了就进入跟踪模式,会发平仓提醒)")
    g.add_argument("--short", metavar="所:开仓价", help="做空腿,如 Bybit:143.50")
    g.add_argument("--long", metavar="所:开仓价", help="做多腿,如 Binance:143.47")
    g.add_argument("--qty", type=float, help="每条腿的数量")
    g.add_argument("--carry-floor", type=float, default=0.0,
                   help="净年化连续低于此值就提醒平仓(默认 0)")
    g.add_argument("--carry-strikes", type=int, default=5,
                   help="连续几次低于 floor 才提醒(默认 5,防抖)")
    g.add_argument("--target", type=float, default=None,
                   help="累计资金费达到此金额(USDT)就提醒")
    args = p.parse_args()

    if args.scan_report:
        return cmd_scan_report(args)
    if args.scan:
        return cmd_scan(args)

    coin = args.coin.upper()
    vs = [v.strip() for v in args.venues.split(",")]
    if len(vs) != 2 or any(v not in SNAP for v in vs):
        print(f"--venues 需要两个,且必须在 {list(SNAP)} 里"); return 1

    print(f"=== {coin} 合约规格 ===")
    info = contract_info(coin)
    for v, d in info.items():
        if "err" in d:
            print(f"  {v:<9} 读不到:{d['err']}"); continue
        print(f"  {v:<9} 最小下单 {d['min_qty']:<10g} 步长 {d['qty_step']:<10g} "
              f"价格精度 {d['tick']:g}"
              + (f"  最大杠杆 {d['max_lev']:g}x" if "max_lev" in d else "")
              + (f"  最小名义 ${d['min_notional']:g}" if d.get("min_notional") else ""))

    iv = {}
    for v in vs:
        iv[v] = interval_binance(coin) if v == "Binance" else interval_bybit(coin)
    print(f"\n=== 结算间隔 ===")
    for v in vs:
        print(f"  {v:<9} {iv[v]:g} 小时  →  一年 {8760/iv[v]:.0f} 次")
    print(f"  ⚠️ Binance 的 fundingInfo 只列非 8 小时合约,查不到就是默认 8 小时。")

    if args.info:
        try:
            a, b = SNAP[vs[0]](coin), SNAP[vs[1]](coin)
            # 两条腿必须**同时**开得出来 → 约束是两所最小量里**较大**的那个
            qtys = {v: info[v]["min_qty"] for v in vs if "min_qty" in info[v]}
            mn = max(qtys.values())
            who = max(qtys, key=qtys.get)
            px = (a["mark"] + b["mark"]) / 2
            notional = mn * px
            # 还要满足各所的最小名义金额
            for v in vs:
                need = info[v].get("min_notional", 0)
                if need and notional < need:
                    mn = need / px
                    notional = need
                    who = f"{v}(最小名义 ${need:g})"
            print(f"\n=== 最小一笔要多少钱(现价 ${px:,.2f})===")
            for v, q in qtys.items():
                print(f"  {v:<9} 最小 {q:g} 个 = ${q*px:,.2f}")
            print(f"  → 两条腿要同时开,**取较大者**:{mn:g} 个 = "
                  f"名义 ${notional:,.2f} / 每条腿   (受限于 {who})")
            for lev in (2, 3, 5):
                print(f"  {lev}x 杠杆 → 每条腿保证金 ${notional/lev:,.2f},"
                      f"两条腿共 ${notional/lev*2:,.2f}")
            print(f"\n  ⚠️ 这是**开得起**的下限,不是**该充**的量。")
            print(f"     保证金只够刚好开仓 = 一点波动就爆。建议留 5 倍以上余量。")
        except Exception as e:
            print(f"  取现价失败:{str(e)[:60]}")
        return 0

    # 基差历史 —— 用来判断「此刻是不是好的进场点」
    OPEN_HOURS = {0, 1, 2, 3, 4, 5, 13, 14}      # 由 funding_probe.py session 实测得出
    brows, berr = basis_history(coin)
    bstats = basis_stats(brows, OPEN_HOURS) if brows else {}
    if berr:
        print(f"\n  ⚠️ 基差历史拉取失败:{berr},将跳过时机判断")
    else:
        print(f"\n=== 基差历史({len(brows)} 小时)===")
        for k in ("开市", "休市", "全部"):
            v = bstats.get(k)
            if v:
                print(f"  {k:<5} {v['n']:>5} 小时   均值 {v['mean']:>+7.1f} bps   "
                      f"标准差 {v['sd']:>5.1f}")
        print(f"  基差 = (Binance − Bybit) / Bybit")

    # ★ 资金费历史体检 —— 实时数据只报此刻,历史才知道此刻是不是假象
    cur_net = None
    try:
        _a, _b = SNAP[vs[0]](coin), SNAP[vs[1]](coin)
        cur_net = (_a["rate"] * (8760 / iv[vs[0]])
                   - _b["rate"] * (8760 / iv[vs[1]])) * 100
    except Exception:
        pass
    hw, hwarn = history_check(coin, vs, iv[vs[0]], cur_net)
    print(f"\n=== 资金费历史体检(净 = {vs[0]} − {vs[1]})===")
    if hw:
        print(f"  {'窗口':<8}{'天':>6}{vs[0]:>10}{vs[1]:>10}{'净年化':>10}"
              f"{'±标准误':>9}{'t':>7}{'净为负':>8}")
        for tag in ("1天", "7天", "30天", "全部"):
            w = hw.get(tag)
            if w:
                print(f"  {tag:<8}{w['days']:>6.1f}{w['a']:>+10.1f}{w['b']:>+10.1f}"
                      f"{w['mean']:>+10.1f}{w['se']:>9.1f}{w['t']:>7.1f}{w['neg']:>7.0f}%")
        if cur_net is not None:
            print(f"  {'★此刻':<8}{'—':>6}{'—':>10}{'—':>10}{cur_net:>+10.1f}")
    for w in hwarn:
        print(f"  {w}")
    if hw:
        print(f"  ⚠️ t 值假设各期独立,而资金费是自相关的 —— **真实标准误比这更大,"
              f"t 更接近 0**。所以这里的 t 是【乐观】的上界。")
    red = [w for w in hwarn if w.startswith("🔴")]
    # ★ 只在「还没开仓」时拦。已经有仓位了(--short/--long/--qty 都给了)
    #   反而更需要监控 —— 这时候拦下来等于让你在有敞口时失去眼睛。
    holding = bool(args.short and args.long and args.qty)
    if red and holding:
        print(f"\n  ⚠️ 体检 {len(red)} 项不通过,但你已有持仓 —— 继续监控。")
        print(f"     这些红旗现在是**平仓依据**,不是入场依据。")
    if red and not holding and not args.force:
        print(f"\n{'='*66}")
        print(f"🔴 体检未通过({len(red)} 项)。**默认拒绝继续。**")
        print(f"   实时面板上那个漂亮的净年化,历史说它站不住。")
        print(f"   VELVET 就是这么来的:此刻 +13.2%,33 天真值 −10.6%。")
        print(f"\n   确认要看,加 --force 重跑。")
        print(f"{'='*66}")
        return 2
    if not red and hw:
        print(f"  ✅ 体检通过 —— 当前值和长期历史不矛盾")

    out = ROOT / (args.out or f"shadow/carry_{coin}_{datetime.now(timezone.utc):%Y%m%d}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n落盘 {out}")
    acls = FEEMOD.asset_class(coin)
    _rt, _meas, _det = FEEMOD.roundtrip(vs[0], vs[1], acls, maker=args.taker is False)
    print(f"每 {args.interval}s 采样一次,按 {'吃单' if args.taker else '挂单'} 费率计算")
    print(f"资产类别 {coin} → **{acls}**"
          + ("(代币化股票/商品,费率和加密不同)" if acls == "tradfi" else ""))
    if args.fee_roundtrip is not None:
        print(f"往返手续费 {args.fee_roundtrip} bps ← 命令行手动指定,已覆盖实测表")
    else:
        print(f"往返手续费 {_det}   {FEEMOD.fee_note(_meas)}")
    print(f"回本 < {args.alert_days} 天就标 ★\n")

    # ── 跟踪模式初始化 ──
    track = None
    if args.short and args.long and args.qty:
        sv, spx = parse_leg(args.short)
        lv, lpx = parse_leg(args.long)
        if {sv, lv} != set(vs):
            print(f"🔴 --short/--long 的交易所({sv}/{lv})和 --venues({vs})对不上")
            return 1
        notional = args.qty * (spx + lpx) / 2
        track = {"sv": sv, "spx": spx, "lv": lv, "lpx": lpx, "qty": args.qty,
                 "notional": notional, "funding": 0.0, "strikes": 0,
                 "last_next": {}, "alerted": set()}
        print(f"\n=== 持仓跟踪 ===")
        print(f"  空 {sv} @ {spx}   多 {lv} @ {lpx}   数量 {args.qty}")
        print(f"  名义 ${notional:.2f}   开仓价差 {(spx-lpx)/((spx+lpx)/2)*1e4:+.2f} bps")
        print(f"  提醒:净年化连续 {args.carry_strikes} 次 < {args.carry_floor}%"
              + (f",或累计资金费 ≥ ${args.target}" if args.target else ""))
        print(f"  提醒会写进 shadow/alerts.log 并响铃\n")

    ALERTS = ROOT / "shadow" / "alerts.log"
    n, best = 0, None
    try:
        while True:
            n += 1
            try:
                a = SNAP[vs[0]](coin)
                b = SNAP[vs[1]](coin)
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] 抓取失败 {str(e)[:60]}")
                time.sleep(args.interval)
                if args.rounds and n >= args.rounds:
                    break
                continue

            ev = evaluate(a, b, iv[vs[0]], iv[vs[1]], maker=not args.taker,
                          cls=acls, fee_override=args.fee_roundtrip)

            # 当前基差 + 它在历史里的位置
            cur_basis = (a["mark"] - b["mark"]) / b["mark"] * 1e4 if a["venue"] == "Binance" \
                else (b["mark"] - a["mark"]) / a["mark"] * 1e4
            h = datetime.now(timezone.utc).hour
            seg = "开市" if h in OPEN_HOURS else "休市"
            bv = basis_verdict(cur_basis, bstats.get(seg)) if bstats else None
            rec_extra = {"basis_bps": cur_basis, "session": seg,
                         "basis_z": bv["z"] if bv else None,
                         "basis_pct": bv["pct"] if bv else None}

            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "coin": coin, "snap": {a["venue"]: a, b["venue"]: b},
                   "interval_h": iv, "eval": ev}
            rec.update(rec_extra)
            with open(out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            # ★ 必须**同时**满足:carry 够大 + 回本够快。
            #   只满足一个的不标 —— 尤其是「开仓即赚但没有 carry」那种,
            #   那是价差套利的机会,不是 carry 的机会。
            good = [d for d in ev["dirs"]
                    if d["net_ann"] >= args.min_carry
                    and d["payback_days"] <= args.alert_days]
            d = max(ev["dirs"], key=lambda x: x["net_ann"])
            # 做空 Binance = 做空基差 → 基差偏【高】才是好时机
            timing = ""
            if bv:
                shorting_binance = d["short"] == "Binance"
                fav = (bv["z"] > 0.5) if shorting_binance else (bv["z"] < -0.5)
                bad = (bv["z"] < -0.5) if shorting_binance else (bv["z"] > 0.5)
                timing = ("  ✅时机好" if fav else "  🔴时机差" if bad else "  ·时机中性")
            star = "★" if good else " "
            if best is None or d["net_ann"] > best:
                best = d["net_ann"]
            eta = min((s["next_ms"] for s in (a, b)), default=0)
            mins = max(0, (eta - time.time() * 1000) / 60000)
            print(f"{star}[{datetime.now():%H:%M:%S}] "
                  f"{ev['ann'][vs[0]]:>+7.1f}% / {ev['ann'][vs[1]]:>+7.1f}%  →  "
                  f"空{d['short'][:3]} 净 {d['net_ann']:>+7.1f}%  "
                  f"开仓 {d['entry_bps']:>+5.1f}bps  "
                  f"成本 {d['total_cost_bps']:>5.1f}bps  "
                  + (f"回本  即赚  " if d["payback_days"] == 0
                     else "回本   ——   " if d["payback_days"] == float("inf")
                     else f"回本 {d['payback_days']:>5.1f}天  ")
                  + 
                  f"基差 {cur_basis:>+6.1f}bps"
                  + (f"({bv['z']:>+4.1f}σ)" if bv else "")
                  + timing, flush=True)

            # ── 持仓跟踪 ──
            if track:
                snaps = {a["venue"]: a, b["venue"]: b}
                pnl, esp, nsp = track_pnl(snaps, track["sv"], track["spx"],
                                          track["lv"], track["lpx"], track["qty"])
                # 结算时刻跨过去了就累计一次资金费
                for v in (track["sv"], track["lv"]):
                    nx = snaps[v]["next_ms"]
                    prev = track["last_next"].get(v)
                    if prev and nx > prev:          # 结算点往后跳 = 刚结算过
                        rate = snaps[v]["rate"]
                        got = track["notional"] * rate * (1 if v == track["sv"] else -1)
                        track["funding"] += got
                        print(f"    💰 {v} 结算 {rate*100:+.4f}% → {got:+.5f} USDT"
                              f"(累计 {track['funding']:+.5f})", flush=True)
                    track["last_next"][v] = nx
                total = pnl + track["funding"]
                print(f"    持仓  基差盈亏 {pnl:+.5f}  资金费 {track['funding']:+.5f}"
                      f"  合计 {total:+.5f} USDT   (价差 {esp:.3f}→{nsp:.3f})", flush=True)

                # 提醒 1:carry 转负
                cur = next((x for x in ev["dirs"] if x["short"] == track["sv"]), None)
                if cur and cur["net_ann"] < args.carry_floor:
                    track["strikes"] += 1
                    if track["strikes"] == args.carry_strikes and "carry" not in track["alerted"]:
                        alert(f"净年化 {cur['net_ann']:+.1f}% 连续 {args.carry_strikes} 次 "
                              f"低于 {args.carry_floor}% —— **收入没了,建议平仓**", ALERTS)
                        track["alerted"].add("carry")
                else:
                    track["strikes"] = 0
                # 提醒 2:达标
                if args.target and track["funding"] >= args.target \
                        and "target" not in track["alerted"]:
                    alert(f"累计资金费 {track['funding']:+.5f} 已达标 ${args.target} "
                          f"—— **目的达到,可以平仓**", ALERTS)
                    track["alerted"].add("target")

            if args.rounds and n >= args.rounds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止")
    print(f"\n采样 {n} 次,最好的净年化 {best:+.1f}%" if best is not None else "")
    print(f"记录在 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
