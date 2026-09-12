"""De dónde saca cada request su workspace, y hasta cuándo lo tiene.

**Una conexión por unidad de trabajo.** Se abre al empezar el request y se cierra al terminarlo,
en el hilo que lo atiende. Una conexión global en el ciclo de vida de la app explota en SQLite
por afinidad de hilo; en Postgres es peor, porque **no** explota: compartir conexión es compartir
transacción, y dos requests terminan commiteándose mutuamente trabajo a medio hacer.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ...services import Workspace


@contextmanager
def workspace(config_path: Path, session_id: str = "") -> Iterator[Workspace]:
    """Un workspace para esta unidad de trabajo, cerrado pase lo que pase.

    `Workspace.open` relee la configuración en cada request. Es barato al lado de cualquier
    consulta, y es lo que hace que un cambio de variable de entorno valga para el request que
    sigue en vez de para el próximo despliegue.
    """
    opened = Workspace.open(config_path, session_id=session_id)
    try:
        yield opened
    finally:
        opened.cleanup()
        opened.conn.close()
