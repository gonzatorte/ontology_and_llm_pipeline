"""El informe de evaluación del parser (`DELIVERABLES-PENDING-PARSER-EVAL`).

No tenía ninguna prueba, y por eso dos fallas convivieron sin que nadie las viera: leía las tres
tablas sin pasar la sesión —levantaba `TypeError` desde que `SESSION-SCOPED-DATA` agregó la
columna— y resolvía los recortes como rutas del disco, que dejó de ser cierto cuando pasaron a
ser artefactos. Las dos se ven al correrlo y ninguna al mirar el código de al lado.
"""

from __future__ import annotations

import json

import pytest

from onto_pipeline import sessions
from onto_pipeline.artifacts import Artifacts
from onto_pipeline.db import connect
from onto_pipeline.report import build_report

SESSION = "test-1"


@pytest.fixture
def ingested(tmp_path, object_store):
    """Un documento ya parseado, con un bloque de figura que apunta a un recorte del almacén."""
    conn = connect(tmp_path / "work")
    sessions.create(conn, use_case="test")
    artifacts = Artifacts(object_store, SESSION)
    artifacts.markdown("d1").write_text("El muestreo teórico guía la recolección.\n\n![](fig)")
    crop = artifacts.crop("d1", "p1_f0.png")
    crop.write_bytes(b"\x89PNG\r\n_recorte_")

    pdf = tmp_path / "d1.pdf"
    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    doc.save(pdf)
    doc.close()

    conn.execute(
        "INSERT INTO documents (id, session_id, path, content_hash, n_pages, parser_used, "
        "parser_version, markdown_hash, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("d1", SESSION, str(pdf), "h", 1, "pymupdf", "1", "sha256:x", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO page_classification (document_id, session_id, page, class, signals) "
        "VALUES (?, ?, ?, ?, ?)",
        ("d1", SESSION, 1, "born_digital", json.dumps({})),
    )
    for ordinal, (kind, text, asset) in enumerate((
        ("paragraph", "El muestreo teórico guía la recolección.", None),
        ("figure", "", crop.key),
    )):
        conn.execute(
            "INSERT INTO blocks (id, session_id, document_id, page, ordinal, block_type, bbox, "
            "text, is_boilerplate, span_start, span_end, asset_path, language, language_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"b{ordinal}", SESSION, "d1", 1, ordinal, kind, json.dumps([0, 0, 10, 10]),
             text, 0, 0, len(text), asset, "es", "declared"),
        )
    conn.commit()
    return conn, artifacts


def test_the_report_reads_the_tables_of_its_own_session(ingested):
    """Las tres lecturas van con la sesión, como todo dato derivado (`SESSION-SCOPED-DATA`).
    Sin eso el comando no fallaba a medias: no arrancaba."""
    conn, artifacts = ingested

    written = build_report(artifacts, conn, "d1", session_id=SESSION)

    assert written.exists()
    assert "muestreo teórico" in written.read_text()


def test_a_figure_crop_is_embedded_from_the_store_and_not_from_disk(ingested):
    """El bloque guarda la **clave** del recorte, no una ruta: una ruta absoluta metería el
    filesystem de la máquina que parseó en un informe que se lee en otra. Resolverla contra el
    disco no fallaba —decía «figure crop missing», que se parece a un PDF sin figuras."""
    conn, artifacts = ingested

    html = build_report(artifacts, conn, "d1", session_id=SESSION).read_text()

    assert "figure crop missing" not in html
    assert "data:image/png;base64," in html


def test_a_document_of_another_session_is_not_found(ingested):
    """La otra mitad del alcance por sesión: pedir el informe de un documento ajeno no devuelve
    el informe de otro, dice que no está."""
    conn, artifacts = ingested

    with pytest.raises(KeyError):
        build_report(artifacts, conn, "d1", session_id="otra")
