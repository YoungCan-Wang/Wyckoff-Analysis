"""Tool result context budgeting helpers."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from cli.scratchpad import wyckoff_home

MAX_TOOL_RESULT_CHARS = 50_000
PREVIEW_CHARS = 2_000

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def serialize_tool_result(result: Any) -> str:
    """Serialize a tool result exactly once for message context."""

    return json.dumps(result, ensure_ascii=False, default=str)


def _safe_part(value: str) -> str:
    cleaned = _SAFE_RE.sub("_", value or "unknown").strip("_")
    return cleaned[:80] or "unknown"


def persist_large_tool_result(tool_name: str, tool_call_id: str, content: str) -> Path:
    """Persist a large tool result and return the file path."""

    results_dir = wyckoff_home() / "tool-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:10]
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    filename = f"{stamp}_{_safe_part(tool_name)}_{_safe_part(tool_call_id)}_{digest}.json"
    path = results_dir / filename
    path.write_text(content, encoding="utf-8")
    return path


def format_tool_result_for_context(
    tool_name: str,
    tool_call_id: str,
    result: Any,
    *,
    max_chars: int = MAX_TOOL_RESULT_CHARS,
) -> str:
    """Return a context-safe tool message body.

    Small results stay inline. Large results are written to disk and replaced
    with a preview plus a path the agent can read back if needed.
    """

    content = serialize_tool_result(result)
    if len(content) <= max_chars:
        return content

    path = persist_large_tool_result(tool_name, tool_call_id, content)
    size_kb = max(1, round(len(content.encode("utf-8")) / 1024))
    preview = content[:PREVIEW_CHARS]
    return (
        f"[工具结果已保存到 {path} ({size_kb} KB)]\n\n预览:\n{preview}\n\n如需完整内容，请调用 read_file 读取该路径。"
    )
