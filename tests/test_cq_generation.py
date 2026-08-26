from __future__ import annotations

import pytest

from onto_pipeline import cq
from onto_pipeline import cq_generation as gen
from onto_pipeline.cq_generation import Passage
from onto_pipeline.db import connect

ASKS = "SELECT ?kind WHERE { ?kind rdfs:subClassOf ?x . ?x skos:prefLabel 'Interview' . }"
TRIVIAL = "SELECT ?label WHERE { ?x skos:prefLabel ?label }"


def block(text: str, block_type: str = "paragraph", document_id="d1", page=1) -> dict:
    return {"text": text, "block_type": block_type, "document_id": document_id, "page": page}


def passage(stratum="definitions") -> Passage:
    return Passage(stratum, "d1", 3, "A focus group is a moderated group interview.")


def answer(question: str, sparql: str = ASKS, index: int = 1, language: str = "en") -> dict:
    return {"question": question, "sparql": sparql, "passage": index, "language": language}


# ─────────────────────────  step 1: the strata  ─────────────────────────


@pytest.mark.parametrize("text,stratum", [
    ("A focus group is defined as a moderated discussion.", "definitions"),
    ("The following types of interview are used.", "enumerations"),
    ("Participants must not be told the hypothesis.", "restrictions"),
    ("We then transcribed each recording.", "procedures"),
])
def test_a_passage_lands_in_the_stratum_its_cue_names(text, stratum):
    assert gen.stratum_of(block(text)) == stratum


def test_a_table_is_its_own_stratum_whatever_it_says():
    assert gen.stratum_of(block("anything", block_type="table")) == gen.TABLE_STRATUM


def test_an_ordinary_paragraph_belongs_to_no_stratum():
    """The strata are what make the question types possible; a paragraph that supports none of
    them is not a sample, it is filler."""
    assert gen.stratum_of(block("We recruited eight participants in June.")) is None


def test_restrictions_are_sampled_at_all():
    """A random sample of paragraphs contains almost none, and restrictive questions would
    then be impossible to ground in a citation."""
    blocks = [block("Nothing to see here.") for _ in range(200)]
    blocks.append(block("A participant cannot be enrolled twice."))
    assert "restrictions" in gen.sample(blocks, per_stratum=5)


def test_the_sample_is_deterministic_and_not_just_the_first_ones():
    blocks = [block(f"Item {index} is defined as a thing.") for index in range(40)]
    first = [p.text for p in gen.sample(blocks, per_stratum=5, seed=1)["definitions"]]
    again = [p.text for p in gen.sample(blocks, per_stratum=5, seed=1)["definitions"]]
    other = [p.text for p in gen.sample(blocks, per_stratum=5, seed=2)["definitions"]]
    assert first == again and first != other


def test_a_small_stratum_is_taken_whole():
    blocks = [block("X is defined as Y.")]
    assert len(gen.sample(blocks, per_stratum=12)["definitions"]) == 1


# ─────────────────────────  step 2: the prompt  ─────────────────────────


def test_the_prompt_says_what_the_type_demands_of_the_ontology():
    """A model asked for "a competency question" writes definitional ones and nothing else."""
    rendered = gen.PROMPT.render(**gen.payload("inferential", [passage()], ["Interview"], 10))
    assert "REASONER" in rendered and "only entailed" in rendered


def test_the_negative_type_is_told_what_the_open_world_means():
    rendered = gen.PROMPT.render(**gen.payload("negative", [passage()], [], 5))
    assert "silence, not a denial" in rendered


def test_parse_refuses_a_question_without_its_query():
    with pytest.raises(ValueError, match="SPARQL"):
        gen.parse('{"questions": [{"question": "What kinds of X?"}]}', {})


def test_parse_refuses_an_answer_that_is_not_json():
    with pytest.raises(ValueError, match="no JSON"):
        gen.parse("Here are some questions.", {})


# ─────────────────────────  step 3: the mechanical filter  ─────────────────────────


def screen(entries, existing=(), **kwargs):
    return gen.screen(
        {"definitional": entries}, {"definitional": [passage()]}, existing, **kwargs
    )


def test_a_question_a_single_triple_answers_is_dropped():
    """It asks whether one fact was written down, which any ontology with that fact passes
    regardless of whether it models the domain."""
    result = screen([answer("What is the label of Interview?", TRIVIAL)])
    assert result.kept == [] and "single triple" in result.dropped[0][1]


def test_a_question_whose_sparql_does_not_parse_is_dropped():
    """If it is not a query it cannot be a stopping criterion."""
    result = screen([answer("What kinds of interview?", "SELECT WHERE nonsense {")])
    assert result.kept == [] and "does not parse" in result.dropped[0][1]


def test_a_question_citing_nothing_is_dropped():
    result = screen([answer("What kinds of interview are there?", index=0)])
    assert result.kept == [] and "cites no passage" in result.dropped[0][1]


def test_a_question_citing_a_passage_it_was_not_shown_is_dropped():
    result = screen([answer("What kinds of interview are there?", index=7)])
    assert result.kept == []


def test_a_kept_question_carries_its_citation_with_the_page():
    result = screen([answer("What kinds of interview are there?")])
    assert result.kept[0].citation["page"] == 3
    assert result.kept[0].citation["document_id"] == "d1"


def test_a_duplicate_of_an_existing_question_is_dropped():
    result = screen([answer("What kinds of interview are there?")],
                    existing=["What kinds of interview are there?"])
    assert result.kept == [] and result.dropped[0][1] == "duplicate"


def test_near_duplicates_are_dropped_when_there_is_an_encoder():
    entries = [answer("What kinds of interview are there?"),
               answer("Which kinds of interview exist?")]
    without = screen(entries)
    with_encoder = screen(entries, similarity=lambda a, b: 0.95)
    assert len(without.kept) == 2, "the text fallback is weaker, and not silently so"
    assert len(with_encoder.kept) == 1


def test_a_kept_question_is_proposed_and_not_accepted():
    """The review is a person's, and it is one-time work rather than per-iteration work."""
    assert screen([answer("What kinds of interview are there?")]).kept[0].status == gen.PROPOSED


# ─────────────────────────  the quota  ─────────────────────────


def test_a_shortfall_is_reported_and_never_topped_up():
    """The inferential and negative types are the ones a model does not produce on its own, so
    a shortfall there is the finding — filling it with definitional questions would hide
    exactly what the quota exists to force."""
    kept = screen([answer("What kinds of interview are there?")]).kept
    missing = gen.shortfall(kept, {"definitional": 1, "inferential": 10, "negative": 6})
    assert missing == {"inferential": 10, "negative": 6}


def test_a_met_quota_reports_nothing():
    kept = screen([answer("What kinds of interview are there?")]).kept
    assert gen.shortfall(kept, {"definitional": 1}) == {}


# ─────────────────────────  it reaches the store  ─────────────────────────


def test_a_proposed_question_stores_and_can_be_accepted(tmp_path):
    conn = connect(tmp_path)
    kept = screen([answer("What kinds of interview are there?")]).kept
    cq.add(conn, kept)
    assert cq.load(conn, status=gen.PROPOSED)[0].origin == cq.GENERATED

    cq.decide(conn, [kept[0].id], cq.ACCEPTED)
    assert [q.id for q in cq.load(conn, status=cq.ACCEPTED)] == [kept[0].id]


def test_an_unknown_decision_is_refused(tmp_path):
    with pytest.raises(ValueError, match="accepted or discarded"):
        cq.decide(connect(tmp_path), ["cq_1"], "maybe")
