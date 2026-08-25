from __future__ import annotations

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
                "seed_ontology": tmp_path / "seed.rdf",
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
