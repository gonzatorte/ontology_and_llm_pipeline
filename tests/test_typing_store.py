from __future__ import annotations

from rdflib import Graph

from onto_pipeline import typing_store
from onto_pipeline.db import connect
from onto_pipeline.matching import Decision, Typing

SESSION = "test-1"

ONTOLOGY = """
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix : <http://example.org/onto#> .
:Technique a owl:Class ; skos:prefLabel "Technique"@en ; rdfs:label "Técnica"@es ;
    skos:definition "A systematic procedure."@en ; skos:altLabel "Method"@en .
:Subject a owl:Class ; skos:prefLabel "Subject"@en .
:Unlabelled a owl:Class .
"""


def graph():
    return Graph().parse(data=ONTOLOGY, format="turtle")


def test_targets_skip_classes_with_no_label():
    targets = typing_store.targets_from(graph(), "label")
    assert [t.label for t in targets] == ["Subject", "Technique"]


def test_a_class_arrives_with_every_name_it_has():
    """Cuál nombre quedó como preferido es un accidente de cómo se escribió la ontología —y
    `skos:prefLabel` existe para que una herramienta tenga qué mostrar—, así que el mapeo no
    puede depender de eso: la clase se compara por todos sus nombres."""
    technique = {t.label: t for t in typing_store.targets_from(graph(), "label")}["Technique"]

    assert technique.texts == ["Technique", "Técnica", "Method"]


def test_targets_honour_the_configured_comparison_text():
    by_label = {t.label: t for t in typing_store.targets_from(graph(), "gloss")}
    assert by_label["Technique"].texts == ["A systematic procedure."]
    assert by_label["Subject"].texts == ["Subject"], "no gloss, so the label is what there is"


def test_typings_are_stored_per_version_not_on_the_mention(tmp_path):
    """A typing is derived from one ontology version and recomputed when the TBox or the
    glosses change; the mention layer stays immutable except by extension (LAYERS)."""
    conn = connect(tmp_path)
    split = typing_store.persist_typings(conn, "v1", [
        Typing("m1", "c:Technique", 0.95, "auto"),
        Typing("m2", "c:Technique", 0.75, "grey"),
        Typing("m3", None, 0.30, "discarded"),
    ], session_id=SESSION)
    assert (split.typed, split.grey, split.orphan) == (1, 1, 1)
    assert split.orphan_rate == 1 / 3
    stored = conn.execute("SELECT COUNT(*) AS n FROM mention_typing WHERE version_id='v1'")
    assert stored.fetchone()["n"] == 3


def test_retyping_the_same_version_replaces_rather_than_accumulates(tmp_path):
    conn = connect(tmp_path)
    typing_store.persist_typings(conn, "v1", [Typing("m1", "c:T", 0.9, "auto")], session_id=SESSION)
    # discarded always carries a null iri: that is what the matcher returns below the zone.
    typing_store.persist_typings(conn, "v1", [Typing("m1", None, 0.4, "discarded")],
        session_id=SESSION)
    rows = [row["iri"] for row in
            conn.execute("SELECT iri FROM mention_typing WHERE version_id='v1'")]
    assert rows == [None]


def test_a_later_version_can_type_the_same_mention_differently(tmp_path):
    """The self-correcting loop of 4.3: a mention orphaned at iteration 3 can be typed at 8."""
    conn = connect(tmp_path)
    typing_store.persist_typings(conn, "v1", [Typing("m1", None, 0.3, "discarded")],
        session_id=SESSION)
    typing_store.persist_typings(conn, "v2", [Typing("m1", "c:Technique", 0.95, "auto")],
        session_id=SESSION)
    rows = dict(conn.execute("SELECT version_id, iri FROM mention_typing WHERE mention_id='m1'"))
    assert rows == {"v1": None, "v2": "c:Technique"}


def test_merges_chain_into_one_entity():
    entities = typing_store.entities_from([
        Decision("m1", "m2", "merge", "identical_proper_name"),
        Decision("m2", "m3", "merge", "identical_proper_name"),
        Decision("m4", "m5", "separate", "low_similarity"),
    ])
    assert len({entities["m1"], entities["m2"], entities["m3"]}) == 1
    assert "m4" not in entities, (
        "separate leaves a mention with its own identity (SEPARATE-UNTIL-CONFIRMED)"
    )


def test_grey_zone_mentions_are_marked_so_they_leave_the_functional_count(tmp_path):
    """Two duplicates with one value each look like confirmation of functionality; the state
    is what keeps them out of that count (ITER-APPLY)."""
    conn = connect(tmp_path)
    conn.executemany(
        "INSERT INTO mentions (id, session_id, document_id, page, surface_text, status) "
        "VALUES (?, ?, 'doc', 1, 'x', 'active')",
        [("m1", SESSION), ("m2", SESSION), ("m3", SESSION)],
    )

    typing_store.persist_entities(conn, {"m1": "m1", "m2": "m1"}, unresolved={"m3"},
        session_id=SESSION)
    rows = dict(conn.execute("SELECT id, status FROM mentions"))
    assert rows["m3"] == typing_store.POSSIBLE_DUPLICATE
    assert rows["m1"] == "active"
    assert dict(conn.execute("SELECT id, candidate_entity FROM mentions"))["m2"] == "m1"
