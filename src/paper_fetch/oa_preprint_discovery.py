"""付费墙论文 → 找开放预印本 URL（学 feishu_agent 的「跨 DOI 找 OA 版本」能力）。

背景（2026-06-12 真实案例）：Nature Climate Change 论文 `10.1038/s41558-026-02659-0`
付费墙，作者把开放版（CC BY）放在 Research Square `10.21203/rs.3.rs-7491013`——
预印本有**独立 DOI**，按正刊 DOI 查 Unpaywall/OpenAlex/Crossref 都说 is_oa=False、查不到。
feishu_agent 靠网页/标题搜索发现这个独立 OA 版本，正是我们 DOI 导向下载链缺的一层。

做法（KISS，复用已有件）：把 Tavily 搜索**限定在预印本站域名**、按标题搜 →「域名命中 + 标题
相似度」双重过滤 → 返回最佳候选 URL，交回 download_pdf 现有链路（oa/meta 段）抓真 PDF。
（实测：限定域名搜该标题，第 1 条即 researchsquare 的 PDF，相似度 0.90；同站不相干论文 0.20，
能干净区分。）
"""
from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

from . import tavily_client
from .tavily_client import TavilyQuotaExhausted

logger = logging.getLogger(__name__)

# 已知开放预印本 / OA 仓储站点：这些站点上同标题论文≈作者自存的开放获取版本。
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
    "europepmc.org",
)

# 匹配阈值：用「正刊标题的词，有多大比例出现在候选标题里」。
# 为什么不用整串相似度：Tavily 给 PDF 的标题常是「[PDF] 截断标题… - Research Square」，
# 整串比对会偏低；词重叠对截断/装饰/改一个词（tripled→quadrupled）都稳。
# 只比标题、不比摘要（2026-08-24 顶包事故）：同实验室另一篇论文的摘要用词与目标高度
# 相似，靠摘要混过旧 0.6 阈值，造成「题录对、正文错」的坏条目。阈值 0.72 与
# preprint_discovery._TITLE_MATCH_THRESHOLD 对齐（0.60-0.72 无可信正面样本，宁漏勿误收）。
# 防误配另一道硬约束：必须是预印本站域名。
_MATCH_THRESHOLD = 0.72


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _significant_words(s: str) -> set[str]:
    # 取长度 > 2 的词（滤掉 a/of/in 等噪声）
    return {w for w in _normalize(s).split() if len(w) > 2}


def _match_score(target_title: str, candidate_title: str) -> float:
    """正刊标题的「实词」有多大比例出现在候选**标题**里（0~1）。

    只比标题不比摘要（2026-08-24 顶包事故）：摘要是同领域/同实验室别篇论文也能高度
    重叠的部分，把它计入会让「标题不同、摘要相似」的候选混过闸门。
    """
    target = _significant_words(target_title)
    if not target:
        return 0.0
    hay = _significant_words(candidate_title)
    overlap = len(target & hay) / len(target)
    # 标题被截断时，再用整串相似度兜一手
    seq = SequenceMatcher(None, _normalize(target_title), _normalize(candidate_title)).ratio()
    return max(overlap, seq)


def _is_preprint_host(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:  # noqa: BLE001 —— URL 解析异常一律视为非预印本站
        return False
    return any(host == h or host.endswith("." + h) for h in _PREPRINT_HOSTS)


def _query_variants(title: str) -> list[str]:
    """多查询变体：学 feishu「agent 多轮迭代」的精髓，用确定性多查询取并集治 web 搜索的随机性。

    实测单次 advanced 搜索结果每次飘（这次返对的、那次返别篇）；三条变体取并集后稳定命中正确预印本。
    """
    return [
        title,
        f"{title} preprint pdf",
        f"{title} Research Square bioRxiv medRxiv preprint",
    ]


async def _search_one(query: str) -> list:
    """单条查询（限定预印本站 + advanced）；任何异常/额度问题都退回空列表，不打断。

    search_depth 必须用 "advanced"——实测 "basic" 下 include_domains 被忽略、搜不出预印本（2026-06-12 踩坑）。
    """
    try:
        return await tavily_client.search_with_pool(
            query,
            limit=10,
            include_domains=list(_PREPRINT_HOSTS),
            search_depth="advanced",
        )
    except TavilyQuotaExhausted:
        logger.warning("oa_preprint_discovery: Tavily 号池额度耗尽，停止预印本发现")
        raise  # 整池用尽，没必要再试其他变体
    except Exception as exc:  # noqa: BLE001
        # REASON: 预印本发现是下载失败后的增强项，单条查询异常不该打断其他变体/整个下载流程。
        logger.warning("oa_preprint_discovery: 单条查询失败：%s", exc)
        return []


async def discover_oa_preprint_url(title: str | None) -> str | None:
    """付费墙论文按标题找开放预印本 URL（多查询取并集 → 域名+标题词重叠匹配，取最佳）。

    返回：预印本站上同标题论文的 URL（PDF 直链或落地页，交回 download_pdf 抓取）；
    无 key / 额度耗尽 / 无匹配 → None（不抛异常，下载兜底走付费墙提示，不打断流程）。
    """
    title = (title or "").strip()
    if len(title) < 12:  # 标题太短无法可靠匹配
        return None

    if not tavily_client.has_keys():
        logger.debug("oa_preprint_discovery: Tavily 号池为空，跳过预印本发现")
        return None

    # 多查询变体取并集（按 URL 去重，保留最高匹配分）
    candidates: dict[str, float] = {}
    try:
        for query in _query_variants(title):
            for c in await _search_one(query):
                if not _is_preprint_host(c.url):
                    continue
                score = _match_score(title, c.title)
                if c.url not in candidates or score > candidates[c.url]:
                    candidates[c.url] = score
    except TavilyQuotaExhausted:
        return None  # 号池整体用尽，放弃发现

    # 取匹配分最高且过阈值的候选
    best_url, best_score = None, 0.0
    for url, score in candidates.items():
        if score >= _MATCH_THRESHOLD and score > best_score:
            best_url, best_score = url, score

    if best_url:
        logger.info(
            "oa_preprint_discovery: 命中开放预印本（匹配分 %.2f，并集 %d 候选）：%s",
            best_score, len(candidates), best_url,
        )
    else:
        logger.debug("oa_preprint_discovery: 预印本站无标题相似候选（并集 %d）", len(candidates))
    return best_url
