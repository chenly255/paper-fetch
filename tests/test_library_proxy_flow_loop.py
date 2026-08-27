"""机构代理 302 循环治理测试（2026-08-23 nature.com 事故 c）。

覆盖：
  A. _get_with_loop_guard：authorize→transit→cookies_not_supported 循环 → 第 3 次命中
     同一 host+path 立即抛 InstitutionalFlowLoop（不等 1800s 整体超时）；
     正常重定向链（WAYF→IdP→回跳→200）正常放行；query 不同但 path 相同也算循环。
  B. fetch_via_library_proxy：循环场景返 (None, "institutional_flow_loop")，快速失败。
  C. library_proxy_channel：flow_loop 不记账号失败（不进冷却）；真实失败第 3 次
     触发冷却 + 发站内通知（institution_credential_cooldown）。

网络一律 mock，不碰真实代理/账号。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class _Resp:
    """轻量 httpx.Response 替身。"""

    def __init__(self, status_code: int, location: str | None = None, url: str = "https://x/"):
        self.status_code = status_code
        self.headers = {"location": location} if location else {}
        self.url = url
        self.content = b""
        self.text = ""


class _LoopClient:
    """模拟 nature.com cookies_not_supported 循环的 client。

    无条件按 302 序列打转：authorize → transit → cookies_not_supported → authorize → …
    """

    def __init__(self):
        self.calls: list[str] = []

    async def get(self, url: str, headers=None):  # noqa: ANN001
        self.calls.append(url)
        seq = ("authorize", "transit", "cookies_not_supported")
        for i, part in enumerate(seq):
            if part in url:
                nxt = seq[(i + 1) % len(seq)]
                return _Resp(302, location=f"https://idp.nature.com/{nxt}?state=xyz", url=url)
        return _Resp(302, location="https://idp.nature.com/authorize?state=xyz", url=url)


class _NormalSsoClient:
    """正常机构 SSO 链：landing → WAYF 302 → IdP 302 → 回跳（同 path，第 2 次命中）→ 200。"""

    def __init__(self):
        self.calls: list[str] = []

    async def get(self, url: str, headers=None):  # noqa: ANN001
        self.calls.append(url)
        if "entitled=1" in url:  # SSO 回跳 → 放行 200（注意先于 articles 分支判断）
            return _Resp(200, url=url)
        if "nature.com/articles" in url:
            return _Resp(302, location="https://wayf.nature.com/authorize?return=x", url=url)
        if "wayf.nature.com" in url:
            return _Resp(
                302, location="https://idp.fudan.edu.cn/idp/profile/SAML2?SAMLRequest=q", url=url
            )
        if "idp.fudan.edu.cn" in url:
            return _Resp(302, location="https://nature.com/articles/s41586?entitled=1", url=url)
        return _Resp(200, url=url)


# ---------------------------------------------------------------------------
# A/B. adapter 层
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_with_loop_guard_循环快速失败():
    from paper_fetch.library_proxy_adapter import (
        InstitutionalFlowLoop,
        _get_with_loop_guard,
    )

    client = _LoopClient()
    with pytest.raises(InstitutionalFlowLoop):
        await _get_with_loop_guard(
            client, "https://nature.com/articles/s41586", {}
        )
    # 3 轮 authorize（每轮 3 跳）内即放弃：3+3+3=9 跳 < _MAX_REDIRECT_HOPS
    assert len(client.calls) <= 10


@pytest.mark.asyncio
async def test_get_with_loop_guard_正常SSO链放行():
    from paper_fetch.library_proxy_adapter import _get_with_loop_guard

    client = _NormalSsoClient()
    resp = await _get_with_loop_guard(client, "https://nature.com/articles/s41586", {})
    assert resp.status_code == 200
    assert len(client.calls) == 4  # landing→WAYF→IdP→回跳 200


@pytest.mark.asyncio
async def test_get_with_loop_guard_query不同path相同也判循环():
    """authorize 的 state 参数每轮都变——循环键必须忽略 query，否则漏判。"""
    from paper_fetch.library_proxy_adapter import (
        InstitutionalFlowLoop,
        _get_with_loop_guard,
    )

    class _ChangingStateClient:
        def __init__(self):
            self.n = 0

        async def get(self, url: str, headers=None):  # noqa: ANN001
            self.n += 1
            return _Resp(302, location=f"https://nature.com/authorize?state={self.n}", url=url)

    with pytest.raises(InstitutionalFlowLoop):
        await _get_with_loop_guard(_ChangingStateClient(), "https://nature.com/start", {})


@pytest.mark.asyncio
async def test_fetch_via_library_proxy_循环场景返flow_loop():
    """302 循环 → (None, 'institutional_flow_loop')，且绝不等到 1800s 整体超时。"""
    from paper_fetch import library_proxy_adapter as lpa

    fake_client = _LoopClient()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch.object(lpa.httpx, "AsyncClient", MagicMock(return_value=ctx)):
        pdf, reason = await lpa.fetch_via_library_proxy(
            doi="10.1038/s41586-024-12345-6",
            landing_url="https://nature.com/articles/s41586",
            username="u",
            password="p",
            proxy_host_port="libproxy.example.edu:8080",
        )
    assert pdf is None
    assert reason == "institutional_flow_loop"


@pytest.mark.asyncio
async def test_classify_failure_循环归类institutional_proxy_failed():
    """下载链失败分类：flow_loop → institutional_proxy_failed + 循环专属中文文案。"""
    from paper_fetch.service import _classify_failure

    detail, message = _classify_failure(
        tried=["library_proxy"],
        auth_required=True,
        library_proxy_reason="institutional_flow_loop",
    )
    assert detail == "institutional_proxy_failed"
    assert "重定向循环" in message


# ---------------------------------------------------------------------------
# C. channel 层记账 + 通知
# ---------------------------------------------------------------------------


# 注：C 部分（library_proxy_channel 的 DB 记账/通知测试）依赖 PaperPilot 数据库与
# 凭据服务，留在 PaperPilot 仓库跑；本仓库只测纯 adapter 部分（A/B）。

