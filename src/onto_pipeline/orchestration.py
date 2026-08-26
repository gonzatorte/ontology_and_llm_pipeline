"""What to run next, and what is waiting on a person (spec 5, 6, 10.3).

The pipeline is a sequence of stages with five points where the *user* decides — the matcher's
grey zone (6.2), the branch (6.6), a functional property (6.8), a competency question (4.4), a
typo in the seed (4.3). An orchestrator that ran straight through them would be deciding those
by default, which is the failure the spec names in D5 and D21 from both ends: never ask, and the
system quietly picks the modelling; ask about everything, and it becomes the manual work it
exists to replace.

So this reads the store and answers one question — what is the next thing to do — with three
possible shapes of answer:

    READY     a stage can run now, and here is the command
    WAITING   a decision is open; nothing downstream is worth running until it is made
    DONE      it has already run against this version

The order is the spec's own. What makes the answer useful rather than a checklist is that each
step knows what it *needs*: a step whose input does not exist yet is not "pending", it is
blocked, and saying which is the difference between advice and a list.

This never runs anything. It has no `--run`, on purpose for now: the stage commands live inside
their Typer wrappers and calling them programmatically would pass option objects instead of
values. Extracting them is recorded as debt rather than worked around here, because a
half-working runner that skips a decision point is worse than none.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field

READY = "ready"
WAITING = "waiting on you"
BLOCKED = "blocked"
DONE = "done"


@dataclass
class Step:
    name: str
    command: str
    state: str
    detail: str = ""
    decision: bool = False       # this one is a person's, not the pipeline's


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)

    @property
    def next(self) -> Step | None:
        """A decision first, then the earliest stage that can run.

        A decision outranks a runnable stage because the stages after it would be built on an
        answer nobody gave. A blocked stage is never the answer: it is not something to do, it
        is something downstream of something else.
        """
        waiting = next((step for step in self.steps if step.state == WAITING), None)
        return waiting or next(
            (step for step in self.steps if step.state == READY), None
        )


def _count(conn: sqlite3.Connection, query: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(query, params).fetchone()
    except sqlite3.OperationalError:
        return 0        # the table belongs to a stage that has never run
    return int(row[0]) if row else 0


def survey(conn: sqlite3.Connection, version_id: str, *, has_provider: bool) -> Plan:
    """The state of every stage against one ontology version."""
    documents = _count(conn, "SELECT COUNT(*) FROM documents WHERE held_out = 0")
    blocks = _count(conn, "SELECT COUNT(*) FROM blocks")
    mentions = _count(conn, "SELECT COUNT(*) FROM mentions")
    grouped = _count(conn, "SELECT COUNT(*) FROM mentions WHERE coref_group IS NOT NULL")
    typed = _count(
        conn, "SELECT COUNT(*) FROM mention_typing WHERE version_id = ?", (version_id,)
    )
    grey = _count(
        conn, "SELECT COUNT(*) FROM mention_typing WHERE version_id = ? AND zone = 'grey'",
        (version_id,),
    )
    orphans = _count(
        conn, "SELECT COUNT(*) FROM mention_typing WHERE version_id = ? AND iri IS NULL",
        (version_id,),
    )
    bridged = _count(conn, "SELECT COUNT(*) FROM bridges WHERE version_id = ?", (version_id,))
    proposals = _count(
        conn, "SELECT COUNT(*) FROM proposed_classes WHERE version_id = ?", (version_id,)
    )
    axioms = _count(
        conn, "SELECT COUNT(*) FROM proposed_axioms WHERE version_id = ?", (version_id,)
    )
    open_branches = _count(
        conn, "SELECT COUNT(*) FROM branches WHERE version_id = ? AND status = 'proposed'",
        (version_id,),
    )
    open_reviews = _count(conn, "SELECT COUNT(*) FROM review_items WHERE status = 'open'")
    questions = _count(conn, "SELECT COUNT(*) FROM competency_questions WHERE status='accepted'")

    def stage(
        name: str, command: str, done: bool, ready: bool, detail: str,
        blocked_by: str = "", decision: bool = False,
    ) -> Step:
        if done:
            return Step(name, command, DONE, detail, decision)
        if not ready:
            return Step(name, command, BLOCKED, blocked_by or detail, decision)
        return Step(name, command, WAITING if decision else READY, detail, decision)

    llm_note = "" if has_provider else " (needs a provider: --env-file)"
    steps = [
        stage("ingest", "onto-pipeline ingest", bool(blocks), True,
              f"{documents} documents, {blocks} blocks",
              "no PDF found under corpus_root"),
        stage("extract", f"onto-pipeline extract{llm_note}", bool(mentions), bool(blocks),
              f"{mentions} mentions", "nothing parsed yet: run ingest"),
        stage("coref", f"onto-pipeline coref{llm_note}", bool(grouped), bool(mentions),
              f"{grouped} of {mentions} mentions grouped", "no mentions: run extract"),
        stage("match", "onto-pipeline match", bool(typed), bool(mentions),
              f"{typed} mentions typed against {version_id}", "no mentions: run extract"),
        stage("grey zone", "onto-pipeline grey list", not grey, bool(typed),
              f"{grey} mentions are in the grey zone and nothing types them until they are "
              "answered", "nothing typed yet: run match", decision=bool(grey)),
        stage("bridge", f"onto-pipeline bridge{llm_note}", bool(bridged), bool(orphans),
              f"{bridged} bridges over {orphans} orphans",
              "no orphans to bridge: run match"),
        stage("induce", f"onto-pipeline induce{llm_note}", bool(proposals), bool(bridged),
              f"{proposals} proposed classes",
              "run bridge first, or every orphan the seed covered becomes a spurious class"),
        stage("axiomatize", f"onto-pipeline axiomatize{llm_note}", bool(axioms), bool(proposals),
              f"{axioms} proposed axioms", "no proposals: run induce"),
        stage("branch", "onto-pipeline branch", False, bool(axioms),
              f"{open_branches} branch(es) proposed and undecided" if open_branches
              else "no decision axis found yet",
              "no axioms: run axiomatize", decision=bool(open_branches)),
        stage("review", "onto-pipeline review", not open_reviews, True,
              f"{open_reviews} open item(s): conflicts, functional properties, seed typos",
              decision=bool(open_reviews)),
        stage("regenerate", "onto-pipeline regenerate", False, bool(typed),
              "the ABox is a function of the mentions and this version; rerun it after any "
              "decision", "nothing typed yet: run match"),
        stage("competency questions", "onto-pipeline cq", False, bool(questions),
              f"{questions} accepted questions", "none imported: run import-cq or propose-cq"),
        stage("stop?", "onto-pipeline stop", False, True, "the four criteria of 10.3"),
    ]
    return Plan(steps=steps)


def blocking(plan: Plan) -> list[Step]:
    return [step for step in plan.steps if step.decision and step.state == WAITING]


def summarize(plan: Plan, say: Callable[[str], None]) -> None:  # pragma: no cover - printing
    for step in plan.steps:
        say(f"{step.state:>14}  {step.name}: {step.detail}")
