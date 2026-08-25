"""Wiring for B2: build targets from an ontology version, store what the matcher decided.

Typing is kept out of the `mentions` table on purpose. The mention layer is immutable except
by extension (spec 3); a typing is derived from one ontology version and is recomputed whenever
the TBox or the glosses change, which is the self-correcting loop of 4.3 — a mention orphaned
at iteration 3 can be typed at 8. Writing it onto the mention would blur the layer boundary the
design calls its central invariant.

Entity resolution does write back to the mention layer, because that is what `candidate_entity`
and `status` are for. Grey-zone pairs land as `possible_duplicate_unresolved`, which is the
state that keeps them out of the functional-property support count (6.8).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from .matching import GREY, Decision, Mention, Target

POSSIBLE_DUPLICATE = "possible_duplicate_unresolved"

SCHEMA = """
CREATE TABLE IF NOT EXISTS mention_typing (
  mention_id  TEXT NOT NULL,
  version_id  TEXT NOT NULL,
  iri         TEXT,             -- NULL is an orphan
  score       REAL,
  zone        TEXT,             -- auto | grey | discarded
  runner_up   TEXT,
  PRIMARY KEY (mention_id, version_id)
);
CREATE INDEX IF NOT EXISTS idx_typing_version ON mention_typing(version_id, zone);
"""


@dataclass
class OrphanSplit:
    """The aggregate orphan rate says nothing (spec 6.2b); only the split is informative.

    Without gold annotations the two kinds cannot be told apart here — that is what the
    retention set is for — so this reports what is knowable: how many were typed, how many
    need a decision, and how many nothing covered.
    """

    typed: int = 0
    grey: int = 0
    orphan: int = 0

    @property
    def total(self) -> int:
        return self.typed + self.grey + self.orphan

    @property
    def orphan_rate(self) -> float:
        return self.orphan / self.total if self.total else 0.0


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def targets_from(graph: Graph, match_against: str) -> list[Target]:
    """Every named class with a preferred label becomes a candidate."""
    targets = []
    for subject in graph.subjects(RDF.type, OWL.Class):
        if not isinstance(subject, URIRef):
            continue
        label = next((str(o) for o in graph.objects(subject, SKOS.prefLabel)), None)
        if not label:
            continue
        gloss = next(
            (str(o) for o in graph.objects(subject, SKOS.definition)
             if getattr(o, "language", None) == "en"),
            None,
        )
        targets.append(
            Target(
                iri=str(subject),
                label=label,
                gloss=gloss,
                alt_labels=[str(o) for o in graph.objects(subject, SKOS.altLabel)],
                has_key=[str(o) for o in graph.objects(subject, OWL.hasKey)],
                match_against=match_against,
            )
        )
    return sorted(targets, key=lambda target: target.iri)


def mentions_from(rows: list[dict]) -> list[Mention]:
    return [
        Mention(
            id=row["id"],
            text=row["surface_text"],
            document_id=row["document_id"],
            language=row["language"] or "en",
        )
        for row in rows
    ]


def persist_typings(conn: sqlite3.Connection, version_id: str, typings) -> OrphanSplit:
    install(conn)
    split = OrphanSplit()
    rows = []
    for typing in typings:
        rows.append(
            (typing.mention_id, version_id, typing.iri, typing.score, typing.zone,
             typing.runner_up)
        )
        if typing.iri is None:
            split.orphan += 1
        elif typing.zone == GREY:
            split.grey += 1
        else:
            split.typed += 1

    conn.execute("DELETE FROM mention_typing WHERE version_id = ?", (version_id,))
    conn.executemany(
        "INSERT INTO mention_typing (mention_id, version_id, iri, score, zone, runner_up) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return split


def entities_from(decisions: list[Decision]) -> dict[str, str]:
    """Union-find over the merge decisions. A mention in no merge keeps its own identity —
    separate individuals until confirmed (D10)."""
    parent: dict[str, str] = {}

    def find(item: str) -> str:
        parent.setdefault(item, item)
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for decision in decisions:
        if decision.action != "merge":
            continue
        left, right = find(decision.left), find(decision.right)
        if left != right:
            parent[min(left, right)] = max(left, right)
            parent[max(left, right)] = min(left, right)

    return {item: find(item) for item in parent}


def persist_entities(
    conn: sqlite3.Connection, entities: dict[str, str], unresolved: set[str]
) -> None:
    conn.executemany(
        "UPDATE mentions SET candidate_entity = ? WHERE id = ?",
        [(entity, mention_id) for mention_id, entity in entities.items()],
    )
    # The conservative policy leaves duplicates behind, and a duplicate whose two halves each
    # carry one value looks like confirmation of functionality (6.8). The state is what keeps
    # them out of that count.
    conn.executemany(
        "UPDATE mentions SET status = ? WHERE id = ?",
        [(POSSIBLE_DUPLICATE, mention_id) for mention_id in sorted(unresolved)],
    )
    conn.commit()
