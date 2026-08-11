#!/usr/bin/env python3
"""Probe A: 给 BSC 上的 bStock 池子称重.

不读 sqrtPriceX96 推出来的瞬时中价 —— 那是文档 §10.1 明确禁止的.
用 PancakeSwap V3 QuoterV2 的 quoteExactInputSingle 逐档模拟, 拿真实
effective price 和 price impact.
"""
import json, urllib.request, sys
from decimal import Decimal, getcontext

getcontext().prec = 40

RPC = 'https://bsc-dataseed.bnbchain.org'
QUOTER = '0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997'  # PancakeSwap V3 QuoterV2
USDT = '0x55d398326f99059fF775485246999027B3197955'   # BSC-USD, 18 decimals

POOLS = {
    'QQQB':  ('0xe531fcb1F5a195de7608B9F4f9518544C2cdB693', '0x205812CdBed920aFf76C6580abD681a46D11efc7'),
    'NVDAB': ('0x8FB4243b553aC29BA088aCf00B9B7dA24bD6690C', '0x02Fca66C1D1aFB4E2A7884261eB00F63598a7436'),
    'SPYB':  ('0x7aA6d92Fc369A8C1EDc631A3aAc44eFB0808ddbF', '0x7138b48df7D98D7e3cc221BfE7192D0a178182D8'),
    'AAPLB': ('0xe9b9998B2EC5430D2246c7f1F8D9f298c97D7365', '0x431a3BEE82E2ca41e49895CbECE5bB0F76A89b7A'),
    'TSLAB': ('0xB0f5E5400E8F0F7C242F2b7740C004f020579c41', '0x5b1910eAaD6450E50f816082Aa078C41F10C292f'),
    'CRCLB': ('0x29967c54c5Bf12E8158c8894376064b30ebaB297', '0x80f3D493EBCe97e343c53D29a137942416B4ffC0'),
}
NOTIONALS = [100, 500, 1000, 2500, 5000, 10000, 25000]

_id = [0]


def rpc(method, params):
    _id[0] += 1
    req = urllib.request.Request(
        RPC, data=json.dumps({'jsonrpc': '2.0', 'method': method,
                              'params': params, 'id': _id[0]}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if 'error' in d:
        raise RuntimeError(d['error'])
    return d['result']


def call(to, data):
    return rpc('eth_call', [{'to': to, 'data': data}, 'latest'])


def w(x):        return hex(x)[2:].rjust(64, '0')
def addr(a):     return a.lower().replace('0x', '').rjust(64, '0')


def erc20_dec(t):
    return int(call(t, '0x313ce567'), 16)          # decimals()


def pool_fee(p):
    return int(call(p, '0xddca3f43'), 16)          # fee()


def quote(tin, tout, amount_in, fee):
    """QuoterV2.quoteExactInputSingle((tokenIn,tokenOut,amountIn,fee,sqrtLimit))"""
    # 该函数只有一个 struct 参数, 且全是静态类型 -> 直接按顺序平铺编码
    data = ('0xc6a5026a' + addr(tin) + addr(tout) + w(amount_in) + w(fee) + w(0))
    out = call(QUOTER, data)
    return int(out[2:66], 16)                      # amountOut


def main():
    print(f"{'tok':<7}{'fee':>7}{'dir':<14}{'notional':>10}"
          f"{'eff_price':>12}{'impact_bp':>11}")
    print('-' * 61)
    results = {}
    for sym, (pool, token) in POOLS.items():
        try:
            fee = pool_fee(pool)
            dec = erc20_dec(token)
        except Exception as e:
            print(f'{sym}: pool/token 读取失败 {e}')
            continue
        base = None
        row = {'fee': fee, 'buy': {}, 'sell': {}}
        for n in NOTIONALS:
            try:
                # USDT -> bStock (在 DEX 上买)
                amt_in = int(Decimal(n) * Decimal(10) ** 18)
                out = quote(USDT, token, amt_in, fee)
                if out == 0:
                    continue
                px_buy = Decimal(n) / (Decimal(out) / Decimal(10) ** dec)
                if base is None:
                    base = px_buy
                imp = (px_buy / base - 1) * 10000
                row['buy'][n] = (float(px_buy), float(imp))
                print(f"{sym:<7}{fee:>7} {'USDT->'+sym:<14}{n:>10,}"
                      f"{float(px_buy):>12.4f}{float(imp):>11.1f}")
            except Exception as e:
                print(f"{sym:<7}{fee:>7} {'USDT->'+sym:<14}{n:>10,}   ERR {type(e).__name__}")
        # 反向: bStock -> USDT
        base_s = None
        for n in NOTIONALS:
            try:
                if not row['buy']:
                    break
                px_ref = list(row['buy'].values())[0][0]
                tok_in = int(Decimal(n) / Decimal(px_ref) * Decimal(10) ** dec)
                out = quote(token, USDT, tok_in, fee)
                if out == 0:
                    continue
                px_sell = (Decimal(out) / Decimal(10) ** 18) / (Decimal(tok_in) / Decimal(10) ** dec)
                if base_s is None:
                    base_s = px_sell
                imp = (1 - px_sell / base_s) * 10000
                row['sell'][n] = (float(px_sell), float(imp))
                print(f"{sym:<7}{fee:>7} {sym+'->USDT':<14}{n:>10,}"
                      f"{float(px_sell):>12.4f}{float(imp):>11.1f}")
            except Exception as e:
                print(f"{sym:<7}{fee:>7} {sym+'->USDT':<14}{n:>10,}   ERR {type(e).__name__}")
        results[sym] = row
        print()
    json.dump(results, open('probe_a_result.json', 'w'), indent=1)
    print('-> probe_a_result.json')


if __name__ == '__main__':
    main()
