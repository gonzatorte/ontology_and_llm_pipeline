from __future__ import annotations

from pathlib import Path

import pytest

from onto_pipeline.parse import PARAGRAPH, paragraphs, parse_text

SOURCE = (
    "Complex trait analysis of the mouse striatum\n"
    "\n"
    "Abstract\n"
    "\n"
    "The striatum plays a pivotal role in modulating motor activity. We analyzed\n"
    "variation in striatal volume and neuron number in mice.\n"
)


@pytest.fixture
def document(tmp_path, config):
    path = tmp_path / "11319941.txt"
    path.write_text(SOURCE, encoding="utf-8")
    return parse_text(path, config, doc_id="craft_1")


# ─────────────  el invariante que hace útil a un corpus anotado  ─────────────


def test_the_markdown_is_the_file_byte_for_byte(document):
    """Las anotaciones gold indexan caracteres de este archivo. Reflowear, recortar o
    normalizar cualquier cosa corre los offsets, y el error no se ve como error: se ve como una
    tasa de acierto peor sin explicación."""
    assert document.markdown == SOURCE


def test_every_block_is_a_view_over_that_texto(document):
    for block in document.blocks:
        assert document.markdown[block.span_start:block.span_end] == block.text


def test_nothing_is_dropped_from_the_middle(document):
    """Un bloque perdido es texto que la extracción nunca ve."""
    covered = sum(block.span_end - block.span_start for block in document.blocks)
    assert covered == len(SOURCE.replace("\n\n", "").rstrip("\n"))


def test_a_paragraph_keeps_its_internal_line_breaks(document):
    body = document.blocks[-1]
    assert "\n" in body.text, "el salto interno es parte del texto original"
    assert body.text.startswith("The striatum") and body.text.endswith("in mice.")


# ─────────────────────────  el corte en párrafos  ─────────────────────────


def test_paragraphs_are_split_on_blank_lines():
    assert len(paragraphs("uno\n\ndos\n\ntres")) == 3


def test_a_blank_line_with_whitespace_still_separates():
    spans = paragraphs("uno\n   \ndos")
    assert len(spans) == 2


def test_repeated_blank_lines_do_not_make_empty_blocks():
    assert len(paragraphs("uno\n\n\n\n\ndos")) == 2


def test_an_empty_file_has_no_blocks():
    assert paragraphs("") == [] and paragraphs("\n\n \n") == []


def test_the_spans_never_include_the_surrounding_blank_lines():
    text = "uno\n\n  dos  \n\ntres"
    for start, end in paragraphs(text):
        assert text[start] != "\n" and not text[start].isspace()
        assert not text[end - 1].isspace()


# ─────────────────────────  lo que esta ruta no hace  ─────────────────────────


def test_no_block_is_a_heading_even_when_it_looks_like_one(document):
    """`Block.render()` le pondría `## ` delante y cambiaría el texto; con el Markdown literal,
    marcar encabezados sería mentir sobre lo que hay en el archivo."""
    assert {block.block_type for block in document.blocks} == {PARAGRAPH}


def test_nothing_is_flagged_as_boilerplate(document):
    """Se detecta por repetición entre páginas, y este formato no tiene páginas."""
    assert not any(block.is_boilerplate for block in document.blocks)


def test_it_does_not_claim_to_have_pages(document):
    """Poner 1 diría que tiene una; tiene cero."""
    assert document.n_pages == 0 and document.page_classes == []


def test_the_language_is_inferred_per_block(document):
    body = document.blocks[-1]
    assert body.language == "en" and body.language_source == "inferred"


def test_the_parser_says_which_route_produced_it(document):
    assert document.parser_used == "text"


def test_block_ids_stay_unique_without_pages(document):
    ids = [block.id for block in document.blocks]
    assert len(ids) == len(set(ids))


# ─────────  contra el corpus anotado de verdad, si está bajado  ─────────

CRAFT = Path(__file__).resolve().parents[1] / ".." / "calibration"


@pytest.mark.skipif(
    not (CRAFT / "_craft" / "articles" / "txt").is_dir(),
    reason="el clone de CRAFT no está; se regenera con el comando de NOTA_FASE0.md",
)
def test_the_gold_annotations_land_where_the_markdown_says(config):
    """La razón de ser de esta ruta, medida y no supuesta.

    Un corpus anotado sirve porque trae la respuesta correcta con offsets. Si la ingesta
    corriera un solo carácter, cada medición posterior compararía contra el lugar equivocado —
    y no fallaría: daría peor sin decir por qué.
    """
    from onto_pipeline.calibration import load_pair

    pair = load_pair(CRAFT / "craft-cl", match_against="label")
    texts = CRAFT / "_craft" / "articles" / "txt"

    checked = 0
    for gold in pair.documents:
        document = parse_text(texts / f"{gold.doc_id}.txt", config, doc_id=gold.doc_id)
        spans = [(b.span_start, b.span_end) for b in document.blocks]
        for mention in gold.mentions:
            start, end = mention.span
            assert document.markdown[start:end] == mention.text, gold.doc_id
            assert any(a <= start and end <= b for a, b in spans), (
                f"{gold.doc_id}: la mención no cae dentro de ningún bloque"
            )
            checked += 1
    assert checked == 8723, "el par cambió de tamaño; revisar antes de dar por buena la cifra"
