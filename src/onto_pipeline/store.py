"""El almacén, sin dialecto: una interfaz para SQLite y para Postgres.

**Por qué existe.** Hasta el 2026-09-10 el SQL crudo vivía repartido por los módulos de dominio
con `sqlite3` importado en cada uno, y `DEBT-DATA-ACCESS-LAYER` decía que la inversión no es migrar
sino escribir esta capa y que el barrido lo haga una sola vez. La necesidad llegó con las
sesiones de usuario en paralelo: SQLite admite un escritor y muchos lectores, y dos corridas
escribiendo a la vez es exactamente lo que no admite.

**Lo que hace, y es poco a propósito.** Casi toda la diferencia entre los dos motores se arregla
escribiendo SQL portable, no traduciendo: `COALESCE` en vez de `IFNULL`, `CASE WHEN` en vez de
`SUM(booleano)`, y leer un JSON en Python en vez de `json_extract`. Lo que **no** se puede
escribir portable son tres cosas, y son las tres que esta capa cubre:

1. **El marcador de parámetro.** `?` en SQLite, `%s` en Postgres. Se traduce respetando los
   literales entre comillas, porque un `?` adentro de una cadena no es un parámetro.
2. **Las filas como mapping.** `sqlite3.Row` deja hacer `row["id"]`; en Postgres hay que pedir
   `dict_row`. Todo el código lee por nombre y ninguno por posición, así que alcanza con que las
   dos den lo mismo.
3. **`executescript`.** No existe en Postgres. `script()` parte en sentencias y las corre.

**El `?` sigue siendo lo que se escribe.** Ningún módulo sabe contra qué motor corre, y
ésa es la propiedad que hace que la capa sirva: si mañana hay un tercero, cambia acá.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SQLITE = "sqlite"
POSTGRES = "postgres"
BACKENDS = frozenset({SQLITE, POSTGRES})


class StoreUnavailable(RuntimeError):
    """El motor pedido no se puede usar: falta el driver, o la conexión no abre."""


# Un literal entre comillas simples —con '' adentro como escape— o un `?` suelto. El orden de
# las alternativas importa: la cadena se consume entera antes de mirar sus signos de pregunta.
_LITERAL_OR_PLACEHOLDER = re.compile(r"('(?:[^']|'')*')|(\?)")


def to_postgres(sql: str) -> str:
    """`?` a `%s`, sin tocar los que viven dentro de una cadena.

    El caso que obliga a mirar los literales no es hipotético: cualquier `LIKE '%?%'` o un texto
    con signo de pregunta se convertiría en un parámetro que nadie pasa, y el error saldría en
    tiempo de ejecución y lejos.
    """
    def swap(match: re.Match) -> str:
        return match.group(1) if match.group(1) else "%s"

    # `%` es el escape de psycopg, así que un `%` literal del SQL tiene que duplicarse.
    return _LITERAL_OR_PLACEHOLDER.sub(swap, sql.replace("%", "%%")).replace("%%s", "%s")


def statements(script: str) -> list[str]:
    """Las sentencias de un script, para los motores que no tienen `executescript`.

    Parte por `;` respetando dos cosas, y las dos costaron: los **literales**, porque el esquema
    tiene textos con punto y coma adentro, y los **comentarios `--`**, porque una sola apóstrofe
    en una prosa como «the spec's mention row» desbalancea las comillas y se traga todos los `;`
    que vienen después — el esquema entero terminaba siendo una sentencia.
    """
    parts, buffer = [], []
    quoted = comment = False
    for index, char in enumerate(script):
        if comment:
            buffer.append(char)
            if char == "\n":
                comment = False
            continue
        if not quoted and char == "-" and script[index - 1: index] == "-":
            comment = True
        elif char == "'":
            quoted = not quoted
        if char == ";" and not quoted:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))
    return [part.strip() for part in parts if part.strip()]


class Store:
    """La conexión al almacén. Se usa igual contra los dos motores."""

    def __init__(self, raw: Any, backend: str) -> None:
        if backend not in BACKENDS:
            raise StoreUnavailable(
                f"backend {backend!r} no implementado; hay {', '.join(sorted(BACKENDS))}"
            )
        self._raw = raw
        self.backend = backend

    # ─────────────────────────────  lo que usan los módulos  ─────────────────────────────

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._cursor(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> Any:
        cursor = self._raw.cursor()
        cursor.executemany(self._sql(sql), list(rows))
        return cursor

    def script(self, script: str) -> None:
        """El equivalente portable de `executescript`.

        En SQLite se delega, que además abre y cierra su propia transacción; en Postgres se
        corren las sentencias una por una sobre la conexión abierta.
        """
        if self.backend == SQLITE:
            self._raw.executescript(script)
            return
        cursor = self._raw.cursor()
        for statement in statements(script):
            cursor.execute(self._sql(statement))

    def table_exists(self, name: str) -> bool:
        """Si la tabla está. Es la única introspección que el código necesita, y es lo único
        que no se puede escribir portable: SQLite tiene `sqlite_master` y Postgres
        `information_schema`.

        Existe porque preguntar es más barato que intentar y atajar: en Postgres una sentencia
        que falla aborta la transacción entera, así que el `except` que servía en SQLite dejaría
        la conexión inutilizable.
        """
        if self.backend == SQLITE:
            query = "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?"
        else:
            query = "SELECT 1 FROM information_schema.tables WHERE table_name = ?"
        return self.execute(query, (name,)).fetchone() is not None

    def columns(self, table: str) -> set[str]:
        """Los nombres de columna de una tabla.

        La segunda introspección que el código necesita, y la otra que no se escribe portable:
        SQLite la da con `PRAGMA table_info` y Postgres con `information_schema.columns`. La usa
        la migración de `versioning`, que agrega una columna a un almacén que la precede.
        """
        if self.backend == SQLITE:
            rows = self.execute(f"PRAGMA table_info({table})")
            return {row["name"] for row in rows}
        rows = self.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)
        )
        return {row["column_name"] for row in rows}

    @property
    def integrity_error(self) -> type[Exception]:
        """La excepción de violación de restricción, que cada driver nombra a su modo.

        Vive acá por la invariante 10: quien encola un job atrapa «esta sesión ya tiene uno» sin
        saber contra qué motor corre. Ojo con lo que dice `table_exists`: en Postgres la
        sentencia que falla aborta la transacción, así que quien la atrape tiene que hacer
        `rollback` antes de seguir usando la conexión.
        """
        if self.backend == SQLITE:
            return sqlite3.IntegrityError
        import psycopg  # noqa: PLC0415 — extra opcional, como en `open_postgres`

        return psycopg.errors.IntegrityError

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()

    # ─────────────────────────────  adentro  ─────────────────────────────

    def _sql(self, sql: str) -> str:
        return sql if self.backend == SQLITE else to_postgres(sql)

    def _cursor(self, sql: str, params: Sequence[Any]) -> Any:
        cursor = self._raw.cursor()
        cursor.execute(self._sql(sql), tuple(params))
        return cursor


# ─────────────────────────────  abrir  ─────────────────────────────


def open_sqlite(path: Path, *, timeout_s: float = 30.0) -> Store:
    """El archivo bajo `work_dir`, en WAL.

    WAL da un escritor y cualquier cantidad de lectores concurrentes en vez de un lock que
    excluye a los dos. El `timeout` no estaba y hacía falta: sin él, la stdlib espera 5 segundos
    y tira `database is locked`, que con dos procesos escribiendo es un error de carrera
    disfrazado de error de datos.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(path, timeout=timeout_s)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA synchronous=NORMAL")
    # El esquema declara claves foráneas y SQLite las ignora salvo que se le pidan. Postgres las
    # va a exigir siempre, así que pedirlas acá es lo que evita que una diferencia de motor
    # aparezca recién en producción.
    raw.execute("PRAGMA foreign_keys=ON")
    return Store(raw, SQLITE)


def open_postgres(dsn: str) -> Store:
    """Postgres, que es lo que admite dos sesiones de usuario escribiendo a la vez."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depende del entorno
        raise StoreUnavailable(
            "el backend postgres necesita psycopg; uv sync --extra postgres"
        ) from exc

    try:
        raw = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.Error as exc:
        raise StoreUnavailable(f"no se pudo abrir {dsn.split('@')[-1]}: {exc}") from exc
    return Store(raw, POSTGRES)


def open_store(backend: str, *, path: Path | None = None, dsn: str = "") -> Store:
    """La apertura que lee la configuración: `database.backend` y `database.dsn`."""
    if backend == POSTGRES:
        if not dsn:
            raise StoreUnavailable("database.backend es postgres y database.dsn está vacío")
        return open_postgres(dsn)
    if path is None:
        raise StoreUnavailable("el backend sqlite necesita una ruta")
    return open_sqlite(path)
