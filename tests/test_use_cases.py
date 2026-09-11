"""Cargar un caso de uso: qué tiene que sobrevivir del corpus publicado al llegar al banco."""

from __future__ import annotations

from pathlib import Path

import pytest

from onto_pipeline import use_cases


def load(directory: Path, **kwargs):
    return use_cases.load_use_case(
        directory, match_against=kwargs.pop("match_against", "label"), **kwargs
    )


def test_the_importer_anchors_offsets_on_the_text_itself(use_case_dir):
    """`BUILD-OUT-OF-SCOPE` deja los importadores fuera de v1 porque los offsets estándar
    indexan texto plano y los del pipeline el Markdown del parser. Saltear el parser saca la
    objeción: el texto es su propia referencia, así que los spans tienen que aguantar contra
    él."""
    from onto_pipeline import annotation

    case = load(use_case_dir())
    for document, annotated in zip(case.documents, case.annotated_documents(), strict=True):
        annotation.validate(annotated, document.text, document.text_hash)


def test_discontinuous_annotations_are_skipped_and_counted(use_case_dir):
    """Aplanar un span discontinuo le daría al encoder el texto que el anotador dejó afuera.
    Tirarlo en silencio subestimaría el corpus."""
    case = load(use_case_dir())
    assert [mention.id for mention in case.documents[0].mentions] == ["i1", "i2"]
    assert case.documents[0].skipped == 1


def test_in_seed_is_decided_by_the_inventory_not_by_a_human(use_case_dir):
    """El motivo entero de medir sobre un corpus publicado: el campo que define la métrica de
    falsos huérfanos no necesita campaña de anotación."""
    case = load(use_case_dir())
    assert all(mention.in_inventory for mention in case.documents[0].mentions)


def test_obsolete_classes_never_become_targets(use_case_dir):
    assert "CL:9999999" not in load(use_case_dir()).inventory


def test_the_extension_file_completes_the_release_without_overriding_it(use_case_dir):
    """CRAFT anota con el release más clases que define él mismo. Las dos tienen que resolver,
    y la redacción del release es la que se está midiendo."""
    case = load(use_case_dir())
    assert "CL_EXT:lens_fibre" in case.inventory
    neuron = next(target for target in case.targets if target.iri == "CL:0000540")
    assert neuron.gloss == "A cell that transmits electrical signals."
    assert neuron.alt_labels == ["nerve cell"]


def test_an_obo_id_and_its_purl_name_the_same_class():
    assert use_cases.curie("http://purl.obolibrary.org/obo/CL_0000540") == "CL:0000540"
    assert use_cases.curie("CL:0000540") == "CL:0000540"
    assert use_cases.curie("CL_EXT:lens_fibre") == "CL_EXT:lens_fibre"


def test_withholding_classes_manufactures_genuine_orphans(tmp_path, use_case_dir):
    """Un corpus anotado contra O no tiene huérfanas genuinas por construcción, así que esa
    mitad de `BUILD-NO-GO-GATE` queda sin probar salvo que se corte el inventario a propósito."""
    plain = load(use_case_dir(tmp_path / "plain"))
    assert all(
        mention.in_inventory for document in plain.documents for mention in document.mentions
    )

    withheld = load(use_case_dir(tmp_path / "cut", holdout_classes="[CL:0000233]"))
    assert withheld.withheld == ["CL:0000233"]
    assert "CL:0000233" not in withheld.inventory
    assert {
        mention.gold_class: mention.in_inventory
        for document in withheld.documents for mention in document.mentions
    } == {"CL:0000540": True, "CL:0000233": False}


def test_the_holdout_is_deterministic(use_case_dir):
    directory = use_case_dir()
    first = load(directory, holdout=0.5).withheld
    second = load(directory, holdout=0.5).withheld
    assert first == second and first


def test_classes_the_annotators_never_use_leave_the_inventory(use_case_dir):
    """CRAFT garantiza que una predicción contra éstas está mal, así que dejarlas de candidatas
    fabrica errores. En CRAFT/CL la clase en cuestión está etiquetada *cell*, idéntica a la
    extension class que los anotadores sí usan, que se lleva el 37% del corpus
    (`FINDINGS-MEASURED-LABEL-COLLISION`)."""
    case = load(use_case_dir(excluded_classes="[CL:0000000]"))
    assert case.excluded_classes == ["CL:0000000"]
    assert "CL:0000000" not in case.inventory and case.dropped_excluded


def test_brat_is_read_too_because_the_use_cases_do_not_share_a_format(tmp_path):
    text = "the fatigue crack grew"
    path = tmp_path / "d.ann"
    path.write_text(
        "T1\tCrack 12 17\tcrack\nT2\tThing 4 11;12 17\tfatigue crack\n#1\tNote T1\tx\n",
        encoding="utf-8",
    )
    mentions, skipped = use_cases.read_brat(path, text)
    assert [(m.gold_class, m.text) for m in mentions] == [("Crack", "crack")]
    assert skipped == 1


def test_an_unreadable_format_fails_loudly(use_case_dir):
    directory = use_case_dir()
    descriptor = directory / "use_case.yml"
    descriptor.write_text(
        descriptor.read_text().replace("knowtator", "inception"), encoding="utf-8"
    )
    with pytest.raises(use_cases.UnknownFormat):
        load(directory)


def test_a_use_case_pointing_at_nothing_says_so(use_case_dir):
    directory = use_case_dir()
    descriptor = directory / "use_case.yml"
    descriptor.write_text(
        descriptor.read_text().replace("dir: txt", "dir: missing"), encoding="utf-8"
    )
    with pytest.raises(use_cases.UseCaseIncomplete):
        load(directory)
