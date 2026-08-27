"""下载链预印本回落修复测试（2026-08-26 Cell/Open-ST 顶包事故复盘）。

事故（缺陷 4）：显式传入 bioRxiv DOI（10.1101/2023.12.22.572554）时，
_resolve_published_doi_for_download 把它升级成正式版 DOI 后 doi_effective 被覆盖，
①0 预印本模板直下被 `not published_doi` 跳过——正式版主路全失败后没有任何一段
回到预印本，整条链退化为「和正式版 DOI 完全相同的路径」，重试预印本 DOI 无法自救。

老板拍板的链路语义：先找正式版 → 正式版所有合法路径都下不到 → 退而下载同研究
预印本 → 预印本也下不到才空手返回。修复：⑥a preprint_doi_fallback 段用**原始**
预印本 DOI 模板直下（在 web_pdf_discovery 之后、preprint_discovery 之前）。

顺带覆盖 C（可观测性）：交付 URL 与身份核验结论落结果字典 / attempts。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_NL = {"url": None, "publisher": None, "requires_auth": False}

_PREPRINT_DOI = "10.1101/2023.12.22.572554"
_PUBLISHED_DOI = "10.1016/j.cell.2024.05.055"  # Cell 订阅前缀 → 正式版主路全失败后 auth_required
_PREPRINT_TEMPLATE_URL = f"https://www.biorxiv.org/content/{_PREPRINT_DOI}"
_TITLE = "Open-ST: High-resolution spatial transcriptomics in 3D"


def _mock_published_main_road(*, discover_returns=None, call_log: list | None = None):
    """mock 正式版主路全部失败：结构化源/浏览器/网页发现都拿不到，只有预印本可活。

    call_log 用来断言段序：每个被 mock 的 adapter 调用时 append (名, 参数摘要)。
    """
    def _log(name):
        def _wrapper(*args, **kwargs):
            if call_log is not None:
                call_log.append((name, args, kwargs))
            return None
        return _wrapper

    mocks = [
        patch(
            "paper_fetch.service.fetch_preprint_pdf",
            AsyncMock(side_effect=_log("fetch_preprint_pdf")),
        ),
        patch(
            "paper_fetch.service.fetch_oa_pdf",
            AsyncMock(side_effect=_log("fetch_oa_pdf")),
        ),
        patch(
            "paper_fetch.service.probe_oa",
            AsyncMock(return_value=(None, [], False)),
        ),
        patch(
            "paper_fetch.service.is_elsevier_target",
            MagicMock(return_value=False),
        ),
        patch(
            "paper_fetch.service.fetch_publisher_direct",
            AsyncMock(side_effect=_log("fetch_publisher_direct")),
        ),
        patch(
            "paper_fetch.service.fetch_via_landing_page",
            AsyncMock(return_value=(None, None, _NL)),
        ),
        patch(
            "paper_fetch.service.fetch_via_unpaywall",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_fetch.service.fetch_via_crossref",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_fetch.service.fetch_via_europe_pmc",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_fetch.service.fetch_via_browser_landing",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_fetch.service.can_discover_pdf_via_web",
            MagicMock(return_value=True),
        ),
        patch(
            "paper_fetch.service.discover_pdf_via_web",
            AsyncMock(return_value=discover_returns),
        ),
        patch(
            "paper_fetch.service.crossref_meta_for_doi",
            AsyncMock(return_value=(_TITLE, [])),
        ),
        patch(
            "paper_fetch.service.discover_preprint",
            AsyncMock(return_value=None),
        ),
    ]
    return mocks


@pytest.mark.asyncio
async def test_显式预印本DOI_升级正式版全失败后回落原始预印本直下():
    """缺陷 4 回归：正式版主路（含 web_pdf_discovery）全失败后，必须用**原始**
    预印本 DOI（不是升级后的正式版 DOI）构造模板 URL 再直下一次并交付。"""
    import contextlib

    from paper_fetch import service as svc

    fake_preprint_pdf = b"%PDF-openst-preprint"
    call_order: list[str] = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "paper_fetch.service._resolve_published_doi_for_download",
                AsyncMock(return_value=_PUBLISHED_DOI),
            )
        )
        for m in _mock_published_main_road(call_log=None):
            stack.enter_context(m)
        # 预印本模板直下在 ⑥a 命中（①0 已被 `not published_doi` 跳过，这是它
        # 本次链内第一次被调用）
        preprint_mock = stack.enter_context(
            patch(
                "paper_fetch.service.fetch_preprint_pdf",
                AsyncMock(return_value=fake_preprint_pdf),
            )
        )
        discover_mock = stack.enter_context(
            patch(
                "paper_fetch.service.discover_pdf_via_web",
                AsyncMock(side_effect=lambda *a, **k: call_order.append("web") or None),
            )
        )
        result = await svc.download_pdf(
            doi=_PREPRINT_DOI, paper_url=None, oa_url=None, title=_TITLE
        )

    assert result["success"] is True
    assert result["source"] == "preprint"
    assert result["pdf_bytes"] == fake_preprint_pdf
    # 用原始预印本 DOI 的模板 URL 直下（原始 DOI 没有被正式版覆盖丢失）
    preprint_mock.assert_awaited_once_with(_PREPRINT_TEMPLATE_URL)
    # 交付语义对齐 preprint_discovery：requested/delivered_doi 说清「要的是正式版、
    # 拿到的是预印本」，查重/通知层才能正确识别
    assert result["requested_doi"] == _PUBLISHED_DOI
    assert result["delivered_doi"] == _PREPRINT_DOI
    assert result["delivered_version"] == "preprint"
    assert result["notice"]
    assert "preprint_doi_fallback" in result["tried_sources"]
    # C：交付来源 URL 落结果字典（不再是 None）
    assert result["content_url"] == _PREPRINT_TEMPLATE_URL
    # 段序：正式版网页发现先跑、预印本回落后跑（先找正式版，找得到就不动预印本）
    assert call_order == ["web"]
    discover_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_显式预印本DOI_预印本直下也失败_链继续到preprint_discovery():
    """⑥a 失败不终局：继续走 ⑥b preprint_discovery（按标题发现），仍可能交付。"""
    import contextlib

    from paper_fetch import service as svc

    fake_pdf = b"%PDF-openst-by-discovery"
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "paper_fetch.service._resolve_published_doi_for_download",
                AsyncMock(return_value=_PUBLISHED_DOI),
            )
        )
        for m in _mock_published_main_road():
            stack.enter_context(m)
        discovery_mock = stack.enter_context(
            patch(
                "paper_fetch.service.discover_preprint",
                AsyncMock(
                    return_value={"doi": _PREPRINT_DOI, "url": _PREPRINT_TEMPLATE_URL}
                ),
            )
        )
        # ⑥a 模板直下失败 → ⑥b 的 _download_preprint_candidate 里再试一次 preprint 模板
        preprint_calls: list[str] = []

        async def _preprint(url):
            preprint_calls.append(url)
            return fake_pdf if len(preprint_calls) > 1 else None

        stack.enter_context(
            patch(
                "paper_fetch.service.fetch_preprint_pdf",
                AsyncMock(side_effect=_preprint),
            )
        )
        result = await svc.download_pdf(
            doi=_PREPRINT_DOI, paper_url=None, oa_url=None, title=_TITLE
        )

    assert result["success"] is True
    assert result["source"] == "preprint_discovery"
    assert result["delivered_doi"] == _PREPRINT_DOI
    discovery_mock.assert_awaited_once()
    assert "preprint_doi_fallback" in result["tried_sources"]
    assert "preprint_discovery" in result["tried_sources"]


@pytest.mark.asyncio
async def test_正式版可下到时不回落预印本_升级语义保持():
    """升级到正式版后正式版主路命中 → 不碰预印本（①0 跳过、⑥a 走不到）。"""
    import contextlib

    from paper_fetch import service as svc

    fake_published_pdf = b"%PDF-openst-cell"
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            patch(
                "paper_fetch.service._resolve_published_doi_for_download",
                AsyncMock(return_value=_PUBLISHED_DOI),
            )
        )
        for m in _mock_published_main_road():
            stack.enter_context(m)
        preprint_mock = stack.enter_context(
            patch(
                "paper_fetch.service.fetch_preprint_pdf",
                AsyncMock(return_value=b"%PDF-should-never-deliver"),
            )
        )
        stack.enter_context(
            patch(
                "paper_fetch.service.fetch_publisher_direct",
                AsyncMock(return_value=fake_published_pdf),
            )
        )
        result = await svc.download_pdf(
            doi=_PREPRINT_DOI, paper_url=None, oa_url=None, title=_TITLE
        )

    assert result["success"] is True
    assert result["source"] == "publisher_direct"
    assert result["pdf_bytes"] == fake_published_pdf
    preprint_mock.assert_not_awaited()  # 正式版命中 → 预印本完全没动
    assert "preprint_doi_fallback" not in result["tried_sources"]
    assert result["content_url"] == f"https://doi.org/{_PUBLISHED_DOI}"


@pytest.mark.asyncio
async def test_候选自带预印本URL时preprint_fallback处理_不重复跑模板段():
    """正式版 DOI + bioRxiv URL 的 defer 场景：⑥ preprint_fallback 用原始 URL 兜底，
    ⑥a 不重复跑（条件 preprint_fallback_url 非空时跳过）。"""
    from paper_fetch.service import download_pdf

    fake_pdf = b"%PDF-preprint-by-url"
    with patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)), \
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
         patch("paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=fake_pdf)) as mock_preprint, \
         patch("paper_fetch.service._try_library_proxy", AsyncMock(return_value=(None, "stalled"))), \
         patch("paper_fetch.service._library_proxy_gate", AsyncMock(return_value=(True, True))):
        result = await download_pdf(
            doi=_PUBLISHED_DOI,
            paper_url="https://www.biorxiv.org/content/10.1101/2023.12.22.572554v1",
            oa_url=None,
            title=_TITLE,
        )

    assert result["success"] is True
    assert result["source"] == "preprint"
    assert "preprint_fallback" in result["tried_sources"]
    assert "preprint_doi_fallback" not in result["tried_sources"]  # 不重复跑
    # ⑥ 用的是候选原始 URL（带版本号），不是 ⑥a 的模板 URL
    mock_preprint.assert_awaited_once_with(
        "https://www.biorxiv.org/content/10.1101/2023.12.22.572554v1"
    )


# ---------------------------------------------------------------------------
# C. 可观测性：attempts 带命中 URL 与核验结论
# ---------------------------------------------------------------------------


# 注：TestAttemptsObservability（document_acquisition_service 落库观测）依赖
# PaperPilot 数据库，留在 PaperPilot 仓库跑。
