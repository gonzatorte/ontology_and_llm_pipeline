"""PREP-NORMALIZE-LABELS-VERIFY: el idioma de cada etiqueta y el veredicto por traducción."""

from __future__ import annotations

import json

import pytest

from onto_pipeline import label_verification as lv


def _answer(readings: dict[str, tuple[str, str, str]], labels: list[str]) -> str:
    """La respuesta del modelo, indexada como la pide el prompt."""
    return json.dumps({
        str(index): {"language": readings[text][0], "en": readings[text][1],
                     "es": readings[text][2]}
        for index, text in enumerate(labels, start=1)
    })


def test_every_label_in_the_batch_has_to_come_back():
    """Un modelo que descarta ítems en silencio devuelve un resultado que se ve bien y al que le
    faltan etiquetas — FINDINGS-SILENT-FAILURES. Numerar y exigir el índice es lo que lo hace
    visible, y el lote entero falla en vez de quedar a medias."""
    batch = lv.Batch(["Valor", "Tipo pauta"])
    body = lv.payload(batch)

    with pytest.raises(ValueError, match="skips label 2"):
        lv.parse(json.dumps({"1": {"language": "es", "en": "value", "es": "valor"}}), body)


def test_a_language_that_is_not_one_of_the_three_fails_the_batch():
    """`es`, `en` o `und`. Aceptar un cuarto valor lo termina escribiendo como tag de un literal
    y el síntoma aparece lejos de acá."""
    batch = lv.Batch(["Valor"])
    with pytest.raises(ValueError, match="not one of"):
        lv.parse(json.dumps({"1": {"language": "pt", "en": "value", "es": "valor"}}),
                 lv.payload(batch))


def test_a_pair_that_matches_once_translated_is_verified():
    """El caso que abrió esto: `Valor` contra `value` es la misma palabra en dos idiomas, y sólo
    se ve comparando las traducciones."""
    readings = lv.parse(
        _answer({"Valor": ("es", "value", "valor"), "value": ("en", "value", "valor")},
                ["Valor", "value"]),
        lv.payload(lv.Batch(["Valor", "value"])),
    )

    verdict = lv.verdict("Valor", ["value"], readings, threshold=0.95)

    assert verdict.verified
    assert verdict.similarity == 1.0
    assert "value" in verdict.comment


def test_the_spec_example_survives_verification():
    """`Aplica_una_o_varias` contra `appliesTechnique` es la salvedad que PREP-NORMALIZE-LABELS
    usa para justificar que no se asuma una traducción: la etiqueta pierde la cuantificación que
    el identificador carga. Traducido sigue sin coincidir, así que sigue marcado — y es la razón
    por la que el veredicto no se le pide al modelo."""
    labels = ["Aplica una o varias", "appliesTechnique"]
    readings = lv.parse(
        _answer({
            "Aplica una o varias": ("es", "applies one or several", "aplica una o varias"),
            "appliesTechnique": ("en", "applies technique", "aplica tecnica"),
        }, labels),
        lv.payload(lv.Batch(labels)),
    )

    verdict = lv.verdict(labels[0], [labels[1]], readings, threshold=0.95)

    assert not verdict.verified
    assert verdict.similarity < 0.8


def test_a_pair_between_the_two_thresholds_is_not_closed_but_carries_its_evidence():
    """Entre 0,8 y 0,95 el hallazgo queda abierto: la traducción no lo confirma, pero lo que se
    averiguó viaja con él para que quien decida no vuelva a preguntárselo."""
    labels = ["Recoleccion de datos", "dataCollectionMethod"]
    readings = lv.parse(
        _answer({
            "Recoleccion de datos": ("es", "data collection", "recoleccion de datos"),
            "dataCollectionMethod": ("en", "data collection method",
                                     "metodo de recoleccion de datos"),
        }, labels),
        lv.payload(lv.Batch(labels)),
    )

    verdict = lv.verdict(labels[0], [labels[1]], readings, threshold=0.95)

    assert not verdict.verified
    assert 0.8 <= verdict.similarity < 0.95
    evidence = verdict.as_evidence()
    assert evidence["declared"]["text"] == "dataCollectionMethod"
    assert evidence["derived"]["es"] == "recoleccion de datos"


def test_the_verdict_is_the_best_of_every_declared_label():
    """Hay una declarada por cada `rdfs:label`, y el hallazgo afirma que **ninguna** nombra al
    concepto: alcanza con que una lo confirme para que no se sostenga."""
    labels = ["Valor", "magnitude", "value"]
    readings = lv.parse(
        _answer({
            "Valor": ("es", "value", "valor"),
            "magnitude": ("en", "magnitude", "magnitud"),
            "value": ("en", "value", "valor"),
        }, labels),
        lv.payload(lv.Batch(labels)),
    )

    verdict = lv.verdict("Valor", ["magnitude", "value"], readings, threshold=0.95)

    assert verdict.verified and verdict.declared_text == "value"


def test_a_label_nobody_could_read_leaves_the_finding_alone():
    """Un lote que falló y no se pudo partir más deja etiquetas sin lectura. Sin veredicto no se
    inventa uno: el hallazgo queda como estaba."""
    assert lv.verdict("Valor", ["value"], {}, threshold=0.95) is None


# ─────────────────────────────  BATCH-BY-CONTENT  ─────────────────────────────


def _labels(n: int) -> list[str]:
    return [f"label number {index}" for index in range(n)]


def test_a_label_is_asked_about_once_even_if_ten_classes_carry_it():
    """BATCH-DEDUPE: el idioma y la traducción son propiedad de la cadena, así que repetirla es
    pagar dos veces por la misma pregunta."""
    grouped = lv.batches(["Valor", "Valor", "value"], size=40)

    assert [text for batch in grouped for text in batch.labels] == ["Valor", "value"]


def test_adding_one_label_only_reruns_its_own_batch():
    """BATCH-CUT: la unidad del ledger es el lote y su clave sale del payload entero. Cortando
    de a `size` en orden, una etiqueta nueva corre todo un lugar y se pierde la caché de todos
    los lotes que siguen — sobre una ontología grande, cientos de llamadas por una palabra."""
    before = lv.batches(_labels(200), size=8)
    after = lv.batches([*_labels(200), "a brand new label"], size=8)

    moved = {batch.key for batch in after} - {batch.key for batch in before}

    assert len(moved) == 1, "sólo el lote donde cae la etiqueta nueva cambia de clave"
    assert len(before) > 5, "y el corte por contenido igual produce lotes del tamaño pedido"


def test_no_batch_grows_past_twice_the_size():
    """El corte por contenido da tamaños variables; sin tope, una racha sin frontera arma un
    lote gigante y con él una llamada que no entra en la ventana del modelo."""
    grouped = lv.batches(_labels(500), size=10)

    assert max(len(batch.labels) for batch in grouped) <= 20


def test_a_batch_the_model_mangles_is_retried_in_halves():
    """VERIFY-3-SPLIT: el reintento del ledger repite la misma unidad con el mismo payload, así
    que no sirve cuando el problema es una etiqueta adentro del lote. Una sola mal contestada no
    puede llevarse puestas las otras."""
    halves = lv.split(lv.Batch(["a", "b", "c", "d"]))

    assert [batch.labels for batch in halves] == [["a", "b"], ["c", "d"]]


def test_a_single_label_that_keeps_failing_is_the_floor_of_the_split():
    """Partir termina en una etiqueta sola: ahí ya no hay a quién culpar, y esa unidad queda
    fallada y se cuenta como tal."""
    assert lv.split(lv.Batch(["a"])) == []


def test_a_batch_the_provider_never_answered_is_not_split():
    """Medido, y caro: con el proveedor devolviendo 429 por límite de uso, partir cada lote
    fallado convirtió 4 unidades en 87 —cada mitad vuelve a fallar y vuelve a partirse— contra
    un servicio que ya estaba rechazando. Partir es la respuesta a una respuesta mala, no a que
    no haya respuesta."""
    assert lv.is_mangled("MangledBatch: the answer skips label 2 ('Valor')")
    assert not lv.is_mangled("RuntimeError: https://opencode.ai/zen/go/v1 returned 429")
    assert not lv.is_mangled("TimeoutError: timed out")
