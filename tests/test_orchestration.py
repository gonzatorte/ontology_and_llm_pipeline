from __future__ import annotations

from onto_pipeline import orchestration
from onto_pipeline.db import connect
from onto_pipeline.orchestration import BLOCKED, DONE, READY, WAITING

SESSION = "test-1"


def store(tmp_path):
    conn = connect(tmp_path)
    from onto_pipeline import review, typing_store

    typing_store.install(conn)
    review.install(conn)
    return conn


def survey(conn, has_provider=True):
    return orchestration.survey(conn, "v1", has_provider=has_provider, session_id=SESSION)


def named(plan, name):
    return next(step for step in plan.steps if step.name == name)


def with_document(conn, blocks=1):
    conn.execute(
        "INSERT INTO documents (id, session_id, held_out) VALUES ('d1', ?, 0)", (SESSION,)
    )
    for index in range(blocks):
        conn.execute(
            "INSERT INTO blocks (id, session_id, document_id, page, ordinal, block_type, text) "
            "VALUES (?, ?, 'd1', 1, ?, 'paragraph', 'text')",
            (f"b{index}", SESSION, index),
        )
    conn.commit()


def with_mention(conn, mention_id="m1"):
    conn.execute(
        "INSERT INTO mentions (id, session_id, document_id, page, surface_text, status) "
        "VALUES (?, ?, 'd1', 1, 'focus group', 'extracted')", (mention_id, SESSION),
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
    plan = orchestration.survey(conn, "v1", has_provider=True, session_id=SESSION)
    assert named(plan, "branch").state == BLOCKED


# ─────────────────────────  `--run`, una etapa y no más  ─────────────────────────


def test_a_decision_is_never_runnable():
    """Correrla sería decidirla por default, que es lo que este comando existe para no hacer."""
    step = orchestration.Step("grey zone", "onto-pipeline grey list", WAITING, decision=True)
    assert not orchestration.runnable(step)


def test_a_blocked_stage_is_not_runnable_either():
    step = orchestration.Step("induce", "onto-pipeline induce", BLOCKED)
    assert not orchestration.runnable(step)


def test_a_ready_stage_is():
    assert orchestration.runnable(orchestration.Step("ingest", "onto-pipeline ingest", READY))


def test_nothing_pending_is_not_runnable():
    assert not orchestration.runnable(None)


def test_the_reader_note_never_becomes_an_argument():
    """`(needs a provider: --env-file)` entraría como cuatro banderas inventadas."""
    from pathlib import Path

    step = orchestration.Step(
        "extract", "onto-pipeline extract (needs a provider: --env-file)", READY
    )

    argv = orchestration.command_line(step, Path("config/x.yaml"))
    assert argv == ["onto-pipeline", "extract", "--config", "config/x.yaml"]


def test_the_env_file_goes_before_the_subcommand():
    """Es la única vía: el callback de la app lo carga antes de que el subcomando arranque."""
    from pathlib import Path

    step = orchestration.Step("extract", "onto-pipeline extract", READY)
    argv = orchestration.command_line(step, Path("c.yaml"), Path("opencode.env"))
    assert argv[:3] == ["onto-pipeline", "--env-file", "opencode.env"]


def test_what_runs_is_what_was_printed():
    """Si `command` y lo ejecutado divergieran, el usuario vería una cosa y correría otra."""
    from pathlib import Path

    step = orchestration.Step("match", "onto-pipeline match", READY)
    assert "match" in orchestration.command_line(step, Path("c.yaml"))
