"""PREP-NORMALIZE — seed normalization.

PREP-NORMALIZE-IRIS mints an opaque IRI per entity and keeps the original as provenance;
PREP-NORMALIZE-LABELS derives labels
and flags the pairs that diverge instead of assuming a translation; PREP-NORMALIZE-TYPOS runs four
deterministic typo detectors against the ontology's own vocabulary; PREP-NORMALIZE-GLOSSES assembles
the
structural neighbourhood each gloss is written from.

PREP-NORMALIZE-PROFILE (OWL profile detection) is not here: its two consumers — reasoner routing and
bounding
ITER-AXIOMATIZE — arrive with the reasoner, and the OWL API computes profiles itself
(REASONING-STACK).

Corrections land on labels, never on identifiers: after PREP-NORMALIZE-IRIS the identifier carries
no
meaning, so the question does not arise.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from . import terms
from .label_overrides import MODEL, UNDETERMINED, USER, Override

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
    # The language was not determined: a proper name, an acronym, or a word spelled the same in
    # both languages. `language` still carries a value because a literal needs a tag, but a pair
    # with an undetermined side is never asserted as a same-language divergence.
    undetermined: bool = False


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
    """What the gloss prompt is built from — never the class name (PREP-NORMALIZE,
    PREP-NORMALIZE-GLOSSES): a gloss is
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
class NormalizedOntology:
    graph: Graph
    entities: list[Entity]
    typos: list[TypoFinding]

    def by_iri(self) -> dict[str, Entity]:
        return {entity.iri: entity for entity in self.entities}


# Formatos que rdflib no lee y la OWL API sí. OBO es el caso que importa: buena parte de las
# ontologías con axiomatización de verdad se publican así.
FOREIGN_SUFFIXES = frozenset({".obo", ".owx", ".ofn"})


def load_ontology(path: Path, *, reasoner_lib: Path | None = None) -> Graph:
    """La ontología inicial como grafo, venga en el formato que venga.

    Un `.obo` no es un error de entrada: es un formato con un mapeo normativo a OWL 2, y su
    implementación de referencia ya está en `lib/`. Convertirlo es parte de leerlo.
    """
    if Path(path).suffix.lower() not in FOREIGN_SUFFIXES:
        return Graph().parse(path)

    from .reasoning import Reasoners, ReasonerUnavailable

    try:
        return Reasoners(reasoner_lib or Path("lib")).to_rdf(Path(path))
    except ReasonerUnavailable as exc:
        raise ReasonerUnavailable(
            f"{path.name} viene en un formato que necesita la OWL API para traducirse a OWL, "
            f"y los jars no están: {exc}"
        ) from exc


@dataclass(frozen=True)
class LabelDecisions:
    """Lo ya decidido sobre las etiquetas, como **entrada** de la normalización.

    Normalizar es función determinista de la ontología que está en disco: una corrección escrita
    encima de la versión commiteada desaparece en la próxima corrida y nadie se entera. Entrando
    por acá se vuelve a aplicar sola, el resultado sigue siendo reproducible, y la decisión —que
    es lo único que no se puede recomputar— vive donde se tomó.

    Qué significa cada una, y las dos son «el hallazgo se sostiene»:

    `typo_fixes`         una errata aceptada. La palabra se corrige en las etiquetas de esa
                         entidad, nunca en el identificador: después de `PREP-NORMALIZE-IRIS` el
                         identificador no carga significado.
    `dropped_derived`    una divergencia aceptada: el nombre que sale del identificador **no** es
                         otro nombre de este concepto, así que se descarta en vez de quedar como
                         una etiqueta más contra la que el matcher compara.
    `languages`          el idioma de una etiqueta cuando la regla de terminaciones no alcanza,
                         por texto y con su fuente (`label_overrides`). Entra por acá y no por
                         un parámetro aparte: dos canales que dicen lo mismo se desincronizan, y
                         el que se olvide de pasar uno no rompe nada visible.
    """

    typo_fixes: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    dropped_derived: frozenset[str] = frozenset()
    languages: dict[str, Override] = field(default_factory=dict)


def _corrected(text: str, fixes: list[tuple[str, str]]) -> str:
    """El texto con las erratas aceptadas ya aplicadas, palabra por palabra.

    Se respeta la mayúscula inicial de lo que había: `Subre` corregido con `sobre` da `Sobre`, y
    cambiarle la capitalización a la etiqueta sería una segunda corrección que nadie pidió.
    """
    if not fixes:
        return text
    words = text.split(" ")
    for index, word in enumerate(words):
        for token, suggestion in fixes:
            if word.lower() == token.lower():
                words[index] = (
                    suggestion[:1].upper() + suggestion[1:] if word[:1].isupper() else suggestion
                )
                break
    return " ".join(words)


def normalize_initial_ontology(
    path: Path, base_iri: str, *, divergence_threshold: float,
    reasoner_lib: Path | None = None,
    decisions: LabelDecisions | None = None,
) -> NormalizedOntology:
    source = load_ontology(path, reasoner_lib=reasoner_lib)
    mapping = _mint_opaque_iris(source, base_iri)
    graph = _rewrite(source, mapping)
    decisions = decisions or LabelDecisions()

    # La divergencia se evalúa después de resolver los idiomas indeterminados, no adentro de
    # `_entity`: el default de UNDETERMINED-LANGUAGE es el idioma mayoritario de la ontología, y
    # ése no se conoce hasta haber leído todas las etiquetas.
    entities, derived_names = [], {}
    for old, new in mapping.items():
        entity, derived = _entity(graph, URIRef(new), str(old), decisions)
        entities.append(entity)
        if derived is not None:
            derived_names[entity.iri] = derived
    _resolve_undetermined(entities)
    for entity in entities:
        derived = derived_names.get(entity.iri)
        if derived is not None:
            declared = [label for label in entity.labels if label.source == "declared"]
            _assess_divergence(entity, derived, declared, divergence_threshold)
    entities.sort(key=lambda entity: entity.original_iri)
    _write_labels(graph, entities)

    return NormalizedOntology(graph=graph, entities=entities, typos=detect_typos(entities))


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
    # leaves the DL profile, ELK's coverage collapses and it is skipped as a filter
    # (REASONING-ELK-ASYMMETRY).
    for annotation in DECLARED_ANNOTATIONS:
        rewritten.add((annotation, RDF.type, OWL.AnnotationProperty))
    return rewritten


def _entity(
    graph: Graph, iri: URIRef, original_iri: str,
    decisions: LabelDecisions | None = None,
) -> tuple[Entity, Label | None]:
    """La entidad y la etiqueta derivada del identificador, o `None` si se descartó.

    Devuelve la derivada porque la divergencia se evalúa afuera, cuando ya se sabe el idioma
    mayoritario de la ontología (`UNDETERMINED-LANGUAGE`).
    """
    decisions = decisions or LabelDecisions()
    kind = next(
        (_KINDS[obj] for obj in graph.objects(iri, RDF.type) if obj in _KINDS), CLASS
    )
    entity = Entity(iri=str(iri), original_iri=original_iri, kind=kind)

    fixes = decisions.typo_fixes.get(str(iri), [])
    derived = _corrected(terms.denormalize(terms.local_name(original_iri)), fixes)
    declared = []
    for literal in graph.objects(iri, RDFS.label):
        if not isinstance(literal, Literal):
            continue
        text = _corrected(str(literal), fixes)
        declared.append(Label(
            text=text, source="declared",
            **_language_of(text, decisions, declared_tag=literal.language),
        ))

    # Una divergencia aceptada dice que el nombre del identificador no es un nombre de este
    # concepto: se descarta, y con él se va el hallazgo, porque volver a preguntar lo ya
    # contestado es lo que esta entrada evita. Salvo que sea el único nombre que hay — una
    # entidad sin etiqueta desaparece del matcher, que es peor que una etiqueta discutida.
    dropped = str(iri) in decisions.dropped_derived and bool(declared)
    derived_label = None
    if not dropped:
        derived_label = Label(text=derived, source="iri", **_language_of(derived, decisions))
        entity.labels.append(derived_label)
    entity.labels.extend(declared)

    entity.preferred = declared[0].text if declared else derived
    return entity, derived_label


def _language_of(
    text: str, decisions: LabelDecisions, *, declared_tag: str | None = None
) -> dict:
    """`LANGUAGE-PRECEDENCE`: `user > declarado > model > guess`.

    El tag declarado es dato de la fuente y no conjetura, así que le gana al modelo: pisarlo
    sería contradecir a quien publicó la ontología. Sólo la corrección del usuario está por
    encima, porque es lo único que no se puede recomputar.
    """
    override = decisions.languages.get(text)
    if override is not None and override.source == USER:
        return _tagged(override.language)
    if declared_tag:
        return {"language": str(declared_tag), "undetermined": False}
    if override is not None and override.source == MODEL:
        return _tagged(override.language)
    return {"language": terms.guess_language(text), "undetermined": False}


def _tagged(language: str) -> dict:
    """Un `und` no es un idioma con el que escribir un literal: se recuerda como
    indeterminado y el idioma se completa después, con el de la ontología."""
    if language == UNDETERMINED:
        return {"language": UNDETERMINED, "undetermined": True}
    return {"language": language, "undetermined": False}


def _resolve_undetermined(entities: list[Entity]) -> str:
    """El idioma con el que se escribe lo que no tiene idioma propio (`UNDETERMINED-LANGUAGE`).

    Un nombre propio, una sigla o una palabra que se escribe igual en los dos idiomas no tiene
    idioma que decidir, pero el literal necesita un tag igual. Va el mayoritario de la ontología,
    contado sobre las etiquetas que sí se resolvieron, para que la elección sea pareja y no al
    azar. La marca de indeterminado queda en la etiqueta.
    """
    counts: dict[str, int] = {}
    for entity in entities:
        for label in entity.labels:
            if not label.undetermined:
                counts[label.language] = counts.get(label.language, 0) + 1
    majority = max(counts, key=lambda language: (counts[language], language)) if counts else "en"
    for entity in entities:
        for label in entity.labels:
            if label.undetermined:
                label.language = majority
    return majority


def _assess_divergence(
    entity: Entity, derived: Label, declared: list[Label], threshold: float
) -> None:
    """An identifier in one language with a label in another is usually the same term, but not
    always: `Aplica_una_o_varias` against `appliesTechnique` drops information the label
    carries. Below the threshold the pair is flagged rather than treated as a translation.

    Two outcomes, and they are not the same finding: a same-language mismatch is a real
    divergence to review, while a cross-language pair is only unverified — string similarity
    cannot judge it, and the semantic comparison the spec asks for needs a model.

    An undetermined side lands on the second: the languages were never established, and
    asserting a real divergence on top of that asserts what nobody knows (OPEN-WORLD).
    """
    if not declared:
        return
    if any(terms.similarity(derived.text, label.text) >= threshold for label in declared):
        return
    entity.divergent = True
    undetermined = derived.undetermined or any(label.undetermined for label in declared)
    if undetermined or all(label.language != derived.language for label in declared):
        entity.divergence_reason = "cross_language_unverified"
    else:
        entity.divergence_reason = "same_language_mismatch"


def _write_labels(graph: Graph, entities: list[Entity]) -> None:
    """Every name as `rdfs:label` and as `skos:altLabel`, the preferred one included.

    The preferred label exists so a tool has something to render (Protégé renders
    `skos:prefLabel`), and that is its whole job: nothing downstream may depend on which name
    happened to be preferred. Repeating it among the `skos:altLabel`s means the set of
    `altLabel`s *is* the set of names the concept has, with no second place to look.
    """
    for entity in entities:
        iri = URIRef(entity.iri)
        graph.remove((iri, RDFS.label, None))
        # El idioma de la preferida sale de la etiqueta que ya se resolvió, no de adivinarlo de
        # nuevo: volver a adivinar acá tira el override y deja el `prefLabel` contradiciendo al
        # `rdfs:label` que dice el mismo texto.
        preferred_language = next(
            (label.language for label in entity.labels if label.text == entity.preferred),
            terms.guess_language(entity.preferred),
        )
        names = [(label.text, label.language) for label in entity.labels]
        for text, language in names:
            graph.add((iri, RDFS.label, Literal(text, lang=language)))
        graph.add((iri, SKOS.prefLabel, Literal(entity.preferred, lang=preferred_language)))
        for text, language in dict.fromkeys([*names, (entity.preferred, preferred_language)]):
            graph.add((iri, SKOS.altLabel, Literal(text, lang=language)))


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
    buckets = {language: _bucket(lexicon) for language, lexicon in lexicons.items()}

    findings: list[TypoFinding] = []
    for entity in entities:
        for label in entity.labels:
            lexicon = lexicons[label.language]
            findings.extend(_near_miss(entity, label, lexicon, buckets[label.language]))
            findings.extend(_truncated_abbreviation(entity, label, lexicon))
            findings.extend(_capitalization_anomaly(entity, label))
    findings.extend(_naming_pattern(entities))
    return findings


def _bucket(lexicon: dict[str, int]) -> dict[tuple[str, int], list[str]]:
    """El léxico indexado por (primera letra, largo), que es lo que `_near_miss` exige igual.

    Sin esto la comparación es cada token contra todo el léxico. Con la ontología inicial de 34
    clases no
    se notaba; con una ontología de verdad —7.159 clases, ~20 mil tokens— son cientos de
    millones de iteraciones y la etapa deja de terminar. El predicado no cambia: son los mismos
    candidatos, buscados en vez de barridos.
    """
    index: dict[tuple[str, int], list[str]] = {}
    for token in lexicon:
        index.setdefault((token[0], len(token)), []).append(token)
    return index


def _near_miss(
    entity: Entity, label: Label, lexicon: dict[str, int],
    buckets: dict[tuple[str, int], list[str]],
) -> list[TypoFinding]:
    """`subre` against `sobre`. A misspelling keeps the first letter and is rarer than the
    word it corrupts."""
    findings = []
    for token in terms.tokens(label.text):
        if len(token) < 4 or lexicon[token] > 1:
            continue
        nearby = [
            candidate
            for length in (len(token) - 1, len(token), len(token) + 1)
            for candidate in buckets.get((token[0], length), ())
        ]
        for candidate in nearby:
            count = lexicon[candidate]
            if candidate == token or count <= lexicon[token]:
                continue
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


def gloss_contexts(seed: NormalizedOntology) -> list[GlossContext]:
    """El vecindario estructural de cada clase **que todavía no tiene definición**.

    PREP-NORMALIZE-GLOSSES es un *bootstrap*: escribe la glosa que falta, no reemplaza la que hay.
    La distinción
    no era ociosa — sobre una ontología publicada, devolver todas las clases hacía que la etapa
    reescribiera 428 definiciones de curadores con texto del modelo, y encima pagando por
    hacerlo. Mejorar una glosa existente con lo que dice el corpus es otra etapa (`enrich`), y
    esa sí parte de la que ya está.
    """
    graph = seed.graph
    labels = {URIRef(entity.iri): entity.preferred for entity in seed.entities}

    def label_of(term) -> str:
        return labels.get(term, str(term))

    contexts = []
    for entity in seed.entities:
        if entity.kind != CLASS:
            continue
        iri = URIRef(entity.iri)
        if graph.value(iri, SKOS.definition) is not None:
            continue
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
