#!/usr/bin/env python3
"""Binance Equity 探针: 资格核实 + 真实费率 + Direct Stock 盘口 + 合约地址校验.

设计原则(沿用 P0 的教训):
  - 只读, 不下单. 全程不碰任何 trading endpoint.
  - 绝不打印 key/secret 的值, 只报成功/失败.
  - 官方返回的合约地址要和链上实测用的地址对账, 不能"看着像就是".

用法:
    python3 equity_probe.py            # 全跑
    python3 equity_probe.py fees       # 只查费率(最快能翻盘 H1 的那个)
"""
import hashlib, hmac, json, os, sys, time, urllib.parse, urllib.request

SPOT = 'https://api.binance.com'
FAPI = 'https://fapi.binance.com'

# Probe A 里实际用来做链上报价的地址 —— 要和官方返回的对账
ONCHAIN_USED = {
    'QQQB':  '0x205812CdBed920aFf76C6580abD681a46D11efc7',
    'NVDAB': '0x02Fca66C1D1aFB4E2A7884261eB00F63598a7436',
    'SPYB':  '0x7138b48df7D98D7e3cc221BfE7192D0a178182D8',
    'AAPLB': '0x431a3BEE82E2ca41e49895CbECE5bB0F76A89b7A',
    'TSLAB': '0x5b1910eAaD6450E50f816082Aa078C41F10C292f',
    'CRCLB': '0x80f3D493EBCe97e343c53D29a137942416B4ffC0',
}


def load_env(path='.env'):
    env = {}
    if not os.path.exists(path):
        return env
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def req(url, key=None, timeout=25):
    r = urllib.request.Request(url, headers={'User-Agent': 'curl/7.81'})
    if key:
        r.add_header('X-MBX-APIKEY', key)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.getcode(), json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', 'replace')
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {'raw': body[:300]}
    except Exception as e:
        return None, {'err': f'{type(e).__name__}: {e}'}


_offset = [None]


def clock_offset():
    """本机时钟不可信 —— 用 serverTime 算偏移.

    实测本机快 ~4.8s, 直接用 time.time() 会稳定触发 -1021.
    取 rtt 中点做估计, 多测几次取中位数压掉抖动.
    """
    if _offset[0] is not None:
        return _offset[0]
    samples = []
    for _ in range(5):
        try:
            t0 = time.time()
            _, b = req(f'{SPOT}/api/v3/time')
            t1 = time.time()
            samples.append(b['serverTime'] - int((t0 + t1) / 2 * 1000))
        except Exception:
            pass
    samples.sort()
    _offset[0] = samples[len(samples) // 2] if samples else 0
    print(f'  [时钟] 本机相对服务器偏移 {-_offset[0]:+d} ms, 已校准')
    return _offset[0]


def signed(base, path, key, secret, params=None):
    p = dict(params or {})
    p['timestamp'] = int(time.time() * 1000) + clock_offset()
    p['recvWindow'] = 20000
    qs = urllib.parse.urlencode(p)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return req(f'{base}{path}?{qs}&signature={sig}', key)


def identify(env):
    """判断哪个变量是 API key, 哪个是 secret. 不打印任何值."""
    cands = {k: v for k, v in env.items()
             if k in ('API_KEY', 'KEY', 'BINANCE_API_KEY', 'BINANCE_API_SECRET')
             and v}
    if not cands:
        print('🔴 .env 里没找到 API_KEY / KEY'); sys.exit(1)
    print(f'  .env 中候选变量: {", ".join(sorted(cands))}')
    api_key = None
    for name, val in cands.items():
        code, body = req(f'{SPOT}/sapi/v1/equity/market/exchangeInfo', val)
        c = body.get('code')
        if c == -2008:                       # key ID 不存在 -> 这不是 API key
            print(f'  {name:<22} ✗ 不是 API key (-2008)')
        elif c == -2014:                     # 格式不对 -> 多半是 secret
            print(f'  {name:<22} ✗ 格式非 API key (-2014, 多半是 secret)')
        elif c in (-2015, -1022) or code == 200:
            print(f'  {name:<22} ✅ 是 API key (服务端认了这个 ID)')
            api_key = name
        else:
            print(f'  {name:<22} ? code={c} msg={body.get("msg")}')
    if not api_key:
        return None, None
    secret = next((k for k in cands if k != api_key), None)
    print(f'  -> API key = {api_key} / secret = {secret}')
    return api_key, secret


def check_equity(key):
    print('\n=== A. Equity 接口资格 (只读) ===')
    for path in ['/sapi/v1/equity/market/exchangeInfo',
                 '/sapi/v1/equity/market/tokenized-assets',
                 '/sapi/v1/equity/market/quote?symbol=NVDA']:
        code, body = req(f'{SPOT}{path}', key)
        if code == 200:
            n = len(body) if isinstance(body, list) else len(json.dumps(body))
            print(f'  ✅ [{code}] {path}  (载荷 {n})')
        else:
            print(f'  🔴 [{code}] {path}  code={body.get("code")} msg={body.get("msg")}')
    return


def check_fees(key, secret):
    """最要紧的一步: 真实费率决定 H1 是不是已经死了."""
    print('\n=== B. 真实费率 (决定 H1 生死) ===')
    code, body = signed(SPOT, '/sapi/v1/asset/tradeFee', key, secret)
    if code == 200 and isinstance(body, list):
        fees = {x['symbol']: x for x in body}
        print(f"  {'symbol':<14}{'maker_bp':>10}{'taker_bp':>10}")
        for s in ['NVDABUSDT', 'QQQBUSDT', 'SPYBUSDT', 'TSLABUSDT',
                  'AAPLBUSDT', 'CRCLBUSDT', 'BTCUSDT']:
            f = fees.get(s)
            if f:
                print(f"  {s:<14}{float(f['makerCommission'])*1e4:>10.2f}"
                      f"{float(f['takerCommission'])*1e4:>10.2f}")
    else:
        print(f'  🔴 现货费率 [{code}] {body.get("code")} {body.get("msg")}')

    for sym in ['NVDAUSDT', 'QQQUSDT', 'SPYUSDT']:
        code, body = signed(FAPI, '/fapi/v1/commissionRate', key, secret,
                            {'symbol': sym})
        if code == 200:
            print(f"  永续 {sym:<10} maker {float(body['makerCommissionRate'])*1e4:>6.2f}bp"
                  f"  taker {float(body['takerCommissionRate'])*1e4:>6.2f}bp")
        else:
            print(f'  🔴 永续 {sym} [{code}] {body.get("code")} {body.get("msg")}')


def check_tokenized(key):
    print('\n=== C. 官方 tokenized-assets vs 我链上用的地址 ===')
    code, body = req(f'{SPOT}/sapi/v1/equity/market/tokenized-assets', key)
    if code != 200:
        print(f'  🔴 [{code}] {body.get("code")} {body.get("msg")}')
        return
    items = body if isinstance(body, list) else body.get('data', [])
    print(f'  官方返回 {len(items)} 个 tokenized asset')
    blob = json.dumps(items).lower()
    for sym, ad in ONCHAIN_USED.items():
        hit = ad.lower() in blob
        print(f"  {sym:<7} {ad}  {'✅ 官方数据中出现' if hit else '⚠️ 未在官方返回中找到'}")
    print('\n  原始样本(前 2 条, 用于确认字段名/multiplier):')
    print('  ' + json.dumps(items[:2], ensure_ascii=False)[:600])


def main():
    env = load_env()
    print('=== 0. 识别 key (不打印任何值) ===')
    kname, sname = identify(env)
    if not kname:
        print('\n🔴 没有一个变量被服务端认成 API key.')
        print('   常见原因: key 还没生效 / IP 白名单没放行当前出口 IP')
        sys.exit(1)
    key, secret = env[kname], env.get(sname or '', '')

    only = sys.argv[1] if len(sys.argv) > 1 else None
    if only in (None, 'equity'):
        check_equity(key)
    if only in (None, 'fees'):
        if secret:
            check_fees(key, secret)
        else:
            print('\n⚠️ 没识别出 secret, 跳过费率(需要签名)')
    if only in (None, 'tokenized'):
        check_tokenized(key)


if __name__ == '__main__':
    main()
