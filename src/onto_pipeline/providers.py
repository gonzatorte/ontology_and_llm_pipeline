"""Concrete `ChatModel` backends (CONFIG, API-LLM-ALLOWED).

One client covers every backend in play, because they all speak the OpenAI chat-completions
shape: OpenCode Zen/Go at https://opencode.ai/zen/go/v1, and Ollama at
http://127.0.0.1:11434/v1. That keeps the per-stage temperature (TEMPERATURE-PER-STAGE) and the
token accounting
the ledger needs (T4) in one place.

Credentials are read from the environment and never from the config file, never logged, and
never included in an error message: a failing request reports status and body, and the body of
an auth error does not contain the key.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import httpx

from .config import Llm
from .llm import ChatModel, Completion, ProviderNotConfigured

USER_AGENT = "onto-pipeline/0.1 (ontology enrichment; batch, not a coding agent)"


@dataclass
class Endpoint:
    base_url: str
    api_key_env: str | None
    models: dict[str, str]


class OpenAICompatibleModel:
    """Chat completions over any endpoint that speaks the OpenAI shape."""

    def __init__(
        self, endpoint: Endpoint, *, timeout_s: float = 120.0, session: str | None = None
    ) -> None:
        self.endpoint = endpoint
        self.session = session
        self._client = httpx.Client(timeout=timeout_s)
        self._api_key = self._read_key()

    def _read_key(self) -> str | None:
        if self.endpoint.api_key_env is None:
            return None  # a local endpoint needs no credential
        key = os.environ.get(self.endpoint.api_key_env)
        if not key:
            raise ProviderNotConfigured(
                f"{self.endpoint.api_key_env} is not set in the environment. "
                "Put it in an env file and pass --env-file; never in the config, which is "
                "committed."
            )
        return key

    def model_for(self, tier: str) -> str:
        try:
            return self.endpoint.models[tier]
        except KeyError as exc:
            raise ProviderNotConfigured(
                f"no model configured for tier {tier!r}; set llm.models.{tier}"
            ) from exc

    def complete(
        self, prompt: str, *, temperature: float, tier: str, session: str | None = None,
        reasoning_effort: str | None = None, max_tokens: int | None = None,
    ) -> Completion:
        headers = {
            "Content-Type": "application/json",
            # Identify honestly. Some gateways require the client to name itself rather than
            # present a generic SDK string, and naming this pipeline something it is not would
            # be misrepresenting the traffic to the provider.
            "User-Agent": USER_AGENT,
        }
        session = session or self.session
        if session:
            headers["x-opencode-session"] = session
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        response = self._client.post(
            f"{self.endpoint.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "model": self.model_for(tier),
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "stream": False,
                **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
                **({"max_tokens": max_tokens} if max_tokens else {}),
            },
        )
        if response.status_code >= 400:
            # The body is echoed for diagnosis; the key lives only in the request header.
            raise RuntimeError(
                f"{self.endpoint.base_url} returned {response.status_code}: "
                f"{response.text[:300]}"
            )

        payload = response.json()
        usage = payload.get("usage") or {}
        return Completion(
            text=payload["choices"][0]["message"]["content"],
            in_tokens=usage.get("prompt_tokens"),
            out_tokens=usage.get("completion_tokens"),
            cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
            reasoning_tokens=(usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
        )


def build(config: Llm, *, timeout_s: float = 300.0) -> ChatModel:
    """Replaces `llm.build`'s placeholder once a provider is configured."""
    from .llm import NoProvider

    if config.provider == "none":
        return NoProvider()
    return OpenAICompatibleModel(
        Endpoint(
            base_url=config.base_url,
            api_key_env=config.api_key_env or None,
            models=dict(config.models),
        ),
        timeout_s=timeout_s,
        # Some gateways ask for a stable session id per conversation so they can route and
        # cache prompts; one per process is the honest granularity for a batch run.
        session=f"onto-pipeline-{uuid.uuid4().hex[:16]}",
    )


def load_env_file(path) -> list[str]:
    """Env files are explicit, never auto-discovered (the convention this workspace already
    uses). Returns the names of the variables set, never their values."""
    from pathlib import Path

    names = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip("'\"")
        os.environ.setdefault(name, value)
        names.append(name)
    return names
