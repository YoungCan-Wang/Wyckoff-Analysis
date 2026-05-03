"""
Wyckoff Funnel 定时任务：5 层漏斗筛选 → 多渠道推送

Layer 1: 剥离垃圾（ST/北交所/科创板/市值/成交额）
Layer 2: 六通道甄选（主升/潜伏/吸筹/地量/暗中护盘/点火破局）
Layer 2.5: Markup 加速检测
Layer 3: 板块共振（行业 Top-N）
Layer 4: 威科夫狙击（Spring / SOS / LPS / Effort vs Result）
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

# Ensure project root is on sys.path for direct script invocation
if __name__ == "__main__" or not __package__:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.sector_rotation import (
    SECTOR_STATE_LABELS,
    analyze_sector_rotation,
)
from core.wyckoff_engine import (
    FunnelConfig,
    FunnelResult,
    allocate_ai_candidates,
    detect_accum_stage,
    detect_markup_stage,
    layer1_filter,
    layer2_strength_detailed,
    layer3_sector_resonance,
    layer4_triggers,
    layer5_exit_signals,
    resolve_ai_candidate_policy,
)
from integrations.data_source import (
    fetch_index_hist,
    fetch_market_cap_map,
    fetch_sector_map,
)
from integrations.fetch_a_share_csv import (
    _resolve_trading_window,
)

# ── tools/ 层导入 ──
from tools.candidate_ranker import (
    TRIGGER_GROUP_ORDER,
    TRIGGER_GROUP_TITLES,
    TRIGGER_LABELS,
    TRIGGER_SHORT_LABELS,
)
from tools.candidate_ranker import (
    rank_l3_candidates as _rank_l3_candidates,
)
from utils.feishu import send_feishu_notification
from utils.trading_clock import CN_TZ, resolve_end_calendar_day

TRADING_DAYS = int(os.getenv("FUNNEL_TRADING_DAYS", "320"))
MAX_RETRIES = int(os.getenv("FUNNEL_FETCH_RETRIES", "2"))
RETRY_BASE_DELAY = float(os.getenv("FUNNEL_RETRY_BASE_DELAY", "1.0"))
SOCKET_TIMEOUT = int(os.getenv("FUNNEL_SOCKET_TIMEOUT", "20"))
FETCH_TIMEOUT = int(os.getenv("FUNNEL_FETCH_TIMEOUT", "45"))
BATCH_TIMEOUT = int(os.getenv("FUNNEL_BATCH_TIMEOUT", "420"))
BATCH_SIZE = int(os.getenv("FUNNEL_BATCH_SIZE", "250"))
BATCH_SLEEP = float(os.getenv("FUNNEL_BATCH_SLEEP", "2"))
MAX_WORKERS = int(os.getenv("FUNNEL_MAX_WORKERS", "8"))
EXECUTOR_MODE = os.getenv("FUNNEL_EXECUTOR_MODE", "process").strip().lower()
if EXECUTOR_MODE not in {"thread", "process"}:
    EXECUTOR_MODE = "process"
ENFORCE_TARGET_TRADE_DATE = False
BREADTH_MA_WINDOW = int(os.getenv("FUNNEL_BREADTH_MA_WINDOW", "20"))
BREADTH_RISK_OFF_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_OFF_PCT", "20.0"))
BREADTH_RISK_ON_THRESHOLD = float(os.getenv("FUNNEL_BREADTH_RISK_ON_PCT", "60.0"))
BREADTH_RISK_ON_MIN_DELTA = float(os.getenv("FUNNEL_BREADTH_RISK_ON_DELTA", "0.0"))
BREADTH_CLIFF_DROP_PCT = float(os.getenv("FUNNEL_BREADTH_CLIFF_DROP_PCT", "-10.0"))
SMALLCAP_BENCH_CODE = os.getenv("FUNNEL_SMALLCAP_BENCH_CODE", "399006").strip() or "399006"
CRASH_MAIN_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_MAIN_DAY_DROP_PCT", "-1.3"))
CRASH_SMALL_DAY_DROP_PCT = float(os.getenv("FUNNEL_CRASH_SMALL_DAY_DROP_PCT", "-2.5"))
CRASH_BREADTH_RATIO_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_RATIO_PCT", "15.0"))
CRASH_BREADTH_DELTA_PCT = float(os.getenv("FUNNEL_CRASH_BREADTH_DELTA_PCT", "-20.0"))
PANIC_REPAIR_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_PANIC_REPAIR_MIN_AVG_AMOUNT_WAN", "7000.0"))
RISK_OFF_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_RISK_OFF_MIN_AVG_AMOUNT_WAN", "8000.0"))
RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_RISK_OFF_DEEP_MIN_AVG_AMOUNT_WAN", "10000.0"))
CRASH_MIN_AVG_AMOUNT_WAN = float(os.getenv("FUNNEL_CRASH_MIN_AVG_AMOUNT_WAN", "12000.0"))
PANIC_REPAIR_ENABLE = os.getenv("FUNNEL_PANIC_REPAIR_ENABLE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PANIC_REPAIR_MAIN_REBOUND_PCT = float(os.getenv("FUNNEL_PANIC_REPAIR_MAIN_REBOUND_PCT", "0.8"))
PANIC_REPAIR_SMALL_REBOUND_PCT = float(os.getenv("FUNNEL_PANIC_REPAIR_SMALL_REBOUND_PCT", "1.5"))
FUNNEL_EXPORT_FULL_FETCH = os.getenv("FUNNEL_EXPORT_FULL_FETCH", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
FUNNEL_EXPORT_DIR = os.getenv("FUNNEL_EXPORT_DIR", "data/funnel_snapshots").strip() or "data/funnel_snapshots"
FUNNEL_AI_SELECTION_MODE = os.getenv("FUNNEL_AI_SELECTION_MODE", "legacy_full_hits").strip().lower()
FUNNEL_CARD_STYLE = os.getenv("FUNNEL_CARD_STYLE", "legacy_compact").strip().lower()
FUNNEL_EVR_POLICY = os.getenv("FUNNEL_EVR_POLICY", "all_regimes").strip().lower()


def _resolve_funnel_end_calendar_day() -> date:
    """Resolve the funnel end date, allowing replay jobs to pin a historical day."""
    raw = os.getenv("END_CALENDAR_DAY", "").strip()
    if raw:
        try:
            return pd.to_datetime(raw).date()
        except Exception as e:
            print(f"[funnel] END_CALENDAR_DAY={raw!r} 解析失败，回退自动日期: {e}")
    return resolve_end_calendar_day()


from tools.data_fetcher import (
    fetch_all_ohlcv,
)
from tools.data_fetcher import (
    latest_trade_date_from_hist as _latest_trade_date_from_hist,
)
from tools.funnel_config import (
    apply_funnel_cfg_overrides as _apply_funnel_cfg_overrides,
)
from tools.market_regime import (
    analyze_benchmark_and_tune_cfg as _analyze_benchmark_and_tune_cfg,
)
from tools.market_regime import (
    calc_market_breadth as _calc_market_breadth,
)
from tools.symbol_pool import (
    _stock_name_map,
)
from tools.symbol_pool import (
    resolve_symbol_pool_from_env as _resolve_symbol_pool_from_env,
)


def _dump_full_fetch_snapshot(
    df_map: dict[str, pd.DataFrame],
    all_symbols: list[str],
    window,
    fetch_stats: dict,
    bench_df: pd.DataFrame | None = None,
    smallcap_df: pd.DataFrame | None = None,
) -> str | None:
    """
    将本轮全量拉取结果落盘，便于后续离线复现和自测。
    导出内容：
    - hist_full.csv.gz: 全量历史（日线）明细（含 symbol 列）
    - latest_quotes.csv: 每只股票最新一条记录
    - fetch_status.csv: 每只股票拉取状态
    - benchmark_main.csv / benchmark_smallcap.csv: 基准指数日线
    - metadata.json: 运行元信息
    """
    if not FUNNEL_EXPORT_FULL_FETCH:
        return None
    if not all_symbols:
        return None

    try:
        base_dir = Path(FUNNEL_EXPORT_DIR)
        base_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(CN_TZ).strftime("%Y%m%d_%H%M%S")
        run_dir = base_dir / f"full_fetch_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)

        frames: list[pd.DataFrame] = []
        status_rows: list[dict] = []
        for symbol in all_symbols:
            df = df_map.get(symbol)
            if df is None or df.empty:
                status_rows.append(
                    {
                        "symbol": symbol,
                        "fetched": 0,
                        "rows": 0,
                        "latest_trade_date": "",
                    }
                )
                continue

            one = df.copy()
            one.insert(0, "symbol", symbol)
            if "date" in one.columns:
                one["date"] = pd.to_datetime(one["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            frames.append(one)
            latest_trade_date = _latest_trade_date_from_hist(df)
            status_rows.append(
                {
                    "symbol": symbol,
                    "fetched": 1,
                    "rows": int(len(df)),
                    "latest_trade_date": latest_trade_date.isoformat() if latest_trade_date else "",
                }
            )

        full_df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        full_path = run_dir / "hist_full.csv.gz"
        full_df.to_csv(full_path, index=False, compression="gzip")

        if not full_df.empty and {"symbol", "date"}.issubset(full_df.columns):
            latest_df = (
                full_df.sort_values(["symbol", "date"])
                .groupby("symbol", as_index=False)
                .tail(1)
                .sort_values("symbol")
                .reset_index(drop=True)
            )
        else:
            latest_df = pd.DataFrame(columns=["symbol"])
        latest_df.to_csv(run_dir / "latest_quotes.csv", index=False)

        status_df = pd.DataFrame(status_rows).sort_values("symbol").reset_index(drop=True)
        status_df.to_csv(run_dir / "fetch_status.csv", index=False)

        def _dump_benchmark(df_src: pd.DataFrame | None, filename: str) -> bool:
            if df_src is None or df_src.empty:
                return False
            cols = [c for c in ["date", "open", "high", "low", "close", "volume", "pct_chg"] if c in df_src.columns]
            if not cols:
                return False
            one = df_src[cols].copy()
            if "date" in one.columns:
                one["date"] = pd.to_datetime(one["date"], errors="coerce").dt.strftime("%Y-%m-%d")
            one.to_csv(run_dir / filename, index=False)
            return True

        has_bench_main = _dump_benchmark(bench_df, "benchmark_main.csv")
        has_bench_smallcap = _dump_benchmark(smallcap_df, "benchmark_smallcap.csv")

        metadata = {
            "generated_at": datetime.now(CN_TZ).isoformat(),
            "export_dir": str(run_dir),
            "window_start_trade_date": window.start_trade_date.isoformat(),
            "window_end_trade_date": window.end_trade_date.isoformat(),
            "symbols_total": int(len(all_symbols)),
            "symbols_fetched": int(sum(1 for s in status_rows if s["fetched"] == 1)),
            "rows_total": int(len(full_df)),
            "fetch_stats": fetch_stats,
            "has_benchmark_main": has_bench_main,
            "has_benchmark_smallcap": has_bench_smallcap,
        }
        with open(run_dir / "metadata.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        with open(base_dir / "latest_run.txt", "w", encoding="utf-8") as f:
            f.write(str(run_dir) + "\n")

        print(
            "[funnel] 全量快照已落盘: "
            f"{run_dir} (symbols={metadata['symbols_fetched']}/{metadata['symbols_total']}, "
            f"rows={metadata['rows_total']})"
        )
        return str(run_dir)
    except Exception as e:
        print(f"[funnel] ⚠️ 全量快照落盘失败: {e}")
        return None


def run_funnel_job(
    include_debug_context: bool = False,
) -> tuple[dict[str, list[tuple[str, float]]], dict]:
    """执行 Wyckoff Funnel，返回 (triggers, metrics)。"""
    cfg = FunnelConfig(trading_days=TRADING_DAYS)
    _apply_funnel_cfg_overrides(cfg)
    window = _resolve_trading_window(
        end_calendar_day=_resolve_funnel_end_calendar_day(),
        trading_days=TRADING_DAYS,
    )
    start_s = window.start_trade_date.strftime("%Y%m%d")
    end_s = window.end_trade_date.strftime("%Y%m%d")

    all_symbols, pool_name_map, pool_stats = _resolve_symbol_pool_from_env()
    main_items = [None] * int(pool_stats.get("pool_main", 0) or 0)
    chinext_items = [None] * int(pool_stats.get("pool_chinext", 0) or 0)
    merged_symbols = list(pool_name_map.keys())
    st_symbols = [None] * int(pool_stats.get("pool_st_excluded", 0) or 0)
    total_batches = (len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE if all_symbols else 0
    print(
        "[funnel] 股票池统计: "
        f"mode={pool_stats.get('pool_mode')}, main={len(main_items)}, chinext={len(chinext_items)}, "
        f"merged={len(merged_symbols)}, st_excluded={len(st_symbols)}, "
        f"final={len(all_symbols)}, limit={pool_stats.get('pool_limit', 0)}, batches={total_batches} (batch_size={BATCH_SIZE})"
    )
    from cli.progress import report_progress

    report_progress("股票池加载", f"共{len(all_symbols)}只", 0.05)

    # 批量元数据
    print("[funnel] 加载行业映射...")
    try:
        sector_map = fetch_sector_map()
    except Exception as e:
        print(f"[funnel] 行业映射加载失败，降级为空映射: {e}")
        sector_map = {}
    print("[funnel] 加载市值数据...")
    try:
        market_cap_map = fetch_market_cap_map()
    except Exception as e:
        print(f"[funnel] 市值数据加载失败，降级为空映射: {e}")
        market_cap_map = {}
    if not market_cap_map:
        print("[funnel] ⚠️ 市值数据为空（TUSHARE_TOKEN 可能缺失/失效），Layer1 将跳过市值过滤")
    # TickFlow 财务指标
    financial_map: dict[str, dict] = {}
    tickflow_api_key = os.getenv("TICKFLOW_API_KEY", "").strip()
    if tickflow_api_key:
        try:
            from integrations.tickflow_client import TickFlowClient

            _tf = TickFlowClient(api_key=tickflow_api_key)
            print(f"[funnel] TickFlow 财务指标请求: symbols={len(all_symbols)}")
            raw_fin = _tf.get_financial_metrics(all_symbols, latest=True)
            for sym, records in raw_fin.items():
                if records:
                    financial_map[sym] = records[0]
            missing = max(len(all_symbols) - len(financial_map), 0)
            sample_missing = ",".join(sorted([s for s in all_symbols if s not in financial_map])[:8])
            print(
                f"[funnel] TickFlow 财务指标加载成功: {len(financial_map)}/{len(all_symbols)}, "
                f"missing={missing}, sample_missing={sample_missing or '-'}"
            )
        except Exception as e:
            print(f"[funnel] TickFlow 财务指标加载失败，跳过财务过滤: {e}")
    print("[funnel] 加载股票名称...")
    try:
        name_map = _stock_name_map()
    except Exception as e:
        print(f"[funnel] 股票名称加载失败，降级为代码展示: {e}")
        name_map = {}

    # 大盘基准
    bench_df = None
    smallcap_df = None
    try:
        bench_df = fetch_index_hist("000001", start_s, end_s)
        print("[funnel] 大盘基准加载成功")
    except Exception as e:
        print(f"[funnel] 大盘基准加载失败: {e}")
    try:
        smallcap_df = fetch_index_hist(SMALLCAP_BENCH_CODE, start_s, end_s)
        print(f"[funnel] 小盘基准加载成功: {SMALLCAP_BENCH_CODE}")
    except Exception as e:
        print(f"[funnel] 小盘基准加载失败 {SMALLCAP_BENCH_CODE}: {e}")
    # 并发拉取日线（委托 tools/data_fetcher）
    all_df_map, fetch_stats = fetch_all_ohlcv(
        symbols=all_symbols,
        window=window,
        enforce_target_trade_date=ENFORCE_TARGET_TRADE_DATE,
        batch_size=BATCH_SIZE,
        max_workers=MAX_WORKERS,
        batch_timeout=BATCH_TIMEOUT,
        batch_sleep=BATCH_SLEEP,
        executor_mode=EXECUTOR_MODE,
    )
    snapshot_dir = _dump_full_fetch_snapshot(
        df_map=all_df_map,
        all_symbols=all_symbols,
        window=window,
        fetch_stats=fetch_stats,
        bench_df=bench_df,
        smallcap_df=smallcap_df,
    )

    # Step 0: 大盘总闸 + 全市场广度 + 动态阈值
    breadth_context = _calc_market_breadth(all_df_map, BREADTH_MA_WINDOW)
    benchmark_context = _analyze_benchmark_and_tune_cfg(
        bench_df,
        smallcap_df,
        cfg,
        breadth=breadth_context,
    )
    print(
        "[funnel] 大盘总闸: "
        f"regime={benchmark_context['regime']}, "
        f"close={benchmark_context['close']}, ma50={benchmark_context['ma50']}, ma200={benchmark_context['ma200']}, "
        f"ma50_slope_5d={benchmark_context['ma50_slope_5d']}, main_today={benchmark_context.get('main_today_pct')}, recent3={benchmark_context['recent3_pct']}, "
        f"recent3_cum={benchmark_context['recent3_cum_pct']}, "
        f"smallcap_code={benchmark_context.get('smallcap_code')}, smallcap_today={benchmark_context.get('smallcap_today_pct')}, "
        f"breadth={benchmark_context.get('breadth')}, "
        f"panic_triggered={benchmark_context.get('panic_triggered')}, panic_reasons={benchmark_context.get('panic_reasons')}, "
        f"repair_triggered={benchmark_context.get('repair_triggered')}, repair_reasons={benchmark_context.get('repair_reasons')}, "
        f"tuned={benchmark_context['tuned']}"
    )

    # 统一漏斗计算：L1 -> L2 -> L3 -> L4
    print("[funnel] 开始执行全量漏斗筛选...")
    report_progress("漏斗筛选", "L1~L4 计算中", 0.85)

    # Layer 1
    l1_input = list(all_df_map.keys())
    l1_passed = layer1_filter(l1_input, name_map, market_cap_map, all_df_map, cfg, financial_map=financial_map)

    # Layer 2
    l2_passed, l2_channel_map = layer2_strength_detailed(
        l1_passed,
        all_df_map,
        bench_df,
        cfg,
        rps_universe=l1_input,
    )
    # 通道标签现在是多标签用 + 拼接，因此用 in 判断包含关系
    l2_momentum = sum(1 for v in l2_channel_map.values() if "主升通道" in v)
    l2_ambush = sum(1 for v in l2_channel_map.values() if "潜伏通道" in v)
    l2_accum = sum(1 for v in l2_channel_map.values() if "吸筹通道" in v)
    l2_dry_vol = sum(1 for v in l2_channel_map.values() if "地量蓄势" in v)
    l2_rs_div = sum(1 for v in l2_channel_map.values() if "暗中护盘" in v)
    l2_sos = sum(1 for v in l2_channel_map.values() if "点火破局" in v)

    # Layer 3 (Sector Resonance)
    l3_passed, top_sectors = layer3_sector_resonance(
        l2_passed,
        sector_map,
        cfg,
        base_symbols=l1_passed,
        df_map=all_df_map,
    )
    sector_rotation = analyze_sector_rotation(
        all_df_map,
        sector_map,
        universe_symbols=list(all_df_map.keys()),
        focus_sectors=top_sectors,
    )
    benchmark_context["sector_rotation"] = sector_rotation
    print(f"[funnel] 板块轮动温度计: {sector_rotation.get('headline', '无')}")

    # Layer 4 (Wyckoff Triggers)
    # L4 需要 l2_df_map，这里直接用 all_df_map 即可，因为 key 都在里面
    triggers = layer4_triggers(l3_passed, all_df_map, cfg)

    # L2 旁路观察池：L1通过 + L2被拒 + 在热门板块 + 有L4原始触发
    l2_rejected = [s for s in l1_passed if s not in set(l2_passed)]
    l2_bypass_in_sector = (
        [s for s in l2_rejected if str(sector_map.get(s, "")).strip() in set(top_sectors)] if top_sectors else []
    )
    bypass_triggers: dict[str, list[tuple[str, float]]] = {}
    l2_bypass_pool: list[str] = []
    if l2_bypass_in_sector:
        bypass_triggers = layer4_triggers(l2_bypass_in_sector, all_df_map, cfg)
        bypass_hit_set: set[str] = set()
        for hits in bypass_triggers.values():
            for code, _ in hits:
                bypass_hit_set.add(code)
        l2_bypass_pool = sorted(bypass_hit_set)
        if l2_bypass_pool:
            print(f"[funnel] L2旁路观察池: {len(l2_bypass_pool)} 只 (L2拒绝但有L4信号+板块共振)")

    # Markup 阶段、Accumulation ABC 细化、Exit 信号
    markup_symbols = detect_markup_stage(l3_passed, all_df_map, cfg)
    accum_stage_map = detect_accum_stage(l2_passed, all_df_map, cfg)
    exit_signals = layer5_exit_signals(l2_passed + markup_symbols, all_df_map, accum_stage_map, cfg)

    total_hits = sum(len(v) for v in triggers.values())
    latest_close_map: dict[str, float] = {}
    for sym, df in all_df_map.items():
        try:
            close_series = pd.to_numeric(df.get("close"), errors="coerce")
            if close_series is None or close_series.empty:
                continue
            last_close = close_series.iloc[-1]
            if pd.notna(last_close):
                latest_close_map[str(sym).strip()] = float(last_close)
        except Exception:
            continue
    ranked_l3_symbols, l3_score_map = _rank_l3_candidates(
        l3_symbols=l3_passed,
        df_map=all_df_map,
        sector_map=sector_map,
        triggers=triggers,
        top_sectors=top_sectors,
        l2_channel_map=l2_channel_map,
        sector_rotation_map=(sector_rotation.get("state_map", {}) or {}),
    )
    metrics = {
        "total_symbols": len(all_symbols),
        "pool_mode": str(pool_stats.get("pool_mode", "") or ""),
        "pool_main": len(main_items),
        "pool_chinext": len(chinext_items),
        "pool_merged": len(merged_symbols),
        "pool_st_excluded": len(st_symbols),
        "pool_batches": total_batches,
        "fetch_ok": int(fetch_stats.get("fetch_ok", len(all_df_map)) or 0),
        "fetch_fail": int(fetch_stats.get("fetch_fail", 0) or 0),
        "fetch_date_mismatch": int(fetch_stats.get("fetch_date_mismatch", 0) or 0),
        "fetch_spot_patched": int(fetch_stats.get("fetch_spot_patched", 0) or 0),
        "snapshot_dir": snapshot_dir,
        "layer1": len(l1_passed),
        "layer2": len(l2_passed),
        "layer2_momentum": l2_momentum,
        "layer2_ambush": l2_ambush,
        "layer2_accum": l2_accum,
        "layer2_dry_vol": l2_dry_vol,
        "layer2_rs_div": l2_rs_div,
        "layer2_sos": l2_sos,
        "layer2_channel_map": l2_channel_map,
        "layer3": len(l3_passed),
        "top_sectors": top_sectors,
        "sector_rotation": sector_rotation,
        "layer3_symbols": ranked_l3_symbols or l3_passed,
        "layer3_score_map": l3_score_map,
        "total_hits": total_hits,
        "by_trigger": {k: len(v) for k, v in triggers.items()},
        "benchmark_context": benchmark_context,
        "latest_close_map": latest_close_map,
        "min_funnel_score": float(getattr(cfg, "min_funnel_score", 0.0) or 0.0),
        # L2 旁路观察池
        "l2_bypass_pool": l2_bypass_pool,
        "l2_bypass_triggers": bypass_triggers,
        # 阶段识别和退出信号
        "markup_symbols": markup_symbols,
        "accum_stage_map": accum_stage_map,
        "exit_signals": exit_signals,
        "all_df_map": all_df_map,
        "financial_map": financial_map,
    }
    if include_debug_context:
        metrics["_debug"] = {
            "cfg": cfg,
            "end_trade_date": window.end_trade_date.isoformat(),
            "all_symbols": all_symbols,
            "name_map": name_map,
            "market_cap_map": market_cap_map,
            "sector_map": sector_map,
            "bench_df": bench_df,
            "all_df_map": all_df_map,
            "layer1_symbols": l1_passed,
            "layer2_symbols": l2_passed,
            "layer3_symbols_raw": l3_passed,
        }
    print(
        f"[funnel] L1={metrics['layer1']}, L2={metrics['layer2']}, "
        f"(主升={l2_momentum}, 潜伏={l2_ambush}, 吸筹={l2_accum}, 地量={l2_dry_vol}, 护盘={l2_rs_div}, 点火={l2_sos}), "
        f"L3={metrics['layer3']}, 命中={total_hits}, "
        f"Top行业={top_sectors}, 各触发={metrics['by_trigger']}"
    )
    report_progress("筛选完成", f"命中={total_hits}只", 1.0)

    return triggers, metrics


def run(
    webhook_url: str,
    *,
    notify: bool = True,
    return_details: bool = False,
) -> tuple[bool, list[dict], dict] | tuple[bool, list[dict], dict, dict]:
    """
    执行 Wyckoff Funnel，漏斗完成后立即发送飞书通知。
    返回 (成功与否, 用于研报的股票信息列表, 大盘上下文)。
    每项为 {"code": str, "name": str, "tag": str}。
    """
    triggers, metrics = run_funnel_job()
    all_df_map = metrics.get("all_df_map", {})
    benchmark_context = metrics.get("benchmark_context", {}) or {}
    try:
        name_map = _stock_name_map()
    except Exception as e:
        print(f"[funnel] 股票名称加载失败，降级为代码展示: {e}")
        name_map = {}
    try:
        sector_map = fetch_sector_map()
    except Exception as e:
        print(f"[funnel] 行业映射加载失败，降级为空映射: {e}")
        sector_map = {}
    latest_close_map = metrics.get("latest_close_map", {}) or {}
    if latest_close_map:
        benchmark_context["latest_close_map"] = latest_close_map

    code_to_reasons: dict[str, list[str]] = {}
    code_to_trigger_keys: dict[str, list[str]] = {}
    code_to_total_score: dict[str, float] = {}
    for key, label in TRIGGER_LABELS.items():
        for code, score in triggers.get(key, []):
            if code not in code_to_reasons:
                code_to_reasons[code] = []
                code_to_trigger_keys[code] = []
                code_to_total_score[code] = 0.0
            code_to_reasons[code].append(label)
            code_to_trigger_keys[code].append(key)
            code_to_total_score[code] += score

    # 兼容旧变量名（下游可能引用）
    code_to_best_score = code_to_total_score
    sorted_codes = sorted(
        code_to_reasons.keys(),
        key=lambda c: -code_to_total_score.get(c, 0),
    )
    unique_hit_count = len(sorted_codes)
    use_legacy_selection = FUNNEL_AI_SELECTION_MODE in {
        "legacy_full_hits",
        "legacy_hits",
        "all_hits",
        "classic",
    }
    use_legacy_card = FUNNEL_CARD_STYLE in {
        "legacy",
        "legacy_compact",
        "classic",
        "v1",
    }
    l3_ranked_symbols = [str(c).strip() for c in (metrics.get("layer3_symbols", []) or []) if str(c).strip()]
    l2_channel_map = metrics.get("layer2_channel_map", {}) or {}
    # 提前取出，供后面的闭包函数引用
    markup_symbols = metrics.get("markup_symbols", []) or []
    accum_stage_map = metrics.get("accum_stage_map", {}) or {}
    exit_signals = metrics.get("exit_signals", {}) or {}
    # L2 旁路观察池（由 run_funnel_job 汇总进 metrics）
    l2_bypass_pool = metrics.get("l2_bypass_pool", []) or []
    bypass_triggers = metrics.get("l2_bypass_triggers", {}) or {}
    sector_rotation = metrics.get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation.get("state_map", {}) or {}
    # 策略：大盘水温驱动的双轨制（Top-Down 择时顺势策略）
    regime = benchmark_context.get("regime", "NEUTRAL")
    if use_legacy_selection:
        trend_selected = []
        accum_selected = []
        score_map = {c: float(code_to_best_score.get(c, 0.0)) for c in sorted_codes}
        ai_policy = {
            "total_cap": len(sorted_codes),
            "trend_quota": 0,
            "accum_quota": 0,
            "requested_trend_quota": 0,
            "requested_accum_quota": 0,
            "quota_family": "LEGACY_FULL_HITS",
            "max_trend_l3_fill": 0,
            "max_accum_l3_fill": 0,
        }
        selected_for_ai = list(sorted_codes)
        print(f"[funnel] AI候选分配完成(legacy_full_hits): total={len(selected_for_ai)}")
    else:
        mock_result = FunnelResult(
            layer1_symbols=[],
            layer2_symbols=[],
            layer3_symbols=metrics.get("layer3_symbols", []) or [],
            top_sectors=[],
            triggers=triggers,
            stage_map=accum_stage_map,
            markup_symbols=markup_symbols,
            exit_signals=exit_signals,
            channel_map=l2_channel_map,
        )
        alloc_started = time.monotonic()
        trend_selected, accum_selected, score_map = allocate_ai_candidates(
            mock_result,
            l3_ranked_symbols,
            regime,
            sector_map=sector_map,
            max_per_sector=2,
        )
        ai_policy = resolve_ai_candidate_policy(regime)
        alloc_elapsed = time.monotonic() - alloc_started
        print(
            f"[funnel] AI候选分配完成: trend={len(trend_selected)}, accum={len(accum_selected)}, "
            f"elapsed={alloc_elapsed:.3f}s"
        )
        selected_for_ai = trend_selected + accum_selected

    min_funnel_score = float(metrics.get("min_funnel_score", 0.0) or 0.0)
    if score_map and min_funnel_score > 0:
        before = len(selected_for_ai)
        selected_for_ai = [c for c in selected_for_ai if score_map.get(c, 0.0) >= min_funnel_score]
        dropped = before - len(selected_for_ai)
        if dropped:
            print(f"[funnel] min_funnel_score={min_funnel_score} 过滤掉 {dropped} 只低质量候选")

    if use_legacy_card and use_legacy_selection:
        bench_line = "未知"
        pv_line = "暂无大盘量价推演"
        if benchmark_context:
            _close = benchmark_context.get("close") or 0
            _ma50 = benchmark_context.get("ma50") or 0
            _ma200 = benchmark_context.get("ma200") or 0
            _cum3 = benchmark_context.get("recent3_cum_pct") or 0
            bench_line = (
                f"{benchmark_context.get('regime')} | "
                f"收盘 {float(_close):.2f} | MA50 {float(_ma50):.2f} | MA200 {float(_ma200):.2f} | "
                f"近3日 {float(_cum3):+.2f}%"
            )
            pv_line = str(
                benchmark_context.get("market_pv_outlook") or benchmark_context.get("market_pv_summary") or pv_line
            )

        lines = [
            (
                f"**股票池**: 主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
                f"-> 去重{metrics['pool_merged']} -> 去ST{metrics['pool_st_excluded']} "
                f"= {metrics['total_symbols']} (共{metrics['pool_batches']}批)"
            ),
            f"**漏斗概览**: {metrics['total_symbols']}只 → L1:{metrics['layer1']} → L2:{metrics['layer2']} → L3:{metrics['layer3']} → 命中:{metrics['total_hits']}",
            f"**大盘水温**: {bench_line}",
            f"**大盘量价推演**: {pv_line}",
            f"**候选分层**: 命中股票{unique_hit_count} -> AI输入全量{len(selected_for_ai)}",
            f"**Top 行业**: {', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
            "",
        ]

        # ── 命中列表：按信号分组 ──
        def _score_star(s: float) -> str:
            if s >= 10:
                return "★★"
            if s >= 5:
                return "★ "
            return "  "

        set(selected_for_ai)

        # 1) 多信号共振组（置顶）
        multi_signal = [c for c in selected_for_ai if len(code_to_trigger_keys.get(c, [])) > 1]
        if multi_signal:
            lines.append(f"**【🔥 多信号共振】{len(multi_signal)} 只**")
            for code in sorted(multi_signal, key=lambda c: -code_to_total_score.get(c, 0)):
                name = name_map.get(code, code)
                short = "+".join(TRIGGER_SHORT_LABELS.get(k, k) for k in code_to_trigger_keys.get(code, []))
                score = code_to_total_score.get(code, 0)
                lines.append(f"{_score_star(score)} {code} {name}  {score:.2f}  {short}")
            lines.append("")

        # 2) 各信号分组
        single_signal_codes = [c for c in selected_for_ai if c not in set(multi_signal)]
        # 为每个 code 确定主信号（取第一个 trigger key）
        code_primary_key: dict[str, str] = {}
        for code in single_signal_codes:
            keys = code_to_trigger_keys.get(code, [])
            code_primary_key[code] = keys[0] if keys else "sos"

        for group_key in TRIGGER_GROUP_ORDER:
            group_codes = [c for c in single_signal_codes if code_primary_key.get(c) == group_key]
            if not group_codes:
                continue
            group_title = TRIGGER_GROUP_TITLES.get(group_key, group_key)
            lines.append(f"**【{group_title}】{len(group_codes)} 只**")
            for code in sorted(group_codes, key=lambda c: -code_to_total_score.get(c, 0)):
                name = name_map.get(code, code)
                score = code_to_total_score.get(code, 0)
                lines.append(f"{_score_star(score)} {code} {name}  {score:.2f}")
            lines.append("")

        if not selected_for_ai:
            lines.append("无")

        if l2_bypass_pool:
            lines.append("")
            lines.append(f"**【👁 L2旁路观察】{len(l2_bypass_pool)} 只**")
            lines.append("形态先于强度，不进正式推荐")
            for code in l2_bypass_pool:
                name = name_map.get(code, code)
                bp_reasons = []
                for key, _label in TRIGGER_LABELS.items():
                    for c, _ in bypass_triggers.get(key, []):
                        if c == code:
                            bp_reasons.append(TRIGGER_SHORT_LABELS.get(key, key))
                industry = str(sector_map.get(code, "") or "")
                lines.append(f"  {code} {name}  {'+'.join(bp_reasons)}  [{industry}]")

        content = "\n".join(lines)
        title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
        ok = True if not notify else send_feishu_notification(webhook_url, title, content)

        sos_hit_set = set(str(c).strip() for c, _ in triggers.get("sos", []))
        evr_hit_set = set(str(c).strip() for c, _ in triggers.get("evr", []))
        spring_hit_set = set(str(c).strip() for c, _ in triggers.get("spring", []))
        lps_hit_set = set(str(c).strip() for c, _ in triggers.get("lps", []))

        def _infer_track(code: str) -> str:
            if code in sos_hit_set or code in evr_hit_set:
                return "Trend"
            if code in spring_hit_set or code in lps_hit_set:
                return "Accum"
            return "Trend"

        def _legacy_stage(code: str) -> str:
            if code in markup_symbols:
                return "Markup"
            return str(accum_stage_map.get(code, "") or "").strip()

        symbols_for_report = [
            {
                "code": c,
                "name": name_map.get(c, c),
                "tag": "、".join(code_to_reasons.get(c, [])),
                "track": _infer_track(c),
                "stage": _legacy_stage(c),
                "score": float((metrics.get("layer3_score_map", {}) or {}).get(c, 0.0)),
                "priority_score": float(code_to_best_score.get(c, 0.0)),
                "priority_rank": idx + 1,
                "selection_source": "l4_hit",
                "selection_is_fill": False,
                "initial_price": float(latest_close_map.get(c, 0.0) or 0.0),
                "industry": str(sector_map.get(c, "") or "未知行业"),
                "sector_state_code": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("state", "")
                ).strip(),
                "sector_state": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get(
                        "label",
                        "",
                    )
                ).strip(),
                "sector_note": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("note", "")
                ).strip(),
                "sector_guidance": str(
                    (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("guidance", "")
                ).strip(),
                "exit_signal": str((exit_signals.get(c, {}) or {}).get("signal", "")).strip(),
                "exit_price": (exit_signals.get(c, {}) or {}).get("price"),
                "exit_reason": str((exit_signals.get(c, {}) or {}).get("reason", "")).strip(),
            }
            for idx, c in enumerate(selected_for_ai)
        ]
        if return_details:
            details = {
                "metrics": metrics,
                "triggers": triggers,
                "content": content,
                "title": title,
                "symbols_for_report": symbols_for_report,
                "selected_for_ai": selected_for_ai,
                "trend_selected": [],
                "accum_selected": [],
                "priority_score_map": score_map,
                "name_map": name_map,
                "sector_map": sector_map,
                "all_df_map": all_df_map,
            }
            return (ok, symbols_for_report, benchmark_context, details)
        return (ok, symbols_for_report, benchmark_context)

    def _channel_tags(code: str) -> set[str]:
        raw = str(l2_channel_map.get(code, "")).strip()
        if not raw:
            return set()
        return {x.strip() for x in raw.split("+") if x.strip()}

    hit_set = set(sorted_codes)
    sos_hit_set = set(str(c).strip() for c, _ in triggers.get("sos", []))
    spring_hit_set = set(str(c).strip() for c, _ in triggers.get("spring", []))
    lps_hit_set = set(str(c).strip() for c, _ in triggers.get("lps", []))
    evr_hit_set = set(str(c).strip() for c, _ in triggers.get("evr", []))

    def _stage_name(code: str) -> str:
        if code in markup_symbols:
            return "Markup"
        return str(accum_stage_map.get(code, "") or "").strip()

    hit_selected_count = sum(1 for c in selected_for_ai if c in hit_set)
    l3_only_count = len(selected_for_ai) - hit_selected_count
    trend_hit_selected = sum(1 for c in trend_selected if c in hit_set)
    trend_l3_only = len(trend_selected) - trend_hit_selected
    accum_hit_selected = sum(1 for c in accum_selected if c in hit_set)
    accum_l3_only = len(accum_selected) - accum_hit_selected

    channel_counts = {
        "主升通道": 0,
        "潜伏通道": 0,
        "吸筹通道": 0,
        "地量蓄势": 0,
        "暗中护盘": 0,
        "点火破局": 0,
    }
    for code in selected_for_ai:
        tags = _channel_tags(code)
        for key in channel_counts:
            if key in tags:
                channel_counts[key] += 1
    l3_score_map = metrics.get("layer3_score_map", {}) or {}
    by_trigger = metrics.get("by_trigger", {}) or {}
    l2_momentum = int(metrics.get("layer2_momentum", 0) or 0)
    l2_ambush = int(metrics.get("layer2_ambush", 0) or 0)
    l2_accum = int(metrics.get("layer2_accum", 0) or 0)
    l2_dry_vol = int(metrics.get("layer2_dry_vol", 0) or 0)
    l2_rs_div = int(metrics.get("layer2_rs_div", 0) or 0)
    l2_sos = int(metrics.get("layer2_sos", 0) or 0)
    sector_rotation = metrics.get("sector_rotation", {}) or {}
    sector_rotation_map = sector_rotation.get("state_map", {}) or {}
    markup_count = len(markup_symbols)
    accum_a_count = sum(1 for v in accum_stage_map.values() if v == "Accum_A")
    accum_b_count = sum(1 for v in accum_stage_map.values() if v == "Accum_B")
    accum_c_count = sum(1 for v in accum_stage_map.values() if v == "Accum_C")
    stop_loss_count = sum(1 for sig in exit_signals.values() if sig.get("signal") == "stop_loss")
    dist_warning_count = sum(1 for sig in exit_signals.values() if sig.get("signal") == "distribution_warning")
    blocked_exit_signals_set = {"stop_loss", "distribution_warning"}
    blocked_exit_codes = [
        code
        for code in l3_ranked_symbols
        if str((exit_signals.get(code, {}) or {}).get("signal", "")).strip() in blocked_exit_signals_set
    ]

    total_cap = int(ai_policy["total_cap"])
    trend_quota = int(ai_policy["trend_quota"])
    accum_quota = int(ai_policy["accum_quota"])
    requested_trend_quota = int(ai_policy["requested_trend_quota"])
    requested_accum_quota = int(ai_policy["requested_accum_quota"])
    quota_family = str(ai_policy["quota_family"])
    max_trend_l3_fill = int(ai_policy["max_trend_l3_fill"])
    max_accum_l3_fill = int(ai_policy["max_accum_l3_fill"])

    print(
        f"[funnel] 候选分层: 命中事件={metrics['total_hits']}, 命中股票={unique_hit_count}, "
        f"配额配置=[{regime}->{quota_family}: requested Trend={requested_trend_quota}, "
        f"requested Accum={requested_accum_quota}, effective Trend={trend_quota}, "
        f"effective Accum={accum_quota}, 总上限={total_cap}, "
        f"l3_fill_limit Trend={max_trend_l3_fill}, Accum={max_accum_l3_fill}], "
        f"最终选入: Trend={len(trend_selected)}, Accum={len(accum_selected)}, 总计={len(selected_for_ai)}"
    )

    bench_line = "未知"
    pv_line = "暂无大盘量价推演"
    if benchmark_context:
        breadth = benchmark_context.get("breadth", {}) or {}
        breadth_text = (
            f"，上涨家数占比 {breadth.get('ratio_pct'):.1f}%"
            f"（前日 {breadth.get('prev_ratio_pct'):.1f}%，变化 {breadth.get('delta_pct'):+.1f}%，样本 {breadth.get('sample_size')} 只）"
            if breadth
            else ""
        )
        repair_text = (
            f"，修复原因：{benchmark_context.get('repair_reasons')}"
            if benchmark_context.get("repair_triggered")
            else ""
        )
        smallcap_close = benchmark_context.get("smallcap_close")
        smallcap_cum3 = benchmark_context.get("smallcap_recent3_cum_pct")
        smallcap_text = (
            f" | 创业板指 {smallcap_close:.2f}，近3日 {smallcap_cum3:+.2f}%"
            if smallcap_close is not None and smallcap_cum3 is not None
            else ""
        )
        bench_line = (
            f"{benchmark_context.get('regime')} | 沪深300 {benchmark_context.get('close'):.2f}"
            f"（MA50={benchmark_context.get('ma50'):.1f} MA200={benchmark_context.get('ma200'):.1f}）"
            f"，近3日 {benchmark_context.get('recent3_cum_pct'):+.2f}%"
            f"{smallcap_text}"
            f"{breadth_text}{repair_text}"
        )
        pv_line = str(
            benchmark_context.get("market_pv_outlook") or benchmark_context.get("market_pv_summary") or pv_line
        )

    data_quality_line = (
        f"成功拉取 {metrics['fetch_ok']} 只"
        + (f"，失败 {metrics['fetch_fail']} 只" if metrics["fetch_fail"] else "，无失败")
        + (f"，日期不对齐跳过 {metrics.get('fetch_date_mismatch', 0)} 只" if metrics.get("fetch_date_mismatch") else "")
        + (f"，实时快照补偿 {metrics.get('fetch_spot_patched', 0)} 只" if metrics.get("fetch_spot_patched") else "")
    )
    ai_channel_summary = (
        " | ".join(
            f"{k}{channel_counts[k]}"
            for k in ["主升通道", "潜伏通道", "吸筹通道", "地量蓄势", "暗中护盘", "点火破局"]
            if channel_counts[k] > 0
        )
        or "无"
    )
    l4_non_hit_count = max(int(metrics["layer3"]) - int(unique_hit_count), 0)
    top_priority_count = sum(
        1 for c in selected_for_ai if c in markup_symbols or c in sos_hit_set or c in spring_hit_set
    )

    lines = [
        "## 一览",
        (
            f"- **股票池**：主板{metrics['pool_main']} + 创业板{metrics['pool_chinext']} "
            f"→ 去重{metrics['pool_merged']} → 去ST{metrics['pool_st_excluded']} "
            f"= **{metrics['total_symbols']}**（共{metrics['pool_batches']}批）"
        ),
        f"- **大盘水温**：{bench_line}",
        f"- **大盘量价推演**：{pv_line}",
        f"- **Top 行业**：{', '.join(metrics['top_sectors']) if metrics['top_sectors'] else '无'}",
        f"- **板块轮动温度计**：{sector_rotation.get('headline', '无')}",
        f"- **数据质量**：{data_quality_line}",
        "",
        "## 漏斗进度",
        f"- **L1 通过**：{metrics['layer1']} / {metrics['total_symbols']}（剔除 {metrics['total_symbols'] - metrics['layer1']}）",
        f"- **L2 通过**：{metrics['layer2']} / {metrics['layer1']}（至少满足一条二级通道）",
        f"- **L3 保留**：{metrics['layer3']} / {metrics['layer2']}（当前仅做行业标记，不做硬剔除）",
        f"- **L4 命中股票**：{unique_hit_count} 只（命中事件 {metrics['total_hits']} 次）",
        f"- **L4 未命中**：{l4_non_hit_count} 只（仍留在 L3 观察池）",
        "",
        "## L2 通道与阶段",
        f"- **L2 通道分布**：主升{l2_momentum} | 潜伏{l2_ambush} | 吸筹{l2_accum} | 地量{l2_dry_vol} | 护盘{l2_rs_div} | 点火{l2_sos}",
        f"- **威科夫阶段**：Markup{markup_count} | Accum_A{accum_a_count} | Accum_B{accum_b_count} | Accum_C{accum_c_count}",
        f"- **板块轮动状态**：分歧{int((sector_rotation.get('counts', {}) or {}).get('DISAGREEMENT_PULLBACK', 0))} | 健康{int((sector_rotation.get('counts', {}) or {}).get('HEALTHY_MAINLINE', 0))} | 高潮{int((sector_rotation.get('counts', {}) or {}).get('CONSENSUS_CLIMAX', 0))} | 退潮{int((sector_rotation.get('counts', {}) or {}).get('DISTRIBUTION_RISK', 0))}",
        "",
        "## L4 形态触发",
        f"- **SOS（量价点火）**：{len(sos_hit_set)}",
        f"- **Spring（终极震仓）**：{len(spring_hit_set)}",
        f"- **LPS（缩量回踩）**：{len(lps_hit_set)}",
        f"- **EVR（放量不跌）**：{len(evr_hit_set)}",
        "",
        "## 风控与 AI 筛后",
        f"- **Exit 参考信号**：结构止损{stop_loss_count} | Distribution警告{dist_warning_count}",
        f"- **硬剔除**：{len(blocked_exit_codes)} 只（已触发结构止损或派发警告，不再送入 AI）",
        (
            f"- **最终送 AI**：{len(selected_for_ai)} 只"
            f"（{regime}->{quota_family}：Trend={trend_quota} / Accum={accum_quota} / 总上限={total_cap}）"
        ),
        f"- **AI 入选构成**：L4命中 {hit_selected_count} | L3补充 {l3_only_count}",
        f"- **Trend 轨**：{len(trend_selected)} 只（L4命中 {trend_hit_selected} | L3补充 {trend_l3_only}）",
        f"- **Accum 轨**：{len(accum_selected)} 只（L4命中 {accum_hit_selected} | L3补充 {accum_l3_only}）",
        f"- **L3 补位上限**：Trend {max_trend_l3_fill} | Accum {max_accum_l3_fill}",
        f"- **高优先级候选**：{top_priority_count} 只",
        f"- **AI 输入通道分布**：{ai_channel_summary}",
    ]
    rotation_overview_lines = sector_rotation.get("overview_lines", []) or []
    if rotation_overview_lines:
        lines.extend(["", "## 板块轮动水温计"])
        lines.extend([f"- {x}" for x in rotation_overview_lines])

    def _append_ai_section(lines_obj: list[str], section_title: str, section_desc: str, codes: list[str]) -> None:
        lines_obj.extend(
            [
                "",
                section_title,
                f"- **说明**：{section_desc}",
                "- **字段**：代码 名称 | 阶段 | 来源标签 | 风控提示 | 分值",
                "",
            ]
        )
        if not codes:
            lines_obj.append("- 无")
            return
        for code in codes:
            name = name_map.get(code, code)
            trigger_reason = "、".join(code_to_reasons.get(code, []))
            channel = str(l2_channel_map.get(code, "")).strip()
            industry = str(sector_map.get(code, "") or "未知行业")
            sector_info = sector_rotation_map.get(industry, {}) or {}
            sector_state_label = str(
                sector_info.get("label", SECTOR_STATE_LABELS.get("NEUTRAL_MIXED", "中性混沌"))
            ).strip()
            stage = accum_stage_map.get(code, "")
            if not stage and code in markup_symbols:
                stage = "Markup"
            stage_str = f"[{stage}]" if stage else ""
            base_reason = trigger_reason or "威科夫候选"
            sector_reason = f"板块:{sector_state_label}"
            if channel:
                reasons = f"{channel} | {sector_reason} | {base_reason}"
            else:
                reasons = f"{sector_reason} | {base_reason}"

            exit_sig = exit_signals.get(code, {})
            exit_str = ""
            if exit_sig.get("signal") == "stop_loss":
                exit_str = f"| ✗止损{exit_sig.get('price', 0):.2f}"
            elif exit_sig.get("signal") == "distribution_warning":
                exit_str = "| ⚠Distribution警告"

            priority_score = float(score_map.get(code, 0.0))
            l3_score = float(l3_score_map.get(code, 0.0))
            lines_obj.append(
                f"- {code} {name} {stage_str} | {reasons} {exit_str} | priority={priority_score:.2f}, l3={l3_score:.2f}"
            )

    _append_ai_section(
        lines,
        "## AI 输入·Trend轨",
        "右侧主升，优先 SOS（量价点火）与 Markup 阶段。",
        trend_selected,
    )
    _append_ai_section(
        lines,
        "## AI 输入·Accum轨",
        "左侧潜伏，优先 Spring（终极震仓）/LPS（缩量回踩）与 Accum_C 阶段。",
        accum_selected,
    )

    if not selected_for_ai:
        lines.extend(
            [
                "",
                "**为什么没候选**",
                f"• 触发信号: SOS={int(by_trigger.get('sos', 0))}, Spring={int(by_trigger.get('spring', 0))}, LPS={int(by_trigger.get('lps', 0))}, EVR={int(by_trigger.get('evr', 0))}",
                f"• 阶段分布: Markup={markup_count}, Accum_A={accum_a_count}, Accum_B={accum_b_count}, Accum_C={accum_c_count}",
                f"• 水温判断: {regime} | 当前配额: Trend={trend_quota}, Accum={accum_quota}, 总上限={total_cap}",
                "• 分析：候选股票尚未达到日线级别的威科夫触发信号（SOS/Spring/LPS）或阶段转折特征。"
                if total_cap > 0
                else "• 当前大盘水温，AI配额已关闭（total_cap=0）。",
            ]
        )

    if l2_bypass_pool:
        lines.append("")
        lines.append(f"**【👁 L2旁路观察】{len(l2_bypass_pool)} 只**")
        lines.append("形态先于强度，不进正式推荐")
        for code in l2_bypass_pool:
            bp_name = name_map.get(code, code)
            bp_reasons = []
            for key in TRIGGER_LABELS.keys():
                for c, _ in bypass_triggers.get(key, []):
                    if c == code:
                        bp_reasons.append(TRIGGER_SHORT_LABELS.get(key, key))
            bp_industry = str(sector_map.get(code, "") or "")
            lines.append(f"  {code} {bp_name}  {'+'.join(bp_reasons)}  [{bp_industry}]")

    content = "\n".join(lines)
    title = f"🔬 Wyckoff Funnel {date.today().strftime('%Y-%m-%d')}"
    ok = True if not notify else send_feishu_notification(webhook_url, title, content)

    def _selection_source(code: str) -> str:
        if code in hit_set:
            return "l4_hit"
        if code in markup_symbols:
            return "markup"
        if _stage_name(code) == "Accum_C":
            return "accum_c"
        return "l3_fill"

    symbols_for_report = [
        {
            "code": c,
            "name": name_map.get(c, c),
            "tag": (
                f"{str(l2_channel_map.get(c, '')).strip()} | {'、'.join(code_to_reasons.get(c, [])) or '威科夫候选'}"
            ).strip(" |"),
            "track": ("Trend" if c in trend_selected else "Accum" if c in accum_selected else ""),
            "stage": _stage_name(c),
            "score": float(l3_score_map.get(c, 0.0)),
            "priority_score": float(score_map.get(c, 0.0)),
            "priority_rank": idx + 1,
            "selection_source": _selection_source(c),
            "selection_is_fill": _selection_source(c) == "l3_fill",
            "initial_price": float(latest_close_map.get(c, 0.0) or 0.0),
            "industry": str(sector_map.get(c, "") or "未知行业"),
            "sector_state_code": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("state", "")
            ).strip(),
            "sector_state": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get(
                    "label",
                    "",
                )
            ).strip(),
            "sector_note": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("note", "")
            ).strip(),
            "sector_guidance": str(
                (sector_rotation_map.get(str(sector_map.get(c, "") or "未知行业"), {}) or {}).get("guidance", "")
            ).strip(),
            "exit_signal": str((exit_signals.get(c, {}) or {}).get("signal", "")).strip(),
            "exit_price": (exit_signals.get(c, {}) or {}).get("price"),
            "exit_reason": str((exit_signals.get(c, {}) or {}).get("reason", "")).strip(),
        }
        for idx, c in enumerate(selected_for_ai)
    ]
    if return_details:
        details = {
            "metrics": metrics,
            "triggers": triggers,
            "content": content,
            "title": title,
            "symbols_for_report": symbols_for_report,
            "selected_for_ai": selected_for_ai,
            "trend_selected": trend_selected,
            "accum_selected": accum_selected,
            "priority_score_map": score_map,
            "name_map": name_map,
            "sector_map": sector_map,
            "all_df_map": all_df_map,
        }
        return (ok, symbols_for_report, benchmark_context, details)
    return (ok, symbols_for_report, benchmark_context)
