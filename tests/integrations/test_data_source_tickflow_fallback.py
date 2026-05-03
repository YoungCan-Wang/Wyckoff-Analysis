"""data_source 中 tickflow 优先链路测试。"""

from __future__ import annotations

import pandas as pd
import pytest

import integrations.data_source as ds


def _sample_cn_hist() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "日期": "2026-04-18",
                "开盘": 10.0,
                "最高": 10.5,
                "最低": 9.9,
                "收盘": 10.3,
                "成交量": 1000000.0,
                "成交额": 10000000.0,
                "涨跌幅": 1.2,
                "换手率": pd.NA,
                "振幅": 2.3,
            }
        ]
    )


def _disable_other_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_SOURCE_DISABLE_AKSHARE", "1")
    monkeypatch.setenv("DATA_SOURCE_DISABLE_BAOSTOCK", "1")
    monkeypatch.setenv("DATA_SOURCE_DISABLE_EFINANCE", "1")
    monkeypatch.delenv("DATA_SOURCE_DISABLE_TICKFLOW", raising=False)


def test_fetch_stock_hist_prefers_tickflow_when_both_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_other_fallbacks(monkeypatch)
    monkeypatch.setenv("TICKFLOW_API_KEY", "dummy")
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT", None)
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT_READY", False)
    monkeypatch.setattr("integrations.tushare_client.get_pro", lambda: object())

    def _raise_tushare_if_called(*args, **kwargs):
        raise RuntimeError("should_not_call")

    monkeypatch.setattr(ds, "_fetch_stock_tushare", _raise_tushare_if_called)
    monkeypatch.setattr(ds, "_fetch_stock_tickflow", lambda *args, **kwargs: _sample_cn_hist())

    out = ds.fetch_stock_hist("600519", "2026-04-10", "2026-04-18", adjust="qfq")
    assert not out.empty
    assert out.attrs.get("source") == "tickflow"
    assert out.iloc[0]["日期"] == "2026-04-18"


def test_fetch_stock_hist_falls_back_to_tushare_when_tickflow_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_other_fallbacks(monkeypatch)
    monkeypatch.setenv("TICKFLOW_API_KEY", "dummy")
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT", None)
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT_READY", False)
    monkeypatch.setattr("integrations.tushare_client.get_pro", lambda: object())

    def _raise_tickflow(*args, **kwargs):
        raise RuntimeError("tickflow timeout")

    monkeypatch.setattr(ds, "_fetch_stock_tickflow", _raise_tickflow)
    monkeypatch.setattr(ds, "_fetch_stock_tushare", lambda *args, **kwargs: _sample_cn_hist())

    out = ds.fetch_stock_hist("000001", "2026-04-10", "2026-04-18", adjust="qfq")
    assert not out.empty
    assert out.attrs.get("source") == "tushare"


def test_fetch_stock_hist_keeps_limit_hint_when_tickflow_rate_limited_and_fallback_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _disable_other_fallbacks(monkeypatch)
    monkeypatch.setenv("TICKFLOW_API_KEY", "dummy")
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT", None)
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT_READY", False)
    monkeypatch.setattr("integrations.tushare_client.get_pro", lambda: object())

    def _raise_tickflow_limit(*args, **kwargs):
        raise RuntimeError('TickFlow HTTP 429: {"code":"RATE_LIMITED"}')

    monkeypatch.setattr(ds, "_fetch_stock_tickflow", _raise_tickflow_limit)
    monkeypatch.setattr(ds, "_fetch_stock_tushare", lambda *args, **kwargs: _sample_cn_hist())

    out = ds.fetch_stock_hist("000001", "2026-04-10", "2026-04-18", adjust="qfq")
    assert not out.empty
    assert out.attrs.get("source") == "tushare"
    assert "触发数据源限制，升级数据源：" in str(out.attrs.get("tickflow_limit_hint", ""))


def test_fetch_stock_hist_error_message_contains_tickflow_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_other_fallbacks(monkeypatch)
    monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT", None)
    monkeypatch.setattr(ds, "_TICKFLOW_CLIENT_READY", False)
    monkeypatch.setattr("integrations.tushare_client.get_pro", lambda: None)

    with pytest.raises(RuntimeError) as exc:
        ds.fetch_stock_hist("000001", "2026-04-10", "2026-04-18", adjust="qfq")
    assert "tickflow→tushare→akshare→baostock→efinance" in str(exc.value)
