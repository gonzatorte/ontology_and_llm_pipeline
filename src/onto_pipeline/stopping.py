"""Stopping criteria (EVAL-STOPPING, STOPPING-CRITERIA).

Two different terminations, and conflating them is the mistake this module exists to prevent:

    iteration   is this round exhausted?
    process     is the ontology ready?

Being incremental, the second one is never "finished". It is "enough until new documents
arrive", and it is reported as such.

Four criteria, in the roles the spec gives them:

    competency questions   PRIMARY     the only one that says *what* is missing
    novelty saturation     secondary   automatic, and about the corpus
    accumulation curve     diagnostic  the only one that says where the problem is
    budget                 HARD        arbitrary, and the only one that always terminates

**Mention coverage is deliberately not among them.** It is a diagnostic and never a target: a
system optimizes what is measured, and an umbrella class maximizes coverage while destroying
exactly the conceptual value the ontology is for.

**The accumulation curve is the one that breaks the circle.** The other three all look at the
corpus, so they can agree and be wrong in the same direction — a limit the spec states outright
and that cannot be removed from inside the system. A curve still climbing steeply at document
100 says the corpus is insufficient and no number of iterations will fix it; one that flattened
at document 40 says the last sixty added little. That is the only distinction available here
between a problem in the pipeline and a problem in the data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .store import Store

PRIMARY = "primary"
SECONDARY = "secondary"
DIAGNOSTIC = "diagnostic"
HARD = "hard"

MET = "met"
NOT_MET = "not met"
UNKNOWN = "unknown"


@dataclass
class Criterion:
    name: str
    role: str
    state: str = UNKNOWN
    value: str = ""
    note: str = ""

    @property
    def stops(self) -> bool:
        """Diagnostics never stop anything, however they read."""
        return self.state == MET and self.role != DIAGNOSTIC


@dataclass
class Point:
    index: int
    document_id: str
    new: int
    cumulative: int


@dataclass
class Assessment:
    criteria: list[Criterion] = field(default_factory=list)
    curve: list[Point] = field(default_factory=list)

    @property
    def stop(self) -> bool:
        return any(criterion.stops for criterion in self.criteria)

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name} ({c.role})" for c in self.criteria if c.stops]


# ─────────────────────────  the measurements  ─────────────────────────


def install(conn: Store) -> None:
    """Create the tables this reads if the store predates them.

    It owns none of them — it only measures. Without this, a store where the CQs were never
    evaluated raises instead of reporting that the primary criterion has nothing to say, and
    "the instrument is missing" would arrive looking like "the process is finished".
    """
    from . import cq, induction, typing_store

    cq.install(conn)
    induction.install(conn)
    typing_store.install(conn)


def pass_rate_history(conn: Store, *, session_id: str) -> list[float]:
    """Fraction of competency questions answered, one entry per iteration, oldest first."""
    install(conn)
    rows = conn.execute(
        "SELECT iteration, AVG(passed) AS rate FROM cq_results WHERE session_id = ? "
        "GROUP BY iteration ORDER BY iteration", (session_id,),
    ).fetchall()
    return [float(row["rate"]) for row in rows]


def concepts_by_document(
    conn: Store, version_id: str, *, session_id: str
) -> dict[str, set[str]]:
    """What concepts each document turned out to be about.

    A concept is a class one of its mentions was typed to, or an induced proposal one of its
    mentions supports. Both halves are needed: counting only typed classes would call a
    document that introduced three genuinely new concepts empty, which is the opposite of what
    the curve is for.
    """
    install(conn)
    found: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT m.document_id AS document_id, t.iri AS concept FROM mention_typing t "
        "JOIN mentions m ON m.id = t.mention_id AND m.session_id = ? "
        "WHERE t.version_id = ? AND t.iri IS NOT NULL",
        (session_id, version_id),
    ):
        found.setdefault(row["document_id"], set()).add(row["concept"])

    for row in conn.execute(
        "SELECT m.document_id AS document_id, p.proposed_id AS concept "
        "FROM proposed_class_mentions p "
        "JOIN mentions m ON m.id = p.mention_id AND m.session_id = ? "
        "JOIN proposed_classes c ON c.id = p.proposed_id "
        "WHERE c.version_id = ?",
        (session_id, version_id),
    ):
        found.setdefault(row["document_id"], set()).add("induced:" + row["concept"])
    return found


def processing_order(conn: Store, *, session_id: str) -> list[str]:
    """Documents in the order they entered the process, which is insertion order.

    The curve is a function of that order and of nothing else, so it has to be the real one:
    sorting by id would draw a curve for a process that never happened.
    """
    return [
        row["id"] for row in conn.execute(
            "SELECT id FROM documents WHERE session_id = ? AND held_out = 0 ORDER BY rowid",
            (session_id,),
        )
    ]


def accumulation(order: Sequence[str], concepts: dict[str, set[str]]) -> list[Point]:
    """Unique concepts seen so far, one point per document."""
    seen: set[str] = set()
    curve = []
    for index, document_id in enumerate(order, start=1):
        before = len(seen)
        seen |= concepts.get(document_id, set())
        curve.append(Point(index, document_id, len(seen) - before, len(seen)))
    return curve


def tail_slope(curve: Sequence[Point], window: int) -> float | None:
    """New concepts per document over the last `window` documents.

    None when there are not enough documents to have a tail. Reported as unknown rather than
    as zero: a flat curve and no curve at all look identical in a number and mean opposite
    things.
    """
    tail = curve[-window:]
    if len(tail) < window or not window:
        return None
    return sum(point.new for point in tail) / len(tail)


# ─────────────────────────  putting it together  ─────────────────────────


def assess(
    conn: Store,
    version_id: str,
    *,
    session_id: str,
    target_pass_rate: float,
    novelty_window: int,
    novelty_threshold: float,
    iteration: int,
    max_iterations: int,
) -> Assessment:
    from .cq import should_stop

    history = pass_rate_history(conn, session_id=session_id)
    curve = accumulation(
        processing_order(conn, session_id=session_id),
        concepts_by_document(conn, version_id, session_id=session_id),
    )
    slope = tail_slope(curve, novelty_window)

    criteria = [_competency(history, target_pass_rate, should_stop)]
    criteria.append(_novelty(slope, novelty_window, novelty_threshold))
    criteria.append(_curve(curve, slope, novelty_window))
    criteria.append(_budget(iteration, max_iterations))
    return Assessment(criteria=criteria, curve=curve)


def _competency(history: list[float], target: float, should_stop) -> Criterion:
    if not history:
        return Criterion(
            "competency questions", PRIMARY, UNKNOWN, "—",
            "no CQ has been evaluated; the primary criterion has nothing to say",
        )
    latest = f"{history[-1]:.0%} of CQs answered, target {target:.0%}"
    if should_stop(history, target):
        return Criterion("competency questions", PRIMARY, MET, latest,
                         "at or above target and not rising for two iterations")
    note = (
        "reaching the target once is not enough; it has to have settled"
        if history[-1] >= target
        else "below target — and the failing ones say what is missing, which no other "
             "criterion does"
    )
    return Criterion("competency questions", PRIMARY, NOT_MET, latest, note)


def _novelty(slope: float | None, window: int, threshold: float) -> Criterion:
    if slope is None:
        return Criterion("novelty saturation", SECONDARY, UNKNOWN, "—",
                         f"fewer than {window} documents processed")
    value = f"{slope:.2f} new concepts/document over the last {window}"
    if slope < threshold:
        return Criterion("novelty saturation", SECONDARY, MET, value,
                         "saturated with respect to the corpus, which is not the same as "
                         "with respect to the domain")
    return Criterion("novelty saturation", SECONDARY, NOT_MET, value,
                     f"still above {threshold:.2f}")


def _curve(curve: Sequence[Point], slope: float | None, window: int) -> Criterion:
    if not curve:
        return Criterion("accumulation curve", DIAGNOSTIC, UNKNOWN, "—", "no documents")
    value = f"{curve[-1].cumulative} concepts over {len(curve)} documents"
    # A shape needs two ends. With fewer than two windows the tail *is* the beginning, and
    # comparing them compares a number with itself — which reads as a flat curve and is not
    # one. Reported as unknown rather than as either answer.
    if slope is None or len(curve) < 2 * window:
        return Criterion("accumulation curve", DIAGNOSTIC, UNKNOWN, value,
                         f"fewer than {2 * window} documents; too few to see a shape")
    early = curve[:window]
    early_slope = sum(point.new for point in early) / len(early)
    if early_slope and slope > early_slope * 0.5:
        return Criterion(
            "accumulation curve", DIAGNOSTIC, NOT_MET, value,
            f"still climbing ({slope:.2f} vs {early_slope:.2f} early): the corpus is "
            "insufficient, and no number of iterations fixes that",
        )
    return Criterion(
        "accumulation curve", DIAGNOSTIC, MET, value,
        f"flattened ({slope:.2f} vs {early_slope:.2f} early): the later documents added "
        "little, so the limit is the corpus and not the pipeline",
    )


def _budget(iteration: int, max_iterations: int) -> Criterion:
    value = f"iteration {iteration} of {max_iterations}"
    if iteration >= max_iterations:
        return Criterion("budget", HARD, MET, value,
                         "arbitrary, and the only criterion that always terminates")
    return Criterion("budget", HARD, NOT_MET, value, "")
