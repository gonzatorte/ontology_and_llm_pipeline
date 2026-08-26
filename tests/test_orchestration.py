from __future__ import annotations

from onto_pipeline import orchestration
from onto_pipeline.db import connect
from onto_pipeline.orchestration import BLOCKED, DONE, READY, WAITING


def store(tmp_path):
    conn = connect(tmp_path)
    from onto_pipeline import review, typing_store

    typing_store.install(conn)
    review.install(conn)
    return conn


def survey(conn, has_provider=True):
    return orchestration.survey(conn, "v1", has_provider=has_provider)


def named(plan, name):
    return next(step for step in plan.steps if step.name == name)


def with_document(conn, blocks=1):
    conn.execute("INSERT INTO documents (id, held_out) VALUES ('d1', 0)")
    for index in range(blocks):
        conn.execute(
            "INSERT INTO blocks (id, document_id, page, ordinal, block_type, text) "
            "VALUES (?, 'd1', 1, ?, 'paragraph', 'text')", (f"b{index}", index),
        )
    conn.commit()


def with_mention(conn, mention_id="m1"):
    conn.execute(
        "INSERT INTO mentions (id, document_id, page, surface_text, status) "
        "VALUES (?, 'd1', 1, 'focus group', 'extracted')", (mention_id,),
    )
    conn.commit()


def typed(conn, mention_id="m1", iri="c:A", zone="auto"):
    conn.execute(
        "INSERT INTO mention_typing (mention_id, version_id, iri, score, zone) "
        "VALUES (?, 'v1', ?, 0.99, ?)", (mention_id, iri, zone),
    )
    conn.commit()


# ─────────────────────────  the shapes of answer  ─────────────────────────


def test_an_empty_store_starts_at_ingest(tmp_path):
    plan = survey(store(tmp_path))
    assert plan.next.name == "ingest" and plan.next.state == READY


def test_a_stage_whose_input_is_missing_is_blocked_not_pending(tmp_path):
    """Saying which is the difference between advice and a list."""
    step = named(survey(store(tmp_path)), "extract")
    assert step.state == BLOCKED and "run ingest" in step.detail


def test_a_stage_that_already_ran_is_done(tmp_path):
    conn = store(tmp_path)
    with_document(conn)
    assert named(survey(conn), "ingest").state == DONE


def test_the_next_step_after_ingesting_is_extraction(tmp_path):
    conn = store(tmp_path)
    with_document(conn)
    assert survey(conn).next.name == "extract"


def test_a_provider_is_named_where_one_is_needed(tmp_path):
    conn = store(tmp_path)
    with_document(conn)
    assert "--env-file" in survey(conn, has_provider=False).next.command
    assert "--env-file" not in survey(conn, has_provider=True).next.command


# ─────────────────────────  the decisions  ─────────────────────────


def test_a_grey_zone_mention_is_a_decision_and_it_outranks_the_stages(tmp_path):
    """The stages after it would be built on an answer nobody gave."""
    conn = store(tmp_path)
    with_document(conn)
    with_mention(conn)
    typed(conn, zone="grey")

    plan = survey(conn)
    assert plan.next.name == "grey zone" and plan.next.state == WAITING
    assert plan.next.decision


def test_with_no_grey_zone_the_step_is_done(tmp_path):
    conn = store(tmp_path)
    with_document(conn)
    with_mention(conn)
    typed(conn)
    assert named(survey(conn), "grey zone").state == DONE


def test_an_open_review_item_is_a_decision(tmp_path):
    from onto_pipeline import review

    conn = store(tmp_path)
    review.sync(conn, [review.Finding("typo", "c:A", "looks misspelled")],
                version_id="v1", kinds=["typo"])
    assert named(survey(conn), "review").state == WAITING


def test_blocking_lists_only_what_is_a_persons_call(tmp_path):
    conn = store(tmp_path)
    with_document(conn)
    with_mention(conn)
    typed(conn, zone="grey")
    assert [step.name for step in orchestration.blocking(survey(conn))] == ["grey zone"]


# ─────────────────────────  the order the spec gives  ─────────────────────────


def test_induction_is_blocked_until_bridging_ran(tmp_path):
    """Without bridging, every orphan the seed did cover becomes a spurious induced class —
    the false orphan feeding the inductor, which the no-go gate exists to prevent."""
    conn = store(tmp_path)
    with_document(conn)
    with_mention(conn)
    typed(conn, iri=None)

    step = named(survey(conn), "induce")
    assert step.state == BLOCKED and "run bridge first" in step.detail


def test_a_store_missing_a_stages_table_reads_as_never_run(tmp_path):
    """The table belongs to the stage; its absence is the stage not having run, not an error."""
    conn = connect(tmp_path)
    plan = orchestration.survey(conn, "v1", has_provider=True)
    assert named(plan, "branch").state == BLOCKED
