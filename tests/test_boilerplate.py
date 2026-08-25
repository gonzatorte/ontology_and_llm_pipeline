from __future__ import annotations

from dataclasses import dataclass

from onto_pipeline.boilerplate import find_boilerplate, template

NORMALIZERS = ["digits", "ips", "dates"]


@dataclass
class Fake:
    page: int
    bbox: tuple[float, float, float, float]
    text: str


def detect(blocks, n_pages, threshold=0.8, tolerance=12.0):
    return find_boilerplate(
        blocks,
        n_pages,
        page_frequency_threshold=threshold,
        bbox_tolerance_px=tolerance,
        normalize_before_compare=NORMALIZERS,
        max_block_chars=200,
    )


def test_template_normalizes_variable_watermark_content():
    """A watermark stamping a different IP and timestamp per page has no two identical pages;
    exact repetition would never catch it."""
    first = template("Downloaded by 192.168.0.7 on 12/03/2024", NORMALIZERS)
    second = template("Downloaded by 10.0.0.155 on 04/11/2025", NORMALIZERS)
    assert first == second


def test_running_header_is_flagged():
    blocks = [Fake(page=n, bbox=(40, 20, 300, 32), text=f"Journal of Testing 2024, {n}:17")
              for n in range(1, 11)]
    assert detect(blocks, 10) == set(range(10))


def test_alternating_margin_header_is_still_flagged():
    """Academic headers swap margins between odd and even pages; each side forms its own
    positional cluster."""
    blocks = []
    for n in range(1, 11):
        bbox = (40, 20, 300, 32) if n % 2 else (295, 20, 555, 32)
        blocks.append(Fake(page=n, bbox=bbox, text="Journal of Testing"))
    assert detect(blocks, 10) == set(range(10))


def test_repeated_body_phrase_at_unstable_positions_is_not_boilerplate():
    blocks = [Fake(page=n, bbox=(40, 100 + n * 37, 300, 130 + n * 37), text="open science")
              for n in range(1, 11)]
    assert detect(blocks, 10) == set()


def test_infrequent_text_is_not_boilerplate():
    blocks = [Fake(page=n, bbox=(40, 20, 300, 32), text="Journal of Testing") for n in range(1, 5)]
    assert detect(blocks, 10) == set()


def test_short_document_has_too_little_evidence():
    """With two pages, >80% means "on both"; one coincidence would condemn a block."""
    blocks = [Fake(page=n, bbox=(40, 20, 300, 32), text="Journal of Testing") for n in (1, 2)]
    assert detect(blocks, 2) == set()


def test_a_long_recurring_paragraph_is_body_text_not_boilerplate():
    body = "The commercialization of academic research has generated mixed results. " * 4
    blocks = [Fake(page=n, bbox=(40, 100, 300, 400), text=body) for n in range(1, 11)]
    assert detect(blocks, 10) == set()
