#!/usr/bin/env python3
"""
chainkit.py —— 从 onChainListen 抽出来的"持续采集"基础设施

来源:~/onChainListen/poll_new_pools.py 的骨架,剥掉了扫池子的业务逻辑,
只留下**任何长期轮询任务都要重写一遍**的那几样东西:

  1. checkpoint  —— 断点续跑,重启不从头再来
  2. 去重         —— 同一个观测不重复记
  3. 轮询循环     —— 单次 / 循环两种模式,单次失败不拖垮整轮
  4. JSONL 追加   —— 一行一条,可以边写边被下游读
  5. 带时间戳的 stderr 日志

onChainListen 用它盯"新池子",这里用它盯"成本门槛"。业务不同,骨架一样 ——
这就是共学文档说的"把一次性观察变成可重复查询"。

注意:onChainListen 是 Python + web3.py 的 RPC 扫描工具集,
不是共学文档最初写的 "Go + PostgreSQL"。
链上读取部分(read_call / erc20_balance)需要 `pip install web3`,
只跑 LI.FI 监控的话不需要,web3 是**按需导入**的。
"""

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# 日志
# ============================================================

def eprint(*a):
    """带时间戳的 stderr 输出。stdout 留给数据,stderr 留给过程。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(ts, *a, file=sys.stderr, flush=True)


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
# checkpoint:断点续跑
# ============================================================

class Checkpoint:
    """
    一个 JSON 小文件,记住"上次跑到哪了"。

    onChainListen 里存的是 lastBlock(块高);这里存什么由调用方决定 ——
    存块高、存时间戳、存上次的门槛值都行。
    """

    def __init__(self, path):
        self.path = Path(path)

    def load(self, default=None):
        if not self.path.exists():
            return default if default is not None else {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            # 文件损坏不该让整个监控挂掉,退回默认值继续跑
            eprint(f"[ckpt] {self.path} 读取失败({e}),用默认值继续")
            return default if default is not None else {}

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再改名:防止写到一半被 Ctrl+C,留下半个坏 JSON
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


# ============================================================
# 去重:同一个观测别记两遍
# ============================================================

class SeenSet:
    """
    内存里的一个 set,可选地从已有输出文件重建 ——
    重启之后不会把之前记过的东西再记一遍。
    """

    def __init__(self, path=None, key_field=None):
        self.keys = set()
        if path and key_field:
            self._rebuild(Path(path), key_field)

    def _rebuild(self, path, key_field):
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self.keys.add(str(json.loads(line).get(key_field)))
            except ValueError:
                continue
        eprint(f"[seen] 从 {path} 恢复 {len(self.keys)} 条历史记录")

    def add_if_new(self, key):
        """新的返回 True 并记下;见过的返回 False。"""
        key = str(key)
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    def __len__(self):
        return len(self.keys)


# ============================================================
# JSONL:一行一条,边写边能被读
# ============================================================

def append_jsonl(path, record):
    """追加一条记录。用 JSONL 而不是 JSON 数组,是为了能安全地边追加边读。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


# ============================================================
# 轮询循环
# ============================================================

def run_loop(fn, interval=0, max_rounds=0, label="loop"):
    """
    把一个"跑一轮"的函数变成长期任务。

    interval=0   → 只跑一次就退出(适合 hermes cron 驱动,每次拉起一个新进程)
    interval>0   → 自己每 N 秒跑一轮(适合前台盯着看)

    关键设计(照搬 onChainListen):**单轮异常不能拖垮整个循环**。
    网络抖一下、API 限速一次,都不该让盯了三天的监控进程死掉。
    """
    rounds = 0
    while True:
        rounds += 1
        started = time.monotonic()
        try:
            fn()
        except KeyboardInterrupt:
            eprint(f"[{label}] 收到 Ctrl+C,退出")
            return
        except Exception as e:
            eprint(f"[{label}] 第 {rounds} 轮异常(跳过,继续): {type(e).__name__}: {e}")

        if interval <= 0:
            return
        if max_rounds and rounds >= max_rounds:
            eprint(f"[{label}] 达到 max_rounds={max_rounds},退出")
            return

        # 减去本轮耗时,保证是"每 N 秒一轮"而不是"每轮之间隔 N 秒"
        sleep_for = max(0.0, interval - (time.monotonic() - started))
        time.sleep(sleep_for)


# ============================================================
# 链上读取(按需 import web3)
# ============================================================

ALCHEMY_NET = {
    "base": "base-mainnet",
    "arbitrum": "arb-mainnet",
    "optimism": "opt-mainnet",
    "polygon": "polygon-mainnet",
    "ethereum": "eth-mainnet",
}


def derive_rpc(base_url, chain):
    """
    从一条 Alchemy 端点推导另一条链的端点(只换网络段,复用同一个 key)。
    直接来自 onChainListen —— 一个 key 盯多条链,不用配一堆 URL。
    """
    slug = ALCHEMY_NET.get(chain)
    if slug is None:
        raise ValueError(f"未知链 {chain},可选: {','.join(ALCHEMY_NET)}")
    if ".g.alchemy.com" not in base_url:
        raise ValueError(
            f"--rpc-url 不是 Alchemy 端点,无法为 {chain} 推导。"
            f"请直接给该链的 RPC。"
        )
    return re.sub(r"://[^/]+?\.g\.alchemy\.com",
                  f"://{slug}.g.alchemy.com", base_url, count=1)


def with_chain(path, chain):
    """把链名插进文件名:watch.jsonl → watch.base.jsonl"""
    p = Path(path)
    return str(p.with_name(f"{p.stem}.{chain}{p.suffix}"))


def get_w3(rpc_url):
    from web3 import Web3
    return Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))


def erc20_balance(w3, token, holder, block="latest"):
    """
    读某个地址持有的 ERC20 数量(原始整数,不除 decimals)。

    在这里的用途:**用链上真实储备交叉验证 LI.FI 报的滑点**。
    LI.FI 说这个规模滑点 5 bps,池子里到底有多少钱撑得住?自己去链上看一眼。
    这就是共学文档"屏幕价差 ≠ 净收益"在数据源层面的版本 ——
    报价接口也是一块屏幕,一样要复核。
    """
    from web3 import Web3
    sel = "0x70a08231"  # balanceOf(address)
    data = sel + holder[2:].lower().rjust(64, "0")
    res = w3.eth.call({"to": Web3.to_checksum_address(token), "data": data},
                      block_identifier=block)
    return int.from_bytes(bytes(res), "big") if res else 0
