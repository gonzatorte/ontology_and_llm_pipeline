"""Local sentence-transformer models behind B2's encoder ports (spec 6.2).

Multilingual by requirement, not preference: the corpus and the glosses are bilingual, and a
monolingual encoder would push every es/en pair into the grey zone on language alone.

Local rather than a hosted embedding service for two reasons: this is the highest-volume stage
of the pipeline (every mention against every candidate class, every iteration), and the
matcher is the one component the design ever fine-tunes — LoRA over the cross-encoder from the
user's own accept/reject decisions (spec 6.3), which needs the weights on this machine.

Imports are deferred so the rest of the pipeline runs without the `matching` extra installed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache

BI_ENCODER = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CROSS_ENCODER = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"


class EncoderUnavailable(RuntimeError):
    """sentence-transformers is not installed: `uv sync --extra matching`."""


@lru_cache(maxsize=4)
def _load_bi_encoder(model_name: str, device: str | None):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - the extra is optional
        raise EncoderUnavailable(str(exc)) from exc
    return SentenceTransformer(model_name, device=device)


@lru_cache(maxsize=4)
def _load_cross_encoder(model_name: str, device: str | None):
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:  # pragma: no cover - the extra is optional
        raise EncoderUnavailable(str(exc)) from exc
    return CrossEncoder(model_name, device=device)


class SentenceTransformerEncoder:
    """Bi-encoder for retrieval."""

    def __init__(self, model_name: str = BI_ENCODER, device: str | None = None) -> None:
        self.model = _load_bi_encoder(model_name, device)

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self.model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in vector] for vector in vectors]


class CrossEncoderReranker:
    """Cross-encoder for re-ranking, squashed into [0, 1].

    The model emits a relevance logit, while the zone thresholds (auto_merge 0.92, grey 0.70)
    are probabilities. Feeding raw logits to them would compare two different scales.
    """

    def __init__(self, model_name: str = CROSS_ENCODER, device: str | None = None) -> None:
        self.model = _load_cross_encoder(model_name, device)

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        raw = self.model.predict(list(pairs), show_progress_bar=False)
        return [_sigmoid(float(value)) for value in raw]


def _sigmoid(value: float) -> float:
    if 0.0 <= value <= 1.0:
        return value  # some checkpoints already emit a probability
    return 1.0 / (1.0 + math.exp(-value))
