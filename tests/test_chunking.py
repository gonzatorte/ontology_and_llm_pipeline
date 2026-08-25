from __future__ import annotations

from onto_pipeline.chunking import chunk_document
from onto_pipeline.parse import CAPTION, PARAGRAPH, TABLE, UNPARSED, Block

TABLE_MARKDOWN = "|a|b|\n|-|-|\n|1|2|\n" * 60


def block(ordinal, kind, text, page=1, boilerplate=False):
    return Block(
        document_id="doc",
        page=page,
        ordinal=ordinal,
        bbox=(0, 0, 1, 1),
        block_type=kind,
        text=text,
        is_boilerplate=boilerplate,
    )


def paragraphs(count, size=1000, start=0):
    return [
        block(start + index, PARAGRAPH, f"P{start + index}. " + "word " * (size // 5))
        for index in range(count)
    ]


def test_a_table_is_never_split_even_when_it_exceeds_the_maximum():
    blocks = [*paragraphs(2), block(2, TABLE, TABLE_MARKDOWN), *paragraphs(2, start=3)]
    chunks = chunk_document(blocks, target_chars=1500, max_chars=2000)
    holding = [chunk for chunk in chunks if "doc:p1:b2" in chunk.block_ids]
    assert len(holding) == 1
    assert holding[0].text.count("|1|2|") == TABLE_MARKDOWN.count("|1|2|")


def test_a_table_travels_with_its_caption():
    blocks = [
        *paragraphs(1),
        block(1, CAPTION, "Table 1 Policies encouraging commercialization"),
        block(2, TABLE, "|a|b|\n|-|-|\n|1|2|"),
        *paragraphs(3, start=3),
    ]
    chunks = chunk_document(blocks, target_chars=900, max_chars=1200)
    caption_chunk = next(c for c in chunks if "doc:p1:b1" in c.block_ids)
    assert "doc:p1:b2" in caption_chunk.block_ids


def test_the_referencing_paragraph_is_attached_as_context_not_moved():
    """It belongs to its own chunk: mentions are anchored on blocks, and duplicating the text
    would extract the same mention twice."""
    blocks = [
        block(0, PARAGRAPH, "As shown in Table 1 the pressure is more than posturing. " * 20),
        *paragraphs(3, start=1),
        block(4, CAPTION, "Table 1 Policies encouraging commercialization", page=2),
        block(5, TABLE, "|a|b|\n|-|-|\n|1|2|", page=2),
    ]
    chunks = chunk_document(blocks, target_chars=900, max_chars=1400)
    table_chunk = next(c for c in chunks if "doc:p2:b5" in c.block_ids)
    assert table_chunk.context_block_ids == ["doc:p1:b0"]
    assert "As shown in Table 1" in table_chunk.context_text
    assert "doc:p1:b0" not in table_chunk.block_ids


def test_boilerplate_and_unparsed_pages_stay_out_of_the_chunks():
    blocks = [
        block(0, PARAGRAPH, "Journal of Testing 2024", boilerplate=True),
        block(1, UNPARSED, "classified scan, needs the VLM route"),
        *paragraphs(2, start=2),
    ]
    chunks = chunk_document(blocks, target_chars=5000, max_chars=6000)
    assert [chunk.block_ids for chunk in chunks] == [["doc:p1:b2", "doc:p1:b3"]]


def test_chunks_carry_the_pages_they_span():
    blocks = [*paragraphs(2), *paragraphs(2, start=2)]
    for index, item in enumerate(blocks):
        item.page = 1 + index // 2
    chunk = chunk_document(blocks, target_chars=10000, max_chars=20000)[0]
    assert chunk.pages == [1, 2]


def test_a_document_with_nothing_usable_yields_no_chunks():
    assert chunk_document([block(0, UNPARSED, "scan")], 1000, 2000) == []
