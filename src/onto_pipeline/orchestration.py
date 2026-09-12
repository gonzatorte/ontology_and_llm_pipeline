"""What to run next, and what is waiting on a person (REORG, ITER, EVAL-STOPPING).

The pipeline is a sequence of stages with five points where the *user* decides — the matcher's
grey zone (`ITER-MATCH`), the branch (`ITER-BRANCH`), a functional property (`ITER-APPLY`), a
competency question (`PREP-CQ-USER`), a typo in the seed (`PREP-NORMALIZE`). An orchestrator
that ran straight through them would be deciding those
by default, which is the failure the spec names in BRANCH-ONLY-REVIEW and AUTO-APPLY-WHEN-NO-AXES
from both ends: never ask, and the
system quietly picks the modelling; ask about everything, and it becomes the manual work it
exists to replace.

So this reads the store and answers one question — what is the next thing to do — with three
possible shapes of answer:

    READY     a stage can run now, and here is the command
    WAITING   a decision is open; nothing downstream is worth running until it is made
    DONE      it has already run against this version

The order is the spec's own. What makes the answer useful rather than a checklist is that each
step knows what it *needs*: a step whose input does not exist yet is not "pending", it is
blocked, and saying which is the difference between advice and a list.

**`--run` corre la etapa siguiente, una sola, y frena.** La ejecuta como un proceso aparte —el
mismo comando que imprimiría— en vez de llamar a la función desde adentro. Extraer los diez
comandos de sus envoltorios de Typer era la otra opción y es un refactor grande a cambio de nada
que se note: el subproceso conserva la salida del comando, su manejo de errores y su código de
retorno tal cual, que es justamente lo que un runner tiene que no romper. Lo que cuesta son un
par de segundos de arranque por etapa, contra minutos de LLM.

Lo que **no** hace, y es la parte que importa: no cruza un punto de decisión. Si lo siguiente es
algo que decide el usuario, imprime cuál y sale sin correr nada.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .store import Store

READY = "ready"
WAITING = "waiting on you"
BLOCKED = "blocked"
DONE = "done"


@dataclass
class Step:
    name: str
    command: str
    state: str
    detail: str = ""
    decision: bool = False       # this one is a person's, not the pipeline's


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)

    @property
    def next(self) -> Step | None:
        """A decision first, then the earliest stage that can run.

        A decision outranks a runnable stage because the stages after it would be built on an
        answer nobody gave. A blocked stage is never the answer: it is not something to do, it
        is something downstream of something else.
        """
        waiting = next((step for step in self.steps if step.state == WAITING), None)
        return waiting or next(
            (step for step in self.steps if step.state == READY), None
        )


_TABLE = re.compile(r"\bFROM\s+([a-z_]+)")


def _count(conn: Store, query: str, params: tuple = ()) -> int:
    """Cuenta, y devuelve 0 si la tabla es de una etapa que nunca corrió.

    Se **pregunta** si la tabla está en vez de intentar y atajar el error: en Postgres una
    sentencia que falla aborta la transacción entera, así que el `except` que servía en SQLite
    dejaba la conexión inutilizable para las once consultas que siguen.
    """
    table = _TABLE.search(query)
    if table and not conn.table_exists(table.group(1)):
        return 0
    row = conn.execute(query, params).fetchone()
    return int(row["n"]) if row else 0


def survey(
    conn: Store, version_id: str, *, session_id: str, has_provider: bool
) -> Plan:
    """The state of every stage against one ontology version."""
    documents = _count(
        conn, "SELECT COUNT(*) AS n FROM documents WHERE session_id = ? AND held_out = 0",
        (session_id,),
    )
    blocks = _count(conn, "SELECT COUNT(*) AS n FROM blocks WHERE session_id = ?", (session_id,))
    mentions = _count(
        conn, "SELECT COUNT(*) AS n FROM mentions WHERE session_id = ?", (session_id,)
    )
    grouped = _count(
        conn, "SELECT COUNT(*) AS n FROM mentions WHERE session_id = ? AND coref_group IS NOT NULL",
        (session_id,),
    )
    # Joined against `mentions`, not counted raw. A version matched before a re-extraction keeps
    # typings for mentions that no longer exist, and counting those makes this command disagree
    # with `grey list`, which does join — two numbers for one question is worse than either.
    live = ("FROM mention_typing t "
            "JOIN mentions m ON m.id = t.mention_id AND m.session_id = ? "
            "WHERE t.version_id = ?")
    scope = (session_id, version_id)
    typed = _count(conn, f"SELECT COUNT(*) AS n {live}", scope)
    grey = _count(conn, f"SELECT COUNT(*) AS n {live} AND t.zone = 'grey'", scope)
    orphans = _count(conn, f"SELECT COUNT(*) AS n {live} AND t.iri IS NULL", scope)
    stale = _count(
        conn, "SELECT COUNT(*) AS n FROM mention_typing WHERE version_id = ?", (version_id,)
    ) - typed
    bridged = _count(conn, "SELECT COUNT(*) AS n FROM bridges WHERE version_id = ?", (version_id,))
    proposals = _count(
        conn, "SELECT COUNT(*) AS n FROM proposed_classes WHERE version_id = ?", (version_id,)
    )
    axioms = _count(
        conn, "SELECT COUNT(*) AS n FROM proposed_axioms WHERE version_id = ?", (version_id,)
    )
    open_branches = _count(
        conn, "SELECT COUNT(*) AS n FROM branches WHERE version_id = ? AND status = 'proposed'",
        (version_id,),
    )
    open_reviews = _count(
        conn,
        "SELECT COUNT(*) AS n FROM review_items WHERE session_id = ? AND status = 'open'",
        (session_id,),
    )
    unverified_labels = _count(
        conn,
        "SELECT COUNT(*) AS n FROM review_items WHERE session_id = ? AND status = 'open' "
        "AND kind IN ('divergent_label', 'pending_semantic_check')",
        (session_id,),
    )
    # El estado de la etapa se deriva de los datos, como todo lo demás (`PHASE-DERIVED`): sus
    # unidades en el ledger dicen si corrió, sin una columna que alguien tenga que acordarse de
    # escribir.
    verified = _count(
        conn,
        "SELECT COUNT(*) AS n FROM work_units WHERE session_id = ? AND stage = ? "
        "AND status = 'done'",
        (session_id, "prep_normalize_labels"),
    )
    questions = _count(
        conn,
        "SELECT COUNT(*) AS n FROM competency_questions WHERE session_id = ? AND status='accepted'",
        (session_id,),
    )

    def stage(
        name: str, command: str, done: bool, ready: bool, detail: str,
        blocked_by: str = "", decision: bool = False,
    ) -> Step:
        if done:
            return Step(name, command, DONE, detail, decision)
        if not ready:
            return Step(name, command, BLOCKED, blocked_by or detail, decision)
        return Step(name, command, WAITING if decision else READY, detail, decision)

    llm_note = "" if has_provider else " (needs a provider: --env-file)"
    steps = [
        stage("ingest", "onto-pipeline ingest", bool(blocks), True,
              f"{documents} documents, {blocks} blocks",
              "no PDF found under corpus_root"),
        stage("extract", f"onto-pipeline extract{llm_note}", bool(mentions), bool(blocks),
              f"{mentions} mentions", "nothing parsed yet: run ingest"),
        stage("coref", f"onto-pipeline coref{llm_note}", bool(grouped), bool(mentions),
              f"{grouped} of {mentions} mentions grouped", "no mentions: run extract"),
        stage("match", "onto-pipeline match", bool(typed), bool(mentions),
              f"{typed} mentions typed against {version_id}"
              + (f" · {stale} stale typings from an older mention layer" if stale else ""),
              "no mentions: run extract"),
        stage("grey zone", "onto-pipeline grey list", not grey, bool(typed),
              f"{grey} mentions are in the grey zone and nothing types them until they are "
              "answered", "nothing typed yet: run match", decision=bool(grey)),
        stage("bridge", f"onto-pipeline bridge{llm_note}", bool(bridged), bool(orphans),
              f"{bridged} bridges over {orphans} orphans",
              "no orphans to bridge: run match"),
        stage("induce", f"onto-pipeline induce{llm_note}", bool(proposals), bool(bridged),
              f"{proposals} proposed classes",
              "run bridge first, or every orphan the seed covered becomes a spurious class"),
        stage("axiomatize", f"onto-pipeline axiomatize{llm_note}", bool(axioms), bool(proposals),
              f"{axioms} proposed axioms", "no proposals: run induce"),
        stage("branch", "onto-pipeline branch", False, bool(axioms),
              f"{open_branches} branch(es) proposed and undecided" if open_branches
              else "no decision axis found yet",
              "no axioms: run axiomatize", decision=bool(open_branches)),
        # Antes de `review`, y no después: las divergencias que el modelo puede verificar son
        # parte de lo que `review` va a preguntar, y preguntarlas antes de verificarlas es
        # cruzar el punto de decisión al revés.
        stage("label check", f"onto-pipeline verify-labels{llm_note}", bool(verified),
              True,
              f"{unverified_labels} label pair(s) nobody has verified"
              if unverified_labels else "no label pair is waiting for a check"),
        # Una decisión le gana a cualquier etapa corrible, así que esta fila es la que `next`
        # elige y es donde tiene que decir que parte de lo que va a preguntar todavía no lo
        # verificó nadie: mandarlo a decidir eso primero es cruzar el punto de decisión al revés.
        stage("review", "onto-pipeline review", not open_reviews, True,
              f"{open_reviews} open item(s): conflicts, functional properties, seed typos"
              + (f" · {unverified_labels} of them are label pairs nobody has verified: "
                 "run verify-labels first" if unverified_labels and not verified else ""),
              decision=bool(open_reviews)),
        stage("regenerate", "onto-pipeline regenerate", False, bool(typed),
              "the ABox is a function of the mentions and this version; rerun it after any "
              "decision", "nothing typed yet: run match"),
        stage("competency questions", "onto-pipeline cq", False, bool(questions),
              f"{questions} accepted questions", "none imported: run import-cq or propose-cq"),
        stage("stop?", "onto-pipeline stop", False, True, "the four criteria of EVAL-STOPPING"),
    ]
    return Plan(steps=steps)


def blocking(plan: Plan) -> list[Step]:
    return [step for step in plan.steps if step.decision and step.state == WAITING]


def runnable(step: Step | None) -> bool:
    """Si esta etapa se puede ejecutar sola. Una decisión nunca, por definición."""
    return step is not None and step.state == READY and not step.decision


def command_line(step: Step, config_path, env_file=None) -> list[str]:
    """El comando de la etapa, como lista de argumentos.

    Se reconstruye desde `step.command`, que es lo que el usuario vería impreso: si alguna vez
    dejan de coincidir, lo que se ejecuta es lo que se mostró, no otra cosa.
    """
    # La nota entre paréntesis es para el lector, no argumentos: se corta entera, no palabra
    # por palabra, o "(needs a provider: --env-file)" entra como cuatro banderas inventadas.
    command = step.command.split("(", 1)[0]
    parts = command.split()
    if parts[:1] == ["onto-pipeline"]:
        parts = parts[1:]
    head = ["onto-pipeline"]
    if env_file:
        head += ["--env-file", str(env_file)]
    return head + parts + ["--config", str(config_path)]


def summarize(plan: Plan, say: Callable[[str], None]) -> None:  # pragma: no cover - printing
    for step in plan.steps:
        say(f"{step.state:>14}  {step.name}: {step.detail}")
