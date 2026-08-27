# paper-fetch

论文 PDF 下载链，独立可分发的 Python 包 + agent skill。从
[PaperPilot](https://github.com/chenly255/paperpilot) 后端迁出的**单一事实源**：
合法来源优先、正式版优先回落预印本、PDF 身份核验防「顶包」。

给谁的：任何 agent 服务器 / 脚本，只要「给 DOI / 标题 / URL，拿回论文 PDF」这一件事。

## 装法

```bash
# 从 GitHub 装（推荐）
pip install git+https://github.com/chenly255/paper-fetch.git@main

# 要无头浏览器兜底（过 Cloudflare/JS 挑战）就带 extras
pip install "paper-fetch[browser]@git+https://github.com/chenly255/paper-fetch.git@main"
playwright install chromium   # browser extras 装完后跑一次
```

要求 Python ≥ 3.11。核心依赖只有 httpx / pymupdf / beautifulsoup4 / curl-cffi。

## 快速上手

### Python API

```python
import asyncio
from pathlib import Path
import paper_fetch

async def main():
    result = await paper_fetch.download_pdf(
        doi="10.1371/journal.pone.0300000",   # 三选一：doi / paper_url / oa_url（title 可选加固）
        paper_url=None,
        oa_url=None,
        title="Some open access paper",
    )
    if result["success"]:
        Path("paper.pdf").write_bytes(result["pdf_bytes"])
        print("来源:", result["source"], "大小:", result["size_bytes"])
    elif result.get("auth_required"):
        print("付费墙，论文页:", result.get("landing_url"))
    else:
        print("失败:", result.get("error"), result.get("message"))

asyncio.run(main())
```

### CLI

```bash
python -m paper_fetch --doi 10.1371/journal.pone.0300000 --out ./paper.pdf
# 链路段进度打到 stderr；退出码：0 成功 / 2 付费墙 / 3 其他失败 / 1 参数错误
```

## 配置项

不配置也能跑（直连）。需要时按环境变量或 `FetchConfig` 传：

| 配置 | 环境变量 | 默认 | 说明 |
|------|----------|------|------|
| HTTP 代理 | `PAPER_FETCH_HTTP_PROXY` | 无（直连） | 所有外发流量走这一个代理（如 `http://127.0.0.1:17891`）。宿主可用 `set_proxy_provider()` 注入按域名分流的实现 |
| Tavily key | `PAPER_FETCH_TAVILY_API_KEY` | 无 | 逗号分隔多 key 自动轮换；或 `PAPER_FETCH_TAVILY_KEYS_FILE` 指向 `{"keys": [...]}` 号池文件。无 key 自动跳过网页发现/预印本发现段 |
| Unpaywall 邮箱 | `UNPAYWALL_EMAIL` | 无 | Unpaywall API 要求的邮箱，配了才启用该段 |
| PDF 上限 | `PAPER_FETCH_MAX_PDF_MB` | 120 | 超限丢弃返 `size_limit_exceeded` |
| 总时间预算 | `PAPER_FETCH_BUDGET_SEC` | 75 | 软预算，超了停止后续段正常返回 |
| Elsevier API | `PAPER_FETCH_ELSEVIER_API_KEY` / `..._INST_TOKEN` | 无 | Elsevier 官方接口（免费申请），授权与出口 IP 绑定 |
| Sci-Hub 开关 | `PAPER_FETCH_SCIHUB_ENABLED` / `PAPER_FETCH_SCIHUB_BASE_URLS` | 关 | 见下方免责声明 |

```python
from paper_fetch import FetchConfig, download_pdf
cfg = FetchConfig(http_proxy="http://127.0.0.1:17891",
                  unpaywall_email="you@example.com",
                  tavily_api_keys=["tvly-xxx"])
result = await download_pdf(doi, None, None, title=t, config=cfg)
```

机构图书馆代理（学校订阅出口）默认停用，宿主应用通过
`FetchConfig.library_proxy_gate / library_proxy_fetcher / elsevier_creds_provider`
钩子注入自己的实现（PaperPilot 就是这么干的）；独立用户用不到。

## 下载链语义（一段话）

**先找正式版 → 正式版所有合法路径全失败 → 回落预印本 → 预印本也没有才空手返回付费墙信号。**
具体降级顺序：① 预印本/直链/OA URL 模板直下 → ② OpenAlex/Elsevier API →
③ 出版商模板/落地页元数据/Unpaywall/Crossref → ④ Europe PMC（含 NIH 作者手稿）→
⑤ 无头浏览器/Tavily 网页发现 → ⑥ 预印本兜底（候选自带 URL ⑥a；显式给了预印本
DOI 用原始 DOI 模板直下；只给正式版 DOI 则按标题发现预印本）→ ⑦ 机构图书馆代理
（默认关）→ ⑧ Sci-Hub（默认关）。付费墙终态返回 `auth_required=True` +
`landing_url`，交给用户走机构订阅或手动下载。

## 身份核验与顶包防护

撞付费墙时，网页来源容易抓到「引用了目标论文的开放获取文章」来顶包——题录全对、
正文全错。`pdf_identity.py` 对非 DOI 锚定段（direct/oa/web discovery）强制首页核验：

- **首页自报异 DOI 拒收**：首页抽到的 DOI 指向另一篇正式出版论文（非预印本前缀）
  → 拒收（`foreign_doi`，2026-08-26 Cell/Open-ST 顶包事故的整改）；自报预印本前缀
  DOI 视为「同一篇的预印本版本」放行，交给标题锚点继续把关；
- 主标题区（首页上半区）标题实词覆盖率 ≥ 0.7 放行；全页满篇命中但主标题区没有
  → 引用列表/书目特征，拒收；
- 勘误/更正页（Corrigendum/Erratum 标题）拒收；
- 核验通过且 DOI/标题真比对命中 → `pdf_identity="verified"`，否则 `unverified`
  （调用方查重只信 verified）。

## 与 PaperPilot 的关系 · 双仓维护契约

- **paper-fetch 是下载链本体（单一事实源）**；PaperPilot 后端通过薄适配层调用
  本包（pyproject 依赖本仓库），注入自己的代理池与机构凭据钩子，**不持有任何下载逻辑副本**。
- **改下载行为只改本仓库**：修 bug / 调段序 / 加 adapter 一律在这里动手，PaperPilot
  跟随升级依赖版本；反向不成立（在 PaperPilot 里改下载逻辑 = 制造漂移，禁止）。
- 两边测试都要绿：本仓库测试锁行为；PaperPilot 侧测试锁适配兼容（返回 schema、
  适配层翻译）。改本仓库导致 PaperPilot 测试红了 = 破坏性变更，必须同步修适配层。
- 迁移自 PaperPilot 的纯函数（`normalize_doi` / `title_match_score` /
  `PREPRINT_DOI_PREFIXES`）在 PaperPilot 侧 re-export 本包定义，防两份漂移。

## 免责声明

本工具只用于下载**开放获取**内容或**你有权访问**的内容（机构订阅、作者自存档等）。
Sci-Hub 适配器默认关闭；开启（`PAPER_FETCH_SCIHUB_ENABLED=1`）表示部署方自行承担
合规与法律责任，与本项目作者无关。无头浏览器仅执行页面正常 JS 挑战，不破解验证码
（遇到图形验证码自然失败）。
