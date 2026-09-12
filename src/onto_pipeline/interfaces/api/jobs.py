"""Las etapas largas, corridas fuera del request que las pidió.

Una etapa que llama al modelo tarda minutos y `ingest` sobre un corpus real tarda más: un
request de cuarenta minutos no lo aguanta ningún proxy y no lo tolera ningún cliente
(`API-JOBS`). Así que la interfaz encola, contesta el id, y el que preguntó poletea.

**Parallel-safe por construcción y no como parche** (`API-PARALLEL-SAFE`). Dos cosas lo
sostienen, y las dos las arbitra la base y no la memoria del proceso:

1. **Un índice parcial único** sobre `session_id` mientras el job está en cola o corriendo. Dos
   `POST` simultáneos sobre la misma sesión no pueden producir dos jobs activos. Chequear y
   después insertar tiene una carrera; el índice no.
2. **El reclamo es un compare-and-set**: gana el `UPDATE` que ve `status='queued'`, y lo dice
   `cursor.rowcount == 1`. Deliberadamente **sin** `FOR UPDATE SKIP LOCKED`, que metería
   dialecto de Postgres fuera de `store.py` y rompería `STORE-NO-DIALECT`.

El default es un worker porque cuesta menos, pero subirlo es configuración y no una apuesta: el
mecanismo es el mismo para N hilos y para varias tareas. Lo que sí exige más de uno es Postgres,
porque SQLite da un escritor y N workers ahí son una cola de esperas.

**Una conexión por unidad de trabajo.** Cada job abre la suya y la cierra al terminar, en su
propio hilo. En SQLite compartirla explota por afinidad de hilo; en Postgres es peor, porque no
explota: compartir conexión es compartir transacción, y dos jobs terminan commiteándose
mutuamente trabajo a medio hacer.
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ...store import POSTGRES, Store

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
ACTIVE = (QUEUED, RUNNING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id           TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL,
  stage        TEXT NOT NULL,
  status       TEXT NOT NULL,
  params       TEXT NOT NULL DEFAULT '{}',
  progress     TEXT NOT NULL DEFAULT '',
  result       TEXT,
  warnings     TEXT NOT NULL DEFAULT '[]',
  error        TEXT NOT NULL DEFAULT '',
  worker       TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  started_at   TEXT,
  finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);

-- Lo que hace que dos POST simultáneos sobre la misma sesión no den dos jobs activos. Los
-- índices parciales existen igual en SQLite y en Postgres.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active
  ON jobs(session_id) WHERE status IN ('queued','running');
"""


class JobInFlight(RuntimeError):
    """Esa sesión ya tiene un job activo. No es un error del sistema: es la respuesta."""


class UnknownJob(KeyError):
    pass


@dataclass
class Job:
    id: str
    session_id: str
    stage: str
    status: str
    params: dict
    progress: str = ""
    result: dict | None = None
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    worker: str = ""
    created_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def finished(self) -> bool:
        return self.status in (DONE, FAILED)


def install(conn: Store) -> None:
    conn.script(SCHEMA)
    conn.commit()


# ─────────────────────────────  encolar  ─────────────────────────────


def enqueue(conn: Store, *, session_id: str, stage: str, params: dict | None = None) -> Job:
    """Poner un job en la cola, o decir que esa sesión ya tiene uno.

    El rechazo lo decide el índice y no una consulta previa: entre el `SELECT` y el `INSERT` de
    un chequeo hay lugar para el segundo `POST`.
    """
    install(conn)
    job = Job(
        id=uuid.uuid4().hex[:12], session_id=session_id, stage=stage, status=QUEUED,
        params=params or {}, created_at=_now(),
    )
    try:
        conn.execute(
            "INSERT INTO jobs (id, session_id, stage, status, params, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job.id, job.session_id, job.stage, job.status, json.dumps(job.params),
             job.created_at),
        )
        conn.commit()
    except conn.integrity_error as exc:
        # En Postgres la sentencia que falla aborta la transacción entera, así que el rollback
        # no es higiene: sin él la conexión queda inutilizable para lo que venga después.
        conn.rollback()
        running = active(conn, session_id)
        raise JobInFlight(
            f"la sesión {session_id} ya tiene un job activo"
            + (f": {running.stage} ({running.id})" if running else "")
        ) from exc
    return job


def active(conn: Store, session_id: str) -> Job | None:
    """El job en cola o corriendo de una sesión, si hay. Es lo que consulta la compuerta de las
    decisiones síncronas: contestar la zona gris mientras corre `match` es decidir sobre un
    estado que está cambiando."""
    install(conn)
    row = conn.execute(
        "SELECT * FROM jobs WHERE session_id = ? AND status IN ('queued','running') "
        "ORDER BY created_at LIMIT 1",
        (session_id,),
    ).fetchone()
    return _from_row(row) if row else None


def require_idle(conn: Store, session_id: str) -> None:
    """Frenar una decisión síncrona mientras la sesión tiene un job en vuelo.

    Contestar la zona gris mientras corre `match` es decidir sobre un estado que está cambiando
    debajo. Los `GET` no se frenan nunca: leer un estado a medias es incómodo, escribirlo es otra
    cosa.
    """
    running = active(conn, session_id)
    if running is not None:
        raise JobInFlight(
            f"la sesión {session_id} tiene {running.stage} en curso ({running.id}); "
            "lo que se decida ahora se decidiría sobre un estado que está cambiando"
        )


def load(conn: Store, job_id: str) -> Job:
    install(conn)
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise UnknownJob(f"no hay job {job_id!r}")
    return _from_row(row)


def for_session(conn: Store, session_id: str, *, limit: int = 50) -> list[Job]:
    install(conn)
    return [
        _from_row(row)
        for row in conn.execute(
            "SELECT * FROM jobs WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        )
    ]


# ─────────────────────────────  reclamar y cerrar  ─────────────────────────────


def claim(conn: Store, job_id: str, worker: str) -> bool:
    """Compare-and-set: gana quien vea la fila todavía en cola.

    La guardia `NOT EXISTS` es la segunda línea, para un almacén creado antes del índice: el
    índice cubre el insert, la guardia cubre que dos jobs de la misma sesión no corran juntos.
    """
    install(conn)
    cursor = conn.execute(
        "UPDATE jobs SET status = 'running', worker = ?, started_at = ? "
        " WHERE id = ? AND status = 'queued' "
        "   AND NOT EXISTS (SELECT 1 FROM jobs j2 "
        "                    WHERE j2.session_id = (SELECT session_id FROM jobs j3 WHERE j3.id = ?)"
        "                      AND j2.status = 'running')",
        (worker, _now(), job_id, job_id),
    )
    won = cursor.rowcount == 1
    conn.commit()
    return won


def claim_next(conn: Store, worker: str) -> Job | None:
    """El más viejo de la cola que se pueda reclamar. `None` si no hay nada que hacer."""
    install(conn)
    for row in conn.execute(
        "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 20"
    ).fetchall():
        if claim(conn, row["id"], worker):
            return load(conn, row["id"])
    return None


def note_progress(conn: Store, job_id: str, message: str) -> None:
    conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (message[:500], job_id))
    conn.commit()


def finish(conn: Store, job_id: str, *, result: dict, warnings: list[str] | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', result = ?, warnings = ?, finished_at = ? WHERE id = ?",
        (json.dumps(result, default=str), json.dumps(warnings or []), _now(), job_id),
    )
    conn.commit()


def fail(conn: Store, job_id: str, message: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
        (message[:4000], _now(), job_id),
    )
    conn.commit()


def sweep_stale(conn: Store) -> list[str]:
    """Los jobs que quedaron en `running` cuando murió el proceso, marcados como fallados.

    Se corre al arrancar. Con una tarea fija (`API-ECS-ONE-TASK`) nadie más puede estar
    corriéndolos, así que lo que está en `running` al arrancar es de un proceso que ya no está.
    Sin esto, el índice parcial deja esa sesión bloqueada para siempre: no se puede encolar nada
    porque hay un job activo que nunca va a terminar.
    """
    install(conn)
    stale = [
        row["id"]
        for row in conn.execute("SELECT id FROM jobs WHERE status = 'running'").fetchall()
    ]
    for job_id in stale:
        fail(conn, job_id, "el proceso que lo corría se terminó antes de que el job terminara")
    return stale


# ─────────────────────────────  correr  ─────────────────────────────


def execute(conn: Store, job: Job, run: Callable[[Job, Callable[[str], None]], dict]) -> None:
    """Correr un job ya reclamado y cerrarlo, pase lo que pase.

    Lo que el usuario tiene que arreglar viaja como mensaje —la interfaz lo muestra— y lo que es
    un bug se guarda entero para el que lea el log, pero nunca vuelve al cliente: el traceback
    dice cosas del servidor que no son suyas.
    """
    try:
        result = run(job, lambda message: note_progress(conn, job.id, message))
    except Exception as exc:  # noqa: BLE001 - un job que muere tiene que quedar cerrado
        fail(conn, job.id, _message(exc))
        return
    finish(conn, job.id, result=result, warnings=list(result.get("warnings") or []))


def _message(exc: Exception) -> str:
    from ...services import StageError

    if isinstance(exc, StageError):
        return str(exc)
    # El traceback va al log del proceso, no a la respuesta.
    traceback.print_exc()
    return f"{type(exc).__name__}: algo falló adentro de la etapa"


class Pool:
    """Los workers del proceso: hilos que reclaman, corren y cierran.

    `open_conn` abre una conexión nueva **en el hilo que la usa**, que es
    `ONE-CONNECTION-PER-UNIT`. `run` recibe el job y una función de progreso.
    """

    def __init__(
        self,
        open_conn: Callable[[], Store],
        run: Callable[[Job, Callable[[str], None]], dict],
        *,
        workers: int = 1,
        poll_s: float = 0.5,
        backend: str = "",
    ) -> None:
        if workers > 1 and backend != POSTGRES:
            raise RuntimeError(
                f"api.worker_count es {workers} y database.backend no es {POSTGRES}: SQLite da "
                "un escritor y varios workers ahí son una cola de esperas. Falla al arrancar "
                "para que no parezca que anda."
            )
        self.open_conn = open_conn
        self.run = run
        self.workers = workers
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for index in range(self.workers):
            thread = threading.Thread(
                target=self._loop, name=f"job-worker-{index}", args=(f"worker-{index}",),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout_s)
        self._threads.clear()

    def drain(self) -> int:
        """Correr lo que haya en la cola, en este hilo, y volver. Para los tests y para un
        proceso de un tiro; el servidor usa `start`."""
        done = 0
        conn = self.open_conn()
        try:
            while (job := claim_next(conn, "drain")) is not None:
                execute(conn, job, self.run)
                done += 1
        finally:
            conn.close()
        return done

    def _loop(self, worker: str) -> None:
        # La conexión es del hilo y vive lo que vive el hilo: abrirla una vez por job costaría
        # un connect por unidad de trabajo, que es lo que `DEBT-API-CONNECTION-POOL` anota.
        conn = self.open_conn()
        try:
            while not self._stop.is_set():
                job = claim_next(conn, worker)
                if job is None:
                    self._stop.wait(self.poll_s)
                    continue
                execute(conn, job, self.run)
        finally:
            conn.close()


def _from_row(row) -> Job:
    return Job(
        id=row["id"], session_id=row["session_id"], stage=row["stage"], status=row["status"],
        params=json.loads(row["params"] or "{}"),
        progress=row["progress"] or "",
        result=json.loads(row["result"]) if row["result"] else None,
        warnings=json.loads(row["warnings"] or "[]"),
        error=row["error"] or "", worker=row["worker"] or "",
        created_at=row["created_at"], started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
