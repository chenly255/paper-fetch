"""paper-fetch 测试基础设施：零数据库、零真网络（mock/respx 风格，与原 PaperPilot 测试同款）。

- Tavily 号池默认为空：相关测试不依赖任何真实 key 文件，也不会误打真网络；
  需要测「号池有号」的用例自己 patch tavily_client。
- domain_cooldown 是进程内全局状态，测试间必须清空。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_tavily_pool(monkeypatch):
    """默认让 Tavily 号池为空：has_keys() 返 False、相关段自动跳过。"""
    from paper_fetch import tavily_client
    from paper_fetch.config import reset_default_config_for_tests

    monkeypatch.delenv("PAPER_FETCH_TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("PAPER_FETCH_TAVILY_KEYS_FILE", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_KEYS_FILE", raising=False)
    reset_default_config_for_tests()
    tavily_client.reset_pool_for_tests()
    yield
    reset_default_config_for_tests()
    tavily_client.reset_pool_for_tests()


@pytest.fixture(autouse=True)
def _reset_domain_cooldowns():
    """R6 冷却表是进程内全局状态，测试之间必须清空，避免限流状态泄漏。"""
    from paper_fetch.domain_cooldown import reset_cooldowns

    reset_cooldowns()
    yield
    reset_cooldowns()


@pytest.fixture(autouse=True)
def _reset_proxy_provider():
    """每个测试后恢复默认 EnvProxyProvider（有的测试会注入自定义 provider）。"""
    from paper_fetch import proxy

    yield
    proxy.set_proxy_provider(proxy.EnvProxyProvider())
