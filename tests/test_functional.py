from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF

from onto_pipeline import functional
from onto_pipeline.mapping import DERIVED_FROM, UNRESOLVED

P = URIRef("c:bornIn")
LABELS = {str(P): "born in"}


def abox(*assertions: tuple[str, str]) -> Graph:
    graph = Graph()
    for subject, value in assertions:
        graph.add((URIRef(subject), RDF.type, OWL.NamedIndividual))
        graph.add((URIRef(subject), P, Literal(value)))
    return graph


def support_of(graph: Graph) -> functional.Support:
    return next(s for s in functional.survey(graph) if s.property_iri == str(P))


# ─────────────────────────  the survey  ─────────────────────────


def test_one_value_everywhere_is_not_proof_of_functionality():
    """It proves that no counterexample was observed, which is a different claim — and the
    only one a corpus can support under the open-world assumption."""
    support = support_of(abox(("c:i1", "Lima"), ("c:i2", "Oslo")))
    assert not support.refuted and support.individuals == 2


def test_one_counterexample_refutes_it_outright():
    """The one direction that is sound: a counterexample is knowledge, its absence is
    silence."""
    graph = abox(("c:i1", "Lima"))
    graph.add((URIRef("c:i1"), P, Literal("Oslo")))
    assert support_of(graph).refuted and support_of(graph).max_values == 2


def test_the_distribution_is_reported_and_not_only_the_verdict():
    """"One value across 3 individuals" and "one value across 400" are the same qualitative
    signal and opposite decisions."""
    support = support_of(abox(("c:i1", "a"), ("c:i2", "b"), ("c:i3", "c")))
    assert support.distribution == {1: 3}
    assert "3 individual(s) with 1 value(s)" in support.rendered_distribution


def test_an_unresolved_duplicate_is_left_out_of_the_count():
    """Two duplicates with one value each look exactly like confirmation of functionality —
    the one way this survey could manufacture its own evidence (6.2)."""
    graph = abox(("c:i1", "Lima"), ("c:i2", "Lima"))
    graph.add((URIRef("c:i2"), UNRESOLVED, Literal(True)))
    support = support_of(graph)
    assert support.individuals == 1 and support.excluded == 1


def test_the_pipelines_own_vocabulary_is_not_a_question_for_anyone():
    graph = Graph()
    graph.add((URIRef("c:i1"), DERIVED_FROM, URIRef("c:m1")))
    graph.add((URIRef("c:i1"), DERIVED_FROM, URIRef("c:m2")))
    assert functional.survey(graph) == []
    assert functional.survey(graph, include_internal=True)


def test_repeating_the_same_value_is_still_one_value():
    graph = abox(("c:i1", "Lima"))
    graph.add((URIRef("c:i1"), P, Literal("Lima")))
    assert not support_of(graph).refuted


# ─────────────────────────  the question  ─────────────────────────


def test_a_candidate_becomes_a_question_never_a_declaration():
    """The only category the spec sends to an individual decision: domain knowledge is
    irreplaceable here."""
    items = functional.findings(
        functional.survey(abox(("c:i1", "a"), ("c:i2", "b"), ("c:i3", "c"))),
        LABELS, min_individuals=3,
    )
    assert len(items) == 1 and items[0].kind == functional.FUNCTIONAL_CANDIDATE
    assert "cannot answer this" in items[0].summary
    assert items[0].payload["distribution"] == {"1": 3}


def test_a_refuted_property_is_not_asked_about():
    graph = abox(("c:i1", "Lima"))
    graph.add((URIRef("c:i1"), P, Literal("Oslo")))
    assert functional.findings(functional.survey(graph), LABELS, min_individuals=1) == []


def test_evidence_too_thin_is_not_worth_someones_attention():
    """Asking anyway trains a person to say yes."""
    supports = functional.survey(abox(("c:i1", "a"), ("c:i2", "b")))
    assert functional.findings(supports, LABELS, min_individuals=5) == []


def test_the_excluded_duplicates_travel_with_the_question():
    graph = abox(("c:i1", "a"), ("c:i2", "b"), ("c:i3", "c"), ("c:i4", "d"))
    graph.add((URIRef("c:i4"), UNRESOLVED, Literal(True)))
    item = functional.findings(functional.survey(graph), LABELS, min_individuals=3)[0]
    assert item.payload["excluded_unresolved_duplicates"] == 1


# ─────────────────────────  declaring it  ─────────────────────────


def test_declaring_leaves_the_previous_graph_untouched():
    """The caller needs the old state, because the consequence of this axiom is not an error
    but a merge."""
    before = abox(("c:i1", "Lima"))
    after = functional.declare(before, str(P))
    assert (P, RDF.type, OWL.FunctionalProperty) not in before
    assert (P, RDF.type, OWL.FunctionalProperty) in after
