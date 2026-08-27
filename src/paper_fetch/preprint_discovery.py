"""preprint_discovery：付费墙正式版 → 按标题发现并定位同研究的开放预印本。

背景（2026-08-21 真实事故，DOI 10.1038/s41586-024-07359-3，Nature 2024 付费墙）：
用户只给正式版 DOI 时，下载链没有任何一段会去「发现」对应的预印本——已有 preprint_fallback
只在候选自带预印本 URL 时生效。产品拍板的降级顺序：先找原文（免费段）→ **找同标题预印本
（快、免费）** → 机构图书馆代理最后兜底（代理实测 ~17KB/s，22.8MB 要 20 分钟，且有防封号额度）。

发现顺序（确定性优先，Tavily 兜底）：
  ① Europe PMC PPR：按标题实词关键词搜预印本记录（SRC:PPR）。实测确定性命中该事故案例
     （PPR609497 / 10.1101/2023.01.27.525553）。注意**精确整标题查询会落空**——预印本标题与
     正式版往往不同（本例正式版 "3D genomic mapping reveals multifocality..."，预印本是
     "Three-dimensional genomic mapping of human pancreatic tissue..."）——所以查询要用标题
     实词的 AND 组合，命中后再做验收。
  ② Crossref query.bibliographic 搜标题、只认 type=posted-content（预印本）候选 + 验收。
  ③ Tavily 限定预印本站搜标题（复用 oa_preprint_discovery，没 key 自动跳过）。

验收 = 双闸门（2026-08-22 产品拍板，替代最初的纯标题阈值）：
  - 标题相似度 ≥ 0.72 仍是主键：同一团队会写多篇相关预印本，只有标题能区分「哪一篇」；
  - 参照作者可得时叠加作者团队闸门：候选的首作者或末位作者（通讯）姓氏须与参照对应位置
    命中其一才接受——作者列表在预印本→正式版之间基本不变，比会改来改去的标题更稳，
    专门拦「标题撞车的别家论文」；
  - 任一侧作者数据缺失 → 退回纯标题验收（阈值不变），绝不因缺数据降召回；
  - Tavily 兜底只有 URL 没有结构化作者，天然走纯标题路径（同上规则）。

查询为何不追加作者限定（AUTH:"首作者"）降噪（2026-08-22 实测后决定不加）：
  a) 实测事故案例：TITLE 关键词查询 hitCount=1，返回的正是正确预印本，本就没什么噪声可降；
  b) 加 AUTH 会在「预印本与正式版首作者不同」时漏召回（合作研究换一作顺序并不罕见），
     而验收闸门已足够防误收——发现阶段宁松勿漏，与实词关键词查询的设计初衷一致；
  c) 参照作者不是总有（只给标题的输入没有），查询会分裂成两条路径，复杂度不划算。

实证过靠不住的路（别依赖）：bioRxiv 的 published 字段（NA）、api.biorxiv.org pubs 接口、
Unpaywall 对该 Nature DOI 无 OA 位置。

本模块只负责「发现」：返回候选（DOI/URL/via），下载由下载链用 preprint_adapter +
浏览器兜底完成（需要链内共享的 FetchBudget）。

W8 网络军规：Europe PMC / Crossref 走后端进程网络环境（直连），Tavily 由其 adapter 自管。
"""
from __future__ import annotations

import logging
import re

from .text_match import title_match_score
from .proxy import async_client_for

from .cooldown_http import cooldown_get

logger = logging.getLogger(__name__)

# 标题相似度阈值：目标（正式版标题）与候选（预印本标题）词重叠 ≥ 此值才认同一篇。
# 实测锚点（2026-08-21 事故案例，写测试锁住）：真实预印本对 0.857 / 同标题摘要 1.0（能收）；
# 无关胰腺/空间组学预印本 0.40-0.47、同系列近缘标题 ≤0.47（能拒）。
# 取 0.72（评审 m3）：0.60-0.72 区间没有可信的正面样本，宁可漏召回（漏了继续走机构代理）
# 不可误收（下错篇），与 preprint_resolve 的 _TITLE_MATCH_MIN 对齐。
_TITLE_MATCH_THRESHOLD = 0.72

_TIMEOUT_SEC = 12.0
_MAILTO = "paperpilot@example.com"
_EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_CROSSREF_WORKS = "https://api.crossref.org/works"

# 预印本 DOI 前缀 → 可构造落地页 URL 的平台（fetch_preprint_pdf 只认这些模板）
_KNOWN_PREPRINT_URL_PREFIXES = ("10.1101/", "10.64898/")

# 作者姓名缩写（名首字母）形态："AM"/"A"/"A.M."/"A.-M." 等——authorString 里跟在姓氏后，
# 解析姓氏时要剥掉。欧洲语系姓氏里没有全大写 ≤3 字符还带点/连号的形态，误伤概率极低。
_INITIALS_RE = re.compile(r"^[A-Z][A-Za-z]?(\.?-?[A-Z][A-Za-z]?\.?)?$")


def _significant_words(title: str, *, min_len: int = 5, limit: int = 3) -> list[str]:
    """取标题实词（≥min_len 字符）前 limit 个（按出现顺序），供 Europe PMC 关键词查询。

    为什么只用少量词而不用整标题：预印本标题与正式版常有出入（本案例整标题查询落空），
    少量核心实词的 AND 组合召回更稳；命中后反正有验收闸门把关，发现阶段宁松勿漏。
    为什么 ≥5 字符：滤掉 "3D"/"of"/"the" 这类短词/停用词，降低误召回。
    """
    words = re.findall(r"[A-Za-z0-9-]{5,}", title or "")
    return words[:limit]


def _preprint_url_for_doi(doi: str | None) -> str | None:
    """已知预印本平台 DOI → 落地页 URL；其他平台返 None。

    bioRxiv/medRxiv（10.1101 / 10.64898）→ biorxiv.org/content/{doi}；
    arXiv（10.48550/arXiv.{id}）→ arxiv.org/abs/{id}（2026-08-23 审计修复 5 补：
    preprint_adapter 的 _ARXIV_RE 能把 abs 页转成 PDF 直链，主链据此对「只给
    arXiv DOI 没给 URL」的输入做模板短路，省掉元数据 API 白跑）。
    """
    doi = (doi or "").strip()
    lowered = doi.lower()
    if lowered.startswith(_KNOWN_PREPRINT_URL_PREFIXES):
        return f"https://www.biorxiv.org/content/{doi}"
    if lowered.startswith("10.48550/arxiv."):
        # 大小写不影响前缀长度（ASCII 1:1），固定长度切片免 split 大小写坑
        arxiv_id = doi[len("10.48550/arxiv."):].strip()
        if arxiv_id:
            return f"https://arxiv.org/abs/{arxiv_id}"
    return None


def _surname_key(name: str | None) -> str | None:
    """作者姓氏归一化比较键：小写、只留字母（含重音/中文）、取末词。

    为什么取末词而不是整串：跨数据源姓氏前缀写法可能不一致（Crossref family
    "van der Heijden" vs 有的源只写 "Heijden"），末词 "heijden" 两边都稳。
    代价是末词同形的不同姓氏（如两个 "Zhang"）可能误配——可接受：作者闸门永远
    叠在标题相似度 ≥0.72 之上，误配也只会放行标题几乎相同的研究。
    """
    cleaned = re.sub(r"[^0-9A-Za-z\u00C0-\u024F\u0400-\u04FF\u4E00-\u9FFF]+", " ", (name or "").lower())
    words = [w for w in cleaned.split() if not w.isdigit()]
    return words[-1] if words else None


def _surnames_from_author_string(author_string: str | None) -> list[str]:
    """Europe PMC authorString（"Braxton AM, Kiemen AL, ..., Wood LD."）→ 姓氏键列表（按作者顺序）。

    每段格式是「姓氏 名缩写」。从尾部剥掉形如缩写（_INITIALS_RE）的 token，
    剩余部分的末词即姓氏键（与 _surname_key 对齐，兼容 "van der Heijden EC" 这类带前缀姓氏）。
    """
    out: list[str] = []
    for seg in (author_string or "").split(","):
        tokens = [t for t in re.split(r"\s+", seg.strip()) if t]
        while tokens and len(tokens) > 1 and _INITIALS_RE.fullmatch(tokens[-1].rstrip(".")):
            tokens.pop()
        key = _surname_key(" ".join(tokens)) if tokens else None
        if key:
            out.append(key)
    return out


def _surnames_from_crossref_authors(authors: list | None) -> list[str]:
    """Crossref author 数组（每项含 family/given；机构作者只有 name）→ 姓氏键列表（按顺序）。"""
    out: list[str] = []
    for a in authors or []:
        if not isinstance(a, dict):
            continue
        key = _surname_key(a.get("family") or a.get("name"))
        if key:
            out.append(key)
    return out


def _author_gate_passes(reference: list[str] | None, candidate: list[str] | None) -> bool | None:
    """作者团队闸门：候选首作者或末位作者姓氏与参照对应位置命中其一 → True；对不上 → False。

    任一侧作者数据缺失（参照拿不到/候选没带作者字段）返 None = 不拦截，
    调用方退回纯标题验收——缺数据不是候选的错，不能因此降召回。
    """
    if not reference or not candidate:
        return None
    if candidate[0] == reference[0] or candidate[-1] == reference[-1]:
        return True
    logger.debug(
        "preprint_discovery: 作者闸门拦截——首/末作者 %s/%s 与参照 %s/%s 均不吻合",
        candidate[0], candidate[-1], reference[0], reference[-1],
    )
    return False


async def crossref_meta_for_doi(doi: str | None) -> tuple[str | None, list[str]]:
    """Crossref works/{doi} 一次 API 补正式版标题 + 作者姓氏键列表；失败静默返 (None, [])。

    为什么需要（评审 M4）：fetch 只给正式版 DOI 是高频输入（2026-08-21 事故本身就是），
    preprint_discovery 靠标题发现、靠作者团队验收——没有它们先来这补一发，都拿不到就跳过该段。
    Crossref 是 DOI 注册机构，题录权威且覆盖全；404（DOI 未收录）也静默返空。

    为什么不带 select 参数（2026-08-22 实测修正）：/works/{doi} 单条路由**不支持** select
    （API 返 400 parameter-not-allowed），旧实现传 select=title 导致线上此调用恒失败、
    只给 DOI 的补题录路径实际从未生效；去掉 select 取全量 message，一次请求同取两者。
    作者解析只认 family（机构作者只有 name，_surnames_from_crossref_authors 兜底）。
    """
    doi = (doi or "").strip()
    if not doi:
        return None, []
    try:
        async with async_client_for(f"{_CROSSREF_WORKS}/{doi}", follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(
                client, f"{_CROSSREF_WORKS}/{doi}", params={"mailto": _MAILTO}
            )
            if resp is None or resp.status_code != 200:
                return None, []
            message = resp.json().get("message") or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("preprint_discovery: Crossref 补题录失败 doi=%s（%s）", doi, exc)
        return None, []
    titles = message.get("title") or []
    title = " ".join(str(t) for t in titles).strip() or None
    return title, _surnames_from_crossref_authors(message.get("author"))


async def _via_europe_pmc(title: str, reference_authors: list[str] | None = None) -> dict | None:
    """Europe PMC SRC:PPR 标题关键词搜（确定性最高，无配额限制）。

    实证查询形态：query=TITLE:"genomic" AND TITLE:"pancreatic" AND SRC:PPR。
    这里用单词 TITLE 子句（TITLE:"genomic" AND TITLE:"mapping" ...），等价且更稳。
    不追加 AUTH:"首作者" 限定：实测无噪声可降 + 首作者变动会漏召回（模块 docstring 有证据）。
    """
    words = _significant_words(title)
    if len(words) < 2:
        return None  # 实词太少，查询噪声大，不搜
    query = " AND ".join(f'TITLE:"{w}"' for w in words) + " AND SRC:PPR"
    try:
        async with async_client_for(_EPMC_SEARCH, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(
                client,
                _EPMC_SEARCH,
                params={
                    "query": query, "format": "json",
                    "resultType": "lite", "pageSize": "5",
                },
            )
            if resp is None or resp.status_code != 200:
                return None
            results = (resp.json().get("resultList") or {}).get("result") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("preprint_discovery: Europe PMC 查询失败（%s）", exc)
        return None

    for r in results:
        cand_doi = str(r.get("doi") or "").strip()
        cand_title = str(r.get("title") or "").strip()
        if not cand_doi or not cand_title:
            continue
        score = title_match_score(title, cand_title)
        if score < _TITLE_MATCH_THRESHOLD:
            continue
        # 作者团队闸门（叠在标题之上，不赦免低标题分）；任一侧缺作者数据 → None → 放行。
        gate = _author_gate_passes(reference_authors, _surnames_from_author_string(r.get("authorString")))
        if gate is False:
            continue
        logger.info(
            "preprint_discovery: Europe PMC 命中预印本 %s（相似度 %.2f，作者闸门=%s）",
            cand_doi, score, "过" if gate else "缺数据跳过",
        )
        return {
            "doi": cand_doi,
            "url": _preprint_url_for_doi(cand_doi),
            "via": "europe_pmc",
            "match_score": score,
        }
    logger.debug("preprint_discovery: Europe PMC 无合格预印本（候选 %d 条）", len(results))
    return None


async def _via_crossref(title: str, reference_authors: list[str] | None = None) -> dict | None:
    """Crossref query.bibliographic 搜标题，只认 type=posted-content（预印本）候选。

    select 里带 author（列表路由支持）供作者团队闸门用；候选缺 author 字段时闸门自动放行。
    """
    try:
        async with async_client_for(_CROSSREF_WORKS, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
            resp = await cooldown_get(
                client,
                _CROSSREF_WORKS,
                params={
                    "query.bibliographic": title, "rows": "5",
                    "select": "DOI,title,type,author", "mailto": _MAILTO,
                },
            )
            if resp is None or resp.status_code != 200:
                return None
            items = (resp.json().get("message") or {}).get("items") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("preprint_discovery: Crossref 查询失败（%s）", exc)
        return None

    for it in items:
        if (it.get("type") or "").lower() != "posted-content":
            continue  # 只要预印本记录；期刊版/会议摘要在正式链路里处理
        cand_doi = str(it.get("DOI") or "").strip()
        cand_title = " ".join(it.get("title") or [])
        if not cand_doi or not cand_title:
            continue
        score = title_match_score(title, cand_title)
        if score < _TITLE_MATCH_THRESHOLD:
            continue
        gate = _author_gate_passes(reference_authors, _surnames_from_crossref_authors(it.get("author")))
        if gate is False:
            continue
        logger.info(
            "preprint_discovery: Crossref 命中预印本 %s（相似度 %.2f，作者闸门=%s）",
            cand_doi, score, "过" if gate else "缺数据跳过",
        )
        return {
            "doi": cand_doi,
            "url": _preprint_url_for_doi(cand_doi),
            "via": "crossref",
            "match_score": score,
        }
    return None


async def _via_tavily(title: str, reference_authors: list[str] | None = None) -> dict | None:
    """Tavily 限定预印本站搜标题（复用 oa_preprint_discovery）；无 key / 额度尽 / 无匹配返 None。

    搜索结果只有 URL + 网页标题，没有结构化作者数据——按双闸门规则天然走纯标题验收路径。
    """
    from .oa_preprint_discovery import discover_oa_preprint_url

    url = await discover_oa_preprint_url(title)
    if not url:
        return None
    return {"doi": None, "url": url, "via": "tavily", "match_score": None}


async def discover_preprint(
    doi: str | None, title: str | None, reference_authors: list[str] | None = None,
) -> dict | None:
    """按标题发现同研究的开放预印本。返回候选 dict 或 None（不抛异常）。

    参数：
        doi               — 用户要的正式版 DOI（仅用于防自映射守卫：发现的 DOI 与输入相同就没意义）
        title             — 正式版标题（发现与验收的依据，为空直接跳过）
        reference_authors — 正式版作者姓氏键列表（crossref_meta_for_doi 产出，可为空）。
                            可得时对候选叠加作者团队闸门；为空时纯标题验收，不降召回。

    返回：{"doi": 预印本 DOI | None, "url": 预印本 URL | None, "via": 来源, "match_score": float | None}
    """
    t = (title or "").strip()
    if len(t) < 12:  # 标题太短无法可靠匹配
        return None
    for finder in (_via_europe_pmc, _via_crossref, _via_tavily):
        try:
            cand = await finder(t, reference_authors)
        except Exception as exc:  # noqa: BLE001
            # REASON: 预印本发现是付费墙后的增强兜底，单个来源失败不该打断其他来源。
            logger.debug("preprint_discovery: %s 失败（%s）", finder.__name__, exc)
            continue
        if cand is None:
            continue
        # 防自映射：发现的 DOI 与输入 DOI 相同（比如输入本身就是预印本）就没有降级意义
        if doi and cand.get("doi") and cand["doi"].strip().lower() == doi.strip().lower():
            continue
        if not cand.get("doi") and not cand.get("url"):
            continue
        return cand
    logger.debug("preprint_discovery: 三个来源都没找到可用的预印本（title=%.40s）", t)
    return None
