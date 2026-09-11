from __future__ import annotations

import pytest

from onto_pipeline.config import Execution
from onto_pipeline.db import connect
from onto_pipeline.telemetry import (
    BarrierViolation,
    Ledger,
    StageAborted,
    UnitResult,
    unit_key,
)

SESSION = "test-1"



@pytest.fixture
def ledger(tmp_path):
    conn = connect(tmp_path)
    execution = Execution(max_retries=3, backoff_base_s=0, stage_failure_rate_abort=0.10)
    return Ledger(conn, execution, sleep=lambda _: None, session_id=SESSION)


def payloads(n):
    return [(f"u{i}", {"i": i}) for i in range(n)]


def test_resume_skips_done_units(ledger):
    """Cache and checkpoint are the same mechanism: resuming is re-running the stage."""
    calls = []

    def worker(payload):
        calls.append(payload["i"])
        return UnitResult(output=payload["i"] * 2)

    first = ledger.run("ITER-EXTRACT", payloads(3), worker)
    second = ledger.run("ITER-EXTRACT", payloads(3), worker)

    assert calls == [0, 1, 2]
    assert first.executed == 3 and second.cached == 3
    assert second.outputs == {"u0": 0, "u1": 2, "u2": 4}


def test_interrupted_stage_resumes_at_the_failed_unit(ledger):
    """A stage of 600 chunks interrupted at 400 resumes at 401."""
    def flaky(payload):
        if payload["i"] == 1:
            raise RuntimeError("upstream timeout")
        return UnitResult(output=payload["i"])

    ledger.run("ITER-EXTRACT", payloads(20), flaky)
    calls = []

    def recovered(payload):
        calls.append(payload["i"])
        return UnitResult(output=payload["i"])

    result = ledger.run("ITER-EXTRACT", payloads(20), recovered)
    assert calls == [1]
    assert result.cached == 19


def test_exhausted_retries_land_as_failed_without_stopping_the_stage(ledger):
    def worker(payload):
        if payload["i"] == 0:
            raise RuntimeError("boom")
        return UnitResult(output=payload["i"])

    result = ledger.run("ITER-EXTRACT", payloads(20), worker)
    assert set(result.failures) == {"u0"}
    assert len(result.outputs) == 19


def test_stage_aborts_above_the_failure_rate(ledger):
    def worker(payload):
        raise RuntimeError("boom")

    with pytest.raises(StageAborted):
        ledger.run("ITER-EXTRACT", payloads(20), worker)


def test_barrier_blocks_while_units_are_unfinished(ledger):
    def worker(payload):
        raise RuntimeError("boom")

    with pytest.raises(StageAborted):
        ledger.run("ITER-EXTRACT", payloads(20), worker)
    with pytest.raises(BarrierViolation):
        ledger.barrier("ITER-EXTRACT")


def test_failed_units_do_not_block_the_next_stage(ledger):
    def worker(payload):
        if payload["i"] == 0:
            raise RuntimeError("boom")
        return UnitResult(output=payload["i"])

    ledger.run("ITER-EXTRACT", payloads(20), worker)
    ledger.barrier("ITER-EXTRACT")


def test_editing_a_prompt_invalidates_the_cache():
    first = unit_key("ITER-AXIOMATIZE", "v1", 0.7, {"chunk": "text"})
    assert first != unit_key("ITER-AXIOMATIZE", "v2", 0.7, {"chunk": "text"})
    assert first != unit_key("ITER-AXIOMATIZE", "v1", 0.0, {"chunk": "text"})


def test_token_counts_are_recorded_per_stage(ledger):
    def worker(payload):
        return UnitResult(output="ok", in_tokens=100, out_tokens=25)

    result = ledger.run("ITER-AXIOMATIZE", payloads(3), worker)
    assert (result.in_tokens, result.out_tokens) == (300, 75)
    assert ledger.stage_report("ITER-AXIOMATIZE")["in_tokens"] == 300


def test_switching_model_invalidates_the_cache(ledger):
    """Silently serving results from a model the configuration no longer names is worse than
    paying to recompute — and the tier is exactly the setting that is easy to forget."""
    medium = {"tier": "medium", "temperature": 0.0, "reasoning_effort": None}
    small = {"tier": "small", "temperature": 0.0, "reasoning_effort": None}

    calls = []

    def worker(payload):
        calls.append(payload["i"])
        return UnitResult(output="x")

    ledger.run("ITER-EXTRACT", payloads(2), worker, settings=medium)
    ledger.run("ITER-EXTRACT", payloads(2), worker, settings=medium)
    assert len(calls) == 2, "same settings, cache hit"

    ledger.run("ITER-EXTRACT", payloads(2), worker, settings=small)
    assert len(calls) == 4, "different model, recomputed"


def test_reasoning_effort_is_part_of_the_key(ledger):
    base = {"tier": "small", "temperature": 0.0, "reasoning_effort": None}
    low = {"tier": "small", "temperature": 0.0, "reasoning_effort": "low"}
    assert (unit_key("ITER-EXTRACT", "v1", base, {"c": 1})
            != unit_key("ITER-EXTRACT", "v1", low, {"c": 1}))


def test_a_stage_that_persists_on_its_own_does_not_reuse_another_sessions_cache(tmp_path):
    """La trampa del caché compartido, medida contra Postgres antes de arreglarla.

    En un acierto de caché el worker **no corre**: sólo se rellena `outputs`. Para las etapas
    que llaman al modelo eso está bien —el llamador re-persiste desde ahí—, pero `ingest`
    parsea *y* guarda adentro del worker. Compartido, la segunda sesión reportaba «cached» y
    escribía **cero bloques**: el trabajo parecía hecho y no estaba.
    """
    conn = connect(tmp_path)
    payload = [("uno", {"texto": "x"})]

    first = Ledger(conn, Execution(), session_id="a")
    first.run("etapa", payload, lambda _: UnitResult(output={"ok": True}), shared_cache=False)

    ran = []
    second = Ledger(conn, Execution(), session_id="b")
    result = second.run(
        "etapa", payload,
        lambda _: (ran.append(1), UnitResult(output={"ok": True}))[1],
        shared_cache=False,
    )

    assert ran, "el worker de la segunda sesión tiene que correr y escribir lo suyo"
    assert result.executed == 1 and result.cached == 0


def test_a_model_answer_is_reused_across_sessions_because_that_is_what_costs(tmp_path):
    """La otra mitad, y es la que justifica compartir: la clave cubre etapa, prompt, modelo,
    effort, temperatura y hash del input, así que la respuesta de una sesión es la respuesta de
    la otra. Volver a pedirla sería pagarla dos veces."""
    conn = connect(tmp_path)
    payload = [("uno", {"texto": "x"})]

    Ledger(conn, Execution(), session_id="a").run(
        "etapa", payload, lambda _: UnitResult(output={"ok": True})
    )

    ran = []
    result = Ledger(conn, Execution(), session_id="b").run(
        "etapa", payload, lambda _: (ran.append(1), UnitResult(output={}))[1]
    )

    assert not ran and result.cached == 1
    assert result.outputs["uno"] == {"ok": True}


def test_one_sessions_unfinished_work_does_not_block_another(tmp_path):
    """El barrier mira las unidades **propias**. Mirando la tabla entera, una sesión con trabajo
    en vuelo frenaba a las demás — y una interrumpida las frenaba para siempre, porque deja las
    suyas en `running`."""
    conn = connect(tmp_path)
    conn.execute(
        "INSERT INTO work_units (key, session_id, stage, status, created_at) "
        "VALUES ('k', 'a', 'etapa', 'running', '2026-01-01')"
    )
    conn.commit()

    Ledger(conn, Execution(), session_id="b").barrier("etapa")   # no levanta
    with pytest.raises(BarrierViolation):
        Ledger(conn, Execution(), session_id="a").barrier("etapa")


def test_an_aborted_stage_says_what_the_units_actually_said(tmp_path):
    """La tasa es la consecuencia; la causa es lo único que permite arreglarlo.

    El mensaje decía «failure rate 100% over 1 units» y nada más, y lo que había fallado quedaba
    en `work_units.error` sin que ninguna interfaz lo mostrara. Quien lo veía tenía que abrir el
    almacén para enterarse de que era un 401.
    """
    conn = connect(tmp_path)
    ledger = Ledger(conn, Execution(stage_failure_rate_abort=0.10, max_retries=1),
                    session_id="s", sleep=lambda _: None)

    def explode(_payload):
        raise RuntimeError("401 Unauthorized")

    with pytest.raises(StageAborted) as raised:
        ledger.run("iter_corefer", [("d1", {"x": 1})], explode)

    aborted = raised.value
    assert aborted.stage == "iter_corefer"
    assert aborted.failures == {"d1": "RuntimeError: 401 Unauthorized"}
    assert "401 Unauthorized" in str(aborted)
    assert "iter_corefer" in str(aborted)


def test_the_abort_message_does_not_repeat_the_same_error_ten_times(tmp_path):
    """Diez unidades con el mismo 401 son un problema, no diez. Repetirlo entierra el resto."""
    conn = connect(tmp_path)
    ledger = Ledger(conn, Execution(stage_failure_rate_abort=0.10, max_retries=1),
                    session_id="s", sleep=lambda _: None)

    def explode(_payload):
        raise RuntimeError("401 Unauthorized")

    with pytest.raises(StageAborted) as raised:
        ledger.run("iter_extract", [(f"d{i}", {"x": i}) for i in range(4)], explode)

    assert str(raised.value).count("401 Unauthorized") == 1
    assert len(raised.value.failures) >= 1
