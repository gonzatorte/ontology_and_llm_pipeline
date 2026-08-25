from __future__ import annotations

import pytest

from onto_pipeline import review
from onto_pipeline.db import connect


def finding(token="subre", suggestion="sobre", subject="c:1"):
    return review.Finding(
        kind=review.TYPO, subject_iri=subject, summary=f"{token} -> {suggestion}",
        payload={"detector": "edit_distance", "token": token, "suggestion": suggestion},
    )


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path)


def sync(conn, findings, version="v0"):
    return review.sync(conn, findings, version_id=version, kinds=[review.TYPO])


def test_the_same_finding_keeps_its_identity_across_runs(conn):
    """Content-derived, so a re-run cannot duplicate it."""
    assert finding().id == finding().id
    assert finding().id != finding(token="frm").id

    assert sync(conn, [finding()]).added == 1
    report = sync(conn, [finding()])
    assert (report.added, report.already_known) == (0, 1)
    assert len(review.load(conn)) == 1


def test_a_decision_survives_a_rerun(conn):
    """What was rejected stays rejected — that history is the point of keeping it."""
    sync(conn, [finding()])
    review.resolve(conn, finding().id, review.REJECTED, "a domain term, not a typo")

    sync(conn, [finding()])

    stored = review.load(conn, status=review.REJECTED)[0]
    assert stored["comment"] == "a domain term, not a typo"
    assert review.load(conn, status=review.OPEN) == []


def test_a_finding_that_stops_being_raised_is_superseded_not_left_open(conn):
    """A finding is relative to a state; leaving it open grows a backlog of questions about
    an ontology that no longer exists."""
    sync(conn, [finding(), finding(token="frm", suggestion="framework")])
    report = sync(conn, [finding()])

    assert report.superseded == 1
    assert [item["summary"] for item in review.load(conn, status=review.OPEN)] == \
        ["subre -> sobre"]


def test_a_superseded_finding_that_returns_reopens(conn):
    sync(conn, [finding()])
    sync(conn, [])
    assert review.load(conn, status=review.OPEN) == []

    sync(conn, [finding()])
    assert len(review.load(conn, status=review.OPEN)) == 1


def test_a_decided_finding_is_not_superseded_by_disappearing(conn):
    """Superseding only touches what is still open; a decision is history, not a pending item."""
    sync(conn, [finding()])
    review.resolve(conn, finding().id, review.ACCEPTED)

    assert sync(conn, []).superseded == 0
    assert len(review.load(conn, status=review.ACCEPTED)) == 1


def test_syncing_one_kind_does_not_retire_another(conn):
    conn_findings = [finding(), review.Finding(kind=review.DIVERGENT_LABEL, subject_iri="c:2",
                                               summary="a | b", payload={"labels": []})]
    review.sync(conn, conn_findings, version_id="v0",
                kinds=[review.TYPO, review.DIVERGENT_LABEL])

    review.sync(conn, [finding()], version_id="v0", kinds=[review.TYPO])

    assert len(review.load(conn, status=review.OPEN, kind=review.DIVERGENT_LABEL)) == 1


def test_only_accept_or_reject_are_decisions(conn):
    sync(conn, [finding()])
    with pytest.raises(ValueError, match="accepted or rejected"):
        review.resolve(conn, finding().id, review.SUPERSEDED)


def test_resolving_something_that_does_not_exist_reports_it(conn):
    assert review.resolve(conn, "nosuchid", review.ACCEPTED) is False
