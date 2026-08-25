from __future__ import annotations

import pytest
from rdflib import Graph

from onto_pipeline import versioning
from onto_pipeline.db import connect

BASE = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix : <http://example.org/onto#> .
:Technique a owl:Class ; rdfs:label "Technique" .
:Interview a owl:Class ; rdfs:label "Interview" ; rdfs:subClassOf :Technique .
"""

RENAMED = BASE.replace('rdfs:label "Interview"', 'rdfs:label "Entrevista"')
REORDERED = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <http://example.org/onto#> .
:Interview rdfs:subClassOf :Technique ; a owl:Class ; rdfs:label "Interview" .
:Technique rdfs:label "Technique" ; a owl:Class .
"""
EXTENDED = BASE + ":Survey a owl:Class ; rdfs:subClassOf :Technique .\n"


def graph(turtle: str) -> Graph:
    return Graph().parse(data=turtle, format="turtle")


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path)


def test_serialization_order_does_not_change_the_state():
    assert versioning.state_hash(graph(BASE)) == versioning.state_hash(graph(REORDERED))


def test_a_pure_rename_is_not_a_new_state():
    """The hash is computed over IRIs, not labels."""
    assert versioning.state_hash(graph(BASE)) == versioning.state_hash(graph(RENAMED))


def test_an_added_axiom_is_a_new_state():
    assert versioning.state_hash(graph(BASE)) != versioning.state_hash(graph(EXTENDED))


def test_blank_nodes_do_not_make_two_equal_states_differ():
    with_bnode = BASE + """
    :Interview rdfs:subClassOf [ a owl:Restriction ;
        owl:onProperty :hasSubject ; owl:someValuesFrom :Subject ] .
    """
    assert versioning.state_hash(graph(with_bnode)) == versioning.state_hash(graph(with_bnode))


def test_diff_separates_axioms_from_renames():
    renaming = versioning.diff(graph(BASE), graph(RENAMED))
    assert not renaming.added and not renaming.removed
    assert renaming.labels_changed

    extending = versioning.diff(graph(BASE), graph(EXTENDED))
    assert len(extending.added) == 2 and not extending.removed


def test_returning_to_an_earlier_state_is_detected_as_a_return(conn):
    """A -> B -> A is exactly a cycle in the DAG. Going back is allowed, but explicitly."""
    versioning.commit(conn, graph(BASE), version_id="v1")
    versioning.commit(conn, graph(EXTENDED), version_id="v2", parent_id="v1", iteration=1)

    returning = graph(BASE)
    found = versioning.find_by_hash(conn, versioning.state_hash(returning))
    assert found is not None and found.id == "v1"


def test_a_genuinely_new_state_is_not_reported_as_a_return(conn):
    versioning.commit(conn, graph(BASE), version_id="v1")
    assert versioning.find_by_hash(conn, versioning.state_hash(graph(EXTENDED))) is None


def test_an_almost_identical_state_is_caught_by_distance(conn):
    """What the exact hash misses: the same commitment with one axiom moved."""
    versioning.commit(conn, graph(EXTENDED), version_id="v1")
    nearly = EXTENDED + ":Survey rdfs:comment \"x\" .\n:Focus a owl:Class .\n"
    match = versioning.nearest_state(conn, graph(nearly), threshold=0.3)
    assert match is not None and match[0].id == "v1" and 0 < match[1] <= 0.3


def test_a_version_round_trips_and_keeps_its_lineage(conn):
    versioning.commit(conn, graph(BASE), version_id="v1")
    versioning.commit(conn, graph(EXTENDED), version_id="v2", parent_id="v1", iteration=1)
    versioning.commit(conn, graph(BASE), version_id="v3", parent_id="v1", iteration=1,
                      branch_id="b_alt")

    version, restored = versioning.load(conn, "v2")
    assert version.parent_id == "v1"
    assert versioning.state_hash(restored) == versioning.state_hash(graph(EXTENDED))
    # v3 is the branch not taken at iteration 1; both stay reachable from v1.
    assert versioning.lineage(conn, "v3") == ["v3", "v1"]
    assert versioning.lineage(conn, "v2") == ["v2", "v1"]


def test_diff_with_parent_reports_what_the_iteration_did(conn):
    versioning.commit(conn, graph(BASE), version_id="v0")
    versioning.commit(conn, graph(EXTENDED), version_id="v1", parent_id="v0", iteration=1)

    parent, result = versioning.diff_with_parent(conn, "v1")

    assert parent.id == "v0"
    assert len(result.added) == 2      # :Survey a owl:Class, :Survey rdfs:subClassOf :Technique
    assert not result.removed
    assert not result.labels_changed


def test_a_root_version_has_nothing_to_diff_against(conn):
    versioning.commit(conn, graph(BASE), version_id="v0")

    assert versioning.diff_with_parent(conn, "v0") is None


def test_a_rename_diffs_as_an_annotation_change_not_axiom_churn(conn):
    versioning.commit(conn, graph(BASE), version_id="v0")
    versioning.commit(conn, graph(RENAMED), version_id="v1", parent_id="v0", iteration=1)

    _, result = versioning.diff_with_parent(conn, "v1")

    assert not result.added and not result.removed
    assert len(result.labels_changed) == 2   # the old label leaves, the new one arrives


def test_a_diff_is_read_through_labels_not_opaque_iris():
    labels = versioning.label_index(graph(BASE), graph(RENAMED))
    interview = "http://example.org/onto#Interview"

    assert versioning.short_name(interview, labels) in {"Interview", "Entrevista"}
    assert versioning.short_name("http://www.w3.org/2002/07/owl#Class", labels) == "owl:Class"
    assert versioning.short_name("http://example.org/onto#Unlabelled", labels) == "…Unlabelled"


def test_the_label_index_covers_both_sides_of_the_comparison():
    """A class the newer version removed is only nameable through the older one."""
    labels = versioning.label_index(graph(EXTENDED), graph(BASE))

    assert labels["http://example.org/onto#Technique"] == "Technique"
