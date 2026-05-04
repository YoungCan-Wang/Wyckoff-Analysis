"""Append-only agent run scratchpad.

Each CLI/TUI turn can write a JSONL trace under ``~/.wyckoff/scratchpad``.
The file is deliberately independent from SQLite chat logs so partial runs,
crashes, and long tool calls still leave an inspectable trail.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

_SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|password|secret|authorization|cookie)", re.IGNORECASE)
_MAX_INLINE_STRING = 200_000


def wyckoff_home() -> Path:
    """Return the local Wyckoff state directory."""

    return Path(os.getenv("WYCKOFF_HOME", Path.home() / ".wyckoff")).expanduser()


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _scrub(value: Any) -> Any:
    """Make values JSON-safe and redact obvious secrets."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            cleaned[key_text] = "***REDACTED***" if _SENSITIVE_KEY_RE.search(key_text) else _scrub(item)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [_scrub(item) for item in value]
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > _MAX_INLINE_STRING:
            return value[:_MAX_INLINE_STRING] + f"\n...[truncated in scratchpad, original chars={len(value)}]"
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


class AgentScratchpad:
    """JSONL trace for a single agent turn."""

    def __init__(
        self,
        query: str,
        *,
        session_id: str = "",
        scratchpad_dir: Path | None = None,
    ) -> None:
        self.query = query
        self.session_id = session_id
        self.dir = scratchpad_dir or wyckoff_home() / "scratchpad"
        self.dir.mkdir(parents=True, exist_ok=True)

        query_hash = hashlib.sha1(query.encode("utf-8", errors="ignore")).hexdigest()[:12]
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.path = self.dir / f"{stamp}_{query_hash}.jsonl"

        self.append(
            {
                "type": "init",
                "timestamp": _timestamp(),
                "session_id": session_id,
                "content": query,
            }
        )

    def append(self, entry: dict[str, Any]) -> None:
        safe_entry = _scrub(entry)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(safe_entry, ensure_ascii=False, default=str))
            fh.write("\n")

    def record_thinking(self, content: str) -> None:
        if not content:
            return
        self.append(
            {
                "type": "thinking",
                "timestamp": _timestamp(),
                "content": content,
            }
        )

    def record_tool_result(
        self,
        tool_name: str,
        args: dict[str, Any],
        result: Any,
        *,
        duration_ms: int | None = None,
        status: str = "ok",
    ) -> None:
        entry: dict[str, Any] = {
            "type": "tool_result",
            "timestamp": _timestamp(),
            "toolName": tool_name,
            "args": args,
            "result": result,
            "status": status,
        }
        if duration_ms is not None:
            entry["durationMs"] = duration_ms
        self.append(entry)

    def record_compaction(self, *, before_messages: int, after_messages: int) -> None:
        self.append(
            {
                "type": "compaction",
                "timestamp": _timestamp(),
                "beforeMessages": before_messages,
                "afterMessages": after_messages,
            }
        )

    def record_final(
        self,
        content: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        elapsed_s: float = 0.0,
    ) -> None:
        self.append(
            {
                "type": "final",
                "timestamp": _timestamp(),
                "content": content,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "elapsed_s": round(elapsed_s, 3),
                },
            }
        )

    def record_error(self, error: str, *, elapsed_s: float = 0.0) -> None:
        self.append(
            {
                "type": "error",
                "timestamp": _timestamp(),
                "error": error,
                "elapsed_s": round(elapsed_s, 3),
            }
        )
