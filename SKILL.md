---
name: paper-fetch
description: >-
  下载学术论文 PDF 的下载链：给 DOI / 标题 / 论文页 URL / OA 直链，返回 PDF 字节或
  结构化失败信号。合法来源优先（OpenAlex/Unpaywall/Crossref/Europe PMC/出版商模板/
  无头浏览器/网页发现），正式版全失败自动回落预印本（含按标题发现预印本），撞付费墙
  返回 auth_required + landing_url。内置 PDF 身份核验防「引用目标论文的文章」顶包。
  触发场景：下载论文 PDF、按 DOI/标题/URL 获取文献全文、找论文的开放获取版本或预印本、
  MCP 工具报付费墙时提示用户走 landing_url。
---

# paper-fetch：论文 PDF 下载

## 什么时候用

- 用户说「下载这篇论文 / 拿到全文 / 给我 PDF」，手里有 DOI、标题、论文页 URL 或 OA 链接任一。
- 需要判断一篇论文有没有开放获取版本（含预印本）——链路会自动找，不用自己先搜一遍。

## 怎么调

### CLI（最简单）

```bash
python -m paper_fetch --doi "10.1371/journal.pone.0300000" --out ./paper.pdf
# 或按标题：python -m paper_fetch --title "Brain-wide spatial transcriptomics" --out ./p.pdf
```

段进度在 stderr；stdout 无输出。退出码：**0 成功 / 2 付费墙 / 3 其他失败 / 1 参数错**。

### Python API

```python
from paper_fetch import download_pdf

result = await download_pdf(
    doi,          # str | None
    paper_url,    # str | None  论文页 URL（可以是 landing page 或 PDF 直链）
    oa_url,       # str | None  已知开放获取直链（有就给，能省整条链）
    title=title,  # str | None  强烈建议给：预印本发现与身份核验的锚点
)
```

## 返回字段怎么解读

- `success=True`：`pdf_bytes`（bytes）、`source`（命中段名）、`size_bytes`。
  - `delivered_version == "preprint"`：拿到的是**预印本**不是正式版——
    `requested_doi` 是用户要的、`delivered_doi` 是实际拿到的，`message` 是给用户的说明，
    原样转述即可。
  - `pdf_identity`：`verified`（首页 DOI/标题真比对通过）或 `unverified`（无锚点或
    DOI 锚定段未跑核验）。入正式库建议只信 verified。
- `success=False`：
  - `auth_required=True`：**付费墙**。`landing_url` 是论文页——把 URL 给用户，建议
    学校网络/机构订阅打开后手动下载，**不要**反复重试同一条链（确定性失败）。
  - `error == "rate_limited"`：被限流，`retry_after_sec` 后再试；配置了代理轮换会
    自动换节点后重试一次。
  - `failure_detail`：细分（`paywall_no_access` / `institutional_proxy_failed` /
    `wrong_paper`——找到过 PDF 但身份核验判为别的论文，已拒收）。
  - `message`：中文用户可见说明，可直接转述。

## 失败重试纪律

- `auth_required` 是确定性失败：换 DOI/标题重试无意义，让用户走机构通道或手动下载。
- 网络瞬断可整链重试一次；连续两次失败就停，报告 `tried_sources` 给用户。
- Sci-Hub 默认关闭；用户没明确要求别开（合规责任在部署方）。

## 配置

代理 `PAPER_FETCH_HTTP_PROXY`、Tavily `PAPER_FETCH_TAVILY_API_KEY`、
Unpaywall `UNPAYWALL_EMAIL` 等，完整表见仓库 README.md。零配置可跑（直连、跳过
需 key 的段）。
