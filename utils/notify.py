"""
统一通知：飞书 + 企微 + 钉钉 + Telegram。按配置的 webhook 分别发送，互不影响。
"""

from __future__ import annotations

import os

import requests

from integrations.tickflow_notice import append_tickflow_limit_hint

# ── Telegram ──

TELEGRAM_MAX_LEN = 3900


def _split_telegram_message(content: str, max_len: int = TELEGRAM_MAX_LEN) -> list[str]:
    """将超长文本按行分割为多段，每段不超过 max_len 字符。"""
    if len(content) <= max_len:
        return [content]
    chunks: list[str] = []
    cur = ""
    for line in content.splitlines(keepends=True):
        if len(line) > max_len:
            if cur:
                chunks.append(cur.rstrip("\n"))
                cur = ""
            start = 0
            while start < len(line):
                chunks.append(line[start : start + max_len].rstrip("\n"))
                start += max_len
            continue
        if len(cur) + len(line) <= max_len:
            cur += line
        else:
            if cur:
                chunks.append(cur.rstrip("\n"))
            cur = line
    if cur:
        chunks.append(cur.rstrip("\n"))
    return chunks


def send_to_telegram(
    message_text: str,
    *,
    tg_bot_token: str,
    tg_chat_id: str,
) -> bool:
    """发送 Telegram Bot 消息。token 或 chat_id 为空则跳过。"""
    token = str(tg_bot_token or "").strip()
    chat_id = str(tg_chat_id or "").strip()
    message_text = append_tickflow_limit_hint(message_text)
    if not token or not chat_id:
        print("[telegram] tg_bot_token/tg_chat_id 未配置，跳过 Telegram 推送")
        return False

    proxy_url = os.getenv("PROXY_URL", "").strip()
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split_telegram_message(message_text)
    for idx, chunk in enumerate(chunks, start=1):
        payload = {
            "chat_id": chat_id,
            "text": chunk if len(chunks) == 1 else f"[{idx}/{len(chunks)}]\n{chunk}",
            "disable_web_page_preview": True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=15, proxies=proxies)
            if resp.status_code != 200:
                print(f"[telegram] 推送失败: status={resp.status_code}, body={resp.text[:200]}")
                return False
        except Exception as e:
            print(f"[telegram] 推送异常: {e}")
            return False
    return True


# ── 企微 / 钉钉（共享逻辑） ──

_MARKDOWN_MAX_BYTES = 4000


def _send_webhook_markdown(tag: str, webhook_url: str, title: str, content: str) -> bool:
    """企微/钉钉 Markdown webhook 的共享发送逻辑。"""
    url = str(webhook_url or "").strip()
    if not url:
        return False
    content = append_tickflow_limit_hint(content)
    body = f"# {title}\n\n{content}" if title else content
    if len(body.encode("utf-8")) > _MARKDOWN_MAX_BYTES:
        body = body[: _MARKDOWN_MAX_BYTES // 2] + "\n\n...(内容过长已截断)"
    if tag == "dingtalk":
        payload = {"msgtype": "markdown", "markdown": {"title": title or "通知", "text": body}}
    else:
        payload = {"msgtype": "markdown", "markdown": {"content": body}}
    try:
        resp = requests.post(url, headers={"Content-Type": "application/json"}, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"[{tag}] http {resp.status_code}: {resp.text[:200]}")
            return False
        data = resp.json()
        if data.get("errcode") != 0:
            print(f"[{tag}] errcode {data.get('errcode')}: {data.get('errmsg', '')}")
            return False
        return True
    except Exception as e:
        print(f"[{tag}] exception: {e}")
        return False


def send_wecom_notification(webhook_url: str, title: str, content: str) -> bool:
    return _send_webhook_markdown("wecom", webhook_url, title, content)


def send_dingtalk_notification(webhook_url: str, title: str, content: str) -> bool:
    return _send_webhook_markdown("dingtalk", webhook_url, title, content)


def send_all_webhooks(
    feishu_url: str,
    wecom_url: str,
    dingtalk_url: str,
    title: str,
    content: str,
    *,
    tg_bot_token: str = "",
    tg_chat_id: str = "",
) -> None:
    """
    向已配置的飞书、企微、钉钉、Telegram 各发一条通知；某个 URL/token 为空则跳过该渠道。
    飞书使用 utils.feishu.send_feishu_notification（支持分片）；企微/钉钉/Telegram 使用本模块。
    """
    # 各渠道内部已做空值守卫，这里直接调用
    try:
        from utils.feishu import send_feishu_notification

        send_feishu_notification(feishu_url, title, content)
    except Exception as e:
        if feishu_url and feishu_url.strip():
            print(f"[notify] feishu failed: {e}")
    try:
        send_wecom_notification(wecom_url, title, content)
    except Exception as e:
        if wecom_url and wecom_url.strip():
            print(f"[notify] wecom failed: {e}")
    try:
        send_dingtalk_notification(dingtalk_url, title, content)
    except Exception as e:
        if dingtalk_url and dingtalk_url.strip():
            print(f"[notify] dingtalk failed: {e}")
    if tg_bot_token and tg_chat_id:
        try:
            tg_content = f"{title}\n\n{content}" if title else content
            send_to_telegram(tg_content, tg_bot_token=tg_bot_token, tg_chat_id=tg_chat_id)
        except Exception as e:
            print(f"[notify] telegram failed: {e}")
