# Hermes Agent 配置指南（保姆版）

> 目标：把刚装好的 Hermes 从"能启动"配到"能干活"。
> 对应共学文档 Week 2 第一条任务：用 Hermes Agent 搭研究工作流。

---

## 0. 先搞清楚 Hermes 是个什么东西

一句话：**它是装在终端里的 AI 助手，跟 Claude Code 是同一类东西。**

不是网页，不是桌面软件，没有图标可以点。你在终端敲 `hermes`，它就起来了，然后你用中文跟它说话，它可以：

- 帮你读文件、写代码、改代码
- 帮你执行终端命令（比如跑 `curl` 调 LI.FI、跑 `psql` 查你的数据库）
- 帮你上网搜资料
- **干完一件事，自己写一个"技能文件"存起来，下次同类任务直接复用**

最后那条是它跟普通 AI 聊天的最大区别，也是 Week 2 真正要产出的东西。

---

## 1. 确认命令能用

打开一个**新的**终端窗口（重要，旧窗口读不到新配置），敲：

```bash
hermes --version
```

**如果显示了版本号** → 跳到第 2 节。

**如果提示 `command not found`** → 说明系统还不知道 `hermes` 这个命令在哪。安装脚本已经把路径写进了 `~/.zshrc`，让它生效一次就行：

```bash
source ~/.zshrc
hermes --version
```

> **PATH 是什么**：系统里有一个"我该去哪些文件夹里找命令"的清单，叫 PATH。Hermes 装在 `~/.local/bin/` 里，安装脚本把这个文件夹加进了清单，但已经开着的终端不会自动重读，所以要么开新窗口，要么手动 `source` 一次。

---

## 2. 看看现在是什么状态

```bash
hermes status
```

这条命令会打印一张体检表。你现在的机器上，关键几行长这样：

```
Model:        tencent/hy3:free        ← 用的哪个大模型
Provider:     Nous Portal             ← 从哪家买的算力
Nous Portal   ✓ logged in             ← 已经登录了，好事

◆ API Keys
  OpenRouter    ✗ (not set)
  Tavily        ✗ (not set)
  GitHub        ✗ (not set)
  ... 全是 ✗
```

再跑一条更细的：

```bash
hermes doctor
```

它会列出哪些工具能用、哪些用不了。你现在的情况：

| 能用 ✅ | 用不了 ❌ |
|---|---|
| 读写文件 | **联网搜索**（没配任何搜索 key） |
| 执行终端命令 | 图像生成 |
| 跑 Python 代码 | 语音 |
| 浏览器自动化 | Discord / 智能家居等（用不到） |
| 记忆、技能、定时任务 | |

**翻译成人话**：Hermes 现在是个"断网"的助手。它能在你电脑上干活，但不能上网查东西。而共学 Week 2 的第一步就是"信息发现"——所以这块必须先补上。

---

## 3. 三件必须配的事

按重要性排序，**从上往下配，配完一个验证一个**。

### 3.1 换一个能干活的模型（最重要）

**为什么要换**：你现在用的 `tencent/hy3:free` 是免费档模型。免费的意思是能力弱 + 会限流。让它读 LI.FI 文档、推导成本公式、写 Python 探针，大概率中途就崩了或者算错。

**另外**：你的 Nous Portal 账户没有付费额度，所以官方托管的联网搜索、浏览器这些"高级工具"全部锁着（`hermes status` 最下面那段 Tool Gateway 提示就是在说这个）。

**两条路，选一条就行：**

---

**路线 A：给 Nous Portal 充值**（省事，一次搞定模型 + 联网）

1. 打开 https://portal.nousresearch.com/billing 充值
2. 回到终端刷新一下：
   ```bash
   hermes model
   ```
3. 在弹出的列表里选一个正经模型（不带 `:free` 后缀的）

充完之后，联网搜索、浏览器这些会自动解锁，**第 3.2 节可以直接跳过**。

---

**路线 B：用 OpenRouter**（推荐，一个 key 能用几乎所有模型）

OpenRouter 是个"模型中转站"，注册一个账号拿一个 key，就能调 Claude、GPT、Gemini、DeepSeek 等等，按量付费。

1. 去 https://openrouter.ai 注册，充点钱（10 刀够用很久），在 Keys 页面创建一个 key，形如 `sk-or-v1-xxxxx`

2. 把 key 写进 Hermes 的配置文件：

   ```bash
   echo 'OPENROUTER_API_KEY=sk-or-v1-把你的key粘这里' >> ~/.hermes/.env
   ```

   > **`.env` 是什么**：一个专门存密码/密钥的文件，位置在 `~/.hermes/.env`。写成 `名字=值` 一行一条。Hermes 启动时会自动读它。
   >
   > **`>>` 是什么**：追加一行到文件末尾。注意是两个 `>`，一个 `>` 会把整个文件清空重写，别打错。

3. 选模型：

   ```bash
   hermes model
   ```

   这是个交互式菜单，用方向键选 `openrouter`，然后挑具体模型。做研究和写代码的话选个强的。


**补充: 线路C**

使用 DeepSeek:

配置DeepSeek:
```bash
hermes setup
```
依次选择:
```bash
Quick Setup
→ DeepSeek
→ 输入 DeepSeek API Key
→ Base URL: https://api.deepseek.com
→ 默认模型: deepseek-v4-flash
```
再设置备用模型(操作同上)：

```bash
hermes fallback add
```
---


**验证**：

```bash
hermes status
```

看 `Model:` 那行有没有变成你选的模型。

---

### 3.2 打开联网搜索

> 走了路线 A（Nous 充值）的话，这节跳过。

**为什么要配**：Hermes 的 `web` 工具现在因为一个 key 都没有而**整个禁用**了。不联网它就没法帮你查 LI.FI 的新接口、查某条链的桥有没有出事、查一个套利案例的公开复盘。

最省事的是 Tavily，专门给 AI 用的搜索接口，免费额度每月 1000 次，对你完全够：

1. 去 https://tavily.com 注册，拿一个 key，形如 `tvly-xxxxx`

2. ```bash
   echo 'TAVILY_API_KEY=key' >> ~/.hermes/.env
   ```

**验证**：

```bash
hermes doctor
```

原来那行 `⚠ web (missing EXA_API_KEY, TAVILY_API_KEY, ...)` 应该消失或者变成 ✓。

---

### 3.3 配 GitHub Token

**为什么要配**：两个用途——① 你共学的打卡要往 GitHub 推，让 Hermes 帮你提交；② Hermes 下载"技能包"时走 GitHub，没 token 的话每小时只能请求 60 次，很容易卡住。

1. 打开 https://github.com/settings/tokens → Generate new token (classic)
2. 权限**只勾** `repo` 和 `read:packages`，别乱给
3. 复制生成的 token（形如 `ghp_xxxxx`，**只显示一次，关掉就没了**）

```bash
echo 'GITHUB_TOKEN=token' >> ~/.hermes/.env
```

顺手初始化一下技能库：

```bash
hermes skills list
```

---

### 3.4 最后统一验收

```bash
hermes doctor && hermes status
```

理想状态：`doctor` 最后不再提示 "Run 'hermes setup' to configure missing API keys"，`status` 里 Model 是你选的模型、OpenRouter/Tavily/GitHub 三行变成 ✓。

---

## 4. 第一次启动

```bash
cd ~/Dev/MEV-LIFI
hermes
```

进去之后直接用中文说话就行。第一次可以拿这个试水，正好验证它读文件和跑代码都正常：

```
读一下 cost_probe.py，用一句话告诉我它在算什么
```

**退出**：`Ctrl+C` 两次，或者输入 `/exit`。

**继续上次的对话**：

```bash
hermes --continue
```

---

## 5. 怎么接上 Week 2 的四个任务

配置到上面就够用了。剩下的是"怎么用"，对应共学文档里那条链路：
`信息发现 → 研究拆解 → 工具调用 → 数据整理 → 持续监控`

### 5.1 先划定工作范围

Hermes 默认能翻你整个电脑。建议先建个"项目"把它框在两个目录里，省得它到处乱找：

```bash
hermes project
```

按提示把 `~/Dev/MEV-LIFI` 和 `~/Dev/onChainListen` 加进去。

### 5.2 让它调你的数据库（工具调用）

你的 onChainListen 是 Go + PostgreSQL。**不需要额外配任何东西**——Hermes 自带的 `terminal` 工具可以直接跑 `psql`，你只要告诉它库在哪、表长什么样，它就能自己查。

想要更规范一点，可以接一个专门的 postgres 连接器：

```bash
hermes mcp catalog        # 看有哪些官方认可的连接器
hermes mcp add            # 按提示添加
```

> **MCP 是什么**：一个让 AI 连外部系统（数据库、API、第三方服务）的标准接口。用 `terminal` 跑 `psql` 是"土办法"，MCP 是"正规办法"，效果差不多，先用土办法也完全可以。

### 5.3 把重复的活固化成技能（数据整理）

这是 Hermes 区别于普通 AI 的核心，也是 **Week 2 真正的交付物**。

做法很简单：让它干一件事，干完之后跟它说"把这个流程存成技能"。它会在 `~/.hermes/skills/` 下写一个 markdown 文件，下次你说"跑一下 XX"，它直接照着做，不用你重新解释一遍。

你该沉淀的几个技能：

- **成本探针**：给定链对 + 资产 → 调 `cost_probe.py` → 输出 token 口径的真实 bps 门槛
- **证据入表**：把探针结果按共学文档第 4 节的 schema 追加到 `evidence.csv`
- **清算复盘**：给定一笔 Sonic 上的清算 tx → 拉链上数据 → 算真实到手 bps

### 5.4 让它自己盯着（持续监控）

Week 2 的目标是"把手动看一眼变成系统持续盯"，落地就靠定时任务：

```bash
hermes cron create
```

比如设成每小时跑一次成本探针，发现 bps 门槛跌破某个值就通知你。

想让通知发到微信/TG/飞书这类地方：

```bash
hermes gateway setup
```

---

## 6. 安全提醒（别跳过）

你在做的事跟真金白银沾边，这几条记住：

1. **别用 `--yolo`**。这个参数的意思是"所有操作不用问我，直接执行"。Hermes 有完整的终端权限，一个 `--yolo` 加一个理解错的指令，可以把你的目录删了或者发出一笔真交易。

2. **默认没有硬性刹车**。配置里 `tool_loop_guardrails.hard_stop_enabled` 是 `false`，意思是它反复失败也不会自动停，只会警告。想加刹车：

   ```bash
   hermes config set tool_loop_guardrails.hard_stop_enabled true
   ```

3. **私钥永远不要放进 `~/.hermes/.env`**。Hermes 确实有密钥脱敏机制（`redact_secrets` 默认开），但那是防止密钥被打印出来，不是防止它被使用。涉及签名和发交易的环节，人工来。

4. **呼应共学文档第 144 行那句**：AI Agent 是工具不是印钞机，它帮你把观察规模化，护城河很薄，别指望它自动生成 alpha。

---

## 附：常用命令速查

| 命令 | 干什么 |
|---|---|
| `hermes` | 启动，开始聊 |
| `hermes --continue` | 接着上次的对话 |
| `hermes status` | 体检：模型、密钥、登录状态 |
| `hermes doctor` | 体检：哪些工具能用 |
| `hermes model` | 换模型 |
| `hermes fallback` | 配备用模型 |
| `hermes config env-path` | 打印 `.env` 文件在哪 |
| `hermes config show` | 看全部配置 |
| `hermes skills list` | 看已有技能 |
| `hermes cron list` | 看定时任务 |
| `hermes project` | 管理工作目录 |
| `hermes update` | 升级 Hermes |

**重要文件位置**：

| 路径 | 是什么 |
|---|---|
| `~/.hermes/.env` | 密钥（API key 都写这里） |
| `~/.hermes/config.yaml` | 主配置 |
| `~/.hermes/skills/` | 技能文件 |
| `~/.hermes/memories/` | 长期记忆 |
| `~/.hermes/sessions/` | 历史对话 |
