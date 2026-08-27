"""PDF 身份核验（2026-08-23 顶包事故新增）。

事故：撞付费墙时，标题搜索 / web_pdf_discovery 路径会抓到「引用了目标论文的开放
获取文章」顶包——题录全对、PDF 正文全错（综述顶替原文、书目参考文献列表顶替专著）。
magic bytes 校验只验「是个 PDF」，不验「是**这篇**论文」。

难点：顶包（引用了 A 的文献 B）的参考文献**就含 A 的 DOI 和标题**，纯全页 contains
拦不住。核验必须区分「A 是这份 PDF 的主标题」vs「A 只是被引用」：

  1. 主标题区（首页上半区 y<50%）：正常论文的主标题/摘要都在这里；
     引用列表整页排布的书目没有「主标题区讲 A」的结构。
  2. 引用区（全页）：书目/综述的 A 痕迹都在这里。

判定（doi/title 锚点至少一个）：
  - 勘误/更正页（主标题区含 corrigendum/erratum/author correction 等标记词）→ 拒收
    （二审 B1：「Corrigendum to <原标题>」页 DOI 命中 + 含原标题词，但不是原文）；
  - DOI 在全页命中（**尾边界匹配**，目标是页面更长 DOI 的前缀不算命中，二审 A2）
    + 主标题区覆盖达标（或全页并非满篇 A 标题词）→ 放行；
    DOI 命中但全页满篇 A 标题词、主标题区却没有 → 引用列表特征，拒绝；
  - **首页自报异 DOI → 拒收**（2026-08-26 Cell/Open-ST 顶包事故新增）：目标 DOI 未命中、
    但首页抽到了别的 DOI，且它不能解释为「同一篇的不同版本」→ reason=foreign_doi 拒收。
    事故复盘：核验明明从首页抽到了顶包自己的 DOI（10.3389/…），却只当备查不作拒收信号；
    而「异 DOI」恰恰是「这份 PDF 主归属是另一篇论文」的最强证据，比标题词覆盖可信得多。
    **版本变体放行的设计权衡**：目标是正式版 DOI、PDF 首页印对应预印本 DOI（bioRxiv 发表
    版 PDF 被当正式版交付，或正式版拿不到回落预印本）属于同一篇的不同版本，必须放行；
    但预印本 DOI ↔ 正式版 DOI 的映射只有 Crossref 等外部 API 才能查证，本模块是纯函数、
    无网络，只能按前缀近似——**保守单向**：仅当首页自报 DOI 是已知预印本前缀
    （10.1101/、10.64898/、10.48550/）时不按 foreign_doi 拒收（落到标题覆盖路径继续验，
    交给标题锚点把关）；自报 DOI 指向另一个正式出版商前缀（10.3389/、10.1016/、
    10.1038/ 等与目标不同的）→ 一律拒收（保守方向的误杀代价小：拒收只是该段失败、
    下载链继续降级，而放行顶包会把错论文固化进文库）。注意该规则只在有目标 DOI 锚点
    时启用：只给标题锚点时无从判断「异」，仍靠标题覆盖把关。
  - 无 DOI 命中时，主标题区标题实词覆盖率 ≥0.7 → 放行（**支持 CJK**：中文按二元
    切分，二审 A1——拉丁词规则会让中文标题词集为空、真论文 100% 误杀）；
  - 首页无文本（扫描件/加密件）→ 无法核验但放行（reason=no_text_unverifiable，
    拒收会误杀扫描件真论文，而事故顶包全是文本型、mismatch 拦得住）；
  - 占位标题（未识别/文件名式/整串 DOI）不是有效锚点（is_placeholder_title）。

调用方分级（paper_download_service._STRICT_IDENTITY_SOURCES）：
  - 非锚定来源（direct / oa / web_pdf_discovery）→ 强制核验，不通过即拒绝该段；
  - DOI 锚定来源（openalex / crossref / publisher_direct 等）→ 放宽，抽首页 DOI 备查。

纯函数、无网络；pymupdf（fitz）只读第一页。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 主标题区标题实词覆盖率阈值：目标标题实词至少这么高比例出现在首页上半区。
# 0.7 容忍版式噪声（页眉期刊名、作者单位占位导致的个别词缺失）。
_TITLE_COVERAGE_THRESHOLD = 0.7
# 「满篇 A」阈值：全页覆盖率高于它且主标题区不达标 → 判为引用列表/书目顶包。
_FULLPAGE_SUSPECT_THRESHOLD = 0.9
# 目标标题实词太少时 word-overlap 不可靠（短标题撞常用词），不足此数不按标题放行。
_MIN_TITLE_WORDS = 3
# 主标题区占页高的比例（上半区）。
_UPPER_ZONE_RATIO = 0.5
# 超过此大小的 PDF 跳过**备查**抽取（大文件偶发偏慢）；strict 核验不受限。
_BACKUP_EXTRACT_MAX_BYTES = 64 * 1024 * 1024

# DOI 抽取正则（与 pdf_metadata._DOI_RE 同字符集口径，这里多剥尾标点防句尾沾连）
_DOI_EXTRACT_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/[^\s\"'<>]+)", re.I)
# 整串 DOI 判定（占位标题检测用）
_DOI_FULL_RE = re.compile(r"(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/\S+", re.I)
_TRAILING_PUNCT_RE = re.compile(r"[.,;:)\]]+$")
# CJK 连续段（汉字/平假名片假名/谚文，含扩展A区）：中文标题整体被非拉丁字符规则剥掉会
# 让词集返回空 → 中文论文在 strict 段 100% 误杀（二审 A1），单独切分。
_CJK_SEG_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
# DOI 尾边界字符集（二审 A2：DOI 之后还跟这些字符 → 是更长 DOI 的前缀，不算命中——
# 目标 10.1111/exampler.123456 不得命中只含 10.1111/exampler.1234567 的 PDF）。
# 集合不含「.」和「;」：点既是 DOI 正文合法字符又是句末标点（由「点后还有字母数字
# 才算版本号段」的附加前瞻单独判定）；分号是引用列表的条目分隔符（doi:xxx; ref），
# 这两种文本形态都不得误拦。只拦字母数字/下划线/括号/斜杠/冒号/连字符的直接拼接。
_DOI_CHAR_SET = "0-9A-Za-z_()/:\\-"

# 勘误/更正页标记词（二审 B1）：「Corrigendum to <原标题>」这类页面 DOI 命中 + 主标题区
# 含原标题词，但它不是原文——是被审稿流程单独发表的更正启事页。
_CORRIGENDUM_MARKERS = (
    "corrigendum",
    "erratum",
    "errata",
    "author correction",
    "publisher correction",
    "correction to",
)

# 已知预印本 DOI 前缀（2026-08-26 事故新增，foreign_doi 版本变体放行用）：
# 首页自报 DOI 属于这些前缀时可能是「同一篇的预印本版本」，不按异 DOI 拒收。
# 本地定义而非引用 paper_search.preprint_resolve 的全量表：本模块是纯函数、不依赖
# service 层，且这里只需要「能构造落地页/常见平台」的保守子集，口径不同勿合并。
_VERSION_VARIANT_PREPRINT_PREFIXES = (
    "10.1101/",  # bioRxiv / medRxiv
    "10.64898/",  # Research Square
    "10.48550/",  # arXiv（含 10.48550/arXiv.* 形态）
)


@dataclass(frozen=True)
class IdentityVerdict:
    """一次核验的结论。ok=False 时 reason 给机器可读原因。"""

    ok: bool
    reason: str  # doi_match / title_match / foreign_doi / mismatch / no_text / no_anchor
    extracted_dois: tuple[str, ...] = ()
    detail: str = ""


def extract_first_page(pdf_bytes: bytes) -> tuple[str, str] | None:
    """抽首页文本，返回 (全页文本, 主标题区文本)；无法解析/无文本返 None。只碰第一页。"""
    try:
        import fitz  # pymupdf

        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if doc.page_count < 1:
                return None
            page = doc[0]
            full = page.get_text() or ""
            height = page.rect.height or 1.0
            # blocks: (x0, y0, x1, y1, text, block_no, type)。取与上半区相交的文本块。
            upper_parts: list[str] = []
            for block in page.get_text("blocks"):
                # blocks: (x0, y0, x1, y1, text, block_no, type)；只用 y0（上边界）与 text
                y0, text = block[1], block[4]
                if not text or not text.strip():
                    continue
                if y0 < height * _UPPER_ZONE_RATIO:
                    upper_parts.append(text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("pdf_identity: 首页文本抽取失败（%s）", exc)
        return None
    full = full.strip()
    upper = " ".join(upper_parts).strip()
    if not full:
        return None
    return full, upper or full


def normalize_doi_for_match(doi: str | None) -> str | None:
    """归一化 DOI 用于比对：剥 doi.org 前缀、去尾部标点、转小写。

    非 DOI 格式的输入返 None（防「随便一段文字」被当成有效锚点误命中）。"""
    s = (doi or "").strip().lower()
    if not s:
        return None
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    s = _TRAILING_PUNCT_RE.sub("", s.strip())
    if not s or not re.fullmatch(r"10\.\d{4,9}/\S+", s):
        return None
    return s


_PDF_SUFFIX_RE = re.compile(r"\.pdf$", re.I)


def is_placeholder_title(title: str | None) -> bool:
    """标题是否是占位名（调用方没拿到真标题时的兜底写法），不能当核验/回填锚点。

    特征：空串、「未识别」开头、文件名式（.pdf 结尾）、整串就是 DOI、
    整串由数字/连字符/下划线/点组成（如 "10.1126_science.ado3927"、"20240503"）。
    """
    s = (title or "").strip()
    if not s:
        return True
    if s.startswith("未识别") or s.startswith("Untitled"):
        return True
    if _PDF_SUFFIX_RE.search(s):
        return True
    if _DOI_FULL_RE.fullmatch(s.rstrip(".,;: ")):
        return True  # 整串就是 DOI
    if re.fullmatch(r"[\d\-_. ]+", s):
        return True  # 纯数字/连字符/下划线/点（文件名式）
    if "_" in s and " " not in s:
        return True  # 无空格且含下划线：文件名式（10.1126_science.ado3927 / 2024_05_03_xxx）
    return False


def extract_dois(text: str) -> tuple[str, ...]:
    """从文本抽全部 DOI（去重、归一化）。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in _DOI_EXTRACT_RE.finditer(text):
        d = normalize_doi_for_match(m.group(1))
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return tuple(out)


def _tokenize(s: str) -> set[str]:
    """标题/文本实词 token 集（二审 A1：支持 CJK）。

    - 拉丁/数字词：lower 后按非字母数字切分，保留 >2 字符的词
      （对齐 web_pdf_discovery_adapter._significant_words 口径）；
    - 判别性 token 加固（2026-08-26 Cell/Open-ST 事故）：连字符整词（open-st、
      high-resolution）与两字符含数字的短词（3d、covid 里没有但 t2e 这类有）作为
      **额外** token 参与覆盖计算——原规则把 open-st 拆成 open+st 后 "st" 因长度
      ≤2 被丢弃，最有区分度的词反而缺席，通用词（open 撞页眉 OPEN ACCESS、high 撞
      high-throughput）撑出了 0.80 的假覆盖。额外 token 只增不改原词集，真论文
      （标题原文照排）两侧同形仍命中，容忍度不变（8 词标题少 1 词仍 0.875 过阈值）。
    - CJK 连续段：二元切分（bigram），单字段直接保留单字——中文没有空格分词，
      bigram 是无词典下的稳定口径，覆盖率语义与拉丁整词一致。
    """
    s = s.lower()
    atoms = re.split(r"[^a-z0-9]+", s)
    tokens = {w for w in atoms if len(w) > 2}
    tokens.update(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", s))
    tokens.update(w for w in atoms if len(w) == 2 and any(c.isdigit() for c in w))
    for seg in _CJK_SEG_RE.findall(s):
        if len(seg) == 1:
            tokens.add(seg)
        else:
            tokens.update(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def _significant_words(s: str) -> set[str]:
    """标题实词（历史别名，内部统一走 _tokenize）。"""
    return _tokenize(s)


def _doi_in_text(doi: str, text_lower: str) -> bool:
    r"""DOI 在文本中命中（带尾边界，二审 A2）。

    裸子串 `doi in text` 会把「目标 = 页面里更长 DOI 的前缀」误判命中
    （10.1111/exampler.123456 命中只含 10.1111/exampler.1234567 的 PDF，
    IEEE 版本后缀形态真实存在）。边界规则：命中位置后不得紧跟 DOI 拼接字符，
    点号单独判定（点后还有字母数字才是更长 DOI 的版本号段，句末句号不拦）。
    """
    pattern = re.escape(doi) + rf"(?![{_DOI_CHAR_SET}])(?!\.\w)"
    return re.search(pattern, text_lower) is not None


def title_coverage(target_title: str, hay_text: str) -> float:
    """目标标题实词在文本中的覆盖率（0~1）。CJK 按 bigram 口径（见 _tokenize）。"""
    target = _tokenize(target_title)
    if not target:
        return 0.0
    hay = _tokenize(hay_text)
    hit = sum(1 for w in target if w in hay)
    return hit / len(target)


def verify_pdf_identity(
    pdf_bytes: bytes,
    *,
    doi: str | None,
    title: str | None,
) -> IdentityVerdict:
    """核验 PDF 是否为目标论文（区分「主标题是 A」与「只是引用了 A」）。

    - doi/title 都没有 → ok=True, reason=no_anchor（没有锚点可比，交调用方决定策略）；
    - 首页无文本 → ok=False, reason=no_text（无法核验，strict 场景应拒绝）；
    - 详见模块 docstring 的三层判定。
    """
    want_doi = normalize_doi_for_match(doi)
    # 占位标题不是有效锚点（事故 b：占位名当锚点会把真 PDF 误拒）
    want_title = None if is_placeholder_title(title) else (title or "").strip()
    if not want_doi and not want_title:
        return IdentityVerdict(ok=True, reason="no_anchor")

    zones = extract_first_page(pdf_bytes)
    if zones is None:
        # 无文本（扫描件/加密件/图片型）：无法核验。拒收会误杀扫描件真论文（事故顶包
        # 全是文本型，mismatch 分支拦得住），故放行 + reason 标记，日志/排查可辨。
        return IdentityVerdict(ok=True, reason="no_text_unverifiable")
    full_text, upper_text = zones
    found = extract_dois(full_text)

    # 勘误/更正页（二审 B1）：「Corrigendum to <原标题>」类页面 DOI 命中 + 主标题区含
    # 原标题词，但它是单独发表的更正启事不是原文——真论文主标题区不会以这些标记词
    # 命名。命中即拒（用户真要勘误页的极罕见场景留给 force 重试）。放在锚点分支之前：
    # 无论 DOI 锚定还是纯标题锚定，勘误页都不是原文。
    upper_low = upper_text.lower()
    if any(m in upper_low for m in _CORRIGENDUM_MARKERS):
        return IdentityVerdict(
            ok=False,
            reason="mismatch",
            extracted_dois=found,
            detail="corrigendum_page",
        )

    # 首页自报的预印本前缀 DOI（可能是同一篇的预印本版本，见下方 foreign_doi 分支）：
    # 不拒收，标题覆盖达标放行时在 detail 里标注。
    preprint_variant_doi: str | None = None
    if want_doi:
        # 尾边界匹配（二审 A2）：目标不得是页面里更长 DOI 的前缀
        doi_hit = want_doi in found or _doi_in_text(want_doi, full_text.lower())
        if doi_hit:
            if not want_title or len(_significant_words(want_title)) < _MIN_TITLE_WORDS:
                return IdentityVerdict(ok=True, reason="doi_match", extracted_dois=found)
            upper_cov = title_coverage(want_title, upper_text)
            if upper_cov >= _TITLE_COVERAGE_THRESHOLD:
                return IdentityVerdict(
                    ok=True,
                    reason="doi_match",
                    extracted_dois=found,
                    detail=f"upper_cov={upper_cov:.2f}",
                )
            full_cov = title_coverage(want_title, full_text)
            if full_cov < _FULLPAGE_SUSPECT_THRESHOLD:
                # DOI 命中且页面并非满篇 A 标题词（DOI 更像页脚版权行而非引用条目）
                return IdentityVerdict(
                    ok=True,
                    reason="doi_match",
                    extracted_dois=found,
                    detail=f"upper_cov={upper_cov:.2f},full_cov={full_cov:.2f}",
                )
            # DOI + 满篇 A 标题词、但主标题区没有 A —— 引用列表/书目特征，拒绝
            return IdentityVerdict(
                ok=False,
                reason="mismatch",
                extracted_dois=found,
                detail=f"citation_list_pattern(upper={upper_cov:.2f},full={full_cov:.2f})",
            )
        # 目标 DOI 未命中但首页自报了 DOI：异 DOI 是「这份 PDF 主归属另一篇论文」的
        # 最强证据（2026-08-26 Cell/Open-ST 顶包事故：Frontiers 综述首页明印自己的
        # 10.3389/ DOI，核验却只当备查放行）。仅当自报 DOI 不能解释为同一篇的预印本
        # 版本（已知预印本前缀，见模块 docstring 的保守单向权衡）时拒收。
        if found:
            foreign = [
                d
                for d in found
                if not d.startswith(_VERSION_VARIANT_PREPRINT_PREFIXES)
            ]
            if foreign:
                return IdentityVerdict(
                    ok=False,
                    reason="foreign_doi",
                    extracted_dois=found,
                    detail=f"self_declared={foreign[0]}",
                )
            # 自报的全是预印本前缀：可能是同一篇的预印本版本（正式版 DOI 目标 +
            # bioRxiv PDF），不在此拒收，交给下面的标题覆盖路径继续验，命中时标注。
            preprint_variant_doi = found[0]

    if want_title:
        if len(_significant_words(want_title)) >= _MIN_TITLE_WORDS:
            cov = title_coverage(want_title, upper_text)
            if cov >= _TITLE_COVERAGE_THRESHOLD:
                detail = f"upper_cov={cov:.2f}"
                if preprint_variant_doi:
                    detail += f",preprint_doi={preprint_variant_doi}"
                return IdentityVerdict(
                    ok=True,
                    reason="title_match",
                    extracted_dois=found,
                    detail=detail,
                )
    return IdentityVerdict(
        ok=False,
        reason="mismatch",
        extracted_dois=found,
        detail=f"found_dois={list(found)[:3]}",
    )


def backup_extract_dois(pdf_bytes: bytes) -> tuple[str, ...]:
    """非 strict 来源的备查抽取：只记日志、不影响结果。失败/超限返空。"""
    if len(pdf_bytes) > _BACKUP_EXTRACT_MAX_BYTES:
        return ()
    zones = extract_first_page(pdf_bytes)
    if zones is None:
        return ()
    return extract_dois(zones[0])
