from __future__ import annotations

import pytest

from onto_pipeline import induction
from onto_pipeline.db import connect
from onto_pipeline.induction import Cluster, Proposal


def unit(*values: float) -> list[float]:
    from onto_pipeline.matching import normalize

    return normalize(list(values))


def cluster_of(pairs, threshold=0.9, min_support=2):
    ids = [name for name, _ in pairs]
    return induction.cluster(
        ids, ids, [vector for _, vector in pairs],
        threshold=threshold, min_support=min_support,
    )


def test_similar_mentions_group_and_lone_ones_do_not():
    clusters = cluster_of([
        ("m1", unit(1, 0, 0)), ("m2", unit(0.98, 0.2, 0)),   # together
        ("m3", unit(0, 1, 0)),                                # alone
    ])
    assert [c.mention_ids for c in clusters] == [["m1", "m2"]]


def test_a_cluster_below_min_support_is_not_a_class():
    """One mention proposing a class is noise, and 6.6 rejects a level with one subclass."""
    pairs = [("m1", unit(1, 0)), ("m2", unit(0.99, 0.1)), ("m3", unit(0.98, 0.15))]
    assert cluster_of(pairs, min_support=4) == []
    assert len(cluster_of(pairs, min_support=3)[0].mention_ids) == 3


def test_single_link_chains_through_an_intermediate_phrasing():
    """A concept's wordings form a chain: two ends need not be close to each other, only to
    something between them."""
    clusters = cluster_of([
        ("a", unit(1, 0, 0)), ("b", unit(0.95, 0.31, 0)), ("c", unit(0.8, 0.6, 0)),
    ], threshold=0.9)
    assert [sorted(c.mention_ids) for c in clusters] == [["a", "b", "c"]]


def test_clusters_are_ordered_by_support():
    clusters = cluster_of([
        ("m1", unit(1, 0)), ("m2", unit(0.99, 0.1)), ("m3", unit(0.995, 0.05)),
        ("n1", unit(0, 1)), ("n2", unit(0.1, 0.99)),
    ])
    assert [c.support for c in clusters] == [3, 2]


def test_clustering_is_deterministic():
    pairs = [("m1", unit(1, 0)), ("m2", unit(0.99, 0.1)), ("m3", unit(0, 1))]
    assert [c.id for c in cluster_of(pairs)] == [c.id for c in cluster_of(pairs)]


def test_the_model_may_say_the_group_is_not_a_class():
    """Expected sometimes, and more useful than a label invented to fit."""
    parsed = induction.parse('{"is_a_class": false, "label": "", "criterion": ""}', {})
    assert parsed == {"is_a_class": False}


def test_a_proposal_without_a_division_criterion_is_refused():
    """6.6 makes an undeclared division criterion a rejection condition of its own."""
    with pytest.raises(ValueError, match="criterion"):
        induction.parse('{"is_a_class": true, "label": "Data Repository", "gloss": "A store."}', {})


def test_a_proposal_without_a_label_is_refused():
    with pytest.raises(ValueError, match="label"):
        induction.parse('{"is_a_class": true, "criterion": "it stores data"}', {})


def test_parse_rejects_an_answer_that_is_not_json():
    with pytest.raises(ValueError, match="no JSON"):
        induction.parse("I grouped them for you.", {})


def test_the_prompt_shows_the_nearest_class_so_the_model_can_differentiate():
    rendered = induction.PROMPT.render(**induction.payload(Cluster(
        id="c1", mention_ids=["m1", "m2"], surfaces=["data repository", "repositories"],
        nearest_label="Record", nearest_gloss="An entry that documents something.",
    )))
    assert "data repository" in rendered
    assert "Record" in rendered and "An entry that documents" in rendered


def test_repeated_surfaces_are_shown_once():
    payload = induction.payload(Cluster(
        id="c1", mention_ids=["m1", "m2", "m3"],
        surfaces=["repositories", "repositories", "data repository"],
    ))
    assert payload["phrases"].count("- repositories") == 1


def test_proposals_persist_with_their_supporting_mentions(tmp_path):
    conn = connect(tmp_path)
    proposal = Proposal(
        cluster_id="c1", label="Data Repository", gloss="A store for datasets.",
        criterion="holds datasets rather than documenting one study", support=3,
        mention_ids=["m1", "m2", "m3"], nearest_iri="c:Record", nearest_score=0.61,
    )
    induction.persist(conn, "v1", [proposal])

    stored = induction.load(conn, "v1")
    assert [row["label"] for row in stored] == ["Data Repository"]
    assert stored[0]["status"] == induction.PROPOSED
    assert stored[0]["nearest_iri"] == "c:Record", "a candidate parent, not an asserted one"
    linked = conn.execute(
        "SELECT COUNT(*) AS n FROM proposed_class_mentions WHERE proposed_id = ?",
        (stored[0]["id"],),
    ).fetchone()
    assert linked["n"] == 3


def test_reproposing_against_the_same_version_replaces(tmp_path):
    conn = connect(tmp_path)
    first = Proposal("c1", "Data Repository", "g", "c", 2, ["m1", "m2"])
    second = Proposal("c1", "Digital Repository", "g", "c", 2, ["m1", "m2"])
    induction.persist(conn, "v1", [first])
    induction.persist(conn, "v1", [second])
    assert [row["label"] for row in induction.load(conn, "v1")] == ["Digital Repository"]


# ────────  el falso huérfano que se vuelve clase espuria (§12.1)  ────────


def proposal(label: str, cluster: str = "c1"):
    return induction.Proposal(cluster, label, "g", "criterio", 2, ["m1", "m2"])


def exact(left, right):
    """Similitud de juguete: 1,0 si las etiquetas coinciden sin distinguir mayúsculas."""
    return [[1.0 if a.lower() == b.lower() else 0.1 for b in right] for a in left]


def test_a_proposal_that_names_an_existing_class_is_flagged():
    """El matcher falló sobre las menciones sueltas; el nombre del grupo sí coincide. Medido
    sobre MaterioMiner: `Test specimen` con coseno 1,00 contra una clase idéntica."""
    found = induction.redundant([proposal("Test specimen")], ["Test specimen", "Aging"],
                                exact, threshold=0.9)
    assert found == {"c1": "Test specimen"}


def test_case_does_not_save_it_from_being_a_duplicate():
    found = induction.redundant([proposal("Grain Boundary")], ["Grain boundary"],
                                exact, threshold=0.9)
    assert found


def test_a_genuinely_new_class_is_not_flagged():
    found = induction.redundant([proposal("Precipitate-Free Zone")], ["Aging", "Carbon"],
                                exact, threshold=0.9)
    assert found == {}


def test_nothing_to_compare_against_is_not_a_duplicate():
    assert induction.redundant([proposal("X")], [], exact, threshold=0.9) == {}


def test_the_flagged_proposals_are_not_removed():
    """Que la inducción reencuentre una clase que ya está es un diagnóstico sobre el matcher.
    Borrarlo en silencio pierde la única señal de que pasó."""
    proposals = [proposal("Test specimen"), proposal("Nueva", "c2")]
    induction.redundant(proposals, ["Test specimen"], exact, threshold=0.9)
    assert len(proposals) == 2


def test_two_clusters_naming_the_same_existing_class_are_both_flagged():
    """Pasó: dos grupos distintos produjeron `Grain Boundary`."""
    found = induction.redundant(
        [proposal("Grain boundary", "c1"), proposal("Grain boundary", "c2")],
        ["Grain boundary"], exact, threshold=0.9,
    )
    assert set(found) == {"c1", "c2"}
