"""Los uploads: el par (corpus, ontología) puesto por quien usa la API.

Lo que se fija acá es lo que un upload tiene que seguir siendo para que la API no se convierta
en un pipeline paralelo: la misma forma que un caso de uso publicado, compartido entre sesiones
a propósito, y con la verdad de qué subió en el almacén y no en la tabla.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from onto_pipeline import sessions
from onto_pipeline.db import connect
from onto_pipeline.interfaces.api import uploads
from onto_pipeline.interfaces.api.deps import workspace as api_workspace
from onto_pipeline.services import StageError


@pytest.fixture
def store(object_store):
    return object_store


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "work")


def _upload(conn, store, *, files=("corpus/a.txt", "ontology.ttl")) -> uploads.Upload:
    upload = uploads.create(conn, name="prueba", filenames=list(files))
    for name in files:
        store.put(upload.key(name), f"contenido de {name}".encode())
    return upload


def test_an_upload_is_the_same_pair_as_a_published_use_case(conn):
    """La convención es una sola: lo que se llama `ontology.*` es la ontología y el resto es
    corpus. Si fueran dos convenciones, `_adopt_use_case` tendría que saber de dónde vino el
    material, que es exactamente lo que `API-UPLOADED-AND-PUBLISHED` evita."""
    upload = uploads.create(
        conn, name="x", filenames=["corpus/uno.txt", "corpus/dos.txt", "ontology.ttl"]
    )

    assert upload.ontology == "ontology.ttl"
    assert upload.corpus == ["corpus/dos.txt", "corpus/uno.txt"]


def test_an_upload_without_an_ontology_is_allowed_and_says_so(conn):
    """Un corpus sin ontología es un upload válido a medias: se puede ingestar y no se puede
    normalizar. Rechazarlo al crearlo obligaría a subir todo de una."""
    upload = uploads.create(conn, name="x", filenames=["corpus/uno.txt"])

    assert upload.ontology is None
    assert upload.corpus == ["corpus/uno.txt"]


def test_what_is_missing_is_asked_to_the_store_and_not_to_the_table(conn, store):
    """El pre-signed PUT no avisa cuándo terminó: la tabla dice qué se prometió subir, y lo que
    hay de verdad lo contesta el almacén. Creerle a la tabla es ingestar medio corpus."""
    upload = uploads.create(conn, name="x", filenames=["corpus/a.txt", "corpus/b.txt"])
    store.put(upload.key("corpus/a.txt"), b"a")

    assert uploads.present(store, upload) == ["corpus/a.txt"]
    assert uploads.missing(store, upload) == ["corpus/b.txt"]


def test_a_put_url_is_handed_out_per_file(conn, store):
    """El cliente sube contra el almacén y no contra la API: un corpus de cien megas no tiene
    por qué pasar por el proceso que atiende HTTP."""
    upload = uploads.create(conn, name="x", filenames=["corpus/a.txt", "ontology.ttl"])

    urls = uploads.put_urls(store, upload, expires_s=60)

    assert sorted(urls) == ["corpus/a.txt", "ontology.ttl"]
    assert all(urls.values())


def test_a_filename_that_could_escape_the_prefix_is_refused(conn):
    """El nombre lo propone el cliente. Sin esto, `../../otro/upload` escribe en otro lado."""
    with pytest.raises(ValueError, match="clave inválida"):
        uploads.create(conn, name="x", filenames=["../fuera.txt"])


def test_a_session_over_an_upload_is_read_back_as_such(conn, store, tmp_path):
    """Sin columna nueva, porque no hay sistema de migraciones: el upload va en `use_case`, que
    es donde ya vive «sobre qué corre esta sesión»."""
    upload = _upload(conn, store)
    created = sessions.create(conn, use_case=upload.use_case)

    loaded = sessions.load(conn, created.id)

    assert uploads.session_upload(loaded.use_case) == upload.id
    assert uploads.session_upload("craft-cl") is None


def test_deleting_an_upload_is_global_and_names_the_sessions_it_leaves_behind(conn, store):
    """`API-SHARED-UPLOADS` en su parte incómoda: borrar es para todos. Que devuelva las sesiones
    afectadas es lo que hace que sea explícito y no un accidente."""
    upload = _upload(conn, store)
    first = sessions.create(conn, use_case=upload.use_case)
    second = sessions.create(conn, use_case=upload.use_case)

    affected = uploads.delete(conn, store, upload.id)

    assert sorted(affected) == sorted([first.id, second.id])
    assert uploads.present(store, upload) == []
    with pytest.raises(uploads.UnknownUpload):
        uploads.load(conn, upload.id)


def test_materializing_keeps_the_corpus_structure(conn, store, tmp_path):
    """El id de documento sale de la ruta relativa al corpus, así que la estructura se conserva:
    si no, el mismo upload ingestado dos veces daría ids distintos y el caché no serviría."""
    upload = _upload(conn, store, files=("corpus/sub/a.txt", "ontology.ttl"))

    corpus, ontology = uploads.materialize(store, upload, tmp_path / "efimero")

    assert (corpus / "sub" / "a.txt").exists()
    assert ontology is not None and ontology.name == "ontology.ttl"


def _config_file(tmp_path) -> Path:
    """Un config de verdad en disco: la dependencia de la API abre el workspace desde el archivo,
    como en un request."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "paths:\n"
        f"  corpus_root: {tmp_path / 'no-existe'}\n"
        f"  initial_ontology: {tmp_path / 'tampoco.rdf'}\n"
        f"  work_dir: {tmp_path / 'work'}\n"
        "storage:\n  bucket: test-bucket\n",
        encoding="utf-8",
    )
    return path


def test_the_inputs_of_an_upload_session_are_brought_down_by_the_api_and_not_by_the_core(
    tmp_path, conn, store
):
    """El core sabe abrir un workspace sobre archivos; qué es un upload lo sabe la API. Baja el
    material a un directorio efímero, apunta `paths` ahí —lo mismo que `--corpus-root` en el
    CLI— y lo borra al terminar la unidad de trabajo."""
    upload = _upload(conn, store)
    created = sessions.create(conn, use_case=upload.use_case)
    conn.close()

    with api_workspace(_config_file(tmp_path), created.id) as opened:
        corpus = opened.config.paths.corpus_root
        assert corpus.is_dir() and (corpus / "a.txt").exists()
        assert opened.config.paths.initial_ontology.name == "ontology.ttl"

    assert not corpus.exists(), "lo efímero se borra al cerrar"


def test_a_stage_refuses_to_run_on_an_upload_that_is_still_incomplete(tmp_path, conn, store):
    """Lo contrario sería correr sobre medio corpus sin decirlo, que es la forma de falla que
    este proyecto colecciona: no rompe, da menos."""
    upload = uploads.create(conn, name="x", filenames=["corpus/a.txt", "corpus/b.txt"])
    store.put(upload.key("corpus/a.txt"), b"a")
    created = sessions.create(conn, use_case=upload.use_case)
    conn.close()

    with pytest.raises(StageError, match="falta"), api_workspace(
        _config_file(tmp_path), created.id
    ):
        pass


def test_a_published_use_case_is_left_alone(tmp_path, conn):
    """Sobre un caso publicado no hay nada que bajar, y `paths` queda como lo dejó el config."""
    created = sessions.create(conn, use_case="craft-cl")
    conn.close()

    with api_workspace(_config_file(tmp_path), created.id) as opened:
        assert opened.config.paths.corpus_root.name == "no-existe"


def test_the_seed_script_reads_a_use_case_the_way_the_pipeline_does(tmp_path):
    """La siembra usa el mismo mecanismo que un upload cualquiera, así que lo único suyo es
    juntar los archivos: el corpus con su estructura y el `ontology.*` de al lado."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "seed_use_cases",
        Path(__file__).resolve().parents[1] / "scripts" / "seed_use_cases.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    (tmp_path / "corpus" / "sub").mkdir(parents=True)
    (tmp_path / "corpus" / "sub" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "ontology.rdf").write_text("<rdf/>", encoding="utf-8")

    assert sorted(module.files_of(tmp_path)) == ["corpus/sub/a.txt", "ontology.rdf"]


def test_a_file_outside_the_use_case_layout_is_refused(conn):
    """La forma de adentro es la de un caso de uso publicado. Aceptar cualquier cosa sería
    dejar archivos que no se ingestan y que nadie ve faltar."""
    with pytest.raises(ValueError, match="corpus/"):
        uploads.create(conn, name="x", filenames=["suelto.txt"])
