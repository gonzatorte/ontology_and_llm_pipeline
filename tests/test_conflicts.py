from __future__ import annotations

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDFS

from onto_pipeline import conflicts, mapping
from onto_pipeline.db import connect
from onto_pipeline.mapping import MappingRules, MentionRow

A, B, C = "c:Technique", "c:Organization", "c:Interview"
LABELS = {A: "Technique", B: "Organization", C: "Interview"}


def member(mention_id: str, document_id: str, surface: str = "focus group") -> dict:
    return {"id": mention_id, "document_id": document_id, "surface_text": surface}


def detect(typings, groups=None, incompatible=frozenset()):
    groups = groups or {"m1": [member("m1", "d12"), member("m2", "d47")]}
    found = conflicts.detect(groups, typings, accepted_zones=["auto"])
    return conflicts.classify(found, set(incompatible))


# ─────────────────────────  detection  ─────────────────────────


def test_two_documents_typing_one_entity_two_ways_is_a_conflict():
    found = detect({"m1": (A, "auto"), "m2": (B, "auto")})
    assert len(found) == 1
    assert found[0].classes == sorted([A, B]) and found[0].documents == ["d12", "d47"]


def test_agreement_is_not_a_conflict():
    assert detect({"m1": (A, "auto"), "m2": (A, "auto")}) == []


def test_a_typing_in_a_rejected_zone_does_not_create_a_conflict():
    """The grey zone does not type until it is answered; it must not disagree either."""
    assert detect({"m1": (A, "auto"), "m2": (B, "grey")}) == []


def test_mentions_of_different_entities_do_not_conflict():
    """A disagreement between two mentions of different things is not a disagreement."""
    groups = {"m1": [member("m1", "d12")], "m2": [member("m2", "d47")]}
    assert detect({"m1": (A, "auto"), "m2": (B, "auto")}, groups=groups) == []


def test_a_conflict_the_tbox_calls_incompatible_reaches_the_user():
    found = detect({"m1": (A, "auto"), "m2": (B, "auto")},
                   incompatible={frozenset((A, B))})
    assert found[0].breaks_reasoner and found[0].incompatible == [(B, A)], "sorted by IRI"


def test_only_the_pairs_that_collided_are_asked_about():
    """Every pair of classes is quadratic in the inventory and almost entirely irrelevant."""
    found = detect({"m1": (A, "auto"), "m2": (B, "auto")})
    assert conflicts.candidate_pairs(found) == [(B, A)] or \
        conflicts.candidate_pairs(found) == [tuple(sorted((A, B)))]


def test_a_conflict_the_reasoner_would_not_break_on_is_only_a_fact():
    """The volume filter: deciding case by case is the manual review BRANCH-ONLY-REVIEW rules out.
    """
    assert not detect({"m1": (A, "auto"), "m2": (B, "auto")})[0].breaks_reasoner


# ─────────────────────────  disjointness  ─────────────────────────


def disjoint_graph() -> Graph:
    graph = Graph()
    graph.add((URIRef(A), OWL.disjointWith, URIRef(B)))
    graph.add((URIRef(C), RDFS.subClassOf, URIRef(A)))
    return graph


def test_disjointness_is_read_in_both_directions():
    """It is symmetric. Reading it one way round is a bug this project already made once."""
    pairs = conflicts.asserted_incompatibilities(disjoint_graph())
    assert frozenset((A, B)) in pairs


def test_disjointness_is_inherited_by_subclasses():
    pairs = conflicts.asserted_incompatibilities(disjoint_graph())
    assert frozenset((C, B)) in pairs, "a subclass of A is disjoint with B too"


def test_a_subclass_cycle_does_not_hang_the_closure():
    graph = disjoint_graph()
    graph.add((URIRef(A), RDFS.subClassOf, URIRef(C)))
    assert frozenset((C, B)) in conflicts.asserted_incompatibilities(graph)


def test_an_ontology_declaring_no_disjointness_yields_no_incompatibility():
    assert conflicts.asserted_incompatibilities(Graph()) == set()


# ─────────────────────────  what reaches the user  ─────────────────────────


def test_only_the_breaking_conflicts_become_review_items():
    harmless = detect({"m1": (A, "auto"), "m2": (B, "auto")})
    items = conflicts.findings(harmless, LABELS, pattern_threshold=99)
    assert items == [], "the harmless ones are notarized without asking"


def test_a_breaking_conflict_names_the_incompatible_pair():
    found = detect({"m1": (A, "auto"), "m2": (B, "auto")}, incompatible={frozenset((A, B))})
    item = conflicts.findings(found, LABELS, pattern_threshold=99)[0]
    assert item.kind == conflicts.FACTUAL_CONFLICT
    assert "Organization / Technique" in item.summary


def test_a_recurring_pair_becomes_one_question_about_the_tbox():
    """6.4's practical rule: the individual conflict is settled in the ABox, the pattern over
    a pair is what triggers the TBox decision."""
    many = []
    for index in range(4):
        groups = {f"a{index}": [member(f"a{index}", "d1"), member(f"b{index}", "d2")]}
        many += detect({f"a{index}": (A, "auto"), f"b{index}": (B, "auto")}, groups=groups)
    items = conflicts.findings(many, LABELS, pattern_threshold=3)
    assert [item.kind for item in items] == [conflicts.CONFLICT_PATTERN]
    assert "4 entities" in items[0].summary


# ─────────────────────────  marks  ─────────────────────────


def test_refuted_and_misextracted_are_kept_apart(tmp_path):
    """They look identical in an interface and are opposite signals; merging them loses the
    only free source of extraction-error labels."""
    conn = connect(tmp_path)
    conflicts.mark(conn, ["m1"], conflicts.REFUTED, "the paper is wrong about this")
    conflicts.mark(conn, ["m2"], conflicts.MISEXTRACTED, "the sentence never says this")
    assert conflicts.marks(conn, conflicts.REFUTED) == {"m1": conflicts.REFUTED}
    assert conflicts.marks(conn, conflicts.MISEXTRACTED) == {"m2": conflicts.MISEXTRACTED}


def test_an_unknown_mark_is_refused(tmp_path):
    with pytest.raises(ValueError, match="refuted"):
        conflicts.mark(connect(tmp_path), ["m1"], "wrong")


def test_a_mark_changes_the_rules_hash(tmp_path):
    """A decision that changed no rule would be a decision the ABox never notices:
    regeneration is idempotent over (state, rules)."""
    conn = connect(tmp_path)
    rules = MappingRules()
    before = rules.with_exceptions(conflicts.as_exceptions(conn)).rules_hash()
    conflicts.mark(conn, ["m1"], conflicts.REFUTED)
    after = rules.with_exceptions(conflicts.as_exceptions(conn)).rules_hash()
    assert before != after


def test_misextractions_export_as_the_evaluation_set_shape(tmp_path):
    import json

    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO mentions (id, document_id, page, span_start, span_end, surface_text, "
        "status) VALUES ('m2', 'd1', 3, 10, 20, 'university based', 'extracted')"
    )
    conn.commit()
    conflicts.mark(conn, ["m2"], conflicts.MISEXTRACTED, "two words joined by the parser")

    line = json.loads(conflicts.export_misextractions(conn))
    assert line["error"] == conflicts.MISEXTRACTED and line["start"] == 10
    assert line["document_id"] == "d1"


# ─────────────────────  the policies, in regeneration  ─────────────────────


def rows() -> list[MentionRow]:
    return [
        MentionRow("m1", "d12", 1, "focus group", 0, 5, "e1", "extracted"),
        MentionRow("m2", "d47", 1, "focus group", 6, 11, "e1", "extracted"),
        MentionRow("m3", "d47", 2, "focus group", 12, 17, "e1", "extracted"),
    ]


TYPINGS = {"m1": (A, "auto"), "m2": (B, "auto"), "m3": (B, "auto")}


def types_of(result) -> set[str]:
    from rdflib.namespace import RDF

    return {
        str(quad[2]) for quad in result.dataset.quads((None, RDF.type, None, None))
        if str(quad[2]) in (A, B, C)
    }


def test_notarize_keeps_both_facts_and_flags_the_disagreement():
    """The silent default, because it is the only policy that destroys no information."""
    result = mapping.regenerate(rows(), TYPINGS, MappingRules(conflict_policy=mapping.NOTARIZE))
    assert types_of(result) == {A, B} and result.n_conflicts == 1
    assert any(quad[1] == mapping.NOTARIZED for quad in result.dataset.quads())


def test_notarizing_is_distinguishable_from_belonging_to_two_classes():
    agreed = mapping.regenerate(rows(), {"m1": (A, "auto")}, MappingRules())
    assert not any(quad[1] == mapping.NOTARIZED for quad in agreed.dataset.quads())


def test_force_without_a_named_winner_takes_the_best_attested_class():
    result = mapping.regenerate(rows(), TYPINGS, MappingRules(conflict_policy=mapping.FORCE))
    assert types_of(result) == {B}, "two mentions support B against one for A"


def test_a_per_case_exception_names_the_winner():
    rules = MappingRules(conflict_policy=mapping.FORCE).with_exceptions({"entity:m1": f"force:{A}"})
    assert types_of(mapping.regenerate(rows(), TYPINGS, rules)) == {A}


def test_refute_leaves_the_individual_untyped_and_says_so():
    result = mapping.regenerate(rows(), TYPINGS, MappingRules(conflict_policy=mapping.REFUTE))
    assert types_of(result) == set() and result.n_untyped == 1
    assert any(quad[1] == mapping.REFUTED_TYPES for quad in result.dataset.quads())


def test_a_refuted_mention_leaves_the_abox():
    rules = MappingRules().with_exceptions({"mention:m1": conflicts.REFUTED})
    result = mapping.regenerate(rows(), TYPINGS, rules)
    assert result.n_excluded == 1
    assert types_of(result) == {B}, "the refuted assertion is gone, so there is no conflict"


def test_a_misextracted_mention_leaves_the_abox_too():
    rules = MappingRules().with_exceptions({"mention:m2": conflicts.MISEXTRACTED})
    assert mapping.regenerate(rows(), TYPINGS, rules).n_excluded == 1


# ─────────────────  subsumption is not disagreement  ─────────────────


def hierarchy() -> dict[str, set[str]]:
    return {C: {A}}          # Interview is a kind of Technique


def test_two_levels_of_one_hierarchy_are_not_a_conflict():
    """One fact stated at two levels of detail. Counting it would fill the report with the
    hierarchy arguing with itself."""
    found = conflicts.detect(
        {"m1": [member("m1", "d12"), member("m2", "d47")]},
        {"m1": (A, "auto"), "m2": (C, "auto")},
        accepted_zones=["auto"], above=hierarchy(),
    )
    assert found == []


def test_the_closure_is_transitive_and_survives_a_cycle():
    graph = Graph()
    graph.add((URIRef(C), RDFS.subClassOf, URIRef(A)))
    graph.add((URIRef(A), RDFS.subClassOf, URIRef(B)))
    graph.add((URIRef(B), RDFS.subClassOf, URIRef(C)))
    assert B in conflicts.ancestors(graph)[C]


def test_an_entailed_supertype_is_not_a_second_opinion_in_the_abox():
    rows = [
        MentionRow("m1", "d12", 1, "interview", 0, 5, "e1", "extracted"),
        MentionRow("m2", "d47", 1, "interview", 6, 11, "e1", "extracted"),
    ]
    result = mapping.regenerate(
        rows, {"m1": (A, "auto"), "m2": (C, "auto")}, MappingRules(), hierarchy()
    )
    assert result.n_conflicts == 0
    assert not any(quad[1] == mapping.NOTARIZED for quad in result.dataset.quads())
    assert types_of(result) == {A, C}, "both types are still asserted; only the flag is not"
