from __future__ import annotations

from rdflib import Graph

from onto_pipeline import structural

HEADER = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <http://example.org/onto#> .
"""


def graph(body: str) -> Graph:
    return Graph().parse(data=HEADER + body, format="turtle")


def chain(length: int) -> str:
    body = ":C0 a owl:Class .\n"
    for index in range(1, length):
        body += f":C{index} a owl:Class ; rdfs:subClassOf :C{index - 1} .\n"
    return body


def test_a_level_with_a_single_subclass_is_rejected():
    """The generator over-generates hierarchy; a level that divides nothing is intercepted in
    the validator rather than argued with in the prompt."""
    report = structural.check(graph("""
    :Technique a owl:Class .
    :Interview a owl:Class ; rdfs:subClassOf :Technique .
    """))
    assert [finding.check for finding in report.findings] == ["single_child_level"]
    assert report.rejected


def test_a_level_that_actually_divides_is_accepted():
    report = structural.check(graph("""
    :Technique a owl:Class .
    :Interview a owl:Class ; rdfs:subClassOf :Technique .
    :Survey a owl:Class ; rdfs:subClassOf :Technique .
    """))
    assert not report.rejected
    assert (report.depth, report.max_branching, report.n_classes) == (2, 2, 3)


def test_excessive_depth_is_rejected():
    report = structural.check(graph(chain(12)), max_depth=8)
    assert any(finding.check == "depth" for finding in report.findings)
    assert report.depth == 12


def test_excessive_branching_is_rejected():
    body = ":Root a owl:Class .\n" + "".join(
        f":C{index} a owl:Class ; rdfs:subClassOf :Root .\n" for index in range(30)
    )
    report = structural.check(graph(body), max_branching=25)
    assert any(finding.check == "branching" for finding in report.findings)


def test_a_class_outside_the_hierarchy_is_an_orphan():
    report = structural.check(graph("""
    :Technique a owl:Class .
    :Interview a owl:Class ; rdfs:subClassOf :Technique .
    :Survey a owl:Class ; rdfs:subClassOf :Technique .
    :Floating a owl:Class .
    """))
    orphans = [finding.subject for finding in report.findings if
               finding.check == "orphan_class"]
    assert orphans == ["http://example.org/onto#Floating"]


def test_a_second_top_level_class_is_not_an_orphan():
    """Without an upper ontology several roots are normal; only isolation is a finding."""
    report = structural.check(graph("""
    :Technique a owl:Class .
    :Interview a owl:Class ; rdfs:subClassOf :Technique .
    :Survey a owl:Class ; rdfs:subClassOf :Technique .
    :Subject a owl:Class .
    :Person a owl:Class ; rdfs:subClassOf :Subject .
    :Institution a owl:Class ; rdfs:subClassOf :Subject .
    """))
    assert not report.rejected


def test_a_subclass_cycle_does_not_hang_the_depth_measurement():
    """The cycle is the reasoner's finding; this check must not spin on it."""
    report = structural.check(graph("""
    :A a owl:Class ; rdfs:subClassOf :B .
    :B a owl:Class ; rdfs:subClassOf :A .
    """))
    assert report.depth == 0  # no root: every class has a superclass
