"""Branch construction — the exceptional path, not the default (spec 6.6).

What the user described is belief revision with multiple extensions: when a set of candidate
axioms cannot all be kept, the maximal consistent subsets are the coherent alternatives. The
spec's one explicit prohibition governs this whole module:

    **Do not ask the model for three alternatives.** It generates correlated garbage.

So nothing here calls an LLM. Axes come from two places, and only two:

* the **reasoner**, which finds the logical conflicts — a justification for an unsatisfiable
  class is a minimal conflict set, and the ways out of it are its minimal hitting sets
  (Reiter's diagnosis, over sets small enough to enumerate exactly);
* a **fixed catalogue of modelling commitments**, which the reasoner cannot find because they
  are perfectly consistent in logic. Reify or use a direct property; `RedProduct` as a class or
  `hasColor red` as a value; divide by function or by structure. These are *enumerated*, never
  discovered: each entry is a pattern with a mechanical detector over the candidate set.

The second kind is what justifies the mechanism. Two branches that a reasoner would separate
are a defect report; two branches it cannot separate are a decision, and every later axiom
depends on which one was taken.

**Grouping is by axis, not by axiom.** A user who is shown thirty axioms to accept or reject is
doing by hand exactly the work the pipeline exists to avoid (D21), and the choices are not
independent anyway. Axes that share no axiom are presented separately — k independent binary
axes are k questions, not 2^k branches — and only coupled ones are expanded into full branches.

The usual path is that there is no axis at all: everything is compatible, it applies
automatically, and a log entry records that it did. Asking on every iteration without a real
conflict is the failure mode the spec names by name.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .axiomatization import Axiom

KIND_LOGICAL = "logical"
KIND_MODELLING = "modelling"

PROPOSED = "proposed"
CHOSEN = "chosen"
REJECTED = "rejected"

SCHEMA = """
CREATE TABLE IF NOT EXISTS branches (
  id             TEXT PRIMARY KEY,
  version_id     TEXT NOT NULL,    -- the state it branches from
  decision_id    TEXT NOT NULL,    -- branches of one decision are the alternatives to it
  iteration      INTEGER,
  axes           TEXT NOT NULL,    -- JSON: [{axis, option, alternatives}]
  add_axioms     TEXT NOT NULL,    -- JSON: candidate axiom ids kept
  remove_axioms  TEXT NOT NULL,    -- JSON: already-asserted axioms it gives up
  score          TEXT,             -- JSON
  state_hash     TEXT,             -- what applying it would produce; loop detection (6.8)
  cq_delta       TEXT,             -- JSON, filled in once the CQs have been re-run
  status         TEXT NOT NULL,    -- proposed | chosen | rejected
  note           TEXT,
  created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_branches_version ON branches(version_id, decision_id);
"""


# ─────────────────────────────  the objects  ─────────────────────────────


@dataclass(frozen=True)
class Option:
    """One way to settle an axis. `drop` is what taking it gives up."""

    id: str
    label: str
    drop: frozenset[str] = frozenset()          # candidate axiom ids
    remove: frozenset[str] = frozenset()        # already-asserted axioms, rendered
    why: str = ""


@dataclass
class Axis:
    """A decision, not an axiom. `governs` is what a choice here changes."""

    id: str
    kind: str
    question: str
    options: list[Option] = field(default_factory=list)
    governs: frozenset[str] = frozenset()

    @property
    def real(self) -> bool:
        """An axis with one option is not a decision; it is a conclusion."""
        return len(self.options) > 1


@dataclass
class Score:
    """Scoring a branch is not scoring its axioms: a branch of individually sound axioms can
    still be a bad global compromise (spec 6.6).

    The last two need history. `historical_affinity` is undefined until something has been
    accepted or rejected before, and `parsimony` is a ratio whose value means nothing without
    earlier iterations to compare it against — both are poor in the first iterations, which is
    the cold start of section 11, not a defect here.
    """

    coverage: float = 0.0
    reorg_cost: int = 0
    abox_regen_cost: int = 0
    historical_affinity: float | None = None
    parsimony: float | None = None

    @property
    def rank_key(self) -> tuple:
        """Coverage first, then cheapness. Affinity breaks ties only once it exists, so the
        early iterations rank on the three computable components alone."""
        return (
            -self.coverage,
            self.reorg_cost + self.abox_regen_cost,
            -(self.historical_affinity or 0.0),
        )


@dataclass
class Branch:
    """Spec 8.2. Without the state hash and the CQ delta there is no way to justify the choice
    or to notice that it returns to a state already visited."""

    id: str
    decision_id: str
    choices: dict[str, str] = field(default_factory=dict)        # axis id -> option id
    add_axioms: list[str] = field(default_factory=list)          # candidate axiom ids
    remove_axioms: list[str] = field(default_factory=list)
    score: Score = field(default_factory=Score)
    state_hash: str = ""
    cq_delta: dict[str, list[str]] = field(default_factory=dict)
    automatic: bool = False
    note: str = ""


@dataclass
class Decision:
    """One question put to the user. Several axes only when they are coupled — sharing an
    axiom means the choices are not separable and the cross-product is the honest question."""

    id: str
    axes: list[Axis]
    branches: list[Branch] = field(default_factory=list)

    @property
    def coupled(self) -> bool:
        return len(self.axes) > 1


@dataclass
class Plan:
    decisions: list[Decision] = field(default_factory=list)
    pre_existing_conflicts: list[str] = field(default_factory=list)

    @property
    def automatic(self) -> bool:
        """No axis: apply everything and continue. The usual path, deliberately."""
        return not self.decisions


# ─────────────────────────────  logical axes  ─────────────────────────────

_IRI = re.compile(r"<([^>]+)>")

# How an OWL API justification renders the predicates this pipeline writes. Only the logical
# ones: an annotation never makes a class unsatisfiable, so it never appears in a conflict.
_RENDERED = {
    "subClassOf": "SubClassOf",
    "type": "Declaration",
}


def mentions_axiom(rendered: str, axiom: Axiom) -> bool:
    """Whether a justification line is this candidate axiom.

    Justifications arrive as OWL functional syntax strings, because that is what the OWL API
    hands back, so the match is by construction textual: the axiom's IRIs must both appear and
    the keyword must be the right one. It is deliberately strict — a false positive here would
    drop an axiom that was never at fault, which is worse than leaving a conflict ungrouped and
    visible as a pre-existing one.
    """
    keyword = _RENDERED.get(axiom.predicate)
    if keyword is None or keyword not in rendered:
        return False
    iris = set(_IRI.findall(rendered))
    wanted = {axiom.subject_iri} | ({axiom.object_iri} if axiom.object_iri else set())
    return wanted <= iris


def conflict_sets(
    axioms: Sequence[Axiom], justifications: dict[str, list[list[str]]]
) -> tuple[list[frozenset[str]], list[str]]:
    """Each justification, restricted to the axioms this iteration is proposing.

    Returns the conflict sets and, separately, the classes whose justification contains no
    candidate at all. Those are not branchable: the ontology was already broken before this
    iteration, and presenting a choice between candidate axioms would hide that.
    """
    by_id = {axiom.id: axiom for axiom in axioms}
    conflicts: list[frozenset[str]] = []
    pre_existing: list[str] = []
    for class_iri, explanations in sorted(justifications.items()):
        touched = False
        for explanation in explanations:
            implicated = frozenset(
                axiom_id for axiom_id, axiom in by_id.items()
                if any(mentions_axiom(line, axiom) for line in explanation)
            )
            if implicated:
                touched = True
                if implicated not in conflicts:
                    conflicts.append(implicated)
        if not touched:
            pre_existing.append(class_iri)
    return conflicts, pre_existing


def hitting_sets(conflicts: Sequence[frozenset[str]], *, max_size: int = 3) -> list[frozenset[str]]:
    """Minimal sets of axioms whose removal breaks every conflict (Reiter's diagnosis).

    Exhaustive by increasing size rather than a hitting-set tree: a justification of a class
    that one iteration just created holds a handful of axioms, and an exact answer over a
    handful is worth more than an approximation over thousands. `max_size` is the admission
    that this is only true while the sets stay small.
    """
    universe = sorted(set().union(*conflicts)) if conflicts else []
    found: list[frozenset[str]] = []
    for size in range(1, max_size + 1):
        for combination in itertools.combinations(universe, size):
            candidate = frozenset(combination)
            if any(existing <= candidate for existing in found):
                continue  # not minimal
            if all(candidate & conflict for conflict in conflicts):
                found.append(candidate)
        if found:
            break   # the smallest diagnoses are the ones worth offering
    return found


def logical_axes(
    conflicts: Sequence[frozenset[str]],
    axioms: Sequence[Axiom],
    labels: dict[str, str] | None = None,
) -> list[Axis]:
    """One axis per connected group of conflicts; its options are the diagnoses.

    Conflicts sharing an axiom cannot be settled independently, so they become one question.
    """
    labels = labels or {}
    axes = []
    for index, group in enumerate(_components(conflicts)):
        diagnoses = hitting_sets(group)
        if len(diagnoses) < 2:
            # One way out is not a branch. The caller applies it and says why in the log.
            continue
        governs = frozenset().union(*group)
        options = [
            Option(
                id=_short("|".join(sorted(diagnosis))),
                label=" + ".join(_name(axiom_id, axioms, labels) for axiom_id in sorted(diagnosis)),
                drop=diagnosis,
                why="giving this up makes the rest satisfiable",
            )
            for diagnosis in diagnoses
        ]
        axes.append(Axis(
            id=f"conflict_{index + 1}",
            kind=KIND_LOGICAL,
            question=(
                f"{len(governs)} proposed axioms cannot all hold. Which one is given up?"
            ),
            options=sorted(options, key=lambda option: option.id),
            governs=governs,
        ))
    return axes


def _components(conflicts: Sequence[frozenset[str]]) -> list[list[frozenset[str]]]:
    """Conflicts that share an axiom belong to the same decision."""
    groups: list[list[frozenset[str]]] = []
    for conflict in conflicts:
        touching = [group for group in groups if any(conflict & other for other in group)]
        merged = [conflict]
        for group in touching:
            merged.extend(group)
            groups.remove(group)
        groups.append(merged)
    return groups


# ────────────────────────  modelling axes (the catalogue)  ────────────────────────


@dataclass
class Situation:
    """What a detector reads: the proposals this iteration wants to add, and where each would
    hang. Nothing about the corpus — a modelling commitment is a property of the model."""

    proposals: dict[str, dict]                    # proposal id -> row (label, criterion, ...)
    parent_of: dict[str, str]                     # proposal id -> parent IRI
    minted: dict[str, str]                        # proposal id -> new class IRI
    axioms: Sequence[Axiom] = ()
    labels: dict[str, str] = field(default_factory=dict)   # IRI -> label


def _axioms_of(situation: Situation, proposal_ids: Iterable[str]) -> frozenset[str]:
    wanted = {situation.minted[pid] for pid in proposal_ids if pid in situation.minted}
    return frozenset(axiom.id for axiom in situation.axioms if axiom.subject_iri in wanted)


def attribute_as_class(situation: Situation, *, min_group: int = 2) -> list[Axis]:
    """`RedProduct` as a class, or `hasColor red` as a value (spec 6.6, the amber node).

    Detected exactly, not guessed: a proposed class whose label is the parent's label with a
    modifier in front — *Startup* Company under Company, *Semi-Structured* Interview under
    Interview. Raised once per parent and only from `min_group` such children, because one
    qualified subclass is a class, while four of them are a decision about whether the
    qualifier is a dimension of the parent or a set of kinds of it.

    Both options are consistent, both are defensible, and every later axiom about those
    concepts depends on which was taken. That is the whole reason this module exists.
    """
    by_parent: dict[str, list[str]] = {}
    for proposal_id, parent in situation.parent_of.items():
        parent_label = situation.labels.get(parent, "")
        label = situation.proposals.get(proposal_id, {}).get("label", "")
        if not parent_label or not label:
            continue
        if _is_qualified(label, parent_label):
            by_parent.setdefault(parent, []).append(proposal_id)

    axes = []
    for parent, children in sorted(by_parent.items()):
        if len(children) < min_group:
            continue
        governed = _axioms_of(situation, children)
        if not governed:
            continue
        parent_label = situation.labels.get(parent, parent)
        qualifiers = sorted(
            _qualifier(situation.proposals[pid]["label"], parent_label) for pid in children
        )
        axes.append(Axis(
            id=f"attribute_as_class:{_short(parent)}",
            kind=KIND_MODELLING,
            question=(
                f"«{parent_label}» gains {len(children)} subclasses that are "
                f"{parent_label} qualified by a modifier ({', '.join(qualifiers)}). "
                f"Is the modifier a set of kinds, or a dimension of {parent_label}?"
            ),
            options=[
                Option(id="as_class", label=f"kinds of {parent_label}",
                       why="each qualifier names a subclass, as proposed"),
                Option(id="as_attribute", label=f"a dimension of {parent_label}",
                       drop=governed,
                       why="the qualifier becomes a value, not a class; no subclass is minted"),
            ],
            governs=governed,
        ))
    return axes


def _is_qualified(label: str, parent_label: str) -> bool:
    lower, parent = label.strip().lower(), parent_label.strip().lower()
    return lower != parent and lower.endswith(f" {parent}")


def _qualifier(label: str, parent_label: str) -> str:
    return label.strip()[: -len(parent_label.strip())].strip()


def division_criterion(
    situation: Situation,
    *,
    similarity: Callable[[str, str], float] | None = None,
    min_separation: float = 0.10,
    min_group: int = 2,
) -> list[Axis]:
    """A parent being divided along two criteria at once (spec 8.2's own example).

    Induction makes every proposal declare what distinguishes it, and the validator already
    rejects one that does not. That turns a modelling commitment nothing can see in the OWL
    into something mechanical: group a parent's new subclasses by their declared criterion, and
    if the criteria fall into two separated groups, the parent is being cut two ways at once.

    Each option is named by a criterion the proposals actually declared, never by an invented
    label like "by function": the point is to show the user the two cuts as they were written.

    `similarity` is injected rather than imported so this module stays free of the embedder —
    and so it can be skipped when it is not installed, which loses the axis but not the run.

    There is no similarity threshold to calibrate: the cut is chosen per parent, because how
    close two criteria read is a property of how someone wrote them, not a constant. What is
    fixed is `min_separation` — how much clearer the split has to be than the noise around it
    before it counts as two cuts rather than one loosely worded one.
    """
    if similarity is None:
        return []
    by_parent: dict[str, list[str]] = {}
    for proposal_id, parent in situation.parent_of.items():
        if situation.proposals.get(proposal_id, {}).get("criterion"):
            by_parent.setdefault(parent, []).append(proposal_id)

    axes = []
    for parent, children in sorted(by_parent.items()):
        if len(children) < 2 * min_group:
            continue
        useful = _group_by_criterion(
            sorted(children), situation.proposals, similarity,
            min_separation=min_separation, min_group=min_group,
        )
        if len(useful) < 2:
            continue
        parent_label = situation.labels.get(parent, parent)
        options = []
        for group in useful:
            others = _axioms_of(situation, [c for c in children if c not in group])
            kept = [situation.proposals[pid]["label"] for pid in group]
            # The shortest criterion of the group names it: the wordier ones say the same
            # thing about one member, and an option has to be readable at a glance.
            options.append(Option(
                id=_short("|".join(group)),
                label=min(
                    (situation.proposals[pid]["criterion"] for pid in group), key=len
                )[:70],
                drop=others,
                why=(
                    f"keeps {', '.join(sorted(kept))}; the other "
                    f"{len(children) - len(group)} wait for another iteration"
                ),
            ))
        axes.append(Axis(
            id=f"division_criterion:{_short(parent)}",
            kind=KIND_MODELLING,
            question=(
                f"«{parent_label}» is being divided along {len(useful)} different criteria at "
                f"once. One criterion per level: which cut is this one?"
            ),
            options=options,
            governs=_axioms_of(situation, children),
        ))
    return axes


def _group_by_criterion(
    children: list[str],
    proposals: dict[str, dict],
    similarity: Callable[[str, str], float],
    *,
    min_separation: float,
    min_group: int,
) -> list[list[str]]:
    """Single-link over the declared criteria, at the cut the criteria themselves suggest.

    A fixed threshold would be one more uncalibrated constant, and a bad one: criteria written
    as full sentences score high against each other whatever they say, while terse ones score
    low even when they name the same cut. So every observed similarity is tried as the cut,
    highest first, and the first one that yields two or more groups is taken.

    The guard is `min_separation`: the cut has to sit that far above the strongest similarity
    it breaks, or the split is an artifact of where the sweep happened to land rather than two
    ways of dividing the parent. Criteria that are all alike, and criteria that are all
    unrelated, both come back as no split — correctly, since neither is a decision.
    """
    scores: dict[tuple[int, int], float] = {}
    for i in range(len(children)):
        for j in range(i + 1, len(children)):
            scores[(i, j)] = similarity(
                proposals[children[i]]["criterion"], proposals[children[j]]["criterion"]
            )

    for cut in sorted(set(scores.values()), reverse=True):
        groups = _single_link(len(children), scores, cut)
        useful = [group for group in groups if len(group) >= min_group]
        if len(useful) < 2 or sum(len(group) for group in useful) != len(children):
            continue
        member_of = {index: number for number, group in enumerate(useful) for index in group}
        across = [
            score for (i, j), score in scores.items() if member_of[i] != member_of[j]
        ]
        if cut - max(across, default=0.0) < min_separation:
            continue
        return [[children[index] for index in group] for group in useful]
    return []


def _single_link(size: int, scores: dict[tuple[int, int], float], cut: float) -> list[list[int]]:
    parent = list(range(size))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for (i, j), score in scores.items():
        if score >= cut:
            a, b = find(i), find(j)
            if a != b:
                parent[a] = b

    grouped: dict[int, list[int]] = {}
    for index in range(size):
        grouped.setdefault(find(index), []).append(index)
    return sorted(grouped.values(), key=lambda group: (-len(group), group[0]))


# The catalogue. Adding an entry means adding a detector, never a prompt: an axis the code
# cannot recognize is not offered, and that is the point of enumerating instead of asking.
CATALOGUE = (attribute_as_class, division_criterion)

# Catalogued and deliberately not detected, so the gap is visible rather than implied:
# reification versus a direct property (an n-ary relation ODP) is a real axis of the same
# family, and no honest detector over labels and criteria distinguishes a nominalized relation
# from a class. It waits for the property extraction the spec puts outside v1.
UNDETECTED = ("reify_vs_direct_property",)


def modelling_axes(situation: Situation, **options) -> list[Axis]:
    """Every detector in the catalogue, each given the options it declares.

    Filtered by signature rather than by an explicit table per detector, so adding an entry to
    the catalogue is adding a function — `co_varnames` alone would also match local variables,
    which is why the slice is by argument count.
    """
    axes: list[Axis] = []
    for detector in CATALOGUE:
        code = detector.__code__
        declared = set(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount])
        axes.extend(detector(situation, **{
            name: value for name, value in options.items() if name in declared
        }))
    return [axis for axis in axes if axis.real]


# ─────────────────────────────  branches  ─────────────────────────────


def couple(axes: Sequence[Axis]) -> list[list[Axis]]:
    """Axes sharing an axiom go together; the rest are separate questions.

    This is the whole defence against the combinatorial explosion the spec names: k
    independent binary axes are k questions, not 2^k branches.
    """
    groups: list[list[Axis]] = []
    for axis in axes:
        touching = [
            group for group in groups
            if any(axis.governs & other.governs for other in group)
        ]
        merged = [axis]
        for group in touching:
            merged.extend(group)
            groups.remove(group)
        groups.append(sorted(merged, key=lambda item: item.id))
    return sorted(groups, key=lambda group: group[0].id)


def plan(
    axes: Sequence[Axis],
    axioms: Sequence[Axiom],
    *,
    max_branches: int = 5,
    separate_independent_axes: bool = True,
) -> Plan:
    """Turn axes into the questions to ask. No axis means no question."""
    real = [axis for axis in axes if axis.real]
    if not real:
        return Plan()
    groups = couple(real) if separate_independent_axes else [sorted(real, key=lambda a: a.id)]
    all_ids = [axiom.id for axiom in axioms]

    decisions = []
    for group in groups:
        decision_id = _short("|".join(axis.id for axis in group))
        branches = []
        for combination in itertools.product(*[axis.options for axis in group]):
            dropped: set[str] = set()
            removed: set[str] = set()
            for option in combination:
                dropped |= set(option.drop)
                removed |= set(option.remove)
            choices = {
                axis.id: option.id for axis, option in zip(group, combination, strict=True)
            }
            branches.append(Branch(
                id="b_" + _short(decision_id + "|" + json.dumps(choices, sort_keys=True))[:8],
                decision_id=decision_id,
                choices=choices,
                add_axioms=[axiom_id for axiom_id in all_ids if axiom_id not in dropped],
                remove_axioms=sorted(removed),
            ))
        decisions.append(Decision(id=decision_id, axes=group, branches=branches[:max_branches]))
    return Plan(decisions=decisions)


def score(
    branch: Branch,
    *,
    support: dict[str, list[str]],
    orphan_total: int,
    entities: int,
    history: dict[tuple[str, str], int] | None = None,
) -> Score:
    """The three computable components, and the two that need history.

    `support` maps a candidate axiom to the mentions behind it; the covered mentions are the
    orphans this branch would type, which is also what the ABox has to regenerate — the same
    set, counted for two different reasons.
    """
    kept = set(branch.add_axioms)
    covered = {
        mention for axiom_id, mentions in support.items() if axiom_id in kept
        for mention in mentions
    }
    affinity = None
    if history:
        seen = [history.get((axis, option), 0) for axis, option in branch.choices.items()]
        decided = [value for value in seen if value]
        affinity = (
            sum(1 for value in decided if value > 0) / len(branch.choices)
            if branch.choices and decided else None
        )
    return Score(
        coverage=len(covered) / orphan_total if orphan_total else 0.0,
        reorg_cost=len(branch.remove_axioms),
        abox_regen_cost=len(covered),
        historical_affinity=affinity,
        parsimony=len(kept) / entities if entities else None,
    )


def rank(branches: Sequence[Branch], *, limit: int) -> list[Branch]:
    """Best first, and never more than the ceiling: past five, a person compares nothing."""
    return sorted(branches, key=lambda branch: (branch.score.rank_key, branch.id))[:limit]


# ─────────────────────────────  persistence  ─────────────────────────────


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(material: str) -> str:
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def _name(axiom_id: str, axioms: Sequence[Axiom], labels: dict[str, str]) -> str:
    for axiom in axioms:
        if axiom.id == axiom_id:
            subject = labels.get(axiom.subject_iri, axiom.subject_iri.rsplit("/", 1)[-1])
            target = labels.get(axiom.object_iri or "", axiom.object_iri or axiom.literal or "")
            return f"{subject} {axiom.predicate} {target}".strip()
    return axiom_id


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def axes_record(decision: Decision, branch: Branch) -> list[dict]:
    """Spec 8.2's `axes`: the option taken and the ones it was taken over. Without the
    alternatives the record cannot justify the choice, which is what it is for."""
    by_id = {axis.id: axis for axis in decision.axes}
    return [
        {
            "axis": axis_id,
            "option": option_id,
            "alternatives": [
                option.id for option in by_id[axis_id].options if option.id != option_id
            ],
        }
        for axis_id, option_id in sorted(branch.choices.items())
    ]


def persist(
    conn: sqlite3.Connection, version_id: str, decisions: Sequence[Decision], *, iteration: int = 0
) -> None:
    install(conn)
    # A decision already settled is not re-opened by proposing again: branch ids are
    # deterministic, so a plain replace would overwrite a chosen branch — and its siblings'
    # rejections — with fresh `proposed` rows, silently erasing the only record of what was
    # turned down (6.7). Re-deciding is a new version's job, not a re-run's.
    settled = {
        row["decision_id"] for row in conn.execute(
            "SELECT DISTINCT decision_id FROM branches WHERE version_id = ? AND status != ?",
            (version_id, PROPOSED),
        )
    }
    conn.execute("DELETE FROM branches WHERE version_id = ? AND status = ?",
                 (version_id, PROPOSED))
    rows = []
    for decision in decisions:
        if decision.id in settled:
            continue
        for branch in decision.branches:
            rows.append((
                branch.id, version_id, decision.id, iteration,
                json.dumps(axes_record(decision, branch), ensure_ascii=False),
                json.dumps(branch.add_axioms), json.dumps(branch.remove_axioms),
                json.dumps(asdict(branch.score)), branch.state_hash,
                json.dumps(branch.cq_delta), PROPOSED, branch.note, _now(),
            ))
    conn.executemany(
        "INSERT OR REPLACE INTO branches (id, version_id, decision_id, iteration, axes, "
        "add_axioms, remove_axioms, score, state_hash, cq_delta, status, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def load(conn: sqlite3.Connection, version_id: str) -> list[dict]:
    install(conn)
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM branches WHERE version_id = ? ORDER BY decision_id, id",
            (version_id,),
        )
    ]


def find(conn: sqlite3.Connection, branch_id: str) -> dict | None:
    install(conn)
    row = conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    return dict(row) if row else None


def settle(conn: sqlite3.Connection, branch_id: str, *, note: str = "") -> dict:
    """Mark one branch chosen and its siblings rejected.

    The rejections are the point. What was accepted is in the ontology already; what was
    rejected exists nowhere else, and it is what a later iteration needs in order not to
    propose the same thing again (spec 6.7).
    """
    install(conn)
    row = conn.execute("SELECT * FROM branches WHERE id = ?", (branch_id,)).fetchone()
    if row is None:
        raise KeyError(f"no branch {branch_id!r}")
    conn.execute(
        "UPDATE branches SET status = ? WHERE version_id = ? AND decision_id = ? AND id != ?",
        (REJECTED, row["version_id"], row["decision_id"], branch_id),
    )
    conn.execute("UPDATE branches SET status = ?, note = ? WHERE id = ?",
                 (CHOSEN, note or row["note"] or "", branch_id))
    conn.commit()
    return dict(row)


def history(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """How often each (axis, option) was chosen before, minus how often it was rejected.

    Feeds `historical_affinity`, and is empty by construction in the first iteration — the
    cold start of section 11, reported as an absent score rather than as a zero.
    """
    install(conn)
    tally: dict[tuple[str, str], int] = {}
    for row in conn.execute("SELECT axes, status FROM branches WHERE status IN (?, ?)",
                            (CHOSEN, REJECTED)):
        delta = 1 if row["status"] == CHOSEN else -1
        for entry in json.loads(row["axes"]):
            key = (entry["axis"], entry["option"])
            tally[key] = tally.get(key, 0) + delta
    return tally
