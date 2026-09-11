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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Execution
from .store import Store


class StageAborted(RuntimeError):
    """La etapa superó `stage_failure_rate_abort` y se cortó.

    **Lleva los errores que la causaron.** Sin ellos el mensaje dice «100% de 1 unidad» y no qué
    pasó, que es lo único que permite arreglarlo: la tasa es la consecuencia, no la causa. Los
    errores están en `work_units.error`, pero pedirle a alguien que abra el almacén para leer
    por qué se cortó lo que acaba de correr es esconder la respuesta donde nadie la busca.
    """

    def __init__(
        self, stage: str, *, total: int, threshold: float, failures: dict[str, str]
    ) -> None:
        self.stage = stage
        self.total = total
        self.threshold = threshold
        self.failures = dict(failures)
        self.rate = len(self.failures) / total if total else 0.0
        distinct = list(dict.fromkeys(self.failures.values()))
        detail = "; ".join(distinct[:3])
        if len(distinct) > 3:
            detail += f"; y {len(distinct) - 3} distinto(s) más"
        super().__init__(
            f"etapa {stage}: {len(self.failures)} de {total} unidades fallaron "
            f"({self.rate:.0%}), por encima del {threshold:.0%} que aborta"
            + (f". Lo que dijeron: {detail}" if detail else "")
        )


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
    """Caché, checkpoint y telemetría. El caché se comparte entre sesiones; el resto no.

    **La clave es content-addressed** —cubre etapa, versión del prompt, modelo, reasoning effort,
    temperatura y hash del input— así que el resultado de una sesión sirve para otra y nadie
    paga dos veces por la misma pregunta. Eso es lo caro y es lo que se comparte.

    **La contabilidad es por sesión, y tiene que serlo.** El barrier pregunta si quedan unidades
    corriendo antes de empezar la etapa siguiente; si mirara la tabla entera, una sesión con
    trabajo en vuelo frenaría a las demás, y una sesión interrumpida las frenaría **para
    siempre**, porque deja sus unidades en `running`. Por eso hay una fila por (sesión, clave):
    el resultado se lee de cualquiera, el estado sólo del propio.
    """

    def __init__(
        self, conn: Store, execution: Execution, *, session_id: str, sleep=time.sleep
    ) -> None:
        self.conn = conn
        self.execution = execution
        self.session_id = session_id
        self._sleep = sleep

    def barrier(self, stage: str, iteration: int | None = None) -> None:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM work_units "
            "WHERE session_id = ? AND stage = ? "
            "AND COALESCE(iteration, -1) = COALESCE(?, -1) "
            "AND status IN ('pending', 'running')",
            (self.session_id, stage, iteration),
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
        shared_cache: bool = True,
    ) -> StageResult:
        """Run `worker` over (label, payload) pairs. Labels index the returned outputs.

        **`shared_cache=False` es para las etapas que persisten por su cuenta.** En un acierto
        de caché el worker **no corre**: sólo se rellena `result.outputs`. Para las etapas que
        llaman al modelo eso está bien, porque el llamador re-persiste desde ahí y una sesión
        nueva escribe sus propias filas reusando la respuesta. Pero `ingest` parsea *y* guarda
        adentro del worker, y lo que queda en el ledger es un resumen: con el caché compartido,
        la segunda sesión reportaba «2 cached» y escribía **cero bloques**. Medido, no supuesto.
        """
        planned = self._plan(stage, iteration, payloads, prompt_version, settings)
        result = StageResult(stage=stage)

        for label, payload, key in planned:
            # La búsqueda en el caché es **por clave sola**, sin sesión: ahí está el ahorro. Que
            # la respuesta la haya pagado otra sesión no la vuelve otra respuesta — la clave
            # cubre todo lo que la determina.
            row = self.conn.execute(
                "SELECT output FROM work_units WHERE key = ? AND status = 'done' LIMIT 1",
                (key,),
            ).fetchone() if shared_cache else self.conn.execute(
                "SELECT output FROM work_units "
                "WHERE session_id = ? AND key = ? AND status = 'done'",
                (self.session_id, key),
            ).fetchone()
            if row is not None:
                result.outputs[label] = json.loads(row["output"])
                result.cached += 1
                self._settle(key, row["output"])
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
                "INSERT INTO work_units (key, session_id, stage, iteration, status, "
                "input_hash, created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?) "
                "ON CONFLICT(session_id, key) DO NOTHING",
                (key, self.session_id, stage, iteration, input_hash(payload), _now()),
            )
            planned.append((label, payload, key))
        self.conn.commit()
        return planned

    def _settle(self, key: str, output: str) -> None:
        """Marcar la unidad propia como hecha con el resultado que pagó otra sesión.

        Sin esto, la sesión que reusa el caché deja su fila en `pending` para siempre y su
        propio barrier la frena en la etapa siguiente.
        """
        self.conn.execute(
            "UPDATE work_units SET status = 'done', output = ?, completed_at = ? "
            "WHERE session_id = ? AND key = ? AND status != 'done'",
            (output, _now(), self.session_id, key),
        )
        self.conn.commit()

    def _execute(
        self,
        stage: str,
        key: str,
        label: str,
        payload: Any,
        worker: Callable[[Any], UnitResult],
        result: StageResult,
    ) -> None:
        self.conn.execute(
            "UPDATE work_units SET status = 'running' WHERE session_id = ? AND key = ?",
            (self.session_id, key),
        )
        self.conn.commit()

        last_error = ""
        for attempt in range(1, self.execution.max_retries + 1):
            try:
                unit = worker(payload)
            except Exception as exc:  # noqa: BLE001 - the ledger records every failure mode
                last_error = f"{type(exc).__name__}: {exc}"
                self.conn.execute(
                    "UPDATE work_units SET attempts = ?, error = ? "
                    "WHERE session_id = ? AND key = ?",
                    (attempt, last_error, self.session_id, key),
                )
                self.conn.commit()
                if attempt < self.execution.max_retries:
                    self._sleep(self.execution.backoff_base_s * (2 ** (attempt - 1)))
                continue

            self.conn.execute(
                "UPDATE work_units SET status = 'done', output = ?, attempts = ?, error = NULL, "
                "in_tokens = ?, out_tokens = ?, completed_at = ? "
                "WHERE session_id = ? AND key = ?",
                (
                    json.dumps(unit.output, ensure_ascii=False, default=str),
                    attempt,
                    unit.in_tokens,
                    unit.out_tokens,
                    _now(),
                    self.session_id,
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
            "UPDATE work_units SET status = 'failed', completed_at = ? "
            "WHERE session_id = ? AND key = ?",
            (_now(), self.session_id, key),
        )
        self.conn.commit()
        result.failures[label] = last_error

    def _check_failure_rate(self, stage: str, result: StageResult, total: int) -> None:
        if not result.failures:
            return
        rate = len(result.failures) / total
        if rate > self.execution.stage_failure_rate_abort:
            raise StageAborted(
                stage, total=total, threshold=self.execution.stage_failure_rate_abort,
                failures=result.failures,
            )

    def stage_report(self, stage: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) AS units, "
            "SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done, "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(COALESCE(in_tokens, 0)) AS in_tokens, "
            "SUM(COALESCE(out_tokens, 0)) AS out_tokens "
            "FROM work_units WHERE session_id = ? AND stage = ?",
            (self.session_id, stage),
        ).fetchone()
        return dict(row)
