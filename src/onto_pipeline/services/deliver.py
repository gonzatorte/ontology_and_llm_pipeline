"""Los artefactos: el diff entre versiones, el DAG, la telemetría y **la ontología terminada**.

`DELIVERABLES`. Todo lo de acá es de sólo lectura sobre el almacén salvo `export`, que escribe
el archivo que se entrega y nada más.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF

from .. import versioning
from ..chunking import chunk_document
from ..ingest import STAGE, load_block_objects
from ..report import build_report
from .session import Progress, Session, StageError, silent

# ─────────────────────────────  lecturas compartidas  ─────────────────────────────


def corpus_blocks(conn: sqlite3.Connection) -> list[dict]:
    """Los bloques de cuerpo de todo documento que el proceso puede leer.

    Los documentos retenidos quedan afuera acá como en todos lados: una glosa escrita desde el
    conjunto de retención haría que ese conjunto midiera al pipeline contra su propio material.
    """
    return [
        dict(row) for row in conn.execute(
            "SELECT b.id, b.document_id, b.page, b.text FROM blocks b "
            "JOIN documents d ON d.id = b.document_id "
            "WHERE d.held_out = 0 AND b.is_boilerplate = 0 AND b.block_type != 'figure' "
            "ORDER BY b.document_id, b.page, b.ordinal"
        )
    ]


_IRI_TOKEN = re.compile(r"<([^>]+)>|(?<![\S])(https?://\S+)")


def readable(line: str, labels: dict[str, str]) -> str:
    """El diff guardado conserva los IRIs enteros; la terminal muestra etiquetas.

    Con IRIs opacos las N-Triples crudas no son revisables, y la revisabilidad es el motivo
    entero de mostrar un diff.
    """
    def name(match: re.Match) -> str:
        iri = match.group(1) or match.group(2)
        trailing = ""
        if match.group(2) and iri.endswith("."):        # el terminador N-Triples
            iri, trailing = iri[:-1], "."
        return versioning.short_name(iri, labels) + trailing

    return _IRI_TOKEN.sub(name, line)


# ─────────────────────────────  diff  ─────────────────────────────


@dataclass
class Comparison:
    baseline: str
    target: str
    diff: versioning.Diff
    labels: dict[str, str]
    path: Path | None = None
    root: bool = False


def write_diff(session: Session, baseline: str, target: str, result: versioning.Diff) -> Path:
    """Al lado de la ontología entera, porque las dos se leen juntas: el artefacto dice qué es
    la ontología, el diff dice qué le hizo esta iteración."""
    path = session.ontology_dir() / f"{baseline}-to-{target}.diff.json"
    payload = {"from": baseline, "to": target, **asdict(result)}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def publish_diff(session: Session, version_id: str) -> Comparison | None:
    """Todo comando que commitea una versión reporta el diff contra la anterior."""
    pair = versioning.diff_with_parent(session.conn, version_id)
    if pair is None:
        return None
    parent, result = pair
    before = session.graph(parent.id)
    after = session.graph(version_id)
    return Comparison(
        baseline=parent.id, target=version_id, diff=result,
        labels=versioning.label_index(before, after),
        path=write_diff(session, parent.id, version_id, result),
    )


def compare(
    session: Session, *, version: str | None = None, against: str | None = None
) -> Comparison:
    """Diff semántico entre dos versiones (`ITER-APPLY`).

    Axiomas lógicos canónicos con nodos en blanco canonicalizados, anotaciones en su propio
    carril: una serialización reordenada no es un cambio, y un renombre es un cambio de
    etiqueta antes que revuelo de axiomas.
    """
    target = session.resolve_version(version)
    if against is None:
        pair = versioning.diff_with_parent(session.conn, target)
        if pair is None:
            return Comparison(
                baseline=target, target=target, diff=versioning.Diff(), labels={}, root=True
            )
        baseline, result = pair[0].id, pair[1]
    else:
        baseline = session.resolve_version(against)
        result = None

    before = session.graph(baseline)
    after = session.graph(target)
    if result is None:
        result = versioning.diff(before, after)
    return Comparison(
        baseline=baseline, target=target, diff=result,
        labels=versioning.label_index(before, after),
        path=write_diff(session, baseline, target, result),
    )


def version_rows(session: Session) -> list[dict]:
    """El DAG de versiones. Las ramas no elegidas se conservan y siguen alcanzables."""
    versioning.install(session.conn)
    return [
        dict(row)
        for row in session.conn.execute("SELECT * FROM versions ORDER BY created_at")
    ]


# ─────────────────────────────  telemetría y volcados  ─────────────────────────────


@dataclass
class Telemetry:
    stages: list[dict]
    page_classes: list[dict]
    ingest_failures: list[dict]


def telemetry(session: Session) -> Telemetry:
    """Unidades de trabajo y costo por etapa, clases de página en todo el corpus."""
    ledger = session.ledger()
    stages = [
        {"stage": row["stage"], **ledger.stage_report(row["stage"])}
        for row in session.conn.execute(
            "SELECT DISTINCT stage FROM work_units ORDER BY stage"
        )
    ]
    page_classes = [
        dict(row) for row in session.conn.execute(
            "SELECT class, COUNT(*) AS n, json_extract(signals, '$.reason') AS reason "
            "FROM page_classification GROUP BY class, reason ORDER BY n DESC"
        )
    ]
    failures = [
        dict(row) for row in session.conn.execute(
            "SELECT key, error FROM work_units WHERE status = 'failed' AND stage = ?", (STAGE,)
        )
    ]
    return Telemetry(stages=stages, page_classes=page_classes, ingest_failures=failures)


def reports(session: Session, *, doc_id: str | None = None) -> list[Path]:
    """`DELIVERABLES-PENDING-PARSER-EVAL`: HTML autocontenido para evaluar el parseo a mano."""
    ids = [doc_id] if doc_id else [
        row["id"] for row in session.conn.execute("SELECT id FROM documents ORDER BY id")
    ]
    if not ids:
        raise StageError("nothing ingested yet")
    return [build_report(session.config, session.conn, identifier) for identifier in ids]


def chunks(session: Session, doc_id: str) -> list:
    """Las unidades de extracción de un documento. Derivadas, nunca guardadas."""
    found = chunk_document(
        load_block_objects(session.conn, doc_id),
        session.config.chunking.target_chars,
        session.config.chunking.max_chars,
    )
    if not found:
        raise StageError(f"no blocks for {doc_id}")
    return found


def blocks(session: Session, doc_id: str, *, page: int | None = None) -> list[dict]:
    """El almacén de bloques de un documento, para inspeccionar procedencia."""
    query = "SELECT * FROM blocks WHERE document_id = ?"
    params: list = [doc_id]
    if page is not None:
        query += " AND page = ?"
        params.append(page)
    return [
        dict(row) for row in session.conn.execute(query + " ORDER BY page, ordinal", params)
    ]


# ─────────────────────────────  la ontología terminada  ─────────────────────────────

TRIG = "trig"
TURTLE = "turtle"
_SUFFIX = {TRIG: ".trig", TURTLE: ".ttl"}


@dataclass
class Step:
    """Un tramo de la historia: qué le hizo esta versión a la anterior."""

    version_id: str
    parent_id: str | None
    note: str
    added: int
    removed: int
    annotations: int


@dataclass
class Delivery:
    version_id: str
    path: Path
    manifest_path: Path
    fmt: str
    history: list[Step]
    tbox_triples: int
    abox_quads: int
    classes: int
    seed_classes: int
    minted_classes: int
    abox_included: bool
    abox_regenerated: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def iterations(self) -> int:
        return max(len(self.history) - 1, 0)


def export(
    session: Session,
    *,
    version: str | None = None,
    out: Path | None = None,
    fmt: str = TRIG,
    include_abox: bool = True,
    refresh_abox: bool = True,
    progress: Progress = silent,
) -> Delivery:
    """La ontología enriquecida, con toda la historia aplicada, en un archivo.

    **Qué quiere decir "aplicar la historia".** Cada versión guarda su Turtle entero, así que
    la cabeza de un linaje ya *es* la acumulación de todo lo que se aplicó para llegar hasta
    ella: no hay deltas que reproducir. Lo que este comando hace es recorrer el linaje para
    poder decir qué aportó cada iteración, y juntar en un archivo las dos mitades que hasta
    ahora vivían separadas — la TBox, que estaba sólo en SQLite, y el ABox, que estaba en disco
    por su cuenta.

    **El ABox se regenera antes de exportar.** Es función pura de (menciones, tipados, reglas),
    así que ponerlo al día no llama al modelo ni al razonador y no puede sorprender a nadie;
    exportar un ABox viejo, en cambio, entrega instancias que no corresponden a la TBox que va
    en el mismo archivo.

    **No se escribe ninguna anotación nueva en la ontología.** La procedencia —el linaje, qué
    hizo cada iteración, con qué reglas se derivó el ABox— va en un manifiesto JSON al lado.
    Escribirla adentro pediría propiedades de anotación que la semilla no declara, y eso saca
    la ontología de OWL 2 DL sin dar ningún error (`seed.DECLARED_ANNOTATIONS`).
    """
    if fmt not in _SUFFIX:
        raise StageError(f"format is {TRIG} or {TURTLE}, not {fmt!r}")

    version_id = session.resolve_version(version)
    tbox = session.graph(version_id)
    warnings: list[str] = []

    lineage = versioning.lineage(session.conn, version_id)
    history = _history(session, lineage)

    dataset = Dataset()
    for triple in tbox:
        dataset.add(triple)

    abox_quads = 0
    regenerated = False
    if include_abox:
        from .iterate import regenerate

        path = session.abox_path(version_id)
        if refresh_abox:
            progress("regenerating the ABox before exporting it")
            try:
                outcome = regenerate(session, version=version_id)
                regenerated = outcome.wrote
            except StageError as exc:
                warnings.append(f"ABox not refreshed: {exc}")
        if path.exists():
            abox = Dataset()
            abox.parse(str(path), format=TRIG)
            for quad in abox.quads((None, None, None, None)):
                dataset.add(quad)
                abox_quads += 1
        else:
            warnings.append(
                f"no ABox for {version_id}: the file holds the TBox alone. "
                "`onto-pipeline regenerate` writes it."
            )
            include_abox = False

    target = Path(out) if out else session.ontology_dir() / f"{version_id}.enriched{_SUFFIX[fmt]}"
    target.parent.mkdir(parents=True, exist_ok=True)
    if fmt == TRIG:
        target.write_text(dataset.serialize(format=TRIG), encoding="utf-8")
    else:
        # Turtle no tiene grafos con nombre, así que aplanar pierde la procedencia por grafo.
        # Se avisa: perder la procedencia en silencio es exactamente lo que la capa de
        # menciones existe para no hacer.
        flat = Graph()
        for quad in dataset.quads((None, None, None, None)):
            flat.add(quad[:3])
        if abox_quads:
            warnings.append(
                "Turtle has no named graphs: the ABox's per-document provenance was flattened "
                f"away. `--format {TRIG}` keeps it."
            )
        target.write_text(flat.serialize(format=TURTLE), encoding="utf-8")

    classes = _named_classes(tbox)
    # Lo que agregó el pipeline es lo que está en la cabeza y no estaba en la raíz. Contar los
    # IRIs bajo `base_iri` daría otra cosa: `PREP-NORMALIZE` acuña opacos para *toda* la
    # semilla, así que ese prefijo no distingue lo inducido de lo que ya venía.
    root = _root_classes(session, lineage)
    seed_classes = len(root)

    manifest = target.with_suffix(target.suffix + ".manifest.json")
    manifest.write_text(
        json.dumps(
            {
                "version": version_id,
                "lineage": lineage,
                "history": [asdict(step) for step in history],
                "tbox_triples": len(tbox),
                "abox_quads": abox_quads,
                "classes": len(classes),
                "classes_in_the_root_version": seed_classes,
                "abox_included": include_abox,
                "format": fmt,
                "warnings": warnings,
            },
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return Delivery(
        version_id=version_id, path=target, manifest_path=manifest, fmt=fmt,
        history=history, tbox_triples=len(tbox), abox_quads=abox_quads,
        classes=len(classes), seed_classes=seed_classes,
        minted_classes=len(classes - root),
        abox_included=include_abox, abox_regenerated=regenerated, warnings=warnings,
    )


def _history(session: Session, lineage: list[str]) -> list[Step]:
    """Qué aportó cada iteración, de la raíz a la cabeza.

    `lineage` viene de la cabeza hacia atrás; se da vuelta porque una historia se lee en el
    orden en que pasó.
    """
    steps: list[Step] = []
    for version_id in reversed(lineage):
        version, graph = versioning.load(session.conn, version_id)
        if version.parent_id is None:
            steps.append(Step(version_id, None, version.note, len(graph), 0, 0))
            continue
        parent_graph = session.graph(version.parent_id)
        result = versioning.diff(parent_graph, graph)
        steps.append(Step(
            version_id, version.parent_id, version.note,
            len(result.added), len(result.removed), len(result.labels_changed),
        ))
    return steps


def _root_classes(session: Session, lineage: list[str]) -> set:
    """Las clases que traía la versión raíz, o sea la semilla normalizada."""
    if not lineage:
        return set()
    return _named_classes(session.graph(lineage[-1]))


def _named_classes(graph: Graph) -> set:
    """Sólo las clases con nombre.

    Los nodos en blanco quedan afuera: una expresión de clase anónima no es una clase que
    alguien pueda nombrar, y además su identificador cambia entre serializaciones — contarlos
    hacía aparecer ocho clases "nuevas" entre dos versiones cuyo contenido lógico es idéntico,
    que es justo el problema que `versioning.logical_axioms` canonicaliza para no tener.
    """
    return {
        subject for subject in graph.subjects(RDF.type, OWL.Class)
        if isinstance(subject, URIRef)
    }


def label_index(graph: Graph) -> dict[str, str]:
    """Reexportado acá para que quien renderiza no tenga que importar `versioning`."""
    return versioning.label_index(graph)


__all__ = [
    "Comparison", "Delivery", "Step", "Telemetry", "TRIG", "TURTLE",
    "blocks", "chunks", "compare", "corpus_blocks", "export", "label_index",
    "publish_diff", "readable", "reports", "telemetry", "version_rows", "write_diff",
]
