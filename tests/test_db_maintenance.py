from __future__ import annotations

from dataclasses import dataclass

from scripts.db_maintenance import cleanup_recommendation_table, cleanup_recommendation_tracking


@dataclass
class _Response:
    data: list[dict] | None = None
    count: int | None = None


class _FakeTable:
    def __init__(self, client: _FakeClient, name: str):
        self.client = client
        self.name = name
        self.delete_mode = False
        self.filters: list[tuple[str, int]] = []
        self.limit_value: int | None = None
        self.order_desc = False
        self.want_count = False

    def select(self, _columns: str, *, count: str | None = None):
        self.want_count = count == "exact"
        return self

    def order(self, _column: str, *, desc: bool = False):
        self.order_desc = desc
        return self

    def limit(self, value: int):
        self.limit_value = value
        return self

    def lt(self, column: str, value: int):
        self.filters.append((column, value))
        return self

    def delete(self):
        self.delete_mode = True
        return self

    def execute(self):
        rows = self.client.tables.setdefault(self.name, [])
        for column, value in self.filters:
            rows = [row for row in rows if row[column] < value]

        if self.delete_mode:
            deleted_ids = {id(row) for row in rows}
            self.client.tables[self.name] = [row for row in self.client.tables[self.name] if id(row) not in deleted_ids]
            return _Response(data=[])

        ordered = sorted(rows, key=lambda row: row["recommend_date"], reverse=self.order_desc)
        limited = ordered[: self.limit_value] if self.limit_value is not None else ordered
        return _Response(data=limited, count=len(rows) if self.want_count else None)


class _FakeClient:
    def __init__(self, rows: list[dict] | dict[str, list[dict]]):
        self.tables = {"recommendation_tracking": rows} if isinstance(rows, list) else rows

    @property
    def rows(self) -> list[dict]:
        return self.tables["recommendation_tracking"]

    def table(self, name: str):
        return _FakeTable(self, name)


def test_cleanup_recommendation_tracking_keeps_latest_distinct_dates():
    dates = [20260505, 20260503, 20260430, 20260425, 20260420]
    rows = [{"recommend_date": date, "code": code} for date in dates for code in range(2)]
    client = _FakeClient(rows)

    status, count = cleanup_recommendation_tracking(client, keep_dates=3, page_size=2)

    remaining_dates = {row["recommend_date"] for row in client.rows}
    assert status == "ok, keep_dates=3, cutoff=20260430"
    assert count is None
    assert remaining_dates == {20260505, 20260503, 20260430}


def test_cleanup_recommendation_tracking_dry_run_counts_rows_before_cutoff():
    dates = [20260505, 20260503, 20260430, 20260425]
    rows = [{"recommend_date": date, "code": code} for date in dates for code in range(2)]
    client = _FakeClient(rows)

    status, count = cleanup_recommendation_tracking(client, keep_dates=3, page_size=3, dry_run=True)

    assert status == "dry_run, keep_dates=3, cutoff=20260430"
    assert count == 2
    assert len(client.rows) == 8


def test_cleanup_recommendation_table_uses_requested_table():
    client = _FakeClient(
        {
            "recommendation_tracking": [{"recommend_date": 20260505, "code": 1}],
            "recommendation_tracking_us": [
                {"recommend_date": 20260505, "code": "A.US"},
                {"recommend_date": 20260504, "code": "B.US"},
                {"recommend_date": 20260503, "code": "C.US"},
            ],
        }
    )

    status, count = cleanup_recommendation_table(
        client,
        "recommendation_tracking_us",
        keep_dates=2,
        page_size=10,
    )

    assert status == "ok, keep_dates=2, cutoff=20260504"
    assert count is None
    assert [row["recommend_date"] for row in client.tables["recommendation_tracking_us"]] == [20260505, 20260504]
    assert client.rows == [{"recommend_date": 20260505, "code": 1}]
