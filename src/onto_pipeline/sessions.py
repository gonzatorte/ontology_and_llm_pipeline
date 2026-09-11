"""La sesión de usuario: una corrida sobre un caso de uso, con su estado y su historial.

**Qué es una sesión, y qué no.** Un *caso de uso* es el par (ontología inicial, corpus), y es
material de entrada: inmutable, y compartido — dos sesiones pueden correr sobre `craft-cl` con
distinta configuración, que es justamente la comparación que este proyecto existe para poder
hacer. Una *sesión de usuario* es todo lo derivado de correr el pipeline sobre uno: las
menciones, los tipados, las versiones de la ontología, y las decisiones que tomó la persona.

**La fase se deriva, no se guarda como verdad.** Hay una columna `phase`, pero es una
afirmación: la verdad sale de los datos, igual que `orchestration.survey`, que nunca puede
mentir porque cuenta filas. Guardar el estado en un solo lugar y creerle es cómo dos lugares
empiezan a discrepar — y acá el que discrepa es el que decide qué etapa corre.

**Volver atrás no borra: ramifica.** Re-hacer la preparación estando en iteración no invalida lo
hecho, commitea una raíz nueva y el linaje viejo queda alcanzable, que es lo que el DAG ya hace
con las ramas no elegidas (`ITER-APPLY`, `REORG-PATH-DEPENDENCE`). Lo que esta capa aporta es
**decir qué quedaría atrás antes de hacerlo**; la decisión es del usuario, como todas las demás.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_sessions (
  id          TEXT PRIMARY KEY,
  name        TEXT,
  use_case    TEXT NOT NULL,        -- el directorio bajo use_cases_root
  phase       TEXT NOT NULL,        -- prep | iter | closed; afirmación, no verdad
  note        TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- El historial, append-only. Las *elecciones* del usuario siguen viviendo donde ya vivían
-- (`grey_decisions`, `decisions`, `review_items`): acá se referencian, no se duplican, porque
-- dos copias de una decisión es cómo una de las dos queda vieja.
CREATE TABLE IF NOT EXISTS session_events (
  id          TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  at          TEXT NOT NULL,
  kind        TEXT NOT NULL,
  summary     TEXT NOT NULL,
  payload     TEXT,
  FOREIGN KEY (session_id) REFERENCES user_sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON session_events(session_id, at);
"""

# Las fases. Son las del spec, no un vocabulario nuevo: `PREP` prepara el material y `ITER`
# itera sobre él. `CLOSED` es la única que se declara a mano — el resto se lee de los datos.
PREP = "prep"
ITER = "iter"
CLOSED = "closed"
PHASES = (PREP, ITER, CLOSED)

# Los tipos de evento. Cerrados a propósito: un log donde cada quien inventa su categoría no se
# puede leer de a mil líneas.
CREATED = "created"
STAGE = "stage"
DECISION = "decision"
PHASE = "phase"
REOPENED = "reopened"

_SLUG = re.compile(r"[^a-z0-9]+")


class UnknownSession(ValueError):
    """Se pidió una sesión que no está."""


class PhaseViolation(ValueError):
    """La transición pedida deja atrás trabajo, y nadie dijo que estaba bien."""


@dataclass
class UserSession:
    id: str
    use_case: str
    phase: str
    name: str = ""
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass
class Event:
    id: str
    session_id: str
    at: str
    kind: str
    summary: str
    payload: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _instant() -> str:
    """Con microsegundos: dos eventos del mismo segundo tienen que quedar en orden."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def install(conn: Store) -> None:
    conn.script(SCHEMA)
    conn.commit()


# ─────────────────────────────  crear, leer, listar  ─────────────────────────────


def next_id(conn: Store, use_case: str) -> str:
    """Un id legible y único: el caso de uso más un ordinal dentro de él.

    `craft-cl-1`, `craft-cl-2`. Un uuid sería único y no se podría teclear; el ordinal se cuenta
    entre las sesiones **de ese caso de uso**, así que dos casos de uso no compiten por el
    número y el id dice sobre qué corre sin abrir nada.
    """
    slug = _SLUG.sub("-", use_case.lower()).strip("-") or "session"
    taken = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM user_sessions WHERE use_case = ?", (use_case,)
        )
    }
    ordinal = 1
    while f"{slug}-{ordinal}" in taken:
        ordinal += 1
    return f"{slug}-{ordinal}"


def create(conn: Store, *, use_case: str, name: str = "", note: str = "") -> UserSession:
    install(conn)
    session = UserSession(
        id=next_id(conn, use_case), use_case=use_case, phase=PREP, name=name, note=note,
        created_at=_now(), updated_at=_now(),
    )
    conn.execute(
        "INSERT INTO user_sessions (id, name, use_case, phase, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session.id, session.name, session.use_case, session.phase, session.note,
         session.created_at, session.updated_at),
    )
    conn.commit()
    record(conn, session.id, CREATED, f"sesión creada sobre {use_case}")
    return session


def load(conn: Store, session_id: str) -> UserSession:
    install(conn)
    row = conn.execute(
        "SELECT * FROM user_sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise UnknownSession(f"no hay sesión {session_id!r}")
    return _from_row(row)


def all_sessions(conn: Store) -> list[UserSession]:
    install(conn)
    return [
        _from_row(row)
        for row in conn.execute("SELECT * FROM user_sessions ORDER BY created_at, id")
    ]


def _from_row(row: Any) -> UserSession:
    return UserSession(
        id=row["id"], use_case=row["use_case"], phase=row["phase"], name=row["name"] or "",
        note=row["note"] or "", created_at=row["created_at"], updated_at=row["updated_at"],
    )


# ─────────────────────────────  el historial  ─────────────────────────────


def record(
    conn: Store, session_id: str, kind: str, summary: str, payload: dict | None = None
) -> Event:
    """Agregar un evento. Append-only: nada de acá se edita ni se borra.

    Un historial que se puede reescribir no es un historial — y el motivo de tener uno es poder
    contestar «¿por qué la ontología quedó así?» seis meses después.
    """
    install(conn)
    at = _instant()
    # El id no puede derivarse del instante: con precisión de segundo y `ON CONFLICT DO NOTHING`,
    # dos eventos del mismo tipo en el mismo segundo —aplicar axiomas y exportar— colisionaban y
    # el segundo **se descartaba sin error**. Un historial append-only que pierde entradas.
    event = Event(
        id=f"{session_id}:{uuid.uuid4().hex}", session_id=session_id, at=at, kind=kind,
        summary=summary, payload=payload or {},
    )
    conn.execute(
        "INSERT INTO session_events (id, session_id, at, kind, summary, payload) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
        (event.id, event.session_id, event.at, event.kind, event.summary,
         json.dumps(event.payload, ensure_ascii=False)),
    )
    conn.execute(
        "UPDATE user_sessions SET updated_at = ? WHERE id = ?", (at, session_id)
    )
    conn.commit()
    return event


def history(conn: Store, session_id: str, *, limit: int = 0) -> list[Event]:
    install(conn)
    query = "SELECT * FROM session_events WHERE session_id = ? ORDER BY at DESC, id DESC"
    if limit:
        query += f" LIMIT {int(limit)}"
    return [
        Event(
            id=row["id"], session_id=row["session_id"], at=row["at"], kind=row["kind"],
            summary=row["summary"], payload=json.loads(row["payload"] or "{}"),
        )
        for row in conn.execute(query, (session_id,))
    ]


# ─────────────────────────────  la fase  ─────────────────────────────


def observed_phase(conn: Store, session_id: str) -> str:
    """La fase que dicen los datos, que es la que vale.

    `ITER` empieza cuando hay menciones: es la primera cosa que la iteración produce y que la
    preparación no. Antes de eso la sesión está preparando material, tenga la columna lo que
    tenga.
    """
    if not conn.table_exists("mentions"):
        return PREP
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM mentions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return ITER if row and row["n"] else PREP


def sync_phase(conn: Store, session_id: str) -> str:
    """Poner la columna de acuerdo con los datos, y registrar la transición si la hubo.

    La columna existe para poder listar sesiones sin contar filas de cada una. Que se
    resincronice acá es lo que evita que alguien la lea y crea algo que los datos desmienten.
    """
    stored = load(conn, session_id)
    if stored.phase == CLOSED:
        return CLOSED
    observed = observed_phase(conn, session_id)
    if observed != stored.phase:
        conn.execute(
            "UPDATE user_sessions SET phase = ?, updated_at = ? WHERE id = ?",
            (observed, _now(), session_id),
        )
        conn.commit()
        record(conn, session_id, PHASE, f"{stored.phase} → {observed}")
    return observed


def close(conn: Store, session_id: str, *, note: str = "") -> UserSession:
    conn.execute(
        "UPDATE user_sessions SET phase = ?, updated_at = ? WHERE id = ?",
        (CLOSED, _now(), session_id),
    )
    conn.commit()
    record(conn, session_id, PHASE, f"cerrada{f': {note}' if note else ''}")
    return load(conn, session_id)


@dataclass
class Reopening:
    """Qué quedaría atrás si la sesión vuelve a preparar material."""

    session_id: str
    versions: int
    mentions: int
    decisions: int

    @property
    def empty(self) -> bool:
        return not (self.versions or self.mentions or self.decisions)

    @property
    def summary(self) -> str:
        return (
            f"{self.versions} versión(es), {self.mentions} menciones y "
            f"{self.decisions} decisión(es) ya tomadas"
        )


def what_reopening_leaves_behind(conn: Store, session_id: str) -> Reopening:
    """Lo que hay que poder decir **antes** de volver a la preparación.

    No se borra nada: re-preparar commitea una raíz nueva y el linaje viejo queda alcanzable. Lo
    que sí pasa es que el trabajo hecho deja de estar en el camino que la sesión sigue, y eso
    tiene que decirse con números antes de preguntar, no después.
    """
    def count(table: str, where: str) -> int:
        if not conn.table_exists(table):
            return 0
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", (session_id,)  # noqa: S608
        ).fetchone()
        return int(row["n"]) if row else 0

    return Reopening(
        session_id=session_id,
        versions=count("versions", "session_id = ?"),
        mentions=count("mentions", "session_id = ?"),
        decisions=count("decisions", "session_id = ?"),
    )


def reopen_prep(conn: Store, session_id: str, *, confirmed: bool, why: str = "") -> Reopening:
    """Volver a la fase de preparación. Pide confirmación si deja trabajo atrás."""
    leaves = what_reopening_leaves_behind(conn, session_id)
    if not leaves.empty and not confirmed:
        raise PhaseViolation(
            f"volver a preparación deja atrás {leaves.summary}. No se borra nada —el linaje "
            "viejo queda alcanzable— pero la sesión sigue por otro camino."
        )
    conn.execute(
        "UPDATE user_sessions SET phase = ?, updated_at = ? WHERE id = ?",
        (PREP, _now(), session_id),
    )
    conn.commit()
    record(
        conn, session_id, REOPENED, why or "vuelta a preparación",
        {"left_behind": leaves.summary},
    )
    return leaves
