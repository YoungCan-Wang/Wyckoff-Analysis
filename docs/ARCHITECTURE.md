# 系统架构

[← 返回 README](../README.md)

## 系统全景

```
     ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
     │  Streamlit   │  │  CLI (TUI)   │  │  MCP Server  │  │  GitHub      │
     │  Web UI      │  │  Terminal    │  │  (stdio)     │  │  Actions     │
     └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
            │                 │                 │                 │
            ▼                 ▼                 ▼                 ▼
     ┌─────────────────────────────────────────────────────────────────┐
     │                      Agent Brain                                │
     │  Web: Google ADK · CLI: Agent Loop + BG Task · MCP: FastMCP    │
     │                                                                 │
     │  10 专业工具 + 5 通用能力 — LLM 自主编排                          │
     │  自动 Plan Mode — 复杂任务拆步骤执行                              │
     └──────────────────────────┬──────────────────────────────────────┘
                                         │
          ┌──────────────────────────────┼──────────────────────────────┐
          ▼                              ▼                              ▼
   ┌─────────────┐              ┌──────────────┐              ┌──────────────┐
   │ Core Engine │              │ LLM          │              │ Storage      │
   │             │              │              │              │              │
   │ Funnel      │              │ Gemini  ★    │              │ Supabase     │
   │ Diagnostic  │              │ Claude       │              │ SQLite 本地  │
   │ Strategy    │              │ OpenAI       │              │  (离线缓存)  │
   │ Signal      │              │ DeepSeek     │              │              │
   │ Sector      │              │ Qwen/Kimi    │              └──────────────┘
   │ Tail-Buy    │              │ 智谱/火山    │
   └──────┬──────┘              │ Minimax      │
          │                     └──────────────┘
          ▼
   ┌─────────────┐
   │ Data Sources│
   │             │
   │ tickflow ★  │
   │ tushare     │
   │ akshare     │
   │ baostock    │
   │ efinance    │
   └─────────────┘
```

## Agent 架构

### 三通道复用

Web、CLI、MCP 共享同一套工具函数（`agents/chat_tools.py`）+ 同一份 System Prompt（`core/prompts.py`），通过不同运行时驱动：

| | Web（Streamlit） | CLI（TUI） | MCP Server |
|---|---|---|---|
| 运行时 | Google ADK `LlmAgent` | Agent Loop（`cli/tui.py`） | FastMCP（stdio） |
| UI | Streamlit 页面 | Textual 全屏 TUI | 无（被 Claude Code 等调用） |
| 入口 | `streamlit_app.py` | `wyckoff`（无子命令） | `wyckoff-mcp` |
| 工具数 | 10（共享） | 18（+5 本地 +3 委派） | 10（三层权限） |
| 对话能力 | ✓ ADK Runner 多轮 | ✓ Agent Loop 多轮 | ✗ 单次工具调用 |
| 后台任务 | ✗ | ✓ 长任务非阻塞 | ✗ |
| 消息排队 | ✗ | ✓ Agent 忙时自动排队 | N/A |
| Thinking | ✗ | ✓ 推理模型 reasoning 展示 | N/A |
| Agent 记忆 | ✗ | ✓ 跨会话记忆（SQLite） | ✗ |
| 上下文压缩 | ✗ | ✓ 动态阈值（25% context window）自动压缩 | N/A |
| 可视化面板 | ✗ | ✓ `wyckoff dashboard` | ✗ |
| Plan Mode | ✓ prompt 驱动 | ✓ prompt 驱动 | N/A |

**CLI 专属工具**（Web / MCP 不可用）：`exec_command`、`read_file`、`write_file`、`web_fetch`、`check_background_tasks`、`delegate_to_research`、`delegate_to_analysis`、`delegate_to_trading`

**MCP 三层权限**：
- Tier 1（无需凭证）：历史查询（`query_history`）— 纯本地 SQLite 读写
- Tier 2（需 TUSHARE_TOKEN 等 env）：搜索、分析（`analyze_stock`）、大盘、扫描、回测
- Tier 3（需 Supabase 用户认证或本地降级）：持仓管理（`portfolio`/`update_portfolio`）、AI 研报、攻防决策

### ReAct 循环（Reasoning + Acting）

Agent 采用 ReAct 范式：每一轮 LLM 先推理（Reason），再决定是否行动（Act），观察工具结果（Observe）后进入下一轮推理，直到能直接回答用户。

```
                        ┌──────────┐
                        │  用户输入  │
                        └────┬─────┘
                             │
                   ┌─────────▼──────────┐
                   │  Reason            │
                   │  LLM 推理 + 规划   │◄───────────┐
                   │  (thinking/text)   │            │
                   └─────────┬──────────┘            │
                             │                       │
                    ┌────────┴────────┐              │
                    │  需要 Act?      │              │
                    └───┬─────────┬───┘              │
                     No │         │ Yes              │
                        ▼         ▼                  │
                  ┌──────────┐  ┌──────────────┐     │
                  │ 输出回答  │  │  Act         │     │
                  └──────────┘  │  执行工具     │     │
                                │              │     │
                                │ 后台工具?     │     │  Observe
                                │  ├─Y→ submit │     │  工具结果
                                │  └─N→ 同步   │     │  注入上下文
                                └──────┬───────┘     │
                                       └─────────────┘
                                    (最多 15 轮)
```

### 工具清单（18 个）

| # | 工具 | 说明 | 执行 | 可用通道 |
|---|------|------|------|---------|
| 1 | `search_stock_by_name` | 代码⇄名字双向模糊搜索（多源降级） | 同步 | 全部 |
| 2 | `analyze_stock` | 单股 Wyckoff 诊断 / 近期 OHLCV 行情（mode 切换） | 同步 | 全部 |
| 3 | `portfolio` | 持仓列表 / 批量持仓健康扫描（mode 切换） | 同步 | 全部 |
| 4 | `update_portfolio` | 新增/修改/删除持仓、设可用资金、删除追踪记录 | 同步 | 全部 |
| 5 | `get_market_overview` | 主要指数涨跌幅 | 同步 | 全部 |
| 6 | `screen_stocks` | 五层漏斗筛选 | ⚡后台 | 全部 |
| 7 | `generate_ai_report` | 三阵营 AI 研报 | ⚡后台 | 全部 |
| 8 | `generate_strategy_decision` | 扫描→研报→决策全流程 | ⚡后台 | 全部 |
| 9 | `query_history` | 历史推荐 / 信号池 / 尾盘记录查询 | 同步 | 全部 |
| 10 | `run_backtest` | 漏斗策略历史回测 | ⚡后台 | 全部 |
| 11 | `check_background_tasks` | 后台任务进度查询 | 同步 | CLI |
| 12 | `exec_command` | 执行本地 shell 命令 | 同步 | CLI |
| 13 | `read_file` | 读取本地文件（CSV/Excel 自动解析） | 同步 | CLI |
| 14 | `write_file` | 写入文件（导出报告/数据） | 同步 | CLI |
| 15 | `web_fetch` | 抓取网页内容（财经新闻/公告） | 同步 | CLI |
| 16 | `delegate_to_research` | 委派数据收集任务给 Research Sub-Agent | 同步 | CLI |
| 17 | `delegate_to_analysis` | 委派深度分析任务给 Analysis Sub-Agent | 同步 | CLI |
| 18 | `delegate_to_trading` | 委派交易决策任务给 Trading Sub-Agent | 同步 | CLI |

标记 ⚡后台 的工具提交到 `BackgroundTaskManager`（daemon Thread），不阻塞对话。
CLI 专属工具仅在 TUI 环境中可用，Web 和 MCP 不注册这些工具。

### 工具路由原则

System Prompt 内建路由规则，LLM 自主判断调哪个工具：

- "我有什么持仓" → `portfolio(mode="view")`（纯数据，秒回）
- "持仓健康吗" → `portfolio(mode="diagnose")`（逐只诊断，较慢）
- "帮我加/删持仓" → `search_stock_by_name` → `update_portfolio`（先查名再改）
- "有什么机会" → `screen_stocks`（后台执行）

**铁律：一个工具能回答的问题，绝不调两个。用户没要求分析，就不要分析。**

### 自动 Plan Mode

复杂任务（≥2 个工具）自动进入 Plan Mode：

```
用户: "帮我全面分析一下现在的市场"
  │
  ▼
Agent 输出计划:
  1. 查大盘水温 → get_market_overview
  2. 全市场扫描 → screen_stocks（后台）
  3. 诊断持仓 → diagnose_portfolio
  4. 综合建议
  │
  ├─→ 逐步执行，每步汇报进度
  ├─→ 步骤间可动态调整（如大盘极弱则跳过进攻）
  │
  ▼
最终综合结论
```

### 后台任务架构

`cli/background.py` — `BackgroundTaskManager`

```
Agent → tool_call: screen_stocks
  │
  ├─→ ToolRegistry 检测为 BACKGROUND_TOOLS
  │   {"screen_stocks", "generate_ai_report", "generate_strategy_decision"}
  │
  ├─→ BackgroundTaskManager.submit() → daemon Thread 执行
  ├─→ 立即返回 {"status": "background", "task_id": "bg_xxx"}
  │
  ▼
Agent → "已提交后台，可继续提问"
  │
  │   （用户继续聊天...）
  │
  ▼   （后台线程完成）
on_complete 回调 → TUI 显示通知 → 结果注入消息队列 → Agent 自动汇报
```

### 消息排队

```
用户输入 → Agent 忙? ─No→ 立即处理
                      │
                      Yes→ 入 deque 队列，显示 "⏳ 已排队 (N)"
                              │
                              ▼ （当前任务完成后）
                         自动取队首消息 → 继续处理
```

`/new` 清对话时同步清空队列。

### CLI Provider 层

```
LLMProvider (abstract)              cli/providers/base.py
  │
  ├── GeminiProvider                google-genai SDK
  ├── ClaudeProvider                anthropic SDK
  ├── OpenAIProvider                openai SDK + base_url + reasoning_content
  └── FallbackProvider              多模型路由，按可用性自动切换
```

统一接口：`chat_stream(messages, tools, system_prompt) → Generator[chunk]`

chunk 类型：`thinking_delta` | `text_delta` | `tool_calls` | `usage`

OpenAI provider 兼容所有 OpenAI API 格式端点（DeepSeek / Qwen / Kimi / LongCat / Minimax 等），支持推理模型的 `reasoning_content` thinking 流，以及 `<tool_call>` XML 标签兜底解析。

### MCP Server

`mcp_server.py` — 通过 [Model Context Protocol](https://modelcontextprotocol.io) 将 Wyckoff 分析能力暴露给外部 AI Agent（Claude Code、Cursor 等）。

```
Claude Code / Cursor / 其他 MCP 客户端
  │
  ├─→ stdio 连接 → wyckoff-mcp 进程
  │
  ├─→ MCP 协议 → FastMCP 路由 → chat_tools.py 中的函数
  │
  └─→ 工具结果 JSON ← 返回
```

**与 CLI / Web 的关键区别**：MCP Server 不具备对话能力，它只是一个工具服务——LLM 的推理和多轮编排由外部客户端（如 Claude Code）负责，Wyckoff MCP 只响应单次工具调用。

安装与注册：

```bash
pip install youngcan-wyckoff-analysis[mcp]
claude mcp add wyckoff -- wyckoff-mcp
```

凭证通过环境变量注入（`TUSHARE_TOKEN`、`SUPABASE_*`），或由 `_get_credential` 自动从 `~/.wyckoff/wyckoff.json` 读取。

### TUI 视觉层次

```
❯ 用户问题                           ← cyan 粗体

  💭 推理摘要…  (1234 字)             ← thinking：一行，dim italic
  ⚙ 搜索股票  keyword=宁德           ← tool 执行：黄色
  ✓ 搜索股票  0.3s                   ← tool 完成：绿色
  ✗ 调取行情  1.2s 超时              ← tool 失败：红色
  ↗ 全市场扫描  已提交后台            ← 后台任务：cyan
  ───                                ← 分隔线
  最终 Markdown 输出...              ← Markdown 渲染

  ↑1,234 ↓567 · 2.3s               ← token 统计：dim
```

## Agent 记忆系统

`cli/memory.py` — 跨会话记忆，存储在 SQLite `agent_memory` 表。

### 写入时机

三种触发路径：
- **`/new`**：开启新会话时保存上一轮
- **退出 TUI**（`/quit`、`/exit`、Ctrl+Q）：后台线程保存，5s 超时
- **Ctrl+C**：同上

满足以下条件自动提取：
- 消息数 ≥ 4
- 至少有 1 次工具调用

LLM 从最近 40 条消息中提取关键结论（≤300 字），带 `[偏好]` 前缀的行以 `memory_type='preference'` 存储（永不清理），其余以 `session` 类型存储。

### 自动清理

TUI 启动时自动执行 `prune_memories()`，清理 90 天前的 `session` 和 `fact` 类型记忆。

### 检索注入

每次用户提问前，三路检索 + 偏好置顶：
1. 从 user_message 提取股票代码（正则 `\d{6}`），匹配相关记忆（最多 5 条）
2. 从 user_message 提取中文 2-gram 关键词（过滤停用词），关键词 OR 匹配补充召回
3. 取最近 3 条 session 记忆
4. 始终拉取全部 `preference` 类型记忆，置顶显示

拼成两段注入 system prompt 尾部：

```
# 用户偏好
- 不要推荐 ST 股
- 风险偏好中等，止损 -6%

# 历史记忆
- [04-20] 000001 处于吸筹 Phase C，支撑位 12.50
- [04-21] 用户关注半导体板块轮动
```

### 记忆类型

| 类型 | 说明 | 自动清理 |
|------|------|---------|
| `session` | LLM 提取的会话摘要 | 90 天 |
| `fact` | 用户主动记录的事实 | 90 天 |
| `preference` | 用户偏好 | 永不清理 |

## 上下文压缩

`cli/compaction.py` — TUI 和 headless agent loop 共用。

### 动态阈值

阈值 = 模型 context window × 25%。按模型前缀匹配：

| 模型前缀 | Context Window | 压缩阈值 |
|---------|---------------|---------|
| deepseek | 64K | 16K |
| gpt-4o | 128K | 32K |
| gemini-2 | 1M | 250K |
| claude | 200K | 50K |
| 未知模型 | 64K（默认） | 16K |

### 压缩策略

1. 保留最近 4 条消息（`TAIL_KEEP = 4`）原文
2. 前面的消息用 LLM 总结为 ≤500 字中文摘要
3. 工具结果做智能摘要而非粗暴截断：
   - `analyze_stock` 诊断模式 → 保留 `code`、`phase`、`health`、`trigger_signals` 等关键字段
   - `analyze_stock` 行情模式 → 保留最近 5 条数据
   - `portfolio` 诊断模式 → 保留 `diagnostics`、`successful_count` 等
   - `portfolio` 查看模式 → 保留 `positions`、`free_cash` 等
   - 通用工具 → 保留 `error`、`message`、`status` 等顶层键

```
[对话摘要]
用户查询了平安银行和贵州茅台的诊断...
---
[最近 4 条原始消息]
```

### 工具确认机制

高风险写操作工具需用户确认后才执行：

| 工具 | 风险等级 |
|------|---------|
| `exec_command` | 高（执行任意命令） |
| `write_file` | 高（写入文件） |
| `update_portfolio` | 中（修改持仓） |

确认选项：允许一次 / 本次会话总是允许 / 修改后执行 / 不允许。

### Doom Loop 防护

滑动窗口检测（最近 6 次调用），同参数重复调用 ≥3 次自动中止，防止模型死循环浪费 token。

## 本地可视化面板

`cli/dashboard.py` — `wyckoff dashboard [--port 8765]`

纯 Python 内置 HTTP 服务器 + 嵌入式 SPA，无外部依赖。启动后自动打开浏览器。

### 功能

| 页面 | 数据源 | 说明 |
|------|--------|------|
| 总览 | sync_meta | 各模块最后同步时间 + 行数 |
| AI 推荐 | recommendation_tracking | 推荐股票 + 当前价 + 收益率，支持逐条删除 |
| 信号池 | signal_pending | L4 信号状态列表，支持逐条删除 |
| 持仓 | portfolio + positions | 当前持仓明细 |
| Agent 记忆 | agent_memory | 跨会话记忆列表，支持逐条删除 |
| 配置 | wyckoff.json | 模型配置（API Key 脱敏） |
| 对话日志 | chat_log | 按会话浏览历史对话 + token 统计，支持按会话删除 |
| Agent 日志 | agent.log | 实时查看文件日志尾部 |
| 同步状态 | sync_meta | 各表 TTL 和最后同步时间 |

### 特性

- **双主题**：暗色（Bloomberg 终端风格）/ 亮色，`localStorage` 持久化
- **双语 i18n**：中文 / English，`localStorage` 持久化
- **9 个 GET + 4 个 DELETE 端点**：GET `/api/config`、`/api/memory`、`/api/recommendations`、`/api/signals`、`/api/portfolio`、`/api/sync`、`/api/chat-sessions`、`/api/chat-log/<sid>`、`/api/agent-log`；DELETE `/api/memory/<id>`、`/api/recommendations/<code>`、`/api/signals/<code>`、`/api/chat-sessions/<sid>`

## 对话日志

### 文件日志

`~/.wyckoff/agent.log` — Python `logging.FileHandler`，记录每次对话的 session_id、用户输入、耗时、token 用量。

### SQLite chat_log 表

```sql
CREATE TABLE chat_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    role        TEXT NOT NULL,       -- user / assistant / tool / error
    content     TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    provider    TEXT DEFAULT '',
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    elapsed_s   REAL DEFAULT 0,
    error       TEXT DEFAULT '',
    tool_calls  TEXT DEFAULT '',     -- JSON
    created_at  TEXT DEFAULT (datetime('now'))
);
```

`list_chat_sessions()` 按 session_id 聚合：起止时间、消息数、总 token、最后错误。

## 本地持久化（~/.wyckoff/）

| 文件 / 数据库 | 用途 |
|-------------|------|
| `wyckoff.json` | 模型配置（provider / api_key / model / base_url） |
| `session.json` | Supabase 登录态（access_token / refresh_token） |
| `agent.log` | Agent 文件日志 |
| `wyckoff.db` | SQLite 数据库（下方详述） |

### SQLite 表（wyckoff.db）

| 表 | 用途 |
|---|------|
| `schema_version` | 迁移版本管理（当前 v4） |
| `recommendation_tracking` | 推荐跟踪镜像 |
| `signal_pending` | 信号池镜像 |
| `market_signal_daily` | 大盘信号镜像 |
| `portfolio` | 持仓元数据镜像 |
| `portfolio_position` | 持仓明细镜像 |
| `agent_memory` | 跨会话 Agent 记忆 |
| `sync_meta` | 同步元数据（每表最后同步时间） |
| `chat_log` | 对话日志（用户输入 + LLM 输出 + token） |
| `tail_buy_history` | 尾盘策略执行历史 |

### Supabase → SQLite 同步

`integrations/sync.py` — TUI 启动时自动后台同步（daemon thread）。

| 表 | 同步策略 | TTL |
|---|---------|-----|
| `recommendation_tracking` | 最近 200 条 | 4 小时 |
| `signal_pending` | 最近 200 条 | 4 小时 |
| `market_signal_daily` | 最近 30 天 | 6 小时 |
| `portfolio` + `positions` | 全量覆写 | 2 小时 |

Supabase 不可达时静默跳过，使用本地陈旧数据。`wyckoff sync` 可手动触发。

## 五层漏斗引擎

`core/wyckoff_engine.py`，~60 可调参数（`FunnelConfig`）。

| 层 | 名称 | 逻辑 |
|----|------|------|
| L1 | 剥离垃圾 | 剔除 ST / 北交 / 科创，市值 ≥ 35 亿，成交额 ≥ 5000 万 |
| L2 | 六通道甄选 | 主升 / 点火 / 潜伏 / 吸筹 / 地量 / 护盘 |
| L2.5 | Markup 识别 | MA50 上穿 MA200 + 角度验证 |
| L3 | 板块共振 | L2 通过股票行业分布，保留 Top-N 行业 |
| L4 | 微观狙击 | Spring / LPS / SOS / EVR 触发信号 |
| L5 | 退出信号 | 初始止损 -6%、利润激活线 +15%、跟踪止损（高点回撤 -10% 或跌破 MA50）、派发警告（高位缩量 3 天） |

## 信号确认状态机

`core/signal_confirmation.py`，L4 信号经 1-3 天价格确认：

```
pending ──(价格确认)──→ confirmed（可操作）
   └──(超时)──→ expired（失效）
```

TTL：SOS 2 天、Spring 3 天、LPS 3 天、EVR 2 天。

## 尾盘策略

`core/tail_buy_strategy.py` + `scripts/tail_buy_intraday_job.py`

盘中 14:00 执行，从前日 L4 信号中筛选尾盘买入标的。

### 两阶段评估

```
signal_pending (pending/confirmed)
  │
  ├─→ 获取 1 分钟盘中数据（TickFlow）
  │
  ├─→ 第一阶段：规则打分（15+ 特征）
  │   VWAP 位置、尾盘量比、日内回撤、突破形态...
  │   BUY ≥ 72 · WATCH ≥ 52 · SKIP < 52
  │
  ├─→ 第二阶段：LLM 复判（Top N 候选）
  │   输入：规则特征 + 5 分钟摘要 + 信号上下文
  │   输出：{"decision":"BUY|WATCH|SKIP","reason":"...","confidence":0.8}
  │
  ├─→ 规则 × LLM 合并 → 最终排序
  │
  └─→ 推送飞书 / Telegram
```

### 持仓监控

同一任务还扫描当前持仓，输出 HOLD / ADD / TRIM 建议。

## Pipeline（定时任务）

### GitHub Actions 工作流（10 个）

| 工作流 | 时间（北京） | 说明 |
|-------|-------------|------|
| **CI** | push/PR | pytest + compile + dry-run |
| **漏斗筛选 + AI 研报 + 决策** | 周日-周四 18:25 | `daily_job.py` Step2→3→4 |
| **尾盘策略** | 周一-周五 13:50 | `tail_buy_intraday_job.py` |
| **盘前风控** | 周一-周五 08:20 | A50 + VIX 预警 |
| **涨停复盘** | 周一-周五 19:25 | 当日涨幅 ≥ 8% 回溯 |
| **推荐跟踪重定价** | 周日-周四 23:00 | 同步收盘价、计算收益 |
| **缓存维护** | 每天 23:05 | 清理过期行情缓存 |
| **回测网格** | 每月 1 / 15 日 04:00 | 3 阶段：快照→18 并行格→聚合通知 |
| **Web 后台任务** | 手动触发 | Streamlit 发起的漏斗/研报任务 |
| **输入预览** | 手动触发 | dry-run 模式查看漏斗输入 |

## 数据源

```
tickflow(★) → tushare → akshare → baostock → efinance   （行情 OHLCV，五级降级）
tushare → akshare + 本地 24h 缓存              （股票列表，代码⇄名字映射）
tickflow                                        （1 分钟盘中数据，尾盘策略专用）
```

`integrations/rag_veto.py` — 新闻否决层：抓取东方财富个股新闻，命中负面关键词则拦截推荐。

## 云端存储（Supabase）

| 表 | 用途 |
|----|------|
| `portfolios` | 投资组合元数据 |
| `portfolio_positions` | 持仓明细 |
| `trade_orders` | AI 交易建议 |
| `user_settings` | 用户配置（API Key / Webhook） |
| `stock_hist_cache` | 行情缓存（qfq，滚动 400 天） |
| `recommendation_tracking` | 推荐跟踪 |
| `signal_pending` | 信号确认池 |
| `market_signal_daily` | 大盘信号 |
| `daily_nav` | 每日净值 |
| `job_usage` | Web 用户限流 |

数据隔离：Web JWT → RLS，CLI access_token → RLS，脚本 service_role_key → 绕过 RLS。

## CLI 命令

```bash
wyckoff                          # 启动 TUI 对话（默认）
wyckoff update                   # 升级到最新版
wyckoff auth <email> <password>  # 登录
wyckoff auth logout              # 登出
wyckoff auth status              # 查看登录状态
wyckoff model list               # 列出模型配置
wyckoff model add                # 交互式添加模型
wyckoff model set <id> ...       # 非交互式设置模型
wyckoff model rm <id>            # 删除模型
wyckoff model default <id>       # 设置默认模型
wyckoff config                   # 查看数据源配置
wyckoff config tushare <token>   # 配置 Tushare
wyckoff config tickflow <key>    # 配置 TickFlow
wyckoff portfolio list           # 查看持仓（别名 pf）
wyckoff portfolio add <code>     # 添加持仓
wyckoff portfolio rm <code>      # 删除持仓
wyckoff portfolio cash [--amount]# 查看/设置可用资金
wyckoff signal [status]          # 查看信号池
wyckoff recommend                # 查看推荐记录（别名 rec）
wyckoff dashboard [--port N]     # 启动可视化面板（别名 dash）
wyckoff sync [status]            # 手动同步 / 查看同步状态
wyckoff-mcp                      # 启动 MCP Server（供 Claude Code 等调用）
```

## 安装方式

| 方式 | 命令 |
|------|------|
| 一键安装 | `curl -fsSL https://raw.githubusercontent.com/.../install.sh \| bash` |
| Homebrew | `brew tap YoungCan-Wang/wyckoff && brew install wyckoff` |
| pip | `uv pip install youngcan-wyckoff-analysis` |

`install.sh`：检测 Python 3.11+ → 安装 uv → 创建 `~/.wyckoff/venv` → 安装 PyPI 包 → 符号链接到 `~/.local/bin/wyckoff`。

## 目录结构

```
mcp_server.py    MCP Server 入口（FastMCP，10 个工具）
agents/          Agent 工具函数（ADK + CLI + MCP 共用）
app/             Streamlit Web 页面
cli/             CLI 入口、TUI、Agent Loop、Provider、Dashboard、Memory
  providers/     LLM Provider 实现（Gemini / Claude / OpenAI / Fallback）
core/            漏斗引擎、诊断、策略、信号确认、尾盘策略、常量
integrations/    数据源集成、Supabase 模块、SQLite 本地层、同步引擎
scripts/         定时任务脚本（GitHub Actions 调用）
tools/           搜索、新闻否决等辅助工具
utils/           通知推送（飞书/企微/钉钉/Telegram）、格式化
tests/           测试用例
data/            本地缓存（交易日历、股票列表、行业映射）
Formula/         Homebrew formula
```
