"""paper_download_service 五段降级链测试（Phase 13 重构后）。

降级链顺序：preprint → oa → meta → unpaywall → europe_pmc

覆盖：
- preprint 命中早返，不调后续四段
- oa 命中（preprint 不匹配时）
- meta 命中（前两段失败时）
- meta 段抽到的 DOI 补给下游 unpaywall / europe_pmc
- unpaywall 命中（meta 也失败时）
- europe_pmc 命中（前四段都失败时）
- 五段全失败 → download_failed
- PDF 超 size 限制 → size_limit_exceeded
- on_stage 回调按 preprint/oa/meta/unpaywall/europe_pmc 顺序触发
- tried_sources 准确记录实际尝试过的源
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# meta_adapter.fetch_via_landing_page 现返三元组 (pdf, doi, landing_info)；中性 landing_info
_NL = {"url": None, "publisher": None, "requires_auth": False}


@pytest.mark.asyncio
async def test_download_pdf_preprint_hit():
    """preprint 段命中时返 source='preprint'，不调后续四段。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-preprint"

    with patch("paper_fetch.service._resolve_published_doi_for_download", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=fake_pdf)), \
         patch("paper_fetch.service.fetch_oa_pdf") as mock_oa, \
         patch("paper_fetch.service.fetch_via_landing_page") as mock_meta, \
         patch("paper_fetch.service.fetch_via_unpaywall") as mock_unp, \
         patch("paper_fetch.service.fetch_via_europe_pmc") as mock_epmc:
        result = await download_pdf(
            doi="10.1101/2024.05.01.123456",
            paper_url="https://www.biorxiv.org/content/10.1101/2024.05.01.123456v1",
            oa_url="https://oa.example.com/x.pdf",
        )

    assert result["success"] is True
    assert result["source"] == "preprint"
    assert result["pdf_bytes"] == fake_pdf
    assert result["tried_sources"] == ["preprint"]
    mock_oa.assert_not_called()
    mock_meta.assert_not_called()
    mock_unp.assert_not_called()
    mock_epmc.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_falls_back_to_oa(monkeypatch):
    """preprint 不匹配 → oa 命中。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-oa"

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=fake_pdf)), \
         patch("paper_fetch.service.fetch_via_landing_page") as mock_meta:
        result = await download_pdf(
            doi="10.1234/abc",
            paper_url="https://www.nature.com/articles/xxxx",
            oa_url="https://oa.example.com/x.pdf",
        )

    assert result["source"] == "oa"
    assert result["pdf_bytes"] == fake_pdf
    assert "preprint" in result["tried_sources"]
    assert "oa" in result["tried_sources"]
    mock_meta.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_falls_back_to_meta(monkeypatch):
    """preprint + oa 都失败时，meta 段成功返 source='meta'。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-meta"

    # probe_oa 打桩为「查无 OA 直链但不确定 DOI 不存在」（is_oa 未知 /
    # doi_not_found=False），避免走真实 OpenAlex/doi.org 网络复核把假 DOI 短路掉
    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(fake_pdf, "10.5555/from-meta", _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall") as mock_unp:
        result = await download_pdf(
            doi="10.1234/abc",
            paper_url="https://publisher.org/article",
            oa_url="https://oa.example.com/dead.pdf",
        )

    assert result["source"] == "meta"
    assert result["pdf_bytes"] == fake_pdf
    mock_unp.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_meta_supplies_doi_for_unpaywall(monkeypatch):
    """meta 段没拿到 PDF 但抽到 DOI 时，下游 unpaywall 用这个 DOI 跑。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    fake_pdf = b"%PDF-from-unpaywall-via-meta-doi"

    unpaywall_mock = AsyncMock(return_value=fake_pdf)

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, "10.5555/meta-supplied", _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", unpaywall_mock):
        # 传入 doi=None，让 meta 补
        result = await download_pdf(
            doi=None,
            paper_url="https://publisher.org/article",
            oa_url=None,
        )

    assert result["source"] == "unpaywall"
    # unpaywall 应该用 meta 抽到的 DOI 调用
    unpaywall_mock.assert_called_once()
    args, kwargs = unpaywall_mock.call_args
    assert args[0] == "10.5555/meta-supplied"


@pytest.mark.asyncio
async def test_download_pdf_falls_back_to_unpaywall(monkeypatch):
    """preprint+oa+meta 都失败时，unpaywall 成功返 source='unpaywall'。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    fake_pdf = b"%PDF-unpaywall"

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=fake_pdf)), \
         patch("paper_fetch.service.fetch_via_europe_pmc") as mock_epmc:
        result = await download_pdf(
            doi="10.1234/test",
            paper_url="https://publisher.org/article",
            oa_url=None,
        )

    assert result["source"] == "unpaywall"
    mock_epmc.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_falls_back_to_europe_pmc(monkeypatch):
    """前四段都失败时，europe_pmc 兜底成功。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    fake_pdf = b"%PDF-europe-pmc"

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=fake_pdf)):
        result = await download_pdf(
            doi="10.1038/cancerlasertest",
            paper_url="https://nature.com/cancer",
            oa_url=None,
        )

    assert result["source"] == "europe_pmc"
    assert result["pdf_bytes"] == fake_pdf
    # paper_url 是出版商页（非聚合站）→ meta_doi 跳过（避免重复抓同一页）
    assert result["tried_sources"] == [
        "preprint", "openalex", "publisher_direct", "meta",
        "unpaywall", "crossref", "europe_pmc",
    ]


@pytest.mark.asyncio
async def test_download_pdf_europe_pmc_runs_even_when_paywalled(monkeypatch):
    """OpenAlex 判定付费墙（is_oa=False）短路掉昂贵段，但 europe_pmc 仍要试——
    付费墙生物医学论文常有免费 PMC 作者手稿，这是拿到全文的主力通道，不能被短路漏掉。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-pmc-manuscript"

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct") as mock_pub, \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=fake_pdf)) as mock_epmc:
        result = await download_pdf(
            doi="10.1038/s41586-023-06837-4",
            paper_url="https://www.nature.com/articles/s41586-023-06837-4",
            oa_url=None,
            title="Slide-tags enables single-nucleus barcoding",
        )

    # known_paywalled=True 短路了 publisher_direct 等昂贵段……
    assert mock_pub.call_count == 0
    # ……但 europe_pmc 照常跑并命中，title 透传过去（预印本→正刊反查用）
    mock_epmc.assert_awaited_once()
    assert mock_epmc.await_args.kwargs.get("title") == "Slide-tags enables single-nucleus barcoding"
    assert result["source"] == "europe_pmc"
    assert result["tried_sources"] == ["preprint", "openalex", "europe_pmc"]


@pytest.mark.asyncio
async def test_download_pdf_reserves_second_browser_attempt_for_europe_pmc(monkeypatch):
    """出版商浏览器失败后，Europe PMC 仍有一次浏览器机会且过程写入最终记录。"""
    from paper_fetch.service import download_pdf

    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    fake_pdf = b"%PDF-pmc-after-publisher"

    async def _publisher_miss(*_args, budget, **_kwargs):
        assert budget.browser == 2
        budget.browser -= 1
        return None

    async def _pmc_hit(*_args, budget, on_stage, **_kwargs):
        assert budget.browser == 1
        await on_stage("browser")
        budget.browser -= 1
        return fake_pdf

    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", _publisher_miss), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", _pmc_hit):
        result = await download_pdf(
            doi="10.1038/s41586-browser-budget",
            paper_url=None,
            oa_url=None,
        )

    assert result["success"] is True
    assert result["source"] == "europe_pmc"
    assert result["tried_sources"] == [
        "openalex", "publisher_direct", "meta_doi", "crossref", "europe_pmc", "browser",
    ]


@pytest.mark.asyncio
async def test_download_pdf_formal_subscription_doi_with_preprint_url_tries_library_proxy(monkeypatch):
    """正式版 DOI 是订阅刊、paper_url 却是预印本页时，免费段失败后仍要走机构代理。

    复现 NicheTrans：标题搜索锁定 Nature Methods 正式 DOI，但候选里还带 bioRxiv URL。
    旧逻辑用 `landing_url or paper_url or doi.org` 选 target，bioRxiv 免费站抢先，导致
    auth_required=False，复旦图书馆代理完全没试。
    2026-08-21 段序调整：preprint_fallback 移到 library_proxy 之前——这里 mock 预印本
    下载不命中，验证机构代理兜底路径仍可达（预印本命中的路径另有专项测试）。
    """
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    fake_pdf = b"%PDF-library-proxy"
    preprint_mock = AsyncMock(return_value=None)
    oa_mock = AsyncMock(return_value=None)
    proxy_mock = AsyncMock(return_value=(fake_pdf, None))

    with patch("paper_fetch.service.fetch_preprint_pdf", preprint_mock), \
         patch("paper_fetch.service.fetch_oa_pdf", oa_mock), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)), \
         patch("paper_fetch.service._try_library_proxy", proxy_mock), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))):
        result = await download_pdf(
            doi="10.1038/s41592-026-03153-3",
            paper_url="https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1",
            oa_url="https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1.full.pdf",
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "library_proxy"
    assert "library_proxy" in result["tried_sources"]
    assert "preprint" not in result["tried_sources"]
    assert "oa" not in result["tried_sources"]
    # preprint_fallback 现在位于 library_proxy 之前（产品拍板：预印本兜底优先于机构代理）
    assert "preprint_fallback" in result["tried_sources"]
    assert result["tried_sources"].index("preprint_fallback") < \
        result["tried_sources"].index("library_proxy")
    proxy_mock.assert_awaited_once()
    assert proxy_mock.await_args.kwargs["landing_url"] == "https://doi.org/10.1038/s41592-026-03153-3"


@pytest.mark.asyncio
async def test_download_pdf_preprint_doi_resolves_published_before_preprint(monkeypatch):
    """只给预印本 DOI/URL 时，能解析正式版 DOI 就先走正式版和机构代理。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    # preprint_fallback 现位于 library_proxy 之前（2026-08-21 段序）：mock 预印本不命中，
    # 验证「解析到正式版 DOI 后走机构代理」的原意图；preprint_mock 仍可验证下载用的是
    # 正式版目标（bioRxiv URL 已被 defer 摘除，只作为 fallback 尝试过）。
    preprint_mock = AsyncMock(return_value=None)
    oa_mock = AsyncMock(return_value=None)
    proxy_mock = AsyncMock(return_value=(b"%PDF-library-proxy", None))

    with patch(
        "paper_fetch.service._resolve_published_doi_for_download",
        AsyncMock(return_value="10.1038/s41592-026-03153-3"),
    ), \
         patch("paper_fetch.service.discover_preprint", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.fetch_preprint_pdf", preprint_mock), \
         patch("paper_fetch.service.fetch_oa_pdf", oa_mock), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)), \
         patch("paper_fetch.service._try_library_proxy", proxy_mock), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))):
        result = await download_pdf(
            doi="10.1101/2024.12.05.626986",
            paper_url="https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1",
            oa_url="https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1.full.pdf",
            title="NicheTrans: Spatial-aware Cross-omics Translation",
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "library_proxy"
    assert "preprint" not in result["tried_sources"]
    assert "oa" not in result["tried_sources"]
    assert "library_proxy" in result["tried_sources"]
    assert result["tried_sources"].index("preprint_fallback") < \
        result["tried_sources"].index("library_proxy")
    assert proxy_mock.await_args.kwargs["doi"] == "10.1038/s41592-026-03153-3"
    assert proxy_mock.await_args.kwargs["landing_url"] == "https://doi.org/10.1038/s41592-026-03153-3"


@pytest.mark.asyncio
async def test_download_pdf_formal_doi_keeps_pmc_oa_url_as_published_copy(monkeypatch):
    """PMC/Europe PMC 作者稿不是预印本；正式 DOI 下仍可作为开放正式副本优先下载。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-pmc"
    proxy_mock = AsyncMock(return_value=b"%PDF-library-proxy")

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=fake_pdf)) as oa_mock, \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service._try_library_proxy", proxy_mock), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))):
        result = await download_pdf(
            doi="10.1038/s41586-023-06837-4",
            paper_url=None,
            oa_url="https://europepmc.org/articles/PMC10764288?pdf=render",
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "oa"
    assert result["pdf_bytes"] == fake_pdf
    assert result["tried_sources"] == ["oa"]
    oa_mock.assert_awaited_once()
    proxy_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_pdf_mdpi_doi_uses_publisher_direct_after_index_pdf_403(monkeypatch):
    """MDPI 开放论文的 www.mdpi.com/pdf 可能返 403；只有 DOI 时也应走 mdpi-res 静态直链。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-mdpi-static"
    fetch_oa_mock = AsyncMock(return_value=None)
    publisher_direct_mock = AsyncMock(return_value=fake_pdf)

    with patch("paper_fetch.service.fetch_oa_pdf", fetch_oa_mock), \
         patch(
             "paper_fetch.service.probe_oa",
             AsyncMock(return_value=(True, ["https://www.mdpi.com/1999-4923/18/6/752/pdf"], False)),
         ), \
         patch("paper_fetch.service.fetch_publisher_direct", publisher_direct_mock), \
         patch("paper_fetch.service.fetch_via_landing_page") as mock_meta:
        result = await download_pdf(
            doi="10.3390/pharmaceutics18060752",
            paper_url=None,
            oa_url=None,
        )

    assert result["success"] is True
    assert result["source"] == "publisher_direct"
    assert result["pdf_bytes"] == fake_pdf
    assert result["tried_sources"] == ["openalex", "publisher_direct"]
    fetch_oa_mock.assert_awaited_once_with("https://www.mdpi.com/1999-4923/18/6/752/pdf")
    publisher_direct_mock.assert_awaited_once()
    assert publisher_direct_mock.await_args.args[:2] == ("10.3390/pharmaceutics18060752", None)
    mock_meta.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_web_discovery_handles_unknown_oa_gap(monkeypatch):
    """结构化 OA/出版商/浏览器链路全失败后，Tavily 网页发现层可补未知期刊 PDF。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    fake_pdf = b"%PDF-web-discovery"
    stages: list[str] = []

    async def _on_stage(stage: str):
        stages.append(stage)

    with patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(True, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.can_discover_pdf_via_web", return_value=True), \
         patch(
             "paper_fetch.service.discover_pdf_via_web",
             AsyncMock(return_value=(fake_pdf, "https://journal.example.org/hidden-static.pdf")),
         ) as web_mock:
        result = await download_pdf(
            doi="10.5555/unknown-oa",
            paper_url="https://journal.example.org/article/unknown-oa",
            oa_url=None,
            title="Unknown OA Article With Hidden Static PDF",
            on_stage=_on_stage,
        )

    assert result["success"] is True
    assert result["source"] == "web_pdf_discovery"
    assert result["pdf_bytes"] == fake_pdf
    assert "web_pdf_discovery" in result["tried_sources"]
    assert stages[-1] == "web_pdf_discovery"
    web_mock.assert_awaited_once()
    assert web_mock.await_args.kwargs["allow_preprint"] is False


@pytest.mark.asyncio
async def test_download_pdf_paywalled_doi_runs_web_discovery_after_library_proxy(monkeypatch):
    """正式订阅 DOI 先走预印本发现与机构代理；网页发现不能抢在机构链路前返回预印本/作者稿。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-web-after-proxy"
    proxy_mock = AsyncMock(return_value=(None, "pdf_download_failed:stalled"))
    web_mock = AsyncMock(return_value=(fake_pdf, "https://publisher.example.org/nichetrans.pdf"))

    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", AsyncMock(return_value=None)), \
         patch("paper_fetch.service._try_library_proxy", proxy_mock), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))), \
         patch("paper_fetch.service.can_discover_pdf_via_web", return_value=True), \
         patch("paper_fetch.service.discover_pdf_via_web", web_mock):
        result = await download_pdf(
            doi="10.1038/s41592-026-03153-3",
            paper_url=None,
            oa_url=None,
            title="NicheTrans: Spatial-aware Cross-omics Translation",
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "web_pdf_discovery"
    assert result["tried_sources"] == [
        "openalex", "europe_pmc", "preprint_discovery", "library_proxy", "web_pdf_discovery",
    ]
    assert result["tried_sources"].index("library_proxy") < \
        result["tried_sources"].index("web_pdf_discovery")
    proxy_mock.assert_awaited_once()
    web_mock.assert_awaited_once()
    assert web_mock.await_args.kwargs["allow_preprint"] is False


@pytest.mark.asyncio
async def test_download_pdf_formal_subscription_doi_falls_back_to_preprint_before_library_proxy(monkeypatch):
    """正式版免费段失败后，候选自带的预印本兜底先于机构代理命中（2026-08-21 产品拍板段序：
    预印本快且免费、代理 ~17KB/s 且有防封号额度——预印本可得就不动学校账号）。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    fake_preprint = b"%PDF-preprint"
    preprint_mock = AsyncMock(return_value=fake_preprint)
    proxy_mock = AsyncMock(return_value=(None, "pdf_download_failed:stalled"))

    with patch("paper_fetch.service.fetch_preprint_pdf", preprint_mock), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)), \
         patch("paper_fetch.service._try_library_proxy", proxy_mock), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))):
        result = await download_pdf(
            doi="10.1038/s41592-026-03153-3",
            paper_url="https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1",
            oa_url=None,
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "preprint"
    assert result["pdf_bytes"] == fake_preprint
    assert "preprint" not in result["tried_sources"]
    assert "preprint_fallback" in result["tried_sources"]
    # 预印本在机构代理前命中 → 学校账号这次完全没动（2026-08-21 新段序的产品语义）
    assert "library_proxy" not in result["tried_sources"]
    proxy_mock.assert_not_awaited()
    preprint_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_pdf_all_five_fail(monkeypatch):
    """五段都失败 → success=False, error='download_failed', tried 含五个源。"""
    from paper_fetch.config import get_config
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    # scihub 兜底受 settings.sci_hub_enabled 控制（合规默认关）；显式打开，
    # 不依赖跑测试的机器 .env 里恰好开了 SCI_HUB_ENABLED（CI 上没有）。
    monkeypatch.setattr(get_config(), "scihub_enabled", True)

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(
            doi="10.1234/test",
            paper_url="https://publisher.org/x",
            oa_url="https://oa.example.com/x.pdf",
        )

    assert result["success"] is False
    assert result["error"] == "download_failed"
    assert result["source"] is None
    # paper_url 是出版商页（非聚合站）→ meta_doi 跳过；上面显式开了 scihub → 兜底也试。
    # 非 Elsevier DOI（10.1234）→ elsevier_api 段不触发。
    assert set(result["tried_sources"]) == {
        "preprint", "oa", "openalex", "publisher_direct", "meta",
        "unpaywall", "crossref", "europe_pmc", "browser", "scihub",
    }


@pytest.mark.asyncio
async def test_download_pdf_size_limit_exceeded():
    """PDF 超 pdf_max_upload_mb 时返 size_limit_exceeded，pdf_bytes=None。"""
    from paper_fetch.config import get_config
    from paper_fetch.service import download_pdf

    s = get_config()
    over_limit = b"%PDF-" + b"x" * (s.max_pdf_mb * 1024 * 1024 + 1)

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=over_limit)):
        result = await download_pdf(
            doi=None,
            paper_url="https://arxiv.org/abs/2301.99999",
            oa_url=None,
        )

    assert result["success"] is False
    assert result["error"] == "size_limit_exceeded"
    assert result["pdf_bytes"] is None


@pytest.mark.asyncio
async def test_download_pdf_on_stage_callback_order(monkeypatch):
    """on_stage 按 preprint → oa → meta → unpaywall → europe_pmc 顺序触发。"""
    from paper_fetch.config import get_config
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    # 同 test_download_pdf_all_five_fail：显式开 scihub 兜底，不依赖本机 .env。
    monkeypatch.setattr(get_config(), "scihub_enabled", True)
    stages: list[str] = []

    async def _on_stage(stage: str):
        stages.append(stage)

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        await download_pdf(
            doi="10.1234/test",
            paper_url="https://publisher.org/x",
            oa_url="https://oa.example.com/x.pdf",
            on_stage=_on_stage,
        )

    # paper_url 是出版商页（非聚合站）→ meta_doi 跳过；上面显式开了 scihub → 末尾兜底。
    assert stages == [
        "preprint", "oa", "openalex", "publisher_direct", "meta",
        "unpaywall", "crossref", "europe_pmc", "browser", "scihub",
    ]


@pytest.mark.asyncio
async def test_download_pdf_meta_doi_runs_for_aggregator_url(monkeypatch):
    """paper_url 是 PubMed 摘要页（聚合站）时，meta_doi 段应触发（用 doi.org 跳真出版商页）。"""
    from paper_fetch.service import download_pdf

    stages: list[str] = []

    async def _on_stage(stage: str):
        stages.append(stage)

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)):
        await download_pdf(
            doi="10.1234/test",
            paper_url="https://pubmed.ncbi.nlm.nih.gov/12345678/",
            oa_url=None,
            on_stage=_on_stage,
        )

    assert "meta_doi" in stages


@pytest.mark.asyncio
async def test_download_pdf_skips_oa_when_no_oa_url(monkeypatch):
    """oa_url=None 时不调 fetch_oa_pdf，跳到 meta 段。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-meta-only"

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf") as mock_oa, \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(fake_pdf, None, _NL)),
         ):
        result = await download_pdf(
            doi=None,
            paper_url="https://publisher.org/article",
            oa_url=None,
        )

    assert result["source"] == "meta"
    mock_oa.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_direct_pdf_url():
    """paper_url 本身是 PDF 直链（.pdf 结尾）→ direct 段命中，source='direct'，不进 oa/meta。

    复现 2026-06-20 中国微生态学杂志（cjm.dmu.edu.cn/.../7-2-yuxia.pdf）真实踩坑：搜索源
    把 PDF 直链当 source_url，旧链无 direct 段，被 meta 当 HTML 抓后把 200+application/pdf 丢弃。
    """
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-direct"

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=fake_pdf)) as mock_oa, \
         patch("paper_fetch.service.fetch_via_landing_page") as mock_meta:
        result = await download_pdf(
            doi=None,
            paper_url="https://cjm.dmu.edu.cn/data/article/zgwstxzz/preview/pdf/7-2-yuxia.pdf",
            oa_url=None,
        )

    assert result["success"] is True
    assert result["source"] == "direct"
    assert result["pdf_bytes"] == fake_pdf
    assert result["tried_sources"] == ["preprint", "direct"]
    mock_oa.assert_awaited_once()
    mock_meta.assert_not_called()


async def test_looks_like_pdf_url():
    """_looks_like_pdf_url：.pdf 结尾或含 /pdf/ 段判 True；普通落地页 / query 里的 .pdf 判 False。"""
    from paper_fetch.service import _looks_like_pdf_url

    assert _looks_like_pdf_url("https://cjm.dmu.edu.cn/data/article/x/preview/pdf/7-2.pdf") is True
    assert _looks_like_pdf_url("https://link.springer.com/content/pdf/10.1/abc") is True  # /pdf/ 段
    assert _looks_like_pdf_url("https://www.nature.com/articles/s41586-x") is False
    assert _looks_like_pdf_url("https://x.org/article?ref=foo.pdf") is False  # 只看 path 不看 query
    assert _looks_like_pdf_url(None) is False
    assert _looks_like_pdf_url("") is False


@pytest.mark.asyncio
async def test_download_pdf_skips_unpaywall_when_email_not_configured(monkeypatch):
    """UNPAYWALL_EMAIL 未配置时跳过 unpaywall 段，直接调 europe_pmc。"""
    from paper_fetch.service import download_pdf

    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    fake_pdf = b"%PDF-epmc"

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall") as mock_unp, \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=fake_pdf)):
        result = await download_pdf(
            doi="10.1234/test",
            paper_url="https://publisher.org/x",
            oa_url=None,
        )

    assert result["source"] == "europe_pmc"
    assert "unpaywall" not in result["tried_sources"]
    mock_unp.assert_not_called()


@pytest.mark.asyncio
async def test_download_pdf_invalid_doi_short_circuits():
    """乱码 DOI 在进下载链前被短路：error=invalid_doi，tried_sources 只有 doi_check。"""
    from paper_fetch.service import download_pdf

    result = await download_pdf(doi="asdf1234", paper_url=None, oa_url=None)

    assert result["success"] is False
    assert result["error"] == "invalid_doi"
    assert result["auth_required"] is False
    assert result["landing_url"] is None
    assert result["tried_sources"] == ["doi_check"]


@pytest.mark.asyncio
async def test_download_pdf_doi_prefix_normalized_before_validation():
    """带 doi: 前缀的合法 DOI 先归一化再校验，不应被误判 invalid_doi。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-oa"
    # openalex 段直接命中 OA 直链，在更贵的 publisher_direct 段之前早返（不发真实网络）
    with patch(
        "paper_fetch.service.probe_oa",
        AsyncMock(return_value=(True, ["https://oa/x.pdf"], False)),
    ), patch(
        "paper_fetch.service.fetch_oa_pdf",
        AsyncMock(return_value=fake_pdf),
    ):
        result = await download_pdf(doi="doi:10.1038/nature123", paper_url=None, oa_url=None)

    assert result["success"] is True
    assert result["source"] == "openalex"


@pytest.mark.asyncio
async def test_download_pdf_doi_not_found_short_circuits():
    """OpenAlex 404 + doi.org 复核确认不存在 → error=doi_not_found，不再误标 auth_required。"""
    from unittest.mock import MagicMock

    from paper_fetch import proxy as proxy_pool
    from paper_fetch.service import download_pdf

    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(return_value={"responseCode": 100})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "paper_fetch.service.probe_oa",
        AsyncMock(return_value=(None, [], True)),
    ), patch.object(proxy_pool.httpx, "AsyncClient", return_value=client), \
         patch("paper_fetch.service.fetch_publisher_direct") as mock_pub:
        result = await download_pdf(
            doi="10.1038/s41586-999-00000-0", paper_url=None, oa_url=None,
        )

    assert result["success"] is False
    assert result["error"] == "doi_not_found"
    assert result["auth_required"] is False
    assert result["landing_url"] is None
    assert result["tried_sources"] == ["openalex", "doi_resolve"]
    mock_pub.assert_not_called()


# =============================================================================
# 拆长事务守卫（2026-08-18 database is locked 事故整改）
# =============================================================================
async def test_download_pdf_signature_has_no_db_param():
    """契约守卫：download_pdf 绝不接收 db session。

    事故根因就是调用方把整条下载链（多源网络 I/O）包在 session_scope 事务里。
    拆完后下载链的 DB 访问全部由宿主钩子自开短事务；此测试防止
    未来有人把 db 参数加回去（那会重新引入"下载期间持有写锁阻塞全站"的路径）。
    （原 PaperPilot 版同时检查 execute_paper_search_agent 的签名——那是宿主侧
    编排器，留在 PaperPilot 仓库测试。）
    """
    import inspect

    from paper_fetch.service import download_pdf

    assert "db" not in inspect.signature(download_pdf).parameters


@pytest.mark.asyncio
# 注：test_download_in_progress_other_write_tx_completes 依赖 PaperPilot 数据库（db_session），留在 PaperPilot 仓库跑。

@pytest.mark.asyncio
async def test_download_pdf_paywalled_doi_discovers_preprint_before_library_proxy(monkeypatch):
    """只给正式版 DOI + 标题、免费段全失败时：发现同研究预印本并在机构代理前交付。

    返回必须带产品语义字段：source=preprint_discovery、delivered_version=preprint、
    requested_doi（用户要的正式版）、delivered_doi（预印本）、中文 notice。
    """
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-preprint-discovered"
    discover_mock = AsyncMock(return_value={
        "doi": "10.1101/2023.01.27.525553",
        "url": "https://www.biorxiv.org/content/10.1101/2023.01.27.525553",
        "via": "europe_pmc", "match_score": 0.86,
    })
    preprint_mock = AsyncMock(return_value=fake_pdf)
    proxy_mock = AsyncMock(return_value=(b"%PDF-proxy", None))

    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", discover_mock), \
         patch("paper_fetch.service.fetch_preprint_pdf", preprint_mock), \
         patch("paper_fetch.service._try_library_proxy", proxy_mock), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))):
        result = await download_pdf(
            doi="10.1038/s41586-024-07359-3",
            paper_url=None,
            oa_url=None,
            title="3D genomic mapping reveals multifocality of human pancreatic precancers",
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "preprint_discovery"
    assert result["pdf_bytes"] == fake_pdf
    assert result["delivered_version"] == "preprint"
    assert result["requested_doi"] == "10.1038/s41586-024-07359-3"
    assert result["delivered_doi"] == "10.1101/2023.01.27.525553"
    assert result["notice"] and "预印本" in result["notice"]
    assert "preprint_discovery" in result["tried_sources"]
    # 预印本已交付 → 不动学校账号
    assert "library_proxy" not in result["tried_sources"]
    proxy_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_pdf_preprint_discovery_skipped_without_title(monkeypatch):
    """标题为空时跳过 preprint_discovery（没有标题就没有发现依据）。"""
    from paper_fetch.service import download_pdf

    discover_mock = AsyncMock(return_value=None)
    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", discover_mock), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(doi="10.1038/s41586-024-07359-3", paper_url=None, oa_url=None)

    assert result["success"] is False
    assert "preprint_discovery" not in result["tried_sources"]
    discover_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_pdf_preprint_discovery_not_triggered_without_paywall(monkeypatch):
    """无付费信号（OA 未知 + 无订阅站 URL）时不做预印本发现——别把 OA 论文降级成预印本。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    discover_mock = AsyncMock(return_value=None)
    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.discover_preprint", discover_mock), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(
            doi="10.1234/unknown-oa", paper_url=None, oa_url=None,
            title="Some Unknown OA Article Title Here",
        )

    assert result["success"] is False
    assert "preprint_discovery" not in result["tried_sources"]
    discover_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_pdf_failure_detail_institutional_proxy_failed(monkeypatch):
    """机构代理试过但传输失败 → failure_detail=institutional_proxy_failed，message 说清
    「已通过学校代理尝试但失败/中断」而非笼统付费墙（2026-08-21 事故核心诉求）。"""
    from paper_fetch.service import download_pdf

    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", AsyncMock(return_value=None)), \
         patch("paper_fetch.service._try_library_proxy",
               AsyncMock(return_value=(None, "pdf_download_failed:stalled"))), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))), \
         patch("paper_fetch.service.can_discover_pdf_via_web", return_value=False), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(
            doi="10.1038/s41586-024-07359-3",
            paper_url=None,
            oa_url=None,
            title="3D genomic mapping reveals multifocality of human pancreatic precancers",
            user=object(),
        )

    assert result["success"] is False
    assert result["auth_required"] is True  # 前端机构引导仍可用
    assert result["failure_detail"] == "institutional_proxy_failed"
    assert result["message"] and "代理" in result["message"]


@pytest.mark.asyncio
async def test_download_pdf_failure_detail_paywall_no_access_without_proxy(monkeypatch):
    """确认付费但没传 user（无机构通道）→ failure_detail=paywall_no_access + 校园网建议。"""
    from paper_fetch.service import download_pdf

    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.can_discover_pdf_via_web", return_value=False), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(
            doi="10.1038/s41586-024-07359-3",
            paper_url=None,
            oa_url=None,
            title="3D genomic mapping reveals multifocality of human pancreatic precancers",
        )

    assert result["success"] is False
    assert result["auth_required"] is True
    assert result["failure_detail"] == "paywall_no_access"
    # 2026-08-23 三段式：论文页 + 学校网络手动下载 + 网页端上传指引（MCP 可直接转述）
    assert result["message"] and "论文页：" in result["message"]
    assert "手动下载 PDF" in result["message"] and "上传" in result["message"]


@pytest.mark.asyncio
async def test_download_pdf_failure_detail_all_sources_failed(monkeypatch):
    """无付费信号的普通失败 → failure_detail=None（不做付费墙分类），不误导用户。"""
    from paper_fetch.service import download_pdf

    monkeypatch.setenv("UNPAYWALL_EMAIL", "test@example.com")
    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(None, [], False))), \
         patch("paper_fetch.service.fetch_publisher_direct", AsyncMock(return_value=None)), \
         patch(
             "paper_fetch.service.fetch_via_landing_page",
             AsyncMock(return_value=(None, None, _NL)),
         ), \
         patch("paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_browser_landing", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(
            doi="10.1234/test", paper_url="https://publisher.org/x", oa_url=None,
            title=None,  # 无标题：preprint_discovery 跳过；publisher.org 非订阅站 → 无付费信号
        )

    assert result["success"] is False
    assert result["failure_detail"] is None
    assert result["message"] is None


# ===========================================================================
# 评审修复补充：M1（失败分类「代理没跑」）、M4（只给 DOI 补标题）、m5（预印本段序）
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("never_ran_reason", ["no_credential", "no_proxy", "credential_unavailable", "channel_unavailable"])
async def test_download_pdf_proxy_never_ran_classified_as_paywall(monkeypatch, never_ran_reason):
    """评审 M1：无凭证/没配代理/冷却等「代理根本没跑」的 reason 不能归
    institutional_proxy_failed（用户会被通知「已通过学校代理尝试但中断」——误导），
    应归 paywall_no_access 并引导配置机构账号。"""
    from paper_fetch.service import download_pdf

    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", AsyncMock(return_value=None)), \
         patch("paper_fetch.service._try_library_proxy",
               AsyncMock(return_value=(None, never_ran_reason))), \
         patch("paper_fetch.service._library_proxy_gate",
               AsyncMock(return_value=(True, True))), \
         patch("paper_fetch.service.can_discover_pdf_via_web", return_value=False), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(
            doi="10.1038/s41586-024-07359-3",
            paper_url=None,
            oa_url=None,
            title="3D genomic mapping reveals multifocality of human pancreatic precancers",
            user=object(),
        )

    assert result["success"] is False
    assert result["failure_detail"] == "paywall_no_access", never_ran_reason
    assert result["message"] and "机构" in result["message"]
    # 不能说「已尝试后失败」
    assert "已通过学校" not in result["message"]


@pytest.mark.asyncio
async def test_download_pdf_doi_only_title_backfilled_via_crossref(monkeypatch):
    """评审 M4：只给 DOI 不给标题（fetch 高频输入，事故本身）时，preprint_discovery
    前先查 Crossref works/{doi} 补标题，补到就正常触发发现段。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-doi-only-discovery"
    ref_authors = ["braxton", "wood"]
    meta_mock = AsyncMock(return_value=(
        "3D genomic mapping reveals multifocality of human pancreatic precancers", ref_authors,
    ))
    discover_mock = AsyncMock(return_value={
        "doi": "10.1101/2023.01.27.525553",
        "url": "https://www.biorxiv.org/content/10.1101/2023.01.27.525553",
        "via": "europe_pmc", "match_score": 0.86,
    })

    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi", meta_mock), \
         patch("paper_fetch.service.discover_preprint", discover_mock), \
         patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=fake_pdf)):
        result = await download_pdf(
            doi="10.1038/s41586-024-07359-3",
            paper_url=None,
            oa_url=None,
            title=None,  # ★ 只给 DOI
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "preprint_discovery"
    meta_mock.assert_awaited_once_with("10.1038/s41586-024-07359-3")
    # 补到的标题与参照作者都用于发现（标题是 args[1]，作者列表是 args[2]）
    assert discover_mock.await_args.args[1] == "3D genomic mapping reveals multifocality of human pancreatic precancers"
    assert discover_mock.await_args.args[2] == ref_authors


@pytest.mark.asyncio
async def test_download_pdf_doi_only_title_backfill_fails_skips_discovery(monkeypatch):
    """评审 M4 反向：补标题失败（Crossref 404/网络异常）→ 静默跳过发现段，不炸链。"""
    from paper_fetch.service import download_pdf

    discover_mock = AsyncMock(return_value=None)
    with patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", discover_mock), \
         patch("paper_fetch.service.can_discover_pdf_via_web", return_value=False), \
         patch("paper_fetch.service.fetch_via_scihub", AsyncMock(return_value=None)):
        result = await download_pdf(
            doi="10.1038/s41586-024-07359-3", paper_url=None, oa_url=None,
            title=None, user=object(),
        )

    assert result["success"] is False
    assert "preprint_discovery" not in result["tried_sources"]
    discover_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_pdf_preprint_fallback_runs_before_discovery(monkeypatch):
    """评审 m5：候选自带确定预印本 URL 时，本地 preprint_fallback 先于外部 preprint_discovery
    （确定直链零网络成本，优先于再搜索）。"""
    from paper_fetch.service import download_pdf

    # fallback URL 直链下载不命中（fetch_preprint_pdf=None），外部发现的候选走
    # fetch_pdf 兜底命中 —— 两段都实际执行，才能断言顺序。
    fake_pdf = b"%PDF-discovered-after-fallback-miss"
    discover_mock = AsyncMock(return_value={
        "doi": "10.1101/2024.12.05.626986", "url": "https://www.biorxiv.org/content/10.1101/2024.12.05.626986",
        "via": "europe_pmc", "match_score": 0.9,
    })

    with patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.fetch_pdf", AsyncMock(return_value=fake_pdf)), \
         patch("paper_fetch.service.probe_oa", AsyncMock(return_value=(False, [], False))), \
         patch("paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)), \
         patch("paper_fetch.service.crossref_meta_for_doi",
               AsyncMock(return_value=(None, []))), \
         patch("paper_fetch.service.discover_preprint", discover_mock):
        result = await download_pdf(
            doi="10.1038/s41592-026-03153-3",
            paper_url="https://www.biorxiv.org/content/10.1101/2024.12.05.626986v1",
            oa_url=None,
            title="NicheTrans: Spatial-aware Cross-omics Translation",
            user=object(),
        )

    assert result["success"] is True
    assert result["source"] == "preprint_discovery"
    assert result["tried_sources"].index("preprint_fallback") < \
        result["tried_sources"].index("preprint_discovery")


@pytest.mark.asyncio
async def test_download_pdf_rate_limited_failure_detail_consistent(monkeypatch):
    """评审 m4：限流时 failure_detail 与 message 同源（都是 rate_limited 语义），
    不再出现「付费墙分类 + 稍后自动重试文案」两说两事。

    2026-08-23 二审拍板更新：限流兜底只覆盖**无付费信号**的失败（场景 A）；
    auth_required=True 时付费墙三段式优先（场景 B，预印本多 IP 轮换打满后终态
    要说「手动下载后上传」而不是「稍后重试」）——两种场景各自分类/文案同源。"""
    from unittest.mock import patch as _patch

    from paper_fetch.service import RATE_LIMITED_MESSAGE, _fail

    with _patch("paper_fetch.service.max_captured_retry_after", return_value=30):
        # 场景 A：无付费信号 + 限流捕获 → 覆盖成 rate_limited 终态（即停，不自动重试）
        result = _fail(
            "download_failed", ["oa"],
            auth_required=False,
        )
        assert result["error"] == "rate_limited"
        assert result["failure_detail"] == "rate_limited"
        assert result["message"] == RATE_LIMITED_MESSAGE

        # 场景 B：付费墙 + 限流捕获（轮换打满的典型形态）→ 付费墙三段式优先，不覆盖
        result = _fail(
            "download_failed", ["preprint", "preprint_discovery"],
            auth_required=True, landing_url="https://nature.com/x", publisher="nature.com",
            failure_detail="paywall_no_access",
            message="这篇论文有付费墙……\n论文页：https://nature.com/x\n你可以用学校网络……上传……",
        )
        assert result["error"] == "download_failed"
        assert result["failure_detail"] == "paywall_no_access"
        assert result["message"] is not None and "上传" in result["message"]
        assert result["message"] != RATE_LIMITED_MESSAGE
