from __future__ import annotations

import pytest

from onto_pipeline import coreference
from onto_pipeline.db import connect

SESSION = "test-1"

MARKDOWN = "The system stores data. The platform indexes it. A second system exists."


def at(surface, occurrence=0):
    """Offsets computed from the text rather than written by hand, so the fixture cannot be
    the thing that is wrong."""
    start = -1
    for _ in range(occurrence + 1):
        start = MARKDOWN.index(surface, start + 1)
    return start, start + len(surface)


def mentions():
    return [
        {"id": "m1", "span_start": at("system")[0], "span_end": at("system")[1]},
        {"id": "m2", "span_start": at("platform")[0], "span_end": at("platform")[1]},
        {"id": "m3", "span_start": at("system", 1)[0], "span_end": at("system", 1)[1]},
    ]


def test_markers_are_inserted_without_shifting_the_offsets_of_earlier_ones():
    marked = coreference.mark(MARKDOWN, mentions())
    assert "The system[M0] stores" in marked.text
    assert "The platform[M1] indexes" in marked.text
    assert "second system[M2] exists" in marked.text
    assert marked.order == ["m1", "m2", "m3"]


def test_markers_follow_document_order_not_input_order():
    shuffled = list(reversed(mentions()))
    assert coreference.mark(MARKDOWN, shuffled).order == ["m1", "m2", "m3"]


def test_parse_rejects_an_answer_that_is_not_json():
    with pytest.raises(ValueError, match="no JSON"):
        coreference.parse("I grouped them for you.", {})


def test_parse_rejects_an_answer_with_no_groups():
    with pytest.raises(ValueError, match="groups"):
        coreference.parse('{"result": []}', {})


def test_markers_translate_back_to_mention_ids():
    marked = coreference.mark(MARKDOWN, mentions())
    grouping = coreference.resolve(marked, [["M0", "M1"]])
    assert grouping.groups == [["m1", "m2"]]
    assert grouping.assignments == {"m1": "g0", "m2": "g0"}


def test_a_marker_that_does_not_exist_is_rejected_not_guessed():
    """The reason for asking about identifiers instead of spans: this is checkable."""
    marked = coreference.mark(MARKDOWN, mentions())
    grouping = coreference.resolve(marked, [["M0", "M99"], ["M0", "banana"]])
    assert grouping.unknown_markers == ["M99", "banana"]
    assert grouping.groups == []


def test_a_marker_claimed_by_two_groups_is_caught():
    marked = coreference.mark(MARKDOWN, mentions())
    grouping = coreference.resolve(marked, [["M0", "M1"], ["M0", "M2"]])
    assert grouping.duplicated_markers == ["M0"]
    assert grouping.groups == [["m1", "m2"]], "the first group keeps it, the second loses it"


def test_a_group_of_one_links_nothing():
    """A mention on its own is the normal case, not a coreference chain."""
    marked = coreference.mark(MARKDOWN, mentions())
    assert coreference.resolve(marked, [["M0"], ["M1", "M2"]]).groups == [["m2", "m3"]]


def test_groups_persist_onto_the_mention_layer(tmp_path):
    conn = connect(tmp_path)
    conn.executemany(
        "INSERT INTO mentions (id, session_id, document_id, page, surface_text, status) "
        "VALUES (?, ?, 'doc', 1, ?, 'active')",
        [("m1", SESSION, "system"), ("m2", SESSION, "platform"),
         ("m3", SESSION, "system")],
    )

    marked = coreference.mark(MARKDOWN, mentions())
    grouping = coreference.resolve(marked, [["M0", "M1"]])
    coreference.persist(conn, grouping.assignments, session_id=SESSION)

    rows = dict(conn.execute("SELECT id, coref_group FROM mentions"))
    assert rows == {"m1": "g0", "m2": "g0", "m3": None}


def test_the_prompt_says_same_kind_is_not_same_thing():
    """The failure mode that would collapse every interview in a paper into one individual."""
    rendered = coreference.PROMPT.render(**coreference.payload(coreference.Marked(text="x")))
    assert "Same *kind* is not the same *thing*" in rendered
