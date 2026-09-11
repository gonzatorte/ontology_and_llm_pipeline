"""Persistent stores: mention layer (SCHEMAS-MENTIONS), work units (8.3), decisions (8.4).

The mention layer is the central persistent artifact; everything else is regenerated from it.
"""

from __future__ import annotations

from pathlib import Path

from .store import Store, open_sqlite, open_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS mentions (
  id                TEXT NOT NULL,
  -- La sesión de usuario que la extrajo. El id de mención deriva del de documento, que deriva
  -- del path relativo al corpus: dos sesiones sobre el mismo corpus generan los mismos ids, así
  -- que sin esta columna la segunda le borra las menciones a la primera.
  session_id        TEXT NOT NULL,
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
  status            TEXT NOT NULL,
  PRIMARY KEY (session_id, id)
);

CREATE TABLE IF NOT EXISTS documents (
  id            TEXT NOT NULL,
  session_id    TEXT NOT NULL,
  path          TEXT, content_hash TEXT,
  n_pages       INTEGER, parser_used TEXT, parser_version TEXT,
  markdown_hash TEXT,
  -- Retention set (EVAL-PIPELINE): parsed, because the annotation offsets index
  -- the Markdown PREP-PARSE
  -- produces, but never fed to the process. Without this flag the held-out documents leak
  -- into ITER-EXTRACT and the evaluation measures the pipeline against its own training material.
  held_out      INTEGER NOT NULL DEFAULT 0,
  -- El orden en que el documento entró al proceso. La curva de acumulación de `EVAL-STOPPING`
  -- es función de ese orden y de nada más, así que tiene que ser el real: ordenar por id
  -- dibujaría la curva de un proceso que nunca pasó. Era `rowid`, que sólo existe en SQLite.
  ingested_at   TEXT,
  PRIMARY KEY (session_id, id)
);

CREATE TABLE IF NOT EXISTS page_classification (
  session_id  TEXT NOT NULL,
  document_id TEXT, page INTEGER,
  class       TEXT,
  signals     TEXT,
  PRIMARY KEY (session_id, document_id, page)
);

-- PREP-PARSE output. Mentions (ITER-EXTRACT) are anchored on these spans; the
-- spec's mention row carries
-- page/bbox/block_type/language, which have to come from somewhere.
CREATE TABLE IF NOT EXISTS blocks (
  id             TEXT NOT NULL,
  session_id     TEXT NOT NULL,
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
  asset_path     TEXT,
  PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_doc ON blocks(session_id, document_id, page, ordinal);

-- Caché, checkpoint y telemetría a la vez. La clave `key` es content-addressed —cubre etapa,
-- versión del prompt, modelo, effort, temperatura y hash del input— así que **el resultado se
-- comparte entre sesiones** y nadie paga dos veces por la misma pregunta. Lo que **no** se
-- comparte es la contabilidad: el barrier pregunta si quedan unidades propias corriendo, y con
-- una fila por (sesión, clave) una sesión interrumpida no bloquea a las demás.
CREATE TABLE IF NOT EXISTS work_units (
  key            TEXT NOT NULL,
  session_id     TEXT NOT NULL,
  stage          TEXT NOT NULL,
  iteration      INTEGER,
  status         TEXT NOT NULL,
  input_hash     TEXT,
  output         TEXT,
  attempts       INTEGER DEFAULT 0,
  error          TEXT,
  in_tokens      INTEGER,
  out_tokens     INTEGER,
  created_at     TEXT, completed_at TEXT,
  PRIMARY KEY (session_id, key)
);
CREATE INDEX IF NOT EXISTS idx_wu_stage ON work_units(session_id, stage, iteration, status);
CREATE INDEX IF NOT EXISTS idx_wu_key ON work_units(key, status);

CREATE TABLE IF NOT EXISTS decisions (
  id                TEXT NOT NULL,
  -- Sin esto, `precedents_like` mete las decisiones de una sesión en el prompt de otra: el
  -- historial de `ITER-FEEDBACK` de alguien decidiendo en la corrida ajena.
  session_id        TEXT NOT NULL,
  iteration         INTEGER,
  branch_id         TEXT,
  status            TEXT,
  axis              TEXT,
  comment           TEXT,
  normalized_axioms TEXT,
  ontology_state    TEXT,
  created_at        TEXT,
  PRIMARY KEY (session_id, id)
);
"""


def prepare(conn: Store) -> Store:
    """El esquema y las correcciones que un almacén viejo necesita, contra cualquier motor."""
    conn.script(SCHEMA)
    # GRADED-FEEDBACK nombra el rechazo blando `not_chosen`; el código escribió `rejected` hasta
    # el 2026-09-10. Un almacén viejo se corrige solo. Las dos tablas las crea `branching`, así
    # que en un almacén que todavía no ramificó no existen.
    for table in ("branches", "decisions"):
        if conn.table_exists(table):
            conn.execute(
                f"UPDATE {table} SET status = 'not_chosen' WHERE status = 'rejected'"
            )
    # Sin este commit la migración deja abierta una transacción de escritura, y en WAL eso
    # bloquea a cualquier segunda conexión sobre el mismo almacén — que es lo que pasa apenas
    # una interfaz abre el almacén dos veces.
    conn.commit()
    return conn


def connect(work_dir: Path) -> Store:
    """El almacén SQLite bajo `work_dir`.

    Es la puerta corta, y la que usan los tests: un archivo por directorio temporal, sin red ni
    servidor. Para elegir el motor por configuración está `open_configured`.
    """
    return prepare(open_sqlite(work_dir / "pipeline.sqlite3"))


def open_configured(database, work_dir: Path) -> Store:
    """El almacén que pide la configuración: `database.backend` y `database.dsn`.

    Devuelve un `Store` y no una conexión de un motor: los módulos no saben contra cuál corren,
    que es lo que permite que dos sesiones de usuario escriban a la vez contra Postgres sin que
    ninguno de los doce que tienen SQL cambie una línea — y lo que deja a los tests en SQLite.
    """
    return prepare(
        open_store(database.backend, path=work_dir / "pipeline.sqlite3", dsn=database.dsn)
    )
