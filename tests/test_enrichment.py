from __future__ import annotations

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SKOS

from onto_pipeline import enrichment as en
from onto_pipeline.db import connect
from onto_pipeline.enrichment import Enrichment, Passage

IRI = "c:FocusGroup"


def block(text: str, document_id: str = "d1", block_id: str = "b1", page: int = 1) -> dict:
    return {"id": block_id, "document_id": document_id, "page": page, "text": text}


def passage(text: str, document_id: str = "d1", block_id: str = "b1") -> Passage:
    return Passage(document_id, block_id, 1, "is a", text, "focus group")


# ─────────────────────────  the cue filter  ─────────────────────────


@pytest.mark.parametrize("text,cue", [
    ("A focus group is a group interview run as one session.", "is a"),
    ("Focus group refers to a moderated discussion.", "refers to"),
    ("We define focus group as a moderated discussion.", "we define"),
    ("The focus group, also known as a group interview, is used widely.", "also known as"),
    ("Un focus group es una entrevista grupal.", "es un"),
])
def test_a_definitional_passage_is_recognized_by_its_cue(text, cue):
    assert en.definitional(text, ["focus group"]) == (cue, "focus group")


@pytest.mark.parametrize("text", [
    "We ran a focus group with eight participants.",
    "The focus group met on Tuesday.",
    "Participants were recruited for the focus group.",
])
def test_a_passage_that_merely_uses_the_term_is_not_definitional(text):
    """The filter is the cheap half of the stage: a corpus has far more paragraphs than a
    budget has requests."""
    assert en.definitional(text, ["focus group"]) is None


def test_a_line_break_inside_the_phrase_does_not_hide_it():
    assert en.definitional("A focus\ngroup is a moderated discussion.", ["focus group"])


def test_a_passage_naming_another_class_is_not_this_class_definition():
    assert en.definitional("An interview is a conversation.", ["focus group"]) is None


def test_passages_spread_across_documents_before_going_deep():
    """Five passages from one paper describe that paper's usage; the circularity control is
    about exactly that difference."""
    blocks = [
        block("A focus group is a group interview.", "d1", "b1"),
        block("The focus group is a moderated session.", "d1", "b2"),
        block("A focus group is a collective interview.", "d2", "b3"),
    ]
    chosen = en.passages_for(blocks, ["focus group"], limit=2)
    assert {item.document_id for item in chosen} == {"d1", "d2"}


def test_no_definitional_block_means_no_passages():
    assert en.passages_for([block("We ran a focus group.")], ["focus group"], limit=3) == []


# ─────────────────────────  the answer  ─────────────────────────


def test_no_improvement_is_a_normal_answer():
    parsed = en.parse('{"definition": null, "alt_labels": [], "why": "nothing new"}', {})
    assert parsed["definition"] is None and parsed["alt_labels"] == []


def test_a_definition_without_english_is_refused():
    with pytest.raises(ValueError, match="en"):
        en.parse('{"definition": {"es": "una cosa"}, "alt_labels": []}', {})


def test_an_answer_that_is_not_json_is_refused():
    with pytest.raises(ValueError, match="no JSON"):
        en.parse("I improved it for you.", {})


def test_a_synonym_the_passages_do_not_contain_is_dropped():
    """Checked mechanically, like `coref` checks its ids and `bridge` checks its classes. A
    synonym from the model's own knowledge may be right, but it would carry a provenance that
    does not hold — and the circularity control is built on that provenance meaning what it
    says."""
    answer = {"definition": None, "alt_labels": ["group interview", "panel study"], "why": ""}
    result = en.verified(
        answer, [passage("A focus group, also known as a group interview, is moderated.")],
        known=set(), iri=IRI,
    )
    assert result.alt_labels == ["group interview"] and result.dropped == ["panel study"]


def test_a_synonym_that_is_already_a_class_label_is_not_added():
    answer = {"definition": None, "alt_labels": ["Interview"], "why": ""}
    result = en.verified(answer, [passage("A focus group is an Interview run in a group.")],
                         known={"interview"}, iri=IRI)
    assert result.alt_labels == []


def test_the_contributing_documents_are_the_ones_the_passages_came_from():
    answer = {"definition": {"en": "A moderated group discussion.", "es": ""},
              "alt_labels": [], "why": ""}
    result = en.verified(
        answer,
        [passage("A focus group is a discussion.", "d1"),
         passage("A focus group is a session.", "d2", "b2")],
        known=set(), iri=IRI,
    )
    assert result.documents == ["d1", "d2"]


# ─────────────────────────  writing it  ─────────────────────────


def seeded() -> Graph:
    graph = Graph()
    graph.add((URIRef(IRI), SKOS.prefLabel, Literal("Focus Group", lang="en")))
    graph.add((URIRef(IRI), SKOS.definition, Literal("An old definition.", lang="en")))
    return graph


def test_the_definition_is_replaced_and_the_previous_graph_is_left_alone():
    before = seeded()
    after = en.apply(before, [Enrichment(
        IRI, definition={"en": "A moderated group discussion.", "es": "Una discusión grupal."},
        documents=["d1"],
    )])
    assert len(before) == 2, "the graph passed in is not modified"
    definitions = {str(o) for o in after.objects(URIRef(IRI), SKOS.definition)}
    assert definitions == {"A moderated group discussion.", "Una discusión grupal."}


def test_alt_labels_only_ever_accumulate():
    """A synonym the corpus attested once is still attested after a later iteration fails to
    find it again, and removing it would undo the matching the loop just gained."""
    first = en.apply(seeded(), [Enrichment(IRI, alt_labels=["group interview"], documents=["d1"])])
    second = en.apply(first, [Enrichment(IRI, alt_labels=["collective interview"],
                                         documents=["d2"])])
    assert {str(o) for o in second.objects(URIRef(IRI), SKOS.altLabel)} == {
        "group interview", "collective interview"
    }


def test_an_enrichment_that_changed_nothing_writes_nothing():
    after = en.apply(seeded(), [Enrichment(IRI, documents=["d1"])])
    assert len(after) == 2 and not list(after.objects(URIRef(IRI), SKOS.historyNote))


def test_the_contributing_documents_are_recorded_in_the_graph_too():
    after = en.apply(seeded(), [Enrichment(IRI, alt_labels=["group interview"],
                                           documents=["d1", "d2"])])
    note = str(next(after.objects(URIRef(IRI), SKOS.historyNote)))
    assert "d1" in note and "d2" in note


def test_every_annotation_it_writes_is_declared_by_the_seed():
    """An undeclared annotation property leaves OWL 2 DL, and the symptom is not an error —
    it is ELK quietly dropping to a fragment too small to filter with."""
    from onto_pipeline.seed import DECLARED_ANNOTATIONS

    assert set(en._PREDICATES.values()) <= set(DECLARED_ANNOTATIONS)


# ─────────────────────────  circularity  ─────────────────────────


def typed(conn, mention_id, iri, document_id, version="v1"):
    conn.execute(
        "INSERT INTO mentions (id, document_id, page, surface_text, status) "
        "VALUES (?, ?, 1, ?, 'typed')", (mention_id, document_id, "focus group"),
    )
    conn.execute(
        "INSERT INTO mention_typing (mention_id, version_id, iri, score, zone) "
        "VALUES (?, ?, ?, 0.97, 'auto')", (mention_id, version, iri),
    )
    conn.commit()


def test_a_match_against_a_document_that_wrote_the_gloss_is_flagged(tmp_path):
    """Not an error and not discarded: the one that must not be counted as independent
    evidence of coverage (§4.3)."""
    conn = connect(tmp_path)
    from onto_pipeline import typing_store

    typing_store.install(conn)
    en.persist(conn, "v1", [Enrichment(IRI, alt_labels=["group interview"], documents=["d1"])])
    typed(conn, "m1", IRI, "d1")
    typed(conn, "m2", IRI, "d2")

    flagged = en.circular_matches(conn, "v1")
    assert [row["mention_id"] for row in flagged] == ["m1"]


def test_a_contribution_from_an_earlier_version_still_counts(tmp_path):
    """Once a document has described a class, every later match of that document against it
    carries the same dependency."""
    conn = connect(tmp_path)
    from onto_pipeline import typing_store

    typing_store.install(conn)
    en.persist(conn, "v1", [Enrichment(IRI, alt_labels=["group interview"], documents=["d1"])])
    typed(conn, "m1", IRI, "d1", version="v4")
    assert len(en.circular_matches(conn, "v4")) == 1


def test_a_class_the_corpus_did_not_improve_contributes_nothing(tmp_path):
    conn = connect(tmp_path)
    en.persist(conn, "v1", [Enrichment(IRI, documents=["d1"])])
    assert en.contributors(conn, IRI) == []
