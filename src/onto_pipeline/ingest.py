"""Runs A1+A2 over a set of documents through the work-unit ledger and persists the result.

A1 and A2 share a single work unit per document: both need the PDF open, and the page
classification is what routes A2. The unit key includes the thresholds that produced it, so
changing a threshold invalidates the cache rather than silently reusing stale output.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .config import Config
from .parse import (
    CAPTION,
    TABLE,
    Block,
    ParsedDocument,
    document_id,
    is_table_caption,
    parse_document,
)
from .telemetry import Ledger, StageResult, UnitResult

STAGE = "A1_A2_ingest"


def discover(corpus_root: Path) -> list[Path]:
    return sorted(corpus_root.rglob("*.pdf"))


def ingest(
    config: Config,
    conn: sqlite3.Connection,
    paths: list[Path],
    ledger: Ledger | None = None,
) -> StageResult:
    ledger = ledger or Ledger(conn, config.execution)
    payloads = [(document_id(path, config.paths.corpus_root), _payload(path, config))
                for path in paths]
    by_id = {doc_id: path for (doc_id, _), path in zip(payloads, paths, strict=True)}

    def worker(payload: dict) -> UnitResult:
        path = by_id[payload["document_id"]]
        parsed = parse_document(path, config, doc_id=payload["document_id"])
        _write_markdown(config, parsed)
        _persist(conn, parsed)
        return UnitResult(output=_summary(parsed))

    return ledger.run(STAGE, payloads, worker)


def _payload(path: Path, config: Config) -> dict:
    return {
        "document_id": document_id(path, config.paths.corpus_root),
        "content_hash": _file_hash(path),
        "parser": config.parser.model_dump(),
        "classification": config.classification.model_dump(),
        "boilerplate": config.boilerplate.model_dump(),
    }


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary(parsed: ParsedDocument) -> dict:
    classes: dict[str, int] = {}
    for page_class in parsed.page_classes:
        classes[page_class.label] = classes.get(page_class.label, 0) + 1
    block_types: dict[str, int] = {}
    for block in parsed.blocks:
        block_types[block.block_type] = block_types.get(block.block_type, 0) + 1
    return {
        "document_id": parsed.document_id,
        "n_pages": parsed.n_pages,
        "markdown_hash": parsed.markdown_hash,
        "markdown_chars": len(parsed.markdown),
        "page_classes": classes,
        "block_types": block_types,
        "boilerplate_blocks": sum(1 for block in parsed.blocks if block.is_boilerplate),
        "unparsed_pages": parsed.unparsed_pages,
        "table_gap_pages": table_gap_pages(parsed.blocks),
    }


def table_gap_pages(blocks) -> list[int]:
    """Pages with a table caption but no extracted table — the table came out as prose."""
    with_table = {block.page for block in blocks if block.block_type == TABLE}
    captioned = {
        block.page
        for block in blocks
        if block.block_type == CAPTION and is_table_caption(block.text)
    }
    return sorted(captioned - with_table)


def markdown_path(config: Config, doc_id: str) -> Path:
    return config.paths.work_dir / "markdown" / f"{doc_id}.md"


def _write_markdown(config: Config, parsed: ParsedDocument) -> None:
    target = markdown_path(config, parsed.document_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(parsed.markdown, encoding="utf-8")


def _persist(conn: sqlite3.Connection, parsed: ParsedDocument) -> None:
    conn.execute(
        "INSERT INTO documents (id, path, content_hash, n_pages, parser_used, parser_version, "
        "markdown_hash) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET path = excluded.path, "
        "content_hash = excluded.content_hash, n_pages = excluded.n_pages, "
        "parser_used = excluded.parser_used, parser_version = excluded.parser_version, "
        "markdown_hash = excluded.markdown_hash",
        (
            parsed.document_id,
            str(parsed.path),
            parsed.content_hash,
            parsed.n_pages,
            parsed.parser_used,
            parsed.parser_version,
            parsed.markdown_hash,
        ),
    )
    conn.execute("DELETE FROM page_classification WHERE document_id = ?", (parsed.document_id,))
    conn.executemany(
        "INSERT INTO page_classification (document_id, page, class, signals) VALUES (?, ?, ?, ?)",
        [
            (parsed.document_id, pc.page, pc.label, json.dumps(pc.signals_json()))
            for pc in parsed.page_classes
        ],
    )
    conn.execute("DELETE FROM blocks WHERE document_id = ?", (parsed.document_id,))
    conn.executemany(
        "INSERT INTO blocks (id, document_id, page, ordinal, bbox, block_type, text, "
        "span_start, span_end, language, language_source, is_boilerplate, asset_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                block.id,
                block.document_id,
                block.page,
                block.ordinal,
                json.dumps([round(value, 2) for value in block.bbox]),
                block.block_type,
                block.text,
                block.span_start,
                block.span_end,
                block.language,
                block.language_source,
                int(block.is_boilerplate),
                block.asset_path,
            )
            for block in parsed.blocks
        ],
    )
    conn.commit()


def load_blocks(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM blocks WHERE document_id = ? ORDER BY page, ordinal", (doc_id,)
    ).fetchall()
    return [dict(row) for row in rows]


def load_page_classes(conn: sqlite3.Connection, doc_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT page, class, signals FROM page_classification WHERE document_id = ? ORDER BY page",
        (doc_id,),
    ).fetchall()
    return [{"page": r["page"], "class": r["class"], "signals": json.loads(r["signals"])}
            for r in rows]


def load_document(conn: sqlite3.Connection, doc_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    return dict(row) if row else None


def load_block_objects(conn: sqlite3.Connection, doc_id: str) -> list[Block]:
    """Blocks as parsed, for the derived stages (chunking) that work on them directly."""
    return [
        Block(
            document_id=row["document_id"],
            page=row["page"],
            ordinal=row["ordinal"],
            bbox=tuple(json.loads(row["bbox"])),
            block_type=row["block_type"],
            text=row["text"],
            span_start=row["span_start"],
            span_end=row["span_end"],
            language=row["language"],
            language_source=row["language_source"],
            is_boilerplate=bool(row["is_boilerplate"]),
            asset_path=row["asset_path"],
        )
        for row in conn.execute(
            "SELECT * FROM blocks WHERE document_id = ? ORDER BY page, ordinal", (doc_id,)
        )
    ]
