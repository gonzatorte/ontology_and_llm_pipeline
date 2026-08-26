"""A0 — seed normalization (spec 4.3).

A0.1 mints an opaque IRI per entity and keeps the original as provenance; A0.2 derives labels
and flags the pairs that diverge instead of assuming a translation; A0.3 runs four
deterministic typo detectors against the ontology's own vocabulary; A0.4 assembles the
structural neighbourhood each gloss is written from.

A0.0 (OWL profile detection) is not here: its two consumers — reasoner routing and bounding
B4 — arrive with the reasoner, and the OWL API computes profiles itself (spec 9.1).

Corrections land on labels, never on identifiers: after A0.1 the identifier carries no
meaning, so the question does not arise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from . import terms

CLASS = "class"
OBJECT_PROPERTY = "object_property"
DATATYPE_PROPERTY = "datatype_property"
ANNOTATION_PROPERTY = "annotation_property"
INDIVIDUAL = "individual"

_KINDS = {
    OWL.Class: CLASS,
    OWL.ObjectProperty: OBJECT_PROPERTY,
    OWL.DatatypeProperty: DATATYPE_PROPERTY,
    OWL.AnnotationProperty: ANNOTATION_PROPERTY,
    OWL.NamedIndividual: INDIVIDUAL,
}

# Every annotation property the pipeline may write, declared in one place. Writing one that is
# not here silently leaves OWL 2 DL, which is how `scopeNote` slipped in with axiomatization:
# the defect does not surface as an error, it surfaces as ELK quietly being skipped.
DECLARED_ANNOTATIONS = (
    SKOS.prefLabel, SKOS.altLabel, SKOS.definition, SKOS.historyNote, SKOS.scopeNote,
)

# uuid5, not uuid4: normalizing the same seed twice has to produce the same ontology, or every
# downstream cache key moves with it.
_IRI_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


@dataclass
class Label:
    text: str
    language: str
    source: str  # iri | declared


@dataclass
class Entity:
    iri: str
    original_iri: str
    kind: str
    labels: list[Label] = field(default_factory=list)
    preferred: str = ""
    divergent: bool = False
    divergence_reason: str | None = None

    @property
    def original_local_name(self) -> str:
        return terms.local_name(self.original_iri)


@dataclass
class TypoFinding:
    entity_iri: str
    label: str
    detector: str
    token: str
    suggestion: str
    evidence: str


@dataclass
class GlossContext:
    """What the gloss prompt is built from — never the class name (spec 4.3, A0.4): a gloss is
    a definition, and denormalizing an identifier only ever yields a name."""

    iri: str
    label: str
    kind: str
    superclasses: list[str] = field(default_factory=list)
    subclasses: list[str] = field(default_factory=list)
    domain_of: list[str] = field(default_factory=list)
    range_of: list[str] = field(default_factory=list)
    disjoint_with: list[str] = field(default_factory=list)


@dataclass
class NormalizedSeed:
    graph: Graph
    entities: list[Entity]
    typos: list[TypoFinding]

    def by_iri(self) -> dict[str, Entity]:
        return {entity.iri: entity for entity in self.entities}


def normalize_seed(path: Path, base_iri: str, *, divergence_threshold: float) -> NormalizedSeed:
    source = Graph().parse(path)
    mapping = _mint_opaque_iris(source, base_iri)
    graph = _rewrite(source, mapping)

    entities = [
        _entity(graph, URIRef(new), str(old), divergence_threshold)
        for old, new in mapping.items()
    ]
    entities.sort(key=lambda entity: entity.original_iri)
    _write_labels(graph, entities)

    return NormalizedSeed(graph=graph, entities=entities, typos=detect_typos(entities))


def _mint_opaque_iris(graph: Graph, base_iri: str) -> dict[URIRef, str]:
    mapping: dict[URIRef, str] = {}
    for subject, _, kind in graph.triples((None, RDF.type, None)):
        if kind in _KINDS and isinstance(subject, URIRef) and subject not in mapping:
            mapping[subject] = base_iri + str(uuid.uuid5(_IRI_NAMESPACE, str(subject)))
    return mapping


def _rewrite(graph: Graph, mapping: dict[URIRef, str]) -> Graph:
    rewritten = Graph()
    for prefix, namespace in graph.namespaces():
        rewritten.bind(prefix, namespace)
    rewritten.bind("skos", SKOS)

    def swap(term):
        return URIRef(mapping[term]) if term in mapping else term

    for subject, predicate, obj in graph:
        rewritten.add((swap(subject), swap(predicate), swap(obj)))

    for original, opaque in mapping.items():
        rewritten.add((URIRef(opaque), SKOS.historyNote, Literal(str(original))))

    # OWL 2 DL requires every annotation property to be declared. Without this the ontology
    # leaves the DL profile, ELK's coverage collapses and it is skipped as a filter (spec 9.2).
    for annotation in DECLARED_ANNOTATIONS:
        rewritten.add((annotation, RDF.type, OWL.AnnotationProperty))
    return rewritten


def _entity(graph: Graph, iri: URIRef, original_iri: str, threshold: float) -> Entity:
    kind = next(
        (_KINDS[obj] for obj in graph.objects(iri, RDF.type) if obj in _KINDS), CLASS
    )
    entity = Entity(iri=str(iri), original_iri=original_iri, kind=kind)

    derived = terms.denormalize(terms.local_name(original_iri))
    entity.labels.append(
        Label(text=derived, language=terms.guess_language(derived), source="iri")
    )
    declared = [
        Label(
            text=str(literal),
            language=str(literal.language) if literal.language else
            terms.guess_language(str(literal)),
            source="declared",
        )
        for literal in graph.objects(iri, RDFS.label)
        if isinstance(literal, Literal)
    ]
    entity.labels.extend(declared)

    entity.preferred = declared[0].text if declared else derived
    _assess_divergence(entity, derived, declared, threshold)
    return entity


def _assess_divergence(
    entity: Entity, derived: str, declared: list[Label], threshold: float
) -> None:
    """An identifier in one language with a label in another is usually the same term, but not
    always: `Aplica_una_o_varias` against `appliesTechnique` drops information the label
    carries. Below the threshold the pair is flagged rather than treated as a translation.

    Two outcomes, and they are not the same finding: a same-language mismatch is a real
    divergence to review, while a cross-language pair is only unverified — string similarity
    cannot judge it, and the semantic comparison the spec asks for needs a model.
    """
    if not declared:
        return
    if any(terms.similarity(derived, label.text) >= threshold for label in declared):
        return
    entity.divergent = True
    derived_language = terms.guess_language(derived)
    if all(label.language != derived_language for label in declared):
        entity.divergence_reason = "cross_language_unverified"
    else:
        entity.divergence_reason = "same_language_mismatch"


def _write_labels(graph: Graph, entities: list[Entity]) -> None:
    for entity in entities:
        iri = URIRef(entity.iri)
        graph.remove((iri, RDFS.label, None))
        for label in entity.labels:
            graph.add((iri, RDFS.label, Literal(label.text, lang=label.language)))
        graph.add((
            iri,
            SKOS.prefLabel,
            Literal(entity.preferred, lang=terms.guess_language(entity.preferred)),
        ))


def detect_typos(entities: list[Entity]) -> list[TypoFinding]:
    """Four deterministic detectors, no LLM, comparing against the ontology's own vocabulary.

    Lexicons are per language. Mixed together, `objective`/`objetivo` and
    `participant`/`participante` read as typos when they are translations — this seed has
    Spanish identifiers under English labels throughout.
    """
    lexicons: dict[str, dict[str, int]] = {}
    for entity in entities:
        for label in entity.labels:
            lexicon = lexicons.setdefault(label.language, {})
            for token in terms.tokens(label.text):
                lexicon[token] = lexicon.get(token, 0) + 1

    findings: list[TypoFinding] = []
    for entity in entities:
        for label in entity.labels:
            lexicon = lexicons[label.language]
            findings.extend(_near_miss(entity, label, lexicon))
            findings.extend(_truncated_abbreviation(entity, label, lexicon))
            findings.extend(_capitalization_anomaly(entity, label))
    findings.extend(_naming_pattern(entities))
    return findings


def _near_miss(entity: Entity, label: Label, lexicon: dict[str, int]) -> list[TypoFinding]:
    """`subre` against `sobre`. A misspelling keeps the first letter and is rarer than the
    word it corrupts."""
    findings = []
    for token in terms.tokens(label.text):
        if len(token) < 4 or lexicon[token] > 1:
            continue
        for candidate, count in lexicon.items():
            if candidate == token or count <= lexicon[token] or candidate[0] != token[0]:
                continue
            if abs(len(candidate) - len(token)) > 1:
                continue  # a misspelling keeps roughly the length of the word it corrupts
            if terms.levenshtein(token, candidate) <= 2:
                findings.append(
                    TypoFinding(
                        entity_iri=entity.iri,
                        label=label.text,
                        detector="edit_distance",
                        token=token,
                        suggestion=candidate,
                        evidence=f"'{candidate}' appears {count}x elsewhere, '{token}' once",
                    )
                )
                break
    return findings


def _truncated_abbreviation(
    entity: Entity, label: Label, lexicon: dict[str, int]
) -> list[TypoFinding]:
    """`Frm` against `Framework`: an abbreviation is short, keeps letter order and the first
    letter, and is not itself a word of the language."""
    findings = []
    for token in terms.tokens(label.text):
        if not 3 <= len(token) <= 6 or lexicon[token] > 1 or token in terms.FUNCTION_WORDS:
            continue
        for candidate in lexicon:
            if (
                len(candidate) < len(token) + 3
                or candidate[0] != token[0]
                or not terms.is_subsequence(token, candidate)
            ):
                continue
            findings.append(
                TypoFinding(
                    entity_iri=entity.iri,
                    label=label.text,
                    detector="truncated_abbreviation",
                    token=token,
                    suggestion=candidate,
                    evidence=f"'{token}' is a subsequence of '{candidate}'",
                )
            )
            break
    return findings


def _capitalization_anomaly(entity: Entity, label: Label) -> list[TypoFinding]:
    findings = []
    for word in label.text.split():
        stripped = "".join(char for char in word if char.isalpha())
        if len(stripped) < 3 or stripped.isupper() or stripped.islower():
            continue
        body = stripped[1:]
        if any(char.isupper() for char in body) and not _is_camel_case(stripped):
            findings.append(
                TypoFinding(
                    entity_iri=entity.iri,
                    label=label.text,
                    detector="capitalization",
                    token=word,
                    suggestion=stripped[0] + body.lower(),
                    evidence="uppercase in an unexpected position",
                )
            )
    return findings


def _is_camel_case(word: str) -> bool:
    """`camelCase` and `TitleCase` are conventions; `TIene` is a slip."""
    return not any(
        word[index].isupper() and word[index + 1].isupper() for index in range(len(word) - 1)
    )


def _naming_pattern(entities: list[Entity]) -> list[TypoFinding]:
    """Properties usually follow one house pattern (`is…Of`, `has…`). Only worth reporting
    when a clear majority exists to deviate from."""
    properties = [
        entity for entity in entities
        if entity.kind in (OBJECT_PROPERTY, DATATYPE_PROPERTY)
    ]
    if len(properties) < 5:
        return []

    def conforms(entity: Entity) -> bool:
        return any(
            terms.tokens(label.text) and terms.tokens(label.text)[0] in ("is", "has", "have")
            for label in entity.labels
        )

    conforming = [entity for entity in properties if conforms(entity)]
    if len(conforming) / len(properties) < 0.6:
        return []
    return [
        TypoFinding(
            entity_iri=entity.iri,
            label=entity.preferred,
            detector="naming_pattern",
            token=entity.preferred,
            suggestion="",
            evidence=(
                f"{len(conforming)}/{len(properties)} properties start with is/has; "
                "this one does not"
            ),
        )
        for entity in properties
        if not conforms(entity)
    ]


def gloss_contexts(seed: NormalizedSeed) -> list[GlossContext]:
    graph = seed.graph
    labels = {URIRef(entity.iri): entity.preferred for entity in seed.entities}

    def label_of(term) -> str:
        return labels.get(term, str(term))

    contexts = []
    for entity in seed.entities:
        if entity.kind != CLASS:
            continue
        iri = URIRef(entity.iri)
        contexts.append(
            GlossContext(
                iri=entity.iri,
                label=entity.preferred,
                kind=entity.kind,
                superclasses=sorted(
                    label_of(obj) for obj in graph.objects(iri, RDFS.subClassOf)
                    if isinstance(obj, URIRef)
                ),
                subclasses=sorted(
                    label_of(sub) for sub in graph.subjects(RDFS.subClassOf, iri)
                    if isinstance(sub, URIRef)
                ),
                domain_of=sorted(
                    label_of(prop) for prop in graph.subjects(RDFS.domain, iri)
                    if isinstance(prop, URIRef)
                ),
                range_of=sorted(
                    label_of(prop) for prop in graph.subjects(RDFS.range, iri)
                    if isinstance(prop, URIRef)
                ),
                # Disjointness is symmetric and asserted in one direction only.
                disjoint_with=sorted(
                    label_of(other)
                    for other in (
                        set(graph.objects(iri, OWL.disjointWith))
                        | set(graph.subjects(OWL.disjointWith, iri))
                    )
                    if isinstance(other, URIRef)
                ),
            )
        )
    return contexts
