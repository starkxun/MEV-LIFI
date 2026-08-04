#!/usr/bin/env python3
"""
graph.py —— The Graph 查询客户端 + **已验证**的 subgraph 注册表

为什么要有这个文件:找 subgraph ID 这件事有三个坑,每个都真实踩到过。

坑 1:**ID 不能猜。**
      网上抄来的 ID 可能早就不用了。正确做法是查 The Graph 自己的网络 subgraph
      (见下面 discover()),它是权威来源。

坑 2:**信号最高 ≠ 你要的那个。**
      Arbitrum 上信号最高的 Uniswap V3 subgraph 用的是 Messari schema
      (`liquidityPool`),和 Base 那个的标准 schema(`pool`)完全不兼容。
      同名不同 schema,不 introspect 就会写出一堆跑不通的查询。

坑 3:**第一次失败不代表不可用。**
      实测网关会瞬时抽风(空响应 / HTTP 000),重试一次就好了。
      和这个项目里其他地方一样:临时失败必须重试,不能当成结论。

注册表里的每个 ID 都经过:① 能查通 ② schema 已确认 ③ 同步无落后
④ 价格与 RPC 直读交叉验证一致。验证日期见 VERIFIED_AT。

用法:
    from lib.graph import GraphClient, SUBGRAPHS
    g = GraphClient()                       # 自动读 .env 里的 THE_GRAPH_KEY
    d = g.query("ARB", '{pool(id:"0x..."){token1Price}}')
"""

import json
import os
import time
from pathlib import Path

import requests

GATEWAY = "https://gateway.thegraph.com/api/subgraphs/id"

VERIFIED_AT = "2026-08-04"

# schema: "uniswap-v3" = 标准 Uniswap 子图(pool / token0Price / token1Price)
#         "messari"    = Messari 统一 schema(liquidityPool / …),字段完全不同
SUBGRAPHS = {
    "ARB": {
        "id": "Fo8QBLpEGfXHWkGMD3jSM4vVLk4JxvxxQD3v3U4fsrbh",
        "name": "uniswap-v3-arbitrum",
        "schema": "uniswap-v3",
        "has_hourly": True,
    },
    "BAS": {
        "id": "GqzP4Xaehti8KSfQmv3ZctFSjnSUYZ4En5NRsiTbvZpz",
        "name": "Uniswap V3 Base",
        "schema": "uniswap-v3",
        "has_hourly": True,
    },
    "ETH": {
        "id": "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV",
        "name": "Uniswap-V3 (mainnet)",
        "schema": "uniswap-v3",
    },
}

# 同名但 schema 不同的,单独记下来,免得以后又踩
KNOWN_INCOMPATIBLE = {
    "FQ6JYszEKApsBpAmiHesRsd9Ygc6mzmpNRANeVQFYoVX":
        "Uniswap V3 Arbitrum —— Messari schema(liquidityPool),不是标准 pool",
    "HyW7A86UEdYVt5b9Lrw8W2F98yKecerHKutZTRbSCX27":
        "Uniswap V3 Arbitrum —— 只有 21 个字段,**没有 poolHourDatas 等时序实体**。"
        "schema 类型对,但拉不了历史。",
    "3V7ZY6muhxaQL5qvntX1CFXJ32W7BxXZTGTwmpH5J4t3":
        "Uniswap V3 Arbitrum —— 同上,无时序实体",
}

# 教训:验 subgraph 不能只验 "schema 类型对不对",
# 还要验 "**我要的实体在不在**"。同名 subgraph 的完整度差很多:
# 21 个字段的和 41 个字段的,顶层都有 `pool`,但只有后者能拉小时数据。

# The Graph 网络自身的 subgraph,用来发现别的 subgraph
NETWORK_SUBGRAPH = "DZz4kDTdmzWLWsV373w2bSmoar3umKKH9y82SUKr5qmp"


def load_key(env_path=None):
    """先看环境变量,再看项目根的 .env。key 永远不进代码、不进 git。"""
    k = os.environ.get("THE_GRAPH_KEY")
    if k:
        return k.strip()
    p = Path(env_path) if env_path else Path(__file__).parent.parent / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("THE_GRAPH_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(
        "找不到 THE_GRAPH_KEY。去 https://thegraph.com/studio/apikeys/ 建一个,"
        "写进项目根的 .env(已在 .gitignore 里)。")


class GraphClient:
    def __init__(self, key=None, timeout=45, retries=4):
        self.key = key or load_key()
        self.timeout = timeout
        self.retries = retries
        self.s = requests.Session()

    def _post(self, subgraph_id, payload):
        url = f"{GATEWAY}/{subgraph_id}"
        headers = {"Authorization": f"Bearer {self.key}"}
        delay = 1.0
        last = None
        for i in range(self.retries):
            try:
                r = self.s.post(url, headers=headers, json=payload,
                                timeout=self.timeout)
                if r.status_code == 200 and r.text.strip():
                    d = r.json()
                    if "errors" in d:
                        # GraphQL 层的错误通常是查询写错了,重试没用,直接抛
                        raise RuntimeError(f"GraphQL 错误: {d['errors']}")
                    return d["data"]
                last = f"HTTP {r.status_code} 空响应/异常"
            except (requests.RequestException, ValueError) as e:
                last = f"{type(e).__name__}: {e}"
            # 空响应和网络错都当瞬时故障重试 —— 实测网关确实会抽
            time.sleep(delay)
            delay *= 2
        raise RuntimeError(f"查询失败({self.retries} 次): {last}")

    def query(self, chain, gql, variables=None):
        """按链名查。chain 用项目里统一的 ARB / BAS / ETH。"""
        meta = SUBGRAPHS.get(chain.upper())
        if not meta:
            raise ValueError(f"没有登记 {chain} 的 subgraph,先跑 discover() 找")
        payload = {"query": gql}
        if variables:
            payload["variables"] = variables
        return self._post(meta["id"], payload)

    def query_id(self, subgraph_id, gql, variables=None):
        payload = {"query": gql}
        if variables:
            payload["variables"] = variables
        return self._post(subgraph_id, payload)

    def health(self, chain):
        """同步健康度。用之前先查一眼 —— 落后太多的数据不能拿来下结论。"""
        d = self.query(chain, "{_meta{block{number} hasIndexingErrors}}")
        m = d["_meta"]
        return {"block": m["block"]["number"],
                "hasIndexingErrors": m["hasIndexingErrors"]}

    def discover(self, keyword, limit=200):
        """
        从 The Graph 网络 subgraph 里搜。**这是找 ID 的唯一可靠方式** ——
        比抄博客里的 ID 靠谱,因为它反映当前网络上真实存在的东西。
        """
        gql = ("{subgraphs(first:%d, where:{active:true}, "
               "orderBy:currentSignalledTokens, orderDirection:desc)"
               "{id metadata{displayName} currentVersion{subgraphDeployment"
               "{ipfsHash manifest{network}}}}}" % limit)
        d = self._post(NETWORK_SUBGRAPH, {"query": gql})
        out = []
        for s in d["subgraphs"]:
            name = (s.get("metadata") or {}).get("displayName") or "?"
            if keyword.lower() not in name.lower():
                continue
            cv = s.get("currentVersion") or {}
            dep = cv.get("subgraphDeployment") or {}
            out.append({"id": s["id"], "name": name,
                        "network": (dep.get("manifest") or {}).get("network")})
        return out

    def schema_kind(self, subgraph_id):
        """introspect 顶层字段,判断是标准 schema 还是 Messari。"""
        d = self._post(subgraph_id,
                       {"query": '{__type(name:"Query"){fields{name}}}'})
        names = {f["name"] for f in d["__type"]["fields"]}
        if "pool" in names:
            return "uniswap-v3"
        if "liquidityPool" in names:
            return "messari"
        return "unknown"


if __name__ == "__main__":
    import sys
    g = GraphClient()
    if len(sys.argv) > 1 and sys.argv[1] == "discover":
        kw = sys.argv[2] if len(sys.argv) > 2 else "uniswap"
        for r in g.discover(kw):
            print(f"{r['name'][:40]:40} {str(r['network']):14} {r['id']}")
    else:
        print(f"注册表验证于 {VERIFIED_AT}")
        for c, m in SUBGRAPHS.items():
            try:
                h = g.health(c)
                print(f"  {c}  {m['name'][:26]:26} block={h['block']:,} "
                      f"errors={h['hasIndexingErrors']}  ✓")
            except Exception as e:
                print(f"  {c}  {m['name'][:26]:26} ✗ {e}")
