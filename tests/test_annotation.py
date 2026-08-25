from __future__ import annotations

import json

import pytest

from onto_pipeline import annotation

MARKDOWN = "Empirical legal research applies document analysis to court rulings."
ENTRY = {
    "doc_id": "chin_zeiler_2021",
    "markdown_hash": "sha256:abc",
    "mentions": [
        {"id": "m1", "page": 3, "span": [0, 25], "text": "Empirical legal research",
         "gold_class": "ResearchField", "in_seed": False, "entity_id": "e7"},
        {"id": "m2", "page": 3, "span": [33, 51], "text": "document analysis",
         "gold_class": "Technique", "in_seed": True, "entity_id": "e12"},
        {"id": "m3", "page": 3, "span": [55, 68], "text": "court rulings",
         "gold_class": None, "in_seed": False},
    ],
    "relations": [
        {"subject": "e7", "predicate": "hasMethodology", "object": "e12", "evidence_page": 3}
    ],
}


@pytest.fixture
def document(tmp_path):
    path = tmp_path / "retention.jsonl"
    path.write_text(json.dumps(ENTRY) + "\n", encoding="utf-8")
    return annotation.read_jsonl(path)[0]


def test_the_annotation_round_trips(document):
    assert document.doc_id == "chin_zeiler_2021"
    assert [mention.id for mention in document.mentions] == ["m1", "m2", "m3"]
    assert document.mentions[1].in_seed is True
    assert document.mentions[2].gold_class is None
    assert document.relations[0].predicate == "hasMethodology"


def test_a_changed_parser_is_refused_rather_than_misaligned(document):
    """R9: standard formats anchor on plain text, these offsets are into the parser's
    Markdown, and a parser version change shifts them."""
    with pytest.raises(annotation.OffsetMismatch, match="Markdown on disk"):
        annotation.validate(document, MARKDOWN, "sha256:different")


def test_a_shifted_span_is_caught(document):
    with pytest.raises(annotation.OffsetMismatch, match="span"):
        annotation.validate(document, "X" + MARKDOWN, "sha256:abc")


def test_false_and_genuine_orphans_are_counted_apart(document):
    """Aggregated, the orphan rate says nothing: one is an error, the other is the pipeline
    working as designed."""
    report = annotation.score(document, {"m1": None, "m2": None})
    assert report.genuine_orphans == ["m1"]  # the seed does not cover it; this feeds B3
    assert report.false_orphans == ["m2"]  # the class existed and the matcher missed it
    assert report.false_orphan_rate == 1.0


def test_a_mention_with_no_assignable_class_is_not_the_matchers_failure(document):
    report = annotation.score(document, {"m1": "ResearchField", "m2": "Technique", "m3": None})
    assert "m3" not in report.false_orphans + report.genuine_orphans
    assert report.correct == ["m1", "m2"]
    assert report.false_orphan_rate == 0.0
    assert report.typing_f1 == 1.0


def test_a_wrong_class_is_mistyped_not_an_orphan(document):
    report = annotation.score(document, {"m1": "ResearchField", "m2": "Subject"})
    assert report.mistyped == ["m2"] and not report.false_orphans
    assert report.typing_f1 == pytest.approx(0.5)


def test_brat_export_keeps_types_relations_and_coreference(document, tmp_path):
    paths = annotation.export_brat(document, MARKDOWN, tmp_path / "brat")
    ann = next(path for path in paths if path.suffix == ".ann").read_text()

    assert "T1\tResearchField 0 25\tEmpirical legal research" in ann
    assert "T3\tUnassigned 55 68\tcourt rulings" in ann
    assert "R1\thasMethodology Arg1:T1 Arg2:T2" in ann
    assert "A1\tInSeed T2" in ann, "in_seed survives only as an ad-hoc attribute"
    assert next(path for path in paths if path.suffix == ".txt").read_text() == MARKDOWN


def test_the_export_carries_the_hash_its_offsets_belong_to(document, tmp_path):
    paths = annotation.export_brat(document, MARKDOWN, tmp_path / "brat")
    carried = next(path for path in paths if path.name.endswith("markdown_hash"))
    assert carried.read_text().strip() == "sha256:abc"


def test_coreference_chains_are_exported_as_equivalence_groups(tmp_path):
    entry = json.loads(json.dumps(ENTRY))
    entry["mentions"][1]["entity_id"] = "e7"
    path = tmp_path / "coref.jsonl"
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    document = annotation.read_jsonl(path)[0]

    paths = annotation.export_brat(document, MARKDOWN, tmp_path / "brat")
    ann = next(item for item in paths if item.suffix == ".ann").read_text()
    assert "*\tCoreference T1 T2" in ann
