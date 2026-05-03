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

_SCHEMA_VERSION = 7

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
    metadata TEXT DEFAULT '',
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

CREATE TABLE IF NOT EXISTS background_task_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    session_id TEXT DEFAULT '',
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    result_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id)
);

CREATE INDEX IF NOT EXISTS idx_rec_date ON recommendation_tracking(recommend_date);
CREATE INDEX IF NOT EXISTS idx_sig_status ON signal_pending(status);
CREATE INDEX IF NOT EXISTS idx_mem_type ON agent_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_mem_codes ON agent_memory(codes);
CREATE INDEX IF NOT EXISTS idx_chatlog_session ON chat_log(session_id);
CREATE INDEX IF NOT EXISTS idx_chatlog_created ON chat_log(created_at);
CREATE INDEX IF NOT EXISTS idx_tail_run_date ON tail_buy_history(run_date);
CREATE INDEX IF NOT EXISTS idx_tail_decision ON tail_buy_history(final_decision);
CREATE INDEX IF NOT EXISTS idx_bg_task_session ON background_task_result(session_id);
CREATE INDEX IF NOT EXISTS idx_bg_task_created ON background_task_result(created_at);

-- FTS5 全文检索索引（记忆系统 hybrid search）
CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
    content,
    content=agent_memory,
    content_rowid=id,
    tokenize='unicode61'
);

-- 保持 FTS5 与 agent_memory 同步的触发器
CREATE TRIGGER IF NOT EXISTS trg_mem_ai AFTER INSERT ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_ad AFTER DELETE ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS trg_mem_au AFTER UPDATE ON agent_memory BEGIN
    INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
END;
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
    if current < 5:
        _backfill_background_tasks_from_chat_log(conn)
    if current < 6:
        _migrate_fts5_memory(conn)
    if current < 7:
        try:
            conn.execute("ALTER TABLE chat_log ADD COLUMN metadata TEXT DEFAULT ''")
        except Exception:
            pass
    if current < _SCHEMA_VERSION:
        conn.execute(
            "INSERT OR REPLACE INTO schema_version(version) VALUES(?)",
            (_SCHEMA_VERSION,),
        )
        conn.commit()


def _backfill_background_tasks_from_chat_log(conn: sqlite3.Connection) -> None:
    """Backfill historical background completions that were only saved as chat messages."""
    try:
        rows = conn.execute(
            """SELECT id, session_id, content, created_at
               FROM chat_log
               WHERE role='user' AND content LIKE '[后台任务完成] %'"""
        ).fetchall()
    except sqlite3.Error:
        return
    for row in rows:
        content = str(row["content"] or "")
        rest = content.removeprefix("[后台任务完成] ").strip()
        tool_name = rest.split(":", 1)[0].strip() or "background"
        status = "failed" if '"error"' in content or "'error'" in content else "completed"
        payload = {"raw": content}
        if ":" in rest:
            raw_json = rest.split(":", 1)[1].strip()
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                payload = {"raw": raw_json}
        result_json = json.dumps(payload, ensure_ascii=False, default=str)
        summary = result_json[:2000] + ("..." if len(result_json) > 2000 else "")
        conn.execute(
            """INSERT OR IGNORE INTO background_task_result
               (task_id, session_id, tool_name, status, result_json, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                f"chatlog_{row['id']}",
                row["session_id"] or "",
                tool_name,
                status,
                result_json,
                summary,
                row["created_at"],
            ),
        )


def _migrate_fts5_memory(conn: sqlite3.Connection) -> None:
    """为已有 agent_memory 数据创建 FTS5 索引。"""
    try:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory_fts USING fts5(
                content, content=agent_memory, content_rowid=id, tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS trg_mem_ai AFTER INSERT ON agent_memory BEGIN
                INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_mem_ad AFTER DELETE ON agent_memory BEGIN
                INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS trg_mem_au AFTER UPDATE ON agent_memory BEGIN
                INSERT INTO agent_memory_fts(agent_memory_fts, rowid, content) VALUES ('delete', old.id, old.content);
                INSERT INTO agent_memory_fts(rowid, content) VALUES (new.id, new.content);
            END;
        """)
        # 回填已有数据
        rows = conn.execute("SELECT id, content FROM agent_memory").fetchall()
        for row in rows:
            try:
                conn.execute(
                    "INSERT INTO agent_memory_fts(rowid, content) VALUES (?, ?)",
                    (row["id"], row["content"]),
                )
            except Exception:
                pass
    except Exception:
        pass


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
    cur = conn.execute("SELECT data_json FROM market_signal_daily ORDER BY trade_date DESC LIMIT 1")
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
    cur = conn.execute("SELECT * FROM portfolio WHERE portfolio_id=?", (portfolio_id,))
    row = cur.fetchone()
    if not row:
        return None
    pos_cur = conn.execute("SELECT * FROM portfolio_position WHERE portfolio_id=?", (portfolio_id,))
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
    portfolio_id: str,
    code: str,
    name: str,
    shares: int,
    cost_price: float,
    buy_dt: str = "",
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


def search_memory_fts(query: str, limit: int = 10) -> list[dict]:
    """FTS5 全文检索记忆。"""
    conn = get_db()
    try:
        cur = conn.execute(
            """SELECT m.*, bm25(agent_memory_fts) AS rank
               FROM agent_memory_fts fts
               JOIN agent_memory m ON m.id = fts.rowid
               WHERE agent_memory_fts MATCH ?
               ORDER BY rank
               LIMIT ?""",
            (query, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:
        return []


def search_memory_hybrid(
    *,
    query_text: str,
    codes: list[str] | None = None,
    keywords: list[str] | None = None,
    limit: int = 8,
    decay_half_life_days: float = 30.0,
) -> list[dict]:
    """Hybrid search: FTS5 全文 + 代码匹配 + 关键词 LIKE + 时间衰减加权。

    返回按综合得分排序的记忆列表，每条带 _score 字段。
    """
    import math
    from datetime import datetime

    candidates: dict[int, dict] = {}

    def _merge(items: list[dict], source_weight: float) -> None:
        for m in items:
            mid = m["id"]
            if mid not in candidates:
                m["_score"] = source_weight
                candidates[mid] = m
            else:
                candidates[mid]["_score"] = max(candidates[mid].get("_score", 0), source_weight)

    # 1. FTS5 全文检索（最高权重）
    if query_text and len(query_text.strip()) >= 2:
        fts_results = search_memory_fts(query_text, limit=limit * 2)
        _merge(fts_results, 1.0)

    # 2. 股票代码精确匹配
    if codes:
        code_results = search_memory(codes=codes, limit=limit * 2)
        _merge(code_results, 0.85)

    # 3. 关键词 LIKE 检索
    if keywords:
        kw_results = search_memory_by_keywords(keywords, limit=limit * 2)
        _merge(kw_results, 0.6)

    # 4. 时间衰减加权
    now = datetime.utcnow()
    for m in candidates.values():
        created = m.get("created_at", "")
        if created:
            try:
                dt = datetime.fromisoformat(str(created))
                age_days = max((now - dt).total_seconds() / 86400, 0)
                decay = math.pow(2, -age_days / decay_half_life_days)
            except (ValueError, TypeError):
                decay = 0.5
        else:
            decay = 0.5
        # 偏好记忆不衰减
        if m.get("memory_type") == "preference":
            decay = 1.0
        m["_score"] = m.get("_score", 0.5) * decay

    # 按得分排序
    ranked = sorted(candidates.values(), key=lambda x: x.get("_score", 0), reverse=True)
    return ranked[:limit]


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
    cur = conn.execute("SELECT * FROM sync_meta WHERE table_name=?", (table_name,))
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
    metadata_json: str = "",
) -> int:
    conn = get_db()
    with conn:
        cur = conn.execute(
            """INSERT INTO chat_log
               (session_id, role, content, model, provider,
                tokens_in, tokens_out, elapsed_s, error, tool_calls, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                role,
                content,
                model,
                provider,
                tokens_in,
                tokens_out,
                elapsed_s,
                error,
                tool_calls_json,
                metadata_json,
            ),
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


def save_background_task_result(
    task_id: str,
    tool_name: str,
    result: Any,
    *,
    session_id: str = "",
    status: str = "completed",
) -> int:
    """Persist a completed CLI background task result for dashboard history."""
    result_json = json.dumps(result, ensure_ascii=False, default=str)
    summary = result_json
    if len(summary) > 2000:
        summary = summary[:2000] + "..."
    conn = get_db()
    with conn:
        cur = conn.execute(
            """INSERT OR REPLACE INTO background_task_result
               (task_id, session_id, tool_name, status, result_json, summary, created_at)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
            (task_id, session_id, tool_name, status, result_json, summary),
        )
        return cur.lastrowid or 0


def load_background_task_results(*, limit: int = 100) -> list[dict]:
    conn = get_db()
    cur = conn.execute(
        """SELECT id, task_id, session_id, tool_name, status, summary, created_at
           FROM background_task_result
           ORDER BY created_at DESC
           LIMIT ?""",
        (min(max(limit, 1), 500),),
    )
    return [dict(r) for r in cur.fetchall()]


def load_background_task_result(task_id: str) -> dict | None:
    conn = get_db()
    cur = conn.execute(
        "SELECT * FROM background_task_result WHERE task_id=?",
        (task_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["result"] = json.loads(data.get("result_json") or "{}")
    except json.JSONDecodeError:
        data["result"] = data.get("result_json") or ""
    return data


def get_session_preview(session_id: str) -> str:
    """取会话首条用户消息作为摘要预览。"""
    conn = get_db()
    cur = conn.execute(
        "SELECT content FROM chat_log WHERE session_id=? AND role='user' ORDER BY created_at ASC LIMIT 1",
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
            "DELETE FROM chat_log WHERE session_id=?",
            (session_id,),
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
                  MAX(CASE WHEN error != '' THEN error ELSE NULL END) AS last_error,
                  MAX(CASE WHEN role='assistant' THEN model ELSE NULL END) AS model,
                  (SELECT content FROM chat_log c2 WHERE c2.session_id=chat_log.session_id AND c2.role='user' ORDER BY c2.created_at ASC LIMIT 1) AS first_user_msg,
                  SUM(elapsed_s) AS total_elapsed_s
           FROM chat_log
           GROUP BY session_id
           ORDER BY MAX(created_at) DESC
           LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def cleanup_old_records(days: int = 30) -> dict[str, int]:
    """删除 N 天前的 chat_log / background_task_result / agent_memory 记录。"""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    deleted: dict[str, int] = {}
    with conn:
        for table in ("chat_log", "background_task_result", "agent_memory"):
            cur = conn.execute(
                f"DELETE FROM {table} WHERE created_at < ?",
                (cutoff,),
            )
            deleted[table] = cur.rowcount
    return deleted
