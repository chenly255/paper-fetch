"""ezproxy_url 测试：网址改写（模板式 + 追加式 + 空配置兜底）。"""
from __future__ import annotations

from paper_fetch.ezproxy_url import make_ezproxy_url


def test_template_placeholder():
    out = make_ezproxy_url(
        "https://www.sciencedirect.com/science/article/pii/S0006?via=ihub",
        "https://login.ezproxy.lib.x.edu/login?url={url}",
    )
    assert out.startswith("https://login.ezproxy.lib.x.edu/login?url=")
    # 目标被 URL 编码
    assert "https%3A%2F%2Fwww.sciencedirect.com" in out


def test_append_when_no_placeholder():
    out = make_ezproxy_url("https://nature.com/x", "https://ezproxy.lib.x.edu/login")
    assert out == "https://ezproxy.lib.x.edu/login?url=https%3A%2F%2Fnature.com%2Fx"


def test_append_with_existing_query():
    out = make_ezproxy_url("https://nature.com/x", "https://ezproxy.lib.x.edu/login?foo=1")
    assert "&url=" in out


def test_empty_returns_empty():
    assert make_ezproxy_url("", "https://ezproxy/login?url={url}") == ""
    assert make_ezproxy_url("https://x.com", "") == ""
