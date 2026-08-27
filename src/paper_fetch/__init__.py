"""paper-fetch：论文 PDF 下载链（独立可分发版，本体从 PaperPilot 迁出）。

对外公共 API：
  - ``download_pdf(...)``  合法来源优先的多级降级下载链（正式版优先 → 预印本回落）
  - ``FetchConfig``        显式配置对象（超时/代理/Tavily key/各开关）
  - ``set_proxy_provider`` 注入自定义代理出口裁决（默认读 PAPER_FETCH_HTTP_PROXY）
  - ``set_default_config`` 进程级默认配置（也可每次调用传 config=）

快速上手::

    import paper_fetch

    result = await paper_fetch.download_pdf(
        doi="10.1038/s41586-024-07359-3",
        paper_url=None,
        oa_url=None,
        title="...",
    )
    if result["success"]:
        Path("paper.pdf").write_bytes(result["pdf_bytes"])

命令行::

    python -m paper_fetch --doi 10.1371/journal.pone.0300000 --out ./paper.pdf

与 PaperPilot 的关系：paper-fetch 是下载链**本体**（单一事实源）；PaperPilot 后端
通过薄适配层调用本包并注入自己的代理池/机构凭据钩子。改下载逻辑必须以本仓库为准
（双仓维护契约见 README）。
"""
from .config import FetchConfig, get_config, set_default_config
from .proxy import ProxyProvider, set_proxy_provider
from .service import download_pdf

__version__ = "0.1.0"

__all__ = [
    "download_pdf",
    "FetchConfig",
    "get_config",
    "set_default_config",
    "set_proxy_provider",
    "ProxyProvider",
    "__version__",
]
