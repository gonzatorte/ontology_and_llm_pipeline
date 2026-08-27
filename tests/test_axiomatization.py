from __future__ import annotations

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from onto_pipeline import axiomatization as ax
from onto_pipeline.axiomatization import Axiom, Judgement
from onto_pipeline.db import connect

BASE = "https://ontology.local/id/"
LABELS = {"Technique": "c:Technique", "Organization": "c:Organization"}

PROPOSAL = {
    "id": "p1", "label": "Focus Group", "gloss": "A group interview run as one session.",
    "criterion": "several informants at once rather than one",
}


_DEFAULT = object()


def assemble(relation, candidate="Technique", proposals=None, support=_DEFAULT):
    # A sentinel, not `or`: an empty support dict is a meaningful input here — it is what a
    # bridged proposal looks like — and `or` would silently replace it with the default.
    if support is _DEFAULT:
        support = {"p1": ["m1", "m2"]}
    return ax.assemble(
        proposals or [PROPOSAL],
        {"p1": Judgement(relation=relation, candidate=candidate, why="because")},
        base_iri=BASE, label_to_iri=LABELS, support=support,
    )


def test_a_kind_of_becomes_a_subsumption():
    result = assemble(ax.SUBCLASS)
    iri = result.minted["p1"]
    subsumptions = [a for a in result.axioms if a.predicate == "subClassOf"]
    assert [a.object_iri for a in subsumptions] == ["c:Technique"]
    assert all(a.subject_iri == iri for a in result.axioms)


def test_an_example_of_is_rejected_rather_than_attached():
    """Not a weaker subsumption — it was never a class. Attaching it is the confusion 6.1
    lists first."""
    result = assemble(ax.INSTANCE, candidate="Organization")
    assert result.axioms == [] and "p1" not in result.minted
    assert "example of Organization" in result.rejected["p1"]


def test_unrelated_leaves_the_class_a_root():
    """A forced parent is worse than none."""
    result = assemble(ax.UNRELATED, candidate=None)
    assert "p1" in result.minted
    assert not [a for a in result.axioms if a.predicate == "subClassOf"]
    assert not result.rejected


def test_a_parent_that_is_not_in_the_ontology_voids_the_proposal():
    """The model naming something that does not exist must not half-create a class."""
    result = assemble(ax.SUBCLASS, candidate="Nonexistent")
    assert result.axioms == [] and "p1" not in result.minted
    assert "not in the ontology" in result.rejected["p1"]


def test_the_class_carries_its_label_gloss_and_criterion():
    result = assemble(ax.UNRELATED, candidate=None)
    by_predicate = {a.predicate: a.literal for a in result.axioms if a.literal}
    assert by_predicate["prefLabel"] == "Focus Group"
    assert by_predicate["definition"].startswith("A group interview")
    assert by_predicate["scopeNote"].startswith("several informants")


def test_minted_iris_are_opaque_and_stable():
    """Opaque like every other entity, and derived so a re-run does not mint a second class."""
    first = assemble(ax.UNRELATED, candidate=None).minted["p1"]
    again = assemble(ax.UNRELATED, candidate=None).minted["p1"]
    assert first == again
    assert first.startswith(BASE) and "Focus" not in first


def test_provenance_splits_by_whether_mentions_support_it():
    """The evidence filter applies to textual axioms only; applied to bridges it would kill
    exactly what makes the seed useful (6.2b)."""
    textual = assemble(ax.UNRELATED, candidate=None, support={"p1": ["m1"]})
    bridged = assemble(ax.UNRELATED, candidate=None, support={})

    assert {a.provenance for a in textual.axioms} == {ax.TEXTUAL}
    assert all(a.support == ["m1"] for a in textual.axioms)
    assert {a.provenance for a in bridged.axioms} == {ax.WORLD_KNOWLEDGE}
    assert all(a.support == [] for a in bridged.axioms)


def test_a_proposal_with_no_judgement_produces_nothing():
    result = ax.assemble([PROPOSAL], {}, base_iri=BASE, label_to_iri=LABELS)
    assert result.axioms == [] and not result.minted and not result.rejected


@pytest.mark.parametrize("answer", [
    '{"relation": "married_to", "candidate": "Technique"}',
    '{"relation": "subclass_of"}',
    "no json at all",
])
def test_a_malformed_judgement_is_refused(answer):
    with pytest.raises(ValueError):
        ax.parse(answer, {})


def test_unrelated_needs_no_candidate():
    assert ax.parse('{"relation": "unrelated", "candidate": null}', {})["candidate"] is None


def test_applying_leaves_the_previous_graph_untouched():
    """The caller still needs the old state to diff against, and to fall back to when the
    reasoner rejects the new one."""
    before = Graph()
    before.add((URIRef("c:Technique"), RDF.type, OWL.Class))
    result = assemble(ax.SUBCLASS)

    after = ax.apply(before, result.axioms)

    assert len(before) == 1, "the graph passed in is not modified"
    iri = URIRef(result.minted["p1"])
    assert (iri, RDF.type, OWL.Class) in after
    assert (iri, RDFS.subClassOf, URIRef("c:Technique")) in after
    assert any(o.language == "en" for o in after.objects(iri, SKOS.prefLabel))


def test_axioms_persist_with_their_provenance(tmp_path):
    conn = connect(tmp_path)
    ax.persist(conn, "v1", [
        Axiom("c:New", "subClassOf", "c:Technique", provenance=ax.TEXTUAL, support=["m1"]),
        Axiom("c:New", "prefLabel", literal="New", language="en",
              provenance=ax.WORLD_KNOWLEDGE),
    ])
    rows = list(conn.execute("SELECT provenance, support FROM proposed_axioms ORDER BY predicate"))
    assert {row["provenance"] for row in rows} == {ax.TEXTUAL, ax.WORLD_KNOWLEDGE}


def test_the_prompt_spells_out_the_difference_it_must_not_blur():
    rendered = ax.PROMPT.render(**ax.payload(
        PROPOSAL, [{"label": "Technique", "gloss": "A systematic procedure."}], ["focus group"],
    ))
    assert "every one, always" in rendered
    assert "kind of it" in rendered
    assert "A systematic procedure." in rendered


def test_every_annotation_it_writes_is_declared_by_the_seed():
    """An undeclared annotation property leaves OWL 2 DL, and the symptom is not an error —
    it is ELK quietly dropping to a fragment too small to filter with. `scopeNote` slipped in
    exactly this way."""
    from onto_pipeline.seed import DECLARED_ANNOTATIONS

    written = {
        predicate for name, predicate in ax._PREDICATES.items()
        if name not in ("type", "subClassOf")
    }
    assert written <= set(DECLARED_ANNOTATIONS), (
        f"undeclared: {written - set(DECLARED_ANNOTATIONS)}"
    )


# ─────────────  forma normal: la misma propuesta con otros IRIs  ─────────────


# `LABELS` de arriba va etiqueta -> IRI, que es lo que `assemble` necesita. La forma normal
# nombra IRIs, así que necesita el mapa al revés.
BY_IRI = {iri: label for label, iri in LABELS.items()}


def minted(iri: str, label: str = "Focus Group", parent: str = "c:Technique", gloss: str = "g"):
    return [
        Axiom(iri, "type", str(OWL.Class)),
        Axiom(iri, "prefLabel", literal=label, language="en"),
        Axiom(iri, "subClassOf", parent),
        Axiom(iri, "definition", literal=gloss, language="en"),
    ]


def test_the_same_commitment_with_other_iris_has_the_same_normal_form():
    """Un IRI acuñado sale de uuid5 sobre el id de la propuesta, así que el mismo compromiso
    vuelve en otra iteración con otro identificador. Sin normalizar no se detecta."""
    assert ax.normal_form(minted("urn:a"), BY_IRI) == ax.normal_form(minted("urn:b"), BY_IRI)


def test_a_rewritten_gloss_is_not_another_proposal():
    a = ax.normal_form(minted("urn:a", gloss="una cosa"), BY_IRI)
    b = ax.normal_form(minted("urn:a", gloss="otra explicación"), BY_IRI)
    assert a == b


def test_another_parent_is_another_commitment():
    assert ax.normal_form(minted("urn:a"), BY_IRI) != ax.normal_form(
        minted("urn:a", parent="c:Organization"), LABELS
    )


def test_another_name_is_another_commitment():
    """La etiqueta sí importa: entra como el nombre del sujeto."""
    assert ax.normal_form(minted("urn:a"), BY_IRI) != ax.normal_form(
        minted("urn:a", label="Group Interview"), LABELS
    )


def test_the_parent_is_named_by_its_label_not_its_iri():
    assert "Technique" in ax.normal_form(minted("urn:a"), BY_IRI)
    assert "c:Technique" not in ax.normal_form(minted("urn:a"), BY_IRI)


def test_the_order_of_the_axioms_does_not_matter():
    forward = minted("urn:a")
    assert ax.normal_form(forward, BY_IRI) == ax.normal_form(list(reversed(forward)), BY_IRI)


def test_precedents_carry_the_comment_because_that_is_what_transfers():
    """El veredicto dice qué pasó; el comentario dice por qué, que es lo único aplicable a una
    propuesta distinta."""
    rendered = ax.render_precedents([
        {"normalized_axioms": "X subClassOf Y", "status": "not_chosen",
         "comment": "el padre ya lo cubre"},
    ])
    assert "se descartó" in rendered and "el padre ya lo cubre" in rendered


def test_no_precedents_says_so_rather_than_showing_nothing():
    assert "primera iteración" in ax.render_precedents([])


def test_the_prompt_says_precedents_are_evidence_and_not_rules():
    rendered = ax.PROMPT.render(**ax.payload(
        PROPOSAL, [{"label": "Technique", "gloss": "A systematic procedure."}], ["focus group"],
        [{"normalized_axioms": "X subClassOf Y", "status": "not_chosen", "comment": "porque sí"}],
    ))
    assert "precedents, not rules" in rendered and "porque sí" in rendered
