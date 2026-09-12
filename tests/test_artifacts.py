"""Los artefactos: una clave por artefacto, y una sola función que la arma.

Lo que se fija acá no es que escribir y leer funcionen —eso es `test_objectstore.py`— sino que
el **nombre** del artefacto no dependa de quién lo pide. Cuando cada punto de uso armaba su
ruta, `normalize` escribía bajo la sesión y quien lo leía miraba `work_dir`: no fallaba, no
avisaba, simplemente no se encontraban.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from onto_pipeline.artifacts import Artifacts
from onto_pipeline.db import connect
from onto_pipeline.objectstore import LocalObjectStore
from onto_pipeline.services import StageError, Workspace, prep

SESSION = "test-1"

_SEED_RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="http://example.org/onto#Nota">
    <rdfs:label>Sobre el tema</rdfs:label></owl:Class>
</rdf:RDF>
"""


def _artifacts(tmp_path: Path, session: str = SESSION) -> Artifacts:
    return Artifacts(LocalObjectStore(tmp_path), session)


def _workspace(tmp_path: Path) -> Workspace:
    from onto_pipeline import sessions
    from onto_pipeline.config import Config

    config = Config.model_validate({
        "paths": {
            "corpus_root": tmp_path / "corpus",
            "initial_ontology": tmp_path / "seed.rdf",
            "work_dir": tmp_path / "work",
        }
    })
    conn = connect(config.paths.work_dir)
    created = sessions.create(conn, use_case="test")
    return Workspace.of(config, conn, session_id=created.id)


def test_two_sessions_do_not_share_an_abox(tmp_path):
    """`SESSION-SCOPED-DATA` sobre los artefactos: el ABox es dato derivado, y el id de versión
    ya lleva la sesión adentro, pero la clave lo dice igual para que se vea sin decodificar nada."""
    one = _artifacts(tmp_path, "s1").abox("s1:v1")
    other = _artifacts(tmp_path, "s2").abox("s2:v1")

    assert one.key != other.key
    assert one.key.startswith("sessions/s1/")
    assert other.key.startswith("sessions/s2/")


def test_a_version_id_does_not_reach_the_key_with_its_colon(tmp_path):
    """`sesión:v3` es el id, y los dos puntos son otra cosa en una URL y no son un carácter de
    nombre en Windows. El artefacto se descarga por URL, así que esto no es cosmético."""
    assert ":" not in _artifacts(tmp_path).abox("test-1:v3").key


def test_the_markdown_of_a_document_does_not_depend_on_the_session(tmp_path):
    """A propósito, y es la excepción: el id de documento sale del corpus, así que dos sesiones
    sobre el mismo corpus producen el mismo Markdown. Dos corpus distintos con ids que coinciden
    sí se pisarían — está anotado como `DEBT-API-DOCUMENTS-PATH` y no arreglado acá, porque mover
    la clave obliga a re-ingestar todo lo que ya está parseado."""
    assert _artifacts(tmp_path, "s1").markdown("doc").key == \
        _artifacts(tmp_path, "s2").markdown("doc").key


def test_writing_and_reading_an_artifact_needs_no_directory(tmp_path):
    """Nadie hace `mkdir` antes de escribir: en un almacén de objetos no hay directorios, y el
    `mkdir` esparcido era parte de lo que ataba las etapas al filesystem."""
    artifact = _artifacts(tmp_path).abox("test-1:v1")

    assert not artifact.exists()
    artifact.write_text("@prefix : <http://x/> .")

    assert artifact.exists()
    assert artifact.read_text().startswith("@prefix")


def test_an_artifact_can_be_brought_down_to_a_file_for_what_needs_a_path(tmp_path):
    """La JVM del razonador abre archivos, no claves. La copia vive lo que viva el directorio que
    le den, que es del que llama: acá es lo que hace que el contenedor no acumule nada."""
    artifact = _artifacts(tmp_path).normalized_ontology().write_text("x")

    local = artifact.materialize(tmp_path / "efimero")

    assert local.read_text(encoding="utf-8") == "x"
    assert local.name == "initial_normalized.ttl"


def test_the_listing_of_a_session_shows_only_its_own(tmp_path):
    """Es lo que la API contesta cuando preguntan qué hay para descargar."""
    mine = _artifacts(tmp_path, "s1")
    mine.abox("s1:v1").write_text("a")
    mine.normalized_ontology().write_text("b")
    _artifacts(tmp_path, "s2").abox("s2:v1").write_text("c")

    assert sorted(item.key for item in mine.listing()) == [
        "sessions/s1/ontology/initial_normalized.ttl",
        "sessions/s1/ontology/s1-v1.abox.trig",
    ]


def test_normalize_writes_where_evaluate_reads(tmp_path):
    """El canario del corte, y un bug que existió: `normalize` escribía la ontología normalizada
    bajo la sesión y dos lectores la buscaban bajo `work_dir`. Ninguno de los dos fallaba de
    forma visible — uno decía «corré normalize» después de haberlo corrido.

    Se prueba por la conducta y no por la ruta: si mañana la clave cambia, este test tiene que
    seguir pasando, y si vuelve a haber dos claves tiene que fallar.
    """
    workspace = _workspace(tmp_path)
    workspace.config.paths.initial_ontology.write_text(_SEED_RDF, encoding="utf-8")

    with pytest.raises(StageError, match="normalize"):
        prep.initial_graph(workspace)

    prep.normalize(workspace)

    assert len(prep.initial_graph(workspace)) > 0, "lo que escribió normalize"
    assert workspace.artifacts.normalized_ontology().exists()


def _string_literals(path: Path) -> set[str]:
    """Los literales del código, sin docstrings ni comentarios: nombrar un artefacto en prosa es
    documentación, y escribirlo en una expresión es la copia que se desincroniza."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    } - docstrings


def test_no_module_spells_an_artifact_name_by_hand():
    """Falla cerrado contra la reincidencia. El desalineo de `initial_normalized.ttl` fue posible
    porque el nombre estaba escrito en tres archivos; mientras viva en uno solo, escribir y leer
    no pueden discrepar. Se lee el código, como hace `tests/test_session_scope.py`.
    """
    package = Path(__file__).resolve().parents[1] / "src" / "onto_pipeline"
    offenders = {
        str(path.relative_to(package)): name
        for path in package.rglob("*.py")
        if path.name != "artifacts.py"
        for name in ("initial_normalized.ttl", ".abox.trig", ".enriched", "shapes.ttl")
        if any(name in literal for literal in _string_literals(path))
    }
    assert not offenders, offenders
