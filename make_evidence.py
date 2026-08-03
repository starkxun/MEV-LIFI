#!/usr/bin/env python3
"""
make_evidence.py —— 把监控历史(watch/*.jsonl)汇总成证据记录表

Week 2 任务二。核心不是"格式转换",是**换一个统计口径**:

    watch/*.jsonl   一行 = 一次报价      → 采样
    evidence.csv    一行 = 一条路径的判断 → 观察

所以真实成本这一列填的是**中位数 + 区间**,不是某一次的值。
单点是采样,分布才是观察 —— 这是铁律 3 在表格上的落地。

三类列,必须分清:

  A. 探针能填的      时间/链/资产/类型/规模/路径/观测次数/真实成本/延迟
  B. 只有人能填的    屏幕价差、成败原因
  C. 要有历史才能填  可复现?

脚本只填 A。B 和 C 留空,**并且在重新生成时保留你已经手填的内容** ——
不然你每次跑一遍就把判断洗掉了。

用法:
    python3 make_evidence.py                  # 汇总 watch/*.jsonl → evidence.csv
    python3 make_evidence.py --fetch-screen   # 顺便抓同资产路径的标价价差
    python3 make_evidence.py --dry-run        # 只看不写
    python3 make_evidence.py --min-obs 5      # 只输出观测数 ≥5 的路径
"""

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
WATCH_DIR = ROOT / "watch"
DEFAULT_CSV = ROOT / "evidence.csv"

# 表头。前 7 列 + 真实成本 + 延迟 由脚本填;其余留给人。
HEADER = ["时间", "链", "资产", "类型", "规模", "路径", "观测次数",
          "屏幕价差bps", "真实成本bps", "净收益bps", "延迟秒",
          "成败原因", "可复现"]

# 这几列是人的判断,重新生成时必须原样保留
MANUAL_COLS = ["屏幕价差bps", "成败原因", "可复现"]

# 一行的身份:同一条路径 + 同一个规模 = 同一行
KEY_COLS = ["链", "资产", "类型", "规模"]


def norm_key(values):
    """
    key 归一化。历史上 cost_probe 写过带千分位的规模("1,000 USDC"),
    watch 写的是 "1000 USDC" —— 不归一化就会把同一条记录裂成两行。
    """
    return tuple(str(v).replace(",", "").replace(" ", "").upper()
                 for v in values)


def load_watch(paths):
    """读所有监控历史,按 (路径, 规模) 摊平成观测点。"""
    buckets = defaultdict(list)
    for p in paths:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = __import__("json").loads(line)
            except ValueError:
                continue
            if not rec.get("all_sizes"):
                continue  # 那一轮全军覆没,没有可用观测

            kind = "跨链" if rec["from_chain"] != rec["to_chain"] else "同链"
            if rec.get("roundtrip"):
                kind += "·往返"

            for s in rec["all_sizes"]:
                key = (
                    f"{rec['from_chain']}→{rec['to_chain']}",
                    f"{rec['from_token']}→{rec['to_token']}",
                    kind,
                    f"{s['size']:g}",
                )
                buckets[key].append({
                    "ts": rec["ts"],
                    "bps": s["bps"],
                    "tools": s["tools"],
                    "duration": s.get("duration_s", 0),
                    "warned": bool(rec.get("warnings")),
                })
    return buckets


def summarise(key, obs):
    """把一组观测压成一行。成本给中位数 + 区间,不给单点。"""
    bps = sorted(o["bps"] for o in obs)
    med = statistics.median(bps)
    lo, hi = bps[0], bps[-1]

    # 成本用中位数 + 区间。只有一次观测时就老实写一个数,
    # 但「观测次数」列会诚实地写 1 —— 读表的人自己知道该信几分。
    cost = f"{med:.2f}" if lo == hi else f"{med:.2f} [{lo:.2f}~{hi:.2f}]"

    tools = defaultdict(int)
    for o in obs:
        tools[o["tools"]] += 1
    tool_str = ", ".join(f"{k}×{v}" for k, v in
                         sorted(tools.items(), key=lambda x: -x[1]))

    ts = sorted(o["ts"] for o in obs)
    when = ts[0] if len(set(ts)) == 1 else f"{ts[0]} ~ {ts[-1]}"

    dur = statistics.median([o["duration"] for o in obs])

    return {
        "时间": when,
        "链": key[0], "资产": key[1], "类型": key[2], "规模": f"{key[3]} {key[1].split('→')[0]}",
        "路径": tool_str,
        "观测次数": len(obs),
        "真实成本bps": cost,
        "延迟秒": f"{dur:g}",
        "_median": med,
        "_spread": hi - lo,
        "_warned": sum(1 for o in obs if o["warned"]),
    }


def load_existing(path):
    """
    读回已有表,返回 (人工列, 整行原文)。

    两件事都要做:
      · 人工列 —— 重新生成时盖回去,不能把判断洗掉
      · 整行原文 —— 有些行**根本不来自监控**(手工记的一次性观察、
        Hermes 记的口径校准),它们在 watch/*.jsonl 里没有对应桶。
        这类"孤儿行"必须原样留下,否则跑一次脚本就把证据删了。
    """
    manual, raw = {}, {}
    if not path.exists():
        return manual, raw
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = norm_key(row.get(c, "") for c in KEY_COLS)
            raw[key] = row
            kept = {c: row.get(c, "") for c in MANUAL_COLS if row.get(c)}
            if kept:
                manual[key] = kept
    return manual, raw


def fetch_screen_spread(chains, token):
    """
    同资产跨链路径的「屏幕价差」:两条链上同一个 token 的标价之差。

    注意这是**标价**不是可成交价差 —— 正因为它是屏幕上的数字,
    才叫「屏幕价差」。这一列存在的意义就是拿它去对撞真实成本。
    """
    import requests
    px = {}
    for c in chains:
        r = requests.get("https://li.quest/v1/token",
                         params={"chain": c, "token": token}, timeout=30)
        r.raise_for_status()
        px[c] = float(r.json()["priceUSD"])
    src, dst = chains
    return (px[dst] - px[src]) / px[src] * 10_000


def main():
    p = argparse.ArgumentParser(description="监控历史 → 证据记录表")
    p.add_argument("--watch-dir", default=str(WATCH_DIR))
    p.add_argument("--out", default=str(DEFAULT_CSV))
    p.add_argument("--min-obs", type=int, default=1, help="观测数下限")
    p.add_argument("--fetch-screen", action="store_true",
                   help="为同资产路径抓一次标价价差(仅作参考,会标注)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    files = sorted(Path(args.watch_dir).glob("*.jsonl"))
    if not files:
        print(f"{args.watch_dir} 里没有监控历史。先跑 watch_probe.py 攒数据。",
              file=sys.stderr)
        return 1

    buckets = load_watch(files)
    if not buckets:
        print("监控历史里没有可用观测。", file=sys.stderr)
        return 1

    out_path = Path(args.out)
    manual, existing_raw = load_existing(out_path)
    touched = set()

    rows, notes = [], []
    for key in sorted(buckets):
        obs = buckets[key]
        if len(obs) < args.min_obs:
            continue
        s = summarise(key, obs)

        row = {c: "" for c in HEADER}
        row.update({c: s[c] for c in HEADER if c in s})

        # 把人手填的内容盖回去 —— 重新生成不能洗掉判断
        mkey = norm_key(s[c] for c in KEY_COLS)
        touched.add(mkey)
        row.update(manual.get(mkey, {}))

        # 屏幕价差:能抓就抓,抓到了才算得出净收益
        if args.fetch_screen and not row["屏幕价差bps"]:
            frm, to = s["资产"].split("→")
            if frm == to:
                try:
                    sp = fetch_screen_spread(s["链"].split("→"), frm)
                    row["屏幕价差bps"] = f"{sp:+.2f}(标价)"
                except Exception as e:
                    notes.append(f"{s['链']} {frm} 标价抓取失败: {e}")

        # 净收益 = 屏幕价差 − 真实成本(中位)。只有屏幕价差有值时才算。
        if row["屏幕价差bps"] and not row["净收益bps"]:
            try:
                sp = float(row["屏幕价差bps"].split("(")[0])
                row["净收益bps"] = f"{sp - s['_median']:+.2f}"
            except ValueError:
                pass

        rows.append(row)

        # 判定建议(只打印,不写进表 —— 判断是你的活)
        n, spread = s["观测次数"], s["_spread"]
        if n < 3:
            notes.append(f"[{key[0]} {key[1]} {key[3]}] 只有 {n} 次观测,"
                         f"「可复现」先别填 —— 样本不够")
        else:
            stab = "极稳" if spread < 1 else ("较稳" if spread < 5 else "波动大")
            hint = (f"[{key[0]} {key[1]} {key[3]}] {n} 次观测,"
                    f"成本极差 {spread:.2f} bps({stab})")
            if row["净收益bps"]:
                sign = "负" if float(row["净收益bps"]) < 0 else "正"
                hint += f",净收益为{sign} → 可考虑填「可复现地{'不成立' if sign=='负' else '成立'}」"
            else:
                hint += ",但缺屏幕价差,净收益未知 → 还判不了"
            notes.append(hint)
        if s["_warned"]:
            notes.append(f"  ⚠ 其中 {s['_warned']} 次伴随上游数据异常,该行结论打折扣")

    # 孤儿行:已有表里存在,但监控历史里没有对应桶的 —— 原样保留在末尾
    orphans = [r for k, r in existing_raw.items() if k not in touched]
    for o in orphans:
        rows.append({c: o.get(c, "") for c in HEADER})
    if orphans:
        notes.append(f"保留了 {len(orphans)} 行非监控来源的记录"
                     f"(手工观察 / 口径校准等),它们不来自 watch/,不会被覆盖")

    if args.dry_run:
        w = csv.DictWriter(sys.stdout, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    else:
        with out_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=HEADER)
            w.writeheader()
            w.writerows(rows)
        print(f"已写入 {out_path}  共 {len(rows)} 行"
              f"(保留了 {len(manual)} 行的人工判断)")

    if notes:
        print("\n判定建议(只是建议,填不填你说了算):", file=sys.stderr)
        for n in notes:
            print(f"  · {n}", file=sys.stderr)

    blanks = sum(1 for r in rows if not r["屏幕价差bps"])
    if blanks:
        print(f"\n还有 {blanks} 行缺「屏幕价差」—— 那一列必须靠观测,"
              f"脚本不该替你猜。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
