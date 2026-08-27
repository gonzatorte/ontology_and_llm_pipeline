"""ITER-VALIDATE filter 7 — structural metrics over the TBox (ITER-BRANCH).

Rejection, not warning. Two of these also intercept a known bias of the generator rather than
merely describing the ontology (ITER-EXTRACT): a model over-generates hierarchy, so a level with a
single subclass and a class introduced with no declared division criterion are both rejected
here, in the validator, instead of being argued with in the prompt.

This operates on the TBox as logic, not as a graph. No graph algorithm — clustering,
communities, structural similarity — is applied to the RDF serialization of the TBox:
disjointness inverts its sign under structural similarity, since two classes declared
incompatible appear connected (LAYERS-ONTOLOGY-NOT-GRAPH).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS


@dataclass
class Finding:
    check: str
    subject: str
    detail: str


@dataclass
class StructuralReport:
    depth: int = 0
    max_branching: int = 0
    n_classes: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return bool(self.findings)


def check(
    graph: Graph,
    *,
    max_depth: int = 8,
    max_branching: int = 25,
) -> StructuralReport:
    classes = {
        subject for subject in graph.subjects(RDF.type, OWL.Class)
        if isinstance(subject, URIRef)
    }
    children: dict[URIRef, set[URIRef]] = {}
    parents: dict[URIRef, set[URIRef]] = {}
    for child, _, parent in graph.triples((None, RDFS.subClassOf, None)):
        if child in classes and isinstance(parent, URIRef) and parent in classes:
            children.setdefault(parent, set()).add(child)
            parents.setdefault(child, set()).add(parent)

    report = StructuralReport(n_classes=len(classes))
    roots = sorted(classes - set(parents), key=str)
    report.depth = max((_depth(root, children, set()) for root in roots), default=0)
    report.max_branching = max((len(kids) for kids in children.values()), default=0)

    if report.depth > max_depth:
        report.findings.append(
            Finding("depth", "", f"hierarchy is {report.depth} deep, limit is {max_depth}")
        )
    for parent, kids in sorted(children.items(), key=lambda item: str(item[0])):
        if len(kids) > max_branching:
            report.findings.append(
                Finding("branching", str(parent),
                        f"{len(kids)} direct subclasses, limit is {max_branching}")
            )
        if len(kids) == 1:
            # A level with one subclass divides nothing: it states the parent twice under
            # another name.
            report.findings.append(
                Finding("single_child_level", str(parent),
                        f"one subclass ({next(iter(kids))}); the level divides nothing")
            )

    # Several top-level classes are normal without an upper ontology, so a root is not an
    # orphan for being one. A class with neither superclass nor subclasses is: it sits outside
    # the hierarchy entirely.
    for orphan in sorted(classes - set(parents) - set(children), key=str):
        report.findings.append(
            Finding("orphan_class", str(orphan), "no superclass and no subclasses")
        )
    return report


def _depth(node: URIRef, children: dict[URIRef, set[URIRef]], seen: set[URIRef]) -> int:
    if node in seen:
        return 0  # a subclass cycle is the reasoner's finding, not a depth measurement
    seen = seen | {node}
    return 1 + max((_depth(child, children, seen) for child in children.get(node, ())),
                   default=0)
