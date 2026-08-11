#!/usr/bin/env python3
"""逆向选择探针 (markout) —— 不下任何单, 纯公开成交数据.

问题: bStock maker 费率 = 0, 那挂被动单做市能不能赚?

做市的收益 = 赚到的半价差 - 逆向选择成本
逆向选择成本可以直接从逐笔成交量出来:

  对每一笔成交, 假设 maker 是我, 问 N 秒后我浮亏多少.
    aggTrades 的 m 字段 = "买方是否为 maker"
      m=True  -> maker 买入,  PnL = 未来价 - 成交价
      m=False -> maker 卖出,  PnL = 成交价 - 未来价

  平均下来为负 = 你总是在错的时候成交, 那就是逆向选择成本.

未来价用 t+N 附近一小段窗口内成交的均价, 以压掉 bid-ask bounce
(直接取单笔会因为买卖交替而带系统性偏差).
"""
import json, statistics as st, sys, time, urllib.request

SPOT = 'https://api.binance.com'
HORIZONS = [1, 5, 30, 60]        # 秒
SMOOTH = 2.0                     # 未来价取 t+N ± SMOOTH 秒内成交均价
SYMS = ['NVDAB', 'QQQB', 'SPYB', 'TSLAB', 'AAPLB', 'CRCLB', 'MSTRB', 'COINB']


def req(url, tries=6):
    last = None
    for i in range(tries):
        try:
            r = urllib.request.Request(url, headers={'User-Agent': 'curl/7.81'})
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                raise RuntimeError(f'限流 {e.code}, 立刻停止')
            last = f'[{e.code}]'
        except Exception as e:
            last = f'{type(e).__name__}'
        time.sleep(min(1.0 * 2 ** i, 8))
    raise RuntimeError(last)


def future_px(trades, i, horizon):
    """t+horizon 附近 ±SMOOTH 秒内成交的均价."""
    t_target = trades[i]['T'] + horizon * 1000
    lo, hi = t_target - SMOOTH * 1000, t_target + SMOOTH * 1000
    px = [float(t['p']) for t in trades[i + 1:] if lo <= t['T'] <= hi]
    if px:
        return sum(px) / len(px)
    # 窗口内没有成交 -> 用之后第一笔(如果还在合理范围内)
    nxt = [t for t in trades[i + 1:] if t['T'] >= t_target]
    return float(nxt[0]['p']) if nxt else None


def analyze(sym):
    pair = sym + 'USDT'
    tr = req(f'{SPOT}/api/v3/aggTrades?symbol={pair}&limit=1000')
    if len(tr) < 50:
        print(f'  {sym:<7} 成交太少 (n={len(tr)}), 跳过'); return None
    span = (tr[-1]['T'] - tr[0]['T']) / 1000
    bt = req(f'{SPOT}/api/v3/ticker/bookTicker?symbol={pair}')
    bid, ask = float(bt['bidPrice']), float(bt['askPrice'])
    mid = (bid + ask) / 2
    half_spread_bp = (ask - bid) / 2 / mid * 1e4

    notional = sum(float(t['p']) * float(t['q']) for t in tr)
    out = {'sym': sym, 'n': len(tr), 'span_s': span,
           'half_spread_bp': half_spread_bp,
           'notional': notional, 'markout': {}}

    for h in HORIZONS:
        pnl = []
        for i, t in enumerate(tr):
            fp = future_px(tr, i, h)
            if fp is None:
                continue
            p = float(t['p'])
            # m=True: 买方是 maker -> maker 买入
            sign = 1 if t['m'] else -1
            pnl.append(sign * (fp - p) / p * 1e4)
        if pnl:
            out['markout'][h] = (st.median(pnl), sum(pnl) / len(pnl), len(pnl))
    return out


def main():
    print('=== bStock 做市可行性: 半价差 vs 逆向选择 ===')
    print('(纯公开成交数据, 未下任何单)\n')
    rows = []
    for s in SYMS:
        try:
            r = analyze(s)
        except RuntimeError as e:
            print(f'  {s}: {e}'); break
        if r:
            rows.append(r)
            print(f"  {s:<7} n={r['n']:<5} 覆盖 {r['span_s']/60:>6.1f} 分钟  "
                  f"成交额 ${r['notional']:>12,.0f}")
        time.sleep(0.3)

    print(f"\n{'sym':<7}{'半价差':>8}{'mo@1s':>9}{'mo@5s':>9}{'mo@30s':>9}{'mo@60s':>9}"
          f"{'净@30s':>9}  裁决")
    print('-' * 78)
    for r in rows:
        mo = {h: v[1] for h, v in r['markout'].items()}      # 用均值
        hs = r['half_spread_bp']
        net30 = hs + mo.get(30, 0)      # markout 为负即亏, 直接相加
        verdict = '🟢 正' if net30 > 0 else '🔴 负'
        print(f"{r['sym']:<7}{hs:>8.1f}"
              + ''.join(f"{mo.get(h, float('nan')):>9.1f}" for h in HORIZONS)
              + f"{net30:>9.1f}  {verdict}")

    print('\n单位 bp.')
    print('  半价差  = 你挂在盘口、成交后能赚到的毛利(理论上限)')
    print('  mo@Ns   = 逆向选择: 假设你是 maker, N 秒后的浮盈亏. 负=你总在错的时候成交')
    print('  净@30s  = 半价差 + mo@30s. 这还没扣对冲腿的费用(永续 taker)')
    print('\n⚠️ 三个必须记住的前提:')
    print('  1. 这假设你 100% 成交. 真实成交率远低于 100%, 而且你排在队尾.')
    print('  2. 半价差是理论上限 —— 真挂在盘口内侧才排得上队, 那价差更薄.')
    print('  3. 样本 = 最近 1000 笔, 覆盖时间见上. 没覆盖高波动时段.')


if __name__ == '__main__':
    main()
