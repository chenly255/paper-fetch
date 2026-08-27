"""可选适配器加载器：主仓自带适配器优先，第三方附加件经 env 注入，缺位跳过。

背景：scihub 适配器随主仓分发、默认关（2026-08-27 Lily 拍板公开含之）；本加载器同时支持第三方附加件注入。
主链对它们只保留「插槽」：适配器在场就挂载，不在场整段跳过——即使部署方把开关
打开（PAPER_FETCH_SCIHUB_ENABLED=1），适配器缺席时该段也走空，不报错。

load_optional_adapter(module_name, attr_name) 的查找顺序：
1. 主包命名空间已有同名模块——部署方把 <module_name>.py 放回 src/paper_fetch/
   （或 site-packages 的 paper_fetch/ 下）即命中，正常 import；
2. 环境变量 ``PAPER_FETCH_EXTRA_ADAPTERS`` 指向的目录或文件——目录下找
   ``<module_name>.py``，文件直接用；以 ``paper_fetch.<module_name>`` 注册进
   sys.modules 后 exec，adapter 内部的相对 import（``from .config import ...``）
   照常工作；
3. 都没有 → 返回 None，调用方把该段当「未安装」跳过。

失败语义：路径不存在 / 文件语法错 / exec 抛错一律只打日志、返回 None——附加段缺位
不能打死主链。env 指向的文件由部署方自己提供，指向即视为信任（与 pip 装包同级信任）。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 指向「私有附加适配器」目录/文件的环境变量（见 README「合规边界」节）。
EXTRA_ADAPTERS_ENV = "PAPER_FETCH_EXTRA_ADAPTERS"


def load_optional_adapter(module_name: str, attr_name: str) -> Any | None:
    """按模块说明的三级顺序找可选适配器入口函数；找不到返 None。

    参数：
        module_name — 适配器模块名（如 "scihub_adapter"，文件名须为 <module_name>.py）
        attr_name   — 要取出的入口协程函数名（如 "fetch_via_scihub"）
    """
    full_name = f"paper_fetch.{module_name}"

    # ① 主包内已装（放回 src/paper_fetch/ 或 pip 装了附加包；也覆盖 sys.modules 命中）
    try:
        mod = importlib.import_module(f".{module_name}", package="paper_fetch")
    except ImportError:
        pass
    else:
        entry = getattr(mod, attr_name, None)
        if entry is None:
            logger.warning("extras: %s 里没有 %s，视为未安装", full_name, attr_name)
        return entry

    # ② env 指向的目录/文件
    raw = os.environ.get(EXTRA_ADAPTERS_ENV, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_dir():
        p = p / f"{module_name}.py"
    if not p.is_file():
        logger.warning("extras: %s=%s 找不到 %s，跳过可选段", EXTRA_ADAPTERS_ENV, raw, p)
        return None

    spec = importlib.util.spec_from_file_location(full_name, p)
    if spec is None or spec.loader is None:
        logger.warning("extras: %s 无法构造加载 spec，跳过可选段", p)
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod  # 注册后模块内相对 import 才能解析
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # noqa: BLE001 —— 附加件坏不能打死主链
        sys.modules.pop(full_name, None)
        logger.warning("extras: 加载 %s 失败（%s），跳过可选段", p, exc)
        return None
    entry = getattr(mod, attr_name, None)
    if entry is None:
        logger.warning("extras: %s 里没有 %s，视为未安装", p, attr_name)
    return entry
