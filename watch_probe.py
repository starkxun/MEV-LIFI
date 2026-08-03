#!/usr/bin/env python3
"""
watch_probe.py —— 把成本探针从"手动看一眼"变成"系统持续盯"

Week 2 任务一的最后一环(持续监控)。复用两边现成的东西:
  · cost_probe.py   —— Week 1 的成本核算逻辑,直接 import,不重写
  · lib/chainkit.py —— 从 onChainListen 抽出来的轮询/断点/去重骨架

它干的事很简单:定期跑一遍成本探针,把每次结果按 JSONL 落盘,
并且在**门槛发生有意义的变化**时喊一声。

为什么值得盯:Week 1 已经发现跨资产路径的门槛是活的 ——
同一条路 1k 规模的闭环成本能从 29 bps 跳到 43 bps,因为 LI.FI 实时换路由。
一次报价只是一个采样点,连续采样才能回答"这条路平时是什么样,
什么时候会变好",而那才是 edge 判断需要的东西。

用法:
    # 跑一次(给 hermes cron 用,每次拉起一个新进程)
    python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC \\
        --amounts 1000,10000 --alert-below 20

    # 前台每 10 分钟跑一轮
    python3 watch_probe.py --from-chain ARB --to-chain BAS --token USDC \\
        --amounts 1000 --interval 600

    # 往返闭环也能盯
    python3 watch_probe.py --from-chain ARB --to-chain BAS \\
        --from-token USDC --to-token WETH --amounts 1000 --roundtrip --interval 900

输出:
    watch/<路径标识>.jsonl     每轮一行,门槛 / 路由 / 耗时 全存着
    stdout                     人类可读的一行摘要
    退出码 10                  触发了告警(方便 shell / cron 判断)
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cost_probe as cp
from lib.chainkit import (Checkpoint, append_jsonl, eprint, read_jsonl,
                          run_loop, utcnow)

WATCH_DIR = Path(__file__).parent / "watch"


def path_id(args):
    """给这条被监控的路径起个稳定的文件名。"""
    tag = f"{args.from_chain}.{args.from_token}-{args.to_chain}.{args.to_token}"
    if args.roundtrip:
        tag += "-rt"
    return tag.replace("/", "_")


def summarise(args, results):
    """从多个规模里挑出这一轮的代表值:门槛最低的那个规模。"""
    ok = [r for r in results if r.ok]
    if not ok:
        return None
    best = min(ok, key=lambda r: r.total_bps)
    return {
        "ts": utcnow(),
        "from_chain": args.from_chain, "to_chain": args.to_chain,
        "from_token": args.from_token, "to_token": args.to_token,
        "roundtrip": args.roundtrip,
        "best_size": best.size,
        "best_bps": round(best.total_bps, 2),
        "hard_bps": round(best.hard_bps, 2),
        "offchain_bps": round(best.offchain_bps, 2),
        "tools": best.tools,
        "duration_s": best.duration,
        "warnings": best.warnings,
        "all_sizes": [
            {"size": r.size, "bps": round(r.total_bps, 2), "tools": r.tools,
             "duration_s": r.duration}
            for r in ok
        ],
        "failed_sizes": [
            {"size": r.size, "reason": r.reason} for r in results if not r.ok
        ],
    }


def decide_alerts(rec, prev, args):
    """
    判断这一轮值不值得喊一声。

    三种触发,对应三种不同的现实:
      1. 门槛跌破阈值  —— 机会窗口可能开了,这是你真正在等的
      2. 门槛突变      —— 路由/深度出事了,可能是机会也可能是陷阱
      3. 路由换了      —— Week 1 学到的:成本没变不等于风险没变
                          (eco 7秒 → polymerStandard 1080秒,门槛一模一样)
    """
    alerts = []

    if args.alert_below is not None and rec["best_bps"] < args.alert_below:
        alerts.append(
            f"门槛 {rec['best_bps']:.2f} bps 跌破阈值 {args.alert_below} bps"
            f"(规模 {rec['best_size']:g})"
        )

    if prev:
        delta = rec["best_bps"] - prev["best_bps"]
        if abs(delta) >= args.alert_delta:
            alerts.append(
                f"门槛突变 {prev['best_bps']:.2f} → {rec['best_bps']:.2f} bps"
                f"({delta:+.2f})"
            )
        if rec["tools"] != prev["tools"]:
            alerts.append(
                f"路由切换 [{prev['tools']}] → [{rec['tools']}]"
                f",耗时 {prev['duration_s']}s → {rec['duration_s']}s"
                f" —— 成本没变不等于风险没变"
            )

    # 上游数据自相矛盾也要喊,别让脏数据静悄悄混进结论
    if rec["warnings"]:
        alerts.append(f"上游数据异常: {rec['warnings'][0]}")

    return alerts


def make_round(args, state):
    """构造"跑一轮"的闭包,交给 chainkit.run_loop 驱动。"""
    pid = path_id(args)
    jsonl = WATCH_DIR / f"{pid}.jsonl"
    ckpt = Checkpoint(WATCH_DIR / f".{pid}.ckpt.json")

    def one_round():
        from_meta = cp.get_token(args.from_chain, args.from_token)

        results = []
        for s in state["sizes"]:
            try:
                results.append(cp.probe_size(args, s, from_meta))
            except cp.NoQuote as e:
                # 报不出价是结论,不是故障 —— 照样记下来
                results.append(cp.Result(size=s, ok=False, reason=str(e)))

        rec = summarise(args, results)
        if rec is None:
            eprint(f"[watch:{pid}] 本轮所有规模都报不出价,记一条空观测")
            append_jsonl(jsonl, {"ts": utcnow(), "ok": False,
                                 "failed": [r.reason for r in results]})
            return

        prev = ckpt.load().get("last")
        rec["alerts"] = decide_alerts(rec, prev, args)

        append_jsonl(jsonl, rec)
        ckpt.save({"last": rec})

        sym = from_meta["symbol"]
        line = (f"[{rec['ts']}] {args.from_chain}.{args.from_token}→"
                f"{args.to_chain}.{args.to_token}"
                f"{'(往返)' if args.roundtrip else ''}  "
                f"门槛 {rec['best_bps']:.2f} bps @ {rec['best_size']:g} {sym}  "
                f"[{rec['tools']}] {rec['duration_s']}s")
        print(line, flush=True)

        for a in rec["alerts"]:
            print(f"  ⚠ {a}", flush=True)
        if rec["alerts"]:
            state["alerted"] = True

    return one_round


def main():
    p = argparse.ArgumentParser(
        description="持续监控一条路径的 token 口径成本门槛")
    p.add_argument("--from-chain", required=True)
    p.add_argument("--to-chain", required=True)
    p.add_argument("--token", help="同资产路径简写")
    p.add_argument("--from-token")
    p.add_argument("--to-token")
    p.add_argument("--amounts", default="1000")
    p.add_argument("--roundtrip", action="store_true")
    p.add_argument("--address", default=cp.DEFAULT_ADDRESS)
    p.add_argument("--interval", type=int, default=0,
                   help=">0 则每 N 秒跑一轮;默认跑一次退出(给 cron 用)")
    p.add_argument("--max-rounds", type=int, default=0, help="最多跑几轮,0=无限")
    p.add_argument("--alert-below", type=float,
                   help="门槛跌破这个 bps 就告警 —— 机会窗口")
    p.add_argument("--alert-delta", type=float, default=10.0,
                   help="门槛单轮变化超过这个 bps 就告警,默认 10")
    p.add_argument("--history", action="store_true",
                   help="只打印历史统计,不发起新请求")
    args = p.parse_args()

    if args.token:
        args.from_token = args.from_token or args.token
        args.to_token = args.to_token or args.token
    if not args.from_token:
        p.error("必须给 --token 或 --from-token")
    args.to_token = args.to_token or args.from_token

    pid = path_id(args)

    if args.history:
        rows = [r for r in read_jsonl(WATCH_DIR / f"{pid}.jsonl")
                if r.get("best_bps") is not None]
        if not rows:
            print(f"还没有 {pid} 的历史记录")
            return 0
        bps = [r["best_bps"] for r in rows]
        tools = {}
        for r in rows:
            tools[r["tools"]] = tools.get(r["tools"], 0) + 1
        print(f"路径 {pid}  共 {len(rows)} 次观测")
        print(f"  门槛 bps: 最低 {min(bps):.2f} / 中位 "
              f"{sorted(bps)[len(bps)//2]:.2f} / 最高 {max(bps):.2f}")
        print(f"  首次 {rows[0]['ts']}  末次 {rows[-1]['ts']}")
        print(f"  路由分布: " + ", ".join(f"{k} ×{v}" for k, v in
                                          sorted(tools.items(), key=lambda x: -x[1])))
        alerted = sum(1 for r in rows if r.get("alerts"))
        print(f"  触发告警 {alerted} 次")
        return 0

    try:
        sizes = sorted({float(x) for x in args.amounts.split(",") if x.strip()})
    except ValueError:
        p.error("--amounts 只能是逗号分隔的数字")

    WATCH_DIR.mkdir(exist_ok=True)
    state = {"sizes": sizes, "alerted": False}

    eprint(f"[watch:{pid}] 启动 规模={sizes} "
           f"{'循环 '+str(args.interval)+'s' if args.interval else '单次'}")

    run_loop(make_round(args, state), interval=args.interval,
             max_rounds=args.max_rounds, label=f"watch:{pid}")

    # 退出码 10 = 有告警,方便 cron / shell 里 `if ! python3 watch_probe.py ...`
    return 10 if state["alerted"] else 0


if __name__ == "__main__":
    sys.exit(main())
