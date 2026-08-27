"""代理出口抽象：ProxyProvider 协议 + 默认 EnvProxyProvider。

PaperPilot 里代理出口由 proxy_pool_service（内嵌 mihomo，按域名分流 + 节点轮换）
统一裁决；paper-fetch 作为独立库不能依赖它，于是把「这个 URL 走哪个代理 /
被限流时能不能换出口 IP」抽象成协议：

- 独立部署：默认 EnvProxyProvider——读 FetchConfig.http_proxy
  （环境变量 PAPER_FETCH_HTTP_PROXY），全部境外流量走这一个固定代理，
  没配就直连；单固定代理无从「换节点」，rotate 恒返 None（链路自动放弃轮换）。
- PaperPilot：启动时 ``set_proxy_provider()`` 注入包装 proxy_pool_service 的
  适配器，行为与重构前完全一致（按域名分流 + 多 IP 轮换 + 失败史）。

网络军规（沿用 PaperPilot 2026-08-22 定稿）：一切 client ``trust_env=False``，
绝不读 http_proxy/HTTPS_PROXY 等环境代理——代理只能来自本模块的判定。
"""
from __future__ import annotations

from typing import Protocol

import httpx

from .config import get_config


class ProxyProvider(Protocol):
    """HTTP 出口判定协议（全链唯一代理来源）。"""

    def proxy_for_url(self, url: str | None) -> str | None:
        """该 URL 应走的代理地址；None = 直连。"""
        ...

    def enabled(self) -> bool:
        """境外源此刻是否实际有代理可用（下载开头的出口状态日志用）。"""
        ...

    async def rotate_node_for_host(
        self, host: str, tried_nodes: set[str]
    ) -> str | None:
        """被限流后换一个该域没失败过的出口节点；无池/无可换节点返 None。"""
        ...

    def record_block(self, url_or_host: str | None) -> None:
        """记一次「被挡」进失败史（供轮换时排除该节点）；无失败史实现为 no-op。"""
        ...


class EnvProxyProvider:
    """默认实现：单一固定代理（FetchConfig.http_proxy）或直连。

    独立部署最常见姿势：``PAPER_FETCH_HTTP_PROXY=http://127.0.0.1:17891``。
    """

    def proxy_for_url(self, url: str | None) -> str | None:
        return get_config().http_proxy

    def enabled(self) -> bool:
        return get_config().http_proxy is not None

    async def rotate_node_for_host(
        self, host: str, tried_nodes: set[str]
    ) -> str | None:
        return None  # 单固定代理：无节点可换（链路自动放弃轮换，维持快速失败）

    def record_block(self, url_or_host: str | None) -> None:
        return None  # 无失败史


_provider: ProxyProvider = EnvProxyProvider()


def set_proxy_provider(provider: ProxyProvider) -> None:
    """宿主注入自定义出口裁决（PaperPilot 启动时调；不调就是 EnvProxyProvider）。"""
    global _provider
    _provider = provider


def get_proxy_provider() -> ProxyProvider:
    return _provider


# ---------------------------------------------------------------------------
# 便捷函数：与 PaperPilot proxy_pool_service 同名同签名，adapter 平移时只改 import。
# ---------------------------------------------------------------------------

def proxy_for_url(url: str | None) -> str | None:
    return _provider.proxy_for_url(url)


def proxy_enabled_for_download() -> bool:
    return _provider.enabled()


async def rotate_node_for_host(host: str, tried_nodes: set[str]) -> str | None:
    return await _provider.rotate_node_for_host(host, tried_nodes)


def record_block(url_or_host: str | None) -> None:
    _provider.record_block(url_or_host)


def async_client_for(url: str | None, **kwargs) -> httpx.AsyncClient:
    """统一 httpx.AsyncClient 工厂：按 URL 直连或走代理；trust_env=False 永远钉死。"""
    proxy = _provider.proxy_for_url(url)
    if proxy:
        kwargs.setdefault("proxy", proxy)
    return httpx.AsyncClient(trust_env=False, **kwargs)
