"""library_proxy_adapter 传输加固测试（2026-08-21 Nature 22.8MB 事故整改）。

覆盖：
  A. _pdf_complete：%PDF 头 + %%EOF 尾完整性校验（完整 / 断尾 / 伪装 HTML）
  B. _download_pdf_resumable：断流 → Range 续传 → 完整（206/content-range）；
     服务器不支持 Range（200）清空重下；连续停滞放弃；HTTP 错误状态冒泡 reason
  C. fetch_via_library_proxy 的 (bytes|None, reason|None) 元组契约

网络一律 mock，不碰真实代理/账号。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from paper_fetch import library_proxy_adapter as lpa

# 一份「完整」的假 PDF：头是 %PDF-，尾 4KB 内有 %%EOF
_FULL_PDF = b"%PDF-1.7 fake header\n" + b"x" * 200 + b"\n%%EOF\n"


# ---------------------------------------------------------------------------
# mock 基建：流式响应 / 会断流的响应 / 按次序出牌的 client
# ---------------------------------------------------------------------------


class _StreamResp:
    def __init__(self, status_code: int, chunks: list[bytes], headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _BrokenStreamResp(_StreamResp):
    """先吐若干块再断流（模拟 HTTP/2 stream INTERNAL_ERROR / 读超时）。"""

    def __init__(self, status_code, chunks, headers=None, error: Exception | None = None):
        super().__init__(status_code, chunks, headers)
        self._error = error or RuntimeError("peer closed connection mid-stream")

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c
        raise self._error


class _StreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *args):
        return False


class _FakeStreamingClient:
    """按调用次序返回预设响应（或异常）的 httpx client 替身；记录每轮请求头。"""

    def __init__(self, scripted: list):
        self._scripted = list(scripted)
        self.request_headers: list[dict] = []

    def stream(self, _method, _url, headers=None):
        self.request_headers.append(dict(headers or {}))
        return _StreamCtx(self._scripted.pop(0))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _patched_run(client: _FakeStreamingClient):
    """在 mock 掉 httpx.AsyncClient 的环境下跑 _download_pdf_resumable。"""
    with patch.object(lpa.httpx, "AsyncClient", return_value=client):
        return await lpa._download_pdf_resumable(
            "https://www.nature.com/articles/x.pdf",
            referer="https://www.nature.com/articles/x",
            cookies=None,
            proxy_url="http://u:p@libproxy.test:8080",
        )


# ---------------------------------------------------------------------------
# A. 完整性校验
# ---------------------------------------------------------------------------


class TestPdfComplete:
    def test_完整pdf(self):
        assert lpa._pdf_complete(_FULL_PDF) is True

    def test_断尾pdf_缺eof(self):
        assert lpa._pdf_complete(b"%PDF-1.7" + b"x" * 200) is False

    def test_流中断的半截文件不算完整(self):
        # 头部到了但流中途崩断：光有 %PDF- 头不能当完整（续传循环的关键判据）
        assert lpa._pdf_complete(_FULL_PDF[: len(_FULL_PDF) // 2]) is False

    def test_html伪装不算(self):
        assert lpa._pdf_complete(b"<html>%%EOF</html>") is False

    def test_eof在末尾4kb内算完整(self):
        # %%EOF 后允许有少量增量更新尾随字节
        assert lpa._pdf_complete(_FULL_PDF + b"\x00" * 100) is True


# ---------------------------------------------------------------------------
# B. 断点续传循环
# ---------------------------------------------------------------------------


class TestResumeLoop:
    @pytest.mark.asyncio
    async def test_断流后续传拉完整(self):
        """第 1 轮下到一半断流 → 第 2 轮带 Range 续传补齐 → 完整返回。"""
        total = len(_FULL_PDF)
        half = _FULL_PDF[: total // 2]
        rest = _FULL_PDF[total // 2 :]
        client = _FakeStreamingClient(
            [
                _BrokenStreamResp(200, [half]),  # 首轮不带 Range，中途崩断
                _StreamResp(  # 续传轮：206 + content-range
                    206,
                    [rest],
                    headers={"content-range": f"bytes {len(half)}-{total - 1}/{total}"},
                ),
            ]
        )
        pdf, reason = await _patched_run(client)
        assert pdf == _FULL_PDF
        assert reason is None
        # 第二轮请求确实带了 Range 头
        assert client.request_headers[1].get("Range") == f"bytes={len(half)}-"

    @pytest.mark.asyncio
    async def test_服务器不支持range_清空重下(self):
        """续传轮服务器返 200（全量）→ 丢弃半截数据从头下完整。"""
        total = len(_FULL_PDF)
        client = _FakeStreamingClient(
            [
                _BrokenStreamResp(200, [_FULL_PDF[: total // 2]]),
                _StreamResp(200, [_FULL_PDF], headers={"content-length": str(total)}),
            ]
        )
        pdf, reason = await _patched_run(client)
        assert pdf == _FULL_PDF
        assert reason is None

    @pytest.mark.asyncio
    async def test_多轮断流_持续续传直到完整(self):
        """22.8MB 真实场景：连续断流 4 轮，第 5 轮补齐（实测 curl 5 轮拉完整）。"""
        parts = [_FULL_PDF[i : i + 40] for i in range(0, len(_FULL_PDF), 40)]
        total = len(_FULL_PDF)
        scripted = [
            _BrokenStreamResp(200, parts[:1]),
            # content-range 用真实格式 "bytes {start}-{end}/{total}"（评审 nit：* 形态只用于 416）
            _BrokenStreamResp(
                206, parts[1:2], headers={"content-range": f"bytes 40-{2 * 40 - 1}/{total}"}
            ),
            _BrokenStreamResp(
                206, parts[2:3], headers={"content-range": f"bytes 80-{3 * 40 - 1}/{total}"}
            ),
            _BrokenStreamResp(
                206, parts[3:4], headers={"content-range": f"bytes 120-{4 * 40 - 1}/{total}"}
            ),
            _StreamResp(
                206, parts[4:], headers={"content-range": f"bytes 160-{total - 1}/{total}"}
            ),
        ]
        client = _FakeStreamingClient(scripted)
        pdf, reason = await _patched_run(client)
        assert pdf == _FULL_PDF
        assert reason is None
        assert len(client.request_headers) == 5

    @pytest.mark.asyncio
    async def test_连续停滞放弃(self):
        """每轮都断流且零字节进展（连接立即死）→ 攒满停滞轮数放弃，reason=stalled。"""
        client = _FakeStreamingClient(
            [_BrokenStreamResp(200, []) for _ in range(lpa._RESUME_MAX_STALLS)]
        )
        pdf, reason = await _patched_run(client)
        assert pdf is None
        assert reason == "stalled"

    @pytest.mark.asyncio
    async def test_http错误状态冒泡reason(self):
        client = _FakeStreamingClient([_StreamResp(403, [])])
        pdf, reason = await _patched_run(client)
        assert pdf is None
        assert reason == "http_403"

    @pytest.mark.asyncio
    async def test_下完但不完整_续传到完整(self):
        """content-length 已满足但缺 %%EOF（比如服务器给的长度本身不对）→ 继续续传轮。"""
        total = len(_FULL_PDF)
        client = _FakeStreamingClient(
            [
                # 声称 total 长度但实际只吐了前半（无 EOF）
                _StreamResp(200, [_FULL_PDF[: total // 2]], headers={"content-length": str(total)}),
                # 续传轮把剩余补上
                _StreamResp(
                    206,
                    [_FULL_PDF[total // 2 :]],
                    headers={"content-range": f"bytes {total // 2}-{total - 1}/{total}"},
                ),
            ]
        )
        pdf, reason = await _patched_run(client)
        assert pdf == _FULL_PDF
        assert client.request_headers[1].get("Range") == f"bytes={total // 2}-"


# ---------------------------------------------------------------------------
# C. fetch_via_library_proxy 元组契约（成功 landing 即 PDF / 无目标）
# ---------------------------------------------------------------------------


class TestTupleContract:
    @pytest.mark.asyncio
    async def test_无目标返None加reason(self):
        pdf, reason = await lpa.fetch_via_library_proxy(
            doi=None,
            landing_url=None,
            username="u",
            password="p",
            proxy_host_port="proxy:8080",
        )
        assert pdf is None
        assert reason == "no_target"

    @pytest.mark.asyncio
    async def test_landing即pdf_返回字节与None(self):
        """代理抓 landing 拿到的就是 PDF → (bytes, None)。"""
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.headers = {}
        resp.content = _FULL_PDF
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch.object(lpa.httpx, "AsyncClient", return_value=client):
            pdf, reason = await lpa.fetch_via_library_proxy(
                doi="10.1038/x",
                landing_url="https://nature.com/x",
                username="u",
                password="p",
                proxy_host_port="proxy:8080",
            )
        assert pdf == _FULL_PDF
        assert reason is None

    @pytest.mark.asyncio
    async def test_经代理仍付费墙_reason可分辨(self):
        """landing HTML 命中付费墙签名 → reason=paywall_no_subscription（≠传输失败）。"""
        resp = type("R", (), {})()
        resp.status_code = 200
        resp.content = b"<html>subscription required</html>"
        resp.text = "<html>Subscription required to view this article</html>"
        resp.headers = {"content-type": "text/html"}
        resp.url = "https://nature.com/x"
        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch.object(lpa.httpx, "AsyncClient", return_value=client):
            pdf, reason = await lpa.fetch_via_library_proxy(
                doi="10.1038/x",
                landing_url="https://nature.com/x",
                username="u",
                password="p",
                proxy_host_port="proxy:8080",
            )
        assert pdf is None
        assert reason == "paywall_no_subscription"


class TestResume416:
    @pytest.mark.asyncio
    async def test_416不读body_清缓冲后全量重下(self):
        """评审 M2：Range 不可满足（416）时 body 是 HTML 错误页——读进缓冲区会污染
        已收字节、后续 Range 偏移全错。必须不读 body、丢弃已收字节、下一轮全量重下。"""
        total = len(_FULL_PDF)
        half = _FULL_PDF[: total // 2]
        err_page = _StreamResp(  # 若被错误读取，HTML 会进 buf 破坏 Range 偏移
            416,
            [b"<html>Range Not Satisfiable</html>"],
            headers={"content-range": f"bytes */{total}"},
        )
        client = _FakeStreamingClient(
            [
                _BrokenStreamResp(200, [half]),  # 首轮断流，收一半
                err_page,  # 续传轮 416：不可读 body
                _StreamResp(200, [_FULL_PDF], headers={"content-length": str(total)}),  # 全量重下
            ]
        )
        pdf, reason = await _patched_run(client)
        assert pdf == _FULL_PDF
        assert reason is None
        # 第三轮请求不再带 Range（416 后清缓冲 → 全量）
        assert "Range" not in client.request_headers[2]

    @pytest.mark.asyncio
    async def test_反复416按停滞放弃(self):
        """服务器一直 416（文件在变/异常）→ 无进展轮攒满停滞上限放弃，不无限白跑。"""
        total = len(_FULL_PDF)
        client = _FakeStreamingClient(
            [
                _BrokenStreamResp(200, [_FULL_PDF[: total // 2]]),
                *[
                    _StreamResp(
                        416, [b"<html>err</html>"], headers={"content-range": f"bytes */{total}"}
                    )
                    for _ in range(lpa._RESUME_MAX_STALLS)
                ],
            ]
        )
        pdf, reason = await _patched_run(client)
        assert pdf is None
        assert reason == "stalled"
