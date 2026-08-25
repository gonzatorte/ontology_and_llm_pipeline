from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph

LIB = Path(__file__).resolve().parent.parent / "lib"

pytest.importorskip("jpype", reason="the reasoning extra is not installed")
pytestmark = pytest.mark.skipif(
    not list(LIB.glob("*.jar")), reason="run scripts/fetch-jars.sh first"
)

CONSISTENT = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <http://example.org/onto#> .
:Technique a owl:Class .
:Subject a owl:Class .
:Interview a owl:Class ; rdfs:subClassOf :Technique .
"""

UNSATISFIABLE = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <http://example.org/onto#> .
:Technique a owl:Class .
:Subject a owl:Class ; owl:disjointWith :Technique .
:Interview a owl:Class ; rdfs:subClassOf :Technique , :Subject .
"""

# Cardinality is outside OWL 2 EL, so ELK ignores the axiom rather than failing on it.
OUTSIDE_EL = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix : <http://example.org/onto#> .
:hasSubject a owl:ObjectProperty .
:Subject a owl:Class .
:Technique a owl:Class ; rdfs:subClassOf
  [ a owl:Restriction ; owl:onProperty :hasSubject ; owl:maxCardinality 1 ] ,
  [ a owl:Restriction ; owl:onProperty :hasSubject ; owl:minCardinality 3 ] .
"""


@pytest.fixture(scope="module")
def reasoners():
    from onto_pipeline.reasoning import Reasoners

    return Reasoners(LIB, hermit_timeout_s=60)


def graph(turtle: str) -> Graph:
    return Graph().parse(data=turtle, format="turtle")


def test_elk_never_approves_only_declines_to_reject(reasoners):
    """The requirement that makes the asymmetry impossible to misread: silence from ELK is
    INCONCLUSIVE, never OK."""
    from onto_pipeline import reasoning

    result = reasoners.elk(reasoners.load(graph(CONSISTENT)), coverage_threshold=0.7)
    assert result.verdict == reasoning.INCONCLUSIVE
    assert reasoning.INCONCLUSIVE != "OK" and not hasattr(reasoning, "OK")


def test_elk_rejection_is_real(reasoners):
    from onto_pipeline import reasoning

    result = reasoners.elk(reasoners.load(graph(UNSATISFIABLE)), coverage_threshold=0.7)
    assert result.verdict == reasoning.REJECTED
    assert result.unsatisfiable == ["http://example.org/onto#Interview"]


def test_elk_is_skipped_when_it_would_ignore_too_much(reasoners):
    from onto_pipeline import reasoning

    ontology = reasoners.load(graph(OUTSIDE_EL))
    assert reasoners.elk(ontology, coverage_threshold=0.99).verdict == reasoning.SKIPPED


def test_what_elk_ignores_hermit_still_catches(reasoners):
    """Why ELK's silence cannot count as approval: min 3 and max 1 on the same property is a
    contradiction expressed outside EL, so ELK reasons past it."""
    from onto_pipeline import reasoning

    ontology = reasoners.load(graph(OUTSIDE_EL))
    assert reasoners.elk(ontology, coverage_threshold=0.0).verdict == reasoning.INCONCLUSIVE
    assert reasoners.hermit(ontology).unsatisfiable == ["http://example.org/onto#Technique"]


def test_hermit_reports_a_consistent_ontology(reasoners):
    result = reasoners.hermit(reasoners.load(graph(CONSISTENT)))
    assert result.consistent and not result.unsatisfiable


def test_justifications_name_the_conflicting_axioms(reasoners):
    """B6 groups branches by decision axis, which needs the conflicting subset, not the fact
    that a conflict exists."""
    result = reasoners.hermit(reasoners.load(graph(UNSATISFIABLE)))
    justifications = result.justifications["http://example.org/onto#Interview"]
    assert justifications
    axioms = " ".join(justifications[0])
    assert "DisjointClasses" in axioms
    assert axioms.count("SubClassOf") == 2


def test_profile_detection_reports_the_seed_as_owl_dl(reasoners):
    report = reasoners.profile(reasoners.load(graph(CONSISTENT)))
    assert report.detected == "OWL2_EL"
    assert report.violations["OWL2_DL"] == 0
    assert report.el_coverage == 1.0


def test_an_undeclared_annotation_property_leaves_owl_dl(reasoners):
    """The defect A0.0 caught in A0's own output: SKOS annotations must be declared or the
    ontology is not OWL 2 DL and ELK's coverage collapses."""
    undeclared = CONSISTENT + """
    @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
    :Technique skos:prefLabel "Technique"@en .
    """
    report = reasoners.profile(reasoners.load(graph(undeclared)))
    assert report.violations["OWL2_DL"] > 0
    assert report.detected == "OWL2_FULL"
