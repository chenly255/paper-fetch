"""可选适配器（extras.py）测试：合规敏感段拆出主仓后的插槽语义。

- 未安装适配器：load_optional_adapter 返 None；主链 scihub_enabled=True 也整段跳过
  （不 emit、不 tried、不炸）——「开了也走空」的拆分承诺。
- PAPER_FETCH_EXTRA_ADAPTERS 指向文件/目录：动态加载成功，adapter 内相对 import
  （from .config import ...）照常工作。
- 路径不存在 / 语法坏 / 缺目标函数：只打日志返 None，不打死主链。
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 与 test_paper_download_service 同款中性 landing_info
_NL = {"url": None, "publisher": None, "requires_auth": False}

_FAKE_ADAPTER = '''\
"""测试用假适配器：验证动态加载后相对 import 照常工作。"""
from .config import get_config


def _config_visible() -> bool:
    return get_config() is not None


async def fetch_via_scihub(doi):
    return b"%PDF-from-extra-adapter"
'''


@pytest.fixture(autouse=True)
def _clean_dynamic_module(monkeypatch):
    """动态加载会注册 paper_fetch.scihub_adapter 进 sys.modules，测试间必须清掉，
    否则第一步 import 命中残留、后续「未安装」用例误红。"""
    monkeypatch.delenv("PAPER_FETCH_EXTRA_ADAPTERS", raising=False)
    sys.modules.pop("paper_fetch.scihub_adapter", None)
    yield
    sys.modules.pop("paper_fetch.scihub_adapter", None)


# ---------- 加载器单元 ----------


def test_load_returns_none_when_adapter_absent():
    """主包无该模块 + env 未设 → None（主仓拆分后的默认形态）。"""
    from paper_fetch.extras import load_optional_adapter

    assert load_optional_adapter("scihub_adapter", "fetch_via_scihub") is None


def test_service_slot_is_none_in_pristine_env():
    """service 模块级插槽在干净环境下应为 None（适配器不随主仓分发）。"""
    from paper_fetch import service

    assert service.fetch_via_scihub is None


@pytest.mark.asyncio
async def test_load_from_env_file(tmp_path):
    """env 指向具体文件：加载成功、可调用、相对 import 工作。"""
    from paper_fetch.extras import load_optional_adapter

    f = tmp_path / "scihub_adapter.py"
    f.write_text(_FAKE_ADAPTER, encoding="utf-8")

    import os

    old = os.environ.get("PAPER_FETCH_EXTRA_ADAPTERS")
    os.environ["PAPER_FETCH_EXTRA_ADAPTERS"] = str(f)
    try:
        entry = load_optional_adapter("scihub_adapter", "fetch_via_scihub")
        assert entry is not None
        assert await entry("10.1/x") == b"%PDF-from-extra-adapter"
        # 相对 import 在动态加载下照常解析（模块注册名 paper_fetch.scihub_adapter）
        mod = sys.modules["paper_fetch.scihub_adapter"]
        assert mod._config_visible() is True
    finally:
        del os.environ["PAPER_FETCH_EXTRA_ADAPTERS"]
        if old is not None:
            os.environ["PAPER_FETCH_EXTRA_ADAPTERS"] = old


@pytest.mark.asyncio
async def test_load_from_env_dir(tmp_path, monkeypatch):
    """env 指向目录：按 <module_name>.py 找。"""
    from paper_fetch.extras import load_optional_adapter

    d = tmp_path / "adapters"
    d.mkdir()
    (d / "scihub_adapter.py").write_text(_FAKE_ADAPTER, encoding="utf-8")
    monkeypatch.setenv("PAPER_FETCH_EXTRA_ADAPTERS", str(d))

    entry = load_optional_adapter("scihub_adapter", "fetch_via_scihub")
    assert entry is not None
    assert await entry(None) == b"%PDF-from-extra-adapter"


def test_load_missing_path_returns_none(tmp_path, monkeypatch):
    """env 指向不存在的路径：返 None 不抛（附加件缺位不能打死主链）。"""
    from paper_fetch.extras import load_optional_adapter

    monkeypatch.setenv("PAPER_FETCH_EXTRA_ADAPTERS", str(tmp_path / "nope"))
    assert load_optional_adapter("scihub_adapter", "fetch_via_scihub") is None


def test_load_broken_file_returns_none(tmp_path, monkeypatch):
    """语法坏的文件：返 None 不抛。"""
    from paper_fetch.extras import load_optional_adapter

    f = tmp_path / "scihub_adapter.py"
    f.write_text("def (broken syntax!!", encoding="utf-8")
    monkeypatch.setenv("PAPER_FETCH_EXTRA_ADAPTERS", str(f))
    assert load_optional_adapter("scihub_adapter", "fetch_via_scihub") is None
    assert "paper_fetch.scihub_adapter" not in sys.modules


def test_load_wrong_attr_returns_none(tmp_path, monkeypatch):
    """文件合法但没有目标函数：视为未安装返 None。"""
    from paper_fetch.extras import load_optional_adapter

    f = tmp_path / "scihub_adapter.py"
    f.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.setenv("PAPER_FETCH_EXTRA_ADAPTERS", str(f))
    assert load_optional_adapter("scihub_adapter", "fetch_via_scihub") is None


# ---------- 主链集成：未装适配器时该段跳过 ----------


@pytest.mark.asyncio
async def test_scihub_stage_skipped_when_adapter_absent(monkeypatch):
    """开关显式打开但适配器未安装：链正常跑完、tried_sources 不含 scihub、
    不向 on_stage emit scihub——「开了也走空」，不是报错。"""
    from paper_fetch.config import get_config
    from paper_fetch import service as svc

    monkeypatch.setattr(get_config(), "scihub_enabled", True)
    # 预算清零也无妨：scihub 段豁免预算，跳过只可能因适配器缺席
    monkeypatch.setattr(get_config(), "total_budget_sec", 0)
    # 显式把插槽置 None（干净环境下本就是 None；防本机 env 影响，双保险）
    monkeypatch.setattr(svc, "fetch_via_scihub", None)

    stages: list[str] = []

    async def on_stage(name: str) -> None:
        stages.append(name)

    with patch(
        "paper_fetch.service._resolve_published_doi_for_download",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_oa_pdf", AsyncMock(return_value=None)
    ), patch(
        "paper_fetch.service.probe_oa",
        AsyncMock(return_value=(None, [], False)),
    ), patch(
        "paper_fetch.service.is_elsevier_target",
        MagicMock(return_value=False),
    ), patch(
        "paper_fetch.service.fetch_publisher_direct",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_landing_page",
        AsyncMock(return_value=(None, None, _NL)),
    ), patch(
        "paper_fetch.service.fetch_via_unpaywall",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_crossref",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_europe_pmc",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.fetch_via_browser_landing",
        AsyncMock(return_value=None),
    ), patch(
        "paper_fetch.service.can_discover_pdf_via_web",
        MagicMock(return_value=False),
    ):
        result = await svc.download_pdf(
            doi="10.1234/paywalled", paper_url=None, oa_url=None, on_stage=on_stage
        )

    assert result["success"] is False
    assert "scihub" not in result["tried_sources"]
    assert "scihub" not in stages
