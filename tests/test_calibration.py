"""El banco: qué mide un barrido sobre un caso de uso, y qué separa de qué."""

from __future__ import annotations

from pathlib import Path

from onto_pipeline import calibration, use_cases
from onto_pipeline.matching import Matcher


def load(directory: Path, **kwargs):
    return use_cases.load_use_case(
        directory, match_against=kwargs.pop("match_against", "label"), **kwargs
    )


def bench(encoder) -> Matcher:
    """Las zonas abiertas de par en par: el barrido las aplica él mismo, umbral por umbral."""
    return Matcher(encoder, auto_merge_threshold=1.1, grey_zone_lower=0.0)


def test_a_withheld_class_separates_a_genuine_orphan_from_a_false_one(use_case_dir):
    """La distinción desde la que se lee `BUILD-NO-GO-GATE` entera. Sin nada tipado, el neuron
    es huérfano falso —su clase está en el inventario— y el platelet retenido es genuino."""
    withheld = load(use_case_dir(holdout_classes="[CL:0000233]"))
    nothing = calibration.Ranking(best={}, score={}, runner_up={})
    report = calibration.evaluate(withheld, nothing, threshold=0.5)
    assert report.false_orphans == ["i1"] and report.genuine_orphans == ["i2"]


def test_the_sweep_reuses_one_encoding_pass_across_thresholds(use_case_dir, keyword_encoder):
    """Las zonas viven dentro de `type_mentions`, que está bien para el pipeline y mal para un
    barrido: re-encodear por umbral costaría horas para recalcular una función de puntajes que
    ya están en la mano."""
    case = load(use_case_dir())
    ranking = calibration.rank(case, bench(keyword_encoder))
    assert set(ranking.best) == {"i1", "i2"}

    low = calibration.evaluate(case, ranking, threshold=0.0)
    high = calibration.evaluate(case, ranking, threshold=1.01)
    assert low.correct and not high.correct
    assert len(high.false_orphans) == 2


def test_a_threshold_above_every_score_turns_hits_into_false_orphans(
    use_case_dir, keyword_encoder
):
    """La falla que nombra `RISKS-FALSE-ORPHANS`, y por qué la métrica se parte: un agregado
    reportaría el mismo número para un matcher que rankeó mal y para uno cuyo umbral está mal
    puesto."""
    case = load(use_case_dir())
    ranking = calibration.rank(case, bench(keyword_encoder))
    assert calibration.evaluate(case, ranking, 1.01).false_orphan_rate == 1.0


def test_the_distribution_separates_right_top_one_from_wrong(use_case_dir, keyword_encoder):
    """La pregunta que va **antes** de dónde poner el umbral: si las dos distribuciones se
    pisan, ningún umbral ayuda."""
    case = load(use_case_dir())
    dist = calibration.distribution(case, calibration.rank(case, bench(keyword_encoder)))
    assert dist.correct
    assert isinstance(dist.separation, float)


def test_keeping_the_never_correct_classes_measures_how_much_error_they_cause(use_case_dir):
    """La corrida de comparación: señal de precisión que no costó anotar nada."""
    case = load(use_case_dir(excluded_classes="[CL:0000000]"), drop_excluded=False)
    assert "CL:0000000" in case.inventory and not case.dropped_excluded
    ranking = calibration.Ranking(
        best={"i1": "CL:0000000"}, score={"i1": 0.9}, runner_up={"i1": None}
    )
    assert calibration.excluded_hits(case, ranking, 0.5) == 1
    assert calibration.excluded_hits(case, ranking, 0.95) == 0
