"""Qué artefacto es cuál, y con qué clave.

`objectstore.py` mueve bytes; acá vive el vocabulario del dominio. Un **artefacto** es una salida
derivada del pipeline —el Markdown de un documento, los recortes de figuras, la ontología
normalizada, el ABox, un diff, el export y su manifiesto—. Los **casos de uso** y los uploads no
son esto: son insumos, y entran por `use_cases.py` y por `uploads.py`.

Que el nombre del artefacto viva en un solo lugar es lo que arregla el desalineo que tenía
`initial_normalized.ttl`, escrito bajo la sesión y leído bajo `work_dir`: con la clave en una
función, escribir y leer no pueden discrepar.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .objectstore import MissingObject, ObjectStore

MARKDOWN = "markdown"
ASSETS = "assets"
ONTOLOGY = "ontology"
EXPORT = "export"

NORMALIZED = "initial_normalized.ttl"
SHAPES = "shapes.ttl"


@dataclass(frozen=True)
class Artifact:
    """Una clave y el almacén donde vive. Se pasa entre módulos como se pasaba un `Path`."""

    store: ObjectStore
    key: str

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]

    def exists(self) -> bool:
        return self.store.exists(self.key)

    def read_text(self) -> str:
        return self.store.get(self.key).decode("utf-8")

    def read_bytes(self) -> bytes:
        return self.store.get(self.key)

    def write_text(self, text: str) -> Artifact:
        self.store.put(self.key, text.encode("utf-8"))
        return self

    def write_bytes(self, data: bytes) -> Artifact:
        self.store.put(self.key, data)
        return self

    def delete(self) -> None:
        self.store.delete(self.key)

    def url(self, *, expires_s: int) -> str:
        return self.store.presigned_get(self.key, expires_s=expires_s)

    def materialize(self, directory: Path) -> Path:
        """Bajarlo a un archivo, para lo que exige una ruta: la JVM del razonador y todo parser
        que no acepte un string. Vive lo que vive el directorio, que es del llamador."""
        target = Path(directory) / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.read_bytes())
        return target

    def sibling(self, suffix: str) -> Artifact:
        """El de al lado, con el nombre extendido: así se nombra el manifiesto del export."""
        return Artifact(self.store, self.key + suffix)

    def __str__(self) -> str:
        return self.key


class Artifacts:
    """Los artefactos de una sesión. La sesión está adentro de la clave, no al lado."""

    def __init__(self, store: ObjectStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id or "default"

    # ─────────────────────────  claves  ─────────────────────────

    @property
    def prefix(self) -> str:
        return f"sessions/{self.session_id}"

    def of(self, key: str) -> Artifact:
        return Artifact(self.store, key)

    def markdown(self, doc_id: str) -> Artifact:
        # Sin la sesión adentro, a diferencia de todo lo demás: el id de documento sale del
        # corpus, así que dos sesiones sobre el mismo corpus escriben el mismo Markdown con el
        # mismo contenido. Dos corpus distintos con ids que coinciden sí se pisarían, y eso está
        # anotado como `DEBT-API-DOCUMENTS-PATH` en vez de arreglado acá: mover la clave obliga a
        # re-ingestar todo lo que ya está parseado.
        return Artifact(self.store, f"{MARKDOWN}/{doc_id}.md")

    def crop(self, doc_id: str, name: str) -> Artifact:
        return Artifact(self.store, f"{ASSETS}/{doc_id}/{name}")

    def normalized_ontology(self) -> Artifact:
        return Artifact(self.store, f"{self.prefix}/{ONTOLOGY}/{NORMALIZED}")

    def abox(self, version_id: str) -> Artifact:
        return Artifact(self.store, f"{self.prefix}/{ONTOLOGY}/{filename(version_id)}.abox.trig")

    def diff(self, baseline: str, target: str) -> Artifact:
        return Artifact(
            self.store,
            f"{self.prefix}/{ONTOLOGY}/{filename(baseline)}-to-{filename(target)}.diff.json",
        )

    def export(self, version_id: str, suffix: str) -> Artifact:
        return Artifact(
            self.store, f"{self.prefix}/{ONTOLOGY}/{filename(version_id)}.enriched{suffix}"
        )

    def shapes(self) -> Artifact:
        """Las shapes de SHACL se escriben a mano y son insumo, no salida; se leen por acá
        igual, porque el que corre en la nube no tiene dónde poner un archivo suelto."""
        return Artifact(self.store, SHAPES)

    # ─────────────────────────  listado  ─────────────────────────

    def listing(self) -> list[Artifact]:
        """Todo lo que esta sesión tiene guardado. Es lo que la API expone para descargar."""
        return [Artifact(self.store, key) for key in self.store.list(self.prefix)]


def filename(version_id: str) -> str:
    """El id de versión como nombre: `sesión:v3` lleva dos puntos, que en una URL son otra cosa
    y en Windows no son un carácter de nombre."""
    return version_id.replace(":", "-")


def read_text_or_missing(artifact: Artifact) -> str | None:
    try:
        return artifact.read_text()
    except MissingObject:
        return None
