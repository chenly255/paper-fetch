"""元数据/发现类 API 请求的冷却收口（2026-08-23 审计修复 1）。

为什么需要：robust_fetch 三级下载出口已接 domain_cooldown，但各元数据 adapter
（Europe PMC / OpenAlex / Unpaywall / Crossref / Elsevier API / preprint_discovery）
直接用 httpx 发请求，撞 429 只当普通失败——域名不进冷却、不进代理失败史，
下一篇任务继续硬撞同一域名（429 越撞越凶）。本模块把这类「查元数据」请求统一
包一层：

  请求前  should_skip_url —— 域名在冷却期就直接跳过（返 None，不发出请求）；
  响应后  observe_http_status —— 429 / 免费站反爬 403 进冷却 + 记代理失败史
          （延迟 import，代理未启用时 no-op）+ 写 capture（供 download_pdf 外层
          的 capture_blocks 把全链失败归一为 rate_limited 终态）。

用法（把 ``client.get(url, ...)`` 换成 ``cooldown_get(client, url, ...)``）：

    async with async_client_for(url, ...) as client:
        resp = await cooldown_get(client, url, params={...})
        if resp is None or resp.status_code != 200:
            return ...  # 冷却跳过 / 非 200：按原有失败路径降级

返回值：httpx.Response 原样返回（状态码判断留给调用方，行为与改前一致，
只是多观察了一眼光环状态码）；冷却跳过时返 None，调用方须判空。

为什么不做六处复制粘贴：规则（何时冷却、冷却多久、怎么归类）必须单点定义，
否则各 adapter 的判定迟早漂移。
"""
from __future__ import annotations

import logging

import httpx

from .domain_cooldown import observe_http_status, should_skip_url

logger = logging.getLogger(__name__)


def _final_url(resp: httpx.Response, fallback: str) -> str:
    """重定向跟随后的最终 URL（限流的是最终域名）；取不到用入参兜底。"""
    try:
        return str(resp.url) or fallback
    except Exception:  # noqa: BLE001
        return fallback


async def cooldown_get(
    client: httpx.AsyncClient, url: str, **kwargs
) -> httpx.Response | None:
    """带冷却观察的异步 GET（元数据 API 层统一出口）。

    kwargs 原样透传给 client.get（params / headers 等）。
    冷却中的域名返 None（跳过不请求）；其余返原始 Response。
    """
    if should_skip_url(url):
        return None
    resp = await client.get(url, **kwargs)
    observe_http_status(_final_url(resp, url), resp.status_code, resp.headers)
    return resp


def cooldown_get_sync(
    client: httpx.Client, url: str, **kwargs
) -> httpx.Response | None:
    """cooldown_get 的同步版（给线程池里跑的同步 adapter，如 Elsevier API）。

    httpx.Client.get 是同步调用；注意调用方若经 asyncio.to_thread 进入，
    to_thread 会拷贝 ContextVar，capture 仍能写回本次 download_pdf。
    """
    if should_skip_url(url):
        return None
    resp = client.get(url, **kwargs)
    observe_http_status(_final_url(resp, url), resp.status_code, resp.headers)
    return resp
