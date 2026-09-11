"""Las etapas de `ITER`, sin interfaz: menciones, tipado, puentes, clases, axiomas, ramas.

Una etapa, una función; ninguna imprime. Los cinco puntos donde decide el usuario —zona gris,
rama, propiedad funcional, CQ, errata— están partidos en dos funciones cada uno, una que
*plantea* y otra que *registra*, porque las dos interfaces cruzan ese corte distinto: el CLI
entre dos comandos, el wizard entre una pregunta y su respuesta.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from .. import (
    axiomatization,
    branching,
    bridging,
    conflicts,
    coreference,
    enrichment,
    extraction,
    functional,
    induction,
    llm,
    mapping,
    matching,
    ontoclean,
    review,
    structural,
    typing_store,
    validation,
    versioning,
)
from ..chunking import chunk_document
from ..ingest import (
    held_out_documents,
    load_block_objects,
    markdown_path,
    process_documents,
    select_for_reload,
)
from ..store import Store
from .deliver import Comparison, corpus_blocks, publish_diff
from .workspace import Progress, StageError, Workspace, silent, table_exists

# ─────────────────────────────  lecturas compartidas  ─────────────────────────────


def orphans(conn: Store, version_id: str, *, session_id: str) -> list[dict]:
    """Las menciones que ninguna clase tipó contra esta versión."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT m.id, m.surface_text, t.runner_up, t.score FROM mention_typing t "
            "JOIN mentions m ON m.id = t.mention_id AND m.session_id = ? "
            "WHERE t.version_id = ? AND t.iri IS NULL ORDER BY m.id",
            (session_id, version_id),
        )
    ]


def label_similarity(matcher, left, right) -> list[list[float]]:
    """Coseno de cada etiqueta propuesta contra cada existente, en una pasada de encoding."""
    from ..matching import dot

    proposed = matcher.vectors_for(list(left))
    known = matcher.vectors_for(list(right))
    return [[dot(a, b) for b in known] for a in proposed]


def _documents(workspace: Workspace, doc_id: str | None, include_held_out: bool) -> list[str]:
    session = workspace.require_session()
    if doc_id:
        return [doc_id]
    ids = process_documents(workspace.conn, session_id=session)
    if include_held_out:
        ids += held_out_documents(workspace.conn, session_id=session)
    return ids


# ─────────────────────────────  ITER-EXTRACT  ─────────────────────────────


@dataclass
class DocumentExtraction:
    document_id: str
    chunks: int
    mentions: int
    rejected: int
    unlocatable: int
    in_tokens: int
    out_tokens: int
    failures: dict = field(default_factory=dict)


@dataclass
class Extraction:
    documents: list[DocumentExtraction]
    skipped: list[str]
    reload: str

    @property
    def mentions(self) -> int:
        return sum(item.mentions for item in self.documents)


def extract(
    workspace: Workspace,
    *,
    doc_id: str | None = None,
    include_held_out: bool = False,
    progress: Progress = silent,
) -> Extraction:
    """`ITER-EXTRACT`: extraer menciones candidatas de cada chunk.

    Los documentos retenidos se saltean salvo que se los pida: son el conjunto de retención, y
    correr el proceso sobre ellos mediría al pipeline contra su propia entrada.
    """
    session = workspace.require_session()
    config, conn = workspace.config, workspace.conn
    model = workspace.model()
    stage = llm.settings(config.llm, extraction.STAGE)
    ledger = workspace.ledger()

    if doc_id:
        ids, skipped = [doc_id], []
    else:
        candidates = process_documents(conn, session_id=session)
        if include_held_out:
            candidates += held_out_documents(conn, session_id=session)
        ids, skipped = select_for_reload(
            conn, candidates,
            strategy=config.iteration.reload,
            sample=config.iteration.reload_sample,
            seed=config.iteration.reload_seed, session_id=session,
        )
    if not ids:
        raise StageError(
            "nothing to extract from"
            + (f"; {len(skipped)} already processed and reload is "
               f"'{config.iteration.reload}'" if skipped else "; ingest first")
        )

    reported: list[DocumentExtraction] = []
    for identifier in ids:
        blocks = {block.id: block for block in load_block_objects(conn, identifier,
            session_id=session)}
        document_chunks = chunk_document(
            list(blocks.values()), config.chunking.target_chars, config.chunking.max_chars
        )
        if not document_chunks:
            continue

        progress(f"ITER-EXTRACT · {identifier[:40]} · {len(document_chunks)} chunks")
        result = llm.run(
            ledger, model, extraction.PROMPT, stage,
            [(chunk.id, extraction.payload(chunk)) for chunk in document_chunks],
            extraction.parse,
        )

        mentions, unlocatable, rejected = [], 0, 0
        for chunk in document_chunks:
            candidates = [
                extraction.Candidate(item["text"], item["kind"])
                for item in result.outputs.get(chunk.id, [])
            ]
            located = extraction.locate(
                chunk, candidates, blocks, max_words=config.extraction.max_mention_words
            )
            mentions.extend(located.mentions)
            unlocatable += len(located.unlocatable)
            rejected += len(located.rejected)
        extraction.persist(conn, identifier, mentions, session_id=session)

        reported.append(DocumentExtraction(
            document_id=identifier, chunks=len(document_chunks), mentions=len(mentions),
            rejected=rejected, unlocatable=unlocatable,
            in_tokens=result.in_tokens, out_tokens=result.out_tokens,
            failures=result.failures,
        ))
    return Extraction(documents=reported, skipped=skipped, reload=config.iteration.reload)


# ─────────────────────────────  ITER-COREFER  ─────────────────────────────


@dataclass
class DocumentCoreference:
    document_id: str
    mentions: int
    groups: int
    linked: int
    rejected: int
    in_tokens: int
    out_tokens: int
    failures: dict = field(default_factory=dict)


@dataclass
class Coreference:
    documents: list[DocumentCoreference]


def corefer(
    workspace: Workspace,
    *,
    doc_id: str | None = None,
    include_held_out: bool = False,
    progress: Progress = silent,
) -> Coreference:
    """`ITER-COREFER`: correferencia intra-documento sobre lo que extrajo `ITER-EXTRACT`.

    El modelo agrupa identificadores de mención, nunca spans, así que su respuesta se puede
    revisar: un marcador que no existe o que se reclama dos veces se rechaza en vez de enlazar
    en silencio las menciones equivocadas.
    """
    session = workspace.require_session()
    config, conn = workspace.config, workspace.conn
    model = workspace.model()
    stage = llm.settings(config.llm, coreference.STAGE)
    ledger = workspace.ledger()

    reported: list[DocumentCoreference] = []
    for identifier in _documents(workspace, doc_id, include_held_out):
        mentions = extraction.load(conn, identifier, session_id=session)
        if not mentions:
            continue
        marked = coreference.mark(
            markdown_path(config, identifier).read_text(encoding="utf-8"), mentions
        )

        progress(f"ITER-COREFER · {identifier[:40]} · {len(mentions)} mentions")
        result = llm.run(
            ledger, model, coreference.PROMPT, stage,
            [(identifier, coreference.payload(marked))], coreference.parse,
        )

        grouping = coreference.resolve(marked, result.outputs.get(identifier, []))
        coreference.persist(conn, grouping.assignments, session_id=session)
        reported.append(DocumentCoreference(
            document_id=identifier, mentions=len(mentions), groups=len(grouping.groups),
            linked=len(grouping.assignments),
            rejected=len(grouping.unknown_markers) + len(grouping.duplicated_markers),
            in_tokens=result.in_tokens, out_tokens=result.out_tokens,
            failures=result.failures,
        ))
    return Coreference(documents=reported)


# ─────────────────────────────  ITER-MATCH  ─────────────────────────────


@dataclass
class Matching:
    version_id: str
    total: int
    typed: int
    grey: int
    orphan: int
    orphan_rate: float
    merges: int
    unresolved: int
    n_targets: int


def match(
    workspace: Workspace,
    *,
    version: str | None = None,
    include_held_out: bool = False,
    progress: Progress = silent,
) -> Matching:
    """`ITER-MATCH`: tipar las menciones contra una versión, y después resolver entidades.

    Es el cuello de botella de calidad del pipeline. Los umbrales que lee están calibrados
    contra `craft-cl` y no contra este par: tratá los números como una primera mirada.
    """
    session = workspace.require_session()
    from ..embeddings import CrossEncoderReranker, EncoderUnavailable, SentenceTransformerEncoder
    from ..matching import ASK, Matcher

    config, conn = workspace.config, workspace.conn
    versioning.install(conn)
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    targets = typing_store.targets_from(graph, config.matching.match_against)
    if not targets:
        raise StageError("the ontology version has no labelled classes")

    rows = [
        mention
        for identifier in _documents(workspace, None, include_held_out)
        for mention in extraction.load(conn, identifier, session_id=session)
    ]
    if not rows:
        raise StageError("no mentions; run extract first")

    try:
        encoder = SentenceTransformerEncoder(config.matching.bi_encoder, config.matching.device)
        reranker = (
            CrossEncoderReranker(config.matching.cross_encoder, config.matching.device)
            if config.matching.use_cross_encoder else None
        )
    except EncoderUnavailable as exc:
        raise StageError(f"{exc}; uv sync --extra matching") from exc

    matcher = Matcher(
        encoder, reranker,
        auto_merge_threshold=config.matching.auto_merge_threshold,
        grey_zone_lower=config.matching.grey_zone_lower,
        cross_language_always_grey=config.matching.cross_language_always_grey,
        respect_declared_haskey=config.matching.respect_declared_haskey,
        blocking_strategy=config.matching.blocking_strategy,
    )
    mentions = typing_store.mentions_from(rows)

    # Lo que la ontología ya dice sobre identidad, entregado al resolvedor: sinónimos
    # declarados, claves declaradas, y la clase a la que cada mención acaba de ser tipada. Sin
    # las dos últimas `respect_declared_haskey` no puede dispararse: una clave declarada se
    # chequea contra la clase que las dos menciones recibieron, y no hay clase que chequear.
    synonyms = matching.synonym_index(targets)
    keys = {target.iri: target.has_key for target in targets if target.has_key}

    progress(f"{len(mentions)} mentions against {len(targets)} classes")
    typings = matcher.type_mentions(mentions, targets)
    split = typing_store.persist_typings(conn, version_id, typings, session_id=session)
    inferred_class = {item.mention_id: item.iri for item in typings if item.iri}
    decisions = matcher.resolve(
        mentions, synonyms=synonyms, keys=keys, inferred_class=inferred_class
    )

    entities = typing_store.entities_from(decisions)
    unresolved = {
        mention_id
        for decision in decisions if decision.action == ASK
        for mention_id in (decision.left, decision.right)
    }
    typing_store.persist_entities(conn, entities, unresolved, session_id=session)

    return Matching(
        version_id=version_id, total=split.total, typed=split.typed, grey=split.grey,
        orphan=split.orphan, orphan_rate=split.orphan_rate,
        merges=sum(1 for decision in decisions if decision.action == "merge"),
        unresolved=len(unresolved), n_targets=len(targets),
    )


# ─────────────────────────────  la zona gris  ─────────────────────────────


@dataclass
class GreyPair:
    """Una pregunta de zona gris, ya legible: el par que el matcher no decide solo."""

    mention_id: str
    surface_text: str
    document_id: str
    iri: str | None
    candidate: str
    runner_up_iri: str | None
    runner_up: str
    score: float


@dataclass
class GreyQueue:
    version_id: str
    pairs: list[GreyPair]


def grey_pending(
    workspace: Workspace, *, version: str | None = None, limit: int | None = None
) -> GreyQueue:
    """Los pares de zona gris sin contestar (`ITER-MATCH`).

    La política conservadora no los tipa, y nada río abajo los trata como tipados. Esperan acá
    en vez de decidirse por un umbral, que es el motivo entero de que la zona exista.
    """
    session = workspace.require_session()
    version_id = workspace.resolve_version(version)
    labels = versioning.label_index(workspace.graph(version_id))
    rows = typing_store.pending(workspace.conn, version_id, limit or 0, session_id=session)
    return GreyQueue(
        version_id=version_id,
        pairs=[
            GreyPair(
                mention_id=row["mention_id"], surface_text=row["surface_text"],
                document_id=row["document_id"], iri=row["iri"],
                candidate=versioning.short_name(row["iri"], labels) if row["iri"] else "",
                runner_up_iri=row["runner_up"],
                runner_up=(
                    versioning.short_name(row["runner_up"], labels) if row["runner_up"] else ""
                ),
                score=row["score"],
            )
            for row in rows
        ],
    )


def grey_answer(
    workspace: Workspace,
    mention_id: str,
    *,
    version: str | None = None,
    to: str | None = None,
    none_of_these: bool = False,
    comment: str = "",
) -> str | None:
    """Contestar un par de zona gris. Devuelve el IRI elegido, o None por «ninguna».

    «Ninguna» es una respuesta de verdad y a menudo la correcta: la mención queda huérfana y
    llega a la inducción, que es donde un concepto genuinamente nuevo pertenece. Las respuestas
    sobreviven al próximo `match` — preguntar lo mismo en cada corrida es como se entrena a
    alguien a dejar de contestar.
    """
    session = workspace.require_session()
    version_id = workspace.resolve_version(version)
    row = workspace.conn.execute(
        "SELECT iri, score FROM mention_typing WHERE mention_id = ? AND version_id = ?",
        (mention_id, version_id),
    ).fetchone()
    if row is None:
        raise StageError(f"no typing for {mention_id!r} against {version_id}")
    if to is None and not none_of_these:
        raise StageError("say which class it really is, or answer «none of these»")

    chosen = None if none_of_these else to
    typing_store.answer(
        workspace.conn, mention_id, chosen, offered=row["iri"], score=row["score"],
        why=comment, session_id=session,
    )
    workspace.note(
        "decision", f"zona gris: {mention_id} → {chosen or 'ninguna'}",
        {"mention": mention_id, "iri": chosen},
    )
    return chosen


def grey_labels(workspace: Workspace) -> list[dict]:
    """Las etiquetas aceptar/rechazar que estas respuestas acumularon (`ITER-TUNE`).

    Nadie las anota a propósito: son un subproducto de alguien haciendo su trabajo, y son la
    única señal de entrenamiento que este diseño produce para ajustar el re-ranker.
    """
    session = workspace.require_session()
    return typing_store.labels(workspace.conn, session_id=session)


def grey_export(workspace: Workspace, path: Path) -> int:
    session = workspace.require_session()
    rows = typing_store.labels(workspace.conn, session_id=session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8"
    )
    return len(rows)


# ─────────────────────────────  ITER-BRIDGE  ─────────────────────────────


@dataclass
class Bridging:
    version_id: str
    orphans: int
    asked: int
    bridges: list
    declined: int
    covered: int
    labels: dict[str, str]
    executed: int = 0
    cached: int = 0
    failures: dict = field(default_factory=dict)
    in_tokens: int = 0
    out_tokens: int = 0


def bridge(
    workspace: Workspace, *, version: str | None = None, progress: Progress = silent
) -> Bridging:
    """`ITER-BRIDGE`: relacionar huérfanas con clases de la ontología inicial por conocimiento del
    mundo.

    Corre entre `match` e `induce`, y correrlo no es opcional si `induce` va a correr: si no,
    toda mención que la ontología inicial sí cubría pero el matcher no conectó se vuelve una clase
    inducida espuria.

    Al modelo nunca se le pide OWL. Recibe un sintagma y una lista corta de clases candidatas,
    y contesta una pregunta atómica — un ejemplo de eso, un tipo de eso, o ninguna. Una clase
    que no se le ofreció es una respuesta rechazada, no un puente.
    """
    config, conn = workspace.config, workspace.conn
    session = workspace.require_session()
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    found = orphans(conn, version_id, session_id=session)
    if not found:
        raise StageError(f"no orphan mentions against {version_id}; run match first")

    targets = typing_store.targets_from(graph, config.matching.match_against)
    if not targets:
        raise StageError(f"{version_id} has no classes to bridge to")

    matcher = workspace.matcher()
    surfaces = [row["surface_text"] for row in found]
    items = bridging.candidates(
        found, targets,
        matcher.vectors_for(surfaces),
        matcher.vectors_for([target.text for target in targets]),
        n_candidates=config.bridging.n_candidates,
        min_score=config.bridging.min_candidate_score,
    )
    labels = {target.iri: target.label for target in targets}
    if not items:
        return Bridging(
            version_id=version_id, orphans=len(found), asked=0, bridges=[], declined=0,
            covered=0, labels=labels,
        )

    stage = llm.settings(config.llm, bridging.STAGE)
    progress(f"ITER-BRIDGE · {len(items)} phrases from {len(found)} orphan mentions")
    result = llm.run(
        workspace.ledger(), workspace.model(), bridging.PROMPT, stage,
        [(item.id, bridging.payload(item)) for item in items],
        bridging.parse,
    )

    bridges, declined = [], 0
    for item in items:
        answer = result.outputs.get(item.id)
        if not answer:
            continue
        built = bridging.bridges_from(item, answer)
        if built is None:
            declined += 1
            continue
        bridges.append(built)
    bridging.persist(conn, version_id, bridges)

    return Bridging(
        version_id=version_id, orphans=len(found), asked=len(items), bridges=bridges,
        declined=declined, covered=sum(len(item.mention_ids) for item in bridges),
        labels=labels, executed=result.executed, cached=result.cached,
        failures=result.failures, in_tokens=result.in_tokens, out_tokens=result.out_tokens,
    )


# ─────────────────────────────  ITER-INDUCE  ─────────────────────────────


@dataclass
class Induction:
    version_id: str
    orphans: int
    bridged: int
    clusters: int
    proposals: list
    declined: int
    duplicates: dict
    labels: dict
    failures: dict = field(default_factory=dict)
    in_tokens: int = 0
    out_tokens: int = 0
    no_bridges: bool = False


def induce(
    workspace: Workspace, *, version: str | None = None, progress: Progress = silent
) -> Induction:
    """`ITER-INDUCE`: volver clases propuestas a las menciones huérfanas.

    El código agrupa, el modelo nombra. No se aplica nada: una propuesta registra la clase
    existente más cercana como padre *candidato* para que `ITER-AXIOMATIZE` decida, no como
    subsunción asertada.
    """
    config, conn = workspace.config, workspace.conn
    session = workspace.require_session()
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    found = orphans(conn, version_id, session_id=session)
    if not found:
        raise StageError(f"no orphan mentions against {version_id}; run match first")

    # Una mención que un puente ya explica no es huérfana: la ontología inicial sí la cubre, lo
    # dijo el
    # conocimiento del mundo, e inducirle una clase sería la clase espuria que `ITER-BRIDGE`
    # existe para evitar.
    bridged = bridging.bridged_mentions(conn, version_id)
    if bridged:
        found = [row for row in found if row["id"] not in bridged]
        if not found:
            return Induction(
                version_id=version_id, orphans=0, bridged=len(bridged), clusters=0,
                proposals=[], declined=0, duplicates={}, labels={},
            )

    matcher = workspace.matcher()
    surfaces = [row["surface_text"] for row in found]
    clusters = induction.cluster(
        [row["id"] for row in found], surfaces, matcher.vectors_for(surfaces),
        threshold=config.induction.similarity_threshold,
        min_support=config.induction.min_support,
    )
    labels = {target.iri: target for target in typing_store.targets_from(graph, "label")}
    if not clusters:
        return Induction(
            version_id=version_id, orphans=len(found), bridged=len(bridged), clusters=0,
            proposals=[], declined=0, duplicates={}, labels=labels,
            no_bridges=not bridged,
        )

    runner_up = {row["id"]: row["runner_up"] for row in found}
    for item in clusters:
        nearest = labels.get(runner_up.get(item.mention_ids[0]) or "")
        if nearest is not None:
            item.nearest_iri, item.nearest_label = nearest.iri, nearest.label
            item.nearest_gloss = nearest.gloss or ""

    stage = llm.settings(config.llm, induction.STAGE)
    progress(f"naming {len(clusters)} clusters from {len(found)} orphans")
    result = llm.run(
        workspace.ledger(), workspace.model(), induction.PROMPT, stage,
        [
            (item.id, induction.payload(
                item, max_phrases=config.induction.max_phrases_in_prompt))
            for item in clusters
        ],
        induction.parse,
    )

    proposals, declined = [], 0
    for item in clusters:
        answer = result.outputs.get(item.id)
        if not answer:
            continue
        if not answer.get("is_a_class"):
            declined += 1
            continue
        proposals.append(induction.Proposal(
            cluster_id=item.id, label=answer["label"], gloss=answer.get("gloss", ""),
            criterion=answer["criterion"], support=item.support,
            mention_ids=item.mention_ids, nearest_iri=item.nearest_iri,
            nearest_score=item.nearest_score,
        ))
    induction.persist(conn, version_id, proposals)

    # El nombre del cluster contra el inventario, ahora que existe. El matcher falló sobre las
    # menciones sueltas; si el nombre del grupo coincide con una clase que ya está, esas
    # menciones eran falsos huérfanos y la clase propuesta sería un duplicado.
    duplicates = induction.redundant(
        proposals, [target.label for target in labels.values()],
        lambda left, right: label_similarity(matcher, left, right),
        threshold=config.induction.redundant_threshold,
    )
    return Induction(
        version_id=version_id, orphans=len(found), bridged=len(bridged),
        clusters=len(clusters), proposals=proposals, declined=declined,
        duplicates=duplicates, labels=labels, failures=result.failures,
        in_tokens=result.in_tokens, out_tokens=result.out_tokens,
        no_bridges=not bridged,
    )


# ─────────────────────────────  ITER-VALIDATE  ─────────────────────────────


@dataclass
class Chain:
    """El veredicto de la cadena completa sobre un grafo candidato.

    Guarda los objetos crudos y no un booleano, porque quien muestra esto tiene que poder decir
    *qué* rechazó y con qué justificación. **ELK nunca devuelve `OK`**: su silencio sólo
    significa que el axioma ofensor pudo haber sido ignorado.
    """

    elk: object
    hermit: object
    ontoclean: validation.Verdict
    pitfalls: validation.Verdict
    structural: object
    missing_imports: list[str] = field(default_factory=list)

    @property
    def reasoner_rejected(self) -> bool:
        from ..reasoning import REJECTED

        return (
            self.elk.verdict == REJECTED
            or not self.hermit.consistent
            or bool(self.hermit.unsatisfiable)
        )

    @property
    def blocked(self) -> bool:
        return self.reasoner_rejected or self.ontoclean.rejected

    @property
    def structural_only(self) -> bool:
        """Hallazgos de forma: advertencias, y el spec deja pasarlas a propósito."""
        return not self.blocked and self.structural.rejected


def run_chain(workspace: Workspace, version_id: str, graph: Graph) -> Chain:
    """`ITER-VALIDATE-1-ELK`, `-2-HERMIT`, `-4-ONTOCLEAN`, `-5-PITFALLS`, `-7-STRUCTURE`.

    Una sola implementación: `axiomatize` y `branch` tenían dos copias de esto y era cuestión
    de tiempo que se separaran — una rama cambia *qué* axiomas se aplican, nunca si el
    razonador puede rechazarlos.
    """
    reasoners = workspace.reasoners(
        why="Applying without the reasoner would skip the filter that makes this safe."
    )
    ontology = reasoners.load(graph)
    return Chain(
        elk=reasoners.elk(
            ontology, coverage_threshold=workspace.config.reasoner.elk_coverage_threshold
        ),
        hermit=reasoners.hermit(ontology),
        ontoclean=ontoclean_verdict(workspace.conn, version_id, graph),
        pitfalls=validation.pitfalls(graph),
        structural=structural.check(graph),
        missing_imports=list(reasoners.missing_imports),
    )


def ontoclean_verdict(conn, version_id: str, graph: Graph) -> validation.Verdict:
    """`ITER-VALIDATE-4-ONTOCLEAN`. Reporta lo que pudo chequear, nunca lo que supuso."""
    labels = ontoclean.load(conn, version_id)
    if not labels:
        return validation.Verdict(
            "OntoClean", validation.SKIPPED,
            "no metaproperties; run `metaproperties` to label the classes",
        )
    violations, checked, skipped = ontoclean.check(graph, labels)
    coverage = f"{checked} subsumption(s) checked, {skipped} unlabelled"
    if not violations:
        return validation.Verdict("OntoClean", validation.PASS, coverage)
    names = versioning.label_index(graph)
    return validation.Verdict(
        "OntoClean", validation.REJECT, f"{len(violations)} violation(s); {coverage}",
        [
            f"{versioning.short_name(item.child, names)} ⊑ "
            f"{versioning.short_name(item.parent, names)} — {item.detail}"
            for item in violations
        ],
    )


def shapes_verdict(workspace: Workspace, version_id: str) -> validation.Verdict:
    """`ITER-VALIDATE-3-SHACL`, sobre el ABox y no sobre la TBox.

    Las restricciones de forma son sobre los datos de instancia. Sin shapes escritas reporta
    que no corrió, que no es lo mismo que pasar.
    """
    shapes_path = workspace.config.paths.work_dir / "shapes.ttl"
    shapes_graph = validation.load_shapes(shapes_path)
    if shapes_graph is None:
        return validation.Verdict(
            "SHACL", validation.SKIPPED, f"no shapes at {shapes_path.name}; nothing to check"
        )
    abox = workspace.abox_path(version_id)
    if not abox.exists():
        return validation.Verdict(
            "SHACL", validation.SKIPPED, "no ABox for this version; run regenerate"
        )
    # A un Dataset y después aplanado: el ABox guarda su procedencia en grafos con nombre, y
    # parsear TriG derecho a un Graph se queda en silencio sólo con el default — ahí toda shape
    # no encontraría objetivos y conformaría sobre nada.
    dataset = Dataset()
    dataset.parse(str(abox), format="trig")
    try:
        return validation.shapes(mapping.flatten(dataset), shapes_graph)
    except validation.ShapesUnavailable as exc:
        return validation.Verdict("SHACL", validation.SKIPPED, str(exc)[:60])


@dataclass
class Validation:
    version_id: str
    profile: object
    chain: Chain
    shacl: validation.Verdict
    target_profile: str = ""
    elk_only: bool = False


def validate(workspace: Workspace, *, version: str | None = None) -> Validation:
    """La cadena de filtros sobre una versión, más la detección de perfil de `PREP-NORMALIZE`.

    Si ELK ya rechazó, HermiT no corre: el hallazgo de ELK es real y el más caro no aporta.
    """
    from ..reasoning import REJECTED

    versioning.install(workspace.conn)
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    reasoners = workspace.reasoners()
    ontology = reasoners.load(graph)
    profile = reasoners.profile(ontology)
    elk = reasoners.elk(
        ontology, coverage_threshold=workspace.config.reasoner.elk_coverage_threshold
    )
    if elk.verdict == REJECTED:
        chain = Chain(
            elk=elk, hermit=_NoHermit(),
            ontoclean=ontoclean_verdict(workspace.conn, version_id, graph),
            pitfalls=validation.pitfalls(graph), structural=structural.check(graph),
            missing_imports=list(reasoners.missing_imports),
        )
        return Validation(
            version_id=version_id, profile=profile, chain=chain,
            shacl=shapes_verdict(workspace, version_id),
            target_profile=workspace.config.owl_profile.target, elk_only=True,
        )

    chain = Chain(
        elk=elk, hermit=reasoners.hermit(ontology),
        ontoclean=ontoclean_verdict(workspace.conn, version_id, graph),
        pitfalls=validation.pitfalls(graph), structural=structural.check(graph),
        missing_imports=list(reasoners.missing_imports),
    )
    return Validation(
        version_id=version_id, profile=profile, chain=chain,
        shacl=shapes_verdict(workspace, version_id),
        target_profile=workspace.config.owl_profile.target,
    )


@dataclass
class _NoHermit:
    """HermiT no corrió porque ELK ya rechazó. No es «consistente»: es que no se preguntó."""

    consistent: bool = True
    unsatisfiable: tuple = ()
    justifications: dict = field(default_factory=dict)
    ran: bool = False


# ─────────────────────────────  aplicar axiomas  ─────────────────────────────

REASONER = "reasoner"
ONTOCLEAN = "ontoclean"
STRUCTURE = "structure"
LOOP = "loop"


@dataclass
class Application:
    chain: Chain
    committed: versioning.Version | None = None
    refused: str = ""                  # "" | reasoner | ontoclean | structure | loop
    loop_version: versioning.Version | None = None
    before: int = 0
    after: int = 0
    diff: Comparison | None = None

    @property
    def applied(self) -> bool:
        return self.committed is not None


def apply_axioms(
    workspace: Workspace,
    version_id: str,
    graph: Graph,
    axioms: list,
    *,
    note: str,
    branch_id: str | None = None,
    override_structural: bool = False,
    progress: Progress = silent,
) -> Application:
    """La cadena de validación, y después una versión.

    Idéntico para `axiomatize` y para una rama elegida: una rama cambia qué axiomas se aplican,
    nunca si el razonador puede rechazarlos. Los hallazgos estructurales son advertencias sobre
    la forma y el spec deja pasarlas explícitamente; el rechazo del razonador y el de OntoClean
    no se pasan por arriba de ninguna manera.
    """
    session = workspace.require_session()
    conn = workspace.conn
    candidate = axiomatization.apply(graph, axioms)
    progress("validating the candidate ontology")
    chain = run_chain(workspace, version_id, candidate)
    result = Application(
        chain=chain, before=len(graph), after=len(candidate)
    )

    if chain.reasoner_rejected:
        result.refused = REASONER
        return result
    if chain.ontoclean.rejected:
        result.refused = ONTOCLEAN
        return result
    if chain.structural.rejected and not override_structural:
        result.refused = STRUCTURE
        return result

    seen = versioning.find_by_hash(conn, versioning.state_hash(candidate), session_id=session)
    if seen is not None:
        # Volver a un estado que ya está en el DAG está permitido, pero explícitamente: si se
        # commiteara igual, el DAG diría que la iteración avanzó cuando no avanzó.
        result.refused = LOOP
        result.loop_version = seen
        return result

    next_id = workspace.next_version_id()
    result.committed = versioning.commit(
        conn, candidate, version_id=next_id, parent_id=version_id, iteration=1,
        branch_id=branch_id, note=note, session_id=session,
    )
    result.diff = publish_diff(workspace, result.committed.id)
    workspace.note(
        "stage", f"versión {result.committed.id}: {note}",
        {"version": result.committed.id, "axioms": len(axioms)},
    )
    return result


# ─────────────────────────────  ITER-AXIOMATIZE  ─────────────────────────────


@dataclass
class Axiomatization:
    version_id: str
    judgements: dict
    tally: dict[str, int]
    minted: list
    axioms: list
    uncited: list
    repeats: dict
    refused: dict
    application: Application | None = None
    labels: dict[str, str] = field(default_factory=dict)


def axiomatize(
    workspace: Workspace,
    *,
    version: str | None = None,
    override_structural: bool = False,
    progress: Progress = silent,
) -> Axiomatization:
    """Volver axiomas las clases propuestas, validarlos y commitear una versión.

    Al modelo se le hace una pregunta atómica por propuesta —un tipo de, un ejemplo de, o
    ninguna— y el código escribe el OWL.
    """
    session = workspace.require_session()
    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    proposals = induction.load(conn, version_id)
    if not proposals:
        raise StageError(f"no proposed classes against {version_id}; run induce first")

    targets = typing_store.targets_from(graph, "label")
    label_to_iri = {target.label: target.iri for target in targets}
    by_iri = {target.iri: target for target in targets}
    support = {
        proposal["id"]: [
            row["mention_id"]
            for row in conn.execute(
                "SELECT mention_id FROM proposed_class_mentions WHERE proposed_id = ?",
                (proposal["id"],),
            )
        ]
        for proposal in proposals
    }

    # Varios candidatos con sus definiciones, no el único subcampeón que sugirió el matcher:
    # sobre datos reales ése suele no tener nada que ver, y un padre forzado es peor que
    # ninguno.
    payloads = []
    shapes: dict[str, str] = {}
    matcher = workspace.matcher()
    has_history = bool(
        conn.execute(
            "SELECT 1 FROM decisions WHERE session_id = ? LIMIT 1", (session,)
        ).fetchone()
        if table_exists(conn, "decisions") else None
    )
    for proposal in proposals:
        nearest = by_iri.get(proposal["nearest_iri"] or "")
        candidates = [{"label": nearest.label, "gloss": nearest.gloss}] if nearest else []
        for target in targets:
            if len(candidates) >= config.axiomatization.n_candidates:
                break
            if nearest is None or target.iri != nearest.iri:
                candidates.append({"label": target.label, "gloss": target.gloss})
        phrases = [
            row["surface_text"]
            for row in conn.execute(
                "SELECT DISTINCT m.surface_text FROM proposed_class_mentions p "
                "JOIN mentions m ON m.id = p.mention_id AND m.session_id = ? "
                "WHERE p.proposed_id = ? LIMIT 12",
                (session, proposal["id"]),
            )
        ]
        # Lo que se decidió antes sobre una propuesta de esta forma. Requiere la forma normal,
        # que es lo que hace comparable una propuesta de hoy con una de tres iteraciones atrás
        # aunque los IRIs sean otros.
        shape = axiomatization.normal_form(
            [
                axiomatization.Axiom("urn:proposal", "prefLabel", literal=proposal["label"]),
                axiomatization.Axiom(
                    "urn:proposal", "subClassOf", proposal["nearest_iri"] or "urn:none"
                ),
            ],
            {target.iri: target.label for target in targets},
        )
        shapes[proposal["id"]] = shape
        precedents = branching.precedents_like(
            conn, shape, lambda left, right: label_similarity(matcher, left, right)
        , session_id=session) if has_history else []
        payloads.append(
            (proposal["id"], axiomatization.payload(proposal, candidates, phrases, precedents))
        )

    stage = llm.settings(config.llm, axiomatization.STAGE)
    progress(f"axiomatize · {len(proposals)} proposals")
    result = llm.run(
        workspace.ledger(), workspace.model(), axiomatization.PROMPT, stage,
        payloads, axiomatization.parse,
    )

    judgements = {
        proposal_id: axiomatization.Judgement(**answer)
        for proposal_id, answer in result.outputs.items()
    }
    assembly = axiomatization.assemble(
        proposals, judgements, base_iri=config.initial_ontology.base_iri,
        label_to_iri=label_to_iri, support=support,
    )
    # `ITER-VALIDATE-6-EVIDENCE`, antes de guardar nada: un axioma `textual` tiene que citar las
    # menciones de las que salió. Aplicado a `world_knowledge` borraría justo los puentes que
    # hacen útil a la ontología inicial, así que a ésos no se les aplica.
    assembly.axioms, uncited = validation.evidence(assembly.axioms)
    axiomatization.persist(conn, version_id, assembly.axioms)

    # Re-proposición: lo mismo que ya se descartó, volviendo con otros IRIs. Se avisa y no se
    # bloquea — con ontología inicial reorganizable un rechazo no es permanente (`ITER-FEEDBACK`).
    repeats = {
        proposal_id: previous
        for proposal_id, shape in shapes.items()
        if (previous := branching.already_rejected(conn, shape, session_id=session))
    }

    tally: dict[str, int] = {}
    for judgement in judgements.values():
        tally[judgement.relation] = tally.get(judgement.relation, 0) + 1

    report = Axiomatization(
        version_id=version_id, judgements=judgements, tally=tally, minted=assembly.minted,
        axioms=assembly.axioms, uncited=uncited, repeats=repeats, refused=assembly.rejected,
        labels=versioning.label_index(graph),
    )
    if not assembly.axioms:
        return report
    report.application = apply_axioms(
        workspace, version_id, graph, assembly.axioms,
        note=f"{len(assembly.minted)} induced classes, applied directly",
        override_structural=override_structural, progress=progress,
    )
    return report


# ─────────────────────────────  ITER-BRANCH  ─────────────────────────────


@dataclass
class Branching:
    version_id: str
    decisions: list
    automatic: bool
    axioms: list
    labels: dict[str, str]
    pre_existing: list = field(default_factory=list)
    no_reasoner: str = ""
    no_similarity: bool = False
    application: Application | None = None

    @property
    def open_decisions(self) -> list:
        return [] if self.automatic else self.decisions


def survey_branches(
    workspace: Workspace, *, version: str | None = None, progress: Progress = silent
) -> Branching:
    """Encontrar los ejes de decisión en los axiomas propuestos, y ponérselos al usuario.

    Nada acá le pide alternativas a un modelo — la única prohibición explícita del spec para
    esta etapa, porque un modelo al que se le piden tres produce tres correlacionadas. Los ejes
    salen del razonador, que encuentra los conflictos lógicos, y de un catálogo fijo de
    compromisos de modelado, que ningún razonador encuentra porque los dos lados son
    consistentes.

    Lo habitual es que no haya eje, y entonces se aplica todo y se dice que fue así.
    """
    session = workspace.require_session()
    from ..reasoning import ReasonerUnavailable

    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    axioms = axiomatization.load(conn, version_id)
    if not axioms:
        raise StageError(f"no proposed axioms against {version_id}; run axiomatize first")

    labels = versioning.label_index(graph)
    proposals = {row["id"]: row for row in induction.load(conn, version_id)}
    situation = _situation(config, proposals, axioms, labels)

    no_reasoner = ""
    try:
        reasoners = workspace.reasoners()
        progress("looking for logical axes")
        hermit = reasoners.hermit(reasoners.load(axiomatization.apply(graph, axioms)))
        conflict_sets, pre_existing = branching.conflict_sets(axioms, hermit.justifications)
    except (StageError, ReasonerUnavailable) as exc:
        # Un eje de modelado no es uno lógico: el catálogo sigue aplicando, y decirlo es más
        # útil que negarse a correr el comando entero.
        no_reasoner, conflict_sets, pre_existing = str(exc), [], []

    axes = branching.logical_axes(conflict_sets, axioms, labels)
    similarity = workspace.text_similarity()
    axes += branching.modelling_axes(
        situation,
        similarity=similarity,
        min_group=config.branching.min_group,
        min_separation=config.branching.min_criterion_separation,
    )
    plan = branching.plan(
        axes, axioms, max_branches=config.branching.max_branches,
        separate_independent_axes=config.branching.present_independent_axes_separately,
    )

    report = Branching(
        version_id=version_id, decisions=plan.decisions, automatic=plan.automatic,
        axioms=axioms, labels=labels, pre_existing=pre_existing, no_reasoner=no_reasoner,
        no_similarity=similarity is None,
    )
    if plan.automatic:
        if config.branching.auto_apply_when_no_axis:
            report.application = apply_axioms(
                workspace, version_id, graph, axioms,
                note=f"{len(axioms)} axioms applied automatically: no decision axis",
                progress=progress,
            )
        return report

    by_id = {axiom.id: axiom for axiom in axioms}
    support = {axiom.id: axiom.support for axiom in axioms}
    orphan_total = len({mention for axiom in axioms for mention in axiom.support})
    entities = len({axiom.subject_iri for axiom in axioms})
    tally = branching.history(conn, session_id=session)
    for decision in plan.decisions:
        for candidate in decision.branches:
            candidate.score = branching.score(
                candidate, support=support, orphan_total=orphan_total, entities=entities,
                history=tally,
            )
            kept = [by_id[axiom_id] for axiom_id in candidate.add_axioms]
            candidate.state_hash = versioning.state_hash(axiomatization.apply(graph, kept))
            seen = versioning.find_by_hash(conn, candidate.state_hash, session_id=session)
            if seen is not None:
                candidate.note = f"returns to {seen.id}"
        decision.branches = branching.rank(
            decision.branches, limit=config.branching.max_branches
        )
    branching.persist(conn, version_id, plan.decisions)
    return report


@dataclass
class BranchChoice:
    branch_id: str
    kept: int
    given_up: int
    application: Application
    settled: bool
    invalid: list[str] = field(default_factory=list)


def choose_branch(
    workspace: Workspace,
    branch_id: str,
    *,
    version: str | None = None,
    why: str = "",
    invalid: list[str] | None = None,
    override_structural: bool = False,
    progress: Progress = silent,
) -> BranchChoice:
    """Commitear el estado que una rama nombra, y recién después asentar la decisión.

    En ese orden a propósito: el razonador todavía puede rechazar la rama, y una decisión
    registrada para un estado que nunca se aplicó diría que el usuario eligió algo que la
    ontología nunca contuvo.
    """
    session = workspace.require_session()
    conn = workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)
    invalid = list(invalid or [])

    axioms = axiomatization.load(conn, version_id)
    if not axioms:
        raise StageError(f"no proposed axioms against {version_id}; run axiomatize first")
    by_id = {axiom.id: axiom for axiom in axioms}

    row = branching.find(conn, branch_id)
    if row is None:
        raise StageError(f"no branch {branch_id!r}; run `branch` to see them")

    chosen = [by_id[axiom_id] for axiom_id in json.loads(row["add_axioms"]) if axiom_id in by_id]
    application = apply_axioms(
        workspace, version_id, graph, chosen,
        note=f"branch {branch_id}: {why}" if why else f"branch {branch_id}",
        branch_id=branch_id, override_structural=override_structural, progress=progress,
    )
    choice = BranchChoice(
        branch_id=branch_id, kept=len(chosen), given_up=len(by_id) - len(chosen),
        application=application, settled=False, invalid=invalid,
    )
    if not application.applied:
        return choice

    labels = versioning.label_index(graph)
    branching.settle(
        conn, branch_id, note=why, invalid=invalid,
        state_hash=versioning.state_hash(axiomatization.apply(graph, chosen)),
        normal_forms={
            other["id"]: axiomatization.normal_form(
                [by_id[a] for a in json.loads(other["add_axioms"]) if a in by_id], labels
            )
            for other in branching.load(conn, version_id)
        }, session_id=session,
    )
    choice.settled = True
    workspace.note(
        "decision", f"rama {branch_id}" + (f": {why}" if why else ""),
        {"branch": branch_id, "invalid": invalid},
    )
    return choice


def choice_labels(decision, branch) -> str:
    """Las palabras de la opción, no `eje=opción`: el eje se imprime arriba de la tabla, y un
    id truncado por la terminal es uno sobre el que el usuario no puede actuar."""
    by_axis = {axis.id: axis for axis in decision.axes}
    return " / ".join(
        next(
            (option.label for option in by_axis[axis_id].options if option.id == option_id),
            option_id,
        )
        for axis_id, option_id in sorted(branch.choices.items())
    )


def _situation(config, proposals: dict, axioms, labels: dict[str, str]):
    """Lo que leen los detectores del catálogo: cada propuesta, y el padre del que colgaría.

    El IRI acuñado se recalcula en vez de guardarse, porque `mint_iri` es función de la
    propuesta — la misma entrada da el mismo IRI, que es lo que hace estable un re-run.
    """
    minted = {
        proposal_id: axiomatization.mint_iri(
            config.initial_ontology.base_iri, row["label"], proposal_id
        )
        for proposal_id, row in proposals.items()
    }
    parent_by_iri = {
        axiom.subject_iri: axiom.object_iri
        for axiom in axioms if axiom.predicate == "subClassOf" and axiom.object_iri
    }
    asserted = {iri for iri in minted.values() if iri in parent_by_iri}
    return branching.Situation(
        proposals={pid: dict(row) for pid, row in proposals.items()},
        parent_of={pid: parent_by_iri[iri] for pid, iri in minted.items() if iri in asserted},
        minted={pid: iri for pid, iri in minted.items() if iri in asserted},
        axioms=axioms,
        labels=labels,
    )


# ─────────────────────────────  ITER-AXIOMATIZE-ENRICH  ─────────────────────────────


@dataclass
class Enrichment:
    version_id: str
    n_classes: int
    n_blocks: int
    passages: dict
    asked: int
    changed: list
    unchanged: int
    failures: dict
    by_iri: dict
    committed: versioning.Version | None = None
    diff: Comparison | None = None
    orphans: int = 0
    dry_run: bool = False


def enrich(
    workspace: Workspace,
    *,
    version: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    progress: Progress = silent,
) -> Enrichment:
    """Mejorar las glosas con pasajes definitorios del corpus, y cosechar sinónimos.

    Los pasajes se encuentran mecánicamente, por señal definitoria — "X es un", "definimos X
    como", "también llamado". Recién ahí se le pregunta a un modelo, y sólo sobre los pasajes
    que se le muestran.

    Cada enriquecimiento registra qué documentos contribuyeron. Un match posterior de una
    mención de un documento contribuyente contra esa clase no es evidencia independiente de
    cobertura; `circular` es lo que las hace contables.
    """
    session = workspace.require_session()
    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    targets = typing_store.targets_from(graph, "label")
    if not targets:
        raise StageError(f"{version_id} has no labelled classes")
    known = {target.label.lower() for target in targets}
    known |= {alt.lower() for target in targets for alt in target.alt_labels}

    blocks = corpus_blocks(conn)
    if not blocks:
        raise StageError("no parsed blocks; run ingest first")

    found: dict[str, list] = {}
    for target in targets:
        passages = enrichment.passages_for(
            blocks, [target.label, *target.alt_labels],
            limit=config.enrichment.max_passages,
            max_chars=config.enrichment.max_passage_chars,
        )
        if len(passages) >= config.enrichment.min_passages:
            found[target.iri] = passages

    by_iri = {target.iri: target for target in targets}
    report = Enrichment(
        version_id=version_id, n_classes=len(targets), n_blocks=len(blocks), passages=found,
        asked=0, changed=[], unchanged=0, failures={}, by_iri=by_iri, dry_run=dry_run,
    )
    if not found or dry_run:
        return report

    selected = list(found.items())[:limit] if limit else list(found.items())
    payloads = [
        (iri, enrichment.payload(by_iri[iri].label, by_iri[iri].gloss, passages))
        for iri, passages in selected
    ]
    stage = llm.settings(config.llm, enrichment.STAGE)
    progress(f"enrich · {len(payloads)} classes")
    result = llm.run(
        workspace.ledger(), workspace.model(), enrichment.PROMPT, stage,
        payloads, enrichment.parse,
    )

    enrichments = [
        enrichment.verified(answer, found[iri], known=known, iri=iri)
        for iri, answer in result.outputs.items()
    ]
    changed = [item for item in enrichments if not item.empty]
    enrichment.persist(conn, version_id, changed, passages=found)

    report.asked = len(payloads)
    report.changed = changed
    report.unchanged = len(enrichments) - len(changed)
    report.failures = result.failures
    if not changed:
        return report

    enriched = enrichment.apply(graph, changed)
    # Sólo anotación, así que el estado lógico es el del padre: re-glosar no es un estado nuevo
    # para razonar (`ITER-APPLY`), y la glosa igual queda versionada y viaja en el DAG.
    next_id = workspace.next_version_id()
    report.committed = versioning.commit(
        conn, enriched, version_id=next_id, parent_id=version_id, iteration=1,
        note=f"glosses enriched from the corpus ({len(changed)} classes; same logical state)",
            session_id=session,
    )
    report.diff = publish_diff(workspace, report.committed.id)
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM mention_typing WHERE version_id = ? AND iri IS NULL",
        (version_id,),
    ).fetchone()
    report.orphans = row["n"] if row else 0
    return report


@dataclass
class Circularity:
    version_id: str
    flagged: list
    typed: int
    labels: dict[str, str]

    @property
    def rate(self) -> float:
        return len(self.flagged) / self.typed if self.typed else 0.0


def circular(workspace: Workspace, *, version: str | None = None) -> Circularity:
    """Matches cuyo propio documento ayudó a escribir la clase que matchearon.

    No son errores, y no se tiran. Son los que no deben contarse como evidencia independiente
    de cobertura: la clase se describió usando ese documento, así que el match es en parte el
    pipeline reconociendo su propia escritura.
    """
    session = workspace.require_session()
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)
    typed = workspace.conn.execute(
        "SELECT COUNT(*) AS n FROM mention_typing WHERE version_id = ? AND iri IS NOT NULL",
        (version_id,),
    ).fetchone()["n"]
    return Circularity(
        version_id=version_id,
        flagged=enrichment.circular_matches(workspace.conn, version_id, session_id=session),
        typed=typed, labels=versioning.label_index(graph),
    )


# ─────────────────────────────  ITER-VALIDATE-4-ONTOCLEAN  ─────────────────────────────


@dataclass
class Metaproperties:
    version_id: str
    n_classes: int
    written: list
    tally: dict[str, int]
    failures: dict
    labels: dict[str, str]
    unlabelled: int
    all_labelled: bool = False


def metaproperties(
    workspace: Workspace,
    *,
    version: str | None = None,
    limit: int | None = None,
    refresh: bool = False,
    progress: Progress = silent,
) -> Metaproperties:
    """Etiquetar cada clase para OntoClean, así `ITER-VALIDATE-4-ONTOCLEAN` tiene qué chequear.

    Al modelo se le hacen cuatro preguntas llanas —¿se puede dejar de ser esto? ¿se distinguen
    dos? ¿cada uno es un todo? ¿cada uno necesita otra cosa para existir?— y nunca se le pide
    la notación de OntoClean. Preguntar en la jerga consigue una respuesta sobre la jerga.

    Las etiquetas cruzan versiones a propósito: una metapropiedad es un hecho sobre el
    concepto, no sobre el estado de la ontología.
    """
    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)

    targets = typing_store.targets_from(graph, "label")
    known = ontoclean.load(conn, version_id)
    pending = [target for target in targets if refresh or target.iri not in known]
    labels = {target.iri: target.label for target in targets}
    if not pending:
        return Metaproperties(
            version_id=version_id, n_classes=len(targets), written=[], tally={}, failures={},
            labels=labels, unlabelled=0, all_labelled=True,
        )

    payloads = [
        (
            target.iri,
            ontoclean.payload(
                target.label, target.gloss,
                [
                    labels.get(str(parent), str(parent))
                    for parent in graph.objects(URIRef(target.iri), RDFS.subClassOf)
                ],
            ),
        )
        for target in (pending[:limit] if limit else pending)
    ]
    stage = llm.settings(config.llm, ontoclean.STAGE)
    progress(f"metaproperties · {len(payloads)} classes")
    result = llm.run(
        workspace.ledger(), workspace.model(), ontoclean.PROMPT, stage,
        payloads, ontoclean.parse,
    )

    written = [ontoclean.Labels(iri=iri, **answer) for iri, answer in result.outputs.items()]
    ontoclean.persist(conn, version_id, written)

    tally: dict[str, int] = {}
    for item in written:
        tally[item.rigidity] = tally.get(item.rigidity, 0) + 1
    return Metaproperties(
        version_id=version_id, n_classes=len(targets), written=written, tally=tally,
        failures=result.failures, labels=labels,
        unlabelled=len(targets) - len(ontoclean.load(conn, version_id)),
    )


# ─────────────────────────────  ITER-CONFLICTS  ─────────────────────────────


@dataclass
class Conflicts:
    version_id: str
    entities: int
    mentions: int
    found: list
    breaking: list
    items: list
    patterns: int
    review_added: int
    review_known: int
    incompatibility_source: str
    conflict_policy: str
    notes: list[str] = field(default_factory=list)


def survey_conflicts(workspace: Workspace, *, version: str | None = None) -> Conflicts:
    """Entidades que dos documentos tiparon distinto (`ITER-CONFLICTS`).

    El filtro de volumen es el punto: un conflicto sobre el que el razonador no se rompería se
    notariza sin preguntar, porque decidir caso por caso es la revisión manual que esto existe
    para evitar. Sólo los que hacen incompatible a una clase llegan a `review`, y serán pocos.
    """
    session = workspace.require_session()
    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)
    labels = versioning.label_index(graph)

    rules = mapping.rules_from_config(config.mapping, config.initial_ontology.base_iri)
    rows, typings = mapping.load_inputs(conn, version_id, session_id=session)
    if not rows:
        raise StageError("no mentions; run extract first")

    groups = {
        anchor: [
            {"id": row.id, "document_id": row.document_id, "surface_text": row.surface_text}
            for row in members
        ]
        for anchor, members in mapping.anchors(rows, rules).items()
    }
    found = conflicts.detect(
        groups, typings, accepted_zones=rules.type_from, above=conflicts.ancestors(graph),
    )
    incompatible, source, notes = _incompatibilities(
        workspace, graph, conflicts.candidate_pairs(found)
    )
    conflicts.classify(found, incompatible)

    items = conflicts.findings(
        found, labels, pattern_threshold=config.mapping.conflict_pattern_threshold
    )
    sync = review.sync(
        conn, items, version_id=version_id,
        kinds=[conflicts.FACTUAL_CONFLICT, conflicts.CONFLICT_PATTERN],
    )
    return Conflicts(
        version_id=version_id, entities=len(groups), mentions=len(rows), found=found,
        breaking=[item for item in found if item.breaks_reasoner], items=items,
        patterns=sum(1 for item in items if item.kind == conflicts.CONFLICT_PATTERN),
        review_added=sync.added, review_known=sync.already_known,
        incompatibility_source=source, conflict_policy=rules.conflict_policy, notes=notes,
    )


def _incompatibilities(workspace: Workspace, graph: Graph, pairs):
    """Lo que la TBox llama incompatible, del razonador cuando hay uno.

    La disjointness asertada-y-heredada es una cota inferior: dos clases pueden ser
    insatisfacibles juntas por razones que ningún `owl:disjointWith` declara. Caer a ella está
    bien mientras la respuesta diga con qué instrumento se midió — reportar "sin conflicto"
    desde el más débil sería reportar la ausencia del instrumento como la ausencia del
    hallazgo.
    """
    from ..reasoning import InconsistentOntology, ReasonerUnavailable

    asserted = conflicts.asserted_incompatibilities(graph)
    if not pairs:
        return asserted, "asserted disjointness", []
    try:
        reasoners = workspace.reasoners()
        return asserted | reasoners.incompatible_pairs(graph, pairs), "the reasoner", []
    except (StageError, ReasonerUnavailable) as exc:
        return asserted, "asserted disjointness", [f"no reasoner ({exc})"]
    except InconsistentOntology as exc:
        return asserted, "asserted disjointness", [str(exc)]


def mark(
    workspace: Workspace, mentions: list[str], how: str, *, comment: str = ""
) -> int:
    """Marcar falsa una aserción. `refuted` y `misextracted` son señales opuestas.

    `refuted`: el documento lo asegura y no es verdad. La aserción sale del ABox.
    `misextracted`: el documento nunca lo dijo y el extractor leyó mal. Eso es un bug de
    `ITER-EXTRACT`, también sale del ABox, y va al conjunto de evaluación.
    """
    session = workspace.require_session()
    try:
        return conflicts.mark(workspace.conn, mentions, how, comment, session_id=session)
    except ValueError as exc:
        raise StageError(str(exc)) from exc


def export_misextractions(workspace: Workspace, path: Path) -> Path:
    session = workspace.require_session()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(conflicts.export_misextractions(workspace.conn,
        session_id=session) + "\n", encoding="utf-8")
    return path


# ─────────────────────────────  propiedades funcionales  ─────────────────────────────


def _abox(workspace: Workspace, version_id: str) -> Graph:
    path = workspace.abox_path(version_id)
    if not path.exists():
        raise StageError(
            f"no ABox for {version_id}; run regenerate first — this reads the instance data, "
            "not the TBox"
        )
    dataset = Dataset()
    dataset.parse(str(path), format="trig")
    return mapping.flatten(dataset)


@dataclass
class FunctionalCandidates:
    version_id: str
    supports: list
    items: list
    review_added: int
    review_known: int
    labels: dict[str, str]


def survey_functional(
    workspace: Workspace, *, version: str | None = None
) -> FunctionalCandidates:
    """Revisar el ABox buscando candidatas a propiedad funcional, y preguntar.

    Nada acá declara funcional a una propiedad por su cuenta. Detectar funcionalidad desde el
    ABox es inválido de principio bajo mundo abierto: que cada entidad tenga un solo valor
    prueba que no se vio contraejemplo, no que no exista.
    """
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)
    abox = _abox(workspace, version_id)

    supports = functional.survey(abox)
    labels = versioning.label_index(graph)
    items = functional.findings(
        supports, labels,
        min_individuals=workspace.config.mapping.functional_min_individuals,
    )
    sync = review.sync(
        workspace.conn, items, version_id=version_id, kinds=[functional.FUNCTIONAL_CANDIDATE]
    )
    return FunctionalCandidates(
        version_id=version_id, supports=supports, items=items, review_added=sync.added,
        review_known=sync.already_known, labels=labels,
    )


@dataclass
class FunctionalDeclaration:
    property_iri: str
    name: str
    merges: list[list[str]]
    labels: dict[str, str]
    committed: versioning.Version | None = None
    diff: Comparison | None = None


def declare_functional(
    workspace: Workspace,
    property_iri: str,
    *,
    version: str | None = None,
    commit: bool = False,
) -> FunctionalDeclaration:
    """Hacer ruidosa la consecuencia silenciosa, y commitear sólo si se lo pidieron.

    Una propiedad declarada funcional por error hace que el razonador implique `owl:sameAs` y
    funda dos entidades distintas — y no levanta ninguna inconsistencia al hacerlo. Esto
    muestra exactamente qué individuos se fundirían antes de commitear nada.
    """
    session = workspace.require_session()
    version_id = workspace.resolve_version(version)
    graph = workspace.graph(version_id)
    abox = _abox(workspace, version_id)

    candidate = functional.declare(graph, property_iri)
    combined = Graph()
    for triple in candidate:
        combined.add(triple)
    for triple in abox:
        combined.add(triple)

    reasoners = workspace.reasoners(
        why="The whole point of this command is to show what the reasoner would merge."
    )
    before = reasoners.merged_individuals(_without_declaration(combined, property_iri))
    after = reasoners.merged_individuals(combined)

    labels = versioning.label_index(graph)
    result = FunctionalDeclaration(
        property_iri=property_iri, name=versioning.short_name(property_iri, labels),
        merges=[group for group in after if group not in before], labels=labels,
    )
    if not commit:
        return result

    next_id = workspace.next_version_id()
    result.committed = versioning.commit(
        workspace.conn, candidate, version_id=next_id, parent_id=version_id, iteration=1,
        note=f"{result.name} declared functional (user decision, ITER-APPLY)", session_id=session,
    )
    result.diff = publish_diff(workspace, result.committed.id)
    workspace.note(
        "decision", f"{result.name} declarada funcional",
        {"version": result.committed.id, "property": property_iri},
    )
    return result


def _without_declaration(graph: Graph, property_iri: str) -> Graph:
    """El mismo grafo menos la declaración.

    La línea de base hay que medirla, no suponerla: una ontología puede implicar fusiones por
    otras razones, y reportar ésas como obra de esta propiedad pone la culpa en el lugar
    equivocado.
    """
    declaration = (URIRef(property_iri), RDF.type, OWL.FunctionalProperty)
    stripped = Graph()
    for triple in graph:
        if triple != declaration:
            stripped.add(triple)
    return stripped


# ─────────────────────────────  ITER-APPLY-REGENERATE  ─────────────────────────────


@dataclass
class Regeneration:
    version_id: str
    wrote: bool
    target: Path | None
    rules_hash: str
    result: object | None = None
    mentions: int = 0
    already: bool = False


def regenerate(
    workspace: Workspace, *, version: str | None = None, force: bool = False
) -> Regeneration:
    """Recalcular el ABox desde la capa de menciones (`mapping_rules_plan.md`).

    No es una migración: el ABox se deriva de las menciones y de una versión de la ontología,
    así que una reorganización de la TBox nunca necesita una — cambian las reglas y esto corre
    de nuevo. Puro y de sólo lectura sobre la capa de menciones, que es el invariante que esta
    etapa podría romper sin querer.
    """
    session = workspace.require_session()
    config, conn = workspace.config, workspace.conn
    version_id = workspace.resolve_version(version)
    # Las marcas de falsedad viajan como excepciones por caso, así que una refutación llega al
    # hash de las reglas.
    rules = mapping.rules_from_config(
        config.mapping, config.initial_ontology.base_iri
    ).with_exceptions(conflicts.as_exceptions(conn, session_id=session))
    rows, typings = mapping.load_inputs(conn, version_id, session_id=session)
    if not rows:
        raise StageError("no mentions; run extract first")

    changed = versioning.record_rules(conn, version_id, rules.rules_hash())
    # Idempotente sobre (estado, reglas) — pero también sobre el artefacto: una versión ya
    # sellada cuyo archivo no está no tiene nada que saltear, tiene un ABox que falta.
    if not changed and not force and workspace.abox_path(version_id).exists():
        return Regeneration(
            version_id=version_id, wrote=False, target=workspace.abox_path(version_id),
            rules_hash=rules.rules_hash(), mentions=len(rows), already=True,
        )

    tbox = workspace.graph(version_id)
    result = mapping.regenerate(rows, typings, rules, conflicts.ancestors(tbox))
    target = workspace.abox_path(version_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.dataset.serialize(format="trig"), encoding="utf-8")
    return Regeneration(
        version_id=version_id, wrote=True, target=target, rules_hash=result.rules_hash,
        result=result, mentions=len(rows),
    )
