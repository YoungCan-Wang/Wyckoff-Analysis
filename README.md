# A 股历史行情 CSV 导出脚本（akshare）

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
├── fetch_a_share_csv.py
├── requirements.txt
└── README.md
```

---

## 环境配置

### 1) 依赖

- macOS / Linux / Windows 均可
- Python 3.10+（建议用 venv；macOS Homebrew Python 默认启用 PEP 668，不能直接全局 pip install）

### 2) 创建虚拟环境并安装依赖（推荐）

```bash
cd /path/to/akshare

python3 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip
python -m pip install -r requirements.txt
```

验证 akshare 可用：

```bash
python -c "import akshare as ak; print(ak.__version__)"
```

---

## 运行方式

脚本：[fetch_a_share_csv.py](file:///Users/youngcan/akshare/fetch_a_share_csv.py)

### 1) 单只股票

```bash
source .venv/bin/activate
python fetch_a_share_csv.py --symbol 300364
```

### 2) 多只股票（直接给代码列表）

```bash
source .venv/bin/activate
python -u fetch_a_share_csv.py --symbols 000973 600798 601390 600362 002186 300459 601698 603885
```

### 3) 多只股票（从混合文本中提取 6 位股票代码）

适合“代码+中文名混在一起/甚至没空格”的输入：

```bash
source .venv/bin/activate
python -u fetch_a_share_csv.py --symbols-text '000973 佛塑科技 600798鲁抗医药 601390中国中诶 600362 江西铜业 002186 全聚德 300459 汤姆猫 601698中国卫通603885 吉祥航空'
```

---

## 交易日/时间窗口规则（关键）

本脚本按“交易日”计算时间窗口，自动跳过周末与法定节假日。

- 结束日（end）：`系统日期 - 1 天（自然日）`，再对齐到 `<= end` 的最近交易日
- 开始日（start）：从结束交易日向前回溯 `N` 个交易日（默认 50 个交易日，包含结束交易日）

对应参数：

- `--trading-days`：交易日数量（默认 500）
- `--end-offset-days`：结束日的自然日偏移（默认 1）

示例：结束日改为“系统日期-2天”

```bash
python fetch_a_share_csv.py --symbol 300364 --end-offset-days 2
```

实现方式：使用 `ak.tool_trade_date_hist_sina()` 获取 A 股历史交易日历，再在该日历里定位结束日并回溯 N 个交易日。

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

## 股票基础知识速览（够用版）

### 1) A 股代码与交易所

国内常见是 6 位数字代码，本脚本直接使用 6 位数字（不需要 `sh`/`sz` 前缀），例如：

- `600xxx / 601xxx / 603xxx`：多数为上交所主板（也有科创板 `688xxx`）
- `000xxx / 002xxx`：多数为深交所主板/中小板
- `300xxx`：创业板

### 2) 交易日是什么

“交易日”不是自然日：

- 周六、周日不交易
- 法定节假日/调休等也可能不交易

所以“最近 500 个交易日”必须借助交易日历计算。本脚本已内置该逻辑。

### 3) 名称以数据源为准

你输入的“代码+中文名”只是辅助阅读，脚本会用 `stock_info_a_code_name()` 反查真实名称并用于文件名。

---

## 常见问题

### 1) macOS 上 pip 报 externally-managed-environment（PEP 668）

用 venv 安装依赖即可（见上方“环境配置”）。

### 2) 输出文件名里有空格（比如 `全 聚 德`）

这是数据源的股票名称本身带空格；脚本会按原样写入文件名（仅替换不允许的文件名字符）。

### 3) 网络/数据源不稳定

akshare 依赖公开数据源，偶尔会有失败/限流/字段变动。脚本会逐只股票打印 `OK/FAIL`，失败不会影响其它股票继续导出。

---

## Web 可视化工具 (Streamlit)

我们提供了一个 Web 界面，可以直接在浏览器中查询、预览数据并一键下载 CSV。

### 1) 本地运行

```bash
# 激活虚拟环境
source .venv/bin/activate

# 启动 Streamlit
streamlit run streamlit_app.py
```

浏览器会自动打开 `http://localhost:8501`。

### 2) 公网部署 (Streamlit Community Cloud)

推荐使用官方免费的 Streamlit Community Cloud 进行一键部署：

1. Fork 本仓库到你的 GitHub。
2. 访问 [share.streamlit.io](https://share.streamlit.io/) 并使用 GitHub 登录。
3. 点击 "New app"，选择你的仓库、分支 (main)。
4. 系统会自动识别 `streamlit_app.py`，点击 "Deploy"，等待几分钟即可访问。

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://share.streamlit.io/)
