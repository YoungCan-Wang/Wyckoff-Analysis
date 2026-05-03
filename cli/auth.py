"""CLI 认证 — 复用 Supabase Auth，session 持久化到 ~/.wyckoff/session.json。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SESSION_DIR = Path.home() / ".wyckoff"
SESSION_FILE = SESSION_DIR / "session.json"

from core.constants import SUPABASE_ANON_KEY as _SUPABASE_KEY
from core.constants import SUPABASE_ANON_URL as _SUPABASE_URL

# ---------------------------------------------------------------------------
# Session 文件读写
# ---------------------------------------------------------------------------


def _save_session(data: dict[str, Any]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _load_session() -> dict[str, Any] | None:
    if not SESSION_FILE.exists():
        return None
    try:
        return json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _clear_session() -> None:
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 登录 / 登出 / 恢复
# ---------------------------------------------------------------------------


def _create_client():
    """用内置的 anon key 创建 Supabase 客户端（不依赖 .env）。"""
    from supabase import create_client

    return create_client(_SUPABASE_URL, _SUPABASE_KEY)


def login(email: str, password: str) -> dict[str, Any]:
    """
    用邮箱密码登录 Supabase，返回用户信息。
    同时将凭证持久化到 wyckoff.json，token 过期时可自动重登。

    Returns: {"user_id": str, "email": str, "access_token": str, "refresh_token": str}
    Raises: Exception on auth failure
    """
    client = _create_client()
    resp = client.auth.sign_in_with_password({"email": email, "password": password})

    data = {
        "user_id": resp.user.id,
        "email": resp.user.email,
        "access_token": resp.session.access_token,
        "refresh_token": resp.session.refresh_token,
    }
    _save_session(data)
    # 持久化凭证到 wyckoff.json，用于 token 过期后自动重登
    save_config_key("email", email)
    save_config_key("password", password)
    return data


def _auto_relogin() -> dict[str, Any] | None:
    """尝试用 wyckoff.json 中保存的凭证自动重登。"""
    cfg = _load_config()
    email = str(cfg.get("email", "") or "").strip()
    password = str(cfg.get("password", "") or "").strip()
    if not email or not password:
        return None
    try:
        return login(email, password)
    except Exception:
        logger.debug("Auto re-login failed", exc_info=True)
        return None


def restore_session() -> dict[str, Any] | None:
    """
    从 ~/.wyckoff/session.json 恢复登录态。
    token 过期时自动用保存的凭证重登。

    Returns: 同 login() 的返回值，或 None（无 session / token 过期）。
    """
    data = _load_session()
    if not data or not data.get("access_token") or not data.get("refresh_token"):
        # 无 session 文件，尝试自动登录
        return _auto_relogin()

    try:
        client = _create_client()
        client.auth.set_session(data["access_token"], data["refresh_token"])
        user_resp = client.auth.get_user()

        if not user_resp or not user_resp.user:
            _clear_session()
            return _auto_relogin()

        # token 可能被 refresh，更新本地缓存
        session = client.auth.get_session()
        if session:
            data["access_token"] = session.access_token
            data["refresh_token"] = session.refresh_token
            _save_session(data)

        return data
    except Exception as e:
        logger.debug("Session restore failed", exc_info=True)
        err = str(e).lower()
        # 仅在 token 确认无效时清除；网络异常保留本地 session
        if "invalid" in err or "expired" in err or "revoked" in err:
            _clear_session()
            return _auto_relogin()
        # 网络问题：保留 session，用本地缓存的 token 继续
        return data


def logout() -> None:
    """清除本地 session。"""
    _clear_session()


# ---------------------------------------------------------------------------
# 统一配置文件 ~/.wyckoff/wyckoff.json
# ---------------------------------------------------------------------------

CONFIG_FILE = SESSION_DIR / "wyckoff.json"
_OLD_CONFIG_FILE = SESSION_DIR / "config.json"


def _load_config() -> dict[str, Any]:
    """加载配置文件，首次运行自动迁移旧 config.json。"""
    if not CONFIG_FILE.exists() and _OLD_CONFIG_FILE.exists():
        try:
            _OLD_CONFIG_FILE.rename(CONFIG_FILE)
        except OSError:
            pass
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_config(data: dict[str, Any]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """旧 flat 格式 → models 列表。只在检测到旧格式时调用一次。"""
    entry = {
        "id": data.get("provider_name", "default"),
        "provider_name": data["provider_name"],
        "api_key": data["api_key"],
        "model": data.get("model", ""),
        "base_url": data.get("base_url", ""),
    }
    return {"models": [entry], "default": entry["id"]}


def _ensure_models_format(data: dict[str, Any]) -> dict[str, Any]:
    """确保配置为 models 列表格式，必要时迁移并持久化。"""
    if "models" in data:
        return data
    if data.get("provider_name") and data.get("api_key"):
        migrated = _migrate_config(data)
        _save_config(migrated)
        return migrated
    return data


def load_model_configs() -> list[dict[str, Any]]:
    """返回有序 models 列表。"""
    data = _ensure_models_format(_load_config())
    return data.get("models", [])


def load_default_model_id() -> str | None:
    """返回默认模型 id。"""
    data = _ensure_models_format(_load_config())
    models = data.get("models", [])
    default = data.get("default", "")
    if default and any(m["id"] == default for m in models):
        return default
    return models[0]["id"] if models else None


def save_model_entry(entry: dict[str, Any]) -> None:
    """按 id 插入或更新一条模型配置。首条自动设为默认。"""
    data = _ensure_models_format(_load_config())
    models = data.get("models", [])
    # 更新已有 or 追加
    found = False
    for i, m in enumerate(models):
        if m["id"] == entry["id"]:
            models[i] = entry
            found = True
            break
    if not found:
        models.append(entry)
    data["models"] = models
    if not data.get("default") or not any(m["id"] == data["default"] for m in models):
        data["default"] = models[0]["id"]
    _save_config(data)


def remove_model_entry(model_id: str) -> bool:
    """删除模型。返回 False 表示是最后一条不允许删。"""
    data = _ensure_models_format(_load_config())
    models = data.get("models", [])
    if len(models) <= 1:
        return False
    data["models"] = [m for m in models if m["id"] != model_id]
    if data.get("default") == model_id:
        data["default"] = data["models"][0]["id"] if data["models"] else ""
    _save_config(data)
    return True


def set_default_model(model_id: str) -> None:
    """设置默认模型。"""
    data = _ensure_models_format(_load_config())
    models = data.get("models", [])
    if any(m["id"] == model_id for m in models):
        data["default"] = model_id
        _save_config(data)


def load_fallback_model_id() -> str:
    """返回 fallback 模型 id（空字符串表示未设置）。"""
    data = _ensure_models_format(_load_config())
    fb = data.get("fallback", "")
    models = data.get("models", [])
    if fb and any(m["id"] == fb for m in models):
        return fb
    return ""


def set_fallback_model(model_id: str) -> None:
    """设置 fallback 模型。空字符串清除设置。"""
    data = _ensure_models_format(_load_config())
    if model_id:
        models = data.get("models", [])
        if not any(m["id"] == model_id for m in models):
            return
    data["fallback"] = model_id
    _save_config(data)


# --- 向后兼容 ---


def save_model_config(config: dict[str, Any]) -> None:
    """将模型配置合并写入 wyckoff.json（向后兼容）。"""
    entry = dict(config)
    if "id" not in entry:
        entry["id"] = entry.get("provider_name", "default")
    save_model_entry(entry)


def load_model_config() -> dict[str, Any] | None:
    """加载默认模型配置（向后兼容）。"""
    configs = load_model_configs()
    if not configs:
        return None
    default_id = load_default_model_id()
    for m in configs:
        if m["id"] == default_id:
            return m
    return configs[0]


def load_config() -> dict[str, Any]:
    """加载完整配置。"""
    return _load_config()


def save_config_key(key: str, value: Any) -> None:
    """写入单个配置项。"""
    data = _load_config()
    data[key] = value
    _save_config(data)
