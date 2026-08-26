"""B5 filter 4 — OntoClean over the subsumption hierarchy (spec 6.6).

A hard reject, and the only filter in the chain that catches a *badly formed subsumption*
rather than an inconsistent one. `Student ⊑ Person` is fine; `Person ⊑ Student` is perfectly
consistent in OWL and wrong for a reason no reasoner can state — being a student is something
one stops being, and being a person is not.

Four metaproperties decide it (Guarino & Welty):

    rigidity     is an instance necessarily one, for as long as it exists?
    identity     is there a criterion for telling two of them apart?
    unity        is each one a single whole with a definite boundary?
    dependence   must each one exist alongside some other, different thing?

and four constraints on `sub ⊑ super`, each of the same shape — a metaproperty of the parent
forbidding one of the child:

    an anti-rigid parent cannot subsume a rigid child
    a parent that carries no identity criterion cannot subsume a child that carries one
    an anti-unity parent cannot subsume a child that carries unity
    a dependent parent cannot subsume an independent child

**Where the labels come from is the weak point, and the spec says so.** With an upper ontology
they are inherited. Without one the model labels them, which the spec itself calls "feasible,
less reliable, and additional work that partly contradicts D5". So the labelling is a separate,
cached stage with its own answers on record, the model is asked four plain questions rather
than for OntoClean jargon, and an unlabelled class produces no violation instead of a guess:
this filter reports what it could check, never what it assumed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from rdflib import Graph, URIRef
from rdflib.namespace import RDFS

from .llm import Prompt

STAGE = "B5_ontoclean"

# The metaproperty values, in the notation of the literature. `~R` is the anti-rigid case that
# does the most work: it is what makes `Person ⊑ Student` a violation.
RIGID, NON_RIGID, ANTI_RIGID = "+R", "-R", "~R"
CARRIES_IDENTITY, NO_IDENTITY = "+I", "-I"
CARRIES_UNITY, ANTI_UNITY = "+U", "-U"
DEPENDENT, INDEPENDENT = "+D", "-D"

VALUES = {
    "rigidity": (RIGID, NON_RIGID, ANTI_RIGID),
    "identity": (CARRIES_IDENTITY, NO_IDENTITY),
    "unity": (CARRIES_UNITY, ANTI_UNITY),
    "dependence": (DEPENDENT, INDEPENDENT),
}

# (metaproperty, value of the parent, value of the child) that must not occur together.
CONSTRAINTS = (
    ("rigidity", ANTI_RIGID, RIGID,
     "an anti-rigid class cannot subsume a rigid one: instances stop being the parent while "
     "never stopping being the child"),
    ("identity", NO_IDENTITY, CARRIES_IDENTITY,
     "a class that carries no identity criterion cannot subsume one that does: the criterion "
     "would have nothing to be inherited from"),
    ("unity", ANTI_UNITY, CARRIES_UNITY,
     "a class whose instances are not wholes cannot subsume one whose instances are"),
    ("dependence", DEPENDENT, INDEPENDENT,
     "every instance of a dependent class needs something else to exist, so a subclass of it "
     "cannot be independent"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS metaproperties (
  version_id  TEXT NOT NULL,
  iri         TEXT NOT NULL,
  rigidity    TEXT NOT NULL,
  identity    TEXT NOT NULL,
  unity       TEXT NOT NULL,
  dependence  TEXT NOT NULL,
  why         TEXT,
  created_at  TEXT,
  PRIMARY KEY (version_id, iri)
);
"""

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""Answer four questions about one kind of thing. Answer about the thing itself,
not about how an ontology models it.

THE KIND: {label}
What it is: {gloss}
It is described as a kind of: {parents}

1. NECESSARY — Can something that is a {label} stop being a {label} while continuing to exist
   as the same thing?
   - "never": nothing that is one ever stops being one. (a person, a mountain)
   - "sometimes": some do and some do not.
   - "always": being one is a phase or a role — everything that is one could stop.
     (a student, an employee, a guest)

2. IDENTITY — Given two {label}s, is there something that settles whether they are the same
   one or two different ones?
   - "yes": there is such a criterion, even an implicit one. (same person: same body/history)
   - "no": the kind is too generic or too vague for one. (a "thing", an "amount of stuff")

3. WHOLE — Is each {label} one whole with a definite boundary, so you can say what is part of
   it and what is not?
   - "yes": each one is a bounded whole. (a book, an organism)
   - "no": they have no such boundary, or it varies. (water, a crowd, a region of interest)

4. DEPENDENT — Must every {label} exist alongside some OTHER, different thing?
   - "yes": one cannot exist alone. (a student needs a school, a symptom needs a disease)
   - "no": one can exist on its own. (a person, a rock)

Answer with JSON only:
{{"necessary": "never" | "sometimes" | "always",
  "identity": "yes" | "no",
  "whole": "yes" | "no",
  "dependent": "yes" | "no",
  "why": "one sentence about the hardest of the four"}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

# The plain answers, mapped to the notation. The model never sees "+R" or "anti-rigid": asking
# in jargon gets an answer about the jargon.
_NECESSARY = {"never": RIGID, "sometimes": NON_RIGID, "always": ANTI_RIGID}
# (what the model is asked, what it is called here, what each answer means)
_YES_NO = (
    ("identity", "identity", {"yes": CARRIES_IDENTITY, "no": NO_IDENTITY}),
    ("whole", "unity", {"yes": CARRIES_UNITY, "no": ANTI_UNITY}),
    ("dependent", "dependence", {"yes": DEPENDENT, "no": INDEPENDENT}),
)


@dataclass
class Labels:
    iri: str
    rigidity: str
    identity: str
    unity: str
    dependence: str
    why: str = ""


@dataclass
class Violation:
    child: str
    parent: str
    metaproperty: str
    detail: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def payload(label: str, gloss: str | None, parents: Sequence[str]) -> dict[str, str]:
    return {
        "label": label,
        "gloss": gloss or "(no definition yet)",
        "parents": ", ".join(parents) or "(nothing; it is a top-level class)",
    }


def parse(text: str, payload: dict[str, str]) -> dict[str, str]:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())

    necessary = str(data.get("necessary", "")).strip().lower()
    if necessary not in _NECESSARY:
        raise ValueError(f"`necessary` is never/sometimes/always, not {necessary!r}")
    answer = {"rigidity": _NECESSARY[necessary]}
    for asked, name, mapping in _YES_NO:
        value = str(data.get(asked, "")).strip().lower()
        if value not in mapping:
            raise ValueError(f"`{asked}` is yes or no, not {value!r}")
        answer[name] = mapping[value]
    answer["why"] = str(data.get("why", "")).strip()
    return answer


def check(graph: Graph, labels: dict[str, Labels]) -> tuple[list[Violation], int, int]:
    """Every asserted subsumption whose two ends are labelled, against the four constraints.

    Returns the violations, how many subsumptions were checked and how many were skipped for
    want of a label. The second pair matters as much as the first: a filter that silently
    checked a tenth of the hierarchy would report a clean result it never established.
    """
    violations: list[Violation] = []
    checked = skipped = 0
    for child, _, parent in sorted(graph.triples((None, RDFS.subClassOf, None)), key=str):
        if not (isinstance(child, URIRef) and isinstance(parent, URIRef)):
            continue
        below, above = labels.get(str(child)), labels.get(str(parent))
        if below is None or above is None:
            skipped += 1
            continue
        checked += 1
        for metaproperty, parent_value, child_value, detail in CONSTRAINTS:
            if (getattr(above, metaproperty) == parent_value
                    and getattr(below, metaproperty) == child_value):
                violations.append(Violation(str(child), str(parent), metaproperty, detail))
    return violations, checked, skipped


def persist(conn: sqlite3.Connection, version_id: str, labels: Sequence[Labels]) -> None:
    install(conn)
    conn.executemany(
        "INSERT OR REPLACE INTO metaproperties (version_id, iri, rigidity, identity, unity, "
        "dependence, why, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (version_id, item.iri, item.rigidity, item.identity, item.unity,
             item.dependence, item.why, _now())
            for item in labels
        ],
    )
    conn.commit()


def load(conn: sqlite3.Connection, version_id: str) -> dict[str, Labels]:
    """The labels for a version, falling back to any earlier version's.

    A metaproperty is a fact about the concept, not about the state of the ontology: a class
    that was rigid in v3 is rigid in v7, and re-asking would pay again for the same answer.
    Only a class whose meaning changed needs re-labelling, and that is a rename, not a version.
    """
    install(conn)
    found: dict[str, Labels] = {}
    for row in conn.execute(
        "SELECT * FROM metaproperties ORDER BY (version_id = ?) ASC, created_at ASC",
        (version_id,),
    ):
        found[row["iri"]] = Labels(
            iri=row["iri"], rigidity=row["rigidity"], identity=row["identity"],
            unity=row["unity"], dependence=row["dependence"], why=row["why"] or "",
        )
    return found
