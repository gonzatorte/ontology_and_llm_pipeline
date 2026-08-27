"""`EVAL`: parada, competency questions, conjunto de retención, calibración y ajuste.

Lo que mide al pipeline, separado de lo que lo corre. Ninguna de estas funciones escribe en la
ontología: contestan qué tan bien anda lo que las otras produjeron.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph

from .. import annotate, annotation, calibration, cq, matching, review, stopping, tuning, versioning
from ..ingest import (
    held_out_documents,
    load_blocks,
    load_document,
    markdown_path,
    process_documents,
    set_held_out,
)
from .session import Progress, Session, StageError, silent

# ─────────────────────────────  EVAL-STOPPING  ─────────────────────────────


@dataclass
class Stopping:
    version_id: str
    assessment: object

    @property
    def stop(self) -> bool:
        return self.assessment.stop


def assess(
    session: Session, *, version: str | None = None, iteration: int = 0
) -> Stopping:
    """¿Esto tiene que parar? Los cuatro criterios de `EVAL-STOPPING`, con sus roles.

    Las competency questions son el primario y el único que dice *qué* falta. La saturación de
    novedad es secundaria y automática. La curva de acumulación es diagnóstico y nunca frena
    nada — es la que dice si el problema es el pipeline o el corpus. El presupuesto es duro y
    arbitrario, y el único que siempre termina.

    La cobertura de menciones está deliberadamente ausente: un sistema optimiza lo que se mide,
    y una clase paraguas maximiza cobertura destruyendo el valor conceptual.
    """
    config = session.config
    version_id = session.resolve_version(version)
    return Stopping(
        version_id=version_id,
        assessment=stopping.assess(
            session.conn, version_id,
            target_pass_rate=config.cq.target_pass_rate,
            novelty_window=config.stopping.novelty_window,
            novelty_threshold=config.stopping.novelty_threshold,
            iteration=iteration,
            max_iterations=config.iteration.max_iterations,
        ),
    )


# ─────────────────────────────  competency questions  ─────────────────────────────


@dataclass
class QuestionRun:
    version_id: str
    questions: list
    evaluation: object
    asserted: int
    entailed: int
    inferred: bool
    note: str = ""


def evaluate_questions(
    session: Session,
    *,
    version: str | None = None,
    iteration: int = 0,
    infer: bool = True,
) -> QuestionRun:
    """Correr cada CQ aceptada contra una versión y registrar la tasa de aprobación.

    Consulta el grafo con entailments por defecto: una pregunta inferencial pregunta qué aporta
    el razonador, y SPARQL sobre las tripletas asertadas no puede verlo.
    """
    conn = session.conn
    questions = cq.load(conn)
    if not questions:
        raise StageError("no accepted competency questions")

    versioning.install(conn)
    version_id = session.resolve_version(version)
    graph = session.graph(version_id)
    asserted = len(graph)
    note = ""

    if infer:
        from ..reasoning import InconsistentOntology, ReasonerUnavailable

        try:
            graph = session.reasoners().inferred_graph(graph)
        except (StageError, ReasonerUnavailable) as exc:
            note = (
                f"reasoning unavailable ({exc}). Inferential questions will under-report."
            )
            infer = False
        except InconsistentOntology as exc:
            raise StageError(str(exc)) from exc
    else:
        note = "asserted triples only, so inferential questions will under-report"

    evaluation = cq.evaluate(graph, questions, iteration=iteration)
    cq.record(conn, evaluation)
    return QuestionRun(
        version_id=version_id, questions=questions, evaluation=evaluation,
        asserted=asserted, entailed=len(graph), inferred=infer, note=note,
    )


# ─────────────────────────────  EVAL-PIPELINE: conjunto de retención  ─────────────────


@dataclass
class Retention:
    changed: int
    requested: int
    process: list[str]
    held_out: list[str]


def hold_out(session: Session, doc_ids: list[str], *, release: bool = False) -> Retention:
    """Marcar documentos como conjunto de retención: parseados, nunca dados al proceso.

    Tienen que estar parseados —los offsets de anotación indexan el Markdown que produce
    `PREP-PARSE`— pero no pueden llegar a `ITER-EXTRACT`, o la evaluación mide al pipeline
    contra su propia entrada.
    """
    changed = set_held_out(session.conn, list(doc_ids), held_out=not release)
    return Retention(
        changed=changed, requested=len(doc_ids),
        process=process_documents(session.conn), held_out=held_out_documents(session.conn),
    )


@dataclass
class AnnotationTool:
    written: list[Path]
    missing: list[str]
    classes: int
    glossed: int


def build_annotation_tool(session: Session, *, doc_id: str | None = None) -> AnnotationTool:
    """Armar la herramienta de anotación para los documentos retenidos (`EVAL-PIPELINE`).

    Los offsets indexan el Markdown del parser, así que la herramienta lo embebe tal cual; el
    corpus nunca sale de la máquina.
    """
    config, conn = session.config, session.conn
    ids = [doc_id] if doc_id else held_out_documents(conn)
    if not ids:
        raise StageError(
            "no held-out documents. Mark them first: onto-pipeline hold-out <doc_id> ..."
        )

    ontology = config.paths.work_dir / "ontology" / "seed_normalized.ttl"
    if not ontology.exists():
        raise StageError(f"{ontology} not found; run normalize-seed first")
    classes = annotate.seed_classes(Graph().parse(ontology))

    written, missing = [], []
    for identifier in ids:
        document = load_document(conn, identifier)
        if document is None:
            missing.append(identifier)
            continue
        written.append(annotate.build(
            doc_id=identifier,
            markdown=markdown_path(config, identifier).read_text(encoding="utf-8"),
            markdown_hash=document["markdown_hash"],
            classes=classes,
            pages=annotate.page_index(load_blocks(conn, identifier)),
            target=config.paths.work_dir / "annotate" / f"{identifier}.html",
        ))
    return AnnotationTool(
        written=written, missing=missing, classes=len(classes),
        glossed=sum(1 for item in classes if item.gloss),
    )


@dataclass
class ExportedDocument:
    doc_id: str
    mentions: int = 0
    in_seed: int = 0
    relations: int = 0
    error: str = ""


@dataclass
class BratExport:
    out_dir: Path
    documents: list[ExportedDocument]


def export_annotations(session: Session, path: Path) -> BratExport:
    """`DELIVERABLES-PENDING-BRAT-EXPORTER`: JSONL del conjunto de retención a BRAT/INCEpTION,
    validado contra el Markdown en disco."""
    config, conn = session.config, session.conn
    out_dir = config.paths.work_dir / "brat"

    documents: list[ExportedDocument] = []
    for document in annotation.read_jsonl(path):
        stored = load_document(conn, document.doc_id)
        if stored is None:
            documents.append(ExportedDocument(document.doc_id, error="not ingested"))
            continue
        markdown = markdown_path(config, document.doc_id).read_text(encoding="utf-8")
        try:
            annotation.validate(document, markdown, stored["markdown_hash"])
        except annotation.OffsetMismatch as exc:
            documents.append(ExportedDocument(document.doc_id, error=str(exc)))
            continue
        annotation.export_brat(document, markdown, out_dir)
        documents.append(ExportedDocument(
            doc_id=document.doc_id, mentions=len(document.mentions),
            in_seed=sum(1 for mention in document.mentions if mention.in_seed),
            relations=len(document.relations),
        ))
    return BratExport(out_dir=out_dir, documents=documents)


# ─────────────────────────────  review: lo que espera decisión  ─────────────────────


def review_items(
    session: Session, *, status: str = "open", kind: str | None = None
) -> list[dict]:
    """Lo que necesita una decisión. Nada se aplica acá; la decisión se registra."""
    return review.load(
        session.conn, status=None if status == "any" else status, kind=kind
    )


def review_counts(session: Session) -> dict:
    return review.counts(session.conn)


def resolve_review(
    session: Session, item_id: str, decision: str, *, comment: str = ""
) -> None:
    """Registrar una decisión sobre un hallazgo: aceptar o rechazar."""
    try:
        found = review.resolve(session.conn, item_id, decision, comment)
    except ValueError as exc:
        raise StageError(str(exc)) from exc
    if not found:
        raise StageError(f"no review item {item_id!r}")


# ─────────────────────────────  calibración  ─────────────────────────────


@dataclass
class Sweep:
    pair_name: str
    pairs: list
    reports: list
    distributions: list[tuple[str, object]]
    path: Path


def calibrate(
    session: Session,
    pair_name: str,
    *,
    match_against: list[str] | None = None,
    cross_encoder: bool = False,
    holdout: float | None = None,
    keep_excluded: bool = False,
    context: str = "none",
    limit: int | None = None,
    progress: Progress = silent,
) -> Sweep:
    """Barrer los umbrales del matcher contra un corpus anotado publicado.

    Esto es el instrumento, no el caso de aplicación. Lo que mide transfiere porque es una
    propiedad del método y del encoder; el punto de operación transfiere sólo en parte.
    """
    from ..embeddings import CrossEncoderReranker, EncoderUnavailable, SentenceTransformerEncoder

    config = session.config
    directory = config.paths.calibration_root / pair_name
    if not directory.is_dir():
        raise StageError(f"no pair at {directory}; see calibration/README.md")

    variants = list(match_against or []) or [config.matching.match_against]
    encoders = [False, True] if cross_encoder else [config.matching.use_cross_encoder]

    reports: list = []
    described: list = []
    distributions: list[tuple[str, object]] = []
    for variant in variants:
        pair = calibration.load_pair(
            directory, match_against=variant, holdout=holdout,
            drop_excluded=not keep_excluded,
        )
        if limit:
            pair.documents = pair.documents[:limit]
        if not described:
            described.append(pair)
        for use_cross in encoders:
            try:
                encoder = SentenceTransformerEncoder(
                    config.matching.bi_encoder, config.matching.device
                )
                reranker = (
                    CrossEncoderReranker(config.matching.cross_encoder, config.matching.device)
                    if use_cross else None
                )
            except EncoderUnavailable as exc:
                raise StageError(f"{exc}; uv sync --extra matching") from exc
            # Un Matcher nuevo por corrida: el caché de vectores se indexa por texto, y una
            # glosa y una etiqueta son textos distintos, pero compartirlo entre corridas
            # escondería cuánto costó cada variante.
            matcher = matching.Matcher(
                encoder, reranker,
                auto_merge_threshold=1.1,   # abierto: el barrido aplica las zonas él mismo
                grey_zone_lower=0.0,
                cross_language_always_grey=config.matching.cross_language_always_grey,
                respect_declared_haskey=config.matching.respect_declared_haskey,
                blocking_strategy=config.matching.blocking_strategy,
                context_mode=context,
            )
            label = f"{variant} · {'cross' if use_cross else 'bi'}"
            if context != matching.NO_CONTEXT:
                label += f" · +{context}"
            progress(f"{label}: {len(pair.mentions())} mentions")
            ranking = calibration.rank(pair, matcher)
            runs = calibration.sweep(
                pair, ranking, calibration.thresholds(),
                match_against=variant, use_cross_encoder=use_cross,
            )
            reports.extend(runs)
            distributions.append((label, runs[0].distribution))

    path = config.paths.work_dir / "calibration" / f"{pair_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {
                    "match_against": run.match_against,
                    "use_cross_encoder": run.use_cross_encoder,
                    "threshold": run.threshold,
                    "correct": len(run.report.correct),
                    "mistyped": len(run.report.mistyped),
                    "false_orphans": len(run.report.false_orphans),
                    "genuine_orphans": len(run.report.genuine_orphans),
                    "false_orphan_rate": run.report.false_orphan_rate,
                    "typing_f1": run.report.typing_f1,
                    "excluded_hits": run.excluded_hits,
                    "separation": run.distribution.separation,
                }
                for run in reports
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return Sweep(
        pair_name=pair_name, pairs=described, reports=reports,
        distributions=distributions, path=path,
    )


# ─────────────────────────────  ITER-TUNE  ─────────────────────────────


@dataclass
class Tuning:
    pair_name: str
    train_documents: int
    eval_documents: int
    targets: int
    examples: int
    train_mentions: int
    trained: object
    result: object
    top_k: int
    eval_on: str = ""
    saved: Path | None = None


def tune(
    session: Session,
    pair_name: str,
    *,
    train_fraction: float | None = None,
    negatives: int | None = None,
    method: str | None = None,
    eval_on: str | None = None,
    out: Path | None = None,
    progress: Progress = silent,
) -> Tuning:
    """Ajustar el cross-encoder con las anotaciones de un par (`ITER-TUNE`).

    El bi-encoder recupera y el cross-encoder reordena, así que el techo de esta etapa es la
    diferencia entre acertar en el primer puesto y acertar en los primeros k.

    La partición es **por documento**. Separar menciones al azar deja las del mismo paper de
    los dos lados, que comparten vocabulario y tema, y eso mide memoria.
    """
    config = session.config
    matcher = matching.Matcher(session.encoder())

    def retrieve(pair, documents, top_k: int):
        mentions = [
            matching.Mention(id=m.id, text=m.text, document_id=d.doc_id, language=pair.language)
            for d in documents for m in d.mentions if m.in_seed and m.gold_class
        ]
        gold = {m.id: m.gold_class for d in documents for m in d.mentions}
        for mention in mentions:
            mention.gold_class = gold[mention.id]
        if not mentions:
            return [], []
        vectors = matcher.vectors_for([m.text for m in mentions])
        targets = matcher.vectors_for([t.text for t in pair.targets])
        ranked = matcher._rank(vectors, targets, pair.targets, top_k)
        return mentions, [[target.iri for _, target in row] for row in ranked]

    settings = config.tuning
    top_k = settings.top_k
    train_fraction = train_fraction if train_fraction is not None else settings.train_fraction
    negatives = negatives if negatives is not None else settings.negatives
    source = calibration.load_pair(
        config.paths.calibration_root / pair_name, match_against=config.matching.match_against
    )
    train_docs, eval_docs = tuning.split_by_document(source.documents, train_fraction)
    texts = {target.iri: target.text for target in source.targets}

    progress("recuperando candidatos")
    # El pozo de negativos es más ancho que la ventana de re-ranking, a propósito.
    train_mentions, train_candidates = retrieve(source, train_docs, settings.negative_pool)
    examples = tuning.examples_from(
        train_mentions, train_candidates, texts, negatives=negatives
    )

    progress("entrenando")
    try:
        trained = tuning.train(
            examples, config.matching.cross_encoder, device=config.matching.device,
            method=method or settings.method, full_max_params=settings.full_max_params,
            lora_rank=settings.lora_rank, lora_alpha=settings.lora_alpha,
            lora_dropout=settings.lora_dropout, epochs=settings.epochs,
            lora_epochs=settings.lora_epochs,
            batch_size=settings.batch_size, max_length=settings.max_length,
        )
    except (ValueError, tuning.TrainerUnavailable) as exc:
        raise StageError(str(exc)) from exc

    target_pair, target_docs, target_texts = source, eval_docs, texts
    if eval_on:
        target_pair = calibration.load_pair(
            config.paths.calibration_root / eval_on,
            match_against=config.matching.match_against,
        )
        target_docs = target_pair.documents
        target_texts = {t.iri: t.text for t in target_pair.targets}

    progress("evaluando")
    mentions, candidates = retrieve(target_pair, target_docs, top_k)
    result = tuning.compare(trained.model, mentions, candidates, target_texts)

    return Tuning(
        pair_name=pair_name, train_documents=len(train_docs), eval_documents=len(eval_docs),
        targets=len(source.targets), examples=len(examples),
        train_mentions=len(train_mentions), trained=trained, result=result, top_k=top_k,
        eval_on=eval_on or "", saved=tuning.save(trained, out) if out is not None else None,
    )


__all__ = [
    "AnnotationTool", "BratExport", "ExportedDocument", "QuestionRun", "Retention",
    "Stopping", "Sweep", "Tuning",
    "assess", "build_annotation_tool", "calibrate", "evaluate_questions",
    "export_annotations", "hold_out", "resolve_review", "review_counts", "review_items",
    "tune",
]
