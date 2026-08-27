"""El banco: barrer umbrales sobre un caso de uso y reportar qué se separa de qué.

Existe porque los umbrales de `matching:` nunca se habían medido, y no se los puede medir sobre
un par cualquiera: hace falta uno donde la respuesta correcta se conozca para cada mención. Eso
es un caso de uso, y cargarlo es trabajo de `use_cases`.

Las métricas son `annotation.score`, las mismas que lee la compuerta. Calibrar con números
calculados por otro código que el que después decide sería calibrar otra cosa.

**Lo que el barrido mide es un corte, y el pipeline usa dos.** Lo calibrado es el corte de
huérfano; dónde empieza la zona gris es cuánta revisión humana se acepta, y eso no lo contesta
ningún corpus.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import annotation
from .matching import Matcher
from .use_cases import UseCase

# ─────────────────────────────  the bench  ─────────────────────────────


@dataclass
class Ranking:
    """Top-1 per mention with thresholds removed, so a sweep costs one encoding pass.

    The matcher applies the zones inside `type_mentions`, which is right for the pipeline and
    wrong for a sweep: re-encoding 9,000 mentions for each candidate threshold would take
    hours to produce numbers that are a function of scores already computed. Running it wide
    open recovers the score, and the zones are applied here.
    """

    best: dict[str, str]
    score: dict[str, float]
    runner_up: dict[str, str | None]


@dataclass
class Distribution:
    correct: list[float] = field(default_factory=list)
    wrong: list[float] = field(default_factory=list)

    @property
    def separation(self) -> float:
        """Distance between the two medians, in units of the pooled spread.

        The single number that answers "is there a threshold at all". A sweep can only find a
        good operating point if the two distributions are apart; near zero says the ranking,
        not the threshold, is what needs work.
        """
        if len(self.correct) < 2 or len(self.wrong) < 2:
            return 0.0
        spread = statistics.pstdev(self.correct + self.wrong)
        if not spread:
            return 0.0
        return (statistics.median(self.correct) - statistics.median(self.wrong)) / spread


@dataclass
class RunReport:
    match_against: str
    use_cross_encoder: bool
    threshold: float
    report: annotation.OrphanReport
    distribution: Distribution
    excluded_hits: int = 0

    @property
    def row(self) -> tuple[str, ...]:
        return (
            self.match_against,
            "cross" if self.use_cross_encoder else "bi",
            f"{self.threshold:.2f}",
            str(len(self.report.correct)),
            str(len(self.report.mistyped)),
            str(len(self.report.false_orphans)),
            str(len(self.report.genuine_orphans)),
            f"{self.report.false_orphan_rate:.1%}",
            f"{self.report.typing_f1:.3f}",
            str(self.excluded_hits),
        )


COLUMNS = (
    "match_against", "encoder", "umbral", "correcto", "mistyped", "falso huérfano",
    "huérfano genuino", "tasa FH", "F1", "clase excluida",
)


def rank(use_case: UseCase, matcher: Matcher, *, top_k: int = 5) -> Ranking:
    """One encoding pass over the use_case, thresholds wide open."""
    mentions = use_case.mentions()
    typings = matcher.type_mentions(mentions, use_case.targets, top_k=top_k)
    return Ranking(
        best={typing.mention_id: typing.iri for typing in typings if typing.iri},
        score={typing.mention_id: typing.score for typing in typings},
        runner_up={typing.mention_id: typing.runner_up for typing in typings},
    )


def distribution(use_case: UseCase, ranking: Ranking) -> Distribution:
    """Scores of the top-1 that was right against the top-1 that was wrong.

    Reported separately from the aggregate because the aggregate cannot distinguish "the
    threshold is in the wrong place" from "no threshold would help".
    """
    result = Distribution()
    for document in use_case.documents:
        for mention in document.mentions:
            if mention.gold_class is None or not mention.in_seed:
                continue
            score = ranking.score.get(mention.id)
            if score is None:
                continue
            hit = ranking.best.get(mention.id) == mention.gold_class
            (result.correct if hit else result.wrong).append(score)
    return result


def evaluate(use_case: UseCase, ranking: Ranking, threshold: float) -> annotation.OrphanReport:
    predicted = {
        mention_id: (iri if ranking.score.get(mention_id, 0.0) >= threshold else None)
        for mention_id, iri in ranking.best.items()
    }
    report = annotation.OrphanReport()
    for document in use_case.annotated_documents():
        partial = annotation.score(document, predicted)
        report.correct += partial.correct
        report.mistyped += partial.mistyped
        report.false_orphans += partial.false_orphans
        report.genuine_orphans += partial.genuine_orphans
    return report


def excluded_hits(use_case: UseCase, ranking: Ranking, threshold: float) -> int:
    excluded = set(use_case.excluded_classes)
    if not excluded:
        return 0
    return sum(
        1
        for mention_id, iri in ranking.best.items()
        if iri in excluded and ranking.score.get(mention_id, 0.0) >= threshold
    )


def sweep(
    use_case: UseCase, ranking: Ranking, thresholds: Iterable[float], *,
    match_against: str, use_cross_encoder: bool,
) -> list[RunReport]:
    shared = distribution(use_case, ranking)
    return [
        RunReport(
            match_against=match_against,
            use_cross_encoder=use_cross_encoder,
            threshold=threshold,
            report=evaluate(use_case, ranking, threshold),
            distribution=shared,
            excluded_hits=excluded_hits(use_case, ranking, threshold),
        )
        for threshold in thresholds
    ]


def thresholds(lower: float = 0.30, upper: float = 0.95, step: float = 0.05) -> list[float]:
    count = int(round((upper - lower) / step)) + 1
    return [round(lower + index * step, 2) for index in range(count)]
