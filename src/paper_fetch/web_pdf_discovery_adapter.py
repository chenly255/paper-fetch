"""web_pdf_discovery_adapter：用 Tavily 找候选全文链接，再由下载器验真。

这一层补的是「未知期刊/未知站点」缺口：OpenAlex/Unpaywall/Crossref/出版商模板都没给出
可用 PDF 时，按 DOI/标题做一次网页发现，拿到候选 URL 后仍走现有 PDF magic bytes 校验。

边界：
- Tavily 只负责发现 URL，不把搜索结果当全文。
- 订阅制正式 DOI 的预印本站候选由上层放在机构链路之后处理；本层默认不接受预印本域名。
- Sci-Hub/LibGen 等灰色来源直接屏蔽，避免合法链路兜底被污染。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import unquote, urlparse

from . import tavily_client
from .tavily_client import TavilyQuotaExhausted, WebResult

from .meta_adapter import fetch_via_landing_page
from .robust_fetch import fetch_pdf_simple

logger = logging.getLogger(__name__)

_MIN_TITLE_LEN = 12
_MATCH_THRESHOLD = 0.85
_MAX_RESULTS_PER_QUERY = 6
_MAX_CANDIDATES_TO_TRY = 10

_PREPRINT_HOSTS = (
    "researchsquare.com",
    "biorxiv.org",
    "medrxiv.org",
    "arxiv.org",
    "preprints.org",
    "osf.io",
    "ssrn.com",
    "chemrxiv.org",
    "techrxiv.org",
    "authorea.com",
)

_BLOCKED_HOST_MARKERS = (
    "sci-hub",
    "libgen",
    "z-library",
    "annas-archive",
)


@dataclass(frozen=True)
class _WebCandidate:
    url: str
    title: str
    abstract: str | None
    score: float
    doi_match: bool
    pdfish: bool
    tavily_score: float | None


def can_discover_pdf_via_web(doi: str | None, title: str | None) -> bool:
    """是否值得进入 Tavily 网页发现层。

    不在这里硬抛配置错误：下载链要能在无 Tavily key 的部署里继续走机构/手工兜底。
    """
    if not tavily_client.has_keys():
        return False
    return bool(_clean_doi(doi) or _usable_title(title))


async def discover_pdf_via_web(
    doi: str | None,
    title: str | None,
    *,
    referer: str | None = None,
    allow_preprint: bool = False,
) -> tuple[bytes, str] | None:
    """按 DOI/标题网搜候选 URL，逐个下载验真，命中返回 (PDF bytes, 命中候选 URL)。

    allow_preprint=False 时会跳过 bioRxiv/arXiv/Research Square 等预印本站，保证有正式版时
    不会在机构/正式链路之前降级到预印本。

    2026-08-26 事故复盘（可观测性）：返回值带上最终命中的候选 URL——该段 URL 来自网页
    搜索、不确定且不可复推，顶包事故排查时正是它缺席导致 document_acquisitions 只能记
    content_url=None、事后无法定位错 PDF 的来源页面。
    """
    if not can_discover_pdf_via_web(doi, title):
        return None

    candidates = await _collect_candidates(doi, title, allow_preprint=allow_preprint)
    if not candidates:
        return None

    ref = referer or (f"https://doi.org/{_clean_doi(doi)}" if _clean_doi(doi) else None)
    for cand in candidates[:_MAX_CANDIDATES_TO_TRY]:
        logger.debug(
            "web_pdf_discovery: 尝试候选 score=%.2f doi_match=%s pdfish=%s url=%s",
            cand.score,
            cand.doi_match,
            cand.pdfish,
            cand.url,
        )
        pdf = await fetch_pdf_simple(cand.url, referer=ref)
        if pdf is not None:
            logger.info("web_pdf_discovery: 直链命中 %s", cand.url)
            return pdf, cand.url

        # 非 PDF 形态的结果多半是落地页；复用 meta_adapter 读 citation_pdf_url。
        if not cand.pdfish:
            pdf, _, _ = await fetch_via_landing_page(cand.url)
            if pdf is not None:
                logger.info("web_pdf_discovery: landing 命中 %s", cand.url)
                return pdf, cand.url

    logger.debug("web_pdf_discovery: %d 个候选均未拿到有效 PDF", len(candidates))
    return None


async def _collect_candidates(
    doi: str | None,
    title: str | None,
    *,
    allow_preprint: bool,
) -> list[_WebCandidate]:
    doi_clean = _clean_doi(doi)
    title_clean = (title or "").strip()
    candidates: dict[str, _WebCandidate] = {}

    try:
        for query in _query_variants(doi_clean, title_clean):
            for item in await tavily_client.search_with_pool(
                query,
                limit=_MAX_RESULTS_PER_QUERY,
                search_depth="advanced",
            ):
                for url in _urls_from_result(item):
                    cand = _score_candidate(
                        url,
                        item,
                        doi=doi_clean,
                        title=title_clean,
                        allow_preprint=allow_preprint,
                    )
                    if cand is None:
                        continue
                    old = candidates.get(cand.url)
                    if old is None or _sort_key(cand) > _sort_key(old):
                        candidates[cand.url] = cand
    except TavilyQuotaExhausted:
        logger.warning("web_pdf_discovery: Tavily 号池额度耗尽，跳过网页 PDF 发现")
        return []
    except Exception as exc:  # noqa: BLE001
        # REASON: web discovery 是下载链增强项，搜索异常不能阻断机构代理/手工兜底。
        logger.warning("web_pdf_discovery: 搜索失败（%s），跳过", exc)
        return []

    out = sorted(candidates.values(), key=_sort_key, reverse=True)
    logger.debug("web_pdf_discovery: 收集到 %d 个相关候选", len(out))
    return out


def _query_variants(doi: str | None, title: str) -> list[str]:
    queries: list[str] = []
    if doi and title:
        queries.append(f'"{title}" "{doi}" pdf')
    if doi:
        queries.append(f'"{doi}" pdf')
    if _usable_title(title):
        queries.append(f'"{title}" filetype:pdf')
        queries.append(f'"{title}" full text pdf')

    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _urls_from_result(item: WebResult) -> list[str]:
    out = [item.url]
    text = f"{item.title} {item.content or ''}"
    for m in re.finditer(r"https?://[^\s<>()\"']+", text):
        out.append(m.group(0).rstrip(".,;:]}"))

    seen: set[str] = set()
    urls: list[str] = []
    for url in out:
        cleaned = (url or "").strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


def _score_candidate(
    url: str,
    item: WebResult,
    *,
    doi: str | None,
    title: str,
    allow_preprint: bool,
) -> _WebCandidate | None:
    if _is_blocked_url(url):
        return None
    if not allow_preprint and _is_preprint_url(url):
        return None

    doi_match = bool(doi and _contains_doi(doi, url, item.title, item.content or ""))
    # 网页摘要经常罗列多篇相关论文。只用搜索结果标题核验，不能让摘要中的目标标题
    # 把另一篇 PDF 误判为命中。
    title_score = _title_match_score(title, item.title)
    if not doi_match and title_score < _MATCH_THRESHOLD:
        return None

    score = max(1.0 if doi_match else 0.0, title_score)
    return _WebCandidate(
        url=url,
        title=item.title,
        abstract=item.content,
        score=score,
        doi_match=doi_match,
        pdfish=_looks_pdfish_url(url),
        tavily_score=item.score,
    )


def _sort_key(cand: _WebCandidate) -> tuple[int, int, float, float]:
    return (
        1 if cand.doi_match else 0,
        1 if cand.pdfish else 0,
        cand.score,
        cand.tavily_score or 0.0,
    )


def _clean_doi(doi: str | None) -> str | None:
    s = (doi or "").strip()
    if not s:
        return None
    s = unquote(s)
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return s.strip().rstrip(".").lower() or None


def _contains_doi(doi: str, *fields: str) -> bool:
    hay = unquote(" ".join(fields)).lower()
    return doi.lower() in hay


def _usable_title(title: str | None) -> bool:
    return len((title or "").strip()) >= _MIN_TITLE_LEN


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _significant_words(s: str) -> set[str]:
    return {w for w in _normalize(s).split() if len(w) > 2}


def _title_match_score(
    target_title: str,
    candidate_title: str,
) -> float:
    target = _significant_words(target_title)
    if not target:
        return 0.0
    hay = _significant_words(candidate_title)
    overlap = len(target & hay) / len(target)
    seq = SequenceMatcher(None, _normalize(target_title), _normalize(candidate_title)).ratio()
    return max(overlap, seq)


def _looks_pdfish_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        query = (parsed.query or "").lower()
    except Exception:
        return False
    return (
        path.endswith(".pdf")
        or path.endswith("/pdf")
        or "/pdf/" in path
        or "pdf" in query
    )


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def _is_preprint_url(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in _PREPRINT_HOSTS)


def _is_blocked_url(url: str) -> bool:
    low = url.lower()
    return any(marker in low for marker in _BLOCKED_HOST_MARKERS)
