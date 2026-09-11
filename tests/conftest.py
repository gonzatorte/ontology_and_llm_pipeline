from __future__ import annotations

import textwrap
from pathlib import Path

import pymupdf
import pytest

from onto_pipeline.config import Config

_BODY = (
    "The commercialization of academic research has generated mixed results and the "
    "imperative to commercialize university research persists across funding agencies."
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "paths": {
                "corpus_root": tmp_path / "corpus",
                "initial_ontology": tmp_path / "seed.rdf",
                "work_dir": tmp_path / "work",
            }
        }
    )


_ORDINALS = ["One", "Two", "Three", "Four"]

_TOPICS = [
    "funding agencies", "peer review", "research data", "technology transfer",
    "open access mandates", "institutional repositories", "grant policy", "patent portfolios",
]


@pytest.fixture
def two_column_pdf(tmp_path: Path) -> Path:
    """Four two-column pages with a running header, and body text that differs per page."""
    doc = pymupdf.open()
    for page_number in range(1, 5):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(
            pymupdf.Rect(40, 20, 555, 40), f"Journal of Testing 2024, {page_number}:17",
            fontsize=8,
        )
        page.insert_textbox(
            pymupdf.Rect(40, 60, 285, 100), f"Section {_ORDINALS[page_number - 1]}", fontsize=16
        )
        for label, rect, topic in (
            ("LEFT", pymupdf.Rect(40, 110, 285, 400), _TOPICS[2 * page_number - 2]),
            ("RIGHT", pymupdf.Rect(310, 110, 555, 400), _TOPICS[2 * page_number - 1]),
        ):
            page.insert_textbox(
                rect, f"{label} {page_number}. {_BODY} This section concerns {topic}.",
                fontsize=10,
            )
    target = tmp_path / "two_column.pdf"
    doc.save(target)
    doc.close()
    return target


# ─────────────────  un caso de uso mínimo, en disco  ─────────────────
#
# Lo comparten `test_use_cases` —que prueba cargarlo— y `test_calibration` —que prueba medir
# con él—. Vive acá y no en uno de los dos para que ninguno tenga que importar del otro.

_KNOWTATOR = """<?xml version="1.0" encoding="UTF-8"?>
<annotations textSource="d1.txt">
  <annotation>
    <mention id="i1"/>
    <span start="0" end="6"/>
  </annotation>
  <classMention id="i1"><mentionClass id="CL:0000540">neuron</mentionClass></classMention>
  <annotation>
    <mention id="i2"/>
    <span start="11" end="19"/>
  </annotation>
  <classMention id="i2"><mentionClass id="CL:0000233">platelet</mentionClass></classMention>
  <annotation>
    <mention id="i3"/>
    <span start="24" end="28"/>
    <span start="34" end="38"/>
  </annotation>
  <classMention id="i3"><mentionClass id="CL_EXT:lens_fibre">lens cell</mentionClass></classMention>
</annotations>
"""

_TEXT = "neuron and platelet and lens fibre cell"

_OBO = """format-version: 1.2

[Term]
id: CL:0000540
name: neuron
def: "A cell that transmits electrical signals." []
synonym: "nerve cell" EXACT []

[Term]
id: CL:0000233
name: platelet
def: "A cell fragment in blood." []

[Term]
id: CL:0000000
name: cell

[Term]
id: CL:9999999
name: gone
is_obsolete: true
"""

_EXTENSIONS = """format-version: 1.2

[Term]
id: CL_EXT:lens_fibre
name: lens fibre cell
def: "A cell of the lens." []

[Term]
id: CL:0000540
name: neuron (do not use)
"""


@pytest.fixture
def use_case_dir(tmp_path: Path):
    """Devuelve un constructor: `use_case_dir(**overrides)` deja el caso de uso en disco.

    Es una fábrica y no un directorio ya armado porque media docena de tests necesitan el mismo
    corpus con una clave distinta en el descriptor —clases excluidas, clases retenidas, un
    formato que nadie lee— y armarlo aparte en cada uno duplicaría el corpus seis veces.
    """
    def build(root: Path | None = None, **overrides) -> Path:
        directory = (root or tmp_path) / "case"
        (directory / "txt").mkdir(parents=True)
        (directory / "ann").mkdir(parents=True)
        (directory / "txt" / "d1.txt").write_text(_TEXT, encoding="utf-8")
        (directory / "ann" / "d1.knowtator.xml").write_text(_KNOWTATOR, encoding="utf-8")
        (directory / "cl.obo").write_text(_OBO, encoding="utf-8")
        (directory / "ext.obo").write_text(_EXTENSIONS, encoding="utf-8")
        body = textwrap.dedent(
            """
            name: tiny
            language: en
            documents:
              dir: txt
            annotations:
              format: knowtator
              dir: ann
              suffix: .knowtator.xml
            ontology:
              files: [cl.obo, ext.obo]
            """
        )
        for key, value in overrides.items():
            body += f"{key}: {value}\n"
        (directory / "use_case.yml").write_text(body, encoding="utf-8")
        return directory

    return build


@pytest.fixture
def keyword_encoder():
    """El mismo reemplazo que usa `test_matching`: bolsa de palabras sobre un vocabulario fijo,
    sin descargar ningún modelo."""

    class KeywordEncoder:
        VOCABULARY = ("neuron", "nerve", "platelet", "blood", "cell", "lens", "electrical")

        def encode(self, texts):
            return [
                [1.0 if word in text.lower() else 0.0 for word in self.VOCABULARY]
                for text in texts
            ]

    return KeywordEncoder()
