from __future__ import annotations

from onto_pipeline import stopping
from onto_pipeline.db import connect
from onto_pipeline.stopping import Point

SESSION = "test-1"


def curve(*new: int) -> list[Point]:
    points, total = [], 0
    for index, count in enumerate(new, start=1):
        total += count
        points.append(Point(index, f"d{index}", count, total))
    return points


def seed(conn, documents: list[str], typings: dict[str, tuple[str, str]]):
    """`typings` maps a mention id to (document, class IRI)."""
    for document in documents:
        conn.execute(
            "INSERT INTO documents (id, session_id, held_out) VALUES (?, ?, 0)",
            (document, SESSION),
        )
    for mention_id, (document, iri) in typings.items():
        conn.execute(
            "INSERT INTO mentions (id, session_id, document_id, page, surface_text, status) "
            "VALUES (?, ?, ?, 1, 'x', 'typed')", (mention_id, SESSION, document),
        )
        conn.execute(
            "INSERT INTO mention_typing (mention_id, version_id, iri, score, zone) "
            "VALUES (?, 'v1', ?, 0.99, 'auto')", (mention_id, iri),
        )
    conn.commit()


# ─────────────────────────  the curve  ─────────────────────────


def test_a_concept_seen_again_adds_nothing(tmp_path):
    conn = connect(tmp_path)
    from onto_pipeline import typing_store

    typing_store.install(conn)
    seed(conn, ["d1", "d2"], {"m1": ("d1", "c:A"), "m2": ("d2", "c:A"), "m3": ("d2", "c:B")})
    points = stopping.accumulation(
        stopping.processing_order(conn,
            session_id=SESSION), stopping.concepts_by_document(conn, "v1", session_id=SESSION)
    )
    assert [(p.new, p.cumulative) for p in points] == [(1, 1), (1, 2)]


def test_a_held_out_document_is_not_part_of_the_curve(tmp_path):
    conn = connect(tmp_path)
    from onto_pipeline import typing_store

    typing_store.install(conn)
    seed(conn, ["d1"], {"m1": ("d1", "c:A")})
    conn.execute(
        "INSERT INTO documents (id, session_id, held_out) VALUES ('d2', ?, 1)", (SESSION,)
    )
    conn.commit()
    assert [p.document_id for p in stopping.accumulation(
        stopping.processing_order(conn,
            session_id=SESSION), stopping.concepts_by_document(conn, "v1", session_id=SESSION)
    )] == ["d1"]


def test_the_order_is_the_one_the_process_used(tmp_path):
    """Sorting by id would draw a curve for a process that never happened."""
    conn = connect(tmp_path)
    for document in ("zebra", "alpha"):
        conn.execute(
            "INSERT INTO documents (id, session_id, held_out) VALUES (?, ?, 0)",
            (document, SESSION),
        )
    conn.commit()
    assert stopping.processing_order(conn, session_id=SESSION) == ["zebra", "alpha"]


def test_an_induced_concept_counts_even_though_no_class_exists_yet(tmp_path):
    """Counting only typed classes would call a document that introduced three genuinely new
    concepts empty, which is the opposite of what the curve is for."""
    conn = connect(tmp_path)
    from onto_pipeline import induction, typing_store

    typing_store.install(conn)
    seed(conn, ["d1"], {"m1": ("d1", "c:A")})
    induction.persist(conn, "v1", [
        induction.Proposal("c1", "Data Repository", "g", "criterion", 1, ["m1"])
    ])
    assert len(stopping.concepts_by_document(conn, "v1", session_id=SESSION)["d1"]) == 2


def test_a_tail_shorter_than_the_window_is_unknown_not_zero():
    """A flat curve and no curve look identical in a number and mean opposite things."""
    assert stopping.tail_slope(curve(3, 2), window=5) is None


def test_the_tail_slope_is_new_concepts_per_document():
    assert stopping.tail_slope(curve(9, 9, 2, 1, 0), window=3) == 1.0


# ─────────────────────────  the four criteria  ─────────────────────────


def assess(conn, *, iteration=1, max_iterations=20, window=3, threshold=1.0, target=0.9):
    return stopping.assess(
        conn, "v1", target_pass_rate=target, novelty_window=window,
        novelty_threshold=threshold, iteration=iteration, max_iterations=max_iterations,
            session_id=SESSION,
    )


def named(assessment, name):
    return next(c for c in assessment.criteria if c.name == name)


def prepared(tmp_path, documents: int, new_per_document: int):
    conn = connect(tmp_path)
    from onto_pipeline import typing_store

    typing_store.install(conn)
    typings, docs = {}, []
    for index in range(documents):
        document = f"d{index}"
        docs.append(document)
        for offset in range(new_per_document):
            typings[f"m{index}_{offset}"] = (document, f"c:C{index}_{offset}")
    seed(conn, docs, typings)
    return conn


def test_no_cq_evaluated_leaves_the_primary_criterion_silent(tmp_path):
    criterion = named(assess(prepared(tmp_path, 1, 1)), "competency questions")
    assert criterion.state == stopping.UNKNOWN and not criterion.stops


def test_the_primary_criterion_needs_the_rate_to_have_settled(tmp_path):
    conn = prepared(tmp_path, 1, 1)
    from onto_pipeline import cq

    cq.install(conn)
    for iteration, passed in enumerate([1, 1, 1]):
        conn.execute(
            "INSERT INTO cq_results (cq_id, session_id, iteration, passed) "
            "VALUES (?, ?, ?, ?)",
            (f"q{iteration}", SESSION, iteration, passed),
        )

    conn.commit()
    assessment = assess(conn)
    assert named(assessment, "competency questions").state == stopping.MET
    assert assessment.stop


def test_saturation_stops_and_says_what_it_saturated_against(tmp_path):
    criterion = named(assess(prepared(tmp_path, 5, 0), threshold=1.0), "novelty saturation")
    assert criterion.state == stopping.MET
    assert "not the same as with respect to the domain" in criterion.note


def test_a_curve_still_climbing_says_the_corpus_is_the_problem(tmp_path):
    criterion = named(assess(prepared(tmp_path, 8, 3)), "accumulation curve")
    assert criterion.state == stopping.NOT_MET
    assert "the corpus is insufficient" in criterion.note


def test_a_curve_with_only_one_window_has_no_shape(tmp_path):
    """The tail *is* the beginning, and comparing them compares a number with itself — which
    reads as a flat curve and is not one."""
    criterion = named(assess(prepared(tmp_path, 4, 3), window=3), "accumulation curve")
    assert criterion.state == stopping.UNKNOWN and "too few to see a shape" in criterion.note


def test_a_diagnostic_never_stops_anything(tmp_path):
    """However it reads. It is the only criterion that says where the problem is, and none of
    that is a reason to terminate."""
    conn = prepared(tmp_path, 8, 0)
    assessment = assess(conn, threshold=0.0)
    curve_criterion = named(assessment, "accumulation curve")
    assert curve_criterion.state == stopping.MET and not curve_criterion.stops


def test_the_budget_is_the_one_that_always_terminates(tmp_path):
    assessment = assess(prepared(tmp_path, 1, 1), iteration=20, max_iterations=20)
    assert named(assessment, "budget").stops and assessment.stop
    assert assessment.reasons == ["budget (hard)"]


def test_mention_coverage_is_not_among_the_criteria(tmp_path):
    """A diagnostic and never a target: a system optimizes what is measured, and an umbrella
    class maximizes coverage while destroying the conceptual value."""
    names = {c.name for c in assess(prepared(tmp_path, 1, 1)).criteria}
    assert not any("coverage" in name for name in names)
    assert len(names) == 4
