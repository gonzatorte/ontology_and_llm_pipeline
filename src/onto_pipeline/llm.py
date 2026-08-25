"""The LLM port.

Governing principle of every LLM use in the pipeline (spec 6.1):

    The LLM classifies and names. The code builds the logic. The reasoner rejects.

The model is never asked for OWL. It answers atomic judgements and the code assembles the
axiom, so a model that only answers that cannot confuse subsumption with instantiation — it
never writes the axiom.

No provider is wired yet. Every stage goes through `run`, so connecting one is a matter of
implementing `ChatModel`; the caching, telemetry and prompt versioning are already in place.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Llm, StageModel
from .telemetry import Ledger, StageResult, UnitResult


class ProviderNotConfigured(RuntimeError):
    """Raised when a stage needs generation and `llm.provider` is `none`."""


@dataclass
class Completion:
    text: str
    in_tokens: int | None = None
    out_tokens: int | None = None


class ChatModel(Protocol):
    def complete(self, prompt: str, *, temperature: float, tier: str) -> Completion: ...


@dataclass
class Prompt:
    """Versioned: the version is part of the cache key, so editing a prompt invalidates that
    stage's cached output instead of silently reusing it (spec 8.3)."""

    stage: str
    version: str
    template: str

    def render(self, **values: Any) -> str:
        return self.template.format(**values)


class NoProvider:
    def complete(self, prompt: str, *, temperature: float, tier: str) -> Completion:
        raise ProviderNotConfigured(
            "llm.provider is 'none'; set it in the config to run generation stages"
        )


class ScriptedModel:
    """Test double. Returns responses in order and records the prompts it was given."""

    def __init__(self, responses: Sequence[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, temperature: float, tier: str) -> Completion:
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("ScriptedModel ran out of responses")
        return Completion(text=self.responses.pop(0), in_tokens=len(prompt), out_tokens=32)


def build(config: Llm) -> ChatModel:
    if config.provider == "none":
        return NoProvider()
    from .providers import build as build_provider

    return build_provider(config)


def settings(config: Llm, stage: str) -> StageModel:
    stage_config = getattr(config, stage, None)
    if not isinstance(stage_config, StageModel):
        raise KeyError(f"no model settings for stage {stage!r}")
    return stage_config


def run(
    ledger: Ledger,
    model: ChatModel,
    prompt: Prompt,
    stage_model: StageModel,
    payloads: Sequence[tuple[str, dict[str, Any]]],
    parse: Callable[[str, dict[str, Any]], Any],
    *,
    iteration: int | None = None,
) -> StageResult:
    """Run one LLM stage: render, call, parse, with the ledger caching and metering it."""

    def worker(payload: dict[str, Any]) -> UnitResult:
        completion = model.complete(
            prompt.render(**payload),
            temperature=stage_model.temperature,
            tier=stage_model.tier,
        )
        return UnitResult(
            output=parse(completion.text, payload),
            in_tokens=completion.in_tokens,
            out_tokens=completion.out_tokens,
        )

    return ledger.run(
        prompt.stage,
        payloads,
        worker,
        iteration=iteration,
        prompt_version=prompt.version,
        temperature=stage_model.temperature,
    )
