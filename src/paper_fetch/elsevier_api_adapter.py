"""elsevier_api_adapter：用 Elsevier 官方接口拿 ScienceDirect/Cell/Elsevier 全文 PDF。

为什么单列一段、ROI 最高：Elsevier 家族（sciencedirect.com / cell.com / 10.1016 前缀）的
网页全是 Cloudflare「真人验证」硬墙，paperpilot 现有 publisher_direct / 浏览器兜底都过不去，
只能投降走校园网。而 Elsevier 自己**免费发接口钥匙（API Key）**，拿钥匙直接问官方数据接口要
全文——**完全不碰网页**，也就绕开了 Cloudflare 和验证码。这是闭源 Elsevier 论文最稳的一条路。

稳定路径（移植自参考项目 scansci-pdf sources/elsevier_api.py，实测 10 篇闭源全中）：
  1. GET  https://api.elsevier.com/content/article/doi/{doi}?view=FULL   （Accept: application/xml）
     → 全文 XML（含附件清单）
  2. 从 XML 解析出**正文 PDF** 的对象编号 EID（如 1-s2.0-S0006320725007013-main.pdf），
     排除 supplement/mmc/appendix/graphical 这些补充材料。
  3. GET  https://api.elsevier.com/content/object/eid/{eid}              （Accept: application/pdf）
     → 出版社正式 PDF。
  直接请求 article 端点的 PDF（不走 XML→object/eid）对闭源文章只返回 1 页预览，故拒单页。

授权依赖（关键坑，源码 + scansci-pdf 文档都强调）：
- Elsevier 的闭源全文授权**与请求出口 IP 强绑定**。必须让 api.elsevier.com 从机构出口出去
  （校园网 / 学校 VPN / 规则 VPN / 图书馆出口）才认订阅。
- 所以这里**直连优先**（trust_env=False，不读环境代理）：绝不让普通代理把 api.elsevier.com
  转发到非机构 IP。后端进程跑在校园网/VPN 上时，直连即走机构出口。direct 拿不到再退到
  显式传入的 fallback 代理（一般用不到）。
- 没机构授权时 OA 文章仍能成功、闭源返回 NOT_ENTITLED / object 401——这正好天然反映
  「当前网络是否在授权范围」，是智能路由判断校园网的依据之一。

合规：Elsevier 官方授权通道，纯 HTTP、不开浏览器、不碰验证码、无封号风险，与项目删 Sci-Hub
的合规态度一致。失败只记 route/状态码/X-ELS-Status，**绝不记 API Key**。
"""
from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET

from defusedxml.ElementTree import fromstring as _defused_fromstring
from urllib.parse import quote

import httpx

from .cooldown_http import cooldown_get_sync

logger = logging.getLogger(__name__)

_API_BASE = "https://api.elsevier.com/content"
_TIMEOUT_SEC = 30
# 正式全文 PDF 的最小字节数（小于此判为异常/预览）
_MIN_PDF_BYTES = 10_000

# Elsevier 家族判定：DOI 前缀 + 落地页域名。命中才值得走这条接口。
_ELSEVIER_DOI_PREFIXES = ("10.1016/", "10.1006/", "10.1053/", "10.1067/", "10.1078/")
_ELSEVIER_HOSTS = ("sciencedirect.com", "elsevier.com", "cell.com")


def is_elsevier_target(doi: str | None, url: str | None) -> bool:
    """DOI 或落地页 URL 是否属 Elsevier 家族（决定是否值得走官方接口）。"""
    d = (doi or "").strip().lower()
    if d:
        if any(d.startswith(p) for p in _ELSEVIER_DOI_PREFIXES):
            return True
    u = (url or "").lower()
    if u and any(h in u for h in _ELSEVIER_HOSTS):
        return True
    return False


# ---------------- 对外入口 ----------------

async def fetch_via_elsevier_api(
    doi: str | None,
    *,
    api_key: str,
    inst_token: str = "",
) -> bytes | None:
    """用 Elsevier 官方接口下全文 PDF。成功返 PDF 字节，否则 None。

    同步逻辑（XML 解析、PDF 校验）丢线程池跑，避免阻塞事件循环。
    """
    d = (doi or "").strip()
    if not d or not api_key:
        return None
    try:
        return await asyncio.to_thread(_fetch_sync, d, api_key, inst_token)
    except Exception as exc:
        # REASON: 接口段是下载链一环，任何异常都降级 None 不炸链。绝不打印 api_key。
        logger.warning("elsevier_api: doi=%s 异常（%s）", d, exc)
        return None


def _fetch_sync(doi: str, api_key: str, inst_token: str) -> bytes | None:
    """直连优先拿 XML → 解析 main.pdf EID → object/eid 下 PDF；兜底直拉 article PDF（OA 用）。"""
    # trust_env=False：不读环境代理，强制直连，让 api.elsevier.com 走机构出口（校园网/VPN）。
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
        eids = _fetch_attachment_eids(client, doi, api_key, inst_token)
        for eid in eids:
            pdf = _fetch_pdf_by_eid(client, eid, api_key, inst_token)
            if pdf is not None:
                logger.info("elsevier_api: 命中 object/eid（%d 字节）doi=%s", len(pdf), doi)
                return pdf
        # 兜底：直接 article 端点要 PDF（OA 文章可成功；闭源多为 1 页预览，会被拒）
        pdf = _fetch_pdf_direct(client, doi, api_key, inst_token)
        if pdf is not None:
            logger.info("elsevier_api: 命中 article direct（%d 字节）doi=%s", len(pdf), doi)
        return pdf


# ---------------- HTTP 请求 ----------------

def _headers(api_key: str, inst_token: str, accept: str) -> dict[str, str]:
    h = {"X-ELS-APIKey": api_key, "Accept": accept}
    if inst_token:
        h["X-ELS-Insttoken"] = inst_token
    return h


def _api_get(
    client: httpx.Client,
    url: str,
    *,
    api_key: str,
    inst_token: str,
    accept: str,
    params: dict | None = None,
) -> httpx.Response | None:
    # cooldown_get_sync：429 进域名冷却（下一篇任务不再硬撞 api.elsevier.com）；
    # 401/403 是订阅授权问题（api.elsevier.com 非免费站，observe 不会冷却），照旧透传。
    try:
        resp = cooldown_get_sync(
            client, url, headers=_headers(api_key, inst_token, accept), params=params
        )
    except httpx.HTTPError as exc:
        logger.debug("elsevier_api: 请求失败 %s（%s）", url, exc)
        return None
    if resp is None:
        return None
    if resp.status_code in (401, 403):
        # NOT_ENTITLED / 授权不足：当前出口 IP 没有该文章的机构订阅。记状态，不记 key。
        logger.info(
            "elsevier_api: HTTP %d（无授权，检查是否走机构出口）X-ELS-Status=%s",
            resp.status_code, resp.headers.get("X-ELS-Status", ""),
        )
    return resp


def _fetch_attachment_eids(
    client: httpx.Client, doi: str, api_key: str, inst_token: str
) -> list[str]:
    """拿 view=FULL XML，解析出按优先级排序的正文 PDF EID 列表。"""
    url = f"{_API_BASE}/article/doi/{doi}"
    resp = _api_get(
        client, url, api_key=api_key, inst_token=inst_token,
        accept="application/xml", params={"view": "FULL"},
    )
    if resp is None or resp.status_code != 200:
        return []
    return _extract_pdf_attachment_eids(resp.text)


def _fetch_pdf_by_eid(
    client: httpx.Client, eid: str, api_key: str, inst_token: str
) -> bytes | None:
    """用 Content Object 接口按 EID 下 PDF。"""
    url = f"{_API_BASE}/object/eid/{quote(eid, safe='')}"
    resp = _api_get(client, url, api_key=api_key, inst_token=inst_token, accept="application/pdf")
    if resp is None or resp.status_code != 200:
        return None
    content = resp.content
    if not _response_is_pdf(resp, content):
        return None
    if not _valid_pdf_bytes(content, f"object/eid {eid}", reject_single_page=True):
        return None
    return content


def _fetch_pdf_direct(
    client: httpx.Client, doi: str, api_key: str, inst_token: str
) -> bytes | None:
    """兜底：article 端点直接要 PDF（OA 文章可成功，闭源多为预览，拒单页）。"""
    url = f"{_API_BASE}/article/doi/{doi}"
    resp = _api_get(client, url, api_key=api_key, inst_token=inst_token, accept="application/pdf")
    if resp is None or resp.status_code != 200:
        return None
    content = resp.content
    if not _response_is_pdf(resp, content):
        return None
    if not _valid_pdf_bytes(content, "article direct", reject_single_page=True):
        return None
    return content


# ---------------- 校验 ----------------

def _response_is_pdf(resp: httpx.Response, content: bytes) -> bool:
    ct = (resp.headers.get("content-type") or "").lower()
    return "pdf" in ct or content[:5] == b"%PDF-"


def _valid_pdf_bytes(content: bytes, label: str, *, reject_single_page: bool) -> bool:
    """PDF 有效性：magic bytes + 最小体积 + 拒单页预览（闭源 article 端点常返 1 页预览）。"""
    if content[:5] != b"%PDF-":
        logger.info("elsevier_api: %s 非 PDF", label)
        return False
    if len(content) < _MIN_PDF_BYTES:
        logger.info("elsevier_api: %s 太小（%d 字节）疑似预览", label, len(content))
        return False
    if reject_single_page and _pdf_page_count(content) == 1:
        logger.info("elsevier_api: %s 仅 1 页，判为预览拒绝", label)
        return False
    return True


def _pdf_page_count(content: bytes) -> int | None:
    """数 PDF 页数（pymupdf）；数不出返 None（不因此拒绝）。"""
    try:
        import pymupdf
    except Exception:
        return None
    try:
        with pymupdf.open(stream=content, filetype="pdf") as doc:
            return int(doc.page_count)
    except Exception:
        return None


# ---------------- XML → 正文 PDF EID（移植自 scansci-pdf，纯函数便于单测）----------------

def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1].lower()


def _element_text(el: ET.Element) -> str:
    return " ".join(" ".join(el.itertext()).split())


def _looks_like_pdf_eid(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(lowered) and ".pdf" in lowered


def _article_eid_to_main_pdf(value: str) -> str:
    """只有文章 EID（1-s2.0-...）时，按 Elsevier 命名推断正文 PDF 名 -main.pdf。"""
    candidate = value.strip()
    if candidate.lower().startswith("eid:"):
        candidate = candidate.split(":", 1)[1].strip()
    if not candidate.startswith("1-s2.0-"):
        return ""
    if candidate.lower().endswith(".pdf"):
        return candidate
    return f"{candidate}-main.pdf"


def _attachment_container(
    el: ET.Element, parent_map: dict[ET.Element, ET.Element]
) -> ET.Element:
    node = parent_map.get(el, el)
    while node is not None:
        local = _local_name(str(node.tag))
        if "attachment" in local or "object" in local or local == "web-pdf":
            return node
        node = parent_map.get(node)
    return parent_map.get(el, el)


def _attachment_metadata(el: ET.Element) -> str:
    parts: list[str] = []
    for node in el.iter():
        local = _local_name(str(node.tag))
        text = _element_text(node)
        if text:
            parts.append(f"{local}:{text}")
        for attr_name, attr_value in node.attrib.items():
            attr_local = _local_name(str(attr_name))
            if attr_value:
                parts.append(f"{attr_local}:{attr_value}")
    return " ".join(parts).lower()


def _attachment_score(eid: str, metadata: str) -> int:
    """给候选 PDF 打分：正文(main/full-text)高分，补充材料(supplement/mmc…)负分。"""
    haystack = f"{eid} {metadata}".lower()
    score = 0
    if eid.lower().endswith(".pdf"):
        score += 20
    if "pdf" in haystack:
        score += 10
    if "main" in haystack or "full-text" in haystack or "fulltext" in haystack:
        score += 100
    if "page-count" in haystack or "pages" in haystack:
        score += 5
    if "attachment-size" in haystack or "filesize" in haystack or "file-size" in haystack:
        score += 5
    if any(m in haystack for m in ("supplement", "supplementary", "mmc", "appendix", "graphical")):
        score -= 100
    return score


# ---------------- 访问探测（智能路由 / 前端"测试访问"按钮用）----------------

# 一个全开放获取（OA）的 Elsevier DOI，用来验「Key 有效 + 服务器网络能到 Elsevier」——
# OA 文章不依赖订阅，只要 Key 对、网络通就返 200，可与"订阅授权"区分开。
_SAMPLE_OA_DOI = "10.1016/j.heliyon.2024.e24161"


async def probe_entitlement(
    api_key: str, *, inst_token: str = "", doi: str | None = None
) -> dict:
    """探测当前服务器网络 + Key 的 Elsevier 访问能力。给前端"测试访问"按钮用。

    - doi 给定：测这篇具体论文能否拿到全文（含订阅授权判断）。
    - doi 省略：用一个 OA 样例 DOI，只验「Key 有效 + 网络可达 Elsevier」。

    返回 {ok, reason, detail}：
      reason ∈ {ok, entitled, not_entitled, invalid_key, unreachable, no_key}
    """
    if not api_key:
        return {"ok": False, "reason": "no_key", "detail": "未配置 Elsevier API Key"}
    test_doi = (doi or "").strip() or _SAMPLE_OA_DOI
    try:
        return await asyncio.to_thread(_probe_sync, test_doi, api_key, inst_token, bool(doi))
    except Exception as exc:
        return {"ok": False, "reason": "unreachable", "detail": f"探测异常：{exc}"}


def _probe_sync(doi: str, api_key: str, inst_token: str, user_doi: bool) -> dict:
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=_TIMEOUT_SEC) as client:
        url = f"{_API_BASE}/article/doi/{doi}"
        resp = _api_get(
            client, url, api_key=api_key, inst_token=inst_token,
            accept="application/xml", params={"view": "FULL"},
        )
        if resp is None:
            return {"ok": False, "reason": "unreachable", "detail": "连不上 api.elsevier.com（检查网络/出口）"}
        if resp.status_code in (401, 403):
            els = resp.headers.get("X-ELS-Status", "")
            if "APIKEY" in els.upper() or resp.status_code == 401:
                return {"ok": False, "reason": "invalid_key", "detail": f"Key 无效或权限不足（HTTP {resp.status_code} {els}）"}
            return {"ok": False, "reason": "not_entitled",
                    "detail": "当前网络无该文章订阅授权——请确认后端跑在校园网/学校 VPN 上"}
        if resp.status_code != 200:
            return {"ok": False, "reason": "unreachable", "detail": f"Elsevier 返回 HTTP {resp.status_code}"}
        # 200：能拿到全文 XML
        eids = _extract_pdf_attachment_eids(resp.text)
        if user_doi:
            if eids:
                return {"ok": True, "reason": "entitled", "detail": "可拿到这篇论文的全文 PDF ✅"}
            return {"ok": True, "reason": "entitled",
                    "detail": "Key 与网络正常，但这篇未解析到全文 PDF（可能仅元数据授权）"}
        return {"ok": True, "reason": "ok",
                "detail": "Key 有效、服务器网络可直连 Elsevier ✅（闭源全文还取决于机构订阅）"}


def _extract_pdf_attachment_eids(xml_text: str) -> list[str]:
    """从 view=FULL XML 解析正文 PDF 的 EID，按优先级（正文优先、补充材料垫底）排序返回。"""
    try:
        root = _defused_fromstring(xml_text)  # defusedxml：上游被劫持/注入实体时拒绝展开（XXE/十亿笑声）
    except ET.ParseError as exc:
        logger.warning("elsevier_api: XML 解析失败：%s", exc)
        return []

    parent_map = {child: parent for parent in root.iter() for child in parent}
    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()

    for el in root.iter():
        local = _local_name(str(el.tag))
        found: list[str] = []

        if local in {"attachment-eid", "object-eid"}:
            text = _element_text(el)
            if _looks_like_pdf_eid(text):
                found.append(text)
        elif local in {"eid", "identifier"}:
            main_pdf = _article_eid_to_main_pdf(_element_text(el))
            if main_pdf:
                found.append(main_pdf)

        for attr_name, attr_value in el.attrib.items():
            attr_local = _local_name(str(attr_name))
            if attr_local in {"attachment-eid", "object-eid", "eid"}:
                value = str(attr_value).strip()
                if _looks_like_pdf_eid(value):
                    found.append(value)
                else:
                    main_pdf = _article_eid_to_main_pdf(value)
                    if main_pdf:
                        found.append(main_pdf)

        for eid in found:
            if eid in seen:
                continue
            seen.add(eid)
            metadata = _attachment_metadata(_attachment_container(el, parent_map))
            candidates.append((_attachment_score(eid, metadata), len(candidates), eid))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [eid for _, _, eid in candidates]
