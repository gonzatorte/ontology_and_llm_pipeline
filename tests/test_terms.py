"""Cómo se comparan los términos que salen de un identificador (PREP-NORMALIZE-LABELS)."""

from __future__ import annotations

from onto_pipeline import terms


def test_the_same_word_with_and_without_an_accent_compares_as_itself():
    """FOLD-DIACRITICS: una ontología que acentúa sus etiquetas y no sus IRIs comparaba la
    misma palabra contra sí misma por debajo de `label_divergence_threshold`."""
    assert terms.similarity("día", "dia") == 1.0
    assert terms.similarity("método", "metodo") == 1.0


def test_folding_accents_does_not_break_the_language_guess():
    """El guess lee el término crudo antes de tokenizar, así que la ñ y las tildes siguen
    siendo evidencia de español aunque `tokens` ya no las conserve."""
    assert terms.guess_language("año") == "es"
    assert terms.guess_language("interpretación") == "es"
    assert terms.guess_language("clasificacion descriptiva") == "es"


def test_an_accent_is_the_only_thing_separating_some_words():
    """El costo que FOLD-DIACRITICS acepta a cambio, fijado acá para que sea un riesgo
    conocido y no una sorpresa: el par mínimo se vuelve indistinguible."""
    assert terms.similarity("año", "ano") == 1.0
    assert terms.similarity("término", "termino") == 1.0


def test_case_conventions_already_compare_equal():
    """snake_case, camelCase y kebab-case ya se normalizaban antes del plegado; el plegado no
    los puede romper."""
    assert terms.similarity("divergent_label", "divergentLabel") == 1.0
    assert terms.similarity("has_subject", "has-subject") == 1.0
    assert terms.denormalize("hasHTTPServer") == "has HTTP Server"
