from __future__ import annotations

import pytest

from onto_pipeline import review
from onto_pipeline.db import connect

SESSION = "test-1"
OTHER = "test-2"


def finding(token="subre", suggestion="sobre", subject="c:1"):
    return review.Finding(
        kind=review.TYPO, subject_iri=subject, summary=f"{token} -> {suggestion}",
        payload={"detector": "edit_distance", "token": token, "suggestion": suggestion},
    )


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path)


def sync(conn, findings, version="v0", session=SESSION):
    return review.sync(
        conn, findings, version_id=version, kinds=[review.TYPO], session_id=session
    )


def load(conn, session=SESSION, **kwargs):
    return review.load(conn, session_id=session, **kwargs)


def resolve(conn, item_id, status, comment="", session=SESSION):
    return review.resolve(conn, item_id, status, comment, session_id=session)


def test_the_same_finding_keeps_its_identity_across_runs(conn):
    """Content-derived, so a re-run cannot duplicate it."""
    assert finding().id == finding().id
    assert finding().id != finding(token="frm").id

    assert sync(conn, [finding()]).added == 1
    report = sync(conn, [finding()])
    assert (report.added, report.already_known) == (0, 1)
    assert len(load(conn)) == 1


def test_a_decision_survives_a_rerun(conn):
    """What was rejected stays rejected — that history is the point of keeping it."""
    sync(conn, [finding()])
    resolve(conn, finding().id, review.REJECTED, "a domain term, not a typo")

    sync(conn, [finding()])

    stored = load(conn, status=review.REJECTED)[0]
    assert stored["comment"] == "a domain term, not a typo"
    assert load(conn, status=review.OPEN) == []


def test_a_finding_that_stops_being_raised_is_superseded_not_left_open(conn):
    """A finding is relative to a state; leaving it open grows a backlog of questions about
    an ontology that no longer exists."""
    sync(conn, [finding(), finding(token="frm", suggestion="framework")])
    report = sync(conn, [finding()])

    assert report.superseded == 1
    assert [item["summary"] for item in load(conn, status=review.OPEN)] == ["subre -> sobre"]


def test_a_superseded_finding_that_returns_reopens(conn):
    sync(conn, [finding()])
    sync(conn, [])
    assert load(conn, status=review.OPEN) == []

    sync(conn, [finding()])
    assert len(load(conn, status=review.OPEN)) == 1


def test_a_decided_finding_is_not_superseded_by_disappearing(conn):
    """Superseding only touches what is still open; a decision is history, not a pending item."""
    sync(conn, [finding()])
    resolve(conn, finding().id, review.ACCEPTED)

    assert sync(conn, []).superseded == 0
    assert len(load(conn, status=review.ACCEPTED)) == 1


def test_syncing_one_kind_does_not_retire_another(conn):
    conn_findings = [finding(), review.Finding(kind=review.DIVERGENT_LABEL, subject_iri="c:2",
                                               summary="a | b", payload={"labels": []})]
    review.sync(conn, conn_findings, version_id="v0", session_id=SESSION,
                kinds=[review.TYPO, review.DIVERGENT_LABEL])

    review.sync(conn, [finding()], version_id="v0", kinds=[review.TYPO], session_id=SESSION)

    assert len(load(conn, status=review.OPEN, kind=review.DIVERGENT_LABEL)) == 1


def test_only_accept_or_reject_are_decisions(conn):
    sync(conn, [finding()])
    with pytest.raises(ValueError, match="accepted or rejected"):
        resolve(conn, finding().id, review.SUPERSEDED)


def test_resolving_something_that_does_not_exist_reports_it(conn):
    assert resolve(conn, "nosuchid", review.ACCEPTED) is False


def test_a_decision_belongs_to_the_session_that_took_it(conn):
    """El id sale del contenido, así que dos sesiones sobre el mismo caso de uso levantan el
    mismo hallazgo con el mismo id. Sin la sesión en la clave, la segunda heredaba una decisión
    que nadie tomó ahí — y «lo rechazado vale más que lo aceptado» deja de valer si lo rechazó
    otra corrida."""
    sync(conn, [finding()])
    resolve(conn, finding().id, review.REJECTED, "es un término del dominio")

    assert sync(conn, [finding()], session=OTHER).added == 1
    assert [item["status"] for item in load(conn, status=None, session=OTHER)] == [review.OPEN]


BEFORE_SESSIONS = """
CREATE TABLE review_items (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  version_id   TEXT,
  subject_iri  TEXT,
  summary      TEXT NOT NULL,
  payload      TEXT NOT NULL,
  status       TEXT NOT NULL,
  comment      TEXT,
  created_at   TEXT,
  resolved_at  TEXT
);
CREATE INDEX idx_review_status ON review_items(status, kind);
"""


def test_a_store_older_than_the_sessions_keeps_its_decisions(conn):
    """La sesión se deduce de `version_id`, que es `<sesión>:v<N>`. Descartar la tabla sería
    tirar lo único que no está en ningún otro lado: lo que alguien ya rechazó."""
    conn.script(BEFORE_SESSIONS)
    conn.execute(
        "INSERT INTO review_items (id, kind, version_id, subject_iri, summary, payload, "
        "status, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (finding().id, review.TYPO, "qualitative-1:v0", "c:1", "subre -> sobre", "{}",
         review.REJECTED, "es un término del dominio"),
    )

    review.install(conn)

    stored = load(conn, status=review.REJECTED, session="qualitative-1")
    assert [(item["id"], item["comment"]) for item in stored] == \
        [(finding().id, "es un término del dominio")]


def test_one_session_does_not_retire_what_another_left_open(conn):
    """Sincronizar los hallazgos de una sesión no puede jubilar los de otra: el hallazgo que la
    otra no volvió a levantar sigue esperando su decisión, no la de esta corrida."""
    sync(conn, [finding()])
    report = sync(conn, [finding(token="frm", suggestion="framework")], session=OTHER)

    assert report.superseded == 0
    assert len(load(conn, status=review.OPEN)) == 1


def test_evidence_can_be_added_without_touching_the_decision(conn):
    """`sync` sólo inserta lo que todavía no está, así que un hallazgo abierto no tiene por dónde
    recibir lo que se averiguó después. La evidencia va al payload de la **fila**, nunca al que
    arma `findings_from_initial`: el id deriva de ése, y tocarlo dejaría huérfana cada decisión
    ya tomada."""
    sync(conn, [finding()])
    item_id = finding().id

    assert review.annotate(conn, item_id, {"translation": {"similarity": 0.83}},
                           session_id=SESSION)

    stored = load(conn, status=review.OPEN)[0]
    assert stored["payload"]["translation"] == {"similarity": 0.83}
    assert stored["payload"]["token"] == "subre", "y lo que ya estaba sigue ahí"
    assert stored["status"] == review.OPEN, "anotar no decide nada"
    assert load(conn, status=review.OPEN)[0]["id"] == item_id, "ni le cambia el id"


def test_annotating_a_finding_of_another_session_does_nothing(conn):
    """SESSION-SCOPED-DATA: el id de un hallazgo deriva del contenido, así que dos sesiones sobre
    el mismo caso de uso tienen el mismo. Sin el filtro, una le escribiría evidencia a la otra."""
    sync(conn, [finding()])

    assert not review.annotate(conn, finding().id, {"translation": {}}, session_id=OTHER)
