"""Factual conflicts — document 12 asserts X, document 47 asserts ¬X (spec 6.4).

Different in kind from the modelling commitments of 6.6. A modelling commitment is a choice
about how to describe the world; a factual conflict is two documents describing it differently,
and the ontology has to hold both facts and the disagreement.

**The volume filter is the design.** Deciding case by case is the manual review the pipeline
exists to avoid (D5), so the split is mechanical:

    a conflict that does not break the reasoner  →  notarized by default, silently
    a conflict that does break it                →  reaches the user, and there will be few

The silent default is notarize because it is the only policy that destroys no information. It
is applied without asking and reported as a count, not as a queue.

**What conflicts, in this pipeline.** The ABox derives from the mention layer, so the assertion
a document makes about an entity is its type. Two documents typing the same entity to two
classes is the disagreement; whether it is a real contradiction is a question for the TBox, and
only the reasoner can answer it — two classes with no declared relation are simply two facts.

**One conflict is an ABox case; a pattern is a TBox question.** The spec's practical rule. The
same pair of classes colliding over and over is not many little disagreements, it is a sign
that the two classes are being used for one thing, or that the property needs contextualizing —
and contextualization changes the shape of every query over it, including the CQs' SPARQL, so
it belongs to branching as a modelling axis and never to a per-case decision.

**Refuted and misextracted must not be mixed.** They look identical in an interface and are
opposite signals. `refuted` means the document asserts X and X is not true — the assertion is
excluded from the ABox. `misextracted` means the document never said X and the extractor
misread it — that is a B1 bug, and it belongs to the evaluation set. Merging them loses the
only free source of extraction-error labels this system has.

Under the open-world assumption, not asserting X and asserting ¬X are different things: the
first is silence, the second is knowledge. A refutation is recorded as a decision about the
mention, not written into the ontology as a negative assertion, unless someone actively knows
the claim is false.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDFS

from . import review

FACTUAL_CONFLICT = "factual_conflict"
CONFLICT_PATTERN = "conflict_pattern"

REFUTED = "refuted"
MISEXTRACTED = "misextracted"
MARKS = (REFUTED, MISEXTRACTED)

SCHEMA = """
-- Falsity marks. Deliberately not a column on `mentions`: the mention layer is append-only,
-- and a mark is a decision *about* a mention rather than a correction *of* it.
CREATE TABLE IF NOT EXISTS assertion_marks (
  mention_id  TEXT PRIMARY KEY,
  mark        TEXT NOT NULL,     -- refuted | misextracted
  why         TEXT,
  created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_marks_kind ON assertion_marks(mark);
"""


@dataclass
class Conflict:
    """One entity that two documents typed differently."""

    anchor: str                                    # the entity's anchor mention id
    label: str
    classes: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    mentions: dict[str, list[str]] = field(default_factory=dict)   # class IRI -> mention ids
    incompatible: list[tuple[str, str]] = field(default_factory=list)

    @property
    def breaks_reasoner(self) -> bool:
        """The whole volume filter turns on this. Two classes with no declared relation are
        two facts; two the TBox calls incompatible are a contradiction."""
        return bool(self.incompatible)

    @property
    def pairs(self) -> list[tuple[str, str]]:
        return [
            (first, second)
            for index, first in enumerate(sorted(self.classes))
            for second in sorted(self.classes)[index + 1:]
        ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ─────────────────────────  what the TBox calls incompatible  ─────────────────────────


def asserted_incompatibilities(graph: Graph) -> set[frozenset[str]]:
    """Disjointness as the TBox asserts it, propagated down both hierarchies.

    Disjointness is symmetric and it is inherited: if A and B are disjoint, so are every
    subclass of A and every subclass of B. Reading it one way round, or only between the two
    named classes, is the bug this project already made once.

    This is the offline answer, and it is a lower bound — a class can be unsatisfiable
    together with another for reasons no `owl:disjointWith` states. `incompatible_pairs` asks
    the reasoner when one is available; this is what the command falls back to when it is not,
    and it says so rather than claiming there is no conflict.
    """
    descendants = _descendants(graph)
    pairs: set[frozenset[str]] = set()
    for left, _, right in graph.triples((None, OWL.disjointWith, None)):
        if not (isinstance(left, URIRef) and isinstance(right, URIRef)):
            continue
        for a in descendants.get(str(left), {str(left)}):
            for b in descendants.get(str(right), {str(right)}):
                if a != b:
                    pairs.add(frozenset((a, b)))
    return pairs


def ancestors(graph: Graph) -> dict[str, set[str]]:
    """Every class to each class's superclasses, transitively.

    Needed because subsumption is not disagreement: an entity typed both `Interview` and
    `Technique`, where the first is a kind of the second, is one fact stated at two levels of
    detail. Counting it as a conflict would fill the report with the hierarchy itself.
    """
    parents: dict[str, set[str]] = {}
    for child, _, parent in graph.triples((None, RDFS.subClassOf, None)):
        if isinstance(child, URIRef) and isinstance(parent, URIRef):
            parents.setdefault(str(child), set()).add(str(parent))

    closure: dict[str, set[str]] = {}

    def walk(node: str, seen: frozenset[str]) -> set[str]:
        if node in closure:
            return closure[node]
        result: set[str] = set()
        for parent in parents.get(node, ()):
            if parent in seen:
                continue
            result.add(parent)
            result |= walk(parent, seen | {node})
        closure[node] = result
        return result

    for child in list(parents):
        walk(child, frozenset())
    return closure


def unentailed(classes: Sequence[str], above: dict[str, set[str]]) -> list[str]:
    """The classes that say something the others do not.

    A superclass of another class in the same set is dropped: it is entailed, so keeping it
    would make every deep hierarchy read as a disagreement with itself.
    """
    kept = [
        iri for iri in classes
        if not any(other != iri and iri in above.get(other, ()) for other in classes)
    ]
    return sorted(kept) or sorted(classes)


def _descendants(graph: Graph) -> dict[str, set[str]]:
    children: dict[str, set[str]] = {}
    for child, _, parent in graph.triples((None, RDFS.subClassOf, None)):
        if isinstance(child, URIRef) and isinstance(parent, URIRef):
            children.setdefault(str(parent), set()).add(str(child))

    closure: dict[str, set[str]] = {}

    def walk(node: str, seen: frozenset[str]) -> set[str]:
        if node in closure:
            return closure[node]
        result = {node}
        for child in children.get(node, ()):
            if child not in seen:
                result |= walk(child, seen | {node})
        closure[node] = result
        return result

    for parent in list(children):
        walk(parent, frozenset())
    return closure


# ─────────────────────────  detection  ─────────────────────────


def detect(
    groups: dict[str, list[dict]],
    typings: dict[str, tuple[str | None, str]],
    *,
    accepted_zones: Sequence[str],
    above: dict[str, set[str]] | None = None,
) -> list[Conflict]:
    """Entities whose mentions were typed to more than one class.

    `groups` maps an anchor mention id to its member rows, exactly as the mapping rules group
    them — the entity is the unit of disagreement, because a conflict between two mentions of
    different things is not a conflict at all.

    `above` is the subclass closure. Without it every entity typed at two levels of one
    hierarchy reads as a disagreement, which is the hierarchy arguing with itself.

    Whether a disagreement is a contradiction is a separate question, answered by `classify`:
    finding them costs a scan, and deciding them costs a reasoner.
    """
    zones = set(accepted_zones)
    conflicts = []
    for anchor, members in sorted(groups.items()):
        by_class: dict[str, list[str]] = {}
        for member in members:
            iri, zone = typings.get(member["id"], (None, ""))
            if iri and zone in zones:
                by_class.setdefault(iri, []).append(member["id"])
        classes = unentailed(sorted(by_class), above or {})
        if len(classes) < 2:
            continue
        documents = sorted({member["document_id"] for member in members})
        conflict = Conflict(
            anchor=anchor, label=members[0]["surface_text"], classes=classes,
            documents=documents, mentions={iri: sorted(ids) for iri, ids in by_class.items()},
        )
        conflicts.append(conflict)
    return conflicts


def candidate_pairs(conflicts: Sequence[Conflict]) -> list[tuple[str, str]]:
    """The class pairs that actually collided — all the reasoner needs to be asked about.

    Every pair of classes is quadratic in the inventory and almost entirely irrelevant: what
    matters is the handful the data put in the same entity.
    """
    return sorted({pair for conflict in conflicts for pair in conflict.pairs})


def classify(
    conflicts: Sequence[Conflict], incompatible: set[frozenset[str]]
) -> list[Conflict]:
    """Split the disagreements into contradictions and facts. This is the volume filter."""
    for conflict in conflicts:
        conflict.incompatible = [
            pair for pair in conflict.pairs if frozenset(pair) in incompatible
        ]
    return list(conflicts)


def by_pair(conflicts: Sequence[Conflict]) -> dict[tuple[str, str], int]:
    """How often each pair of classes collided. One collision is a case; a pattern is a
    question about the TBox (6.4's practical rule)."""
    tally: dict[tuple[str, str], int] = {}
    for conflict in conflicts:
        for pair in conflict.pairs:
            tally[pair] = tally.get(pair, 0) + 1
    return tally


# ─────────────────────────  what reaches the user, and what does not  ─────────────────────────


def findings(
    conflicts: Sequence[Conflict], labels: dict[str, str], *, pattern_threshold: int
) -> list[review.Finding]:
    """Only the conflicts the reasoner would break on, plus the patterns.

    Everything else is notarized without asking. A queue that lists every disagreement is the
    manual review D5 rules out, and the conflicts that matter are exactly the ones where a
    person's judgement changes the answer.
    """
    def name(iri: str) -> str:
        return labels.get(iri, iri.rsplit("/", 1)[-1])

    items = []
    for conflict in conflicts:
        if not conflict.breaks_reasoner:
            continue
        pairs = ", ".join(f"{name(a)} / {name(b)}" for a, b in conflict.incompatible)
        items.append(review.Finding(
            kind=FACTUAL_CONFLICT,
            subject_iri=conflict.anchor,
            summary=(
                f"«{conflict.label}» is typed to classes the ontology calls incompatible "
                f"({pairs}), across {len(conflict.documents)} documents"
            ),
            payload={
                "classes": conflict.classes,
                "incompatible": [list(pair) for pair in conflict.incompatible],
                "documents": conflict.documents,
                "mentions": conflict.mentions,
            },
        ))

    for (first, second), count in sorted(by_pair(conflicts).items()):
        if count < pattern_threshold:
            continue
        items.append(review.Finding(
            kind=CONFLICT_PATTERN,
            subject_iri=first,
            summary=(
                f"{name(first)} and {name(second)} collide over {count} entities. A recurring "
                f"collision is one question about the TBox, not {count} cases"
            ),
            payload={"pair": [first, second], "entities": count},
        ))
    return items


# ─────────────────────────  marks  ─────────────────────────


def mark(
    conn: sqlite3.Connection, mention_ids: Sequence[str], kind: str, why: str = ""
) -> int:
    """Record a falsity mark. `refuted` and `misextracted` are opposite signals (6.4)."""
    if kind not in MARKS:
        raise ValueError(f"a mark is {' or '.join(MARKS)}, not {kind!r}")
    install(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO assertion_marks (mention_id, mark, why, created_at) "
        "VALUES (?, ?, ?, ?)",
        [(mention_id, kind, why, _now()) for mention_id in mention_ids],
    )
    conn.commit()
    return len(mention_ids)


def marks(conn: sqlite3.Connection, kind: str | None = None) -> dict[str, str]:
    install(conn)
    query = "SELECT mention_id, mark FROM assertion_marks"
    params: tuple = ()
    if kind:
        query += " WHERE mark = ?"
        params = (kind,)
    return {row["mention_id"]: row["mark"] for row in conn.execute(query, params)}


def as_exceptions(conn: sqlite3.Connection) -> dict[str, str]:
    """Marks as mapping-rule exceptions, so they travel into the rules hash.

    This is what makes a refutation take effect. Regeneration is idempotent over
    (state, rules), so a decision that changed no rule would be a decision the ABox never
    notices — the mention would stay in it until something unrelated forced a rebuild.
    """
    return {f"mention:{mention_id}": mark for mention_id, mark in sorted(marks(conn).items())}


def misextractions(conn: sqlite3.Connection) -> list[dict]:
    """The extraction errors, for the evaluation set.

    Free labels: nobody annotated them on purpose, they are what a reader noticed while
    resolving conflicts, and they are the only source of B1 error labels this system has that
    costs nothing to produce.
    """
    install(conn)
    return [
        dict(row) for row in conn.execute(
            "SELECT a.mention_id, a.why, a.created_at, m.document_id, m.page, m.surface_text, "
            "m.span_start, m.span_end FROM assertion_marks a "
            "JOIN mentions m ON m.id = a.mention_id "
            "WHERE a.mark = ? ORDER BY m.document_id, m.span_start",
            (MISEXTRACTED,),
        )
    ]


def export_misextractions(conn: sqlite3.Connection) -> str:
    """JSONL, the same shape the retention set uses, so both feed one evaluation."""
    return "\n".join(
        json.dumps({
            "document_id": row["document_id"],
            "mention_id": row["mention_id"],
            "text": row["surface_text"],
            "start": row["span_start"],
            "end": row["span_end"],
            "page": row["page"],
            "error": MISEXTRACTED,
            "why": row["why"] or "",
        }, ensure_ascii=False)
        for row in misextractions(conn)
    )
