"""Work-unit ledger: cache, checkpoint and cost telemetry are one mechanism (SCHEMAS-WORK-UNITS).

Execution protocol, verbatim from the spec:
1. a stage enumerates all its units before emitting anything, marked `pending`;
2. only units that are not `done` run — resuming is re-running the stage;
3. granularity is the individual call;
4. failures retry with backoff up to `max_retries`, then land as `failed` and the stage goes on;
   the stage aborts only if the failure rate exceeds `stage_failure_rate_abort`;
5. a stage does not start while the previous one has `pending` or `running` units;
6. deterministic non-LLM stages are recomputed whole — only what costs money or real time is kept.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Execution


class StageAborted(RuntimeError):
    """Raised when a stage's failure rate exceeds `stage_failure_rate_abort`."""


class BarrierViolation(RuntimeError):
    """Raised when a stage starts while an earlier one still has unfinished units."""


@dataclass
class UnitResult:
    """What a unit's worker returns. Token counts are None for non-LLM stages."""

    output: Any
    in_tokens: int | None = None
    out_tokens: int | None = None


@dataclass
class StageResult:
    stage: str
    outputs: dict[str, Any] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    cached: int = 0
    executed: int = 0
    in_tokens: int = 0
    out_tokens: int = 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def unit_key(stage: str, prompt_version: str, settings: Any, payload: Any) -> str:
    """Cache key.

    `settings` is everything about *how* the unit was produced that changes its output: the
    prompt version, the temperature, and — the part that is easy to forget — the model and its
    reasoning effort. Leaving the model out means switching tiers silently serves results from
    a model the configuration no longer names, which is worse than paying to recompute.
    """
    material = json.dumps(
        {
            "stage": stage,
            "prompt_version": prompt_version,
            "settings": settings,
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def input_hash(payload: Any) -> str:
    material = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, conn: sqlite3.Connection, execution: Execution, sleep=time.sleep) -> None:
        self.conn = conn
        self.execution = execution
        self._sleep = sleep

    def barrier(self, stage: str, iteration: int | None = None) -> None:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM work_units "
            "WHERE stage = ? AND IFNULL(iteration, -1) = IFNULL(?, -1) "
            "AND status IN ('pending', 'running')",
            (stage, iteration),
        ).fetchone()
        if row["n"]:
            raise BarrierViolation(f"stage {stage} still has {row['n']} unfinished units")

    def run(
        self,
        stage: str,
        payloads: Sequence[tuple[str, Any]],
        worker: Callable[[Any], UnitResult],
        *,
        iteration: int | None = None,
        prompt_version: str = "",
        settings: Any = None,
    ) -> StageResult:
        """Run `worker` over (label, payload) pairs. Labels index the returned outputs."""
        planned = self._plan(stage, iteration, payloads, prompt_version, settings)
        result = StageResult(stage=stage)

        for label, payload, key in planned:
            row = self.conn.execute(
                "SELECT status, output FROM work_units WHERE key = ?", (key,)
            ).fetchone()
            if row["status"] == "done":
                result.outputs[label] = json.loads(row["output"])
                result.cached += 1
                continue
            self._execute(stage, key, label, payload, worker, result)
            self._check_failure_rate(stage, result, len(planned))

        return result

    def _plan(
        self,
        stage: str,
        iteration: int | None,
        payloads: Sequence[tuple[str, Any]],
        prompt_version: str,
        settings: Any,
    ) -> list[tuple[str, Any, str]]:
        planned = []
        for label, payload in payloads:
            key = unit_key(stage, prompt_version, settings, payload)
            self.conn.execute(
                "INSERT INTO work_units (key, stage, iteration, status, input_hash, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?) ON CONFLICT(key) DO NOTHING",
                (key, stage, iteration, input_hash(payload), _now()),
            )
            planned.append((label, payload, key))
        self.conn.commit()
        return planned

    def _execute(
        self,
        stage: str,
        key: str,
        label: str,
        payload: Any,
        worker: Callable[[Any], UnitResult],
        result: StageResult,
    ) -> None:
        self.conn.execute("UPDATE work_units SET status = 'running' WHERE key = ?", (key,))
        self.conn.commit()

        last_error = ""
        for attempt in range(1, self.execution.max_retries + 1):
            try:
                unit = worker(payload)
            except Exception as exc:  # noqa: BLE001 - the ledger records every failure mode
                last_error = f"{type(exc).__name__}: {exc}"
                self.conn.execute(
                    "UPDATE work_units SET attempts = ?, error = ? WHERE key = ?",
                    (attempt, last_error, key),
                )
                self.conn.commit()
                if attempt < self.execution.max_retries:
                    self._sleep(self.execution.backoff_base_s * (2 ** (attempt - 1)))
                continue

            self.conn.execute(
                "UPDATE work_units SET status = 'done', output = ?, attempts = ?, error = NULL, "
                "in_tokens = ?, out_tokens = ?, completed_at = ? WHERE key = ?",
                (
                    json.dumps(unit.output, ensure_ascii=False, default=str),
                    attempt,
                    unit.in_tokens,
                    unit.out_tokens,
                    _now(),
                    key,
                ),
            )
            self.conn.commit()
            result.outputs[label] = unit.output
            result.executed += 1
            result.in_tokens += unit.in_tokens or 0
            result.out_tokens += unit.out_tokens or 0
            return

        self.conn.execute(
            "UPDATE work_units SET status = 'failed', completed_at = ? WHERE key = ?",
            (_now(), key),
        )
        self.conn.commit()
        result.failures[label] = last_error

    def _check_failure_rate(self, stage: str, result: StageResult, total: int) -> None:
        if not result.failures:
            return
        rate = len(result.failures) / total
        if rate > self.execution.stage_failure_rate_abort:
            raise StageAborted(
                f"stage {stage}: failure rate {rate:.0%} over {total} units "
                f"exceeds {self.execution.stage_failure_rate_abort:.0%}"
            )

    def stage_report(self, stage: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS units, "
            "SUM(status = 'done') AS done, SUM(status = 'failed') AS failed, "
            "SUM(IFNULL(in_tokens, 0)) AS in_tokens, SUM(IFNULL(out_tokens, 0)) AS out_tokens "
            "FROM work_units WHERE stage = ?",
            (stage,),
        ).fetchone()
        return dict(row)
