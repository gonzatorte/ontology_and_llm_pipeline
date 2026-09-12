"""Ninguna consulta sobre una tabla por sesión puede olvidarse de filtrar por ella.

**Por qué un test y no la disciplina.** Olvidarse de un `WHERE session_id = ?` no rompe nada
visible: devuelve filas de más, o borra las de otro, y los tests de esa etapa siguen pasando
porque corren sobre una sesión sola. Es la misma forma de falla que
`FINDINGS-SILENT-FAILURES` colecciona — pasa cuando debería fallar — y la única defensa que
escala a sesenta consultas es que algo las cuente.

Esto lee el código fuente, no la base: una consulta que todavía no se ejecutó nunca igual
aparece acá.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src" / "onto_pipeline"

# Las tablas cuyo contenido pertenece a una sesión de usuario. Las que no están acá o son
# compartidas a propósito (`work_units` tiene su propia regla, abajo) o cuelgan de `version_id`,
# que ya lleva la sesión adentro.
SCOPED = (
    "documents", "blocks", "page_classification", "mentions", "versions",
    "competency_questions", "cq_results", "decisions", "assertion_marks", "grey_decisions",
    "review_items", "jobs",
)

_STATEMENT = re.compile(
    r"(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b[^\"']*?(?=\"|')", re.IGNORECASE | re.DOTALL
)


def statements() -> list[tuple[Path, str]]:
    """Cada literal SQL del código, con el archivo en el que vive.

    Se reconstruyen las cadenas partidas en varias líneas —el estilo del proyecto— concatenando
    los literales adyacentes de una misma llamada.
    """
    found = []
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for chunk in re.findall(r'(?:"[^"]*"\s*)+', text):
            sql = " ".join(re.findall(r'"([^"]*)"', chunk))
            if re.search(r"\b(FROM|INTO|UPDATE)\b", sql, re.IGNORECASE):
                found.append((path, " ".join(sql.split())))
    return found


def touches(sql: str, table: str) -> bool:
    return bool(re.search(rf"\b(FROM|INTO|UPDATE|JOIN)\s+{table}\b", sql, re.IGNORECASE))


# Un id de versión es `<sesión>:v<N>`, único globalmente: filtrar por él ya acota la sesión, y
# es lo que deja que las seis tablas colgadas de `version_id` no lleven columna propia. El id de
# un job es un uuid, también único: tocar **una** fila por id no puede alcanzar a otra sesión.
_BY_VERSION_ID = re.compile(r"\b(id|version_id|parent_id)\s*=\s*\?", re.IGNORECASE)
_UNIQUE_ROW_ID = ("versions", "jobs")


# La única consulta que lee una tabla por sesión sin filtrar, porque lee la tabla **anterior** a
# la columna: en un almacén que precede a las sesiones no hay `session_id` que filtrar. Que
# además la rellene lo fija el test de abajo.
_BEFORE_SESSIONS = ("SELECT * FROM review_items",)


# La cola de jobs es lo único que se mira entre sesiones, y tiene que serlo: un worker reclama el
# job más viejo **de cualquiera**, porque atender a una sesión sola sería no tener cola. El
# reclamo escribe por `id`, que es único, y el barrido de arranque cierra lo que quedó colgado de
# un proceso muerto — las dos cosas son del proceso y no de una sesión.
_QUEUE_WIDE = (
    "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at, id LIMIT 20",
    "SELECT id FROM jobs WHERE status = 'running'",
)


def test_every_query_on_a_per_session_table_filters_by_the_session():
    offenders = []
    for path, sql in statements():
        if "session_id" in sql or sql in _BEFORE_SESSIONS or sql in _QUEUE_WIDE:
            continue
        if any(touches(sql, table) for table in _UNIQUE_ROW_ID) and _BY_VERSION_ID.search(sql):
            continue
        for table in SCOPED:
            if touches(sql, table):
                offenders.append(f"{path.name}: {sql[:90]}")
                break
    assert not offenders, "consultas sin sesión:\n" + "\n".join(sorted(offenders))


def test_the_work_unit_cache_is_looked_up_across_sessions_on_purpose():
    """La excepción, y tiene que ser explícita.

    La clave de `work_units` es content-addressed —cubre etapa, prompt, modelo, effort,
    temperatura y hash del input— así que el **resultado** se comparte y nadie paga dos veces
    por la misma pregunta. Lo que no se comparte es la contabilidad: el barrier pregunta si
    quedan unidades **propias** corriendo, y sin eso una sesión interrumpida bloquea a las demás.
    """
    telemetry = (SOURCE / "telemetry.py").read_text(encoding="utf-8")

    assert "WHERE key = ? AND status = 'done'" in " ".join(telemetry.split()), (
        "la búsqueda en el caché tiene que ser por clave sola, entre sesiones"
    )
    assert "session_id = ? AND stage = ?" in " ".join(telemetry.split()), (
        "el barrier y el reporte por etapa tienen que ser de la sesión, no de la tabla entera"
    )


def test_the_exempt_query_is_a_migration_that_fills_the_column_it_lacks():
    """La otra excepción, y la razón por la que se le puede permitir leer sin filtro.

    `_BEFORE_SESSIONS` exime una consulta sobre `review_items`. Vale sólo mientras sea la
    migración: lee la tabla que precede a la columna y la rellena desde `version_id`, que es
    `<sesión>:v<N>`. Si algún día esa consulta deja de rellenarla, la exención quedaría tapando
    una consulta sin sesión de verdad.
    """
    review = (SOURCE / "review.py").read_text(encoding="utf-8")
    migration = review[review.index("def _rescue_from_before_sessions"):]

    assert 'str(row["version_id"] or "").split(":")[0]' in migration, (
        "la migración tiene que deducir la sesión del id de versión"
    )
    assert "INSERT INTO review_items (id, session_id," in review, (
        "y escribirla, o las filas rescatadas quedarían sin sesión"
    )
