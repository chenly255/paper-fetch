"""SSRF 防护测试（R2-1 下载链 + url_safety 公共模块）。

覆盖：
- url_safety：内网/本机/保留 IP、非法 scheme、非 80/443 端口、账号密码、localhost/.local 一律拒绝；
  公网 IP 字面值通过；域名解析到私网 IP 拒绝（DNS rebinding 的第一道闸）；
  多地址固化优先 IPv4（评审2 F8）。
- robust_fetch：httpx / curl_cffi / 浏览器三个出口对内网地址直接放弃（返 None、不发请求）；
  httpx 段重定向逐跳校验——公网地址 302 到内网时第二跳不发请求。
- DNS 固化端到端（评审1#1/评审2 F2）：校验时返公网 IP、连接时 DNS 已被改成环回，
  httpx/curl 段的实际连接目标仍是首次验证的 IP（httpx 用 IP 字面值 + Host 头 +
  sni_hostname；curl 用 CURLOPT_RESOLVE）。
- 浏览器出口重定向拦截（评审1#2）：公网 URL 302 到环回，第二跳导航被 route.abort。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from paper_fetch import proxy as proxy_pool
from paper_fetch import url_safety
from paper_fetch import robust_fetch


# ============================================================
# url_safety 静态规则
# ============================================================
@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/admin/users",
    "http://localhost/x.pdf",
    "http://10.0.0.5:9200/",
    "http://172.16.0.1/x",
    "http://192.168.1.1/x",
    "http://169.254.169.254/latest/meta-data/iam/",
    "http://[::1]/x",
    "http://100.64.0.1/x",          # CGNAT 共享地址（is_global=False）
    "http://intranet.local/x.pdf",  # .local 内网域名
    "ftp://8.8.8.8/x.pdf",          # 非 http/https
    "http://8.8.8.8:8080/x.pdf",    # 非 80/443 端口
    "https://user:pass@8.8.8.8/x.pdf",  # URL 内嵌账号密码
])
def test_validate_public_url_rejects_unsafe(url: str):
    with pytest.raises(url_safety.UnsafeUrlError):
        url_safety.validate_public_url(url)


@pytest.mark.parametrize("url", [
    "https://8.8.8.8/x.pdf",
    "http://93.184.216.34/paper.pdf",
])
def test_validate_public_url_accepts_global_ips(url: str):
    assert url_safety.validate_public_url(url) == url


def test_resolve_public_url_sync_rejects_domain_resolving_to_private_ip(monkeypatch):
    """域名解析结果含私网 IP（DNS rebinding 场景）→ 拒绝。"""
    def fake_getaddrinfo(host, port, type=None):
        return [(None, None, None, "", ("10.1.2.3", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(url_safety.UnsafeUrlError):
        url_safety.resolve_public_url_sync("https://evil.example.com/x.pdf")


def test_pin_url_host_rewrites_host_and_keeps_port():
    pinned, host, is_https = url_safety.pin_url_host("https://example.com:443/a/b.pdf", "1.2.3.4")
    assert host == "example.com"
    assert is_https is True
    assert pinned == "https://1.2.3.4/a/b.pdf"  # 默认端口不重复出现


def test_check_addresses_prefers_ipv4_when_both_families():
    """A/AAAA 并存时固化优先返回 IPv4（评审2 F8：IPv6 可能无路由，任选会导致抓取必败）。"""
    ip = url_safety._check_addresses({"2606:4700::1111", "93.184.216.34"})
    assert ip == "93.184.216.34"


def test_check_addresses_returns_ipv6_when_only_v6():
    ip = url_safety._check_addresses({"2606:4700::1111"})
    assert ip == "2606:4700::1111"


# ============================================================
# robust_fetch 出口拒绝
# ============================================================
@pytest.mark.asyncio
async def test_httpx_get_rejects_private_url_without_request(monkeypatch):
    """内网 URL 在发请求前即被拒：SSRF 校验先行，httpx 不发请求、返 None。"""
    validated: list[str] = []

    async def spy(url):
        validated.append(url)
        return await url_safety.resolve_public_url(url)

    monkeypatch.setattr(robust_fetch, "resolve_public_url", spy)

    class _Client:
        requested: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None, extensions=None):
            _Client.requested.append(url)

    with patch.object(proxy_pool.httpx, "AsyncClient", lambda **kw: _Client()):
        result = await robust_fetch._httpx_get(
            "http://169.254.169.254/latest/meta-data/", None
        )

    assert result is None
    assert validated == ["http://169.254.169.254/latest/meta-data/"]
    # 校验失败发生在发出请求之前——从未真正 GET 内网地址
    assert _Client.requested == []


@pytest.mark.asyncio
async def test_httpx_get_validates_each_redirect_hop(monkeypatch):
    """公网地址 302 → 内网：第二跳不发请求，整体返 None。"""
    class _Resp:
        def __init__(self, status_code, location=None):
            self.status_code = status_code
            self.headers = {"location": location} if location else {}
            self.content = b""

    class _Client:
        calls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None, extensions=None):
            _Client.calls.append(url)
            return _Resp(302, location="http://127.0.0.1:8000/secret")

    monkeypatch.setattr(
        proxy_pool.httpx, "AsyncClient", lambda **kwargs: _Client()
    )
    # 用公网 IP 字面值首跳（校验通过），302 跳内网
    result = await robust_fetch._httpx_get("https://8.8.8.8/a.pdf", None)

    assert result is None
    assert _Client.calls == ["https://8.8.8.8/a.pdf"]  # 第二跳（内网）没发出


@pytest.mark.asyncio
async def test_curl_cffi_get_rejects_private_url_without_request():
    """curl 段同样在发请求前拒绝内网地址。"""
    with patch("curl_cffi.requests.Session") as fake_session_cls:
        result = await robust_fetch._curl_cffi_get("http://10.0.0.5:9200/", None)
    assert result is None
    fake_session_cls.assert_not_called()


@pytest.mark.asyncio
async def test_browser_get_rejects_private_url():
    """浏览器段：内网 PDF URL 不进浏览器（BrowserSession 不启动）。"""
    with patch(
        "paper_fetch.browser_session.BrowserSession"
    ) as fake_session_cls:
        result = await robust_fetch._browser_get("http://192.168.1.10/secret.pdf", None)
    assert result is None
    fake_session_cls.assert_not_called()


# ============================================================
# DNS 固化端到端（评审1#1/评审2 F2）：校验返公网 IP，连接时 DNS 已被改指环回
# ============================================================
_PINNED_IP = "93.184.216.34"


@pytest.mark.asyncio
async def test_httpx_pins_verified_ip_even_if_dns_rebinds(monkeypatch):
    """httpx 段端到端：连接目标是首次验证的 IP 字面值，不再做连接时 DNS 解析。

    模拟 rebinding：校验时 resolve_public_url 返公网 IP；把 socket.getaddrinfo 改成
    返 127.0.0.1——若实现回退到「用域名连接」，httpx 会对 example.com 做 DNS 解析
    （拿到环回）或 MockTransport 里 host 仍是域名，断言即红。
    """
    dns_lookups: list[str] = []

    def rebinding_getaddrinfo(host, *args, **kwargs):
        dns_lookups.append(host)
        return [(None, None, None, "", ("127.0.0.1", 443))]

    async def fake_resolve(url):
        return (url, _PINNED_IP)

    monkeypatch.setattr("socket.getaddrinfo", rebinding_getaddrinfo)
    monkeypatch.setattr(robust_fetch, "resolve_public_url", fake_resolve)

    served: list[tuple[str, str | None]] = []

    def handler(request):
        served.append((str(request.url.host), request.headers.get("host")))
        return proxy_pool.httpx.Response(200, content=b"%PDF-1.7 pinned")

    real_client = proxy_pool.httpx.AsyncClient

    def factory(**kwargs):
        return real_client(transport=proxy_pool.httpx.MockTransport(handler), **kwargs)

    with patch.object(proxy_pool.httpx, "AsyncClient", factory):
        content = await robust_fetch._httpx_get("https://example.com/a.pdf", None)

    assert content == b"%PDF-1.7 pinned"
    # 连接 URL 的主机是固化 IP、Host 头是原域名（SNI 也按原域名，见 pin_url_host）
    assert served == [(_PINNED_IP, "example.com")]
    # 连接期没有对原域名做二次 DNS 解析（rebinding 无窗口）
    assert dns_lookups == []


@pytest.mark.asyncio
async def test_curl_pins_verified_ip_via_resolve_option(monkeypatch):
    """curl_cffi 段：CURLOPT_RESOLVE 把已验证 IP 固化进本次会话 DNS，不再二次解析。"""
    def fake_resolve_sync(url):
        return (url, _PINNED_IP)

    monkeypatch.setattr(robust_fetch, "resolve_public_url_sync", fake_resolve_sync)

    sessions: list[dict] = []
    gets: list[str] = []

    class _Resp:
        status_code = 200
        headers: dict = {}
        content = b"%PDF-1.7 pinned"

    class _FakeSession:
        def __init__(self, *, curl_options=None):
            sessions.append(dict(curl_options or {}))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            gets.append(url)
            return _Resp()

    with patch("curl_cffi.requests.Session", _FakeSession):
        content = await robust_fetch._curl_cffi_get("https://example.com/a.pdf", None)

    assert content == b"%PDF-1.7 pinned"
    # 请求 URL 保持域名不变，靠 RESOLVE 选项固化 80/443 两个端口的映射
    from curl_cffi import CurlOpt

    assert gets == ["https://example.com/a.pdf"]
    resolve_entries = sessions[0][CurlOpt.RESOLVE]
    assert "example.com:443:93.184.216.34" in resolve_entries
    assert "example.com:80:93.184.216.34" in resolve_entries


# ============================================================
# 浏览器出口重定向拦截（评审1#2）
# ============================================================
@pytest.mark.asyncio
async def test_browser_redirect_to_loopback_second_hop_aborted(monkeypatch):
    """公网 URL 302 → 环回：第二跳导航请求被 route.abort，浏览器不去连 127.0.0.1。"""
    async def fake_resolve(url):
        if "127.0.0.1" in url:
            raise url_safety.UnsafeUrlError("loopback")
        return (url, _PINNED_IP)

    monkeypatch.setattr(robust_fetch, "resolve_public_url", fake_resolve)

    events: list[tuple[str, str]] = []

    class _Req:
        def __init__(self, url: str, *, navigation: bool = True):
            self.url = url
            self._navigation = navigation

        def is_navigation_request(self) -> bool:
            return self._navigation

    class _Route:
        def __init__(self, request: _Req):
            self.request = request

        async def abort(self):
            events.append(("abort", self.request.url))

        async def continue_(self):
            events.append(("continue", self.request.url))

    class _FakeAPIRequest:
        async def get(self, url, timeout=None, max_redirects=None):
            class _R:
                async def body(self):
                    return b"%PDF-1.7 guarded"

            return _R()

    class _FakeContext:
        def __init__(self):
            self.request = _FakeAPIRequest()
            self.handlers: list = []

        async def route(self, pattern, handler):
            self.handlers.append(handler)

    class _FakePage:
        def __init__(self, context: _FakeContext):
            self._ctx = context

        async def goto(self, url, wait_until=None):
            # 模拟服务器 302：公网第一跳导航放行后，重定向到环回的第二跳
            # 导航请求同样流经 context 路由（评审1#2 的攻击面）。
            for nav_url in (url, "http://127.0.0.1:8000/evil"):
                for handler in self._ctx.handlers:
                    await handler(_Route(_Req(nav_url)))

        async def wait_for_timeout(self, ms):
            return None

    class _FakeSession:
        def __init__(self):
            self.context = _FakeContext()
            self.page = _FakePage(self.context)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch(
        "paper_fetch.browser_session.BrowserSession",
        lambda **kwargs: _FakeSession(),  # 生产代码现在传 proxy_server=…（出口判定）
    ):
        content = await robust_fetch._browser_get("https://example.com/a.pdf", None)

    assert content == b"%PDF-1.7 guarded"
    # 公网跳放行、环回跳被中止——回滚（未装路由拦截）时 events 为空，断言红。
    assert ("continue", "https://example.com/a") in events  # 导航页（.pdf 已剥）
    assert ("abort", "http://127.0.0.1:8000/evil") in events


@pytest.mark.asyncio
async def test_browser_context_get_blocks_redirect_to_loopback(monkeypatch):
    """context.request.get 不经 route：公网 302 到 127.0.0.1 必须在第二跳校验处被拦。"""

    async def fake_resolve(url):
        if "127.0.0.1" in url:
            raise url_safety.UnsafeUrlError("loopback")
        return (url, _PINNED_IP)

    monkeypatch.setattr(robust_fetch, "resolve_public_url", fake_resolve)

    calls: list[tuple[str, int | None]] = []

    class _Resp:
        def __init__(self, status, location=None, body=b"%PDF-1.7 leaked"):
            self.status = status
            self.headers = {"location": location} if location else {}
            self._body = body

        async def body(self):
            return self._body

    class _API:
        async def get(self, url, timeout=None, max_redirects=None):
            calls.append((url, max_redirects))
            if "127.0.0.1" in url:
                return _Resp(200, body=b"%PDF-1.7 leaked")
            return _Resp(302, location="http://127.0.0.1/secret.pdf")

    class _Ctx:
        request = _API()

    result = await robust_fetch._browser_context_get(_Ctx(), "https://8.8.8.8/a.pdf")

    assert result is None
    assert calls == [("https://8.8.8.8/a.pdf", 0)]  # 第二跳没发出


# ============================================================
# meta_adapter / browser_fetch_adapter 出口收口（审计修复：这两段
# 此前未接 url_safety——用户可控 paper_url 可直连内网/云元数据）
# ============================================================
from paper_fetch import browser_fetch_adapter  # noqa: E402
from paper_fetch import meta_adapter  # noqa: E402


@pytest.mark.asyncio
async def test_meta_fetch_html_rejects_private_url_without_request():
    """meta 段入口校验：内网 landing URL 在发请求前即被拒，httpx 客户端不创建。"""
    with patch.object(
        proxy_pool.httpx, "AsyncClient", side_effect=AssertionError("不应发请求")
    ):
        html, pdf, status, final = await meta_adapter._fetch_html(
            "http://169.254.169.254/latest/meta-data/"
        )
    assert (html, pdf, status, final) == (None, None, None, None)


@pytest.mark.asyncio
async def test_meta_fetch_html_validates_each_redirect_hop():
    """meta 段重定向逐跳校验：公网 landing 302 到环回，第二跳不发请求。"""

    class _Resp:
        def __init__(self, status_code, location=None):
            self.status_code = status_code
            self.headers = {"location": location} if location else {}
            self.content = b""
            self.text = ""

        def raise_for_status(self):
            return None

    class _Client:
        calls: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None, extensions=None):
            _Client.calls.append(url)
            return _Resp(302, location="http://127.0.0.1:8000/secret")

    with patch.object(proxy_pool.httpx, "AsyncClient", lambda **kw: _Client()):
        html, pdf, status, final = await meta_adapter._fetch_html("https://8.8.8.8/a")

    assert (html, pdf) == (None, None)
    assert _Client.calls == ["https://8.8.8.8/a"]  # 内网第二跳没发出


@pytest.mark.asyncio
async def test_browser_fetch_adapter_rejects_private_landing_without_browser():
    """browser 兜底段入口校验：内网 landing 不启动浏览器，且不消耗浏览器预算。"""
    budget = robust_fetch.FetchBudget(browser=1)
    with patch(
        "paper_fetch.browser_session.BrowserSession"
    ) as fake_session_cls:
        result = await browser_fetch_adapter.fetch_via_browser_landing(
            "http://192.168.1.10/landing", budget=budget
        )
    assert result is None
    fake_session_cls.assert_not_called()
    assert budget.browser == 1  # 入口拒绝不扣预算


@pytest.mark.asyncio
async def test_browser_fetch_adapter_dom_pdf_url_to_internal_blocked(monkeypatch):
    """活 DOM 读出的 PDF 链接完全可控：指向云元数据/内网时必须被逐跳校验拦下。"""

    async def fake_resolve(url):
        if "169.254.169.254" in url:
            raise url_safety.UnsafeUrlError("link-local")
        return (url, _PINNED_IP)

    monkeypatch.setattr(robust_fetch, "resolve_public_url", fake_resolve)

    api_calls: list[str] = []

    class _FakeAPIRequest:
        async def get(self, url, timeout=None, max_redirects=None):
            api_calls.append(url)

            class _R:
                async def body(self):
                    return b"%PDF-1.7 leaked"

            return _R()

    class _FakeContext:
        def __init__(self):
            self.request = _FakeAPIRequest()
            self.handlers: list = []

        async def route(self, pattern, handler):
            self.handlers.append(handler)

    class _FakePage:
        async def goto(self, url, wait_until=None):
            return None

        async def wait_for_timeout(self, ms):
            return None

        async def evaluate(self, script):
            return "http://169.254.169.254/latest/meta-data/iam/"

    class _FakeSession:
        def __init__(self):
            self.context = _FakeContext()
            self.page = _FakePage()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    with patch(
        "paper_fetch.browser_session.BrowserSession",
        lambda **kwargs: _FakeSession(),
    ):
        result = await browser_fetch_adapter.fetch_via_browser_landing(
            "https://example.com/landing", budget=robust_fetch.FetchBudget(browser=1)
        )

    assert result is None
    assert api_calls == []  # 内网 PDF 链接一次请求都没发出
