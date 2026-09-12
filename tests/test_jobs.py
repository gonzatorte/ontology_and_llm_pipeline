"""Los jobs: lo que tiene que seguir siendo cierto cuando corren dos cosas a la vez.

Nada de esto se ve fallar en una corrida sola, que es la razón de que los tests usen hilos de
verdad y conexiones separadas. Un reclamo que no fuera atómico devolvería el mismo job dos veces
y las dos corridas terminarían «bien»; una sesión con dos jobs activos escribiría la capa de
menciones desde dos lados. Las dos fallas son mudas.

Sin red ni servidor: SQLite alcanza para fijar las reglas, porque quien arbitra es la base y el
SQL es el mismo. Los cuatro primeros se corren además contra Postgres cuando hay uno
(`ONTO_PIPELINE_TEST_DSN`), que es el motor que se despliega.
"""

from __future__ import annotations

import os
import pathlib
import threading

import pytest

from onto_pipeline import jobs, store
from onto_pipeline.db import connect, prepare

DSN = os.environ.get("ONTO_PIPELINE_TEST_DSN", "")

BACKENDS = [
    pytest.param(store.SQLITE, id="sqlite"),
    pytest.param(
        store.POSTGRES, id="postgres",
        marks=pytest.mark.skipif(
            not DSN, reason="sin ONTO_PIPELINE_TEST_DSN; se prueba sólo sqlite"
        ),
    ),
]


@pytest.fixture(params=BACKENDS)
def open_conn(request, tmp_path):
    """Cómo abrir una conexión nueva contra el motor del parámetro.

    Es una **fábrica** y no una conexión: la mitad de lo que se prueba acá necesita dos
    conexiones en dos hilos, que es la única forma de que haya contención de verdad.

    El reclamo y el índice parcial son SQL nuevo, así que valen contra el motor que se despliega
    o no valen. En Postgres se limpia al entrar y no al salir: si un test falla, la tabla queda
    para poder mirarla.
    """
    work_dir = tmp_path / "work"

    def sqlite_conn():
        return connect(work_dir)

    def postgres_conn():
        return prepare(store.open_postgres(DSN))

    factory = sqlite_conn if request.param == store.SQLITE else postgres_conn
    if request.param == store.POSTGRES:
        first = factory()
        first.execute("DROP TABLE IF EXISTS jobs")
        first.commit()
        first.close()
    return factory


@pytest.fixture
def conn(open_conn):
    opened = open_conn()
    yield opened
    opened.close()


def _enqueued(conn, session="s1", stage="ingest") -> jobs.Job:
    return jobs.enqueue(conn, session_id=session, stage=stage)


def test_two_workers_cannot_claim_the_same_job(conn, open_conn):
    """El reclamo es compare-and-set y lo arbitra la base. Si dos workers pudieran quedarse con
    el mismo job, las dos corridas escribirían lo mismo dos veces y ninguna se quejaría."""
    job = _enqueued(conn)
    winners: list[bool] = []
    barrier = threading.Barrier(2)

    def claim() -> None:
        # Conexión propia: las de SQLite son afines al hilo que las creó, y compartir una en
        # Postgres es compartir transacción, que es peor porque no explota.
        own = open_conn()
        try:
            barrier.wait(timeout=5)
            winners.append(jobs.claim(own, job.id, threading.current_thread().name))
        finally:
            own.close()

    threads = [threading.Thread(target=claim, name=f"w{index}") for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert winners.count(True) == 1, winners


def test_a_session_cannot_have_two_active_jobs(conn):
    """El índice parcial único. Chequear y después insertar tiene una carrera entre el `SELECT` y
    el `INSERT`; el índice no tiene dónde metérsele."""
    first = _enqueued(conn)

    with pytest.raises(jobs.JobInFlight):
        _enqueued(conn, stage="extract")

    jobs.claim(conn, first.id, "w")
    jobs.finish(conn, first.id, result={"ok": True})

    assert _enqueued(conn, stage="extract").stage == "extract", "termina uno, entra el que sigue"


def test_another_session_is_not_blocked_by_the_first(conn):
    """El índice es por sesión, no global: dos sesiones son dos corridas independientes, que es
    para lo que existe Postgres acá."""
    _enqueued(conn, session="s1")

    assert _enqueued(conn, session="s2").session_id == "s2"


def test_a_decision_is_refused_while_a_job_runs_on_that_session(conn):
    """Contestar la zona gris con `match` en curso es decidir sobre un estado que está cambiando
    debajo. Cuando el job termina, la decisión pasa."""
    job = _enqueued(conn, stage="match")
    jobs.claim(conn, job.id, "w")

    with pytest.raises(jobs.JobInFlight):
        jobs.require_idle(conn, "s1")

    jobs.finish(conn, job.id, result={})
    jobs.require_idle(conn, "s1")


def test_more_than_one_worker_requires_postgres(open_conn):
    """Falla al arrancar y no cuando se nota. SQLite da un escritor: cuatro workers ahí son una
    cola de esperas que parece paralelismo."""
    with pytest.raises(RuntimeError, match="postgres"):
        jobs.Pool(open_conn, lambda job, progress: {}, workers=4, backend="sqlite")

    assert jobs.Pool(open_conn, lambda job, progress: {}, workers=1, backend="sqlite")


def test_each_unit_of_work_opens_and_closes_its_own_store(conn, open_conn):
    """Una conexión por unidad de trabajo. Compartirla entre hilos explota en SQLite por afinidad
    de hilo, y en Postgres **no** explota: compartir conexión es compartir transacción, y dos
    jobs terminan commiteándose mutuamente trabajo a medio hacer."""
    opened, closed = [], []

    def counted():
        opened_store = open_conn()
        opened.append(opened_store)
        original = opened_store.close

        def close():
            closed.append(opened_store)
            original()

        opened_store.close = close
        return opened_store

    _enqueued(conn)
    pool = jobs.Pool(counted, lambda job, progress: {"stage": job.stage}, backend="sqlite")
    pool.drain()
    _enqueued(conn, stage="extract")
    pool.drain()

    assert len(opened) == 2
    assert len(closed) == 2


def test_a_job_that_raises_is_closed_and_says_what_the_user_has_to_fix(conn):
    """Un job que muere sin cerrarse deja la sesión bloqueada para siempre: el índice parcial ve
    un activo que nunca termina. Y lo que el usuario tiene que arreglar viaja como mensaje; un
    bug del servidor, no — el traceback dice cosas que no son suyas."""
    from onto_pipeline.services import StageError

    job = _enqueued(conn)
    jobs.claim(conn, job.id, "w")

    def run(_job, _progress):
        raise StageError("no hay documentos bajo el corpus")

    jobs.execute(conn, job, run)
    failed = jobs.load(conn, job.id)

    assert failed.status == jobs.FAILED
    assert "no hay documentos" in failed.error


def test_an_internal_error_does_not_reach_the_client_as_a_traceback(conn, capsys):
    """El traceback va al log del proceso. Lo que vuelve dice que falló adentro de la etapa, sin
    rutas ni nombres del servidor."""
    job = _enqueued(conn)
    jobs.claim(conn, job.id, "w")

    def run(_job, _progress):
        raise ZeroDivisionError("division by zero")

    jobs.execute(conn, job, run)

    assert "division by zero" not in jobs.load(conn, job.id).error
    assert "ZeroDivisionError" in jobs.load(conn, job.id).error


def test_a_job_left_running_by_a_dead_process_does_not_block_the_session_forever(conn):
    """La otra cara del índice parcial: lo que lo hace correcto es también lo que puede trabar
    una sesión para siempre. Con una tarea fija, lo que está en `running` al arrancar es de un
    proceso que ya no está, así que el barrido lo cierra."""
    job = _enqueued(conn)
    jobs.claim(conn, job.id, "el-que-se-murió")

    with pytest.raises(jobs.JobInFlight):
        _enqueued(conn, stage="extract")

    swept = jobs.sweep_stale(conn)

    assert swept == [job.id]
    assert jobs.load(conn, job.id).status == jobs.FAILED
    assert _enqueued(conn, stage="extract").stage == "extract"


def test_progress_is_visible_before_the_job_finishes(conn):
    """Es lo único que tiene el que poletea para saber que algo pasa: sin esto, una etapa de
    cuarenta minutos es indistinguible de una colgada."""
    job = _enqueued(conn)
    jobs.claim(conn, job.id, "w")
    seen: list[str] = []

    def run(_job, progress):
        progress("ITER-EXTRACT · 3 de 40")
        seen.append(jobs.load(conn, job.id).progress)
        return {}

    jobs.execute(conn, job, run)

    assert seen == ["ITER-EXTRACT · 3 de 40"]


def test_two_sessions_run_at_once_and_each_counts_only_its_own(open_conn):
    """Dos sesiones corriendo a la vez, cada una con su conexión y su hilo. Lo que se fija es que
    el trabajo de una no aparezca en la contabilidad de la otra — la forma que tomó el bug que
    `SESSION-SCOPED-DATA` nombra."""
    from onto_pipeline import sessions

    setup = open_conn()
    for name in ("s1", "s2"):
        jobs.enqueue(setup, session_id=name, stage="ingest")
    sessions.install(setup)

    def worker(name: str) -> None:
        own = open_conn()
        try:
            job = jobs.claim_next(own, name)
            while job is not None:
                jobs.execute(own, job, lambda item, _p: {"session": item.session_id})
                job = jobs.claim_next(own, name)
        finally:
            own.close()

    threads = [threading.Thread(target=worker, args=(f"w{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    for name in ("s1", "s2"):
        finished = jobs.for_session(setup, name)
        assert [item.status for item in finished] == [jobs.DONE]
        assert finished[0].result == {"session": name}


# ── Lo que no se puede probar sin el entorno completo ──────────────────────────
#
# Se saltean en vez de faltar: son las dos piezas caras que dos jobs simultáneos comparten, y si
# alguna no tolerara hilos el paralelismo sería falso. Correrlas necesita los jars (o la JVM
# quedaría sin razonador) y el encoder bajado, que es lo que la suite por defecto no hace.

_LIB = pathlib.Path(__file__).resolve().parents[1] / "lib"


@pytest.mark.skipif(
    not list(_LIB.glob("*.jar")), reason="sin jars en lib/; ./scripts/fetch-jars.sh los baja"
)
def test_two_reasoner_instances_classify_at_once():
    """La JVM es una sola por proceso y se arranca una vez; la unidad de aislamiento es la
    instancia de `Reasoners`, que es por llamada. Si dos hilos no pudieran tener la suya, un
    worker tendría que esperar al otro para validar."""
    from rdflib import Graph

    from onto_pipeline.reasoning import Reasoners

    turtle = """
    @prefix owl: <http://www.w3.org/2002/07/owl#> .
    @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
    <http://x/A> a owl:Class . <http://x/B> a owl:Class ; rdfs:subClassOf <http://x/A> .
    """
    graph = Graph().parse(data=turtle, format="turtle")
    results, errors = [], []

    def classify() -> None:
        try:
            reasoner = Reasoners(_LIB)
            ontology = reasoner.load(graph)
            results.append(reasoner.elk(ontology, coverage_threshold=0.7))
        except Exception as exc:  # noqa: BLE001 - lo que se mira es que no haya ninguna
            errors.append(exc)

    threads = [threading.Thread(target=classify) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert not errors, errors
    assert len(results) == 2


@pytest.mark.skipif(
    not os.environ.get("ONTO_PIPELINE_TEST_ENCODER"),
    reason="sin ONTO_PIPELINE_TEST_ENCODER; el encoder se baja de la red la primera vez",
)
def test_the_shared_encoder_encodes_from_two_threads():
    """Los encoders tienen caché de proceso, así que dos jobs comparten el mismo objeto. Lo que
    se fija es que compartirlo no cambie el resultado: un vector distinto según quién lo pidió
    sería un tipado distinto según quién lo corrió."""
    from onto_pipeline.embeddings import SentenceTransformerEncoder

    encoder = SentenceTransformerEncoder()
    phrases = ["muestreo teórico", "codificación abierta"]
    serial = encoder.encode(phrases)
    parallel: list[list[list[float]]] = []

    def encode() -> None:
        parallel.append(SentenceTransformerEncoder().encode(phrases))

    threads = [threading.Thread(target=encode) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=300)

    assert parallel and all(vectors == serial for vectors in parallel)
