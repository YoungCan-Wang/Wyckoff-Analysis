from __future__ import annotations

from typing import Any

from integrations import strategy_api_client as client


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


def _configure_remote(monkeypatch):
    monkeypatch.setenv("WYCKOFF_STRATEGY_API_URL", "https://strategy.example")
    monkeypatch.setenv("WYCKOFF_STRATEGY_API_KEY", "secret")
    monkeypatch.setenv("WYCKOFF_STRATEGY_API_MODE", "remote")


def test_strategy_api_enabled_only_when_configured_in_auto(monkeypatch):
    monkeypatch.setenv("WYCKOFF_STRATEGY_API_MODE", "auto")
    monkeypatch.delenv("WYCKOFF_STRATEGY_API_URL", raising=False)
    monkeypatch.delenv("WYCKOFF_STRATEGY_API_KEY", raising=False)
    assert client.is_strategy_api_enabled() is False

    monkeypatch.setenv("WYCKOFF_STRATEGY_API_URL", "https://strategy.example")
    monkeypatch.setenv("WYCKOFF_STRATEGY_API_KEY", "secret")
    assert client.is_strategy_api_enabled() is True


def test_analyze_stock_legacy_maps_public_response(monkeypatch):
    _configure_remote(monkeypatch)

    def fake_request(method, url, headers, json, timeout):
        assert method == "POST"
        assert url == "https://strategy.example/v1/analyze"
        assert headers["X-API-Key"] == "secret"
        assert json["code"] == "600519"
        return FakeResponse(
            200,
            {
                "code": "600519",
                "name": "Kweichow Moutai",
                "strategy_version": "private-v1",
                "trade_date": "2026-05-15",
                "latest_close": 1600.0,
                "score": 88,
                "rating": "strong",
                "risk_level": "low",
                "phase": "Trend",
                "setups": ["main trend", "SOS"],
                "risk_notes": ["structure ok"],
                "explanation": "public summary",
            },
        )

    monkeypatch.setattr(client.requests, "request", fake_request)

    result = client.analyze_stock_legacy("600519", cost=1500)

    assert result["source"] == "strategy_api"
    assert result["health"] == "healthy"
    assert result["l4_triggers"] == ["SOS"]
    assert result["pnl_pct"] == 6.67
    assert result["formatted_text"] == "public summary"


def test_run_backtest_legacy_polls_task(monkeypatch):
    _configure_remote(monkeypatch)
    calls: list[tuple[str, str]] = []

    def fake_request(method, url, headers, json=None, timeout=0):
        calls.append((method, url))
        if url.endswith("/v1/backtest"):
            return FakeResponse(200, {"task_id": "task-1", "status": "completed", "created_at": "2026-05-15T00:00:00Z"})
        return FakeResponse(
            200,
            {
                "task_id": "task-1",
                "status": "completed",
                "created_at": "2026-05-15T00:00:00Z",
                "completed_at": "2026-05-15T00:00:01Z",
                "result": {
                    "strategy_version": "private-v1",
                    "top_n": 2,
                    "best": {"hold_days": 10, "trades": 3, "sharpe_ratio": 1.2},
                    "rows": [],
                },
            },
        )

    monkeypatch.setattr(client.requests, "request", fake_request)

    result = client.run_backtest_legacy(
        start="2026-01-01",
        end="2026-05-01",
        hold_days=10,
        top_n=2,
        board="all",
        stop_loss_pct=-7.0,
        take_profit_pct=18.0,
    )

    assert calls == [
        ("POST", "https://strategy.example/v1/backtest"),
        ("GET", "https://strategy.example/v1/tasks/task-1"),
    ]
    assert result["source"] == "strategy_api"
    assert result["trades"] == 3
    assert result["sharpe_ratio"] == 1.2


def test_tail_buy_remote_calls_score_endpoint(monkeypatch):
    _configure_remote(monkeypatch)

    def fake_request(method, url, headers, json, timeout):
        assert method == "POST"
        assert url == "https://strategy.example/v1/tail-buy/score"
        assert headers["X-API-Key"] == "secret"
        assert json["candidates"][0]["code"] == "600519"
        assert json["intraday_by_code"]["600519"][0]["close"] == 10.0
        return FakeResponse(
            200,
            {
                "strategy_version": "private-v1",
                "candidates": [
                    {
                        "code": "600519",
                        "name": "贵州茅台",
                        "rule_score": 80,
                        "rule_decision": "BUY",
                        "final_decision": "BUY",
                        "priority_score": 80,
                    }
                ],
            },
        )

    monkeypatch.setattr(client.requests, "request", fake_request)

    result = client.score_tail_buy_remote(
        candidates=[{"code": "600519", "name": "贵州茅台"}],
        intraday_by_code={"600519": [{"close": 10.0}]},
    )

    assert result["candidates"][0]["rule_decision"] == "BUY"
