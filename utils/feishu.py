# -*- coding: utf-8 -*-
"""
飞书 Webhook 通知，纯工具函数。

配置来源由调用方决定，互不耦合：
- Streamlit：使用用户登录后 Supabase 中的 feishu_webhook
- 定时任务：使用 GitHub Actions 的 FEISHU_WEBHOOK_URL secret
"""
from __future__ import annotations

import requests


def send_feishu_notification(webhook_url: str, title: str, content: str) -> bool:
    """发送飞书卡片消息。webhook_url 由调用方传入，为空时返回 False。"""
    if not webhook_url or not webhook_url.strip():
        return False

    headers = {"Content-Type": "application/json"}
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": content}}
            ],
        },
    }

    try:
        resp = requests.post(webhook_url.strip(), headers=headers, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"Feishu notification failed: {e}")
        return False
