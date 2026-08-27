"""ITER-VALIDATE — the filter chain (ITER-BRANCH).

Stacked, and only what survives all of them reaches branch construction. **The user never sees
an individual axiom** (BRANCH-ONLY-REVIEW): they see branches, and this chain decides what goes into
one.

    1  ELK                    hard reject, incomplete       reasoning.py
    2  HermiT                 hard reject, with justifications   reasoning.py
    3  SHACL                  reject, shape constraints over the ABox   here
    4  OntoClean              hard reject, ill-formed subsumptions      not built
    5  pitfalls (OOPS!-like)  warning, never a reject                   here
    6  textual evidence       reject, and ONLY for `textual` provenance here
    7  structural metrics     reject                        structural.py

Two of these have a shape worth stating up front.

**ITER-VALIDATE-6-EVIDENCE applies to one provenance and not the other.** The rule "every axiom
without a
citation is discarded" would delete exactly the bridges that make a seed useful: a
`world_knowledge` axiom has no citation by construction (6.2b), and that is what it is for.
Applying the evidence filter to it is not a stricter policy, it is a different and wrong one.

**ITER-VALIDATE-5-PITFALLS never rejects.** A pitfall is a smell: a class with no definition, a
property with no
domain, a cycle in the hierarchy. Some of those are deliberate. The spec makes it a warning and
the chain treats it as one — it is reported and nothing is dropped.

What is here is a **local subset of the OOPS! catalogue**, not OOPS!. The real scanner is a web
service, and sending someone's ontology to a third party is a decision for its owner, not a step
a pipeline takes on its own. The checks below are the ones computable offline from the graph;
which pitfalls that leaves out is recorded in DEUDA_TECNICA.md rather than implied.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from .axiomatization import TEXTUAL, Axiom

REJECT = "REJECT"
WARN = "WARN"
PASS = "PASS"
SKIPPED = "SKIPPED"


@dataclass
class Verdict:
    name: str
    decision: str
    note: str = ""
    findings: list[str] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return self.decision == REJECT


# ─────────────────────────  ITER-VALIDATE-6-EVIDENCE — textual evidence  ─────────────────────────


def evidence(axioms: Sequence[Axiom]) -> tuple[list[Axiom], list[tuple[Axiom, str]]]:
    """Every `textual` axiom must carry the mentions it came from. Nothing else is checked.

    Returns what survives and what did not, because this filter rejects axioms rather than the
    batch: one uncited claim is not a reason to throw away the iteration.
    """
    kept, dropped = [], []
    for axiom in axioms:
        if axiom.provenance == TEXTUAL and not axiom.support:
            dropped.append((axiom, "claims textual provenance and cites no mention"))
        else:
            kept.append(axiom)
    return kept, dropped


# ─────────────────────────  ITER-VALIDATE-3-SHACL — SHACL  ─────────────────────────


class ShapesUnavailable(RuntimeError):
    """pyshacl is not installed: `uv sync --extra validation`."""


def shapes(data: Graph, shapes_graph: Graph) -> Verdict:
    """Shape constraints over the ABox.

    The shapes are not derived from the TBox: OWL axioms are about what must be true, SHACL
    shapes about what must be *stated*, and under the open-world assumption those are different
    claims. Generating one from the other would turn every unstated fact into a violation,
    which is precisely the closed-world reading this project is not making. So the shapes are
    written by hand, and with none written this filter reports that it did not run.
    """
    try:
        from pyshacl import validate as pyshacl_validate
    except ImportError as exc:  # pragma: no cover - the extra is optional
        raise ShapesUnavailable(str(exc)) from exc

    conforms, _, text = pyshacl_validate(
        data, shacl_graph=shapes_graph, advanced=True, inference="none",
    )
    if conforms:
        return Verdict("SHACL", PASS, f"{len(shapes_graph)} shape triples")
    lines = [line.strip() for line in str(text).splitlines() if line.strip().startswith("Message")]
    return Verdict("SHACL", REJECT, f"{len(lines)} violation(s)", lines[:20])


def load_shapes(path: Path | None) -> Graph | None:
    if path is None or not Path(path).exists():
        return None
    return Graph().parse(str(path))


# ─────────────────────────  ITER-VALIDATE-5-PITFALLS — pitfalls  ─────────────────────────

# Each entry is one OOPS! pitfall that can be decided from the graph alone. The identifiers are
# theirs; the implementations are ours, and deliberately conservative — a warning nobody trusts
# is worse than one that fires rarely.
PITFALLS = ("P06", "P08", "P11", "P19", "P24")


def pitfalls(graph: Graph) -> Verdict:
    """Modelling smells. A warning, never a rejection (ITER-VALIDATE-5-PITFALLS)."""
    findings: list[str] = []
    classes = {s for s in graph.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}
    properties = {
        s for kind in (OWL.ObjectProperty, OWL.DatatypeProperty)
        for s in graph.subjects(RDF.type, kind) if isinstance(s, URIRef)
    }

    findings += [f"P06 cycle in the hierarchy: {name}" for name in _cycles(graph, classes)]

    naming = (("label", (SKOS.prefLabel, RDFS.label)),
              ("definition", (SKOS.definition, RDFS.comment)))
    for subject in sorted(classes, key=str):
        # `graph.value(...) is None`, not `any(graph.objects(...))`: `objects` returns a
        # generator, and a generator object is always truthy, so the second form is a check
        # that can never fail.
        missing = [
            name for name, predicates in naming
            if all(graph.value(subject, predicate) is None for predicate in predicates)
        ]
        if missing:
            findings.append(f"P08 {subject} has no {' and no '.join(missing)}")

    for subject in sorted(properties, key=str):
        for name, predicate in (("domain", RDFS.domain), ("range", RDFS.range)):
            declared = list(graph.objects(subject, predicate))
            if not declared:
                findings.append(f"P11 {subject} declares no {name}")
            elif len(declared) > 1:
                # Several domains intersect in OWL; almost nobody means that.
                findings.append(
                    f"P19 {subject} declares {len(declared)} {name}s, which OWL reads as their "
                    "intersection"
                )

    findings += [
        f"P24 {subject} is defined in terms of itself"
        for subject in sorted(classes, key=str)
        if (subject, OWL.equivalentClass, subject) in graph
    ]

    if not findings:
        return Verdict("pitfalls", PASS, f"{len(PITFALLS)} checks, {len(classes)} classes")
    return Verdict(
        "pitfalls", WARN,
        f"{len(findings)} smell(s) across {len(PITFALLS)} checks; a warning, not a reject",
        findings[:40],
    )


def _cycles(graph: Graph, classes: set[URIRef]) -> list[str]:
    """Classes reachable from themselves through `rdfs:subClassOf`.

    A cycle makes every class in it equivalent, which is almost never intended and which the
    depth metric of ITER-VALIDATE-7-STRUCTURE cannot see — that one stops at a repeated node rather
    than
    reporting it.
    """
    parents: dict[URIRef, set[URIRef]] = {}
    for child, _, parent in graph.triples((None, RDFS.subClassOf, None)):
        if child in classes and isinstance(parent, URIRef):
            parents.setdefault(child, set()).add(parent)

    def reaches(start: URIRef) -> bool:
        stack, seen = list(parents.get(start, ())), set()
        while stack:
            node = stack.pop()
            if node == start:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(parents.get(node, ()))
        return False

    return [str(start) for start in sorted(parents, key=str) if reaches(start)]
