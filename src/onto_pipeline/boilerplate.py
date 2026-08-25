"""Boilerplate filter (spec 4.2), independent of the parser.

Rule: a text block present in more than `page_frequency_threshold` of the pages with an
approximately constant bbox is boilerplate. Comparison is by template — digits, IPs and dates
normalize to wildcards first, so a watermark whose content varies per page (IP, timestamp)
still collapses onto a single template.

Blocks are marked, never dropped from the store: provenance survives, and only the Markdown
handed downstream excludes them.
"""

from __future__ import annotations

import re
from typing import Protocol

_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_DATE_RE = re.compile(
    r"\b(?:\d{1,4}[/-]\d{1,2}[/-]\d{1,4}"
    r"|\d{1,2}\s+de\s+\w+\s+de\s+\d{4}"
    r"|\w+\s+\d{1,2},\s*\d{4})\b",
    re.IGNORECASE,
)
_DIGITS_RE = re.compile(r"\d+")
_SPACE_RE = re.compile(r"\s+")

_NORMALIZERS = {
    "ips": lambda text: _IP_RE.sub("<ip>", text),
    "dates": lambda text: _DATE_RE.sub("<date>", text),
    "digits": lambda text: _DIGITS_RE.sub("<n>", text),
}


class _Positioned(Protocol):
    page: int
    bbox: tuple[float, float, float, float]
    text: str


def template(text: str, normalizers: list[str]) -> str:
    normalized = text.strip().lower()
    # IPs and dates before digits: the digit rule would otherwise eat their structure.
    for name in ("ips", "dates", "digits"):
        if name in normalizers:
            normalized = _NORMALIZERS[name](normalized)
    return _SPACE_RE.sub(" ", normalized)


MIN_PAGES = 3


def find_boilerplate(
    blocks: list[_Positioned],
    n_pages: int,
    *,
    page_frequency_threshold: float,
    bbox_tolerance_px: float,
    normalize_before_compare: list[str],
    max_block_chars: int,
) -> set[int]:
    """Indices of `blocks` that are boilerplate."""
    # Below three pages a frequency threshold of 0.8 can only mean "on every page", where one
    # coincidence is enough to condemn a block. Too little evidence to act on.
    if n_pages < MIN_PAGES:
        return set()

    groups: dict[str, list[int]] = {}
    for index, block in enumerate(blocks):
        # Running heads, folios and watermarks are short. A long paragraph recurring on every
        # page is something else, and losing body text costs more than keeping noise.
        if not block.text.strip() or len(block.text) > max_block_chars:
            continue
        groups.setdefault(template(block.text, normalize_before_compare), []).append(index)

    flagged: set[int] = set()
    for indices in groups.values():
        coverage = len({blocks[index].page for index in indices}) / n_pages
        if coverage <= page_frequency_threshold:
            continue
        for cluster in _position_clusters(blocks, indices, bbox_tolerance_px):
            # A singleton cluster means the text repeats at an unstable position — a recurring
            # body phrase, not a running header. Headers alternating between odd/even margins
            # form two dense clusters and are still caught.
            if len(cluster) > 1:
                flagged.update(cluster)
    return flagged


def _position_clusters(
    blocks: list[_Positioned], indices: list[int], tolerance: float
) -> list[list[int]]:
    clusters: list[list[int]] = []
    for index in indices:
        for cluster in clusters:
            if _close(blocks[index].bbox, blocks[cluster[0]].bbox, tolerance):
                cluster.append(index)
                break
        else:
            clusters.append([index])
    return clusters


def _close(left, right, tolerance: float) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right, strict=True))
