"""PREP-CQ-GENERATED — assisted generation of competency questions.

**The circularity warning comes first, because it is the whole caveat of this stage.** Questions
derived from the corpus measure completeness *with respect to the corpus*, not with respect to
the domain. It is the same limitation as novelty saturation, and it is not fixable from inside:
the mitigation is PREP-CQ-USER, the questions the user writes without looking at these.

Four steps, and three of them are mechanical.

**Stratified sampling**, not the whole corpus. Ten to fifteen passages per stratum —
definitions, tables, enumerations, restrictions, procedures — because the strata are what make
the different question types possible: a passage stating "must not" is where a restrictive
question comes from, and it is invisible in a random sample of paragraphs.

**Generation per type, with a quota.** One prompt per category. The types come from the declared
expressivity, and two of them matter more than the rest: *inferential* (whose stated point is
that the reasoner contributes something) and *negative* (which makes the open world explicit).
Those two are also the ones a model never generates on its own, which is exactly why the quota
is mandatory rather than a target.

**Mechanical filtering, before any human looks.** Deduplicate; drop what a single triple already
answers; drop what does not formalize as SPARQL — if it is not a query it cannot be a stopping
criterion; drop what carries no citation and page. The user reviews what survives, and that
review is one-time work, not per iteration.

Bilingual: each question in the language of its source passage, the SPARQL against canonical
IRIs. The same question in two languages does not count twice.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .cq import GENERATED, TYPES, CompetencyQuestion, MalformedQuery, validate
from .llm import Prompt

STAGE = "prep_cq_generated"

PROPOSED = "proposed"

# ─────────────────────────  step 1: stratified sampling  ─────────────────────────

# Each stratum is a cue for the kind of passage that makes one kind of question possible. A
# random sample of paragraphs would contain almost no restrictions, and restrictive questions
# would then be impossible to ground in a citation.
STRATA: dict[str, tuple[str, ...]] = {
    "definitions": (
        r"\bis\s+defined\s+as\b", r"\brefers?\s+to\b", r"\bwe\s+define\b", r"\bis\s+the\s+term\b",
        r"\bse\s+define\s+como\b", r"\bse\s+entiende\s+por\b",
    ),
    "enumerations": (
        r"\bthe\s+following\b", r"\b(types|kinds|categories|classes)\s+of\b",
        r"\bclassified\s+(as|into)\b", r"\bconsists?\s+of\b",
        r"\btipos\s+de\b", r"\bse\s+clasifica\b",
    ),
    "restrictions": (
        r"\bmust\s+(not\s+)?\b", r"\bcannot\b", r"\bmay\s+not\b", r"\bonly\s+if\b",
        r"\brequired\s+to\b", r"\bnever\b",
        r"\bdebe\s+(no\s+)?\b", r"\bno\s+puede\b", r"\bsólo\s+si\b", r"\bsolo\s+si\b",
    ),
    "procedures": (
        r"\bstep\s+\d\b", r"\bprocedure\b", r"\bprotocol\b", r"\bwe\s+(then|first|next)\b",
        r"\bin\s+order\s+to\b", r"\bprocedimiento\b",
    ),
}
TABLE_STRATUM = "tables"


@dataclass
class Passage:
    stratum: str
    document_id: str
    page: int
    text: str

    @property
    def citation(self) -> dict:
        return {"document_id": self.document_id, "page": self.page,
                "quote": self.text[:240]}


def stratum_of(block: dict) -> str | None:
    """Which stratum a block belongs to, or none.

    First match wins, and the order is the dict's: a passage that both defines and enumerates
    is a definition, because that is the stronger signal for grounding a question.
    """
    if block.get("block_type") == "table":
        return TABLE_STRATUM
    text = block.get("text", "").lower()
    for stratum, cues in STRATA.items():
        if any(re.search(cue, text) for cue in cues):
            return stratum
    return None


def sample(
    blocks: Sequence[dict], *, per_stratum: int = 12, seed: int = 0, max_chars: int = 900
) -> dict[str, list[Passage]]:
    """Ten to fifteen passages per stratum, chosen deterministically.

    Seeded rather than "the first N": the first blocks of a corpus are the first documents'
    abstracts, and a sample of abstracts produces questions about abstracts.
    """
    pools: dict[str, list[Passage]] = {}
    for block in blocks:
        stratum = stratum_of(block)
        if stratum is None:
            continue
        pools.setdefault(stratum, []).append(Passage(
            stratum=stratum, document_id=block["document_id"], page=block["page"],
            text=block["text"][:max_chars],
        ))

    chosen: dict[str, list[Passage]] = {}
    for stratum, pool in sorted(pools.items()):
        generator = random.Random(f"{seed}:{stratum}")
        picked = pool if len(pool) <= per_stratum else generator.sample(pool, per_stratum)
        chosen[stratum] = sorted(picked, key=lambda p: (p.document_id, p.page))
    return chosen


# ─────────────────────────  step 2: one prompt per type  ─────────────────────────

# What each type demands of the ontology, in the spec's own terms. The wording matters: a model
# asked for "a competency question" writes definitional ones and nothing else.
TYPE_BRIEF = {
    "definitional": (
        "What kinds of X are there? — it demands a hierarchy.",
        "¿Qué tipos de X existen?",
    ),
    "relational": (
        "Which X is associated with which Y? — it demands properties with domain and range.",
        "¿Qué X está asociado a qué Y?",
    ),
    "restrictive": (
        "Can an X also be a Y? — it demands disjointness.",
        "¿Puede un X ser también un Y?",
    ),
    "quantificational": (
        "How many Ys can an X have? — it demands functionality.",
        "¿Cuántos Y puede tener un X?",
    ),
    "inferential": (
        "If X is an A and every A is a B, is X a B? — the point is that the REASONER "
        "contributes the answer, not the asserted triples. Write a question whose answer is "
        "only entailed.",
        "Si X es A y todo A es B, ¿X es B?",
    ),
    "negative": (
        "Which X does NOT satisfy Y? — it makes the open world explicit. Remember that not "
        "asserting something is silence, not a denial: ask about what the ontology states "
        "negatively, not about what it happens to omit.",
        "¿Qué X no cumple Y?",
    ),
}

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""Write competency questions of ONE type for an ontology, grounded in passages
from a research corpus. A competency question is a question the finished ontology must be able
to answer.

THE TYPE YOU MUST WRITE: {cq_type}
{brief}
An example of the shape, in Spanish: {shape}

CLASSES THE ONTOLOGY ALREADY HAS:
{classes}

PASSAGES (each numbered; you must cite the one a question comes from):
{passages}

Rules:
- Write at most {count} questions, all of the type above. Fewer is fine; inventing them is not.
- Each question must come from a passage and cite its number. A question you cannot point at is
  not usable and will be discarded.
- Each question must be paired with a SPARQL query that answers it against the ontology, using
  the prefixes rdf:, rdfs:, owl: and skos:. Write the query against class LABELS via
  skos:prefLabel, never against invented IRIs.
- Do not write a question a single triple answers, such as "what is the label of X".
- Write the question in the language of its passage.

Answer with JSON only:
{{"questions": [{{"question": "...", "language": "en" | "es", "passage": 1,
                  "sparql": "SELECT ..."}}]}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def payload(cq_type: str, passages: Sequence[Passage], classes: Sequence[str], count: int) -> dict:
    brief, shape = TYPE_BRIEF[cq_type]
    rendered = "\n\n".join(
        f"[{index}] ({item.document_id}, p. {item.page})\n{item.text}"
        for index, item in enumerate(passages, start=1)
    )
    return {
        "cq_type": cq_type,
        "brief": brief,
        "shape": shape,
        "classes": ", ".join(sorted(classes)[:60]) or "(none)",
        "passages": rendered or "(none)",
        "count": str(count),
    }


def parse(text: str, payload: dict) -> dict:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())
    questions = data.get("questions")
    if not isinstance(questions, list):
        raise ValueError("`questions` must be a list")

    parsed = []
    for entry in questions:
        question = str(entry.get("question", "")).strip()
        sparql = str(entry.get("sparql", "")).strip()
        if not question or not sparql:
            raise ValueError("every question needs its text and its SPARQL")
        parsed.append({
            "question": question,
            "sparql": sparql,
            "language": str(entry.get("language", "en")).strip() or "en",
            "passage": int(entry.get("passage", 0) or 0),
        })
    return {"questions": parsed}


# ─────────────────────────  step 3: the mechanical filter  ─────────────────────────


@dataclass
class Filtered:
    kept: list[CompetencyQuestion] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)   # (question, why)

    def drop(self, question: str, why: str) -> None:
        self.dropped.append((question, why))


_TRIPLE_PATTERN = re.compile(r"\?\w+|<[^>]+>|\w+:\w+|\"[^\"]*\"")


def answered_by_one_triple(sparql: str) -> bool:
    """A query with a single triple pattern and no filter, aggregate or path.

    Not a stopping criterion: it asks whether one fact was written down, which any ontology
    with that fact passes and any without fails, regardless of whether it models the domain.
    """
    body = sparql[sparql.lower().find("where"):] if "where" in sparql.lower() else sparql
    if re.search(r"\b(FILTER|GROUP\s+BY|COUNT|UNION|MINUS|OPTIONAL|NOT\s+EXISTS)\b", body,
                 re.IGNORECASE):
        return False
    if "/" in body or "+" in body or "*" in body:
        return False   # a property path is doing real work
    statements = [part for part in re.split(r"[.;]", body) if _TRIPLE_PATTERN.search(part)]
    return len(statements) <= 1


def mint_id(question: str) -> str:
    return "cq_" + hashlib.sha1(question.strip().lower().encode("utf-8")).hexdigest()[:10]


def _normalized(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def screen(
    answers: dict[str, list[dict]],
    passages: dict[str, list[Passage]],
    existing: Sequence[str] = (),
    *,
    similarity: Callable[[str, str], float] | None = None,
    duplicate_threshold: float = 0.92,
) -> Filtered:
    """Everything mechanical, before any person reads a question.

    `answers` maps a CQ type to what the model returned for it; `passages` maps that type to
    the passages it was shown, so a cited number can be turned back into a citation.

    Deduplication uses the encoder when there is one and normalized text otherwise. The
    fallback is weaker and it is not a silent one: two questions that differ by a word survive
    it, which is a review cost, not a wrong stopping criterion.
    """
    result = Filtered()
    seen = [_normalized(item) for item in existing]
    kept_texts: list[str] = []

    for cq_type, entries in sorted(answers.items()):
        shown = passages.get(cq_type, [])
        for entry in entries:
            question, sparql = entry["question"], entry["sparql"]
            index = entry.get("passage", 0)
            if not 1 <= index <= len(shown):
                result.drop(question, "cites no passage, or one it was not shown")
                continue
            if _normalized(question) in seen:
                result.drop(question, "duplicate")
                continue
            if similarity is not None and any(
                similarity(question, other) >= duplicate_threshold for other in kept_texts
            ):
                result.drop(question, "near-duplicate of one already kept")
                continue
            candidate = CompetencyQuestion(
                id=mint_id(question), question=question, sparql=sparql, origin=GENERATED,
                language=entry.get("language", "en"), cq_type=cq_type, status=PROPOSED,
                citation=shown[index - 1].citation,
            )
            # Parsed before its shape is judged: counting triple patterns in text that is not
            # a query measures nothing, and "does not parse" is the more useful answer anyway.
            try:
                validate(candidate)
            except MalformedQuery as exc:
                result.drop(question, f"its SPARQL does not parse: {str(exc)[:60]}")
                continue
            except ValueError as exc:
                result.drop(question, str(exc)[:80])
                continue
            if answered_by_one_triple(sparql):
                result.drop(question, "a single triple answers it")
                continue

            seen.append(_normalized(question))
            kept_texts.append(question)
            result.kept.append(candidate)
    return result


def shortfall(kept: Sequence[CompetencyQuestion], quota: dict[str, int]) -> dict[str, int]:
    """How far each type is from its quota.

    Reported rather than filled: the inferential and negative types are the ones a model does
    not produce on its own, so a shortfall there is the finding — quietly topping it up with
    definitional questions would hide exactly what the quota exists to force.
    """
    have: dict[str, int] = {}
    for question in kept:
        have[question.cq_type] = have.get(question.cq_type, 0) + 1
    return {
        cq_type: quota.get(cq_type, 0) - have.get(cq_type, 0)
        for cq_type in TYPES
        if quota.get(cq_type, 0) - have.get(cq_type, 0) > 0
    }
