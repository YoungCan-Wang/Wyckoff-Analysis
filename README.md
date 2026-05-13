<div align="center">

# WyckoffAgent — Open-Source Wyckoff Trading Agent

**A 股 / 港股 / 美股威科夫量价分析智能体 — 你说人话，他读盘面。**

[![PyPI](https://img.shields.io/pypi/v/youngcan-wyckoff-analysis?color=blue)](https://pypi.org/project/youngcan-wyckoff-analysis/)
[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![Web App](https://img.shields.io/badge/Web-React%20App-0ea5e9.svg)](https://wyckoff-analysis.pages.dev/)
[![Homepage](https://img.shields.io/badge/homepage-Wyckoff%20Homepage-0ea5e9.svg)](https://youngcan-wang.github.io/wyckoff-homepage/)

[English](docs/README_EN.md) | [日本語](docs/README_JA.md) | [Español](docs/README_ES.md) | [한국어](docs/README_KO.md) | [架构文档](docs/ARCHITECTURE.md)

</div>

---

用自然语言和一位威科夫大师对话。系统把 A 股日线行情、威科夫结构识别、AI 研报、持仓风控、形态复盘和通知推送串成一条自动化链路，并已扩展支持港股与美股漏斗扫描。

React Web、CLI、MCP 与 GitHub Actions 共同组成当前产品形态；行情优先复用 Supabase 缓存，缺口再回源补拉并回写。

> Risk disclosure: WyckoffAgent is for educational, research, and informational use. It does not provide investment advice, does not account for every personal financial circumstance, and does not guarantee future performance.

---

## Operating Cost Transparency

从 **2026-06-03** 起，WyckoffAgent 按付费基础设施运行：行情源、数据库、AI 报告、在线分析服务和自动化维护都会进入显性成本模型。

<p align="center">
  <img src="docs/screenshots/supabase-quota-grace-2026-06-03.svg" alt="Supabase quota grace period until 03 Jun, 2026" width="900" />
</p>

公开成本模型见 [docs/COST_MODEL.md](docs/COST_MODEL.md)。

---

## Special Thanks

<table>
  <tr>
    <td width="150" align="center">
      <a href="https://tickflow.org/auth/register?ref=5N4NKTCPL4">
        <img src="attach/tickflow-logo.png" alt="TickFlow" width="120" />
      </a>
    </td>
    <td>
      <strong><a href="https://tickflow.org/auth/register?ref=5N4NKTCPL4">TickFlow</a></strong><br />
      感谢 TickFlow 为 WyckoffAgent 提供高质量 A 股 / 美股 / 港股行情数据能力支持。
    </td>
  </tr>
  <tr>
    <td width="150" align="center">
      <strong><a href="https://github.com/waditu/czsc">CZSC</a></strong><br />
      <sub>缠中说禅</sub>
    </td>
    <td>
      <strong><a href="https://github.com/waditu/czsc">缠中说禅（CZSC）</a></strong><br />
      感谢顶级交易开源项目缠中说禅（CZSC）的作者 <a href="https://github.com/zengbin93">zengbin93</a> 在交易策略上的指导与点拨。
    </td>
  </tr>
</table>

---

## 快速开始

### CLI（推荐）

```bash
# 一键安装
curl -fsSL https://raw.githubusercontent.com/YoungCan-Wang/WyckoffTradingAgent/main/install.sh | bash

# 或 Homebrew / pip
brew tap YoungCan-Wang/wyckoff && brew install wyckoff
uv pip install youngcan-wyckoff-analysis
```

```bash
wyckoff          # 启动 Agent 对话
wyckoff dashboard  # 启动本地可视化面板
```

启动后 `/model` 选择模型（Gemini / Claude / OpenAI），输入 API Key 即可对话。

<p align="center">
  <img src="attach/cli-home.png" alt="CLI 启动界面" width="900" />
</p>

<details>
<summary><strong>展开更多 CLI 截图</strong></summary>

| 持仓查询 | 诊断报告 | 操作指令 |
|:---:|:---:|:---:|
| <img src="attach/cli-running.png" width="300" /> | <img src="attach/cli-analysis.png" width="300" /> | <img src="attach/cli-result.png" width="300" /> |

</details>

### Web App

在线地址：**[wyckoff-analysis.pages.dev](https://wyckoff-analysis.pages.dev/)**

<p align="center">
  <img src="docs/screenshots/web-chat.png" alt="Web 读盘室" width="900" />
</p>

<details>
<summary><strong>展开更多 Web App 截图</strong></summary>

| 漏斗选股 | 形态复盘 |
|:---:|:---:|
| <img src="docs/screenshots/web-screen.png" width="450" /> | <img src="docs/screenshots/web-track.png" width="450" /> |

| 持仓管理 | 单股分析（脱敏样例） |
|:---:|:---:|
| <img src="docs/screenshots/web-portfolio.png" width="450" /> | <img src="docs/screenshots/web-analysis-redacted.png" width="450" /> |

</details>

### Streamlit（维护入口）

在线地址：**[wyckoff-analysis-youngcanphoenix.streamlit.app](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)**

<p align="center">
  <img src="attach/demo/streamlit-chat.png" alt="Streamlit 读盘室" width="900" />
</p>

<details>
<summary><strong>展开更多 Streamlit 截图</strong></summary>

| 数据导出 |
|:---:|
| <img src="attach/demo/streamlit-export.png" width="450" /> |

</details>

### 本地可视化面板（Dashboard）

```bash
wyckoff dashboard
```

<p align="center">
  <img src="attach/demo/dashboard-overview-new.png" alt="Dashboard 总览" width="900" />
</p>

<details>
<summary><strong>展开更多 Dashboard 截图</strong></summary>

| 形态复盘 | 信号池 | 尾盘记录 |
|:---:|:---:|:---:|
| <img src="attach/demo/dashboard-recommendations.png" width="300" /> | <img src="attach/demo/dashboard-signals.png" width="300" /> | <img src="attach/demo/dashboard-tail-buy.png" width="300" /> |

| 持仓 | Agent 记忆 | 后台任务 |
|:---:|:---:|:---:|
| <img src="attach/demo/dashboard-portfolio.png" width="300" /> | <img src="attach/demo/dashboard-memory.png" width="300" /> | <img src="attach/demo/dashboard-bgtasks.png" width="300" /> |

| 对话日志 | 同步状态 | 对话日志详情（Trace） |
|:---:|:---:|:---:|
| <img src="attach/demo/dashboard-chatlog-new.png" width="300" /> | <img src="attach/demo/dashboard-sync.png" width="300" /> | <img src="attach/demo/dashboard-chatlog-detail-content.png" width="300" /> |

</details>

### 回测网格

<p align="center">
  <img src="attach/backtest-grid-1.png" alt="回测网格最优参数与梯队表" width="900" />
</p>

<details>
<summary><strong>展开更多回测截图</strong></summary>

| 参数矩阵 |
|:---:|
| <img src="attach/backtest-grid-2.png" width="450" /> |

</details>

---

## 功能亮点

- **对话式 Agent** — 用自然语言触发诊断、筛选、研报，LLM 自主编排 15 个工具
- **五层漏斗筛选** — A 股全市场约 4500 股，港股 / 美股独立 universe 扫描（六通道 + 板块共振 + 微观狙击 + AI 审判）
- **跨市场** — A 股 / 港股 / 美股漏斗独立 workflow
- **AI 三阵营研报** — 逻辑破产 / 储备营地 / 起跳板，LLM 独立审判
- **持仓诊断 & 私人决断** — 批量体检 + EXIT/TRIM/HOLD/PROBE/ATTACK 指令
- **Agent 记忆** — FTS5 全文检索 + 时间衰减混合召回，跨会话记忆
- **Skills 扩展** — 内置 `/screen`、`/checkup`、`/report`、`/backtest`，用户可自定义
- **MCP Server** — 10 个工具通过 MCP 协议对外暴露，Claude Code / Cursor 即插即用
- **多通道推送** — 飞书 / 企微 / 钉钉 / Telegram
- **本地面板** — `wyckoff dashboard` 一条命令启动可视化

---

## 演示视频

<details>
<summary><strong>「从0到1读盘」Web 全流程（读盘室→设置）</strong></summary>

<img src="attach/demo/web-demo.gif" width="900" />

</details>

<details>
<summary><strong>「终端党最爱」CLI 流程（启动→执行→结果）</strong></summary>

<img src="attach/demo/cli-demo.gif" width="900" />

</details>

---

## 文档导航

| 想了解 | 去哪里看 |
|--------|----------|
| 架构、Actions、数据表、缓存 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| 运营成本、规模化预算 | [docs/COST_MODEL.md](docs/COST_MODEL.md) |
| 漏斗、AI 研报、OMS、回测 | [README_STRATEGY.md](README_STRATEGY.md) |
| 术语速查 | [GLOSSARY.md](GLOSSARY.md) |
| MCP Server 配置 | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#mcp-server) |

> **Wiki 深度阅读**：[交易方法论 Wiki](https://github.com/YoungCan-Wang/WyckoffTradingAgent/wiki/01_Finance_Wyckoff_Method) ｜ [技术架构 Wiki](https://github.com/YoungCan-Wang/WyckoffTradingAgent/wiki/03_Tech_Architecture)

---

## 配置

**零配置即可使用** — 启动后 `/model` 添加 LLM API Key 即可对话。

进阶配置见 [架构文档](docs/ARCHITECTURE.md)。

> 数据源购买：[TickFlow →](https://tickflow.org/auth/register?ref=5N4NKTCPL4) ｜ 大模型购买：[1Route →](https://www.1route.dev/register?aff=359904261)

---

## 交流

| 飞书群 | QQ群 | 飞书个人 |
|:---:|:---:|:---:|
| <img src="attach/飞书群二维码.png" width="200" /> | <img src="attach/QQ群二维码.jpg" width="200" /><br/>群号: 761348919 | <img src="attach/飞书个人二维码.png" width="200" /> |

## 赞助

觉得有帮助？给个 Star。赚到钱了？请作者吃个汉堡。

| 支付宝 | 微信 |
|:---:|:---:|
| <img src="attach/支付宝收款码.jpg" width="200" /> | <img src="attach/微信收款码.png" width="200" /> |

## License

[AGPL-3.0](LICENSE) &copy; 2024-2026 youngcan

---

[![Star History Chart](https://api.star-history.com/svg?repos=YoungCan-Wang/WyckoffTradingAgent&type=Date)](https://star-history.com/#YoungCan-Wang/WyckoffTradingAgent&Date)
