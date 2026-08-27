"""新增下载适配器单测（2026-06-12 下载能力大改）。

覆盖纯逻辑 + mock：
- robust_fetch：_looks_like_pdf / _derive_article_url / fetch_pdf_simple 两级降级
- publisher_direct：candidate_pdf_urls 出版商模板
- openalex：probe_oa 解析（is_oa + pdf_urls）
- crossref：link[] 抽 pdf
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paper_fetch import proxy as proxy_pool

# ---------- robust_fetch ----------

def test_looks_like_pdf():
    from paper_fetch.robust_fetch import _looks_like_pdf
    assert _looks_like_pdf(b"%PDF-1.4 ...") is True
    assert _looks_like_pdf(b"<!DOCTYPE html>") is False
    assert _looks_like_pdf(b"") is False
    assert _looks_like_pdf(None) is False
    # 容忍前导 BOM / 空白 / NUL（CDN/代理注入），合法 PDF 不误杀
    assert _looks_like_pdf(b"\xef\xbb\xbf%PDF-1.5 ...") is True  # UTF-8 BOM
    assert _looks_like_pdf(b"\r\n%PDF-1.7 ...") is True          # 前导 CRLF
    assert _looks_like_pdf(b"   %PDF-1.4 ...") is True           # 前导空格
    # 但挑战页/HTML 仍判 False（前 1KB 内无 %PDF-）
    assert _looks_like_pdf(b"<html><title>Client Challenge</title></html>") is False


def test_derive_article_url():
    from paper_fetch.robust_fetch import _derive_article_url
    # 显式 referer 优先
    assert _derive_article_url("https://x/a.pdf", "https://ref") == "https://ref"
    # 去掉 .pdf 还原文章页
    assert _derive_article_url("https://www.nature.com/articles/x.pdf", None) == \
        "https://www.nature.com/articles/x"
    # 去掉 /pdf
    assert _derive_article_url("https://site/article/pdf", None) == "https://site/article"
    # 其它原样
    assert _derive_article_url("https://site/foo", None) == "https://site/foo"


@pytest.mark.asyncio
async def test_fetch_pdf_simple_httpx_hit():
    from paper_fetch import robust_fetch
    with patch.object(robust_fetch, "_httpx_get", AsyncMock(return_value=b"%PDF-1.7 ok")):
        out = await robust_fetch.fetch_pdf_simple("https://x/a.pdf")
    assert out == b"%PDF-1.7 ok"


@pytest.mark.asyncio
async def test_fetch_pdf_simple_falls_to_curl_cffi():
    from paper_fetch import robust_fetch
    with patch.object(robust_fetch, "_httpx_get", AsyncMock(return_value=b"<html>challenge</html>")), \
         patch.object(robust_fetch, "_curl_cffi_get", AsyncMock(return_value=b"%PDF-1.5 via cffi")):
        out = await robust_fetch.fetch_pdf_simple("https://x/a.pdf")
    assert out == b"%PDF-1.5 via cffi"


@pytest.mark.asyncio
async def test_fetch_pdf_via_browser_respects_budget():
    from paper_fetch import robust_fetch
    from paper_fetch.robust_fetch import FetchBudget
    # budget=0 → 不开浏览器，直接 None
    out = await robust_fetch.fetch_pdf_via_browser("https://x/a.pdf", budget=FetchBudget(browser=0))
    assert out is None
    # budget=1 → 调 _browser_get 一次后扣到 0
    b = FetchBudget(browser=1)
    with patch.object(robust_fetch, "_browser_get", AsyncMock(return_value=b"%PDF-x")):
        out = await robust_fetch.fetch_pdf_via_browser("https://x/a.pdf", budget=b)
    assert out == b"%PDF-x"
    assert b.browser == 0


@pytest.mark.asyncio
async def test_browser_get_polls_until_challenge_returns_pdf():
    """浏览器初次拿到挑战页后持续轮询，不能按固定短等待直接失败。"""
    from paper_fetch import robust_fetch

    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    session = MagicMock()
    session.page = page
    # context.route 是 async（robust_fetch 装浏览器出口重定向拦截用），必须 AsyncMock
    context = MagicMock()
    context.route = AsyncMock()
    session.context = context
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    browser_session = MagicMock(return_value=session)
    context_get = AsyncMock(side_effect=[
        b"<html>proof of work</html>",
        b"<html>proof of work</html>",
        b"%PDF-1.7 resolved",
    ])

    with patch("paper_fetch.browser_session.BrowserSession", browser_session), \
         patch.object(robust_fetch, "_browser_context_get", context_get):
        out = await robust_fetch._browser_get(
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC13289604/pdf/lnag014.pdf",
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC13289604/",
        )

    assert out == b"%PDF-1.7 resolved"
    assert context_get.await_count == 3
    assert page.wait_for_timeout.await_count == 3


# ---------- publisher_direct ----------

def test_candidate_urls_nature():
    from paper_fetch.publisher_direct_adapter import candidate_pdf_urls
    urls = candidate_pdf_urls("10.1038/s41467-023-44511-5", None)
    assert "https://www.nature.com/articles/s41467-023-44511-5.pdf" in urls


def test_candidate_urls_springer_frontiers_pnas():
    from paper_fetch.publisher_direct_adapter import candidate_pdf_urls
    assert "https://link.springer.com/content/pdf/10.1186/abc.pdf" in \
        candidate_pdf_urls("10.1186/abc", None)
    assert "https://www.frontiersin.org/articles/10.3389/fimmu.2023.1/pdf" in \
        candidate_pdf_urls("10.3389/fimmu.2023.1", None)
    assert "https://www.pnas.org/doi/pdf/10.1073/pnas.123" in \
        candidate_pdf_urls("10.1073/pnas.123", None)


def test_candidate_urls_mdpi_from_doi():
    from paper_fetch.publisher_direct_adapter import candidate_pdf_urls
    urls = candidate_pdf_urls("10.3390/pharmaceutics18060752", None)
    assert urls[:2] == [
        "https://mdpi-res.com/d_attachment/pharmaceutics/pharmaceutics-18-00752/article_deploy/pharmaceutics-18-00752.pdf",
        "https://mdpi-res.com/d_attachment/pharmaceutics/pharmaceutics-18-00752/article_deploy/pharmaceutics-18-00752-v2.pdf",
    ]


def test_candidate_urls_from_paper_url_and_dedupe():
    from paper_fetch.publisher_direct_adapter import candidate_pdf_urls
    # 论文页 URL 也能推 Nature 直链；与 DOI 推出的同一条去重
    urls = candidate_pdf_urls(
        "10.1038/x", "https://www.nature.com/articles/x")
    assert urls.count("https://www.nature.com/articles/x.pdf") == 1


def test_candidate_urls_empty_for_unknown():
    from paper_fetch.publisher_direct_adapter import candidate_pdf_urls
    assert candidate_pdf_urls("10.9999/unknown", "https://random.org/x") == []


# ---------- openalex probe ----------

def _mock_httpx_json(payload: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_probe_oa_open_with_pdf():
    from paper_fetch import openalex_adapter
    payload = {
        "open_access": {"is_oa": True, "oa_url": "https://oa/x"},
        "primary_location": {"pdf_url": "https://pub/x.pdf"},
        "locations": [{"pdf_url": "https://repo/x.pdf"}],
        "best_oa_location": {"pdf_url": "https://pub/x.pdf"},
    }
    with patch.object(proxy_pool.httpx, "AsyncClient", return_value=_mock_httpx_json(payload)):
        is_oa, urls, not_found = await openalex_adapter.probe_oa("10.1/x")
    assert is_oa is True
    assert not_found is False
    assert urls[0] == "https://oa/x"
    assert "https://pub/x.pdf" in urls and "https://repo/x.pdf" in urls
    # 去重：pub/x.pdf 只出现一次
    assert urls.count("https://pub/x.pdf") == 1


@pytest.mark.asyncio
async def test_probe_oa_closed():
    from paper_fetch import openalex_adapter
    payload = {"open_access": {"is_oa": False}, "locations": []}
    with patch.object(proxy_pool.httpx, "AsyncClient", return_value=_mock_httpx_json(payload)):
        is_oa, urls, not_found = await openalex_adapter.probe_oa("10.1/closed")
    assert is_oa is False
    assert not_found is False
    assert urls == []


@pytest.mark.asyncio
async def test_probe_oa_not_found_returns_none():
    from paper_fetch import openalex_adapter
    with patch.object(proxy_pool.httpx, "AsyncClient",
                      return_value=_mock_httpx_json({}, status=404)):
        is_oa, urls, not_found = await openalex_adapter.probe_oa("10.1/missing")
    assert is_oa is None
    assert urls == []
    # 404 = OpenAlex 明确未收录，给调用方 not_found 信号去复核 DOI 存在性
    assert not_found is True


# ---------- crossref ----------

@pytest.mark.asyncio
async def test_crossref_picks_pdf_link():
    from paper_fetch import crossref_adapter
    payload = {"message": {"link": [
        {"URL": "https://pub/full.xml", "content-type": "application/xml"},
        {"URL": "https://pub/full.pdf", "content-type": "application/pdf"},
    ]}}
    with patch.object(proxy_pool.httpx, "AsyncClient", return_value=_mock_httpx_json(payload)), \
         patch.object(crossref_adapter, "fetch_pdf_simple",
                      AsyncMock(side_effect=lambda u, referer=None: b"%PDF-ok" if u.endswith(".pdf") else None)):
        out = await crossref_adapter.fetch_via_crossref("10.1/x")
    assert out == b"%PDF-ok"
