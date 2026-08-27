"""web_pdf_discovery_adapter 测试。

覆盖：
- 无 Tavily key 时不进入发现层
- 标题/DOI 相关候选才会被尝试
- 默认跳过预印本站，避免正式版优先级被抢
- 灰色来源屏蔽
- landing page 候选可复用 meta_adapter 抓 citation_pdf_url
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from paper_fetch import web_pdf_discovery_adapter as M
from paper_fetch.tavily_client import WebResult

_TITLE = "NicheTrans: Spatial-aware Cross-omics Translation"


def _cand(title: str, url: str, abstract: str | None = None) -> WebResult:
    return WebResult(title=title, url=url, content=abstract, score=0.8)


def test_can_discover_requires_tavily_key(monkeypatch):
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: False)
    assert M.can_discover_pdf_via_web("10.1038/x", _TITLE) is False

    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)
    assert M.can_discover_pdf_via_web("10.1038/x", None) is True
    assert M.can_discover_pdf_via_web(None, _TITLE) is True
    assert M.can_discover_pdf_via_web(None, "short") is False


@pytest.mark.asyncio
async def test_discover_pdf_via_web_downloads_relevant_pdf(monkeypatch):
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)
    fake_pdf = b"%PDF-web"
    cands = [
        _cand(
            _TITLE,
            "https://publisher.example.org/articles/nichetrans.pdf",
            abstract=f"{_TITLE}. DOI 10.1038/s41592-026-03153-3",
        )
    ]

    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)), \
         patch.object(M, "fetch_pdf_simple", new=AsyncMock(return_value=fake_pdf)) as fetch_mock:
        out = await M.discover_pdf_via_web(
            "10.1038/s41592-026-03153-3",
            _TITLE,
            referer="https://doi.org/10.1038/s41592-026-03153-3",
        )

    # 2026-08-26 起返回 (pdf, 命中候选 URL)——命中 URL 落 attempts/content_url 供排查
    assert out == (fake_pdf, "https://publisher.example.org/articles/nichetrans.pdf")
    fetch_mock.assert_awaited_once_with(
        "https://publisher.example.org/articles/nichetrans.pdf",
        referer="https://doi.org/10.1038/s41592-026-03153-3",
    )


@pytest.mark.asyncio
async def test_discover_pdf_via_web_skips_preprint_by_default(monkeypatch):
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)
    cands = [
        _cand(_TITLE, "https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1.full.pdf")
    ]

    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)), \
         patch.object(M, "fetch_pdf_simple", new=AsyncMock(return_value=b"%PDF-preprint")) as fetch_mock:
        out = await M.discover_pdf_via_web(None, _TITLE)

    assert out is None
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_discover_pdf_via_web_allows_preprint_when_requested(monkeypatch):
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)
    fake_pdf = b"%PDF-preprint"
    cands = [
        _cand(_TITLE, "https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1.full.pdf")
    ]

    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)), \
         patch.object(M, "fetch_pdf_simple", new=AsyncMock(return_value=fake_pdf)):
        out = await M.discover_pdf_via_web(None, _TITLE, allow_preprint=True)

    assert out == (fake_pdf, "https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1.full.pdf")


@pytest.mark.asyncio
async def test_discover_pdf_via_web_blocks_gray_sources(monkeypatch):
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)
    cands = [_cand(_TITLE, "https://sci-hub.se/10.1038/s41592-026-03153-3")]

    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)), \
         patch.object(M, "fetch_pdf_simple", new=AsyncMock(return_value=b"%PDF-gray")) as fetch_mock:
        out = await M.discover_pdf_via_web("10.1038/s41592-026-03153-3", _TITLE)

    assert out is None
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_discover_pdf_via_web_tries_landing_page_candidate(monkeypatch):
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)
    fake_pdf = b"%PDF-from-landing"
    cands = [
        _cand(
            f"{_TITLE} | Publisher",
            "https://publisher.example.org/articles/nichetrans",
            abstract="Full text for NicheTrans: Spatial-aware Cross-omics Translation",
        )
    ]

    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)), \
         patch.object(M, "fetch_pdf_simple", new=AsyncMock(return_value=None)), \
         patch.object(
             M,
             "fetch_via_landing_page",
             new=AsyncMock(return_value=(fake_pdf, None, {})),
         ) as landing_mock:
        out = await M.discover_pdf_via_web(None, _TITLE)

    assert out == (fake_pdf, "https://publisher.example.org/articles/nichetrans")
    landing_mock.assert_awaited_once_with("https://publisher.example.org/articles/nichetrans")


@pytest.mark.asyncio
async def test_discover_pdf_via_web_rejects_abstract_only_title_match(monkeypatch):
    """摘要提到目标论文、结果标题却是另一篇时，不能把该 PDF 当作目标全文。"""
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)
    cands = [
        _cand(
            "A review of spatial multi-omics methods",
            "https://repository.example.org/unrelated-review.pdf",
            abstract=f"This review discusses {_TITLE} and related methods.",
        )
    ]

    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)), \
         patch.object(M, "fetch_pdf_simple", new=AsyncMock(return_value=b"%PDF-wrong")) as fetch_mock:
        out = await M.discover_pdf_via_web(None, _TITLE)

    assert out is None
    fetch_mock.assert_not_awaited()
