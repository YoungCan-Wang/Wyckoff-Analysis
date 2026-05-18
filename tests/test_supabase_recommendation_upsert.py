from __future__ import annotations

from types import SimpleNamespace

from integrations.supabase_recommendation import upsert_recommendations


class FakeSupabaseClient:
    def __init__(self, rows: list[dict] | None = None, *, fail_select: bool = False) -> None:
        self.rows = rows or []
        self.fail_select = fail_select
        self.upserts: list[list[dict]] = []

    def table(self, _name: str):
        return FakeSupabaseQuery(self)


class FakeSupabaseQuery:
    def __init__(self, client: FakeSupabaseClient) -> None:
        self.client = client
        self.kind = ""
        self.payload: list[dict] = []

    def select(self, *_args, **_kwargs):
        self.kind = "select"
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, *_args, **_kwargs):
        return self

    def upsert(self, payload, **_kwargs):
        self.kind = "upsert"
        self.payload = list(payload)
        return self

    def execute(self):
        if self.kind == "select":
            if self.client.fail_select:
                raise RuntimeError("transient fetch failure")
            return SimpleNamespace(data=self.client.rows)
        self.client.upserts.append(self.payload)
        return SimpleNamespace(data=self.payload)


def _enable_fake_supabase(monkeypatch, client: FakeSupabaseClient) -> None:
    monkeypatch.setattr("integrations.supabase_recommendation.is_supabase_configured", lambda: True)
    monkeypatch.setattr("integrations.supabase_recommendation._get_supabase_admin_client", lambda: client)


def test_upsert_recommendations_aborts_when_history_fetch_fails(monkeypatch):
    client = FakeSupabaseClient(fail_select=True)
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(20260518, [{"code": "000001", "name": "Ping An", "initial_price": 10.0}])

    assert ok is False
    assert client.upserts == []


def test_upsert_recommendations_preserves_existing_recommend_count(monkeypatch):
    client = FakeSupabaseClient(
        rows=[
            {"code": 1, "recommend_count": 3, "recommend_date": 20260517},
            {"code": 1, "recommend_count": 2, "recommend_date": 20260516},
        ]
    )
    _enable_fake_supabase(monkeypatch, client)

    ok = upsert_recommendations(20260518, [{"code": "000001", "name": "Ping An", "initial_price": 10.0}])

    assert ok is True
    assert client.upserts[0][0]["recommend_count"] == 4
