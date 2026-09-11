"""Competency questions: the store and the evaluation
(PREP-CQ-GENERATED, PREP-CQ-USER, EVAL-STOPPING).

Hard requirement from the spec: every accepted CQ is paired with its SPARQL query. Without
that pairing, evaluating the stopping criterion needs human judgement every iteration and the
automation is gone. A question that cannot be formalized as a query is discarded in
PREP-CQ-GENERATED's
mechanical filter for exactly this reason.

The primary stopping rule is CQ pass rate >= target with no rise over two iterations. What
makes it the primary criterion is not the number: a failing CQ says *what* is missing, which
feeds the next iteration's prompt. No other criterion has that property.

PREP-CQ-GENERATED generation lives in `cq_generation.py`, which needs a model. The store, the
evaluation and
PREP-CQ-USER import are deterministic and are here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rdflib import Graph
from rdflib.plugins.sparql import prepareQuery

from .store import Store

GENERATED = "generated"
USER = "user"

ACCEPTED = "accepted"
DISCARDED = "discarded"
# PREP-CQ-GENERATED's output before anyone has looked at it. The mechanical filter has already run;
# what is
# left is the three-action review the spec calls one-time work, not per-iteration work.
PROPOSED = "proposed"

TYPES = (
    "definitional",
    "relational",
    "restrictive",
    "quantificational",
    "inferential",
    "negative",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS competency_questions (
  id           TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  question     TEXT NOT NULL,
  language     TEXT,
  cq_type      TEXT,
  origin       TEXT NOT NULL,   -- generated (PREP-CQ-GENERATED) | user (PREP-CQ-USER)
  sparql       TEXT NOT NULL,
  status       TEXT NOT NULL,   -- proposed (PREP-CQ-GENERATED, unreviewed) | accepted | discarded
  citation     TEXT,            -- JSON {document_id, page, quote}; PREP-CQ-GENERATED requires one
  created_at   TEXT,
  PRIMARY KEY (session_id, id)
);

CREATE TABLE IF NOT EXISTS cq_results (
  cq_id        TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  iteration    INTEGER NOT NULL,
  passed       INTEGER NOT NULL,
  n_rows       INTEGER,
  error        TEXT,
  created_at   TEXT,
  PRIMARY KEY (session_id, cq_id, iteration)
);
"""


class MalformedQuery(ValueError):
    """A CQ whose SPARQL does not parse. It is not a competency question yet."""


@dataclass
class CompetencyQuestion:
    id: str
    question: str
    sparql: str
    origin: str = USER
    language: str = "en"
    cq_type: str = "definitional"
    status: str = ACCEPTED
    citation: dict[str, Any] | None = None


@dataclass
class Evaluation:
    iteration: int
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    errored: dict[str, str] = field(default_factory=dict)
    n_rows: dict[str, int] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        total = len(self.passed) + len(self.failed) + len(self.errored)
        return len(self.passed) / total if total else 0.0

    def delta(self, previous: Evaluation | None) -> dict[str, list[str]]:
        """`cq_delta` of a branch (SCHEMAS-BRANCH): which questions this change won and lost."""
        if previous is None:
            return {"newly_passing": sorted(self.passed), "newly_failing": []}
        before = set(previous.passed)
        return {
            "newly_passing": sorted(set(self.passed) - before),
            "newly_failing": sorted(before - set(self.passed)),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: Store) -> None:
    conn.script(SCHEMA)
    conn.commit()


def validate(question: CompetencyQuestion) -> None:
    if question.cq_type not in TYPES:
        raise ValueError(f"unknown CQ type {question.cq_type!r}")
    try:
        prepareQuery(question.sparql)
    except Exception as exc:  # noqa: BLE001 - rdflib raises several parse error types
        raise MalformedQuery(f"{question.id}: {exc}") from exc
    if question.origin == GENERATED and not question.citation:
        # PREP-CQ-GENERATED's filter: a generated question without a citation and page is discarded.
        raise ValueError(f"{question.id}: a generated CQ needs a citation")


def add(conn: Store, questions: list[CompetencyQuestion], *, session_id: str) -> int:
    install(conn)
    for question in questions:
        validate(question)
    conn.executemany(
        "INSERT INTO competency_questions (id, session_id, question, language, cq_type, "
        "origin, sparql, status, citation, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id, id) DO UPDATE SET question = excluded.question, "
        "sparql = excluded.sparql, "
        "status = excluded.status, cq_type = excluded.cq_type, citation = excluded.citation",
        [
            (
                question.id,
                session_id,
                question.question,
                question.language,
                question.cq_type,
                question.origin,
                question.sparql,
                question.status,
                json.dumps(question.citation) if question.citation else None,
                _now(),
            )
            for question in questions
        ],
    )
    conn.commit()
    return len(questions)


def load(
    conn: Store, *, session_id: str, status: str = ACCEPTED
) -> list[CompetencyQuestion]:
    install(conn)
    return [
        CompetencyQuestion(
            id=row["id"],
            question=row["question"],
            sparql=row["sparql"],
            origin=row["origin"],
            language=row["language"],
            cq_type=row["cq_type"],
            status=row["status"],
            citation=json.loads(row["citation"]) if row["citation"] else None,
        )
        for row in conn.execute(
            "SELECT * FROM competency_questions WHERE session_id = ? AND status = ? ORDER BY id",
        (session_id, status),
        )
    ]


def decide(conn: Store, ids: list[str], status: str, *, session_id: str) -> int:
    """Accept or discard proposed questions. The third action, reformulating, is an edit and
    goes through `import` like any question the user writes."""
    if status not in (ACCEPTED, DISCARDED):
        raise ValueError(f"a question is accepted or discarded, not {status!r}")
    install(conn)
    cursor = conn.executemany(
        "UPDATE competency_questions SET status = ? WHERE session_id = ? AND id = ?",
        [(status, session_id, item) for item in ids],
    )
    conn.commit()
    return cursor.rowcount


def read_file(path: Path) -> list[CompetencyQuestion]:
    """PREP-CQ-USER: the questions the user writes without looking at the generated ones."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        CompetencyQuestion(
            id=entry["id"],
            question=entry["question"],
            sparql=entry["sparql"],
            origin=entry.get("origin", USER),
            language=entry.get("language", "en"),
            cq_type=entry.get("type", entry.get("cq_type", "definitional")),
            status=entry.get("status", ACCEPTED),
            citation=entry.get("citation"),
        )
        for entry in raw
    ]


def evaluate(
    graph: Graph, questions: list[CompetencyQuestion], iteration: int = 0
) -> Evaluation:
    """A question is answered when its query returns at least one row against the ontology."""
    evaluation = Evaluation(iteration=iteration)
    for question in questions:
        try:
            rows = list(graph.query(question.sparql))
        except Exception as exc:  # noqa: BLE001 - a broken query is a result, not a crash
            evaluation.errored[question.id] = f"{type(exc).__name__}: {exc}"
            continue
        evaluation.n_rows[question.id] = len(rows)
        (evaluation.passed if rows else evaluation.failed).append(question.id)
    return evaluation


def record(conn: Store, evaluation: Evaluation, *, session_id: str) -> None:
    install(conn)
    rows = (
        [(cq_id, session_id, evaluation.iteration, 1, evaluation.n_rows.get(cq_id), None, _now())
         for cq_id in evaluation.passed]
        + [(cq_id, session_id, evaluation.iteration, 0, 0, None, _now())
           for cq_id in evaluation.failed]
        + [(cq_id, session_id, evaluation.iteration, 0, None, error, _now())
           for cq_id, error in evaluation.errored.items()]
    )
    conn.executemany(
        "INSERT INTO cq_results (cq_id, session_id, iteration, passed, n_rows, error, "
        "created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, cq_id, iteration) DO UPDATE SET "
        "passed = excluded.passed, n_rows = excluded.n_rows, error = excluded.error",
        rows,
    )
    conn.commit()


def should_stop(history: list[float], target: float) -> bool:
    """Primary stopping rule (EVAL-STOPPING): at or above target, and not rising for two
    iterations. Reaching the target once is not enough — the rate has to have settled."""
    if len(history) < 3 or history[-1] < target:
        return False
    return history[-1] <= history[-2] <= history[-3]
