from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH, SKOS

from onto_pipeline import validation
from onto_pipeline.axiomatization import TEXTUAL, WORLD_KNOWLEDGE, Axiom

C = URIRef("c:Technique")
D = URIRef("c:Interview")
P = URIRef("c:uses")


def described(*iris) -> Graph:
    graph = Graph()
    for iri in iris:
        graph.add((iri, RDF.type, OWL.Class))
        graph.add((iri, SKOS.prefLabel, Literal("name", lang="en")))
        graph.add((iri, SKOS.definition, Literal("a definition", lang="en")))
    return graph


# ─────────────────────  ITER-VALIDATE-6-EVIDENCE — textual evidence  ─────────────────────


def test_a_textual_axiom_with_no_citation_is_rejected():
    kept, dropped = validation.evidence([Axiom("c:A", "subClassOf", "c:B",
                                               provenance=TEXTUAL, support=[])])
    assert kept == [] and "cites no mention" in dropped[0][1]


def test_a_bridge_is_not_asked_for_a_citation():
    """"Every axiom without a citation is discarded" would delete exactly the bridges that
    make a seed useful. It is not a stricter policy, it is a different and wrong one."""
    axiom = Axiom("c:A", "subClassOf", "c:B", provenance=WORLD_KNOWLEDGE, support=[])
    kept, dropped = validation.evidence([axiom])
    assert kept == [axiom] and dropped == []


def test_a_cited_textual_axiom_survives():
    axiom = Axiom("c:A", "subClassOf", "c:B", provenance=TEXTUAL, support=["m1"])
    assert validation.evidence([axiom])[0] == [axiom]


def test_the_filter_rejects_axioms_and_not_the_batch():
    """One uncited claim is not a reason to throw away the iteration."""
    good = Axiom("c:A", "subClassOf", "c:B", provenance=TEXTUAL, support=["m1"])
    bad = Axiom("c:C", "subClassOf", "c:B", provenance=TEXTUAL, support=[])
    kept, dropped = validation.evidence([good, bad])
    assert kept == [good] and len(dropped) == 1


# ─────────────────────────  ITER-VALIDATE-5-PITFALLS — pitfalls  ─────────────────────────


def test_a_well_described_ontology_raises_nothing():
    assert validation.pitfalls(described(C, D)).decision == validation.PASS


def test_a_pitfall_is_a_warning_and_never_a_rejection():
    """Some of these are deliberate. The spec makes ITER-VALIDATE-5-PITFALLS a warning
    and the chain treats it
    as one: it is reported and nothing is dropped."""
    graph = Graph()
    graph.add((C, RDF.type, OWL.Class))
    verdict = validation.pitfalls(graph)
    assert verdict.decision == validation.WARN and not verdict.rejected


def test_a_class_with_no_definition_is_a_smell():
    graph = Graph()
    graph.add((C, RDF.type, OWL.Class))
    graph.add((C, SKOS.prefLabel, Literal("Technique", lang="en")))
    assert any("P08" in item and "definition" in item
               for item in validation.pitfalls(graph).findings)


def test_rdfs_naming_counts_as_naming():
    graph = Graph()
    graph.add((C, RDF.type, OWL.Class))
    graph.add((C, RDFS.label, Literal("Technique")))
    graph.add((C, RDFS.comment, Literal("A systematic procedure.")))
    assert not any("P08" in item for item in validation.pitfalls(graph).findings)


def test_a_cycle_in_the_hierarchy_is_reported():
    """ITER-VALIDATE-7-STRUCTURE cannot see it: the depth metric stops at a repeated node
    rather than saying
    that it repeated."""
    graph = described(C, D)
    graph.add((C, RDFS.subClassOf, D))
    graph.add((D, RDFS.subClassOf, C))
    assert any("P06" in item for item in validation.pitfalls(graph).findings)


def test_a_plain_hierarchy_is_not_a_cycle():
    graph = described(C, D)
    graph.add((D, RDFS.subClassOf, C))
    assert not any("P06" in item for item in validation.pitfalls(graph).findings)


def test_a_property_with_no_domain_is_a_smell():
    graph = described(C)
    graph.add((P, RDF.type, OWL.ObjectProperty))
    findings = validation.pitfalls(graph).findings
    assert sum("P11" in item for item in findings) == 2, "no domain and no range"


def test_two_domains_are_flagged_because_owl_intersects_them():
    graph = described(C, D)
    graph.add((P, RDF.type, OWL.ObjectProperty))
    graph.add((P, RDFS.domain, C))
    graph.add((P, RDFS.domain, D))
    graph.add((P, RDFS.range, C))
    assert any("P19" in item for item in validation.pitfalls(graph).findings)


def test_a_class_equivalent_to_itself_is_a_recursive_definition():
    graph = described(C)
    graph.add((C, OWL.equivalentClass, C))
    assert any("P24" in item for item in validation.pitfalls(graph).findings)


# ─────────────────────────  ITER-VALIDATE-3-SHACL — SHACL  ─────────────────────────


def shape_requiring_a_label() -> Graph:
    graph = Graph()
    shape = URIRef("c:TechniqueShape")
    graph.add((shape, RDF.type, SH.NodeShape))
    graph.add((shape, SH.targetClass, C))
    prop = URIRef("c:TechniqueShape-label")
    graph.add((shape, SH.property, prop))
    graph.add((prop, SH.path, SKOS.prefLabel))
    graph.add((prop, SH.minCount, Literal(1)))
    return graph


def test_data_that_satisfies_the_shapes_passes():
    data = Graph()
    individual = URIRef("c:i1")
    data.add((individual, RDF.type, C))
    data.add((individual, SKOS.prefLabel, Literal("one")))
    assert validation.shapes(data, shape_requiring_a_label()).decision == validation.PASS


def test_a_shape_violation_is_a_rejection_with_the_message():
    data = Graph()
    data.add((URIRef("c:i1"), RDF.type, C))
    verdict = validation.shapes(data, shape_requiring_a_label())
    assert verdict.rejected and verdict.findings


def test_no_shapes_file_means_the_filter_did_not_run():
    """Reported as not run rather than as passed: the absence of the instrument is not the
    absence of the finding."""
    assert validation.load_shapes(None) is None


def test_shapes_see_an_abox_that_keeps_its_provenance_in_named_graphs():
    """The trap this filter fell into once: parsing TriG straight into a Graph keeps only the
    default graph, every shape finds no target, and the filter conforms over nothing."""
    from rdflib import Dataset

    from onto_pipeline import mapping

    dataset = Dataset()
    dataset.graph(URIRef("urn:abox")).add((URIRef("c:i1"), RDF.type, C))
    serialized = dataset.serialize(format="trig")

    naive = Graph()
    naive.parse(data=serialized, format="trig")
    assert validation.shapes(naive, shape_requiring_a_label()).decision == validation.PASS

    reloaded = Dataset()
    reloaded.parse(data=serialized, format="trig")
    assert validation.shapes(
        mapping.flatten(reloaded), shape_requiring_a_label()
    ).rejected, "flattened, the shape finds its target and the missing label is a violation"
