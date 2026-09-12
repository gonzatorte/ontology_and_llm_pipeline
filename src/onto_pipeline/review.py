"""Findings that are waiting for a human decision (PREP-NORMALIZE, ITER-FEEDBACK).

These used to be a JSON file, which meant nothing recorded whether a decision had been made:
re-running PREP-NORMALIZE rewrote the file and any judgement already formed was gone. They are
state, so
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .store import Store

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
  id           TEXT NOT NULL,      -- derived from the content, so a re-run is idempotent
  session_id   TEXT NOT NULL,
  kind         TEXT NOT NULL,      -- divergent_label | pending_semantic_check | typo
  version_id   TEXT,               -- the ontology version it was raised against
  subject_iri  TEXT,
  summary      TEXT NOT NULL,      -- one line, for a list
  payload      TEXT NOT NULL,      -- JSON with the specifics
  status       TEXT NOT NULL,      -- open | accepted | rejected | superseded
  comment      TEXT,
  created_at   TEXT,
  resolved_at  TEXT,
  PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_items(session_id, status, kind);
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


def install(conn: Store) -> None:
    rescued = _rescue_from_before_sessions(conn)
    conn.script(SCHEMA)
    if rescued:
        conn.executemany(
            "INSERT INTO review_items (id, session_id, kind, version_id, subject_iri, summary, "
            "payload, status, comment, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rescued,
        )
    conn.commit()


def _rescue_from_before_sessions(conn: Store) -> list[tuple]:
    """Carry a pre-session table's decisions over instead of dropping them.

    A store older than user sessions has this table without `session_id` and keyed on the id
    alone. The session is recoverable: `version_id` is `<session>:v<N>`. The table is rebuilt
    rather than altered because the primary key is what makes a decision belong to one session,
    and no `ALTER` changes a key — leaving the old one would keep two sessions sharing the
    decision only one of them took.
    """
    if not conn.table_exists("review_items") or "session_id" in conn.columns("review_items"):
        return []
    rows = [dict(row) for row in conn.execute("SELECT * FROM review_items")]
    conn.execute("DROP TABLE review_items")
    return [
        (row["id"], str(row["version_id"] or "").split(":")[0], row["kind"], row["version_id"],
         row["subject_iri"], row["summary"], row["payload"], row["status"], row["comment"],
         row["created_at"], row["resolved_at"])
        for row in rows
    ]


def sync(
    conn: Store, findings: list[Finding], *, version_id: str, kinds: list[str], session_id: str
) -> SyncReport:
    """Record this run's findings, leaving decisions already made untouched.

    `kinds` bounds what may be superseded, so syncing one kind of finding cannot retire
    another kind that simply was not part of this run.

    The id is content-derived, so two user sessions over the same use case raise findings with
    the same id: without the session in the key, the second one would inherit the first one's
    decisions and retire its open items as superseded.
    """
    install(conn)
    report = SyncReport()
    seen = {finding.id for finding in findings}

    for finding in findings:
        existing = conn.execute(
            "SELECT status FROM review_items WHERE session_id = ? AND id = ?",
            (session_id, finding.id),
        ).fetchone()
        if existing is not None:
            report.already_known += 1
            # A finding that was superseded and is raised again becomes open; a decision the
            # user already made is left alone.
            if existing["status"] == SUPERSEDED:
                conn.execute(
                    "UPDATE review_items SET status = ?, version_id = ? "
                    "WHERE session_id = ? AND id = ?",
                    (OPEN, version_id, session_id, finding.id),
                )
            continue
        conn.execute(
            "INSERT INTO review_items (id, session_id, kind, version_id, subject_iri, summary, "
            "payload, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (finding.id, session_id, finding.kind, version_id, finding.subject_iri,
             finding.summary, json.dumps(finding.payload, ensure_ascii=False), OPEN, _now()),
        )
        report.added += 1

    placeholders = ",".join("?" for _ in kinds)
    stale = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM review_items WHERE session_id = ? AND status = ? "  # noqa: S608
            f"AND kind IN ({placeholders})",
            (session_id, OPEN, *kinds),
        )
        if row["id"] not in seen
    ]
    conn.executemany(
        "UPDATE review_items SET status = ?, resolved_at = ? WHERE session_id = ? AND id = ?",
        [(SUPERSEDED, _now(), session_id, item_id) for item_id in stale],
    )
    report.superseded = len(stale)
    conn.commit()
    return report


def resolve(
    conn: Store, item_id: str, status: str, comment: str = "", *, session_id: str
) -> bool:
    if status not in RESOLVED:
        raise ValueError(f"a review item is accepted or rejected, not {status!r}")
    install(conn)
    cursor = conn.execute(
        "UPDATE review_items SET status = ?, comment = ?, resolved_at = ? "
        "WHERE session_id = ? AND id = ?",
        (status, comment or None, _now(), session_id, item_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def annotate(conn: Store, item_id: str, patch: dict[str, Any], *, session_id: str) -> bool:
    """Sumarle evidencia a un hallazgo sin tocar la decisión.

    `sync` sólo inserta lo que todavía no está, así que no hay por dónde agregarle a un hallazgo
    abierto lo que se averiguó después — las traducciones que dejan ver por qué el par sigue
    marcado, por ejemplo. Se escribe en el payload de la **fila** y nunca en el que arma
    `findings_from_initial`: el id deriva de ese último, y tocarlo cambiaría todos los ids y
    dejaría huérfana cada decisión ya tomada.
    """
    install(conn)
    row = conn.execute(
        "SELECT payload FROM review_items WHERE session_id = ? AND id = ?",
        (session_id, item_id),
    ).fetchone()
    if row is None:
        return False
    payload = json.loads(row["payload"]) | patch
    conn.execute(
        "UPDATE review_items SET payload = ? WHERE session_id = ? AND id = ?",
        (json.dumps(payload, ensure_ascii=False), session_id, item_id),
    )
    conn.commit()
    return True


def load(
    conn: Store, *, status: str | None = OPEN, kind: str | None = None, session_id: str
) -> list[dict]:
    install(conn)
    query = "SELECT * FROM review_items WHERE session_id = ?"
    params: list[Any] = [session_id]
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


def counts(conn: Store, *, session_id: str) -> dict[tuple[str, str], int]:
    install(conn)
    return {
        (row["kind"], row["status"]): row["n"]
        for row in conn.execute(
            "SELECT kind, status, COUNT(*) AS n FROM review_items WHERE session_id = ? "
            "GROUP BY kind, status",
            (session_id,),
        )
    }


def findings_from_initial(seed) -> list[Finding]:
    """PREP-NORMALIZE's divergences and typos as reviewable items."""
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
