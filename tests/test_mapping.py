from __future__ import annotations

import pytest
from rdflib import URIRef
from rdflib.namespace import OWL, RDF

from onto_pipeline import mapping, versioning
from onto_pipeline.db import connect
from onto_pipeline.mapping import MappingRules, MentionRow
from onto_pipeline.typing_store import POSSIBLE_DUPLICATE

INSTITUTION = "https://ontology.local/id/institution"
TOOL = "https://ontology.local/id/tool"


def row(identifier, document, text="Genome Canada", entity=None, status="ok", page=1):
    return MentionRow(
        id=identifier, document_id=document, page=page, surface_text=text,
        span_start=0, span_end=len(text), candidate_entity=entity, status=status,
    )


def individuals(result):
    return {
        str(subject)
        for subject, _, obj, _ in result.dataset.quads((None, RDF.type, OWL.NamedIndividual, None))
    }


def test_an_entity_keeps_its_iri_when_a_new_document_adds_a_mention():
    """The frequent case: every iteration brings more mentions. Deriving the IRI from the whole
    group would change an entity's identity for having become better attested."""
    rules = MappingRules()
    before = mapping.regenerate([row("m17", "d1", entity="m42"),
                                 row("m42", "d2", entity="m42")], {}, rules)
    after = mapping.regenerate([row("m17", "d1", entity="m42"),
                                row("m42", "d2", entity="m42"),
                                row("m91", "d3", entity="m42")], {}, rules)

    assert individuals(before) == individuals(after)
    assert len(individuals(after)) == 1


def test_the_iri_does_not_depend_on_which_member_the_union_find_elected():
    """The stored entity key is one member's id, not necessarily the smallest."""
    rules = MappingRules()
    keyed_low = mapping.regenerate(
        [row("m17", "d1", entity="m17"), row("m42", "d2", entity="m17")], {}, rules
    )
    keyed_high = mapping.regenerate(
        [row("m17", "d1", entity="m42"), row("m42", "d2", entity="m42")], {}, rules
    )

    assert individuals(keyed_low) == individuals(keyed_high)


def test_splitting_a_group_gives_the_separated_half_a_new_identity():
    rules = MappingRules()
    joined = mapping.regenerate([row("m17", "d1", entity="m17"),
                                 row("m91", "d2", entity="m17")], {}, rules)
    split = mapping.regenerate([row("m17", "d1", entity="m17"),
                                row("m91", "d2", entity="m91")], {}, rules)

    assert individuals(joined) < individuals(split)
    assert len(individuals(split)) == 2


def test_a_mention_no_merge_touched_is_its_own_individual():
    """Separate individuals until confirmed (SEPARATE-UNTIL-CONFIRMED): a singleton group is what
    that looks like.
    """
    rows = [row("m1", "d1"), row("m2", "d2", text="NVivo")]
    result = mapping.regenerate(rows, {}, MappingRules())

    assert len(individuals(result)) == 2


def test_only_the_accepted_zones_produce_a_type():
    """The grey zone does not type until it is answered: 6.2's conservative policy in the ABox."""
    rows = [row("m1", "d1"), row("m2", "d2", text="NVivo")]
    typings = {"m1": (INSTITUTION, "auto"), "m2": (TOOL, "grey")}

    strict = mapping.regenerate(rows, typings, MappingRules())
    permissive = mapping.regenerate(rows, typings, MappingRules(type_from=("auto", "grey")))

    assert (strict.n_typed, strict.n_untyped) == (1, 1)
    assert (permissive.n_typed, permissive.n_untyped) == (2, 0)


def test_a_mention_typed_in_a_rejected_zone_is_still_an_individual():
    """It exists and it has provenance; what is missing is only its class."""
    result = mapping.regenerate(
        [row("m1", "d1")], {"m1": (INSTITUTION, "grey")}, MappingRules()
    )

    assert len(individuals(result)) == 1
    assert (None, RDF.type, URIRef(INSTITUTION)) not in result.dataset.graph(
        URIRef(mapping.ONTO + "abox")
    )


def test_an_unresolved_duplicate_is_marked_rather_than_resolved():
    """The functional-property support count reads this flag (6.8)."""
    result = mapping.regenerate(
        [row("m1", "d1", status=POSSIBLE_DUPLICATE)], {}, MappingRules()
    )

    assert result.n_unresolved == 1
    marked = list(result.dataset.quads((None, mapping.UNRESOLVED, None, None)))
    assert len(marked) == 1


def test_provenance_partitions_by_document():
    result = mapping.regenerate(
        [row("m1", "d1", entity="m1"), row("m2", "d2", entity="m1")], {}, MappingRules()
    )
    names = {str(graph.identifier) for graph in result.dataset.graphs()}

    assert mapping.ONTO + "doc/d1" in names
    assert mapping.ONTO + "doc/d2" in names


def test_flat_provenance_puts_everything_in_one_graph():
    result = mapping.regenerate(
        [row("m1", "d1")], {}, MappingRules(provenance=mapping.FLAT)
    )
    named = {str(g.identifier) for g in result.dataset.graphs()}

    assert not any(name.startswith(mapping.ONTO + "doc/") for name in named)
    assert list(result.dataset.quads((None, mapping.DERIVED_FROM, None, None)))


def test_regeneration_is_deterministic():
    rows = [row("m1", "d1"), row("m2", "d2", text="NVivo", status=POSSIBLE_DUPLICATE)]
    typings = {"m1": (INSTITUTION, "auto")}

    first = mapping.regenerate(rows, typings, MappingRules())
    second = mapping.regenerate(rows, typings, MappingRules())

    assert set(first.dataset.quads((None, None, None, None))) == set(
        second.dataset.quads((None, None, None, None))
    )


def test_the_rules_hash_ignores_field_order_and_moves_with_content():
    assert MappingRules().rules_hash() == MappingRules(type_from=("auto",)).rules_hash()
    assert MappingRules().rules_hash() != MappingRules(type_from=("auto", "grey")).rules_hash()
    assert MappingRules().rules_hash() != MappingRules(conflict_policy="force").rules_hash()


def test_an_exception_changes_the_hash_but_not_the_global_policy():
    base = MappingRules()
    amended = base.with_exceptions({"m17": "refute"})

    assert amended.rules_hash() != base.rules_hash()
    assert amended.conflict_policy == base.conflict_policy


def test_a_policy_that_is_not_implemented_is_refused():
    with pytest.raises(mapping.UnknownPolicy, match="not implemented"):
        MappingRules(conflict_policy="ignore")
    with pytest.raises(mapping.UnknownPolicy, match="unknown zone"):
        MappingRules(type_from=("auto", "maybe"))


def test_recording_the_rules_twice_reports_nothing_changed(tmp_path):
    """This is what makes regeneration idempotent rather than merely deterministic."""
    conn = connect(tmp_path)
    graph = mapping.flatten(mapping.regenerate([row("m1", "d1")], {}, MappingRules()).dataset)
    versioning.commit(conn, graph, version_id="v0")

    assert versioning.record_rules(conn, "v0", "sha256:aaa") is True
    assert versioning.record_rules(conn, "v0", "sha256:aaa") is False
    assert versioning.record_rules(conn, "v0", "sha256:bbb") is True


def test_the_rules_column_is_added_to_a_store_that_predates_it(tmp_path):
    conn = connect(tmp_path)
    conn.script(
        "DROP TABLE IF EXISTS versions;"
        "CREATE TABLE versions (id TEXT PRIMARY KEY, parent_id TEXT, iteration INTEGER,"
        " branch_id TEXT, state_hash TEXT NOT NULL, turtle TEXT NOT NULL, note TEXT,"
        " created_at TEXT);"
    )
    versioning.install(conn)

    assert "rules_hash" in conn.columns("versions")


def test_load_inputs_never_writes_to_the_mention_layer(tmp_path):
    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO mentions (id, document_id, page, surface_text, status) "
        "VALUES ('m1', 'd1', 1, 'Genome Canada', 'ok')"
    )
    conn.commit()
    before = list(conn.execute("SELECT * FROM mentions"))

    rows, typings = mapping.load_inputs(conn, "v0")

    assert [item.id for item in rows] == ["m1"]
    assert typings == {}
    assert list(conn.execute("SELECT * FROM mentions")) == before
