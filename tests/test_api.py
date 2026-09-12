"""La interfaz REST: que traduzca, y que no decida nada por su cuenta.

Cada test de acá mira una de las dos cosas que la API podría romper. La primera es la
autenticación, que falla cerrado porque una API que queda abierta por una variable que nadie
exportó no da ningún error. La segunda es `DECISION-NEVER-CROSSED`: `next` frena ante un punto de
decisión y `wizard` lo pregunta; por HTTP, encolar lo que viene después de una decisión que nadie
tomó sería tomarla por default.

Sin red y sin servidor: `TestClient`, SQLite y el almacén de objetos local.
"""

from __future__ import annotations

import pytest

from onto_pipeline.interfaces.api import create_app, jobs, uploads
from onto_pipeline.interfaces.api.auth import HEADER, MissingToken
from onto_pipeline.services import Workspace

KEY = "un-token-de-prueba"
AUTH = {HEADER: KEY}

_SEED_RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
         xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Class rdf:about="http://example.org/onto#Nota">
    <rdfs:label>Sobre el tema</rdfs:label></owl:Class>
</rdf:RDF>
"""


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Un despliegue entero en un directorio: corpus, ontología, almacén y artefactos."""
    monkeypatch.setenv("ONTO_PIPELINE_API_KEY", KEY)
    corpus = tmp_path / "use_cases" / "humo" / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "doc.txt").write_text(
        "El muestreo teórico guía la recolección de datos.", encoding="utf-8"
    )
    (tmp_path / "use_cases" / "humo" / "ontology.rdf").write_text(_SEED_RDF, encoding="utf-8")
    path = tmp_path / "config.yaml"
    path.write_text(
        "paths:\n"
        f"  corpus_root: {corpus}\n"
        f"  initial_ontology: {tmp_path / 'use_cases' / 'humo' / 'ontology.rdf'}\n"
        f"  work_dir: {tmp_path / 'data'}\n"
        f"  use_cases_root: {tmp_path / 'use_cases'}\n"
        "storage:\n  bucket: test-bucket\n"
        "llm:\n  provider: none\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def client(config_path):
    from fastapi.testclient import TestClient

    # Sin workers: los jobs se corren a mano en el test, que es lo que hace que no haya que
    # esperar a un hilo para saber qué pasó.
    with TestClient(create_app(config_path, start_workers=False)) as opened:
        yield opened


def _session(client, **body) -> str:
    response = client.post("/sessions", json={"use_case": "humo", **body}, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _drain(config_path) -> int:
    """Correr la cola en este hilo. Es lo que haría un worker, sin el hilo."""
    from onto_pipeline.interfaces.api.app import _runner

    pool = jobs.Pool(
        lambda: Workspace.open(config_path).conn, _runner(config_path), backend="sqlite"
    )
    return pool.drain()


# ─────────────────────────  autenticación  ─────────────────────────


def test_the_app_does_not_start_without_a_token(config_path, monkeypatch):
    """Falla cerrado. Arrancar abierta no daría ningún error y nadie se enteraría."""
    monkeypatch.delenv("ONTO_PIPELINE_API_KEY")

    with pytest.raises(MissingToken):
        create_app(config_path, start_workers=False)


def test_healthz_answers_without_a_token(client):
    """Es lo que mira el balanceador, que no tiene token."""
    assert client.get("/healthz").status_code == 200


@pytest.mark.parametrize("path", ["/stages", "/uploads", "/sessions"])
def test_everything_else_needs_the_header(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=AUTH).status_code == 200


# ─────────────────────────  el plan y la compuerta  ─────────────────────────


def test_the_plan_of_an_empty_session_says_what_to_run_first(client):
    """Sin versión todavía no es un error: es el estado de una sesión recién creada, y el plan es
    justamente lo que dice por dónde empezar."""
    session_id = _session(client)

    plan = client.get(f"/sessions/{session_id}/plan", headers=AUTH).json()

    assert plan["version"] == Workspace.NO_VERSION
    assert {step["name"] for step in plan["steps"]} >= {"ingest", "extract", "match"}
    assert next(step for step in plan["steps"] if step["name"] == "ingest")["state"] == "ready"


def test_a_stage_the_plan_blocks_is_not_queued_and_says_what_is_missing(client):
    """`DECISION-NEVER-CROSSED` sobre HTTP. `extract` sin nada parseado no es «falla después»:
    es que todavía no corresponde, y la respuesta lo dice en vez de encolar algo que va a fallar."""
    session_id = _session(client)

    response = client.post(f"/sessions/{session_id}/stages/match", json={}, headers=AUTH)

    assert response.status_code == 409
    assert "run extract" in response.json()["error"]


def test_a_stage_that_needs_the_model_says_so_instead_of_failing_late(client):
    """El proveedor no está configurado en este despliegue de prueba. Sin esto, el job se encola,
    corre y falla — y el que preguntó se entera dos minutos después."""
    session_id = _session(client)

    response = client.post(f"/sessions/{session_id}/stages/extract", json={}, headers=AUTH)

    assert response.status_code == 409
    assert "credencial" in response.json()["error"]


def test_an_unknown_stage_is_a_bad_request_and_lists_the_ones_there_are(client):
    session_id = _session(client)

    response = client.post(f"/sessions/{session_id}/stages/inventada", json={}, headers=AUTH)

    assert response.status_code == 400
    assert "ingest" in response.json()["error"]


def test_a_parameter_the_stage_does_not_accept_never_reaches_the_service(client):
    """Lo que llega por HTTP no se pasa como `**kwargs`: la lista de una etapa es cerrada."""
    session_id = _session(client)

    response = client.post(
        f"/sessions/{session_id}/stages/ingest",
        json={"params": {"rm": "-rf"}}, headers=AUTH,
    )

    assert response.status_code == 400
    assert "no acepta" in response.json()["error"]


# ─────────────────────────  jobs  ─────────────────────────


def test_a_long_stage_is_queued_and_polled_until_it_finishes(client, config_path):
    """El ciclo entero: encolar, correr, ver el resultado. Un request de cuarenta minutos no lo
    aguanta ningún proxy, así que lo que tarda devuelve un id."""
    session_id = _session(client)

    queued = client.post(f"/sessions/{session_id}/stages/ingest", json={}, headers=AUTH)
    assert queued.status_code == 202
    job_id = queued.json()["id"]
    assert client.get(f"/jobs/{job_id}", headers=AUTH).json()["status"] == "queued"

    assert _drain(config_path) == 1

    done = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert done["status"] == "done"
    assert done["result"]["executed"] == 1
    assert [item["id"] for item in client.get(
        f"/sessions/{session_id}/jobs", headers=AUTH).json()] == [job_id]


def test_a_session_with_a_job_in_flight_refuses_a_second_one(client):
    """El índice parcial, visto desde HTTP: 409 y no dos jobs activos."""
    session_id = _session(client)
    client.post(f"/sessions/{session_id}/stages/ingest", json={}, headers=AUTH)

    second = client.post(f"/sessions/{session_id}/stages/ingest", json={}, headers=AUTH)

    assert second.status_code == 409
    assert "ya tiene un job activo" in second.json()["error"]


def test_a_decision_is_refused_while_a_job_runs_on_that_session(client):
    """Contestar la zona gris con una etapa en curso es decidir sobre un estado que está
    cambiando debajo. Los `GET` no se frenan nunca."""
    session_id = _session(client)
    client.post(f"/sessions/{session_id}/stages/ingest", json={}, headers=AUTH)

    refused = client.post(
        f"/sessions/{session_id}/grey/m1", json={"none_of_these": True}, headers=AUTH
    )

    assert refused.status_code == 409
    assert client.get(f"/sessions/{session_id}/plan", headers=AUTH).status_code == 200
    assert client.get(f"/sessions/{session_id}/jobs", headers=AUTH).status_code == 200


def test_a_stage_error_travels_in_the_body_and_not_only_to_the_log(client, config_path, tmp_path):
    """Lo que el usuario tiene que arreglar llega como mensaje, que es lo mismo que el CLI pinta
    con `rich`. Un job que falla queda cerrado y con su motivo."""
    (tmp_path / "use_cases" / "humo" / "corpus" / "doc.txt").unlink()
    session_id = _session(client)
    job_id = client.post(
        f"/sessions/{session_id}/stages/ingest", json={}, headers=AUTH
    ).json()["id"]

    _drain(config_path)

    failed = client.get(f"/jobs/{job_id}", headers=AUTH).json()
    assert failed["status"] == "failed"
    assert "no hay documentos" in failed["error"]


def test_a_synchronous_stage_answers_in_the_request_that_asked_for_it(client, config_path):
    """`regenerate` es función pura de (menciones, tipados, reglas): no llama a nadie y no vale
    la pena poletearla."""
    session_id = _session(client)
    client.post(f"/sessions/{session_id}/stages/ingest", json={}, headers=AUTH)
    _drain(config_path)
    client.post(f"/sessions/{session_id}/stages/normalize", json={}, headers=AUTH)
    _drain(config_path)

    response = client.post(f"/sessions/{session_id}/stages/export", json={}, headers=AUTH)

    assert response.status_code == 200
    assert response.json()["result"]["version_id"].startswith(session_id)


# ─────────────────────────  uploads y artefactos  ─────────────────────────


def test_an_upload_hands_back_one_put_url_per_file(client):
    """El cliente sube contra el almacén: los bytes no pasan por la API."""
    response = client.post(
        "/uploads", json={"name": "corpus", "files": ["corpus/a.txt", "ontology.ttl"]},
        headers=AUTH,
    )

    assert response.status_code == 201
    body = response.json()
    assert sorted(body["put_urls"]) == ["corpus/a.txt", "ontology.ttl"]
    assert body["missing"] == ["corpus/a.txt", "ontology.ttl"], "todavía no subió nada"
    assert body["ontology"] == "ontology.ttl"


def test_a_session_over_an_upload_is_created_by_id(client, config_path):
    """Un upload y un caso publicado se usan en el mismo lugar
    (`API-UPLOADED-AND-PUBLISHED`)."""
    upload = client.post(
        "/uploads", json={"files": ["corpus/a.txt"]}, headers=AUTH
    ).json()

    created = client.post("/sessions", json={"upload_id": upload["id"]}, headers=AUTH)

    assert created.status_code == 201
    assert created.json()["use_case"] == f"upload:{upload['id']}"


def test_creating_a_session_over_an_upload_that_is_not_there_is_a_404(client):
    response = client.post("/sessions", json={"upload_id": "no-existe"}, headers=AUTH)

    assert response.status_code == 404
    assert "no hay upload" in response.json()["error"]


def test_deleting_an_upload_says_which_sessions_it_leaves_without_corpus(client):
    """Borrar es global y la respuesta lo dice: explícito, no un accidente."""
    upload = client.post("/uploads", json={"files": ["corpus/a.txt"]}, headers=AUTH).json()
    session_id = client.post(
        "/sessions", json={"upload_id": upload["id"]}, headers=AUTH
    ).json()["id"]

    response = client.delete(f"/uploads/{upload['id']}", headers=AUTH)

    assert response.status_code == 200
    assert response.json()["sessions_left_without_corpus"] == [session_id]


def test_an_artifact_is_downloaded_by_a_signed_url_and_not_through_the_api(
    client, config_path
):
    """`API-PRESIGNED-GET`. Un export de cien megas no tiene por qué pasar por el proceso que
    atiende HTTP."""
    session_id = _session(client)
    client.post(f"/sessions/{session_id}/stages/normalize", json={}, headers=AUTH)
    _drain(config_path)

    listed = client.get(f"/sessions/{session_id}/artifacts", headers=AUTH).json()
    assert [item["name"] for item in listed] == ["initial_normalized.ttl"]

    url = client.get(
        f"/sessions/{session_id}/artifacts/{listed[0]['key']}", headers=AUTH
    ).json()
    assert url["url"].startswith("https://")
    assert url["expires_s"] > 0


def test_one_session_cannot_ask_for_the_artifact_of_another(client, config_path):
    """La clave la propone el cliente. Es `SESSION-SCOPED-DATA` en el borde de HTTP: sin esto, pedir
    la de otra sesión es escribir otra ruta."""
    session_id = _session(client)
    other = _session(client)
    client.post(f"/sessions/{other}/stages/normalize", json={}, headers=AUTH)
    _drain(config_path)

    response = client.get(
        f"/sessions/{session_id}/artifacts/sessions/{other}/ontology/initial_normalized.ttl",
        headers=AUTH,
    )

    assert response.status_code == 400
    assert "no hay artefacto" in response.json()["error"]


# ─────────────────────────  la conexión  ─────────────────────────


def test_each_request_opens_and_closes_its_own_store(client, monkeypatch):
    """Una conexión global en el ciclo de vida de la app explota en SQLite por afinidad de hilo,
    y en Postgres **no** explota: compartir conexión es compartir transacción, y dos requests
    terminan commiteándose mutuamente trabajo a medio hacer."""
    from onto_pipeline.services import workspace as workspace_module

    opened, closed = [], []
    original = workspace_module.open_configured

    def counted(database, work_dir):
        store = original(database, work_dir)
        opened.append(store)
        close = store.close

        def closing():
            closed.append(store)
            close()

        store.close = closing
        return store

    monkeypatch.setattr(workspace_module, "open_configured", counted)
    client.get("/sessions", headers=AUTH)
    client.get("/sessions", headers=AUTH)

    assert len(opened) == 2
    assert len(closed) == 2


def test_a_job_left_running_by_a_dead_process_is_closed_when_the_app_starts(config_path):
    """El barrido de arranque. Sin él, una sesión con un job colgado no vuelve a aceptar nada:
    el índice parcial ve un activo que nunca va a terminar."""
    from fastapi.testclient import TestClient

    setup = Workspace.open(config_path)
    job = jobs.enqueue(setup.conn, session_id="quedó-colgada", stage="ingest")
    jobs.claim(setup.conn, job.id, "el-que-se-murió")
    setup.conn.close()

    with TestClient(create_app(config_path, start_workers=False)) as client:
        assert client.get(f"/jobs/{job.id}", headers=AUTH).json()["status"] == "failed"


def test_an_upload_and_a_published_use_case_are_listed_the_same_way(client):
    """No hay dos caminos: lo que la API sabe de un upload es lo que sabe de cualquier par
    (corpus, ontología)."""
    client.post("/uploads", json={"name": "uno", "files": ["corpus/a.txt"]}, headers=AUTH)

    listed = client.get("/uploads", headers=AUTH).json()

    assert [item["name"] for item in listed] == ["uno"]
    assert uploads.SESSION_MARK == "upload:"
