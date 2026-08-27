"""CLI 入口：``python -m paper_fetch --doi 10.xxxx --out ./paper.pdf``。

链路段进度打到 stderr（人看的），结果 JSON 打到 stdout（脚本友好的单行摘要）。
退出码：0 = 成功下载；2 = 付费墙（auth_required，附 landing_url）；3 = 其他失败；
1 = 参数/环境错误。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paper-fetch",
        description="论文 PDF 下载链：正式版优先 → 预印本回落 → 付费墙信号（开放获取优先，Sci-Hub 默认关）",
    )
    parser.add_argument("--doi", default=None, help="论文 DOI（如 10.1371/journal.pone.0300000）")
    parser.add_argument("--url", default=None, help="论文页 URL（landing page 或 PDF 直链）")
    parser.add_argument("--oa-url", dest="oa_url", default=None, help="开放获取 PDF 直链")
    parser.add_argument("--title", default=None, help="论文标题（预印本发现/身份核验锚点用）")
    parser.add_argument("--out", default=None, help="PDF 输出路径（默认 ./<doi 或标题>.pdf）")
    parser.add_argument(
        "--max-pdf-mb", type=int, default=None,
        help="PDF 大小上限 MB（默认 FetchConfig/环境变量）",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="打 DEBUG 日志（默认 INFO，链路进度始终可见）",
    )
    return parser


def _default_out(args: argparse.Namespace) -> Path:
    stem = (args.doi or args.title or "paper").strip()
    # DOI 里的 / 会变成路径分隔符，替换成 _
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in stem)[:120]
    return Path(f"{safe}.pdf")


async def _run(args: argparse.Namespace) -> int:
    from .config import FetchConfig
    from .service import download_pdf

    config = FetchConfig()
    if args.max_pdf_mb is not None:
        config.max_pdf_mb = args.max_pdf_mb

    async def on_stage(stage: str) -> None:
        print(f"[paper-fetch] ▶ {stage}", file=sys.stderr)

    result = await download_pdf(
        args.doi,
        args.url,
        args.oa_url,
        title=args.title,
        on_stage=on_stage,
        config=config,
    )

    summary = {
        "success": result.get("success"),
        "source": result.get("source"),
        "size_bytes": result.get("size_bytes"),
        "error": result.get("error"),
        "tried_sources": result.get("tried_sources"),
        "auth_required": result.get("auth_required"),
        "landing_url": result.get("landing_url"),
        "failure_detail": result.get("failure_detail"),
        "message": result.get("message"),
        "delivered_version": result.get("delivered_version"),
        "requested_doi": result.get("requested_doi"),
        "delivered_doi": result.get("delivered_doi"),
        "pdf_identity": result.get("pdf_identity"),
        "content_url": result.get("content_url"),
    }
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)

    if result.get("success"):
        out = Path(args.out) if args.out else _default_out(args)
        out.write_bytes(result["pdf_bytes"])
        print(f"[paper-fetch] 已保存 {out}（{result['size_bytes']} 字节，来源 {result['source']}）", file=sys.stderr)
        return 0
    if result.get("auth_required"):
        return 2
    return 3


def main() -> None:
    args = _build_parser().parse_args()
    if not (args.doi or args.url or args.oa_url or args.title):
        print("错误：至少提供 --doi / --url / --oa-url / --title 之一", file=sys.stderr)
        sys.exit(1)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        sys.exit(asyncio.run(_run(args)))
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
