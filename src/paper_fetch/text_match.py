"""DOI / 标题文本工具（从 PaperPilot 内联迁移的纯函数，零外部依赖）。

- ``normalize_doi``：原 app.services.document_service.normalize_doi（查重口径）。
- ``title_match_score``：原 app.services.paper_search.merge.title_match_score
  （中英标题匹配；paper-fetch 侧预印本发现/网页发现的验收闸门用）。

两份函数都是纯字符串归一，PaperPilot 侧保留原实现（document_service / merge），
行为由两边各自的测试锁死；改动任一份必须同步另一份（双仓维护契约，见 README）。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# normalize_doi（原 PaperPilot document_service 实现）
# ---------------------------------------------------------------------------

_DOI_PREFIXES = (
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "dx.doi.org/",
    "https://doi.org/",
    "http://doi.org/",
    "doi.org/",
    "doi:",
)
_DOI_TRAILING_PUNCT = ".,;:)]}"

# bioRxiv/medRxiv 的 DOI 带显示用版本尾巴（10.1101/2024.12.10.627865v1），
# 各版本共用同一 DOI；仅对 10.1101 前缀剥离，其他出版社 DOI 结尾 vN 极罕见、不动。
_BIORXIV_VERSION_RE = re.compile(r"v\d+$")


def normalize_doi(doi: str | None) -> str | None:
    """归一化 DOI：去前缀（doi: / https://doi.org/ / dx.doi.org）、去空白、
    去尾标点、去 bioRxiv 版本尾巴（v1/v2）、转小写。
    """
    if not doi:
        return None
    d = doi.strip().lower()
    for prefix in _DOI_PREFIXES:
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    d = d.strip().rstrip(_DOI_TRAILING_PUNCT).strip()
    if d.startswith("10.1101/"):
        d = _BIORXIV_VERSION_RE.sub("", d).strip()
    return d or None


# ---------------------------------------------------------------------------
# title_match_score（原 PaperPilot paper_search.merge 实现）
# ---------------------------------------------------------------------------


def _title_norm(s: str) -> str:
    """打分用归一：小写、保留英文数字 + 中文（去标点/空白噪声）。

    不能只留 ASCII——那样会把整条中文标题清成空串、中文标题打分恒为 0。
    """
    return re.sub(r"[^a-z0-9一-鿿 ]+", " ", (s or "").lower()).strip()


def _significant_tokens(s: str) -> set[str]:
    """实词集合：英文取长度 > 2 的词；中文按字 + 2-gram（无空格分词，bigram 更能区分）。"""
    toks: set[str] = set()
    for w in _title_norm(s).split():
        if w.isascii():
            if len(w) > 2:
                toks.add(w)
        else:
            toks.update(w)  # 单字
            for i in range(len(w) - 1):
                toks.add(w[i : i + 2])  # 2-gram
    return toks


def title_match_score(
    target_title: str, candidate_title: str, candidate_abstract: str | None = None
) -> float:
    """目标标题的实词有多大比例出现在候选标题(+可选摘要)里（0~1），兼顾整串相似度。

    中英都管用：英文走词重叠、中文走字/2-gram 重叠，再用保留中文的整串相似度兜一手。
    """
    target = _significant_tokens(target_title)
    if not target:
        # 极端：标题全是标点/单字符——退回纯整串相似度，避免恒 0
        return SequenceMatcher(
            None, _title_norm(target_title), _title_norm(candidate_title)
        ).ratio()
    hay = _significant_tokens(f"{candidate_title} {candidate_abstract or ''}")
    overlap = len(target & hay) / len(target)
    seq = SequenceMatcher(
        None, _title_norm(target_title), _title_norm(candidate_title)
    ).ratio()
    return max(overlap, seq)
