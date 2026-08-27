"""scihub_adapter：Sci-Hub 多镜像串行尝试，解析 HTML 找 PDF URL 后 fetch。

合规约束（D-02）：
- 仅供个人 / 课题组内部临时使用，作为合法源全失败后的最后一级兜底
- 默认 config.scihub_enabled=False 禁用（直接返 None，不发任何网络请求）
- 调用方（paper_download_service）负责把 source='scihub' 标进结果；
  开关开启时才允许 Sci-Hub 来源入知识库（documents.py 据此放行/拦截）

复活说明（2026-06-29）：commit 1a9cdd8 当年为合规删掉本适配器；Lily 本地调试 +
课题组内部使用，灰色地带可接受，故复活。相对旧版的改动：
- 开关走 FetchConfig.scihub_enabled（默认 False，可由 env PAPER_FETCH_SCIHUB_ENABLED 开）
- 镜像列表从 FetchConfig.scihub_base_urls 读（可在 .env 调，镜像域名常变）
- 拿到 PDF URL 后改用 robust_fetch.fetch_pdf_simple（httpx → curl_cffi 两级 +
  magic bytes 校验），过掉一批反爬 CDN，比旧版裸 httpx 稳

安全（T-08-06）：
- 用 re 严格匹配 src 属性；URL 标准化（绝对 / 协议相对 // / 根相对 /）
- 非法 URL（如 javascript:）返 None

W8 网络军规：外部请求继承后端进程网络环境（直连），此处不硬写代理。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from .config import get_config
from .proxy import async_client_for

from .robust_fetch import fetch_pdf_simple

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (Linux; rv:115.0) Firefox/115.0"

# 匹配 <embed ... src="..."> 或 <iframe ... src="...">（不区分大小写）
_SRC_RE = re.compile(
    r'<(?:embed|iframe)[^>]+\bsrc=["\']([^"\']+)["\']',
    re.IGNORECASE,
)


def _mirrors() -> list[str]:
    """从 settings 读镜像列表（英文逗号分隔），去空白 / 去尾斜杠。"""
    raw = get_config().scihub_base_urls or ""
    out: list[str] = []
    for part in raw.split(","):
        m = part.strip().rstrip("/")
        if m:
            out.append(m)
    return out


async def fetch_via_scihub(doi: str | None) -> bytes | None:
    """对 doi 串行尝试各 Sci-Hub 镜像；config.scihub_enabled 为 False 时直接返 None。

    参数：
        doi — 论文 DOI（如 10.1016/j.cell.2021.01.008）

    返回：
        bytes — PDF 原始字节
        None  — 开关禁用 / 无 DOI / 所有镜像都失败 / HTML 里找不到 PDF URL
    """
    cfg = get_config()
    if not cfg.scihub_enabled:
        logger.debug("scihub_adapter: sci_hub_enabled=False，跳过")
        return None

    d = (doi or "").strip()
    if not d:
        return None

    timeout = cfg.scihub_timeout_sec
    for mirror in _mirrors():
        result = await _try_mirror(mirror, d, timeout)
        if result is not None:
            logger.info("scihub_adapter: 命中镜像 %s，doi=%s", mirror, d)
            return result

    logger.debug("scihub_adapter: 所有镜像均失败，doi=%s", d)
    return None


async def _try_mirror(mirror: str, doi: str, timeout: int) -> bytes | None:
    """对单个镜像发请求，解析 HTML 拿 PDF URL，再用 robust_fetch 下 PDF 字节。"""
    page_url = f"{mirror}/{doi}"
    try:
        async with async_client_for(page_url, follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(page_url, headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            pdf_url = _extract_pdf_url(resp.content, mirror)
    except httpx.HTTPStatusError as exc:
        logger.debug("scihub_adapter: 镜像 %s HTTP %d，doi=%s", mirror, exc.response.status_code, doi)
        return None
    except httpx.TimeoutException:
        logger.debug("scihub_adapter: 镜像 %s 超时，doi=%s", mirror, doi)
        return None
    except httpx.RequestError as exc:
        logger.debug("scihub_adapter: 镜像 %s 请求错误（%s），doi=%s", mirror, exc, doi)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("scihub_adapter: 镜像 %s 未知错误（%s），doi=%s", mirror, exc, doi)
        return None

    if not pdf_url:
        logger.debug("scihub_adapter: 镜像 %s 页面未找到 PDF URL，doi=%s", mirror, doi)
        return None

    # 用 robust_fetch（httpx → curl_cffi，自带 magic bytes 校验）拉真正的 PDF 字节，
    # referer 给镜像页（部分 CDN 校验来源）。
    return await fetch_pdf_simple(pdf_url, referer=page_url)


def _extract_pdf_url(html: bytes, mirror: str) -> str | None:
    """从 Sci-Hub HTML 中提取 PDF URL，标准化为绝对 URL。

    处理三种 src 格式：
    1. 绝对 URL：https://cdn.example.com/xxx.pdf → 直接用
    2. 协议相对：//cdn.example.com/xxx.pdf → 加 https:
    3. 根相对路径：/downloads/xxx.pdf → 拼 mirror host

    T-08-06：非法 URL（无法解析 / 不含 http(s)）返 None。
    """
    try:
        html_str = html.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None

    match = _SRC_RE.search(html_str)
    if not match:
        return None

    src = match.group(1).strip()
    # 去掉 src 里可能带的 #navpanes 等锚点（不影响下载，但保持干净）
    if "#" in src:
        src = src.split("#", 1)[0]

    # 协议相对
    if src.startswith("//"):
        return "https:" + src

    # 根相对路径
    if src.startswith("/"):
        parsed_mirror = urlparse(mirror)
        return f"{parsed_mirror.scheme}://{parsed_mirror.netloc}{src}"

    # 绝对 URL（允许 http:// 和 https://）
    if src.startswith("http://") or src.startswith("https://"):
        return src

    # 其他格式（如 javascript:）—— 拒绝（T-08-06）
    logger.debug("scihub_adapter: 发现异常 src 格式，已拒绝：%r", src[:100])
    return None
