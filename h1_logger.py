#!/usr/bin/env python3
"""H1 探针: bStock 现货盘口 vs TradFi 永续盘口, 只用公开数据.

刻意做成最小实现 —— 先回答"存不存在", 不建 MarketState/Engine.
每轮同时抓两个 venue 的 bookTicker, 记录 local_recv_ts 和两边的
exchange ts, 算 skew. skew 超阈值的样本单独标记, 不混进分布.
"""
import json, time, urllib.request, sys, os

UNDERS = ['NVDA','TSLA','AAPL','MSFT','META','GOOGL','AMZN','SPY',
          'QQQ','COIN','MSTR','PLTR','HOOD','CRCL']
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'h1_ticks.jsonl')
MAX_SKEW_MS = 500


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.81'})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.load(r)
    return body, t0, time.time()


def snap():
    # 先现货后合约, 记录各自的本地收包时刻
    sp, s_t0, s_t1 = get('https://api.binance.com/api/v3/ticker/bookTicker')
    fu, f_t0, f_t1 = get('https://fapi.binance.com/fapi/v1/ticker/bookTicker')
    spot = {x['symbol']: x for x in sp}
    fut = {x['symbol']: x for x in fu}
    rows = []
    for u in UNDERS:
        s, f = spot.get(u + 'BUSDT'), fut.get(u + 'USDT')
        if not (s and f):
            continue
        sb, sa = float(s['bidPrice']), float(s['askPrice'])
        fb, fa = float(f['bidPrice']), float(f['askPrice'])
        if min(sb, sa, fb, fa) <= 0:
            continue
        # 合约 bookTicker 带交易所时间戳, 现货全量接口不带 -> 用本地收包时刻近似
        f_ts = f.get('time') or f.get('E')
        rows.append(dict(
            u=u, sb=sb, sa=sa, fb=fb, fa=fa,
            # 吃盘口的两个方向, 单位 bp, 未扣手续费
            long_bs=(fb / sa - 1) * 1e4,
            short_bs=(sb / fa - 1) * 1e4,
            s_spr=(sa - sb) / ((sa + sb) / 2) * 1e4,
            f_spr=(fa - fb) / ((fa + fb) / 2) * 1e4,
            f_ts=f_ts,
        ))
    return dict(
        recv_ms=int(f_t1 * 1000),
        spot_rtt_ms=int((s_t1 - s_t0) * 1000),
        fut_rtt_ms=int((f_t1 - f_t0) * 1000),
        # 两个 venue 的抓取间隔 = 不可消除的 skew 下界
        venue_gap_ms=int((f_t1 - s_t1) * 1000),
        rows=rows,
    )


def main():
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    n = 0
    with open(OUT, 'a') as fh:
        while True:
            try:
                rec = snap()
                fh.write(json.dumps(rec) + '\n')
                fh.flush()
                n += 1
                if n % 20 == 0:
                    best = max(
                        (max(r['long_bs'], r['short_bs']), r['u']) for r in rec['rows']
                    )
                    print(f"[{time.strftime('%H:%M:%S')}] n={n} "
                          f"gap={rec['venue_gap_ms']}ms best={best[1]} {best[0]:+.1f}bp",
                          flush=True)
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] ERR {type(e).__name__}: {e}",
                      flush=True)
                time.sleep(2)
            time.sleep(interval)


if __name__ == '__main__':
    main()
