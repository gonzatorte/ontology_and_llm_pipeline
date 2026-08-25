"""B2 — matching and entity resolution (spec 6.2).

The quality bottleneck of the pipeline. If the matcher types badly, entities that belonged to
the seed fall into orphans and induce spurious classes in B3, so this is where the evaluation
effort goes and why the false-orphan rate is measured apart from any aggregate F1.

Two operations that must not be conflated:

    typing            mention -> seed class. Bi-encoder retrieval, cross-encoder re-ranking,
                      over *glosses* — the matcher compares a mention's text against the
                      definition, not against the name.
    entity resolution mention <-> mention. Separate individuals until confirmed (D10), which
                      is the default, not the whole policy.

"Separate until confirmed" is completed by four components: blocking, three zones rather than
two, escalation by mention type, and declared keys overriding similarity.

Three zones, not two, because asking about everything that is not obviously identical is the
mass manual review the design exists to avoid. The low zone is discarded without asking.

The conservative policy leaves unresolved duplicates behind. That contaminates the support
count for functional properties — two duplicates with one value each look like confirmation of
functionality when they are one entity with two values, which is a hidden conflict — so those
individuals are marked `possible_duplicate_unresolved` and excluded from that count (6.8).

Glosses are bootstrapped in A0.4 and enriched in B4b, and this stage is re-run over orphans
when they change: better gloss, better matching, fewer false orphans. A mention orphaned at
iteration 3 can be typed correctly at 8.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

AUTO = "auto"
GREY = "grey"
DISCARDED = "discarded"

MERGE = "merge"
ASK = "ask"
SEPARATE = "separate"

_GENERIC_RE = re.compile(r"^(the|this|that|el|la|los|las|un|una|est[ae])\b", re.IGNORECASE)


class Encoder(Protocol):
    """Bi-encoder for retrieval. Vectors are expected already normalized."""

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """Cross-encoder for re-ranking. Scores are expected in [0, 1]."""

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]: ...


@dataclass
class Target:
    """A seed class as the matcher sees it: the gloss when there is one, the label until then."""

    iri: str
    label: str
    gloss: str | None = None
    alt_labels: list[str] = field(default_factory=list)
    has_key: list[str] = field(default_factory=list)

    match_against: str = "label"

    @property
    def text(self) -> str:
        """What a mention is compared against.

        The spec matches against the gloss, on the reasoning that a definition captures the
        concept where a name may not. Measured on this seed it does the opposite: over ten
        unambiguous mention/class pairs the bi-encoder scored 7/10 recall@1 against labels and
        2/10 against glosses, with two independently generated sets of glosses. A mention is a
        short noun phrase and so is a label; a gloss is a long sentence, and a symmetric
        paraphrase encoder loses on that mismatch more than the added meaning wins.

        So the default is the label, and the premise becomes worth revisiting once the
        cross-encoder is tuned (spec 6.3) or an asymmetric retrieval model is in place.
        """
        if self.match_against == "gloss" and self.gloss:
            return self.gloss
        if self.match_against == "label_and_gloss" and self.gloss:
            return f"{self.label}: {self.gloss}"
        return self.label

    @property
    def grounded_in_gloss(self) -> bool:
        return bool(self.gloss)


@dataclass
class Mention:
    id: str
    text: str
    document_id: str
    language: str = "en"
    key_values: dict[str, str] = field(default_factory=dict)


@dataclass
class Typing:
    mention_id: str
    iri: str | None
    score: float
    zone: str
    runner_up: str | None = None


@dataclass
class Decision:
    left: str
    right: str
    action: str  # merge | ask | separate
    reason: str
    score: float = 0.0


def dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class Matcher:
    def __init__(
        self,
        encoder: Encoder,
        reranker: Reranker | None = None,
        *,
        auto_merge_threshold: float = 0.92,
        grey_zone_lower: float = 0.70,
        cross_language_always_grey: bool = True,
        respect_declared_haskey: bool = True,
    ) -> None:
        self.encoder = encoder
        self.reranker = reranker
        self.auto_merge_threshold = auto_merge_threshold
        self.grey_zone_lower = grey_zone_lower
        self.cross_language_always_grey = cross_language_always_grey
        self.respect_declared_haskey = respect_declared_haskey

    def type_mentions(
        self, mentions: Sequence[Mention], targets: Sequence[Target], *, top_k: int = 5
    ) -> list[Typing]:
        """Bi-encoder retrieval then cross-encoder re-ranking, over the targets' glosses."""
        if not targets:
            return [Typing(m.id, None, 0.0, DISCARDED) for m in mentions]

        target_vectors = [normalize(v) for v in self.encoder.encode([t.text for t in targets])]
        mention_vectors = [normalize(v) for v in self.encoder.encode([m.text for m in mentions])]

        typings = []
        for mention, vector in zip(mentions, mention_vectors, strict=True):
            ranked = sorted(
                ((dot(vector, tv), target) for tv, target in
                 zip(target_vectors, targets, strict=True)),
                key=lambda pair: (-pair[0], pair[1].iri),
            )[:top_k]
            typings.append(self._resolve_typing(mention, ranked))
        return typings

    def _resolve_typing(self, mention: Mention, ranked: list[tuple[float, Target]]) -> Typing:
        """Whichever stage produced the final ranking also supplies the score the zones are
        read from — mixing a cosine from one model with a threshold meant for another is how
        the zones stop meaning anything.

        Which is why an untuned cross-encoder must not be switched on. Measured on the real
        seed, a generic IR re-ranker (mmarco) ranked `semi-structured interview` against the
        seed's classes correctly but squashed every score to ~0.01, dropping a match the
        bi-encoder had put at 0.73 into the discard zone. That is R1 — a false orphan —
        manufactured by the matcher itself. A cross-encoder earns its place here only once
        it is tuned on accumulated accept/reject labels (spec 6.3).
        """
        if self.reranker is not None:
            scores = self.reranker.score([(mention.text, target.text) for _, target in ranked])
            ranked = sorted(
                zip(scores, (target for _, target in ranked), strict=True),
                key=lambda pair: (-pair[0], pair[1].iri),
            )
        best_score, best = ranked[0]
        runner_up = ranked[1][1].iri if len(ranked) > 1 else None

        if best_score >= self.auto_merge_threshold:
            zone = AUTO
        elif best_score >= self.grey_zone_lower:
            zone = GREY
        else:
            return Typing(mention.id, None, best_score, DISCARDED, runner_up)
        return Typing(mention.id, best.iri, best_score, zone, runner_up)


    def resolve(
        self,
        mentions: Sequence[Mention],
        *,
        synonyms: dict[str, set[str]] | None = None,
        keys: dict[str, list[str]] | None = None,
        inferred_class: dict[str, str] | None = None,
    ) -> list[Decision]:
        """Cross-document entity resolution. Intra-document anaphora is B1b's job and never
        reaches here."""
        synonyms = synonyms or {}
        keys = keys or {}
        inferred_class = inferred_class or {}
        decisions: list[Decision] = []

        seen: set[tuple[str, str]] = set()
        for block in self.blocks(mentions):
            for index, left in enumerate(block):
                for right in block[index + 1:]:
                    if left.document_id == right.document_id:
                        continue
                    pair = tuple(sorted((left.id, right.id)))
                    if pair in seen:
                        continue  # blocks overlap when a mention carries key values
                    seen.add(pair)
                    decisions.append(
                        self._decide(left, right, synonyms, keys, inferred_class)
                    )
        return decisions

    def blocks(self, mentions: Sequence[Mention]) -> list[list[Mention]]:
        """Thousands of mentions make exhaustive comparison unworkable; only pairs inside a
        block are compared."""
        grouped: dict[str, list[Mention]] = {}
        for mention in mentions:
            for key in self._blocking_keys(mention):
                grouped.setdefault(key, []).append(mention)
        return [block for block in grouped.values() if len(block) > 1]

    @staticmethod
    def _blocking_keys(mention: Mention) -> list[str]:
        """Surface form, plus one key per declared key value.

        Blocking on surface form alone would let it silently overrule a declared key: two
        mentions of one entity written differently never land in the same block, so the key
        that says they are identical never gets asked. A declared key outranks similarity,
        which means it has to outrank the blocking too.
        """
        tokens = sorted(re.findall(r"[^\W\d_]+", mention.text.lower()))
        keys = [tokens[0][:4] if tokens else ""]
        keys.extend(f"key:{prop}={value}" for prop, value in sorted(mention.key_values.items()))
        return keys

    def _decide(
        self,
        left: Mention,
        right: Mention,
        synonyms: dict[str, set[str]],
        keys: dict[str, list[str]],
        inferred_class: dict[str, str],
    ) -> Decision:
        # A declared key outranks every similarity threshold: it is the ontology stating what
        # identity means for that class.
        if self.respect_declared_haskey:
            verdict = self._key_verdict(left, right, keys, inferred_class)
            if verdict is not None:
                return verdict

        if self.cross_language_always_grey and left.language != right.language:
            return Decision(left.id, right.id, ASK, "cross_language")

        normalized_left, normalized_right = _normalized(left.text), _normalized(right.text)
        if normalized_left in synonyms.get(normalized_right, set()) or (
            normalized_right in synonyms.get(normalized_left, set())
        ):
            return Decision(left.id, right.id, MERGE, "synonym_declared_in_seed", 1.0)

        if _is_generic(left.text) or _is_generic(right.text):
            # A generic phrase says nothing about identity across documents, and asking about
            # it is the mass review the design avoids.
            return Decision(left.id, right.id, SEPARATE, "generic_phrase_cross_document")

        if normalized_left == normalized_right and _is_proper_name(left.text):
            same_class = inferred_class.get(left.id) == inferred_class.get(right.id)
            if same_class and inferred_class.get(left.id) is not None:
                return Decision(left.id, right.id, MERGE, "identical_proper_name", 1.0)
            return Decision(left.id, right.id, ASK, "identical_name_different_class")

        score = self._similarity(left.text, right.text)
        if score >= self.auto_merge_threshold:
            return Decision(left.id, right.id, MERGE, "high_similarity", score)
        if score >= self.grey_zone_lower:
            return Decision(left.id, right.id, ASK, "grey_zone", score)
        return Decision(left.id, right.id, SEPARATE, "low_similarity", score)

    def _key_verdict(
        self,
        left: Mention,
        right: Mention,
        keys: dict[str, list[str]],
        inferred_class: dict[str, str],
    ) -> Decision | None:
        class_iri = inferred_class.get(left.id)
        if class_iri is None or class_iri != inferred_class.get(right.id):
            return None
        key_properties = keys.get(class_iri)
        if not key_properties:
            return None
        if not all(prop in left.key_values and prop in right.key_values
                   for prop in key_properties):
            return None
        if all(left.key_values[prop] == right.key_values[prop] for prop in key_properties):
            return Decision(left.id, right.id, MERGE, "declared_key_matches", 1.0)
        return Decision(left.id, right.id, SEPARATE, "declared_key_differs")

    def _similarity(self, left: str, right: str) -> float:
        if self.reranker is not None:
            return self.reranker.score([(left, right)])[0]
        vectors = [normalize(v) for v in self.encoder.encode([left, right])]
        return dot(vectors[0], vectors[1])


def unresolved_duplicates(decisions: Sequence[Decision]) -> set[str]:
    """Mentions left in the grey zone. They carry `possible_duplicate_unresolved` in the
    mention layer and are excluded from functional-property support counts (spec 6.8)."""
    pending: set[str] = set()
    for decision in decisions:
        if decision.action == ASK:
            pending.update((decision.left, decision.right))
    return pending


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"[^\W\d_]+", text.lower()))


def _is_generic(text: str) -> bool:
    return bool(_GENERIC_RE.match(text.strip())) or len(_normalized(text).split()) == 1 and (
        text.strip().islower()
    )


def _is_proper_name(text: str) -> bool:
    words = [word for word in text.split() if word]
    return bool(words) and all(word[0].isupper() or not word[0].isalpha() for word in words)
