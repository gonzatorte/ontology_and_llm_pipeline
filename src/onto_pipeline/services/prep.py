"""Las etapas de `PREP`, sin interfaz: corpus adentro, semilla normalizada, glosas, CQ.

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
from ..ingest import discover, markdown_path
from ..ingest import ingest as ingest_documents
from ..parse import TEXT_SUFFIXES
from ..seed import NormalizedSeed, gloss_contexts, normalize_seed
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
    result = ingest_documents(workspace.config, workspace.conn, paths)
    return Ingestion(
        paths=paths, outputs=result.outputs, executed=result.executed,
        cached=result.cached, failures=result.failures,
    )


# ─────────────────────────────  PREP-NORMALIZE  ─────────────────────────────


@dataclass
class SeedNormalization:
    seed: NormalizedSeed
    target: Path
    kinds: dict[str, int]
    pending_glosses: int
    committed: versioning.Version | None
    same_state_as: versioning.Version | None
    review_added: int
    review_known: int
    review_superseded: int
    contexts: list = field(default_factory=list)

    @property
    def version_id(self) -> str:
        return (self.committed or self.same_state_as).id


def normalize(workspace: Workspace) -> SeedNormalization:
    """`PREP-NORMALIZE`: IRIs opacos, etiquetas derivadas, erratas, contextos de glosa.

    No genera glosas: eso es `generate_glosses`, que llama al modelo y por eso es una etapa
    aparte que la interfaz decide si corre.
    """
    config = workspace.config
    seed = normalize_seed(
        config.paths.seed_ontology,
        config.seed.base_iri,
        divergence_threshold=config.seed.label_divergence_threshold,
        reasoner_lib=config.paths.reasoner_lib,
    )

    target = workspace.ontology_dir() / "seed_normalized.ttl"
    seed.graph.serialize(target, format="turtle")

    conn = workspace.conn
    existing = versioning.find_by_hash(conn, versioning.state_hash(seed.graph))
    committed = None
    if existing is None:
        committed = versioning.commit(
            conn, seed.graph, version_id="v0", note="normalized seed"
        )

    contexts = gloss_contexts(seed)
    current = versioning.find_by_hash(conn, versioning.state_hash(seed.graph))
    sync = review.sync(
        conn, review.findings_from_seed(seed),
        version_id=current.id if current else "v0",
        kinds=[review.DIVERGENT_LABEL, review.PENDING_SEMANTIC_CHECK, review.TYPO],
    )

    kinds: dict[str, int] = {}
    for entity in seed.entities:
        kinds[entity.kind] = kinds.get(entity.kind, 0) + 1

    return SeedNormalization(
        seed=seed, target=target, kinds=kinds, pending_glosses=len(contexts),
        committed=committed, same_state_as=existing,
        review_added=sync.added, review_known=sync.already_known,
        review_superseded=sync.superseded, contexts=contexts,
    )


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
    target: Path


def generate_glosses(
    workspace: Workspace,
    normalization: SeedNormalization,
    *,
    progress: Progress = silent,
) -> GlossBootstrap:
    """`PREP-NORMALIZE-GLOSSES`.

    La glosa es contra lo que compara `ITER-MATCH`, así que esto es lo que cierra la brecha de
    falsos huérfanos que el matcher muestra mientras cada clase tiene sólo una etiqueta.
    """
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
    seed.graph.serialize(target, format="turtle")

    # Una glosa cambia el artefacto guardado pero no el estado lógico, así que la versión nueva
    # conserva el hash de su padre: re-glosar no es un estado nuevo para razonar (`ITER-APPLY`),
    # y la glosa igual queda versionada y viaja en el DAG (`PREP-NORMALIZE`).
    parent = versioning.find_by_hash(conn, versioning.state_hash(seed.graph))
    next_id = f"v{conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0]}"
    committed = versioning.commit(
        conn, seed.graph, version_id=next_id,
        parent_id=parent.id if parent else None,
        note="glosses (annotation-only; same logical state)",
    )
    return GlossBootstrap(
        written=len(written), executed=result.executed, cached=result.cached,
        failures=result.failures, in_tokens=result.in_tokens, out_tokens=result.out_tokens,
        committed=committed, parent_id=parent.id if parent else None, target=target,
    )


# ─────────────────────────────  alineación corpus/semilla  ─────────────────────────────


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
    """¿El corpus habla de lo que la semilla nombra? (`DEBT-QUALITATIVE-PAIR`)

    La cobertura global es diagnóstico y no veredicto, y eso está medido. Donde sí decide es
    con los términos declarados: si quien conoce el dominio nombra el vocabulario que lo define
    y nada de eso está en el corpus, no hay matcher que lo arregle.
    """
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    labels = [
        (target.iri, target.label)
        for target in typing_store.targets_from(graph, "label")
    ]
    if not labels:
        raise StageError(f"{version_id} no tiene clases con etiqueta")

    documents = [
        markdown_path(workspace.config, row["id"]).read_text(encoding="utf-8")
        for row in workspace.conn.execute(
            "SELECT id FROM documents WHERE held_out = 0 ORDER BY id"
        )
        if markdown_path(workspace.config, row["id"]).exists()
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
    return cq.add(workspace.conn, cq.read_file(path))


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
    from .deliver import corpus_blocks

    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    blocks = corpus_blocks(conn)
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
    existing = [question.question for question in cq.load(conn, status=cq.ACCEPTED)]
    existing += [question.question for question in cq.load(conn, status=cq_generation.PROPOSED)]
    similarity = workspace.text_similarity()
    filtered = cq_generation.screen(
        answers, {cq_type: pool for cq_type in answers}, existing, similarity=similarity,
    )
    cq.add(conn, filtered.kept)

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
    return cq.load(workspace.conn, status=status)


def decide_questions(
    workspace: Workspace, ids: list[str], *, discard: bool = False
) -> tuple[str, int]:
    decision = cq.DISCARDED if discard else cq.ACCEPTED
    return decision, cq.decide(workspace.conn, ids, decision)


def seed_graph(workspace: Workspace) -> Graph:
    """La semilla normalizada tal como quedó en disco, para quien la necesite sin versión."""
    path = workspace.config.paths.work_dir / "ontology" / "seed_normalized.ttl"
    if not path.exists():
        raise StageError(f"{path} not found; run normalize-seed first")
    return Graph().parse(path)
