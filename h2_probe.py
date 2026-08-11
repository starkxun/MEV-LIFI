#!/usr/bin/env python3
"""H2 探针: Direct Stock <-> bStock 转换套利.

关键: 必须先用官方 multiplier 归一化再比价.
实测 multiplier 最大 1.0050058 (=50bp), 比任何真实价差都大 —— 不归一化
会稳定造出一个假的 50bp "套利".

只打 api.binance.com (现货 + sapi equity), 不碰 fapi (会触发 418 IP ban).
"""
import hashlib, hmac, json, os, sys, time, urllib.parse, urllib.request

SPOT = 'https://api.binance.com'


def load_env(path='.env'):
    env = {}
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def req(url, key=None, tries=8):
    """代理链路会随机 Connection reset —— 退避重试, 但 418/429 绝不重试.

    撞一次 418 会把封禁时间往后续, 所以限流必须立刻放弃而不是退避.
    """
    last = None
    for i in range(tries):
        try:
            r = urllib.request.Request(url, headers={'User-Agent': 'curl/7.81'})
            if key:
                r.add_header('X-MBX-APIKEY', key)
            with urllib.request.urlopen(r, timeout=25) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')
            last = f'[{e.code}] {body[:150]}'
            if e.code in (418, 429):
                raise RuntimeError(last)
        except Exception as e:
            last = f'{type(e).__name__}: {e}'
        time.sleep(min(1.0 * 2 ** i, 8))
    raise RuntimeError(last)


def cached(name, fn):
    """静态数据(multiplier 表)缓存到磁盘, 少打一次是一次."""
    p = os.path.join('cache', name)
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < 3600:
        return json.load(open(p))
    v = fn()
    os.makedirs('cache', exist_ok=True)
    json.dump(v, open(p, 'w'))
    return v


def main():
    env = load_env()
    key = env['API_KEY']

    ta = cached('tokenized_assets.json', lambda: req(f'{SPOT}/sapi/v1/equity/market/tokenized-assets', key))
    items = ta if isinstance(ta, list) else ta.get('data', [])
    # underlying -> (bStock code, multiplier)
    mp = {it['underlyingEquitySymbol']: (it['assetCode'], float(it['multiplier']))
          for it in items}

    non_unit = {u: m for u, (c, m) in mp.items() if m != 1.0}
    print(f'=== multiplier != 1 的标的 ({len(non_unit)}/{len(mp)}) ===')
    for u, m in sorted(non_unit.items(), key=lambda x: -x[1]):
        print(f'  {u:<8} multiplier={m:<12} 不归一化会造成 {(m-1)*1e4:>6.1f}bp 的假价差')

    # 只测既有 Direct Stock 行情、又有现货 bStock 交易对的标的
    all_spot = {x['symbol']: x for x in req(f'{SPOT}/api/v3/ticker/bookTicker')}
    cand = sorted(u for u, (c, m) in mp.items() if c + 'USDT' in all_spot)

    # 两条腿必须成对抓 —— 一次性快照 + 顺序遍历会让最后几个标的的现货
    # 数据陈旧上百秒, 那算出来的价差是"幽灵套利", 不是机会.
    print(f'\n=== H2: Direct Stock <-> bStock ({len(cand)} 个标的, 成对抓取) ===')
    print(f"{'sym':<7}{'mult':>11}{'stk_bid':>9}{'stk_ask':>9}{'bS_bid*':>9}{'bS_ask*':>9}"
          f"{'stk_spr':>8}{'bS_spr':>8}{'股→bS':>8}{'bS→股':>8}{'skew_ms':>9}")
    print('-' * 105)
    rows = []
    for u in cand:
        code, mult = mp[u]
        try:
            q = req(f'{SPOT}/sapi/v1/equity/market/quote?symbol={u}', key)
            t_stk = time.time()
            b = req(f'{SPOT}/api/v3/ticker/bookTicker?symbol={code}USDT')
            t_bs = time.time()
        except RuntimeError as e:
            print(f'  {u}: {e}'); break
        skew = (t_bs - t_stk) * 1000
        sbid, sask = float(q['bidPrice']), float(q['askPrice'])
        # 归一化: bStock 报价 / multiplier 才是"每股 underlying"的价格
        bbid, bask = float(b['bidPrice']) / mult, float(b['askPrice']) / mult
        if min(sbid, sask, bbid, bask) <= 0:
            continue
        e1 = (bbid / sask - 1) * 1e4        # 买股票(ask) -> mint -> 卖 bStock(bid)
        e2 = (sbid / bask - 1) * 1e4        # 买 bStock(ask) -> redeem -> 卖股票(bid)
        sspr, bspr = (sask - sbid) / sask * 1e4, (bask - bbid) / bask * 1e4
        rows.append(dict(u=u, mult=mult, e1=e1, e2=e2, sspr=sspr, bspr=bspr, skew=skew))
        print(f"{u:<7}{mult:>11.8f}{sbid:>9.2f}{sask:>9.2f}{bbid:>9.3f}{bask:>9.3f}"
              f"{sspr:>8.1f}{bspr:>8.1f}{e1:>8.1f}{e2:>8.1f}{skew:>9.0f}")
        time.sleep(0.3)                     # 主动限速, 别再被 ban

    json.dump(rows, open('h2_snapshot.json', 'w'), indent=1)
    good = [r for r in rows if r['skew'] <= 1500]
    print(f'\n=== 汇总 (skew<=1500ms 的 {len(good)}/{len(rows)} 个) ===')
    best = sorted(good, key=lambda r: -max(r['e1'], r['e2']))[:8]
    for r in best:
        d = '股→bS' if r['e1'] > r['e2'] else 'bS→股'
        print(f"  {r['u']:<7}{max(r['e1'],r['e2']):>7.1f}bp  {d}  "
              f"(股 spread {r['sspr']:.1f}bp / bS spread {r['bspr']:.1f}bp, skew {r['skew']:.0f}ms)")
    pos = [r for r in good if max(r['e1'], r['e2']) > 0]
    print(f"\n  毛价差为正的: {len(pos)}/{len(good)}")
    print(f"  其中 >15bp 的: {sum(1 for r in good if max(r['e1'],r['e2'])>15)}")

    print('\n单位 bp, 毛价差, 未扣费. 成本参考:')
    print('  bStock 现货 taker 10bp / maker 0bp (实测)')
    print('  Direct Stock 手续费未知 —— 需要单独确认')
    print('  转换(mint/redeem)官方称不收 conversion fee, 但有延迟')
    print('\n⚠️ 宽 spread 的标的(如 QNT/SNXX, spread 数百 bp)算出来的"价差"是噪声,')
    print('   不是机会 —— 两边都没有真实可成交的对手盘.')


if __name__ == '__main__':
    main()
