"""PREP-CLASSIFY — page-level document classification.

Per page, not per document: mixed documents (born-digital report with a scanned annex) are
common. Uncertainty routes to the expensive path.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

import pymupdf

BORN_DIGITAL = "born_digital"
SCAN = "scan"
UNCERTAIN = "uncertain"

# PDF text render mode 3 = invisible. Searchable scans put their OCR layer here.
_INVISIBLE_RENDER_MODE = 3
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_VOWELS = set("aeiouáéíóúüàèìòùâêîôûAEIOUÁÉÍÓÚÜÀÈÌÒÙÂÊÎÔÛ")


@dataclass
class PageSignals:
    visible_chars: int = 0
    hidden_chars: int = 0
    image_coverage: float = 0.0
    n_images: int = 0
    n_vector_ops: int = 0
    n_fonts: int = 0
    n_embedded_fonts: int = 0
    garbled_ratio: float = 0.0
    producer: str = ""
    creator: str = ""
    reason: str = ""


@dataclass
class PageClass:
    page: int
    label: str
    signals: PageSignals = field(default_factory=PageSignals)

    def signals_json(self) -> dict[str, Any]:
        return asdict(self.signals)


def garbled_ratio(text: str) -> float:
    """Fraction of alphabetic tokens that do not look like words.

    Detects mojibake and broken CID-to-Unicode maps, which produce vowel-less or
    single-letter-run tokens at a rate normal prose never reaches.
    """
    tokens = _WORD_RE.findall(text)
    if not tokens:
        return 0.0
    bad = sum(1 for token in tokens if not _word_like(token))
    return bad / len(tokens)


def _word_like(token: str) -> bool:
    if len(token) > 3 and not any(char in _VOWELS for char in token):
        return False
    # Interior capitals in a lowercase word ("aBcDe") signal a broken encoding, not camelCase
    # prose; a leading capital and all-caps acronyms stay legal.
    body = token[1:]
    return not (body != body.lower() and body != body.upper())


def page_signals(page: pymupdf.Page, metadata: dict[str, str]) -> PageSignals:
    visible, hidden = _char_counts(page)
    page_area = abs(page.rect.get_area()) or 1.0
    images = page.get_image_info()
    covered = sum(abs(pymupdf.Rect(image["bbox"]).get_area()) for image in images)
    fonts = page.get_fonts(full=False)

    return PageSignals(
        visible_chars=visible,
        hidden_chars=hidden,
        image_coverage=min(covered / page_area, 1.0),
        n_images=len(images),
        n_vector_ops=len(page.get_drawings()),
        n_fonts=len(fonts),
        n_embedded_fonts=sum(1 for font in fonts if font[1] not in ("n/a", "")),
        garbled_ratio=garbled_ratio(page.get_text()),
        producer=metadata.get("producer") or "",
        creator=metadata.get("creator") or "",
    )


def _char_counts(page: pymupdf.Page) -> tuple[int, int]:
    """Visible and invisible character counts, split by text render mode."""
    visible = hidden = 0
    for span in page.get_texttrace():
        count = len(span.get("chars", ()))
        if span.get("type") == _INVISIBLE_RENDER_MODE:
            hidden += count
        else:
            visible += count
    return visible, hidden


def classify_signals(
    signals: PageSignals,
    *,
    min_visible_chars: int,
    garbled_ratio_threshold: float,
    image_coverage_scan_threshold: float,
) -> str:
    """Route a page from its signals. Sets `signals.reason` with the rule that fired."""
    if signals.visible_chars and signals.garbled_ratio >= garbled_ratio_threshold:
        signals.reason = "garbled_text"
        return SCAN

    if signals.hidden_chars and signals.image_coverage >= image_coverage_scan_threshold:
        # OCR'd scan: invisible text layer over a full-page image. The costly false negative.
        signals.reason = "ocr_layer_over_page_image"
        return SCAN

    if signals.visible_chars < min_visible_chars:
        if not signals.n_images and signals.n_vector_ops < 5:
            # Nothing to read and nothing to OCR.
            signals.reason = "empty_page"
            return BORN_DIGITAL
        # Text converted to curves lands here; misclassifying it is harmless, the route is
        # the same one it needs.
        signals.reason = "no_extractable_text"
        return SCAN

    if not signals.hidden_chars and not signals.n_images:
        signals.reason = "base_heuristic"
        return BORN_DIGITAL

    if (
        not signals.hidden_chars
        and signals.n_embedded_fonts
        and signals.image_coverage < image_coverage_scan_threshold
    ):
        # Text page carrying figures: the base heuristic's known recall gap (CCpdf recall 43%).
        signals.reason = "embedded_fonts_with_figures"
        return BORN_DIGITAL

    signals.reason = "unresolved_signals"
    return UNCERTAIN


def classify_document(doc: pymupdf.Document, classification) -> list[PageClass]:
    metadata = doc.metadata or {}
    results = []
    for number, page in enumerate(doc, start=1):
        signals = page_signals(page, metadata)
        label = classify_signals(
            signals,
            min_visible_chars=classification.min_visible_chars,
            garbled_ratio_threshold=classification.garbled_ratio_threshold,
            image_coverage_scan_threshold=classification.image_coverage_scan_threshold,
        )
        results.append(PageClass(page=number, label=label, signals=signals))
    return results
