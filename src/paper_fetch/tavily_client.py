"""最小 Tavily 搜索客户端（号池轮换），供 web_pdf_discovery / 预印本发现用。

从 PaperPilot paper_search.tavily_adapter + tavily_pool 内联迁移，只保留下载链
需要的 ``search`` 能力（extract 不需要）；key 来源改为 FetchConfig
（显式列表 / 号池文件 / PAPER_FETCH_TAVILY_API_KEY 环境变量），**本库不内置任何 key**。

额度硬规则（沿用 D-06）：quota 用尽抛 TavilyQuotaExhausted，上层决定跳段还是上抛。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any

import httpx

from .config import get_config

logger = logging.getLogger(__name__)

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilyQuotaExhausted(Exception):
    """Tavily 额度用尽（HTTP 429 / key 失效 401/402/403/432 / 响应含 credits）。"""


class TavilyError(Exception):
    """其他 Tavily HTTP / 网络 / 解析错误。"""


@dataclass(frozen=True)
class WebResult:
    """搜索结果最小投影（下载链只用这四个字段，不需要完整 PaperCandidate）。"""

    title: str
    url: str
    content: str | None = None
    score: float | None = None


# ---------------------------------------------------------------------------
# 号池：进程内轮换下标（key 全部用尽才抛 TavilyQuotaExhausted）
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_idx = 0


def reset_pool_for_tests() -> None:
    global _idx
    _idx = 0


def has_keys() -> bool:
    return len(get_config().tavily_keys_resolved()) > 0


def ordered_keys() -> list[str]:
    """从当前轮换位起的完整 key 列表（调用方按序尝试，额度错误就 advance 再试）。"""
    keys = get_config().tavily_keys_resolved()
    if not keys:
        return []
    with _lock:
        i = _idx % len(keys)
    return keys[i:] + keys[:i]


def advance() -> None:
    global _idx
    with _lock:
        _idx += 1


# ---------------------------------------------------------------------------
# 搜索
# ---------------------------------------------------------------------------


def _raise_for_tavily_response(resp: httpx.Response) -> None:
    """把 Tavily 的鉴权、额度和 HTTP 错误统一映射为异常（与 PaperPilot 同口径）。"""
    if resp.status_code == 429:
        raise TavilyQuotaExhausted("Tavily 额度用尽（HTTP 429）")
    if resp.status_code in (401, 402, 403, 432):
        raise TavilyQuotaExhausted(f"Tavily key 失效/额度用尽（HTTP {resp.status_code}）")
    if resp.status_code == 200 and "credits" in resp.text.lower():
        raise TavilyQuotaExhausted("Tavily 额度用尽（响应含 credits）")
    if resp.status_code >= 400:
        raise TavilyQuotaExhausted(f"Tavily 返回 HTTP {resp.status_code}")


def _parse_result(item: dict) -> WebResult | None:
    title = (item.get("title") or "").strip()
    url = (item.get("url") or "").strip()
    if not title or not url:
        return None
    content = (item.get("content") or "").strip()
    return WebResult(
        title=title,
        url=url,
        content=content[:500] or None,
        score=item.get("score"),
    )


async def _search_once(
    query: str,
    limit: int,
    api_key: str,
    include_domains: list[str] | None,
    search_depth: str,
    http_client: httpx.AsyncClient | None,
) -> list[WebResult]:
    own_client = http_client is None
    if own_client:
        from .proxy import async_client_for

        http_client = async_client_for(_TAVILY_SEARCH_URL, timeout=httpx.Timeout(60.0))
    try:
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": search_depth,
            "max_results": limit,
            "include_answer": False,
            "include_raw_content": False,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            resp = await http_client.post(_TAVILY_SEARCH_URL, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise TavilyError(f"Tavily 网络错误：{exc}") from exc
        _raise_for_tavily_response(resp)
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise TavilyError(f"Tavily 响应 JSON 解析失败：{exc}") from exc
        out: list[WebResult] = []
        for item in data.get("results") or []:
            parsed = _parse_result(item)
            if parsed is not None:
                out.append(parsed)
        return out
    finally:
        if own_client:
            await http_client.aclose()


async def search_with_pool(
    query: str,
    limit: int = 10,
    *,
    http_client: httpx.AsyncClient | None = None,
    include_domains: list[str] | None = None,
    search_depth: str = "basic",
) -> list[WebResult]:
    """按号池顺序逐个 key 搜索；单 key 额度尽自动轮下一个，全池用尽才抛异常。

    号池为空 → 抛 ValueError（调用方 catch 改走「跳过」分支，下载链不炸）。
    """
    keys = ordered_keys()
    if not keys:
        raise ValueError("Tavily 号池为空（key 列表 / 号池文件 / 环境变量均无 key）")
    for key in keys:
        try:
            return await _search_once(
                query, limit, key, include_domains, search_depth, http_client
            )
        except TavilyQuotaExhausted:
            advance()
    logger.warning("tavily_client: 号池全部 %d 个 key 都额度用尽/失效", len(keys))
    raise TavilyQuotaExhausted("Tavily 号池全部额度用尽")
