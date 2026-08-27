"""ITER-BRIDGE — world-knowledge bridging, between matching and class induction.

Before an orphan mention becomes a proposed class, ask whether it relates to a class the seed
already has **even though no document says so**. The corpus writes "focus group" and the seed
has `Technique`; no paragraph states that one is a case of the other, and the model's general
knowledge does. The spec is explicit that treating the absence of that bridge as a matcher
failure is a design error.

Why this stage is not optional. Induction runs on whatever the matcher left orphaned, so every
mention the seed did cover but the matcher failed to connect becomes a spurious induced class —
the false orphan feeding the inducer, which is the failure the no-go gate exists to prevent.
This is the filter between them.

**The model classifies, the code builds the logic.** It is never asked for OWL: it gets a
phrase and a short list of candidate classes and answers one atomic question — is this an
example of that class, a kind of it, or neither. The axiom, if any, is assembled later by
ITER-AXIOMATIZE.
The answer is verified mechanically: a class the model did not receive is a rejected answer,
not a bridge, in the same way coreference verifies that every mention id it grouped exists.

**Provenance splits in two here** (6.2b), and that is this stage's structural consequence:

    textual          cita, page, bbox        the evidence filter applies: no citation, discarded
    world_knowledge  no citation possible    the evidence filter does not apply

Without the distinction, "every axiom without a citation is discarded" would kill exactly the
bridges that make the seed useful. Bridges still face the reasoner and OntoClean; what they
skip is the evidence filter, and they reach the user marked as what they are.

**The unit is the surface form, not the mention.** One answer covers every occurrence of a
phrase, which is what keeps the stage affordable — and it assumes one surface form is one
concept. That assumption is the homonymy gap the matcher has too: "cell" in a biology paper and
"cell" in a security paper get one answer. Bridges only ever propose, and a proposal is read by
a human, so the cost of the assumption is bounded here in a way it is not in the matcher.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .llm import Prompt
from .matching import Target, dot, normalize

STAGE = "iter_bridge"

WORLD_KNOWLEDGE = "world_knowledge"
TEXTUAL = "textual"

INSTANCE_OF = "instance_of"
SUBCLASS_OF = "subclass_of"
NONE = "none"
RELATIONS = frozenset({INSTANCE_OF, SUBCLASS_OF})

PROPOSED = "proposed"
ACCEPTED = "accepted"
REJECTED = "rejected"

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""A phrase was found in a research corpus. An ontology of the domain failed to
match it to any of its classes, so it is about to be treated as a concept the ontology does not
cover. Before that, judge whether your general knowledge connects it to one of these classes,
even though no document in the corpus says so.

PHRASE: {phrase}

CANDIDATE CLASSES:
{candidates}

Answer with JSON only:
{{"relation": "instance_of" | "subclass_of" | "none",
  "class_id": "the id of the class you chose, or null",
  "why": "one sentence"}}

- `instance_of`: the phrase names one particular thing that is an example of the class.
- `subclass_of`: the phrase names a kind of the class — a narrower concept, not one example.
- `none`: your knowledge does not connect it to any of them.

Judge the meaning, not the wording. Sharing a word with a class name is not a relation: a
"cell" in a paper about organisations is not the biological class, and a "subject" meaning
*topic* is not a research subject. If the phrase is too vague to place — "the data", "this
approach" — answer `none`.

`none` is a normal answer and is more useful than a forced connection. The ontology is meant to
grow, and a concept it genuinely lacks should become a new class rather than be filed under a
class it does not belong to.""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Candidate:
    """One orphan concept and the seed classes it might bridge to."""

    surface: str
    mention_ids: list[str] = field(default_factory=list)
    targets: list[tuple[float, Target]] = field(default_factory=list)

    @property
    def id(self) -> str:
        return hashlib.sha1(self.surface.encode("utf-8")).hexdigest()[:16]

    @property
    def support(self) -> int:
        return len(self.mention_ids)


@dataclass
class Bridge:
    surface: str
    target_iri: str
    relation: str
    why: str
    score: float
    mention_ids: list[str] = field(default_factory=list)
    provenance: str = WORLD_KNOWLEDGE

    @property
    def id(self) -> str:
        return hashlib.sha1(
            f"{self.surface}|{self.target_iri}|{self.relation}".encode()
        ).hexdigest()[:16]


SCHEMA = """
CREATE TABLE IF NOT EXISTS bridges (
  id           TEXT PRIMARY KEY,
  version_id   TEXT NOT NULL,     -- bridged against this state of the ontology
  surface      TEXT NOT NULL,
  target_iri   TEXT NOT NULL,
  relation     TEXT NOT NULL,     -- instance_of | subclass_of
  why          TEXT,
  provenance   TEXT NOT NULL,     -- world_knowledge; the evidence filter skips these (6.2b)
  score        REAL,              -- the encoder's score for the chosen class, for context
  support      INTEGER NOT NULL,
  status       TEXT NOT NULL,     -- proposed | accepted | rejected
  created_at   TEXT
);
CREATE TABLE IF NOT EXISTS bridge_mentions (
  bridge_id  TEXT NOT NULL,
  mention_id TEXT NOT NULL,
  PRIMARY KEY (bridge_id, mention_id)
);
CREATE INDEX IF NOT EXISTS idx_bridges_version ON bridges(version_id, status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def candidates(
    rows: Sequence[dict],
    targets: Sequence[Target],
    vectors: Sequence[Sequence[float]],
    target_vectors: Sequence[Sequence[float]],
    *,
    n_candidates: int,
    min_score: float,
) -> list[Candidate]:
    """Group the orphans by surface form and attach the nearest classes to each.

    A surface whose best class falls below `min_score` gets no candidates and is dropped: the
    stage is a bridge to something, and asking the model to relate a phrase to five classes
    none of which is remotely close invites the forced connection the prompt warns against.
    """
    grouped: dict[str, Candidate] = {}
    for row, vector in zip(rows, vectors, strict=True):
        surface = row["surface_text"]
        candidate = grouped.get(surface)
        if candidate is None:
            ranked = sorted(
                (
                    (dot(normalize(vector), normalize(target_vector)), target)
                    for target_vector, target in zip(target_vectors, targets, strict=True)
                ),
                key=lambda pair: (-pair[0], pair[1].iri),
            )[:n_candidates]
            candidate = Candidate(
                surface=surface,
                targets=[pair for pair in ranked if pair[0] >= min_score],
            )
            grouped[surface] = candidate
        candidate.mention_ids.append(row["id"])

    return sorted(
        (item for item in grouped.values() if item.targets),
        key=lambda item: (-item.support, item.id),
    )


def payload(candidate: Candidate) -> dict[str, str]:
    lines = []
    for _, target in candidate.targets:
        gloss = target.gloss or "(no definition)"
        lines.append(f"- id: {target.iri}\n  name: {target.label}\n  definition: {gloss}")
    return {"phrase": candidate.surface, "candidates": "\n".join(lines)}


def parse(text: str, payload_: dict[str, Any]) -> dict[str, Any]:
    """Mechanically verifiable, like coreference's group check.

    The model may only choose among the ids it was given. An id it invented, or one from
    another candidate's list, is a parse error rather than a bridge — the check costs nothing
    and it is the difference between a judgement and a guess we cannot audit.
    """
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())

    relation = str(data.get("relation", "")).strip()
    if relation not in RELATIONS:
        if relation != NONE:
            raise ValueError(f"unknown relation {relation!r}")
        return {"relation": NONE}

    class_id = str(data.get("class_id") or "").strip()
    offered = {
        line.removeprefix("- id: ").strip()
        for line in payload_["candidates"].splitlines()
        if line.startswith("- id: ")
    }
    if class_id not in offered:
        raise ValueError(f"class_id {class_id!r} was not among the candidates offered")

    return {
        "relation": relation,
        "class_id": class_id,
        "why": str(data.get("why", "")).strip(),
    }


def bridges_from(candidate: Candidate, answer: dict[str, Any]) -> Bridge | None:
    if answer.get("relation") not in RELATIONS:
        return None
    scores = {target.iri: score for score, target in candidate.targets}
    return Bridge(
        surface=candidate.surface,
        target_iri=answer["class_id"],
        relation=answer["relation"],
        why=answer.get("why", ""),
        score=scores.get(answer["class_id"], 0.0),
        mention_ids=list(candidate.mention_ids),
    )


def persist(conn: sqlite3.Connection, version_id: str, bridges: Sequence[Bridge]) -> None:
    install(conn)
    conn.execute("DELETE FROM bridges WHERE version_id = ?", (version_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO bridges (id, version_id, surface, target_iri, relation, why, "
        "provenance, score, support, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (bridge.id, version_id, bridge.surface, bridge.target_iri, bridge.relation,
             bridge.why, bridge.provenance, bridge.score, len(bridge.mention_ids),
             PROPOSED, _now())
            for bridge in bridges
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO bridge_mentions (bridge_id, mention_id) VALUES (?, ?)",
        [(bridge.id, mention_id) for bridge in bridges for mention_id in bridge.mention_ids],
    )
    conn.commit()


def bridged_mentions(conn: sqlite3.Connection, version_id: str) -> set[str]:
    """Mentions a bridge already accounts for.

    Induction has to skip them, and that is the whole point of the stage sitting where it does:
    a mention the seed does cover, bridged rather than orphaned, must not also become a new
    class.
    """
    install(conn)
    return {
        row["mention_id"]
        for row in conn.execute(
            "SELECT bm.mention_id FROM bridge_mentions bm JOIN bridges b ON b.id = bm.bridge_id "
            "WHERE b.version_id = ? AND b.status != ?",
            (version_id, REJECTED),
        )
    }


def load(conn: sqlite3.Connection, version_id: str) -> list[dict]:
    install(conn)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM bridges WHERE version_id = ? ORDER BY support DESC, surface",
            (version_id,),
        )
    ]
