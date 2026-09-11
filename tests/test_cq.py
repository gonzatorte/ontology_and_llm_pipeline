from __future__ import annotations

import json

import pytest
from rdflib import Graph

from onto_pipeline import cq
from onto_pipeline.db import connect

SESSION = "test-1"

ONTOLOGY = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <http://example.org/onto#> .
:Technique a owl:Class .
:Interview a owl:Class ; rdfs:subClassOf :Technique .
"""

ANSWERED = (
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
    "SELECT ?s WHERE { ?s rdfs:subClassOf <http://example.org/onto#Technique> }"
)
UNANSWERED = (
    "PREFIX owl: <http://www.w3.org/2002/07/owl#> "
    "SELECT ?s WHERE { ?s owl:disjointWith ?other }"
)


@pytest.fixture
def graph():
    return Graph().parse(data=ONTOLOGY, format="turtle")


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path)


def question(identifier, sparql, **kwargs):
    return cq.CompetencyQuestion(
        id=identifier, question="?", sparql=sparql, **kwargs
    )


def test_a_question_whose_query_does_not_parse_is_rejected():
    """If it is not a query it cannot serve as a stopping criterion."""
    with pytest.raises(cq.MalformedQuery):
        cq.validate(question("cq1", "this is not SPARQL"))


def test_a_generated_question_without_a_citation_is_rejected():
    with pytest.raises(ValueError, match="citation"):
        cq.validate(question("cq1", ANSWERED, origin=cq.GENERATED))
    cq.validate(
        question("cq1", ANSWERED, origin=cq.GENERATED,
                 citation={"document_id": "d", "page": 4, "quote": "..."})
    )


def test_a_user_question_needs_no_citation():
    cq.validate(question("cq1", ANSWERED, origin=cq.USER))


def test_evaluation_separates_answered_from_unanswered(graph):
    evaluation = cq.evaluate(graph, [question("cq1", ANSWERED), question("cq2", UNANSWERED)])
    assert evaluation.passed == ["cq1"]
    assert evaluation.failed == ["cq2"]
    assert evaluation.pass_rate == pytest.approx(0.5)


def test_a_query_that_blows_up_at_run_time_is_an_error_not_a_failure():
    """A parse error is caught on import; this is the query that only breaks when run, and
    counting it as an honest 'unanswered' would hide a bug."""

    class Exploding:
        def query(self, _sparql):
            raise RuntimeError("evaluation blew up")

    evaluation = cq.evaluate(Exploding(), [question("cq1", ANSWERED)])
    assert evaluation.failed == [] and "cq1" in evaluation.errored
    assert evaluation.pass_rate == 0.0


def test_results_are_recorded_per_iteration(conn, graph):
    cq.add(conn, [question("cq1", ANSWERED), question("cq2", UNANSWERED)], session_id=SESSION)
    stored = cq.load(conn, session_id=SESSION)
    cq.record(conn, cq.evaluate(graph, stored, iteration=3), session_id=SESSION)
    rows = dict(conn.execute("SELECT cq_id, passed FROM cq_results WHERE iteration = 3"))
    assert rows == {"cq1": 1, "cq2": 0}


def test_delta_reports_what_a_change_won_and_lost():
    before = cq.Evaluation(iteration=1, passed=["cq1", "cq2"], failed=["cq3"])
    after = cq.Evaluation(iteration=2, passed=["cq2", "cq3"], failed=["cq1"])
    assert after.delta(before) == {"newly_passing": ["cq3"], "newly_failing": ["cq1"]}


def test_stopping_needs_the_rate_to_settle_not_just_reach_the_target():
    assert not cq.should_stop([0.5, 0.7, 0.95], target=0.9), "still rising"
    assert not cq.should_stop([0.95, 0.95], target=0.9), "too little history"
    assert not cq.should_stop([0.95, 0.95, 0.8], target=0.9), "below target"
    assert cq.should_stop([0.95, 0.92, 0.91], target=0.9)


def test_questions_are_imported_from_the_users_file(conn, tmp_path):
    path = tmp_path / "cq.json"
    path.write_text(
        json.dumps([
            {"id": "cq_a4_01", "question": "What kinds of technique exist?",
             "type": "definitional", "sparql": ANSWERED}
        ]),
        encoding="utf-8",
    )

    assert cq.add(conn, cq.read_file(path), session_id=SESSION) == 1
    stored = cq.load(conn, session_id=SESSION)
    assert (stored[0].origin, stored[0].cq_type) == (cq.USER, "definitional")
