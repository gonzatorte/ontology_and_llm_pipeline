from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from onto_pipeline import calibration
from onto_pipeline.matching import Matcher

KNOWTATOR = """<?xml version="1.0" encoding="UTF-8"?>
<annotations textSource="d1.txt">
  <annotation>
    <mention id="i1" />
    <span start="0" end="6" />
    <spannedText>neuron</spannedText>
  </annotation>
  <annotation>
    <mention id="i2" />
    <span start="11" end="19" />
    <spannedText>platelet</spannedText>
  </annotation>
  <annotation>
    <mention id="i3" />
    <span start="24" end="28" />
    <span start="33" end="37" />
    <spannedText>lens ... cell</spannedText>
  </annotation>
  <classMention id="i1"><mentionClass id="CL:0000540">neuron</mentionClass></classMention>
  <classMention id="i2"><mentionClass id="CL:0000233">platelet</mentionClass></classMention>
  <classMention id="i3"><mentionClass id="CL:0000000">cell</mentionClass></classMention>
</annotations>
"""

TEXT = "neuron and platelet and lens fibre cell"

OBO = """format-version: 1.2

[Term]
id: CL:0000540
name: neuron
def: "A cell that transmits electrical signals." [x]
synonym: "nerve cell" EXACT []

[Term]
id: CL:0000233
name: platelet
def: "A small anucleate cell of the blood." [x]

[Term]
id: CL:0000000
name: cell
def: "A material entity with a plasma membrane." [x]

[Term]
id: CL:9999999
name: obsolete thing
def: "Gone." [x]
is_obsolete: true
"""

EXTENSIONS = """format-version: 1.2

[Term]
id: CL_EXT:lens_fibre
name: lens fibre cell
def: "A cell of the lens." [craft]
"""


def build_pair(tmp_path: Path, **overrides) -> Path:
    directory = tmp_path / "pair"
    (directory / "txt").mkdir(parents=True)
    (directory / "ann").mkdir(parents=True)
    (directory / "txt" / "d1.txt").write_text(TEXT, encoding="utf-8")
    (directory / "ann" / "d1.knowtator.xml").write_text(KNOWTATOR, encoding="utf-8")
    (directory / "cl.obo").write_text(OBO, encoding="utf-8")
    (directory / "ext.obo").write_text(EXTENSIONS, encoding="utf-8")
    body = textwrap.dedent(
        """
        name: tiny
        language: en
        documents:
          dir: txt
        annotations:
          format: knowtator
          dir: ann
          suffix: .knowtator.xml
        ontology:
          files: [cl.obo, ext.obo]
        """
    )
    for key, value in overrides.items():
        body += f"{key}: {value}\n"
    (directory / "pair.yml").write_text(body, encoding="utf-8")
    return directory


class KeywordEncoder:
    """Same stand-in as test_matching: bag of words over a fixed vocabulary, no download."""

    VOCABULARY = ("neuron", "nerve", "platelet", "blood", "cell", "lens", "electrical")

    def encode(self, texts):
        return [
            [1.0 if word in text.lower() else 0.0 for word in self.VOCABULARY] for text in texts
        ]


def load(directory: Path, **kwargs):
    return calibration.load_pair(directory, match_against=kwargs.pop("match_against", "label"),
                                 **kwargs)


def test_the_importer_anchors_offsets_on_the_text_itself(tmp_path):
    """12.2 keeps importers out of v1 because standard offsets index plain text while the
    pipeline's index the parser's Markdown. Skipping the parser removes the objection: the
    text is its own reference, so the spans have to hold against it."""
    from onto_pipeline import annotation

    pair = load(build_pair(tmp_path))
    for document, annotated in zip(pair.documents, pair.annotated_documents(), strict=True):
        annotation.validate(annotated, document.text, document.text_hash)


def test_discontinuous_annotations_are_skipped_and_counted(tmp_path):
    """Flattening a discontinuous span would feed the encoder the text the annotator left
    out. Dropping it silently would understate the corpus."""
    pair = load(build_pair(tmp_path))
    assert [mention.id for mention in pair.documents[0].mentions] == ["i1", "i2"]
    assert pair.documents[0].skipped == 1


def test_in_seed_is_decided_by_the_inventory_not_by_a_human(tmp_path):
    """The whole reason to calibrate on a published corpus: the field that defines the
    false-orphan metric needs no annotation campaign."""
    pair = load(build_pair(tmp_path))
    assert all(mention.in_seed for mention in pair.documents[0].mentions)


def test_obsolete_classes_never_become_targets(tmp_path):
    pair = load(build_pair(tmp_path))
    assert "CL:9999999" not in pair.inventory


def test_the_extension_file_completes_the_release_without_overriding_it(tmp_path):
    """CRAFT annotates with the release plus classes it defines itself. Both must resolve, and
    the release's wording is the one being measured."""
    pair = load(build_pair(tmp_path))
    assert "CL_EXT:lens_fibre" in pair.inventory
    neuron = next(target for target in pair.targets if target.iri == "CL:0000540")
    assert neuron.gloss == "A cell that transmits electrical signals."
    assert neuron.alt_labels == ["nerve cell"]


def test_an_obo_id_and_its_purl_name_the_same_class():
    assert calibration.curie("http://purl.obolibrary.org/obo/CL_0000540") == "CL:0000540"
    assert calibration.curie("CL:0000540") == "CL:0000540"
    assert calibration.curie("CL_EXT:lens_fibre") == "CL_EXT:lens_fibre"


def test_withholding_classes_manufactures_genuine_orphans(tmp_path):
    """A corpus annotated against O has no genuine orphans by construction, so that half of
    the 12.1 gate is untested unless the inventory is cut on purpose."""
    assert not any(
        not mention.in_seed
        for document in load(build_pair(tmp_path / "plain")).documents
        for mention in document.mentions
    )

    withheld = load(build_pair(tmp_path / "cut", holdout_classes="[CL:0000233]"))
    assert withheld.withheld == ["CL:0000233"]
    assert "CL:0000233" not in withheld.inventory
    by_id = {
        mention.gold_class: mention.in_seed
        for document in withheld.documents for mention in document.mentions
    }
    assert by_id == {"CL:0000540": True, "CL:0000233": False}

    # Nothing typed at all: the neuron becomes a false orphan, the withheld platelet a genuine
    # one. Which is the distinction the whole 12.1 gate is read from.
    nothing = calibration.Ranking(best={}, score={}, runner_up={})
    report = calibration.evaluate(withheld, nothing, threshold=0.5)
    assert report.false_orphans == ["i1"] and report.genuine_orphans == ["i2"]


def test_the_holdout_is_deterministic(tmp_path):
    directory = build_pair(tmp_path)
    first = load(directory, holdout=0.5).withheld
    second = load(directory, holdout=0.5).withheld
    assert first == second and first


def test_the_sweep_reuses_one_encoding_pass_across_thresholds(tmp_path):
    """The zones live inside `type_mentions`, which is right for the pipeline and wrong for a
    sweep: re-encoding per threshold would cost hours to recompute a function of scores
    already in hand."""
    pair = load(build_pair(tmp_path))
    matcher = Matcher(KeywordEncoder(), auto_merge_threshold=1.1, grey_zone_lower=0.0)
    ranking = calibration.rank(pair, matcher)
    assert set(ranking.best) == {"i1", "i2"}

    low = calibration.evaluate(pair, ranking, threshold=0.0)
    high = calibration.evaluate(pair, ranking, threshold=1.01)
    assert low.correct and not high.correct
    assert len(high.false_orphans) == 2


def test_a_threshold_above_every_score_turns_hits_into_false_orphans(tmp_path):
    """The failure R1 names, and the reason the metric is split: an aggregate would report the
    same number for a matcher that ranked badly and one whose threshold was misplaced."""
    pair = load(build_pair(tmp_path))
    matcher = Matcher(KeywordEncoder(), auto_merge_threshold=1.1, grey_zone_lower=0.0)
    ranking = calibration.rank(pair, matcher)
    assert calibration.evaluate(pair, ranking, 1.01).false_orphan_rate == 1.0


def test_the_distribution_separates_right_top_one_from_wrong(tmp_path):
    pair = load(build_pair(tmp_path))
    matcher = Matcher(KeywordEncoder(), auto_merge_threshold=1.1, grey_zone_lower=0.0)
    dist = calibration.distribution(pair, calibration.rank(pair, matcher))
    assert dist.correct
    assert isinstance(dist.separation, float)


def test_classes_the_annotators_never_use_leave_the_inventory(tmp_path):
    """CRAFT guarantees a prediction against these is wrong, so keeping them as candidates
    manufactures errors. On CRAFT/CL the class in question is labelled *cell*, identical to the
    extension class the annotators did use, which carries 37% of the corpus."""
    directory = build_pair(tmp_path, excluded_classes="[CL:0000000]")
    pair = load(directory)
    assert pair.excluded_classes == ["CL:0000000"]
    assert "CL:0000000" not in pair.inventory and pair.dropped_excluded


def test_keeping_them_measures_how_much_error_they_cause(tmp_path):
    """The comparison run: precision signal that costs no annotation."""
    directory = build_pair(tmp_path, excluded_classes="[CL:0000000]")
    pair = load(directory, drop_excluded=False)
    assert "CL:0000000" in pair.inventory and not pair.dropped_excluded
    ranking = calibration.Ranking(
        best={"i1": "CL:0000000"}, score={"i1": 0.9}, runner_up={"i1": None}
    )
    assert calibration.excluded_hits(pair, ranking, 0.5) == 1
    assert calibration.excluded_hits(pair, ranking, 0.95) == 0


def test_brat_is_read_too_because_the_other_pairs_use_it(tmp_path):
    text = "the fatigue crack grew"
    path = tmp_path / "d.ann"
    path.write_text(
        "T1\tCrack 12 17\tcrack\nT2\tThing 4 11;12 17\tfatigue crack\n#1\tNote T1\tx\n",
        encoding="utf-8",
    )
    mentions, skipped = calibration.read_brat(path, text)
    assert [(m.gold_class, m.text) for m in mentions] == [("Crack", "crack")]
    assert skipped == 1


def test_an_unreadable_format_fails_loudly(tmp_path):
    directory = build_pair(tmp_path)
    (directory / "pair.yml").write_text(
        (directory / "pair.yml").read_text().replace("knowtator", "inception"), encoding="utf-8"
    )
    with pytest.raises(calibration.UnknownFormat):
        load(directory)


def test_a_pair_pointing_at_nothing_says_so(tmp_path):
    directory = build_pair(tmp_path)
    (directory / "pair.yml").write_text(
        (directory / "pair.yml").read_text().replace("dir: txt", "dir: missing"), encoding="utf-8"
    )
    with pytest.raises(calibration.PairIncomplete):
        load(directory)
