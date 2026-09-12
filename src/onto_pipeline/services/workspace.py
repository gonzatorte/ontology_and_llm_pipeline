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
from ..artifacts import Artifact, Artifacts
from ..config import Config
from ..db import open_configured
from ..objectstore import open_configured as open_objectstore
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
    # La sesión de usuario sobre la que corre todo lo que se pida a este workspace. Es el único
    # estado de corrida que vive acá; lo demás es configuración.
    session_id: str = ""
    config_path: Path | None = None
    # Sobreescrituras de `paths` pedidas por la interfaz, no por el archivo: el par
    # (corpus, ontología) es un parámetro de la corrida y no una decisión de configuración.
    overrides: dict[str, Path] = field(default_factory=dict)
    _artifacts: Artifacts | None = field(default=None, repr=False)

    @classmethod
    def open(
        cls,
        config_path: Path,
        *,
        session_id: str | None = None,
        corpus_root: Path | None = None,
        initial_ontology: Path | None = None,
        work_dir: Path | None = None,
    ) -> Workspace:
        """Abrir el almacén y elegir sobre qué sesión de usuario se va a trabajar.

        Sin `session_id` se usa la sesión actual —la que dejó marcada `session use`—, y si no
        hay ninguna se dice cómo crear una en vez de fallar con una consulta vacía.
        """
        config = Config.load(config_path)
        overrides: dict[str, Path] = {}
        for name, value in (
            ("corpus_root", corpus_root),
            ("initial_ontology", initial_ontology),
            ("work_dir", work_dir),
        ):
            if value is not None:
                resolved = Path(value).expanduser().resolve()
                setattr(config.paths, name, resolved)
                overrides[name] = resolved
        conn = open_configured(config.database, config.paths.work_dir)
        workspace = cls(
            config=config, conn=conn, config_path=Path(config_path), overrides=overrides,
        )
        workspace.session_id = session_id or current_session(config.paths.work_dir) or ""
        workspace._adopt_use_case()
        return workspace

    def _adopt_use_case(self) -> None:
        """Sobre qué corre esto lo dice la sesión, no el archivo de configuración.

        Una sesión corre sobre un caso de uso, y el caso de uso **es** el par (ontología
        inicial, corpus). Si `paths` siguiera mandando, dos sesiones sobre casos de uso
        distintos leerían el mismo corpus — que es precisamente la confusión que separarlos vino
        a deshacer.

        La convención es un `corpus/` y un `ontology.*` dentro del directorio del caso de uso;
        pueden ser symlinks a material que vive afuera, que es lo que hace `use_cases/README.md`.
        Sin ellos se cae a lo que diga `paths`, y `ingest` dirá que no encuentra el corpus —
        que es la falla correcta y no una corrida silenciosa sobre otra cosa.
        """
        from .. import sessions

        if not self.session_id or not sessions_table(self.conn):
            return
        try:
            session = sessions.load(self.conn, self.session_id)
        except sessions.UnknownSession:
            return
        directory = self.config.paths.use_cases_root / session.use_case
        corpus = directory / "corpus"
        if corpus.exists():
            self.config.paths.corpus_root = corpus.resolve()
        ontology = next(
            (item for item in sorted(directory.glob("ontology.*")) if item.exists()), None
        )
        if ontology is not None:
            self.config.paths.initial_ontology = ontology.resolve()

    @classmethod
    def of(cls, config: Config, conn: Store, *, session_id: str = "") -> Workspace:
        """Para los tests y para quien ya tiene las dos cosas abiertas."""
        return cls(config=config, conn=conn, session_id=session_id)

    def require_session(self) -> str:
        """La sesión sobre la que corre esto, o un error que dice cómo crear una."""
        if not self.session_id:
            raise StageError(
                "no hay sesión de usuario elegida. `onto-pipeline session new --use-case "
                "<nombre>` crea una, `session list` muestra las que hay, y `--session <id>` "
                "elige una para un comando suelto."
            )
        return self.session_id

    # ─────────────────────────  versiones  ─────────────────────────

    def resolve_version(self, version: str | None = None) -> str:
        """La versión nombrada, o la más nueva **de esta sesión**.

        `created_at` tiene precisión de segundo, así que dos versiones del mismo segundo
        empatan; `seq` —el ordinal dentro de la sesión— desempata, que es lo que "la más
        nueva" significa acá. Era `rowid`, que sólo existe en SQLite.

        El id de versión es `<sesión>:v<N>`, pero se acepta también la forma corta `v3`: es lo
        que alguien teclea, y calificarla con la sesión actual es lo que vuelve innecesario
        escribir el prefijo.
        """
        versioning.install(self.conn)
        if version and ":" not in version:
            version = f"{self.session_id}:{version}"
        row = self.conn.execute(
            "SELECT id FROM versions WHERE session_id = ? AND id = COALESCE(?, id) "
            "ORDER BY created_at DESC, seq DESC LIMIT 1",
            (self.session_id, version),
        ).fetchone()
        if row is None:
            raise StageError(
                f"no version {version!r}" if version
                else "no ontology version; run `normalize` first"
            )
        return row["id"]

    def latest_version(self) -> str | None:
        """Como `resolve_version`, pero un almacén recién creado no es un error.

        `next` y el wizard preguntan esto: antes de la ontología inicial no hay versión y eso es el
        estado normal, no una falla.
        """
        try:
            return self.resolve_version(None)
        except StageError:
            return None

    def graph(self, version_id: str) -> Graph:
        return versioning.load(self.conn, version_id)[1]

    def next_version_id(self) -> str:
        return versioning.next_version_id(self.conn, self.require_session())

    @property
    def artifacts(self) -> Artifacts:
        """Los artefactos de esta sesión, sin que la etapa sepa dónde viven.

        Un solo lugar arma cada clave. Cuando eran rutas armadas en el punto de uso, escribir y
        leer podían discrepar sin que nadie se enterara —y discreparon: `initial_normalized.ttl`
        se escribía bajo la sesión y se leía bajo `work_dir`—, y encima lo escrito a disco no
        sobrevive a que el contenedor se recicle, que en la nube pasa sin aviso.

        Se abre tarde y una sola vez por workspace: con el backend de objetos, construirlo
        arma un cliente, y hay comandos que no tocan un artefacto.
        """
        # La sesión se elige después de construir el workspace, así que el caché se rehace si
        # cambió: un artefacto de la sesión equivocada es exactamente el bug que esto arregla.
        if self._artifacts is None or self._artifacts.session_id != (self.session_id or "default"):
            self._artifacts = Artifacts(
                open_objectstore(self.config.storage, self.config.paths.work_dir),
                self.session_id,
            )
        return self._artifacts

    def abox(self, version_id: str) -> Artifact:
        return self.artifacts.abox(version_id)

    # ─────────────────────────  modelo y encoders  ─────────────────────────

    def note(self, kind: str, summary: str, payload: dict | None = None) -> None:
        """Anotar en el historial de la sesión.

        Las **elecciones** del usuario ya se guardan donde corresponde —`grey_decisions`,
        `decisions`, `review_items`—; esto anota que **pasaron**, y qué etapa las produjo. Dos
        copias de una decisión es cómo una de las dos queda vieja, así que acá va el resumen y
        no el contenido.
        """
        from .. import sessions

        if self.session_id:
            sessions.record(self.conn, self.session_id, kind, summary, payload)

    def ledger(self) -> Ledger:
        return Ledger(self.conn, self.config.execution, session_id=self.session_id)

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


# La sesión actual vive en un archivo del almacén y no en el config, que se versiona: cuál
# sesión está activa es estado de esta máquina, no una decisión del proyecto.
_CURRENT = "current_session"


def sessions_table(conn: Store) -> bool:
    """Si el almacén ya tiene la tabla. Un almacén recién creado no la tiene y eso no es error."""
    return conn.table_exists("user_sessions")


def current_session(work_dir: Path) -> str:
    marker = work_dir / _CURRENT
    return marker.read_text(encoding="utf-8").strip() if marker.exists() else ""


def use_session(work_dir: Path, session_id: str) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / _CURRENT).write_text(session_id, encoding="utf-8")


def table_exists(conn: Store, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def count(conn: Store, query: str, params: tuple = ()) -> int:
    row = conn.execute(query, params).fetchone()
    return int(row["n"]) if row else 0
