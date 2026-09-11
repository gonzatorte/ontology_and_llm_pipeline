"""ITER-EXTRACT — candidate extraction.

Governing principle of every LLM use in the pipeline:

    The LLM classifies and names. The code builds the logic. The reasoner rejects.

So this stage does not assign ontology classes — that is ITER-MATCH's job, and giving it away here
would let the model conflate naming with typing. It extracts mentions and a bare descriptor.

**The model is never asked for character offsets.** It returns the surface string; the code
finds it in the chunk and translates to an absolute Markdown offset. That is the same reasoning
the spec applies to ITER-COREFER: ask for something mechanically verifiable, not for spans. A model
that
miscounts characters would otherwise anchor a mention to the wrong text, and nothing downstream
could tell.

A surface form occurring more than once is disambiguated by occurrence index, also supplied by
the code rather than the model. Anything that cannot be located is dropped and counted: a
mention the extractor invented is a bug in ITER-EXTRACT, and it should show up as one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .chunking import Chunk
from .llm import Prompt
from .store import Store

STAGE = "iter_extract"

PROMPT = Prompt(
    stage=STAGE,
    version="v2",
    template="""Extract the concept mentions from this passage of a research paper.

Apply one test to every candidate: **can you put a number in front of it and count them?**

  "three researchers" -> yes, extract
  "two interviews", "a PET scan", "four datasets" -> yes, extract
  "three commercializations" -> no. Skip it.
  "two transparencies", "three impacts", "four innovations" -> no. Skip them.

Nominalizations naming a quality or a process (-ation, -ity, -ness, -ance) almost never pass
the test. Neither do years and quantities ("2011", "40%"), verbs, adjectives, or the paper
talking about itself ("this section", "our argument", "Table 2").

A mention is a noun phrase, at most a handful of words. Never return a clause or a sentence.

For each mention give:
- "text": the exact surface string, copied character for character from the passage. Do not
  normalize, correct, expand abbreviations or change capitalization.
- "kind": two or three words saying what sort of thing it is, in plain language. Do not try to
  fit it to any ontology.

Include the ordinary and obvious mentions, not only the interesting ones. A passage usually
has a handful, not dozens.

PASSAGE:
{text}
{context}
Answer with JSON only: {{"mentions": [{{"text": "...", "kind": "..."}}]}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_NUMERIC_RE = re.compile(r"^[\W\d]+$", re.UNICODE)

ACTIVE = "active"
MISEXTRACTED = "misextracted"


@dataclass
class Candidate:
    text: str
    kind: str


@dataclass
class Mention:
    id: str
    document_id: str
    chunk_id: str
    block_id: str
    page: int
    span_start: int
    span_end: int
    surface_text: str
    kind: str
    block_type: str | None = None
    language: str | None = None
    language_source: str | None = None
    status: str = ACTIVE


@dataclass
class Located:
    mentions: list[Mention]
    unlocatable: list[Candidate]
    rejected: list[Candidate] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rejected is None:
            self.rejected = []


def admissible(candidate: Candidate, max_words: int) -> bool:
    """Mechanical constraints only — the code enforces shape, the model judges meaning.

    A mention is a noun phrase, so a clause is a extraction error whatever it means; and a bare
    year or quantity names no thing at all. Deciding whether a *word* denotes a countable thing
    is semantic and stays in the prompt: a hand-written stoplist of abstractions would be the
    code overruling the model on exactly the judgement it was asked for.
    """
    surface = candidate.text.strip()
    if not surface or _NUMERIC_RE.match(surface):
        return False
    return len(surface.split()) <= max_words


def payload(chunk: Chunk) -> dict[str, str]:
    context = (
        f"\nPASSAGE THAT REFERENCES A TABLE ABOVE (context only, do not extract from it):\n"
        f"{chunk.context_text}\n"
        if chunk.context_text
        else ""
    )
    return {"text": chunk.text, "context": context}


def parse(text: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())
    if not isinstance(data.get("mentions"), list):
        raise ValueError("the answer has no 'mentions' list")
    return [
        {"text": str(item["text"]), "kind": str(item.get("kind", ""))}
        for item in data["mentions"]
        if isinstance(item, dict) and str(item.get("text", "")).strip()
    ]


def locate(
    chunk: Chunk, candidates: list[Candidate], blocks: dict[str, Any], *, max_words: int = 8
) -> Located:
    """Find each surface string in the chunk and translate to a Markdown offset.

    Repeated surface forms consume successive occurrences, so `data` appearing four times
    yields four distinct mentions rather than four copies of the first.
    """
    used: dict[str, int] = {}
    mentions: list[Mention] = []
    unlocatable: list[Candidate] = []
    rejected: list[Candidate] = []

    for candidate in candidates:
        if not admissible(candidate, max_words):
            rejected.append(candidate)
            continue
        surface = candidate.text
        start = chunk.text.find(surface, used.get(surface, 0))
        if start < 0:
            unlocatable.append(candidate)
            continue
        used[surface] = start + 1

        block_id, absolute = chunk.absolute_offset(start)
        block = blocks.get(block_id)
        mentions.append(
            Mention(
                id=f"{chunk.id}:m{len(mentions)}",
                document_id=chunk.document_id,
                chunk_id=chunk.id,
                block_id=block_id,
                page=getattr(block, "page", chunk.pages[0] if chunk.pages else 0),
                span_start=absolute,
                span_end=absolute + len(surface),
                surface_text=surface,
                kind=candidate.kind,
                block_type=getattr(block, "block_type", None),
                language=getattr(block, "language", None),
                language_source=getattr(block, "language_source", None),
            )
        )
    return Located(mentions=mentions, unlocatable=unlocatable, rejected=rejected)


def persist(conn: Store, document_id: str, mentions: list[Mention]) -> None:
    """The mention layer is immutable except by extension, so a re-run replaces this
    document's rows rather than accumulating duplicates."""
    conn.execute("DELETE FROM mentions WHERE document_id = ?", (document_id,))
    conn.executemany(
        "INSERT INTO mentions (id, document_id, page, bbox, span_start, span_end, "
        "surface_text, block_type, language, language_source, coref_group, candidate_entity, "
        "status) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
        [
            (
                m.id, m.document_id, m.page, m.span_start, m.span_end, m.surface_text,
                m.block_type, m.language, m.language_source, m.status,
            )
            for m in mentions
        ],
    )
    conn.commit()


def load(conn: Store, document_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM mentions WHERE document_id = ? ORDER BY span_start", (document_id,)
        )
    ]
