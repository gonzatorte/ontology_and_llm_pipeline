"""El idioma de una etiqueta cuando el guess no alcanza (LANGUAGE-PRECEDENCE)."""

from __future__ import annotations

import pytest

from onto_pipeline import label_overrides
from onto_pipeline.db import connect

SESSION = "s1"


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path)


def test_a_correction_made_by_the_user_is_not_overwritten_by_the_model(conn):
    """El pase del modelo re-corre sobre todo el inventario en cada corrida. Si pudiera pisar lo
    que alguien corrigió a mano, la corrección duraría hasta la próxima vez que se ejecuta la
    etapa y nadie se enteraría de que volvió atrás."""
    label_overrides.record(
        conn, {"Valor": "es"}, source=label_overrides.USER, session_id=SESSION
    )
    label_overrides.record(
        conn, {"Valor": "en", "Tipo pauta": "es"},
        source=label_overrides.MODEL, session_id=SESSION,
    )

    stored = label_overrides.load(conn, session_id=SESSION)

    assert stored["Valor"] == label_overrides.Override("es", label_overrides.USER)
    assert stored["Tipo pauta"].source == label_overrides.MODEL


def test_the_user_corrects_what_the_model_had_already_said(conn):
    """La precedencia va en un solo sentido: el usuario entra después y gana."""
    label_overrides.record(
        conn, {"Valor": "en"}, source=label_overrides.MODEL, session_id=SESSION
    )
    label_overrides.record(
        conn, {"Valor": "es"}, source=label_overrides.USER, session_id=SESSION
    )

    assert label_overrides.load(conn, session_id=SESSION)["Valor"].language == "es"


def test_a_second_model_pass_refreshes_what_the_model_had_said(conn):
    """Lo suyo sí lo puede reescribir: un lote que antes falló y ahora contesta tiene que poder
    corregir lo que quedó."""
    label_overrides.record(
        conn, {"Valor": "en"}, source=label_overrides.MODEL, session_id=SESSION
    )
    label_overrides.record(
        conn, {"Valor": "es"}, source=label_overrides.MODEL, session_id=SESSION
    )

    assert label_overrides.load(conn, session_id=SESSION)["Valor"].language == "es"


def test_an_override_belongs_to_one_session(conn):
    """SESSION-SCOPED-DATA: dos sesiones sobre el mismo caso de uso derivan los mismos textos,
    así que sin el filtro una le estaría decidiendo el idioma a la otra."""
    label_overrides.record(
        conn, {"Valor": "es"}, source=label_overrides.USER, session_id=SESSION
    )

    assert label_overrides.load(conn, session_id="otra") == {}


def test_a_language_that_is_not_one_of_the_three_is_refused(conn):
    """`es`, `en` o `und`. Un cuarto valor termina como tag de un literal que nadie puede leer,
    y el síntoma aparece lejos de acá."""
    with pytest.raises(ValueError):
        label_overrides.record(
            conn, {"Valor": "pt"}, source=label_overrides.USER, session_id=SESSION
        )
