"""学校图书馆代理配置：institution → 带认证的 HTTP 正向代理（端口式，如复旦 libproxy Squid）。

★ 数据驱动、可扩展：配置在 `data/library_proxies.json`，要加新学校只需往那加一条
`{学校名: {proxy, auth, note, aliases}}`，不用改代码（改后重启后端即生效）。

这类代理是学校订阅 IP 的出口：请求经它转发后，出版商看到的是学校订阅 IP，据此放行付费全文。
用户账密经 institution_credential 加密存，运行时拼进代理 URL（library_proxy_adapter 负责）。

与 WebVPN（webvpn_url.py，国内多数高校）互补——WebVPN 是「网址加密改写 + 网关登录」，
这种端口式认证代理更干净：纯 HTTP、不碰验证码、不用逐个数据库点「机构登录」。少数高校
（如复旦，libproxy.fudan.edu.cn:8080，Basic 认证）提供这种代理。

⚠ 合规：学校通常禁止「用工具批量下载文献」（复旦代理的认证提示头明文写了）。所以走这条
通道的下载必须经 institution_credential_service 五道闸限速，只在用户主动「获取原文」时按需触发。
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_FILE = Path(__file__).resolve().parent / "data" / "library_proxies.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, str]:
    """规范化学校名（strip().lower()，中文 lower 恒等）→ 代理 'host:port'。含中文名 + 英文别名。"""
    out: dict[str, str] = {}
    try:
        raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        # REASON: 数据文件缺失/损坏时降级为「没有任何图书馆代理」，下载链自然走 WebVPN/CARSI，不炸。
        logger.warning("library_proxies: 读 %s 失败（%s），图书馆代理通道禁用", _DATA_FILE, exc)
        return out
    for name, info in (raw.get("schools") or {}).items():
        if not isinstance(info, dict):
            continue
        proxy = (info.get("proxy") or "").strip()
        if not proxy:
            continue
        for key in [name, *(info.get("aliases") or [])]:
            if key and str(key).strip():
                out[str(key).strip().lower()] = proxy
    return out


def get_library_proxy(institution_name: str | None) -> str | None:
    """按学校名取图书馆代理 host:port；该校没有端口式代理则返 None（走 WebVPN/CARSI）。"""
    if not institution_name:
        return None
    return _load().get(institution_name.strip().lower())


def has_library_proxy(institution_name: str | None) -> bool:
    """该校是否配了图书馆代理（供 /institutions 标注访问方式、前端可视化用）。"""
    return get_library_proxy(institution_name) is not None
