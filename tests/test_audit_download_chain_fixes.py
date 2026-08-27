"""审计修复 2/4/5 防回归测试（2026-08-23）。

- 修复2：scihub 段豁免 75s 软预算——预算耗尽（library_proxy 大文件跑完后的常态）
  仍必须轮到 scihub，否则开了 sci_hub_enabled 也永远白开。
- 修复4：免费站域名表对齐——researchsquare 等预印本站被反爬 403 时不得误判付费墙。
- 修复5：只给预印本 DOI（无 URL）时主链 ① 段用模板直链短路，不再白跑 openalex API。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 与 test_paper_download_service 同款中性 landing_info
_NL = {"url": None, "publisher": None, "requires_auth": False}


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    from paper_fetch import domain_cooldown as dc

    dc.reset_cooldowns()
    yield
    dc.reset_cooldowns()


# ---------- 修复2：scihub 预算豁免 ----------


@pytest.mark.asyncio
async def test_scihub_runs_even_when_budget_exhausted(monkeypatch):
    """75s 软预算耗尽（total_budget_sec=0 → deadline 立即到期）时：
    查预算的段全跳过，但 scihub 段仍执行并命中。"""
    from paper_fetch.config import get_config
    from paper_fetch import service as svc

    # 显式打开开关，不依赖本机 .env（load_dotenv override=True 会盖外部环境变量）
    monkeypatch.setattr(get_config(), "scihub_enabled", True)
    # 预算走 FetchConfig.total_budget_sec（迁移前 patch 的模块常量已改为 config 读取）
    monkeypatch.setattr(get_config(), "total_budget_sec", 0)

    fake_pdf = b"%PDF-scihub"
    with patch(
        "paper_fetch.service._resolve_published_doi_for_download",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)
    ), patch(
        "paper_fetch.service.probe_oa",
        AsyncMock(return_value=(None, [], False)),
    ), patch(
        "paper_fetch.service.is_elsevier_target",
        MagicMock(return_value=False),
    ), patch(
        "paper_fetch.service.fetch_publisher_direct",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_landing_page",
        AsyncMock(return_value=(None, None, _NL)),
    ), patch(
        "paper_fetch.service.fetch_via_unpaywall",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_crossref",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_europe_pmc",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_browser_landing",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.can_discover_pdf_via_web",
        MagicMock(return_value=False),
    ), patch(
        "paper_fetch.service.fetch_via_scihub",
        AsyncMock(return_value=fake_pdf),
    ) as mock_scihub:
        result = await svc.download_pdf(
            doi="10.1234/paywalled", paper_url=None, oa_url=None
        )

    assert result["success"] is True
    assert result["source"] == "scihub"
    assert result["pdf_bytes"] == fake_pdf
    mock_scihub.assert_awaited_once()
    # 预算耗尽确实挡掉了前面查预算的昂贵段（对照组：这些段没跑）
    assert "publisher_direct" not in result["tried_sources"]
    assert "europe_pmc" not in result["tried_sources"]


# ---------- 修复4：免费站域名表对齐 ----------


def test_free_site_markers_cover_all_preprint_url_markers():
    """_PREPRINT_URL_MARKERS 认的每个预印本站都必须在 FREE_SITE_MARKERS 里
    （表间锁死，防再漂移：ResearchSquare 403 误判付费墙事故的根因）。"""
    from paper_fetch.robust_fetch import FREE_SITE_MARKERS
    from paper_fetch.service import _PREPRINT_URL_MARKERS

    missing = [m for m in _PREPRINT_URL_MARKERS if m not in FREE_SITE_MARKERS]
    assert not missing, f"预印本站缺进免费站表: {missing}"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.researchsquare.com/articles/rs-1234/v1",
        "https://www.ssrn.com/abstract=1234",
        "https://osf.io/preprints/xyz",
        "https://www.preprints.org/manuscript/202608.0001/v1",
        "https://chemrxiv.org/engage/chemrxiv/article-details/abc",
    ],
)
def test_preprint_hosts_are_free_sites(url):
    from paper_fetch.robust_fetch import is_free_site

    assert is_free_site(url) is True


@pytest.mark.asyncio
async def test_researchsquare_403_not_reported_as_paywall():
    """ResearchSquare 预印本被反爬 403：meta_adapter 不得标 requires_auth=True
    （否则前端误弹「付费墙/机构登录」）。"""
    from paper_fetch.meta_adapter import fetch_via_landing_page

    rs_url = "https://www.researchsquare.com/articles/rs-1234/v1"
    with patch(
        "paper_fetch.meta_adapter._fetch_html",
        AsyncMock(return_value=(None, None, 403, rs_url)),
    ):
        pdf, doi, landing_info = await fetch_via_landing_page(rs_url)

    assert pdf is None
    assert landing_info["requires_auth"] is False


def test_subscription_hosts_do_not_include_preprint_sites():
    """订阅站表不该把预印本站当付费墙刊（对齐的方向性检查）。"""
    from paper_fetch.robust_fetch import is_free_site
    from paper_fetch.service import _SUBSCRIPTION_HOSTS

    for host in _SUBSCRIPTION_HOSTS:
        assert not is_free_site(f"https://{host}/x"), (
            f"{host} 同时出现在订阅站表与免费站表，判定自相矛盾"
        )


# ---------- 修复5：预印本 DOI 模板直链前置 ----------


@pytest.mark.asyncio
async def test_preprint_doi_without_url_short_circuits_via_template():
    """只给 bioRxiv DOI、没给 URL：主链 ① 段用模板构造落地页直下，
    不再白跑 openalex 等元数据 API。"""
    from paper_fetch import service as svc

    fake_pdf = b"%PDF-preprint-doi"
    with patch(
        "paper_fetch.service._resolve_published_doi_for_download",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_preprint_pdf",
        AsyncMock(return_value=fake_pdf),
    ) as mock_preprint, patch(
        "paper_fetch.service.probe_oa",
        AsyncMock(return_value=(None, [], False)),
    ) as mock_probe:
        result = await svc.download_pdf(
            doi="10.1101/2024.05.01.123456", paper_url=None, oa_url=None
        )

    assert result["success"] is True
    assert result["source"] == "preprint"
    assert result["pdf_bytes"] == fake_pdf
    # 模板直链与 preprint_discovery._preprint_url_for_doi 同款
    mock_preprint.assert_awaited_once_with(
        "https://www.biorxiv.org/content/10.1101/2024.05.01.123456"
    )
    # 短路命中后不再白跑 openalex
    mock_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_arxiv_doi_without_url_short_circuits_via_template():
    """只给 arXiv DOI（官方大小写 arXiv. 形态）、没给 URL：同样模板短路。"""
    from paper_fetch import service as svc

    fake_pdf = b"%PDF-arxiv-doi"
    with patch(
        "paper_fetch.service._resolve_published_doi_for_download",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_preprint_pdf",
        AsyncMock(return_value=fake_pdf),
    ) as mock_preprint:
        result = await svc.download_pdf(
            doi="10.48550/arXiv.2301.99999", paper_url=None, oa_url=None
        )

    assert result["success"] is True
    assert result["source"] == "preprint"
    mock_preprint.assert_awaited_once_with("https://arxiv.org/abs/2301.99999")


@pytest.mark.asyncio
async def test_preprint_doi_upgrade_to_published_still_wins():
    """升级语义不破坏：预印本 DOI 能解析到正式版 DOI 时，仍优先走正式版主链，
    不用预印本模板短路。"""
    from paper_fetch import service as svc

    fake_pdf = b"%PDF-published"
    with patch(
        "paper_fetch.service._resolve_published_doi_for_download",
        AsyncMock(return_value="10.1038/s41586-024-0001"),
    ), patch(
        "paper_fetch.service.fetch_preprint_pdf",
        AsyncMock(return_value=None),
    ) as mock_preprint, patch(
        "paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)
    ), patch(
        "paper_fetch.service.probe_oa",
        AsyncMock(return_value=(None, [], False)),
    ), patch(
        "paper_fetch.service.is_elsevier_target",
        MagicMock(return_value=False),
    ), patch(
        "paper_fetch.service.fetch_publisher_direct",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_landing_page",
        AsyncMock(return_value=(fake_pdf, None, _NL)),
    ):
        result = await svc.download_pdf(
            doi="10.1101/2024.05.01.123456", paper_url=None, oa_url=None
        )

    assert result["success"] is True
    # 模板直链段不该被触发（fetch_preprint_pdf 只可能来自模板段：本测试没给 paper_url）
    mock_preprint.assert_not_awaited()
    # meta 段（正式版主链）命中
    assert result["source"] == "meta"
