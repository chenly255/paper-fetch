"""PDF 身份核验门测试（2026-08-23 顶包事故整改）。

覆盖：
  A. pdf_identity 纯函数：DOI 命中 / 标题命中 / 顶包拒收（综述、引用列表）/ 占位标题
     过滤 / 无文本 / 无锚点。
  B. 下载链接入：strict 段（web_pdf_discovery / oa）下到「题录是 A、正文是 B」的 PDF
     必须拒收——不返回成功、全链失败归类 wrong_paper、不落假条目（不产生 document）。
  C. 拒收后继续降级：顶包被拒后后续段（scihub / openalex）拿到正确 PDF 仍能成功。
  D. 真论文正常放行：主标题在首页上半区，不被误杀。

PDF 用 pymupdf 现场构造（标题在上半区 y≈100、参考文献在下半区 y≈600），不碰网络。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 与 test_paper_download_service 同款中性 landing_info
_NL = {"url": None, "publisher": None, "requires_auth": False}

_TARGET_DOI = "10.1126/science.ado3927"
_TARGET_TITLE = "Brain-wide spatial transcriptomics of the mouse cerebellum across three species"


def _make_pdf(lines_upper: list[str], lines_lower: list[str] | None = None) -> bytes:
    """构造单页 PDF：上半区文本在 y≈100 起，下半区在 y≈600 起（A4 高 842）。

    注意 insert_text 不自动换行：单行过长会被截断（行尾丢失），测试里的行请保持短行。
    含 CJK 字符的行用 fitz 内置中文字体（默认 helv 无中文字形，中文会写成空白）。
    """
    import re

    import fitz

    _cjk_re = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")

    def _insert(page, y, text, size):  # noqa: ANN001
        kw = {"fontname": "china-s"} if _cjk_re.search(text) else {}
        page.insert_text((72, y), text, fontsize=size, **kw)

    doc = fitz.open()
    page = doc.new_page()
    y = 100
    for line in lines_upper:
        _insert(page, y, line, 12)
        y += 20
    y = 600
    for line in lines_lower or []:
        _insert(page, y, line, 10)
        y += 16
    out = doc.tobytes()
    doc.close()
    return out


# ---------------------------------------------------------------------------
# A. pdf_identity 纯函数
# ---------------------------------------------------------------------------


class TestVerifyPdfIdentity:
    def test_doi_match_真论文(self):
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [f"{_TARGET_TITLE}", "Abstract: We profile the cerebellum across species."],
            [f"doi: {_TARGET_DOI}"],
        )
        v = verify_pdf_identity(pdf, doi=_TARGET_DOI, title=_TARGET_TITLE)
        assert v.ok and v.reason == "doi_match"

    def test_title_match_无DOI锚点(self):
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [_TARGET_TITLE, "Spatial transcriptomics of cerebellum, mouse, human, marmoset."]
        )
        v = verify_pdf_identity(pdf, doi=None, title=_TARGET_TITLE)
        assert v.ok and v.reason == "title_match"

    def test_综述顶包_主标题不是目标_拒收(self):
        """事故 a 场景：开放获取综述引用了目标论文，主标题是综述自己的。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [
                "A survey of spatial transcriptomics methods and applications",
                "We review recent advances in sequencing technologies.",
            ],
            [f"[1] Someone. {_TARGET_TITLE}.", f"Science 2024. doi:{_TARGET_DOI}"],
        )
        v = verify_pdf_identity(pdf, doi=_TARGET_DOI, title=_TARGET_TITLE)
        assert not v.ok and v.reason == "mismatch"

    def test_引用列表顶包_满篇目标标题词但主标题区没有_拒收(self):
        """事故 a 场景（MOSTA）：整页参考文献列表，含目标 DOI 与标题。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        refs: list[str] = []
        for i in range(1, 15):
            refs.append(f"[{i}] {_TARGET_TITLE}. Science.")
            refs.append(f"doi:{_TARGET_DOI}")
        pdf = _make_pdf(["References and bibliography of the book"], refs)
        v = verify_pdf_identity(pdf, doi=_TARGET_DOI, title=_TARGET_TITLE)
        assert not v.ok and v.reason == "mismatch"
        assert "citation_list_pattern" in v.detail

    def test_另一篇论文顶包_拒收(self):
        """事故 a 场景（Neuron）：题录是 A、PDF 是另一篇脑图谱论文。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [
                "A single-cell atlas of the whole mouse brain homogenized samples",
                "Cell typing and spatial mapping with MERFISH panels.",
            ],
        )
        v = verify_pdf_identity(pdf, doi=_TARGET_DOI, title=_TARGET_TITLE)
        assert not v.ok

    def test_占位标题不当锚点_有DOI真PDF放行(self):
        """事故 b 场景：标题是占位名，真 PDF（DOI 命中）不能被误拒。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [_TARGET_TITLE, "The cerebellum atlas across mouse, human and marmoset."],
            [f"DOI: {_TARGET_DOI}"],
        )
        for placeholder in (None, "未识别-文献", "10.1126_science.ado3927", f"{_TARGET_DOI}.pdf"):
            v = verify_pdf_identity(pdf, doi=_TARGET_DOI, title=placeholder)
            assert v.ok, f"占位标题 {placeholder!r} 不应导致真 PDF 被拒"

    def test_无文本_无法核验但放行(self):
        """扫描件/无文本层 PDF 无法核验：放行（不误杀扫描件真论文），reason 标记可辨。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf([])  # 空白页（模拟扫描件无文本层）
        v = verify_pdf_identity(pdf, doi=_TARGET_DOI, title=_TARGET_TITLE)
        assert v.ok and v.reason == "no_text_unverifiable"

    def test_无锚点_放行(self):
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(["Whatever content"])
        v = verify_pdf_identity(pdf, doi=None, title=None)
        assert v.ok and v.reason == "no_anchor"

    # ---- 二审返工防回归（A1 / A2 / B1）----

    def test_中文标题真PDF放行(self):
        """二审 A1：拉丁词规则把中文标题剥成空词集 → 中文论文 strict 段 100% 误杀。
        CJK 按二元切分后应正常按标题锚点放行。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        zh_title = "基于深度学习的空间转录组细胞分割方法"
        pdf = _make_pdf(
            [zh_title, "摘要 空间转录组 细胞分割 深度学习 方法的应用研究。"],
            ["DOI: 10.1234/zh.example.001"],
        )
        # DOI 锚定 + 中文标题锚点
        v = verify_pdf_identity(pdf, doi="10.1234/zh.example.001", title=zh_title)
        assert v.ok, f"中文标题真论文不得误杀（reason={v.reason} {v.detail}）"
        # 纯标题锚点（无 DOI）也要放行
        v2 = verify_pdf_identity(pdf, doi=None, title=zh_title)
        assert v2.ok and v2.reason == "title_match", f"{v2.reason} {v2.detail}"

    def test_前缀DOI误命中_拒收(self):
        """二审 A2：目标 10.1111/exampler.123456，PDF 只含 10.1111/exampler.1234567
        （IEEE 版本后缀形态）——裸子串会误放行，尾边界必须拦住。
        2026-08-26 起该形态同时命中 foreign_doi（首页自报异 DOI 且非预印本前缀），
        拒收结论不变、reason 更精确。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            ["A totally different IEEE paper about signal processing"],
            ["doi: 10.1109/ICCV.2024.1234567"],
        )
        v = verify_pdf_identity(
            pdf, doi="10.1109/ICCV.2024.123456", title="Signal processing methods"
        )
        assert not v.ok and v.reason == "foreign_doi", f"前缀 DOI 不得误命中（{v.reason}）"

    def test_尾边界不误伤精确DOI(self):
        """A2 配套：DOI 后跟句号/空白（正常文本形态）仍算命中，边界规则不误伤。"""
        from paper_fetch.pdf_identity import _doi_in_text

        assert _doi_in_text("10.1109/x.123456", "see doi 10.1109/x.123456.")
        assert _doi_in_text("10.1109/x.123456", "https://doi.org/10.1109/x.123456, 2024")
        assert not _doi_in_text("10.1109/x.123456", "10.1109/x.1234567")

    def test_勘误页_拒收(self):
        """二审 B1：「Corrigendum to <原标题>」DOI 命中 + 主标题区含原标题词——
        它是单独发表的更正启事不是原文，必须拒收（评审员实测 Nature Author
        Correction 可触发）。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [f"Author Correction: {_TARGET_TITLE}", "The original article contained errors."],
            [f"Original article: doi:{_TARGET_DOI}", "This correction: doi:10.1038/s41586-9999"],
        )
        v = verify_pdf_identity(pdf, doi=_TARGET_DOI, title=_TARGET_TITLE)
        assert not v.ok and v.reason == "mismatch"
        assert v.detail == "corrigendum_page"


# ---------------------------------------------------------------------------
# 2026-08-26 Cell/Open-ST 顶包事故回归：首页自报异 DOI 拒收 + 判别 token 加固
# ---------------------------------------------------------------------------

_ACCIDENT_PDF = Path(__file__).parent / "fixtures" / "frontiers_review_impersonating_openst_p1.pdf"
_ACCIDENT_TARGET_DOI = "10.1016/j.cell.2024.05.055"  # Cell 2024 正式版
_ACCIDENT_TARGET_TITLE = "Open-ST: High-resolution spatial transcriptomics in 3D"
_ACCIDENT_FOREIGN_DOI = "10.3389/fbinf.2025.1715821"  # 顶包综述自报的 DOI


class TestForeignDoiRejection:
    """事故主规则：首页自报异 DOI（不能解释为同一篇的预印本版本）→ 拒收。

    事故复盘（2026-08-26）：目标 Open-ST（Cell 付费墙），web_pdf_discovery 按标题
    抓到一篇 Frontiers 综述顶包——首页明印自己的 10.3389/ DOI，核验却只当备查；
    标题覆盖率路径又被通用词击穿（open 撞页眉 OPEN ACCESS、high 撞 high-throughput、
    spatial/transcriptomics 撞综述自己的标题，upper_cov=0.80 放行）。
    """

    def test_事故PDF回归_改动前放行现在必须拒收(self):
        """真实事故 PDF 首页（fixtures 单页抽取）：改动前 ok=True/upper_cov=0.80，
        改动后必须 ok=False。双保险：foreign_doi 规则 + tokenize 判别词加固。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        v = verify_pdf_identity(
            _ACCIDENT_PDF.read_bytes(),
            doi=_ACCIDENT_TARGET_DOI,
            title=_ACCIDENT_TARGET_TITLE,
        )
        assert not v.ok
        assert v.reason == "foreign_doi"
        assert _ACCIDENT_FOREIGN_DOI in v.detail

    def test_事故PDF_无DOI锚点时标题路径也要拦住(self):
        """tokenize 加固的独立验证：就算没有 DOI 锚点（只有标题），open-st/3d/
        high-resolution 这些判别 token 参与覆盖后，通用词凑不出 0.7。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        v = verify_pdf_identity(
            _ACCIDENT_PDF.read_bytes(), doi=None, title=_ACCIDENT_TARGET_TITLE
        )
        assert not v.ok

    def test_预印本变体放行_目标正式版_首页印bioRxivDOI(self):
        """「同一篇的不同版本」必须放行：正式版拿不到、回落下到 bioRxiv PDF，
        首页印的是预印本 DOI——不按 foreign_doi 拒收，交给标题覆盖把关（同一篇
        标题一致 → title_match 放行且 detail 标注预印本 DOI）。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [_ACCIDENT_TARGET_TITLE, "We present Open-ST, a high-resolution framework."],
            ["preprint doi: 10.1101/2023.12.22.572554"],
        )
        v = verify_pdf_identity(pdf, doi=_ACCIDENT_TARGET_DOI, title=_ACCIDENT_TARGET_TITLE)
        assert v.ok, f"预印本变体不得误杀（reason={v.reason} {v.detail}）"
        assert v.reason == "title_match"
        assert "10.1101/2023.12.22.572554" in v.detail  # reason 里标注预印本归属

    def test_预印本变体_首页印预印本DOI_但标题不是这篇_仍拒(self):
        """预印本前缀放行不是免死金牌：自报 bioRxiv DOI 但标题对不上（另一篇的
        预印本），标题覆盖不达标 → 拒收（mismatch，不是 foreign_doi）。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            ["An unrelated study of yeast ribosomes in translation"],
            ["doi: 10.1101/2024.01.01.000001"],
        )
        v = verify_pdf_identity(pdf, doi=_ACCIDENT_TARGET_DOI, title=_ACCIDENT_TARGET_TITLE)
        assert not v.ok and v.reason == "mismatch"

    def test_目标就是预印本DOI_首页印同DOI_照常doi_match(self):
        """目标 DOI 锚定本身命中的场景不受 foreign_doi 规则影响（预印本直下段
        下到的就是对应预印本，首页 DOI 即目标）。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            [_ACCIDENT_TARGET_TITLE, "bioRxiv preprint for the Open-ST method."],
            ["doi: 10.1101/2023.12.22.572554"],
        )
        v = verify_pdf_identity(
            pdf, doi="10.1101/2023.12.22.572554", title=_ACCIDENT_TARGET_TITLE
        )
        assert v.ok and v.reason == "doi_match"

    def test_只给标题锚点_首页异DOI不触发foreign_doi(self):
        """无 DOI 锚点时无从判「异」（规则只在有目标 DOI 时启用，见模块 docstring）；
        该场景由标题覆盖（含判别 token）把关。"""
        from paper_fetch.pdf_identity import verify_pdf_identity

        pdf = _make_pdf(
            ["Applications of AI to single-cell and spatial transcriptomics"],
            ["doi: 10.3389/fbinf.2025.1715821"],
        )
        v = verify_pdf_identity(pdf, doi=None, title=_ACCIDENT_TARGET_TITLE)
        # 标题覆盖不达标 → 拒（mismatch），但不是 foreign_doi（没有目标 DOI 可比）
        assert not v.ok and v.reason == "mismatch"


class TestTokenizeDiscriminativeTokens:
    """判别 token 加固：连字符整词与含数字短词不再被切碎丢弃。"""

    def test_连字符整词与含数字短词保留(self):
        from paper_fetch.pdf_identity import _tokenize

        t = _tokenize("Open-ST: High-resolution spatial transcriptomics in 3D")
        assert "open-st" in t
        assert "high-resolution" in t
        assert "3d" in t
        assert "st" not in t  # 无数字的两字符碎片仍然丢弃

    def test_通用词撞不出假覆盖(self):
        from paper_fetch.pdf_identity import title_coverage

        # 事故实测形态：页眉 OPEN ACCESS + high-throughput + 综述自己的 spatial 标题
        hay = (
            "OPEN ACCESS\nApplications of AI to single-cell and spatial transcriptomics\n"
            "high-throughput methods"
        )
        cov = title_coverage(_ACCIDENT_TARGET_TITLE, hay)
        # 8 个 token 只命中 4 个通用词（open/high/spatial/transcriptomics），
        # 判别词 open-st/high-resolution/3d/resolution 缺席 → 0.5，低于 0.7 阈值
        assert cov < 0.7, f"判别 token 加固失效（cov={cov:.2f}）"

    def test_真论文连字符标题照常命中(self):
        from paper_fetch.pdf_identity import title_coverage

        hay = _ACCIDENT_TARGET_TITLE + " bioRxiv preprint"
        cov = title_coverage(_ACCIDENT_TARGET_TITLE, hay)
        assert cov >= 0.99  # 同串标题全命中（额外 token 不误伤真论文）


class TestPlaceholderTitle:
    def test_占位判定(self):
        from paper_fetch.pdf_identity import is_placeholder_title

        for bad in (
            None,
            "",
            "未识别",
            "未识别 10.1126/x",
            "paper.pdf",
            "10.1126/science.ado3927",
            "2024_05_03_ado3927",
        ):
            assert is_placeholder_title(bad), f"{bad!r} 应判占位"
        for good in (_TARGET_TITLE, "A survey of methods"):
            assert not is_placeholder_title(good), f"{good!r} 不应判占位"
        # 「Untitled …」开头是展示用兜底文案，非真标题，也判占位
        assert is_placeholder_title("Untitled document 3")


# ---------------------------------------------------------------------------
# B/C/D. 下载链接入（mock 全部网络，只验证核验门行为）
# ---------------------------------------------------------------------------


def _fake_pdf_downloaded_via_discovery() -> bytes:
    """web_pdf_discovery 段下到的「顶包」：正文是另一篇论文（题录是 A、正文是 B）。"""
    return _make_pdf(
        [
            "An open-access review of spatial biology technologies",
            "This review surveys recent methods in spatial omics.",
        ],
    )


@pytest.fixture(autouse=True)
def _clean_cooldowns():
    from paper_fetch import domain_cooldown as dc

    dc.reset_cooldowns()
    yield
    dc.reset_cooldowns()


def _mock_chain_prefix():
    """mock 下载链前置段（preprint/openalex/elsevier/publisher/meta/unpaywall/crossref/europe_pmc/browser
    及 preprint_discovery 的元数据查询——测试不碰真实网络）。"""
    return [
        patch(
            "paper_fetch.service._resolve_published_doi_for_download",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_fetch.service.fetch_preprint_pdf", AsyncMock(return_value=None)
        ),
        patch("paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)),
        patch(
            "paper_fetch.service.probe_oa",
            AsyncMock(return_value=(None, [], False)),
        ),
        patch(
            "paper_fetch.service.is_elsevier_target", MagicMock(return_value=False)
        ),
        patch(
            "paper_fetch.service.fetch_publisher_direct",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_fetch.service.fetch_via_landing_page",
            AsyncMock(return_value=(None, None, _NL)),
        ),
        patch(
            "paper_fetch.service.fetch_via_unpaywall", AsyncMock(return_value=None)
        ),
        patch(
            "paper_fetch.service.fetch_via_crossref", AsyncMock(return_value=None)
        ),
        patch(
            "paper_fetch.service.fetch_via_europe_pmc", AsyncMock(return_value=None)
        ),
        patch(
            "paper_fetch.service.fetch_via_browser_landing",
            AsyncMock(return_value=None),
        ),
        patch(
            "paper_fetch.service.crossref_meta_for_doi",
            AsyncMock(return_value=(None, [])),
        ),
        patch(
            "paper_fetch.service.discover_preprint", AsyncMock(return_value=None)
        ),
    ]


_ZENODO_DOI = "10.5281/zenodo.1234567"  # 非订阅前缀：不触发 auth_required/paywall 分类


@pytest.mark.asyncio
async def test_顶包被拒_全链失败归类wrong_paper():
    """题录是 A、web_pdf_discovery 下到正文是 B 的 PDF → 拒收 + failure_detail=wrong_paper。"""
    import contextlib

    from paper_fetch import service as svc

    with contextlib.ExitStack() as stack:
        for m in _mock_chain_prefix():
            stack.enter_context(m)
        stack.enter_context(
            patch(
                "paper_fetch.service.can_discover_pdf_via_web",
                MagicMock(return_value=True),
            )
        )
        stack.enter_context(
            patch(
                "paper_fetch.service.discover_pdf_via_web",
                AsyncMock(
                    return_value=(
                        _fake_pdf_downloaded_via_discovery(),
                        "https://frontiers.example.org/fake-review.pdf",
                    )
                ),
            )
        )
        result = await svc.download_pdf(
            doi=_ZENODO_DOI,
            paper_url=None,
            oa_url=None,
            title=_TARGET_TITLE,
        )

    assert result["success"] is False
    assert result["failure_detail"] == "wrong_paper"
    assert "不符" in (result["message"] or "")
    # 顶包 PDF 绝不能作为成功结果外传
    assert result.get("pdf_bytes") is None


@pytest.mark.asyncio
async def test_顶包被拒后_继续降级到scihub成功():
    """顶包被拒不终止下载链：scihub（DOI 锚定段）拿到正确 PDF 仍成功交付。"""
    import contextlib

    from paper_fetch.config import get_config
    from paper_fetch import service as svc

    good_pdf = _make_pdf(
        [_TARGET_TITLE, "Cerebellum spatial atlas across mouse human and marmoset brains."],
        [f"doi: {_TARGET_DOI}"],
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(get_config(), "scihub_enabled", True))
        for m in _mock_chain_prefix():
            stack.enter_context(m)
        stack.enter_context(
            patch(
                "paper_fetch.service.can_discover_pdf_via_web",
                MagicMock(return_value=True),
            )
        )
        stack.enter_context(
            patch(
                "paper_fetch.service.discover_pdf_via_web",
                AsyncMock(
                    return_value=(
                        _fake_pdf_downloaded_via_discovery(),
                        "https://frontiers.example.org/fake-review.pdf",
                    )
                ),
            )
        )
        mock_scihub = stack.enter_context(
            patch(
                "paper_fetch.service.fetch_via_scihub",
                AsyncMock(return_value=good_pdf),
            )
        )
        result = await svc.download_pdf(
            doi=_TARGET_DOI,
            paper_url=None,
            oa_url=None,
            title=_TARGET_TITLE,
        )

    assert result["success"] is True
    assert result["source"] == "scihub"
    assert mock_scihub.await_count == 1  # 顶包被拒后链继续走到了 scihub


@pytest.mark.asyncio
async def test_oa段顶包被拒_openalex段正确PDF成功():
    """oa 段（外部传入 oa_url）下到顶包 → 拒收；openalex（DOI 锚定）拿到正确 PDF → 成功。"""
    import contextlib

    from paper_fetch import service as svc

    good_pdf = _make_pdf(
        [_TARGET_TITLE, "Spatial transcriptomics datasets from three species cerebellum."],
    )
    bad_pdf = _fake_pdf_downloaded_via_discovery()
    call_count = {"n": 0}

    async def _fetch_oa_by_call(url, **_kw):  # noqa: ANN001
        call_count["n"] += 1
        return bad_pdf if call_count["n"] == 1 else good_pdf

    with contextlib.ExitStack() as stack:
        for m in _mock_chain_prefix():
            # fetch_oa_pdf / probe_oa 由下方单独 patch 覆盖，跳过前缀里的同名项
            if "fetch_oa_pdf" in str(m) or "probe_oa" in str(m):
                continue
            stack.enter_context(m)
        stack.enter_context(
            patch(
                "paper_fetch.service.fetch_oa_pdf",
                AsyncMock(side_effect=_fetch_oa_by_call),
            )
        )
        stack.enter_context(
            patch(
                "paper_fetch.service.probe_oa",
                AsyncMock(return_value=(None, ["https://openalex.org/good.pdf"], False)),
            )
        )
        result = await svc.download_pdf(
            doi=_TARGET_DOI,
            paper_url=None,
            oa_url="https://example.org/fake.pdf",
            title=_TARGET_TITLE,
        )

    assert result["success"] is True
    assert result["source"] == "openalex"
    assert call_count["n"] == 2  # oa 段一次（顶包被拒）+ openalex 段一次（正确 PDF）


@pytest.mark.asyncio
async def test_web_pdf_discovery真PDF正常放行():
    """D. 真论文（主标题在上半区）不被误杀——核验门只拦顶包。"""
    import contextlib

    from paper_fetch import service as svc

    good_pdf = _make_pdf(
        [_TARGET_TITLE, "We present a brain-wide atlas of the cerebellum in three species."],
        [f"Science. doi: {_TARGET_DOI}"],
    )
    with contextlib.ExitStack() as stack:
        for m in _mock_chain_prefix():
            stack.enter_context(m)
        stack.enter_context(
            patch(
                "paper_fetch.service.can_discover_pdf_via_web",
                MagicMock(return_value=True),
            )
        )
        stack.enter_context(
            patch(
                "paper_fetch.service.discover_pdf_via_web",
                AsyncMock(return_value=(good_pdf, "https://science.example.org/target.pdf")),
            )
        )
        result = await svc.download_pdf(
            doi=_TARGET_DOI,
            paper_url=None,
            oa_url=None,
            title=_TARGET_TITLE,
        )

    assert result["success"] is True
    assert result["source"] == "web_pdf_discovery"
