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


@pytest.fixture
def ledger(tmp_path):
    conn = connect(tmp_path)
    execution = Execution(max_retries=3, backoff_base_s=0, stage_failure_rate_abort=0.10)
    return Ledger(conn, execution, sleep=lambda _: None)


def payloads(n):
    return [(f"u{i}", {"i": i}) for i in range(n)]


def test_resume_skips_done_units(ledger):
    """Cache and checkpoint are the same mechanism: resuming is re-running the stage."""
    calls = []

    def worker(payload):
        calls.append(payload["i"])
        return UnitResult(output=payload["i"] * 2)

    first = ledger.run("B1", payloads(3), worker)
    second = ledger.run("B1", payloads(3), worker)

    assert calls == [0, 1, 2]
    assert first.executed == 3 and second.cached == 3
    assert second.outputs == {"u0": 0, "u1": 2, "u2": 4}


def test_interrupted_stage_resumes_at_the_failed_unit(ledger):
    """A stage of 600 chunks interrupted at 400 resumes at 401."""
    def flaky(payload):
        if payload["i"] == 1:
            raise RuntimeError("upstream timeout")
        return UnitResult(output=payload["i"])

    ledger.run("B1", payloads(20), flaky)
    calls = []

    def recovered(payload):
        calls.append(payload["i"])
        return UnitResult(output=payload["i"])

    result = ledger.run("B1", payloads(20), recovered)
    assert calls == [1]
    assert result.cached == 19


def test_exhausted_retries_land_as_failed_without_stopping_the_stage(ledger):
    def worker(payload):
        if payload["i"] == 0:
            raise RuntimeError("boom")
        return UnitResult(output=payload["i"])

    result = ledger.run("B1", payloads(20), worker)
    assert set(result.failures) == {"u0"}
    assert len(result.outputs) == 19


def test_stage_aborts_above_the_failure_rate(ledger):
    def worker(payload):
        raise RuntimeError("boom")

    with pytest.raises(StageAborted):
        ledger.run("B1", payloads(20), worker)


def test_barrier_blocks_while_units_are_unfinished(ledger):
    def worker(payload):
        raise RuntimeError("boom")

    with pytest.raises(StageAborted):
        ledger.run("B1", payloads(20), worker)
    with pytest.raises(BarrierViolation):
        ledger.barrier("B1")


def test_failed_units_do_not_block_the_next_stage(ledger):
    def worker(payload):
        if payload["i"] == 0:
            raise RuntimeError("boom")
        return UnitResult(output=payload["i"])

    ledger.run("B1", payloads(20), worker)
    ledger.barrier("B1")


def test_editing_a_prompt_invalidates_the_cache():
    first = unit_key("B4", "v1", 0.7, {"chunk": "text"})
    assert first != unit_key("B4", "v2", 0.7, {"chunk": "text"})
    assert first != unit_key("B4", "v1", 0.0, {"chunk": "text"})


def test_token_counts_are_recorded_per_stage(ledger):
    def worker(payload):
        return UnitResult(output="ok", in_tokens=100, out_tokens=25)

    result = ledger.run("B4", payloads(3), worker)
    assert (result.in_tokens, result.out_tokens) == (300, 75)
    assert ledger.stage_report("B4")["in_tokens"] == 300
