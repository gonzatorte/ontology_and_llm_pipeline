"""De dónde saca cada request su workspace, y hasta cuándo lo tiene.

**`ONE-CONNECTION-PER-UNIT`.** Se abre al empezar el request y se cierra al terminarlo, en el
hilo que lo atiende. Una conexión global en el ciclo de vida de la app explota en SQLite por
afinidad de hilo; en Postgres es peor, porque **no** explota: compartir conexión es compartir
transacción, y dos requests terminan commiteándose mutuamente trabajo a medio hacer.

**Acá se resuelve el upload, y sólo acá.** El core no sabe qué es un upload: sabe abrir un
workspace sobre un corpus y una ontología que están en el filesystem. Si la sesión corre sobre
un upload, esta capa lo baja a un directorio efímero y abre el workspace apuntando ahí, con los
mismos overrides que usa el CLI para correr sobre otro material. El directorio se borra al
terminar la unidad de trabajo.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ... import sessions
from ...services import StageError, Workspace
from ...services.workspace import sessions_table
from . import uploads


@contextmanager
def workspace(config_path: Path, session_id: str = "") -> Iterator[Workspace]:
    """Un workspace para esta unidad de trabajo, cerrado pase lo que pase.

    `Workspace.open` relee la configuración en cada request. Es barato al lado de cualquier
    consulta, y es lo que hace que un cambio de variable de entorno valga para el request que
    sigue en vez de para el próximo despliegue.
    """
    opened = Workspace.open(config_path, session_id=session_id)
    directory = None
    try:
        directory = _materialize_upload(opened)
        yield opened
    finally:
        opened.conn.close()
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


def _materialize_upload(opened: Workspace) -> Path | None:
    """Si la sesión corre sobre un upload, bajarlo y apuntar `paths` ahí.

    Se hace tarde y sólo cuando hace falta: bajar el corpus entero para listar jobs sería
    pagarlo por nada. Apuntar `paths` es lo mismo que hace `--corpus-root` en el CLI — el core
    no se entera de que el material vino de un bucket.

    Falla si falta algún archivo prometido: el pre-signed PUT no avisa cuándo terminó de subir,
    así que ingestar «lo que haya» sería correr sobre medio corpus sin decirlo.
    """
    if not opened.session_id or not sessions_table(opened.conn):
        return None
    try:
        session = sessions.load(opened.conn, opened.session_id)
    except sessions.UnknownSession:
        return None
    upload_id = uploads.session_upload(session.use_case)
    if not upload_id:
        return None
    upload = uploads.load(opened.conn, upload_id)
    pending = uploads.missing(opened.artifacts.store, upload)
    if pending:
        raise StageError(
            f"al upload {upload_id} le faltan {len(pending)} archivo(s) por subir: "
            + ", ".join(pending[:5])
        )
    directory = Path(tempfile.mkdtemp(prefix=f"onto-{upload_id}-"))
    corpus, ontology = uploads.materialize(opened.artifacts.store, upload, directory)
    opened.config.paths.corpus_root = corpus
    if ontology is not None:
        opened.config.paths.initial_ontology = ontology
    return directory
