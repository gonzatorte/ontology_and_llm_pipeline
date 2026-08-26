"""Command line entry point."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path

import typer
from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rich.console import Console
from rich.table import Table

from . import (
    annotate,
    annotation,
    axiomatization,
    branching,
    bridging,
    calibration,
    conflicts,
    coreference,
    cq,
    cq_generation,
    enrichment,
    extraction,
    functional,
    glosses,
    induction,
    llm,
    mapping,
    matching,
    ontoclean,
    orchestration,
    review,
    stopping,
    structural,
    typing_store,
    validation,
    versioning,
)
from .chunking import chunk_document
from .config import Config
from .db import connect
from .ingest import (
    STAGE,
    discover,
    held_out_documents,
    ingest,
    load_block_objects,
    load_blocks,
    load_document,
    markdown_path,
    process_documents,
    select_for_reload,
    set_held_out,
)
from .providers import load_env_file
from .report import build_report
from .seed import gloss_contexts, normalize_seed
from .telemetry import Ledger

app = typer.Typer(add_completion=False, help="LLM-assisted ontology enrichment pipeline.")
console = Console()

ConfigOption = typer.Option(Path("config/default.yaml"), "--config", "-c", help="Config file.")
LimitOption = typer.Option(None, "--limit", "-n", help="First N documents only.")
DocumentOption = typer.Option(None, "--document", "-d", help="Specific PDFs.")
DocIdOption = typer.Option(None, "--doc-id", help="One document; default all.")
PageOption = typer.Option(None, "--page", "-p")
IterationOption = typer.Option(0, "--iteration", "-i")
VersionOption = typer.Option(None, "--version", help="Default: the newest version.")
EnvFileOption = typer.Option(None, "--env-file", help="Env file with the provider credential.")
ReleaseOption = typer.Option(False, "--release", help="Return the documents to the process.")
AgainstOption = typer.Option(None, "--against", help="Default: the version's parent.")
DiffLimitOption = typer.Option(10, "--limit", "-n", help="Axioms shown per lane; 0 for all.")
KindOption = typer.Option(None, "--kind", help="divergent_label | pending_semantic_check | typo.")
StatusOption = typer.Option(
    "open", "--status", help="open | accepted | rejected | superseded | any."
)
JsonOption = typer.Option(False, "--json", help="Machine-readable output.")
InferOption = typer.Option(
    True, "--infer/--no-infer", help="Query the entailed graph, not only the asserted one."
)
CommentOption = typer.Option("", "--comment", help="Why, in your words.")
IncludeHeldOutOption = typer.Option(
    False, "--include-held-out", help="Also process the retention set. Normally you do not."
)
ApplyOption = typer.Option(
    False, "--apply", help="Commit even with structural findings, which are warnings."
)
ForceRegenOption = typer.Option(
    False, "--force", help="Write the ABox again even if the rules did not change."
)
MarkOption = typer.Option(
    ..., "--mark", help="refuted (the document is wrong) | misextracted (B1 read it wrong)."
)
MentionsOption = typer.Option(..., "--mention", "-m", help="Repeatable: mention ids to mark.")
ExportOption = typer.Option(
    None, "--export", help="Write the misextractions to this JSONL, for the evaluation set."
)
PerStratumOption = typer.Option(
    12, "--per-stratum", help="Passages sampled per stratum. The spec says 10-15."
)
SeedOption = typer.Option(0, "--seed", help="Sampling seed; the sample is deterministic.")
CqStatusOption = typer.Option(
    "proposed", "--status", help="proposed | accepted | discarded."
)
DiscardOption = typer.Option(False, "--discard", help="Discard instead of accepting.")
ToOption = typer.Option(
    None, "--to", help="The class the mention really is. Omit with --none to orphan it."
)
NoneOfTheseOption = typer.Option(
    False, "--none", help="None of the candidates. A real answer, not a refusal."
)
ExportLabelsOption = typer.Option(
    None, "--export", help="Write the accept/reject labels to this JSONL (spec 6.3)."
)
CurveOption = typer.Option(
    False, "--curve", help="Print the accumulation curve document by document."
)
DeclareOption = typer.Option(
    None, "--declare", help="Ask what declaring this property functional would merge."
)
YesOption = typer.Option(
    False, "--yes", help="Commit the declaration after seeing what it merges."
)
RefreshOption = typer.Option(
    False, "--refresh", help="Ask again for what is already labelled."
)
DryRunOption = typer.Option(
    False, "--dry-run", help="Show what would be asked about, and ask nothing."
)
ChooseOption = typer.Option(
    None, "--choose", help="Apply this branch. Its siblings are recorded as rejected."
)
WhyOption = typer.Option("", "--why", help="Why this branch, in your words. Kept with it.")
PairOption = typer.Argument(..., help="Calibration pair: a directory under calibration_root.")
MatchAgainstOption = typer.Option(
    [], "--match-against", "-m", help="Repeatable: label | gloss | label_and_gloss."
)
CrossEncoderSweepOption = typer.Option(
    False, "--cross-encoder", help="Run each variant with and without the re-ranker."
)
HoldoutOption = typer.Option(
    None, "--holdout", help="Withhold this fraction of classes to manufacture genuine orphans."
)
KeepExcludedOption = typer.Option(
    False, "--keep-excluded",
    help="Keep classes the corpus guarantees are wrong. Shows how much error they cause.",
)


@app.callback()
def main(env_file: Path | None = EnvFileOption) -> None:
    """Env files are explicit, never auto-discovered."""
    if env_file is not None:
        names = load_env_file(env_file)
        console.print(f"[dim]loaded {', '.join(names)} from {env_file}[/]")


def _resolve_version(conn, version: str | None) -> str:
    """The named version, or the newest one. `created_at` has second precision, so two
    versions committed in the same second tie on it; rowid breaks the tie by insertion order,
    which is what "newest" means here."""
    versioning.install(conn)
    row = conn.execute(
        "SELECT id FROM versions WHERE id = COALESCE(?, id) "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (version,),
    ).fetchone()
    if row is None:
        raise typer.BadParameter(
            f"no version {version!r}" if version
            else "no ontology version; run normalize-seed first"
        )
    return row["id"]


@app.command("ingest")
def ingest_cmd(
    config_path: Path = ConfigOption,
    limit: int | None = LimitOption,
    document: list[Path] | None = DocumentOption,
) -> None:
    """A1+A2: classify pages, parse, populate the block store and the Markdown."""
    config = Config.load(config_path)
    paths = [p.resolve() for p in document] if document else discover(config.paths.corpus_root)
    if not paths:
        raise typer.BadParameter(f"no PDFs under {config.paths.corpus_root}")
    if limit:
        paths = paths[:limit]

    conn = connect(config.paths.work_dir)
    with console.status(f"ingesting {len(paths)} document(s)"):
        result = ingest(config, conn, paths)

    table = Table(
        "document", "pages", "classes", "blocks", "boilerplate", "unparsed", "table gap"
    )
    for label, summary in result.outputs.items():
        table.add_row(
            label[:44],
            str(summary["n_pages"]),
            ", ".join(f"{k}:{v}" for k, v in sorted(summary["page_classes"].items())),
            ", ".join(f"{k}:{v}" for k, v in sorted(summary["block_types"].items())),
            str(summary["boilerplate_blocks"]),
            str(len(summary["unparsed_pages"])),
            str(len(summary.get("table_gap_pages", []))),
        )
    console.print(table)
    console.print(
        f"[green]{result.executed} executed[/], {result.cached} cached, "
        f"{len(result.failures)} failed"
    )
    for label, error in result.failures.items():
        console.print(f"[red]{label}[/]: {error}")


@app.command()
def report(
    config_path: Path = ConfigOption,
    doc_id: str | None = DocIdOption,
) -> None:
    """T1: self-contained HTML for manual parser evaluation."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    ids = [doc_id] if doc_id else [
        row["id"] for row in conn.execute("SELECT id FROM documents ORDER BY id")
    ]
    if not ids:
        raise typer.BadParameter("nothing ingested yet")
    for identifier in ids:
        target = build_report(config, conn, identifier)
        console.print(f"[green]wrote[/] {target}")


@app.command("normalize-seed")
def normalize_seed_cmd(config_path: Path = ConfigOption) -> None:
    """A0: opaque IRIs, derived labels, typo detection, gloss contexts."""
    config = Config.load(config_path)
    seed = normalize_seed(
        config.paths.seed_ontology,
        config.seed.base_iri,
        divergence_threshold=config.seed.label_divergence_threshold,
    )

    ontology_dir = config.paths.work_dir / "ontology"
    ontology_dir.mkdir(parents=True, exist_ok=True)
    target = ontology_dir / "seed_normalized.ttl"
    seed.graph.serialize(target, format="turtle")

    conn = connect(config.paths.work_dir)
    existing = versioning.find_by_hash(conn, versioning.state_hash(seed.graph))
    if existing is None:
        version = versioning.commit(conn, seed.graph, version_id="v0", note="normalized seed")
        console.print(f"[green]committed[/] version {version.id} {version.state_hash[:19]}")
        _publish_diff(config, conn, version.id)
    else:
        console.print(f"[yellow]same state[/] as version {existing.id}; nothing committed")

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
    summary = Table("what", "count")
    for kind, count in sorted(kinds.items()):
        summary.add_row(kind, str(count))
    summary.add_row("classes awaiting a gloss", str(len(contexts)))
    summary.add_row("review: new findings", str(sync.added))
    summary.add_row("review: already decided or open", str(sync.already_known))
    summary.add_row("review: superseded", str(sync.superseded))
    console.print(summary)
    console.print(f"[green]wrote[/] {target}")
    console.print("run [bold]onto-pipeline review list[/] to see what needs a decision")

    if config.llm.provider == "none":
        console.print(
            f"[yellow]A0.4 skipped[/]: {len(contexts)} glosses need generation and "
            "llm.provider is 'none'. Set a provider in the config to run it."
        )
        return

    _generate_glosses(config, conn, seed, contexts, target)


def _generate_glosses(config, conn, seed, contexts, target) -> None:
    """A0.4. The gloss is what B2 matches against, so this is what closes the false-orphan
    gap the matcher shows while every class still has only a label."""
    model = llm.build(config.llm)
    stage = llm.settings(config.llm, glosses.STAGE)
    ledger = Ledger(conn, config.execution)

    with console.status(f"A0.4: {len(contexts)} glosses at temperature {stage.temperature}"):
        result = llm.run(
            ledger, model, glosses.PROMPT, stage,
            [(context.iri, glosses.payload(context)) for context in contexts],
            glosses.parse,
        )

    written = [
        glosses.Gloss(iri=iri, en=value["en"], es=value["es"])
        for iri, value in result.outputs.items()
    ]
    glosses.write(seed.graph, written)
    seed.graph.serialize(target, format="turtle")

    console.print(
        f"[green]A0.4[/]: {len(written)} glosses ({result.executed} generated, "
        f"{result.cached} cached, {len(result.failures)} failed) · "
        f"{result.in_tokens} in / {result.out_tokens} out tokens"
    )
    for iri, error in list(result.failures.items())[:5]:
        console.print(f"  [red]{iri}[/]: {error}")

    # A gloss changes the stored artifact but not the logical state, so the new version keeps
    # its parent's hash: a re-glossing is not a new state to reason about (spec 6.8), while
    # the gloss itself is still versioned and travels in the DAG (spec 4.3).
    parent = versioning.find_by_hash(conn, versioning.state_hash(seed.graph))
    next_id = f"v{conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0]}"
    version = versioning.commit(
        conn, seed.graph, version_id=next_id,
        parent_id=parent.id if parent else None,
        note="glosses (annotation-only; same logical state)",
    )
    console.print(f"[green]committed[/] {version.id} (parent {parent.id if parent else '-'})")
    _publish_diff(config, conn, version.id)


@app.command()
def status(config_path: Path = ConfigOption) -> None:
    """Telemetry: work units and cost per stage, page classes across the corpus."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    ledger = Ledger(conn, config.execution)

    stages = Table("stage", "units", "done", "failed", "in tokens", "out tokens")
    for row in conn.execute("SELECT DISTINCT stage FROM work_units ORDER BY stage"):
        stage_report = ledger.stage_report(row["stage"])
        stages.add_row(
            row["stage"],
            *(str(stage_report[key] or 0)
              for key in ("units", "done", "failed", "in_tokens", "out_tokens")),
        )
    console.print(stages)

    classes = Table("page class", "pages", "reason")
    for row in conn.execute(
        "SELECT class, COUNT(*) AS n, json_extract(signals, '$.reason') AS reason "
        "FROM page_classification GROUP BY class, reason ORDER BY n DESC"
    ):
        classes.add_row(row["class"], str(row["n"]), row["reason"] or "")
    console.print(classes)

    failures = conn.execute(
        "SELECT key, error FROM work_units WHERE status = 'failed' AND stage = ?", (STAGE,)
    ).fetchall()
    for row in failures:
        console.print(f"[red]{row['key'][:12]}[/]: {row['error']}")


@app.command("extract")
def extract_cmd(
    config_path: Path = ConfigOption,
    doc_id: str | None = DocIdOption,
    include_held_out: bool = IncludeHeldOutOption,
) -> None:
    """B1: extract candidate mentions from every chunk (spec 6.1).

    Held-out documents are skipped unless asked for: they are the retention set, and running
    the process over them would measure the pipeline against its own input.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, extraction.STAGE)
    ledger = Ledger(conn, config.execution)

    if doc_id:
        ids, skipped = [doc_id], []
    else:
        candidates = process_documents(conn)
        if include_held_out:
            candidates += held_out_documents(conn)
        ids, skipped = select_for_reload(
            conn, candidates,
            strategy=config.iteration.reload,
            sample=config.iteration.reload_sample,
            seed=config.iteration.reload_seed,
        )
    if not ids:
        raise typer.BadParameter(
            "nothing to extract from"
            + (f"; {len(skipped)} already processed and reload is "
               f"'{config.iteration.reload}'" if skipped else "; ingest first")
        )
    if skipped:
        console.print(
            f"[dim]reload '{config.iteration.reload}': re-processing {len(ids)}, "
            f"leaving {len(skipped)} as they are[/]"
        )

    table = Table(
        "document", "chunks", "mentions", "rejected", "unlocatable", "in tok", "out tok"
    )
    for identifier in ids:
        blocks = {block.id: block for block in load_block_objects(conn, identifier)}
        chunks = chunk_document(
            list(blocks.values()), config.chunking.target_chars, config.chunking.max_chars
        )
        if not chunks:
            continue

        with console.status(f"B1 · {identifier[:40]} · {len(chunks)} chunks"):
            result = llm.run(
                ledger, model, extraction.PROMPT, stage,
                [(chunk.id, extraction.payload(chunk)) for chunk in chunks],
                extraction.parse,
            )

        mentions, unlocatable, rejected = [], 0, 0
        for chunk in chunks:
            candidates = [
                extraction.Candidate(item["text"], item["kind"])
                for item in result.outputs.get(chunk.id, [])
            ]
            located = extraction.locate(
                chunk, candidates, blocks,
                max_words=config.extraction.max_mention_words,
            )
            mentions.extend(located.mentions)
            unlocatable += len(located.unlocatable)
            rejected += len(located.rejected)
        extraction.persist(conn, identifier, mentions)

        table.add_row(
            identifier[:38], str(len(chunks)), str(len(mentions)), str(rejected),
            f"[red]{unlocatable}[/]" if unlocatable else "0",
            str(result.in_tokens), str(result.out_tokens),
        )
        for chunk_id, error in list(result.failures.items())[:3]:
            console.print(f"  [red]{chunk_id}[/]: {error}")
    console.print(table)


@app.command("coref")
def coref_cmd(
    config_path: Path = ConfigOption,
    doc_id: str | None = DocIdOption,
    include_held_out: bool = IncludeHeldOutOption,
) -> None:
    """B1b: intra-document coreference over the mentions B1 extracted (spec 6.1b).

    The model groups mention identifiers, never spans, so its answer can be checked: a marker
    that does not exist or is claimed twice is rejected instead of silently linking the wrong
    mentions.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, coreference.STAGE)
    ledger = Ledger(conn, config.execution)

    if doc_id:
        ids = [doc_id]
    else:
        ids = process_documents(conn)
        if include_held_out:
            ids += held_out_documents(conn)

    table = Table("document", "mentions", "groups", "linked", "rejected", "in tok", "out tok")
    for identifier in ids:
        mentions = extraction.load(conn, identifier)
        if not mentions:
            continue
        marked = coreference.mark(
            markdown_path(config, identifier).read_text(encoding="utf-8"), mentions
        )

        with console.status(f"B1b · {identifier[:40]} · {len(mentions)} mentions"):
            result = llm.run(
                ledger, model, coreference.PROMPT, stage,
                [(identifier, coreference.payload(marked))], coreference.parse,
            )

        groups = result.outputs.get(identifier, [])
        grouping = coreference.resolve(marked, groups)
        coreference.persist(conn, grouping.assignments)

        rejected = len(grouping.unknown_markers) + len(grouping.duplicated_markers)
        table.add_row(
            identifier[:32], str(len(mentions)), str(len(grouping.groups)),
            str(len(grouping.assignments)),
            f"[red]{rejected}[/]" if rejected else "0",
            str(result.in_tokens), str(result.out_tokens),
        )
        for label, error in result.failures.items():
            console.print(f"  [red]{label}[/]: {error}")
    console.print(table)


def _orphans(conn, version_id: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT m.id, m.surface_text, t.runner_up, t.score FROM mention_typing t "
            "JOIN mentions m ON m.id = t.mention_id "
            "WHERE t.version_id = ? AND t.iri IS NULL ORDER BY m.id",
            (version_id,),
        )
    ]


def _encoder(config):
    from .embeddings import EncoderUnavailable, SentenceTransformerEncoder

    try:
        return SentenceTransformerEncoder(config.matching.bi_encoder, config.matching.device)
    except EncoderUnavailable as exc:
        raise typer.BadParameter(f"{exc}; uv sync --extra matching") from exc


@app.command("bridge")
def bridge_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
) -> None:
    """B2b: relate orphans to seed classes by world knowledge (spec 6.2b).

    Runs between `match` and `induce`, and running it is not optional if `induce` is going to
    run: every mention the seed did cover but the matcher failed to connect would otherwise
    become a spurious induced class.

    The model is never asked for OWL. It gets a phrase and a short list of candidate classes,
    and answers one atomic question — an example of it, a kind of it, or neither. A class it was
    not offered is a rejected answer, not a bridge.
    """
    from .matching import Matcher

    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    orphans = _orphans(conn, version_id)
    if not orphans:
        raise typer.BadParameter(f"no orphan mentions against {version_id}; run match first")

    targets = typing_store.targets_from(graph, config.matching.match_against)
    if not targets:
        raise typer.BadParameter(f"{version_id} has no classes to bridge to")

    matcher = Matcher(_encoder(config))
    surfaces = [row["surface_text"] for row in orphans]
    items = bridging.candidates(
        orphans, targets,
        matcher.vectors_for(surfaces),
        matcher.vectors_for([target.text for target in targets]),
        n_candidates=config.bridging.n_candidates,
        min_score=config.bridging.min_candidate_score,
    )
    if not items:
        console.print(
            f"[yellow]{len(orphans)} orphans, none with a class above "
            f"{config.bridging.min_candidate_score}[/]; nothing to ask about"
        )
        return

    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, bridging.STAGE)
    with console.status(f"B2b · {len(items)} phrases from {len(orphans)} orphan mentions"):
        result = llm.run(
            Ledger(conn, config.execution), model, bridging.PROMPT, stage,
            [(item.id, bridging.payload(item)) for item in items],
            bridging.parse,
        )

    bridges, declined = [], 0
    for item in items:
        answer = result.outputs.get(item.id)
        if not answer:
            continue
        bridge = bridging.bridges_from(item, answer)
        if bridge is None:
            declined += 1
            continue
        bridges.append(bridge)
    bridging.persist(conn, version_id, bridges)

    labels = {target.iri: target.label for target in targets}
    table = Table("phrase", "relation", "seed class", "mentions", "score")
    for bridge in sorted(bridges, key=lambda item: -len(item.mention_ids))[:15]:
        table.add_row(
            bridge.surface[:34], bridge.relation,
            labels.get(bridge.target_iri, bridge.target_iri)[:26],
            str(len(bridge.mention_ids)), f"{bridge.score:.2f}",
        )
    console.print(table)
    covered = sum(len(bridge.mention_ids) for bridge in bridges)
    console.print(
        f"[green]{len(bridges)} bridges[/] covering {covered} of {len(orphans)} orphan "
        f"mentions · {declined} phrases the model declined to connect\n"
        f"marked [bold]{bridging.WORLD_KNOWLEDGE}[/]: no citation is possible, so the evidence "
        f"filter does not apply to them (6.2b)\n"
        f"{result.executed} executed, {result.cached} cached, {len(result.failures)} failed · "
        f"{result.in_tokens} in / {result.out_tokens} out tokens"
    )
    for identifier, error in list(result.failures.items())[:5]:
        console.print(f"  [red]{identifier}[/]: {error}")


@app.command("axiomatize")
def axiomatize_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    apply_changes: bool = ApplyOption,
) -> None:
    """Turn proposed classes into axioms, validate them, and commit a new version.

    The model is asked one atomic question per proposal — a kind of, an example of, or
    neither — and the code writes the OWL. Direct application, no branches: that is the
    milestone the spec's build sequence puts before branching exists.
    """
    from .reasoning import REJECTED, Reasoners, ReasonerUnavailable

    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    proposals = induction.load(conn, version_id)
    if not proposals:
        raise typer.BadParameter(f"no proposed classes against {version_id}; run induce first")

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

    # Several candidates with their definitions, not the single runner-up the matcher
    # suggested: on real data that one is often unrelated, and a forced parent is worse
    # than none.
    payloads = []
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
                "JOIN mentions m ON m.id = p.mention_id WHERE p.proposed_id = ? LIMIT 12",
                (proposal["id"],),
            )
        ]
        payloads.append((proposal["id"], axiomatization.payload(proposal, candidates, phrases)))

    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, axiomatization.STAGE)
    with console.status(f"axiomatize · {len(proposals)} proposals"):
        result = llm.run(
            Ledger(conn, config.execution), model, axiomatization.PROMPT, stage,
            payloads, axiomatization.parse,
        )

    judgements = {
        proposal_id: axiomatization.Judgement(**answer)
        for proposal_id, answer in result.outputs.items()
    }
    assembly = axiomatization.assemble(
        proposals, judgements, base_iri=config.seed.base_iri,
        label_to_iri=label_to_iri, support=support,
    )
    # B5 filter 6, before anything is stored: a `textual` axiom must cite the mentions it came
    # from. Applied to `world_knowledge` it would delete exactly the bridges that make the seed
    # useful, so it is not applied to them (6.2b).
    assembly.axioms, uncited = validation.evidence(assembly.axioms)
    axiomatization.persist(conn, version_id, assembly.axioms)

    tally: dict[str, int] = {}
    for judgement in judgements.values():
        tally[judgement.relation] = tally.get(judgement.relation, 0) + 1
    table = Table("what", "count")
    table.add_row("proposals judged", str(len(judgements)))
    for relation, count in sorted(tally.items()):
        table.add_row(f"  {relation}", str(count))
    table.add_row("classes to mint", str(len(assembly.minted)))
    table.add_row("axioms assembled", str(len(assembly.axioms)))
    table.add_row("  dropped for lack of evidence", str(len(uncited)))
    table.add_row("proposals refused", str(len(assembly.rejected)))
    console.print(table)
    for proposal_id, why in list(assembly.rejected.items())[:5]:
        console.print(f"  [yellow]refused[/] {proposal_id}: {why}")

    if not assembly.axioms:
        console.print("nothing to apply")
        return

    candidate_graph = axiomatization.apply(graph, assembly.axioms)
    try:
        reasoners = Reasoners(
            config.paths.reasoner_lib, hermit_timeout_s=config.reasoner.hermit_timeout_s
        )
    except ReasonerUnavailable as exc:
        raise typer.BadParameter(
            f"{exc}. Applying without the reasoner would skip the filter that makes this safe."
        ) from exc

    ontology = reasoners.load(candidate_graph)
    elk = reasoners.elk(ontology, coverage_threshold=config.reasoner.elk_coverage_threshold)
    hermit = reasoners.hermit(ontology)
    metrics = structural.check(candidate_graph)

    verdict = Table("filter", "result", "detail")
    verdict.add_row("ELK", elk.verdict, elk.note[:52])
    verdict.add_row("HermiT", "consistent" if hermit.consistent else REJECTED,
                    f"{len(hermit.unsatisfiable)} unsatisfiable")
    clean = _ontoclean(conn, version_id, candidate_graph)
    smells = validation.pitfalls(candidate_graph)
    verdict.add_row("OntoClean", clean.decision, clean.note[:52])
    verdict.add_row("pitfalls", smells.decision, smells.note[:52])
    verdict.add_row("structural", REJECTED if metrics.rejected else "clean",
                    f"depth {metrics.depth} · {len(metrics.findings)} finding(s)")
    console.print(verdict)
    for finding in clean.findings[:6]:
        console.print(f"  [red]OntoClean[/] {finding}")

    blocked = (
        elk.verdict == REJECTED or not hermit.consistent or hermit.unsatisfiable
        or clean.rejected
    )
    if blocked:
        for iri, justifications in hermit.justifications.items():
            console.print(f"[red]unsatisfiable[/] {iri}")
            for axiom in (justifications[0] if justifications else []):
                console.print(f"    {axiom}")
        console.print("[red]not applied[/]: the reasoner rejected it")
        return
    if metrics.rejected:
        for finding in metrics.findings[:8]:
            console.print(f"  [yellow]{finding.check}[/] {finding.subject} — {finding.detail}")
        console.print(
            "[yellow]not applied[/]: structural findings. Fix them or run with --apply to "
            "override, which the spec allows only because these are warnings about shape."
        )
        if not apply_changes:
            return

    next_id = f"v{conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0]}"
    committed = versioning.commit(
        conn, candidate_graph, version_id=next_id, parent_id=version_id, iteration=1,
        note=f"{len(assembly.minted)} induced classes, applied directly",
    )
    console.print(
        f"[green]committed[/] {committed.id} (parent {version_id}) · "
        f"{len(graph)} -> {len(candidate_graph)} triples"
    )
    _publish_diff(config, conn, committed.id)


grey_app = typer.Typer(help="The matcher's grey zone: the pairs it will not decide alone.")
app.add_typer(grey_app, name="grey")


@grey_app.command("list")
def grey_list(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
    as_json: bool = JsonOption,
) -> None:
    """Grey-zone pairs still unanswered (spec 6.2).

    The conservative policy of 6.2 does not type these, and nothing downstream treats them as
    typed. They wait here rather than being decided by a threshold, which is the whole reason
    the zone exists.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)
    labels = versioning.label_index(graph)

    rows = typing_store.pending(conn, version_id, limit or 0)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return

    table = Table("mention", "phrase", "candidate", "score", "runner-up", "document")
    for row in rows:
        table.add_row(
            row["mention_id"], row["surface_text"][:32],
            versioning.short_name(row["iri"] or "", labels) if row["iri"] else "—",
            f"{row['score']:.3f}",
            versioning.short_name(row["runner_up"], labels) if row["runner_up"] else "—",
            row["document_id"][:24],
        )
    console.print(table)
    console.print(
        f"{len(rows)} waiting. Answer one with `onto-pipeline grey answer <mention> "
        f"--to <class>` or `--none`."
    )


@grey_app.command("answer")
def grey_answer(
    mention_id: str,
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    to: str | None = ToOption,
    none_of_these: bool = NoneOfTheseOption,
    comment: str = CommentOption,
) -> None:
    """Answer one grey-zone pair.

    `--none` is a real answer and often the right one: the mention becomes an orphan and
    reaches induction, which is where a genuinely new concept belongs. Answers survive the
    next `match` — asking the same question every run is how a system trains someone to stop
    answering.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)

    row = conn.execute(
        "SELECT iri, score FROM mention_typing WHERE mention_id = ? AND version_id = ?",
        (mention_id, version_id),
    ).fetchone()
    if row is None:
        raise typer.BadParameter(f"no typing for {mention_id!r} against {version_id}")
    if to is None and not none_of_these:
        raise typer.BadParameter("say `--to <class iri>` or `--none`")

    chosen = None if none_of_these else to
    typing_store.answer(
        conn, mention_id, chosen, offered=row["iri"], score=row["score"], why=comment
    )
    console.print(
        f"[green]recorded[/] {mention_id} → {chosen or 'orphan (none of these)'}. "
        "Re-run `match` to apply it, or it applies itself on the next run."
    )


@grey_app.command("labels")
def grey_labels(
    config_path: Path = ConfigOption,
    export: Path | None = ExportLabelsOption,
) -> None:
    """The accept/reject labels these answers have accumulated (spec 6.3).

    Nobody annotates them on purpose: they are a by-product of someone doing their work, and
    they are the only training signal this design ever produces for tuning the re-ranker. The
    measured cross-encoder result — separation -0.50 out of the box — is the argument for
    needing them, not against re-ranking.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    rows = typing_store.labels(conn)

    accepted = sum(1 for row in rows if row["accepted"])
    console.print(
        f"[bold]{len(rows)}[/] labels · {accepted} accepted, {len(rows) - accepted} rejected"
    )
    if not rows:
        console.print(
            "[dim]Answer grey-zone pairs to accumulate them: `onto-pipeline grey list`[/]"
        )
        return
    if export is not None:
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        console.print(f"[green]wrote[/] {export}")


@app.command("next")
def next_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
) -> None:
    """What to run next, and what is waiting on you.

    Five points in this design are the user's decision — the matcher's grey zone, the branch, a
    functional property, a competency question, a typo in the seed. A runner that went straight
    through them would be deciding them by default, so a pending decision outranks any stage
    that could run: everything after it would be built on an answer nobody gave.

    A stage whose input does not exist is reported as blocked rather than pending. Which of the
    two it is is the difference between advice and a checklist.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    # The env file is loaded by the app-level callback, so this reads the result rather than
    # the flag: `--env-file` before the subcommand is the one way in, and there is no second.
    has_provider = bool(os.environ.get(config.llm.api_key_env, ""))

    plan = orchestration.survey(conn, version_id, has_provider=has_provider)
    colours = {
        orchestration.DONE: "green", orchestration.READY: "bold",
        orchestration.WAITING: "yellow", orchestration.BLOCKED: "dim",
    }
    table = Table("stage", "state", "detail", title=f"against {version_id}")
    for step in plan.steps:
        table.add_row(
            step.name, f"[{colours[step.state]}]{step.state}[/]", step.detail
        )
    console.print(table)

    waiting = orchestration.blocking(plan)
    if waiting:
        console.print(
            f"[yellow]{len(waiting)} decision(s) are yours[/], and the stages after them would "
            "be built on an answer nobody gave:"
        )
        for step in waiting:
            console.print(f"  {step.name} — [bold]{step.command}[/]")
        return
    if plan.next is None:
        console.print("[green]nothing pending[/] · `onto-pipeline stop` says whether it is done")
        return
    console.print(f"[bold]next:[/] {plan.next.name} — [bold]{plan.next.command}[/]")


@app.command("stop")
def stop_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    iteration: int = IterationOption,
    curve: bool = CurveOption,
) -> None:
    """Should this stop? The four criteria of spec 10.3, with their roles.

    Competency questions are primary and the only criterion that says *what* is missing.
    Novelty saturation is secondary and automatic. The accumulation curve is a diagnostic and
    never stops anything — it is the one that says whether the problem is the pipeline or the
    corpus, and the only one that breaks the circle the other three sit in. The budget is hard
    and arbitrary, and the only one that always terminates.

    Mention coverage is deliberately absent: a system optimizes what is measured, and an
    umbrella class maximizes coverage while destroying the conceptual value.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)

    assessment = stopping.assess(
        conn, version_id,
        target_pass_rate=config.cq.target_pass_rate,
        novelty_window=config.stopping.novelty_window,
        novelty_threshold=config.stopping.novelty_threshold,
        iteration=iteration,
        max_iterations=config.iteration.max_iterations,
    )

    table = Table("criterion", "role", "state", "value", "note")
    for criterion in assessment.criteria:
        colour = {stopping.MET: "green", stopping.NOT_MET: "yellow"}.get(criterion.state, "dim")
        table.add_row(
            criterion.name, criterion.role, f"[{colour}]{criterion.state}[/]",
            criterion.value, criterion.note,
        )
    console.print(table)

    if assessment.stop:
        console.print(
            f"[green]stop[/] — {', '.join(assessment.reasons)}. Being incremental, this is "
            "'enough until new documents arrive', not 'finished'."
        )
    else:
        console.print("[yellow]keep going[/]: no criterion is met")

    if curve:
        plot = Table("#", "document", "new", "cumulative")
        for point in assessment.curve:
            plot.add_row(str(point.index), point.document_id[:40], str(point.new),
                         str(point.cumulative))
        console.print(plot)


@app.command("functional")
def functional_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    declare: str | None = DeclareOption,
    yes: bool = YesOption,
    limit: int | None = LimitOption,
) -> None:
    """Survey the ABox for functional-property candidates, and ask (spec 6.8, D2).

    Nothing here declares a property functional on its own. Detecting functionality from the
    ABox is invalid in principle under the open-world assumption: every entity having one value
    proves that no counterexample was seen, not that none exists.

    The asymmetry is why it is asked rather than inferred. A property declared functional by
    mistake makes the reasoner entail `owl:sameAs` and merge two distinct entities — and it
    raises no inconsistency doing it. `--declare` shows exactly which individuals would merge
    before anything is committed.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    abox_path = config.paths.work_dir / "ontology" / f"{version_id}.abox.trig"
    if not abox_path.exists():
        raise typer.BadParameter(
            f"no ABox for {version_id}; run regenerate first — this reads the instance data, "
            "not the TBox"
        )
    dataset = Dataset()
    dataset.parse(str(abox_path), format="trig")
    abox = mapping.flatten(dataset)

    if declare is not None:
        _declare_functional(config, conn, version_id, graph, abox, declare, yes)
        return

    supports = functional.survey(abox)
    labels = versioning.label_index(graph)
    items = functional.findings(
        supports, labels, min_individuals=config.mapping.functional_min_individuals
    )
    report = review.sync(conn, items, version_id=version_id,
                         kinds=[functional.FUNCTIONAL_CANDIDATE])

    table = Table("property", "individuals", "distribution", "excluded", "verdict")
    for support in supports[: limit or 15]:
        table.add_row(
            versioning.short_name(support.property_iri, labels),
            str(support.individuals), support.rendered_distribution,
            str(support.excluded),
            "refuted" if support.refuted else "candidate",
        )
    console.print(table)
    if not supports:
        console.print(
            "[yellow]no domain properties in the ABox[/]: this pipeline extracts types and "
            "provenance, not properties, so there is nothing here to be functional yet."
        )
        return
    console.print(
        f"{report.added} question(s) added to review, {report.already_known} already there. "
        f"A 'refuted' verdict is settled — a counterexample is knowledge. A 'candidate' one "
        f"is not: it is the absence of a counterexample, which is silence."
    )
    console.print(
        "See what one would merge before answering: "
        "`onto-pipeline functional --declare <property>`"
    )


def _declare_functional(config, conn, version_id, graph, abox, property_iri, yes) -> None:
    """Make the silent consequence loud, then commit only if told to."""
    from .reasoning import Reasoners, ReasonerUnavailable

    candidate = functional.declare(graph, property_iri)
    combined = Graph()
    for triple in candidate:
        combined.add(triple)
    for triple in abox:
        combined.add(triple)

    try:
        reasoners = Reasoners(
            config.paths.reasoner_lib, hermit_timeout_s=config.reasoner.hermit_timeout_s
        )
    except ReasonerUnavailable as exc:
        raise typer.BadParameter(
            f"{exc}. The whole point of this command is to show what the reasoner would merge."
        ) from exc

    before = reasoners.merged_individuals(_without_declaration(combined, property_iri))
    after = reasoners.merged_individuals(combined)
    new_merges = [group for group in after if group not in before]

    labels = versioning.label_index(graph)
    name = versioning.short_name(property_iri, labels)
    if new_merges:
        console.print(
            f"[red]declaring {name} functional would merge {len(new_merges)} group(s) of "
            f"individuals[/], and the reasoner would raise no inconsistency doing it:"
        )
        for group in new_merges[:10]:
            console.print("  " + " = ".join(
                versioning.short_name(iri, labels) for iri in group
            ))
    else:
        console.print(f"[green]declaring {name} functional merges nothing[/] in the ABox today")
    console.print(
        "[dim]Today's ABox is not the argument. The question is whether the property is "
        "functional in the domain; this only shows what the mistake would cost here.[/]"
    )

    if not yes:
        console.print("Nothing committed. `--yes` commits the declaration.")
        return
    next_id = f"v{conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0]}"
    committed = versioning.commit(
        conn, candidate, version_id=next_id, parent_id=version_id, iteration=1,
        note=f"{name} declared functional (user decision, spec 6.8)",
    )
    console.print(f"[green]committed[/] {committed.id} (parent {version_id})")
    _publish_diff(config, conn, committed.id)


def _without_declaration(graph: Graph, property_iri: str) -> Graph:
    """The same graph minus the declaration.

    The baseline has to be measured, not assumed: an ontology can already entail merges for
    other reasons, and reporting those as this property's doing would put the blame in the
    wrong place.
    """
    declaration = (URIRef(property_iri), RDF.type, OWL.FunctionalProperty)
    stripped = Graph()
    for triple in graph:
        if triple != declaration:
            stripped.add(triple)
    return stripped


@app.command("metaproperties")
def metaproperties_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
    refresh: bool = RefreshOption,
) -> None:
    """Label each class for OntoClean, so B5's filter 4 has something to check.

    The model is asked four plain questions — can one stop being this? can two of them be told
    apart? is each one a whole? does each need something else to exist? — and never asked for
    OntoClean's notation. Asking in jargon gets an answer about the jargon.

    Labels carry across versions on purpose: a metaproperty is a fact about the concept, not
    about the state of the ontology, so a class rigid in v3 is rigid in v7. `--refresh` asks
    again for classes already labelled.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    targets = typing_store.targets_from(graph, "label")
    known = ontoclean.load(conn, version_id)
    pending = [t for t in targets if refresh or t.iri not in known]
    if not pending:
        console.print(
            f"[green]all {len(targets)} classes are labelled[/] · --refresh asks again"
        )
        return

    labels = {target.iri: target.label for target in targets}
    payloads = [
        (
            target.iri,
            ontoclean.payload(
                target.label, target.gloss,
                [labels.get(str(parent), str(parent))
                 for parent in graph.objects(URIRef(target.iri), RDFS.subClassOf)],
            ),
        )
        for target in (pending[:limit] if limit else pending)
    ]
    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, ontoclean.STAGE)
    with console.status(f"metaproperties · {len(payloads)} classes"):
        result = llm.run(
            Ledger(conn, config.execution), model, ontoclean.PROMPT, stage,
            payloads, ontoclean.parse,
        )

    written = [ontoclean.Labels(iri=iri, **answer) for iri, answer in result.outputs.items()]
    ontoclean.persist(conn, version_id, written)

    tally: dict[str, int] = {}
    for item in written:
        tally[item.rigidity] = tally.get(item.rigidity, 0) + 1
    table = Table("what", "count")
    table.add_row("classes labelled", str(len(written)))
    for value, count in sorted(tally.items()):
        table.add_row(f"  rigidity {value}", str(count))
    table.add_row("failed", str(len(result.failures)))
    table.add_row("still unlabelled", str(len(targets) - len(ontoclean.load(conn, version_id))))
    console.print(table)
    for iri, error in list(result.failures.items())[:5]:
        console.print(f"  [red]{labels.get(iri, iri)}[/]: {error}")


@app.command("conflicts")
def conflicts_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
) -> None:
    """Entities two documents typed differently (spec 6.4).

    The volume filter is the point: a conflict the reasoner would not break on is notarized
    without asking, because deciding case by case is the manual review this exists to avoid.
    Only the ones that make a class incompatible reach `review`, and there will be few.

    A pair of classes that collides again and again is not many cases — it is one question
    about the TBox, and it is reported as such.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)
    labels = versioning.label_index(graph)

    rules = mapping.rules_from_config(config.mapping, config.seed.base_iri)
    rows, typings = mapping.load_inputs(conn, version_id)
    if not rows:
        raise typer.BadParameter("no mentions; run extract first")

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
    incompatible, source = _incompatibilities(config, graph, conflicts.candidate_pairs(found))
    conflicts.classify(found, incompatible)

    breaking = [item for item in found if item.breaks_reasoner]
    items = conflicts.findings(
        found, labels, pattern_threshold=config.mapping.conflict_pattern_threshold
    )
    report = review.sync(
        conn, items, version_id=version_id,
        kinds=[conflicts.FACTUAL_CONFLICT, conflicts.CONFLICT_PATTERN],
    )

    table = Table("what", "count", "note")
    table.add_row("entities", str(len(groups)), f"from {len(rows)} mentions")
    table.add_row("in conflict", str(len(found)), "typed to more than one class")
    table.add_row(f"  {rules.conflict_policy}d silently", str(len(found) - len(breaking)),
                  "no incompatibility, so no question")
    table.add_row("  sent to review", str(len(breaking)), f"incompatible per {source}")
    table.add_row("patterns", str(sum(1 for i in items if i.kind == conflicts.CONFLICT_PATTERN)),
                  "a recurring pair is a TBox question")
    table.add_row("review items added", str(report.added), f"{report.already_known} known")
    console.print(table)

    for item in items[: limit or 10]:
        console.print(f"  [yellow]{item.kind}[/] {item.summary}")
    if found and not breaking:
        console.print(
            f"[dim]The {len(found)} disagreements are kept with their provenance and flagged "
            f"`{mapping.NOTARIZED}` in the ABox. Notarizing is the silent default because it "
            "is the only policy that destroys no information.[/]"
        )


@app.command("mark")
def mark_cmd(
    mentions: list[str] = MentionsOption,
    mark: str = MarkOption,
    config_path: Path = ConfigOption,
    comment: str = CommentOption,
    export: Path | None = ExportOption,
) -> None:
    """Mark an assertion false. `refuted` and `misextracted` are opposite signals (spec 6.4).

    `refuted`: the document asserts it and it is not true. The assertion leaves the ABox.

    `misextracted`: the document never said it and the extractor misread. That is a B1 bug, it
    also leaves the ABox, and it goes to the evaluation set — free extraction-error labels
    nobody had to annotate on purpose. Mixing the two loses them.

    Under the open-world assumption these are not the same as asserting the negation: silence
    is not knowledge. Nothing is written into the ontology as a negative assertion here.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    try:
        marked = conflicts.mark(conn, mentions, mark, comment)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    console.print(f"[green]marked[/] {marked} mention(s) as {mark}")
    console.print(
        "[dim]The mapping rules changed, so the ABox is stale: run `regenerate` to apply "
        "it.[/]"
    )
    if export is not None:
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_text(conflicts.export_misextractions(conn) + "\n", encoding="utf-8")
        console.print(f"[green]wrote[/] {export}")


def _incompatibilities(config, graph, pairs):
    """What the TBox calls incompatible, from the reasoner when one is available.

    The asserted-and-inherited disjointness is a lower bound: two classes can be unsatisfiable
    together for reasons no `owl:disjointWith` states. Falling back to it is fine as long as
    the answer says which test was used — reporting "no conflict" from the weaker one would be
    reporting the absence of the instrument as the absence of the finding.
    """
    from .reasoning import InconsistentOntology, Reasoners, ReasonerUnavailable

    asserted = conflicts.asserted_incompatibilities(graph)
    if not pairs:
        return asserted, "asserted disjointness"
    try:
        reasoners = Reasoners(
            config.paths.reasoner_lib, hermit_timeout_s=config.reasoner.hermit_timeout_s
        )
        return asserted | reasoners.incompatible_pairs(graph, pairs), "the reasoner"
    except ReasonerUnavailable as exc:
        console.print(f"[yellow]no reasoner[/] ({exc}); using asserted disjointness only")
    except InconsistentOntology as exc:
        console.print(f"[red]{exc}[/]; using asserted disjointness only")
    return asserted, "asserted disjointness"


@app.command("enrich")
def enrich_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
    dry_run: bool = DryRunOption,
) -> None:
    """Improve the glosses from definitional passages in the corpus, and harvest synonyms.

    The passages are found mechanically, by definitional cue — "X is a", "we define X as",
    "also known as". Only then is a model asked, and only about the passages it is shown.

    Every enrichment records which documents contributed to it. A later match of a mention from
    a contributing document against that class is not independent evidence of coverage; the
    `circular` command is what makes those countable (spec 4.3).
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    targets = typing_store.targets_from(graph, "label")
    if not targets:
        raise typer.BadParameter(f"{version_id} has no labelled classes")
    known = {target.label.lower() for target in targets}
    known |= {alt.lower() for target in targets for alt in target.alt_labels}

    blocks = _corpus_blocks(conn)
    if not blocks:
        raise typer.BadParameter("no parsed blocks; run ingest first")

    found: dict[str, list[enrichment.Passage]] = {}
    for target in targets:
        surfaces = [target.label, *target.alt_labels]
        passages = enrichment.passages_for(
            blocks, surfaces, limit=config.enrichment.max_passages,
            max_chars=config.enrichment.max_passage_chars,
        )
        if len(passages) >= config.enrichment.min_passages:
            found[target.iri] = passages

    by_iri = {target.iri: target for target in targets}
    console.print(
        f"[green]passages[/] {sum(len(items) for items in found.values())} definitional "
        f"across {len(found)} of {len(targets)} classes, from {len(blocks)} blocks"
    )
    if not found:
        console.print(
            "[yellow]nothing to enrich[/]: no passage in the corpus defines a class of this "
            "ontology. On a corpus and a seed about different things, that is the expected "
            "answer, not a failure."
        )
        return
    if dry_run:
        table = Table("class", "passages", "documents", "cues")
        for iri, passages in list(found.items())[: limit or 20]:
            table.add_row(
                by_iri[iri].label, str(len(passages)),
                str(len({item.document_id for item in passages})),
                ", ".join(sorted({item.cue for item in passages})),
            )
        console.print(table)
        return

    selected = list(found.items())[:limit] if limit else list(found.items())
    payloads = [
        (iri, enrichment.payload(by_iri[iri].label, by_iri[iri].gloss, passages))
        for iri, passages in selected
    ]
    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, enrichment.STAGE)
    with console.status(f"enrich · {len(payloads)} classes"):
        result = llm.run(
            Ledger(conn, config.execution), model, enrichment.PROMPT, stage,
            payloads, enrichment.parse,
        )

    enrichments = [
        enrichment.verified(answer, found[iri], known=known, iri=iri)
        for iri, answer in result.outputs.items()
    ]
    changed = [item for item in enrichments if not item.empty]
    enrichment.persist(conn, version_id, changed, passages=found)

    table = Table("what", "count")
    table.add_row("classes asked", str(len(payloads)))
    table.add_row("  definitions rewritten", str(sum(1 for i in changed if i.definition)))
    table.add_row("  synonyms kept", str(sum(len(i.alt_labels) for i in changed)))
    table.add_row("  synonyms dropped as unattested", str(sum(len(i.dropped) for i in changed)))
    table.add_row("  unchanged", str(len(enrichments) - len(changed)))
    table.add_row("failed", str(len(result.failures)))
    console.print(table)
    for item in changed[:8]:
        names = f" + {', '.join(item.alt_labels)}" if item.alt_labels else ""
        console.print(f"  [green]{by_iri[item.iri].label}[/]{names} — {item.why[:80]}")
    for iri, error in list(result.failures.items())[:5]:
        console.print(f"  [red]{by_iri.get(iri).label if iri in by_iri else iri}[/]: {error}")

    if not changed:
        console.print("nothing changed; no version committed")
        return

    enriched = enrichment.apply(graph, changed)
    # Annotation-only, so the logical state is the parent's: a re-glossing is not a new state
    # to reason about (6.8), while the gloss itself is versioned and travels in the DAG (4.3).
    next_id = f"v{conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0]}"
    committed = versioning.commit(
        conn, enriched, version_id=next_id, parent_id=version_id, iteration=1,
        note=f"glosses enriched from the corpus ({len(changed)} classes; same logical state)",
    )
    console.print(f"[green]committed[/] {committed.id} (parent {version_id})")
    _publish_diff(config, conn, committed.id)

    orphans = conn.execute(
        "SELECT COUNT(*) AS n FROM mention_typing WHERE version_id = ? AND iri IS NULL",
        (version_id,),
    ).fetchone()
    if orphans and orphans["n"]:
        console.print(
            f"\n[yellow]{orphans['n']} orphan mentions[/] were typed against {version_id}. "
            f"The synonyms just added are what closes the loop, so re-run "
            f"`match --version {committed.id}` to give them another chance (spec 4.3)."
        )


@app.command("circular")
def circular_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
) -> None:
    """Matches whose own document helped write the class they matched (spec 4.3).

    Not errors, and not thrown away. They are the ones that must not be counted as independent
    evidence of coverage: the class was described using that document, so the match is partly
    the pipeline recognizing its own writing.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)
    labels = versioning.label_index(graph)

    flagged = enrichment.circular_matches(conn, version_id)
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM mention_typing WHERE version_id = ? AND iri IS NOT NULL",
        (version_id,),
    ).fetchone()["n"]
    console.print(
        f"[bold]{len(flagged)}[/] of {total} typed mentions against {version_id} are "
        f"circular ({len(flagged) / total:.1%})" if total
        else f"no typed mentions against {version_id}"
    )
    if not flagged:
        return
    table = Table("mention", "class", "document", "score", "zone")
    for row in flagged[: limit or 20]:
        table.add_row(
            row["surface_text"][:40], versioning.short_name(row["iri"], labels),
            row["document_id"][:30], f"{row['score']:.3f}", row["zone"],
        )
    console.print(table)


def _corpus_blocks(conn) -> list[dict]:
    """Body blocks of every document the process may read.

    Held-out documents are excluded here as everywhere: a gloss written from the retention set
    would make the retention set measure the pipeline against its own material.
    """
    return [
        dict(row) for row in conn.execute(
            "SELECT b.id, b.document_id, b.page, b.text FROM blocks b "
            "JOIN documents d ON d.id = b.document_id "
            "WHERE d.held_out = 0 AND b.is_boilerplate = 0 AND b.block_type != 'figure' "
            "ORDER BY b.document_id, b.page, b.ordinal"
        )
    ]


@app.command("branch")
def branch_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    choose: str | None = ChooseOption,
    why: str = WhyOption,
    apply_changes: bool = ApplyOption,
) -> None:
    """Find the decision axes in this iteration's proposed axioms, and put them to the user.

    Nothing here asks a model for alternatives — the spec's one prohibition for this stage,
    because a model asked for three produces three correlated ones. The axes come from the
    reasoner, which finds the logical conflicts, and from a fixed catalogue of modelling
    commitments, which no reasoner can find because both sides are consistent.

    The usual outcome is that there is no axis, and then this applies everything and says so.
    """
    from .reasoning import Reasoners, ReasonerUnavailable

    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    axioms = axiomatization.load(conn, version_id)
    if not axioms:
        raise typer.BadParameter(
            f"no proposed axioms against {version_id}; run axiomatize first"
        )
    by_id = {axiom.id: axiom for axiom in axioms}

    if choose is not None:
        _apply_branch(config, conn, version_id, graph, by_id, choose, why, apply_changes)
        return

    labels = versioning.label_index(graph)
    proposals = {row["id"]: row for row in induction.load(conn, version_id)}
    situation = _situation(config, proposals, axioms, labels)

    try:
        reasoners = Reasoners(
            config.paths.reasoner_lib, hermit_timeout_s=config.reasoner.hermit_timeout_s
        )
        hermit = reasoners.hermit(reasoners.load(axiomatization.apply(graph, axioms)))
        conflicts, pre_existing = branching.conflict_sets(axioms, hermit.justifications)
    except ReasonerUnavailable as exc:
        # A modelling axis is not a logical one: the catalogue still applies, and saying so is
        # more useful than refusing the whole command.
        console.print(f"[yellow]no reasoner[/] ({exc}); logical axes not looked for")
        conflicts, pre_existing = [], []

    axes = branching.logical_axes(conflicts, axioms, labels)
    similarity = _text_similarity(config)
    if similarity is None:
        console.print("[dim]  the division-criterion axis is one of them[/]")
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
    for class_iri in pre_existing:
        console.print(
            f"[red]pre-existing[/] {versioning.short_name(class_iri, labels)} is unsatisfiable "
            "without any of this iteration's axioms; that is a defect, not a branch"
        )

    if plan.automatic:
        console.print(
            f"[green]no decision axis[/] over {len(axioms)} proposed axioms: they are "
            "compatible and the catalogue recognizes no commitment among them."
        )
        if not config.branching.auto_apply_when_no_axis:
            console.print("[dim]branching.auto_apply_when_no_axis is off; nothing applied[/]")
            return
        console.print("[dim]applying all of them — the usual path, per spec 6.6[/]")
        _commit_axioms(
            config, conn, version_id, graph, axioms, apply_changes,
            note=f"{len(axioms)} axioms applied automatically: no decision axis",
        )
        return

    support = {axiom.id: axiom.support for axiom in axioms}
    orphan_total = len({mention for axiom in axioms for mention in axiom.support})
    entities = len({axiom.subject_iri for axiom in axioms})
    tally = branching.history(conn)
    for decision in plan.decisions:
        for branch in decision.branches:
            branch.score = branching.score(
                branch, support=support, orphan_total=orphan_total, entities=entities,
                history=tally,
            )
            kept = [by_id[axiom_id] for axiom_id in branch.add_axioms]
            branch.state_hash = versioning.state_hash(axiomatization.apply(graph, kept))
            seen = versioning.find_by_hash(conn, branch.state_hash)
            if seen is not None:
                branch.note = f"returns to {seen.id}"
        decision.branches = branching.rank(
            decision.branches, limit=config.branching.max_branches
        )
    branching.persist(conn, version_id, plan.decisions)

    for decision in plan.decisions:
        console.print()
        for axis in decision.axes:
            console.print(f"[bold]{axis.kind} axis[/] {axis.id}")
            console.print(f"  {axis.question}")
            for option in axis.options:
                console.print(f"    [cyan]{option.label}[/] — {option.why}")
        if decision.coupled:
            console.print(
                "[dim]  these axes share axioms, so they are one question: a choice on one "
                "changes what the other is choosing between[/]"
            )
        table = Table("branch", "choice", "coverage", "reorg", "abox", "affinity", "note")
        for branch in decision.branches:
            table.add_row(
                branch.id,
                _choice_labels(decision, branch),
                f"{branch.score.coverage:.0%}",
                str(branch.score.reorg_cost),
                str(branch.score.abox_regen_cost),
                "—" if branch.score.historical_affinity is None
                else f"{branch.score.historical_affinity:.2f}",
                branch.note,
            )
        console.print(table)
    console.print(
        "\n[dim]affinity is empty until something has been decided before — the cold start of "
        "spec 11, reported as absent rather than as zero.[/]"
    )
    console.print("Choose one with `onto-pipeline branch --choose <branch> --why '...'`.")


def _choice_labels(decision, branch) -> str:
    """The option's own words, not `axis=option_id`: the axis is printed above the table, and
    an id truncated by the terminal is one the user cannot act on."""
    by_axis = {axis.id: axis for axis in decision.axes}
    return " / ".join(
        next(
            (option.label for option in by_axis[axis_id].options if option.id == option_id),
            option_id,
        )
        for axis_id, option_id in sorted(branch.choices.items())
    )


def _situation(config, proposals: dict, axioms, labels: dict[str, str]):
    """What the catalogue's detectors read: each proposal, and the parent it would hang from.

    The minted IRI is recomputed rather than stored, because `mint_iri` is a function of the
    proposal — the same input gives the same IRI, which is what makes a re-run stable.
    """
    minted = {
        proposal_id: axiomatization.mint_iri(
            config.seed.base_iri, row["label"], proposal_id
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
        parent_of={
            pid: parent_by_iri[iri] for pid, iri in minted.items() if iri in asserted
        },
        minted={pid: iri for pid, iri in minted.items() if iri in asserted},
        axioms=axioms,
        labels=labels,
    )


def _text_similarity(config):
    """Cosine similarity between two short texts, from the bi-encoder already configured.

    Two callers, both of which prefer to lose a capability over guessing at it: the
    division-criterion axis, which needs to know when two declared criteria name the same cut,
    and A3's deduplication. Returns None when the encoder is not installed, and each caller
    says what it does without one.
    """
    from .embeddings import EncoderUnavailable, SentenceTransformerEncoder
    from .matching import dot

    try:
        encoder = SentenceTransformerEncoder(config.matching.bi_encoder, config.matching.device)
    except EncoderUnavailable:
        console.print(
            "[yellow]no encoder[/] (`uv sync --extra matching`): the checks that need one are "
            "skipped, and the command says which"
        )
        return None

    cache: dict[str, list[float]] = {}

    def similarity(left: str, right: str) -> float:
        missing = [text for text in (left, right) if text not in cache]
        if missing:
            cache.update(zip(missing, encoder.encode(missing), strict=True))
        return dot(cache[left], cache[right])

    return similarity


def _apply_branch(config, conn, version_id, graph, by_id, branch_id, why, apply_changes) -> None:
    """Commit the state a branch names, and only then settle the decision.

    In that order on purpose: the reasoner still gets to reject the branch, and a decision
    recorded for a state that was never applied would say the user chose something the
    ontology never contained.
    """
    row = branching.find(conn, branch_id)
    if row is None:
        raise typer.BadParameter(f"no branch {branch_id!r}; run `branch` to see them")

    chosen = [by_id[axiom_id] for axiom_id in json.loads(row["add_axioms"]) if axiom_id in by_id]
    console.print(
        f"branch {branch_id}: {len(chosen)} axioms kept, {len(by_id) - len(chosen)} given up"
    )
    committed = _commit_axioms(
        config, conn, version_id, graph, chosen, apply_changes,
        note=f"branch {branch_id}: {why}" if why else f"branch {branch_id}",
        branch_id=branch_id,
    )
    if not committed:
        console.print("[yellow]decision not recorded[/]: nothing was applied")
        return
    branching.settle(conn, branch_id, note=why)
    console.print(
        f"[green]chose[/] {branch_id}. Its siblings are recorded as rejected — that record is "
        "what a later iteration reads so it does not propose the same thing again."
    )


def _commit_axioms(
    config, conn, version_id, graph, axioms, apply_changes, *, note, branch_id=None
) -> bool:
    """The validation chain, then a version. Identical to what `axiomatize` does directly —
    a branch changes which axioms are applied, never whether the reasoner gets to reject
    them."""
    from .reasoning import REJECTED, Reasoners, ReasonerUnavailable

    candidate = axiomatization.apply(graph, axioms)
    try:
        reasoners = Reasoners(
            config.paths.reasoner_lib, hermit_timeout_s=config.reasoner.hermit_timeout_s
        )
    except ReasonerUnavailable as exc:
        raise typer.BadParameter(
            f"{exc}. Applying without the reasoner would skip the filter that makes this safe."
        ) from exc

    ontology = reasoners.load(candidate)
    elk = reasoners.elk(ontology, coverage_threshold=config.reasoner.elk_coverage_threshold)
    hermit = reasoners.hermit(ontology)
    metrics = structural.check(candidate)
    smells = validation.pitfalls(candidate)
    clean = _ontoclean(conn, version_id, candidate)

    verdict = Table("filter", "result", "detail")
    verdict.add_row("ELK", elk.verdict, elk.note[:52])
    verdict.add_row("HermiT", "consistent" if hermit.consistent else REJECTED,
                    f"{len(hermit.unsatisfiable)} unsatisfiable")
    verdict.add_row("OntoClean", clean.decision, clean.note[:52])
    verdict.add_row("pitfalls", smells.decision, smells.note[:52])
    verdict.add_row("structural", REJECTED if metrics.rejected else "clean",
                    f"depth {metrics.depth} · {len(metrics.findings)} finding(s)")
    console.print(verdict)

    if elk.verdict == REJECTED or not hermit.consistent or hermit.unsatisfiable:
        console.print("[red]not applied[/]: the reasoner rejected it")
        return False
    if clean.rejected:
        for finding in clean.findings[:6]:
            console.print(f"  [red]OntoClean[/] {finding}")
        console.print("[red]not applied[/]: OntoClean rejected a subsumption")
        return False
    if metrics.rejected and not apply_changes:
        for finding in metrics.findings[:8]:
            console.print(f"  [yellow]{finding.check}[/] {finding.subject} — {finding.detail}")
        console.print("[yellow]not applied[/]: structural findings. `--apply` overrides.")
        return False

    hash_value = versioning.state_hash(candidate)
    seen = versioning.find_by_hash(conn, hash_value)
    if seen is not None:
        console.print(
            f"[yellow]loop[/]: this state is {seen.id}, already in the DAG. Returning is "
            "allowed, but explicitly — nothing was committed."
        )
        return False

    next_id = f"v{conn.execute('SELECT COUNT(*) FROM versions').fetchone()[0]}"
    committed = versioning.commit(
        conn, candidate, version_id=next_id, parent_id=version_id, iteration=1,
        branch_id=branch_id, note=note,
    )
    console.print(
        f"[green]committed[/] {committed.id} (parent {version_id}) · "
        f"{len(graph)} -> {len(candidate)} triples"
    )
    _publish_diff(config, conn, committed.id)
    return True


@app.command("induce")
def induce_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
) -> None:
    """B3: turn orphan mentions into proposed classes (spec 6.1, 6.6).

    The code clusters, the model names. Nothing is applied: a proposal records the nearest
    existing class as a *candidate* parent for B4 to rule on, not as an asserted subsumption.
    """
    from .matching import Matcher

    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    orphans = _orphans(conn, version_id)
    if not orphans:
        raise typer.BadParameter(
            f"no orphan mentions against {version_id}; run match first"
        )

    # A mention a bridge already accounts for is not an orphan any more: the seed does cover it,
    # world knowledge said so, and inducing a class for it would be the spurious class 12.1
    # exists to prevent.
    bridged = bridging.bridged_mentions(conn, version_id)
    if bridged:
        orphans = [row for row in orphans if row["id"] not in bridged]
        console.print(f"[dim]{len(bridged)} mentions covered by a bridge; skipped[/]")
        if not orphans:
            console.print("[yellow]every orphan was bridged[/]; nothing left to induce")
            return
    else:
        console.print(
            "[yellow]no bridges recorded[/] for this version. Run `bridge` first, or every "
            "mention the matcher missed becomes a proposed class (6.2b)."
        )

    matcher = Matcher(_encoder(config))
    surfaces = [row["surface_text"] for row in orphans]
    clusters = induction.cluster(
        [row["id"] for row in orphans], surfaces, matcher.vectors_for(surfaces),
        threshold=config.induction.similarity_threshold,
        min_support=config.induction.min_support,
    )
    if not clusters:
        console.print(
            f"[yellow]{len(orphans)} orphans, no cluster reached "
            f"min_support={config.induction.min_support}[/]"
        )
        return

    labels = {
        target.iri: target for target in typing_store.targets_from(graph, "label")
    }
    runner_up = {row["id"]: row["runner_up"] for row in orphans}
    for item in clusters:
        nearest = labels.get(runner_up.get(item.mention_ids[0]) or "")
        if nearest is not None:
            item.nearest_iri, item.nearest_label = nearest.iri, nearest.label
            item.nearest_gloss = nearest.gloss or ""

    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, induction.STAGE)
    with console.status(f"B3 · naming {len(clusters)} clusters from {len(orphans)} orphans"):
        result = llm.run(
            Ledger(conn, config.execution), model, induction.PROMPT, stage,
            [(item.id, induction.payload(
                item, max_phrases=config.induction.max_phrases_in_prompt)) for item in clusters],
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
        proposals.append(
            induction.Proposal(
                cluster_id=item.id, label=answer["label"], gloss=answer.get("gloss", ""),
                criterion=answer["criterion"], support=item.support,
                mention_ids=item.mention_ids, nearest_iri=item.nearest_iri,
                nearest_score=item.nearest_score,
            )
        )
    induction.persist(conn, version_id, proposals)

    table = Table("proposed class", "support", "criterion", "nearest existing")
    for proposal in sorted(proposals, key=lambda p: -p.support)[:20]:
        table.add_row(
            proposal.label[:28], str(proposal.support), proposal.criterion[:44],
            (labels[proposal.nearest_iri].label if proposal.nearest_iri in labels else "-"),
        )
    console.print(table)
    console.print(
        f"{len(orphans)} orphans -> {len(clusters)} clusters -> [green]{len(proposals)}[/] "
        f"proposed classes, {declined} groups the model declined to name · "
        f"{len(result.failures)} failed · {result.in_tokens}/{result.out_tokens} tokens"
    )
    console.print("[dim]nothing applied; B4 decides the axioms[/]")


@app.command("match")
def match_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    include_held_out: bool = IncludeHeldOutOption,
) -> None:
    """B2: type the mentions against an ontology version, then resolve entities (spec 6.2).

    This is the pipeline's quality bottleneck, and it is uncalibrated: the thresholds it reads
    have not been set from data. Treat the numbers as a first look, not as the no-go decision
    of 12.1, which needs the gold annotations.
    """
    from .embeddings import CrossEncoderReranker, EncoderUnavailable, SentenceTransformerEncoder
    from .matching import ASK, Matcher

    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    versioning.install(conn)

    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    targets = typing_store.targets_from(graph, config.matching.match_against)
    if not targets:
        raise typer.BadParameter("the ontology version has no labelled classes")

    ids = process_documents(conn)
    if include_held_out:
        ids += held_out_documents(conn)
    rows = [mention for identifier in ids for mention in extraction.load(conn, identifier)]
    if not rows:
        raise typer.BadParameter("no mentions; run extract first")

    try:
        encoder = SentenceTransformerEncoder(config.matching.bi_encoder, config.matching.device)
        reranker = (
            CrossEncoderReranker(config.matching.cross_encoder, config.matching.device)
            if config.matching.use_cross_encoder
            else None
        )
    except EncoderUnavailable as exc:
        raise typer.BadParameter(f"{exc}; uv sync --extra matching") from exc

    matcher = Matcher(
        encoder, reranker,
        auto_merge_threshold=config.matching.auto_merge_threshold,
        grey_zone_lower=config.matching.grey_zone_lower,
        cross_language_always_grey=config.matching.cross_language_always_grey,
        respect_declared_haskey=config.matching.respect_declared_haskey,
        blocking_strategy=config.matching.blocking_strategy,
    )
    mentions = typing_store.mentions_from(rows)

    # What the ontology already says about identity, handed to the resolver: declared
    # synonyms, declared keys, and the class each mention was just typed to. Without the last
    # two, `respect_declared_haskey` cannot fire at all — a declared key is checked against the
    # class both mentions were assigned, and there is no class to check against.
    synonyms = matching.synonym_index(targets)
    keys = {target.iri: target.has_key for target in targets if target.has_key}

    with console.status(f"{len(mentions)} mentions against {len(targets)} classes"):
        typings = matcher.type_mentions(mentions, targets)
        split = typing_store.persist_typings(conn, version_id, typings)
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
    typing_store.persist_entities(conn, entities, unresolved)

    table = Table("what", "count", "note")
    table.add_row("mentions", str(split.total), f"against version {version_id}")
    table.add_row("typed automatically", str(split.typed), "")
    table.add_row("grey zone", str(split.grey), "needs a decision from you")
    table.add_row(
        "orphans", str(split.orphan),
        f"{split.orphan_rate:.0%} — false vs genuine needs the retention set (10.1)",
    )
    table.add_row("merge decisions", str(sum(1 for d in decisions if d.action == "merge")), "")
    table.add_row("pairs to ask about", str(len(unresolved)), "possible_duplicate_unresolved")
    console.print(table)
    console.print(
        "[yellow]uncalibrated[/]: the thresholds are the spec's defaults, not measured ones. "
        "See Limitaciones in the README."
    )


@app.command("calibrate")
def calibrate_cmd(
    pair_name: str = PairOption,
    config_path: Path = ConfigOption,
    match_against: list[str] = MatchAgainstOption,
    cross_encoder: bool = CrossEncoderSweepOption,
    holdout: float | None = HoldoutOption,
    keep_excluded: bool = KeepExcludedOption,
    limit: int | None = LimitOption,
) -> None:
    """Sweep the matcher's thresholds against a published annotated corpus (plan, C3–C5).

    This is the instrument, not the case of application. What it measures transfers because it
    is a property of the method and the encoder — label versus gloss, whether the cross-encoder
    crushes the scale, where true and false separate. The operating point transfers only in
    part: an inventory of thousands offers more chances for something spurious to clear a
    threshold than one of thirty-four.
    """
    from .embeddings import CrossEncoderReranker, EncoderUnavailable, SentenceTransformerEncoder

    config = Config.load(config_path)
    directory = config.paths.calibration_root / pair_name
    if not directory.is_dir():
        raise typer.BadParameter(f"no pair at {directory}; see calibration/README.md")

    variants = list(match_against) or [config.matching.match_against]
    encoders = [False, True] if cross_encoder else [config.matching.use_cross_encoder]

    reports: list[calibration.RunReport] = []
    for variant in variants:
        pair = calibration.load_pair(
            directory, match_against=variant, holdout=holdout,
            drop_excluded=not keep_excluded,
        )
        if limit:
            pair.documents = pair.documents[:limit]
        _describe_pair(pair, variant, variants[0] == variant)
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
                raise typer.BadParameter(f"{exc}; uv sync --extra matching") from exc
            # A fresh Matcher per run: the vector cache is keyed by text, and a gloss and a
            # label are different texts, but sharing it across runs would hide how much of the
            # cost each variant carries.
            matcher = matching.Matcher(
                encoder, reranker,
                auto_merge_threshold=1.1,   # wide open: the sweep applies the zones itself
                grey_zone_lower=0.0,
                cross_language_always_grey=config.matching.cross_language_always_grey,
                respect_declared_haskey=config.matching.respect_declared_haskey,
                blocking_strategy=config.matching.blocking_strategy,
            )
            label = f"{variant} · {'cross' if use_cross else 'bi'}"
            with console.status(f"{label}: {len(pair.mentions())} mentions"):
                ranking = calibration.rank(pair, matcher)
                runs = calibration.sweep(
                    pair, ranking, calibration.thresholds(),
                    match_against=variant, use_cross_encoder=use_cross,
                )
            reports.extend(runs)
            _report_distribution(label, runs[0].distribution)

    _report_sweep(reports)
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
    console.print(f"[dim]{path}[/]")


def _describe_pair(pair: calibration.Pair, variant: str, first: bool) -> None:
    if not first:
        return
    mentions = sum(len(document.mentions) for document in pair.documents)
    skipped = sum(document.skipped for document in pair.documents)
    glossed = sum(1 for target in pair.targets if target.gloss)
    table = Table("pair", pair.name)
    table.add_row("documents", str(len(pair.documents)))
    table.add_row("gold mentions", f"{mentions} ({skipped} discontinuous, skipped)")
    table.add_row("inventory", f"{len(pair.targets)} classes, {glossed} with a definition")
    if pair.withheld:
        table.add_row("withheld", f"{len(pair.withheld)} classes — their mentions are orphans")
    if pair.excluded_classes:
        fate = "dropped from the inventory" if pair.dropped_excluded else "kept, --keep-excluded"
        table.add_row("never correct", f"{', '.join(pair.excluded_classes[:5])} — {fate}")
    console.print(table)
    if variant in {"gloss", "label_and_gloss"} and glossed < len(pair.targets):
        console.print(
            f"[yellow]{len(pair.targets) - glossed} classes have no definition[/]: "
            f"they fall back to the label, so the comparison is not clean for those."
        )


def _report_distribution(label: str, dist: calibration.Distribution) -> None:
    """The number that says whether a threshold exists at all, before asking where to put it."""
    if not dist.correct or not dist.wrong:
        console.print(f"[dim]{label}: not enough of both classes to compare distributions[/]")
        return
    import statistics as _stats

    console.print(
        f"{label}: correct top-1 median [bold]{_stats.median(dist.correct):.3f}[/] "
        f"(n={len(dist.correct)}) · wrong top-1 median "
        f"[bold]{_stats.median(dist.wrong):.3f}[/] (n={len(dist.wrong)}) · "
        f"separation [bold]{dist.separation:.2f}[/]"
    )


def _report_sweep(reports: list[calibration.RunReport]) -> None:
    """Every threshold for the best variant, and the best threshold for every variant.

    Printing the full cross-product would be a hundred rows nobody reads. What the decision
    needs is the shape of one curve and the comparison between variants at their own optimum.
    """
    if not reports:
        return
    best = max(reports, key=lambda run: run.report.typing_f1)
    table = Table(*calibration.COLUMNS, title="threshold sweep · best variant")
    for run in reports:
        if (run.match_against, run.use_cross_encoder) == (best.match_against,
                                                          best.use_cross_encoder):
            table.add_row(*run.row, style="bold" if run is best else None)
    console.print(table)

    variants = Table(*calibration.COLUMNS, title="each variant at its own best threshold")
    seen = {}
    for run in reports:
        key = (run.match_against, run.use_cross_encoder)
        if key not in seen or run.report.typing_f1 > seen[key].report.typing_f1:
            seen[key] = run
    for run in sorted(seen.values(), key=lambda item: -item.report.typing_f1):
        variants.add_row(*run.row)
    console.print(variants)


@app.command("annotate")
def annotate_cmd(
    config_path: Path = ConfigOption,
    doc_id: str | None = DocIdOption,
) -> None:
    """Build the annotation tool for the held-out documents (spec 10.1).

    Offsets index the parser's Markdown, so the tool embeds it verbatim; the corpus never
    leaves the machine.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)

    ids = [doc_id] if doc_id else held_out_documents(conn)
    if not ids:
        raise typer.BadParameter(
            "no held-out documents. Mark them first: onto-pipeline hold-out <doc_id> ..."
        )

    ontology = config.paths.work_dir / "ontology" / "seed_normalized.ttl"
    if not ontology.exists():
        raise typer.BadParameter(f"{ontology} not found; run normalize-seed first")
    classes = annotate.seed_classes(Graph().parse(ontology))
    glossed = sum(1 for item in classes if item.gloss)

    for identifier in ids:
        document = load_document(conn, identifier)
        if document is None:
            console.print(f"[red]{identifier}[/]: not ingested")
            continue
        target = annotate.build(
            doc_id=identifier,
            markdown=markdown_path(config, identifier).read_text(encoding="utf-8"),
            markdown_hash=document["markdown_hash"],
            classes=classes,
            pages=annotate.page_index(load_blocks(conn, identifier)),
            target=config.paths.work_dir / "annotate" / f"{identifier}.html",
        )
        console.print(f"[green]wrote[/] {target}")

    console.print(
        f"{len(classes)} seed classes offered ({glossed} with a gloss). "
        "Open the file in a browser, annotate, export the JSONL, then: "
        "onto-pipeline export-annotations <archivo.jsonl>"
    )


review_app = typer.Typer(help="Findings from A0 that are waiting for a decision.")
app.add_typer(review_app, name="review")


@review_app.command("list")
def review_list(
    config_path: Path = ConfigOption,
    kind: str | None = KindOption,
    status: str = StatusOption,
    as_json: bool = JsonOption,
) -> None:
    """What needs a decision. Nothing is applied here; the decision is recorded."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    items = review.load(conn, status=None if status == "any" else status, kind=kind)

    if as_json:
        # Plain stdout, not the console: rich colours its JSON, which is unparseable.
        print(json.dumps(items, ensure_ascii=False))
        return

    table = Table("id", "kind", "status", "finding")
    for item in items:
        table.add_row(item["id"], item["kind"], item["status"], item["summary"][:60])
    console.print(table)

    tally = review.counts(conn)
    console.print(
        " · ".join(f"{k}/{s}: {n}" for (k, s), n in sorted(tally.items())) or "nothing yet"
    )


@review_app.command("resolve")
def review_resolve(
    item_id: str,
    decision: str,
    config_path: Path = ConfigOption,
    comment: str = CommentOption,
) -> None:
    """Record a decision on one finding: accept or reject."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    try:
        found = review.resolve(conn, item_id, decision, comment)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not found:
        raise typer.BadParameter(f"no review item {item_id!r}")
    console.print(f"[green]{decision}[/] {item_id}")


@app.command("hold-out")
def hold_out(
    doc_id: list[str],
    config_path: Path = ConfigOption,
    release: bool = ReleaseOption,
) -> None:
    """Mark documents as the retention set: parsed, but never fed to the process (spec 10.1).

    They have to be parsed — the annotation offsets index the Markdown A2 produces — but they
    must not reach B1, or the evaluation measures the pipeline against its own input.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    changed = set_held_out(conn, list(doc_id), held_out=not release)
    if changed != len(doc_id):
        console.print("[yellow]some ids did not match an ingested document[/]")

    table = Table("document", "role")
    for identifier in process_documents(conn):
        table.add_row(identifier[:52], "process")
    for identifier in held_out_documents(conn):
        table.add_row(identifier[:52], "[bold]retention set[/]")
    console.print(table)


@app.command("export-annotations")
def export_annotations(
    path: Path,
    config_path: Path = ConfigOption,
) -> None:
    """T3: retention-set JSONL to BRAT/INCEpTION, validated against the Markdown on disk."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    out_dir = config.paths.work_dir / "brat"

    table = Table("document", "mentions", "in seed", "relations", "status")
    for document in annotation.read_jsonl(path):
        stored = load_document(conn, document.doc_id)
        if stored is None:
            table.add_row(document.doc_id, "", "", "", "[red]not ingested[/]")
            continue
        markdown = markdown_path(config, document.doc_id).read_text(encoding="utf-8")
        try:
            annotation.validate(document, markdown, stored["markdown_hash"])
        except annotation.OffsetMismatch as exc:
            table.add_row(document.doc_id, "", "", "", f"[red]{exc}[/]")
            continue
        annotation.export_brat(document, markdown, out_dir)
        table.add_row(
            document.doc_id,
            str(len(document.mentions)),
            str(sum(1 for mention in document.mentions if mention.in_seed)),
            str(len(document.relations)),
            "[green]exported[/]",
        )
    console.print(table)
    console.print(f"output in {out_dir}")


@app.command()
def validate(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
) -> None:
    """B5's reasoner filters over an ontology version, plus A0.0's profile detection.

    ELK never returns an approval: an inconsistency it finds is real, but its silence only
    means the offending axiom may have been ignored, so the verdict is INCONCLUSIVE.
    """
    from .reasoning import REJECTED, Reasoners, ReasonerUnavailable

    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    versioning.install(conn)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    try:
        reasoners = Reasoners(
            config.paths.reasoner_lib, hermit_timeout_s=config.reasoner.hermit_timeout_s
        )
    except ReasonerUnavailable as exc:
        raise typer.BadParameter(str(exc)) from exc

    ontology = reasoners.load(graph)
    profile = reasoners.profile(ontology)
    table = Table("check", "result", "detail")
    table.add_row(
        "A0.0 profile",
        profile.detected,
        f"target {config.owl_profile.target} · "
        + ", ".join(f"{name} {count}" for name, count in sorted(profile.violations.items())),
    )

    elk = reasoners.elk(ontology, coverage_threshold=config.reasoner.elk_coverage_threshold)
    table.add_row("ELK", elk.verdict, f"EL coverage {elk.coverage:.0%} · {elk.note}")

    if elk.verdict == REJECTED:
        table.add_row("HermiT", "not run", "ELK already rejected; its finding is real")
        console.print(table)
        for iri in elk.unsatisfiable:
            console.print(f"[red]unsatisfiable[/] {iri}")
        return

    hermit = reasoners.hermit(ontology)
    table.add_row(
        "HermiT",
        "consistent" if hermit.consistent else REJECTED,
        f"{len(hermit.unsatisfiable)} unsatisfiable class(es)",
    )

    for verdict in _shape_and_smell(config, conn, version_id, graph):
        table.add_row(verdict.name, verdict.decision, verdict.note[:52])

    metrics = structural.check(graph)
    table.add_row(
        "structural",
        REJECTED if metrics.rejected else "clean",
        f"depth {metrics.depth} · max branching {metrics.max_branching} · "
        f"{metrics.n_classes} classes · {len(metrics.findings)} finding(s)",
    )
    console.print(table)
    for verdict in _shape_and_smell(config, conn, version_id, graph):
        for finding in verdict.findings[:12]:
            colour = "red" if verdict.rejected else "yellow"
            console.print(f"[{colour}]{verdict.name}[/] {finding}")
    for finding in metrics.findings:
        console.print(f"[yellow]{finding.check}[/] {finding.subject} — {finding.detail}")
    for iri, justifications in hermit.justifications.items():
        console.print(f"[red]unsatisfiable[/] {iri}")
        for index, axioms in enumerate(justifications, start=1):
            console.print(f"  justification {index}:")
            for axiom in axioms:
                console.print(f"    {axiom}")


def _shape_and_smell(config, conn, version_id, graph) -> list[validation.Verdict]:
    """B5 filters 3 and 5. Neither needs a model and only the first can reject.

    SHACL runs over the ABox, not the TBox: shape constraints are about the instance data.
    With no shapes written it reports that it did not run, which is not the same as passing.
    """
    verdicts = []
    shapes_path = config.paths.work_dir / "shapes.ttl"
    shapes_graph = validation.load_shapes(shapes_path)
    if shapes_graph is None:
        verdicts.append(validation.Verdict(
            "SHACL", validation.SKIPPED, f"no shapes at {shapes_path.name}; nothing to check"
        ))
    else:
        abox = config.paths.work_dir / "ontology" / f"{version_id}.abox.trig"
        if not abox.exists():
            verdicts.append(validation.Verdict(
                "SHACL", validation.SKIPPED, "no ABox for this version; run regenerate"
            ))
        else:
            # Into a Dataset and then flattened: the ABox keeps its provenance in named
            # graphs, and parsing TriG straight into a Graph silently keeps only the default
            # one — every shape would then find no targets and conform over nothing.
            dataset = Dataset()
            dataset.parse(str(abox), format="trig")
            data = mapping.flatten(dataset)
            try:
                verdicts.append(validation.shapes(data, shapes_graph))
            except validation.ShapesUnavailable as exc:
                verdicts.append(validation.Verdict("SHACL", validation.SKIPPED, str(exc)[:60]))

    verdicts.append(_ontoclean(conn, version_id, graph))
    verdicts.append(validation.pitfalls(graph))
    return verdicts


def _ontoclean(conn, version_id, graph) -> validation.Verdict:
    """B5 filter 4. Reports what it could check, never what it assumed."""
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


_IRI_TOKEN = re.compile(r"<([^>]+)>|(?<![\S])(https?://\S+)")


def _readable(line: str, labels: dict[str, str]) -> str:
    """The stored diff keeps full IRIs; the terminal shows labels. With opaque IRIs the raw
    N-Triples are not reviewable, and reviewability is the whole point of showing a diff."""
    def name(match: re.Match) -> str:
        iri = match.group(1) or match.group(2)
        trailing = ""
        if match.group(2) and iri.endswith("."):        # the N-Triples terminator
            iri, trailing = iri[:-1], "."
        return versioning.short_name(iri, labels) + trailing

    return _IRI_TOKEN.sub(name, line)


def _show_diff(
    baseline: str,
    target: str,
    result: versioning.Diff,
    limit: int,
    labels: dict[str, str] | None = None,
) -> None:
    table = Table("change", "count", title=f"{baseline} → {target}")
    table.add_row("axioms added", str(len(result.added)))
    table.add_row("axioms removed", str(len(result.removed)))
    table.add_row("annotations changed", str(len(result.labels_changed)))
    console.print(table)
    if result.empty:
        console.print("[yellow]no logical or annotation change[/]")
        return
    for label, lines, colour in (
        ("+", result.added, "green"), ("-", result.removed, "red"),
        ("~", result.labels_changed, "yellow"),
    ):
        shown = lines if limit == 0 else lines[:limit]
        for line in shown:
            console.print(f"[{colour}]{label}[/] {_readable(line, labels or {})}")
        if len(lines) > len(shown):
            console.print(f"[dim]  … {len(lines) - len(shown)} more[/]")


def _write_diff(config, baseline: str, target: str, result: versioning.Diff) -> Path:
    """Next to the whole ontology, because the two are read together: the artifact says what
    the ontology is, the diff says what this iteration did to it."""
    ontology_dir = config.paths.work_dir / "ontology"
    ontology_dir.mkdir(parents=True, exist_ok=True)
    path = ontology_dir / f"{baseline}-to-{target}.diff.json"
    payload = {"from": baseline, "to": target, **asdict(result)}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _publish_diff(config, conn, version_id: str, *, limit: int = 10) -> None:
    """Every command that commits a version reports the diff against the one before it."""
    pair = versioning.diff_with_parent(conn, version_id)
    if pair is None:
        console.print(f"[dim]{version_id} is a root version: nothing to diff against[/]")
        return
    parent, result = pair
    _, before = versioning.load(conn, parent.id)
    _, after = versioning.load(conn, version_id)
    _show_diff(parent.id, version_id, result, limit, versioning.label_index(before, after))
    console.print(f"[green]wrote[/] {_write_diff(config, parent.id, version_id, result)}")


@app.command("diff")
def diff_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    against: str | None = AgainstOption,
    limit: int = DiffLimitOption,
) -> None:
    """Semantic diff between two ontology versions (spec 6.8).

    Canonical logical axioms with canonicalized blank nodes, annotations in a lane of their
    own: a reordered serialization is not a change, and a rename is a label change rather
    than axiom churn.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    versioning.install(conn)
    target = _resolve_version(conn, version)

    if against is None:
        pair = versioning.diff_with_parent(conn, target)
        if pair is None:
            console.print(
                f"[yellow]{target} is a root version[/]: no parent to diff against. "
                "Pass --against to compare it with any other version."
            )
            return
        baseline = pair[0].id
        result = pair[1]
    else:
        baseline = _resolve_version(conn, against)
    _, before = versioning.load(conn, baseline)
    _, after = versioning.load(conn, target)
    if against is not None:
        result = versioning.diff(before, after)

    _show_diff(baseline, target, result, limit, versioning.label_index(before, after))
    console.print(f"[green]wrote[/] {_write_diff(config, baseline, target, result)}")


@app.command("regenerate")
def regenerate_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    force: bool = ForceRegenOption,
) -> None:
    """Recompute the ABox from the mention layer (plan_reglas_de_mapeo.md).

    Not a migration: the ABox derives from the mentions and from one ontology version, so a
    reorganization of the TBox never needs one — the rules change and this runs again. Pure and
    read-only over the mention layer, which is the invariant of section 3 and the one this
    stage could break by accident.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    # The falsity marks travel as per-case exceptions, so a refutation reaches the rules hash.
    # A decision that changed no rule would be a decision this stage never notices: it is
    # idempotent over (state, rules) by design.
    rules = mapping.rules_from_config(config.mapping, config.seed.base_iri).with_exceptions(
        conflicts.as_exceptions(conn)
    )

    rows, typings = mapping.load_inputs(conn, version_id)
    if not rows:
        raise typer.BadParameter("no mentions; run extract first")

    changed = versioning.record_rules(conn, version_id, rules.rules_hash())
    if not changed and not force:
        console.print(
            f"[yellow]{version_id} already regenerated[/] under {rules.rules_hash()[:19]}; "
            "nothing to do. --force writes it again."
        )
        return

    _, tbox = versioning.load(conn, version_id)
    result = mapping.regenerate(rows, typings, rules, conflicts.ancestors(tbox))
    target = config.paths.work_dir / "ontology" / f"{version_id}.abox.trig"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.dataset.serialize(format="trig"), encoding="utf-8")

    table = Table("what", "count", "note")
    table.add_row("individuals", str(result.n_individuals), f"from {len(rows)} mentions")
    table.add_row("typed", str(result.n_typed), f"zones {', '.join(rules.type_from)}")
    table.add_row("untyped", str(result.n_untyped), "orphans, or typed in a rejected zone")
    table.add_row("possible duplicates", str(result.n_unresolved),
                  "out of the functional-property support count")
    table.add_row("type conflicts", str(result.n_conflicts), f"policy {rules.conflict_policy}")
    table.add_row("excluded by a mark", str(result.n_excluded), "refuted or misextracted")
    table.add_row("quads", str(result.n_triples), rules.provenance)
    console.print(table)
    console.print(
        f"version [bold]{version_id}[/] · rules {result.rules_hash[:19]}\n"
        f"[green]wrote[/] {target}"
    )


@app.command()
def versions(config_path: Path = ConfigOption) -> None:
    """The version DAG. Branches that were not chosen are kept and stay reachable."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    versioning.install(conn)
    table = Table("version", "parent", "iteration", "branch", "state", "note")
    for row in conn.execute("SELECT * FROM versions ORDER BY created_at"):
        table.add_row(
            row["id"], row["parent_id"] or "-", str(row["iteration"]),
            row["branch_id"] or "-", row["state_hash"][7:19], row["note"] or "",
        )
    console.print(table)


@app.command()
def chunks(
    doc_id: str,
    config_path: Path = ConfigOption,
) -> None:
    """Show B1's extraction units for one document. Derived, never stored."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    document_chunks = chunk_document(
        load_block_objects(conn, doc_id),
        config.chunking.target_chars,
        config.chunking.max_chars,
    )
    if not document_chunks:
        raise typer.BadParameter(f"no blocks for {doc_id}")
    table = Table("chunk", "pages", "blocks", "chars", "context")
    for chunk in document_chunks:
        table.add_row(
            str(chunk.ordinal),
            ",".join(str(page) for page in chunk.pages),
            str(len(chunk.block_ids)),
            str(len(chunk.text)),
            str(len(chunk.context_block_ids)),
        )
    console.print(table)


cq_app = typer.Typer(help="Competency questions: the primary stopping criterion.")
app.add_typer(cq_app, name="cq")


@cq_app.command("import")
def cq_import(
    path: Path,
    config_path: Path = ConfigOption,
) -> None:
    """A4: load questions written by the user, each paired with its SPARQL."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    questions = cq.read_file(path)
    console.print(f"[green]stored[/] {cq.add(conn, questions)} competency questions")


@cq_app.command("propose")
def cq_propose(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    per_stratum: int = PerStratumOption,
    seed: int = SeedOption,
) -> None:
    """A3: generate competency questions from the corpus, filtered before you read them.

    **They measure completeness with respect to the corpus, not to the domain.** That is the
    same limitation as novelty saturation and it cannot be fixed from inside; the mitigation is
    A4 — the questions you write without looking at these.

    Sampling is stratified because the strata are what make the question types possible: a
    passage saying "must not" is where a restrictive question comes from, and it is invisible
    in a random sample of paragraphs. Generation is one prompt per type with a quota, because
    the inferential and negative types are the ones a model never writes on its own and the
    ones the spec says pay best.

    What survives the mechanical filter is `proposed`. Accepting is yours, and it is one-time
    work rather than per-iteration work.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    blocks = _corpus_blocks(conn)
    if not blocks:
        raise typer.BadParameter("no parsed blocks; run ingest first")
    strata = cq_generation.sample(blocks, per_stratum=per_stratum, seed=seed)

    table = Table("stratum", "passages")
    for name, passages in sorted(strata.items()):
        table.add_row(name, str(len(passages)))
    console.print(table)
    missing = set(cq_generation.STRATA) | {cq_generation.TABLE_STRATUM} - set(strata)
    if missing:
        console.print(
            f"[yellow]no passage found for[/] {', '.join(sorted(missing))} — the question "
            "types those strata support cannot be grounded in this corpus"
        )

    labels = [
        target.label for target in typing_store.targets_from(graph, "label")
    ]
    quota = config.cq.type_quota or {name: 10 for name in cq.TYPES}
    pool = [item for passages in strata.values() for item in passages]

    payloads = [
        (cq_type, cq_generation.payload(cq_type, pool, labels, count))
        for cq_type, count in sorted(quota.items()) if count
    ]
    model = llm.build(config.llm, timeout_s=config.execution.request_timeout_s)
    stage = llm.settings(config.llm, cq_generation.STAGE)
    with console.status(f"propose-cq · {len(payloads)} types"):
        result = llm.run(
            Ledger(conn, config.execution), model, cq_generation.PROMPT, stage,
            payloads, cq_generation.parse,
        )

    answers = {
        cq_type: answer["questions"] for cq_type, answer in result.outputs.items()
    }
    existing = [q.question for q in cq.load(conn, status=cq.ACCEPTED)]
    existing += [q.question for q in cq.load(conn, status=cq_generation.PROPOSED)]
    similarity = _text_similarity(config)
    if similarity is None:
        console.print(
            "[dim]  near-duplicate detection is one of them; the text fallback lets two "
            "questions differing by a word through, which is a review cost[/]"
        )
    filtered = cq_generation.screen(
        answers, {cq_type: pool for cq_type in answers}, existing, similarity=similarity,
    )
    cq.add(conn, filtered.kept)

    summary = Table("what", "count")
    summary.add_row("questions generated", str(sum(len(v) for v in answers.values())))
    summary.add_row("kept", str(len(filtered.kept)))
    summary.add_row("dropped by the filter", str(len(filtered.dropped)))
    summary.add_row("failed", str(len(result.failures)))
    console.print(summary)
    for question, why in filtered.dropped[:8]:
        console.print(f"  [yellow]dropped[/] {question[:60]} — {why}")

    short = cq_generation.shortfall(filtered.kept, quota)
    if short:
        console.print(
            "[yellow]below quota[/]: "
            + ", ".join(f"{name} needs {count} more" for name, count in sorted(short.items()))
            + ". Reported, never topped up with another type — the inferential and negative "
            "quotas exist because a model does not write those on its own."
        )
    console.print(
        "\n[dim]These measure completeness against the corpus, not the domain. Write your own "
        "(A4) before reading them, or that limitation goes unmitigated.[/]"
    )
    console.print("Review them with `onto-pipeline cq list --status proposed`.")


@cq_app.command("list")
def cq_list(
    config_path: Path = ConfigOption,
    status: str = CqStatusOption,
    limit: int | None = LimitOption,
) -> None:
    """The questions in the store, by status."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    questions = cq.load(conn, status=status)

    table = Table("id", "type", "lang", "question", "cited")
    for question in questions[: limit or 40]:
        table.add_row(
            question.id, question.cq_type, question.language, question.question[:60],
            f"{(question.citation or {}).get('document_id', '—')[:18]}"
            f" p.{(question.citation or {}).get('page', '?')}",
        )
    console.print(table)
    console.print(f"{len(questions)} with status {status}")


@cq_app.command("accept")
def cq_accept(
    ids: list[str],
    config_path: Path = ConfigOption,
    discard: bool = DiscardOption,
) -> None:
    """Accept (or `--discard`) proposed questions. The third action of A3's review,
    reformulating, is an edit and goes back through `cq import`."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    decision = cq.DISCARDED if discard else cq.ACCEPTED
    changed = cq.decide(conn, ids, decision)
    console.print(f"[green]{decision}[/] {changed} question(s)")


@cq_app.command("eval")
def cq_eval(
    config_path: Path = ConfigOption,
    iteration: int = IterationOption,
    version: str | None = VersionOption,
    infer: bool = InferOption,
) -> None:
    """Run every accepted CQ against an ontology version and record the pass rate.

    Queries the entailed graph by default: an inferential question asks what the reasoner
    contributes, and SPARQL over the asserted triples alone cannot see it.
    """
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    questions = cq.load(conn)
    if not questions:
        raise typer.BadParameter("no accepted competency questions")

    versioning.install(conn)
    version_id = _resolve_version(conn, version)
    _, graph = versioning.load(conn, version_id)

    # SPARQL reads triples, and an entailment is not a triple until something writes it down.
    # The inferential type is a mandatory quota in spec 4.4 whose stated point is that the
    # reasoner contributes; over the asserted graph alone such a question can never pass.
    asserted = len(graph)
    if infer:
        from .reasoning import InconsistentOntology, Reasoners, ReasonerUnavailable

        try:
            reasoners = Reasoners(
                config.paths.reasoner_lib, hermit_timeout_s=config.reasoner.hermit_timeout_s
            )
            graph = reasoners.inferred_graph(graph)
            console.print(
                f"version [bold]{version_id}[/] · {asserted} asserted triples, "
                f"{len(graph)} with entailments"
            )
        except ReasonerUnavailable as exc:
            console.print(
                f"[yellow]reasoning unavailable[/] ({exc}). Inferential questions will "
                "under-report; --no-infer silences this."
            )
        except InconsistentOntology as exc:
            raise typer.BadParameter(str(exc)) from exc
    else:
        console.print(
            f"version [bold]{version_id}[/] · asserted triples only, "
            "so inferential questions will under-report"
        )

    evaluation = cq.evaluate(graph, questions, iteration=iteration)
    cq.record(conn, evaluation)

    table = Table("cq", "type", "answered", "rows")
    for question in questions:
        answered = (
            "error" if question.id in evaluation.errored
            else "yes" if question.id in evaluation.passed
            else "no"
        )
        table.add_row(question.id, question.cq_type, answered,
                      str(evaluation.n_rows.get(question.id, "")))
    console.print(table)
    console.print(
        f"pass rate [bold]{evaluation.pass_rate:.0%}[/] "
        f"(target {config.cq.target_pass_rate:.0%})"
    )
    for cq_id, error in evaluation.errored.items():
        console.print(f"[red]{cq_id}[/]: {error}")


@app.command()
def blocks(
    doc_id: str,
    config_path: Path = ConfigOption,
    page: int | None = PageOption,
) -> None:
    """Dump the block store for one document as JSON (provenance inspection)."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    query = "SELECT * FROM blocks WHERE document_id = ?"
    params: list = [doc_id]
    if page is not None:
        query += " AND page = ?"
        params.append(page)
    rows = conn.execute(query + " ORDER BY page, ordinal", params).fetchall()
    print(json.dumps([dict(row) for row in rows], ensure_ascii=False))


if __name__ == "__main__":
    app()
