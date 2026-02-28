# -*- coding: utf-8 -*-
"""
飞书 Webhook 通知，纯工具函数。

配置来源由调用方决定，互不耦合：
- Streamlit：使用用户登录后 Supabase 中的 feishu_webhook
- 定时任务：使用 GitHub Actions 的 FEISHU_WEBHOOK_URL secret
"""
from __future__ import annotations

import os
import requests
import time


def _normalize_for_lark_md(content: str) -> str:
    """
    飞书 lark_md 不是完整 Markdown：
    - 标题 '#' 在卡片里常不按标题渲染
    - 分割线 '---' 会出现为普通文本
    这里做轻量归一化，保证展示稳定。
    """
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            out.append(f"**{title}**" if title else "")
            continue
        if stripped in {"---", "***", "___"}:
            out.append("")
            continue
        out.append(line)
    return "\n".join(out).strip()


def _split_lark_md(content: str, max_len: int = 2800) -> list[str]:
    """
    飞书卡片单个 lark_md 文本体积有限，长文按段分片。
    """
    if len(content) <= max_len:
        return [content]

    paragraphs = content.split("\n\n")
    chunks: list[str] = []
    current = ""
    for p in paragraphs:
        candidate = p if not current else f"{current}\n\n{p}"
        if len(candidate) <= max_len:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(p) <= max_len:
            current = p
            continue
        start = 0
        while start < len(p):
            chunks.append(p[start:start + max_len])
            start += max_len
    if current:
        chunks.append(current)
    return chunks


def _post_card(webhook_url: str, title: str, chunk: str) -> tuple[bool, str]:
    headers = {"Content-Type": "application/json"}
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": chunk}}
            ],
        },
    }
    resp = requests.post(webhook_url.strip(), headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        return (False, f"http_{resp.status_code}")
    try:
        data = resp.json()
        code = int(data.get("code", -1))
        if code == 0:
            return (True, "ok")
        return (False, f"feishu_code_{code}: {data.get('msg', '')}")
    except Exception:
        return (True, "ok_non_json")


def send_feishu_notification(webhook_url: str, title: str, content: str) -> bool:
    """发送飞书卡片消息。webhook_url 由调用方传入，为空时返回 False。"""
    if not webhook_url or not webhook_url.strip():
        return False

    normalized = _normalize_for_lark_md(content)
    max_len = int(os.getenv("FEISHU_LARK_MAX_LEN", "2800"))
    chunks = _split_lark_md(normalized, max_len=max_len)

    try:
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            part_title = title if total == 1 else f"{title} ({idx}/{total})"
            ok = False
            last_err = "unknown"
            for attempt in range(1, 4):
                ok, err = _post_card(webhook_url, part_title, chunk)
                if ok:
                    print(f"[feishu] sent part {idx}/{total}, len={len(chunk)}, attempt={attempt}")
                    break
                last_err = err
                sleep_s = 0.6 * attempt
                print(
                    f"[feishu] failed part {idx}/{total}, len={len(chunk)}, "
                    f"attempt={attempt}, err={err}, retry_in={sleep_s:.1f}s"
                )
                time.sleep(sleep_s)
            if not ok:
                print(f"Feishu notification failed on part {idx}/{total}: {last_err}")
                return False
            if idx < total:
                time.sleep(0.15)
        return True
    except Exception as e:
        print(f"Feishu notification failed: {e}")
        return False
