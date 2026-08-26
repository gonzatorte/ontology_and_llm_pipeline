"""Functional properties — the silent risk (spec 6.8, D2).

`owl:FunctionalProperty` is maximum cardinality 1 under another name, and this is the one
category the spec sends to an individual decision by the user, because domain knowledge is
irreplaceable here.

**Detecting functionality from the ABox is invalid in principle under the open-world
assumption.** Every entity having exactly one value of X does not prove that X is functional; it
proves that no counterexample was observed. Those are different claims, and the corpus can only
ever support the second.

The asymmetry is what makes it dangerous. Declaring a property functional by mistake makes the
reasoner infer `owl:sameAs` between distinct individuals and merge them — **and it throws no
inconsistency while doing it**. The ontology stays consistent and quietly says two things are
one. That is why nothing here declares anything: this module surveys, and asks.

One direction *is* sound, and it is free: an individual with two values refutes functionality
outright. Under the open-world assumption a counterexample is knowledge while its absence is
silence, so `refuted` below is the only verdict this can reach on its own.

What the question shows matters as much as that it is asked. "One value across 3 individuals"
and "one value across 400" are the same qualitative signal and opposite decisions, so the
distribution goes in the question, not just the conclusion.

Individuals marked `possible_duplicate_unresolved` are excluded from the count (6.2): two
duplicates carrying one value each look exactly like confirmation of functionality, and that is
the one way this survey could manufacture its own evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, PROV, RDF, RDFS

from . import review
from .mapping import ONTO, UNRESOLVED

FUNCTIONAL_CANDIDATE = "functional_candidate"

# Pipeline vocabulary, not the domain's. `derivedFromMention` is many-valued by design and
# `document` is functional by construction; neither is a question for anyone.
INTERNAL = (ONTO, str(RDF), str(RDFS), str(OWL), str(PROV))


@dataclass
class Support:
    """What is known about one property, and what is only not contradicted."""

    property_iri: str
    individuals: int = 0                       # subjects with at least one value
    distribution: dict[int, int] = field(default_factory=dict)   # values per subject -> count
    excluded: int = 0                          # unresolved duplicates, left out of the count
    examples: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def refuted(self) -> bool:
        """Someone has two values. The only conclusion this survey can reach by itself."""
        return any(count > 1 for count in self.distribution)

    @property
    def max_values(self) -> int:
        return max(self.distribution, default=0)

    @property
    def rendered_distribution(self) -> str:
        return ", ".join(
            f"{subjects} individual(s) with {values} value(s)"
            for values, subjects in sorted(self.distribution.items())
        )


def survey(graph: Graph, *, include_internal: bool = False) -> list[Support]:
    """Value counts per property, over the ABox, with unresolved duplicates left out."""
    unresolved = {
        subject for subject, _, _ in graph.triples((None, UNRESOLVED, None))
    }
    by_property: dict[URIRef, dict[URIRef, list[str]]] = {}
    excluded: dict[URIRef, set[URIRef]] = {}

    for subject, predicate, obj in graph:
        if predicate == RDF.type or (
            not include_internal and any(str(predicate).startswith(ns) for ns in INTERNAL)
        ):
            continue
        if not isinstance(subject, URIRef):
            continue
        if subject in unresolved:
            excluded.setdefault(predicate, set()).add(subject)
            continue
        by_property.setdefault(predicate, {}).setdefault(subject, []).append(str(obj))

    supports = []
    for predicate, subjects in by_property.items():
        distribution: dict[int, int] = {}
        for values in subjects.values():
            count = len(set(values))
            distribution[count] = distribution.get(count, 0) + 1
        supports.append(Support(
            property_iri=str(predicate),
            individuals=len(subjects),
            distribution=distribution,
            excluded=len(excluded.get(predicate, ())),
            examples=[
                (str(subject), sorted(set(values))[:4])
                for subject, values in sorted(subjects.items(), key=lambda item: str(item[0]))
                if len(set(values)) > 1
            ][:5],
        ))
    return sorted(supports, key=lambda item: (-item.individuals, item.property_iri))


def findings(
    supports: Sequence[Support], labels: dict[str, str], *, min_individuals: int
) -> list[review.Finding]:
    """One question per property that survives the heuristic. Never a declaration.

    A property refuted by a counterexample is not asked about — that is settled, and settled in
    the only direction the open world allows. One below `min_individuals` is not asked about
    either: the answer would rest on evidence too thin to be worth someone's attention, and
    asking anyway trains a person to say yes.
    """
    items = []
    for support in supports:
        if support.refuted or support.individuals < min_individuals:
            continue
        name = labels.get(support.property_iri, support.property_iri.rsplit("/", 1)[-1])
        items.append(review.Finding(
            kind=FUNCTIONAL_CANDIDATE,
            subject_iri=support.property_iri,
            summary=(
                f"«{name}» has exactly one value on all {support.individuals} individuals that "
                f"carry it. Is it functional? The corpus cannot answer this: no counterexample "
                f"was seen, which is not the same as there being none"
            ),
            payload={
                "individuals": support.individuals,
                "distribution": {str(k): v for k, v in sorted(support.distribution.items())},
                "excluded_unresolved_duplicates": support.excluded,
            },
        ))
    return items


def declare(graph: Graph, property_iri: str) -> Graph:
    """A new graph with the declaration added. The caller still needs the old one to fall back
    to, because the consequence of this axiom is not an error but a merge."""
    extended = Graph()
    for prefix, namespace in graph.namespaces():
        extended.bind(prefix, namespace)
    for triple in graph:
        extended.add(triple)
    extended.add((URIRef(property_iri), RDF.type, OWL.FunctionalProperty))
    return extended
