
### 准备工作（先做这个）

1. 打开浏览器，或者 postman
2. 基础地址统一是：
   ```
   https://li.quest/v1/
   ```

---

### 按顺序手动打一遍（推荐顺序）

#### 1. `/chains` —— 先看支持哪些链

**作用**：返回 LI.FI 目前支持的所有链。

**直接在浏览器打开**：
```
https://li.quest/v1/chains
```

**你要关注的数据**：
- `id`：链 ID（数字，比如 42161 = Arbitrum）
- `key`：简写（比如 `arb`、`bas`、`son`）
- `name`：全名
- `coin`：原生币符号
- `nativeToken`：原生币详细信息（包括当前大概价格）

---

#### 2. `/tokens` —— 看有哪些代币

**作用**：返回支持的代币列表（可以按链过滤）。

**推荐请求**：
```
https://li.quest/v1/tokens?chains=ARB,BAS
```
（先只看 Arbitrum 和 Base，数据量小一点）

**你要关注的数据**：
- 每个代币的 `address`
- `symbol`（USDC、USDT、WETH 等）
- `decimals`（精度，USDC 通常是 6，ETH 是 18）
- `chainId`
- `priceUSD`（大概价格）
- `logoURI`

**重点理解**：后面所有报价都要用到正确的 `address` 和 `decimals`。

---

#### 3. `/tools` —— 有哪些桥和交易所可用

**作用**：列出当前可用的桥（bridges）和交易所（exchanges）。

**请求**：
```
https://li.quest/v1/tools
```

**你要关注的数据**：
- 桥的名字（比如 stargate、across、hop、polymer 等）
- 交易所的名字（uniswap、1inch、sushiswap 等）
- 每个工具支持的链

这个端点帮你知道“LI.FI 背后到底集成了哪些工具”。

---

#### 4. `/connections` —— 两条链之间能不能通、有哪些代币能转

**作用**：查询从 A 链到 B 链，有哪些代币是可以互相转的。

**示例请求**（Arbitrum → Base）：
```
https://li.quest/v1/connections?fromChain=42161&toChain=8453
```

**你要关注的数据**：
- `fromChainId` / `toChainId`
- `fromTokens` 和 `toTokens` 列表（哪些代币有连接）

这个端点用来快速判断“这条链对之间有没有路”。

---

#### 5. `/quote` —— 最核心的报价接口（单条最优路径）

**作用**：给你一条（通常是当前最优的）完整路径报价，包含预计能收到多少、要用哪个桥/DEX、预估费用等。

**最简单的测试例子**（Arbitrum USDC → Base USDC，转 100 USDC）：

```
https://li.quest/v1/quote?fromChain=42161&toChain=8453&fromToken=USDC&toToken=USDC&fromAmount=100000000&fromAddress=0x0000000000000000000000000000000000000001
```

注意：
- `fromAmount` 要按代币精度写（USDC 是 6 位，所以 100 USDC = 100000000）
- `fromAddress` 随便填一个有效地址就行（用来算 gas 等）

**你要重点盯的字段**：
- `estimate.toAmount`：预计能收到多少
- `estimate.toAmountMin`：最坏情况下能收到多少（保底）
- `estimate.feeCosts`：各种费用
- `estimate.gasCosts`：Gas 成本
- `action` 和 `tool`：实际用了哪个工具（桥或 DEX）
- `includedSteps`：路径拆解成了哪几步

这就是你前面脚本里看到那些数字的来源。

---

#### 6. `/routes`（或高级 routes）—— 看多条可选路径

有些文档会用 `/routes` 或 `/advanced/routes`。

它和 `/quote` 的区别是：
- `/quote`：通常直接给你当前认为最好的一条
- `/routes`：会返回多条可选路径，让你自己比较

建议你先把 `/quote` 吃透，再尝试 routes。

---

### 实际执行建议（新人友好版）

1. **先用浏览器**一个一个打开上面的链接，感受返回的 JSON 长什么样。
2. 把每个端点的“最重要 3-5 个字段”记在笔记里，用自己的话写。
3. 重点反复玩 `/quote`，换不同的：
   - 金额（小额 vs 大额）
   - 链组合（同链 swap vs 跨链）
   - 代币（稳定币 vs 非稳定币）
4. 观察当金额变大时，`toAmount` 和 `toAmountMin` 的差距会怎么变化（这其实就和前面学的“深度与价格冲击”有关）。

---

### 完成标准（你可以对照检查）

当你能用自己的话回答下面这些问题，这个任务就过关了：

- `/chains` 返回的是什么？我怎么快速找到某条链的 ID？
- `/tokens` 里最重要的字段是哪些？
- `/quote` 里 `toAmount` 和 `toAmountMin` 分别代表什么？
- 费用相关的信息大概在哪些字段里？
- `/connections` 和 `/quote` 有什么不同？

---
