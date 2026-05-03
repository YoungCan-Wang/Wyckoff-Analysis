from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_OWNER = "YoungCan-Wang"
DEFAULT_REPO = "Wyckoff-Analysis"
DEFAULT_REF = "feature/visible"
DEFAULT_WORKFLOW_FILE = "web_quant_jobs.yml"


def _optional_cache(ttl: int, **kwargs):
    """Return ``st.cache_data`` decorator when Streamlit is available, else a no-op."""
    try:
        import streamlit as st

        return st.cache_data(ttl=ttl, **kwargs)
    except Exception:
        return lambda fn: fn


@dataclass
class WorkflowRun:
    run_id: int
    status: str
    conclusion: str | None
    html_url: str
    display_title: str
    created_at: str
    updated_at: str
    run_number: int


def _secrets_get(name: str, default: str = "") -> str:
    val = str(os.getenv(name, "") or "").strip()
    if val:
        return val
    try:
        import streamlit as st

        if hasattr(st, "secrets") and name in st.secrets:
            return str(st.secrets.get(name) or "").strip()
    except Exception:
        pass
    return default


def _config() -> dict[str, str]:
    return {
        "token": _secrets_get("GITHUB_ACTIONS_TOKEN"),
        "owner": _secrets_get("GITHUB_ACTIONS_REPO_OWNER", DEFAULT_OWNER),
        "repo": _secrets_get("GITHUB_ACTIONS_REPO_NAME", DEFAULT_REPO),
        "ref": _secrets_get("GITHUB_ACTIONS_REF", DEFAULT_REF),
        "workflow": _secrets_get("GITHUB_ACTIONS_WORKFLOW_FILE", DEFAULT_WORKFLOW_FILE),
        "allow_user_ids": _secrets_get("GITHUB_ACTIONS_ALLOWED_USER_IDS"),
    }


def github_actions_ready() -> tuple[bool, str]:
    cfg = _config()
    if not cfg["token"]:
        return (False, "未配置 GITHUB_ACTIONS_TOKEN")
    return (True, "")


def background_jobs_allowed_for_user(user_id: str) -> bool:
    cfg = _config()
    allow_raw = str(cfg.get("allow_user_ids", "") or "").strip()
    if not allow_raw:
        return True
    allow_set = {x.strip() for x in allow_raw.replace(";", ",").split(",") if x.strip()}
    return str(user_id or "").strip() in allow_set


def _headers() -> dict[str, str]:
    cfg = _config()
    token = cfg["token"]
    if not token:
        raise RuntimeError("GITHUB_ACTIONS_TOKEN 未配置")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _base_api() -> str:
    cfg = _config()
    return f"https://api.github.com/repos/{cfg['owner']}/{cfg['repo']}"


def create_request_id(prefix: str = "web") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def clear_github_actions_caches() -> None:
    for fn in (find_run_by_request_id, list_recent_runs, load_result_json_for_run):
        if hasattr(fn, "clear"):
            fn.clear()


def trigger_web_job(job_kind: str, payload: dict[str, Any]) -> str:
    cfg = _config()
    request_id = create_request_id(job_kind)
    url = f"{_base_api()}/actions/workflows/{cfg['workflow']}/dispatches"
    body = {
        "ref": cfg["ref"],
        "inputs": {
            "job_kind": job_kind,
            "request_id": request_id,
            "payload_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    }
    resp = requests.post(url, headers=_headers(), json=body, timeout=20)
    if resp.status_code not in {200, 201, 204}:
        raise RuntimeError(f"触发 GitHub Actions 失败: HTTP {resp.status_code} {resp.text[:300]}")
    clear_github_actions_caches()
    return request_id


def _parse_run(row: dict[str, Any]) -> WorkflowRun:
    return WorkflowRun(
        run_id=int(row.get("id") or 0),
        status=str(row.get("status", "") or ""),
        conclusion=(str(row.get("conclusion")) if row.get("conclusion") is not None else None),
        html_url=str(row.get("html_url", "") or ""),
        display_title=str(row.get("display_title", "") or row.get("name", "") or ""),
        created_at=str(row.get("created_at", "") or ""),
        updated_at=str(row.get("updated_at", "") or ""),
        run_number=int(row.get("run_number") or 0),
    )


@_optional_cache(ttl=8, show_spinner=False, max_entries=20)
def find_run_by_request_id(request_id: str, *, per_page: int = 20) -> WorkflowRun | None:
    cfg = _config()
    url = (
        f"{_base_api()}/actions/workflows/{cfg['workflow']}/runs"
        f"?event=workflow_dispatch&branch={cfg['ref']}&per_page={per_page}"
    )
    resp = requests.get(url, headers=_headers(), timeout=20)
    resp.raise_for_status()
    runs = resp.json().get("workflow_runs", []) or []
    for row in runs:
        title = str(row.get("display_title", "") or row.get("name", "") or "")
        if request_id in title:
            return _parse_run(row)
    return None


@_optional_cache(ttl=10, show_spinner=False, max_entries=10)
def list_recent_runs(*, per_page: int = 10) -> list[WorkflowRun]:
    cfg = _config()
    url = (
        f"{_base_api()}/actions/workflows/{cfg['workflow']}/runs"
        f"?event=workflow_dispatch&branch={cfg['ref']}&per_page={per_page}"
    )
    resp = requests.get(url, headers=_headers(), timeout=20)
    resp.raise_for_status()
    rows = resp.json().get("workflow_runs", []) or []
    return [_parse_run(row) for row in rows]


def _list_run_artifacts(run_id: int) -> list[dict[str, Any]]:
    url = f"{_base_api()}/actions/runs/{int(run_id)}/artifacts"
    resp = requests.get(url, headers=_headers(), timeout=20)
    resp.raise_for_status()
    return resp.json().get("artifacts", []) or []


@_optional_cache(ttl=10, show_spinner=False, max_entries=20)
def load_result_json_for_run(run_id: int) -> dict[str, Any] | None:
    artifacts = _list_run_artifacts(run_id)
    target = next(
        (item for item in artifacts if str(item.get("name", "")).startswith("web-job-result-")),
        None,
    )
    if not target:
        return None
    artifact_id = int(target.get("id") or 0)
    if artifact_id <= 0:
        return None
    url = f"{_base_api()}/actions/artifacts/{artifact_id}/zip"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                with zf.open(name) as f:
                    return json.load(f)
    return None


def load_latest_result(
    job_kind: str,
    *,
    requested_by_user_id: str = "",
    per_page: int = 10,
) -> tuple[WorkflowRun | None, dict[str, Any] | None]:
    for run in list_recent_runs(per_page=per_page):
        if run.status != "completed" or run.conclusion not in ("success", "failure"):
            continue
        result = load_result_json_for_run(run.run_id)
        if not result:
            continue
        if str(result.get("job_kind", "") or "") != job_kind:
            continue
        if requested_by_user_id and str(result.get("requested_by_user_id", "") or "") != requested_by_user_id:
            continue
        return (run, result)
    return (None, None)
