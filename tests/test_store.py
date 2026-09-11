"""La capa de acceso: el mismo contrato contra los dos motores.

Lo que fija este archivo es que **cambiar de motor no sea cambiar de código**. Los doce módulos
que tienen SQL escriben `?` y leen las filas por nombre; si eso deja de valer contra alguno de
los dos backends, la capa dejó de ser una capa y la migración vuelve a ser un barrido a mano.

Los casos de Postgres se saltean cuando no hay `ONTO_PIPELINE_TEST_DSN` — el mismo patrón que
`test_parse_text.py` usa con el corpus de CRAFT: la suite sigue corriendo sin red ni servidor, y
quien tenga un Postgres a mano prueba el otro lado sin tocar nada.
"""

from __future__ import annotations

import os

import pytest

from onto_pipeline import store

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
def conn(request, tmp_path):
    """Un almacén vacío contra el motor del parámetro.

    En Postgres se limpia al entrar y no al salir: si un test falla, la tabla queda para poder
    mirarla, que es la mitad de para qué sirve tener el motor de verdad a mano.
    """
    if request.param == store.SQLITE:
        opened = store.open_sqlite(tmp_path / "t.sqlite3")
    else:
        opened = store.open_postgres(DSN)
        opened.execute("DROP TABLE IF EXISTS probe")
        opened.commit()
    yield opened
    opened.close()


# ─────────────────────────  traducir sin romper el SQL  ─────────────────────────


def test_a_question_mark_inside_a_literal_is_not_a_parameter():
    """El caso que obliga a mirar los literales, y no es hipotético: un texto con signo de
    pregunta se convertiría en un parámetro que nadie pasa, y el error saldría lejos del lugar
    donde se escribió la consulta."""
    assert store.to_postgres("SELECT * FROM t WHERE a = ?") == "SELECT * FROM t WHERE a = %s"
    translated = store.to_postgres("SELECT * FROM t WHERE note = '¿y?' AND a = ?")
    assert translated == "SELECT * FROM t WHERE note = '¿y?' AND a = %s"


def test_a_percent_sign_survives_the_translation():
    """`%` es el escape de psycopg: un `LIKE '%x%'` sin escapar es un error de formato."""
    assert store.to_postgres("SELECT * FROM t WHERE a LIKE '%x%'") == (
        "SELECT * FROM t WHERE a LIKE '%%x%%'"
    )


def test_a_script_splits_on_semicolons_outside_literals():
    """`executescript` no existe en Postgres, y el esquema tiene textos con punto y coma."""
    parts = store.statements(
        "CREATE TABLE a (x TEXT DEFAULT 'uno;dos');\nCREATE INDEX i ON a(x);\n"
    )
    assert parts == ["CREATE TABLE a (x TEXT DEFAULT 'uno;dos')", "CREATE INDEX i ON a(x)"]


def test_a_backend_that_is_not_implemented_is_refused_loudly():
    """La misma regla que `matching.blocking_strategy`: un valor que no existe no pasa en
    silencio, porque el síntoma aparecería recién al primer `execute`."""
    with pytest.raises(store.StoreUnavailable, match="no implementado"):
        store.Store(raw=None, backend="mysql")


def test_postgres_without_a_dsn_says_so_instead_of_falling_back():
    """Caer a SQLite en silencio sería correr dos sesiones contra un motor que no las admite."""
    with pytest.raises(store.StoreUnavailable, match="dsn"):
        store.open_store(store.POSTGRES, dsn="")


# ─────────────────────────  el contrato, contra los dos  ─────────────────────────


def test_the_same_sql_runs_on_either_backend(conn):
    """El único `?` que escribe el código, y la única forma de leer una fila: por nombre."""
    conn.script("CREATE TABLE probe (id TEXT PRIMARY KEY, n INTEGER, note TEXT)")
    conn.execute("INSERT INTO probe (id, n, note) VALUES (?, ?, ?)", ("a", 1, "uno"))
    conn.commit()

    row = conn.execute("SELECT id, n, note FROM probe WHERE id = ?", ("a",)).fetchone()
    assert row["id"] == "a" and row["n"] == 1 and row["note"] == "uno"


def test_executemany_and_iteration_behave_the_same(conn):
    conn.script("CREATE TABLE probe (id TEXT PRIMARY KEY, n INTEGER)")
    conn.executemany("INSERT INTO probe (id, n) VALUES (?, ?)", [("a", 1), ("b", 2)])
    conn.commit()

    assert sorted(row["n"] for row in conn.execute("SELECT n FROM probe ORDER BY id")) == [1, 2]


def test_upsert_does_nothing_on_conflict_on_either_backend(conn):
    """Es lo que usa el ledger para no duplicar una unidad de trabajo, y la única sintaxis que
    los dos motores comparten literalmente."""
    conn.script("CREATE TABLE probe (id TEXT PRIMARY KEY, n INTEGER)")
    for value in (1, 2):
        conn.execute(
            "INSERT INTO probe (id, n) VALUES (?, ?) ON CONFLICT(id) DO NOTHING", ("a", value)
        )
    conn.commit()

    assert conn.execute("SELECT n FROM probe").fetchone()["n"] == 1


def test_introspection_answers_the_two_questions_the_code_asks(conn):
    """Las dos únicas cosas que no se escriben portable, y por eso viven acá y no en un módulo:
    si la tabla está, y qué columnas tiene."""
    assert conn.table_exists("probe") is False
    conn.script("CREATE TABLE probe (id TEXT PRIMARY KEY, n INTEGER)")
    conn.commit()

    assert conn.table_exists("probe") is True
    assert {"id", "n"} <= conn.columns("probe")


def test_a_rollback_undoes_what_was_not_committed(conn):
    conn.script("CREATE TABLE probe (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.execute("INSERT INTO probe (id) VALUES (?)", ("a",))
    conn.rollback()

    assert conn.execute("SELECT COUNT(*) AS n FROM probe").fetchone()["n"] == 0
