from __future__ import annotations

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import RDFS

from onto_pipeline import ontoclean as oc
from onto_pipeline.db import connect
from onto_pipeline.ontoclean import Labels

PERSON, STUDENT = "c:Person", "c:Student"


def labels(iri: str, rigidity=oc.RIGID, identity=oc.CARRIES_IDENTITY,
           unity=oc.CARRIES_UNITY, dependence=oc.INDEPENDENT, why="") -> Labels:
    return Labels(iri, rigidity, identity, unity, dependence, why)


def hierarchy(child: str, parent: str) -> Graph:
    graph = Graph()
    graph.add((URIRef(child), RDFS.subClassOf, URIRef(parent)))
    return graph


def check(child_labels, parent_labels, child=STUDENT, parent=PERSON):
    return oc.check(hierarchy(child, parent),
                    {child: child_labels, parent: parent_labels})


# ─────────────────────────  the four constraints  ─────────────────────────


def test_a_student_under_a_person_is_fine():
    violations, checked, skipped = check(
        labels(STUDENT, rigidity=oc.ANTI_RIGID, dependence=oc.DEPENDENT), labels(PERSON),
    )
    assert violations == [] and (checked, skipped) == (1, 0)


def test_a_person_under_a_student_is_the_violation_no_reasoner_can_state():
    """Perfectly consistent in OWL, and wrong: being a student is something one stops being,
    and being a person is not."""
    violations, _, _ = check(
        labels(PERSON), labels(STUDENT, rigidity=oc.ANTI_RIGID, dependence=oc.DEPENDENT),
        child=PERSON, parent=STUDENT,
    )
    assert [item.metaproperty for item in violations] == ["rigidity", "dependence"]


def test_a_parent_with_no_identity_criterion_cannot_subsume_one_that_has_it():
    violations, _, _ = check(labels(STUDENT), labels(PERSON, identity=oc.NO_IDENTITY))
    assert [item.metaproperty for item in violations] == ["identity"]


def test_an_anti_unity_parent_cannot_subsume_a_whole():
    violations, _, _ = check(labels(STUDENT), labels(PERSON, unity=oc.ANTI_UNITY))
    assert [item.metaproperty for item in violations] == ["unity"]


def test_a_dependent_parent_cannot_subsume_an_independent_child():
    violations, _, _ = check(labels(STUDENT), labels(PERSON, dependence=oc.DEPENDENT))
    assert [item.metaproperty for item in violations] == ["dependence"]


def test_a_non_rigid_parent_is_not_an_anti_rigid_one():
    """`-R` says some instances could stop being one; only `~R` says they all could, and only
    that forbids subsuming a rigid class."""
    violations, _, _ = check(labels(STUDENT), labels(PERSON, rigidity=oc.NON_RIGID))
    assert violations == []


def test_the_violation_says_why_in_words_not_in_notation():
    violations, _, _ = check(labels(STUDENT), labels(PERSON, unity=oc.ANTI_UNITY))
    assert "not wholes" in violations[0].detail


# ─────────────────────────  what it does not know  ─────────────────────────


def test_an_unlabelled_class_produces_no_violation_and_is_counted():
    """A filter that silently checked a tenth of the hierarchy would report a clean result it
    never established."""
    violations, checked, skipped = oc.check(
        hierarchy(STUDENT, PERSON), {STUDENT: labels(STUDENT)},
    )
    assert violations == [] and (checked, skipped) == (0, 1)


# ─────────────────────────  the labelling stage  ─────────────────────────


def test_the_plain_answers_map_to_the_notation():
    parsed = oc.parse(
        '{"necessary": "always", "identity": "yes", "whole": "no", "dependent": "yes",'
        ' "why": "a role"}', {},
    )
    assert parsed["rigidity"] == oc.ANTI_RIGID
    assert parsed["identity"] == oc.CARRIES_IDENTITY
    assert parsed["unity"] == oc.ANTI_UNITY
    assert parsed["dependence"] == oc.DEPENDENT


def test_the_model_is_never_asked_in_jargon():
    """Asking in jargon gets an answer about the jargon."""
    rendered = oc.PROMPT.render(**oc.payload("Student", "Someone enrolled.", ["Person"]))
    for notation in ("+R", "~R", "anti-rigid", "OntoClean", "metaproperty"):
        assert notation not in rendered


@pytest.mark.parametrize("answer", [
    '{"necessary": "maybe", "identity": "yes", "whole": "yes", "dependent": "no"}',
    '{"necessary": "never", "identity": "probably", "whole": "yes", "dependent": "no"}',
    '{"necessary": "never", "identity": "yes", "whole": "yes"}',
    "not json",
])
def test_a_malformed_answer_is_refused(answer):
    with pytest.raises(ValueError):
        oc.parse(answer, {})


def test_labels_persist_and_come_back(tmp_path):
    conn = connect(tmp_path)
    oc.persist(conn, "v1", [labels(PERSON, why="a person stays one")])
    stored = oc.load(conn, "v1")
    assert stored[PERSON].rigidity == oc.RIGID and stored[PERSON].why


def test_labels_from_an_earlier_version_still_apply(tmp_path):
    """A metaproperty is a fact about the concept, not about the state of the ontology: a class
    rigid in v3 is rigid in v7, and re-asking would pay twice for one answer."""
    conn = connect(tmp_path)
    oc.persist(conn, "v3", [labels(PERSON)])
    assert PERSON in oc.load(conn, "v7")


def test_a_relabelling_in_this_version_wins(tmp_path):
    conn = connect(tmp_path)
    oc.persist(conn, "v3", [labels(PERSON, rigidity=oc.RIGID)])
    oc.persist(conn, "v7", [labels(PERSON, rigidity=oc.ANTI_RIGID)])
    assert oc.load(conn, "v7")[PERSON].rigidity == oc.ANTI_RIGID
    assert oc.load(conn, "v3")[PERSON].rigidity == oc.RIGID
