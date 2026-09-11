"""La sesión de usuario: qué la identifica, qué la deja avanzar, y qué la deja volver.

Lo que fijan estos tests es la diferencia entre guardar el estado y derivarlo. Guardarlo es lo
barato; derivarlo es lo que hace que el estado no pueda mentir. Acá conviven los dos —hay una
columna `phase`— y lo que se prueba es que la columna nunca gane.
"""

from __future__ import annotations

import pytest

from onto_pipeline import sessions
from onto_pipeline.db import connect


@pytest.fixture
def conn(tmp_path):
    store = connect(tmp_path)
    sessions.install(store)
    return store


def mention(conn, session_id, mention_id="m1"):
    conn.execute(
        "INSERT INTO mentions (id, session_id, document_id, page, surface_text, status) "
        "VALUES (?, ?, 'd1', 1, 'focus group', 'extracted')", (mention_id, session_id),
    )
    conn.commit()


# ─────────────────────────  identidad  ─────────────────────────


def test_the_id_says_what_it_runs_on_without_opening_anything(conn):
    """Un uuid sería único y no se podría teclear. El ordinal se cuenta dentro del caso de uso,
    así que dos casos de uso no compiten por el número."""
    first = sessions.create(conn, use_case="craft-cl")
    second = sessions.create(conn, use_case="craft-cl")
    other = sessions.create(conn, use_case="materiominer")

    assert (first.id, second.id, other.id) == ("craft-cl-1", "craft-cl-2", "materiominer-1")


def test_two_sessions_can_share_a_use_case(conn):
    """El caso de uso es material de entrada, inmutable: correr dos configuraciones sobre el
    mismo corpus es la comparación que el proyecto existe para poder hacer."""
    first = sessions.create(conn, use_case="craft-cl")
    second = sessions.create(conn, use_case="craft-cl")

    assert first.id != second.id
    assert first.use_case == second.use_case == "craft-cl"


def test_asking_for_a_session_that_is_not_there_says_so(conn):
    with pytest.raises(sessions.UnknownSession, match="no hay sesión"):
        sessions.load(conn, "nope-1")


# ─────────────────────────  la fase se deriva  ─────────────────────────


def test_a_new_session_is_preparing_material(conn):
    assert sessions.create(conn, use_case="craft-cl").phase == sessions.PREP


def test_iteration_starts_when_there_are_mentions_whatever_the_column_says(conn):
    """Las menciones son lo primero que la iteración produce y la preparación no. La columna se
    fuerza a mano para probar lo que importa: que los datos le ganen."""
    session = sessions.create(conn, use_case="craft-cl")
    mention(conn, session.id)

    assert sessions.observed_phase(conn, session.id) == sessions.ITER
    assert sessions.load(conn, session.id).phase == sessions.PREP   # la columna, todavía vieja
    assert sessions.sync_phase(conn, session.id) == sessions.ITER
    assert sessions.load(conn, session.id).phase == sessions.ITER


def test_the_phase_of_one_session_does_not_depend_on_another(conn):
    """El caso que hoy no existe y es la razón de todo esto: dos corridas a la vez."""
    working = sessions.create(conn, use_case="craft-cl")
    fresh = sessions.create(conn, use_case="craft-cl")
    mention(conn, working.id)

    assert sessions.observed_phase(conn, working.id) == sessions.ITER
    assert sessions.observed_phase(conn, fresh.id) == sessions.PREP


def test_a_closed_session_stays_closed(conn):
    """Cerrar es la única fase que se declara a mano, así que los datos no la pueden desmentir."""
    session = sessions.create(conn, use_case="craft-cl")
    mention(conn, session.id)
    sessions.close(conn, session.id, note="alcanza")

    assert sessions.sync_phase(conn, session.id) == sessions.CLOSED


# ─────────────────────────  volver atrás  ─────────────────────────


def test_reopening_says_what_it_leaves_behind_before_doing_it(conn):
    """La transición restringida. No se bloquea: se dice con números qué queda atrás y se
    pregunta, que es como se tratan las demás decisiones de este diseño."""
    session = sessions.create(conn, use_case="craft-cl")
    mention(conn, session.id)
    sessions.sync_phase(conn, session.id)

    with pytest.raises(sessions.PhaseViolation, match="deja atrás"):
        sessions.reopen_prep(conn, session.id, confirmed=False)
    assert sessions.load(conn, session.id).phase == sessions.ITER


def test_reopening_an_untouched_session_needs_no_confirmation(conn):
    """Preguntar cuando no hay nada que perder entrena a decir que sí sin leer."""
    session = sessions.create(conn, use_case="craft-cl")
    leaves = sessions.reopen_prep(conn, session.id, confirmed=False)

    assert leaves.empty
    assert sessions.load(conn, session.id).phase == sessions.PREP


def test_reopening_confirmed_deletes_nothing(conn):
    """No se borra: re-preparar commitea una raíz nueva y el linaje viejo queda alcanzable, que
    es lo que el DAG ya hace con las ramas no elegidas."""
    session = sessions.create(conn, use_case="craft-cl")
    mention(conn, session.id)
    sessions.sync_phase(conn, session.id)
    sessions.reopen_prep(conn, session.id, confirmed=True, why="otra ontología inicial")

    assert sessions.load(conn, session.id).phase == sessions.PREP
    assert conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"] == 1


# ─────────────────────────  el historial  ─────────────────────────


def test_creating_a_session_is_itself_the_first_event(conn):
    session = sessions.create(conn, use_case="craft-cl")
    events = sessions.history(conn, session.id)

    assert [event.kind for event in events] == [sessions.CREATED]
    assert "craft-cl" in events[0].summary


def test_the_history_is_append_only_and_keeps_the_reason(conn):
    """Un historial que se puede reescribir no es un historial. Su motivo de ser es contestar
    «¿por qué la ontología quedó así?» seis meses después."""
    session = sessions.create(conn, use_case="craft-cl")
    sessions.record(conn, session.id, sessions.STAGE, "extract", {"mentions": 848})
    mention(conn, session.id)
    sessions.sync_phase(conn, session.id)

    kinds = [event.kind for event in sessions.history(conn, session.id)]
    assert kinds.count(sessions.CREATED) == 1
    assert sessions.STAGE in kinds and sessions.PHASE in kinds
    stage = next(e for e in sessions.history(conn, session.id) if e.kind == sessions.STAGE)
    assert stage.payload == {"mentions": 848}


def test_one_session_does_not_see_the_history_of_another(conn):
    first = sessions.create(conn, use_case="craft-cl")
    second = sessions.create(conn, use_case="craft-cl")
    sessions.record(conn, first.id, sessions.STAGE, "extract")

    assert len(sessions.history(conn, first.id)) == 2
    assert len(sessions.history(conn, second.id)) == 1


def test_two_events_in_the_same_second_are_both_kept(conn):
    """El historial es append-only, y append-only quiere decir que no se pierde nada.

    El id se derivaba del instante con precisión de segundo y la inserción era `ON CONFLICT DO
    NOTHING`: aplicar axiomas y exportar, que corren en el mismo segundo, dejaban **un** evento.
    Lo encontró el test de punta a punta; ningún test de módulo registraba dos cosas tan juntas.
    """
    session = sessions.create(conn, use_case="craft-cl")
    sessions.record(conn, session.id, sessions.STAGE, "versión v1")
    sessions.record(conn, session.id, sessions.STAGE, "exportada v1")

    stages = [e.summary for e in sessions.history(conn, session.id) if e.kind == sessions.STAGE]
    assert sorted(stages) == ["exportada v1", "versión v1"]
