# A 股历史行情 CSV 导出脚本（akshare）

> **Context for AI Agents:** This project is a Python-based tool for fetching and exporting Chinese A-Share stock data. It uses `akshare` for data, `streamlit` for the UI, and `supabase` for authentication.

用 Python + [akshare](https://github.com/akfamily/akshare) 拉取指定 A 股近 N 个交易日的日线数据，并生成两份 CSV：

- `{股票代码}_{股票名}_hist_data.csv`：akshare 返回的原始字段
- `{股票代码}_{股票名}_ohlcv.csv`：面向分析的增强 OHLCV（含成交额/换手率/振幅/均价/行业）

示例：

- `300364_中文在线_hist_data.csv`
- `300364_中文在线_ohlcv.csv`

---

## 目录结构

```text
.
├── fetch_a_share_csv.py    # 核心逻辑：获取数据、处理数据、生成 CSV
├── streamlit_app.py        # Web UI 入口
├── supabase_client.py      # Supabase 客户端配置
├── auth_component.py       # 登录/注册组件
├── requirements.txt        # 依赖列表
└── .env.example            # 环境变量示例
```

---

## ✨ 功能特性 (Features)

- 📊 **多维数据导出**: 支持原始行情 (Hist Data) 与 增强型 OHLCV (含换手率/振幅/板块) 双份导出。
- 🖥️ **可视化交互**: 基于 Streamlit 的 Web 界面，支持移动端适配。
- 🔐 **用户系统**: 集成 Supabase Auth，支持登录/注册与配置云端同步 (RLS 安全隔离)。
- 🤖 **通知推送**: 支持飞书 Webhook 消息推送批量下载状态。
- ⚡️ **批量处理**: 支持单只/批量股票代码解析与导出 (.zip 打包)。
- 📝 **历史记录**: 自动记录最近查询与批量下载任务。

---

## 🚀 快速开始 (AI & Humans)

### 1. 环境配置

需要 **Python 3.10+**。

```bash
# 1. 进入目录
cd akshare

# 2. 创建虚拟环境
python3 -m venv .venv

# 3. 激活虚拟环境
# macOS / Linux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

# 4. 安装依赖
# 注意：必须安装 supabase 库，否则无法运行 Streamlit App
python -m pip install -U pip
python -m pip install -r requirements.txt
```

### 2. 配置文件 (.env)

项目依赖 Supabase 进行用户认证。

1.  复制示例文件：
    ```bash
    cp .env.example .env
    ```
2.  修改 `.env` 文件，填入你的配置：
    *   `SUPABASE_URL`: 你的 Supabase 项目 URL
    *   `SUPABASE_KEY`: 你的 Supabase **anon** Key
    *   `FEISHU_WEBHOOK_URL`: (可选) 飞书机器人 Webhook 地址

### 3. 运行方式

#### 方式 A: Web 可视化界面 (推荐)

直接在浏览器中查询、预览数据并一键下载 CSV。

```bash
# 确保已激活虚拟环境
source .venv/bin/activate

# 启动 Streamlit
streamlit run streamlit_app.py
```
浏览器会自动打开 `http://localhost:8501`。

#### 方式 B: 命令行脚本 (CLI)

适合批量处理或无界面环境。

```bash
# 单只股票
python fetch_a_share_csv.py --symbol 300364

# 多只股票（直接给代码列表）
python -u fetch_a_share_csv.py --symbols 000973 600798 601390

# 多只股票（从混合文本中提取）
python -u fetch_a_share_csv.py --symbols-text '000973 佛塑科技 600798鲁抗医药'
```

---

## 交易日/时间窗口规则（关键）

本脚本按“交易日”计算时间窗口，自动跳过周末与法定节假日。

- 结束日（end）：`系统日期 - 1 天（自然日）`，再对齐到 `<= end` 的最近交易日
- 开始日（start）：从结束交易日向前回溯 `N` 个交易日（默认 50 个交易日，包含结束交易日）

对应参数：

- `--trading-days`：交易日数量（默认 500）
- `--end-offset-days`：结束日的自然日偏移（默认 1）

---

## CSV 字段说明

### 1) hist_data.csv（原始字段）

以 akshare `stock_zh_a_hist` 的返回为准，常见列包括：

- `日期`
- `股票代码`
- `开盘` / `收盘` / `最高` / `最低`
- `成交量` / `成交额`
- `振幅` / `涨跌幅` / `涨跌额` / `换手率`

### 2) ohlcv.csv（标准 OHLCV）

固定列（便于喂给策略回测/可视化工具；列名为英文大驼峰）：

- `Date`：YYYY-MM-DD
- `Open, High, Low, Close`
- `Volume`：成交量（股）
- `Amount`：成交额（金额，数据源口径）
- `TurnoverRate`：换手率（数值，非百分号字符串）
- `Amplitude`：振幅（数值，非百分号字符串）
- `AvgPrice`：`Amount / Volume`（Volume 为 0 时为空）
- `Sector`：行业（来自 `stock_individual_info_em` 的 `行业` 字段，取不到则为空）

---

## 复权说明（前复权/后复权）

参数 `--adjust`：

- `""`：不复权（默认）
- `qfq`：前复权
- `hfq`：后复权

示例：

```bash
python fetch_a_share_csv.py --symbol 300364 --adjust qfq
```

---

## 常见问题

### 1) ImportError: cannot import name 'create_client' from 'supabase'
这是因为未安装 `supabase` 库。请运行：
```bash
pip install supabase>=2.0.0
```

### 2) macOS 上 pip 报 externally-managed-environment
请使用虚拟环境（venv）安装依赖，参考上文“快速开始”。

### 3) 输出文件名里有空格
这是数据源的股票名称本身带空格；脚本会按原样写入文件名（仅替换不允许的文件名字符）。

---

## 部署 (Streamlit Community Cloud)

1. Fork 本仓库到你的 GitHub。
2. 访问 [share.streamlit.io](https://share.streamlit.io/) 并部署。
3. **关键**：在 Streamlit Cloud 的 "Secrets" 设置中配置 `SUPABASE_URL` 和 `SUPABASE_KEY`，格式与 `.env` 文件一致。

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://share.streamlit.io/)
