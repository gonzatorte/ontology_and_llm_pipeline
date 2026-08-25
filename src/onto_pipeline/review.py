"""Findings that are waiting for a human decision (spec 4.3, 6.7).

These used to be a JSON file, which meant nothing recorded whether a decision had been made:
re-running A0 rewrote the file and any judgement already formed was gone. They are state, so
they live in the store with the rest of it.

Two properties this has to have, and both come from the spec's treatment of rejection (6.7):

A finding has a stable identity. The same typo on the same entity is the same finding across
runs, so its id is derived from its content and a re-run cannot duplicate it or reopen it.
What was rejected stays rejected — "lo rechazado vale más que lo aceptado" is the reason the
history is worth keeping at all.

And a finding is relative to a state. When the seed changes and the finding stops being raised,
it is not still open: it is superseded. Leaving it open would grow a backlog of questions about
an ontology that no longer exists.

This is the table an interactive review session reads. Nothing here applies a correction — the
decision is recorded, acting on it is a separate step.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DIVERGENT_LABEL = "divergent_label"
PENDING_SEMANTIC_CHECK = "pending_semantic_check"
TYPO = "typo"

OPEN = "open"
ACCEPTED = "accepted"
REJECTED = "rejected"
SUPERSEDED = "superseded"

RESOLVED = (ACCEPTED, REJECTED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_items (
  id           TEXT PRIMARY KEY,   -- derived from the content, so a re-run is idempotent
  kind         TEXT NOT NULL,      -- divergent_label | pending_semantic_check | typo
  version_id   TEXT,               -- the ontology version it was raised against
  subject_iri  TEXT,
  summary      TEXT NOT NULL,      -- one line, for a list
  payload      TEXT NOT NULL,      -- JSON with the specifics
  status       TEXT NOT NULL,      -- open | accepted | rejected | superseded
  comment      TEXT,
  created_at   TEXT,
  resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(status, kind);
"""


@dataclass
class Finding:
    kind: str
    subject_iri: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        material = json.dumps(
            {"kind": self.kind, "subject": self.subject_iri, "payload": self.payload},
            sort_keys=True, ensure_ascii=False, default=str,
        )
        return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class SyncReport:
    added: int = 0
    already_known: int = 0
    superseded: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def sync(
    conn: sqlite3.Connection, findings: list[Finding], *, version_id: str, kinds: list[str]
) -> SyncReport:
    """Record this run's findings, leaving decisions already made untouched.

    `kinds` bounds what may be superseded, so syncing one kind of finding cannot retire
    another kind that simply was not part of this run.
    """
    install(conn)
    report = SyncReport()
    seen = {finding.id for finding in findings}

    for finding in findings:
        existing = conn.execute(
            "SELECT status FROM review_items WHERE id = ?", (finding.id,)
        ).fetchone()
        if existing is not None:
            report.already_known += 1
            # A finding that was superseded and is raised again becomes open; a decision the
            # user already made is left alone.
            if existing["status"] == SUPERSEDED:
                conn.execute(
                    "UPDATE review_items SET status = ?, version_id = ? WHERE id = ?",
                    (OPEN, version_id, finding.id),
                )
            continue
        conn.execute(
            "INSERT INTO review_items (id, kind, version_id, subject_iri, summary, payload, "
            "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (finding.id, finding.kind, version_id, finding.subject_iri, finding.summary,
             json.dumps(finding.payload, ensure_ascii=False), OPEN, _now()),
        )
        report.added += 1

    placeholders = ",".join("?" for _ in kinds)
    stale = [
        row["id"]
        for row in conn.execute(
            f"SELECT id FROM review_items WHERE status = ? AND kind IN ({placeholders})",  # noqa: S608
            (OPEN, *kinds),
        )
        if row["id"] not in seen
    ]
    conn.executemany(
        "UPDATE review_items SET status = ?, resolved_at = ? WHERE id = ?",
        [(SUPERSEDED, _now(), item_id) for item_id in stale],
    )
    report.superseded = len(stale)
    conn.commit()
    return report


def resolve(
    conn: sqlite3.Connection, item_id: str, status: str, comment: str = ""
) -> bool:
    if status not in RESOLVED:
        raise ValueError(f"a review item is accepted or rejected, not {status!r}")
    install(conn)
    cursor = conn.execute(
        "UPDATE review_items SET status = ?, comment = ?, resolved_at = ? WHERE id = ?",
        (status, comment or None, _now(), item_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def load(
    conn: sqlite3.Connection, *, status: str | None = OPEN, kind: str | None = None
) -> list[dict]:
    install(conn)
    query = "SELECT * FROM review_items WHERE 1=1"
    params: list[Any] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    return [
        dict(row) | {"payload": json.loads(row["payload"])}
        for row in conn.execute(query + " ORDER BY kind, subject_iri", params)
    ]


def counts(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    install(conn)
    return {
        (row["kind"], row["status"]): row["n"]
        for row in conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM review_items GROUP BY kind, status"
        )
    }


def findings_from_seed(seed) -> list[Finding]:
    """A0's divergences and typos as reviewable items."""
    findings = []
    for entity in seed.entities:
        if entity.divergence_reason is None:
            continue
        kind = (
            DIVERGENT_LABEL if entity.divergence_reason == "same_language_mismatch"
            else PENDING_SEMANTIC_CHECK
        )
        labels = [
            {"text": label.text, "language": label.language, "source": label.source}
            for label in entity.labels
        ]
        findings.append(
            Finding(
                kind=kind,
                subject_iri=entity.iri,
                summary=" | ".join(f"{label['text']} ({label['language']})" for label in labels),
                payload={"original_iri": entity.original_iri,
                         "reason": entity.divergence_reason, "labels": labels},
            )
        )

    for typo in seed.typos:
        findings.append(
            Finding(
                kind=TYPO,
                subject_iri=typo.entity_iri,
                summary=f"{typo.token} -> {typo.suggestion or '(no suggestion)'}",
                payload={"detector": typo.detector, "label": typo.label, "token": typo.token,
                         "suggestion": typo.suggestion, "evidence": typo.evidence},
            )
        )
    return findings
