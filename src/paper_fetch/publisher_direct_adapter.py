"""publisher_direct_adapter：按 DOI 前缀 / 论文页 URL 构造主流出版商的 PDF 直链。

为什么 ROI 高：很多 OA 论文的 PDF 直链有稳定规律（如 Nature 文章页 + .pdf），
但既不在 Unpaywall/Europe PMC 索引里，也要过 JS 挑战——现有 httpx 链全漏。这一层用
已知模板直接命中，配 robust_fetch（curl_cffi + 浏览器兜底）拿到字节。

模板来自参考项目 scansci-pdf `sources/publishers.py` / `nature.py` + paper-fetch-skill 的
per-publisher PDF 路径模板，挑覆盖面大、OA 命中率高的几家。

注意：付费论文用这些模板也只会拿到挑战页/重定向 HTML（非 %PDF），robust_fetch 自然返 None，
不会误把付费内容当成功——付费由上层 auth_required 信号转机构登录通道。
"""
from __future__ import annotations

import logging
import re

from .robust_fetch import FetchBudget, fetch_pdf_simple, fetch_pdf_via_browser

logger = logging.getLogger(__name__)


def candidate_pdf_urls(doi: str | None, paper_url: str | None) -> list[str]:
    """按 DOI 前缀 + 论文页 URL 规律，构造候选 PDF 直链列表（保序去重）。"""
    out: list[str] = []
    doi = (doi or "").strip()
    low = doi.lower()

    # ---- 按 DOI 前缀（出版商）----
    if doi:
        if low.startswith("10.1038/"):  # Nature 家族（Nature / Nat Commun / Sci Rep …）
            suffix = doi.split("/", 1)[1]
            out.append(f"https://www.nature.com/articles/{suffix}.pdf")
        elif low.startswith("10.1186/") or low.startswith("10.1007/"):  # BMC / SpringerOpen / Springer
            out.append(f"https://link.springer.com/content/pdf/{doi}.pdf")
        elif low.startswith("10.3389/"):  # Frontiers
            out.append(f"https://www.frontiersin.org/articles/{doi}/pdf")
        elif low.startswith("10.1073/"):  # PNAS
            out.append(f"https://www.pnas.org/doi/pdf/{doi}")
        elif low.startswith("10.1126/"):  # Science / Science Advances
            out.append(f"https://www.science.org/doi/pdf/{doi}")
        elif low.startswith("10.1002/"):  # Wiley
            out.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true")
        elif low.startswith("10.1111/"):  # Wiley（另一前缀）
            out.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true")
        elif low.startswith("10.3390/"):  # MDPI
            out.extend(_mdpi_pdf_urls_from_doi(doi))

    # ---- 按论文页 URL 规律（无 DOI 或前缀未覆盖时兜底）----
    pu = (paper_url or "").strip()
    if pu:
        m = re.search(r"nature\.com/articles/([^/?#]+)", pu, re.IGNORECASE)
        if m:
            out.append(f"https://www.nature.com/articles/{m.group(1).removesuffix('.pdf')}.pdf")
        m = re.search(r"link\.springer\.com/article/(10\.[^\s?#]+)", pu, re.IGNORECASE)
        if m:
            out.append(f"https://link.springer.com/content/pdf/{m.group(1)}.pdf")
        if "mdpi.com" in pu.lower():  # MDPI：文章页 + /pdf
            base = pu.split("?")[0].rstrip("/")
            base = re.sub(r"/htm$", "", base, flags=re.IGNORECASE)
            out.append(base + "/pdf")
        if "frontiersin.org" in pu.lower() and "/full" in pu.lower():
            out.append(re.sub(r"/full\b", "/pdf", pu, flags=re.IGNORECASE))

    # 保序去重
    seen: set[str] = set()
    result: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def _mdpi_pdf_urls_from_doi(doi: str) -> list[str]:
    """从 MDPI DOI 构造静态资源域名 PDF 直链。

    MDPI 网页域名 `www.mdpi.com/.../pdf` 在一些网络环境会直接返 403，但同一篇
    论文的正式 PDF 通常同时挂在 `mdpi-res.com/d_attachment/...`。DOI 形如
    `10.3390/pharmaceutics18060752`：journal=pharmaceutics, volume=18,
    issue=06, article=0752；静态文件名使用 5 位 article 编号。
    """
    suffix = doi.split("/", 1)[1].strip().lower()
    m = re.match(r"^([a-z0-9]+?)(\d{2})(\d{2})(\d{3,5})$", suffix)
    if not m:
        return []
    journal, volume, _issue, article = m.groups()
    volume_num = str(int(volume))
    article_num = f"{int(article):05d}"
    stem = f"{journal}-{volume_num}-{article_num}"
    base = f"https://mdpi-res.com/d_attachment/{journal}/{stem}/article_deploy/{stem}"
    return [
        f"{base}.pdf",
        f"{base}-v2.pdf",
    ]


async def fetch_publisher_direct(
    doi: str | None,
    paper_url: str | None,
    *,
    referer: str | None = None,
    budget: FetchBudget | None = None,
) -> bytes | None:
    """构造出版商 PDF 直链并下载。先对所有候选走快路（httpx+curl_cffi），都 miss 再对首选开浏览器。"""
    cands = candidate_pdf_urls(doi, paper_url)
    if not cands:
        return None
    ref = referer or paper_url or (f"https://doi.org/{doi}" if doi else None)
    logger.debug("publisher_direct: %d 个候选直链 %s", len(cands), cands)

    # 第一轮：所有候选走快路
    for url in cands:
        pdf = await fetch_pdf_simple(url, referer=ref)
        if pdf is not None:
            logger.info("publisher_direct: 快路命中 %s", url)
            return pdf

    # 第二轮：首选候选开浏览器（过 JS 挑战），消耗 budget
    pdf = await fetch_pdf_via_browser(cands[0], referer=ref, budget=budget)
    if pdf is not None:
        logger.info("publisher_direct: 浏览器命中 %s", cands[0])
    return pdf
