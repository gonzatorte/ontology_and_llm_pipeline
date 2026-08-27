from __future__ import annotations

from onto_pipeline import alignment

CORPUS = [
    "We ran a focus group with eight participants and took a field note after each session.",
    "The coding scheme emerged from repeated readings of the transcripts.",
]


def survey(labels):
    return alignment.survey([(f"c:{i}", label) for i, label in enumerate(labels)], CORPUS)


# ─────────────────────────  contar apariciones  ─────────────────────────


def test_a_label_that_is_not_there_is_reported_as_absent():
    report = survey(["Grounded Theory"])
    assert report.absent[0].label == "Grounded Theory"
    assert report.coverage == 0.0


def test_a_label_that_is_there_is_counted_with_its_documents():
    presence = survey(["field note"]).classes[0]
    assert presence.occurrences == 1 and presence.documents == 1


def test_a_phrase_broken_across_lines_still_counts():
    """Un PDF parte los sintagmas donde se le termina la columna."""
    report = alignment.survey([("c:1", "coding scheme")], ["the coding\nscheme emerged"])
    assert not report.classes[0].absent


def test_case_does_not_matter():
    assert not alignment.survey([("c:1", "FIELD NOTE")], CORPUS).classes[0].absent


def test_a_label_in_one_document_only_is_flagged():
    """Tan sospechosa como una que no aparece: casi siempre es casualidad o eco léxico."""
    report = survey(["field note", "Grounded Theory"])
    assert [item.label for item in report.single_document] == ["field note"]


def test_multiword_labels_are_reported_apart():
    """Una etiqueta de una sola palabra puede aparecer por eco léxico; una de tres, no."""
    report = survey(["session", "focus group with eight"])
    assert [item.label for item in report.multiword] == ["focus group with eight"]


def test_an_empty_label_does_not_crash():
    assert alignment.survey([("c:1", "  ")], CORPUS).classes[0].absent


# ─────────  lo único que decide: los términos declarados centrales  ─────────


def test_a_declared_term_that_is_missing_is_conclusive():
    """Si quien conoce el dominio dice que se trata de esto y no está, no hay matcher que lo
    arregle."""
    found = alignment.check_terms(["grounded theory", "field note"], CORPUS)
    assert found == {"grounded theory": 0, "field note": 1}


def test_declared_terms_are_counted_across_every_document():
    found = alignment.check_terms(["the"], CORPUS)
    assert found["the"] >= 2


# ─────────  por qué la cobertura global no alcanza para un veredicto  ─────────


def test_the_report_offers_no_verdict_on_coverage():
    """Medido: decidía "desalineado" con 20% sobre un par bueno y "alineado" con 50% sobre uno
    roto. Una ontología publicada cubre un dominio entero; un corpus, una franja."""
    assert not hasattr(alignment.Report, "verdict")
