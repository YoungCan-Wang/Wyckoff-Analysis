from __future__ import annotations

import os
import re
import time
import uuid
import zipfile
from pathlib import Path
from tempfile import gettempdir

import pandas as pd

_EXPORT_ROOT = Path(os.getenv("STREAMLIT_EXPORT_DIR") or Path(gettempdir()) / "akshare_streamlit_exports")
_EXPORT_TTL_SECONDS = max(
    int(os.getenv("STREAMLIT_EXPORT_TTL_SECONDS", str(12 * 60 * 60))),
    60 * 60,
)
_EXPORT_MAX_FILES = max(int(os.getenv("STREAMLIT_EXPORT_MAX_FILES", "240")), 20)


def _ensure_export_root() -> Path:
    _EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    return _EXPORT_ROOT


def _safe_stem(text: str) -> str:
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", str(text or "").strip())
    stem = stem.strip("._")
    return stem[:80] or "artifact"


def _unique_path(prefix: str, suffix: str) -> Path:
    root = _ensure_export_root()
    ts = time.strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:8]
    return root / f"{_safe_stem(prefix)}_{ts}_{token}{suffix}"


def cleanup_export_artifacts(
    *,
    ttl_seconds: int = _EXPORT_TTL_SECONDS,
    max_files: int = _EXPORT_MAX_FILES,
) -> None:
    root = _ensure_export_root()
    now_ts = time.time()
    files = [p for p in root.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    stale_cutoff = now_ts - max(ttl_seconds, 60)
    for idx, path in enumerate(files):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        should_delete = stat.st_mtime < stale_cutoff or idx >= max_files
        if not should_delete:
            continue
        try:
            path.unlink(missing_ok=True)
        except Exception:
            continue


def write_dataframe_csv(df: pd.DataFrame, *, prefix: str) -> Path:
    path = _unique_path(prefix, ".csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def write_zip_from_files(files: list[tuple[str, str | Path]], *, prefix: str) -> Path:
    zip_path = _unique_path(prefix, ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in files:
            zf.write(Path(path), arcname=arcname)
    return zip_path


def file_loader(path: str | Path):
    target = Path(path)

    def _load() -> bytes:
        return target.read_bytes()

    return _load
