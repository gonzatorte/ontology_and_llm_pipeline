"""El camino feliz entero: corpus y ontología inicial adentro, ontología enriquecida afuera.

**Por qué hace falta, teniendo 592 tests.** Los otros prueban módulos contra dependencias
reales, y cada uno es correcto por su cuenta; lo que ninguno mira es la **composición**. Las
tres fallas más caras de este proyecto vivían justamente ahí: una etapa que persistía adentro
del worker y se salteaba con el caché, un `rowid` que sólo existe en SQLite, y un id de documento
que colisionaba entre corridas. Ninguna la atrapó un test de módulo, y las tres se vieron
corriendo el pipeline de punta a punta.

**Qué se sustituye, y por qué sólo eso.** Dos cosas, las dos porque cuestan plata o red:

    el modelo     `Scripted` contesta según qué prompt le llega. Lo que se prueba es que el
                  código sepa **pedir y aprovechar** una respuesta, no que el modelo acierte:
                  «el LLM clasifica y nombra, el código arma la lógica» quiere decir que la
                  lógica es lo que hay que probar.
    el encoder    bolsa de palabras sobre un vocabulario fijo, para no bajar un
                  sentence-transformer de 400 MB en cada corrida.

Todo lo demás es real: el parser, SQLite, rdflib, el DAG de versiones, la cadena de validación
con ELK y HermiT, la regeneración del ABox y el export. Un e2e que además simulara el razonador
probaría que las piezas encajan con piezas que no son las que se usan.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from onto_pipeline import embeddings, llm, sessions
from onto_pipeline.config import Config
from onto_pipeline.db import connect
from onto_pipeline.services import Workspace, deliver, iterate, prep

LIB = Path(__file__).resolve().parent.parent / "lib"

pytest.importorskip("jpype", reason="el extra `reasoning` no está instalado")
pytestmark = pytest.mark.skipif(
    not list(LIB.glob("*.jar")), reason="correr scripts/fetch-jars.sh primero"
)

# Con jerarquía, y no por decoración: el filtro estructural (`ITER-VALIDATE-7-STRUCTURE`) rechaza
# una clase sin padre ni hijos, así que dos clases sueltas harían fallar la cadena por la
# ontología de prueba y no por el pipeline.
ONTOLOGY = """
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex:   <http://example.org/> .

ex:ResearchMethod  a owl:Class ; rdfs:label "Research Method" .
ex:Technique       a owl:Class ; rdfs:label "Technique" ; rdfs:subClassOf ex:ResearchMethod .
ex:Instrument      a owl:Class ; rdfs:label "Instrument" ; rdfs:subClassOf ex:ResearchMethod .
"""

# Dos documentos con una mención repetida —«focus group» en los dos— y una que la ontología
# inicial no cubre, para que haya algo que puentear y algo que inducir.
DOCUMENTS = {
    "d1.txt": (
        "We ran a focus group with eight participants.\n\n"
        "The focus group lasted two hours and the field note was written the same day.\n"
    ),
    "d2.txt": (
        "A second focus group followed the first.\n\n"
        "Each field note was coded by two researchers.\n"
    ),
}


class Scripted:
    """Un modelo que contesta según qué etapa le está preguntando.

    Despacha por el texto del prompt y no por orden de llamada: las etapas se intercalan, y un
    doble que depende del orden se rompe al agregar una etapa en el medio — que es ruido, no un
    hallazgo.
    """

    def __init__(self) -> None:
        self.asked: list[str] = []

    def complete(self, prompt: str, **_) -> llm.Completion:
        self.asked.append(prompt)
        return llm.Completion(text=self._answer(prompt), in_tokens=len(prompt), out_tokens=16)

    def _answer(self, prompt: str) -> str:
        if prompt.startswith("You are writing a dictionary definition"):
            return json.dumps({"en": "A way of doing something.",
                               "es": "Una manera de hacer algo."})
        if prompt.startswith("Extract the concept mentions"):
            return json.dumps({"mentions": [
                {"text": "focus group", "kind": "method"},
                {"text": "field note", "kind": "artifact"},
            ]})
        if prompt.startswith("The mentions in this document are numbered"):
            return json.dumps({"groups": []})
        if prompt.startswith("A phrase was found in a research corpus"):
            return json.dumps({"relation": "none", "candidate": None, "why": "no encaja"})
        if prompt.startswith("These phrases were found in a research corpus"):
            # Se contesta según los sintagmas que el prompt trae: un doble que devuelve siempre
            # lo mismo haría que dos grupos distintos nombraran la misma clase, y eso no es lo
            # que el pipeline hace — es lo que el doble haría mal.
            label = "Field Note" if "field note" in prompt.lower() else "Focus Group"
            return json.dumps({
                "is_a_class": True, "label": label,
                "gloss": f"A {label.lower()} found in the corpus.",
                "criterion": "por cómo se registra",
            })
        if prompt.startswith("A new concept was found in a research corpus"):
            return json.dumps({
                "relation": "subclass_of", "candidate": "Technique",
                "why": "es una manera de registrar",
            })
        raise AssertionError(f"prompt sin respuesta preparada: {prompt[:80]!r}")


class Keywords:
    """Bolsa de palabras sobre un vocabulario fijo. No baja nada."""

    VOCABULARY = ("focus", "group", "field", "note", "technique", "instrument")

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def encode(self, texts):
        return [
            [1.0 if word in text.lower() else 0.0 for word in self.VOCABULARY]
            for text in texts
        ]


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """Un caso de uso mínimo en disco, una sesión sobre él, y el workspace abierto."""
    use_cases = tmp_path / "use_cases" / "tiny"
    (use_cases / "corpus").mkdir(parents=True)
    for name, text in DOCUMENTS.items():
        (use_cases / "corpus" / name).write_text(text, encoding="utf-8")
    (use_cases / "ontology.ttl").write_text(ONTOLOGY, encoding="utf-8")

    config = Config.model_validate({
        "paths": {
            "corpus_root": use_cases / "corpus",
            "initial_ontology": use_cases / "ontology.ttl",
            "work_dir": tmp_path / "work",
            "use_cases_root": tmp_path / "use_cases",
            "reasoner_lib": LIB,
        },
        "llm": {"provider": "openai_compatible", "api_key_env": "IRRELEVANTE"},
        # Un solo soporte: con dos documentos cortos, tres menciones no se juntan nunca.
        "induction": {"min_support": 2, "similarity_threshold": 0.5},
        "matching": {"grey_zone_lower": 0.5, "auto_merge_threshold": 0.9},
    })

    scripted = Scripted()
    monkeypatch.setattr(llm, "build", lambda *_args, **_kwargs: scripted)
    monkeypatch.setattr(embeddings, "SentenceTransformerEncoder", Keywords)

    conn = connect(config.paths.work_dir)
    session = sessions.create(conn, use_case="tiny", name="e2e")
    workspace = Workspace.of(config, conn, session_id=session.id)
    return workspace, scripted


def test_the_happy_path_turns_a_corpus_and_an_ontology_into_an_enriched_one(pipeline):
    """De punta a punta, con el modelo y el encoder sustituidos y todo lo demás real.

    Lo que este test mira no es ninguna etapa —cada una tiene la suya— sino que **encajen**: que
    lo que una escribe sea lo que la siguiente lee, que los ids sobrevivan el viaje, y que el
    archivo que sale al final contenga el trabajo de las nueve anteriores.
    """
    workspace, scripted = pipeline

    # PREP: la ontología inicial entra y queda versionada, con sus glosas.
    normalized = prep.normalize(workspace)
    assert normalized.committed is not None
    assert normalized.pending_glosses == 3
    prep.generate_glosses(workspace, normalized)

    # PREP: el corpus entra.
    ingested = prep.ingest(workspace)
    assert ingested.executed == 2 and not ingested.failures

    # La fase la dicen los datos, no una bandera.
    assert sessions.observed_phase(workspace.conn, workspace.session_id) == sessions.PREP

    # ITER: menciones, correferencia, tipado.
    extracted = iterate.extract(workspace)
    assert extracted.mentions == 4          # dos por documento
    assert sessions.sync_phase(workspace.conn, workspace.session_id) == sessions.ITER

    iterate.corefer(workspace)
    matched = iterate.match(workspace)
    assert matched.total == 4
    assert matched.typed + matched.grey + matched.orphan == matched.total

    # ITER: lo que la ontología inicial no cubre se puentea o se induce.
    iterate.bridge(workspace)
    induced = iterate.induce(workspace)
    assert sorted(p.label for p in induced.proposals) == ["Field Note", "Focus Group"]

    # ITER: el código arma los axiomas y la cadena de validación decide.
    axiomatized = iterate.axiomatize(workspace)
    assert axiomatized.application is not None
    assert axiomatized.application.applied, (
        f"la cadena rechazó: {axiomatized.application.refused}"
    )

    # Los tipados son por versión: la versión que agregó las clases todavía no tipó nada. Volver
    # a tipar contra ella es el paso que el plan pide después de aplicar, y es lo que prueba que
    # las clases inducidas **se usan**, no sólo que se escribieron.
    rematched = iterate.match(workspace)
    assert rematched.version_id == axiomatized.application.committed.id
    assert rematched.typed == rematched.total, "las menciones tienen ahora una clase propia"

    # ITER-APPLY: el ABox se deriva de las menciones. Cuatro menciones son dos entidades —«focus
    # group» dos veces y «field note» dos veces— y cada una queda tipada.
    regenerated = iterate.regenerate(workspace)
    assert regenerated.wrote
    assert regenerated.result.n_individuals == 2
    assert regenerated.result.n_typed == 2

    # La entrega: una ontología con la historia aplicada, más su manifiesto.
    delivered = deliver.export(workspace)
    assert delivered.path.exists() and delivered.manifest_path.exists()
    assert delivered.minted_classes == 2, "las clases inducidas tienen que estar en el archivo"
    assert delivered.abox_quads > 0
    assert len(delivered.history) >= 2, "la raíz y al menos la versión que agregó las clases"

    # Y el historial cuenta lo que pasó, que es la otra mitad de la procedencia.
    kinds = [event.kind for event in sessions.history(workspace.conn, workspace.session_id)]
    assert kinds.count(sessions.STAGE) >= 2


def test_the_enriched_ontology_carries_the_induced_class_and_its_provenance(pipeline):
    """El entregable, leído como lo leería alguien de afuera: con rdflib, desde el archivo."""
    from rdflib import Dataset, URIRef
    from rdflib.namespace import OWL, RDF, SKOS

    workspace, _ = pipeline
    normalized = prep.normalize(workspace)
    prep.generate_glosses(workspace, normalized)
    prep.ingest(workspace)
    iterate.extract(workspace)
    iterate.match(workspace)
    iterate.bridge(workspace)
    iterate.induce(workspace)
    iterate.axiomatize(workspace)
    iterate.regenerate(workspace)

    delivered = deliver.export(workspace)
    graph = Dataset()
    graph.parse(delivered.path, format="trig")

    labels = {str(o) for _, p, o, _ in graph.quads((None, SKOS.prefLabel, None, None))}
    assert "Field Note" in labels, "la clase inducida no llegó al archivo"
    classes = {s for s, _, o, _ in graph.quads((None, RDF.type, OWL.Class, None))
               if isinstance(s, URIRef)}
    assert len(classes) == 5, "las tres iniciales más las dos inducidas"

    manifest = json.loads(delivered.manifest_path.read_text(encoding="utf-8"))
    assert manifest["lineage"][-1].endswith(":v0")
    assert manifest["classes_in_the_root_version"] == 3
