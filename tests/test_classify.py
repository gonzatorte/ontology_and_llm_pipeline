from __future__ import annotations

import pytest

from onto_pipeline.classify import (
    BORN_DIGITAL,
    SCAN,
    UNCERTAIN,
    PageSignals,
    classify_signals,
    garbled_ratio,
)

THRESHOLDS = {
    "min_visible_chars": 100,
    "garbled_ratio_threshold": 0.35,
    "image_coverage_scan_threshold": 0.65,
}


def classify(**signals) -> tuple[str, str]:
    page_signals = PageSignals(**signals)
    label = classify_signals(page_signals, **THRESHOLDS)
    return label, page_signals.reason


def test_base_heuristic_born_digital():
    assert classify(visible_chars=500) == (BORN_DIGITAL, "base_heuristic")


def test_ocr_layer_over_page_image_is_a_scan():
    """The costly false negative: a searchable PDF has extractable text but must be OCR'd."""
    label, reason = classify(visible_chars=2000, hidden_chars=2000, image_coverage=0.95,
                             n_images=1)
    assert (label, reason) == (SCAN, "ocr_layer_over_page_image")


def test_text_as_curves_is_a_scan():
    label, reason = classify(visible_chars=0, n_images=0, n_vector_ops=4000)
    assert (label, reason) == (SCAN, "no_extractable_text")


def test_garbled_text_is_a_scan():
    label, reason = classify(visible_chars=800, garbled_ratio=0.6)
    assert (label, reason) == (SCAN, "garbled_text")


def test_empty_page_is_not_sent_to_the_vlm():
    assert classify(visible_chars=3)[0] == BORN_DIGITAL


def test_text_page_with_figures_uses_the_remaining_signals():
    """The base heuristic's recall gap: a text page carrying a figure is still born-digital."""
    label, reason = classify(visible_chars=3000, n_images=2, image_coverage=0.2,
                             n_embedded_fonts=4)
    assert (label, reason) == (BORN_DIGITAL, "embedded_fonts_with_figures")


def test_unresolved_signals_route_to_the_expensive_path():
    label, reason = classify(visible_chars=3000, n_images=2, image_coverage=0.2,
                             n_embedded_fonts=0)
    assert (label, reason) == (UNCERTAIN, "unresolved_signals")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The commercialization of academic research has generated results", False),
        ("La comercialización de la investigación académica generó resultados", False),
        ("bcdf ghjkl mnprs tvwxz bcdfg hjklm", True),
    ],
)
def test_garbled_ratio_separates_prose_from_mojibake(text, expected):
    assert (garbled_ratio(text) >= 0.35) is expected
