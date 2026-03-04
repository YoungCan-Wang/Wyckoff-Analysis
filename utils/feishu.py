# -*- coding: utf-8 -*-
"""
飞书 Webhook 通知，纯工具函数。

配置来源由调用方决定，互不耦合：
- Streamlit：使用用户登录后 Supabase 中的 feishu_webhook
- 定时任务：使用 GitHub Actions 的 FEISHU_WEBHOOK_URL secret
"""
from __future__ import annotations

import os
import re
import requests
import time


_TERM_GLOSSARY_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Regime / risk state
    (re.compile(r"\bBLACK_SWAN\b(?!\s*[（(])"), "BLACK_SWAN（黑天鹅高风险）"),
    (re.compile(r"\bRISK_OFF\b(?!\s*[（(])"), "RISK_OFF（风险收缩）"),
    (re.compile(r"\bRISK_ON\b(?!\s*[（(])"), "RISK_ON（风险偏好）"),
    (re.compile(r"\bNORMAL\b(?!\s*[（(])"), "NORMAL（常态）"),
    (re.compile(r"\bPANIC_REPAIR\b(?!\s*[（(])"), "PANIC_REPAIR（恐慌修复）"),
    # Macro / market indicators
    (re.compile(r"\bVIX\b(?!\s*[（(])"), "VIX（波动率恐慌指数）"),
    (re.compile(r"\bA50\b(?!\s*[（(])"), "A50（富时中国A50期货）"),
    (re.compile(r"\bATR\b(?!\s*[（(])"), "ATR（真实波动幅度）"),
    (re.compile(r"\bRPS\b(?!\s*[（(])"), "RPS（相对强弱百分位）"),
    (re.compile(r"\bQPS\b(?!\s*[（(])"), "QPS（每秒请求量）"),
    # OMS actions
    (re.compile(r"\bFULL_ATTACK\b(?!\s*[（(])"), "FULL_ATTACK（全仓进攻）"),
    (re.compile(r"\bLIGHT_ADD\b(?!\s*[（(])"), "LIGHT_ADD（轻量加仓）"),
    (re.compile(r"\bATTACK\b(?!\s*[（(])"), "ATTACK（进攻建仓）"),
    (re.compile(r"\bPROBE\b(?!\s*[（(])"), "PROBE（试探建仓）"),
    (re.compile(r"\bTRIM\b(?!\s*[（(])"), "TRIM（减仓）"),
    (re.compile(r"\bHOLD\b(?!\s*[（(])"), "HOLD（持有观察）"),
    (re.compile(r"\bEXIT\b(?!\s*[（(])"), "EXIT（清仓离场）"),
    (re.compile(r"\bNO_TRADE\b(?!\s*[（(])"), "NO_TRADE（拒单）"),
    (re.compile(r"\bAPPROVED\b(?!\s*[（(])"), "APPROVED（核准执行）"),
    # Wyckoff terms
    (re.compile(r"\bComposite Man\b(?!\s*[（(])"), "Composite Man（综合人/主力）"),
    (re.compile(r"\bTape Reading\b(?!\s*[（(])"), "Tape Reading（盘面解读）"),
    (re.compile(r"\bSpring\b(?!\s*[（(])"), "Spring（弹簧/假跌破）"),
    (re.compile(r"\bLPS\b(?!\s*[（(])"), "LPS（最后支撑点）"),
    (re.compile(r"\bSOS\b(?!\s*[（(])"), "SOS（强势信号）"),
    (re.compile(r"\bUTAD\b(?!\s*[（(])"), "UTAD（上冲诱多）"),
    (re.compile(r"\bEVR\b(?!\s*[（(])"), "EVR（努力无结果）"),
    (re.compile(r"\bJAC\b(?!\s*[（(])"), "JAC（跃过小溪）"),
    (re.compile(r"\bBUEC\b(?!\s*[（(])"), "BUEC（回踩小溪边缘）"),
    # Common trade terms
    (re.compile(r"\bStop[- ]?Loss\b(?!\s*[（(])", re.IGNORECASE), "Stop-Loss（止损位）"),
    (re.compile(r"\bEntry\b(?!\s*[（(])", re.IGNORECASE), "Entry（入场区）"),
    (re.compile(r"\bTarget\b(?!\s*[（(])", re.IGNORECASE), "Target（目标位）"),
]


def _annotate_financial_terms(content: str) -> str:
    """
    将常见金融英文术语补充为“术语（中文解释）”，提升飞书可读性。
    已带括号解释的术语会跳过，避免重复注释。
    """
    if not content:
        return content
    out = content
    for pattern, replacement in _TERM_GLOSSARY_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


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

    annotated = _annotate_financial_terms(content)
    normalized = _normalize_for_lark_md(annotated)
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
