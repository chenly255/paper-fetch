"""preprint_discovery：付费墙正式版 → 按标题发现同研究开放预印本（2026-08-21 事故新增段）。

覆盖：
  A. 标题相似度阈值锚点——用真实事故案例的两个标题锁住阈值（能收真预印本、能拒无关篇）
  B. 作者姓氏解析——Europe PMC authorString 与 Crossref author 数组的归一化（单测）
  C. 作者团队闸门（_author_gate_passes）——首/末位命中、对不上、缺数据放行
  D. Europe PMC SRC:PPR 关键词搜 + 双闸门验收（标题过线±作者命中/对不上/缺数据）
  E. Crossref posted-content 搜 + 双闸门验收
  F. Tavily 兜底（复用 oa_preprint_discovery）
  G. discover_preprint 编排——来源顺序、防自映射、短标题跳过
  H. crossref_meta_for_doi——一次 API 补标题+作者（含 select 参数回归锁）
  I. 真实案例正面锚点回归（Braxton…Wood / PPR609497，作者数据取自真实 API 响应）

网络一律 mock（httpx.AsyncClient / oa_preprint_discovery），不碰真 API。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paper_fetch import proxy as proxy_pool
from paper_fetch import preprint_discovery
from paper_fetch.text_match import title_match_score

# ---- 真实事故案例（2026-08-21，Nature 10.1038/s41586-024-07359-3 付费墙）----
# 正式版标题（Crossref 实查）与预印本标题（Europe PMC PPR609497 实查）——阈值锚点用。
REAL_PUBLISHED_TITLE = "3D genomic mapping reveals multifocality of human pancreatic precancers"
REAL_PREPRINT_TITLE = (
    "Three-dimensional genomic mapping of human pancreatic tissue reveals striking "
    "multifocality and genetic heterogeneity in precancerous lesions"
)
REAL_PREPRINT_DOI = "10.1101/2023.01.27.525553"
# 真实作者数据（2026-08-22 实查 Europe PMC authorString 与 Crossref author 数组）：
# 首作者 Braxton、末位作者 Wood，预印本与正式版一致——正面锚点回归用。
REAL_EPMC_AUTHOR_STRING = (
    "Braxton AM, Kiemen AL, Grahn MP, Forjaz A, Babu JM, Zheng L, Jiang L, Cheng H, "
    "Song Q, Reichel R, Graham S, Damanakis AI, Fischer CG, Mou S, Metz C, Granger J, "
    "Liu X, Bachmann N, Almagro-Pérez C, Jiang AC, Yoo J, Kim B, Du S, Foster E, Hsu JY, "
    "Rivera PA, Chu LC, Liu F, Niknafs N, Fishman EK, Yuille A, Roberts NJ, Thompson ED, "
    "Scharpf RB, Cornish TC, Jiao Y, Karchin R, Hruban RH, Wu P, Wirtz D, Wood LD."
)
REAL_FIRST_AUTHOR = "braxton"
REAL_LAST_AUTHOR = "wood"


def _mock_client(resp_json: dict | None = None, status: int = 200):
    """构造可作 httpx.AsyncClient 返回值的 mock（支持 async with + .get）。"""
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=resp_json or {})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


def _epmc_response(items: list[dict]) -> dict:
    return {"resultList": {"result": items}}


class TestThresholdAnchor:
    """阈值锚点：真实标题对的分数必须稳定落在阈值两侧（改打分逻辑/阈值会立刻红）。"""

    def test_真实预印本对过阈值(self):
        score = title_match_score(REAL_PUBLISHED_TITLE, REAL_PREPRINT_TITLE)
        assert score >= preprint_discovery._TITLE_MATCH_THRESHOLD

    def test_无关预印本被拒(self):
        # 同领域无关篇：词重叠远低于阈值（实测 0.40-0.47）
        for unrelated in [
            "Single-cell transcriptomics of human pancreatic islets across diabetes",
            "Spatial mapping of gut microbiome reveals metabolic interactions",
        ]:
            assert title_match_score(REAL_PUBLISHED_TITLE, unrelated) \
                < preprint_discovery._TITLE_MATCH_THRESHOLD

    def test_同系列近缘标题不误收(self):
        """评审 m3：0.60-0.72 区间没有可信正面样本，阈值提到 0.72——同领域/同系列
        （同器官、同技术）但不同研究的标题必须拒收，防止下错篇。"""
        same_series = [
            "Single-cell atlas of human pancreas vs liver",
            "Multifocality of KRAS mutations in pancreatic cancer models",
            "Three-dimensional mapping of chromatin architecture in pancreatic organoids",
        ]
        for cand in same_series:
            score = title_match_score(REAL_PUBLISHED_TITLE, cand)
            assert score < preprint_discovery._TITLE_MATCH_THRESHOLD, f"{cand} -> {score}"

    def test_阈值当前取值(self):
        # 锁住当前阈值本身（0.72 = preprint_resolve._TITLE_MATCH_MIN 对齐，评审 m3），
        # 改动需连带重跑锚点用例（真实对 0.857 仍收、同系列 ≤0.47 仍拒）
        assert preprint_discovery._TITLE_MATCH_THRESHOLD == 0.72


class TestSurnameParsing:
    """姓氏解析单测：Europe PMC authorString 与 Crossref author 数组 → 同一套归一化键。"""

    def test_authorString解析_真实案例首末位(self):
        keys = preprint_discovery._surnames_from_author_string(REAL_EPMC_AUTHOR_STRING)
        assert keys[0] == REAL_FIRST_AUTHOR
        assert keys[-1] == REAL_LAST_AUTHOR
        assert len(keys) == 41  # 实查作者总数，顺带锁定分段解析没丢人

    def test_authorString解析_带前缀姓氏与连字符(self):
        # 剥缩写后取末词：前缀姓氏 "van der Heijden EC" → "heijden"，
        # 连字符复姓 "Almagro-Pérez C" → "pérez"——与 Crossref family 同规则归一，两边可比。
        keys = preprint_discovery._surnames_from_author_string(
            "van der Heijden EC, Almagro-Pérez C, Li X"
        )
        assert keys == ["heijden", "pérez", "li"]
        # 同一姓氏在 Crossref 侧（family 字段）归一出同样的键
        assert preprint_discovery._surnames_from_crossref_authors(
            [{"family": "van der Heijden"}, {"family": "Almagro-Pérez"}]
        ) == ["heijden", "pérez"]

    def test_authorString解析_空输入返空列表(self):
        assert preprint_discovery._surnames_from_author_string(None) == []
        assert preprint_discovery._surnames_from_author_string("") == []
        assert preprint_discovery._surnames_from_author_string(",,,") == []

    def test_crossref_author数组解析(self):
        authors = [
            {"given": "Alicia M.", "family": "Braxton", "sequence": "first"},
            {"given": "Laura D.", "family": "Wood", "sequence": "additional"},
        ]
        assert preprint_discovery._surnames_from_crossref_authors(authors) == ["braxton", "wood"]

    def test_crossref_author数组_机构作者与脏数据(self):
        # 机构作者只有 name；非 dict 元素跳过；空/None 返空
        authors = [
            {"name": "WHO Consortium"},
            "not-a-dict",
            {"family": "Zhang"},
        ]
        # 机构作者只有 name 字段：同样按末词规则归一（两边数据源规则一致即可比）
        assert preprint_discovery._surnames_from_crossref_authors(authors) == ["consortium", "zhang"]
        assert preprint_discovery._surnames_from_crossref_authors(None) == []
        assert preprint_discovery._surnames_from_crossref_authors([]) == []

    def test_姓氏键_前缀姓氏两侧一致(self):
        # Crossref family="van der Heijden" 与 EPMC 段 "van der Heijden EC" 归一到同一键
        assert preprint_discovery._surname_key("van der Heijden") == "heijden"
        assert preprint_discovery._surname_key(None) is None
        assert preprint_discovery._surname_key("123") is None


class TestAuthorGate:
    """作者团队闸门：首↔首、末↔末 位置比较；任一侧缺数据返 None（不拦截）。"""

    REF = [REAL_FIRST_AUTHOR, "kiemen", "grahn", REAL_LAST_AUTHOR]

    def test_首作者命中放行(self):
        assert preprint_discovery._author_gate_passes(self.REF, ["braxton", "other", "smith"]) is True

    def test_末位作者命中放行(self):
        # 首作者不同（版本演进换一作）但通讯作者一致 → 仍认同一团队
        assert preprint_discovery._author_gate_passes(self.REF, ["someone", "else", "wood"]) is True

    def test_完全对不上拦截(self):
        assert preprint_discovery._author_gate_passes(self.REF, ["zhang", "li", "wang"]) is False

    def test_参照缺失不拦截(self):
        assert preprint_discovery._author_gate_passes(None, ["braxton", "wood"]) is None
        assert preprint_discovery._author_gate_passes([], ["braxton", "wood"]) is None

    def test_候选缺失不拦截(self):
        assert preprint_discovery._author_gate_passes(self.REF, None) is None
        assert preprint_discovery._author_gate_passes(self.REF, []) is None

    def test_单作者论文自比自(self):
        assert preprint_discovery._author_gate_passes(["solo"], ["solo"]) is True
        assert preprint_discovery._author_gate_passes(["solo"], ["other"]) is False


class TestEuropePmc:
    @pytest.mark.asyncio
    async def test_命中真实案例预印本_首作者闸门通过(self):
        """真实案例正面回归：标题过线 + 参照首作者命中候选首作者 → 收（含 biorxiv URL）。"""
        client = _mock_client(_epmc_response([
            {"doi": REAL_PREPRINT_DOI, "title": REAL_PREPRINT_TITLE,
             "authorString": REAL_EPMC_AUTHOR_STRING, "source": "PPR"},
        ]))
        ref_authors = [REAL_FIRST_AUTHOR, "kiemen", REAL_LAST_AUTHOR]
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE, ref_authors)
        assert out is not None
        assert out["doi"] == REAL_PREPRINT_DOI
        assert out["url"] == f"https://www.biorxiv.org/content/{REAL_PREPRINT_DOI}"
        assert out["via"] == "europe_pmc"

    @pytest.mark.asyncio
    async def test_标题过线但末位作者命中_仍收(self):
        """版本演进首作者变了、通讯作者没变 → 仍是同一团队的研究，收。"""
        client = _mock_client(_epmc_response([
            {"doi": REAL_PREPRINT_DOI, "title": REAL_PREPRINT_TITLE,
             "authorString": "Kiemen AL, Grahn MP, Forjaz A, Wood LD.", "source": "PPR"},
        ]))
        ref_authors = [REAL_FIRST_AUTHOR, "kiemen", REAL_LAST_AUTHOR]
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE, ref_authors)
        assert out is not None and out["doi"] == REAL_PREPRINT_DOI

    @pytest.mark.asyncio
    async def test_标题过线但作者完全对不上_拒收(self):
        """撞标题的别家论文：标题相似度过线，首/末作者都不同 → 拦下，继续找下一条。"""
        other_doi = "10.1101/2024.99.99.000001"
        client = _mock_client(_epmc_response([
            # 第一条：撞标题别家论文（作者对不上）→ 拒
            {"doi": other_doi, "title": REAL_PREPRINT_TITLE,
             "authorString": "Zhang Y, Li M, Wang H.", "source": "PPR"},
            # 第二条：真正同研究 → 收（证明拒后循环继续）
            {"doi": REAL_PREPRINT_DOI, "title": REAL_PREPRINT_TITLE,
             "authorString": REAL_EPMC_AUTHOR_STRING, "source": "PPR"},
        ]))
        ref_authors = [REAL_FIRST_AUTHOR, "kiemen", REAL_LAST_AUTHOR]
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE, ref_authors)
        assert out is not None and out["doi"] == REAL_PREPRINT_DOI

    @pytest.mark.asyncio
    async def test_作者全对不上且无其他候选_返None(self):
        client = _mock_client(_epmc_response([
            {"doi": "10.1101/2024.99.99.000001", "title": REAL_PREPRINT_TITLE,
             "authorString": "Zhang Y, Li M, Wang H.", "source": "PPR"},
        ]))
        ref_authors = [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR]
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE, ref_authors)
        assert out is None

    @pytest.mark.asyncio
    async def test_候选缺authorString_退回纯标题验收仍收(self):
        """候选侧没作者数据 → 闸门返 None 不拦截（不因缺数据降召回）。"""
        client = _mock_client(_epmc_response([
            {"doi": REAL_PREPRINT_DOI, "title": REAL_PREPRINT_TITLE, "source": "PPR"},
        ]))
        ref_authors = [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR]
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE, ref_authors)
        assert out is not None and out["doi"] == REAL_PREPRINT_DOI

    @pytest.mark.asyncio
    async def test_无参照作者_纯标题路径正常收拒(self):
        """参照作者缺失（只给标题的发现）→ 纯标题验收：过线收、不过线拒。"""
        client = _mock_client(_epmc_response([
            {"doi": REAL_PREPRINT_DOI, "title": REAL_PREPRINT_TITLE,
             "authorString": "Zhang Y, Li M.", "source": "PPR"},
        ]))
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE, None)
        assert out is not None and out["doi"] == REAL_PREPRINT_DOI
        # 低标题分候选：即使作者「匹配」也不收——作者闸门不赦免低标题分
        client2 = _mock_client(_epmc_response([
            {"doi": "10.1101/2020.01.01.999999",
             "title": "Single-cell transcriptomics of pancreatic islets in diabetes",
             "authorString": "Braxton AM, Wood LD.", "source": "PPR"},
        ]))
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client2):
            assert await preprint_discovery._via_europe_pmc(
                REAL_PUBLISHED_TITLE, [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR],
            ) is None

    @pytest.mark.asyncio
    async def test_标题校验拦截无关候选(self):
        """PPR 命中但标题相似度不足 → 不认（防张冠李戴下载别篇）。"""
        client = _mock_client(_epmc_response([
            {"doi": "10.1101/2020.01.01.999999",
             "title": "Single-cell transcriptomics of pancreatic islets in diabetes", "source": "PPR"},
        ]))
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE)
        assert out is None

    @pytest.mark.asyncio
    async def test_api失败静默返None(self):
        client = _mock_client(status=500)
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            assert await preprint_discovery._via_europe_pmc(REAL_PUBLISHED_TITLE) is None

    def _assert_query(self, client, title):
        """检查构造的查询：全部实词都是 TITLE:"..." 子句且带 SRC:PPR。"""
        query = client.get.await_args.kwargs["params"]["query"]
        assert query.endswith("AND SRC:PPR")
        clauses = query.replace(" AND SRC:PPR", "").split(" AND ")
        for c in clauses:
            assert c.startswith('TITLE:"') and c.endswith('"')

    @pytest.mark.asyncio
    async def test_查询用实词关键词而非整标题_且不追加AUTH限定(self):
        """整标题精确查询会落空（预印本标题与正式版不同），必须拆成关键词 AND 组合。
        且不追加 AUTH:"首作者"：实测无噪声可降、首作者变动会漏召回（模块 docstring 有证据）。"""
        client = _mock_client(_epmc_response([]))
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            await preprint_discovery._via_europe_pmc(
                REAL_PUBLISHED_TITLE, [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR],
            )
        self._assert_query(client, REAL_PUBLISHED_TITLE)
        query = client.get.await_args.kwargs["params"]["query"]
        # 实标题的完整字符串不应作为单一查询出现；也不允许出现 AUTH 限定
        assert REAL_PUBLISHED_TITLE not in query
        assert "AUTH:" not in query


class TestCrossref:
    @pytest.mark.asyncio
    async def test_命中posted_content预印本_作者闸门通过(self):
        client = _mock_client({"message": {"items": [
            {"DOI": REAL_PREPRINT_DOI, "type": "posted-content",
             "title": [REAL_PREPRINT_TITLE],
             "author": [{"family": "Braxton"}, {"family": "Wood"}]},
        ]}})
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_crossref(
                REAL_PUBLISHED_TITLE, [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR],
            )
        assert out is not None
        assert out["doi"] == REAL_PREPRINT_DOI
        assert out["via"] == "crossref"

    @pytest.mark.asyncio
    async def test_标题过线但作者对不上_拒收(self):
        client = _mock_client({"message": {"items": [
            {"DOI": "10.1101/2024.99.99.000001", "type": "posted-content",
             "title": [REAL_PREPRINT_TITLE],
             "author": [{"family": "Zhang"}, {"family": "Wang"}]},
        ]}})
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_crossref(
                REAL_PUBLISHED_TITLE, [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR],
            )
        assert out is None

    @pytest.mark.asyncio
    async def test_候选缺author字段_退回纯标题验收仍收(self):
        client = _mock_client({"message": {"items": [
            {"DOI": REAL_PREPRINT_DOI, "type": "posted-content",
             "title": [REAL_PREPRINT_TITLE]},
        ]}})
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            out = await preprint_discovery._via_crossref(
                REAL_PUBLISHED_TITLE, [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR],
            )
        assert out is not None and out["doi"] == REAL_PREPRINT_DOI

    @pytest.mark.asyncio
    async def test_select参数包含author字段(self):
        """作者闸门依赖列表路由返回 author 数组——锁定 select 里带它。"""
        client = _mock_client({"message": {"items": []}})
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            await preprint_discovery._via_crossref(REAL_PUBLISHED_TITLE)
        select = client.get.await_args.kwargs["params"]["select"]
        assert "author" in select.split(",")

    @pytest.mark.asyncio
    async def test_拒非posted_content候选(self):
        """只认 type=posted-content：期刊论文/会议摘要候选一律不认（它们走正式链路）。"""
        client = _mock_client({"message": {"items": [
            {"DOI": "10.1038/s41586-024-07359-3", "type": "journal-article",
             "title": [REAL_PUBLISHED_TITLE]},
            {"DOI": "10.1158/1557-3125.ras23-ia11", "type": "proceedings-article",
             "title": [f"Abstract IA11: {REAL_PREPRINT_TITLE}"]},
        ]}})
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            assert await preprint_discovery._via_crossref(REAL_PUBLISHED_TITLE) is None


class TestTavilyFallback:
    @pytest.mark.asyncio
    async def test_tavily给URL则透传_无作者数据走纯标题路径(self):
        with patch.object(
            preprint_discovery, "_via_europe_pmc", AsyncMock(return_value=None)
        ), patch.object(
            preprint_discovery, "_via_crossref", AsyncMock(return_value=None)
        ), patch(
            "paper_fetch.oa_preprint_discovery.discover_oa_preprint_url",
            AsyncMock(return_value="https://www.researchsquare.com/rs/rs-123/v1.pdf"),
        ):
            out = await preprint_discovery.discover_preprint(
                "10.1038/s41586-024-07359-3", REAL_PUBLISHED_TITLE,
                reference_authors=[REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR],
            )
        assert out is not None
        assert out["via"] == "tavily"
        assert out["url"].startswith("https://www.researchsquare.com/")
        assert out["doi"] is None


class TestDiscoverOrchestration:
    @pytest.mark.asyncio
    async def test_确定性来源优先_失败依次降级(self):
        """Europe PMC 挂 → Crossref 命中 → 不再问 Tavily；参照作者原样透传给各来源。"""
        epmc = AsyncMock(return_value=None)
        crossref = AsyncMock(return_value={
            "doi": "10.1101/x", "url": None, "via": "crossref", "match_score": 0.9,
        })
        tavily = AsyncMock(return_value=None)
        ref_authors = [REAL_FIRST_AUTHOR, REAL_LAST_AUTHOR]
        with patch.object(preprint_discovery, "_via_europe_pmc", epmc), \
             patch.object(preprint_discovery, "_via_crossref", crossref), \
             patch.object(preprint_discovery, "_via_tavily", tavily):
            out = await preprint_discovery.discover_preprint(
                "10.1038/x", REAL_PUBLISHED_TITLE, ref_authors,
            )
        assert out["via"] == "crossref"
        tavily.assert_not_awaited()
        assert epmc.await_args.args[1] == ref_authors
        assert crossref.await_args.args[1] == ref_authors

    @pytest.mark.asyncio
    async def test_防自映射_发现的doi与输入相同则跳过(self):
        """输入本身已是预印本 DOI 时，发现同一篇没有降级意义 → 继续找/最终 None。"""
        with patch.object(preprint_discovery, "_via_europe_pmc", AsyncMock(return_value={
            "doi": "10.1101/2023.01.27.525553", "url": None,
            "via": "europe_pmc", "match_score": 1.0,
        })), patch.object(preprint_discovery, "_via_crossref", AsyncMock(return_value=None)), \
             patch.object(preprint_discovery, "_via_tavily", AsyncMock(return_value=None)):
            out = await preprint_discovery.discover_preprint(
                "10.1101/2023.01.27.525553", REAL_PUBLISHED_TITLE,
            )
        assert out is None

    @pytest.mark.asyncio
    async def test_短标题跳过(self):
        spy = AsyncMock(return_value=None)
        with patch.object(preprint_discovery, "_via_europe_pmc", spy):
            assert await preprint_discovery.discover_preprint("10.1/x", "short") is None
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_单个来源抛异常不打断(self):
        with patch.object(
            preprint_discovery, "_via_europe_pmc", AsyncMock(side_effect=RuntimeError("boom"))
        ), patch.object(preprint_discovery, "_via_crossref", AsyncMock(return_value={
            "doi": "10.1101/y", "url": None, "via": "crossref", "match_score": 0.9,
        })):
            out = await preprint_discovery.discover_preprint("10.1038/x", REAL_PUBLISHED_TITLE)
        assert out["doi"] == "10.1101/y"

    def test_实词提取_滤短词停用词(self):
        words = preprint_discovery._significant_words(
            "3D genomic mapping of human pancreatic tissue"
        )
        assert "3D" not in words and "the" not in words
        assert len(words) <= 3


class TestCrossrefMetaForDoi:
    """评审 M4：只给 DOI 无标题时补题录（标题+作者），一次 API 同取两者。"""

    def _crossref_message(self):
        # 结构按真实 API 响应构造（2026-08-22 实查该 DOI）
        return {"message": {
            "title": [REAL_PUBLISHED_TITLE],
            "author": [
                {"given": "Alicia M.", "family": "Braxton", "sequence": "first"},
                {"given": "Anzeo L.", "family": "Kiemen", "sequence": "additional"},
                {"given": "Laura D.", "family": "Wood", "sequence": "additional"},
            ],
        }}

    @pytest.mark.asyncio
    async def test_一次API补到标题和作者(self):
        client = _mock_client(self._crossref_message())
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            title, authors = await preprint_discovery.crossref_meta_for_doi(
                "10.1038/s41586-024-07359-3"
            )
        assert title == REAL_PUBLISHED_TITLE
        assert authors[0] == REAL_FIRST_AUTHOR and authors[-1] == REAL_LAST_AUTHOR
        assert client.get.await_args.args[0].endswith("/works/10.1038/s41586-024-07359-3")

    @pytest.mark.asyncio
    async def test_不带select参数_回归锁(self):
        """/works/{doi} 单条路由不支持 select（API 返 400，2026-08-22 实测）——
        旧实现传 select=title 导致线上补题录恒失败；此测试防回退。"""
        client = _mock_client(self._crossref_message())
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            await preprint_discovery.crossref_meta_for_doi("10.1038/s41586-024-07359-3")
        assert "select" not in client.get.await_args.kwargs["params"]

    @pytest.mark.asyncio
    async def test_404与空题录返空元组(self):
        for resp_json in ({"message": {"title": []}}, {"message": {}}, {}):
            client = _mock_client(resp_json)
            with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
                assert await preprint_discovery.crossref_meta_for_doi("10.1/x") == (None, [])

    @pytest.mark.asyncio
    async def test_网络异常静默返空元组(self):
        client = _mock_client(status=500)
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            assert await preprint_discovery.crossref_meta_for_doi("10.1/x") == (None, [])

    @pytest.mark.asyncio
    async def test_空doi返空元组不发请求(self):
        client = _mock_client(self._crossref_message())
        with patch.object(proxy_pool.httpx, "AsyncClient", return_value=client):
            assert await preprint_discovery.crossref_meta_for_doi(None) == (None, [])
            assert await preprint_discovery.crossref_meta_for_doi("") == (None, [])
        client.get.assert_not_called()


class TestRealCaseAnchor:
    """真实案例正面锚点回归（任务⑥）：用实查数据端到端验证双闸门不误杀真预印本。

    正式版 10.1038/s41586-024-07359-3（Braxton…Wood）→ 预印本 PPR609497
    （10.1101/2023.01.27.525553，authorString 同上）。标题 0.857 过线 +
    首末作者全吻合 → discover_preprint 必须返回该预印本。
    """

    @pytest.mark.asyncio
    async def test_端到端_真实数据双闸门通过(self):
        # Crossref 补题录：标题 + 参照作者
        meta_client = _mock_client({"message": {
            "title": [REAL_PUBLISHED_TITLE],
            "author": [{"family": "Braxton"}, {"family": "Wood"}],
        }})
        # Europe PMC 搜到真预印本（真实 authorString）
        epmc_client = _mock_client(_epmc_response([
            {"doi": REAL_PREPRINT_DOI, "title": REAL_PREPRINT_TITLE,
             "authorString": REAL_EPMC_AUTHOR_STRING, "source": "PPR"},
        ]))
        clients = iter([meta_client, epmc_client])
        with patch.object(
            proxy_pool.httpx, "AsyncClient", side_effect=lambda **kw: next(clients)
        ):
            title, ref_authors = await preprint_discovery.crossref_meta_for_doi(
                "10.1038/s41586-024-07359-3"
            )
            out = await preprint_discovery.discover_preprint(
                "10.1038/s41586-024-07359-3", title, ref_authors or None,
            )
        assert out is not None
        assert out["doi"] == REAL_PREPRINT_DOI
        assert out["via"] == "europe_pmc"
        assert out["match_score"] >= preprint_discovery._TITLE_MATCH_THRESHOLD
