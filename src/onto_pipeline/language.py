"""Per-block language detection for the es/en corpus (spec 4.2).

Function-word frequency rather than a statistical library: the pipeline caches on input
hashes, so detection has to be deterministic and inspectable. `language_source` records
whether the value was declared by the document or inferred here.
"""

from __future__ import annotations

import re

DECLARED = "declared"
INFERRED = "inferred"

_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

_MARKERS = {
    "es": {
        "de", "la", "que", "el", "en", "y", "los", "del", "se", "las", "por", "un", "para",
        "con", "no", "una", "su", "al", "es", "lo", "como", "más", "pero", "sus", "le", "ya",
        "o", "este", "sí", "porque", "esta", "entre", "cuando", "muy", "sin", "sobre", "también",
        "me", "hasta", "hay", "donde", "quien", "desde", "todo", "nos", "durante", "estados",
    },
    "en": {
        "the", "of", "and", "to", "in", "a", "is", "that", "for", "it", "as", "was", "with",
        "be", "by", "on", "not", "he", "i", "this", "are", "or", "his", "from", "at", "which",
        "but", "have", "an", "had", "they", "you", "were", "their", "one", "all", "we", "can",
        "her", "has", "there", "been", "if", "more", "when", "will", "would", "who", "so",
    },
}


def detect(text: str, *, minimum_tokens: int = 8) -> str | None:
    """Best-guess language, or None when the text is too short or too ambiguous to judge."""
    tokens = [token.lower() for token in _TOKEN_RE.findall(text)]
    if len(tokens) < minimum_tokens:
        return None

    hits = {lang: sum(1 for token in tokens if token in markers)
            for lang, markers in _MARKERS.items()}
    best = max(hits, key=lambda lang: hits[lang])
    runner_up = min(hits, key=lambda lang: hits[lang])
    # Two markers minimum: a single incidental hit ("al" in a running header) is not evidence.
    if hits[best] < 2 or hits[best] == hits[runner_up]:
        return None
    return best


def normalize_declared(value: str | None) -> str | None:
    """Map a PDF `/Lang` value ('en-US', 'es') onto the corpus languages."""
    if not value:
        return None
    code = value.strip().strip("()").lower().replace("_", "-").split("-")[0]
    return code if code in _MARKERS else None
