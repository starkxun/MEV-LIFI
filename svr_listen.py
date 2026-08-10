#!/usr/bin/env python3
"""
svr_listen.py —— 只读订阅 Chainlink SVR 拍卖推送

目的:在**不部署合约、不押金、不出价**的前提下,验证三件事
  1. 这个端点让不让匿名连接?(文档说 permissionless,但没说 WS 要不要鉴权)
  2. 推送里到底有什么字段?(文档说带 aggregator 地址 + median price)
  3. 推送比链上早多少?(这决定了估值有多少时间可算)

⚠️ 本脚本**在代码层面就不可能出价** —— 没有实现 solver_submitSolverOperation,
   也不读私钥。它只发 solver_subscribe,然后收。

    python3 svr_listen.py --seconds 300
    python3 svr_listen.py --seconds 600 --out shadow/svr_feed.jsonl

端点来自 Chainlink 官方文档 SVR Searcher Onboarding: Atlas。
"""

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
ENDPOINT = "wss://svr-bid-endpoint.chain.link/ws/solver"

# 文档声称推送里会有的东西,用来对照实际收到的
DOCUMENTED_HINTS = ["auctionId", "chainId", "aggregator", "medianPrice", "userOperation"]


def flatten_keys(obj, prefix="", out=None):
    """把嵌套 JSON 的键路径拍平,用来看推送到底长什么样。"""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.add(p)
            flatten_keys(v, p, out)
    elif isinstance(obj, list) and obj:
        flatten_keys(obj[0], prefix + "[]", out)
    return out


async def run(args):
    import websockets

    out_f = None
    if args.out:
        p = ROOT / args.out
        p.parent.mkdir(parents=True, exist_ok=True)
        out_f = open(p, "a", encoding="utf-8")

    print(f"连接 {ENDPOINT}")
    print(f"订阅 solver_subscribe([\"userOperations\"])")
    print(f"监听 {args.seconds}s,只收不发。Ctrl-C 可提前停。\n")

    t0 = time.time()
    frames = 0
    keyseen = Counter()
    methods = Counter()
    first_payload = None

    try:
        async with websockets.connect(
            ENDPOINT,
            open_timeout=20,
            ping_interval=20,
            max_size=8 * 1024 * 1024,
        ) as ws:
            print(f"[{time.strftime('%H:%M:%S')}] ✅ WebSocket 已建立(未鉴权)")

            sub = {"jsonrpc": "2.0", "id": 1,
                   "method": "solver_subscribe", "params": ["userOperations"]}
            await ws.send(json.dumps(sub))
            print(f"[{time.strftime('%H:%M:%S')}] → 已发送订阅请求\n")

            while time.time() - t0 < args.seconds:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(1.0, args.seconds - (time.time() - t0)))
                except asyncio.TimeoutError:
                    break

                frames += 1
                ts = time.time()
                stamp = time.strftime("%H:%M:%S")

                try:
                    msg = json.loads(raw)
                except Exception:
                    print(f"[{stamp}] 非 JSON 帧 ({len(raw)} 字节): {str(raw)[:160]}")
                    continue

                if out_f:
                    out_f.write(json.dumps({"t": ts, "msg": msg}, ensure_ascii=False) + "\n")
                    out_f.flush()

                # 订阅响应 / 错误
                if "error" in msg:
                    print(f"[{stamp}] 🔴 服务端返回错误:")
                    print(f"          {json.dumps(msg['error'], ensure_ascii=False)[:400]}")
                    print(f"\n  → 这就是答案的一部分:**匿名订阅被拒**。")
                    print(f"    说明 permissionless 指的是「不用申请」,")
                    print(f"    但连接本身可能要求已注册/已押金的 solver 身份。")
                    break

                if msg.get("id") == 1:
                    print(f"[{stamp}] ✅ 订阅被接受,返回: "
                          f"{json.dumps(msg.get('result'), ensure_ascii=False)[:200]}")
                    print(f"          → 匿名即可订阅,等推送…\n")
                    continue

                m = msg.get("method", "(无 method)")
                methods[m] += 1
                params = msg.get("params", {})
                payload = params.get("result", params) if isinstance(params, dict) else params
                if first_payload is None:
                    first_payload = payload
                for k in flatten_keys(payload):
                    keyseen[k] += 1

                # 挑出最关心的字段
                def dig(d, *names):
                    if not isinstance(d, dict):
                        return None
                    for n in names:
                        if n in d:
                            return d[n]
                    for v in d.values():
                        r = dig(v, *names)
                        if r is not None:
                            return r
                    return None

                agg = dig(payload, "aggregator", "aggregatorAddress", "feed")
                px = dig(payload, "medianPrice", "median", "price")
                cid = dig(payload, "chainId", "chain_id")
                aid = dig(payload, "auctionId", "auction_id", "id")
                bits = []
                if aid is not None: bits.append(f"auction={str(aid)[:18]}")
                if cid is not None: bits.append(f"chain={cid}")
                if agg is not None: bits.append(f"aggregator={str(agg)[:20]}")
                if px is not None: bits.append(f"medianPrice={px}")
                print(f"[{stamp}] 推送 #{frames}  {m}  " + ("  ".join(bits) if bits
                      else f"({len(json.dumps(payload))} 字节)"))

    except Exception as e:
        print(f"\n🔴 连接失败: {type(e).__name__}: {str(e)[:300]}")
        print("\n  可能原因:")
        print("    · 端点要求鉴权(header / 签名 / 已注册 solver)")
        print("    · 路径或子协议不对")
        print("    · Cloudflare 拦了非常规客户端")
        print("  → 无论哪种,这都是有价值的结论:**光有文档跑不通,还差一步。**")
        return 2
    finally:
        if out_f:
            out_f.close()

    # ── 小结
    print(f"\n{'='*62}")
    print(f"=== {time.time()-t0:.0f}s 内收到 {frames} 帧 ===")
    if methods:
        print(f"\n方法分布:")
        for m, n in methods.most_common():
            print(f"  {m}  ×{n}")
    if keyseen:
        print(f"\n推送里出现过的字段(共 {len(keyseen)} 个):")
        for k, n in sorted(keyseen.items()):
            print(f"  {k:<44} ×{n}")
        print(f"\n对照文档声称的字段:")
        allk = " ".join(keyseen).lower()
        for d in DOCUMENTED_HINTS:
            print(f"  {d:<16} {'✅ 有' if d.lower() in allk else '❌ 没看到'}")
    if first_payload is not None:
        print(f"\n第一条推送的原文(截断 1200 字符):")
        print(json.dumps(first_payload, ensure_ascii=False, indent=1)[:1200])
    if frames == 0:
        print("\n  一帧都没收到。可能是:")
        print("    · 订阅成功但这段时间没有价格更新(SVR ETH/USD 约 90s 一次,")
        print("      但拍卖只在有 Aave 相关机会时才推)")
        print("    · 需要先押金才会给你推")
        print("  → 建议加长 --seconds 再试一次,排除「刚好没事发生」。")
    if args.out:
        print(f"\n原始帧已存 {args.out}")
    return 0


def main():
    p = argparse.ArgumentParser(description="只读订阅 SVR 拍卖推送(永不出价)")
    p.add_argument("--seconds", type=int, default=300, help="监听多久")
    p.add_argument("--out", default="shadow/svr_feed.jsonl", help="原始帧落盘位置")
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
