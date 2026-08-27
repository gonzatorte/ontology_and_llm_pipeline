"""Persistent stores: mention layer (SCHEMAS-MENTIONS), work units (8.3), decisions (8.4).

The mention layer is the central persistent artifact; everything else is regenerated from it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS mentions (
  id                TEXT PRIMARY KEY,
  document_id       TEXT NOT NULL,
  page              INTEGER NOT NULL,
  bbox              TEXT,
  span_start        INTEGER,
  span_end          INTEGER,
  surface_text      TEXT NOT NULL,
  block_type        TEXT,
  language          TEXT,
  language_source   TEXT,
  coref_group       TEXT,
  candidate_entity  TEXT,
  status            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id            TEXT PRIMARY KEY,
  path          TEXT, content_hash TEXT,
  n_pages       INTEGER, parser_used TEXT, parser_version TEXT,
  markdown_hash TEXT,
  -- Retention set (EVAL-PIPELINE): parsed, because the annotation offsets index
  -- the Markdown PREP-PARSE
  -- produces, but never fed to the process. Without this flag the held-out documents leak
  -- into ITER-EXTRACT and the evaluation measures the pipeline against its own training material.
  held_out      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS page_classification (
  document_id TEXT, page INTEGER,
  class       TEXT,
  signals     TEXT,
  PRIMARY KEY (document_id, page)
);

-- PREP-PARSE output. Mentions (ITER-EXTRACT) are anchored on these spans; the
-- spec's mention row carries
-- page/bbox/block_type/language, which have to come from somewhere.
CREATE TABLE IF NOT EXISTS blocks (
  id             TEXT PRIMARY KEY,
  document_id    TEXT NOT NULL,
  page           INTEGER NOT NULL,
  ordinal        INTEGER NOT NULL,
  bbox           TEXT,
  block_type     TEXT NOT NULL,
  text           TEXT NOT NULL,
  span_start     INTEGER,           -- NULL for boilerplate: kept as provenance, absent
  span_end       INTEGER,           -- from the Markdown handed downstream
  language       TEXT,
  language_source TEXT,
  is_boilerplate INTEGER NOT NULL DEFAULT 0,
  asset_path     TEXT
);
CREATE INDEX IF NOT EXISTS idx_blocks_doc ON blocks(document_id, page, ordinal);

CREATE TABLE IF NOT EXISTS work_units (
  key            TEXT PRIMARY KEY,
  stage          TEXT NOT NULL,
  iteration      INTEGER,
  status         TEXT NOT NULL,
  input_hash     TEXT,
  output         TEXT,
  attempts       INTEGER DEFAULT 0,
  error          TEXT,
  in_tokens      INTEGER,
  out_tokens     INTEGER,
  created_at     TEXT, completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wu_stage ON work_units(stage, iteration, status);

CREATE TABLE IF NOT EXISTS decisions (
  id                TEXT PRIMARY KEY,
  iteration         INTEGER,
  branch_id         TEXT,
  status            TEXT,
  axis              TEXT,
  comment           TEXT,
  normalized_axioms TEXT,
  ontology_state    TEXT,
  created_at        TEXT
);
"""


def connect(work_dir: Path) -> sqlite3.Connection:
    work_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(work_dir / "pipeline.sqlite3")
    conn.row_factory = sqlite3.Row
    # WAL: one writer and any number of concurrent readers, instead of a lock that excludes
    # both. The pipeline is sequential by design — the ledger puts a barrier between stages —
    # but ITER-EXTRACT makes hundreds of API calls whose latency dwarfs a write, so parallelising
    # those
    # only needs readers not to block. It is also more robust to an interrupted run.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # GRADED-FEEDBACK nombra el rechazo blando `not_chosen`; el código escribió `rejected` hasta
    # el 2026-09-10. Un almacén viejo se corrige solo. Las dos tablas las crea `branching`, así
    # que en un almacén que todavía no ramificó no existen.
    existing = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table in existing & {"branches", "decisions"}:
        conn.execute(f"UPDATE {table} SET status = 'not_chosen' WHERE status = 'rejected'")
    # Sin este commit la migración deja abierta una transacción de escritura, y en WAL eso
    # bloquea a cualquier segunda conexión sobre el mismo almacén — que es lo que pasa apenas
    # una interfaz abre el almacén dos veces.
    conn.commit()
    return conn
