from __future__ import annotations

import json
import re

import pytest
from rdflib import Graph

from onto_pipeline import annotate
from onto_pipeline.db import connect
from onto_pipeline.ingest import (
    held_out_documents,
    ingest,
    process_documents,
    set_held_out,
)

ONTOLOGY = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix : <http://example.org/onto#> .
:Technique a owl:Class ; skos:prefLabel "Technique"@en ;
    skos:definition "A procedure applied within a strategy."@en .
:Subject a owl:Class ; skos:prefLabel "Subject"@en .
:Unlabelled a owl:Class .
"""


def test_seed_classes_carry_label_and_gloss():
    classes = annotate.seed_classes(Graph().parse(data=ONTOLOGY, format="turtle"))
    assert [c.label for c in classes] == ["Subject", "Technique"], "unlabelled ones are skipped"
    assert classes[1].gloss.startswith("A procedure")
    assert classes[0].gloss == ""


def test_page_index_skips_blocks_with_no_span():
    blocks = [
        {"span_start": 0, "span_end": 10, "page": 1},
        {"span_start": None, "span_end": None, "page": 1},   # boilerplate
        {"span_start": 12, "span_end": 30, "page": 2},
    ]
    assert annotate.page_index(blocks) == [[0, 10, 1], [12, 30, 2]]


def test_the_tool_embeds_the_markdown_verbatim(tmp_path):
    """The offsets index this string character for character, so anything that rewrote it
    would silently shift every annotation."""
    markdown = 'A <table> & "quotes" — ünïcode\n\nsecond paragraph'
    target = annotate.build(
        doc_id="doc1", markdown=markdown, markdown_hash="sha256:abc",
        classes=annotate.seed_classes(Graph().parse(data=ONTOLOGY, format="turtle")),
        pages=[[0, 46, 1]], target=tmp_path / "doc1.html",
    )
    html = target.read_text(encoding="utf-8")
    body = re.search(r'<div id="text">(.*?)</div>', html, re.DOTALL).group(1)
    unescaped = body.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    assert unescaped == markdown


def test_the_payload_carries_what_the_export_has_to_reproduce(tmp_path):
    target = annotate.build(
        doc_id="doc1", markdown="text", markdown_hash="sha256:abc",
        classes=annotate.seed_classes(Graph().parse(data=ONTOLOGY, format="turtle")),
        pages=[[0, 4, 1]], target=tmp_path / "doc1.html",
    )
    html = target.read_text(encoding="utf-8")
    payload = json.loads(re.search(r"const DATA = (\{.*?\});", html, re.DOTALL).group(1))
    assert payload["doc_id"] == "doc1"
    assert payload["markdown_hash"] == "sha256:abc"
    assert {c["label"] for c in payload["classes"]} == {"Technique", "Subject"}


def test_held_out_documents_are_kept_out_of_the_process(two_column_pdf, config, tmp_path):
    """They are parsed — the offsets index PREP-PARSE's Markdown — but must never reach
    ITER-EXTRACT, or the
    evaluation measures the pipeline against its own input (EVAL-PIPELINE)."""
    conn = connect(config.paths.work_dir)
    result = ingest(config, conn, [two_column_pdf])
    doc_id = next(iter(result.outputs))

    assert process_documents(conn) == [doc_id]
    assert held_out_documents(conn) == []

    set_held_out(conn, [doc_id])
    assert process_documents(conn) == []
    assert held_out_documents(conn) == [doc_id]


def test_reingesting_does_not_silently_return_a_document_to_the_process(two_column_pdf, config):
    conn = connect(config.paths.work_dir)
    doc_id = next(iter(ingest(config, conn, [two_column_pdf]).outputs))
    set_held_out(conn, [doc_id])

    config.classification.min_visible_chars = 250      # invalidate the cache, force a re-parse
    ingest(config, conn, [two_column_pdf])
    assert held_out_documents(conn) == [doc_id], "the flag must survive re-ingestion"


@pytest.mark.parametrize("released", [False, True])
def test_holding_out_is_reversible(two_column_pdf, config, released):
    conn = connect(config.paths.work_dir)
    doc_id = next(iter(ingest(config, conn, [two_column_pdf]).outputs))
    set_held_out(conn, [doc_id])
    if released:
        set_held_out(conn, [doc_id], held_out=False)
    assert (process_documents(conn) == [doc_id]) is released
