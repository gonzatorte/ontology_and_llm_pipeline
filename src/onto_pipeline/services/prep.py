"""Las etapas de `PREP`, sin interfaz: corpus adentro, ontología inicial normalizada, glosas, CQ.

Cada función devuelve lo que pasó; ninguna imprime. Lo que antes era el cuerpo de un comando
de Typer vive acá, y el comando quedó como adaptador — ver `services/workspace.py` para por qué.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph

from .. import cq, cq_generation, glosses, llm, review, typing_store, versioning
from ..alignment import check_terms
from ..alignment import survey as alignment_survey
from ..artifacts import Artifact
from ..ingest import discover
from ..ingest import ingest as ingest_documents
from ..initial_ontology import (
    LabelDecisions,
    NormalizedOntology,
    gloss_contexts,
    normalize_initial_ontology,
)
from ..parse import TEXT_SUFFIXES
from .workspace import Progress, StageError, Workspace, silent

# ─────────────────────────────  PREP-CLASSIFY + PREP-PARSE  ─────────────────────────────


@dataclass
class Ingestion:
    paths: list[Path]
    outputs: dict
    executed: int
    cached: int
    failures: dict


def ingest(
    workspace: Workspace,
    *,
    documents: list[Path] | None = None,
    limit: int | None = None,
    progress: Progress = silent,
) -> Ingestion:
    """`PREP-CLASSIFY`+`PREP-PARSE`: clasificar páginas, parsear, poblar bloques y Markdown."""
    session = workspace.require_session()
    paths = (
        [path.resolve() for path in documents] if documents
        else discover(workspace.config.paths.corpus_root)
    )
    if not paths:
        formats = ", ".join(sorted(TEXT_SUFFIXES))
        raise StageError(
            f"no hay documentos bajo {workspace.config.paths.corpus_root}: se buscan PDFs y "
            f"texto plano ({formats})"
        )
    if limit:
        paths = paths[:limit]

    progress(f"ingesting {len(paths)} document(s)")
    result = ingest_documents(
        workspace.config, workspace.conn, paths, session_id=session
    )
    return Ingestion(
        paths=paths, outputs=result.outputs, executed=result.executed,
        cached=result.cached, failures=result.failures,
    )


# ─────────────────────────────  PREP-NORMALIZE  ─────────────────────────────


@dataclass
class Normalization:
    seed: NormalizedOntology
    target: Artifact
    kinds: dict[str, int]
    pending_glosses: int
    committed: versioning.Version | None
    same_state_as: versioning.Version | None
    review_added: int
    review_known: int
    review_superseded: int
    contexts: list = field(default_factory=list)
    carried_glosses: int = 0

    @property
    def version_id(self) -> str:
        return (self.committed or self.same_state_as).id


def label_decisions(workspace: Workspace) -> LabelDecisions:
    """Lo que el usuario ya decidió sobre las etiquetas, para que la normalización lo re-aplique.

    Normalizar es función determinista de la ontología que está en disco, así que una corrección
    escrita encima de la versión commiteada se pierde en la próxima corrida sin que nadie se
    entere. Por eso la decisión entra como insumo: se lee de `review_items` —donde se tomó— y se
    vuelve a aplicar sola.

    Aceptar es siempre «el hallazgo se sostiene»: una errata aceptada se corrige, y una
    divergencia aceptada dice que el nombre sacado del identificador no nombra a ese concepto.
    """
    session = workspace.require_session()
    fixes: dict[str, list[tuple[str, str]]] = {}
    for item in review.load(
        workspace.conn, status=review.ACCEPTED, kind=review.TYPO, session_id=session
    ):
        payload = item["payload"]
        if payload.get("token") and payload.get("suggestion"):
            fixes.setdefault(item["subject_iri"], []).append(
                (payload["token"], payload["suggestion"])
            )
    dropped = {
        item["subject_iri"]
        for kind in (review.DIVERGENT_LABEL, review.PENDING_SEMANTIC_CHECK)
        for item in review.load(
            workspace.conn, status=review.ACCEPTED, kind=kind, session_id=session
        )
    }
    return LabelDecisions(typo_fixes=fixes, dropped_derived=frozenset(dropped))


def normalize(workspace: Workspace) -> Normalization:
    """`PREP-NORMALIZE`: IRIs opacos, etiquetas derivadas, erratas, contextos de glosa.

    No genera glosas: eso es `generate_glosses`, que llama al modelo y por eso es una etapa
    aparte que la interfaz decide si corre.
    """
    session = workspace.require_session()
    config = workspace.config
    seed = normalize_initial_ontology(
        config.paths.initial_ontology,
        config.initial_ontology.base_iri,
        divergence_threshold=config.initial_ontology.label_divergence_threshold,
        reasoner_lib=config.paths.reasoner_lib,
        decisions=label_decisions(workspace),
    )

    conn = workspace.conn
    carried = _carry_glosses(workspace, seed.graph)

    target = workspace.artifacts.normalized_ontology()
    target.write_text(seed.graph.serialize(format="turtle"))

    state = versioning.state_hash(seed.graph)
    existing = versioning.find_by_hash(conn, state, session_id=session)
    committed = None
    if existing is None:
        committed = versioning.commit(
            conn, seed.graph, version_id=workspace.next_version_id(),
            note="ontología inicial normalizada"
        , session_id=session)
    else:
        # Una etiqueta es una anotación, así que corregir una no mueve el hash de estado y esta
        # etapa no commitearía nada: quedaría el TTL del disco corregido y el grafo que lee el
        # pipeline —que sale de la versión guardada, no del archivo— con la etiqueta vieja. La
        # versión nueva conserva el hash de su padre, como ya hace `generate_glosses`.
        tip = versioning.latest_by_hash(conn, state, session_id=session) or existing
        if versioning.diff(versioning.load(conn, tip.id)[1], seed.graph).labels_changed:
            committed = versioning.commit(
                conn, seed.graph, version_id=workspace.next_version_id(), parent_id=tip.id,
                note="etiquetas decididas en revisión (sólo anotaciones; mismo estado lógico)",
                session_id=session,
            )

    contexts = gloss_contexts(seed)
    current = committed or existing
    sync = review.sync(
        conn, review.findings_from_initial(seed),
        version_id=current.id if current else workspace.next_version_id(),
        kinds=[review.DIVERGENT_LABEL, review.PENDING_SEMANTIC_CHECK, review.TYPO],
        session_id=session,
    )

    kinds: dict[str, int] = {}
    for entity in seed.entities:
        kinds[entity.kind] = kinds.get(entity.kind, 0) + 1

    return Normalization(
        seed=seed, target=target, kinds=kinds, pending_glosses=len(contexts),
        committed=committed, same_state_as=existing,
        review_added=sync.added, review_known=sync.already_known,
        review_superseded=sync.superseded, contexts=contexts, carried_glosses=carried,
    )


def _carry_glosses(workspace: Workspace, graph: Graph) -> int:
    """Las definiciones que la versión más nueva ya tiene, sobre el grafo recién derivado.

    Normalizar re-lee la semilla del disco: sin esto, re-normalizar una sesión ya glosada pisa
    el artefacto con un grafo sin `skos:definition` y commitea una versión que las perdió — y el
    matcher pasa a comparar contra nombres hasta que alguien vuelva a glosar.
    """
    latest = workspace.latest_version()
    if latest is None:
        return 0
    _, previous = versioning.load(workspace.conn, latest)
    return glosses.carry_over(previous, graph)


@dataclass
class GlossBootstrap:
    written: int
    executed: int
    cached: int
    failures: dict
    in_tokens: int
    out_tokens: int
    committed: versioning.Version
    parent_id: str | None
    target: Artifact


def generate_glosses(
    workspace: Workspace,
    normalization: Normalization,
    *,
    progress: Progress = silent,
) -> GlossBootstrap:
    """`PREP-NORMALIZE-GLOSSES`.

    La glosa es contra lo que compara `ITER-MATCH`, así que esto es lo que cierra la brecha de
    falsos huérfanos que el matcher muestra mientras cada clase tiene sólo una etiqueta.
    """
    session = workspace.require_session()
    config, conn = workspace.config, workspace.conn
    seed, contexts, target = normalization.seed, normalization.contexts, normalization.target
    if not contexts:
        raise StageError("no class is missing a gloss; nothing to bootstrap")

    model = llm.build(config.llm)
    stage = llm.settings(config.llm, glosses.STAGE)
    progress(f"glosses: {len(contexts)} at temperature {stage.temperature}")
    result = llm.run(
        workspace.ledger(), model, glosses.PROMPT, stage,
        [(context.iri, glosses.payload(context)) for context in contexts],
        glosses.parse,
    )

    written = [
        glosses.Gloss(iri=iri, en=value["en"], es=value["es"])
        for iri, value in result.outputs.items()
    ]
    glosses.write(seed.graph, written)
    target.write_text(seed.graph.serialize(format="turtle"))

    # Una glosa cambia el artefacto guardado pero no el estado lógico, así que la versión nueva
    # conserva el hash de su padre: re-glosar no es un estado nuevo para razonar (`ITER-APPLY`),
    # y la glosa igual queda versionada y viaja en el DAG (`PREP-NORMALIZE`).
    parent = versioning.find_by_hash(conn, versioning.state_hash(seed.graph), session_id=session)
    next_id = workspace.next_version_id()
    committed = versioning.commit(
        conn, seed.graph, version_id=next_id,
        parent_id=parent.id if parent else None,
        note="glosses (annotation-only; same logical state)", session_id=session,
    )
    return GlossBootstrap(
        written=len(written), executed=result.executed, cached=result.cached,
        failures=result.failures, in_tokens=result.in_tokens, out_tokens=result.out_tokens,
        committed=committed, parent_id=parent.id if parent else None, target=target,
    )


# ─────────────────────────────  alineación corpus/ontología inicial  ─────────────────────────────


@dataclass
class Alignment:
    version_id: str
    n_classes: int
    n_documents: int
    report: object
    declared: dict[str, int] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def decided(self) -> bool:
        """Sin términos declarados esto es diagnóstico; con ellos, veredicto."""
        return bool(self.declared)


def alignment(
    workspace: Workspace, *, version: str | None = None, terms: list[str] | None = None
) -> Alignment:
    """¿El corpus habla de lo que la ontología inicial nombra? (`DEBT-QUALITATIVE-PAIR`)

    La cobertura global es diagnóstico y no veredicto, y eso está medido. Donde sí decide es
    con los términos declarados: si quien conoce el dominio nombra el vocabulario que lo define
    y nada de eso está en el corpus, no hay matcher que lo arregle.
    """
    session = workspace.require_session()
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    labels = [
        (target.iri, target.label)
        for target in typing_store.targets_from(graph, "label")
    ]
    if not labels:
        raise StageError(f"{version_id} no tiene clases con etiqueta")

    documents = [
        markdown.read_text()
        for markdown in (
            workspace.artifacts.markdown(row["id"])
            for row in workspace.conn.execute(
                "SELECT id FROM documents WHERE session_id = ? AND held_out = 0 ORDER BY id",
                (session,),
            )
        )
        if markdown.exists()
    ]
    if not documents:
        raise StageError("no hay documentos parseados; corré ingest primero")

    report = alignment_survey(labels, documents)
    declared = check_terms(terms, documents) if terms else {}
    return Alignment(
        version_id=version_id, n_classes=len(labels), n_documents=len(documents),
        report=report, declared=declared,
        missing=[term for term, count in declared.items() if count == 0],
    )


# ─────────────────────────────  PREP-CQ  ─────────────────────────────


def import_questions(workspace: Workspace, path: Path) -> int:
    """`PREP-CQ-USER`: preguntas escritas por el usuario, cada una con su SPARQL."""
    session = workspace.require_session()
    return cq.add(workspace.conn, cq.read_file(path), session_id=session)


@dataclass
class ProposedQuestions:
    strata: dict[str, int]
    missing_strata: list[str]
    generated: int
    kept: int
    dropped: list[tuple[str, str]]
    failures: dict
    shortfall: dict[str, int]
    deduplicated: bool


def propose_questions(
    workspace: Workspace,
    *,
    version: str | None = None,
    per_stratum: int = 12,
    seed: int = 0,
    progress: Progress = silent,
) -> ProposedQuestions:
    """`PREP-CQ-GENERATED`: generar CQ desde el corpus, filtradas antes de que las leas.

    El muestreo es estratificado porque los estratos son los que hacen posibles los tipos de
    pregunta: un pasaje que dice "no debe" es de donde sale una pregunta restrictiva, y es
    invisible en una muestra al azar de párrafos.
    """
    session = workspace.require_session()
    from .deliver import corpus_blocks

    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    blocks = corpus_blocks(conn, session_id=session)
    if not blocks:
        raise StageError("no parsed blocks; run ingest first")
    strata = cq_generation.sample(blocks, per_stratum=per_stratum, seed=seed)

    # Con paréntesis: `-` liga más fuerte que `|`, y sin ellos esto es todos los estratos más
    # tablas-si-falta, y el mensaje nombra estratos que sí se encontraron.
    missing = (set(cq_generation.STRATA) | {cq_generation.TABLE_STRATUM}) - set(strata)

    labels = [target.label for target in typing_store.targets_from(graph, "label")]
    quota = config.cq.type_quota or {name: 10 for name in cq.TYPES}
    pool = [item for passages in strata.values() for item in passages]

    payloads = [
        (cq_type, cq_generation.payload(cq_type, pool, labels, count))
        for cq_type, count in sorted(quota.items()) if count
    ]
    model = workspace.model()
    stage = llm.settings(config.llm, cq_generation.STAGE)
    progress(f"propose-cq · {len(payloads)} types")
    result = llm.run(
        workspace.ledger(), model, cq_generation.PROMPT, stage,
        payloads, cq_generation.parse,
    )

    answers = {cq_type: answer["questions"] for cq_type, answer in result.outputs.items()}
    existing = [question.question for question in cq.load(conn, status=cq.ACCEPTED,
        session_id=session)]
    existing += [question.question for question in cq.load(conn, status=cq_generation.PROPOSED,
        session_id=session)]
    similarity = workspace.text_similarity()
    filtered = cq_generation.screen(
        answers, {cq_type: pool for cq_type in answers}, existing, similarity=similarity,
    )
    cq.add(conn, filtered.kept, session_id=session)

    return ProposedQuestions(
        strata={name: len(passages) for name, passages in sorted(strata.items())},
        missing_strata=sorted(missing),
        generated=sum(len(value) for value in answers.values()),
        kept=len(filtered.kept), dropped=list(filtered.dropped),
        failures=result.failures,
        shortfall=cq_generation.shortfall(filtered.kept, quota),
        deduplicated=similarity is not None,
    )


def list_questions(workspace: Workspace, *, status: str = "proposed") -> list:
    session = workspace.require_session()
    return cq.load(workspace.conn, status=status, session_id=session)


def decide_questions(
    workspace: Workspace, ids: list[str], *, discard: bool = False
) -> tuple[str, int]:
    session = workspace.require_session()
    decision = cq.DISCARDED if discard else cq.ACCEPTED
    return decision, cq.decide(workspace.conn, ids, decision, session_id=session)


def initial_graph(workspace: Workspace) -> Graph:
    """La ontología inicial normalizada tal como la dejó `normalize`, para quien la necesite sin
    versión.

    La clave sale de la misma función que la escribe. Cuando cada punto de uso armaba su ruta,
    esta leía de `work_dir/ontology/` y `normalize` escribía bajo la sesión: nunca se
    encontraban.
    """
    artifact = workspace.artifacts.normalized_ontology()
    if not artifact.exists():
        raise StageError(f"{artifact} not found; run `normalize` first")
    return Graph().parse(data=artifact.read_text(), format="turtle")
