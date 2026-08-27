"""付费墙论文找开放预印本（oa_preprint_discovery）单元测试。

覆盖：
- Tavily 号池为空 → 直接 None（不调 tavily）
- 标题太短 → None
- 预印本站域名 + 标题相似 → 返回该 URL
- 同站但标题完全不同（如另一篇论文）→ 不选
- 非预印本站（即便标题对）→ 不选
- 预印本与正刊标题小改（tripled→quadrupled）→ 仍能匹配（放宽阈值的关键用例）
- 2026-08-24 顶包事故回归：标题不同但摘要高度相似 → 必须拒（只比标题，阈值 0.72）
- tavily 号池全用尽 / 异常 → 优雅返回 None（不打断下载）

号池层：conftest 默认让 tavily_pool 为空；要测「有号」的用例 patch has_keys=True。
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from paper_fetch import oa_preprint_discovery as M
from paper_fetch.tavily_client import TavilyQuotaExhausted, WebResult

_TITLE = "Human-driven sea-level rise has quadrupled the frequency of coastal sea-level extremes since 1900"


def _cand(title: str, url: str, abstract: str | None = None) -> WebResult:
    return WebResult(title=title, url=url, content=abstract, score=0.9)


def _enable_pool(monkeypatch):
    """让号池「有号」（conftest 默认为空）。"""
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: True)


def test_empty_pool_returns_none(monkeypatch):
    monkeypatch.setattr(M.tavily_client, "has_keys", lambda: False)
    assert asyncio.run(M.discover_oa_preprint_url(_TITLE)) is None


def test_short_title_returns_none(monkeypatch):
    _enable_pool(monkeypatch)
    assert asyncio.run(M.discover_oa_preprint_url("短")) is None


def test_preprint_host_title_match_returns_url(monkeypatch):
    _enable_pool(monkeypatch)
    # 预印本标题是 tripled（正刊是 quadrupled），只差一词 → 应仍匹配
    cands = [
        _cand("Human-driven sea-level rise has tripled the frequency of coastal sea-level extremes since 1900",
              "https://www.researchsquare.com/article/rs-7491013/v1.pdf"),
    ]
    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)):
        url = asyncio.run(M.discover_oa_preprint_url(_TITLE))
    assert url == "https://www.researchsquare.com/article/rs-7491013/v1.pdf"


def test_same_host_different_paper_not_selected(monkeypatch):
    _enable_pool(monkeypatch)
    # 同在 researchsquare 但完全是另一篇论文 → 标题相似度低 → 不选
    cands = [
        _cand("Key drivers of large scale changes in North Atlantic atmospheric and ocean circulation",
              "https://www.researchsquare.com/article/rs-4977370/v1.pdf"),
    ]
    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)):
        url = asyncio.run(M.discover_oa_preprint_url(_TITLE))
    assert url is None


def test_non_preprint_host_not_selected(monkeypatch):
    _enable_pool(monkeypatch)
    # 标题完全一致但不是预印本站（如新闻站）→ 不选
    cands = [_cand(_TITLE, "https://www.prnewswire.com/news-releases/whatever.html")]
    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)):
        url = asyncio.run(M.discover_oa_preprint_url(_TITLE))
    assert url is None


# ── 2026-08-24 顶包事故回归：只比标题、不比摘要，阈值 0.72 ─────────────────
# 事故：目标 Spateo 论文（"Spatiotemporal modeling of molecular holograms"），
# Tavily 在预印本站搜到同实验室另一篇（Navigo，摘要用词高度相似），旧逻辑
# 「标题+摘要词重叠 0.6」放行 → 题录对、正文错的坏条目入库。
_SPATEO_TITLE = "Spatiotemporal modeling of molecular holograms"
_NAVIGO_TITLE = "Generative Modeling of Mouse Embryogenesis for Fate and Disease Prediction"
_NAVIGO_ABSTRACT = (
    "We present a generative framework for the spatiotemporal modeling of molecular "
    "holograms, enabling joint molecular hologram reconstruction and fate prediction "
    "of mouse embryogenesis in space and time."
)


def test_match_threshold_is_072():
    assert M._MATCH_THRESHOLD == 0.72


def test_match_score_ignores_abstract():
    """摘要塞满目标标题实词也救不了标题不匹配的候选（分数必须低于阈值）。"""
    score = M._match_score(_SPATEO_TITLE, _NAVIGO_TITLE)
    assert score < M._MATCH_THRESHOLD


def test_similar_abstract_different_title_not_selected(monkeypatch):
    """端到端：标题不同、摘要高度相似的同实验室论文必须被拒。"""
    _enable_pool(monkeypatch)
    cands = [_cand(
        _NAVIGO_TITLE,
        "https://www.biorxiv.org/content/10.1101/2025.10.30.624999v1.full.pdf",
        abstract=_NAVIGO_ABSTRACT,
    )]
    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)):
        url = asyncio.run(M.discover_oa_preprint_url(_SPATEO_TITLE))
    assert url is None


def test_true_title_match_with_decoration_still_selected(monkeypatch):
    """对照：标题真匹配（带 [PDF] 装饰前缀）的候选仍通过——闸门收紧不误伤正例。"""
    _enable_pool(monkeypatch)
    cands = [_cand(
        f"[PDF] {_SPATEO_TITLE} - bioRxiv",
        "https://www.biorxiv.org/content/10.1101/2026.01.01.000001v1.full.pdf",
    )]
    with patch.object(M.tavily_client, "search_with_pool", new=AsyncMock(return_value=cands)):
        url = asyncio.run(M.discover_oa_preprint_url(_SPATEO_TITLE))
    assert url == "https://www.biorxiv.org/content/10.1101/2026.01.01.000001v1.full.pdf"


def test_pool_exhausted_returns_none(monkeypatch):
    _enable_pool(monkeypatch)
    with patch.object(M.tavily_client, "search_with_pool",
                      new=AsyncMock(side_effect=TavilyQuotaExhausted("号池全用尽"))):
        assert asyncio.run(M.discover_oa_preprint_url(_TITLE)) is None


def test_tavily_error_returns_none(monkeypatch):
    _enable_pool(monkeypatch)
    with patch.object(M.tavily_client, "search_with_pool",
                      new=AsyncMock(side_effect=RuntimeError("网络炸了"))):
        assert asyncio.run(M.discover_oa_preprint_url(_TITLE)) is None
