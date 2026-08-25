"""Command line entry point."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import cq, versioning
from .chunking import chunk_document
from .config import Config
from .db import connect
from .ingest import STAGE, discover, ingest, load_block_objects
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
    else:
        console.print(f"[yellow]same state[/] as version {existing.id}; nothing committed")

    contexts = gloss_contexts(seed)

    def entry(entity) -> dict:
        return {
            "iri": entity.iri,
            "original_iri": entity.original_iri,
            "reason": entity.divergence_reason,
            "labels": [
                {"text": label.text, "language": label.language, "source": label.source}
                for label in entity.labels
            ],
        }

    review = {
        "divergent_labels": [
            entry(entity) for entity in seed.entities
            if entity.divergence_reason == "same_language_mismatch"
        ],
        "pending_semantic_check": [
            entry(entity) for entity in seed.entities
            if entity.divergence_reason == "cross_language_unverified"
        ],
        "typos": [asdict(finding) for finding in seed.typos],
    }
    review_path = config.paths.work_dir / "review" / "seed_review.json"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")

    kinds: dict[str, int] = {}
    for entity in seed.entities:
        kinds[entity.kind] = kinds.get(entity.kind, 0) + 1
    summary = Table("what", "count")
    for kind, count in sorted(kinds.items()):
        summary.add_row(kind, str(count))
    summary.add_row("divergent label pairs", str(len(review["divergent_labels"])))
    summary.add_row("pending semantic check", str(len(review["pending_semantic_check"])))
    summary.add_row("typo candidates", str(len(seed.typos)))
    summary.add_row("classes awaiting a gloss", str(len(contexts)))
    console.print(summary)
    console.print(f"[green]wrote[/] {target}\n[green]wrote[/] {review_path}")

    if config.llm.provider == "none":
        console.print(
            f"[yellow]A0.4 skipped[/]: {len(contexts)} glosses need generation and "
            "llm.provider is 'none'. Set a provider in the config to run it."
        )


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


@cq_app.command("eval")
def cq_eval(
    config_path: Path = ConfigOption,
    iteration: int = IterationOption,
    version: str | None = VersionOption,
) -> None:
    """Run every accepted CQ against an ontology version and record the pass rate."""
    config = Config.load(config_path)
    conn = connect(config.paths.work_dir)
    questions = cq.load(conn)
    if not questions:
        raise typer.BadParameter("no accepted competency questions")

    versioning.install(conn)
    row = conn.execute(
        "SELECT id FROM versions WHERE id = COALESCE(?, id) ORDER BY created_at DESC LIMIT 1",
        (version,),
    ).fetchone()
    if row is None:
        raise typer.BadParameter("no ontology version; run normalize-seed first")
    _, graph = versioning.load(conn, row["id"])
    console.print(f"evaluating against version [bold]{row['id']}[/]")

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
    console.print_json(json.dumps([dict(row) for row in rows], ensure_ascii=False))


if __name__ == "__main__":
    app()
