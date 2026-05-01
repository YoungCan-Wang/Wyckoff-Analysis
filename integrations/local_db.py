# -*- coding: utf-8 -*-
"""
本地 SQLite 存储层 — CLI Agent 的离线缓存 + 记忆。

所有 CLI 场景下的读操作优先走本地 SQLite，Supabase 降级为 fallback。
GitHub Actions 不用此模块。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from core.constants import LOCAL_DB_PATH

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_SCHEMA_VERSION = 4

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recommendation_tracking (
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    recommend_date INTEGER NOT NULL,
    recommend_reason TEXT DEFAULT '',
    initial_price REAL DEFAULT 0,
    current_price REAL DEFAULT 0,
    is_ai_recommended INTEGER DEFAULT 0,
    camp TEXT DEFAULT '',
    synced_at TEXT DEFAULT (datetime('now')),
    UNIQUE(code, recommend_date)
);

CREATE TABLE IF NOT EXISTS signal_pending (
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    signal_type TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    signal_score REAL DEFAULT 0,
    days_elapsed INTEGER DEFAULT 0,
    regime TEXT DEFAULT '',
    industry TEXT DEFAULT '',
    synced_at TEXT DEFAULT (datetime('now')),
    UNIQUE(code, signal_type, signal_date)
);

CREATE TABLE IF NOT EXISTS market_signal_daily (
    trade_date TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    synced_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio (
    portfolio_id TEXT PRIMARY KEY,
    free_cash REAL DEFAULT 0,
    synced_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS portfolio_position (
    portfolio_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    shares INTEGER DEFAULT 0,
    cost_price REAL DEFAULT 0,
    buy_dt TEXT DEFAULT '',
    stop_loss REAL,
    synced_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (portfolio_id, code)
);

CREATE TABLE IF NOT EXISTS agent_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_type TEXT NOT NULL,
    content TEXT NOT NULL,
    codes TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sync_meta (
    table_name TEXT PRIMARY KEY,
    last_synced_at TEXT NOT NULL,
    row_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    model TEXT DEFAULT '',
    provider TEXT DEFAULT '',
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    elapsed_s REAL DEFAULT 0,
    error TEXT DEFAULT '',
    tool_calls TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tail_buy_history (
    code TEXT NOT NULL,
    name TEXT DEFAULT '',
    run_date TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    signal_type TEXT DEFAULT '',
    status TEXT DEFAULT '',
    final_decision TEXT NOT NULL,
    rule_score REAL DEFAULT 0,
    priority_score REAL DEFAULT 0,
    rule_reasons TEXT DEFAULT '',
    llm_decision TEXT DEFAULT '',
    llm_reason TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(code, run_date)
);

CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendation_tracking(recommend_date);
CREATE INDEX IF NOT EXISTS idx_sig_status ON signal_pending(status);
CREATE INDEX IF NOT EXISTS idx_mem_type ON agent_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_mem_codes ON agent_memory(codes);
CREATE INDEX IF NOT EXISTS idx_chatlog_session ON chat_log(session_id);
CREATE INDEX IF NOT EXISTS idx_chatlog_created ON chat_log(created_at);
CREATE INDEX IF NOT EXISTS idx_tail_run_date ON tail_buy_history(run_date);
CREATE INDEX IF NOT EXISTS idx_tail_decision ON tail_buy_history(final_decision);
"""


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    with _lock:
        if _conn is not None:
            return _conn
        LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(LOCAL_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        _conn = conn
        return _conn


def init_db() -> None:
    conn = get_db()
    conn.executescript(_DDL)
    cur = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    current = row[0] if row and row[0] else 0
    if current < 4:
        try:
            conn.execute("ALTER TABLE portfolio_position ADD COLUMN buy_dt TEXT DEFAULT ''")
        except Exception:
            pass
    if current < _SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version) VALUES(?)",
            (_SCHEMA_VERSION,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Recommendation tracking
# ---------------------------------------------------------------------------

def save_recommendations(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = get_db()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO recommendation_tracking
               (code, name, recommend_date, recommend_reason, initial_price,
                current_price, is_ai_recommended, camp, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            [
                (
                    str(r.get("code", "")).strip(),
                    str(r.get("name", "")).strip(),
                    int(r.get("recommend_date", 0)),
                    str(r.get("recommend_reason", "")).strip(),
                    float(r.get("initial_price", 0) or 0),
                    float(r.get("current_price", 0) or 0),
                    1 if r.get("is_ai_recommended") else 0,
                    str(r.get("camp", "")).strip(),
                )
                for r in rows
            ],
        )
    return len(rows)


def load_recommendations(*, limit: int = 100) -> list[dict]:
    conn = get_db()
    cur = conn.execute(
        "SELECT * FROM recommendation_tracking ORDER BY recommend_date DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Signal pending
# ---------------------------------------------------------------------------

def save_signals(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = get_db()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO signal_pending
               (code, name, signal_type, signal_date, status, signal_score,
                days_elapsed, regime, industry, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            [
                (
                    str(r.get("code", "")).strip(),
                    str(r.get("name", "")).strip(),
                    str(r.get("signal_type", "")).strip(),
                    str(r.get("signal_date", "")).strip(),
                    str(r.get("status", "pending")).strip(),
                    float(r.get("signal_score", 0) or 0),
                    int(r.get("days_elapsed", 0) or 0),
                    str(r.get("regime", "")).strip(),
                    str(r.get("industry", "")).strip(),
                )
                for r in rows
            ],
        )
    return len(rows)


def delete_recommendations(codes: list[str]) -> int:
    if not codes:
        return 0
    conn = get_db()
    placeholders = ",".join("?" for _ in codes)
    with conn:
        cur = conn.execute(
            f"DELETE FROM recommendation_tracking WHERE code IN ({placeholders})",
            codes,
        )
    return cur.rowcount


def load_signals(*, status: str | None = None, limit: int = 200) -> list[dict]:
    conn = get_db()
    if status:
        cur = conn.execute(
            "SELECT * FROM signal_pending WHERE status=? ORDER BY signal_date DESC LIMIT ?",
            (status, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM signal_pending ORDER BY signal_date DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def delete_signals(codes: list[str]) -> int:
    if not codes:
        return 0
    conn = get_db()
    placeholders = ",".join("?" for _ in codes)
    with conn:
        cur = conn.execute(
            f"DELETE FROM signal_pending WHERE code IN ({placeholders})",
            codes,
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Market signal daily
# ---------------------------------------------------------------------------

def save_market_signal(trade_date: str, data: dict) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_signal_daily
               (trade_date, data_json, synced_at) VALUES (?, ?, datetime('now'))""",
            (str(trade_date).strip(), json.dumps(data, ensure_ascii=False)),
        )


def load_latest_market_signal() -> dict | None:
    conn = get_db()
    cur = conn.execute(
        "SELECT data_json FROM market_signal_daily ORDER BY trade_date DESC LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row["data_json"])
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------

def save_portfolio(portfolio_id: str, free_cash: float, positions: list[dict]) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO portfolio
               (portfolio_id, free_cash, synced_at) VALUES (?, ?, datetime('now'))""",
            (portfolio_id, free_cash),
        )
        conn.execute(
            "DELETE FROM portfolio_position WHERE portfolio_id=?",
            (portfolio_id,),
        )
        if positions:
            conn.executemany(
                """INSERT INTO portfolio_position
                   (portfolio_id, code, name, shares, cost_price, buy_dt, stop_loss, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                [
                    (
                        portfolio_id,
                        str(p.get("code", "")).strip(),
                        str(p.get("name", "")).strip(),
                        int(p.get("shares", 0) or 0),
                        float(p.get("cost_price", 0) or 0),
                        str(p.get("buy_dt", "") or ""),
                        float(p["stop_loss"]) if p.get("stop_loss") is not None else None,
                    )
                    for p in positions
                ],
            )


def load_portfolio(portfolio_id: str) -> dict | None:
    conn = get_db()
    cur = conn.execute(
        "SELECT * FROM portfolio WHERE portfolio_id=?", (portfolio_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    pos_cur = conn.execute(
        "SELECT * FROM portfolio_position WHERE portfolio_id=?", (portfolio_id,)
    )
    return {
        "portfolio_id": row["portfolio_id"],
        "free_cash": row["free_cash"],
        "positions": [dict(p) for p in pos_cur.fetchall()],
    }


def _ensure_local_portfolio(portfolio_id: str) -> None:
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO portfolio (portfolio_id, free_cash) VALUES (?, 0)",
        (portfolio_id,),
    )
    conn.commit()


def upsert_local_position(
    portfolio_id: str, code: str, name: str,
    shares: int, cost_price: float, buy_dt: str = "",
) -> None:
    _ensure_local_portfolio(portfolio_id)
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO portfolio_position
               (portfolio_id, code, name, shares, cost_price, buy_dt, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (portfolio_id, code, name, shares, cost_price, buy_dt),
        )


def delete_local_position(portfolio_id: str, code: str) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            "DELETE FROM portfolio_position WHERE portfolio_id=? AND code=?",
            (portfolio_id, code),
        )


def update_local_free_cash(portfolio_id: str, free_cash: float) -> None:
    _ensure_local_portfolio(portfolio_id)
    conn = get_db()
    with conn:
        conn.execute(
            "UPDATE portfolio SET free_cash=?, synced_at=datetime('now') WHERE portfolio_id=?",
            (free_cash, portfolio_id),
        )


# ---------------------------------------------------------------------------
# Agent memory
# ---------------------------------------------------------------------------

def save_memory(memory_type: str, content: str, codes: str = "") -> int:
    conn = get_db()
    with conn:
        cur = conn.execute(
            """INSERT INTO agent_memory (memory_type, content, codes)
               VALUES (?, ?, ?)""",
            (memory_type, content, codes),
        )
        return cur.lastrowid or 0


def search_memory(
    *,
    codes: list[str] | None = None,
    keyword: str | None = None,
    limit: int = 10,
) -> list[dict]:
    conn = get_db()
    clauses: list[str] = []
    params: list[Any] = []
    if codes:
        or_parts = []
        for c in codes:
            or_parts.append("codes LIKE ?")
            params.append(f"%{c}%")
        clauses.append(f"({' OR '.join(or_parts)})")
    if keyword:
        clauses.append("content LIKE ?")
        params.append(f"%{keyword}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT * FROM agent_memory {where} ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    )
    return [dict(r) for r in cur.fetchall()]


def get_recent_memories(*, memory_type: str | None = None, limit: int = 20) -> list[dict]:
    conn = get_db()
    if memory_type:
        cur = conn.execute(
            "SELECT * FROM agent_memory WHERE memory_type=? ORDER BY created_at DESC LIMIT ?",
            (memory_type, limit),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM agent_memory ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def search_memory_by_keywords(keywords: list[str], limit: int = 5) -> list[dict]:
    conn = get_db()
    if not keywords:
        return []
    clauses = ["content LIKE ?" for _ in keywords]
    params = [f"%{kw}%" for kw in keywords]
    cur = conn.execute(
        f"SELECT * FROM agent_memory WHERE ({' OR '.join(clauses)}) ORDER BY created_at DESC LIMIT ?",
        params + [limit],
    )
    return [dict(r) for r in cur.fetchall()]


def prune_memories(keep_days: int = 90) -> int:
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(days=keep_days)).isoformat()
    with conn:
        cur = conn.execute(
            "DELETE FROM agent_memory WHERE created_at < ? AND memory_type != 'preference'",
            (cutoff,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Sync metadata
# ---------------------------------------------------------------------------

def update_sync_meta(table_name: str, row_count: int) -> None:
    conn = get_db()
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO sync_meta
               (table_name, last_synced_at, row_count) VALUES (?, datetime('now'), ?)""",
            (table_name, row_count),
        )


def get_sync_meta(table_name: str) -> dict | None:
    conn = get_db()
    cur = conn.execute(
        "SELECT * FROM sync_meta WHERE table_name=?", (table_name,)
    )
    row = cur.fetchone()
    return dict(row) if row else None


def needs_sync(table_name: str, max_age_hours: int = 6) -> bool:
    meta = get_sync_meta(table_name)
    if not meta:
        return True
    try:
        last = datetime.fromisoformat(meta["last_synced_at"])
        return datetime.utcnow() - last > timedelta(hours=max_age_hours)
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Chat log — 对话记录
# ---------------------------------------------------------------------------

def save_chat_log(
    session_id: str,
    role: str,
    content: str,
    *,
    model: str = "",
    provider: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    elapsed_s: float = 0,
    error: str = "",
    tool_calls_json: str = "",
) -> int:
    conn = get_db()
    with conn:
        cur = conn.execute(
            """INSERT INTO chat_log
               (session_id, role, content, model, provider,
                tokens_in, tokens_out, elapsed_s, error, tool_calls)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, role, content, model, provider,
             tokens_in, tokens_out, elapsed_s, error, tool_calls_json),
        )
        return cur.lastrowid or 0


def load_chat_logs(*, session_id: str | None = None, limit: int = 200) -> list[dict]:
    conn = get_db()
    if session_id:
        cur = conn.execute(
            "SELECT * FROM chat_log WHERE session_id=? ORDER BY created_at ASC",
            (session_id,),
        )
    else:
        cur = conn.execute(
            "SELECT * FROM chat_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in cur.fetchall()]


def get_session_preview(session_id: str) -> str:
    """取会话首条用户消息作为摘要预览。"""
    conn = get_db()
    cur = conn.execute(
        "SELECT content FROM chat_log WHERE session_id=? AND role='user' "
        "ORDER BY created_at ASC LIMIT 1",
        (session_id,),
    )
    row = cur.fetchone()
    if row:
        t = (row["content"] or "").strip().replace("\n", " ")
        return t[:60] + ("…" if len(t) > 60 else "")
    return "(空会话)"


# ---------------------------------------------------------------------------
# Tail-buy history
# ---------------------------------------------------------------------------

def save_tail_buy_results(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = get_db()
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO tail_buy_history
               (code, name, run_date, signal_date, signal_type, status,
                final_decision, rule_score, priority_score, rule_reasons,
                llm_decision, llm_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            [
                (
                    str(r.get("code", "")).strip(),
                    str(r.get("name", "")).strip(),
                    str(r.get("run_date", "")).strip(),
                    str(r.get("signal_date", "")).strip(),
                    str(r.get("signal_type", "")).strip(),
                    str(r.get("status", "")).strip(),
                    str(r.get("final_decision", "")).strip(),
                    float(r.get("rule_score", 0) or 0),
                    float(r.get("priority_score", 0) or 0),
                    str(r.get("rule_reasons", "")).strip(),
                    str(r.get("llm_decision", "")).strip(),
                    str(r.get("llm_reason", "")).strip(),
                )
                for r in rows
            ],
        )
    return len(rows)


def load_tail_buy_history(
    *,
    run_date: str = "",
    decision: str = "",
    limit: int = 50,
) -> list[dict]:
    conn = get_db()
    clauses: list[str] = []
    params: list[Any] = []
    if run_date:
        clauses.append("run_date = ?")
        params.append(run_date.strip())
    if decision:
        clauses.append("final_decision = ?")
        params.append(decision.strip().upper())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cur = conn.execute(
        f"SELECT * FROM tail_buy_history {where} ORDER BY run_date DESC, priority_score DESC LIMIT ?",
        params + [min(max(limit, 1), 200)],
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Chat sessions
# ---------------------------------------------------------------------------

def delete_chat_session(session_id: str) -> int:
    conn = get_db()
    with conn:
        cur = conn.execute(
            "DELETE FROM chat_log WHERE session_id=?", (session_id,),
        )
    return cur.rowcount


def list_chat_sessions(limit: int = 50) -> list[dict]:
    """返回最近的会话列表，每个会话的首条用户消息作为摘要。"""
    conn = get_db()
    cur = conn.execute(
        """SELECT session_id,
                  MIN(created_at) AS started_at,
                  MAX(created_at) AS ended_at,
                  COUNT(*) AS msg_count,
                  SUM(tokens_in) AS total_tokens_in,
                  SUM(tokens_out) AS total_tokens_out,
                  MAX(CASE WHEN error != '' THEN error ELSE NULL END) AS last_error
           FROM chat_log
           GROUP BY session_id
           ORDER BY MAX(created_at) DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]
