from __future__ import annotations

from utils import feishu


def _sample_tail_buy_report() -> str:
    lines = [
        "⏰ Tail Buy 2026-04-27 16:05:42",
        "",
        "- 候选来源: signal_pending + recommendation_tracking (signal_date/recommend_date=2026-04-24; rec_only=0)",
        "- 扫描数量: 80",
        "- 分层结果: BUY=6 / WATCH=13 / SKIP=61",
        "- LLM 二判: 14/19",
        "- LLM 路由: gemini:gemini-pro-latest",
        "- 总耗时: 57.9s",
        "",
        "⚠️ 风险提醒: UNKNOWN/NORMAL（常态） | 风险提示文案",
        "",
        "## 持仓动作建议（加仓/减仓）",
        "- 持仓来源: portfolio=USER_LIVE:demo, state_sig=abc",
        "- 持仓数量: 3",
        "- 动作分布: ADD=0 / HOLD（持有观察）=1 / TRIM（减仓）=2",
        "",
        "### ADD（可考虑加仓）",
        "- 无",
        "",
        "### TRIM（可考虑减仓）",
        "- 300613 富瀚微 | 持仓=100股 | 现价=60.21 | 浮盈=-6.2%",
        "",
        "### HOLD（持有观察）",
        "- 300590 移为通信 | 持仓=2100股 | 现价=14.02 | 浮盈=-4.5%",
        "",
        "## BUY（优先关注）",
        "- 603060 国检集团 | priority=112.0 | rule=BUY(100.0)",
        "",
        "## WATCH（观察）",
        "- 600985 淮北矿业 | priority=84.6 | rule=BUY(81.6)",
        "",
        "## SKIP（暂不买入）",
    ]
    for i in range(15):
        lines.append(f"- 600{i:03d} 示例{i} | priority=1.{i} | rule=SKIP(2.{i})")
    lines.extend(["", "说明：本任务仅输出尾盘扫描建议，不生成订单，不写入交易表。"])
    return "\n".join(lines)


def test_send_tail_buy_card_uses_rich_card_and_keeps_full_items_by_default(monkeypatch):
    captured = {}

    def fake_post_rich_card(webhook_url: str, title: str, elements: list, template: str = "blue"):
        captured["webhook_url"] = webhook_url
        captured["title"] = title
        captured["elements"] = elements
        captured["template"] = template
        return True, "ok"

    monkeypatch.setattr(feishu, "_post_rich_card", fake_post_rich_card)

    ok = feishu.send_tail_buy_card(
        webhook_url="https://example.com/hook",
        title="⏰ Tail Buy 2026-04-27",
        content=_sample_tail_buy_report(),
    )
    assert ok is True
    assert captured["template"] == "blue"

    body_text = "\n".join(
        str(el.get("text", {}).get("content", "")) for el in captured["elements"] if isinstance(el, dict)
    )
    assert "持仓动作建议（加仓/减仓）" in body_text
    assert "BUY（优先关注）" in body_text
    assert "WATCH（观察）" in body_text
    assert "SKIP（暂不买入）" in body_text
    assert "600014 示例14" in body_text
    assert "其余" not in body_text
