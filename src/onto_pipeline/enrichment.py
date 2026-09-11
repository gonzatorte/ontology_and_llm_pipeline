"""ITER-AXIOMATIZE-ENRICH — gloss enrichment from definitional passages
(ITER-AXIOMATIZE, PREP-NORMALIZE).

A gloss is not a fixed value of PREP-NORMALIZE. It is bootstrapped from the structural
neighbourhood, and
then every iteration improves it from what the corpus actually says, which closes a
self-correcting loop: a better gloss means better matching, which means fewer false orphans, so
a mention orphaned at iteration 3 can be typed correctly at 8.

Two things about this pipeline change how that loop runs, and both are worth stating plainly.

The matcher was measured to work better against **labels** than against glosses, against the
spec's own premise — so the part of an enrichment that feeds back into matching is the
`skos:altLabel` it harvests, not the `skos:definition` it rewrites. The definition still
matters: it is what the axiomatization prompt shows as a candidate's meaning, and it is what a
person reads. But the loop the spec describes runs through the synonyms here.

And the passages are found **mechanically**, by definitional cue, not by asking a model which
passages are definitional. "X is a Y", "X refers to", "we define X as", "X, also known as Y":
a small enumerated set of patterns anchored on the class's own surface forms. This is the
cheap half of the stage, and it has to be cheap — a corpus has far more paragraphs than a
budget has requests, and a filter that costs one request per paragraph is not a filter.

**Circularity control (PREP-NORMALIZE).** Every enrichment records which documents contributed. A
later
match of a mention from a contributing document against that class is not independent evidence:
the class was described using that document, so the match is partly the pipeline recognizing
its own writing. Those matches inflate coverage, and `circular_matches` is what makes them
countable instead of invisible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SKOS

from .llm import Prompt
from .store import Store

STAGE = "iter_axiomatize_enrich"

SCHEMA = """
CREATE TABLE IF NOT EXISTS gloss_contributions (
  version_id   TEXT NOT NULL,
  iri          TEXT NOT NULL,
  document_id  TEXT NOT NULL,
  cue          TEXT,
  quote        TEXT,
  created_at   TEXT,
  PRIMARY KEY (version_id, iri, document_id)
);
CREATE INDEX IF NOT EXISTS idx_contrib_iri ON gloss_contributions(iri, document_id);
"""


# ─────────────────────────  finding the passages  ─────────────────────────

# Definitional cues, in the two languages of the corpus. `{s}` is the class's surface form,
# escaped before substitution. Enumerated rather than learned: the set is small, every entry is
# a pattern a reader would recognize as introducing a definition, and one that fires wrongly
# costs a passage the model then has to reject, not a wrong axiom.
_CUES = (
    ("is a",            r"\b{s}\b\s+(?:is|are)\s+(?:a|an|the)\b"),
    ("is defined as",   r"\b{s}\b\s+(?:is|are)\s+(?:formally\s+)?defined\s+as\b"),
    ("we define",       r"\bwe\s+define\s+{s}\b"),
    ("refers to",       r"\b{s}\b\s+refers?\s+to\b"),
    ("means",           r"\b{s}\b\s+means\b"),
    ("by X we mean",    r"\bby\s+{s}\s*,?\s+we\s+mean\b"),
    ("also known as",   r"\b{s}\b\s*[,(]?\s*(?:also\s+(?:known\s+as|called)|a\.?k\.?a\.?)\b"),
    ("that is",         r"\b{s}\b\s*,\s*(?:that\s+is|i\.e\.)\b"),
    ("or simply",       r"\b{s}\b\s*,?\s+or\s+simply\b"),
    ("es un",           r"\b{s}\b\s+(?:es|son)\s+(?:un|una|el|la|los|las)\b"),
    ("se define",       r"(?:se\s+define\s+{s}\s+como|\b{s}\b\s+se\s+define\s+como)"),
    ("también llamado", r"\b{s}\b\s*[,(]?\s*(?:también\s+(?:llamado|llamada|conocido|conocida))"),
)

_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class Passage:
    document_id: str
    block_id: str
    page: int
    cue: str
    text: str
    surface: str


def definitional(text: str, surfaces: list[str]) -> tuple[str, str] | None:
    """The first cue this passage fires, and the surface form it fired on.

    One cue is enough. A passage is a candidate, not a verdict — the model still has to find
    something in it worth writing down, and the point of the filter is only to stop paying for
    paragraphs that were never going to contain a definition.
    """
    lowered = text.lower()
    for surface in surfaces:
        words = surface.lower().split()
        if not words or words[0] not in lowered:
            continue    # cheap rejection first: this runs over every block times every class
        # Joined with `\s+` rather than escaped whole: a phrase broken across a line is the
        # same phrase, and a parsed PDF breaks lines wherever the column ended.
        escaped = r"\s+".join(re.escape(word) for word in words)
        for name, pattern in _CUES:
            if re.search(pattern.format(s=escaped), lowered):
                return name, surface
    return None


def passages_for(
    blocks: list[dict], surfaces: list[str], *, limit: int, max_chars: int = 1200
) -> list[Passage]:
    """Definitional blocks for one class, best-effort ordered by how many documents they span.

    Spread across documents on purpose: five passages from one paper describe that paper's
    usage, and the circularity control below is about exactly that difference.
    """
    found: list[Passage] = []
    for block in blocks:
        hit = definitional(block["text"], surfaces)
        if hit is None:
            continue
        cue, surface = hit
        found.append(Passage(
            document_id=block["document_id"], block_id=block["id"], page=block["page"],
            cue=cue, text=block["text"][:max_chars], surface=surface,
        ))

    # Round-robin over the documents: breadth before depth, so a class attested once in each
    # of four papers is preferred to one attested four times in the same paper.
    by_document: dict[str, list[Passage]] = {}
    for passage in found:
        by_document.setdefault(passage.document_id, []).append(passage)

    chosen: list[Passage] = []
    queues = [iter(items) for _, items in sorted(by_document.items())]
    while queues and len(chosen) < limit:
        for queue in list(queues):
            item = next(queue, None)
            if item is None:
                queues.remove(queue)
                continue
            chosen.append(item)
            if len(chosen) >= limit:
                break
    return chosen


# ─────────────────────────  asking for the improvement  ─────────────────────────

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""Below are passages from research papers that appear to define one concept, and
the definition an ontology currently gives it. Improve the definition using only what the
passages say, and collect the other names they use for the same concept.

CONCEPT: {label}
CURRENT DEFINITION: {gloss}

PASSAGES:
{passages}

Rules:
- Use only the passages. If they add nothing the current definition does not already say,
  answer with `"definition": null`. That is a normal answer and often the right one.
- Do not copy a passage. Write one sentence of at most 40 words, in the words a research paper
  would use, saying what the thing is and what separates it from neighbouring concepts.
- Never describe the ontology, a property or a relation.
- `alt_labels`: other names for THIS SAME concept that literally appear in the passages —
  spelled-out forms of an acronym, a synonym introduced by "also known as", a common variant.
  Not narrower kinds, not broader categories, not examples. Empty is a normal answer.

Answer with JSON only:
{{"definition": {{"en": "...", "es": "..."}} | null,
  "alt_labels": ["..."],
  "why": "one sentence, or why nothing changed"}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def payload(label: str, gloss: str | None, passages: list[Passage]) -> dict[str, str]:
    rendered = "\n\n".join(
        f"[{index}] ({passage.document_id}, p. {passage.page}, cue “{passage.cue}”)\n"
        f"{passage.text}"
        for index, passage in enumerate(passages, start=1)
    )
    return {
        "label": label,
        "gloss": gloss or "(none yet)",
        "passages": rendered or "(none)",
    }


def parse(text: str, payload: dict[str, str]) -> dict:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())

    definition = data.get("definition")
    if definition is not None:
        if not isinstance(definition, dict) or not str(definition.get("en", "")).strip():
            raise ValueError("a definition must carry at least `en`, or be null")
        definition = {
            "en": str(definition.get("en", "")).strip(),
            "es": str(definition.get("es", "")).strip(),
        }

    alt_labels = data.get("alt_labels") or []
    if not isinstance(alt_labels, list):
        raise ValueError("alt_labels must be a list")
    return {
        "definition": definition,
        "alt_labels": [str(item).strip() for item in alt_labels if str(item).strip()],
        "why": str(data.get("why", "")).strip(),
    }


# ─────────────────────────  what gets written  ─────────────────────────


@dataclass
class Enrichment:
    iri: str
    definition: dict[str, str] | None = None
    alt_labels: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    cues: list[str] = field(default_factory=list)
    why: str = ""
    dropped: list[str] = field(default_factory=list)   # names the passages did not contain

    @property
    def empty(self) -> bool:
        return self.definition is None and not self.alt_labels


def verified(
    answer: dict, passages: list[Passage], *, known: set[str], iri: str
) -> Enrichment:
    """Keep only the synonyms the passages actually contain.

    Checked mechanically, the way `coref` checks that every grouped id exists and `bridge`
    checks that a named class is in the ontology. A synonym the model produced from its own
    knowledge is not an enrichment from the corpus — it may even be right, but it would be
    recorded with a provenance that does not hold, and the circularity control is built on
    that provenance meaning what it says.
    """
    haystack = " \n ".join(passage.text for passage in passages).lower()
    kept, dropped = [], []
    for name in answer["alt_labels"]:
        lowered = name.lower()
        if lowered in known:
            continue        # already a label of some class; adding it invites a collision
        (kept if _contains(haystack, lowered) else dropped).append(name)
    return Enrichment(
        iri=iri,
        definition=answer["definition"],
        alt_labels=kept,
        documents=sorted({passage.document_id for passage in passages}),
        cues=sorted({passage.cue for passage in passages}),
        why=answer["why"],
        dropped=dropped,
    )


def _contains(haystack: str, needle: str) -> bool:
    """Whitespace-insensitive, so a line break inside a phrase does not hide it."""
    pattern = r"\s+".join(re.escape(word) for word in _WORD.findall(needle))
    return bool(pattern) and re.search(pattern, haystack) is not None


# Every annotation property this stage writes. Cross-checked against the seed's declaration in
# a test: an undeclared annotation leaves OWL 2 DL, and the symptom is not an error — it is ELK
# quietly dropping to a fragment too small to filter with.
_PREDICATES = {
    "definition": SKOS.definition,
    "altLabel": SKOS.altLabel,
    "historyNote": SKOS.historyNote,
}


def apply(graph: Graph, enrichments: list[Enrichment]) -> Graph:
    """A new graph, never the one passed in — the caller still needs the previous state.

    Definitions replace; alt labels only ever accumulate. A synonym the corpus attested once is
    still attested after a later iteration fails to find it again, and removing it would undo
    the matching the loop just gained.
    """
    extended = Graph()
    for prefix, namespace in graph.namespaces():
        extended.bind(prefix, namespace)
    for triple in graph:
        extended.add(triple)

    for item in enrichments:
        subject = URIRef(item.iri)
        if item.definition:
            extended.remove((subject, SKOS.definition, None))
            for language, text in item.definition.items():
                if text:
                    extended.add((subject, SKOS.definition, Literal(text, lang=language)))
        for name in item.alt_labels:
            extended.add((subject, SKOS.altLabel, Literal(name, lang="en")))
        if not item.empty and item.documents:
            extended.add((subject, SKOS.historyNote, Literal(
                "enriched from " + ", ".join(item.documents), lang="en"
            )))
    return extended


# ─────────────────────────  provenance and circularity  ─────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: Store) -> None:
    conn.script(SCHEMA)
    conn.commit()


def persist(
    conn: Store, version_id: str, enrichments: list[Enrichment],
    passages: dict[str, list[Passage]] | None = None,
) -> None:
    install(conn)
    passages = passages or {}
    rows = []
    for item in enrichments:
        if item.empty:
            continue    # a class the corpus did not improve contributed nothing to it
        quotes = {
            passage.document_id: passage.text[:300]
            for passage in passages.get(item.iri, [])
        }
        for document_id in item.documents:
            rows.append((version_id, item.iri, document_id, ", ".join(item.cues),
                         quotes.get(document_id, ""), _now()))
    conn.executemany(
        "INSERT OR REPLACE INTO gloss_contributions "
        "(version_id, iri, document_id, cue, quote, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def contributors(conn: Store, iri: str) -> list[str]:
    install(conn)
    return [
        row["document_id"] for row in conn.execute(
            "SELECT DISTINCT document_id FROM gloss_contributions WHERE iri = ? "
            "ORDER BY document_id",
            (iri,),
        )
    ]


def circular_matches(conn: Store, version_id: str) -> list[dict]:
    """Typed mentions whose own document helped write the class they were typed to.

    Deliberately not filtered by the version that recorded the contribution: once a document
    has described a class, every later match of that document against it carries the same
    dependency. These are not errors and they are not thrown away — they are the ones that must
    not be counted as independent evidence of coverage (PREP-NORMALIZE).
    """
    install(conn)
    return [
        dict(row) for row in conn.execute(
            "SELECT t.mention_id, t.iri, m.document_id, m.surface_text, t.score, t.zone "
            "FROM mention_typing t "
            "JOIN mentions m ON m.id = t.mention_id "
            "JOIN gloss_contributions g ON g.iri = t.iri AND g.document_id = m.document_id "
            "WHERE t.version_id = ? AND t.iri IS NOT NULL "
            "GROUP BY t.mention_id ORDER BY t.score DESC",
            (version_id,),
        )
    ]
