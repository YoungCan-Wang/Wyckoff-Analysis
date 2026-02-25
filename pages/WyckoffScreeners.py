import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta, datetime

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st
import akshare as ak

from utils import extract_symbols_from_text, stock_sector_em
from layout import setup_page
from wyckoff_engine import (
    _resister_bench_cum,
    screen_one_anomaly,
    screen_one_first_board,
    screen_one_jumper,
    screen_one_resister,
)
from fetch_a_share_csv import (
    _resolve_trading_window,
    _fetch_hist,
    get_all_stocks,
    get_stocks_by_board,
    _normalize_symbols,
)
from navigation import show_right_nav

_CACHE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "wyckoff_cache")
)


@dataclass
class ResisterConfig:
    benchmark_code: str = "000001"
    lookback_window: int = 3
    benchmark_drop_threshold: float = -2.0
    relative_strength_threshold: float = 2.0


@dataclass
class JumperConfig:
    consolidation_window: int = 60
    box_range: float = 0.25
    squeeze_window: int = 5
    squeeze_amplitude: float = 0.05
    volume_dry_ratio: float = 0.6
    volume_long_window: int = 50


@dataclass
class AnomalyConfig:
    volume_spike_ratio: float = 2.5
    stall_pct_limit: float = 2.0
    panic_pct_floor: float = -3.0
    volume_window: int = 5


@dataclass
class FirstBoardConfig:
    exclude_st: bool = True
    exclude_new_days: int = 30
    min_market_cap: float = 200000.0
    max_market_cap: float = 10000000.0
    lookback_limit_days: int = 10
    breakout_window: int = 60


@dataclass
class ScreenerConfig:
    trading_days: int = 500
    resister: ResisterConfig = field(default_factory=ResisterConfig)
    jumper: JumperConfig = field(default_factory=JumperConfig)
    anomaly: AnomalyConfig = field(default_factory=AnomalyConfig)
    first_board: FirstBoardConfig = field(default_factory=FirstBoardConfig)


setup_page(page_title="沙里淘金", page_icon="🧭")

content_col = show_right_nav()
with content_col:
    st.title("🧭 沙里淘金")
    st.markdown("在市场的沙砾里淘金，只输出值得关注的股票代码。")


    @st.cache_data(ttl=3600, show_spinner=False)
    def _stock_name_map() -> dict[str, str]:
        items = get_all_stocks()
        return {x.get("code", ""): x.get("name", "") for x in items if isinstance(x, dict)}


    @st.cache_data(ttl=600, show_spinner=False)
    def _fetch_index_hist(code: str, start: str, end: str) -> pd.DataFrame:
        df = ak.index_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end)
        if df is None or df.empty:
            raise RuntimeError(f"empty index data: {code}")
        return df


    def _fetch_index_hist_with_source(
        code: str, start: str, end: str, use_cache: bool
    ) -> tuple[pd.DataFrame, str]:
        cache_key = _cache_key("index", code, start, end, "none")
        if use_cache:
            cached = _load_cache(cache_key)
            if cached is not None and not cached.empty:
                return cached, "cache"

        try:
            df = _fetch_index_hist(code, start, end)
            df = _normalize_hist(df)
            if use_cache:
                _save_cache(cache_key, df)
            return df, "akshare"
        except Exception:
            if not _baostock_available():
                raise RuntimeError("baostock not installed")
            df = _fetch_index_hist_baostock(code, start, end)
            df = _normalize_hist_baostock(df)
            if use_cache:
                _save_cache(cache_key, df)
            return df, "baostock"


    def _fetch_index_hist_baostock(code: str, start: str, end: str) -> pd.DataFrame:
        import baostock as bs

        def normalize_index(idx: str) -> str:
            if idx.startswith("sh.") or idx.startswith("sz."):
                return idx
            if idx.startswith("000"):
                return f"sh.{idx}"
            return f"sz.{idx}"

        bs_code = normalize_index(code)
        start_date = datetime.strptime(start, "%Y%m%d").strftime("%Y-%m-%d")
        end_date = datetime.strptime(end, "%Y%m%d").strftime("%Y-%m-%d")

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"baostock login failed: {login.error_msg}")
        try:
            rs = bs.query_history_k_data_plus(
                code=bs_code,
                fields="date,open,high,low,close,volume,pctChg",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2",
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock query failed: {rs.error_msg}")
            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())
            if not data_list:
                raise RuntimeError(f"baostock empty data for index {code}")
            return pd.DataFrame(data_list, columns=rs.fields)
        finally:
            bs.logout()


    def _normalize_hist(df: pd.DataFrame) -> pd.DataFrame:
        df = df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "涨跌幅": "pct_chg",
            }
        )
        keep = ["date", "open", "close", "high", "low", "volume", "pct_chg"]
        out = df[[c for c in keep if c in df.columns]].copy()
        for col in ["open", "close", "high", "low", "volume", "pct_chg"]:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        return out


    def _cache_key(prefix: str, symbol: str, start: str, end: str, adjust: str) -> str:
        safe = f"{prefix}_{symbol}_{start}_{end}_{adjust}".replace("/", "_")
        return safe


    def _cache_path(key: str) -> str:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        return os.path.join(_CACHE_DIR, f"{key}.csv")


    def _load_cache(key: str) -> pd.DataFrame | None:
        path = _cache_path(key)
        if not os.path.exists(path):
            return None
        try:
            return pd.read_csv(path)
        except Exception:
            return None


    def _save_cache(key: str, df: pd.DataFrame) -> None:
        path = _cache_path(key)
        try:
            df.to_csv(path, index=False)
        except Exception:
            return


    def _normalize_hist_baostock(df: pd.DataFrame) -> pd.DataFrame:
        df = df.rename(
            columns={
                "date": "date",
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
                "volume": "volume",
                "pctChg": "pct_chg",
            }
        )
        keep = ["date", "open", "close", "high", "low", "volume", "pct_chg"]
        out = df[[c for c in keep if c in df.columns]].copy()
        for col in ["open", "close", "high", "low", "volume", "pct_chg"]:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        return out


    @st.cache_data(ttl=3600, show_spinner=False)
    def _baostock_available() -> bool:
        try:
            import baostock  # noqa: F401

            return True
        except Exception:
            return False


    @st.cache_data(ttl=3600, show_spinner=False)
    def _stock_sector(symbol: str) -> str:
        return stock_sector_em(symbol)


    @st.cache_data(ttl=3600, show_spinner=False)
    def _stock_list_date(symbol: str) -> date | None:
        try:
            df = ak.stock_individual_info_em(symbol=symbol)
            if df is None or df.empty:
                return None
            row = df.loc[df["item"] == "上市日期", "value"]
            if row.empty:
                return None
            raw = str(row.iloc[0]).strip()
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except Exception:
            return None


    def _parse_symbols(
        pool_mode: str, text: str, board: str, limit_count: int
    ) -> list[str]:
        if pool_mode == "手动输入":
            candidates = extract_symbols_from_text(str(text or ""), valid_codes=None)
            return _normalize_symbols(candidates)

        stocks = get_stocks_by_board(board)
        codes = [s.get("code") for s in stocks if s.get("code")]
        if limit_count > 0:
            return codes[:limit_count]
        return codes


    def _load_hist(symbol: str, window, adjust: str) -> pd.DataFrame:
        df = _fetch_hist(symbol=symbol, window=window, adjust=adjust)
        return _normalize_hist(df)


    def _load_hist_with_source(
        symbol: str,
        window,
        adjust: str,
        use_cache: bool,
    ) -> tuple[pd.DataFrame, str]:
        start = window.start_trade_date.strftime("%Y%m%d")
        end = window.end_trade_date.strftime("%Y%m%d")
        cache_key = _cache_key("stock", symbol, start, end, adjust or "none")
        if use_cache:
            cached = _load_cache(cache_key)
            if cached is not None and not cached.empty:
                return cached, "cache"

        try:
            df = _fetch_hist(symbol=symbol, window=window, adjust=adjust)
            df = _normalize_hist(df)
            if use_cache:
                _save_cache(cache_key, df)
            return df, "akshare"
        except Exception:
            if not _baostock_available():
                raise RuntimeError("baostock not installed")
            df = _fetch_hist_baostock(symbol=symbol, window=window)
            df = _normalize_hist_baostock(df)
            if use_cache:
                _save_cache(cache_key, df)
            return df, "baostock"


    def _fetch_hist_baostock(symbol: str, window) -> pd.DataFrame:
        import baostock as bs

        def normalize_code(code: str) -> str:
            if code.startswith("sh.") or code.startswith("sz."):
                return code
            if code.startswith(("600", "601", "603", "605", "688")):
                return f"sh.{code}"
            return f"sz.{code}"

        bs_code = normalize_code(symbol)
        start = window.start_trade_date.strftime("%Y-%m-%d")
        end = window.end_trade_date.strftime("%Y-%m-%d")

        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"baostock login failed: {login.error_msg}")
        try:
            rs = bs.query_history_k_data_plus(
                code=bs_code,
                fields="date,open,high,low,close,volume,pctChg",
                start_date=start,
                end_date=end,
                frequency="d",
                adjustflag="2",
            )
            if rs.error_code != "0":
                raise RuntimeError(f"baostock query failed: {rs.error_msg}")
            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())
            if not data_list:
                raise RuntimeError(f"baostock empty data for {symbol}")
            df = pd.DataFrame(data_list, columns=rs.fields)
            return df
        finally:
            bs.logout()


    def _estimate_market_cap(symbol: str) -> float:
        try:
            df = ak.stock_individual_info_em(symbol=symbol)
            if df is None or df.empty:
                return 0.0
            row = df.loc[df["item"] == "总市值", "value"]
            if row.empty:
                return 0.0
            raw = str(row.iloc[0]).strip()
            if raw.endswith("亿"):
                return float(raw.replace("亿", "")) * 10000
            if raw.endswith("万"):
                return float(raw.replace("万", ""))
            return float(raw)
        except Exception:
            return 0.0


    def _render_results(payload: dict, group_power: bool) -> None:
        label_map = {
            "resisters": "抗跌主力（相对强弱）",
            "anomalies": "异常吸筹/出货（量价背离）",
            "jumpers": "突破临界（箱体挤压）",
            "first_board": "启动龙头（首板）",
        }
        score_map = {
            "resisters": "RS(%)",
            "anomalies": "量比",
            "jumpers": "短期振幅(%)",
            "first_board": "涨幅(%)",
        }
        results = payload.get("results", {})
        errors = payload.get("errors", {})
        source_map = payload.get("source_map", {})
        benchmark_source = payload.get("benchmark_source", "")

        st.subheader("淘金结果")
        if source_map:
            source_counts: dict[str, int] = {}
            for source in source_map.values():
                source_counts[source] = source_counts.get(source, 0) + 1
            summary = "，".join(
                [f"{name}({count})" for name, count in source_counts.items()]
            )
            st.caption(f"数据来源: {summary}")
        if benchmark_source:
            st.caption(f"基准来源: {benchmark_source}")

        sector_counts: dict[str, int] = {}
        if group_power:
            for pairs in results.values():
                for code, _ in pairs:
                    sector = _stock_sector(code)
                    if not sector:
                        continue
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1
            if sector_counts:
                top = sorted(sector_counts.items(), key=lambda x: (-x[1], x[0]))[:8]
                summary = "，".join([f"{name}({count})" for name, count in top])
                st.caption(f"板块共振: {summary}")

        for key, label in label_map.items():
            if key not in results:
                continue
            pairs = results.get(key, [])
            st.markdown(f"**{label}**")
            if pairs:
                if group_power and sector_counts:
                    pairs = sorted(
                        pairs,
                        key=lambda item: (
                            -sector_counts.get(_stock_sector(item[0]), 0),
                            item[0],
                        ),
                    )
                score_label = score_map.get(key, "评分")
                lines = [f"{code} | {score_label}: {score:.2f}" for code, score in pairs]
                st.code("\n".join(lines))
            else:
                st.caption("无")

        if errors:
            with st.expander("淘金失败明细"):
                for code, msg in errors.items():
                    st.write(f"{code}: {msg}")


    with st.sidebar:
        st.subheader("淘金目标")
        tactic = st.radio(
            "选择战术",
            options=["抗跌主力", "突破临界", "异常吸筹/出货", "启动龙头"],
        )

        st.divider()
        st.subheader("淘金参数")
        trading_days = st.number_input(
            "交易日数量", min_value=120, max_value=1200, value=500, step=20
        )
        st.caption("建议交易日数量 >= 500，便于 MA200 的稳定性")

        if tactic == "抗跌主力":
            benchmark_code = st.text_input("对比指数", value="000001")
            lookback_window = st.number_input(
                "回溯窗口(天)", min_value=3, max_value=5, value=3, step=1
            )
            bench_drop = st.number_input(
                "指数累计跌幅阈值(%)", value=-2.0, step=0.5, format="%.1f"
            )
            rs_threshold = st.number_input(
                "相对强弱阈值(%)", value=2.0, step=0.5, format="%.1f"
            )
        elif tactic == "突破临界":
            consolidation_window = st.number_input(
                "盘整周期", min_value=20, max_value=120, value=60, step=5
            )
            box_range = st.number_input(
                "箱体幅度上限",
                min_value=0.15,
                max_value=0.4,
                value=0.25,
                step=0.01,
                format="%.2f",
            )
            squeeze_window = st.number_input(
                "短期挤压窗口", min_value=5, max_value=10, value=5, step=1
            )
            squeeze_amplitude = st.number_input(
                "短期振幅阈值",
                min_value=0.03,
                max_value=0.08,
                value=0.05,
                step=0.01,
                format="%.2f",
            )
            volume_dry = st.number_input(
                "缩量比例",
                min_value=0.3,
                max_value=0.8,
                value=0.6,
                step=0.05,
                format="%.2f",
            )
            volume_long = st.number_input(
                "长周期均量", min_value=30, max_value=80, value=50, step=5
            )
        elif tactic == "异常吸筹/出货":
            vol_spike = st.number_input(
                "量比阈值", min_value=1.5, max_value=5.0, value=2.5, step=0.1, format="%.1f"
            )
            stall_limit = st.number_input(
                "滞涨阈值(%)",
                min_value=0.5,
                max_value=4.0,
                value=2.0,
                step=0.1,
                format="%.1f",
            )
            panic_floor = st.number_input(
                "恐慌跌幅下限(%)",
                min_value=-6.0,
                max_value=-1.0,
                value=-3.0,
                step=0.5,
                format="%.1f",
            )
            volume_window = st.number_input(
                "量能均值窗口", min_value=3, max_value=10, value=5, step=1
            )
        else:
            exclude_st = st.checkbox("剔除 ST", value=True)
            exclude_new_days = st.number_input(
                "新股天数过滤", min_value=0, max_value=180, value=30, step=5
            )
            min_market_cap = st.number_input(
                "最小市值(万)",
                min_value=0.0,
                max_value=10000000.0,
                value=200000.0,
                step=10000.0,
            )
            max_market_cap = st.number_input(
                "最大市值(万)",
                min_value=0.0,
                max_value=10000000.0,
                value=10000000.0,
                step=10000.0,
            )
            lookback_limit = st.number_input(
                "首板回溯(天)", min_value=5, max_value=20, value=10, step=1
            )
            breakout_window = st.number_input(
                "突破窗口(天)", min_value=30, max_value=120, value=60, step=5
            )

        group_power = st.checkbox("板块共振排序", value=True)
        use_cache = st.checkbox("使用缓存", value=True)
        max_workers = int(st.number_input("并发拉取数", min_value=1, max_value=16, value=10, step=1))
        st.caption("并发数越大越快，但过高可能触发数据源限流")


    st.subheader("淘金池")
    pool_mode = st.radio("来源", options=["手动输入", "板块"], horizontal=True)
    board = "all"
    limit_count = 500
    symbols_input = ""

    if pool_mode == "手动输入":
        symbols_input = st.text_area(
            "股票代码", placeholder="例如: 600519, 000001", height=120
        )
    else:
        board = st.selectbox(
            "选择板块",
            options=["all", "main", "chinext", "star", "bse"],
            format_func=lambda v: {
                "all": "全部 A 股",
                "main": "主板",
                "chinext": "创业板",
                "star": "科创板",
                "bse": "北交所",
            }.get(v, v),
        )
        limit_count = st.number_input(
            "股票数量上限", min_value=50, max_value=2000, value=100, step=50
        )
        st.caption("首次测试建议 100–200 只；缓存命中后可适当调大")

    run = st.button("开始淘金", type="primary")

    if run:
        cfg = ScreenerConfig(trading_days=int(trading_days))
        if tactic == "抗跌主力":
            cfg.resister = ResisterConfig(
                benchmark_code=benchmark_code.strip() or "000001",
                lookback_window=int(lookback_window),
                benchmark_drop_threshold=float(bench_drop),
                relative_strength_threshold=float(rs_threshold),
            )
        elif tactic == "突破临界":
            cfg.jumper = JumperConfig(
                consolidation_window=int(consolidation_window),
                box_range=float(box_range),
                squeeze_window=int(squeeze_window),
                squeeze_amplitude=float(squeeze_amplitude),
                volume_dry_ratio=float(volume_dry),
                volume_long_window=int(volume_long),
            )
        elif tactic == "异常吸筹/出货":
            cfg.anomaly = AnomalyConfig(
                volume_spike_ratio=float(vol_spike),
                stall_pct_limit=float(stall_limit),
                panic_pct_floor=float(panic_floor),
                volume_window=int(volume_window),
            )
        else:
            cfg.first_board = FirstBoardConfig(
                exclude_st=bool(exclude_st),
                exclude_new_days=int(exclude_new_days),
                min_market_cap=float(min_market_cap),
                max_market_cap=float(max_market_cap),
                lookback_limit_days=int(lookback_limit),
                breakout_window=int(breakout_window),
            )
        symbols = _parse_symbols(pool_mode, symbols_input, board, int(limit_count))
        symbols = [s for s in symbols if s]
        if not symbols:
            st.warning("请先输入股票代码或选择板块")
            st.stop()

        window = _resolve_trading_window(
            end_calendar_day=date.today() - timedelta(days=1),
            trading_days=int(cfg.trading_days),
        )

        start = window.start_trade_date.strftime("%Y%m%d")
        end = window.end_trade_date.strftime("%Y%m%d")

        progress = st.progress(0)
        status_text = st.empty()
        data_map: dict[str, pd.DataFrame] = {}
        source_map: dict[str, str] = {}
        errors: dict[str, str] = {}
        total = len(symbols)

        def _fetch_one(sym: str) -> tuple[str, pd.DataFrame | None, str | None, str | None]:
            try:
                df, src = _load_hist_with_source(
                    sym, window, adjust="qfq", use_cache=use_cache
                )
                return (sym, df, src, None)
            except Exception as exc:
                return (sym, None, None, str(exc))

        benchmark_df = None
        benchmark_source = ""
        bench_cum: float | None = None
        if tactic == "抗跌主力":
            status_text.caption("拉取基准指数…")
            try:
                benchmark_df, benchmark_source = _fetch_index_hist_with_source(
                    cfg.resister.benchmark_code, start, end, use_cache=use_cache
                )
                bench_cum = _resister_bench_cum(benchmark_df, cfg.resister)
            except Exception as exc:
                errors[cfg.resister.benchmark_code] = f"benchmark failed: {exc}"

        results: dict[str, list[tuple[str, float]]] = {
            "resisters": [],
            "jumpers": [],
            "anomalies": [],
            "first_board": [],
        }
        jump_stats: dict[str, int] = {
            "total": 0,
            "box_pass": 0,
            "squeeze_pass": 0,
            "volume_pass": 0,
            "position_pass": 0,
        }
        name_map = _stock_name_map()

        def _screen_one(sym: str, df: pd.DataFrame) -> None:
            try:
                if tactic == "抗跌主力" and bench_cum is not None:
                    hit = screen_one_resister(sym, df, bench_cum, cfg.resister)
                    if hit:
                        results["resisters"].append(hit)
                elif tactic == "突破临界":
                    hit = screen_one_jumper(sym, df, cfg.jumper, jump_stats)
                    if hit:
                        results["jumpers"].append(hit)
                elif tactic == "异常吸筹/出货":
                    hit = screen_one_anomaly(sym, df, cfg.anomaly)
                    if hit:
                        results["anomalies"].append(hit)
                elif tactic == "启动龙头":
                    hit = screen_one_first_board(
                        sym, df, cfg.first_board,
                        stock_name_map=name_map,
                        list_date_fn=_stock_list_date,
                        market_cap_fn=_estimate_market_cap,
                    )
                    if hit:
                        results["first_board"].append(hit)
            except Exception:
                pass

        completed = 0
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_fetch_one, sym): sym for sym in symbols}
                for future in as_completed(futures):
                    sym, df, src, err = future.result()
                    completed += 1
                    progress.progress(completed / total)
                    status_text.caption(f"已拉取 {completed}/{total}，筛出 {sum(len(v) for v in results.values())} 只")
                    if err:
                        errors[sym] = err
                    else:
                        data_map[sym] = df
                        source_map[sym] = src
                        _screen_one(sym, df)
        except Exception as exc:
            st.error(f"拉取或筛选出错: {exc}")

        progress.progress(1.0)
        status_text.caption(f"✅ 完成 {len(data_map)}/{total}，筛出 {sum(len(v) for v in results.values())} 只")
        progress.empty()
        status_text.empty()

        st.session_state.wyckoff_payload = {
            "results": results,
            "errors": errors,
            "source_map": source_map,
            "benchmark_source": benchmark_source,
            "symbols_count": len(symbols),
            "data_count": len(data_map),
            "tactic": tactic,
            "jump_stats": jump_stats if tactic == "突破临界" else None,
        }

    payload = st.session_state.get("wyckoff_payload")
    if payload:
        _render_results(payload, group_power=group_power)
