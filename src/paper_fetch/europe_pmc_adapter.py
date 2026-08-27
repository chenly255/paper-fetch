"""europe_pmc_adapter：DOI → PMC ID → Europe PMC 全文 PDF（生物医学场景的关键兜底）。

为什么这一层对医学场景特别重要：
  NIH policy 要求 NIH 资助的论文 12 个月内必须进 PubMed Central。绝大多数生物医学
  论文（即使发在 Nature/Cell/Lancet 这种付费墙刊上）都有 PMC 作者手稿版本，免费、开放。
  Europe PMC 与 US PMC 同步数据但反爬更宽松。这是「付费墙论文拿到免费全文」的主力通道。

流程（2026-06-17 对齐飞书 agent 的成功路径重写）：
  1. 查 PMC ID：先用 Europe PMC 自家 REST API 按 DOI 查（生物医学最全、无限流），
     查不到再用 Semantic Scholar 的 externalIds.PubMedCentral 兜底。
  2. 预印本兜底：给的若是预印本 DOI（10.1101/... 等），在 PMC 里只查到 PPR 记录、没有
     可下载手稿 → 先解析出正式发表版 DOI（Crossref/bioRxiv/标题反查），再用正式版 DOI 查 PMC。
     （Slide-tags：bioRxiv 预印本 → 解析到 Nature 正式版 → PMC10764288 → 下到 29MB 作者手稿。）
  3. 下载：先试 Europe PMC 的历史 PDF 接口；若接口已下线，再从美国 PMC 文章页解析
     官方 PDF 链接。NCBI 的 PDF 端点可能返回工作量证明页面，此时复用下载链共享的
     Chromium 预算执行挑战后下载。

W8 网络军规：S2 / Europe PMC REST / 下载都走后端进程网络环境（直连，不显式设代理）。
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .proxy import async_client_for

from .cooldown_http import cooldown_get
from .oa_adapter import fetch_oa_pdf
from .robust_fetch import FetchBudget, fetch_pdf_simple, fetch_pdf_via_browser

logger = logging.getLogger(__name__)


def _doi_looks_preprint(doi: str | None) -> bool:
    """输入 DOI 是否像预印本（前缀启发式）——不像就不该做正式版解析（防误映射守门）。

    前缀表用 paper_search.preprint_resolve.PREPRINT_DOI_PREFIXES 单一定义（评审 m2 收敛），
    延迟 import 避免加载顺序问题（该模块零重依赖，import 成本可忽略）。
    """
    from .preprint_resolve import PREPRINT_DOI_PREFIXES

    return bool(doi) and doi.strip().lower().startswith(PREPRINT_DOI_PREFIXES)

# Europe PMC 自家检索 API（按 DOI 精确查，返回 pmcid / source / inEPMC）
_EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
# Semantic Scholar 兜底（externalIds.PubMedCentral）
_S2_API = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
# 下载接口（按可靠性排序）：①网页 render PDF 的真实终点 ②老的 ptpmcrender
_EPMC_PDF_URLS = (
    "https://europepmc.org/api/getPdf?pmcid=PMC{num}",
    "https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC{num}&blobtype=pdf",
)
_US_PMC_ARTICLE = "https://pmc.ncbi.nlm.nih.gov/articles/PMC{num}/"
_TIMEOUT_SEC = 15
OnStage = Callable[[str], Awaitable[None]] | None


async def fetch_via_europe_pmc(
    doi: str | None,
    *,
    title: str | None = None,
    budget: FetchBudget | None = None,
    on_stage: OnStage = None,
) -> bytes | None:
    """DOI → PMC ID → Europe PMC 下载 PDF；任何环节失败返 None。

    参数：
        doi   — 论文 DOI（如 10.1038/s41586-020-2649-2，或预印本 10.1101/...）
        title — 论文标题（可选）。给的是预印本 DOI、PMC 查不到时，用标题反查正式发表版
                （Crossref 标题反查需要它，slide-tags 这类预印本没登记正式版就靠这条）。

    返回：
        bytes — PDF 字节
        None  — doi/title 都空 / 查不到可下载的 PMC ID / 下载失败
    """
    if not (doi or "").strip() and not (title or "").strip():
        return None

    pmc_num = await _lookup_pmc_num(doi)

    # 没查到可下载 PMC 时，仅当输入 DOI 像预印本（10.1101/... 等前缀）才解析正式版再查。
    # PMC 查不到 ≠ 是预印本：Nature 等正式版 DOI 也常查不到，把它们送进标题反查会把
    # 同标题会议摘要误判成「正式发表版」（2026-08-21 DOI 误映射事故），preprint_resolve
    # 内部有同款守门，这里前置判断省掉无谓的网络往返。
    if not pmc_num and _doi_looks_preprint(doi):
        published = await _resolve_published(doi, title)
        if published:
            logger.info("europe_pmc_adapter: 预印本 %s → 正式版 %s，重查 PMC", doi, published)
            pmc_num = await _lookup_pmc_num(published)

    if not pmc_num:
        logger.debug("europe_pmc_adapter: doi=%s title=%s 查不到可下载 PMC ID", doi, (title or "")[:40])
        return None

    return await _download_pmc_pdf(
        pmc_num,
        budget=budget or FetchBudget(browser=1),
        on_stage=on_stage,
    )


async def _resolve_published(doi: str | None, title: str | None) -> str | None:
    """预印本 DOI → 正式发表版 DOI（复用 paper_search.preprint_resolve，避免重复实现）。"""
    try:
        from .preprint_resolve import resolve_published_doi
        return await resolve_published_doi(doi, title)
    except Exception as exc:
        # REASON: 正式版解析是兜底增强，失败只是这条 PMC 路走不通，不该炸整条下载链。
        logger.debug("europe_pmc_adapter: 正式版解析失败 doi=%s（%s）", doi, exc)
        return None


async def _lookup_pmc_num(doi: str | None) -> str | None:
    """查 DOI 对应的可下载 PMC 编号（纯数字，不带 PMC 前缀）；查不到返 None。

    先 Europe PMC REST（生物医学最全、无限流），再 Semantic Scholar 兜底。
    """
    doi = (doi or "").strip()
    if not doi:
        return None
    num = await _pmc_num_via_epmc_rest(doi)
    if num:
        return num
    return await _pmc_num_via_s2(doi)


async def _pmc_num_via_epmc_rest(doi: str) -> str | None:
    """Europe PMC REST 按 DOI 查 PMC 编号。仅当全文确在 EPMC（inEPMC=Y）且非预印本（source≠PPR）才认。"""
    try:
        async with async_client_for(_EPMC_SEARCH, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(
                client,
                _EPMC_SEARCH,
                params={"query": f"DOI:{doi}", "format": "json", "resultType": "lite", "pageSize": "5"},
            )
            if resp is None or resp.status_code != 200:
                return None
            results = (resp.json().get("resultList") or {}).get("result") or []
    except Exception as exc:
        logger.debug("europe_pmc_adapter: Europe PMC REST 查询失败 doi=%s（%s）", doi, exc)
        return None

    for r in results:
        pmcid = str(r.get("pmcid") or "").strip()
        # source=PPR 是预印本记录、没有可下载手稿；inEPMC=Y 才表示全文在 EPMC 可下
        if pmcid and r.get("source") != "PPR" and str(r.get("inEPMC") or "").upper() == "Y":
            return pmcid.upper().removeprefix("PMC")
    return None


async def _pmc_num_via_s2(doi: str) -> str | None:
    """Semantic Scholar externalIds.PubMedCentral 兜底；找不到 / 限流 / 404 返 None。"""
    try:
        async with async_client_for(_S2_API.format(doi=doi), follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(
                client, _S2_API.format(doi=doi), params={"fields": "externalIds"}
            )
            if resp is None:
                return None
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            ext_ids = (resp.json().get("externalIds") or {})
            pmc_id = ext_ids.get("PubMedCentral")
            if not pmc_id:
                return None
            return str(pmc_id).strip().upper().removeprefix("PMC")
    except httpx.HTTPStatusError as exc:
        logger.debug("europe_pmc_adapter: S2 HTTP 错误 %s，doi=%s", exc.response.status_code, doi)
        return None
    except httpx.RequestError as exc:
        logger.debug("europe_pmc_adapter: S2 请求错误（%s），doi=%s", exc, doi)
        return None
    except Exception as exc:
        # REASON: 下载链一段，未知异常降级 None 让上层走下一档。
        logger.warning("europe_pmc_adapter: S2 未知错误（%s），doi=%s", exc, doi, exc_info=True)
        return None


async def _download_pmc_pdf(
    pmc_num: str,
    *,
    budget: FetchBudget,
    on_stage: OnStage = None,
) -> bytes | None:
    """先试 Europe PMC 历史接口，再走美国 PMC 正式文章页。"""
    for tmpl in _EPMC_PDF_URLS:
        url = tmpl.format(num=pmc_num)
        pdf = await fetch_oa_pdf(url)
        if pdf is not None:
            logger.debug("europe_pmc_adapter: PMC%s 下载成功 ← %s", pmc_num, url)
            return pdf

    article_url = _US_PMC_ARTICLE.format(num=pmc_num)
    pdf_url = await _discover_us_pmc_pdf_url(article_url)
    if not pdf_url:
        # NCBI 偶尔会把文章页本身替换成 HTTP 200 的工作量证明页，静态 HTML 因而没有
        # PDF 链接。PMC 当前主文地址稳定为 pdf/main.pdf；仍需经过 %PDF 验真，猜错只会降级。
        pdf_url = urljoin(article_url, "pdf/main.pdf")
        logger.debug(
            "europe_pmc_adapter: 文章页未暴露 PDF 链接，尝试官方主文地址 %s",
            pdf_url,
        )

    # NCBI 目前会给部分 PDF 端点返回一个 JS 工作量证明页面。先走快速请求；只有拿到的
    # 不是 PDF 时才启 Chromium，且使用 download_pdf 全链共享预算，避免重复开浏览器。
    pdf = await fetch_pdf_simple(pdf_url, referer=article_url)
    if pdf is not None:
        return pdf
    if budget.browser <= 0:
        return None
    if on_stage is not None:
        await on_stage("browser")
    return await fetch_pdf_via_browser(pdf_url, referer=article_url, budget=budget)


async def _discover_us_pmc_pdf_url(article_url: str) -> str | None:
    """从美国 PMC 文章页解析官方 PDF 链接；网络或页面变化时返回 None。"""
    try:
        async with async_client_for(article_url, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(
                client,
                article_url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,*/*"},
            )
            if resp is None or resp.status_code != 200:
                return None
    except Exception as exc:
        logger.debug("europe_pmc_adapter: 美国 PMC 文章页读取失败 %s（%s）", article_url, exc)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if href and href.lower().split("?", 1)[0].endswith(".pdf"):
            return urljoin(str(resp.url), href)
    logger.debug("europe_pmc_adapter: 美国 PMC 文章页没有 PDF 链接 %s", article_url)
    return None
