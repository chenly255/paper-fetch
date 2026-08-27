"""preprint_adapter：用 URL 模式直接构造 preprint PDF 直链。

为什么单独一层：arXiv / bioRxiv / medRxiv / PLOS / PeerJ 这类站点的论文页 URL
和 PDF URL 之间有稳定的字符串映射规则，纯本地构造，零网络开销；命中后调
oa_adapter.fetch_oa_pdf 完成下载 + PDF magic 校验。

适用场景（按命中率排）：
  1. bioRxiv / medRxiv — Semantic Scholar 返回的 openAccessPdf 经常指向 landing page
     而非 .full.pdf，这层补这个缺口
  2. arXiv             — abs/X → pdf/X.pdf
  3. PLOS              — article?id={doi} → article/file?id={doi}&type=printable
  4. PeerJ             — articles/{id} → articles/{id}.pdf

W8 网络军规：外部下载由 oa_adapter 走 17891 代理，本模块不直接发请求。
"""
from __future__ import annotations

import logging
import re

from .oa_adapter import fetch_oa_pdf

logger = logging.getLogger(__name__)


# arXiv：abs/XXXX.YYYYY → pdf/XXXX.YYYYY.pdf；版本号 vN 可选
_ARXIV_RE = re.compile(
    r"(?:https?://)?(?:export\.)?arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(v\d+)?(?:\.pdf)?",
    re.IGNORECASE,
)

# bioRxiv / medRxiv：/content/{doi} → /content/{doi}.full.pdf
_BIORXIV_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(bio|med)rxiv\.org/content/(10\.\d{4,9}/[^\s?#]+)",
    re.IGNORECASE,
)

# PLOS：journals.plos.org/{family}/article?id={doi}
_PLOS_RE = re.compile(
    r"(?:https?://)?journals\.plos\.org/(\w+)/article\?id=(10\.\d{4,9}/[^\s&#]+)",
    re.IGNORECASE,
)

# PeerJ：peerj.com/articles/{id}
_PEERJ_RE = re.compile(
    r"(?:https?://)?(?:www\.)?peerj\.com/articles/(\d+)",
    re.IGNORECASE,
)


def _resolve_pdf_url(paper_url: str) -> str | None:
    """根据 URL 模式构造 PDF 直链；不匹配任何 preprint 站点返 None。"""
    m = _ARXIV_RE.search(paper_url)
    if m:
        paper_id = m.group(1)
        version = m.group(2) or ""
        return f"https://arxiv.org/pdf/{paper_id}{version}.pdf"

    m = _BIORXIV_RE.search(paper_url)
    if m:
        server = m.group(1).lower()  # 'bio' 或 'med'
        doi_path = _strip_biorxiv_suffix(m.group(2))
        return f"https://www.{server}rxiv.org/content/{doi_path}.full.pdf"

    m = _PLOS_RE.search(paper_url)
    if m:
        family = m.group(1)
        doi = m.group(2)
        return f"https://journals.plos.org/{family}/article/file?id={doi}&type=printable"

    m = _PEERJ_RE.search(paper_url)
    if m:
        article_id = m.group(1)
        return f"https://peerj.com/articles/{article_id}.pdf"

    return None


def _strip_biorxiv_suffix(doi_path: str) -> str:
    """biorxiv DOI 路径可能带 .full / .full.pdf / vN 后缀，去掉再拼新后缀。"""
    s = doi_path
    s = re.sub(r"\.full\.pdf$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.full$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.pdf$", "", s, flags=re.IGNORECASE)
    return s


async def fetch_preprint_pdf(paper_url: str | None) -> bytes | None:
    """匹配 preprint URL 模式构造 PDF 直链并下载；不匹配或下载失败返 None。

    参数：
        paper_url — 论文页 URL（来自 PaperCandidate.source_url 或用户原文）

    返回：
        bytes — PDF 字节
        None  — paper_url 为空 / 不匹配任何 preprint 模式 / 下载失败
    """
    if not paper_url:
        return None

    pdf_url = _resolve_pdf_url(paper_url)
    if not pdf_url:
        return None

    logger.debug("preprint_adapter: 匹配到 PDF 直链 %s（来自 %s）", pdf_url, paper_url)
    return await fetch_oa_pdf(pdf_url)
