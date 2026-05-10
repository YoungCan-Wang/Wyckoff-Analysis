from __future__ import annotations

import threading
import time

_cache: dict[str, tuple[float, bool]] = {}
_lock = threading.Lock()
_TTL = 600  # 10 minutes


def is_user_in_cache_whitelist(user_id: str) -> bool:
    if not user_id or user_id == "local":
        return False

    now = time.monotonic()
    with _lock:
        entry = _cache.get(user_id)
        if entry and (now - entry[0]) < _TTL:
            return entry[1]

    allowed = _query_whitelist(user_id)
    with _lock:
        _cache[user_id] = (time.monotonic(), allowed)
    return allowed


def _query_whitelist(user_id: str) -> bool:
    try:
        from integrations.supabase_base import create_admin_client

        client = create_admin_client()
        resp = client.table("whitelist").select("user_id").eq("user_id", user_id).limit(1).execute()
        return bool(resp.data)
    except Exception:
        return False


def invalidate_cache(user_id: str = "") -> None:
    with _lock:
        if user_id:
            _cache.pop(user_id, None)
        else:
            _cache.clear()
