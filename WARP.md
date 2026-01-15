# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

A股历史行情 CSV 导出工具 (A-share Stock Historical Data Export Tool) - A Python-based web application that fetches Chinese A-share stock market data using akshare and provides enhanced OHLCV exports for Wyckoff analysis.

**Tech Stack**: Python 3.10+, Streamlit, akshare, pandas

**Deployment**: Hosted on Streamlit Community Cloud at https://wyckoff-analysis-youngcanphoenix.streamlit.app/

## Development Commands

### Environment Setup
```bash
# Create and activate virtual environment (required on macOS due to PEP 668)
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip and install dependencies
python -m pip install -U pip
python -m pip install -r requirements.txt

# Verify akshare installation
python -c "import akshare as ak; print(ak.__version__)"
```

### Running the Application

#### Local Web Interface (Streamlit)
```bash
# Start the Streamlit web app (opens at http://localhost:8501)
streamlit run streamlit_app.py
```

#### Command-Line Script
```bash
# Single stock
python fetch_a_share_csv.py --symbol 300364

# Multiple stocks
python -u fetch_a_share_csv.py --symbols 000973 600798 601390

# Extract from mixed text (supports Chinese names)
python -u fetch_a_share_csv.py --symbols-text '000973 佛塑科技 600798鲁抗医药'

# With custom parameters
python fetch_a_share_csv.py --symbol 300364 --trading-days 500 --end-offset-days 1 --adjust qfq
```

### Testing
No formal test suite is currently implemented. Manual testing is done through the web interface and CLI.

## Architecture

### Core Components

#### 1. Data Fetching Layer (`fetch_a_share_csv.py`)
The core module that interacts with akshare API to fetch stock data.

**Key Functions**:
- `_resolve_trading_window()`: Calculates trading date windows using A-share calendar (skips weekends/holidays)
- `_fetch_hist()`: Fetches historical stock data from akshare
- `_stock_sector_em()`: Retrieves stock sector/industry information
- `_build_export()`: Transforms raw data into standardized OHLCV format with additional fields
- `_extract_symbols_from_text()`: Extracts 6-digit stock codes from mixed Chinese/English text

**Data Sources**: Uses `akshare` library which pulls from public Chinese stock market data sources (Sina, East Money, etc.)

#### 2. Web Interface (`streamlit_app.py`)
Multi-page Streamlit application providing browser-based access.

**Pages**:
- Main page (`streamlit_app.py`): Stock data query and download interface
- Changelog page (`pages/Changelog.py`): Version history viewer

**Key Features**:
- Stock search with code/name autocomplete (caches stock list for 24 hours in `data/stock_list_cache.json`)
- Batch mode for up to 6 stocks simultaneously
- Mobile-optimized layout toggle
- Search history tracking (last 10 queries)
- Floating navigation bar with custom CSS/HTML injection
- ZIP file generation for batch downloads

**Session State Management**:
- `search_history`: List of recent stock queries
- `current_symbol`: Currently selected stock code
- `should_run`: Trigger flag for programmatic data fetching
- `mobile_mode`: UI layout preference

#### 3. Output Format
Generates two CSV files per stock:

**`{code}_{name}_hist_data.csv`**: Raw data from akshare with Chinese column names (日期, 开盘, 收盘, etc.)

**`{code}_{name}_ohlcv.csv`**: Enhanced English-format OHLCV with columns:
- Date, Open, High, Low, Close, Volume, Amount
- TurnoverRate, Amplitude (parsed from percentage strings to floats)
- AvgPrice (calculated as Amount/Volume)
- Sector (industry classification from East Money)

### Trading Date Calculation
Critical business logic: All date ranges are based on **trading days** (交易日), not calendar days.

- Uses `ak.tool_trade_date_hist_sina()` to fetch A-share trading calendar
- Automatically skips weekends and Chinese holidays
- Default: Last 500 trading days with 1-day offset (yesterday as end date)
- Implementation in `_resolve_trading_window()` uses binary search (`bisect_right`) for efficiency

### Stock Code Format
6-digit numeric codes without exchange prefix:
- `600xxx/601xxx/603xxx/688xxx`: Shanghai Stock Exchange (SSE)
- `000xxx/002xxx`: Shenzhen Stock Exchange (SZSE)
- `300xxx`: ChiNext (Growth Enterprise Board)

### Caching Strategy
- **Stock list cache**: `data/stock_list_cache.json` with 24-hour TTL
- Managed by `get_all_stocks()` function
- Falls back to API call if cache is stale or missing
- Streamlit's `@st.cache_data(ttl=3600)` decorator prevents repeated loads during session

## Development Notes

### Adjustment Types (复权)
- `""` (default): No adjustment - raw prices
- `qfq`: Forward adjustment - adjusts historical prices to current scale (common for trend analysis)
- `hfq`: Backward adjustment - adjusts current prices to historical scale

### Error Handling
- Individual stock failures in batch mode don't stop other fetches
- Network failures are logged but gracefully handled
- Missing sector data results in empty string (non-fatal)

### File Naming
Uses `_safe_filename_part()` to sanitize stock names by replacing special characters (`< > : " / \ | ? *`) with underscores.

### Known Constraints
- Batch mode limited to 6 stocks to prevent abuse ("开超市不是一个好的行为")
- Data quality depends on akshare's upstream sources (can be unstable)
- Chinese stock names may contain spaces (preserved in filenames)

## Configuration Files

### `.streamlit/config.toml`
Disables sidebar navigation (`showSidebarNavigation = false`) - navigation is handled by custom floating nav bar.

### `.gitignore`
Excludes:
- Virtual environments (`.venv/`, `venv/`)
- Generated data files (`data/*.csv`, `*.csv`, `*.xlsx`)
- Stock list cache (`data/stock_list_cache.json`)
- Python artifacts (`__pycache__/`, `*.pyc`)

## Deployment

### Streamlit Community Cloud
- Repository: https://github.com/YoungCan-Wang/Wyckoff-Analysis
- Live app: https://wyckoff-analysis-youngcanphoenix.streamlit.app/
- Auto-deploys from `main` branch
- No additional secrets or configuration required (akshare uses public APIs)

## Project Context

**Inspiration**: Created by @YoungCan-Wang, inspired by 秋生trader (@Hoyooyoo)

**Purpose**: Provides Wyckoff analysis practitioners with properly formatted Chinese stock data including volume, turnover rate, amplitude, and sector information.

**Language**: Mixed Chinese/English codebase - UI in Chinese, code/exports in English.
