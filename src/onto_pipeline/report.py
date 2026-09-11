"""DELIVERABLES-PENDING-PARSER-EVAL — manual parser evaluation report, the advance
criterion of BUILD-STEP-1.

One self-contained HTML file per document: the rendered page beside what the parser made of
it, so that criterion ("visual inspection: the parse is acceptable") can
actually be exercised. Everything the parse decided is visible — page class with the signals
that produced it, block type, bbox, language and its source, Markdown span, and which blocks
the boilerplate filter removed.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

import pymupdf

from .config import Config
from .ingest import load_blocks, load_document, load_page_classes, markdown_path
from .parse import CAPTION, TABLE, is_table_caption
from .store import Store

_KATEX_CSS = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css"
_KATEX_JS = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"
_KATEX_AUTO = "https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"

_STYLE = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 24px; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: #666; font-size: 12px; margin-bottom: 24px; }
.meta code { font-size: 12px; }
.page { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px;
        border-top: 2px solid #8884; padding: 20px 0; align-items: start; }
.page > div { min-width: 0; }
.render img { width: 100%; border: 1px solid #8884; }
.pageclass { font-size: 12px; margin-bottom: 8px; }
.pill { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11px;
        font-weight: 600; }
.born_digital { background: #22863a22; color: #22863a; }
.scan { background: #d7382222; color: #d73822; }
.uncertain { background: #b0850022; color: #b08500; }
table.signals { border-collapse: collapse; font-size: 11px; margin-top: 6px; }
table.signals td { border: 1px solid #8883; padding: 1px 6px; }
.block { border-left: 3px solid #8886; padding: 2px 0 2px 10px; margin: 0 0 12px; }
.block .tag { font-size: 10px; color: #777; font-family: ui-monospace, monospace;
              display: block; margin-bottom: 2px; }
.block.heading > .body { font-weight: 700; font-size: 15px; }
.block.caption > .body { font-style: italic; color: #555; }
.block.boilerplate { opacity: .45; border-left-color: #d73822; }
.block.unparsed { border-left-color: #d73822; background: #d738220f; padding: 8px 10px; }
.block img { max-width: 100%; border: 1px solid #8884; }
table.data { border-collapse: collapse; font-size: 12px; width: 100%; }
table.data td, table.data th { border: 1px solid #8886; padding: 3px 6px; text-align: left; }
.warn { background: #b0850022; color: #8a6600; border-left: 3px solid #b08500;
        padding: 6px 10px; font-size: 12px; margin-bottom: 12px; }
details { margin-top: 12px; font-size: 12px; }
pre { white-space: pre-wrap; word-break: break-word; background: #8881; padding: 8px;
      font-size: 11px; }
"""


def build_report(config: Config, conn: Store, doc_id: str, dpi: int = 100) -> Path:
    document = load_document(conn, doc_id)
    if document is None:
        raise KeyError(f"document {doc_id} has not been ingested")
    blocks = load_blocks(conn, doc_id)
    page_classes = {entry["page"]: entry for entry in load_page_classes(conn, doc_id)}
    markdown = markdown_path(config, doc_id).read_text(encoding="utf-8")

    by_page: dict[int, list[dict]] = {}
    for block in blocks:
        by_page.setdefault(block["page"], []).append(block)

    gaps = _table_gap_pages(by_page)
    with pymupdf.open(document["path"]) as pdf:
        pages = [
            _page_section(
                pdf[number - 1], number, page_classes.get(number),
                by_page.get(number, []), markdown, dpi, number in gaps,
                config.paths.work_dir,
            )
            for number in sorted(page_classes)
        ]

    needs_katex = any(block["block_type"] == "formula" for block in blocks)
    body = _header(document, blocks, markdown, gaps) + "\n".join(pages)
    target = config.paths.work_dir / "reports" / f"{doc_id}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_document(document["id"], body, needs_katex), encoding="utf-8")
    return target


def _document(title: str, body: str, needs_katex: bool) -> str:
    katex = (
        f'<link rel="stylesheet" href="{_KATEX_CSS}">'
        f'<script defer src="{_KATEX_JS}"></script>'
        f'<script defer src="{_KATEX_AUTO}" '
        'onload="renderMathInElement(document.body)"></script>'
        if needs_katex
        else ""
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>parser report — {html.escape(title)}</title>"
        f"<style>{_STYLE}</style>{katex}</head><body>{body}</body></html>"
    )


def _table_gap_pages(by_page: dict[int, list[dict]]) -> set[int]:
    gaps = set()
    for number, blocks in by_page.items():
        has_table = any(block["block_type"] == TABLE for block in blocks)
        has_caption = any(
            block["block_type"] == CAPTION and is_table_caption(block["text"]) for block in blocks
        )
        if has_caption and not has_table:
            gaps.add(number)
    return gaps


def _header(document: dict, blocks: list[dict], markdown: str, gaps: set[int]) -> str:
    counts: dict[str, int] = {}
    for block in blocks:
        counts[block["block_type"]] = counts.get(block["block_type"], 0) + 1
    boilerplate = sum(1 for block in blocks if block["is_boilerplate"])
    summary = ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))
    warning = (
        f"<div class='warn'>table caption without an extracted table on page(s) "
        f"{', '.join(str(page) for page in sorted(gaps))} — the table came out as prose. "
        f"Spec 4.2 puts table fidelity first; those pages are candidates for the VLM parser."
        f"</div>"
        if gaps
        else ""
    )
    return (
        f"<h1>{html.escape(Path(document['path']).name)}</h1>"
        f"<div class='meta'><code>{html.escape(document['id'])}</code> · "
        f"{document['n_pages']} pages · parser {html.escape(document['parser_used'])} "
        f"{html.escape(document['parser_version'] or '')} · "
        f"{len(markdown)} Markdown chars · blocks: {html.escape(summary)} · "
        f"{boilerplate} boilerplate · "
        f"<code>{html.escape(document['markdown_hash'] or '')}</code></div>{warning}"
    )


def _page_section(
    page: pymupdf.Page, number: int, page_class: dict | None, blocks: list[dict],
    markdown: str, dpi: int, table_gap: bool, work_dir: Path,
) -> str:
    render = page.get_pixmap(dpi=dpi).tobytes("png")
    encoded = base64.b64encode(render).decode("ascii")
    rendered_blocks = (
        "".join(_block_html(block, work_dir) for block in blocks)
        or "<p><em>no blocks</em></p>"
    )
    slice_ = _markdown_slice(markdown, blocks)
    gap = "<div class='warn'>table caption, no extracted table</div>" if table_gap else ""
    return (
        f"<section class='page'><div class='render'>"
        f"{_page_class_html(number, page_class)}"
        f"<img alt='page {number}' src='data:image/png;base64,{encoded}'></div>"
        f"<div class='parsed'>{gap}{rendered_blocks}"
        f"<details><summary>raw Markdown for this page</summary>"
        f"<pre>{html.escape(slice_)}</pre></details></div></section>"
    )


def _page_class_html(number: int, page_class: dict | None) -> str:
    if page_class is None:
        return f"<div class='pageclass'>page {number}</div>"
    signals = page_class["signals"]
    rows = "".join(
        f"<tr><td>{html.escape(str(key))}</td><td>{html.escape(_format(value))}</td></tr>"
        for key, value in signals.items()
        if value not in ("", 0, 0.0)
    )
    return (
        f"<div class='pageclass'>page {number} · "
        f"<span class='pill {page_class['class']}'>{page_class['class']}</span> · "
        f"rule <code>{html.escape(signals.get('reason', ''))}</code>"
        f"<table class='signals'>{rows}</table></div>"
    )


def _format(value) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _block_html(block: dict, work_dir: Path) -> str:
    kind = block["block_type"]
    classes = f"block {kind}" + (" boilerplate" if block["is_boilerplate"] else "")
    span = (
        f"[{block['span_start']}:{block['span_end']}]"
        if block["span_start"] is not None
        else "no span (excluded from Markdown)"
    )
    tag = (
        f"{kind} · bbox {json.loads(block['bbox'])} · "
        f"{block['language'] or '?'}/{block['language_source'] or '?'} · {span}"
    )
    return (
        f"<div class='{classes}'><span class='tag'>{html.escape(tag)}</span>"
        f"<div class='body'>{_body_html(block, work_dir)}</div></div>"
    )


def _body_html(block: dict, work_dir: Path) -> str:
    kind = block["block_type"]
    if kind == "figure":
        asset = _resolve_asset(block["asset_path"], work_dir)
        if asset and asset.exists():
            encoded = base64.b64encode(asset.read_bytes()).decode("ascii")
            return f"<img alt='figure' src='data:image/png;base64,{encoded}'>"
        return "<em>figure crop missing</em>"
    if kind == "table":
        return _table_html(block["text"])
    if kind == "formula":
        return f"\\[{html.escape(block['text'])}\\]"
    if kind == "unparsed":
        return f"<strong>{html.escape(block['text'])}</strong>"
    return html.escape(block["text"])


def _resolve_asset(asset_path: str | None, work_dir: Path) -> Path | None:
    """Asset paths are stored relative to the work directory so the Markdown stays portable."""
    if not asset_path:
        return None
    path = Path(asset_path)
    return path if path.is_absolute() else work_dir / path


def _table_html(markdown: str) -> str:
    rows = [line for line in markdown.splitlines() if line.strip().startswith("|")]
    cells = []
    for row in rows:
        values = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if all(set(value) <= {"-", ":", " "} and value for value in values):
            continue  # the header separator row
        cells.append(values)
    if not cells:
        return f"<pre>{html.escape(markdown)}</pre>"
    head = "".join(f"<th>{html.escape(value)}</th>" for value in cells[0])
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(value)}</td>" for value in row) + "</tr>"
        for row in cells[1:]
    )
    return f"<table class='data'><tr>{head}</tr>{body}</table>"


def _markdown_slice(markdown: str, blocks: list[dict]) -> str:
    spans = [b for b in blocks if b["span_start"] is not None]
    if not spans:
        return ""
    return markdown[spans[0]["span_start"]: spans[-1]["span_end"]]
