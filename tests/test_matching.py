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


def compared(matcher, mentions, **kwargs):
    return sorted(
        sorted((left.id, right.id)) for left, right in matcher.candidate_pairs(mentions, **kwargs)
    )


def test_a_declared_key_also_outranks_candidate_generation(matcher):
    """A key that identifies two differently written mentions is never asked if the two never
    meet, so the key has to reach candidate generation, not only the decision."""
    pairs = compared(matcher, [
        mention("m1", "Genome Canada", "d1", key_values={"p:regNo": "A-1"}),
        mention("m2", "Genome Cda.", "d2", key_values={"p:regNo": "A-1"}),
    ])
    assert pairs == [["m1", "m2"]], "no encoder puts these two together"


def test_unrelated_mentions_are_not_compared(matcher):
    pairs = compared(matcher, [
        mention("m1", "Interview", "d1"), mention("m2", "Interviewing", "d2"),
        mention("m3", "Commercialization", "d3"),
    ])
    assert pairs == [["m1", "m2"]]


def test_the_same_surface_is_a_candidate_without_consulting_the_encoder(matcher):
    """`identical_proper_name` decides on exact text; its pairs must not depend on a score."""
    pairs = compared(matcher, [
        mention("m1", "Genome Canada", "d1"), mention("m2", "Genome Canada", "d2"),
    ])
    assert pairs == [["m1", "m2"]], "the stand-in encoder scores these at zero"


def test_a_declared_synonym_reaches_candidate_generation(matcher):
    """`GT` and `grounded theory` are the same concept by assertion and would not survive a
    cosine cut."""
    pairs = compared(
        matcher,
        [mention("m1", "GT", "d1"), mention("m2", "grounded theory", "d2")],
        synonyms={"gt": {"grounded theory"}, "grounded theory": {"gt"}},
    )
    assert pairs == [["m1", "m2"]]


def test_the_surface_strategy_stays_available_and_blocks_as_it_did():
    matcher = Matcher(KeywordEncoder(), blocking_strategy=matching.SURFACE_AND_KEYS)
    pairs = compared(matcher, [
        mention("m1", "in-depth interview", "d1"),
        mention("m2", "semi-structured interview", "d2"),
    ])
    assert pairs == [], "the heuristic this replaces splits these two"


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
    """The thresholds are calibrated against these numbers: a pair that was never formed must
    not be silently indistinguishable from one the encoder scored too low."""
    from pydantic import ValidationError

    from onto_pipeline.config import Matching

    assert Matching().blocking_strategy == matching.EMBEDDING
    with pytest.raises(ValidationError, match="not implemented"):
        Matching(blocking_strategy="fuzzy")


def test_declared_synonyms_are_indexed_in_both_directions():
    targets = [
        Target(iri="urn:i", label="Interview", alt_labels=["Entrevista", "guided conversation"]),
    ]
    index = matching.synonym_index(targets)

    assert "entrevista" in index["interview"]
    assert "interview" in index["entrevista"]
    assert "guided conversation" in index["entrevista"]


def test_the_synonym_index_does_not_join_two_unrelated_classes():
    targets = [
        Target(iri="urn:i", label="Interview", alt_labels=["Entrevista"]),
        Target(iri="urn:o", label="Observation", alt_labels=["Observación"]),
    ]
    index = matching.synonym_index(targets)

    assert "observation" not in index["interview"]


class RandomEncoder:
    """Dense vectors, so the two ranking paths are compared on something without ties."""

    def __init__(self, dimensions: int = 16, seed: int = 0) -> None:
        import random

        self.random = random.Random(seed)
        self.dimensions = dimensions

    def encode(self, texts):
        return [[self.random.random() for _ in range(self.dimensions)] for _ in texts]


def test_the_numpy_and_python_ranking_paths_agree(monkeypatch):
    """Typing against a real inventory is 30 million dot products, so it runs through numpy.
    A pipeline run and a calibration run on the same install must not disagree about which
    class won just because one of them found numpy."""
    import builtins

    targets = [Target(iri=f"c:{index:03d}", label=f"label {index}") for index in range(200)]
    mentions = [mention(f"m{index}", f"text {index}") for index in range(40)]
    matcher = Matcher(RandomEncoder(), auto_merge_threshold=1.1, grey_zone_lower=0.0)
    target_vectors = matcher.vectors_for([target.text for target in targets])
    mention_vectors = matcher.vectors_for([m.text for m in mentions])

    with_numpy = matcher._rank(mention_vectors, target_vectors, targets, 5)

    real_import = builtins.__import__

    def without_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("numpy is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_numpy)
    in_python = matcher._rank(mention_vectors, target_vectors, targets, 5)

    assert [[t.iri for _, t in row] for row in with_numpy] == \
           [[t.iri for _, t in row] for row in in_python]
    # float32 against the interpreter's float64: close, not identical, and far below any zone.
    assert all(
        abs(left - right) < 1e-5
        for fast, slow in zip(with_numpy, in_python, strict=True)
        for (left, _), (right, _) in zip(fast, slow, strict=True)
    )


def test_top_k_larger_than_the_inventory_is_not_an_error(matcher):
    """The seed has thirty-four classes and top_k defaults to five, but a subset ontology or a
    withheld inventory can be smaller than k."""
    typings = matcher.type_mentions([mention("m1", "a procedure we applied")], TARGETS, top_k=99)
    assert typings[0].iri == "c:Technique"
