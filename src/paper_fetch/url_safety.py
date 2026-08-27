"""公共 URL 安全校验（防 SSRF）+ 校验后 IP 固化（防 DNS rebinding）。

抽自 web_clip_service.validate_public_url（2026-08-14 R2 安全审查第 1/3 条），
供 web_clip 与论文下载链（paper_download/robust_fetch）共用同一套规则：

- scheme 限定 http/https；
- 端口限定 80/443；
- 拒绝 localhost / *.local / URL 内嵌账号密码；
- 解析主机名后逐 IP 校验 ``ipaddress.is_global``（拒绝 127.0.0.1 / 10.x / 172.16-31.x /
  192.168.x / 169.254.x / ::1 等本机与内网地址）。

DNS rebinding（TOCTOU）：校验时解析的 IP 与请求时连接的 IP 不是同一次解析。本模块的
``resolve_public_url`` 在校验通过的同时返回解析到的公网 IP 字面值，调用方把该 IP 固化进
实际请求（httpx 用 ``sni_hostname`` 扩展 + Host 头；curl 用 CURLOPT_RESOLVE），消除
「校验与请求之间二次 DNS 解析」的时间窗口。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

# 允许跟随的最大重定向跳数（逐跳校验用）。与 web_clip._MAX_REDIRECTS 同值。
MAX_SAFE_REDIRECTS = 5


class UnsafeUrlError(ValueError):
    """地址可能访问本机或内网，或格式/端口不允许外访。"""


class UrlResolveError(RuntimeError):
    """无法解析主机名。"""


def _parse_and_check(url: str) -> tuple[str, str, int]:
    """纯静态校验：返回 (host, scheme, port)。不解析 DNS。"""
    value = (url or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeUrlError("链接格式不正确") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("只支持公开的 HTTP 或 HTTPS 链接")
    if parsed.username or parsed.password or port not in {80, 443}:
        raise UnsafeUrlError("链接包含不允许的账号信息或端口")

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".local"):
        raise UnsafeUrlError("不能读取本机或内网地址")
    return host, parsed.scheme, port


def _check_addresses(addresses: set[str]) -> str:
    """逐 IP 校验 is_global；全部通过返回其中一个 IP（供固化）。

    多地址取舍：优先返回 IPv4——部分域名的 AAAA 记录在本机无 IPv6 路由时不可达，
    任选到它会导致固化后抓取必败且无回退（评审2 F8）。只有 v6 时才返回 v6。
    """
    if not addresses:
        raise UrlResolveError("无法解析网页地址")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise UnsafeUrlError("不能读取本机或内网地址")
    for address in addresses:
        if "." in address:  # IPv4 字面值含点、IPv6 含冒号
            return address
    return next(iter(addresses))


async def resolve_public_url(url: str) -> tuple[str, str]:
    """校验 URL 并返回 (原 URL, 通过校验的公网 IP 字面值)。

    抛 ``UnsafeUrlError``（不允许的地址）或 ``UrlResolveError``（DNS 解析失败）。
    """
    host, _, port = _parse_and_check(url)
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = {record[4][0] for record in records}
        except OSError as exc:
            raise UrlResolveError("无法解析网页地址") from exc
    return (url or "").strip(), _check_addresses(addresses)


def resolve_public_url_sync(url: str) -> tuple[str, str]:
    """同步版 ``resolve_public_url``（给线程池里跑的 curl 段用）。"""
    host, _, port = _parse_and_check(url)
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = {record[4][0] for record in records}
        except OSError as exc:
            raise UrlResolveError("无法解析网页地址") from exc
    return (url or "").strip(), _check_addresses(addresses)


def validate_public_url(url: str) -> str:
    """向后兼容的同步纯校验（不固化 IP）：通过返回原 URL，否则抛 UnsafeUrlError。

    仅供不需要 IP 固化的轻量场景；需要防 DNS rebinding 的抓取路径请用
    ``resolve_public_url`` / ``resolve_public_url_sync``。
    """
    host, _, port = _parse_and_check(url)
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        try:
            records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = {record[4][0] for record in records}
        except OSError as exc:
            raise UnsafeUrlError("无法解析网页地址") from exc
    if not addresses:
        raise UnsafeUrlError("无法解析网页地址")
    for address in addresses:
        if not ipaddress.ip_address(address).is_global:
            raise UnsafeUrlError("不能读取本机或内网地址")
    return (url or "").strip()


def pin_url_host(url: str, ip: str) -> tuple[str, str, bool]:
    """把 URL 的主机名替换为已校验 IP 字面值，返回 (连接用 URL, 原主机名, 是否 HTTPS)。

    请求发给连接用 URL（不再二次 DNS），同时：
    - httpx：Host 头设原主机名，HTTPS 时传 ``extensions={"sni_hostname": host}``
      （SNI 与证书校验仍按原域名进行，不影响正常网站抓取）；
    - curl：用 CURLOPT_RESOLVE 固化，无需改写 URL（见调用方）。
    IPv6 字面值会自动加方括号。
    """
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    netloc_host = f"[{ip}]" if ":" in ip else ip
    port = parsed.port
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{netloc_host}:{port}"
    else:
        netloc = netloc_host
    pinned = urlsplit(url)._replace(netloc=netloc).geturl()
    return pinned, host, parsed.scheme == "https"
