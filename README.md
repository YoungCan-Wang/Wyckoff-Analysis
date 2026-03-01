# A 股选股与行情工具

> 每天从全市场筛选出**最值得次日操作的 6 只股票**，并生成 AI 研报；同时支持行情数据查询与 CSV 导出。

用 [akshare](https://github.com/akfamily/akshare) + 自研 Wyckoff 漏斗做量化初筛，再用大模型做深度分析。适合想快速拿到每日「明日可买清单」的 A 股投资者。

**在线体验：** [https://wyckoff-analysis-youngcanphoenix.streamlit.app/](https://wyckoff-analysis-youngcanphoenix.streamlit.app/)

---

## 你能做什么

| 功能 | 说明 |
|------|------|
| 📊 **每日选股** | 配置 GitHub Actions 后，工作日 16:30 自动跑 Wyckoff Funnel，从主板+创业板筛出 6 只，推送到飞书 |
| 🔬 **Wyckoff Funnel** | 4 层漏斗筛选：剥离垃圾 → 强弱甄别 → 板块共振 → 威科夫狙击 |
| 🤖 **AI 研报** | 对筛选结果生成观察池 + 操作池，含技术结构、阻力位、交易计划等 |
| 🎓 **AI 分析（大师模式）** | "Alpha"虚拟投委会，七位历史级交易大师人格（利弗莫尔/威科夫/缠论/彼得林奇等）多维分析 |
| 🕶️ **私人决断** | 结合个人持仓与外部候选，生成 Buy/Hold/Sell 私密指令，并通过 Telegram 单独发送 |
| 🛡️ **RAG 防雷** | 基于新闻检索自动过滤有负面舆情的股票（立案/调查/减持/业绩预亏等） |
| 📁 **行情导出** | Web 或命令行拉取指定股票日线，导出原始/增强两份 CSV（OHLCV 开高低收量等） |
| 🧰 **自定义导出** | 支持 A股/指数/ETF/宏观 CPI 等多数据源，灵活配置参数 |
| 📈 **持仓管理** | 实时同步持仓至云端，记录成本价、买入日期、策略标签 |
| 🕘 **下载历史** | 查看历史下载记录（最近 20 条） |
| 🔐 **登录与配置** | 支持登录、飞书 Webhook、API Key 云端同步 |

---

## 🚀 快速开始

### 1. 环境

需要 **Python 3.10+**。

```bash
cd akshare
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入：

- `SUPABASE_URL`、`SUPABASE_KEY`：登录用（项目依赖 Supabase）
- `FEISHU_WEBHOOK_URL`（可选）：飞书推送地址

### 3. 运行

**Web 界面（推荐）**：浏览器里查数据、下 CSV。

```bash
streamlit run streamlit_app.py
```

**命令行**：批量导出。

```bash
python -m integrations.fetch_a_share_csv --symbol 300364
python -u -m integrations.fetch_a_share_csv --symbols 000973 600798 601390
```

---

## 📅 每日选股（Wyckoff Funnel）

从全市场（主板 + 创业板）多轮过滤，最终输出**最值得次日操作的 6 只股票**，并生成 AI 研报，推送到飞书。

### 4 层漏斗筛选逻辑

| 层级 | 名称 | 筛选逻辑 |
|------|------|----------|
| Layer 1 | **剥离垃圾** | 剔除 ST/北交所/科创板，保留市值 ≥ 20 亿、日均成交额 ≥ 5000 万的股票 |
| Layer 2 | **强弱甄别** | MA50 > MA200（多头排列），或大盘连跌时守住 MA20；相对强度 RS 筛选 |
| Layer 3 | **板块共振** | 行业分布 Top-N，筛选与热门板块共振的标的 |
| Layer 4 | **威科夫狙击** | Spring（终极震仓）、LPS（缩量回踩）、Effort vs Result（放量不跌） |

### 启用方式

仓库内置工作流：`.github/workflows/wyckoff_funnel.yml`

- **定时**：工作日 16:30（北京时间）
- **手动**：Actions 页面选择 `Wyckoff Funnel` → `Run workflow`

### 配置 GitHub Secrets

`Settings` → `Secrets and variables` → `Actions`，添加：

| 名称 | 必填 | 说明 |
|------|------|------|
| `FEISHU_WEBHOOK_URL` | 是 | 接收选股结果与研报 |
| `GEMINI_API_KEY` | 是 | AI 研报 |
| `TUSHARE_TOKEN` | 是 | 行情与市值数据 |
| `GEMINI_MODEL` | 否 | 未配则用默认模型 |
| `MY_PORTFOLIO_STATE` | Step4 用 | 可选，格式见 `.env.example` |
| `TG_BOT_TOKEN` | Step4 用 | 可选 |
| `TG_CHAT_ID` | Step4 用 | 可选 |

### 验证

跑一次手动触发后，检查：

- 日志中有 `阶段汇总` 且 `ok=True`
- Artifacts 中有 `daily-job-logs-*`
- 飞书收到漏斗结果 + 研报

常规跑完约 90～130 分钟。报错时看日志里缺哪个配置。

### 常见报错

- `配置缺失: FEISHU_WEBHOOK_URL`
  - 原因：未配置飞书 Secret
  - 处理：在仓库 Secrets 添加 `FEISHU_WEBHOOK_URL`
- `配置缺失: GEMINI_API_KEY`
  - 原因：未配置模型 Key 或已失效
  - 处理：更新 `GEMINI_API_KEY` 后重跑
- `市值数据为空（TUSHARE_TOKEN 可能缺失/失效）`
  - 原因：`TUSHARE_TOKEN` 缺失/失效/额度问题
  - 处理：检查并更新 `TUSHARE_TOKEN`，确认账号权限
- `[step3] 模型 ... 失败` / `llm_failed`
  - 原因：模型不可用、限流、网络抖动
  - 处理：更换 `GEMINI_MODEL` 或稍后重试
- `[step3] 飞书推送失败` / `feishu_failed`
  - 原因：Webhook 无效、限流、网络问题
  - 处理：重新生成飞书机器人 Webhook 并替换 Secret
- `阶段 3 私人再平衡: 跳过（MY_PORTFOLIO_STATE 未配置）`
  - 原因：未配置账户状态
  - 处理：配置 `MY_PORTFOLIO_STATE` 后重跑
- `阶段 3 私人再平衡: 跳过（TG_BOT_TOKEN/TG_CHAT_ID 未配置）`
  - 原因：未配置 Telegram
  - 处理：配置 Telegram Secrets 后重跑
- `User location is not supported for the API use`
  - 原因：模型地域限制
  - 处理：更换可用网络出口或供应商
- `Action 超时或明显慢于 2 小时`
  - 原因：数据源抖动、重试变多
  - 处理：查看批次日志定位卡点，必要时手动重跑

### 私人决断（可选）

若配置了持仓和 Telegram，选股和研报跑完后会单独发一份「针对你账户的买卖建议」到你的 Telegram，只有你能看到。不配则跳过，不影响选股和研报。

账户格式见 `.env.example`。其中 `total_equity` 是可选字段，不填会自动按“`free_cash + 持仓最新市值`”推导。TG_CHAT_ID 需先给 bot 发 `/start`，再从 getUpdates 返回里取 chat.id。

---

## 交流

### 飞书群二维码

![飞书群二维码](attach/飞书群二维码.png)

### 飞书个人二维码

![飞书个人二维码](attach/飞书个人二维码.png)

---

## 附录

### 目录结构

```text
.
├── streamlit_app.py        # Web 入口
├── app/                    # UI 组件（layout/auth/navigation）
├── core/                   # 核心策略与领域逻辑
│   ├── wyckoff_engine.py   # Wyckoff Funnel 4层漏斗引擎
│   ├── wyckoff_single_prompt.py  # 单股分析 Prompt
│   ├── single_stock_logic.py    # 单股分析页面逻辑
│   ├── download_history.py      # 下载历史记录
│   ├── stock_cache.py            # 股票数据缓存
│   └── constants.py              # 常量定义
├── integrations/           # 数据源/LLM/Supabase 适配层
│   ├── data_source.py      # 统一数据源（自动降级）
│   ├── fetch_a_share_csv.py  # CSV 导出模块
│   ├── llm_client.py      # LLM 客户端
│   ├── ai_prompts.py      # AI 提示词（Alpha 投委会）
│   ├── supabase_client.py # Supabase 云端同步
│   ├── rag_veto.py        # RAG 防雷模块
│   └── feishu.py          # 飞书推送
├── pages/                  # Streamlit 页面
│   ├── AIAnalysis.py      # AI 分析（大师模式）
│   ├── WyckoffScreeners.py # Wyckoff Funnel 筛选页
│   ├── Portfolio.py       # 持仓管理
│   ├── CustomExport.py    # 自定义导出
│   ├── DownloadHistory.py # 下载历史
│   ├── Settings.py        # 设置
│   └── Changelog.py       # 版本更新日志
├── scripts/
│   ├── wyckoff_funnel.py  # 定时选股任务
│   ├── step3_batch_report.py  # AI 研报
│   ├── step4_rebalancer.py    # 私人决断
│   ├── daily_job.py      # 日终流水线
│   └── benchmark_funnel_fetch.py  # 取数性能基准测试
├── requirements.txt
└── .env.example
```

### 交易日与时间窗口

按交易日计算，自动跳过周末与节假日。

- 结束日：系统日期 - 1 天，对齐到最近交易日
- 开始日：从结束日向前回溯 N 个交易日（默认 500）
- 参数：`--trading-days`、`--end-offset-days`

### CSV 输出说明

- **hist_data.csv**：akshare 原始字段（日期、开高低收、成交量、成交额、振幅、换手率等）
- **ohlcv.csv**：增强版（OHLCV + 均价、行业），便于回测与可视化

### 复权

`--adjust`：`""` 不复权，`qfq` 前复权，`hfq` 后复权。

```bash
python -m integrations.fetch_a_share_csv --symbol 300364 --adjust qfq
```

### 常见问题

- **ImportError: create_client from supabase** → `pip install supabase>=2.0.0`
- **macOS pip externally-managed-environment** → 用虚拟环境安装依赖
- **文件名有空格** → 股票名本身带空格，脚本会做安全替换

### AI 分析：Alpha 虚拟投委会

Web 端提供「AI 分析」页面，采用「Alpha」虚拟投委会模式，由七位历史级交易大师人格共同决策：

| 大师 | 职责 |
|------|------|
| 🌊 **道氏与艾略特** | 宏观定势，判断牛市/熊市主趋势 |
| 💰 **彼得·林奇** | 价值透视，PEG 估值，六大股票分类 |
| 🕵️ **理查德·威科夫** | 主力侦察，吸筹/派发阶段判断，供需法则 |
| 📐 **缠中说禅** | 结构精算，中枢、背驰、买卖点数学定位 |
| 🔥 **情绪流龙头战法** | 情绪博弈，周期定位，龙头识别 |
| 🐊 **杰西·利弗莫尔** | 关键时机，关键点突破确认 |
| 🕯️ **史蒂夫·尼森** | 微观信号，蜡烛图反转形态 |

### RAG 防雷（负面舆情过滤）

每日选股流程中集成 RAG 防雷模块，自动过滤有负面舆情的股票：

- **数据源**：Tavily 新闻搜索（如未配置则跳过）
- **默认负面关键词**：立案、调查、证监会、处罚、违规、造假、退市、减持、质押爆仓、债务违约、业绩预亏、业绩下滑、商誉减值、诉讼、仲裁、冻结等
- **配置**：`TAVILY_API_KEY` 环境变量

### 数据源降级机制

个股数据获取失败时自动降级：

```
akshare → baostock → efinance → tushare
```

指数数据优先使用 tushare 直连，确保稳定性。

### 自定义导出支持的数据源

| 数据源 | 说明 | 复权支持 |
|--------|------|----------|
| A股个股历史 | 日线 K 线，6 位代码 | ✅ |
| 指数历史 | 上证/深证/创业板/北证 | ❌ |
| ETF 历史 | 510300 / 159707 等 | ✅ |
| 宏观 CPI | 月度 CPI 指标 | ❌ |

### Fork 与部署

1. Fork 本仓库，克隆到本地
2. 按上文配置 `.env` 后运行
3. 部署到 [Streamlit Cloud](https://share.streamlit.io/)：入口选 `streamlit_app.py`，Secrets 配 `SUPABASE_URL`、`SUPABASE_KEY`、`COOKIE_SECRET`

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://share.streamlit.io/)

---

## 版本更新

详见 [`CHANGELOG.md`](CHANGELOG.md)。Web 端「版本更新日志」页面直接读取该文件。

---

**版权声明 | Copyright & License**

- **版权所有** © 2024 youngcan. All Rights Reserved.
- **开源用途**：个人学习研究免费使用，署名即可
- **商业授权**：如需将本项目用于商业产品或服务（包括但不限于 SaaS、付费咨询、代客选股、量化基金、OEM/嵌入式等），**必须先联系作者获得授权并支付授权费用**
- **联系方式**：可通过微信/飞书，或 GitHub Issue 联系

---

> ⚠️ 未经授权的商业使用将被视为侵权，作者保留追究法律责任的权利。
