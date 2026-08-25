from __future__ import annotations

import pytest

from onto_pipeline import extraction
from onto_pipeline.chunking import chunk_document
from onto_pipeline.db import connect
from onto_pipeline.extraction import Candidate
from onto_pipeline.parse import PARAGRAPH, Block


def block(ordinal, text, page=1, span_start=0):
    b = Block(document_id="doc", page=page, ordinal=ordinal, bbox=(0, 0, 1, 1),
              block_type=PARAGRAPH, text=text, language="en", language_source="inferred")
    b.span_start = span_start
    b.span_end = span_start + len(text)
    return b


def chunk_of(*blocks):
    return chunk_document(list(blocks), target_chars=10_000, max_chars=20_000)[0]


def test_parse_rejects_an_answer_that_is_not_json():
    with pytest.raises(ValueError, match="no JSON"):
        extraction.parse("I could not do that.", {})


def test_parse_rejects_an_answer_with_no_mentions_list():
    with pytest.raises(ValueError, match="mentions"):
        extraction.parse('{"result": []}', {})


def test_parse_drops_blank_surfaces():
    parsed = extraction.parse(
        '{"mentions": [{"text": "researchers", "kind": "people"}, {"text": "  "}]}', {}
    )
    assert parsed == [{"text": "researchers", "kind": "people"}]


def test_offsets_come_from_the_code_not_the_model():
    """The model returns a surface string; asking it for character offsets would let a
    miscount anchor a mention to the wrong text with nothing downstream able to tell."""
    first = block(0, "The researchers shared the data.", span_start=0)
    second = block(1, "A PET scan was acquired.", page=2, span_start=34)
    chunk = chunk_of(first, second)
    markdown = first.text + "\n\n" + second.text

    located = extraction.locate(
        chunk,
        [Candidate("researchers", "people"), Candidate("PET scan", "imaging method")],
        {first.id: first, second.id: second},
    )
    assert not located.unlocatable
    for mention in located.mentions:
        assert markdown[mention.span_start:mention.span_end] == mention.surface_text


def test_a_repeated_surface_form_yields_distinct_mentions():
    only = block(0, "data about data, and more data still", span_start=0)
    located = extraction.locate(chunk_of(only), [Candidate("data", "d")] * 3, {only.id: only})
    starts = [m.span_start for m in located.mentions]
    assert len(set(starts)) == 3, "three occurrences, not the first one three times"
    assert starts == sorted(starts)


def test_a_mention_the_model_invented_is_dropped_and_counted():
    """A surface the passage does not contain is a B1 bug and has to surface as one."""
    only = block(0, "The researchers shared the data.", span_start=0)
    located = extraction.locate(
        chunk_of(only),
        [Candidate("researchers", "people"), Candidate("questionnaire", "instrument")],
        {only.id: only},
    )
    assert [m.surface_text for m in located.mentions] == ["researchers"]
    assert [c.text for c in located.unlocatable] == ["questionnaire"]


def test_provenance_comes_from_the_block_the_mention_lands_in():
    first = block(0, "The researchers shared the data.", span_start=0)
    second = block(1, "A PET scan was acquired.", page=7, span_start=34)
    located = extraction.locate(
        chunk_of(first, second), [Candidate("PET scan", "imaging method")],
        {first.id: first, second.id: second},
    )
    mention = located.mentions[0]
    assert (mention.page, mention.block_id, mention.language) == (7, second.id, "en")


def test_mentions_persist_and_a_rerun_replaces_rather_than_duplicates(tmp_path):
    conn = connect(tmp_path)
    only = block(0, "The researchers shared the data.", span_start=0)
    located = extraction.locate(chunk_of(only), [Candidate("researchers", "people")],
                                {only.id: only})

    extraction.persist(conn, "doc", located.mentions)
    extraction.persist(conn, "doc", located.mentions)
    rows = extraction.load(conn, "doc")
    assert len(rows) == 1
    assert rows[0]["surface_text"] == "researchers"
    assert rows[0]["status"] == extraction.ACTIVE


def test_the_referencing_paragraph_is_marked_as_context_not_as_material():
    """It is attached so a table can be read in context, but extracting from it would double
    the mention that already belongs to its own chunk."""
    chunk = chunk_of(block(0, "text", span_start=0))
    chunk.context_text = "As shown in Table 1, the researchers..."
    rendered = extraction.PROMPT.render(**extraction.payload(chunk))
    assert "do not extract from it" in rendered
    assert "As shown in Table 1" in rendered
