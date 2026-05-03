"""
模型输入/输出 debug 落盘工具。

step3 / step4 共用的 _dump_model_input 统一提取至此。
通过 DEBUG_MODEL_IO / DEBUG_MODEL_IO_FULL 环境变量启用。
"""

from __future__ import annotations

import os
import re
from datetime import datetime

DEBUG_MODEL_IO: bool = os.getenv("DEBUG_MODEL_IO", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEBUG_MODEL_IO_FULL: bool = os.getenv("DEBUG_MODEL_IO_FULL", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def dump_model_input(
    *,
    step_prefix: str,
    model: str,
    system_prompt: str,
    user_message: str,
    symbols: list[str] | None = None,
    items: list[dict] | None = None,
    name_hint: str = "",
) -> str:
    """
    将模型输入落盘到 $LOGS_DIR/<step_prefix>_model_input_<ts>.txt。

    Parameters
    ----------
    step_prefix : str
        日志前缀，例如 "step3" 或 "step4"。
    model, system_prompt, user_message : str
        模型名、系统提示词、用户消息。
    symbols : list[str] | None
        股票代码列表（step4 风格）。
    items : list[dict] | None
        候选字典列表（step3 风格，取 code 字段拼成 symbols）。
    name_hint : str
        文件名后缀提示。

    Returns
    -------
    str
        落盘路径，未启用时返回空串。
    """
    if not DEBUG_MODEL_IO:
        return ""

    logs_dir = os.getenv("LOGS_DIR", "logs")
    os.makedirs(logs_dir, exist_ok=True)
    hint = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name_hint or "").strip())[:32]
    suffix = f"_{hint}" if hint else ""
    path = os.path.join(
        logs_dir,
        f"{step_prefix}_model_input_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{suffix}.txt",
    )

    # 统一 symbols 来源
    if symbols is None and items is not None:
        symbols = [str(x.get("code", "")) for x in items]
    symbols = symbols or []

    body = (
        f"[{step_prefix}] model={model}\n"
        f"[{step_prefix}] symbol_count={len(symbols)}\n"
        f"[{step_prefix}] symbols={','.join(symbols)}\n"
        f"[{step_prefix}] system_prompt_len={len(system_prompt)}\n"
        f"[{step_prefix}] user_message_len={len(user_message)}\n"
    )
    if DEBUG_MODEL_IO_FULL:
        body += f"\n===== SYSTEM PROMPT =====\n{system_prompt}\n\n===== USER MESSAGE =====\n{user_message}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"[{step_prefix}] 模型输入已落盘: {path}")
    return path
