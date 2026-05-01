<div align="center">

# Wyckoff Trading Agent

**A 股威科夫量价分析智能体 — 你说人话，他读盘面**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![Streamlit](https://img.shields.io/badge/demo-Streamlit-FF4B4B.svg)](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)
[![Homepage](https://img.shields.io/badge/homepage-Wyckoff%20Homepage-0ea5e9.svg)](https://youngcan-wang.github.io/wyckoff-homepage/)

[English](docs/README_EN.md) | [日本語](docs/README_JA.md) | [Español](docs/README_ES.md) | [한국어](docs/README_KO.md) | [架构文档](docs/ARCHITECTURE.md)

</div>

---

用自然语言和一位威科夫大师对话。他能调动 10 个专业工具 + 5 个通用能力，自主串联多步推理，给出"打还是不打"的结论。

Web + CLI + MCP 三通道，Gemini / Claude / OpenAI 多模型切换，GitHub Actions 定时全自动。

项目主页：**[youngcan-wang.github.io/wyckoff-homepage](https://youngcan-wang.github.io/wyckoff-homepage/)**

## 启动即用 — 一键配置 Agent

| 启动界面 | 持仓查询 |
|:---:|:---:|
| <img src="attach/cli-home.png" width="450" /> | <img src="attach/cli-running.png" width="450" /> |

启动后只需两步：
1. `/model` — 选择模型（Gemini / Claude / OpenAI），输入 API Key
2. 开始对话 — 无需注册，持仓数据自动存本地

```
> 帮我看看 000001 和 600519 哪个更值得买
> 审判我的持仓
> 大盘现在什么水温
```

> 💡 可选：`/login` 登录后持仓同步云端，多设备共享。不登录也能完整使用所有功能。

## 功能一览

| 能力 | 说明 |
|------|------|
| 对话式 Agent | 用自然语言触发诊断、筛选、研报，LLM 自主编排工具；还能读写文件、执行命令、抓取网页 |
| Skills | 内置斜杠命令（`/screen`、`/checkup`、`/report`、`/strategy`、`/backtest`）一键复合工作流；用户可通过 `~/.wyckoff/skills/*.md` 扩展 |
| 上下文压缩 | 动态阈值（25% model context window）自动压缩，智能摘要保留工具关键数据，支持超长对话 |
| 工具确认 | `exec_command`、`write_file`、`update_portfolio` 执行前弹窗确认，防止误操作 |
| 五层漏斗筛选 | 全市场 ~4500 股 → ~30 候选，六通道 + 板块共振 + 微观狙击。基于历史量价结构发现潜力标的，不构成投资建议 |
| AI 三阵营研报 | 逻辑破产 / 储备营地 / 起跳板，LLM 独立审判 |
| 持仓诊断 | 批量体检：均线结构、吸筹阶段、触发信号、止损状态 |
| 私人决断 | 综合持仓 + 候选，输出 EXIT/TRIM/HOLD/PROBE/ATTACK 指令，Telegram 推送 |
| 尾盘策略 | 盘中 13:50 执行，规则打分 + LLM 复判两阶段筛选尾盘买入标的 |
| 信号确认池 | L4 触发信号经 1-3 天价格确认后才可操作 |
| 推荐跟踪 | 历史推荐自动同步收盘价、计算累计收益 |
| 日线回测 | 回放漏斗命中后 N 日收益，输出胜率/Sharpe/最大回撤 |
| 盘前风控 | A50 + VIX 监测，四档预警推送 |
| 本地可视化面板 | `wyckoff dashboard` — 推荐、信号、持仓、Agent 记忆、对话日志，暗色/亮色主题，中英双语 |
| Agent 记忆 | 跨会话记忆：自动提取对话结论，下次提问时注入相关上下文 |
| 通用 Agent 能力 | 执行命令、读写文件、抓取网页 — 发一个 CSV 路径即可分析，不只是股票工具 |
| MCP Server | 10 个工具通过 MCP 协议对外暴露，Claude Code / Cursor / 任何 MCP Client 即插即用 |
| 多通道推送 | 飞书 / 企微 / 钉钉 / Telegram |

## 数据源

个股日线自动降级：

```
tickflow → tushare → akshare → baostock → efinance
```

任一源不可用时自动切换，无需干预。

> **推荐接入 TickFlow（高频/分时/实时能力更强）**  
> 注册链接：[TickFlow注册链接](https://tickflow.org/auth/register?ref=5N4NKTCPL4)

## 快速开始

### 一键安装（推荐）

```bash
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/Wyckoff-Analysis/main/install.sh | bash
```

自动检测 Python、安装 uv、创建隔离环境，装完直接 `wyckoff` 启动。

### Homebrew

```bash
brew tap YoungCan-Wang/wyckoff
brew install wyckoff
```

### pip

```bash
uv venv && source .venv/bin/activate
uv pip install youngcan-wyckoff-analysis
wyckoff
```

升级：`wyckoff update`

### CLI 效果

| 诊断报告 | 操作指令 |
|:---:|:---:|
| <img src="attach/cli-analysis.png" width="450" /> | <img src="attach/cli-result.png" width="450" /> |

### 本地可视化面板

```bash
wyckoff dashboard
```

一条命令启动本地 HTTP 面板（默认端口 8765），自动打开浏览器。全部数据存储在本地 SQLite，无需联网，安全可信。

功能页面：AI 推荐、信号池、持仓、Agent 记忆、配置、对话日志、Agent 日志、同步状态。支持暗色/亮色主题切换，中英双语。

| 数据总览 | 对话日志 |
|:---:|:---:|
| <img src="attach/dashboard-overview.png" width="450" /> | <img src="attach/dashboard-chatlog.png" width="450" /> |

### 回测网格

18 组参数并行回测，自动输出最优参数组合、夏普矩阵和策略体检：

| 最优参数 & 梯队表 | 参数矩阵 |
|:---:|:---:|
| <img src="attach/backtest-grid-1.png" width="450" /> | <img src="attach/backtest-grid-2.png" width="450" /> |

### Web

```bash
git clone https://github.com/YoungCan-Wang/Wyckoff-Analysis.git
cd Wyckoff-Analysis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

在线体验：**[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

| 读盘室 | 数据导出 |
|:---:|:---:|
| <img src="attach/web-chat.png" width="450" /> | <img src="attach/web-export.png" width="450" /> |

## 工具

Agent 的武器库 — 10 个量价工具 + 5 个通用能力：

| 工具 | 能力 |
|------|------|
| `search_stock_by_name` | 名称 / 代码 / 拼音模糊搜索 |
| `analyze_stock` | 单股 Wyckoff 诊断 / 近期 OHLCV 行情（mode 切换） |
| `portfolio` | 查看持仓 / 批量持仓健康扫描（mode 切换） |
| `update_portfolio` | 新增 / 修改 / 删除持仓、设置可用资金、删除追踪记录 |
| `get_market_overview` | 大盘水温概览 |
| `screen_stocks` | 五层漏斗全市场筛选（⚡后台） |
| `generate_ai_report` | 三阵营 AI 深度研报（⚡后台） |
| `generate_strategy_decision` | 持仓去留 + 新标买入决策（⚡后台） |
| `query_history` | 历史推荐 / 信号池 / 尾盘记录查询 |
| `run_backtest` | 漏斗策略历史回测（⚡后台） |
| `check_background_tasks` | 后台任务进度查询 |
| `exec_command` | 执行本地 shell 命令 |
| `read_file` | 读取本地文件（CSV/Excel 自动解析） |
| `write_file` | 写入文件（导出报告/数据） |
| `web_fetch` | 抓取网页内容（财经新闻/公告） |

工具调用顺序和次数由 LLM 实时决策，无需预编排。发一个 CSV 路径他就能读；说"帮我装个包"他就能执行。

## 五层漏斗

| 层 | 名称 | 做什么 |
|----|------|--------|
| L1 | 剥离垃圾 | 剔除 ST / 北交所 / 科创板，市值 ≥ 35 亿，日均成交 ≥ 5000 万 |
| L2 | 六通道甄选 | 主升 / 点火 / 潜伏 / 吸筹 / 地量 / 护盘 |
| L3 | 板块共振 | 行业 Top-N 分布筛选 |
| L4 | 微观狙击 | Spring / LPS / SOS / EVR 四大触发信号 |
| L5 | AI 审判 | LLM 三阵营分类：逻辑破产 / 储备 / 起跳板 |

## 每日自动化

仓库内置 GitHub Actions 定时任务：

| 任务 | 时间（北京） | 说明 |
|------|-------------|------|
| 漏斗筛选 + AI 研报 + 私人决断 | 周日-周四 18:25 | 全自动，结果推送飞书/Telegram |
| 尾盘策略 | 周一-周五 13:50 | 规则打分 + LLM 复判，筛选尾盘买入 |
| 盘前风控 | 周一-周五 08:20 | A50 + VIX 预警 |
| 涨停复盘 | 周一-周五 19:25 | 当日涨幅 ≥ 8% 复盘 |
| 推荐跟踪重定价 | 周日-周四 23:00 | 同步收盘价 |
| 回测网格 | 每月 1/15 日 04:00 | 18 并行参数格 → 聚合通知 |
| 缓存维护 | 每天 23:05 | 清理过期行情缓存 |

## 模型支持

**CLI**：Gemini / Claude / OpenAI，`/model` 一键切换，支持任意 OpenAI 兼容端点。

**Web / Pipeline**：Gemini / OpenAI / DeepSeek / Qwen / Kimi / 智谱 / 火山引擎 / Minimax，共 8 家。

## 配置

**零配置即可使用** — 启动后 `/model` 添加任意 LLM API Key 即可对话。持仓数据自动存本地。

进阶配置（`.env` 文件或 GitHub Actions Secrets）：

| 变量 | 说明 | 是否必须 |
|------|------|---------|
| LLM API Key | `/model add` 交互式配置即可 | ✅ |
| `TUSHARE_TOKEN` | 股票行情数据（`/config set tushare_token`） | ✅ |
| `SUPABASE_URL` / `SUPABASE_KEY` | 云端持仓同步（多设备共享） | 可选 |
| `TICKFLOW_API_KEY` | TickFlow 实时/分时数据 | 可选 |
| `FEISHU_WEBHOOK_URL` | 飞书推送 | 可选 |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Telegram 推送 | 可选 |

> Tushare 注册推荐：[此链接注册](https://tushare.pro/weborder/#/login?reg=955650)，双方可提升数据权益。

完整配置项和 GitHub Actions Secrets 说明见 [架构文档](docs/ARCHITECTURE.md)。

## MCP Server

将 Wyckoff 分析能力通过 [MCP 协议](https://modelcontextprotocol.io/) 对外暴露，让 Claude Code / Cursor / 任何 MCP Client 直接调用诊股、筛选、回测等工具。

```bash
# 安装 MCP 依赖
uv pip install youngcan-wyckoff-analysis[mcp]

# 注册到 Claude Code
claude mcp add wyckoff -- wyckoff-mcp
```

或在 MCP Client 配置文件中手动添加：

```json
{
  "mcpServers": {
    "wyckoff": {
      "command": "wyckoff-mcp",
      "env": {
        "TUSHARE_TOKEN": "your_token",
        "TICKFLOW_API_KEY": "your_key"
      }
    }
  }
}
```

注册后在 Claude Code / Cursor 中直接问"帮我诊断 000001"即可调用 Wyckoff 工具。

## Wyckoff Skills

轻量复用威科夫分析能力：[`YoungCan-Wang/wyckoff_skill`](https://github.com/YoungCan-Wang/wyckoff_skill.git)

适合给 AI 助手快速挂载一套"威科夫视角"。

## 交流

| 飞书群 | 飞书个人 |
|:---:|:---:|
| <img src="attach/飞书群二维码.png" width="200" /> | <img src="attach/飞书个人二维码.png" width="200" /> |

## 赞助

觉得有帮助？给个 Star。赚到钱了？请作者吃个汉堡。

| 支付宝 | 微信 |
|:---:|:---:|
| <img src="attach/支付宝收款码.jpg" width="200" /> | <img src="attach/微信收款码.png" width="200" /> |

## 风险提示

> **本工具基于历史量价结构发现潜力标的，过去表现不代表未来收益，所有筛选、推荐、回测结果均不构成任何投资建议。投资有风险，决策需自主。**

## License

[AGPL-3.0](LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/Wyckoff-Analysis&type=Date)](https://star-history.com/#YoungCan-Wang/Wyckoff-Analysis&Date)
