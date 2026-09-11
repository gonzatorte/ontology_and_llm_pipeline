from __future__ import annotations

import pymupdf

from onto_pipeline.db import connect
from onto_pipeline.ingest import ingest, markdown_path
from onto_pipeline.parse import PARAGRAPH, parse_document

SESSION = "test-1"


def test_spans_index_the_markdown_exactly(two_column_pdf, config):
    """The central provenance guarantee: mention offsets (SCHEMAS-MENTIONS) are anchored on these
    spans, and `markdown_hash` validates them on reimport."""
    parsed = parse_document(two_column_pdf, config)
    for block in parsed.blocks:
        if block.span_start is None:
            continue
        assert parsed.markdown[block.span_start:block.span_end] == block.render()


def test_boilerplate_is_kept_as_provenance_but_left_out_of_the_markdown(two_column_pdf, config):
    parsed = parse_document(two_column_pdf, config)
    headers = [block for block in parsed.blocks if "Journal of Testing" in block.text]
    assert headers, "the running header should have been extracted"
    assert all(block.is_boilerplate for block in headers)
    assert all(block.span_start is None for block in headers)
    assert "Journal of Testing" not in parsed.markdown


def test_columns_are_read_top_to_bottom_not_interleaved(two_column_pdf, config):
    parsed = parse_document(two_column_pdf, config)
    order = [
        block.text.split(".")[0]
        for block in parsed.blocks
        if block.block_type == PARAGRAPH and block.text.startswith(("LEFT", "RIGHT"))
    ]
    assert order == [f"{side} {n}" for n in range(1, 5) for side in ("LEFT", "RIGHT")]


def test_heading_is_detected_by_relative_font_size(two_column_pdf, config):
    parsed = parse_document(two_column_pdf, config)
    headings = [block for block in parsed.blocks if block.block_type == "heading"]
    assert [block.text for block in headings] == [
        "Section One", "Section Two", "Section Three", "Section Four",
    ]
    assert parsed.markdown.startswith("## Section One")


def test_ingest_persists_blocks_pages_and_markdown(two_column_pdf, config):
    conn = connect(config.paths.work_dir)
    result = ingest(config, conn, [two_column_pdf], session_id=SESSION)

    doc_id = next(iter(result.outputs))
    assert result.executed == 1
    assert conn.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM page_classification").fetchone()[0] == 4
    stored = conn.execute("SELECT markdown_hash FROM documents").fetchone()[0]
    written = markdown_path(config, doc_id).read_text(encoding="utf-8")
    assert stored == "sha256:" + __import__("hashlib").sha256(written.encode()).hexdigest()


def test_reingesting_an_unchanged_document_is_a_cache_hit(two_column_pdf, config):
    conn = connect(config.paths.work_dir)
    ingest(config, conn, [two_column_pdf], session_id=SESSION)
    again = ingest(config, conn, [two_column_pdf], session_id=SESSION)
    assert again.cached == 1 and again.executed == 0


def test_changing_a_threshold_invalidates_the_cache(two_column_pdf, config):
    conn = connect(config.paths.work_dir)
    ingest(config, conn, [two_column_pdf], session_id=SESSION)
    config.classification.min_visible_chars = 250
    again = ingest(config, conn, [two_column_pdf], session_id=SESSION)
    assert again.executed == 1


def test_line_break_hyphen_is_dropped_but_a_lexical_one_is_kept(tmp_path, config):
    """"Eco-nomic" is a syllable break; "university-based" is a real compound. The document
    itself is the evidence: the compound appears hyphenated inside a line elsewhere."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(
        pymupdf.Rect(40, 40, 250, 200),
        "The International Covenant on Eco-\nnomic rights applies to university-\n"
        "based genomics researchers.",
        fontsize=10,
    )
    page.insert_textbox(
        pymupdf.Rect(40, 220, 550, 400),
        "Elsewhere the text says university-based research plainly, on one line.",
        fontsize=10,
    )
    target = tmp_path / "hyphens.pdf"
    doc.save(target)
    doc.close()

    markdown = parse_document(target, config).markdown
    assert "Economic" in markdown
    assert "university-based" in markdown
    assert "universitybased" not in markdown


def test_discovery_takes_pdfs_and_plain_text_whatever_the_case(tmp_path, config):
    """`.PDF` appears in real corpora and matching only `.pdf` drops documents silently. Plain
    text is here because annotated corpora — the ones that come with the right answer — are
    published as `.txt`."""
    from onto_pipeline.ingest import discover

    corpus = tmp_path / "corpus"
    (corpus / "a").mkdir(parents=True)
    (corpus / "a" / "lower.pdf").write_bytes(b"%PDF-1.4")
    (corpus / "a" / "UPPER.PDF").write_bytes(b"%PDF-1.4")
    (corpus / "a" / "notes.txt").write_bytes(b"x")
    (corpus / "a" / "figure.png").write_bytes(b"\x89PNG")

    assert [p.name for p in discover(corpus)] == ["UPPER.PDF", "lower.pdf", "notes.txt"]
