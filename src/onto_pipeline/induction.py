"""B3 — class induction from orphan mentions (spec 6.1, 6.6, 3.1).

The division of labour is the pipeline's governing principle: **the code clusters, the model
names.** Grouping mentions by similarity is arithmetic and belongs in code; deciding what a
group of surface forms is called, and what distinguishes it, is the judgement the model is for.
The stage's model setting is called `B3_naming` for that reason.

Clustering here is legitimate where it would not be one layer up. Spec 3.1 permits graph
algorithms over the *orphan mention graph* — raw ABox — and forbids them over the TBox
serialization, because disjointness inverts its sign under structural similarity: two classes
declared incompatible come out looking connected.

Two guards against the generator's known bias of over-producing hierarchy (spec 6.1, 6.6):

- A cluster below `min_support` is not a class. One mention proposing a class is noise, and the
  validator rejects levels with a single subclass anyway.
- The model must state the **division criterion** — what separates this class from its
  neighbours. A proposal without one is rejected here rather than at the reasoner, because 6.6
  makes "sin criterio de división declarado" a rejection condition in its own right.

Nothing is applied. B3 proposes; axiomatizing and applying are later stages, and the nearest
existing class is recorded as a *candidate* parent for B4 to decide on rather than a subsumption
asserted here.
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
from .matching import dot

STAGE = "B3_naming"

PROPOSED = "proposed"
REJECTED = "rejected"

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""These phrases were found in a research corpus and grouped together because they
are used similarly. An ontology of the domain has no class covering them.

PHRASES:
{phrases}

The closest existing class is "{nearest}", defined as: {nearest_gloss}
They were not matched to it, so if they belong to a genuinely different concept, say what that
concept is.

Answer with JSON only:
{{"is_a_class": true/false,
  "label": "a short noun phrase naming the concept",
  "gloss": "one sentence saying what it is, in the words a paper would use",
  "criterion": "what distinguishes it from the closest existing class"}}

Say `"is_a_class": false` when the phrases do not share one concept — grouped by wording rather
than by meaning, or too disparate to name. That answer is expected sometimes and is more useful
than a label invented to fit.

The criterion is required. A class nobody can say how to tell apart from its neighbours is not
worth adding.""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Cluster:
    id: str
    mention_ids: list[str] = field(default_factory=list)
    surfaces: list[str] = field(default_factory=list)
    nearest_iri: str | None = None
    nearest_label: str = ""
    nearest_gloss: str = ""
    nearest_score: float = 0.0

    @property
    def support(self) -> int:
        return len(self.mention_ids)


@dataclass
class Proposal:
    cluster_id: str
    label: str
    gloss: str
    criterion: str
    support: int
    mention_ids: list[str] = field(default_factory=list)
    nearest_iri: str | None = None
    nearest_score: float = 0.0

    @property
    def id(self) -> str:
        return hashlib.sha1(
            f"{self.label}|{sorted(self.mention_ids)}".encode()
        ).hexdigest()[:16]


SCHEMA = """
CREATE TABLE IF NOT EXISTS proposed_classes (
  id            TEXT PRIMARY KEY,
  version_id    TEXT NOT NULL,      -- proposed against this state of the ontology
  label         TEXT NOT NULL,
  gloss         TEXT,
  criterion     TEXT,               -- what separates it; 6.6 rejects a class without one
  nearest_iri   TEXT,               -- a candidate parent for B4, not an asserted subsumption
  nearest_score REAL,
  support       INTEGER NOT NULL,
  status        TEXT NOT NULL,      -- proposed | rejected
  created_at    TEXT
);
CREATE TABLE IF NOT EXISTS proposed_class_mentions (
  proposed_id TEXT NOT NULL,
  mention_id  TEXT NOT NULL,
  PRIMARY KEY (proposed_id, mention_id)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def cluster(
    mention_ids: Sequence[str],
    surfaces: Sequence[str],
    vectors: Sequence[Sequence[float]],
    *,
    threshold: float,
    min_support: int,
) -> list[Cluster]:
    """Single-link over a similarity graph: connected components above the threshold.

    Single-link because a concept's phrasings form a chain — "semi-structured interview" and
    "interview protocol" may each be close to "interview" without being close to each other.
    Its known weakness is chaining two concepts through a bridging phrase, which is what the
    threshold and the model's `is_a_class: false` answer are there to contain.
    """
    parent = list(range(len(mention_ids)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            if dot(vectors[i], vectors[j]) >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    grouped: dict[int, list[int]] = {}
    for index in range(len(mention_ids)):
        grouped.setdefault(find(index), []).append(index)

    clusters = []
    for members in grouped.values():
        if len(members) < min_support:
            continue  # one mention proposing a class is noise, not a class
        members.sort(key=lambda index: surfaces[index])
        clusters.append(
            Cluster(
                id=hashlib.sha1(
                    "|".join(sorted(mention_ids[index] for index in members)).encode()
                ).hexdigest()[:16],
                mention_ids=[mention_ids[index] for index in members],
                surfaces=[surfaces[index] for index in members],
            )
        )
    return sorted(clusters, key=lambda item: (-item.support, item.id))


def payload(cluster_: Cluster, *, max_phrases: int = 30) -> dict[str, str]:
    seen: list[str] = []
    for surface in cluster_.surfaces:
        if surface not in seen:
            seen.append(surface)
    return {
        "phrases": "\n".join(f"- {surface}" for surface in seen[:max_phrases]),
        "nearest": cluster_.nearest_label or "(none)",
        "nearest_gloss": cluster_.nearest_gloss or "(no definition)",
    }


def parse(text: str, payload: dict[str, Any]) -> dict[str, Any]:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())
    if not data.get("is_a_class"):
        return {"is_a_class": False}
    for required in ("label", "criterion"):
        if not str(data.get(required, "")).strip():
            # 6.6 rejects a class with no declared division criterion; so does this.
            raise ValueError(f"a proposed class needs a {required}")
    return {
        "is_a_class": True,
        "label": str(data["label"]).strip(),
        "gloss": str(data.get("gloss", "")).strip(),
        "criterion": str(data["criterion"]).strip(),
    }


def persist(conn: sqlite3.Connection, version_id: str, proposals: list[Proposal]) -> None:
    install(conn)
    conn.execute("DELETE FROM proposed_classes WHERE version_id = ?", (version_id,))
    conn.executemany(
        "INSERT INTO proposed_classes (id, version_id, label, gloss, criterion, nearest_iri, "
        "nearest_score, support, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (p.id, version_id, p.label, p.gloss, p.criterion, p.nearest_iri, p.nearest_score,
             p.support, PROPOSED, _now())
            for p in proposals
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO proposed_class_mentions (proposed_id, mention_id) VALUES (?, ?)",
        [(p.id, mention_id) for p in proposals for mention_id in p.mention_ids],
    )
    conn.commit()


def load(conn: sqlite3.Connection, version_id: str) -> list[dict]:
    install(conn)
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM proposed_classes WHERE version_id = ? ORDER BY support DESC, label",
            (version_id,),
        )
    ]
