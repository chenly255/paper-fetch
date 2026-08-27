"""域名级冷却器：撞 429 / 确认反爬 403 后同域退避（R6）。

进程内状态即可（当前单 worker 部署）。多 worker 需换成 Redis 等共享存储，
否则各进程各自冷却、无法互相让路。

正常下载路径：冷却表为空时 ``is_cooling`` 只是一次字典查询，不睡眠、不改请求。
只在撞限流时写入；冷却期内同域请求直接判「暂时被挡」跳过，不再硬撞。

403 区分：免费站（``is_free_site``）的 403 当反爬冷却；订阅站 403 是付费墙，不冷却。
429 一律冷却。冷却器只应在 SSRF 校验通过之后介入，不替代 url_safety。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .robust_fetch import is_free_site

logger = logging.getLogger(__name__)

# 指数退避：首撞 30s，封顶 15 分钟；无 Retry-After 时再加 20% 抖动。
_BASE_BACKOFF_SEC = 30.0
_MAX_BACKOFF_SEC = 15 * 60.0
_JITTER_MIN = 0.8
_JITTER_MAX = 1.2


@dataclass
class _HostState:
    until_mono: float
    strikes: int = 1


@dataclass(frozen=True)
class BlockEvent:
    """一次「被挡」记录，供 download_pdf 判断要不要标 rate_limited。"""

    host: str
    retry_after_sec: float
    reason: str  # "429" | "403" | "cooling"


_lock = threading.Lock()
_hosts: dict[str, _HostState] = {}
_capture: ContextVar[list[BlockEvent] | None] = ContextVar(
    "domain_cooldown_capture", default=None
)


def reset_cooldowns() -> None:
    """测试用：清空进程内冷却表。"""
    with _lock:
        _hosts.clear()


def normalize_host(url_or_host: str | None) -> str | None:
    """取小写主机名，去掉 www. 与尾点；解不出则返 None。"""
    if not url_or_host:
        return None
    raw = url_or_host.strip().lower()
    if not raw:
        return None
    if "://" in raw:
        host = (urlsplit(raw).hostname or "").rstrip(".")
    else:
        host = raw.split("/")[0].split(":")[0].rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def remaining_cooldown_sec(url_or_host: str | None, *, now: float | None = None) -> float:
    """该域名还剩多少秒冷却；未冷却返 0。"""
    host = normalize_host(url_or_host)
    if not host:
        return 0.0
    now_m = time.monotonic() if now is None else now
    with _lock:
        state = _hosts.get(host)
        if state is None:
            return 0.0
        left = state.until_mono - now_m
        if left <= 0:
            _hosts.pop(host, None)
            return 0.0
        return left


def clear_host_cooldown(url_or_host: str | None) -> None:
    """手动清除某域名的冷却（2026-08-22 代理轮换配套）。

    场景：下载被 429 → 域名进冷却 → 用户手动重试且代理模式为 auto →
    proxy_pool_service.prepare_for_retry 先把节点切到该域未失败的出口，
    换了出口 IP 后旧冷却失去意义，清掉让本次下载真正能再试。
    """
    host = normalize_host(url_or_host)
    if not host:
        return
    with _lock:
        popped = _hosts.pop(host, None)
    if popped is not None:
        logger.info("domain_cooldown: 手动清除冷却 host=%s", host)


def is_cooling(url_or_host: str | None) -> bool:
    return remaining_cooldown_sec(url_or_host) > 0


def should_skip_url(url: str | None) -> bool:
    """冷却期内跳过该 URL。会记一条 capture，供上层标「暂时被挡」。

    必须在 SSRF 校验通过之后调用。
    """
    left = remaining_cooldown_sec(url)
    if left <= 0:
        return False
    host = normalize_host(url)
    if host:
        _record_capture(BlockEvent(host=host, retry_after_sec=left, reason="cooling"))
        logger.info("domain_cooldown: 跳过冷却中的域名 host=%s 剩余 %.1fs", host, left)
    return True


def is_antibot_block(url: str | None, status_code: int) -> bool:
    """429 一律算限流；403 仅免费站算反爬（订阅站 403 是付费墙）。"""
    if status_code == 429:
        return True
    if status_code == 403 and is_free_site(url):
        return True
    return False


def observe_http_status(
    url: str | None,
    status_code: int,
    headers: dict | None = None,
) -> bool:
    """看一次 HTTP 状态。若应冷却则写入并返 True（调用方应中止本次抓取）。

    429/反爬确认的同时通知 proxy_pool 记「域名 × 当前节点」失败史
    （延迟 import 防循环；代理未启用时是 no-op），供手动重试前的节点轮换排除。
    """
    if not is_antibot_block(url, status_code):
        return False
    retry_after = _parse_retry_after((headers or {}).get("retry-after") or (headers or {}).get("Retry-After"))
    _enter_cooldown(url, reason=str(status_code), retry_after_sec=retry_after)
    try:
        from . import proxy as proxy_pool_service

        proxy_pool_service.record_block(url)
    except Exception:  # noqa: BLE001
        pass  # 失败史是增强，绝不影响冷却主流程
    return True


def max_captured_retry_after() -> float:
    """当前 download_pdf 捕获到的最长剩余冷却（秒）；无捕获返 0。"""
    hits = _capture.get()
    if not hits:
        return 0.0
    return max(h.retry_after_sec for h in hits)


def captured_blocks() -> tuple[BlockEvent, ...]:
    hits = _capture.get()
    return tuple(hits) if hits else ()


@contextmanager
def capture_blocks() -> Iterator[list[BlockEvent]]:
    """包住一次 download_pdf：收集本轮跳过/撞限流的域名。"""
    hits: list[BlockEvent] = []
    token = _capture.set(hits)
    try:
        yield hits
    finally:
        _capture.reset(token)


def _enter_cooldown(
    url: str | None,
    *,
    reason: str,
    retry_after_sec: float | None,
) -> None:
    host = normalize_host(url)
    if not host:
        return
    with _lock:
        prev = _hosts.get(host)
        strikes = (prev.strikes + 1) if prev is not None else 1
        delay = _compute_delay(strikes, retry_after_sec)
        until = time.monotonic() + delay
        # 已有更晚的截止时间就保留（并发撞同一域时取更长的）。
        if prev is not None and prev.until_mono > until:
            until = prev.until_mono
            delay = max(delay, prev.until_mono - time.monotonic())
        _hosts[host] = _HostState(until_mono=until, strikes=strikes)
    _record_capture(BlockEvent(host=host, retry_after_sec=delay, reason=reason))
    logger.warning(
        "domain_cooldown: 域名进入冷却 host=%s reason=%s strikes=%d delay=%.1fs",
        host, reason, strikes, delay,
    )


def _compute_delay(strikes: int, retry_after_sec: float | None) -> float:
    if retry_after_sec is not None:
        return min(max(retry_after_sec, 1.0), _MAX_BACKOFF_SEC)
    exp = _BASE_BACKOFF_SEC * (2 ** max(strikes - 1, 0))
    exp = min(exp, _MAX_BACKOFF_SEC)
    return exp * random.uniform(_JITTER_MIN, _JITTER_MAX)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return float(text)
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max(0.0, (dt - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _record_capture(event: BlockEvent) -> None:
    hits = _capture.get()
    if hits is not None:
        hits.append(event)
