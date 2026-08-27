"""Wiring for ITER-MATCH: build targets from an ontology version, store what the matcher decided.

Typing is kept out of the `mentions` table on purpose. The mention layer is immutable except
by extension (LAYERS); a typing is derived from one ontology version and is recomputed whenever
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
from datetime import datetime, timezone

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, SKOS

from .matching import AUTO, DISCARDED, GREY, Decision, Mention, Target

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

-- Grey-zone answers. Kept apart from `mention_typing` because that table is rewritten every
-- time the matcher runs, and an answer that a re-match erased would be an answer the user gave
-- twice. They are also, unglamorously, the accept/reject labels ITER-TUNE wants for tuning the
-- re-ranker: the only source of them this design ever has.
CREATE TABLE IF NOT EXISTS grey_decisions (
  mention_id  TEXT PRIMARY KEY,
  iri         TEXT,             -- NULL means "none of these": the mention is an orphan
  offered     TEXT,             -- what the matcher had proposed, for the training set
  score       REAL,
  why         TEXT,
  created_at  TEXT
);
"""

ACCEPTED = "accepted"
REJECTED = "rejected"
RETYPED = "retyped"


@dataclass
class OrphanSplit:
    """The aggregate orphan rate says nothing (ITER-BRIDGE); only the split is informative.

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
    """Store one run's typings, with the grey-zone answers already applied.

    Applied here rather than left to a consumer: a decision the user made has to survive the
    next `match`, and the alternative is asking the same question every run — which is how a
    system trains someone to stop answering.
    """
    install(conn)
    answers = decisions(conn)
    split = OrphanSplit()
    rows = []
    for typing in typings:
        iri, score, zone = typing.iri, typing.score, typing.zone
        if zone == GREY and typing.mention_id in answers:
            iri = answers[typing.mention_id]
            zone = AUTO if iri else DISCARDED
        rows.append((typing.mention_id, version_id, iri, score, zone, typing.runner_up))
        if iri is None:
            split.orphan += 1
        elif zone == GREY:
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


def answer(
    conn: sqlite3.Connection, mention_id: str, iri: str | None, *,
    offered: str | None = None, score: float | None = None, why: str = "",
) -> None:
    """Record one grey-zone answer. `iri=None` is "none of these", which is a real answer."""
    install(conn)
    conn.execute(
        "INSERT OR REPLACE INTO grey_decisions (mention_id, iri, offered, score, why, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (mention_id, iri, offered, score, why,
         datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    conn.commit()


def decisions(conn: sqlite3.Connection) -> dict[str, str | None]:
    install(conn)
    return {
        row["mention_id"]: row["iri"]
        for row in conn.execute("SELECT mention_id, iri FROM grey_decisions")
    }


def labels(conn: sqlite3.Connection) -> list[dict]:
    """The accept/reject labels, as the re-ranker would need them (ITER-TUNE).

    A row per answer: the mention's text, the class that was offered, its score, and whether
    the user took it. This is the only place such labels ever come from — nobody annotates them
    on purpose, they are a by-product of someone doing their work.
    """
    install(conn)
    return [
        dict(row) | {"accepted": bool(row["iri"] and row["iri"] == row["offered"])}
        for row in conn.execute(
            "SELECT g.mention_id, m.surface_text, m.document_id, g.offered, g.iri, g.score, "
            "g.why FROM grey_decisions g JOIN mentions m ON m.id = g.mention_id "
            "ORDER BY g.created_at"
        )
    ]


def pending(conn: sqlite3.Connection, version_id: str, limit: int = 0) -> list[dict]:
    """Grey-zone typings nobody has answered yet, best score first."""
    install(conn)
    query = (
        "SELECT t.mention_id, t.iri, t.score, t.runner_up, m.surface_text, m.document_id, "
        "m.page FROM mention_typing t JOIN mentions m ON m.id = t.mention_id "
        "LEFT JOIN grey_decisions g ON g.mention_id = t.mention_id "
        "WHERE t.version_id = ? AND t.zone = ? AND g.mention_id IS NULL "
        "ORDER BY t.score DESC"
    )
    if limit:
        query += f" LIMIT {int(limit)}"
    return [dict(row) for row in conn.execute(query, (version_id, GREY))]


def entities_from(decisions: list[Decision]) -> dict[str, str]:
    """Union-find over the merge decisions. A mention in no merge keeps its own identity —
    separate individuals until confirmed (SEPARATE-UNTIL-CONFIRMED)."""
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
