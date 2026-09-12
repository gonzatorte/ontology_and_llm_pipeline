"""Denormalising identifiers and comparing the terms that come out (PREP-NORMALIZE,
PREP-NORMALIZE-LABELS/PREP-NORMALIZE-TYPOS).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SEPARATORS = re.compile(r"[_\-\s]+")
_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)

_SPANISH_MARKERS = {
    "de", "del", "la", "el", "los", "las", "una", "uno", "un", "y", "o", "en", "con", "por",
    "para", "que", "se", "su", "al", "es", "son", "tiene", "sobre", "entre", "sin", "varias",
    "varios",
}
# Endings that are common in Spanish and rare in English. Without them a plain noun like
# "objetivo" or "metadato" is indistinguishable from its English cognate, and the two land in
# the same lexicon, where the typo detectors read the translation pair as a misspelling.
_SPANISH_ORTHOGRAPHY = re.compile(
    r"[áéíóúñü]|(?:ci[oó]n|dad|miento|mente|iv[oa]|at[oa]|ncia|eza|ura)s?\b",
    re.IGNORECASE,
)


def denormalize(local_name: str) -> str:
    """`appliesTechnique` -> `applies Technique`; `Aplica_una_o_varias` -> `Aplica una o varias`."""
    spaced = _SEPARATORS.sub(" ", local_name)
    spaced = _CAMEL_BOUNDARY.sub(" ", spaced)
    return " ".join(spaced.split())


def local_name(iri: str) -> str:
    for separator in ("#", "/", ":"):
        if separator in iri:
            head, _, tail = iri.rpartition(separator)
            if tail:
                return tail
    return iri


def fold(text: str) -> str:
    """`día` and `dia` are one word written by two hands (PREP-NORMALIZE-LABELS/FOLD-DIACRITICS).

    An ontology that spells its Spanish labels with accents and mints its identifiers without
    them compares the same word against itself and scores 0,667 — below the divergence
    threshold, so the pair is flagged. The accepted cost is the minimal pair: `año`/`ano` and
    `término`/`terminó` fold together. It is contained because the comparison is between two
    spellings of the *same* entity, where the same word is overwhelmingly likelier than a pair
    that only an accent separates.
    """
    return "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )


def tokens(text: str) -> list[str]:
    """Denormalizes first: a declared label is as likely to be `isGeneratedBy` as a phrase,
    and leaving it glued makes every lexicon comparison meaningless."""
    return [
        token for token in _NON_WORD.sub(" ", fold(denormalize(text)).lower()).split() if token
    ]


FUNCTION_WORDS = _SPANISH_MARKERS | {
    "the", "of", "and", "to", "in", "a", "is", "for", "it", "as", "with", "by", "on", "or",
    "from", "at", "has", "have", "be", "are", "that", "this",
}


def guess_language(term: str) -> str:
    """Term-level guess for the es/en corpus. Too short for the block-level detector, and
    every term needs a tag, so the default is the corpus majority rather than None."""
    if _SPANISH_ORTHOGRAPHY.search(term):
        return "es"
    if any(token in _SPANISH_MARKERS for token in tokens(term)):
        return "es"
    return "en"


def similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, " ".join(tokens(left)), " ".join(tokens(right))).ratio()


def levenshtein(left: str, right: str, *, maximum: int = 2) -> int:
    """Edit distance, abandoned once it exceeds `maximum`."""
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (left_char != right_char),
                )
            )
        if min(current) > maximum:
            return maximum + 1
        previous = current
    return previous[-1]


def is_subsequence(short: str, long: str) -> bool:
    """`Frm` inside `Framework`: a truncated abbreviation keeps order but drops letters."""
    iterator = iter(long)
    return all(char in iterator for char in short)
