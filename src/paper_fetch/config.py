"""FetchConfig：paper-fetch 的显式配置对象（零 app 依赖）。

设计原则：所有可调参数集中在一个 dataclass；每个字段带环境变量默认值，
独立部署（CLI / 其他 agent 服务器）零配置可用；PaperPilot 这类宿主应用
把自己的 settings / 用户偏好翻译成 FetchConfig 传入，不落环境变量。

配置作用域：模块级 contextvar（``get_config`` / ``use_config``），download_pdf
入口用 ``use_config`` 临时切换，链内 adapter（如 scihub 读开关）统一经
``get_config()`` 读——既支持进程级默认配置，也支持每次调用换配置（多用户宿主）。
"""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

# 浏览器 UA：与各 adapter 历史默认一致（伪装真实 Chrome，防爬虫指纹拒连）。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "no", "off")


def _env_opt(name: str) -> str | None:
    val = os.environ.get(name, "").strip()
    return val or None


# ---------------------------------------------------------------------------
# 机构通道钩子（宿主注入；paper-fetch 自身不碰任何数据库/凭据存储）
# ---------------------------------------------------------------------------

# 取用户级 Elsevier 凭据：(api_key, inst_token) | None（None → 回退全局 elsevier_api_key）。
# user 是不透明对象：宿主（如 PaperPilot）自己知道怎么解读，paper-fetch 只透传。
ElsevierCredsProvider = Callable[[object], Awaitable[tuple[str, str] | None]]

# library_proxy 段入口守门：返回 (enabled, has_credential)。
#   enabled=False        → 整段跳过，终态文案用「机构代理通道已关闭」版本；
#   has_credential=False → 跳过防空转，文案走「未配置账号」版本。
# 任一为 False 时 tried_sources 不记 library_proxy（与 PaperPilot 原行为一致）。
LibraryProxyGate = Callable[[object], Awaitable[tuple[bool, bool]]]

# library_proxy 实际下载：返回 (pdf_bytes | None, reason | None)。
# reason 约定与 PaperPilot 原 library_proxy_channel 相同（no_credential /
# institutional_flow_loop / paywall_no_subscription / pdf_download_failed:* …），
# 供链尾失败分类（institutional_proxy_failed vs paywall_no_access）。
LibraryProxyFetcher = Callable[
    ..., Awaitable[tuple[bytes | None, str | None]]
]


@dataclass
class FetchConfig:
    """一次下载链运行的全部可调参数（字段默认值 = 独立部署的合理缺省）。"""

    # ---- 大小 / 预算 ----
    # PDF 大小上限（MB）：超限丢弃并返 size_limit_exceeded（上游入库前的守门）。
    # env 默认值一律 default_factory：实例化时读当前环境（import 时定格会让测试
    # 的 monkeypatch.setenv 失效——迁移时踩过）。
    max_pdf_mb: int = field(default_factory=lambda: int(os.environ.get("PAPER_FETCH_MAX_PDF_MB", "120")))
    # 下载总时间软预算（秒）：超了停止后续段、正常返回失败 dict（不被上层超时杀掉）。
    total_budget_sec: float = field(default_factory=lambda: float(os.environ.get("PAPER_FETCH_BUDGET_SEC", "75")))

    # ---- HTTP 出口 ----
    user_agent: str = DEFAULT_USER_AGENT
    # 固定 HTTP(S) 代理（如 http://127.0.0.1:7890）。None = 由 ProxyProvider 决定
    # （默认 EnvProxyProvider 读的就是这个字段，宿主可注入更聪明的按域名分流实现）。
    http_proxy: str | None = field(default_factory=lambda: _env_opt("PAPER_FETCH_HTTP_PROXY"))

    # ---- Tavily（web_pdf_discovery / 预印本发现用；无 key 自动跳过相关段）----
    # key 列表（号池轮换）：直接给 key 或给号池文件路径（JSON: {"keys": ["tvly-..."]}）。
    tavily_api_keys: list[str] = field(default_factory=list)
    tavily_keys_file: str | None = field(default_factory=lambda: _env_opt("PAPER_FETCH_TAVILY_KEYS_FILE"))
    # 兼容裸 env（独立部署最常见姿势）。
    # 注意：绝不内置任何 key；密钥文件只由部署方自己提供路径。

    # ---- Unpaywall ----
    unpaywall_email: str | None = field(default_factory=lambda: _env_opt("UNPAYWALL_EMAIL"))

    # ---- Elsevier 官方 API ----
    elsevier_api_key: str = field(default_factory=lambda: os.environ.get("PAPER_FETCH_ELSEVIER_API_KEY", ""))
    elsevier_inst_token: str = field(default_factory=lambda: os.environ.get("PAPER_FETCH_ELSEVIER_INST_TOKEN", ""))
    # 用户级凭据钩子（宿主注入；None → 只用上面的全局 key）。
    elsevier_creds_provider: ElsevierCredsProvider | None = None

    # ---- 机构图书馆代理（默认停用；宿主注入 gate + fetcher 才启用）----
    library_proxy_gate: LibraryProxyGate | None = None
    library_proxy_fetcher: LibraryProxyFetcher | None = None

    # ---- Sci-Hub（默认关；开启 = 部署方自担合规责任，见 README 免责声明）----
    scihub_enabled: bool = field(default_factory=lambda: _env_bool("PAPER_FETCH_SCIHUB_ENABLED", False))
    scihub_base_urls: str = field(default_factory=lambda: os.environ.get("PAPER_FETCH_SCIHUB_BASE_URLS", ""))
    scihub_timeout_sec: int = field(default_factory=lambda: int(os.environ.get("PAPER_FETCH_SCIHUB_TIMEOUT_SEC", "30")))

    def tavily_keys_resolved(self) -> list[str]:
        """最终 Tavily key 列表：显式列表 > 号池文件 > 裸 env（逗号分隔）。"""
        if self.tavily_api_keys:
            return [k for k in self.tavily_api_keys if k.strip()]
        keys: list[str] = []
        if self.tavily_keys_file:
            try:
                import json
                from pathlib import Path

                data = json.loads(Path(self.tavily_keys_file).read_text(encoding="utf-8"))
                keys = [k.strip() for k in data.get("keys", []) if str(k).strip()]
            except Exception:  # noqa: BLE001 —— 文件缺失/损坏按无 key 处理，不打炸下载链
                keys = []
        if not keys:
            env = os.environ.get("PAPER_FETCH_TAVILY_API_KEY", "")
            keys = [k.strip() for k in env.split(",") if k.strip()]
        return keys


# ---------------------------------------------------------------------------
# 作用域管理
# ---------------------------------------------------------------------------

_default_config: FetchConfig | None = None
_current: ContextVar[FetchConfig] = ContextVar("paper_fetch_config")


def get_config() -> FetchConfig:
    """读当前配置：优先 contextvar（download_pdf 调用作用域），否则进程级默认。"""
    try:
        return _current.get()
    except LookupError:
        pass
    global _default_config
    if _default_config is None:
        _default_config = FetchConfig()
    return _default_config


def set_default_config(config: FetchConfig) -> None:
    """设置进程级默认配置（宿主启动时调一次；download_pdf(config=...) 可按调用覆盖）。"""
    global _default_config
    _default_config = config


def use_config(config: FetchConfig | None):
    """contextvar 作用域切换（download_pdf 入口用；None 表示恢复外层配置）。

    用法::

        token = use_config(my_config)
        try:
            ...
        finally:
            use_config(None)  # 或 reset
    """
    if config is None:
        # 调用方应在 finally 里用第一次返回的 token reset；这里提供对称入口
        return None
    return _current.set(config)


def reset_config(token) -> None:
    """配合 use_config 的 reset（token 为 None 时是 no-op）。"""
    if token is not None:
        _current.reset(token)


def reset_default_config_for_tests() -> None:
    """测试用：丢弃进程级默认配置缓存（下次 get_config 按当前 env 重新构造）。"""
    global _default_config
    _default_config = None
