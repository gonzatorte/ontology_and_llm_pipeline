"""La capa de servicios: el contrato que hace posible tener dos interfaces.

Lo que se fija acá no es mecánica sino la razón de que la capa exista — que una etapa no sepa
quién la está corriendo, y que lo que el usuario tiene que arreglar llegue como algo que las
dos interfaces puedan mostrar a su manera.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from onto_pipeline import versioning
from onto_pipeline.db import connect
from onto_pipeline.services import StageError, Workspace, deliver, iterate

SESSION = "test-1"


SERVICES = Path(__file__).resolve().parents[1] / "src" / "onto_pipeline" / "services"


# ─────────────────────────  el invariante de la capa  ─────────────────────────


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_no_service_imports_a_user_interface():
    """El corte entero depende de esto.

    Una etapa que importa `typer` ya eligió quién la corre, y `rich` en el medio del cuerpo es
    lo que hacía imposible correrla desde el wizard sin copiarla. Si esto se rompe, la capa
    dejó de ser una capa.
    """
    offenders = {
        path.name: sorted(_imported_modules(path) & {"typer", "rich", "click"})
        for path in SERVICES.glob("*.py")
        if _imported_modules(path) & {"typer", "rich", "click"}
    }
    assert not offenders, offenders


def test_a_missing_precondition_is_a_stage_error_and_not_an_exit(tmp_path):
    """`StageError` en vez de `typer.BadParameter`: el wizard lo muestra y sigue preguntando,
    el CLI lo convierte en un código de salida. La etapa no sabe cuál de las dos pasa."""
    with pytest.raises(StageError):
        _workspace(tmp_path).resolve_version("no-such-version")


def _config(tmp_path: Path):
    from onto_pipeline.config import Config

    return Config.model_validate({
        "paths": {
            "corpus_root": tmp_path / "corpus",
            "initial_ontology": tmp_path / "seed.rdf",
            "work_dir": tmp_path / "work",
        }
    })


def _workspace(tmp_path: Path) -> Workspace:
    """Con la sesión creada de verdad y no sólo nombrada: el historial tiene clave foránea contra
    `user_sessions`, y anotar para una sesión que no existe tiene que fallar."""
    from onto_pipeline import sessions

    config = _config(tmp_path)
    conn = connect(config.paths.work_dir)
    created = sessions.create(conn, use_case="test")
    assert created.id == SESSION
    return Workspace.of(config, conn, session_id=created.id)


def test_only_a_decided_finding_becomes_an_input_of_the_normalization(tmp_path):
    """Lo que el usuario contestó tiene que volver a aplicarse solo en cada `normalize`, porque
    normalizar es función determinista de la ontología en disco y una corrección escrita encima
    de la versión commiteada se pierde en la corrida siguiente.

    Lo que sigue abierto no entra: sería tomar la decisión por default, que es lo que
    `BRANCH-ONLY-REVIEW` nombra.
    """
    from onto_pipeline import review
    from onto_pipeline.services import prep

    workspace = _workspace(tmp_path)
    findings = [
        review.Finding(review.TYPO, "c:1", "subre -> sobre",
                       {"token": "subre", "suggestion": "sobre"}),
        review.Finding(review.DIVERGENT_LABEL, "c:2", "Valor (en) | value (en)", {"labels": []}),
        review.Finding(review.TYPO, "c:3", "frm -> framework",
                       {"token": "frm", "suggestion": "framework"}),
    ]
    review.sync(workspace.conn, findings, version_id="v0", session_id=SESSION,
                kinds=[review.TYPO, review.DIVERGENT_LABEL])
    review.resolve(workspace.conn, findings[0].id, review.ACCEPTED, session_id=SESSION)
    review.resolve(workspace.conn, findings[1].id, review.ACCEPTED, session_id=SESSION)

    decisions = prep.label_decisions(workspace)

    assert decisions.typo_fixes == {"c:1": [("subre", "sobre")]}, "la tercera sigue abierta"
    assert decisions.dropped_derived == frozenset({"c:2"})


_SEED_RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="http://example.org/onto#Nota">
    <rdfs:label>Subre el tema</rdfs:label></owl:Class>
  <owl:Class rdf:about="http://example.org/onto#Otra">
    <rdfs:label>Sobre el metodo</rdfs:label></owl:Class>
  <owl:Class rdf:about="http://example.org/onto#Tercera">
    <rdfs:label>Sobre el resultado</rdfs:label></owl:Class>
</rdf:RDF>
"""


def test_a_decided_label_reaches_the_dag_although_it_is_only_an_annotation(tmp_path):
    """El hash de estado descarta las anotaciones, así que corregir una etiqueta no produce un
    estado lógico nuevo. Sin commitear igual, la corrección quedaría sólo en el TTL del disco y
    el grafo que lee el pipeline —que sale de la versión guardada— seguiría con la etiqueta
    vieja: la decisión del usuario no llegaría al matcher.

    La versión nueva conserva el hash de su padre, como ya hace `generate_glosses`: no es un
    estado nuevo para razonar, pero es algo que el DAG tiene que llevar.
    """
    from rdflib.namespace import RDFS

    from onto_pipeline import review, versioning
    from onto_pipeline.services import prep

    workspace = _workspace(tmp_path)
    workspace.config.paths.initial_ontology.write_text(_SEED_RDF, encoding="utf-8")
    first = prep.normalize(workspace)
    typo = next(
        item for item in review.load(
            workspace.conn, status=review.OPEN, kind=review.TYPO, session_id=SESSION
        )
        if item["payload"].get("token") == "subre"
    )
    review.resolve(workspace.conn, typo["id"], review.ACCEPTED, session_id=SESSION)

    second = prep.normalize(workspace)

    assert second.committed is not None, "la corrección tiene que llegar al DAG"
    assert second.committed.parent_id == first.committed.id
    assert second.committed.state_hash == first.committed.state_hash, "mismo estado lógico"
    graph = versioning.load(workspace.conn, second.committed.id)[1]
    assert "Sobre el tema" in {str(text) for text in graph.objects(None, RDFS.label)}
    assert prep.normalize(workspace).committed is None, \
        "y volver a correrla no apila una versión por corrida"


# ─────────────────────────  resolución de versión  ─────────────────────────


BASE = "https://ontology.local/id/"


def _initial_graph() -> Graph:
    graph = Graph()
    for name in ("A", "B"):
        iri = URIRef(BASE + name)
        graph.add((iri, RDF.type, OWL.Class))
        graph.add((iri, SKOS.prefLabel, Literal(name, lang="en")))
    return graph


def test_an_empty_store_has_no_version_and_that_is_not_an_error(tmp_path):
    """Antes de la semilla no hay versión, y ése es el estado normal de un almacén nuevo: el
    wizard pregunta esto para saber si tiene que normalizar, no para fallar."""
    workspace = _workspace(tmp_path)
    assert workspace.latest_version() is None
    with pytest.raises(StageError):
        workspace.resolve_version()


def test_the_newest_version_wins_ties_by_insertion_order(tmp_path):
    """`created_at` tiene precisión de segundo, así que dos versiones del mismo segundo empatan
    y el orden de inserción desempata. Sin eso, "la más nueva" es una lotería."""
    workspace = _workspace(tmp_path)
    graph = _initial_graph()
    versioning.commit(
        workspace.conn, graph, version_id=f"{SESSION}:v0", note="seed", session_id=SESSION
    )
    graph.add((URIRef(BASE + "C"), RDF.type, OWL.Class))
    versioning.commit(
        workspace.conn, graph, version_id=f"{SESSION}:v1", parent_id=f"{SESSION}:v0",
        note="more", session_id=SESSION,
    )
    assert workspace.resolve_version() == f"{SESSION}:v1"
    assert workspace.resolve_version("v0") == f"{SESSION}:v0"


# ─────────────────────────  la entrega  ─────────────────────────


def test_export_walks_the_lineage_and_says_what_each_version_contributed(tmp_path):
    """«Aplicar la historia» no es reproducir deltas: cada versión guarda su Turtle entero, así
    que la cabeza ya es la acumulación. Lo que agrega el export es poder decir qué aportó cada
    iteración, y eso sólo se sabe recorriendo el linaje."""
    workspace = _workspace(tmp_path)
    graph = _initial_graph()
    versioning.commit(workspace.conn, graph, version_id=f"{SESSION}:v0", note="normalized seed",
        session_id=SESSION)
    graph.add((URIRef(BASE + "C"), RDF.type, OWL.Class))
    graph.add((URIRef(BASE + "C"), RDFS.subClassOf, URIRef(BASE + "A")))
    versioning.commit(
        workspace.conn, graph, version_id=f"{SESSION}:v1", parent_id=f"{SESSION}:v0",
        note="one class", session_id=SESSION,
    )

    result = deliver.export(workspace, version=f"{SESSION}:v1", include_abox=False)

    assert [step.version_id for step in result.history] == [f"{SESSION}:v0", f"{SESSION}:v1"]
    assert result.history[0].parent_id is None          # la raíz se reporta entera
    assert result.history[1].added == 2                 # la clase y su subsunción
    assert result.classes == 3 and result.inventory_classes == 2
    assert result.minted_classes == 1


def test_export_writes_no_annotation_property_the_seed_did_not_declare(tmp_path):
    """Una propiedad de anotación no declarada saca la ontología de OWL 2 DL, y el síntoma no
    es un error: es ELK salteándose en silencio. Por eso la procedencia del export va en un
    manifiesto al lado y no adentro de la ontología."""
    from onto_pipeline.initial_ontology import DECLARED_ANNOTATIONS

    workspace = _workspace(tmp_path)
    versioning.commit(
        workspace.conn, _initial_graph(), version_id=f"{SESSION}:v0", note="seed",
        session_id=SESSION,
    )
    result = deliver.export(workspace, version=f"{SESSION}:v0", include_abox=False)

    written = Graph()
    written.parse(result.path, format="trig")
    allowed = set(DECLARED_ANNOTATIONS) | {
        RDF.type, RDFS.subClassOf, RDFS.label, RDFS.comment, OWL.equivalentClass,
        OWL.disjointWith,
    }
    assert {predicate for _, predicate, _ in written} <= allowed


def test_export_records_the_lineage_in_a_manifest_next_to_the_ontology(tmp_path):
    """El artefacto dice qué es la ontología; el manifiesto dice de dónde salió. Se leen
    juntos, así que se escriben juntos."""
    workspace = _workspace(tmp_path)
    versioning.commit(
        workspace.conn, _initial_graph(), version_id=f"{SESSION}:v0", note="seed",
        session_id=SESSION,
    )
    result = deliver.export(workspace, version=f"{SESSION}:v0", include_abox=False)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["lineage"] == [f"{SESSION}:v0"]
    assert manifest["version"] == f"{SESSION}:v0"
    assert manifest["abox_included"] is False


def test_exporting_to_turtle_says_that_it_flattened_the_provenance(tmp_path):
    """Turtle no tiene grafos con nombre. Perder la procedencia por documento está permitido;
    perderla en silencio es exactamente lo que la capa de menciones existe para no hacer."""
    workspace = _workspace(tmp_path)
    versioning.commit(
        workspace.conn, _initial_graph(), version_id=f"{SESSION}:v0", note="seed",
        session_id=SESSION,
    )
    result = deliver.export(
        workspace, version=f"{SESSION}:v0", include_abox=False, fmt=deliver.TURTLE
    )
    assert result.path.suffix == ".ttl"


def test_export_says_when_there_is_no_abox_instead_of_pretending(tmp_path):
    """Entregar sólo la TBox es una respuesta válida; entregarla sin decir que faltan las
    instancias no lo es."""
    workspace = _workspace(tmp_path)
    versioning.commit(
        workspace.conn, _initial_graph(), version_id=f"{SESSION}:v0", note="seed",
        session_id=SESSION,
    )
    result = deliver.export(workspace, version=f"{SESSION}:v0", refresh_abox=False)
    assert result.abox_included is False
    assert any("no ABox" in warning for warning in result.warnings)


# ─────────────────────────  la cadena de validación, una sola  ─────────────────────────


def test_the_validation_chain_names_the_reason_it_refused(tmp_path):
    """Que no se aplicó es media respuesta. `axiomatize` y `branch` comparten esta función
    justamente para que las dos den la otra mitad: el razonador, OntoClean, la forma, o el
    loop."""
    assert {iterate.REASONER, iterate.ONTOCLEAN, iterate.STRUCTURE, iterate.LOOP} == {
        "reasoner", "ontoclean", "structure", "loop"
    }
    refused = iterate.Application(chain=None, refused=iterate.LOOP)
    assert not refused.applied
