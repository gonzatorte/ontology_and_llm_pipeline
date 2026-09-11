"""Un caso de uso: un corpus anotado y la ontología contra la que se lo anotó.

**Por qué el nombre.** El proyecto no tiene un dominio comprometido, así que no hay «caso de
aplicación» ni «corpus de calibración»: hay pares (corpus, ontología) que se usan como
instrumentos, y lo que califica al sistema es cómo se comporta *a través* de ellos
(`SCOPE-PURPOSE`). Calibrar es una de las cosas que se hacen con uno; correr el pipeline entero
contra una respuesta conocida es otra. Este módulo carga el instrumento; `calibration` lo usa
para medir.

Lo que un caso de uso da gratis: **`in_inventory` es decidible por construcción**. La clase gold
está
en la ontología o no está, así que la tasa de falsos huérfanos —la métrica que gobierna
`BUILD-NO-GO-GATE`— no necesita campaña de anotación.

Tres cosas que esta carga **no** hace, a propósito:

    sin ingesta      el corpus ya es texto plano; `PREP-CLASSIFY` y `PREP-PARSE` no corren, y
                     `markdown_hash` hashea el texto fuente. Por eso la objeción de
                     `BUILD-OUT-OF-SCOPE` a los importadores no aplica acá: el texto es su
                     propia referencia y ninguna versión del parser puede correr los offsets.
    sin ITER-EXTRACT el corpus ya trae las menciones. Pasarlas por el extractor mediría al
                     extractor y no al matcher.
    sin normalizar   `initial_ontology.normalize_initial_ontology` acuña IRIs opacos y caza
    erratas, que es el
     la semilla      tratamiento correcto para una semilla que escribió una persona y el
                     equivocado para una ontología publicada: las anotaciones gold nombran
                     clases por su id propio, así que los ids tienen que sobrevivir intactos.

## La retención de clases

Un corpus anotado contra O no tiene huérfanas genuinas: toda clase gold está en O por
construcción, así que `in_inventory` es siempre verdadero y el carril de huérfanas genuinas de
`EVAL-PIPELINE` queda vacío. Ese carril es la mitad de `BUILD-NO-GO-GATE`, y dejarlo sin probar
calibraría medio instrumento.

`holdout_classes` lo fabrica: una fracción determinista del inventario se retiene, y toda mención
de una clase retenida pasa a ser huérfana genuina *con respuesta conocida*. Mide lo que ninguna
otra cosa acá puede — si el matcher se abstiene de tipar lo que debería, en vez de forzar la
mención sobre la clase sobreviviente más cercana.
"""

from __future__ import annotations

import hashlib
import random
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import annotation
from .matching import Mention, Target

KNOWTATOR = "knowtator"
BRAT = "brat"
WEBANNO = "webanno"

# An OBO id (CL:0000540) and the IRI a released OWL file uses for it
# (http://purl.obolibrary.org/obo/CL_0000540) are the same class. The gold annotations use the
# first form, so everything is keyed on it.
_OBO_IRI = re.compile(r"^https?://purl\.obolibrary\.org/obo/([A-Za-z][A-Za-z0-9_]*)_(\d+)$")
_DEF_QUOTED = re.compile(r'^def:\s*"((?:[^"\\]|\\.)*)"')


class UnknownFormat(ValueError):
    """The use case declares an annotation format nothing reads."""


class UseCaseIncomplete(ValueError):
    """The use case descriptor points at something that is not on disk."""


@dataclass
class GoldMention:
    id: str
    span: tuple[int, int]
    text: str
    gold_class: str | None
    in_inventory: bool = False


@dataclass
class GoldDocument:
    doc_id: str
    text: str
    mentions: list[GoldMention] = field(default_factory=list)
    # Annotations the reader could not represent, kept as a number rather than dropped in
    # silence: a corpus whose conventions do not survive the import is a fact about the
    # measurement, not a detail of the importer.
    skipped: int = 0

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]


@dataclass
class UseCase:
    """A corpus and the ontology it was annotated against."""

    name: str
    language: str
    documents: list[GoldDocument]
    targets: list[Target]
    withheld: list[str] = field(default_factory=list)
    excluded_classes: list[str] = field(default_factory=list)
    dropped_excluded: bool = True
    description: str = ""

    @property
    def inventory(self) -> set[str]:
        return {target.iri for target in self.targets}

    def annotated_documents(self) -> list[annotation.AnnotatedDocument]:
        """The gold standard in the project's own format, so `annotation.score` applies.

        `page` is 0 throughout: these corpora are plain text with no pagination, and inventing
        one would put a number in the field that nothing could check.
        """
        return [
            annotation.AnnotatedDocument(
                doc_id=document.doc_id,
                markdown_hash=document.text_hash,
                mentions=[
                    annotation.Mention(
                        id=mention.id,
                        page=0,
                        span=mention.span,
                        text=mention.text,
                        gold_class=mention.gold_class,
                        in_inventory=mention.in_inventory,
                    )
                    for mention in document.mentions
                ],
            )
            for document in self.documents
        ]

    def mentions(self) -> list[Mention]:
        return [
            Mention(
                id=mention.id,
                text=mention.text,
                document_id=document.doc_id,
                language=self.language,
                context=sentence_around(document.text, mention.span),
            )
            for document in self.documents
            for mention in document.mentions
        ]


_SENTENCE_END = re.compile(r"[.!?]\s|\n")


def sentence_around(text: str, span: tuple[int, int], *, window: int = 400) -> str:
    """La oración que contiene ese span, acotada por una ventana.

    Acotada porque el texto de un documento puede no tener puntuación donde uno la espera —una
    tabla, una lista, un encabezado— y sin tope la "oración" se vuelve el documento entero, que
    es exactamente el desbalance de forma que hace perder al encoder.
    """
    start, end = span
    left = max(0, start - window)
    right = min(len(text), end + window)
    before = text[left:start]
    after = text[end:right]
    cuts = [match.end() for match in _SENTENCE_END.finditer(before)]
    opening = left + (cuts[-1] if cuts else 0)
    closing = next(
        (end + match.start() + 1 for match in _SENTENCE_END.finditer(after)), right
    )
    return " ".join(text[opening:closing].split())


# ─────────────────────────────  annotation formats  ─────────────────────────────
# Kept behind a registry because the use cases do not share a format: CRAFT is Knowtator,
# the food and materials corpora are BRAT. Adding one is a reader, not a change to the loader.


def read_knowtator(path: Path, text: str) -> tuple[list[GoldMention], int]:
    """Knowtator standoff XML: `<annotation>` carries the spans, `<classMention>` the class,
    joined by mention id.

    Discontinuous annotations — 424 of CRAFT's 9,147 CL mentions — are skipped rather than
    flattened. Flattening them to (first start, last end) would hand the encoder the text
    between the fragments, which the annotator deliberately left out.
    """
    root = ET.parse(path).getroot()
    class_of = {
        node.get("id"): child.get("id")
        for node in root.findall("classMention")
        for child in node.findall("mentionClass")
    }
    mentions, skipped = [], 0
    for node in root.findall("annotation"):
        spans = node.findall("span")
        reference = node.find("mention")
        identifier = reference.get("id") if reference is not None else None
        if len(spans) != 1 or identifier is None or identifier not in class_of:
            skipped += 1
            continue
        start, end = int(spans[0].get("start")), int(spans[0].get("end"))
        mentions.append(
            GoldMention(
                id=identifier, span=(start, end), text=text[start:end],
                gold_class=class_of[identifier],
            )
        )
    return mentions, skipped


def read_brat(path: Path, text: str) -> tuple[list[GoldMention], int]:
    """BRAT `.ann`: `T<n>\\t<class> <start> <end>\\t<text>`. Only text-bound annotations are
    read; relations and attributes are not part of what ITER-MATCH is measured on."""
    mentions, skipped = [], 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("T"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            skipped += 1
            continue
        head = parts[1].split(" ")
        # A discontinuous BRAT span is written `start end;start end`; same decision as above.
        if len(head) != 3 or ";" in parts[1]:
            skipped += 1
            continue
        kind, start, end = head[0], int(head[1]), int(head[2])
        mentions.append(
            GoldMention(
                id=f"{path.stem}:{parts[0]}", span=(start, end), text=text[start:end],
                gold_class=kind,
            )
        )
    return mentions, skipped


_WEBANNO_SPAN = re.compile(r"\[(\d+)\]")


def webanno_rows(path: Path) -> list[tuple[int, int, str, str, str, str]]:
    """Filas de token de un TSV de WebAnno 3.x:
    (inicio, fin, token, identificador, marca, oración).

    El formato es una fila por token con offsets absolutos sobre el documento original, y las
    columnas de la capa declarada en la cabecera `#T_SP=`. Las líneas que empiezan con `#` son
    cabecera o el texto de la oración; las vacías separan oraciones.
    """
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 5 or "-" not in parts[1]:
            continue
        begin, _, end = parts[1].partition("-")
        if not (begin.isdigit() and end.isdigit()):
            continue
        sentence = parts[0].partition("-")[0]
        rows.append((int(begin), int(end), parts[2], parts[3], parts[4], sentence))
    return rows


def webanno_text(path: Path) -> str:
    """El documento reconstruido desde los offsets de sus tokens.

    Cada token se coloca en su posición y los huecos se rellenan con espacios, así que
    `texto[inicio:fin] == token` vale para todos por construcción. No reproduce los espacios
    del original —el TSV no los guarda— y no hace falta: lo que tiene que quedar exacto son las
    posiciones, que es contra lo que se mide.
    """
    rows = webanno_rows(path)
    if not rows:
        return ""
    buffer = [" "] * max(end for _, end, *_ in rows)

    previous = None
    for begin, end, token, _, _, sentence in rows:
        buffer[begin:end] = token.ljust(end - begin)[: end - begin]
        if previous is not None and sentence != previous[0]:
            # Corte de oración: se escribe en el hueco anterior al token, no encima de él, así
            # que los offsets no se mueven. Sin esto el documento sale como un único bloque
            # gigante —el TSV no guarda los saltos de línea— y el chunker no tiene por dónde
            # partirlo: medido, 30 mil caracteres en un solo bloque.
            gap = begin - previous[1]
            if gap >= 2:
                buffer[begin - 2:begin] = "\n\n"
            elif gap == 1:
                buffer[begin - 1:begin] = "\n"
        previous = (sentence, end)
    return "".join(buffer)


def read_webanno(path: Path, text: str) -> tuple[list[GoldMention], int]:
    """WebAnno TSV 3.x. Una anotación puede abarcar varios tokens, unidos por su marca `*[n]`.

    Un token sin marca de grupo (`*` a secas) es una anotación de un token. Los que comparten
    `[n]` dentro del mismo archivo son el mismo span, y se unen por sus extremos: WebAnno no
    escribe spans discontinuos en esta capa, así que unir por extremos no puede tragarse texto
    que el anotador dejó afuera —que es la razón por la que los otros dos lectores los saltean—.
    """
    grouped: dict[str, list[tuple[int, int]]] = {}
    classes: dict[str, str] = {}
    mentions, skipped = [], 0

    for index, (begin, end, _, identifier, marker, _sentence) in enumerate(webanno_rows(path)):
        if identifier == "_" or not identifier.strip():
            continue
        if identifier.count("|") or marker.count("|"):
            # Un token con varias anotaciones superpuestas: WebAnno las separa con `|`. No es
            # representable como una mención con una clase, y contarlo callado sería inventar.
            skipped += 1
            continue
        group = match.group(1) if (match := _WEBANNO_SPAN.search(marker)) else f"t{index}"
        grouped.setdefault(group, []).append((begin, end))
        classes[group] = identifier

    for group, spans in grouped.items():
        start, finish = min(s for s, _ in spans), max(e for _, e in spans)
        mentions.append(GoldMention(
            id=f"{path.stem}:{group}", span=(start, finish), text=text[start:finish],
            gold_class=classes[group],
        ))
    return sorted(mentions, key=lambda item: item.span), skipped


READERS = {KNOWTATOR: read_knowtator, BRAT: read_brat, WEBANNO: read_webanno}


# ─────────────────────────────  ontology  ─────────────────────────────


def curie(identifier: str) -> str:
    """`http://purl.obolibrary.org/obo/CL_0000540` and `CL:0000540` name the same class."""
    match = _OBO_IRI.match(identifier)
    return f"{match.group(1)}:{match.group(2)}" if match else identifier


def read_obo(path: Path) -> list[dict]:
    """Enough of OBO to build targets: id, name, definition, synonyms.

    Deliberately not a full parser. The alternative is loading the OWL through rdflib, which
    for a released ontology means minutes and hundreds of megabytes to recover four fields.
    The axioms the ontology is chosen for are not read here — they are the symbolic side's
    input, and the `.owl` sits next to this file for that.
    """
    entries = []
    for stanza in Path(path).read_text(encoding="utf-8", errors="replace").split("\n[")[1:]:
        kind, _, body = stanza.partition("]\n")
        if kind != "Term" or "\nis_obsolete: true" in body:
            continue
        identifier = _first(body, "id")
        name = _first(body, "name")
        if not identifier or not name:
            continue
        definition = None
        for line in body.splitlines():
            match = _DEF_QUOTED.match(line)
            if match:
                definition = match.group(1).replace('\\"', '"')
                break
        entries.append(
            {
                "id": identifier,
                "label": name,
                "gloss": definition,
                "synonyms": re.findall(r'^synonym:\s*"((?:[^"\\]|\\.)*)"', body, re.M),
            }
        )
    return entries


def _first(body: str, tag: str) -> str | None:
    match = re.search(rf"^{tag}:\s*(.+)$", body, re.M)
    return match.group(1).strip() if match else None


# Formatos que rdflib lee. OBO tiene su propio lector acá porque el banco no necesita la
# semántica completa —etiqueta, definición y sinónimos alcanzan— y arrancar la JVM para leer
# un inventario sería pagar el arranque en cada barrido.
_RDF_SUFFIXES = frozenset({".ttl", ".owl", ".rdf", ".nt", ".xml", ".jsonld"})


def read_rdf(path: Path) -> list[dict]:
    """Clases de una ontología en cualquier serialización RDF, con la misma forma que
    `read_obo` devuelve. Sin etiqueta no hay contra qué comparar, así que esas se saltean."""
    from rdflib import Graph, URIRef
    from rdflib.namespace import OWL, RDF, RDFS, SKOS

    graph = Graph()
    graph.parse(str(path))
    entries = []
    for subject in graph.subjects(RDF.type, OWL.Class):
        if not isinstance(subject, URIRef):
            continue
        label = graph.value(subject, SKOS.prefLabel) or graph.value(subject, RDFS.label)
        if label is None:
            continue
        gloss = graph.value(subject, SKOS.definition) or graph.value(subject, RDFS.comment)
        entries.append({
            "id": str(subject),
            "label": str(label),
            "gloss": str(gloss) if gloss is not None else "",
            "synonyms": [str(value) for value in graph.objects(subject, SKOS.altLabel)],
        })
    return entries


def read_ontology(path: Path) -> list[dict]:
    """El inventario de un par, venga en OBO o en RDF."""
    if Path(path).suffix.lower() in _RDF_SUFFIXES:
        return read_rdf(Path(path))
    return read_obo(Path(path))


def load_targets(paths: Sequence[Path], match_against: str) -> list[Target]:
    """Union of the given ontology files, first definition of a class winning.

    More than one file because CRAFT annotates with the released ontology *plus* extension
    classes it defines itself, and a gold class from either has to resolve. Order matters:
    the released ontology goes first, so its wording is the one measured.
    """
    by_id: dict[str, Target] = {}
    for path in paths:
        for entry in read_ontology(path):
            identifier = curie(entry["id"])
            if identifier in by_id:
                continue
            by_id[identifier] = Target(
                iri=identifier,
                label=entry["label"],
                gloss=entry["gloss"],
                alt_labels=list(entry["synonyms"]),
                match_against=match_against,
            )
    return sorted(by_id.values(), key=lambda target: target.iri)


# ─────────────────────────  cargar un caso de uso  ─────────────────────────


def load_use_case(
    directory: Path, *, match_against: str, holdout: float | None = None,
    drop_excluded: bool = True,
) -> UseCase:
    """Read `use_case.yml` and everything it points at. Paths inside it resolve against it.

    `holdout` overrides the descriptor's `holdout_classes`, because withholding classes changes
    what the headline number means and that belongs in the command that asked for it, not in a
    file someone else may read later without noticing.

    `drop_excluded` takes the corpus's own guarantee seriously. On CRAFT/CL the effect is not
    marginal: `CL:0000000` is labelled *cell*, the annotators used the extension class
    `CL_GO_EXT:cell` instead — also labelled *cell* — and that class is 3,262 of the 8,723 gold
    mentions. Leaving a label-identical class that is guaranteed wrong in the candidate pool
    measures a modelling collision, not the matcher. `--keep-excluded` restores it, which is
    the run that shows how much of the error was that.
    """
    directory = Path(directory)
    descriptor = directory / "use_case.yml"
    if not descriptor.exists():
        raise UseCaseIncomplete(f"{directory} has no use_case.yml")
    spec = yaml.safe_load(descriptor.read_text(encoding="utf-8"))

    def resolve(value: str) -> Path:
        return (directory / value).resolve()

    fmt = spec["annotations"]["format"]
    if fmt not in READERS:
        raise UnknownFormat(f"{fmt!r}; available: {', '.join(sorted(READERS))}")
    reader = READERS[fmt]

    text_dir = resolve(spec["documents"]["dir"])
    annotation_dir = resolve(spec["annotations"]["dir"])
    suffix = spec["annotations"].get("suffix", ".ann")
    if not text_dir.is_dir() or not annotation_dir.is_dir():
        raise UseCaseIncomplete(f"{text_dir} or {annotation_dir} is missing; see PROCEDENCIA.md")

    documents = []
    for text_path in sorted(text_dir.glob(spec["documents"].get("glob", "*.txt"))):
        annotation_path = annotation_dir / f"{text_path.stem}{suffix}"
        if not annotation_path.exists():
            continue
        text = text_path.read_text(encoding="utf-8")
        mentions, skipped = reader(annotation_path, text)
        documents.append(GoldDocument(text_path.stem, text, mentions, skipped))
    if not documents:
        raise UseCaseIncomplete(f"{directory}: no document had both text and annotations")

    targets = load_targets(
        [resolve(item) for item in spec["ontology"]["files"]], match_against
    )
    requested = {"fraction": holdout} if holdout else spec.get("holdout_classes")
    withheld = _withhold(targets, requested)
    excluded = _excluded(spec.get("excluded_classes"), resolve)

    removed = set(withheld) | (set(excluded) if drop_excluded else set())
    use_case = UseCase(
        name=spec.get("name", directory.name),
        language=spec.get("language", "en"),
        documents=documents,
        targets=[target for target in targets if target.iri not in removed],
        withheld=withheld,
        excluded_classes=excluded,
        dropped_excluded=drop_excluded,
        description=spec.get("description", ""),
    )
    inventory = use_case.inventory
    for document in use_case.documents:
        for mention in document.mentions:
            mention.gold_class = curie(mention.gold_class) if mention.gold_class else None
            mention.in_inventory = mention.gold_class in inventory
    return use_case


def _withhold(targets: Sequence[Target], holdout) -> list[str]:
    """Deterministic class-level holdout. A fraction, or an explicit list of ids."""
    if not holdout:
        return []
    if isinstance(holdout, list):
        return sorted(curie(item) for item in holdout)
    fraction = float(holdout.get("fraction", 0.0))
    if fraction <= 0:
        return []
    identifiers = sorted(target.iri for target in targets)
    generator = random.Random(int(holdout.get("seed", 0)))
    return sorted(generator.sample(identifiers, int(len(identifiers) * fraction)))


def _excluded(value, resolve) -> list[str]:
    """Classes a mention must never be typed to.

    CRAFT ships one such list per annotation set: classes its annotators decided not to use,
    which its README says are *guaranteed* false positives for anything that predicts them.
    It is precision signal that costs no annotation.
    """
    if not value:
        return []
    if isinstance(value, list):
        return sorted(curie(item) for item in value)
    path = resolve(value)
    if not path.exists():
        return []
    return sorted(
        curie(line.split("\t")[0].strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
