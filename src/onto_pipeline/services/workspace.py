"""Lo que toda etapa necesita antes de empezar: config, almacén, versión, modelo.

Esta capa existe porque hay **dos** interfaces sobre el mismo pipeline —el CLI de banderas y
el `wizard` línea por línea— y una etapa no puede pertenecer a ninguna de las dos. Una función
de servicio recibe un `Workspace`, devuelve un resultado tipado, y no imprime: quien la llamó
decide cómo mostrarlo. La regla que lo mantiene honesto es que **nada acá importa `typer` ni
`rich`**; hay un test que lo fija.

Los errores que el usuario tiene que arreglar viajan como `StageError`, no como
`typer.BadParameter`: el wizard los muestra y sigue preguntando, el CLI los convierte en un
código de salida. Que la etapa no sepa cuál de las dos cosas va a pasar es justamente el punto.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph

from .. import versioning
from ..config import Config
from ..db import open_configured
from ..store import Store
from ..telemetry import Ledger

# Qué está pasando ahora, para una barra de progreso o un spinner. No es logging: es la única
# forma que tiene una etapa larga de decirle algo a quien la mira, sin saber quién mira.
Progress = Callable[[str], None]


def silent(_message: str) -> None:
    """El progreso por defecto: nadie mirando."""


class StageError(Exception):
    """Algo que el usuario tiene que arreglar antes de que la etapa pueda correr.

    Se distingue de un bug a propósito: el mensaje se le muestra tal cual, así que dice qué
    falta y qué comando lo produce, nunca un traceback.
    """


class ProviderMissing(StageError):
    """La etapa llama al modelo y no hay credencial cargada.

    Subclase y no un mensaje más porque las dos interfaces la tratan distinto: el CLI dice
    `--env-file`, el wizard puede pedir el archivo y seguir.
    """


@dataclass
class Workspace:
    """El ámbito de trabajo: la configuración y el acceso al almacén.

    Se abre una vez y se pasa a mano. La alternativa —que cada función haga `Config.load` y
    abra el almacén— es lo que hacía el CLI, y significa que una interfaz que quiera correr
    cinco etapas sobre la misma configuración no puede.

    **Se llamaba `Session`, y ese nombre era de otra cosa.** Una *sesión de usuario* es una
    corrida sobre un caso de uso, con su estado y su historial; esto es cómo se llega a la
    configuración y al almacén, que es lo mismo para todas. Mezclar las dos es la mezcla que el
    corte de `cli.py` acaba de deshacer.
    """

    config: Config
    conn: Store
    config_path: Path | None = None
    # Sobreescrituras de `paths` pedidas por la interfaz, no por el archivo: el par
    # (corpus, ontología) es un parámetro de la corrida y no una decisión de configuración.
    overrides: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def open(
        cls,
        config_path: Path,
        *,
        corpus_root: Path | None = None,
        seed_ontology: Path | None = None,
        work_dir: Path | None = None,
    ) -> Workspace:
        config = Config.load(config_path)
        overrides: dict[str, Path] = {}
        for name, value in (
            ("corpus_root", corpus_root),
            ("seed_ontology", seed_ontology),
            ("work_dir", work_dir),
        ):
            if value is not None:
                resolved = Path(value).expanduser().resolve()
                setattr(config.paths, name, resolved)
                overrides[name] = resolved
        return cls(
            config=config,
            conn=open_configured(config.database, config.paths.work_dir),
            config_path=Path(config_path), overrides=overrides,
        )

    @classmethod
    def of(cls, config: Config, conn: Store) -> Workspace:
        """Para los tests y para quien ya tiene las dos cosas abiertas."""
        return cls(config=config, conn=conn)

    # ─────────────────────────  versiones  ─────────────────────────

    def resolve_version(self, version: str | None = None) -> str:
        """La versión nombrada, o la más nueva.

        `created_at` tiene precisión de segundo, así que dos versiones del mismo segundo
        empatan; `rowid` desempata por orden de inserción, que es lo que "la más nueva"
        significa acá.
        """
        versioning.install(self.conn)
        row = self.conn.execute(
            "SELECT id FROM versions WHERE id = COALESCE(?, id) "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (version,),
        ).fetchone()
        if row is None:
            raise StageError(
                f"no version {version!r}" if version
                else "no ontology version; run normalize-seed first"
            )
        return row["id"]

    def latest_version(self) -> str | None:
        """Como `resolve_version`, pero un almacén recién creado no es un error.

        `next` y el wizard preguntan esto: antes de la semilla no hay versión y eso es el
        estado normal, no una falla.
        """
        try:
            return self.resolve_version(None)
        except StageError:
            return None

    def graph(self, version_id: str) -> Graph:
        return versioning.load(self.conn, version_id)[1]

    def ontology_dir(self) -> Path:
        target = self.config.paths.work_dir / "ontology"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def abox_path(self, version_id: str) -> Path:
        return self.config.paths.work_dir / "ontology" / f"{version_id}.abox.trig"

    # ─────────────────────────  modelo y encoders  ─────────────────────────

    def ledger(self) -> Ledger:
        return Ledger(self.conn, self.config.execution)

    def has_provider(self) -> bool:
        """Si hay credencial cargada para el proveedor configurado.

        Lee el entorno, no la bandera: `--env-file` la carga antes del subcomando y el wizard
        la carga cuando la pide, y las dos rutas tienen que dar la misma respuesta.
        """
        import os

        if self.config.llm.provider == "none":
            return False
        return bool(os.environ.get(self.config.llm.api_key_env, ""))

    def model(self):
        from .. import llm

        return llm.build(self.config.llm, timeout_s=self.config.execution.request_timeout_s)

    def encoder(self):
        """El bi-encoder configurado, o un `StageError` que dice cómo instalarlo."""
        from ..embeddings import EncoderUnavailable, SentenceTransformerEncoder

        try:
            return SentenceTransformerEncoder(
                self.config.matching.bi_encoder, self.config.matching.device
            )
        except EncoderUnavailable as exc:
            raise StageError(f"{exc}; uv sync --extra matching") from exc

    def matcher(self):
        from ..matching import Matcher

        return Matcher(self.encoder())

    def text_similarity(self) -> Callable[[str, str], float] | None:
        """Coseno entre dos textos cortos, del bi-encoder ya configurado.

        Dos usuarios, y los dos prefieren perder una capacidad antes que adivinarla: el eje de
        criterio de división, que necesita saber cuándo dos criterios declarados nombran el
        mismo corte, y la deduplicación de `PREP-CQ-GENERATED`. Devuelve None cuando el encoder
        no está instalado, y cada usuario dice qué hace sin él.
        """
        from ..embeddings import EncoderUnavailable, SentenceTransformerEncoder
        from ..matching import dot

        try:
            encoder = SentenceTransformerEncoder(
                self.config.matching.bi_encoder, self.config.matching.device
            )
        except EncoderUnavailable:
            return None

        cache: dict[str, list[float]] = {}

        def similarity(left: str, right: str) -> float:
            missing = [text for text in (left, right) if text not in cache]
            if missing:
                cache.update(zip(missing, encoder.encode(missing), strict=True))
            return dot(cache[left], cache[right])

        return similarity

    def reasoners(self, *, why: str = ""):
        """El stack de razonamiento, o un `StageError` que dice qué se perdería sin él."""
        from ..reasoning import Reasoners, ReasonerUnavailable

        try:
            return Reasoners(
                self.config.paths.reasoner_lib,
                hermit_timeout_s=self.config.reasoner.hermit_timeout_s,
            )
        except ReasonerUnavailable as exc:
            raise StageError(f"{exc}. {why}".strip()) from exc


def table_exists(conn: Store, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def count(conn: Store, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row[0]) if row else 0
