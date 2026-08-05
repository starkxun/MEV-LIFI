#!/usr/bin/env python3
"""
LI.FI 通用成本探针 —— 链上套利残酷共学 · Week 1
====================================================
Day-0 探针的升级版。Day-0 只能跑写死的 ARB→BAS USDC;
这一版:**输入任意路径,输出 token 口径的真实 bps 门槛**。

相比 Day-0 的四个升级:
  1. 任意路径:链 / 资产 / 规模全部命令行传入,decimals 自动查询(不再写死 6)
  2. 规模扫描:一次跑多个规模,画出成本曲线 —— 看清"固定成本 vs 比例成本"
  3. 往返模式:跨资产路径(USDC→WETH)无法直接相减,用"闭环回到原资产"
     来得到**零价格假设**的真实成本。这是铁律1 在任意资产对上的推广。
  4. 诚实分账:把成本拆成【零假设的硬成本】和【需要价格折算的部分】,
     绝不把两者混成一个数字糊弄自己。

用法:
    pip install requests

    # 单程同资产(最经典的稳定币搬砖)
    python3 cost_probe.py --from-chain ARB --to-chain BAS --token USDC \\
        --amounts 100,1000,10000,100000

    # 跨资产 —— 会自动提示你用往返模式
    python3 cost_probe.py --from-chain ARB --to-chain BAS \\
        --from-token USDC --to-token WETH --amounts 10000

    # 往返闭环(跨资产路径的唯一诚实算法)
    python3 cost_probe.py --from-chain ARB --to-chain BAS \\
        --from-token USDC --to-token WETH --amounts 1000,10000 --roundtrip

    # 同链三角/DEX 交易也能跑(from-chain == to-chain)
    python3 cost_probe.py --from-chain ARB --to-chain ARB \\
        --from-token USDC --to-token WETH --amounts 10000 --roundtrip

    # 导出证据记录表(第 4 节 schema)
    python3 cost_probe.py --from-chain ARB --to-chain BAS --token USDC \\
        --amounts 1000,10000 --csv evidence.csv

LI.FI 报价接口只读、免 key,不需要钱包里有钱。
文档: https://docs.li.fi/api-reference/introduction
"""

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

BASE = "https://li.quest/v1"

# 未认证限速实测:100 次 / 60 秒窗口。留足余量,别把自己打挂。
PACE_SECONDS = 0.35
MAX_RETRY = 4

# 只读报价用的占位地址,不动你的钱,只用来让 LI.FI 估 gas
DEFAULT_ADDRESS = "0x552008c0f6870c2f77e5cC1d2eb9bdff03e30Ea0"


# ============================================================
# 一、HTTP 层:限速、重试、错误分类
# ============================================================

_session = requests.Session()
_last_call = [0.0]


def _pace():
    """限速自保:两次调用之间至少隔 PACE_SECONDS。"""
    gap = time.monotonic() - _last_call[0]
    if gap < PACE_SECONDS:
        time.sleep(PACE_SECONDS - gap)
    _last_call[0] = time.monotonic()


class NoQuote(Exception):
    """这条路在这个规模上报不出价 —— 这本身就是一条结论,不是脚本坏了。"""


def _get(path, params):
    """带重试的 GET。429/5xx 退避重试,4xx 直接判定为'这条路不通'。"""
    delay = 1.0
    last_err = None
    for attempt in range(MAX_RETRY):
        _pace()
        try:
            r = _session.get(f"{BASE}{path}", params=params, timeout=40)
        except requests.RequestException as e:
            last_err = f"网络错误: {e}"
            time.sleep(delay)
            delay *= 2
            continue

        if r.status_code == 200:
            return r.json()

        if r.status_code == 429:
            # 限速了,按 ratelimit-reset 等,等不到头就指数退避
            wait = float(r.headers.get("ratelimit-reset", delay) or delay)
            print(f"    [限速] 等 {wait:.0f}s 后重试…", file=sys.stderr)
            time.sleep(min(wait, 65))
            continue

        if 500 <= r.status_code < 600:
            last_err = f"服务端 {r.status_code}"
            time.sleep(delay)
            delay *= 2
            continue

        # 4xx:参数错 / 没有路由 / 金额太小。取出 LI.FI 给的原因。
        try:
            body = r.json()
            msg = body.get("message") or body.get("errors") or body
        except ValueError:
            msg = r.text[:200]
        raise NoQuote(f"HTTP {r.status_code}: {msg}")

    raise NoQuote(f"重试 {MAX_RETRY} 次仍失败 ({last_err})")


_token_cache = {}


def get_token(chain, token):
    """查 token 元数据 —— decimals 必须查,写死 decimals 是 Day-0 脚本最大的局限。"""
    key = (str(chain).upper(), str(token).upper())
    if key not in _token_cache:
        _token_cache[key] = _get("/token", {"chain": chain, "token": token})
    return _token_cache[key]


def get_quote(from_chain, to_chain, from_token, to_token, from_amount_raw, address):
    return _get(
        "/quote",
        {
            "fromChain": from_chain,
            "toChain": to_chain,
            "fromToken": from_token,
            "toToken": to_token,
            "fromAmount": str(int(from_amount_raw)),
            "fromAddress": address,
        },
    )


# ============================================================
# 二、单腿解析:把一次报价拆成"硬成本"和"需要折算的成本"
# ============================================================

@dataclass
class Leg:
    """一条腿(一次 quote)的解析结果。所有金额都是人类可读单位。"""
    tool: str
    duration: int
    from_sym: str
    to_sym: str
    from_units: float
    to_units: float
    to_min_units: float
    from_price: float          # 报价时刻的标价,仅用于折算 gas,不用于算硬成本
    to_price: float
    gas_usd: float             # 原生币 gas,链下另付,不在 toAmount 里
    extra_fee_usd: float       # included=False 的费用,同样另付
    included_fee_usd: float    # included=True,已经从 toAmount 里扣掉了,仅供参考
    warnings: list = field(default_factory=list)

    @property
    def offchain_usd(self):
        """账外成本:gas + 未包含费。这部分必须靠价格折算,是"有假设"的部分。"""
        return self.gas_usd + self.extra_fee_usd


def parse_leg(q):
    est = q.get("estimate", {})
    act = q.get("action", {})
    ft, tt = act.get("fromToken", {}), act.get("toToken", {})
    warns = []

    d_from = int(ft.get("decimals", 18))
    d_to = int(tt.get("decimals", 18))

    from_units = int(est.get("fromAmount", 0)) / 10 ** d_from
    to_units = int(est.get("toAmount", 0)) / 10 ** d_to
    to_min_units = int(est.get("toAmountMin", 0)) / 10 ** d_to

    # 不变量检查:保底值不该大于预期值。LI.FI 偶尔会返回反常数据,
    # 不检查就会在"滑点缓冲"上算出负数还不自知。
    if to_min_units > to_units:
        warns.append(
            f"数据异常: toAmountMin({to_min_units:.6f}) > toAmount({to_units:.6f}),"
            f"该路径的保底值不可信"
        )

    gas_usd = sum(float(g.get("amountUSD") or 0) for g in est.get("gasCosts", []))

    inc_usd = 0.0
    extra_usd = 0.0
    for f in est.get("feeCosts", []):
        amt = float(f.get("amountUSD") or 0)
        if "included" not in f:
            warns.append(f"费用项 '{f.get('name')}' 缺少 included 字段,已按'已包含'处理")
            inc_usd += amt
        elif f["included"]:
            inc_usd += amt          # 已经从 toAmount 扣了,再加一次就是重复计算
        else:
            extra_usd += amt        # 你要另外掏钱,必须加进成本

    return Leg(
        tool=q.get("tool", "?"),
        duration=int(est.get("executionDuration") or 0),
        from_sym=ft.get("symbol", "?"),
        to_sym=tt.get("symbol", "?"),
        from_units=from_units,
        to_units=to_units,
        to_min_units=to_min_units,
        from_price=float(ft.get("priceUSD") or 0),
        to_price=float(tt.get("priceUSD") or 0),
        gas_usd=gas_usd,
        extra_fee_usd=extra_usd,
        included_fee_usd=inc_usd,
        warnings=warns,
    )


# ============================================================
# 三、成本核算:硬成本(零假设) + 折算成本(有假设)
# ============================================================

@dataclass
class Result:
    size: float                # 本金规模(from token 单位)
    ok: bool
    reason: str = ""
    legs: list = field(default_factory=list)
    end_units: float = 0.0     # 闭环回到原资产的数量(往返模式)
    hard_cost: float = 0.0     # token 口径硬成本,零价格假设
    offchain_usd: float = 0.0
    offchain_units: float = 0.0
    total_units: float = 0.0
    hard_bps: float = 0.0
    offchain_bps: float = 0.0
    total_bps: float = 0.0
    duration: int = 0
    tools: str = ""
    warnings: list = field(default_factory=list)


def analyse(size, legs, same_asset, entry_price):
    """
    把若干条腿合成一个结论。

    same_asset=True 时(单程同资产,或往返闭环),终点资产 == 起点资产,
    于是 硬成本 = 投入数量 - 到手数量,**完全不需要任何价格**。这是铁律1的核心。

    gas 和未包含费是用原生币另付的,和被搬运的 token 不是同种资产,
    折算必须用价格 —— 这部分单独列,永远不和硬成本混在一起。
    """
    r = Result(size=size, ok=True, legs=legs)
    r.duration = sum(l.duration for l in legs)
    r.tools = " → ".join(l.tool for l in legs)
    for l in legs:
        r.warnings.extend(l.warnings)

    r.end_units = legs[-1].to_units
    r.offchain_usd = sum(l.offchain_usd for l in legs)

    # 折算:账外美元 → 起点资产的数量。稳定币时几乎无损;波动资产时是估算。
    r.offchain_units = r.offchain_usd / entry_price if entry_price > 0 else 0.0

    if same_asset:
        r.hard_cost = size - r.end_units
    else:
        r.hard_cost = float("nan")   # 跨资产单程无法定义,交给往返模式

    r.total_units = r.hard_cost + r.offchain_units
    if size > 0:
        r.hard_bps = r.hard_cost / size * 10_000
        r.offchain_bps = r.offchain_units / size * 10_000
        r.total_bps = r.total_units / size * 10_000
    return r


def probe_size(args, size, from_meta):
    """跑一个规模。往返模式下跑两条腿。"""
    d_from = int(from_meta["decimals"])
    raw = int(round(size * 10 ** d_from))

    q1 = get_quote(args.from_chain, args.to_chain, args.from_token,
                   args.to_token, raw, args.address)
    leg1 = parse_leg(q1)
    legs = [leg1]

    if args.roundtrip:
        # 回程:拿去程实际到手的数量原样送回来,闭环回到起点资产。
        # 用 toAmount(预期到手)而不是 toAmountMin,保持和去程口径一致。
        back_raw = int(q1["estimate"]["toAmount"])
        q2 = get_quote(args.to_chain, args.from_chain, args.to_token,
                       args.from_token, back_raw, args.address)
        legs.append(parse_leg(q2))

    same_asset = args.roundtrip or (leg1.from_sym.upper() == leg1.to_sym.upper())
    entry_price = leg1.from_price or float(from_meta.get("priceUSD") or 0)
    return analyse(size, legs, same_asset, entry_price)


# ============================================================
# 四、输出
# ============================================================

def fmt_size(x):
    """规模格式化:大额加千分位不带小数,小额保留有效位(别把 0.5 印成 0)。"""
    return f"{x:,.0f}" if x >= 100 else f"{x:,g}"


def decompose(results):
    """
    判断成本随规模变化的**形态**,再决定怎么拆。

    两种截然不同的形态,不能用同一套话术:

    A. 摊薄型(门槛随规模下降):成本 ≈ 固定成本 + 比例费率 × 规模。
       固定成本(gas)被摊薄,比例成本(桥费)摊不薄 → 门槛有个地板。

    B. 冲击型(门槛随规模上升):深度不够,滑点/路由降级随规模变凶。
       这时候成本对规模是**凸的**,两点拟合会解出"负的固定成本"这种
       没有物理意义的东西 —— 一旦出现,就说明模型选错了,必须换 B 的解释。
    """
    ok = [r for r in results if r.ok and r.size > 0]
    if len(ok) < 2:
        return None
    lo, hi = ok[0], ok[-1]
    if hi.size == lo.size:
        return None

    marginal = (hi.total_units - lo.total_units) / (hi.size - lo.size)
    fixed = lo.total_units - marginal * lo.size

    # 门槛明显上升,或拟合出负固定成本 → 冲击型,线性拆解不成立
    impact = (hi.total_bps > lo.total_bps + 0.5) or (fixed < 0)
    return {
        "impact": impact,
        "fixed": fixed,
        "marginal_bps": marginal * 10_000,
        "lo": lo,
        "hi": hi,
    }


def render(args, results, from_meta):
    sym = from_meta["symbol"]
    mode = "往返闭环" if args.roundtrip else "单程"
    same = args.roundtrip or args.from_token.upper() == args.to_token.upper()
    kind = "同资产" if same else "跨资产"
    path = (f"{args.from_chain}.{args.from_token} → "
            f"{args.to_chain}.{args.to_token}")
    if args.roundtrip:
        path += f" → {args.from_chain}.{args.from_token}"

    print()
    print("=" * 78)
    print(f"LI.FI 通用成本探针  |  {path}")
    print(f"模式: {mode}·{kind}  |  时间: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    print("=" * 78)

    if not same:
        print("⚠  跨资产单程:终点和起点不是同种资产,token 口径的成本**无法定义**。")
        print("   下面只报告实现汇率(客观事实),不报门槛。")
        print("   要拿到真实 bps 门槛,请加 --roundtrip 让闭环回到起点资产。")
        print("-" * 78)
        for r in results:
            if not r.ok:
                print(f"  {r.size:>12,.2f} {sym}  ✗ {r.reason}")
                continue
            l = r.legs[0]
            rate = l.to_units / l.from_units if l.from_units else 0
            print(f"  {r.size:>12,.2f} {sym} → {l.to_units:,.8f} {l.to_sym}"
                  f"   实现汇率 {rate:,.10f}   [{l.tool}]")
            print(f"  {'':>12}   账外成本(gas+未含费) ${r.offchain_usd:,.4f}"
                  f"  ≈ {r.offchain_bps:,.2f} bps")
        print("=" * 78)
        _warn_block(results)
        return

    # ---- 同资产:可以给出真实门槛 ----
    end_label = "回到" if args.roundtrip else "到手"
    print(f"{'规模':>13} {end_label:>15} {'硬成本':>10} {'账外bps':>9} "
          f"{'真实门槛':>10} {'耗时':>7}  路径")
    print(f"{'('+sym+')':>13} {'('+sym+')':>15} {'(bps)':>10} {'(gas等)':>9} "
          f"{'(bps)':>10} {'(秒)':>7}")
    print("-" * 78)
    for r in results:
        if not r.ok:
            print(f"{r.size:>13,.2f} {'—':>15}  ✗ {r.reason[:44]}")
            continue
        print(f"{r.size:>13,.2f} {r.end_units:>15,.4f} {r.hard_bps:>10,.2f} "
              f"{r.offchain_bps:>9,.2f} {r.total_bps:>10,.2f} {r.duration:>7,d}"
              f"  {r.tools}")
    print("=" * 78)

    ok = [r for r in results if r.ok]
    if not ok:
        print("所有规模都报不出价 —— 这条路径当前不可用,本身就是结论。")
        return

    best = min(ok, key=lambda r: r.total_bps)
    print(f">> 真实门槛(token 口径): 最低 {best.total_bps:,.2f} bps"
          f" @ 规模 {fmt_size(best.size)} {sym}")
    if args.roundtrip:
        print(f"   含义: 走完这个闭环本身就亏 {best.total_bps:,.2f} bps。")
        print(f"         任何基于它的套利,毛价差必须 > {best.total_bps:,.2f} bps 才可能为正。")
    else:
        print(f"   含义: {args.to_chain} 上的价格必须比 {args.from_chain} 高"
              f" {best.total_bps:,.2f} bps 才刚好回本。")

    d = decompose(ok)
    if d:
        lo, hi = d["lo"], d["hi"]
        print("-" * 78)
        if d["impact"]:
            # 冲击型:门槛随规模变大而恶化 —— 这是"深度不够"的签名
            print(f">> 成本形态: **冲击型** —— 门槛随规模上升,"
                  f"{lo.total_bps:,.2f} → {hi.total_bps:,.2f} bps"
                  f"(规模 {fmt_size(lo.size)} → {fmt_size(hi.size)} {sym})")
            print(f"   这不是 gas 摊薄的问题,是深度不够:规模越大,"
                  f"滑点和路由降级吃得越狠。")
            print(f"   边际成本约 {d['marginal_bps']:,.2f} bps —— "
                  f"从 {fmt_size(lo.size)} 加到 {fmt_size(hi.size)} 的那部分资金,每单位要付这么多。")
            print(f"   → 最优规模在小端。放大规模不会摊薄门槛,只会抬高它。")
        else:
            print(f">> 成本形态: **摊薄型** —— 总成本 ≈ {d['fixed']:,.4f} {sym}(固定) "
                  f"+ {d['marginal_bps']:,.2f} bps × 规模(比例)")
            print(f"   固定成本(主要是 gas)被摊薄,比例成本(桥费/滑点)摊不薄。")
            if d["marginal_bps"] > 0:
                # 固定成本占比降到比例成本 10% 以下所需的规模
                need = d["fixed"] / (d["marginal_bps"] / 10_000 * 0.1)
                if need > 0:
                    print(f"   规模 ≳ {fmt_size(need)} {sym} 之后,gas 就基本不影响门槛了"
                          f"(降到比例成本的 10% 以内)。")
                print(f"   → 门槛的地板是 {d['marginal_bps']:,.2f} bps,"
                      f"再大的规模也降不下去。")

    print("-" * 78)
    print(f">> 口径声明(别自己骗自己):")
    print(f"   · 硬成本 {best.hard_bps:,.2f} bps —— 同种资产数量相减,**零价格假设**,可信。")
    print(f"   · 账外 {best.offchain_bps:,.2f} bps —— gas/未含费用原生币付,"
          f"折算用了标价,是估算。")
    lat = (f"执行延迟({best.duration}s 内价差可能消失)" if best.duration > 0
           else "执行延迟(同链,报价给 0s;但仍有区块确认和被抢跑的风险)")
    print(f"   · 还没算: {lat}、失败交易概率、资金占用。")
    print("=" * 78)
    _warn_block(results)


def _warn_block(results):
    warns = []
    for r in results:
        for w in r.warnings:
            if w not in warns:
                warns.append(w)
    tools = {r.tools for r in results if r.ok}
    if len(tools) > 1:
        warns.append(
            "不同规模走了不同的桥/DEX(" + " | ".join(sorted(tools)) + ")。"
            "跨规模比较时,差异里混进了'换了工具',不纯是'深度变化'。"
        )
    if warns:
        print("\n⚠ 注意:")
        for w in warns:
            print(f"  · {w}")


def write_csv(path, args, results, from_meta):
    """导出成第 4 节《证据记录表》的行,Week 2 直接接着填。"""
    sym = from_meta["symbol"]
    new = True
    try:
        with open(path, "r", encoding="utf-8") as f:
            new = not f.read(1)
    except FileNotFoundError:
        pass
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["时间", "链", "资产", "类型", "规模", "路径",
                        "屏幕价差bps", "真实成本bps", "净收益bps", "延迟秒",
                        "成败原因", "可复现"])
        for r in results:
            if not r.ok:
                continue
            kind = "跨链" if args.from_chain != args.to_chain else "同链"
            if args.roundtrip:
                kind += "·往返"
            w.writerow([
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                f"{args.from_chain}→{args.to_chain}",
                f"{args.from_token}→{args.to_token}",
                kind,
                # 规模不加千分位:这一列是 make_evidence.py 合并时的 key,
                # "1,000 USDC" 和 "1000 USDC" 会被当成两条不同的记录
                f"{r.size:g} {sym}",
                r.tools,
                "",                                  # 屏幕价差:你自己观察后填
                f"{r.total_bps:.2f}",
                "",                                  # 净收益 = 屏幕价差 - 真实成本
                r.duration,
                "",
                "",
            ])
    print(f"\n证据记录表已追加: {path}")


# ============================================================
# 五、入口
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="LI.FI 通用成本探针:输入任意路径,输出 token 口径的真实 bps 门槛",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--from-chain", required=True, help="源链,如 ARB / 42161")
    p.add_argument("--to-chain", required=True, help="目标链,如 BAS / 8453")
    p.add_argument("--token", help="同资产路径的简写(等于同时设置 from/to token)")
    p.add_argument("--from-token", help="源资产,如 USDC")
    p.add_argument("--to-token", help="目标资产,如 WETH")
    p.add_argument("--amounts", default="1000",
                   help="逗号分隔的规模列表,如 100,1000,10000,100000")
    p.add_argument("--roundtrip", action="store_true",
                   help="往返闭环:回到起点资产,得到零价格假设的真实成本")
    p.add_argument("--address", default=DEFAULT_ADDRESS, help="只读报价用的占位地址")
    p.add_argument("--csv", help="把结果追加进证据记录表 CSV")
    p.add_argument("--dump", help="把原始 JSON 存下来,方便自己翻字段")
    args = p.parse_args()

    if args.token:
        args.from_token = args.from_token or args.token
        args.to_token = args.to_token or args.token
    if not args.from_token:
        p.error("必须给 --token 或 --from-token")
    args.to_token = args.to_token or args.from_token

    try:
        sizes = sorted({float(x) for x in args.amounts.split(",") if x.strip()})
    except ValueError:
        p.error("--amounts 只能是逗号分隔的数字")
    if not sizes:
        p.error("--amounts 不能为空")

    try:
        from_meta = get_token(args.from_chain, args.from_token)
    except NoQuote as e:
        print(f"查不到 {args.from_chain} 上的 {args.from_token}: {e}", file=sys.stderr)
        return 1

    results, raw = [], []
    for s in sizes:
        print(f"  查询规模 {s:,.0f} {from_meta['symbol']} …", file=sys.stderr)
        try:
            r = probe_size(args, s, from_meta)
            results.append(r)
            if args.dump:
                raw.append({"size": s, "legs": [l.__dict__ for l in r.legs]})
        except NoQuote as e:
            # 报不出价不是脚本坏了,是"这条路在这个规模上不成立"
            results.append(Result(size=s, ok=False, reason=str(e)))

    render(args, results, from_meta)
    if args.csv:
        write_csv(args.csv, args, results, from_meta)
    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2, default=str)
        print(f"原始数据已存: {args.dump}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
