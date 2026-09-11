from __future__ import annotations

from onto_pipeline import typing_store
from onto_pipeline.db import connect
from onto_pipeline.matching import AUTO, DISCARDED, GREY, Typing

SESSION = "test-1"

CANDIDATE = "c:Technique"


def store(tmp_path, zone=GREY, iri=CANDIDATE):
    conn = connect(tmp_path)
    typing_store.install(conn)
    conn.execute(
        "INSERT INTO mentions (id, session_id, document_id, page, surface_text, status) "
        "VALUES ('m1', ?, 'd1', 1, 'focus group', 'extracted')", (SESSION,)
    )
    conn.commit()
    typing_store.persist_typings(conn, "v1", [Typing("m1", iri, 0.86, zone, "c:Other")],
        session_id=SESSION)
    return conn


def zone_of(conn, mention_id="m1"):
    row = conn.execute(
        "SELECT iri, zone FROM mention_typing WHERE mention_id = ?", (mention_id,)
    ).fetchone()

    return row["iri"], row["zone"]


# ─────────────────────────  the queue  ─────────────────────────


def test_an_unanswered_pair_is_waiting(tmp_path):
    conn = store(tmp_path)
    assert [row["mention_id"] for row in typing_store.pending(conn, "v1",
        session_id=SESSION)] == ["m1"]


def test_an_answered_pair_leaves_the_queue(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", CANDIDATE, session_id=SESSION)
    assert typing_store.pending(conn, "v1", session_id=SESSION) == []


def test_an_automatic_typing_was_never_a_question(tmp_path):
    assert typing_store.pending(store(tmp_path, zone=AUTO), "v1", session_id=SESSION) == []


# ─────────────────────────  the answers  ─────────────────────────


def test_accepting_types_the_mention(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", CANDIDATE, session_id=SESSION)
    typing_store.persist_typings(conn, "v1", [Typing("m1", CANDIDATE, 0.86, GREY, None)],
        session_id=SESSION)
    assert zone_of(conn) == (CANDIDATE, AUTO)


def test_none_of_these_is_a_real_answer_and_orphans_the_mention(tmp_path):
    """Often the right one: the mention reaches induction, which is where a genuinely new
    concept belongs."""
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", None, offered=CANDIDATE, session_id=SESSION)
    typing_store.persist_typings(conn, "v1", [Typing("m1", CANDIDATE, 0.86, GREY, None)],
        session_id=SESSION)
    assert zone_of(conn) == (None, DISCARDED)


def test_the_answer_may_name_a_different_class(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", "c:Interview", offered=CANDIDATE, session_id=SESSION)
    typing_store.persist_typings(conn, "v1", [Typing("m1", CANDIDATE, 0.86, GREY, None)],
        session_id=SESSION)
    assert zone_of(conn) == ("c:Interview", AUTO)


def test_an_answer_survives_the_next_match(tmp_path):
    """Asking the same question every run is how a system trains someone to stop answering."""
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", None, offered=CANDIDATE, session_id=SESSION)
    for _ in range(3):
        typing_store.persist_typings(conn, "v1", [Typing("m1", CANDIDATE, 0.91, GREY, None)],
            session_id=SESSION)
    assert zone_of(conn) == (None, DISCARDED)
    assert typing_store.pending(conn, "v1", session_id=SESSION) == []


def test_an_answer_does_not_touch_a_pair_that_was_never_grey(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", None, offered=CANDIDATE, session_id=SESSION)
    typing_store.persist_typings(conn, "v1", [Typing("m1", CANDIDATE, 0.99, AUTO, None)],
        session_id=SESSION)
    assert zone_of(conn) == (CANDIDATE, AUTO)


def test_the_split_counts_the_answered_pair_where_it_landed(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", None, offered=CANDIDATE, session_id=SESSION)
    split = typing_store.persist_typings(
        conn, "v1", [Typing("m1", CANDIDATE, 0.86, GREY, None)]
    , session_id=SESSION)
    assert (split.grey, split.orphan, split.typed) == (0, 1, 0)


# ─────────────────────────  the labels  ─────────────────────────


def test_taking_the_offer_is_an_accept_label(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", CANDIDATE, offered=CANDIDATE, score=0.86, session_id=SESSION)
    label = typing_store.labels(conn, session_id=SESSION)[0]
    assert label["accepted"] and label["surface_text"] == "focus group"


def test_refusing_the_offer_is_a_reject_label(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", None, offered=CANDIDATE, score=0.86, session_id=SESSION)
    assert not typing_store.labels(conn, session_id=SESSION)[0]["accepted"]


def test_naming_another_class_is_also_a_reject_of_what_was_offered(tmp_path):
    conn = store(tmp_path)
    typing_store.answer(conn, "m1", "c:Interview", offered=CANDIDATE, score=0.86,
        session_id=SESSION)
    label = typing_store.labels(conn, session_id=SESSION)[0]
    assert not label["accepted"] and label["iri"] == "c:Interview"


def test_no_answers_means_no_labels(tmp_path):
    assert typing_store.labels(store(tmp_path), session_id=SESSION) == []
