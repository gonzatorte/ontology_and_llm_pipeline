from __future__ import annotations

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from onto_pipeline import terms
from onto_pipeline.seed import (
    CLASS,
    OBJECT_PROPERTY,
    detect_typos,
    gloss_contexts,
    normalize_seed,
)

BASE = "https://ontology.local/id/"
NS = "http://example.org/onto#"

_SEED = f"""
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix : <{NS}> .

:MethodologicalStrategy a owl:Class .
:Technique a owl:Class ; rdfs:subClassOf :MethodologicalStrategy .
:DocumentAnalysis a owl:Class ; rdfs:subClassOf :Technique ;
    rdfs:label "documentAnalysis" .
:Subject a owl:Class ; owl:disjointWith :Technique .
:Aplica_una_o_varias a owl:ObjectProperty ;
    rdfs:domain :MethodologicalStrategy ; rdfs:range :Technique ;
    rdfs:label "appliesTechnique" .
:hasSubject a owl:ObjectProperty ;
    rdfs:domain :Technique ; rdfs:range :Subject ; rdfs:label "hasSubject" .
"""


@pytest.fixture
def seed_file(tmp_path):
    target = tmp_path / "seed.ttl"
    target.write_text(_SEED, encoding="utf-8")
    return target


@pytest.fixture
def seed(seed_file):
    return normalize_seed(seed_file, BASE, divergence_threshold=0.8)


def test_iris_are_opaque_and_the_original_is_kept_as_provenance(seed):
    assert all(entity.iri.startswith(BASE) for entity in seed.entities)
    assert all(NS not in entity.iri for entity in seed.entities)
    notes = {str(note) for note in seed.graph.objects(None, SKOS.historyNote)}
    assert f"{NS}Technique" in notes


def test_normalization_is_reproducible(seed_file):
    """Same seed, same IRIs: every downstream cache key is built on them."""
    first = normalize_seed(seed_file, BASE, divergence_threshold=0.8)
    second = normalize_seed(seed_file, BASE, divergence_threshold=0.8)
    assert [e.iri for e in first.entities] == [e.iri for e in second.entities]


def test_structure_survives_the_rewrite(seed):
    by_original = {entity.original_iri: entity for entity in seed.entities}
    technique = URIRef(by_original[f"{NS}Technique"].iri)
    strategy = URIRef(by_original[f"{NS}MethodologicalStrategy"].iri)
    assert (technique, RDFS.subClassOf, strategy) in seed.graph
    assert (technique, RDF.type, OWL.Class) in seed.graph


def test_labels_are_derived_from_the_identifier_and_the_declared_one_is_kept(seed):
    by_original = {entity.original_iri: entity for entity in seed.entities}
    entity = by_original[f"{NS}Aplica_una_o_varias"]
    texts = {(label.text, label.source) for label in entity.labels}
    assert ("Aplica una o varias", "iri") in texts
    assert ("appliesTechnique", "declared") in texts
    assert entity.kind == OBJECT_PROPERTY


def test_a_label_that_omits_what_the_identifier_says_is_flagged_not_translated(seed):
    """`Aplica_una_o_varias` against `appliesTechnique`: assuming a translation would lose
    the quantification the identifier carries."""
    by_original = {entity.original_iri: entity for entity in seed.entities}
    entity = by_original[f"{NS}Aplica_una_o_varias"]
    assert entity.divergent
    assert entity.divergence_reason == "cross_language_unverified"


def test_an_agreeing_pair_is_not_flagged(seed):
    by_original = {entity.original_iri: entity for entity in seed.entities}
    assert not by_original[f"{NS}hasSubject"].divergent
    assert not by_original[f"{NS}DocumentAnalysis"].divergent


def test_preferred_label_is_written_as_skos_preflabel(seed):
    prefs = {str(literal) for literal in seed.graph.objects(None, SKOS.prefLabel)}
    assert "appliesTechnique" in prefs
    assert "Technique" in prefs  # no declared label, so the derived one is preferred


def test_gloss_context_is_the_neighbourhood_not_the_name(seed):
    contexts = {context.label: context for context in gloss_contexts(seed)}
    technique = contexts["Technique"]
    assert technique.superclasses == ["Methodological Strategy"]
    assert technique.subclasses == ["documentAnalysis"]
    assert technique.range_of == ["appliesTechnique"]
    assert technique.domain_of == ["hasSubject"]
    assert technique.disjoint_with == ["Subject"]
    assert all(context.kind == CLASS for context in gloss_contexts(seed))


def _labels_from(pairs):
    graph = Graph()
    for name, declared in pairs:
        subject = URIRef(NS + name)
        graph.add((subject, RDF.type, OWL.Class))
        if declared:
            graph.add((subject, RDFS.label, Literal(declared)))
    return graph


def test_edit_distance_detector_finds_a_misspelling(tmp_path):
    source = tmp_path / "typo.ttl"
    source.write_text(
        _labels_from([("Sobre_el_tema", None), ("Subre_el_asunto", None),
                      ("Sobre_el_marco", None)]).serialize(format="turtle"),
        encoding="utf-8",
    )
    findings = normalize_seed(source, BASE, divergence_threshold=0.8).typos
    assert any(
        finding.detector == "edit_distance" and finding.token == "subre"
        and finding.suggestion == "sobre"
        for finding in findings
    )


def test_translations_are_not_reported_as_typos(seed_file, tmp_path):
    """`objective`/`objetivo` differ by one edit and are not a misspelling."""
    source = tmp_path / "bilingual.ttl"
    source.write_text(
        _labels_from([("Objetivo", None), ("Objective", None), ("Objective_general", None)])
        .serialize(format="turtle"),
        encoding="utf-8",
    )
    findings = normalize_seed(source, BASE, divergence_threshold=0.8).typos
    assert not [f for f in findings if {f.token, f.suggestion} == {"objetivo", "objective"}]


def test_capitalization_anomaly_is_detected():
    from onto_pipeline.seed import Entity, Label

    entity = Entity(iri="x", original_iri="y", kind=CLASS,
                    labels=[Label(text="TIene respuesta", language="es", source="iri")])
    findings = detect_typos([entity])
    assert [(f.detector, f.token, f.suggestion) for f in findings if
            f.detector == "capitalization"] == [("capitalization", "TIene", "Tiene")]


def test_camel_case_and_title_case_are_not_anomalies():
    from onto_pipeline.seed import Entity, Label

    entities = [
        Entity(iri=str(index), original_iri=str(index), kind=CLASS,
               labels=[Label(text=text, language="en", source="declared")])
        for index, text in enumerate(["hasSubject", "Document Analysis", "PDF"])
    ]
    assert not [f for f in detect_typos(entities) if f.detector == "capitalization"]


def test_denormalizing_identifiers():
    assert terms.denormalize("appliesTechnique") == "applies Technique"
    assert terms.denormalize("Aplica_una_o_varias") == "Aplica una o varias"
    assert terms.denormalize("has-theoretical-frame") == "has theoretical frame"
    assert terms.denormalize("PDFDocument") == "PDF Document"
