from __future__ import annotations

import pytest

from onto_pipeline import bridging
from onto_pipeline.bridging import Bridge, Candidate
from onto_pipeline.db import connect
from onto_pipeline.matching import Target

TECHNIQUE = Target(iri="c:Technique", label="Technique",
                   gloss="A procedure applied within a methodological strategy.")
SUBJECT = Target(iri="c:Subject", label="Subject",
                 gloss="A subject studied by a methodological strategy.")


def rows(*pairs):
    return [{"id": identifier, "surface_text": surface} for identifier, surface in pairs]


def vectors_for(surfaces, table):
    return [table[surface] for surface in surfaces]


def test_orphans_are_grouped_by_surface_so_one_answer_covers_every_occurrence():
    """The unit is the concept, not the mention: three occurrences are one question."""
    table = {"focus group": [1.0, 0.0], "Technique": [1.0, 0.0], "Subject": [0.0, 1.0]}
    items = bridging.candidates(
        rows(("m1", "focus group"), ("m2", "focus group"), ("m3", "focus group")),
        [TECHNIQUE, SUBJECT],
        vectors_for(["focus group"] * 3, table),
        vectors_for(["Technique", "Subject"], table),
        n_candidates=5, min_score=0.4,
    )

    assert len(items) == 1
    assert items[0].support == 3
    assert items[0].mention_ids == ["m1", "m2", "m3"]


def test_a_phrase_with_no_class_anywhere_near_is_never_asked_about():
    """Offering five classes none of which is close invites the forced connection."""
    table = {"open access repository": [0.0, 1.0], "Technique": [1.0, 0.0],
             "Subject": [0.9, 0.1]}
    items = bridging.candidates(
        rows(("m1", "open access repository")), [TECHNIQUE, SUBJECT],
        vectors_for(["open access repository"], table),
        vectors_for(["Technique", "Subject"], table),
        n_candidates=5, min_score=0.45,
    )

    assert items == []


def test_the_candidate_list_is_capped_and_ordered_by_score():
    table = {"interview": [1.0, 0.2], "Technique": [1.0, 0.0], "Subject": [0.0, 1.0]}
    items = bridging.candidates(
        rows(("m1", "interview")), [TECHNIQUE, SUBJECT],
        vectors_for(["interview"], table),
        vectors_for(["Technique", "Subject"], table),
        n_candidates=1, min_score=0.0,
    )

    assert [target.iri for _, target in items[0].targets] == ["c:Technique"]


def payload_for(*targets):
    return bridging.payload(
        Candidate(surface="focus group", mention_ids=["m1"],
                  targets=[(0.8, target) for target in targets])
    )


def test_the_prompt_carries_the_definition_not_only_the_name():
    body = payload_for(TECHNIQUE)["candidates"]

    assert "c:Technique" in body
    assert "A procedure applied" in body


def test_a_bridge_is_parsed_when_the_class_was_one_of_the_candidates():
    answer = bridging.parse(
        '{"relation": "subclass_of", "class_id": "c:Technique", "why": "a kind of procedure"}',
        payload_for(TECHNIQUE, SUBJECT),
    )

    assert answer["relation"] == bridging.SUBCLASS_OF
    assert answer["class_id"] == "c:Technique"


def test_a_class_the_model_was_not_offered_is_refused():
    """Mechanically verifiable, like coreference's check that every grouped id exists."""
    with pytest.raises(ValueError, match="not among the candidates"):
        bridging.parse(
            '{"relation": "instance_of", "class_id": "c:Invented"}',
            payload_for(TECHNIQUE),
        )


def test_declining_to_connect_is_a_valid_answer_and_makes_no_bridge():
    answer = bridging.parse('{"relation": "none", "class_id": null}', payload_for(TECHNIQUE))
    candidate = Candidate(surface="focus group", mention_ids=["m1"], targets=[(0.8, TECHNIQUE)])

    assert answer == {"relation": bridging.NONE}
    assert bridging.bridges_from(candidate, answer) is None


def test_an_unknown_relation_is_a_parse_error_rather_than_a_silent_none():
    with pytest.raises(ValueError, match="unknown relation"):
        bridging.parse('{"relation": "sort_of", "class_id": "c:Technique"}',
                       payload_for(TECHNIQUE))


def test_a_bridge_carries_world_knowledge_provenance():
    """No citation is possible, so the evidence filter must not apply to it (6.2b)."""
    candidate = Candidate(surface="focus group", mention_ids=["m1", "m2"],
                          targets=[(0.63, TECHNIQUE)])
    bridge = bridging.bridges_from(
        candidate, {"relation": "subclass_of", "class_id": "c:Technique", "why": "a procedure"}
    )

    assert bridge.provenance == bridging.WORLD_KNOWLEDGE
    assert bridge.score == pytest.approx(0.63)
    assert bridge.mention_ids == ["m1", "m2"]


def test_a_bridged_mention_is_no_longer_an_orphan_for_induction(tmp_path):
    """The whole point of the stage sitting where it does: a mention the seed covers must not
    also become a proposed class."""
    conn = connect(tmp_path)
    bridge = Bridge(surface="focus group", target_iri="c:Technique", relation="subclass_of",
                    why="", score=0.6, mention_ids=["m1", "m2"])
    bridging.persist(conn, "v1", [bridge])

    assert bridging.bridged_mentions(conn, "v1") == {"m1", "m2"}
    assert bridging.bridged_mentions(conn, "v2") == set()


def test_a_rejected_bridge_returns_its_mentions_to_induction(tmp_path):
    conn = connect(tmp_path)
    bridge = Bridge(surface="focus group", target_iri="c:Technique", relation="subclass_of",
                    why="", score=0.6, mention_ids=["m1"])
    bridging.persist(conn, "v1", [bridge])
    conn.execute("UPDATE bridges SET status = ?", (bridging.REJECTED,))
    conn.commit()

    assert bridging.bridged_mentions(conn, "v1") == set()


def test_rerunning_replaces_the_version_s_bridges_rather_than_accumulating(tmp_path):
    conn = connect(tmp_path)
    first = Bridge(surface="focus group", target_iri="c:Technique", relation="subclass_of",
                   why="", score=0.6, mention_ids=["m1"])
    second = Bridge(surface="field note", target_iri="c:Technique", relation="subclass_of",
                    why="", score=0.5, mention_ids=["m2"])
    bridging.persist(conn, "v1", [first])
    bridging.persist(conn, "v1", [second])

    stored = bridging.load(conn, "v1")
    assert [row["surface"] for row in stored] == ["field note"]


def test_a_bridge_id_is_derived_from_its_content():
    left = Bridge(surface="focus group", target_iri="c:Technique", relation="subclass_of",
                  why="one wording", score=0.6, mention_ids=["m1"])
    right = Bridge(surface="focus group", target_iri="c:Technique", relation="subclass_of",
                   why="another wording", score=0.9, mention_ids=["m1", "m2"])
    other = Bridge(surface="focus group", target_iri="c:Technique", relation="instance_of",
                   why="one wording", score=0.6, mention_ids=["m1"])

    assert left.id == right.id
    assert left.id != other.id
