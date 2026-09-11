"""ITER-MATCH — matching and entity resolution.

The quality bottleneck of the pipeline. If the matcher types badly, entities that belonged to
the seed fall into orphans and induce spurious classes in ITER-INDUCE, so this is where the
evaluation
effort goes and why the false-orphan rate is measured apart from any aggregate F1.

Two operations that must not be conflated:

    typing            mention -> seed class. Bi-encoder retrieval, cross-encoder re-ranking,
                      over *glosses* — the matcher compares a mention's text against the
                      definition, not against the name.
    entity resolution mention <-> mention. Separate individuals until confirmed
    (SEPARATE-UNTIL-CONFIRMED), which
                      is the default, not the whole policy.

"Separate until confirmed" is completed by four components: blocking, three zones rather than
two, escalation by mention type, and declared keys overriding similarity.

Three zones, not two, because asking about everything that is not obviously identical is the
mass manual review the design exists to avoid. The low zone is discarded without asking.

The conservative policy leaves unresolved duplicates behind. That contaminates the support
count for functional properties — two duplicates with one value each look like confirmation of
functionality when they are one entity with two values, which is a hidden conflict — so those
individuals are marked `possible_duplicate_unresolved` and excluded from that count (6.8).

Glosses are bootstrapped in PREP-NORMALIZE-GLOSSES and enriched in ITER-AXIOMATIZE-ENRICH, and this
stage is re-run over orphans
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

SURFACE_AND_KEYS = "surface_and_keys"
EMBEDDING = "embedding"

# The similarity matrix is materialized in row chunks, so peak memory is chunk x n floats
# rather than n squared.
_CHUNK = 512


class NumpyUnavailable(RuntimeError):
    """`blocking_strategy: embedding` needs numpy: `uv sync --extra matching`."""

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
        concept where a name may not. Over ten hand-built unambiguous pairs the bi-encoder
        scored 7/10 recall@1 against labels and 2/10 against glosses, with two independently
        generated sets of glosses, which is why the default sits on the label.

        That evidence is thin and one-sided, and saying so here matters more than the number:
        n=10, recall only, on a corpus and seed that turned out to be thematically mismatched.
        It does not measure precision, and labels lose there — "subject" in the sense of *topic*
        types as the class Subject at 0.992, inside the auto-merge zone, by lexical echo that no
        threshold can filter. Whichever way this setting ends up, it needs an annotated corpus
        to decide, not another handful of pairs.
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
    # La oración en la que apareció. Vacía cuando no se pidió contexto, y entonces todo se
    # comporta como antes.
    context: str = ""


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


NO_CONTEXT = "none"
SENTENCE = "sentence"
CONTEXT_MODES = frozenset({NO_CONTEXT, SENTENCE})


def mention_text(mention: Mention, mode: str = NO_CONTEXT) -> str:
    """Qué texto representa a la mención cuando se la compara contra una clase.

    Por defecto, el sintagma pelado, que es lo que el pipeline hizo siempre. Con `sentence` se
    le agrega la oración donde apareció, **detrás y separada**, no fundida: el sintagma tiene
    que seguir dominando la forma del texto. Es la lección de la medición de glosas — una
    mención es un sintagma corto y una etiqueta también, y un encoder simétrico pierde más por
    la diferencia de forma que lo que gana en significado—, así que el contexto entra como
    complemento y no como reemplazo.

    Existe porque sobre MaterioMiner el cuello medido no es el umbral sino la recuperación: la
    clase correcta no está en el top-50 el 37% de las veces, y `lifetime` a secas no tiene cómo
    llegar a `FatigueLifetime`.
    """
    if mode == NO_CONTEXT or not mention.context:
        return mention.text
    return f"{mention.text}. {mention.context}"


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
        blocking_strategy: str = EMBEDDING,
        context_mode: str = NO_CONTEXT,
    ) -> None:
        self.encoder = encoder
        self.reranker = reranker
        self.auto_merge_threshold = auto_merge_threshold
        self.grey_zone_lower = grey_zone_lower
        self.cross_language_always_grey = cross_language_always_grey
        self.respect_declared_haskey = respect_declared_haskey
        self.blocking_strategy = blocking_strategy
        self.context_mode = context_mode
        # Encoding is O(mentions); comparing is O(pairs). Caching the vectors keeps it that
        # way — without it every pair re-encodes both of its texts, which measured 6.9 ms per
        # pair against 0.38 ms per mention encoded once in batch.
        self._vectors: dict[str, list[float]] = {}

    def vectors_for(self, texts: Sequence[str]) -> list[list[float]]:
        """Normalized vectors, encoding in one batch only what is not cached yet."""
        missing = [text for text in dict.fromkeys(texts) if text not in self._vectors]
        if missing:
            encoded = self.encoder.encode(missing)
            for text, vector in zip(missing, encoded, strict=True):
                self._vectors[text] = normalize(vector)
        return [self._vectors[text] for text in texts]

    def type_mentions(
        self, mentions: Sequence[Mention], targets: Sequence[Target], *, top_k: int = 5
    ) -> list[Typing]:
        """Bi-encoder retrieval then cross-encoder re-ranking, over the targets' glosses."""
        if not targets:
            return [Typing(m.id, None, 0.0, DISCARDED) for m in mentions]

        target_vectors = self.vectors_for([t.text for t in targets])
        mention_vectors = self.vectors_for(
            [mention_text(m, self.context_mode) for m in mentions]
        )
        ranked = self._rank(mention_vectors, target_vectors, targets, top_k)
        return [
            self._resolve_typing(mention, candidates)
            for mention, candidates in zip(mentions, ranked, strict=True)
        ]

    def _rank(
        self,
        mention_vectors: Sequence[Sequence[float]],
        target_vectors: Sequence[Sequence[float]],
        targets: Sequence[Target],
        top_k: int,
    ) -> list[list[tuple[float, Target]]]:
        """Top-k targets per mention, by cosine.

        Two paths for one calculation. The Python one is O(mentions x targets) dot products in
        the interpreter, which is fine at the thirty-four classes of the seed and stops being
        fine immediately after: against CRAFT's 3,419-class inventory the same loop is 30
        million dot products of 384 dimensions, hours of work for a number numpy produces in
        seconds. Chunked in rows like `_neighbour_pairs`, so peak memory is chunk x targets.

        Both paths order the candidates identically, ties broken by IRI. The scores are not
        bit-identical: numpy works in float32, like `_neighbour_pairs`, so they differ around
        the seventh decimal. That is far below any threshold the zones are read from, but it
        means a score is reproducible to a display precision, not to the last bit.
        """
        width = min(top_k, len(targets))
        try:
            import numpy as np
        except ImportError:  # pragma: no cover - depends on the install
            return [
                sorted(
                    ((dot(vector, tv), target)
                     for tv, target in zip(target_vectors, targets, strict=True)),
                    key=lambda pair: (-pair[0], pair[1].iri),
                )[:width]
                for vector in mention_vectors
            ]

        mention_matrix = np.asarray(mention_vectors, dtype="float32")
        target_matrix = np.asarray(target_vectors, dtype="float32").T
        ranked: list[list[tuple[float, Target]]] = []
        for start in range(0, len(mention_matrix), _CHUNK):
            block = mention_matrix[start:start + _CHUNK] @ target_matrix
            # argpartition is O(targets) against the O(targets log targets) of a full sort,
            # and only the top-k order matters; the exact order comes from the sort below.
            top = np.argpartition(-block, width - 1, axis=1)[:, :width]
            for row, columns in enumerate(top.tolist()):
                ranked.append(
                    sorted(
                        ((float(block[row, column]), targets[column]) for column in columns),
                        key=lambda pair: (-pair[0], pair[1].iri),
                    )
                )
        return ranked

    def _resolve_typing(self, mention: Mention, ranked: list[tuple[float, Target]]) -> Typing:
        """Whichever stage produced the final ranking also supplies the score the zones are
        read from — mixing a cosine from one model with a threshold meant for another is how
        the zones stop meaning anything.

        Which is why an untuned cross-encoder must not be switched on. Measured on the real
        seed, a generic IR re-ranker (mmarco) ranked `semi-structured interview` against the
        seed's classes correctly but squashed every score to ~0.01, dropping a match the
        bi-encoder had put at 0.73 into the discard zone. That is RISKS-FALSE-ORPHANS — a false
        orphan —
        manufactured by the matcher itself. A cross-encoder earns its place here only once
        it is tuned on accumulated accept/reject labels (ITER-TUNE).
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
        """Cross-document entity resolution. Intra-document anaphora is ITER-COREFER's job and never
        reaches here."""
        synonyms = synonyms or {}
        keys = keys or {}
        inferred_class = inferred_class or {}
        return [
            self._decide(left, right, synonyms, keys, inferred_class)
            for left, right in self.candidate_pairs(mentions, synonyms=synonyms)
        ]

    def candidate_pairs(
        self, mentions: Sequence[Mention], *, synonyms: dict[str, set[str]] | None = None
    ) -> list[tuple[Mention, Mention]]:
        """Which pairs are worth deciding on at all.

        Under `embedding` the candidates come from three sources, unioned, none of which is a
        tuned constant:

            neighbours   cosine at or above `grey_zone_lower`. The same number the zones are
                         read from, so nothing is pruned that would have been decided
                         differently: below it `_decide` returns SEPARATE anyway.
            declared key two mentions carrying the same value of a declared key. The ontology
                         stating what identity means for that class outranks similarity, so it
                         has to outrank candidate generation too.
            declared     two surfaces the ontology declares equivalent. `GT` and `grounded
            synonym      theory` are the same concept by assertion and would not survive a
                         cosine cut.

        The surface strategy it replaces blocked on the first four characters of the
        alphabetically first token — a constant that no experiment can calibrate, that split
        `in-depth interview` from `semi-structured interview`, and that put every mention
        whose first token was a stopword into one bucket.
        """
        synonyms = synonyms or {}
        if self.blocking_strategy == SURFACE_AND_KEYS:
            indexed = self._surface_pairs(mentions)
        else:
            indexed = self._neighbour_pairs(mentions) | self._asserted_pairs(mentions, synonyms)
        return [
            (mentions[left], mentions[right])
            for left, right in sorted(indexed)
            if mentions[left].document_id != mentions[right].document_id
        ]

    def _neighbour_pairs(self, mentions: Sequence[Mention]) -> set[tuple[int, int]]:
        """Every pair at or above the grey-zone floor, from the vectors typing already
        computed. The matrix is built in row chunks: peak memory is chunk x n, not n x n."""
        if len(mentions) < 2:
            return set()
        try:
            import numpy as np
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise NumpyUnavailable(NumpyUnavailable.__doc__) from exc

        vectors = np.asarray(self.vectors_for([m.text for m in mentions]), dtype="float32")
        pairs: set[tuple[int, int]] = set()
        for start in range(0, len(mentions), _CHUNK):
            block = vectors[start:start + _CHUNK] @ vectors.T
            rows, columns = np.nonzero(block >= self.grey_zone_lower)
            for row, column in zip(rows.tolist(), columns.tolist(), strict=True):
                left = start + row
                if left < column:
                    pairs.add((left, column))
        return pairs

    def _asserted_pairs(
        self, mentions: Sequence[Mention], synonyms: dict[str, set[str]]
    ) -> set[tuple[int, int]]:
        """Candidates that hold by identity or by assertion, which no similarity cut may drop.

        Three exact sources, no threshold among them: the same surface form written twice, the
        same value of a declared key, and two surfaces the ontology declares equivalent. The
        first matters more than it looks — `identical_proper_name` and
        `generic_phrase_cross_document` both decide on exact text, and leaving their pairs to
        the encoder would make two rules that need no model depend on one.
        """
        by_key: dict[str, list[int]] = {}
        by_surface: dict[str, list[int]] = {}
        for index, mention in enumerate(mentions):
            by_surface.setdefault(_normalized(mention.text), []).append(index)
            for prop, value in mention.key_values.items():
                by_key.setdefault(f"{prop}={value}", []).append(index)

        pairs: set[tuple[int, int]] = set()
        for group in by_key.values():
            pairs.update(_combinations(group))
        for group in by_surface.values():
            pairs.update(_combinations(group))
        for surface, group in by_surface.items():
            for equivalent in synonyms.get(surface, ()):  # declared, so no threshold applies
                for left in group:
                    for right in by_surface.get(equivalent, ()):
                        if left != right:
                            pairs.add((min(left, right), max(left, right)))
        return pairs

    def _surface_pairs(self, mentions: Sequence[Mention]) -> set[tuple[int, int]]:
        """The heuristic this replaces, kept behind `blocking_strategy: surface_and_keys`."""
        grouped: dict[str, list[int]] = {}
        for index, mention in enumerate(mentions):
            tokens = sorted(re.findall(r"[^\W\d_]+", mention.text.lower()))
            keys = [tokens[0][:4] if tokens else ""]
            keys.extend(f"key:{prop}={value}"
                        for prop, value in sorted(mention.key_values.items()))
            for key in keys:
                grouped.setdefault(key, []).append(index)
        pairs: set[tuple[int, int]] = set()
        for group in grouped.values():
            pairs.update(_combinations(group))
        return pairs

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
            return Decision(left.id, right.id, MERGE, "synonym_declared_in_ontology", 1.0)

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
        vectors = self.vectors_for([left, right])
        return dot(vectors[0], vectors[1])


def _combinations(group: Sequence[int]) -> set[tuple[int, int]]:
    return {(left, right) for position, left in enumerate(group) for right in group[position + 1:]
            if left != right}


def synonym_index(targets: Sequence[Target]) -> dict[str, set[str]]:
    """Surface form to the surfaces the ontology declares equivalent to it.

    A class's label and its `skos:altLabel`s name the same concept, so two mentions carrying
    two of those names are the same entity by assertion, not by similarity — which is why the
    resolver merges them without consulting a threshold.
    """
    index: dict[str, set[str]] = {}
    for target in targets:
        group = {_normalized(name) for name in [target.label, *target.alt_labels] if name}
        for name in group:
            index.setdefault(name, set()).update(group - {name})
    return index


def unresolved_duplicates(decisions: Sequence[Decision]) -> set[str]:
    """Mentions left in the grey zone. They carry `possible_duplicate_unresolved` in the
    mention layer and are excluded from functional-property support counts (ITER-APPLY)."""
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
