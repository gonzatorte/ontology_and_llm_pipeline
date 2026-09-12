"""Qué etapas hay, cómo se llaman y cuáles tardan.

El catálogo existe porque hay más de una interfaz y ninguna puede ser la dueña de la lista. El
CLI la tiene implícita en sus comandos; la API necesita la misma lista explícita para encolar un
job por nombre, y `wizard` recorre el mismo plan. Acá está una sola vez, sin `typer` y sin HTTP.

**Qué es un job y qué es síncrono.** Es job lo que llama al modelo, al razonador o a los
encoders, y lo que parsea PDFs: minutos, a veces más. Es síncrono lo que consulta el almacén y lo
que registra una decisión, que contesta en un parpadeo y no vale la pena poletear. `regenerate`
es síncrono a propósito aunque escriba: es función pura de (menciones, tipados, reglas) y no
llama a nadie.

**Los parámetros se enumeran.** Nada de pasar el cuerpo del request como `**kwargs`: lo que no
está en la lista de una etapa no llega a la función de servicio.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from .. import orchestration
from . import deliver, evaluate, iterate, prep
from .workspace import Progress, StageError, Workspace


@dataclass(frozen=True)
class Stage:
    """Una etapa corrible, con lo que hace falta para correrla desde cualquier interfaz."""

    name: str
    run: Callable[..., Any]
    # Nombre del parámetro -> tipo aceptado. Se valida antes de llamar.
    params: dict[str, type]
    # Cómo se llama en `orchestration.survey`, si aparece. Vacío quiere decir que el plan no
    # opina sobre esta etapa y entonces no hay compuerta que consultar.
    plan_step: str = ""
    needs_provider: bool = False
    # Si tarda lo suficiente como para que valga la pena encolarla en vez de contestarla.
    long: bool = True


def _stage(name, run, params=None, *, plan_step="", needs_provider=False, long=True) -> Stage:
    return Stage(name, run, params or {}, plan_step, needs_provider, long)


STAGES: dict[str, Stage] = {
    stage.name: stage
    for stage in (
        # ── PREP ──
        _stage("ingest", prep.ingest, {"limit": int}, plan_step="ingest"),
        _stage("normalize", prep.normalize),
        _stage("alignment", prep.alignment, {"version": str}),
        _stage("propose-cq", prep.propose_questions,
               {"version": str, "per_stratum": int, "seed": int}, needs_provider=True),
        # ── ITER ──
        _stage("extract", iterate.extract, {"doc_id": str, "include_held_out": bool},
               plan_step="extract", needs_provider=True),
        _stage("coref", iterate.corefer, {"doc_id": str, "include_held_out": bool},
               plan_step="coref", needs_provider=True),
        _stage("match", iterate.match, {"version": str, "include_held_out": bool},
               plan_step="match"),
        _stage("bridge", iterate.bridge, {"version": str}, plan_step="bridge",
               needs_provider=True),
        _stage("induce", iterate.induce, {"version": str}, plan_step="induce",
               needs_provider=True),
        _stage("axiomatize", iterate.axiomatize, {"version": str, "override_structural": bool},
               plan_step="axiomatize", needs_provider=True),
        _stage("enrich", iterate.enrich, {"version": str, "limit": int, "dry_run": bool},
               needs_provider=True),
        _stage("validate", iterate.validate, {"version": str}),
        # ── EVAL ──
        _stage("cq-eval", evaluate.evaluate_questions, {"version": str, "iteration": int},
               needs_provider=True),
        # ── síncronas: contestan en el request que las pidió ──
        _stage("regenerate", iterate.regenerate, {"version": str, "force": bool},
               plan_step="regenerate", long=False),
        _stage("export", deliver.export,
               {"version": str, "fmt": str, "include_abox": bool, "refresh_abox": bool},
               long=False),
        _stage("stop", evaluate.assess, {"version": str, "iteration": int},
               plan_step="stop?", long=False),
    )
}

JOBS = {name: stage for name, stage in STAGES.items() if stage.long}


def stage(name: str) -> Stage:
    try:
        return STAGES[name]
    except KeyError as exc:
        raise StageError(
            f"no hay etapa {name!r}; hay {', '.join(sorted(STAGES))}"
        ) from exc


def clean(spec: Stage, params: dict | None) -> dict:
    """Los parámetros que esta etapa acepta, con el tipo que declara. El resto se rechaza."""
    given = params or {}
    unknown = sorted(set(given) - set(spec.params))
    if unknown:
        raise StageError(
            f"{spec.name} no acepta {', '.join(unknown)}"
            + (f"; acepta {', '.join(sorted(spec.params))}" if spec.params else
               " ningún parámetro")
        )
    clean_params = {}
    for key, value in given.items():
        if value is None:
            continue
        expected = spec.params[key]
        # `bool` es `int` en Python y no al revés: aceptar `True` donde se pide un entero es
        # cómo un `limit` termina valiendo 1 sin que nadie lo haya pedido.
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise StageError(f"{spec.name}: {key} tiene que ser {expected.__name__}")
        clean_params[key] = value
    return clean_params


def blocked_by(workspace: Workspace, spec: Stage) -> str:
    """Qué impide correr esta etapa ahora, o vacío si nada.

    Es `DECISION-NEVER-CROSSED` sobre HTTP: `next` frena ante un punto de decisión y `wizard` lo
    pregunta. Correr lo que viene después de una decisión que nadie tomó es tomarla por default,
    que es lo que `BRANCH-ONLY-REVIEW` nombra. El plan no opina sobre toda etapa; de las que
    opina, manda.
    """
    if spec.needs_provider and not workspace.has_provider():
        return (
            f"{spec.name} llama al modelo y no hay credencial cargada para el proveedor "
            f"configurado ({workspace.config.llm.provider})"
        )
    if not spec.plan_step:
        return ""
    version_id = workspace.plan_version()
    plan = orchestration.survey(
        workspace.conn, version_id, session_id=workspace.require_session(),
        has_provider=workspace.has_provider(),
    )
    step = next((item for item in plan.steps if item.name == spec.plan_step), None)
    if step is None or step.state in (orchestration.READY, orchestration.DONE):
        return ""
    if step.state == orchestration.BLOCKED:
        return f"{spec.name}: {step.detail}"
    pending = orchestration.blocking(plan)
    return f"{spec.name} espera una decisión: " + "; ".join(
        f"{item.name} — {item.detail}" for item in pending
    ) if pending else f"{spec.name}: {step.detail}"


def run(
    workspace: Workspace,
    name: str,
    params: dict | None = None,
    *,
    progress: Progress | None = None,
) -> Any:
    """Correr una etapa por nombre. Devuelve lo que devuelve el servicio, sin tocarlo."""
    spec = stage(name)
    blocked = blocked_by(workspace, spec)
    if blocked:
        raise StageError(blocked)
    arguments = clean(spec, params)
    if progress is not None and _takes_progress(spec.run):
        arguments["progress"] = progress
    return spec.run(workspace, **arguments)


def _takes_progress(function: Callable) -> bool:
    import inspect  # noqa: PLC0415 - sólo para esto

    return "progress" in inspect.signature(function).parameters


# ─────────────────────────────  el resultado, en algo que viaje  ─────────────────────────────

_SIMPLE = (str, int, float, bool)


def summarize(result: Any) -> dict:
    """El resultado de una etapa en algo serializable, sin inventarle forma.

    Los resultados son dataclasses con grafos, artefactos y objetos de dominio adentro. Lo que
    viaja es lo que se puede escribir: los números y los textos tal cual, las rutas y los
    artefactos como su nombre, y lo demás como su tamaño — que es lo que alguien quiere saber de
    una lista de menciones, y no las menciones.
    """
    if not is_dataclass(result):
        return {"value": _value(result)}
    return {item.name: _value(getattr(result, item.name)) for item in fields(result)}


def _value(value: Any) -> Any:
    from ..artifacts import Artifact

    if value is None or isinstance(value, _SIMPLE):
        return value
    if isinstance(value, Artifact | Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_value(item) for item in value] if len(value) <= 50 else {"count": len(value)}
    if is_dataclass(value):
        return summarize(value)
    return str(value)[:200]
