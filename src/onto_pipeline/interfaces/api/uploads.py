"""Un corpus subido por HTTP, con su ontología inicial al lado.

Un *upload* es lo mismo que un caso de uso —el par (corpus, ontología)— pero puesto por quien
usa la API en vez de publicado en el repositorio. La forma es la misma a propósito: los archivos
que no se llaman `ontology.*` son el corpus, y el que sí es la ontología que se enriquece. Así
`upload:<id>` y `craft-cl` se pueden usar en el mismo lugar, que es lo que dice
`API-UPLOADED-AND-PUBLISHED`.

**Los uploads se comparten entre sesiones y borrarlos es global** (`API-SHARED-UPLOADS`). Es la
excepción escrita a `SESSION-SCOPED-DATA`, no un olvido: subir dos veces el mismo corpus para dos
sesiones sería pagar dos veces el almacenamiento de algo idéntico. La consecuencia —que borrar
uno le saque el corpus a toda sesión que corra sobre él— es explícita, y por eso `delete` dice
cuántas sesiones quedan apuntando a la nada.

**Cómo entra el contenido.** La API no recibe los bytes: devuelve un pre-signed PUT por archivo
y el cliente sube contra el almacén. Un archivo de cien megas no tiene por qué pasar por el
proceso que atiende HTTP. La contrapartida es que nadie avisa cuándo terminó de subir, así que
lo que existe de verdad se pregunta al almacén (`present`) y no a la tabla, que sólo dice qué se
prometió subir.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ...objectstore import ObjectStore, check_key
from ...store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  note        TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL
);

-- Qué archivos se prometieron. La verdad de qué subió está en el almacén: esto es lo que
-- permite decir «faltan tres» sin listar el bucket entero.
CREATE TABLE IF NOT EXISTS upload_files (
  upload_id   TEXT NOT NULL,
  filename    TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY (upload_id, filename),
  FOREIGN KEY (upload_id) REFERENCES uploads(id)
);
"""

PREFIX = "uploads"
# La forma de adentro es la de un caso de uso publicado —`corpus/` y un `ontology.*` al lado—,
# para que la convención sea una sola y `_adopt_use_case` no tenga que saber de dónde vino el
# material. Se valida al crear: un archivo colgado en otro lado no se ingesta y nadie se entera.
ONTOLOGY_STEM = "ontology"
CORPUS_DIR = "corpus"
# `sessions.use_case` guarda `upload:<id>` cuando la sesión corre sobre un upload. Sin columna
# nueva: no hay sistema de migraciones, y la columna que hay dice exactamente esto.
SESSION_MARK = "upload:"


class UnknownUpload(KeyError):
    pass


@dataclass
class Upload:
    id: str
    name: str
    note: str
    created_at: str
    filenames: list[str]

    @property
    def use_case(self) -> str:
        """Cómo se nombra este upload en `user_sessions.use_case`."""
        return f"{SESSION_MARK}{self.id}"

    def key(self, filename: str) -> str:
        return f"{PREFIX}/{self.id}/{filename}"

    @property
    def ontology(self) -> str | None:
        return next(
            (name for name in sorted(self.filenames)
             if Path(name).stem == ONTOLOGY_STEM and "/" not in name),
            None,
        )

    @property
    def corpus(self) -> list[str]:
        return [name for name in self.filenames if name != self.ontology]


def check_layout(filename: str) -> str:
    """Un archivo del upload es la ontología o es corpus, y el corpus vive bajo `corpus/`."""
    if Path(filename).stem == ONTOLOGY_STEM and "/" not in filename:
        return filename
    if filename.startswith(f"{CORPUS_DIR}/") and len(filename) > len(CORPUS_DIR) + 1:
        return filename
    raise ValueError(
        f"{filename!r}: un upload lleva el corpus bajo `{CORPUS_DIR}/` y la ontología como "
        f"`{ONTOLOGY_STEM}.<ext>`, igual que un caso de uso publicado"
    )


def install(conn: Store) -> None:
    conn.script(SCHEMA)
    conn.commit()


def session_upload(use_case: str) -> str | None:
    """El id del upload sobre el que corre una sesión, o `None` si corre sobre un caso publicado."""
    return use_case[len(SESSION_MARK):] if use_case.startswith(SESSION_MARK) else None


def create(conn: Store, *, name: str, filenames: list[str], note: str = "") -> Upload:
    """Reservar un upload y sus archivos. No sube nada: eso lo hace el cliente con `put_urls`."""
    install(conn)
    if not filenames:
        raise ValueError("un upload sin archivos no es un upload")
    upload = Upload(
        id=uuid.uuid4().hex[:12], name=name or "upload", note=note,
        created_at=_now(), filenames=sorted(set(filenames)),
    )
    for filename in upload.filenames:
        # La clave la propone el cliente, así que se valida acá y no en el backend: `..` es un
        # segmento literal en S3 y salir del directorio en el filesystem.
        check_key(upload.key(filename))
        check_layout(filename)
    conn.execute(
        "INSERT INTO uploads (id, name, note, created_at) VALUES (?, ?, ?, ?)",
        (upload.id, upload.name, upload.note, upload.created_at),
    )
    for filename in upload.filenames:
        conn.execute(
            "INSERT INTO upload_files (upload_id, filename, created_at) VALUES (?, ?, ?)",
            (upload.id, filename, upload.created_at),
        )
    conn.commit()
    return upload


def put_urls(store: ObjectStore, upload: Upload, *, expires_s: int) -> dict[str, str]:
    """Una URL de subida por archivo. El cliente sube contra el almacén, no contra la API."""
    return {
        name: store.presigned_put(upload.key(name), expires_s=expires_s)
        for name in upload.filenames
    }


def load(conn: Store, upload_id: str) -> Upload:
    install(conn)
    row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    if row is None:
        raise UnknownUpload(f"no hay upload {upload_id!r}")
    return _from_row(conn, row)


def all_uploads(conn: Store) -> list[Upload]:
    install(conn)
    return [
        _from_row(conn, row)
        for row in conn.execute("SELECT * FROM uploads ORDER BY created_at, id")
    ]


def present(store: ObjectStore, upload: Upload) -> list[str]:
    """Los archivos que están de verdad en el almacén. El pre-signed PUT no avisa cuándo
    terminó, así que quién contesta esto es el almacén y no la tabla."""
    offset = len(f"{PREFIX}/{upload.id}/")
    return sorted(key[offset:] for key in store.list(f"{PREFIX}/{upload.id}"))


def missing(store: ObjectStore, upload: Upload) -> list[str]:
    return sorted(set(upload.filenames) - set(present(store, upload)))


def sessions_on(conn: Store, upload_id: str) -> list[str]:
    """Las sesiones que corren sobre este upload. Se pregunta antes de borrarlo."""
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM user_sessions WHERE use_case = ?", (f"{SESSION_MARK}{upload_id}",)
        )
    ]


def delete(conn: Store, store: ObjectStore, upload_id: str) -> list[str]:
    """Borrar el upload del almacén y de la tabla. **Es global**: devuelve las sesiones que
    quedan apuntando a un corpus que ya no está, para que quien borró se entere."""
    upload = load(conn, upload_id)
    affected = sessions_on(conn, upload_id)
    for key in store.list(f"{PREFIX}/{upload.id}"):
        store.delete(key)
    conn.execute("DELETE FROM upload_files WHERE upload_id = ?", (upload_id,))
    conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    conn.commit()
    return affected


def materialize(store: ObjectStore, upload: Upload, directory: Path) -> tuple[Path, Path | None]:
    """Bajar el upload a un directorio efímero, con la forma que espera un caso de uso.

    `ingest.discover` recorre un directorio y el id de documento sale de la ruta relativa al
    corpus, así que la estructura se conserva: el mismo upload ingestado dos veces da los mismos
    ids, que es lo que hace que el caché por unidad de trabajo sirva.

    El directorio es del que llama y se limpia al terminar el trabajo: en la nube, lo que quede
    escrito en el contenedor se pierde igual, pero llenarle el disco al proceso mientras corre
    sí se nota.
    """
    corpus = Path(directory) / CORPUS_DIR
    corpus.mkdir(parents=True, exist_ok=True)
    for name in upload.corpus:
        # El nombre ya trae `corpus/` adentro, que es la forma de un caso de uso: se escribe
        # relativo al directorio y no al corpus, o quedaría `corpus/corpus/`.
        target = Path(directory) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(store.get(upload.key(name)))
    ontology = None
    if upload.ontology:
        ontology = Path(directory) / upload.ontology
        ontology.write_bytes(store.get(upload.key(upload.ontology)))
    return corpus, ontology


def _from_row(conn: Store, row) -> Upload:
    return Upload(
        id=row["id"], name=row["name"], note=row["note"] or "", created_at=row["created_at"],
        filenames=[
            item["filename"]
            for item in conn.execute(
                "SELECT filename FROM upload_files WHERE upload_id = ? ORDER BY filename",
                (row["id"],),
            )
        ],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
