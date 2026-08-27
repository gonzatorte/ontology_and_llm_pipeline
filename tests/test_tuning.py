from __future__ import annotations

from dataclasses import dataclass

from onto_pipeline import tuning


@dataclass
class FakeMention:
    id: str
    text: str
    gold_class: str


@dataclass
class FakeDocument:
    doc_id: str


TEXTS = {"c:A": "alpha", "c:B": "beta", "c:C": "gamma", "c:D": "delta"}


# ─────────────────────────  la partición  ─────────────────────────


def test_the_split_is_by_document_and_deterministic():
    """Separar menciones al azar deja las del mismo paper de los dos lados: comparten
    vocabulario y tema, y eso mide memoria."""
    docs = [FakeDocument(f"d{i}") for i in range(10)]
    first = tuning.split_by_document(docs, 0.8)
    again = tuning.split_by_document(docs, 0.8)
    assert [d.doc_id for d in first[0]] == [d.doc_id for d in again[0]]
    assert len(first[0]) == 8 and len(first[1]) == 2


def test_neither_side_is_ever_empty():
    """Con cuatro documentos el redondeo puede dejar la evaluación vacía, y entonces el número
    no tiene conjunto de prueba."""
    train, evaluate = tuning.split_by_document([FakeDocument(f"d{i}") for i in range(4)], 0.99)
    assert train and evaluate


def test_a_document_is_never_on_both_sides():
    docs = [FakeDocument(f"d{i}") for i in range(20)]
    train, evaluate = tuning.split_by_document(docs, 0.7)
    assert not ({d.doc_id for d in train} & {d.doc_id for d in evaluate})


# ─────────────────────────  los ejemplos  ─────────────────────────


def test_negatives_come_from_what_the_retriever_actually_offered():
    """Un negativo al azar es una clase que el recuperador nunca iba a proponer: entrenar
    contra eso enseña a distinguir lo que ya estaba distinguido."""
    mentions = [FakeMention("m1", "una mención", "c:A")]
    examples = tuning.examples_from(mentions, [["c:A", "c:B", "c:C"]], TEXTS, negatives=2)
    assert [e.label for e in examples] == [1.0, 0.0, 0.0]
    assert {e.target for e in examples if e.label == 0.0} <= {"beta", "gamma"}
    assert "delta" not in {e.target for e in examples}, "c:D nunca fue candidato"


def test_the_gold_class_is_never_also_a_negative():
    mentions = [FakeMention("m1", "x", "c:A")]
    examples = tuning.examples_from(mentions, [["c:A", "c:B"]], TEXTS, negatives=5)
    assert sum(1 for e in examples if e.target == "alpha") == 1


def test_a_mention_whose_class_is_not_in_the_inventory_is_skipped():
    mentions = [FakeMention("m1", "x", "c:MISSING")]
    assert tuning.examples_from(mentions, [["c:A"]], TEXTS) == []


def test_the_examples_are_reproducible():
    mentions = [FakeMention(f"m{i}", f"m {i}", "c:A") for i in range(20)]
    candidates = [["c:A", "c:B", "c:C", "c:D"]] * 20
    first = tuning.examples_from(mentions, candidates, TEXTS, negatives=2)
    again = tuning.examples_from(mentions, candidates, TEXTS, negatives=2)
    assert [(e.mention, e.target, e.label) for e in first] == [
        (e.mention, e.target, e.label) for e in again
    ]


# ─────────────────────────  la comparación  ─────────────────────────


class Reorders:
    """Un modelo que pone primero la clase que se le diga."""

    def __init__(self, winner: str) -> None:
        self.winner = winner

    def predict(self, pairs, **_):
        return [1.0 if target == self.winner else 0.0 for _, target in pairs]


def test_the_comparison_is_paired_over_the_same_mentions():
    """Es lo único que aísla la variable (spec 6.3)."""
    mentions = [FakeMention("m1", "x", "c:A"), FakeMention("m2", "y", "c:A")]
    candidates = [["c:B", "c:A"], ["c:B", "c:A"]]
    result = tuning.compare(Reorders("alpha"), mentions, candidates, TEXTS)
    assert result.base_at_1 == 0.0 and result.tuned_at_1 == 1.0
    assert result.n == 2


def test_the_ceiling_is_reported_next_to_the_gain():
    """Subir nueve puntos cuando había once es otra cosa que subir nueve cuando había
    cuarenta."""
    mentions = [FakeMention("m1", "x", "c:A"), FakeMention("m2", "y", "c:D")]
    candidates = [["c:B", "c:A"], ["c:B", "c:C"]]   # la segunda no tiene la respuesta
    result = tuning.compare(Reorders("alpha"), mentions, candidates, TEXTS)
    assert result.ceiling == 0.5
    assert result.headroom_taken == 1.0, "capturó todo el margen que había"


def test_no_usable_mention_is_not_a_crash():
    assert tuning.compare(Reorders("alpha"), [], [], TEXTS).n == 0
