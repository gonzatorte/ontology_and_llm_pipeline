from __future__ import annotations

import pytest

from onto_pipeline.db import connect
from onto_pipeline.ingest import select_for_reload


@pytest.fixture
def conn(tmp_path):
    conn = connect(tmp_path)
    # d1..d4 already processed; d5 and d6 never were
    conn.executemany(
        "INSERT INTO mentions (id, document_id, page, surface_text, status) "
        "VALUES (?, ?, 1, 'x', 'active')",
        [(f"m{i}", f"d{i}") for i in range(1, 5)],
    )
    conn.commit()
    return conn


ALL = [f"d{i}" for i in range(1, 7)]


def select(conn, strategy, sample=0.5, seed=0):
    return select_for_reload(conn, ALL, strategy=strategy, sample=sample, seed=seed)


def test_a_document_never_processed_is_always_processed(conn):
    """The policy governs re-processing, not first processing."""
    for strategy in ("all", "none", "sample"):
        chosen, _ = select(conn, strategy)
        assert {"d5", "d6"} <= set(chosen), strategy


def test_reload_none_leaves_the_processed_ones_alone(conn):
    chosen, skipped = select(conn, "none")
    assert chosen == ["d5", "d6"]
    assert skipped == ["d1", "d2", "d3", "d4"]


def test_reload_all_reprocesses_everything(conn):
    chosen, skipped = select(conn, "all")
    assert sorted(chosen) == ALL and skipped == []


def test_reload_sample_takes_a_fraction_of_the_processed_ones(conn):
    chosen, skipped = select(conn, "sample", sample=0.5)
    assert {"d5", "d6"} <= set(chosen)
    assert len(set(chosen) - {"d5", "d6"}) == 2, "half of the four already processed"
    assert len(skipped) == 2


def test_the_sample_is_seeded_so_the_corpus_is_not_a_moving_target(conn):
    """Unseeded, the accumulation curve of 10.3 could not be read across iterations."""
    first, _ = select(conn, "sample", sample=0.5, seed=7)
    again, _ = select(conn, "sample", sample=0.5, seed=7)
    other, _ = select(conn, "sample", sample=0.5, seed=8)
    assert first == again
    assert first != other or len(ALL) < 4


def test_a_zero_fraction_is_the_same_as_not_reloading(conn):
    assert select(conn, "sample", sample=0.0)[0] == ["d5", "d6"]


def test_an_unknown_strategy_is_refused_rather_than_defaulted(conn):
    with pytest.raises(ValueError, match="unknown reload strategy"):
        select(conn, "sometimes")
