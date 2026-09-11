"""El wizard: lo que fija cada test es algo que el wizard **no** puede hacer.

La interfaz guiada existe para preguntar lo que `next` no pregunta. Eso la pone a un paso de
convertirse en lo contrario de lo que el pipeline es: un botón que corre todo y decide por
default. Los tests de acá son las barandas de ese paso.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

from onto_pipeline import orchestration, wizard
from onto_pipeline.config import Config
from onto_pipeline.db import connect
from onto_pipeline.services import StageError, Workspace

SESSION = "test-1"


@pytest.fixture
def console() -> Console:
    return Console(record=True, width=100)


def _workspace(tmp_path: Path) -> Workspace:
    config = Config.model_validate({
        "paths": {
            "corpus_root": tmp_path / "corpus",
            "initial_ontology": tmp_path / "seed.rdf",
            "work_dir": tmp_path / "work",
        }
    })
    conn = connect(config.paths.work_dir)
    from onto_pipeline import review, typing_store

    typing_store.install(conn)
    review.install(conn)
    return Workspace.of(config, conn, session_id=SESSION)


def _plan(*steps) -> orchestration.Plan:
    return orchestration.Plan(steps=list(steps))


def _step(name, state, *, decision=False):
    return orchestration.Step(name, f"onto-pipeline {name}", state, "", decision)


# ─────────────────────────  no cruzar un punto de decisión  ─────────────────────────


def test_a_pending_decision_is_asked_and_never_answered_by_default(tmp_path, console, monkeypatch):
    """El invariante que comparte con `next`, cumplido al revés.

    `next` frena ante una decisión porque cruzarla sería decidirla por default. El wizard no
    frena: la hace. Lo que ninguno de los dos puede hacer es seguir de largo, y esto lo fija —
    la etapa en `waiting` llega a su handler de pregunta, no al que la corre.
    """
    asked: list[str] = []
    monkeypatch.setattr(
        wizard, "DECISIONS", {"grey zone": lambda _console, _workspace: asked.append("grey zone")}
    )
    monkeypatch.setattr(wizard, "STAGES", {})
    monkeypatch.setattr(
        orchestration, "survey",
        lambda *_args, **_kwargs: _plan(_step("grey zone", orchestration.WAITING, decision=True)),
    )
    wizard._pass(console, _workspace(tmp_path))
    assert asked == ["grey zone"]


def test_a_blocked_stage_is_explained_and_not_attempted(tmp_path, console, monkeypatch):
    """Una etapa bloqueada no es algo pendiente: es algo río abajo de otra cosa. Correrla igual
    daría un error que parece un bug del pipeline y es en realidad el orden."""
    ran: list[str] = []
    monkeypatch.setattr(wizard, "DECISIONS", {})
    monkeypatch.setattr(wizard, "STAGES", {
        "match": wizard.Stage("match", "tipa", lambda *_: ran.append("match")),
    })
    monkeypatch.setattr(
        orchestration, "survey",
        lambda *_args, **_kwargs: _plan(_step("match", orchestration.BLOCKED)),
    )
    wizard._pass(console, _workspace(tmp_path))
    assert ran == []
    assert "bloqueada" in console.export_text()


# ─────────────────────────  no arrancar una corrida cara sola  ─────────────────────


def test_a_stage_that_calls_the_model_is_never_run_without_a_yes(tmp_path, console, monkeypatch):
    """Una corrida de horas arrancada por un Enter distraído es exactamente lo que no puede
    pasar en esta máquina. La etapa dice que llama al modelo, y espera."""
    ran: list[str] = []
    workspace = _workspace(tmp_path)
    monkeypatch.setattr(wizard, "_provider", lambda *_args: True)
    monkeypatch.setattr(wizard, "_confirm", lambda *_args, **_kwargs: False)
    stage = wizard.Stage("extract", "pide sintagmas", lambda *_: ran.append("extract"),
                         model=True)
    wizard._run_stage(console, workspace, _step("extract", orchestration.READY), stage)
    assert ran == []
    assert "llama al modelo" in console.export_text()


def test_a_model_stage_without_a_credential_is_skipped_rather_than_failed(
    tmp_path, console, monkeypatch
):
    """Sin credencial la etapa no puede correr, y decirlo es más útil que un traceback: el
    wizard sigue vivo y las etapas que no necesitan modelo siguen a mano."""
    ran: list[str] = []
    monkeypatch.setattr(wizard, "_provider", lambda *_args: False)
    stage = wizard.Stage("extract", "pide sintagmas", lambda *_: ran.append("extract"),
                         model=True)
    wizard._run_stage(console, _workspace(tmp_path), _step("extract", orchestration.READY), stage)
    assert ran == []


def test_a_stage_error_leaves_the_wizard_alive(tmp_path, console, monkeypatch):
    """La diferencia con el CLI, que sale con un código: acá hay alguien esperando la próxima
    pregunta, y matarle la sesión por una precondición que falta le cuesta todo el contexto."""
    def explode(_console, _workspace):
        raise StageError("no mentions; run extract first")

    monkeypatch.setattr(wizard, "_confirm", lambda *_args, **_kwargs: True)
    stage = wizard.Stage("match", "tipa", explode)
    wizard._run_stage(console, _workspace(tmp_path), _step("match", orchestration.READY), stage)
    assert "no mentions" in console.export_text()


# ─────────────────────────  saltear deja el punto abierto  ─────────────────────────


def test_skipping_a_grey_pair_records_nothing(tmp_path, console, monkeypatch):
    """Saltear no es contestar. «Ninguna» sí es una respuesta —la mención se vuelve huérfana y
    la ve la inducción—, y confundir las dos cosas inventa un tipado que nadie pidió."""
    from onto_pipeline.services import iterate

    answered: list[tuple] = []
    monkeypatch.setattr(
        iterate, "grey_pending",
        lambda *_args, **_kwargs: iterate.GreyQueue(
            version_id="v0",
            pairs=[iterate.GreyPair(
                mention_id="m1", surface_text="focus group", document_id="d1",
                iri="c:A", candidate="A", runner_up_iri="c:B", runner_up="B", score=0.85,
            )],
        ),
    )
    monkeypatch.setattr(
        iterate, "grey_answer",
        lambda *args, **kwargs: answered.append((args, kwargs)),
    )
    monkeypatch.setattr(wizard, "_ask", lambda *_args, **_kwargs: wizard.SKIP)
    wizard._decide_grey(console, _workspace(tmp_path))
    assert answered == []


def test_none_of_these_is_recorded_as_the_answer_it_is(tmp_path, console, monkeypatch):
    """«Ninguna» es a menudo la respuesta correcta, y llega al almacén como respuesta: si se
    tratara como un salteo, el mismo par se volvería a preguntar en cada corrida, que es como
    se entrena a alguien a dejar de contestar."""
    from onto_pipeline.services import iterate

    answered: list[dict] = []
    monkeypatch.setattr(
        iterate, "grey_pending",
        lambda *_args, **_kwargs: iterate.GreyQueue(
            version_id="v0",
            pairs=[iterate.GreyPair(
                mention_id="m1", surface_text="focus group", document_id="d1",
                iri="c:A", candidate="A", runner_up_iri=None, runner_up="", score=0.85,
            )],
        ),
    )
    monkeypatch.setattr(
        iterate, "grey_answer", lambda _workspace, _mention, **kwargs: answered.append(kwargs)
    )
    answers = iter([wizard.NONE])
    monkeypatch.setattr(wizard, "_ask", lambda *_args, **_kwargs: next(answers))
    wizard._decide_grey(console, _workspace(tmp_path))
    assert answered == [{"none_of_these": True}]
