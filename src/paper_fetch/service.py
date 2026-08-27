"""下载链编排主链（原 PaperPilot paper_download_service，迁移为 paper_fetch.service）。

对外暴露 download_pdf(doi, paper_url, oa_url, on_stage) → dict

主链按“确定性直链 -> 结构化开放源 -> 通用发现 -> 授权来源”逐步降级：
  ① preprint/direct/oa：预印本模板、论文 PDF 直链和搜索源携带的开放链接
  ② OpenAlex/Elsevier API：开放位置或用户已配置的官方接口授权
  ③ publisher/meta/Unpaywall/Crossref：出版商模板、论文页元数据和 DOI 全文登记
  ④ Europe PMC/美国 PMC：按 DOI 或标题查生物医学作者稿，并处理 NCBI 工作量证明
  ⑤ browser/web discovery：结构化来源失败后过合法 JS 挑战，或用 Tavily 发现候选再验 `%PDF-`
  ⑥ preprint 各兜底：正式版免费段/网页发现全失败后回到预印本——
    a. preprint_fallback：候选自带预印本 URL（2026-08-21 事故新增）
    b. preprint_doi_fallback：显式传入预印本 DOI 但已升级到正式版时，用**原始**预印本
       DOI 模板直下（2026-08-26 Cell/Open-ST 顶包事故新增：升级后 ①0 被跳过，正式版
       拿不到必须回落预印本，不能直接空手/进机构代理）
    c. preprint_discovery：按标题发现同研究开放预印本（2026-08-21 事故新增；只给正式版
       DOI 也要能找到预印本；预印本快且免费，代理慢且有额度）。
    预印本/免费源段被 429/403 挡下时同任务内自动换出口 IP 重试（多 IP 轮换，2026-08-23 定稿）。
  ⑦ library proxy：预印本也不可得时用机构账号兜底。**默认停用**（2026-08-23 定稿：
    library_proxy_enabled 默认 False + 无凭据即跳过，防慢通道空转；用户可在设置里手动开）。
  ⑧ Sci-Hub：默认关（见 README「合规边界」），
     开关（FetchConfig.scihub_enabled，默认关）+ 适配器在场双条件才生效

返回 schema：
  {
    "success": bool,
    "pdf_bytes": bytes | None,
    "source": str | None,
    "size_bytes": int,
    "error": "download_failed" | "size_limit_exceeded" | None,
    "tried_sources": list[str],  # 实际尝试过的源名
    "failure_detail": str | None,  # 全链失败时的细分分类（paywall_no_access / institutional_proxy_failed…）
    "message": str | None,        # 中文用户可见失败说明（成功交付预印本时为 notice）
    "requested_doi": str | None,  # preprint_discovery 交付时：用户要的正式版 DOI
    "delivered_doi": str | None,  # preprint_discovery 交付时：实际拿到的预印本 DOI
    "delivered_version": str | None,  # 交付版本（"preprint" = 非正式版）
    "content_url": str | None,  # 交付段的来源 URL（模板/入口；web discovery 为命中候选）
    "pdf_identity": str | None,  # verified / unverified（锚点是否真比对通过）
    "pdf_identity_reason": str | None,  # 核验结论 reason（doi_match/title_match/…；strict 段才有）
    "pdf_identity_detail": str | None,  # 核验细节（覆盖率、自报 DOI 等）
  }

设计原则：
- 每个 adapter 一个文件，纯函数签名，无状态共享（解耦）
- Sci-Hub 段默认关：适配器随仓分发（2026-08-27 Lily 拍板公开含之），存在才挂、不存在
  整段跳过——开关默认关，且没装适配器时开了也走空（见 extras.py 加载器）
- 每段成功后立即 size 校验，超限丢弃返 size_limit_exceeded（T-08-08）
- pdf_bytes 不进 LLM tool_result（由 paper_search_agent 转 cache，T-08-13）

W7/W8 网络军规：外部 fetch 走 17891 代理，由进程启动 env 控制，此处不硬写。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable

from .config import FetchConfig, get_config, reset_config, use_config
# DB 会话由宿主钩子自管（见 config.py 机构通道协议）
from .browser_fetch_adapter import fetch_via_browser_landing
from .crossref_adapter import fetch_via_crossref
from .domain_cooldown import capture_blocks, max_captured_retry_after
from .elsevier_api_adapter import (
    fetch_via_elsevier_api,
    is_elsevier_target,
)
from .europe_pmc_adapter import fetch_via_europe_pmc
from .meta_adapter import fetch_via_landing_page
from .oa_adapter import fetch_oa_pdf
from .openalex_adapter import probe_oa
from .pdf_identity import (
    IdentityVerdict,
    backup_extract_dois,
    verify_pdf_identity,
)
from .preprint_adapter import fetch_preprint_pdf
from .preprint_discovery import (
    crossref_meta_for_doi,
    discover_preprint,
)
from .publisher_direct_adapter import fetch_publisher_direct
from .robust_fetch import FetchBudget, fetch_pdf, is_free_site
from .extras import load_optional_adapter
from .unpaywall_adapter import fetch_via_unpaywall
from .web_pdf_discovery_adapter import (
    can_discover_pdf_via_web,
    discover_pdf_via_web,
)
from .preprint_resolve import (
    PREPRINT_DOI_PREFIXES as _PREPRINT_DOI_PREFIXES,
)
from . import proxy as _proxy_mod
from .proxy import async_client_for

logger = logging.getLogger(__name__)

# ⑧ Sci-Hub 入口：适配器随仓分发、默认关——第三方附加件也可经
# src/paper_fetch/ 或设 PAPER_FETCH_EXTRA_ADAPTERS 指向私有附加仓，加载器才返回入口；
# 否则为 None，该段即使开关开着也整段跳过。测试/宿主可直接对
# paper_fetch.service.fetch_via_scihub 赋值（或 patch）注入替身。
fetch_via_scihub = load_optional_adapter("scihub_adapter", "fetch_via_scihub")

# 下载总时间软预算（秒）：付费/慢站把所有段串行跑满可能超 100s，而 agent loop 总超时 120s
# 一到点会直接抛 TimeoutError 杀掉整个 loop、连 auth_required/landing_url 兜底信号都丢。
# 故每段开始前查这个软上限，超了就停止后续段、正常返回失败 dict（带兜底信号），别甩给上层超时。
# 默认总预算（文档值）：实际读 FetchConfig.total_budget_sec（PaperPilot 传 75）。
_TOTAL_BUDGET_SEC = 75

# 聚合/摘要站（非出版商落地页）：meta 段抓不到 PDF 时才值得用 doi.org 跳真出版商页再抓（meta_doi）。
# paper_url 已经是出版商落地页时，doi.org 会 302 回同一页，meta_doi 等于重复抓——据此跳过省一整轮。
_AGGREGATOR_HOSTS = (
    "pubmed",
    "ncbi.nlm.nih.gov",
    "semanticscholar.org",
    "europepmc.org",
    "scholar.google",
    "researchgate.net",
    "doi.org",
    "core.ac.uk",
    "base-search.net",
)

# 已知订阅制出版商域名：免费段全失败时即便没收到显式 401/403，也判定需机构访问，
# 让前端弹「校园网打开 / 设置机构账号」引导（Nature 等付费墙是 200+重定向，不会报 401）。
_SUBSCRIPTION_HOSTS = (
    "nature.com",
    "springer.com",
    "link.springer.com",
    "sciencedirect.com",
    "cell.com",
    "wiley.com",
    "onlinelibrary.wiley.com",
    "science.org",
    "academic.oup.com",
    "tandfonline.com",
    "pubs.acs.org",
    "pubs.rsc.org",
    "ieee.org",
    "ieeexplore.ieee.org",
    "pnas.org",
    "jamanetwork.com",
    "thelancet.com",
    "nejm.org",
    "bmj.com",
)

# 回调类型：每段开始前调一次，传入源名
OnStage = Callable[[str], Awaitable[None]] | None

# DOI 格式白名单（与 api/documents/fetch.py 的 _DOI_PATTERN 同字符集）：
# 归一化后必须整体匹配，乱码 DOI 在进下载链前就被短路。
_DOI_FORMAT_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+")

# 强制身份核验的来源段（2026-08-23 顶包事故新增）：这些段的 PDF URL 来自外部输入
# 或网页搜索（与目标论文无 DOI 级强绑定），历史上抓到过「引用了目标论文的开放获取
# 文章」顶包（题录全对、正文全错）。命中后必须过 pdf_identity 首页核验才放行。
# DOI 锚定段（openalex/crossref/publisher_direct 等 URL 由目标 DOI 查询/模板构造）
# 不在此列，但仍抽首页 DOI 记日志备查（见 _validate_and_return）。
_STRICT_IDENTITY_SOURCES = frozenset(
    {
        "direct",  # paper_url 形似 PDF 直链（外部传入，无锚定）
        "oa",  # 搜索源带的 oa_url（外部传入）
        "web_pdf_discovery",  # Tavily 网页发现（事故重灾区：综述顶原文、书目列表顶专著）
    }
)

# 身份核验「真比对通过」的 reason 集（2026-08-25 坏条目防固化）：只有 DOI/标题锚点
# 实际命中才算 verified；no_anchor（无锚点放行）/ no_text_unverifiable（扫描件放行）
# 是「没有依据、拒绝无意义」的放行，折算 unverified。落进结果字典的 pdf_identity
# 由任务层写进 Document.pdf_identity_status，查重侧只信 verified。
_VERIFIED_IDENTITY_REASONS = frozenset({"doi_match", "title_match"})


async def download_pdf(
    doi: str | None,
    paper_url: str | None,
    oa_url: str | None,
    *,
    title: str | None = None,
    on_stage: OnStage = None,
    user=None,  # noqa: ANN001  当前用户（不透明对象：宿主钩子按它取机构/Elsevier 凭证）
    config: FetchConfig | None = None,
) -> dict:
    """按模块说明中的合法来源优先顺序尝试，返回第一个通过 PDF 验真的结果。

    参数：
        doi       — 论文 DOI（Unpaywall / Europe PMC 需要；meta 层会尝试从 HTML 补抽）
        paper_url — 论文页 URL（preprint 模式匹配 + meta 抓取的入口）
        oa_url    — 开放访问 PDF 直链（搜索源带的，如 SS openAccessPdf）
        title     — 论文标题（可选）。europe_pmc 段给的是预印本 DOI 时，靠它反查正式发表版
                    （Slide-tags 这类预印本没在元数据里登记正式版，只能按标题反查）
        on_stage  — 可选回调，每段开始前调一次，传入阶段名；供前端 SSE 实时推进度
        user      — 宿主侧用户对象（不透明，只透传给 config 里的机构/凭据钩子）。
                    铁律（2026-08-18 database is locked 事故整改）不变：下载链全程
                    不持有任何 DB 事务，凭证读取由宿主钩子自开短事务完成。
        config    — 本次调用的 FetchConfig；None = 用进程级默认（env 构造）。
                    传入后整个链（含 scihub 开关等 adapter 内部读取）都看到它。

    返回：
        dict，schema 见模块文档
    """
    token = use_config(config) if config is not None else None
    try:
        with capture_blocks():
            return await _download_pdf_chain(
                doi,
                paper_url,
                oa_url,
                title=title,
                on_stage=on_stage,
                user=user,
            )
    finally:
        reset_config(token)


def _log_proxy_egress(doi: str | None, paper_url: str | None, oa_url: str | None) -> None:
    """每次下载开头记录出口状态（2026-08-23 事故 d 显式日志证据）。

    事故：代理池没配置时全部下载静默走直连（IP 固化）、无任何痕迹，事后排查只能猜。
    现在每次下载固定打一行 INFO：境外源走内嵌代理（mixed-port）还是直连，
    巡检/复盘时 grep 这行即可核实下载链真实出口。
    """
    from .proxy import proxy_enabled_for_download

    egress = "内嵌代理(mihomo)" if proxy_enabled_for_download() else "直连（代理池未启用或未运行）"
    logger.info(
        "download_pdf: 出口=%s doi=%s paper_url=%s oa_url=%s",
        egress,
        doi,
        paper_url,
        oa_url,
    )


async def _download_pdf_chain(
    doi: str | None,
    paper_url: str | None,
    oa_url: str | None,
    *,
    title: str | None = None,
    on_stage: OnStage = None,
    user=None,  # noqa: ANN001
) -> dict:
    """download_pdf 的实际降级链；由外层 capture_blocks 收集本轮限流。"""
    cfg = get_config()
    max_bytes = cfg.max_pdf_mb * 1024 * 1024
    tried: list[str] = []
    # 出口状态显式日志（事故 d）：本次下载境外源走代理还是直连，开头就写清
    _log_proxy_egress(doi, paper_url, oa_url)
    # DOI 归一化 + 格式校验（收口在此，/documents/fetch 与对话 download-pdf 两条入口同受益）：
    # 先剥 doi:/doi.org 前缀再验格式；给了 doi 但格式不合法 → 直接失败短路，
    # 不进后续下载链（乱码会空转 ~14s 还拼出坏 landing_url、空试 scihub）。
    from .text_match import normalize_doi

    doi_effective = normalize_doi(doi) if doi else None
    if doi and not (doi_effective and _DOI_FORMAT_RE.fullmatch(doi_effective)):
        logger.info("download_pdf: DOI 格式非法，直接短路：%r", doi)
        return _fail("invalid_doi", tried=["doi_check"])
    # 付费墙信号（meta 段抓 landing page 时填）：全段失败时回传给前端，触发「机构登录」通道 + 兜底 UI
    auth_required = False
    landing_url: str | None = None
    publisher: str | None = None
    # 同一篇最多允许在两个不同站点各试一次浏览器。容器仍由 BrowserSession 的全局
    # 信号量限制为单实例，避免并发撑爆内存；这里的第二次机会主要留给 Europe PMC，
    # 防止出版商挑战失败后把官方仓储也一起饿死。
    budget = FetchBudget(browser=2)
    web_pdf_discovery_tried = False
    # 顶包拒收记录（2026-08-23 顶包事故）：strict 段核验未通过时记该段名，
    # 全链失败时据此归类 wrong_paper，向用户说清「下到过 PDF 但不是这篇」。
    identity_rejected: str | None = None
    # 总时间软预算：超了就停止后续段、走兜底返回（防整体被 agent 120s 超时杀掉丢兜底信号）
    deadline = time.monotonic() + cfg.total_budget_sec
    # 版本规划：能确认有正式发表版时，正式版是主路；预印本/OA 仓库链接只做最终兜底。
    # 这同时覆盖两类真实场景：
    # 1) 标题搜索返回“正式版 DOI + bioRxiv URL”；
    # 2) 用户/外部 agent 只给了预印本 DOI/URL，但可解析到正式发表版 DOI。
    # 2026-08-26 事故修复（缺陷 4）：升级成功后 doi_effective 被正式版覆盖、①0 预印本
    # 模板直下被 `not published_doi` 跳过——**原始**预印本 DOI 这条确定性线索随之丢失，
    # 正式版下不到时整条链退化为「和正式版 DOI 完全相同的路径」，重试预印本 DOI 无法
    # 自救。先在此保存原始预印本 DOI（从 DOI 或预印本 URL 抽取，须在 doi_effective 被
    # 覆盖前），正式版主路全失败后由 ⑥a preprint_doi_fallback 用它回落直下。
    original_preprint_doi = (
        doi_effective
        if _is_preprint_doi(doi_effective)
        else _extract_preprint_doi_from_url(paper_url)
    )
    published_doi = await _resolve_published_doi_for_download(doi_effective, paper_url, title)
    if published_doi:
        logger.info("download_pdf: 预印本 DOI 解析到正式版 DOI=%s，优先下载正式版", published_doi)
        doi_effective = published_doi
    defer_preprint = _should_defer_free_fulltext(doi_effective, paper_url, oa_url)
    preprint_fallback_url = paper_url if defer_preprint and _is_preprint_url(paper_url) else None
    oa_fallback_url = oa_url if defer_preprint and _is_preprint_url(oa_url) else None
    if preprint_fallback_url:
        paper_url = None
    if oa_fallback_url:
        oa_url = None

    # ① preprint：URL 模式匹配，零开销
    # 预印本多 IP 轮换计数（2026-08-23 定稿）：成功换过的出口节点数，终态文案要向
    # 用户说明「已自动更换 N 个出口 IP 重试」。链内各预印本/免费源段共用一份。
    rotation_counter = {"count": 0}
    # ①0 preprint_doi（2026-08-23 审计修复 5）：只给预印本 DOI、没给 URL 时，直接用平台
    # 模板构造落地页（preprint_discovery._preprint_url_for_doi 同款）走 preprint 段——
    # 预印本的 PDF 几乎总在预印本站本身，省掉 openalex 等元数据 API 的白跑。
    # 仅当「没升级到正式版」时才短路（published_doi 有值 = 正式版主路已定，预印本交给
    # ⑥b preprint_discovery 按需兜底，升级语义不变）。
    preprint_doi_url: str | None = None
    if not paper_url and doi_effective and not published_doi:
        from .preprint_discovery import _preprint_url_for_doi

        preprint_doi_url = _preprint_url_for_doi(doi_effective)
    if preprint_doi_url:
        await _emit(on_stage, "preprint")
        tried.append("preprint")
        pdf = await _fetch_with_node_rotation(
            lambda: fetch_preprint_pdf(preprint_doi_url),
            preprint_doi_url,
            source="preprint",
            rotation_counter=rotation_counter,
        )
        if pdf is not None:
            return await _validate_and_return(
                pdf, "preprint", tried, max_bytes, doi=doi_effective, title=title,
                content_url=preprint_doi_url,
            )
        logger.debug("download_pdf: preprint 段（DOI 模板 %s）未命中，继续降级", preprint_doi_url)

    if paper_url:
        await _emit(on_stage, "preprint")
        tried.append("preprint")
        pdf = await _fetch_with_node_rotation(
            lambda: fetch_preprint_pdf(paper_url),
            paper_url,
            source="preprint",
            rotation_counter=rotation_counter,
        )
        if pdf is not None:
            return await _validate_and_return(
                pdf, "preprint", tried, max_bytes, doi=doi_effective, title=title,
                content_url=paper_url,
            )
        logger.debug("download_pdf: preprint 段未命中（%s），尝试 direct", paper_url)

    # ①b direct：paper_url 本身就是 PDF 直链（.pdf 结尾 / 含 /pdf/ 段）。
    # 中文期刊、机构仓库常把 PDF 直链当 source_url 给搜索源（如 Tavily）——这类站既不在
    # preprint/publisher 模板里，又会被 meta 段当 HTML landing page 抓、拿到 200+application/pdf
    # 响应后因「不是 HTML」整段丢弃，全链漏下（2026-06-20 中国微生态学杂志真实踩坑）。
    # 单独兜住：直接按直链下，magic bytes 严校验防 HTML 伪装，命中即省后续所有段。
    if paper_url and _looks_like_pdf_url(paper_url):
        await _emit(on_stage, "direct")
        tried.append("direct")
        pdf = await fetch_oa_pdf(paper_url)
        if pdf is not None:
            verdict = await _identity_gate(pdf, "direct", doi=doi_effective, title=title)
            if verdict is None:
                identity_rejected = identity_rejected or "direct"
            else:
                return await _validate_and_return(
                    pdf, "direct", tried, max_bytes, doi=doi_effective, title=title,
                    identity_verdict=verdict, content_url=paper_url,
                )
        logger.debug("download_pdf: direct 段（paper_url 形似 PDF）未命中，尝试 oa")

    # ② oa：搜索源带的 OA 直链
    if oa_url:
        await _emit(on_stage, "oa")
        tried.append("oa")
        pdf = await fetch_oa_pdf(oa_url)
        if pdf is not None:
            verdict = await _identity_gate(pdf, "oa", doi=doi_effective, title=title)
            if verdict is None:
                identity_rejected = identity_rejected or "oa"
            else:
                return await _validate_and_return(
                    pdf, "oa", tried, max_bytes, doi=doi_effective, title=title,
                    identity_verdict=verdict, content_url=oa_url,
                )
        logger.debug("download_pdf: oa 段失败，尝试 openalex 探测")

    # ②b openalex 早期探测（单次 API，快）：拿 OA 状态 + OA pdf 直链。
    # - 有 OA pdf 直链 → 立刻试下（多数 OA 论文这里秒命中）。
    # - 明确 is_oa=False → 标记 known_paywalled，跳过后面所有昂贵的浏览器/重复抓取，省 ~50 秒。
    known_paywalled = False
    if doi_effective:
        await _emit(on_stage, "openalex")
        tried.append("openalex")
        is_oa, oa_pdf_urls, doi_not_found = await probe_oa(doi_effective)
        # OpenAlex 明确 404（未收录）：用 doi.org handle API 复核——复核也不存在才短路，
        # 防 OpenAlex 收录滞后误杀新注册 DOI；复核请求失败按「未知」处理，行为退回现状。
        if doi_not_found and await _doi_definitely_missing(doi_effective):
            logger.info("download_pdf: DOI 经复核确认不存在，短路：%s", doi_effective)
            return _fail("doi_not_found", tried + ["doi_resolve"])
        for url in oa_pdf_urls:
            pdf = await fetch_oa_pdf(url)
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "openalex", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=url,
                )
        if is_oa is False:
            known_paywalled = True
            logger.debug("download_pdf: OpenAlex 明确 is_oa=False，短路跳过昂贵段")

    # ②d elsevier_api：Elsevier 家族（ScienceDirect/Cell/10.1016…）专用官方接口通道。
    # **故意放在 known_paywalled 短路之外**——闭源 Elsevier 正是它的用武之地：网页是 Cloudflare
    # 硬墙（publisher_direct/浏览器都过不去），而官方接口绕开网页直接拿全文 PDF。纯 HTTP、无浏览器、
    # 无封号风险。Key 取用户自填（无则全局兜底）；闭源全文授权与出口 IP 绑定，须后端在校园网/VPN 上跑。
    # 同时天然反映"当前网络是否在授权范围"（NOT_ENTITLED=不在），是智能路由判断校园网的依据之一。
    if (
        (doi_effective or paper_url)
        and is_elsevier_target(doi_effective, paper_url)
        and _time_left(deadline) > 0
    ):
        creds = await _resolve_elsevier_creds(user)
        if creds is not None:
            await _emit(on_stage, "elsevier_api")
            tried.append("elsevier_api")
            pdf = await fetch_via_elsevier_api(doi_effective, api_key=creds[0], inst_token=creds[1])
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "elsevier_api", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=_doi_entry_url(doi_effective),
                )
            logger.debug("download_pdf: elsevier_api 段未命中（无授权或非全文），继续后续段")

    # 以下为「OA 但前面没下到」的昂贵段——付费论文（known_paywalled）整段跳过。
    # 每段前查 _time_left：超总预算就停止后续段、走下面的兜底返回（不把超时甩给上层 agent loop）。
    if not known_paywalled:
        # ②c publisher_direct：按 DOI 前缀/论文页 URL 构造出版商 PDF 直链（Nature/Springer/Frontiers…）。
        # 补「OA 论文但有 JS 挑战、不在 Unpaywall 索引」的大缺口，配 curl_cffi + 浏览器兜底。
        if (doi_effective or paper_url) and _time_left(deadline) > 0:
            await _emit(on_stage, "publisher_direct")
            tried.append("publisher_direct")
            pdf = await fetch_publisher_direct(doi_effective, paper_url, budget=budget)
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "publisher_direct", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=_doi_entry_url(doi_effective),
                )
            logger.debug("download_pdf: publisher_direct 段失败，尝试 meta")

        # ③ meta：抓 landing page citation_pdf_url；副产物：补抽 DOI + 付费墙信号
        if paper_url and _time_left(deadline) > 0:
            await _emit(on_stage, "meta")
            tried.append("meta")
            pdf, meta_doi, landing_info = await fetch_via_landing_page(paper_url)
            auth_required, landing_url, publisher = _merge_landing(
                landing_info, auth_required, landing_url, publisher
            )
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "meta", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=paper_url,
                )
            if not doi_effective and meta_doi:
                doi_effective = meta_doi
                logger.debug("download_pdf: meta 段补抽到 DOI=%s 给下游用", meta_doi)
            logger.debug("download_pdf: meta 段失败，尝试 meta_doi")

        # ③b meta via doi.org：仅当 paper_url 是聚合/摘要站（PubMed 等）时才用 doi.org 跳真出版商页再抓。
        # paper_url 已经是出版商落地页时 doi.org 会 302 回同一页，meta 刚抓过 → 跳过省一整轮（避免重复抓取）。
        if (
            doi_effective
            and "doi.org" not in (paper_url or "")
            and (not paper_url or _is_aggregator(paper_url))
            and _time_left(deadline) > 0
        ):
            doi_url = f"https://doi.org/{doi_effective}"
            await _emit(on_stage, "meta_doi")
            tried.append("meta_doi")
            pdf, _, landing_info = await fetch_via_landing_page(doi_url)
            auth_required, landing_url, publisher = _merge_landing(
                landing_info, auth_required, landing_url, publisher
            )
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "meta", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=doi_url,
                )
            logger.debug("download_pdf: meta_doi 段也失败，尝试 unpaywall")

        # ④ unpaywall：DOI → OA URL
        unpaywall_email = (cfg.unpaywall_email or "").strip()
        if doi_effective and unpaywall_email and _time_left(deadline) > 0:
            await _emit(on_stage, "unpaywall")
            tried.append("unpaywall")
            pdf = await fetch_via_unpaywall(doi_effective, unpaywall_email)
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "unpaywall", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=_doi_entry_url(doi_effective),
                )
            logger.debug("download_pdf: unpaywall 段失败，尝试 crossref")
        elif doi_effective and not unpaywall_email:
            logger.debug("download_pdf: UNPAYWALL_EMAIL 未配置，跳过 unpaywall")

        # ④b crossref：DOI → 出版商按 TDM 规范登记的全文链接
        if doi_effective and _time_left(deadline) > 0:
            await _emit(on_stage, "crossref")
            tried.append("crossref")
            pdf = await fetch_via_crossref(doi_effective)
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "crossref", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=_doi_entry_url(doi_effective),
                )
            logger.debug("download_pdf: crossref 段失败，尝试 europe_pmc")

    # ⑤ europe_pmc：DOI → PMC ID → europepmc.org。**故意放在 known_paywalled 短路之外**——
    # 生物医学论文即便发在付费墙刊（Nature/Cell/Lancet），多数也有 NIH 强制存入的免费 PMC
    # 作者手稿，这正是付费论文拿到全文的主力通道；若被 is_oa=False 短路掉就白白漏下。
    # 位置仍在 crossref 之后、browser 之前（非付费墙路径的 tried_sources 顺序不变）。
    if doi_effective and _time_left(deadline) > 0:
        await _emit(on_stage, "europe_pmc")
        tried.append("europe_pmc")

        async def _on_europe_pmc_stage(stage: str) -> None:
            if stage not in tried:
                tried.append(stage)
            await _emit(on_stage, stage)

        pdf = await fetch_via_europe_pmc(
            doi_effective,
            title=title,
            budget=budget,
            on_stage=_on_europe_pmc_stage,
        )
        if pdf is not None:
            return await _validate_and_return(
                pdf, "europe_pmc", tried, max_bytes, doi=doi_effective, title=title,
                content_url=_doi_entry_url(doi_effective),
            )
        logger.debug("download_pdf: europe_pmc 段也失败，尝试 browser 兜底")

    if not known_paywalled:
        # ⑤b browser：通用无头浏览器兜底——打开 landing page 过 JS 挑战、活 DOM 读 citation_pdf_url 再下。
        # 覆盖「OA 但有反爬、没出版商模板」的论文（含 biorxiv 这类免费但被 Cloudflare 挡的预印本）。
        # ★不挂 _should_try_institutional 守门——那个守门本意是「免费站别浪费学校账号」（守下面的
        # carsi 段），不该挡通用浏览器兜底。biorxiv 自 2025-12 加 Cloudflare 后，httpx/curl_cffi 都
        # 拿不到 PDF，唯一活路就是这层真实 Chromium 过 JS 挑战，之前被守门误锁根本走不到。
        browser_target = landing_url or paper_url
        if (
            browser_target
            and budget.browser > 0
            and _time_left(deadline) > 12
            and not known_paywalled  # OpenAlex 已明确付费：浏览器过不了付费墙，跳过省时直奔机构通道
        ):
            await _emit(on_stage, "browser")
            tried.append("browser")
            pdf = await fetch_via_browser_landing(browser_target, budget=budget)
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "browser", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=browser_target,
                )
            logger.debug("download_pdf: browser 兜底也失败，尝试图书馆代理")

        # ⑤c web_pdf_discovery：结构化官方源都没给可用 PDF 时，用 Tavily 找候选 URL，再由
        # 下载器验 %PDF-。默认不接受预印本站，避免有正式版时过早降级到 preprint。
        if (
            can_discover_pdf_via_web(doi_effective, title)
            and _time_left(deadline) > 10
            and not known_paywalled  # OpenAlex 已明确付费：网页发现也找不到，跳过省时直奔机构通道
        ):
            await _emit(on_stage, "web_pdf_discovery")
            tried.append("web_pdf_discovery")
            web_pdf_discovery_tried = True
            got = await discover_pdf_via_web(
                doi_effective,
                title,
                referer=landing_url or paper_url,
                allow_preprint=False,
            )
            if got is not None:
                pdf, hit_url = got
                verdict = await _identity_gate(
                    pdf, "web_pdf_discovery", doi=doi_effective, title=title
                )
                if verdict is None:
                    identity_rejected = identity_rejected or "web_pdf_discovery"
                else:
                    return await _validate_and_return(
                        pdf, "web_pdf_discovery", tried, max_bytes, doi=doi_effective,
                        title=title, identity_verdict=verdict, content_url=hit_url,
                    )
            logger.debug("download_pdf: web_pdf_discovery 未命中，尝试图书馆代理")

        if _time_left(deadline) <= 0:
            logger.info("download_pdf: 命中总时间软预算 %.0fs，停止后续段走兜底", cfg.total_budget_sec)

    # 需机构访问判定：① OpenAlex 明确付费（known_paywalled）② 落地页/论文页属订阅站。
    # 任一成立即标 auth_required（Nature 等付费墙是 200+重定向不报 401，靠这两条兜住），
    # 让图书馆代理 + 前端「校园网打开 / 设置机构账号」引导能触发。
    # ★免费站（biorxiv 等）豁免：它的 403 是反爬不是付费墙，标 auth_required 会误导前端弹付费墙兜底。
    doi_landing = f"https://doi.org/{doi_effective}" if doi_effective else None
    subscription_target_url = _subscription_target_url(
        doi=doi_effective,
        doi_landing=doi_landing,
        paper_url=paper_url,
        landing_url=landing_url,
        known_paywalled=known_paywalled,
    )
    target_url = subscription_target_url or landing_url or paper_url or doi_landing
    if (
        not auth_required
        and (known_paywalled or subscription_target_url is not None)
        and not is_free_site(target_url)
    ):
        auth_required = True
        landing_url = subscription_target_url or landing_url or paper_url or doi_landing
        if not publisher:
            publisher = _host_of(landing_url)
        target_url = landing_url
        logger.debug(
            "download_pdf: 判定需机构访问（known_paywalled=%s, url=%s）",
            known_paywalled,
            target_url,
        )

    # ⑥ preprint_fallback：候选自带预印本 URL 的场景（搜索结果「正式版 DOI + bioRxiv URL」），
    # 正式版免费段失败后回到预印本。排在 preprint_discovery 之前（评审 m5）：这里 URL 是本地
    # 已知的确定直链（零外部 API），外部标题发现是搜索语义——确定资源优先于再搜索。
    # source 仍记为 preprint，tried_sources 用 preprint_fallback 表达它是降级兜底。
    # 查 75s 软预算（评审 m6 恢复）：本段只发普通下载请求，无需豁免；对话路径超预算就停。
    if defer_preprint and preprint_fallback_url and _time_left(deadline) > 0:
        await _emit(on_stage, "preprint_fallback")
        tried.append("preprint_fallback")

        async def _fallback_fetch() -> bytes | None:
            pdf = await fetch_preprint_pdf(preprint_fallback_url)
            if pdf is None and oa_fallback_url:
                pdf = await fetch_oa_pdf(oa_fallback_url)
            return pdf

        pdf = await _fetch_with_node_rotation(
            _fallback_fetch,
            preprint_fallback_url,
            source="preprint_fallback",
            rotation_counter=rotation_counter,
        )
        if pdf is not None:
            return await _validate_and_return(
                pdf, "preprint", tried, max_bytes, doi=doi_effective, title=title,
                content_url=preprint_fallback_url,
            )
        logger.debug(
            "download_pdf: preprint_fallback 段未命中（paper_url=%s, oa_url=%s）",
            preprint_fallback_url,
            oa_fallback_url,
        )

    # ⑥a preprint_doi_fallback（2026-08-26 Cell/Open-ST 顶包事故复盘新增）：显式传入
    # 预印本 DOI/URL 且已升级到正式版时，①0 模板直下被 `not published_doi` 跳过；
    # 老板拍板的链路语义是「先找正式版 → 正式版所有合法路径都下不到 → 退而下载同研究
    # 预印本」，所以正式版主路（含 web_pdf_discovery）全失败后，这里用**原始**预印本
    # DOI 构造模板 URL 直下一次。位置权衡：
    # - 放 ⑤c/⑥d web_pdf_discovery 之后：那两段 allow_preprint=False、只找正式版
    #   渠道，属正式版主路的延伸，不能被预印本抢跑；
    # - 放 ⑥ preprint_fallback 之后：候选自带预印本 URL 时（preprint_fallback_url 非空）
    #   原始 URL 更保真、⑥ 已试过，本段不重复跑；
    # - 放 ⑥b preprint_discovery 之前：本段是本地模板构造的确定性直链（零外部 API），
    #   优先于按标题搜索的发现语义；也先于 ⑥c library_proxy——预印本免费快，可得就
    #   不动学校账号（与 ⑥/⑥b 的产品语义一致）。
    # 不查 75s 软预算（同 ⑥b）：预印本模板直下是单次 HTTP 请求、adapter 自带超时；
    # 且这是用户显式给的 DOI，优先级高于软预算——事故里正是预算耗尽 + 段序缺失导致
    # 预印本这条路永远走不到，重试也无法自救。
    if original_preprint_doi and published_doi and not preprint_fallback_url:
        from .preprint_discovery import _preprint_url_for_doi

        preprint_fallback_template_url = _preprint_url_for_doi(original_preprint_doi)
        if preprint_fallback_template_url:
            await _emit(on_stage, "preprint_doi_fallback")
            tried.append("preprint_doi_fallback")
            pdf = await _fetch_with_node_rotation(
                lambda: fetch_preprint_pdf(preprint_fallback_template_url),
                preprint_fallback_template_url,
                source="preprint_doi_fallback",
                rotation_counter=rotation_counter,
            )
            if pdf is not None:
                # 交付语义对齐 ⑥b：用户要的（可能是）正式版、拿到的是预印本——
                # requested/delivered_doi 与 delivered_version 让上层通知/查重正确识别。
                # 核验锚点用原始预印本 DOI（PDF 首页印的就是它，doi_match 最准）。
                return await _validate_and_return(
                    pdf,
                    "preprint",
                    tried,
                    max_bytes,
                    doi=original_preprint_doi,
                    title=title,
                    delivered_version="preprint",
                    requested_doi=doi_effective,
                    delivered_doi=original_preprint_doi,
                    notice=_PREPRINT_DELIVERY_NOTICE,
                    content_url=preprint_fallback_template_url,
                )
            logger.debug(
                "download_pdf: preprint_doi_fallback 段未命中（模板 %s），继续降级",
                preprint_fallback_template_url,
            )

    # ⑥b preprint_discovery：候选没带预印本 URL（只给正式版 DOI，2026-08-21 事故场景）时，
    # 按标题发现同研究的开放预印本。产品拍板的降级顺序：先找原文 → 找预印本（快、免费）→
    # 机构代理最后兜底（代理实测 ~17KB/s 大 PDF 要 20 分钟，且有防封号额度）。
    # 触发条件：有 DOI + 付费信号（known_paywalled 或 auth_required）+ 标题可用——
    # 标题为空但 DOI 存在时先查一次 Crossref 补标题（评审 M4：只给 DOI 是 fetch 的高频输入，
    # 本次事故就是；补不到静默跳过本段）。发现的 DOI 与输入相同时无降级意义，内部自守。
    # 与 library_proxy 段一样不查 75s 软预算：发现 API（Europe PMC/Crossref/Tavily）各自带
    # 单请求超时，且此段位于最慢的机构代理段之前，通常几秒内出结果。
    library_proxy_reason: str | None = None
    if doi_effective and (known_paywalled or auth_required):
        discovery_title = (title or "").strip()
        discovery_authors: list[str] = []
        if not discovery_title:
            discovery_title, discovery_authors = await crossref_meta_for_doi(doi_effective)
            discovery_title = (discovery_title or "").strip()
            if discovery_title:
                logger.debug(
                    "download_pdf: 只给 DOI，Crossref 补到标题 %.60s（作者 %d 位）供预印本发现",
                    discovery_title,
                    len(discovery_authors),
                )
        elif doi_effective:
            # 标题已有也补一次作者：作者团队是预印本验收的第二道闸门（防撞标题的别家论文），
            # 参照作者缺失时闸门自动退回纯标题验收，不降召回（详见 preprint_discovery docstring）。
            _, discovery_authors = await crossref_meta_for_doi(doi_effective)
        if discovery_title:
            await _emit(on_stage, "preprint_discovery")
            tried.append("preprint_discovery")
            candidate = await discover_preprint(
                doi_effective,
                discovery_title,
                discovery_authors or None,
            )
            if candidate is not None:
                pdf = await _fetch_with_node_rotation(
                    lambda: _download_preprint_candidate(candidate, budget=budget),
                    candidate.get("url") or f"https://doi.org/{candidate.get('doi') or ''}",
                    source="preprint_discovery",
                    rotation_counter=rotation_counter,
                )
                if pdf is not None:
                    return await _validate_and_return(
                        pdf,
                        "preprint_discovery",
                        tried,
                        max_bytes,
                        doi=doi_effective,
                        title=title or discovery_title,
                        delivered_version="preprint",
                        requested_doi=doi_effective,
                        delivered_doi=candidate.get("doi"),
                        notice=_PREPRINT_DELIVERY_NOTICE,
                        content_url=candidate.get("url") or _doi_entry_url(candidate.get("doi")),
                    )
                logger.debug(
                    "download_pdf: preprint_discovery 找到候选 %s 但下载失败，继续降级",
                    candidate.get("doi") or candidate.get("url"),
                )

    # ⑥c library_proxy：前面全失败 + 检到付费墙信号 + 用户配了机构账号时，才走图书馆代理通道。
    # 只有 channel='institutional'（传了 user）才会到这——避免对每篇 OA 论文浪费学校账号。
    # 该校没配图书馆代理（如非复旦等少数有端口代理的学校）channel 内部直接返 None。
    # 通道内部自开短事务读凭证/记防封号状态（见 library_proxy_channel），下载链不传 session。
    # ★不受 75s 软预算限制（现状保持）：它是付费链路的最后几段之一（后面只剩 scihub），
    # 而代理下大 PDF 本来就要十几分钟；adapter 自带 30 分钟硬上限 + 断点续传。fetch API 层
    # 是后台任务（await_download 超时返 downloading 后任务继续跑），不会掐死调用方。
    # 2026-08-23 产品定稿：机构代理默认停用。入口守门（_library_proxy_gate）三查——
    # 用户开关关 / 没配任何机构凭据 / 读取失败——任一命中整段跳过（tried_sources 不记，
    # 不空转 30 分钟慢通道），终态文案用「机构代理通道已关闭」版本。
    proxy_disabled_by_user = False
    if user is not None and auth_required and target_url and _should_try_institutional(target_url):
        proxy_enabled, has_credential = await _library_proxy_gate(user)
        if not proxy_enabled:
            proxy_disabled_by_user = True
            logger.info("download_pdf: 机构代理开关关闭（用户偏好），跳过 library_proxy 段")
        elif not has_credential:
            # 开关开着但一个凭据都没有（2026-08-23 复旦账号已删，凭据表为空）：跳过防空转。
            # 概念上仍是「通道没跑」，归 paywall_no_access（_PROXY_NEVER_RAN_REASONS 的
            # no_credential 同义），文案走「未配置账号」版本而非「已关闭」版本。
            logger.info("download_pdf: 未配置任何机构凭据，跳过 library_proxy 段（防空转）")
        else:
            await _emit(on_stage, "library_proxy")
            tried.append("library_proxy")
            pdf, library_proxy_reason = await _try_library_proxy(
                user=user, doi=doi_effective, landing_url=target_url, title=None
            )
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "library_proxy", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=target_url,
                )
            logger.debug("download_pdf: 图书馆代理也失败（reason=%s）", library_proxy_reason)

    # ⑥d web_pdf_discovery（机构链路后）：OpenAlex 已明确付费时，不在机构代理前跑网页发现。
    # 这里再补一轮非预印本候选，覆盖机构未订阅/代理失败但作者稿或出版社备用域可用的情况。
    if (
        not web_pdf_discovery_tried
        and can_discover_pdf_via_web(doi_effective, title)
        and _time_left(deadline) > 10
    ):
        await _emit(on_stage, "web_pdf_discovery")
        tried.append("web_pdf_discovery")
        web_pdf_discovery_tried = True
        got = await discover_pdf_via_web(
            doi_effective,
            title,
            referer=target_url,
            allow_preprint=False,
        )
        if got is not None:
            pdf, hit_url = got
            verdict = await _identity_gate(pdf, "web_pdf_discovery", doi=doi_effective, title=title)
            if verdict is None:
                identity_rejected = identity_rejected or "web_pdf_discovery"
            else:
                return await _validate_and_return(
                    pdf, "web_pdf_discovery", tried, max_bytes, doi=doi_effective, title=title,
                    identity_verdict=verdict, content_url=hit_url,
                )
        logger.debug("download_pdf: 机构链路后的 web_pdf_discovery 也未命中")

    # ⑦ scihub：默认关。双条件生效：开关开 + 适配器在场；
    # 开关开了但适配器缺席（未装/加载失败）时只打一行提示并跳过——不报错、不 tried、不空转。
    # 故意放在所有合法通道（含图书馆代理）之后、known_paywalled 短路之外——付费墙论文
    # 正是它的用武之地，只需 DOI。
    # ★不受 75s 软预算限制（2026-08-23 审计修复）：前面的 library_proxy 段为传大文件本来
    # 就故意豁免预算（可跑十几分钟），走到这里时 deadline 必已耗尽——若查预算，开了
    # 开关也永远轮不到 scihub。scihub adapter 自带单镜像超时（FetchConfig.scihub_timeout_sec），
    # 不会无限拖。顺序上它仍是最后一段（合法源优先），只是不再被预算挡在门外。
    if cfg.scihub_enabled and doi_effective:
        if fetch_via_scihub is None:
            logger.info(
                "download_pdf: scihub 开关已开但适配器未安装（装法见 README"
                "「合规边界」节），跳过该段"
            )
        else:
            await _emit(on_stage, "scihub")
            tried.append("scihub")
            pdf = await fetch_via_scihub(doi_effective)
            if pdf is not None:
                return await _validate_and_return(
                    pdf, "scihub", tried, max_bytes, doi=doi_effective, title=title,
                    content_url=_doi_entry_url(doi_effective),
                )
            logger.debug("download_pdf: scihub 兜底也失败")

    logger.info(
        "download_pdf: 全部下载段失败，tried_sources=%s，auth_required=%s，proxy_reason=%s，"
        "identity_rejected=%s，proxy_disabled_by_user=%s，preprint_rotations=%d",
        tried,
        auth_required,
        library_proxy_reason,
        identity_rejected,
        proxy_disabled_by_user,
        rotation_counter["count"],
    )
    failure_detail, message = _classify_failure(
        tried=tried,
        auth_required=auth_required,
        library_proxy_reason=library_proxy_reason,
        identity_rejected=identity_rejected,
        landing_url=landing_url,
        proxy_disabled_by_user=proxy_disabled_by_user,
        preprint_rotation_used=rotation_counter["count"],
    )
    return _fail(
        "download_failed",
        tried,
        auth_required=auth_required,
        landing_url=landing_url,
        publisher=publisher,
        failure_detail=failure_detail,
        message=message,
    )


# 「机构通道根本没跑」的 reason 集合（channel/adapter 各守门点返回）：这些不是传输失败，
# 把它们归进 institutional_proxy_failed 会让用户被通知「已通过学校代理尝试但中断」——
# 误导（评审 M1）。本质仍是付费墙无可用通道，归 paywall_no_access 并引导配置账号。
_PROXY_NEVER_RAN_REASONS = frozenset(
    {
        "no_credential",  # 用户/课题组都没配机构账号
        "no_proxy",  # 该校没配图书馆代理（非复旦等少数有端口代理的学校）
        "credential_unavailable",  # 有账号但冷却中/日配额满
        "channel_unavailable",  # 通道模块缺失（可选增强未就绪）
    }
)

# 预印本多 IP 轮换：同一次下载任务内，预印本/免费源段被 429/403 挡下时最多动用的
# 出口节点数（含初始出口）。每个节点只打一次（失败史 + tried_nodes 排除），
# 换的是出口 IP 不是重复打同一 IP，不违反「429 不硬刷」（2026-08-23 产品定稿）。
_PREPRINT_ROTATION_MAX_NODES = 3

# 限流终态的统一用户文案（2026-08-23 二审收敛：全后端同源单一定义）。
# 使用方：本模块 _fail、document_download_task_service（通知兜底/downloading 提示）、
# api/documents/fetch.py 的 message 兜底——四处必须说同一句话，改文案只改这里。
RATE_LIMITED_MESSAGE = (
    "被来源限流（429），已停止自动重试。请稍后手动重试；若已配置代理轮换，会先切换节点再试。"
)


def _paywall_terminal_message(headline: str, landing_url: str | None) -> str:
    """付费墙终态的统一三段式 message（2026-08-23 定稿，通知与 MCP 同一条）。

    三段：①headline（各分支的「为什么没拿到」）；②论文页链接；③手动下载指引。
    通知（paper_fetch_failed）、fetch API / MCP fetch_paper 的 message 都用它，
    MCP 调用方可原样转述给用户。
    """
    parts = [headline]
    if landing_url:
        parts.append(f"论文页：{landing_url}")
    parts.append("你可以用学校网络打开上面的论文页手动下载 PDF，然后在网页端「我的文库 → 上传」把它传进文库。")
    return "\n".join(parts)


def _classify_failure(
    *,
    tried: list[str],
    auth_required: bool,
    library_proxy_reason: str | None,
    identity_rejected: str | None = None,
    landing_url: str | None = None,
    proxy_disabled_by_user: bool = False,
    preprint_rotation_used: int = 0,
) -> tuple[str | None, str | None]:
    """全链失败时的失败分类（failure_detail）+ 中文用户可见 message。

    为什么分这一层（2026-08-21 事故整改）：机构代理「HTTP/2 流中途崩断」曾被笼统报成
    auth_required=True（付费墙），用户和运维都被误导。付费墙判定（订阅站/OpenAlex is_oa=False）
    与「机构通道尝试后失败」是两回事，返回里要区分：
    - institutional_proxy_failed：机构通道真的跑了但传输/落地失败（auth_required 语义保留，
      前端机构引导仍可用，但 message 说清是「试过了但失败」而非「没权限」）。
    - paywall_no_access：确认付费且机构通道没跑（无凭证/该校无代理/冷却——通道自身就绪性
      问题，不是传输失败），或跑了但确认无订阅。
    - wrong_paper（2026-08-23 顶包事故）：无付费信号、但 strict 段下到过「与目标论文
      不符」的 PDF 被身份核验拒收——要向用户说清不是没找到，是找到的都不是这篇。
    - None：无付费信号的普通失败。

    2026-08-23 定稿：所有付费墙终态统一三段式（没拿到→论文页→手动上传指引，见
    _paywall_terminal_message）。「机构代理通道已关闭」（用户开关）与「已尝试但失败」
    严格区分；preprint_discovery 跑过没找到时明确说「也未找到公开预印本」。
    """
    preprint_tried = "preprint_discovery" in tried
    channels = "自动下载渠道（含公开预印本搜索）" if preprint_tried else "自动下载渠道"
    rotation_note = (
        f"（已自动更换 {preprint_rotation_used} 个出口 IP 重试仍未成功）"
        if preprint_rotation_used > 0
        else ""
    )
    preprint_note = "，也未找到公开预印本" if preprint_tried else ""

    proxy_tried = "library_proxy" in tried
    if proxy_disabled_by_user and auth_required and not proxy_tried:
        # 用户关了机构代理开关：说清「通道已关闭、未尝试」，与「尝试但失败」区分。
        return (
            "paywall_no_access",
            _paywall_terminal_message(
                f"这篇论文有付费墙，{channels}都没能拿到 PDF{rotation_note}"
                f"{preprint_note}；机构代理通道已按你的设置关闭，本次未尝试。",
                landing_url,
            ),
        )
    if proxy_tried and library_proxy_reason:
        if library_proxy_reason in _PROXY_NEVER_RAN_REASONS:
            # 通道没跑过：不能说「已尝试后失败」，本质是付费墙且机构通道不可用
            return (
                "paywall_no_access",
                _paywall_terminal_message(
                    f"这篇论文有付费墙，{channels}都没能拿到 PDF{rotation_note}"
                    f"{preprint_note}；机构通道当前不可用（未配置账号或额度受限）。",
                    landing_url,
                ),
            )
        if library_proxy_reason == "institutional_flow_loop":
            # 机构登录 302 循环（2026-08-23 nature.com 事故）：authorize→transit→
            # cookies_not_supported 打转，快速中止而非等 30 分钟整体超时。
            return (
                "institutional_proxy_failed",
                _paywall_terminal_message(
                    f"这篇论文有付费墙，{channels}都没能拿到 PDF{rotation_note}"
                    f"{preprint_note}；已尝试学校图书馆代理，但机构登录陷入重定向循环"
                    "（图书馆代理与该出版商的登录流程可能不兼容），已快速中止。",
                    landing_url,
                ),
            )
        if library_proxy_reason != "paywall_no_subscription":
            return (
                "institutional_proxy_failed",
                _paywall_terminal_message(
                    f"这篇论文有付费墙，{channels}都没能拿到 PDF{rotation_note}"
                    f"{preprint_note}；已尝试学校图书馆代理，但传输中断或失败"
                    "（可稍后重试；大文件经代理较慢）。",
                    landing_url,
                ),
            )
        return (
            "paywall_no_access",
            _paywall_terminal_message(
                f"这篇论文有付费墙，{channels}都没能拿到 PDF{rotation_note}"
                f"{preprint_note}；已通过学校代理确认该刊不在机构订阅范围。",
                landing_url,
            ),
        )
    if auth_required:
        headline = f"这篇论文有付费墙，{channels}都没能拿到 PDF{rotation_note}{preprint_note}"
        if identity_rejected:
            headline += "（网页来源曾返回与目标论文不符的文件，已拒收入库）"
        return "paywall_no_access", _paywall_terminal_message(headline, landing_url)
    if identity_rejected:
        return (
            "wrong_paper",
            "网页来源返回了与目标论文（题录）不符的 PDF，已拒绝入库、未产生错误条目。"
            "请核对标题/DOI 后重试，或改用 DOI 直接获取。",
        )
    return None, None


async def _download_preprint_candidate(candidate: dict, *, budget: FetchBudget) -> bytes | None:
    """下载 preprint_discovery 找到的候选：先 preprint_adapter 模板直链，失败再浏览器兜底。

    biorxiv 自 2025-12 有 Cloudflare 挑战，httpx/curl_cffi 可能拿不到——复用链里共享的
    FetchBudget 走 Chromium 兜底（与 europe_pmc 段共用预算，不额外多开浏览器）。
    """
    url = candidate.get("url")
    if url:
        pdf = await fetch_preprint_pdf(url)
        if pdf is not None:
            return pdf
        pdf = await fetch_pdf(url, budget=budget)
        if pdf is not None:
            return pdf
    # URL 不可构造（非 bioRxiv 平台且 Tavily 没给 URL）→ 只剩 DOI，交给 doi.org 跳转碰运气
    doi = candidate.get("doi")
    if doi:
        pdf = await fetch_pdf(f"https://doi.org/{doi}", budget=budget)
        if pdf is not None:
            return pdf
    return None


async def _emit(on_stage: OnStage, stage: str) -> None:
    if on_stage:
        await on_stage(stage)


async def _doi_definitely_missing(doi: str) -> bool:
    """doi.org Handle API 复核 DOI 是否存在：responseCode 100 = 确认不存在；
    注意该接口对不存在的 handle 同时返回 HTTP 404 + responseCode 100 的 JSON 体（2026-08-18 实测），
    所以只认 responseCode、不看 HTTP 状态码；网络异常/解析失败一律按「可能存在」处理，宁多试几段不误杀。"""
    try:
        async with async_client_for(f"https://doi.org/api/handles/{doi}", timeout=8.0) as client:
            resp = await client.get(f"https://doi.org/api/handles/{doi}")
            return resp.json().get("responseCode") == 100
    except Exception:
        return False


# 免费站（预印本/OA 仓库），机构登录通道跳过它们——既然是免费的，登学校账号纯浪费。
# 标记定义已移到 robust_fetch.FREE_SITE_MARKERS（meta_adapter 也要用，避免循环引用 + DRY），
# 这里通过 is_free_site 引用。


def _time_left(deadline: float) -> float:
    """距总时间软预算还剩多少秒（<=0 表示超预算，应停止后续段）。"""
    return deadline - time.monotonic()


def _doi_entry_url(doi: str | None) -> str | None:
    """DOI 锚定段的 content_url 定位入口（2026-08-26 事故可观测性）。

    这些段（elsevier_api/publisher_direct/unpaywall/crossref/europe_pmc/scihub）的真实
    PDF URL 在 adapter 内部按 DOI 模板/查询构造、不外传，这里记 doi.org 跳转入口——
    与目标 DOI 一一对应，排查时足以定位论文与来源；URL 真正不确定的段（web discovery、
    预印本模板、外部 URL）则记录真实命中/入口 URL。
    """
    return f"https://doi.org/{doi}" if doi else None


def _looks_like_pdf_url(url: str | None) -> bool:
    """paper_url 路径是否直指 PDF 文件（.pdf 结尾或含 /pdf/ 段）——决定是否走 direct 直下段。

    只看路径不看 query，避免把 `?ref=foo.pdf` 这类误判；真正下不下得到由 magic bytes 兜底。
    """
    if not url:
        return False
    try:
        from urllib.parse import urlparse

        path = (urlparse(url).path or "").lower()
    except Exception:
        return False
    return path.endswith(".pdf") or "/pdf/" in path


def _is_aggregator(url: str | None) -> bool:
    """URL 是否属聚合/摘要站（PubMed/S2/Europe PMC 等，非出版商落地页）。"""
    if not url:
        return False
    low = url.lower()
    return any(h in low for h in _AGGREGATOR_HOSTS)


def _should_try_institutional(url: str | None) -> bool:
    """免费站（arXiv/bioRxiv/PMC 等）不走机构登录，省学校账号。"""
    return not is_free_site(url)


def _should_defer_free_fulltext(doi: str | None, paper_url: str | None, oa_url: str | None) -> bool:
    """正式订阅 DOI + 预印本全文 URL 时，预印本延后到机构/官方链路之后。

    这类候选通常来自标题搜索或预印本解析：元数据已经指向正式发表版，URL/oa_url
    却仍是 bioRxiv/medRxiv 等预印本。对用户来说首选应是正式版；预印本仍保留，
    但只能在正式链路失败后兜底。
    """
    return bool(
        doi
        and _is_subscription_doi(doi)
        and (_is_preprint_url(paper_url) or _is_preprint_url(oa_url))
    )


# 预印本 DOI 前缀：顶部从 paper_search.preprint_resolve 导入的共享单一定义
# （评审 m2 收敛，防三份拷贝漂移）。注意共享表用 "10.48550/"（覆盖 arXiv DOI 全形态，
# 比本处旧表的 "10.48550/arxiv." 更宽，对 defer/升级判断只有正向影响）。

_PREPRINT_URL_MARKERS = (
    "arxiv.org",
    "biorxiv.org",
    "medrxiv.org",
    "researchsquare.com",
    "preprints.org",
    "chemrxiv.org",
    "osf.io",
    "ssrn.com",
)


def _is_preprint_url(url: str | None) -> bool:
    """URL 是否属于预印本站。注意 PMC/Europe PMC 不是预印本，不应被延后到最后。"""
    if not url:
        return False
    low = url.lower()
    return any(marker in low for marker in _PREPRINT_URL_MARKERS)


def _is_preprint_doi(doi: str | None) -> bool:
    """DOI 是否看起来是预印本 DOI。"""
    if not doi:
        return False
    low = doi.strip().lower()
    return low.startswith(_PREPRINT_DOI_PREFIXES)


def _extract_preprint_doi_from_url(url: str | None) -> str | None:
    """从预印本 URL 里抽 DOI，用于“只给 URL 没给 DOI”时查正式发表版。"""
    if not url or not _is_preprint_url(url):
        return None
    m = re.search(r"(10\.\d{4,9}/[^\s?#&]+)", url, flags=re.IGNORECASE)
    if not m:
        return None
    doi = m.group(1)
    doi = re.sub(r"\.full(?:\.pdf)?$", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"\.pdf$", "", doi, flags=re.IGNORECASE)
    # bioRxiv/medRxiv URL 常带 v1/v2 版本号；正式版解析接口用不带版本的 DOI 更稳。
    doi = re.sub(r"(10\.1101/\d{4}\.\d{2}\.\d{2}\.\d+?)v\d+$", r"\1", doi, flags=re.IGNORECASE)
    return doi


async def _resolve_published_doi_for_download(
    doi: str | None, paper_url: str | None, title: str | None
) -> str | None:
    """下载前把预印本 DOI/URL 尽量升级到正式发表版 DOI。

    这是下载层的兜底保障：搜索层通常已经做过 published_doi 回填，但 MCP/agent/用户也可能
    直接传预印本 DOI 或 URL。解析失败静默，保留原预印本路径。
    """
    if doi and not _is_preprint_doi(doi):
        return None
    preprint_doi = doi if _is_preprint_doi(doi) else _extract_preprint_doi_from_url(paper_url)
    if not preprint_doi:
        return None
    try:
        from .preprint_resolve import resolve_published_doi

        return await resolve_published_doi(preprint_doi, title)
    except Exception as exc:
        # REASON: 预印本→正式版解析只是优先级增强，失败不能阻断下载。
        logger.debug("download_pdf: 解析正式发表版 DOI 失败 doi=%s（%s）", preprint_doi, exc)
        return None


def _is_subscription_host(url: str | None) -> bool:
    """URL 是否属已知订阅制出版商（Nature/Elsevier/Wiley/Science 等）。"""
    if not url:
        return False
    low = url.lower()
    return any(h in low for h in _SUBSCRIPTION_HOSTS)


def _is_subscription_doi(doi: str | None) -> bool:
    """DOI 前缀是否通常指向订阅制出版商。

    用于标题搜索命中“正式版 DOI + 预印本 URL”的场景：URL 看起来是 bioRxiv 免费站，
    但 DOI 已经指向 Nature/Science/Wiley 等正式版，合法免费段失败后应进入机构代理。
    """
    if not doi:
        return False
    low = doi.strip().lower()
    return low.startswith(
        (
            "10.1038/",  # Nature/Springer Nature
            "10.1007/",  # Springer
            "10.1016/",  # Elsevier/Cell/Lancet
            "10.1126/",  # Science
            "10.1002/",  # Wiley
            "10.1111/",  # Wiley
            "10.1093/",  # Oxford Academic
            "10.1021/",  # ACS
            "10.1109/",  # IEEE
            "10.1056/",  # NEJM
        )
    )


def _subscription_target_url(
    *,
    doi: str | None,
    doi_landing: str | None,
    paper_url: str | None,
    landing_url: str | None,
    known_paywalled: bool,
) -> str | None:
    """选择机构代理应访问的目标 URL，避免被预印本 URL 抢走。

    机构代理要打到正式出版商或 DOI 跳转页。若搜索结果同时带正式 DOI 和 bioRxiv URL，
    不能把 bioRxiv 当 target，否则 `is_free_site` 会阻止机构段。
    """
    for url in (landing_url, paper_url):
        if _is_subscription_host(url):
            return url
    if doi_landing and (known_paywalled or _is_subscription_doi(doi)):
        return doi_landing
    return None


def _host_of(url: str | None) -> str | None:
    """取 URL 主机名（去 www.），失败返 None。"""
    if not url:
        return None
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or ""
        return host.removeprefix("www.") or None
    except Exception:
        return None


async def _resolve_elsevier_creds(user) -> tuple[str, str] | None:  # noqa: ANN001
    """取 Elsevier (api_key, inst_token)：优先宿主钩子（用户自填），否则全局兜底。

    钩子（config.elsevier_creds_provider）由宿主注入——PaperPilot 在钩子里自开
    短事务读用户设置（下载链全程不持有 DB 事务的铁律不变，2026-08-18 事故整改）。
    都没配 → None（跳过 Elsevier 接口段）。任何异常都降级（不炸下载链）。
    """
    cfg = get_config()
    # 1) 宿主钩子（用户自填，含解密 + 宿主内部可再回退全局）
    if user is not None and cfg.elsevier_creds_provider is not None:
        try:
            got = await cfg.elsevier_creds_provider(user)
            if got is not None:
                return got
        except Exception as exc:
            logger.warning("download_pdf: 取用户 Elsevier Key 失败（%s），试全局兜底", exc)
    # 2) 全局兜底（匿名/无 user 调用，或用户没配）
    gk = (cfg.elsevier_api_key or "").strip()
    if gk:
        return (gk, (cfg.elsevier_inst_token or "").strip())
    return None


async def _library_proxy_gate(user) -> tuple[bool, bool]:  # noqa: ANN001
    """library_proxy 段入口守门（2026-08-23 产品定稿：机构通道默认停用）。

    返回 (enabled, has_credential)：
    - enabled=False：宿主判定机构代理总开关关闭（默认关）——整段跳过，
      tried_sources 不记，终态文案用「机构代理通道已关闭」版本。
    - has_credential=False：开关开着但用户/课题组都没配任何机构凭据——同样跳过
      （防空转；此前要进通道才知道 no_credential）。
    无宿主钩子（独立部署默认）或钩子异常 → (False, False)（机构通道是可选增强，
    读不出来不能放行慢通道）；宿主钩子内部自己遵守「自开短事务」铁律。
    """
    cfg = get_config()
    if cfg.library_proxy_gate is None:
        return False, False
    try:
        return await cfg.library_proxy_gate(user)
    except Exception as exc:  # noqa: BLE001
        logger.warning("download_pdf: 读机构代理开关/凭据失败，按关闭处理（%s）", exc)
        return False, False


def _host_block_count(host: str) -> int:
    """当前 download_pdf 捕获到的该域名「被挡」事件数（429/403/cooling）。"""
    from .domain_cooldown import captured_blocks

    return sum(1 for b in captured_blocks() if b.host == host)


async def _fetch_with_node_rotation(
    fetch_once,  # noqa: ANN001  无参协程，返回 bytes | None
    url: str | None,
    *,
    source: str,
    rotation_counter: dict,
) -> bytes | None:
    """预印本/免费源下载 + 同一次任务内的多出口 IP 轮换（2026-08-23 定稿）。

    fetch_once 被挡（本轮新增该域 429/403/cooling 事件）且代理池 auto 模式可换节点时，
    切到该域没失败过的节点重试——换的是出口 IP 不是重复打同一 IP，最多动用
    _PREPRINT_ROTATION_MAX_NODES 个节点（含初始），每个只打一次。池子没运行/没节点
    可换/失败不是「被挡」而是「没有这个文件」时，直接维持现状返回 None。

    rotation_counter["count"] 累计本次下载链成功动用的换节点次数（终态文案要提
    「已自动更换 N 个出口 IP」）。网络错误（transport 异常）在 adapter 层与「404
    文件不存在」同返 None 且无被挡信号，不触发轮换——盲换 IP 正是要避免的空转。
    """
    from . import proxy as proxy_pool_service
    from .domain_cooldown import normalize_host

    host = normalize_host(url)
    blocks_before = _host_block_count(host) if host else 0
    pdf = await fetch_once()
    if pdf is not None or not host:
        return pdf
    nodes_used = 1  # 初始出口（直连或当时的节点）
    tried: set[str] = set()
    while nodes_used < _PREPRINT_ROTATION_MAX_NODES:
        if _host_block_count(host) <= blocks_before:
            return None  # 这次失败没有「被挡」信号：不是 429/403/cooling，换 IP 无意义
        node = await proxy_pool_service.rotate_node_for_host(host, tried_nodes=tried)
        if node is None:
            return None  # 池子没运行 / 没有可换节点：维持现状快速失败
        tried.add(node)
        nodes_used += 1
        rotation_counter["count"] += 1
        blocks_before = _host_block_count(host)
        logger.info(
            "download_pdf: %s 段被挡（host=%s），已换出口节点 %s 重试（第 %d 个节点）",
            source,
            host,
            node,
            nodes_used,
        )
        pdf = await fetch_once()
        if pdf is not None:
            return pdf
    logger.info(
        "download_pdf: %s 段已试满 %d 个出口节点仍未拿到（host=%s），停止轮换",
        source,
        _PREPRINT_ROTATION_MAX_NODES,
        host,
    )
    return None


async def _try_library_proxy(*, user, doi, landing_url, title) -> tuple[bytes | None, str | None]:  # noqa: ANN001
    """走图书馆代理通道（复旦 libproxy 等纯 HTTP 代理）。

    通道本体由宿主注入（config.library_proxy_fetcher；PaperPilot 的实现是
    library_proxy_channel——凭据解析 + 防封号记账 + 通知，全部自开短事务）。
    返回 (pdf, reason)：pdf=None 时 reason 是通道失败原因（无凭证 / 传输中断 /
    未订阅等），供链尾失败分类（institutional_proxy_failed vs paywall_no_access）。
    无钩子/任何异常都降级 (None, reason)，不炸下载链。
    """
    fetcher = get_config().library_proxy_fetcher
    if fetcher is None:
        # REASON: 图书馆代理通道是可选增强（默认停用），宿主没注入时整条下载链
        # 仍应正常返回失败信号，由前端走「校园网打开」兜底。
        return None, "channel_unavailable"
    try:
        # 通道内部自开短事务（凭证读取/防封号记账），下载链不传 session（见函数 docstring）。
        return await fetcher(user=user, doi=doi, landing_url=landing_url, title=title)
    except Exception as exc:
        # REASON: 跨进程/跨网络，任何异常（代理连接失败/超时/认证失败）都降级，
        # 宿主通道内部已做防封号 record_failure，这里只保证不炸下载链。
        # 异常文本可能内嵌代理凭证，脱敏后才落日志（评审 m9）。
        from .library_proxy_adapter import _redact_creds

        logger.warning("download_pdf: 图书馆代理通道异常（%s），降级", _redact_creds(str(exc)))
        return None, "exception"


def _merge_landing(
    landing_info: dict | None,
    auth_required: bool,
    landing_url: str | None,
    publisher: str | None,
) -> tuple[bool, str | None, str | None]:
    """把一次 meta 抓取的 landing_info 并进累计的付费墙信号（requires_auth 一旦为真就保持）。"""
    if not landing_info:
        return auth_required, landing_url, publisher
    if landing_info.get("requires_auth"):
        auth_required = True
        # 检到付费墙时优先记下该 landing_url（机构登录通道 + 前端兜底用）
        landing_url = landing_info.get("url") or landing_url
    landing_url = landing_url or landing_info.get("url")
    publisher = publisher or landing_info.get("publisher")
    return auth_required, landing_url, publisher


# 交付预印本时的中文用户可见说明（preprint_discovery 段成功返回的 notice 字段，
# fetch API 原样透传给 MCP/前端；入库通知也用它）。
_PREPRINT_DELIVERY_NOTICE = (
    "该论文的正式发表版需要付费订阅，已为你获取同一研究的开放预印本（内容与正式版基本一致）。"
    "如需正式排版版本，请通过机构订阅或校园网访问获取。"
)


async def _identity_gate(
    pdf: bytes, source: str, *, doi: str | None, title: str | None
) -> IdentityVerdict | None:
    """strict 段身份核验门：通过（ok=True）返核验结论，拒绝返 None（调用方把该段视为
    未命中、继续降级）。返回的 verdict 交给 _validate_and_return 折算 pdf_identity
    落进结果字典（任务层据此写 Document.pdf_identity_status）。

    无锚点（doi/title 都没有）时 verify 返回 ok=True（no_anchor）——没有比对依据，
    拒绝无意义，维持旧行为放行（折算为 unverified 而非 verified）。核验失败只拒当前段，
    不终止整条链：后续段（DOI 锚定的官方源 / 机构代理）仍可能拿到正确 PDF。
    fitz 解析包 asyncio.to_thread（二审 B3）：同步 CPU 解析在事件循环里可阻塞
    数百毫秒（60MB+ PDF），下载链是并发的后台任务，不能卡住其他协程。
    """
    verdict = await asyncio.to_thread(verify_pdf_identity, pdf, doi=doi, title=title)
    if verdict.ok:
        return verdict
    logger.warning(
        "download_pdf: %s 段身份核验未通过（reason=%s %s doi=%s title=%.60s），"
        "拒绝该段 %d 字节 PDF、继续降级——疑似顶包文件",
        source,
        verdict.reason,
        verdict.detail,
        doi,
        (title or ""),
        len(pdf),
    )
    return None


async def _validate_and_return(
    pdf: bytes,
    source: str,
    tried: list[str],
    max_bytes: int,
    *,
    doi: str | None = None,
    title: str | None = None,
    identity_verdict: IdentityVerdict | None = None,
    content_url: str | None = None,
    **extra,
) -> dict:
    """size 校验通过返成功 dict，超限返 size_limit_exceeded。extra 并入成功 dict（如 notice）。

    identity_verdict 是 strict 段刚通过的核验结论（2026-08-25 坏条目防固化）：折算成
    pdf_identity 落进成功 dict——仅当锚点真比对通过（doi_match/title_match）才是
    verified；no_anchor / no_text_unverifiable 是「无依据放行」，与非 strict 段
    （DOI 锚定、未跑核验）一律记 unverified。查重侧（fetch / 任务层）只信 verified。
    reason/detail 原文一并落 dict（2026-08-26 事故可观测性：排查「为什么放行/拒收」
    不用再读代码复算，document_acquisitions.attempts 会继续带走）。

    content_url 是该段交付时使用的来源 URL（模板/入口 URL；web discovery 为真实命中
    候选）——2026-08-26 事故里 content_url=None 导致事后无法定位顶包来源页面。
    """
    if len(pdf) > max_bytes:
        logger.warning("download_pdf: %s PDF 超大小上限（%d bytes），拒绝", source, len(pdf))
        return _fail("size_limit_exceeded", tried)
    # DOI 锚定段备查（2026-08-23 顶包事故）：strict 段已在入口核验；其余段抽首页 DOI
    # 记日志，出问题时可从日志比对「下载到的 PDF 到底是哪篇」。
    # to_thread 同 _identity_gate（二审 B3：fitz 解析不阻塞事件循环）。
    if source not in _STRICT_IDENTITY_SOURCES:
        dois = await asyncio.to_thread(backup_extract_dois, pdf)
        if dois:
            logger.info("download_pdf: %s 段首页 DOI 备查：%s", source, list(dois)[:3])
    pdf_identity = (
        "verified"
        if identity_verdict is not None
        and identity_verdict.reason in _VERIFIED_IDENTITY_REASONS
        else "unverified"
    )
    if content_url is not None:
        extra.setdefault("content_url", content_url)
    if identity_verdict is not None:
        extra.setdefault("pdf_identity_reason", identity_verdict.reason)
        extra.setdefault("pdf_identity_detail", identity_verdict.detail or None)
    return _success(pdf, source, tried, doi=doi, title=title, pdf_identity=pdf_identity, **extra)


def _success(
    pdf_bytes: bytes,
    source: str,
    tried: list[str],
    *,
    doi: str | None = None,
    title: str | None = None,
    **extra,
) -> dict:
    result = {
        "success": True,
        "pdf_bytes": pdf_bytes,
        "source": source,
        "size_bytes": len(pdf_bytes),
        "doi": doi,
        "title": title,
        "error": None,
        "tried_sources": tried,
        "auth_required": False,
        "landing_url": None,
        "publisher": None,
    }
    result.update(extra)
    return result


def _fail(
    error: str,
    tried: list[str],
    *,
    auth_required: bool = False,
    landing_url: str | None = None,
    publisher: str | None = None,
    failure_detail: str | None = None,
    message: str | None = None,
) -> dict:
    result = {
        "success": False,
        "pdf_bytes": None,
        "source": None,
        "size_bytes": 0,
        "error": error,
        "tried_sources": tried,
        "auth_required": auth_required,
        "landing_url": landing_url,
        "publisher": publisher,
        # 失败分类（2026-08-21 事故整改）：机器可读的细分原因 + 中文用户可见 message，
        # 区分「付费墙无权限」与「机构通道试过后传输失败」。
        "failure_detail": failure_detail,
        "message": message,
    }
    # 全链失败且本轮撞过限流/冷却：标 rate_limited（2026-08-22 改：即停终态，无自动重试）。
    # failure_detail 一并归一为 rate_limited（评审 m4）：分类与 message 必须同源，
    # 否则「限流稍后重试」和「付费墙无权限」两说两事，用户/运维都被误导。
    # ★付费墙文案优先（2026-08-23 二审定稿）：auth_required=True 时不得覆盖——预印本
    # 多 IP 轮换打满 3 节点后本轮必带 429 捕获，但用户此时要听到的是三段式付费墙指引
    # （手动下载后网页端上传），不是「稍后重试」；限流兜底只留给无付费信号的普通限流。
    if error == "download_failed" and not auth_required:
        retry_after = max_captured_retry_after()
        if retry_after > 0:
            result["error"] = "rate_limited"
            result["retry_after_sec"] = max(1, int(retry_after))
            result["message"] = RATE_LIMITED_MESSAGE
            result["failure_detail"] = "rate_limited"
    return result
