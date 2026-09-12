"""La interfaz guiada: pregunta lo que hace falta, corre lo que se corre solo, y entrega.

**Qué la distingue de `next`.** `next` contesta qué corresponde hacer y frena cuando lo
siguiente es una decisión del usuario: cruzarla sería decidirla por default. El wizard contesta
la misma pregunta y, en vez de frenar, **la hace**. El invariante es el mismo — ninguna
decisión se toma sola — y lo que cambia es que acá hay alguien a quien preguntarle.

**Qué no hace, y es deliberado.**

- No corre una etapa que llama al modelo sin decir antes cuánto va a pedirle y esperar un sí.
  Una corrida de horas arrancada por un Enter distraído es exactamente lo que no puede pasar.
- No inventa una respuesta cuando el usuario saltea una pregunta. Saltear deja el punto
  abierto, y las etapas que dependían de él siguen bloqueadas — que es la verdad.
- No reimplementa ninguna etapa. Todo lo que corre son las funciones de `services`, las mismas
  que corre el CLI de banderas; lo único propio de este archivo es el diálogo.

El estado vive donde vivía: en SQLite. Cortar el wizard a la mitad y volver mañana es lo
normal, no un caso de borde — cuando vuelve, `orchestration.survey` dice dónde quedó.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import TextType

from . import orchestration, render
from .config import Config
from .providers import load_env_file
from .services import StageError, Workspace, deliver, evaluate, iterate, prep
from .telemetry import StageAborted

try:
    # Importarlo es lo que le da a `input()` edición de línea e historial. Sin él las flechas
    # escriben `^[[D` en vez de mover el cursor.
    import readline
except ImportError:  # Windows no lo trae
    readline = None

# Corta la cola de este punto de decisión, no el wizard: lo que no se contestó queda abierto y
# se sigue con la etapa siguiente. No «a», que choca con «aceptar».
ABORT = "q"
SKIP = "s"
NONE = "n"


class Abort(Exception):
    """El usuario pidió salir. No es un error: es una respuesta."""


def _key(letter: str) -> str:
    """Una tecla, escapada.

    `rich` lee `[n]` como una etiqueta de marcado y se la come: el menú quedaba con las
    opciones sin su letra, que es la única parte que el usuario tiene que tipear.
    """
    return rf"\[{letter}]"


# ─────────────────────────────  el diálogo mínimo  ─────────────────────────────


# Las secuencias de color, que readline tiene que saber que no ocupan columnas.
_ESCAPE = re.compile(r"(\x1b\[[0-9;]*m)")


def _read_line(
    console: Console, prompt: TextType, password: bool, stream: TextIO | None
) -> str:
    """El prompt se le entrega a `input()` en vez de imprimirse antes.

    `rich` lo imprime por su cuenta y llama a `input()` sin prompt. Con readline cargado, cada
    vez que readline redibuja la línea —subir en el historial, borrarla entera— vuelve al
    margen, escribe el prompt que conoce, que es ninguno, y el de `rich` desaparece. Los colores
    van entre \\001 y \\002 porque, si no, readline los cuenta como columnas y corre el cursor.
    """
    if readline is None or password or stream is not None or not console.is_terminal:
        return console.input(prompt, password=password, stream=stream)
    with console.capture() as capture:
        console.print(prompt, end="")
    return input(_ESCAPE.sub("\001\\1\002", capture.get()))


class _LineEditing:
    @classmethod
    def get_input(
        cls, console: Console, prompt: TextType, password: bool, stream: TextIO | None = None
    ) -> str:
        return _read_line(console, prompt, password, stream)


class _Prompt(_LineEditing, Prompt):
    pass


class _Confirm(_LineEditing, Confirm):
    pass


def _ask(console: Console, question: str, *, default: str | None = None) -> str:
    try:
        return _Prompt.ask(question, default=default, console=console)
    except (EOFError, KeyboardInterrupt) as exc:
        raise Abort from exc


def _confirm(console: Console, question: str, *, default: bool = True) -> bool:
    try:
        return _Confirm.ask(question, default=default, console=console)
    except (EOFError, KeyboardInterrupt) as exc:
        raise Abort from exc


def _path(console: Console, question: str, default: Path | None) -> Path:
    """Una ruta que existe, o la pregunta otra vez.

    Aceptar una que no existe y fallar tres etapas después es la forma más cara de decir lo
    mismo.
    """
    while True:
        answer = _ask(console, question, default=str(default) if default else None)
        candidate = Path(answer).expanduser()
        if candidate.exists():
            return candidate.resolve()
        console.print(f"[red]no existe[/]: {candidate}")


# ─────────────────────────────  arranque  ─────────────────────────────


def _open(
    console: Console,
    config_path: Path,
    session_id: str | None,
    env_file: Path | None,
) -> Workspace:
    """Confirmar la configuración y elegir la sesión de usuario.

    **Lo primero que se pregunta es sobre qué sesión se trabaja**: retomar una que quedó a medias
    o empezar una nueva sobre un caso de uso. El caso de uso —el par (ontología inicial,
    corpus)— es material de entrada y ya vive en `use_cases/`, así que no se pregunta por rutas
    sueltas: se elige cuál.

    Del archivo de configuración se muestra lo que decide algo y se confirma; una clave que no
    esté toma su default, que es lo que pydantic ya hacía en silencio y acá se dice.
    """
    console.print(Panel.fit(
        "[bold]onto-pipeline[/] · enriquecimiento ontológico asistido\n"
        "Corro las etapas que se corren solas y te pregunto en cada punto de decisión.\n"
        "[dim]Cortar acá y volver después no pierde nada: el estado vive en el almacén.[/]",
        border_style="cyan",
    ))

    if not Path(config_path).exists():
        raise StageError(f"no hay configuración en {config_path}")
    # El archivo se lee antes de abrir el almacén: mostrar la configuración no necesita una
    # conexión, y el almacén se abre una sola vez, ya con el par de esta corrida.
    config = Config.load(config_path)

    table = Table("qué", "valor", title=f"configuración · {config_path}")
    table.add_row("almacén de trabajo", str(config.paths.work_dir))
    table.add_row("razonador (jars)", str(config.paths.reasoner_lib))
    table.add_row("proveedor de modelo", config.llm.provider or "none")
    table.add_row("umbral de auto-merge", str(config.matching.auto_merge_threshold))
    table.add_row("piso de la zona gris", str(config.matching.grey_zone_lower))
    table.add_row("compara contra", config.matching.match_against)
    console.print(table)
    console.print(
        "[dim]Toda clave que el archivo no defina toma su default; los umbrales vigentes son "
        "los de `config/default.yaml` y no los del spec.[/]"
    )
    if not _confirm(console, "¿Sigo con esta configuración?"):
        raise Abort

    workspace = Workspace.open(config_path, session_id=session_id)
    _choose_session(console, workspace)

    if env_file is not None:
        names = load_env_file(env_file)
        console.print(f"[dim]cargadas {', '.join(names)} de {env_file}[/]")
    return workspace


def _choose_session(console: Console, workspace: Workspace) -> None:
    """Retomar una sesión o empezar una nueva. Es la primera pregunta, y no se saltea.

    Sin sesión no hay dónde poner nada: las menciones, las versiones y las decisiones cuelgan
    de una. Y con varias guardadas, asumir cuál es sería decidir por el usuario lo único que
    esta pantalla existe para preguntar.
    """
    existing = evaluate.list_sessions(workspace)
    if existing:
        console.print(Panel.fit(
            f"[bold]sesiones[/] · {len(existing)} guardadas\n"
            "[dim]Una sesión es una corrida sobre un caso de uso. Retomar una sigue donde "
            "quedó; el estado vive en el almacén.[/]",
            border_style="cyan",
        ))
        render.session_list(console, existing, current=workspace.session_id)
        for index, item in enumerate(existing, start=1):
            console.print(f"  {_key(str(index))} {item.id} · {item.phase}")
        console.print(f"  {_key('n')} empezar una nueva")

        choice = _ask(
            console, "¿Cuál retomo?",
            default=str(_index_of(existing, workspace.session_id) or 1),
        ).strip().lower()
        if choice != "n":
            if choice.isdigit() and 1 <= int(choice) <= len(existing):
                evaluate.use_current(workspace, existing[int(choice) - 1].id)
                return
            console.print("[yellow]no entendí; empiezo una nueva[/]")

    cases = _use_cases(workspace)
    if not cases:
        raise StageError(
            f"no hay ningún caso de uso en {workspace.config.paths.use_cases_root}. "
            "Un caso de uso es un directorio con su `use_case.yml`; ver use_cases/README.md."
        )
    console.print("\n[bold]¿Sobre qué caso de uso?[/] (el par ontología inicial + corpus)")
    for index, name in enumerate(cases, start=1):
        console.print(f"  {_key(str(index))} {name}")
    picked = _ask(console, "¿Cuál?", default="1").strip()
    if not (picked.isdigit() and 1 <= int(picked) <= len(cases)):
        raise Abort
    name = _ask(console, "¿Le ponés un nombre? (opcional)", default="").strip()
    created = evaluate.new_session(workspace, use_case=cases[int(picked) - 1], name=name)
    console.print(f"[green]sesión {created.id}[/] sobre {created.use_case}")


def _use_cases(workspace: Workspace) -> list[str]:
    root = workspace.config.paths.use_cases_root
    if not root.is_dir():
        return []
    return sorted(
        item.name for item in root.iterdir()
        if item.is_dir() and not item.name.startswith("_")
    )


def _index_of(found: list, session_id: str) -> int | None:
    for index, item in enumerate(found, start=1):
        if item.id == session_id:
            return index
    return None


def _provider(console: Console, workspace: Workspace) -> bool:
    """Conseguir la credencial si hace falta, o decir qué se pierde sin ella."""
    if workspace.has_provider():
        return True
    if workspace.config.llm.provider == "none":
        console.print(
            "[yellow]`llm.provider` es 'none'[/]: las etapas que llaman al modelo no pueden "
            "correr con esta configuración."
        )
        return False
    console.print(
        f"[yellow]falta la credencial[/] ({workspace.config.llm.api_key_env} no está en el "
        "entorno). Esta etapa llama al modelo."
    )
    if not _confirm(console, "¿Cargo un archivo de entorno?"):
        return False
    path = _path(console, "Archivo de entorno", Path("opencode.env"))
    names = load_env_file(path)
    console.print(f"[dim]cargadas {', '.join(names)} de {path}[/]")
    return workspace.has_provider()


# ─────────────────────────────  las etapas que se corren solas  ─────────────────


@dataclass
class Stage:
    """Una etapa del survey, con lo que el wizard necesita saber antes de correrla."""

    name: str
    what: str                      # qué hace, en una línea, para quien no leyó el spec
    run: Callable[[Console, Workspace], None]
    model: bool = False            # ¿llama al modelo? entonces se pregunta antes


def _ingest(console: Console, workspace: Workspace) -> None:
    with console.status("parseando") as status:
        render.ingestion(console, prep.ingest(workspace, progress=status.update))


def _extract(console: Console, workspace: Workspace) -> None:
    with console.status("extrayendo") as status:
        render.extraction(console, iterate.extract(workspace, progress=status.update))


def _corefer(console: Console, workspace: Workspace) -> None:
    with console.status("correferencia") as status:
        render.coreference(console, iterate.corefer(workspace, progress=status.update))


def _match(console: Console, workspace: Workspace) -> None:
    with console.status("tipando") as status:
        render.matching(console, iterate.match(workspace, progress=status.update))


def _bridge(console: Console, workspace: Workspace) -> None:
    with console.status("puenteando") as status:
        render.bridging(console, iterate.bridge(workspace, progress=status.update))


def _induce(console: Console, workspace: Workspace) -> None:
    with console.status("induciendo") as status:
        render.induction(console, iterate.induce(workspace, progress=status.update))


def _axiomatize(console: Console, workspace: Workspace) -> None:
    with console.status("axiomatizando") as status:
        render.axiomatization(console, iterate.axiomatize(workspace, progress=status.update))


def _regenerate(console: Console, workspace: Workspace) -> None:
    render.regeneration(console, iterate.regenerate(workspace))


def _branch(console: Console, workspace: Workspace) -> None:
    """La misma función que atiende el punto de decisión.

    Cuando no hay eje, `branch` es una etapa que corre sola y aplica todo — el camino habitual
    de `ITER-BRANCH`. Cuando lo hay, es una pregunta. Quién de las dos cosas es no se sabe
    hasta correr el survey, así que hay una sola implementación y decide ella.
    """
    _decide_branch(console, workspace)


def _questions(console: Console, workspace: Workspace) -> None:
    render.question_run(
        console, evaluate.evaluate_questions(workspace),
        target=workspace.config.cq.target_pass_rate,
    )


def _stop(console: Console, workspace: Workspace) -> None:
    render.stopping(console, evaluate.assess(workspace))


STAGES = {
    "ingest": Stage(
        "ingest", "clasifica las páginas, parsea el corpus y llena el almacén de bloques",
        _ingest,
    ),
    "extract": Stage(
        "extract", "le pide al modelo los sintagmas candidatos de cada chunk", _extract,
        model=True,
    ),
    "coref": Stage(
        "coref", "agrupa las menciones que hablan de lo mismo dentro de un documento",
        _corefer, model=True,
    ),
    "match": Stage(
        "match", "tipa cada mención contra la ontología y separa la zona gris de las huérfanas",
        _match,
    ),
    "bridge": Stage(
        "bridge", "pregunta si una huérfana es en realidad un caso de una clase que ya existe",
        _bridge, model=True,
    ),
    "induce": Stage(
        "induce", "agrupa las huérfanas que quedaron y le pide al modelo que las nombre",
        _induce, model=True,
    ),
    "axiomatize": Stage(
        "axiomatize", "convierte cada propuesta en axiomas, los valida y commitea una versión",
        _axiomatize, model=True,
    ),
    "regenerate": Stage(
        "regenerate", "recalcula el ABox desde la capa de menciones; es puro y no pregunta nada",
        _regenerate,
    ),
    "branch": Stage(
        "branch", "busca los ejes de decisión de esta iteración; si no hay ninguno, aplica todo",
        _branch,
    ),
    "competency questions": Stage(
        "competency questions",
        "corre las CQ aceptadas contra la ontología y registra la tasa de aprobación",
        _questions,
    ),
    "stop?": Stage(
        "stop?", "mira los cuatro criterios de parada y dice si tiene sentido seguir", _stop,
    ),
}


# ─────────────────────────────  los puntos de decisión  ─────────────────────────


def _decide_grey(console: Console, workspace: Workspace) -> None:
    """La zona gris, un par por vez.

    «Ninguna» es una respuesta de verdad y a menudo la correcta: la mención se vuelve huérfana
    y llega a la inducción, que es donde un concepto genuinamente nuevo pertenece.
    """
    queue = iterate.grey_pending(workspace)
    if not queue.pairs:
        return
    console.print(Panel.fit(
        f"[bold]zona gris[/] · {len(queue.pairs)} pares que el matcher no decide solo\n"
        "[dim]El umbral no los tipa a propósito. Contestar uno vale para siempre: la respuesta "
        "sobrevive al próximo `match`.[/]",
        border_style="yellow",
    ))
    answered = 0
    for pair in queue.pairs:
        options: list[tuple[str, str]] = []
        if pair.iri:
            options.append((pair.candidate, pair.iri))
        if pair.runner_up_iri and pair.runner_up_iri != pair.iri:
            options.append((pair.runner_up, pair.runner_up_iri))

        console.print(
            f"\n«[bold]{pair.surface_text}[/]» · {pair.document_id[:32]} · "
            f"puntaje {pair.score:.3f}"
        )
        for index, (label, _) in enumerate(options, start=1):
            console.print(f"  {_key(str(index))} {label}")
        console.print(f"  {_key(NONE)} ninguna — queda huérfana y la ve la inducción")
        console.print(f"  {_key(SKIP)} saltear · {_key(ABORT)} dejar el resto para después")

        choice = _ask(console, "¿Cuál es?", default=SKIP).strip().lower()
        if choice == ABORT:
            break
        if choice == SKIP or not choice:
            continue
        if choice == NONE:
            iterate.grey_answer(workspace, pair.mention_id, none_of_these=True)
            answered += 1
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            iterate.grey_answer(workspace, pair.mention_id, to=options[int(choice) - 1][1])
            answered += 1
            continue
        console.print("[yellow]no entendí; la salteo[/]")

    remaining = len(iterate.grey_pending(workspace).pairs)
    console.print(
        f"\n[green]{answered} contestadas[/], {remaining} siguen esperando. "
        "Las respuestas se aplican en el próximo `match`."
    )


def _decide_branch(console: Console, workspace: Workspace) -> None:
    """La rama. A un modelo nunca se le piden alternativas acá: los ejes salen del razonador
    y de un catálogo enumerado (`ITER-BRANCH`)."""
    with console.status("buscando ejes de decisión") as status:
        survey = iterate.survey_branches(workspace, progress=status.update)
    render.branches(console, survey)
    if survey.automatic:
        return

    ids = [
        branch.id for decision in survey.decisions for branch in decision.branches
    ]
    console.print(
        "\n[dim]Elegir una rama descarta a sus hermanas, y ese descarte se guarda: es lo que "
        "una iteración posterior lee para no volver a proponer lo mismo.[/]"
    )
    choice = _ask(
        console, f"¿Qué rama? ({', '.join(ids)}, o {_key(SKIP)} para decidir después)",
        default=SKIP,
    ).strip()
    if choice in (SKIP, ABORT, ""):
        console.print("[yellow]sin decidir[/]: las etapas que siguen quedan bloqueadas.")
        return
    if choice not in ids:
        console.print(f"[yellow]{choice!r} no es una de las ramas; no se aplicó nada[/]")
        return

    why = _ask(console, "¿Por qué ésa? (queda guardado con la decisión)", default="")
    invalid = _ask(
        console,
        "¿Alguna hermana está además MAL, no sólo no elegida? (ids separados por coma)",
        default="",
    )
    with console.status("aplicando la rama") as status:
        result = iterate.choose_branch(
            workspace, choice, why=why,
            invalid=[item.strip() for item in invalid.split(",") if item.strip()],
            progress=status.update,
        )
    render.branch_choice(console, result)


# Qué significa aceptar y qué rechazar, por tipo de hallazgo. Aceptar es siempre «el hallazgo se
# sostiene»; lo que cambia es qué se sigue de eso, y sin decirlo la pregunta no se puede contestar.
MEANING = {
    "typo": ("es una errata — se corrige la etiqueta al volver a normalizar",
             "no es una errata — la etiqueta queda como está"),
    "divergent_label": (
        "no son el mismo término — el nombre que sale del identificador deja de ser un nombre "
        "del concepto",
        "sí son el mismo término — el concepto se queda con los dos nombres",
    ),
    "pending_semantic_check": (
        "no son el mismo término — el nombre que sale del identificador deja de ser un nombre "
        "del concepto",
        "sí son el mismo término — el concepto se queda con los dos nombres",
    ),
}


def _decide_review(console: Console, workspace: Workspace) -> None:
    """Lo que espera decisión: erratas de la ontología inicial, conflictos, propiedades
    funcionales."""
    items = evaluate.review_items(workspace)
    if not items:
        return
    console.print(Panel.fit(
        f"[bold]revisión[/] · {len(items)} hallazgos esperando una decisión\n"
        "[dim]Las decisiones sobre la ontología inicial se aplican al volver a normalizar; las "
        "demás se registran.[/]",
        border_style="yellow",
    ))
    decided_on_the_ontology = 0
    for item in items:
        console.print(f"\n[bold]{item['kind']}[/] · {item['summary']}")
        meaning = MEANING.get(item["kind"])
        if meaning:
            console.print(f"  [dim]aceptar: {meaning[0]}[/]")
            console.print(f"  [dim]rechazar: {meaning[1]}[/]")
        choice = _ask(
            console,
            f"{_key('a')}ceptar · {_key('r')}echazar · {_key(SKIP)} saltear · "
            f"{_key(ABORT)} dejar el resto para después",
            default=SKIP,
        ).strip().lower()
        if choice == ABORT:
            break
        if choice.startswith("a"):
            comment = _ask(console, "¿Por qué? (opcional)", default="")
            evaluate.resolve_review(workspace, item["id"], "accepted", comment=comment)
        elif choice.startswith("r"):
            comment = _ask(console, "¿Por qué? (opcional)", default="")
            evaluate.resolve_review(workspace, item["id"], "rejected", comment=comment)
        else:
            continue
        decided_on_the_ontology += bool(meaning)

    if decided_on_the_ontology:
        _apply_to_the_ontology(console, workspace, decided_on_the_ontology)


def _apply_to_the_ontology(console: Console, workspace: Workspace, decided: int) -> None:
    """Volver a normalizar es lo que **aplica** lo decidido: las correcciones son insumo de esa
    etapa, no una edición sobre la versión ya commiteada.

    Se ofrece en vez de hacerse solo porque commitea una versión nueva de la ontología, y una
    versión que nadie pidió es la clase de cosa que este wizard no hace.
    """
    console.print(
        f"\n[bold]{decided} decisión(es) sobre la ontología inicial.[/] Se aplican volviendo a "
        "normalizar, que deriva las etiquetas otra vez y commitea una versión nueva si algo "
        "cambió."
    )
    if not _confirm(console, "¿Vuelvo a normalizar?"):
        console.print("[yellow]quedan registradas[/]: se aplican en la próxima normalización.")
        return
    with console.status("normalizando la ontología inicial"):
        result = prep.normalize(workspace)
    render.normalization(console, result)
    if result.committed is not None:
        published = deliver.publish_diff(workspace, result.committed.id)
        if published is not None:
            render.comparison(console, published)


DECISIONS = {
    "grey zone": _decide_grey,
    "branch": _decide_branch,
    "review": _decide_review,
}


# ─────────────────────────────  la ontología inicial  ─────────────────────────────


def _normalize_initial(console: Console, workspace: Workspace) -> bool:
    """`PREP-NORMALIZE`, que no está en el survey porque pasa una sola vez y antes que todo.

    Sin versión no hay contra qué tipar, así que el wizard la trata como precondición y no
    como etapa opcional.
    """
    existing = workspace.latest_version()
    if existing is not None:
        console.print(f"[dim]la ontología ya está normalizada · versión {existing}[/]")
        return True

    console.print(Panel.fit(
        "[bold]PREP-NORMALIZE[/] · normalizar la ontología inicial\n"
        "IRIs opacos, etiquetas derivadas del nombre, detección de erratas, y el contexto de "
        "cada clase que todavía no tiene definición.",
        border_style="cyan",
    ))
    if not _confirm(console, "¿La normalizo?"):
        return False

    with console.status("normalizando la ontología inicial"):
        result = prep.normalize(workspace)
    render.normalization(console, result)
    if result.committed is not None:
        published = deliver.publish_diff(workspace, result.committed.id)
        if published is not None:
            render.comparison(console, published)

    if not result.pending_glosses:
        return True
    console.print(
        f"\n[bold]{result.pending_glosses} clases no tienen definición.[/] La glosa es contra "
        "lo que compara el matcher, así que escribirlas es lo que evita que el corpus parezca "
        "hablar de otra cosa."
    )
    if not _provider(console, workspace):
        console.print("[yellow]sin proveedor[/]: se siguen sin glosas, con peor matching.")
        return True
    if not _confirm(
        console, f"¿Le pido al modelo las {result.pending_glosses} definiciones?"
    ):
        return True
    with console.status("escribiendo glosas") as status:
        bootstrap = prep.generate_glosses(workspace, result, progress=status.update)
    render.glosses(console, bootstrap)
    return True


# ─────────────────────────────  una pasada  ─────────────────────────────


def _run_stage(console: Console, workspace: Workspace, step, stage: Stage) -> None:
    console.print(Panel.fit(
        f"[bold]{stage.name}[/] · {stage.what}\n[dim]{step.detail}[/]",
        border_style="cyan",
    ))
    if stage.model:
        console.print(
            "[yellow]Esta etapa llama al modelo[/]: tarda y cuesta. `status` muestra después "
            "cuántas unidades de trabajo y cuántos tokens se gastaron."
        )
        if not _provider(console, workspace):
            console.print("[yellow]no se puede correr sin credencial; la salteo[/]")
            return
    # El nombre de la etapa `stop?` ya trae su signo; pegarle otro da «¿Corro stop??».
    if not _confirm(console, f"¿Corro {stage.name.rstrip('?')}?"):
        return
    try:
        stage.run(console, workspace)
    except StageError as exc:
        # Un `StageError` es algo que el usuario tiene que arreglar, no un bug: se muestra y el
        # wizard sigue vivo, que es la diferencia con el CLI, que sale.
        console.print(f"[red]{exc}[/]")
    except StageAborted as exc:
        # La etapa se cortó por tasa de fallas. Lo que importa mostrar es **qué dijeron** las
        # unidades, no el porcentaje: el porcentaje no se puede arreglar.
        render.stage_aborted(console, exc)


def _pass(console: Console, workspace: Workspace) -> None:
    """Un recorrido por el plan, en el orden del spec: decidir lo que espera, correr lo que se
    puede, y decir por qué no lo demás."""
    version_id = workspace.latest_version() or "(sin versión todavía)"
    plan = orchestration.survey(
        workspace.conn, version_id, session_id=workspace.require_session(),
        has_provider=workspace.has_provider(),
    )
    render.plan(console, plan, version_id)

    for step in plan.steps:
        if step.state == orchestration.DONE:
            continue
        if step.state == orchestration.BLOCKED:
            console.print(f"[dim]{step.name}: bloqueada — {step.detail}[/]")
            continue
        if step.state == orchestration.WAITING:
            handler = DECISIONS.get(step.name)
            if handler is not None:
                handler(console, workspace)
            continue
        stage = STAGES.get(step.name)
        if stage is None:
            console.print(f"[dim]{step.name}: {step.detail} · `{step.command}`[/]")
            continue
        _run_stage(console, workspace, step, stage)


# ─────────────────────────────  la entrega  ─────────────────────────────


def _deliver(console: Console, workspace: Workspace) -> None:
    version_id = workspace.latest_version()
    if version_id is None:
        console.print("[yellow]no hay ninguna versión que entregar todavía[/]")
        return
    console.print(Panel.fit(
        "[bold]la ontología enriquecida[/]\n"
        "Toda la historia del linaje, la TBox y las instancias derivadas del corpus, en un "
        "archivo. La procedencia va en un manifiesto al lado.",
        border_style="green",
    ))
    if not _confirm(console, f"¿Exporto {version_id}?"):
        return
    fmt = _ask(
        console,
        "¿Formato? trig conserva la procedencia por documento, ttl la aplana",
        default=deliver.TRIG,
    ).strip().lower()
    out = _ask(console, "¿A qué archivo? (vacío = junto a las demás)", default="").strip()
    with console.status("exportando") as status:
        result = deliver.export(
            workspace, version=version_id, out=Path(out) if out else None,
            fmt=fmt if fmt in (deliver.TRIG, deliver.TURTLE) else deliver.TRIG,
            progress=status.update,
        )
    render.delivery(console, result)


# ─────────────────────────────  el bucle  ─────────────────────────────


def run(
    console: Console,
    config_path: Path,
    *,
    session_id: str | None = None,
    env_file: Path | None = None,
) -> None:
    """El wizard entero: elegir sesión, preparar, iterar, entregar."""
    try:
        workspace = _open(console, config_path, session_id, env_file)
        if not _normalize_initial(console, workspace):
            return
        while True:
            _pass(console, workspace)
            console.print()
            if not _confirm(
                console,
                "¿Otra pasada? (las etapas ya hechas se saltean; las decisiones abiertas "
                "vuelven a preguntarse)",
                default=False,
            ):
                break
        _deliver(console, workspace)
        console.print(
            "\n[dim]`onto-pipeline next` dice dónde quedó esto, y cada etapa tiene su comando "
            "suelto si querés correrla a mano.[/]"
        )
    except Abort:
        console.print(
            "\n[yellow]cortado[/]. Nada se perdió: el estado está en el almacén y "
            "`onto-pipeline wizard` retoma donde quedó."
        )
    except StageError as exc:
        console.print(f"[red]{exc}[/]")
