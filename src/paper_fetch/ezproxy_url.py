"""EZproxy 网址改写（图书馆代理，核心可复用件，移植自 scansci-pdf）。

EZproxy 是欧美高校图书馆常用的校外访问方式：图书馆跑一个代理服务器，把目标网址改写成
"绕图书馆代理走"，由代理带上学校的付费身份访问出版商。

国内高校（本平台学校库里的 144 所）几乎都用 WebVPN（深澜）而非 EZproxy，且 EZproxy 的
代理登录地址没有公开数据源、需用户自填，故这里只提供**网址改写**这一纯逻辑机制（已就位、
可单测），登录与下载通道待真有学校需要时再按 webvpn_agent 的范式接（复用 BrowserSession +
机构凭证防封号）。WebVPN 已覆盖我们有数据的全部学校的校外访问需求。

两种常见 EZproxy 改写式：
- 模板式：``https://login.ezproxy.lib.x.edu/login?url={url}``（{url} 占位）。
- 主机重写式：``host.com`` → ``host-com.ezproxy.lib.x.edu``（点换连字符 + 代理后缀）。本模块
  实现更通用的模板式（与 scansci-pdf 一致），主机重写式按需再加。
"""
from __future__ import annotations

from urllib.parse import quote


def make_ezproxy_url(target_url: str, ezproxy_login_url: str) -> str:
    """把目标 URL 转成走 EZproxy 的地址。

    ezproxy_login_url 形如 ``https://login.ezproxy.lib.x.edu/login?url={url}``：
    - 含 ``{url}`` 占位 → 直接替换（URL 编码）。
    - 不含占位 → 按 EZproxy 惯例追加 ``?url=`` 或 ``&url=``。
    空配置或空目标返回空串（上层据此跳过）。
    """
    if not target_url or not ezproxy_login_url:
        return ""
    base = ezproxy_login_url.strip()
    if "{url}" in base:
        return base.replace("{url}", quote(target_url, safe=""))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}url={quote(target_url, safe='')}"
