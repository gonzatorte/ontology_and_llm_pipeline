from __future__ import annotations

import pytest

from onto_pipeline import matching
from onto_pipeline.matching import Matcher, Mention, Target


class KeywordEncoder:
    """Deterministic stand-in for the multilingual bi-encoder: a bag-of-words vector over a
    fixed vocabulary. Enough to exercise retrieval and the zones without a model download."""

    VOCABULARY = ("interview", "entrevista", "technique", "procedure", "survey", "subject",
                  "questioning", "informant", "policy", "commercialization")

    def encode(self, texts):
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append([1.0 if word in lowered else 0.0 for word in self.VOCABULARY])
        return vectors


@pytest.fixture
def matcher():
    return Matcher(KeywordEncoder(), auto_merge_threshold=0.92, grey_zone_lower=0.70)


# Explicitly gloss-matching: these exercise the retrieval path, and the default moved to the
# label once measurement showed glosses lose with a symmetric encoder.
TARGETS = [
    Target(iri="c:Technique", label="Technique", match_against="gloss",
           gloss="A procedure applied within a methodological strategy."),
    Target(iri="c:Subject", label="Subject", match_against="gloss",
           gloss="A subject studied by a methodological strategy."),
    Target(iri="c:Policy", label="Policy", match_against="gloss",
           gloss="A policy on commercialization."),
]


def mention(identifier, text, document="d1", language="en", **kwargs):
    return Mention(id=identifier, text=text, document_id=document, language=language, **kwargs)


def test_typing_can_match_against_the_gloss(matcher):
    """A mention whose words appear in the definition and not in the label still types, which
    is the case the spec's gloss premise is meant to cover."""
    typing = matcher.type_mentions([mention("m1", "a procedure we applied")], TARGETS)[0]
    assert typing.iri == "c:Technique"
    assert typing.zone == matching.AUTO


def test_a_mention_nothing_covers_is_an_orphan(matcher):
    typing = matcher.type_mentions([mention("m1", "open access repository")], TARGETS)[0]
    assert typing.iri is None and typing.zone == matching.DISCARDED


def test_the_grey_zone_exists_so_the_low_zone_can_be_dropped_silently():
    """Two zones would mean asking about everything not obviously identical, which is the
    mass manual review the design avoids."""
    matcher = Matcher(KeywordEncoder(), auto_merge_threshold=0.99, grey_zone_lower=0.5)
    typing = matcher.type_mentions([mention("m1", "an interview procedure")], TARGETS)[0]
    assert typing.zone == matching.GREY and typing.iri == "c:Technique"


def test_the_cross_encoder_reranks_the_bi_encoders_shortlist(matcher):
    class Contrarian:
        def score(self, pairs):
            # Prefers whichever target mentions policy, regardless of retrieval order.
            return [1.0 if "policy" in target.lower() else 0.1 for _, target in pairs]

    class Contrarian2:
        def score(self, pairs):
            return [0.95 if "policy" in target.lower() else 0.1 for _, target in pairs]

    reranked = Matcher(KeywordEncoder(), Contrarian2()).type_mentions(
        [mention("m1", "a procedure we applied")], TARGETS
    )[0]
    assert reranked.iri == "c:Policy", "the re-ranker has the last word on which wins"
    assert reranked.zone == matching.AUTO, "and on the score the zone is read from"


def test_a_target_without_a_gloss_has_only_its_label():
    """Glosses arrive in A0.4 and improve in B4b; until then the label is what there is."""
    target = Target(iri="c:Technique", label="Technique")
    assert target.text == "Technique" and not target.grounded_in_gloss


def test_identical_proper_names_of_the_same_class_merge_automatically(matcher):
    decisions = matcher.resolve(
        [mention("m1", "Genome Canada", "d1"), mention("m2", "Genome Canada", "d2")],
        inferred_class={"m1": "c:Institution", "m2": "c:Institution"},
    )
    assert [(d.action, d.reason) for d in decisions] == [
        (matching.MERGE, "identical_proper_name")
    ]


def test_the_same_name_under_a_different_class_is_asked_about(matcher):
    decisions = matcher.resolve(
        [mention("m1", "Genome Canada", "d1"), mention("m2", "Genome Canada", "d2")],
        inferred_class={"m1": "c:Institution", "m2": "c:Policy"},
    )
    assert decisions[0].action == matching.ASK


def test_cross_language_pairs_are_always_grey(matcher):
    decisions = matcher.resolve([
        mention("m1", "Interview", "d1", language="en"),
        mention("m2", "Interview", "d2", language="es"),
    ])
    assert (decisions[0].action, decisions[0].reason) == (matching.ASK, "cross_language")


def test_a_declared_synonym_merges(matcher):
    decisions = matcher.resolve(
        [mention("m1", "Interview", "d1"), mention("m2", "Interviewing", "d2")],
        synonyms={"interview": {"interviewing"}},
    )
    assert decisions[0].reason == "synonym_declared_in_seed"


def test_a_generic_phrase_separates_without_asking(matcher):
    """It says nothing about identity across documents, and asking would be the review the
    design exists to avoid."""
    decisions = matcher.resolve(
        [mention("m1", "the system", "d1"), mention("m2", "the system", "d2")]
    )
    assert (decisions[0].action, decisions[0].reason) == (
        matching.SEPARATE, "generic_phrase_cross_document"
    )


def test_a_declared_key_outranks_the_similarity_thresholds(matcher):
    """The ontology stating what identity means for a class beats any embedding score."""
    left = mention("m1", "Genome Canada", "d1", key_values={"p:regNo": "A-1"})
    right = mention("m2", "Genome Canada", "d2", key_values={"p:regNo": "B-2"})
    decisions = matcher.resolve(
        [left, right],
        keys={"c:Institution": ["p:regNo"]},
        inferred_class={"m1": "c:Institution", "m2": "c:Institution"},
    )
    assert (decisions[0].action, decisions[0].reason) == (
        matching.SEPARATE, "declared_key_differs"
    ), "identical names would otherwise have merged"


def test_a_matching_declared_key_merges_across_differing_surface_forms(matcher):
    decisions = matcher.resolve(
        [mention("m1", "Genome Canada", "d1", key_values={"p:regNo": "A-1"}),
         mention("m2", "Genome Cda.", "d2", key_values={"p:regNo": "A-1"})],
        keys={"c:Institution": ["p:regNo"]},
        inferred_class={"m1": "c:Institution", "m2": "c:Institution"},
    )
    assert decisions[0].reason == "declared_key_matches"


def test_intra_document_pairs_are_not_compared(matcher):
    """Anaphora is resolved in B1b with mention ids; it never reaches cross-document linking."""
    assert matcher.resolve([
        mention("m1", "Genome Canada", "d1"), mention("m2", "Genome Canada", "d1")
    ]) == []


def test_a_declared_key_also_outranks_the_blocking(matcher):
    """Blocking on surface form alone would silently overrule the key: differently written
    mentions of one entity never meet, so the key that identifies them is never asked."""
    blocks = matcher.blocks([
        mention("m1", "Genome Canada", "d1", key_values={"p:regNo": "A-1"}),
        mention("m2", "Genome Cda.", "d2", key_values={"p:regNo": "A-1"}),
    ])
    assert any(sorted(m.id for m in block) == ["m1", "m2"] for block in blocks)


def test_blocking_keeps_unrelated_mentions_from_being_compared(matcher):
    blocks = matcher.blocks([
        mention("m1", "Interview", "d1"), mention("m2", "Interviewing", "d2"),
        mention("m3", "Commercialization", "d3"),
    ])
    assert [sorted(m.id for m in block) for block in blocks] == [["m1", "m2"]]


def test_grey_zone_pairs_become_unresolved_duplicates(matcher):
    decisions = matcher.resolve([
        mention("m1", "Interview", "d1", language="en"),
        mention("m2", "Interview", "d2", language="es"),
    ])
    assert matching.unresolved_duplicates(decisions) == {"m1", "m2"}


def test_an_uncalibrated_reranker_turns_matches_into_false_orphans():
    """Why the cross-encoder is off by default. Measured on the real seed: mmarco ranked
    `semi-structured interview` correctly but squashed every score to ~0.01, dropping a match
    the bi-encoder had at 0.73 into the discard zone. R1, manufactured by the matcher."""

    class Flattening:
        def score(self, pairs):
            return [0.01 if "procedure" in target.lower() else 0.001 for _, target in pairs]

    bi_only = Matcher(KeywordEncoder())
    with_reranker = Matcher(KeywordEncoder(), Flattening())
    subject = [mention("m1", "a procedure we applied")]

    assert bi_only.type_mentions(subject, TARGETS)[0].zone == matching.AUTO
    assert with_reranker.type_mentions(subject, TARGETS)[0].zone == matching.DISCARDED


def test_what_a_mention_is_compared_against_is_configurable():
    """The spec matches against the gloss; measured on the real seed the label wins, so the
    default moved and the choice became explicit rather than hard-coded."""
    target = Target(iri="c:T", label="Technique", gloss="A systematic procedure.")
    assert target.text == "Technique"
    assert Target(iri="c:T", label="Technique", gloss="A systematic procedure.",
                  match_against="gloss").text == "A systematic procedure."
    assert Target(iri="c:T", label="Technique", gloss="A systematic procedure.",
                  match_against="label_and_gloss").text == "Technique: A systematic procedure."


def test_a_target_with_no_gloss_falls_back_to_the_label_whatever_the_setting():
    assert Target(iri="c:T", label="Technique", match_against="gloss").text == "Technique"


def test_the_config_refuses_a_blocking_strategy_that_is_not_built():
    """The thresholds are calibrated against these numbers: a pair the blocking never formed
    must not be silently indistinguishable from one the encoder scored too low."""
    from pydantic import ValidationError

    from onto_pipeline.config import Matching

    assert Matching().blocking_strategy == "surface_and_keys"
    with pytest.raises(ValidationError, match="not implemented"):
        Matching(blocking_strategy="embedding")
