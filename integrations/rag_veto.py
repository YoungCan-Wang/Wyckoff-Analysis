# -*- coding: utf-8 -*-
"""
RAG 防雷：基于新闻检索做负面关键词 veto

默认使用 Tavily Search API（若未配置 TAVILY_API_KEY 则自动跳过）。
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import requests

DEFAULT_NEGATIVE_KEYWORDS = [
    "立案",
    "调查",
    "证监会",
    "处罚",
    "违规",
    "造假",
    "财务造假",
    "退市",
    "st",
    "*st",
    "减持",
    "质押爆仓",
    "债务违约",
    "业绩预亏",
    "业绩下滑",
    "商誉减值",
    "诉讼",
    "仲裁",
    "冻结",
    "无法表示意见",
    "审计保留意见",
]

RAG_TIMEOUT = int(os.getenv("RAG_TIMEOUT", "12"))
RAG_MAX_WORKERS = int(os.getenv("RAG_MAX_WORKERS", "6"))
RAG_NEWS_LOOKBACK_DAYS = int(os.getenv("RAG_NEWS_LOOKBACK_DAYS", "7"))
RAG_MAX_RESULTS = int(os.getenv("RAG_MAX_RESULTS", "5"))


@dataclass
class VetoResult:
    code: str
    name: str
    veto: bool
    hits: list[str]
    evidence: list[str]
    error: str | None = None


def is_rag_veto_enabled() -> bool:
    flag = os.getenv("RAG_VETO_ENABLED", "1").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _normalize_keywords() -> list[str]:
    raw = os.getenv("RAG_NEGATIVE_KEYWORDS", "").strip()
    if not raw:
        return DEFAULT_NEGATIVE_KEYWORDS
    parts = [x.strip().lower() for x in raw.replace("，", ",").split(",") if x.strip()]
    return parts or DEFAULT_NEGATIVE_KEYWORDS


def _tavily_search(query: str, max_results: int = RAG_MAX_RESULTS) -> list[dict[str, Any]]:
    api_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not api_key:
        return []
    url = "https://api.tavily.com/search"
    after = (datetime.utcnow() - timedelta(days=max(RAG_NEWS_LOOKBACK_DAYS, 1))).date().isoformat()
    payload = {
        "api_key": api_key,
        "query": query,
        "search_depth": "basic",
        "topic": "news",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "start_date": after,
    }
    resp = requests.post(url, json=payload, timeout=RAG_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", []) or []


def _serpapi_search(query: str, max_results: int = RAG_MAX_RESULTS) -> list[dict[str, Any]]:
    api_key = (os.getenv("SERPAPI_API_KEY") or "").strip()
    if not api_key:
        return []
    # SerpApi Google News Search
    # 官方参数: engine=google_news, q=..., api_key=..., gl=cn, hl=zh-cn, tbs=qdr:w (过去一周)
    params = {
        "engine": "google_news",
        "q": query,
        "api_key": api_key,
        "gl": "cn",
        "hl": "zh-cn",
        "num": max_results,
        "tbs": "qdr:w",  # past week
    }
    resp = requests.get("https://serpapi.com/search", params=params, timeout=RAG_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    # 转换为统一格式
    out = []
    for item in data.get("news_results", []) or []:
        out.append({
            "title": item.get("title", ""),
            "content": item.get("snippet", ""),
            "url": item.get("link", ""),
        })
    return out


def _scan_one(code: str, name: str, keywords: list[str]) -> VetoResult:
    query = f"{code} {name} A股 公告 风险"
    results = []
    error_msg = None

    # 1) 优先 Tavily；失败或空结果时，再尝试 SerpApi
    try:
        results = _tavily_search(query, max_results=RAG_MAX_RESULTS)
    except Exception as e:
        error_msg = f"tavily_err:{e}"

    if not results:
        try:
            results = _serpapi_search(query, max_results=RAG_MAX_RESULTS)
            if results:
                error_msg = None
        except Exception as e2:
            if error_msg:
                error_msg = f"{error_msg}; serpapi_err:{e2}"
            else:
                error_msg = f"serpapi_err:{e2}"

    if not results and error_msg:
        return VetoResult(code=code, name=name, veto=False, hits=[], evidence=[], error=error_msg)

    text_parts: list[str] = []
    evidence: list[str] = []
    for item in results:
        title = str(item.get("title", "")).strip()
        content = str(item.get("content", "")).strip()
        url = str(item.get("url", "")).strip()
        merged = f"{title}\n{content}".strip()
        if merged:
            text_parts.append(merged.lower())
        if title:
            evidence.append(f"{title} | {url}" if url else title)
    combined = "\n".join(text_parts)

    hits = [kw for kw in keywords if kw.lower() in combined]
    veto = len(hits) > 0
    return VetoResult(code=code, name=name, veto=veto, hits=hits, evidence=evidence[:3], error=None)


def run_negative_news_veto(candidates: list[dict[str, str]]) -> dict[str, VetoResult]:
    """
    candidates: [{"code":"000001","name":"平安银行"}, ...]
    """
    out: dict[str, VetoResult] = {}
    if not is_rag_veto_enabled():
        return out

    tavily_key = (os.getenv("TAVILY_API_KEY") or "").strip()
    serpapi_key = (os.getenv("SERPAPI_API_KEY") or "").strip()
    if not tavily_key and not serpapi_key:
        return out

    keywords = _normalize_keywords()
    items = [
        {"code": str(x.get("code", "")).strip(), "name": str(x.get("name", "")).strip()}
        for x in candidates
        if str(x.get("code", "")).strip()
    ]
    if not items:
        return out

    with ThreadPoolExecutor(max_workers=max(RAG_MAX_WORKERS, 1)) as ex:
        futures = {
            ex.submit(_scan_one, it["code"], it["name"] or it["code"], keywords): it["code"]
            for it in items
        }
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                result = fut.result()
            except Exception as e:
                result = VetoResult(code=code, name=code, veto=False, hits=[], evidence=[], error=str(e))
            out[code] = result
    return out
