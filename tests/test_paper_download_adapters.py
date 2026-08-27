"""PDF 下载 adapter 测试。

覆盖（Phase 13 重构后五段降级链）：
- oa_adapter.fetch_oa_pdf            ：正常字节 / HTML 内容（非 PDF）/ 404 / 超时
- unpaywall_adapter.fetch_via_unpaywall：正常路径 / is_oa=false / 5xx / oa_locations 回退
- preprint_adapter.fetch_preprint_pdf：arXiv / bioRxiv / medRxiv / PLOS / PeerJ / 不匹配返 None
- meta_adapter.fetch_via_landing_page：citation_pdf_url 命中 / link alternate 回退 / 付费墙短路 / DOI 抽取
- europe_pmc_adapter.fetch_via_europe_pmc：S2 返 PMC ID / S2 404 / S2 无 PMC
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# ---------- oa_adapter ----------


# oa_adapter 现委托给 robust_fetch.fetch_pdf_simple（httpx → curl_cffi 两级）。
# 这些测试改为打桩 robust_fetch 的两级抓取助手，验证 magic bytes 校验 + 失败降级。


@pytest.mark.asyncio
async def test_oa_adapter_returns_bytes_on_pdf():
    """fetch_oa_pdf：第一级（httpx）返回 PDF magic bytes 时成功取回 bytes。"""
    from paper_fetch.oa_adapter import fetch_oa_pdf

    fake_pdf = b"%PDF-fake content"
    with patch(
        "paper_fetch.robust_fetch._httpx_get",
        AsyncMock(return_value=fake_pdf),
    ):
        result = await fetch_oa_pdf("https://example.com/paper.pdf")

    assert result == fake_pdf


@pytest.mark.asyncio
async def test_oa_adapter_rejects_html_content():
    """fetch_oa_pdf：两级都返回 HTML 内容（非 PDF magic bytes）时返回 None。"""
    from paper_fetch.oa_adapter import fetch_oa_pdf

    fake_html = b"<html><body>Not a PDF</body></html>"
    with patch(
        "paper_fetch.robust_fetch._httpx_get",
        AsyncMock(return_value=fake_html),
    ), patch(
        "paper_fetch.robust_fetch._curl_cffi_get",
        AsyncMock(return_value=fake_html),
    ):
        result = await fetch_oa_pdf("https://example.com/page")

    assert result is None


@pytest.mark.asyncio
async def test_oa_adapter_returns_none_on_404():
    """fetch_oa_pdf：两级都拿不到内容（如 404 → None）时返回 None，不抛异常。"""
    from paper_fetch.oa_adapter import fetch_oa_pdf

    with patch(
        "paper_fetch.robust_fetch._httpx_get",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.robust_fetch._curl_cffi_get",
        AsyncMock(return_value=None),
    ):
        result = await fetch_oa_pdf("https://example.com/gone.pdf")

    assert result is None


@pytest.mark.asyncio
async def test_oa_adapter_returns_none_on_timeout():
    """fetch_oa_pdf：底层超时被吞成 None 时返回 None，不抛异常。"""
    from paper_fetch.oa_adapter import fetch_oa_pdf

    # _httpx_get 内部已 catch 超时返 None；curl_cffi 同理。两级都 None → 整体 None。
    with patch(
        "paper_fetch.robust_fetch._httpx_get",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.robust_fetch._curl_cffi_get",
        AsyncMock(return_value=None),
    ):
        result = await fetch_oa_pdf("https://example.com/slow.pdf")

    assert result is None


# ---------- unpaywall_adapter ----------


@pytest.mark.asyncio
async def test_unpaywall_adapter_returns_bytes_on_hit():
    """fetch_via_unpaywall：best_oa_location.url_for_pdf 有效时返回 bytes。"""
    from paper_fetch.unpaywall_adapter import fetch_via_unpaywall

    fake_pdf = b"%PDF-unpaywall content"
    unpaywall_resp = {
        "doi": "10.1234/test",
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": "https://oa-server.org/paper.pdf",
            "host_type": "repository",
        },
        "oa_locations": [],
    }

    mock_unp_resp = MagicMock()
    mock_unp_resp.status_code = 200
    mock_unp_resp.raise_for_status = MagicMock()
    mock_unp_resp.json = MagicMock(return_value=unpaywall_resp)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_unp_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("paper_fetch.unpaywall_adapter.httpx.AsyncClient", return_value=mock_client):
        with patch(
            "paper_fetch.unpaywall_adapter.fetch_oa_pdf",
            AsyncMock(return_value=fake_pdf),
        ):
            result = await fetch_via_unpaywall("10.1234/test", "test@example.com")

    assert result == fake_pdf


@pytest.mark.asyncio
async def test_unpaywall_adapter_returns_none_when_not_oa():
    """fetch_via_unpaywall：is_oa=false 时返回 None。"""
    from paper_fetch.unpaywall_adapter import fetch_via_unpaywall

    unpaywall_resp = {
        "doi": "10.1234/paywalled",
        "is_oa": False,
        "best_oa_location": None,
        "oa_locations": [],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=unpaywall_resp)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("paper_fetch.unpaywall_adapter.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_via_unpaywall("10.1234/paywalled", "test@example.com")

    assert result is None


@pytest.mark.asyncio
async def test_unpaywall_adapter_returns_none_on_5xx():
    """fetch_via_unpaywall：Unpaywall API 5xx 时返回 None，不抛异常。"""
    from paper_fetch.unpaywall_adapter import fetch_via_unpaywall

    mock_resp = MagicMock()
    mock_resp.status_code = 500

    def _raise():
        raise httpx.HTTPStatusError("500", request=MagicMock(), response=mock_resp)

    mock_resp.raise_for_status = _raise

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("paper_fetch.unpaywall_adapter.httpx.AsyncClient", return_value=mock_client):
        result = await fetch_via_unpaywall("10.1234/test", "test@example.com")

    assert result is None


@pytest.mark.asyncio
async def test_unpaywall_adapter_falls_back_to_oa_locations():
    """fetch_via_unpaywall：best_oa_location.url_for_pdf 为 None 但 oa_locations 有值时取第一个有效 URL。"""
    from paper_fetch.unpaywall_adapter import fetch_via_unpaywall

    fake_pdf = b"%PDF-fallback"
    unpaywall_resp = {
        "doi": "10.1234/test",
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": None,
            "host_type": "publisher",
        },
        "oa_locations": [
            {"url_for_pdf": None, "host_type": "publisher"},
            {"url_for_pdf": "https://repo.org/paper.pdf", "host_type": "repository"},
        ],
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=unpaywall_resp)

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("paper_fetch.unpaywall_adapter.httpx.AsyncClient", return_value=mock_client):
        with patch(
            "paper_fetch.unpaywall_adapter.fetch_oa_pdf",
            AsyncMock(return_value=fake_pdf),
        ):
            result = await fetch_via_unpaywall("10.1234/test", "test@example.com")

    assert result == fake_pdf


# ---------- preprint_adapter ----------


@pytest.mark.parametrize(
    "paper_url,expected_pdf_url",
    [
        ("https://arxiv.org/abs/2301.00001", "https://arxiv.org/pdf/2301.00001.pdf"),
        ("https://arxiv.org/abs/2301.00001v2", "https://arxiv.org/pdf/2301.00001v2.pdf"),
        ("https://export.arxiv.org/abs/2301.00001", "https://arxiv.org/pdf/2301.00001.pdf"),
        (
            "https://www.biorxiv.org/content/10.1101/2024.05.01.123456v1",
            "https://www.biorxiv.org/content/10.1101/2024.05.01.123456v1.full.pdf",
        ),
        (
            "https://www.medrxiv.org/content/10.1101/2023.07.15.555444",
            "https://www.medrxiv.org/content/10.1101/2023.07.15.555444.full.pdf",
        ),
        (
            "https://www.biorxiv.org/content/10.1101/2024.05.01.123456v1.full",
            "https://www.biorxiv.org/content/10.1101/2024.05.01.123456v1.full.pdf",
        ),
        (
            "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0123456",
            "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0123456&type=printable",
        ),
        ("https://peerj.com/articles/12345", "https://peerj.com/articles/12345.pdf"),
    ],
)
@pytest.mark.asyncio
async def test_preprint_adapter_constructs_pdf_url(paper_url, expected_pdf_url):
    """preprint_adapter：各 preprint 站点 URL 都能映射到对应的 PDF 直链并下载。"""
    from paper_fetch.preprint_adapter import fetch_preprint_pdf

    fake_pdf = b"%PDF-preprint"
    captured: list[str] = []

    async def _fake_fetch(url: str):
        captured.append(url)
        return fake_pdf

    with patch("paper_fetch.preprint_adapter.fetch_oa_pdf", _fake_fetch):
        result = await fetch_preprint_pdf(paper_url)

    assert result == fake_pdf
    assert captured == [expected_pdf_url]


@pytest.mark.asyncio
async def test_preprint_adapter_returns_none_on_unmatched_url():
    """preprint_adapter：非 preprint 站点（如 nature.com）返 None，不调下载。"""
    from paper_fetch.preprint_adapter import fetch_preprint_pdf

    called = AsyncMock()
    with patch("paper_fetch.preprint_adapter.fetch_oa_pdf", called):
        result = await fetch_preprint_pdf("https://www.nature.com/articles/s41586-020-2649-2")

    assert result is None
    called.assert_not_called()


@pytest.mark.asyncio
async def test_preprint_adapter_returns_none_on_empty_url():
    """preprint_adapter：paper_url=None / 空字符串都返 None。"""
    from paper_fetch.preprint_adapter import fetch_preprint_pdf

    assert await fetch_preprint_pdf(None) is None
    assert await fetch_preprint_pdf("") is None


# ---------- meta_adapter ----------


def _mock_html_response(html: str, status_code: int = 200, content_type: str = "text/html"):
    """构造 httpx.AsyncClient 的 mock，返回给定 HTML。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.text = html
    resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_meta_adapter_extracts_citation_pdf_url():
    """meta_adapter：HTML 含 citation_pdf_url 时拿到 PDF 字节 + DOI。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    html = """
    <html><head>
      <meta name="citation_pdf_url" content="https://publisher.org/article/123.pdf">
      <meta name="citation_doi" content="10.1234/abcd.5678">
    </head><body>Article body</body></html>
    """
    fake_pdf = b"%PDF-meta"

    with patch(
        "paper_fetch.meta_adapter.httpx.AsyncClient",
        return_value=_mock_html_response(html),
    ), patch(
        "paper_fetch.meta_adapter.fetch_oa_pdf",
        AsyncMock(return_value=fake_pdf),
    ):
        pdf, doi, info = await fetch_via_landing_page("https://publisher.org/article/123")

    assert pdf == fake_pdf
    assert doi == "10.1234/abcd.5678"
    assert info["requires_auth"] is False


@pytest.mark.asyncio
async def test_meta_adapter_falls_back_to_link_alternate():
    """meta_adapter：无 citation_pdf_url 但有 link rel=alternate type=application/pdf 时回退。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    html = """
    <html><head>
      <link rel="alternate" type="application/pdf" href="/pdf/article.pdf">
    </head></html>
    """
    fake_pdf = b"%PDF-link"
    captured: list[str] = []

    async def _fake_fetch(url: str):
        captured.append(url)
        return fake_pdf

    with patch(
        "paper_fetch.meta_adapter.httpx.AsyncClient",
        return_value=_mock_html_response(html),
    ), patch(
        "paper_fetch.meta_adapter.fetch_oa_pdf",
        _fake_fetch,
    ):
        pdf, doi, info = await fetch_via_landing_page("https://publisher.org/article/abc")

    assert pdf == fake_pdf
    # urljoin 应把相对路径补成绝对 URL
    assert captured == ["https://publisher.org/pdf/article.pdf"]
    assert doi is None


@pytest.mark.asyncio
async def test_meta_adapter_short_circuits_on_paywall_signal():
    """meta_adapter：HTML 含 'subscription required' 时直接返 (None, doi)，不抓 PDF URL。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    html = """
    <html><head>
      <meta name="citation_doi" content="10.9999/paywalled.111">
      <meta name="citation_pdf_url" content="https://x.com/p.pdf">
    </head><body>This page requires subscription required to access full text.</body></html>
    """
    fetched = AsyncMock()

    with patch(
        "paper_fetch.meta_adapter.httpx.AsyncClient",
        return_value=_mock_html_response(html),
    ), patch(
        "paper_fetch.meta_adapter.fetch_oa_pdf",
        fetched,
    ):
        pdf, doi, info = await fetch_via_landing_page("https://elsevier.com/paywalled")

    assert pdf is None
    assert doi == "10.9999/paywalled.111"
    fetched.assert_not_called()
    # 付费墙签名命中 → requires_auth=True，保留 landing_url 给机构登录通道
    assert info["requires_auth"] is True
    assert info["url"] == "https://elsevier.com/paywalled"


@pytest.mark.asyncio
async def test_meta_adapter_returns_none_none_on_http_error():
    """meta_adapter：HTTP 403 时返 (None, None)，不抛异常。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    resp = MagicMock()
    resp.status_code = 403
    resp.headers = {"content-type": "text/html"}

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("paper_fetch.meta_adapter.httpx.AsyncClient", return_value=client):
        pdf, doi, info = await fetch_via_landing_page("https://locked.com/article")

    assert pdf is None
    assert doi is None
    # HTTP 403 是明确付费墙信号
    assert info["requires_auth"] is True


@pytest.mark.asyncio
async def test_meta_adapter_returns_none_none_on_empty_url():
    """meta_adapter：paper_url=None 时直接返 (None, None)，不发请求。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    pdf, doi, info = await fetch_via_landing_page(None)
    assert pdf is None and doi is None


@pytest.mark.asyncio
async def test_meta_adapter_returns_pdf_when_landing_is_pdf():
    """meta_adapter：paper_url 本身就是 PDF（content-type=application/pdf + %PDF 魔数）时，
    直接返回下到的字节，别再当 HTML 解析丢弃（2026-06-20 中文期刊真实踩坑，双保险）。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    pdf_bytes = b"%PDF-1.6\nfake cjm body bytes"
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/pdf"}
    resp.content = pdf_bytes
    resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("paper_fetch.meta_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.meta_adapter.fetch_oa_pdf") as mock_oa:
        pdf, doi, info = await fetch_via_landing_page("https://cjm.dmu.edu.cn/x/pdf/7-2-yuxia.pdf")

    assert pdf == pdf_bytes
    # 拿到直链 PDF 就别再走 citation_pdf_url 二次下载
    mock_oa.assert_not_called()


@pytest.mark.asyncio
async def test_meta_adapter_rejects_fake_pdf_content_type():
    """meta_adapter：content-type 声称 pdf 但正文非 %PDF（挑战页伪装）时不误当 PDF，返 None。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/pdf"}
    resp.content = b"<html>Just a moment... challenge</html>"
    resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch("paper_fetch.meta_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.meta_adapter.fetch_oa_pdf") as mock_oa:
        pdf, doi, info = await fetch_via_landing_page("https://x.org/fake.pdf")

    assert pdf is None
    mock_oa.assert_not_called()
    assert info["requires_auth"] is False


# ---------- europe_pmc_adapter ----------


def _mk_resp(status: int, payload: dict) -> MagicMock:
    """造一个 httpx 风格响应 mock（status_code + json() + raise_for_status）。"""
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _url_dispatch_client(route: dict[str, MagicMock], default: MagicMock | None = None):
    """造一个按 URL 子串分发响应的 httpx.AsyncClient mock。

    route：{URL 子串: 响应 mock}；命中第一个子串就返对应响应，都不命中返 default（默认空 200）。
    """
    async def _get(url: str, **_kw):
        for frag, resp in route.items():
            if frag in url:
                return resp
        return default if default is not None else _mk_resp(200, {})

    client = AsyncMock()
    client.get = _get
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.mark.asyncio
async def test_europe_pmc_adapter_rest_hit_downloads_getpdf():
    """europe_pmc：Europe PMC REST 查到 PMC（inEPMC=Y）→ 走 api/getPdf 主接口下载成功。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc

    rest_resp = _mk_resp(200, {"resultList": {"result": [
        {"pmcid": "PMC1234567", "source": "MED", "inEPMC": "Y"},
    ]}})
    client = _url_dispatch_client({"ebi.ac.uk/europepmc": rest_resp})

    captured: list[str] = []

    async def _fake_fetch(url: str):
        captured.append(url)
        return b"%PDF-pmc"

    with patch("paper_fetch.europe_pmc_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.europe_pmc_adapter.fetch_oa_pdf", _fake_fetch):
        result = await fetch_via_europe_pmc("10.1038/test")

    assert result == b"%PDF-pmc"
    # 主下载接口是 api/getPdf（render 按钮真实终点），不再是老的 ptpmcrender
    assert captured[0] == "https://europepmc.org/api/getPdf?pmcid=PMC1234567"


@pytest.mark.asyncio
async def test_europe_pmc_adapter_rest_skips_ppr_then_s2_fallback():
    """europe_pmc：REST 只命中预印本 PPR 记录（无可下手稿）→ 退 Semantic Scholar 拿 PMC。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc

    # REST 命中 PPR（应被跳过：source=PPR）；S2 给出真 PMC
    rest_resp = _mk_resp(200, {"resultList": {"result": [
        {"pmcid": None, "source": "PPR", "inEPMC": "N"},
    ]}})
    s2_resp = _mk_resp(200, {"externalIds": {"PubMedCentral": "PMC7654321"}})
    client = _url_dispatch_client({
        "ebi.ac.uk/europepmc": rest_resp,
        "api.semanticscholar.org": s2_resp,
    })

    captured: list[str] = []

    async def _fake_fetch(url: str):
        captured.append(url)
        return b"%PDF-s2"

    with patch("paper_fetch.europe_pmc_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.europe_pmc_adapter.fetch_oa_pdf", _fake_fetch):
        result = await fetch_via_europe_pmc("10.1038/test")

    assert result == b"%PDF-s2"
    assert captured[0] == "https://europepmc.org/api/getPdf?pmcid=PMC7654321"


@pytest.mark.asyncio
async def test_europe_pmc_adapter_preprint_resolves_published_then_pmc():
    """europe_pmc：给预印本 DOI，首查无 PMC → 解析正式版 DOI 再查到 PMC（Slide-tags 场景）。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc

    # 预印本 DOI 首查：REST + S2 都查不到可下 PMC
    empty_rest = _mk_resp(200, {"resultList": {"result": []}})
    s2_nopmc = _mk_resp(200, {"externalIds": {"DOI": "10.1101/pre"}})
    # 正式版 DOI 复查：REST 命中真 PMC
    pub_rest = _mk_resp(200, {"resultList": {"result": [
        {"pmcid": "PMC10764288", "source": "MED", "inEPMC": "Y"},
    ]}})

    async def _get(url: str, **kw):
        if "ebi.ac.uk/europepmc" in url:
            # DOI 在 params.query 里（"DOI:10.1038/..."）：正式版走真 PMC，预印本走空
            query = (kw.get("params") or {}).get("query", "")
            return pub_rest if "10.1038" in query else empty_rest
        if "api.semanticscholar.org" in url:
            return s2_nopmc
        return _mk_resp(200, {})

    client = AsyncMock()
    client.get = _get
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    captured: list[str] = []

    async def _fake_fetch(url: str):
        captured.append(url)
        return b"%PDF-published"

    async def _fake_resolve(doi, title=None):
        return "10.1038/s41586-023-06837-4"

    with patch("paper_fetch.europe_pmc_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.europe_pmc_adapter.fetch_oa_pdf", _fake_fetch), \
         patch("paper_fetch.preprint_resolve.resolve_published_doi", _fake_resolve):
        result = await fetch_via_europe_pmc("10.1101/2023.04.01.535228", title="Slide-tags ...")

    assert result == b"%PDF-published"
    assert captured[0] == "https://europepmc.org/api/getPdf?pmcid=PMC10764288"


@pytest.mark.asyncio
async def test_europe_pmc_adapter_getpdf_fails_falls_back_to_ptpmcrender():
    """europe_pmc：api/getPdf 拿不到 → 退老接口 ptpmcrender.fcgi。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc

    rest_resp = _mk_resp(200, {"resultList": {"result": [
        {"pmcid": "PMC222", "source": "MED", "inEPMC": "Y"},
    ]}})
    client = _url_dispatch_client({"ebi.ac.uk/europepmc": rest_resp})

    async def _fake_fetch(url: str):
        return None if "api/getPdf" in url else b"%PDF-old"

    with patch("paper_fetch.europe_pmc_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.europe_pmc_adapter.fetch_oa_pdf", _fake_fetch):
        result = await fetch_via_europe_pmc("10.1038/test")

    assert result == b"%PDF-old"


@pytest.mark.asyncio
async def test_europe_pmc_adapter_us_pmc_pow_falls_back_to_shared_browser_budget():
    """Europe PMC 接口失效时，从美国 PMC 页面找 PDF；挑战页由共享浏览器预算处理。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc
    from paper_fetch.robust_fetch import FetchBudget

    rest_resp = _mk_resp(200, {"resultList": {"result": [
        {"pmcid": "PMC13289604", "source": "MED", "inEPMC": "Y"},
    ]}})
    article_resp = MagicMock()
    article_resp.status_code = 200
    article_resp.url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC13289604/"
    article_resp.text = '<a href="pdf/lnag014.pdf">PDF</a>'
    client = _url_dispatch_client({
        "ebi.ac.uk/europepmc": rest_resp,
        "pmc.ncbi.nlm.nih.gov/articles": article_resp,
    })
    budget = FetchBudget(browser=1)
    stages: list[str] = []

    async def _no_europe_pdf(_url: str):
        return None

    async def _no_simple_pdf(_url: str, *, referer: str | None = None):
        return None

    async def _browser_pdf(url: str, *, referer: str | None, budget: FetchBudget):
        assert url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC13289604/pdf/lnag014.pdf"
        assert referer == "https://pmc.ncbi.nlm.nih.gov/articles/PMC13289604/"
        budget.browser -= 1
        return b"%PDF-ncbi-pow"

    async def _on_stage(stage: str):
        stages.append(stage)

    with patch("paper_fetch.europe_pmc_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.europe_pmc_adapter.fetch_oa_pdf", _no_europe_pdf), \
         patch("paper_fetch.europe_pmc_adapter.fetch_pdf_simple", _no_simple_pdf), \
         patch("paper_fetch.europe_pmc_adapter.fetch_pdf_via_browser", _browser_pdf):
        result = await fetch_via_europe_pmc(
            "10.1093/lifemedi/lnag014",
            budget=budget,
            on_stage=_on_stage,
        )

    assert result == b"%PDF-ncbi-pow"
    assert stages == ["browser"]
    assert budget.browser == 0


@pytest.mark.asyncio
async def test_europe_pmc_adapter_article_challenge_tries_canonical_main_pdf():
    """PMC 文章页本身是工作量证明页时，仍尝试当前官方主文地址并交给浏览器。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc
    from paper_fetch.robust_fetch import FetchBudget

    rest_resp = _mk_resp(200, {"resultList": {"result": [
        {"pmcid": "PMC13123348", "source": "MED", "inEPMC": "Y"},
    ]}})
    challenge_resp = MagicMock()
    challenge_resp.status_code = 200
    challenge_resp.url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC13123348/"
    challenge_resp.text = "<html><title>Checking your browser</title></html>"
    client = _url_dispatch_client({
        "ebi.ac.uk/europepmc": rest_resp,
        "pmc.ncbi.nlm.nih.gov/articles": challenge_resp,
    })
    budget = FetchBudget(browser=1)
    stages: list[str] = []
    canonical = "https://pmc.ncbi.nlm.nih.gov/articles/PMC13123348/pdf/main.pdf"

    async def _no_europe_pdf(_url: str):
        return None

    async def _no_simple_pdf(url: str, *, referer: str | None = None):
        assert url == canonical
        assert referer == "https://pmc.ncbi.nlm.nih.gov/articles/PMC13123348/"
        return None

    async def _browser_pdf(url: str, *, referer: str | None, budget: FetchBudget):
        assert url == canonical
        assert referer == "https://pmc.ncbi.nlm.nih.gov/articles/PMC13123348/"
        budget.browser -= 1
        return b"%PDF-pmc-main"

    async def _on_stage(stage: str):
        stages.append(stage)

    with patch("paper_fetch.europe_pmc_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.europe_pmc_adapter.fetch_oa_pdf", _no_europe_pdf), \
         patch("paper_fetch.europe_pmc_adapter.fetch_pdf_simple", _no_simple_pdf), \
         patch("paper_fetch.europe_pmc_adapter.fetch_pdf_via_browser", _browser_pdf):
        result = await fetch_via_europe_pmc(
            "10.1016/j.omton.2026.201200",
            budget=budget,
            on_stage=_on_stage,
        )

    assert result == b"%PDF-pmc-main"
    assert stages == ["browser"]
    assert budget.browser == 0


@pytest.mark.asyncio
async def test_library_proxy_follows_linkinghub_meta_refresh():
    """LinkingHub 用 HTML 元刷新跳 ScienceDirect；代理会话必须继续跟随再提取 PDF。"""
    from paper_fetch.library_proxy_adapter import fetch_via_library_proxy

    linking_html = b"""
      <html><head><meta http-equiv="refresh"
        content="2; url='/retrieve/articleSelectSinglePerm?Redirect=https%3A%2F%2Fwww.sciencedirect.com%2Fscience%2Farticle%2Fpii%2FS0006291X26007679&amp;key=test'">
      </head></html>
    """
    article_html = b"""
      <html><head><meta name="citation_pdf_url"
        content="https://www.sciencedirect.com/science/article/pii/S0006291X26007679/pdfft">
      </head></html>
    """

    linking_resp = MagicMock()
    linking_resp.content = linking_html
    linking_resp.text = linking_html.decode()
    linking_resp.url = "https://linkinghub.elsevier.com/retrieve/pii/S0006291X26007679"
    linking_resp.headers = {"content-type": "text/html;charset=UTF-8"}
    article_resp = MagicMock()
    article_resp.content = article_html
    article_resp.text = article_html.decode()
    article_resp.url = "https://www.sciencedirect.com/science/article/pii/S0006291X26007679"
    article_resp.headers = {"content-type": "text/html;charset=UTF-8"}

    requested: list[str] = []

    async def _get(url: str, **_kwargs):
        requested.append(url)
        return linking_resp if len(requested) == 1 else article_resp

    client = AsyncMock()
    client.get = _get
    client.cookies = httpx.Cookies()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    fake_pdf = b"%PDF-institution-copy"

    with patch(
        "paper_fetch.library_proxy_adapter.httpx.AsyncClient",
        return_value=client,
    ), patch(
        "paper_fetch.library_proxy_adapter._download_pdf_once",
        AsyncMock(return_value=fake_pdf),
    ) as download_mock:
        result = await fetch_via_library_proxy(
            doi="10.1016/j.bbrc.2026.154003",
            landing_url="https://doi.org/10.1016/j.bbrc.2026.154003",
            username="test-user",
            password="test-password",
            proxy_host_port="proxy.example:8080",
        )

    assert result == (fake_pdf, None)
    assert len(requested) == 2
    assert "articleSelectSinglePerm" in requested[1]
    download_mock.assert_awaited_once_with(
        "https://www.sciencedirect.com/science/article/pii/S0006291X26007679/pdfft",
        referer="https://www.sciencedirect.com/science/article/pii/S0006291X26007679",
        cookies=client.cookies,
        proxy=None,
    )


@pytest.mark.asyncio
async def test_europe_pmc_adapter_returns_none_when_no_pmc_anywhere():
    """europe_pmc：REST/S2 都无 PMC 且解析不出正式版 → 返 None。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc

    empty_rest = _mk_resp(200, {"resultList": {"result": []}})
    s2_nopmc = _mk_resp(200, {"externalIds": {"DOI": "10.1234/x"}})
    client = _url_dispatch_client({
        "ebi.ac.uk/europepmc": empty_rest,
        "api.semanticscholar.org": s2_nopmc,
    })

    async def _fake_resolve(doi, title=None):
        return None

    with patch("paper_fetch.europe_pmc_adapter.httpx.AsyncClient", return_value=client), \
         patch("paper_fetch.preprint_resolve.resolve_published_doi", _fake_resolve):
        result = await fetch_via_europe_pmc("10.1234/x")

    assert result is None


@pytest.mark.asyncio
async def test_europe_pmc_adapter_returns_none_on_empty_doi_and_title():
    """europe_pmc：doi 与 title 都空时直接返 None，不发请求。"""
    from paper_fetch.europe_pmc_adapter import fetch_via_europe_pmc

    assert await fetch_via_europe_pmc(None) is None
    assert await fetch_via_europe_pmc("") is None
    assert await fetch_via_europe_pmc("", title="") is None
