<div align="center">

# Wyckoff Trading Agent

**Wyckoff Volume-Price Analysis Agent for China A-Shares — Talk to it like a human, it reads the tape**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](../LICENSE)
[![Web App](https://img.shields.io/badge/Web-React%20App-0ea5e9.svg)](https://wyckoff-analysis.pages.dev/home)
[![Streamlit](https://img.shields.io/badge/Streamlit-FF4B4B.svg)](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)

[中文](../README.md) | [日本語](README_JA.md) | [Español](README_ES.md) | [한국어](README_KO.md) | [Architecture](ARCHITECTURE.md)

</div>

---

Talk to a Wyckoff master in natural language. He commands 10 professional tools + 5 general capabilities, chains multi-step reasoning, and tells you whether to strike.

Web + CLI + MCP triple channel, Gemini / Claude / OpenAI / DeepSeek multi-model switching, GitHub Actions for fully automated daily runs.

## Features

| Capability | Description |
|---|---|
| Conversational Agent | Trigger diagnosis, screening, and reports in plain language; the LLM orchestrates tools autonomously; also reads/writes files, executes commands, and fetches web pages |
| Skills | Built-in slash commands (`/screen`, `/checkup`, `/report`, `/strategy`, `/backtest`) for one-tap complex workflows; user-extensible via `~/.wyckoff/skills/*.md` |
| Five-Layer Funnel | Full market ~4 500 stocks -> ~30 candidates via six channels + sector resonance + micro triggers |
| AI Three-Camp Report | Logic Bankrupt / Reserve Camp / Springboard — LLM renders an independent verdict |
| Portfolio Diagnosis | Batch health check: MA structure, accumulation phase, trigger signals, stop-loss status |
| Private Rebalance | Synthesizes holdings + candidates, outputs EXIT / TRIM / HOLD / PROBE / ATTACK orders, pushes to Telegram |
| Tail-Buy Strategy | Executes at 13:50, two-stage evaluation (rule scoring + LLM review) for end-of-day entries |
| Signal Confirmation Pool | L4 trigger signals must pass 1-3 day price confirmation before becoming actionable |
| Recommendation Tracking | Historical picks auto-sync closing prices and compute cumulative returns |
| Daily-Bar Backtest | Replays post-funnel N-day returns; reports win rate / Sharpe / max drawdown |
| Pre-Market Risk | A50 futures + VIX monitoring with four alert levels |
| Local Dashboard | `wyckoff dashboard` — recommendations, signals, portfolio, agent memory, chat logs; dark/light theme, bilingual CN/EN |
| Agent Memory | Cross-session memory: auto-extracts session conclusions, injects relevant context on next query |
| Context Compaction | Dynamic threshold (25% of model context window) auto-compresses long conversations, smart tool result summarization preserves key data |
| Tool Confirmation | `exec_command`, `write_file`, `update_portfolio` require user approval before execution |
| General Agent Capabilities | Execute commands, read/write files, fetch web pages — send a CSV path and it will analyze it |
| MCP Server | 10 tools exposed via MCP protocol — plug into Claude Code / Cursor / any MCP client |
| Multi-Channel Notifications | Feishu / WeCom / DingTalk / Telegram |

## Data Sources

Daily bar auto-fallback chain:

```
tickflow → tushare → akshare → baostock → efinance
```

When any source is unavailable the system silently falls back to the next — zero intervention required.

> **Recommended: connect TickFlow for stronger real-time / intraday capabilities**
> Register: [TickFlow Registration](https://tickflow.org/auth/register?ref=5N4NKTCPL4)

## Quick Start

### One-line Install (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/Wyckoff-Analysis/main/install.sh | bash
```

Detects Python, installs uv, creates an isolated environment. Run `wyckoff` when done.

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

### Start Using — One-Click Agent Setup

Just two steps after launch:
1. `/model` — choose a model (Gemini / Claude / OpenAI) and enter your API key
2. Start asking questions — no registration needed, portfolio data stored locally

```
> Compare 000001 and 600519 — which one is the better buy?
> Judge my portfolio
> What's the market temperature right now?
```

> Optional: `/login` to sync portfolio to cloud for multi-device access. All features work without login.

Upgrade: `wyckoff update`

### Backtest Grid

18 parameter combos run in parallel, outputs optimal parameters, Sharpe matrix, and strategy health check:

| Optimal Params & Ranking | Parameter Matrix |
|:---:|:---:|
| <img src="../attach/backtest-grid-1.png" width="450" /> | <img src="../attach/backtest-grid-2.png" width="450" /> |

### Web

```bash
git clone https://github.com/YoungCan-Wang/Wyckoff-Analysis.git
cd Wyckoff-Analysis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Web App: **[wyckoff-analysis.pages.dev](https://wyckoff-analysis.pages.dev/home)**

Streamlit: **[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

## Tools

The agent's arsenal — 10 quant tools + 5 general capabilities:

| Tool | Capability |
|---|---|
| `search_stock_by_name` | Fuzzy search by name, ticker, or pinyin |
| `analyze_stock` | Wyckoff diagnosis / recent OHLCV quotes (mode switch) |
| `portfolio` | View holdings / batch portfolio health scan (mode switch) |
| `update_portfolio` | Add / modify / delete holdings, set available cash, delete tracking records |
| `get_market_overview` | Broad market temperature overview |
| `screen_stocks` | Five-layer funnel full-market screening (⚡background) |
| `generate_ai_report` | Three-camp AI deep research report (⚡background) |
| `generate_strategy_decision` | Hold/exit existing positions + new buy decisions (⚡background) |
| `query_history` | Historical recommendations / signal pool / tail-buy records |
| `run_backtest` | Funnel strategy historical backtest (⚡background) |
| `check_background_tasks` | Background task progress query |
| `exec_command` | Execute local shell commands |
| `read_file` | Read local files (CSV/Excel auto-parsed) |
| `write_file` | Write files (export reports/data) |
| `web_fetch` | Fetch web content (financial news/announcements) |

Tool call order and frequency are decided by the LLM at runtime — no pre-choreography needed. Send a CSV path and it reads it; say "install a package" and it executes.

## Five-Layer Funnel

| Layer | Name | What It Does |
|---|---|---|
| L1 | Garbage Filter | Remove ST / BSE / STAR Market; market cap >= 3.5 B CNY; avg daily turnover >= 50 M CNY |
| L2 | Six-Channel Selection | Rally / Ignition / Stealth / Accumulation / Dry Volume / Support |
| L3 | Sector Resonance | Top-N industry distribution filter |
| L4 | Micro Triggers | Spring / LPS / SOS / EVR — four trigger signals |
| L5 | AI Verdict | LLM three-camp classification: Logic Bankrupt / Reserve / Springboard |

## Daily Automation

Built-in GitHub Actions cron jobs:

| Task | Schedule (Beijing Time) | Description |
|---|---|---|
| Funnel + AI Report + Rebalance | Sun–Thu 18:25 | Fully automated; results pushed to Feishu / Telegram |
| Tail-Buy Strategy | Mon–Fri 13:50 | Rule scoring + LLM review, end-of-day entry screening |
| Pre-Market Risk | Mon–Fri 08:20 | A50 + VIX alert |
| Limit-Up Review | Mon–Fri 19:25 | Review stocks that rose >= 8% today |
| Recommendation Reprice | Sun–Thu 23:00 | Sync closing prices |
| Backtest Grid | 1st & 15th monthly 04:00 | 18 parallel parameter combos → aggregated report |
| DB Maintenance | Daily 23:05 | Purge stale quotes, orders, signals, market signals, and other rolling-window data |

## Model Support

**CLI**: Gemini / Claude / OpenAI — switch with `/model`; any OpenAI-compatible endpoint works (DeepSeek, Qwen, Kimi, etc.).

**Web / Pipeline**: 1Route / Gemini / OpenAI / Zhipu / Minimax / DeepSeek / Qwen / Volcengine. Kimi and other OpenAI-compatible providers can be configured via custom `base_url` / `custom_providers`.

## Configuration

**Zero config to get started** — just launch and `/model add` any LLM API key. Portfolio data is stored locally by default.

Advanced configuration (`.env` file or GitHub Actions Secrets):

| Variable | Purpose | Required? |
|---|---|---|
| LLM API Key | Configure via `/model add` interactively | Yes |
| `TUSHARE_TOKEN` | Stock market data (`/config set tushare_token`) | Yes |
| `SUPABASE_URL` / `SUPABASE_KEY` | Cloud portfolio sync (multi-device) | Optional |
| `TICKFLOW_API_KEY` | TickFlow real-time / intraday data | Optional |
| `FEISHU_WEBHOOK_URL` | Feishu push notifications | Optional |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Telegram push notifications | Optional |

See the [Architecture doc](ARCHITECTURE.md) for the full config reference and GitHub Actions Secrets setup.

## MCP Server

Expose Wyckoff analysis capabilities via the [MCP protocol](https://modelcontextprotocol.io/), enabling Claude Code / Cursor / any MCP client to call 10 tools directly.

```bash
# Install MCP dependency
uv pip install youngcan-wyckoff-analysis[mcp]

# Register with Claude Code
claude mcp add wyckoff -- wyckoff-mcp
```

Or add manually in your MCP client config:

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

Once registered, just ask "diagnose 000001" in Claude Code / Cursor to invoke Wyckoff tools.

## Wyckoff Skills

Lightweight reuse of the Wyckoff analysis capability: [`YoungCan-Wang/wyckoff_skill`](https://github.com/YoungCan-Wang/wyckoff_skill.git)

Ideal for giving any AI assistant a quick "Wyckoff lens."

## Disclaimer

> **This tool identifies potential based on historical volume-price patterns. Past performance does not guarantee future results. All screening, recommendation, and backtest outputs do not constitute investment advice. Invest at your own risk.**

## License

[AGPL-3.0](../LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/Wyckoff-Analysis&type=Date)](https://star-history.com/#YoungCan-Wang/Wyckoff-Analysis&Date)
