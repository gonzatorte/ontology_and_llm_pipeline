"""La interfaz de banderas: un comando por etapa, y nada más.

**Los cuerpos de las etapas no viven acá.** Viven en `services/`, porque hay una segunda
interfaz —`wizard`, línea por línea— sobre exactamente las mismas funciones, y una etapa cuyo
cuerpo está pegado a Typer no se puede correr desde ningún otro lado. Un comando de este
archivo hace tres cosas y ninguna más: leer banderas, llamar a un servicio, y pasarle el
resultado a `render`.

Los errores que el usuario tiene que arreglar salen de los servicios como `StageError` y los
atrapa `entrypoint`, una sola vez, en vez de cuarenta `try` iguales.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import typer
from rich.console import Console

from . import orchestration, render, sessions, versioning
from .providers import load_env_file
from .services import StageError, Workspace, deliver, evaluate, iterate, prep

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
UseCaseNameOption = typer.Option(
    ..., "--use-case", help="Directorio bajo `use_cases/`: el par (ontología inicial, corpus)."
)
SessionNameOption = typer.Option("", "--name", help="Un nombre para acordarse, opcional.")
SessionArgument = typer.Argument(None, help="Por defecto, la sesión actual.")
SessionOption = typer.Option(
    None, "--session",
    help="Sesión de usuario sobre la que correr. Por defecto, la actual (`session use`).",
)
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
    ..., "--mark",
    help="refuted (the document is wrong) | misextracted (ITER-EXTRACT read it wrong).",
)
MentionsOption = typer.Option(..., "--mention", "-m", help="Repeatable: mention ids to mark.")
ExportOption = typer.Option(
    None, "--export", help="Write the misextractions to this JSONL, for the evaluation set."
)
PerStratumOption = typer.Option(
    12, "--per-stratum", help="Passages sampled per stratum. The spec says 10-15."
)
SeedOption = typer.Option(0, "--seed", help="Sampling seed; the sample is deterministic.")
CqStatusOption = typer.Option("proposed", "--status", help="proposed | accepted | discarded.")
DiscardOption = typer.Option(False, "--discard", help="Discard instead of accepting.")
ToOption = typer.Option(
    None, "--to", help="The class the mention really is. Omit with --none to orphan it."
)
NoneOfTheseOption = typer.Option(
    False, "--none", help="None of the candidates. A real answer, not a refusal."
)
ExportLabelsOption = typer.Option(
    None, "--export", help="Write the accept/reject labels to this JSONL (ITER-TUNE)."
)
TermOption = typer.Option(
    [], "--term",
    help="Repetible: vocabulario que define el dominio. Si no aparece, el par no sirve.",
)
RunOption = typer.Option(
    False, "--run", help="Ejecutar la etapa siguiente. Una sola, y nunca una decisión tuya."
)
RunEnvFileOption = typer.Option(
    None, "--run-env-file", help="Env file para la etapa que --run ejecute, si necesita modelo."
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
RefreshOption = typer.Option(False, "--refresh", help="Ask again for what is already labelled.")
DryRunOption = typer.Option(
    False, "--dry-run", help="Show what would be asked about, and ask nothing."
)
ChooseOption = typer.Option(
    None, "--choose", help="Apply this branch. Its siblings are recorded as rejected."
)
WhyOption = typer.Option("", "--why", help="Why this branch, in your words. Kept with it.")
InvalidOption = typer.Option(
    [], "--invalid",
    help="Repetible: hermanas que además de no elegidas están MAL. Señal más fuerte que "
         "rechazarlas, y ITER-FEEDBACK las separa a propósito.",
)
UseCaseOption = typer.Argument(
    ..., help="Caso de uso: un directorio bajo use_cases_root."
)
MatchAgainstOption = typer.Option(
    [], "--match-against", "-m", help="Repeatable: label | gloss | label_and_gloss."
)
CrossEncoderSweepOption = typer.Option(
    False, "--cross-encoder", help="Run each variant with and without the re-ranker."
)
TrainFractionOption = typer.Option(
    None, "--train", help="Fracción de documentos para entrenar; el resto evalúa."
)
NegativesOption = typer.Option(
    None, "--negatives",
    help="Negativos por mención: clases que el bi-encoder puso primero y no eran la correcta.",
)
MethodOption = typer.Option(
    None, "--method", help="auto | full | lora. Por defecto, lo que diga el config."
)
EvalOnOption = typer.Option(
    None, "--eval-on", help="Evaluar contra otro par: mide si transfiere. Medido: poco."
)
OutOption = typer.Option(None, "--out", help="Dónde guardar el modelo ajustado.")
ContextOption = typer.Option(
    "none", "--context",
    help="none | sentence: qué texto representa a la mención. Medido: `sentence` empeora.",
)
HoldoutOption = typer.Option(
    None, "--holdout", help="Withhold this fraction of classes to manufacture genuine orphans."
)
KeepExcludedOption = typer.Option(
    False, "--keep-excluded",
    help="Keep classes the corpus guarantees are wrong. Shows how much error they cause.",
)
ExportFormatOption = typer.Option(
    "trig", "--format",
    help="trig conserva la procedencia por documento; ttl la aplana en un solo grafo.",
)
ExportOutOption = typer.Option(
    None, "--out", help="Dónde escribir la ontología. Por defecto, junto a las demás."
)
TboxOnlyOption = typer.Option(
    False, "--tbox-only", help="Sólo la ontología, sin las instancias derivadas del corpus."
)
StaleAboxOption = typer.Option(
    True, "--refresh-abox/--no-refresh-abox",
    help="Regenerar el ABox antes de exportar. Es puro: no llama al modelo ni al razonador.",
)
InitialOntologyOption = typer.Option(
    None, "--seed-ontology",
    help="Ontología semilla de esta corrida. Reemplaza a paths.initial_ontology.",
)


# La sesión elegida con `--session`, que es una opción global y va antes del subcomando. Un
# `state` de módulo y no un parámetro por comando: son cuarenta comandos y la alternativa es
# repetir la bandera en todos, que es donde alguno se olvida.
_CHOSEN: dict[str, str | None] = {"session": None}


@app.callback()
def main(
    env_file: Path | None = EnvFileOption,
    session: str | None = SessionOption,
) -> None:
    """Env files are explicit, never auto-discovered."""
    if env_file is not None:
        names = load_env_file(env_file)
        console.print(f"[dim]loaded {', '.join(names)} from {env_file}[/]")
    _CHOSEN["session"] = session


def _workspace(config_path: Path) -> Workspace:
    return Workspace.open(config_path, session_id=_CHOSEN["session"])


# ─────────────────────────────  PREP  ─────────────────────────────


@app.command("ingest")
def ingest_cmd(
    config_path: Path = ConfigOption,
    limit: int | None = LimitOption,
    document: list[Path] | None = DocumentOption,
) -> None:
    """PREP-CLASSIFY+PREP-PARSE: classify pages, parse, populate blocks and the Markdown."""
    workspace = _workspace(config_path)
    with console.status("ingesting") as status:
        result = prep.ingest(
            workspace, documents=document, limit=limit, progress=status.update
        )
    render.ingestion(console, result)


@app.command()
def report(config_path: Path = ConfigOption, doc_id: str | None = DocIdOption) -> None:
    """DELIVERABLES-PENDING-PARSER-EVAL: self-contained HTML for manual parser evaluation."""
    for target in deliver.reports(_workspace(config_path), doc_id=doc_id):
        console.print(f"[green]wrote[/] {target}")


@app.command("normalize")
def normalize_cmd(config_path: Path = ConfigOption) -> None:
    """PREP-NORMALIZE: opaque IRIs, derived labels, typo detection, gloss contexts."""
    workspace = _workspace(config_path)
    result = prep.normalize(workspace)
    render.normalization(console, result)
    if result.committed is not None:
        published = deliver.publish_diff(workspace, result.committed.id)
        if published is not None:
            render.comparison(console, published)

    if workspace.config.llm.provider == "none":
        console.print(
            f"[yellow]glosses skipped[/]: {result.pending_glosses} need generation and "
            "llm.provider is 'none'. Set a provider in the config to run it."
        )
        return
    with console.status("glosses") as status:
        bootstrap = prep.generate_glosses(workspace, result, progress=status.update)
    render.glosses(console, bootstrap)
    published = deliver.publish_diff(workspace, bootstrap.committed.id)
    if published is not None:
        render.comparison(console, published)


@app.command()
def status(config_path: Path = ConfigOption) -> None:
    """Telemetry: work units and cost per stage, page classes across the corpus."""
    render.telemetry(console, deliver.telemetry(_workspace(config_path)))


@app.command("alignment")
def alignment_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
    terms: list[str] = TermOption,
) -> None:
    """¿El corpus habla de lo que la semilla nombra? (DEBT-QUALITATIVE-PAIR)

    La cobertura global es diagnóstico y no veredicto, y eso está medido: la primera versión de
    este comando la usaba para decidir y daba 20% sobre un par bueno contra 50% sobre uno roto.
    Donde sí decide es con `--term`.
    """
    render.alignment(
        console, prep.alignment(_workspace(config_path), version=version, terms=list(terms)),
        limit=limit,
    )


# ─────────────────────────────  ITER  ─────────────────────────────


@app.command("extract")
def extract_cmd(
    config_path: Path = ConfigOption,
    doc_id: str | None = DocIdOption,
    include_held_out: bool = IncludeHeldOutOption,
) -> None:
    """ITER-EXTRACT: extract candidate mentions from every chunk."""
    with console.status("ITER-EXTRACT") as status:
        result = iterate.extract(
            _workspace(config_path), doc_id=doc_id, include_held_out=include_held_out,
            progress=status.update,
        )
    render.extraction(console, result)


@app.command("coref")
def coref_cmd(
    config_path: Path = ConfigOption,
    doc_id: str | None = DocIdOption,
    include_held_out: bool = IncludeHeldOutOption,
) -> None:
    """ITER-COREFER: intra-document coreference over the mentions ITER-EXTRACT extracted."""
    with console.status("ITER-COREFER") as status:
        result = iterate.corefer(
            _workspace(config_path), doc_id=doc_id, include_held_out=include_held_out,
            progress=status.update,
        )
    render.coreference(console, result)


@app.command("match")
def match_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    include_held_out: bool = IncludeHeldOutOption,
) -> None:
    """ITER-MATCH: type the mentions against an ontology version, then resolve entities."""
    with console.status("ITER-MATCH") as status:
        result = iterate.match(
            _workspace(config_path), version=version, include_held_out=include_held_out,
            progress=status.update,
        )
    render.matching(console, result)


grey_app = typer.Typer(help="The matcher's grey zone: the pairs it will not decide alone.")
app.add_typer(grey_app, name="grey")


@grey_app.command("list")
def grey_list(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
    as_json: bool = JsonOption,
) -> None:
    """Grey-zone pairs still unanswered (ITER-MATCH)."""
    queue = iterate.grey_pending(_workspace(config_path), version=version, limit=limit)
    if as_json:
        print(json.dumps([vars(pair) for pair in queue.pairs], ensure_ascii=False))
        return
    render.grey_queue(console, queue)


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
    reaches induction, which is where a genuinely new concept belongs.
    """
    chosen = iterate.grey_answer(
        _workspace(config_path), mention_id, version=version, to=to,
        none_of_these=none_of_these, comment=comment,
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
    """The accept/reject labels these answers have accumulated (ITER-TUNE)."""
    workspace = _workspace(config_path)
    rows = iterate.grey_labels(workspace)
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
        iterate.grey_export(workspace, export)
        console.print(f"[green]wrote[/] {export}")


@app.command("bridge")
def bridge_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
) -> None:
    """ITER-BRIDGE: relate orphans to seed classes by world knowledge."""
    with console.status("ITER-BRIDGE") as status:
        result = iterate.bridge(_workspace(config_path), version=version, progress=status.update)
    render.bridging(console, result)


@app.command("induce")
def induce_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
) -> None:
    """ITER-INDUCE: turn orphan mentions into proposed classes."""
    with console.status("ITER-INDUCE") as status:
        result = iterate.induce(_workspace(config_path), version=version, progress=status.update)
    render.induction(console, result)


@app.command("axiomatize")
def axiomatize_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    apply_changes: bool = ApplyOption,
) -> None:
    """Turn proposed classes into axioms, validate them, and commit a new version."""
    with console.status("ITER-AXIOMATIZE") as status:
        result = iterate.axiomatize(
            _workspace(config_path), version=version, override_structural=apply_changes,
            progress=status.update,
        )
    render.axiomatization(console, result)


@app.command("branch")
def branch_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    choose: str | None = ChooseOption,
    why: str = WhyOption,
    invalid: list[str] = InvalidOption,
    apply_changes: bool = ApplyOption,
) -> None:
    """Find the decision axes in this iteration's proposed axioms, and put them to the user.

    Nothing here asks a model for alternatives — the spec's one prohibition for this stage.
    """
    workspace = _workspace(config_path)
    if choose is not None:
        with console.status("applying the branch") as status:
            result = iterate.choose_branch(
                workspace, choose, version=version, why=why, invalid=list(invalid),
                override_structural=apply_changes, progress=status.update,
            )
        render.branch_choice(console, result)
        return
    with console.status("ITER-BRANCH") as status:
        survey = iterate.survey_branches(workspace, version=version, progress=status.update)
    render.branches(console, survey)
    if not survey.automatic:
        console.print("Choose one with `onto-pipeline branch --choose <branch> --why '...'`.")


@app.command("enrich")
def enrich_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
    dry_run: bool = DryRunOption,
) -> None:
    """Improve the glosses from definitional passages in the corpus, and harvest synonyms."""
    with console.status("ITER-AXIOMATIZE-ENRICH") as status:
        result = iterate.enrich(
            _workspace(config_path), version=version, limit=limit, dry_run=dry_run,
            progress=status.update,
        )
    render.enrichment(console, result, limit=limit)


@app.command("circular")
def circular_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
) -> None:
    """Matches whose own document helped write the class they matched (PREP-NORMALIZE)."""
    render.circularity(
        console, iterate.circular(_workspace(config_path), version=version), limit=limit
    )


@app.command("metaproperties")
def metaproperties_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
    refresh: bool = RefreshOption,
) -> None:
    """Label each class for OntoClean, so ITER-VALIDATE-4-ONTOCLEAN has something to check."""
    with console.status("metaproperties") as status:
        result = iterate.metaproperties(
            _workspace(config_path), version=version, limit=limit, refresh=refresh,
            progress=status.update,
        )
    render.metaproperties(console, result)


@app.command("conflicts")
def conflicts_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    limit: int | None = LimitOption,
) -> None:
    """Entities two documents typed differently (ITER-CONFLICTS)."""
    render.conflicts(
        console, iterate.survey_conflicts(_workspace(config_path), version=version), limit=limit
    )


@app.command("mark")
def mark_cmd(
    mentions: list[str] = MentionsOption,
    mark: str = MarkOption,
    config_path: Path = ConfigOption,
    comment: str = CommentOption,
    export: Path | None = ExportOption,
) -> None:
    """Mark an assertion false. `refuted` and `misextracted` are opposite signals."""
    workspace = _workspace(config_path)
    marked = iterate.mark(workspace, list(mentions), mark, comment=comment)
    console.print(f"[green]marked[/] {marked} mention(s) as {mark}")
    console.print(
        "[dim]The mapping rules changed, so the ABox is stale: run `regenerate` to apply it.[/]"
    )
    if export is not None:
        console.print(f"[green]wrote[/] {iterate.export_misextractions(workspace, export)}")


@app.command("functional")
def functional_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    declare: str | None = DeclareOption,
    yes: bool = YesOption,
    limit: int | None = LimitOption,
) -> None:
    """Survey the ABox for functional-property candidates, and ask (ITER-APPLY).

    Nothing here declares a property functional on its own: detecting functionality from the
    ABox is invalid in principle under the open-world assumption.
    """
    workspace = _workspace(config_path)
    if declare is not None:
        result = iterate.declare_functional(workspace, declare, version=version, commit=yes)
        render.functional_declaration(console, result)
        if not yes:
            console.print("Nothing committed. `--yes` commits the declaration.")
        return
    render.functional_candidates(
        console, iterate.survey_functional(workspace, version=version), limit=limit
    )
    console.print(
        "See what one would merge before answering: "
        "`onto-pipeline functional --declare <property>`"
    )


@app.command()
def validate(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
) -> None:
    """ITER-VALIDATE's reasoner filters over an ontology version, plus profile detection.

    ELK never returns an approval: an inconsistency it finds is real, but its silence only
    means the offending axiom may have been ignored.
    """
    with console.status("ITER-VALIDATE") as status:
        status.update("running the reasoners")
        result = iterate.validate(_workspace(config_path), version=version)
    render.validation(console, result)


@app.command("regenerate")
def regenerate_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    force: bool = ForceRegenOption,
) -> None:
    """Recompute the ABox from the mention layer (mapping_rules_plan.md)."""
    render.regeneration(
        console, iterate.regenerate(_workspace(config_path), version=version, force=force)
    )


# ─────────────────────────────  EVAL  ─────────────────────────────


@app.command("stop")
def stop_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    iteration: int = IterationOption,
    curve: bool = CurveOption,
) -> None:
    """Should this stop? The four criteria of EVAL-STOPPING, with their roles."""
    render.stopping(
        console, evaluate.assess(_workspace(config_path), version=version, iteration=iteration),
        curve=curve,
    )


@app.command("tune")
def tune_cmd(
    name: str = UseCaseOption,
    config_path: Path = ConfigOption,
    train_fraction: float | None = TrainFractionOption,
    negatives: int | None = NegativesOption,
    method: str | None = MethodOption,
    eval_on: str | None = EvalOnOption,
    out: Path | None = OutOption,
) -> None:
    """Ajusta el cross-encoder con las anotaciones de un par (ITER-TUNE)."""
    with console.status("ITER-TUNE") as status:
        result = evaluate.tune(
            _workspace(config_path), name, train_fraction=train_fraction,
            negatives=negatives, method=method, eval_on=eval_on, out=out,
            progress=status.update,
        )
    render.tuning(console, result)


@app.command("calibrate")
def calibrate_cmd(
    name: str = UseCaseOption,
    config_path: Path = ConfigOption,
    match_against: list[str] = MatchAgainstOption,
    cross_encoder: bool = CrossEncoderSweepOption,
    holdout: float | None = HoldoutOption,
    keep_excluded: bool = KeepExcludedOption,
    context: str = ContextOption,
    limit: int | None = LimitOption,
) -> None:
    """Barre los umbrales del matcher sobre un caso de uso publicado.

    Un caso de uso es un instrumento, no un destino: lo que califica al sistema es cómo se
    comporta a través de varios, no cómo le va en uno.
    """
    with console.status("calibrating") as status:
        result = evaluate.calibrate(
            _workspace(config_path), name, match_against=list(match_against),
            cross_encoder=cross_encoder, holdout=holdout, keep_excluded=keep_excluded,
            context=context, limit=limit, progress=status.update,
        )
    render.sweep(console, result)


@app.command("annotate")
def annotate_cmd(config_path: Path = ConfigOption, doc_id: str | None = DocIdOption) -> None:
    """Build the annotation tool for the held-out documents (EVAL-PIPELINE)."""
    render.annotation_tool(
        console, evaluate.build_annotation_tool(_workspace(config_path), doc_id=doc_id)
    )


@app.command("hold-out")
def hold_out(
    doc_id: list[str],
    config_path: Path = ConfigOption,
    release: bool = ReleaseOption,
) -> None:
    """Mark documents as the retention set: parsed, but never fed to the process."""
    render.retention(
        console, evaluate.hold_out(_workspace(config_path), list(doc_id), release=release)
    )


@app.command("export-annotations")
def export_annotations(path: Path, config_path: Path = ConfigOption) -> None:
    """DELIVERABLES-PENDING-BRAT-EXPORTER: retention-set JSONL to BRAT/INCEpTION."""
    render.brat_export(console, evaluate.export_annotations(_workspace(config_path), path))


review_app = typer.Typer(help="Findings from PREP-NORMALIZE that are waiting for a decision.")
app.add_typer(review_app, name="review")


@review_app.command("list")
def review_list(
    config_path: Path = ConfigOption,
    kind: str | None = KindOption,
    status: str = StatusOption,
    as_json: bool = JsonOption,
) -> None:
    """What needs a decision. Nothing is applied here; the decision is recorded."""
    workspace = _workspace(config_path)
    items = evaluate.review_items(workspace, status=status, kind=kind)
    if as_json:
        # Plain stdout, not the console: rich colours its JSON, which is unparseable.
        print(json.dumps(items, ensure_ascii=False))
        return
    render.review_list(console, items, evaluate.review_counts(workspace))


@review_app.command("resolve")
def review_resolve(
    item_id: str,
    decision: str,
    config_path: Path = ConfigOption,
    comment: str = CommentOption,
) -> None:
    """Record a decision on one finding: accept or reject."""
    evaluate.resolve_review(_workspace(config_path), item_id, decision, comment=comment)
    console.print(f"[green]{decision}[/] {item_id}")


cq_app = typer.Typer(help="Competency questions: the primary stopping criterion.")
app.add_typer(cq_app, name="cq")


@cq_app.command("import")
def cq_import(path: Path, config_path: Path = ConfigOption) -> None:
    """PREP-CQ-USER: load questions written by the user, each paired with its SPARQL."""
    stored = prep.import_questions(_workspace(config_path), path)
    console.print(f"[green]stored[/] {stored} competency questions")


@cq_app.command("propose")
def cq_propose(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    per_stratum: int = PerStratumOption,
    seed: int = SeedOption,
) -> None:
    """PREP-CQ-GENERATED: generate competency questions from the corpus, filtered first.

    **They measure completeness with respect to the corpus, not to the domain.**
    """
    with console.status("PREP-CQ-GENERATED") as status:
        result = prep.propose_questions(
            _workspace(config_path), version=version, per_stratum=per_stratum, seed=seed,
            progress=status.update,
        )
    render.proposed_questions(console, result)
    console.print("Review them with `onto-pipeline cq list --status proposed`.")


@cq_app.command("list")
def cq_list(
    config_path: Path = ConfigOption,
    status: str = CqStatusOption,
    limit: int | None = LimitOption,
) -> None:
    """The questions in the store, by status."""
    render.questions(
        console, prep.list_questions(_workspace(config_path), status=status),
        status=status, limit=limit,
    )


@cq_app.command("accept")
def cq_accept(
    ids: list[str],
    config_path: Path = ConfigOption,
    discard: bool = DiscardOption,
) -> None:
    """Accept (or `--discard`) proposed questions."""
    decision, changed = prep.decide_questions(_workspace(config_path), ids, discard=discard)
    console.print(f"[green]{decision}[/] {changed} question(s)")


@cq_app.command("eval")
def cq_eval(
    config_path: Path = ConfigOption,
    iteration: int = IterationOption,
    version: str | None = VersionOption,
    infer: bool = InferOption,
) -> None:
    """Run every accepted CQ against an ontology version and record the pass rate."""
    workspace = _workspace(config_path)
    with console.status("cq eval"):
        result = evaluate.evaluate_questions(
            workspace, version=version, iteration=iteration, infer=infer
        )
    render.question_run(console, result, target=workspace.config.cq.target_pass_rate)


# ─────────────────────────────  DELIVERABLES  ─────────────────────────────


@app.command("diff")
def diff_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    against: str | None = AgainstOption,
    limit: int = DiffLimitOption,
) -> None:
    """Semantic diff between two ontology versions (ITER-APPLY)."""
    render.comparison(
        console, deliver.compare(_workspace(config_path), version=version, against=against),
        limit=limit,
    )


@app.command()
def versions(config_path: Path = ConfigOption) -> None:
    """The version DAG. Branches that were not chosen are kept and stay reachable."""
    render.versions(console, deliver.version_rows(_workspace(config_path)))


@app.command("export")
def export_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    out: Path | None = ExportOutOption,
    fmt: str = ExportFormatOption,
    tbox_only: bool = TboxOnlyOption,
    refresh_abox: bool = StaleAboxOption,
) -> None:
    """La ontología enriquecida, con toda la historia aplicada, en un archivo.

    Cada versión guarda su Turtle entero, así que la cabeza de un linaje ya *es* la acumulación
    de lo que se aplicó para llegar hasta ella. Lo que esto agrega es recorrer la historia para
    decir qué aportó cada iteración, y juntar las dos mitades que vivían separadas: la TBox,
    que estaba sólo en SQLite, y el ABox, que estaba en disco por su cuenta.

    La procedencia va en un manifiesto JSON al lado y no adentro de la ontología: escribirla
    adentro pediría propiedades de anotación que la semilla no declara, y eso saca la ontología
    de OWL 2 DL sin dar ningún error.
    """
    with console.status("export") as status:
        result = deliver.export(
            _workspace(config_path), version=version, out=out, fmt=fmt,
            include_abox=not tbox_only, refresh_abox=refresh_abox, progress=status.update,
        )
    render.delivery(console, result)


@app.command()
def chunks(doc_id: str, config_path: Path = ConfigOption) -> None:
    """Show ITER-EXTRACT's extraction units for one document. Derived, never stored."""
    render.chunks(console, deliver.chunks(_workspace(config_path), doc_id))


@app.command()
def blocks(
    doc_id: str,
    config_path: Path = ConfigOption,
    page: int | None = PageOption,
) -> None:
    """Dump the block store for one document as JSON (provenance inspection)."""
    print(json.dumps(deliver.blocks(_workspace(config_path), doc_id, page=page),
                     ensure_ascii=False))


# ─────────────────────────────  qué corresponde correr  ─────────────────────────────


@app.command("next")
def next_cmd(
    config_path: Path = ConfigOption,
    version: str | None = VersionOption,
    run: bool = RunOption,
    env_file: Path | None = RunEnvFileOption,
) -> None:
    """What to run next, and what is waiting on you.

    Five points in this design are the user's decision — the matcher's grey zone, the branch, a
    functional property, a competency question, a typo in the seed. A pending decision outranks
    any stage that could run: everything after it would be built on an answer nobody gave.

    `--run` ejecuta **una** etapa, la siguiente, y frena. Si lo que sigue es una decisión tuya,
    no corre nada y sale con error. El wizard es la otra respuesta a la misma pregunta: en vez
    de frenar ante la decisión, te la hace.
    """
    workspace = _workspace(config_path)
    versioning.install(workspace.conn)
    version_id = workspace.latest_version() if version is None else version
    if version_id is None:
        # Sin versión todavía no es un error acá: es el estado normal de un almacén recién
        # creado, y éste es justamente el comando que tiene que decir qué hacer primero.
        version_id = "(sin versión todavía)"

    plan = orchestration.survey(
        workspace.conn, version_id, session_id=workspace.require_session(),
        has_provider=workspace.has_provider(),
    )
    render.plan(console, plan, version_id)

    waiting = orchestration.blocking(plan)
    if waiting:
        console.print(
            f"[yellow]{len(waiting)} decision(s) are yours[/], and the stages after them would "
            "be built on an answer nobody gave:"
        )
        for step in waiting:
            console.print(f"  {step.name} — [bold]{step.command}[/]")
        console.print(
            "[dim]`onto-pipeline wizard` te las hace una por una, en vez de frenar acá.[/]"
        )
        if run:
            console.print(
                "[yellow]--run no corre nada[/]: cruzar un punto de decisión sería decidirlo "
                "por default, que es lo que este comando existe para no hacer."
            )
            raise typer.Exit(code=1)
        return
    if plan.next is None:
        console.print("[green]nothing pending[/] · `onto-pipeline stop` says whether it is done")
        return
    console.print(f"[bold]next:[/] {plan.next.name} — [bold]{plan.next.command}[/]")
    if not run:
        return
    if not orchestration.runnable(plan.next):
        console.print(
            "[yellow]no se corre sola[/]: es una decisión tuya, y correr lo que viene después "
            "sería construir sobre una respuesta que nadie dio."
        )
        raise typer.Exit(code=1)

    argv = orchestration.command_line(plan.next, config_path, env_file)
    console.print(f"[dim]$ {' '.join(argv)}[/]")
    completed = subprocess.run(argv, check=False)
    if completed.returncode != 0:
        console.print(f"[red]{plan.next.name} falló[/] (código {completed.returncode})")
        raise typer.Exit(code=completed.returncode)
    console.print(
        f"[green]{plan.next.name} terminó[/] · `onto-pipeline next` para ver qué sigue"
    )


session_app = typer.Typer(
    help="Sesiones de usuario: una corrida sobre un caso de uso, con su estado e historial."
)
app.add_typer(session_app, name="session")


@session_app.command("list")
def session_list(config_path: Path = ConfigOption) -> None:
    """Las sesiones que hay, con su fase y su caso de uso."""
    workspace = Workspace.open(config_path)
    render.session_list(
        console, evaluate.list_sessions(workspace), current=workspace.session_id
    )


@session_app.command("new")
def session_new(
    config_path: Path = ConfigOption,
    use_case: str = UseCaseNameOption,
    name: str = SessionNameOption,
) -> None:
    """Crear una sesión sobre un caso de uso, y dejarla como la actual.

    El caso de uso es el par (ontología inicial, corpus) que vive en `use_cases/`. Es material
    de entrada y se comparte: dos sesiones sobre el mismo son la comparación que este proyecto
    existe para poder hacer.
    """
    workspace = Workspace.open(config_path)
    created = evaluate.new_session(workspace, use_case=use_case, name=name)
    console.print(
        f"[green]creada[/] {created.id}"
        + (f" ({created.name})" if created.name else "")
        + f" sobre {created.use_case} · fase {created.phase}"
    )
    console.print("[dim]es ahora la sesión actual; `--session <id>` elige otra por comando[/]")


@session_app.command("use")
def session_use(session_id: str, config_path: Path = ConfigOption) -> None:
    """Fijar la sesión actual, la que usan los comandos sin `--session`."""
    workspace = Workspace.open(config_path)
    console.print(f"[green]actual[/] {evaluate.use_session(workspace, session_id).id}")


@session_app.command("show")
def session_show(
    session_id: str | None = SessionArgument,
    config_path: Path = ConfigOption,
    limit: int | None = LimitOption,
) -> None:
    """Una sesión: su fase, su caso de uso, y qué pasó en ella."""
    workspace = _workspace(config_path)
    render.session_detail(
        console, *evaluate.session_detail(workspace, session_id), limit=limit
    )


@session_app.command("reopen")
def session_reopen(
    config_path: Path = ConfigOption,
    session_id: str | None = SessionArgument,
    yes: bool = YesOption,
    why: str = WhyOption,
) -> None:
    """Volver a la fase de preparación.

    **No borra nada**: re-preparar commitea una raíz nueva y el linaje viejo queda alcanzable,
    que es lo que el DAG ya hace con las ramas no elegidas. Lo que sí pasa es que el trabajo
    hecho deja de estar en el camino que la sesión sigue, y por eso se dice con números antes
    de preguntar.
    """
    workspace = _workspace(config_path)
    try:
        left = evaluate.reopen_session(workspace, session_id, confirmed=yes, why=why)
    except sessions.PhaseViolation as exc:
        console.print(f"[yellow]{exc}[/]")
        console.print("`--yes` lo hace igual.")
        raise typer.Exit(code=1) from exc
    console.print(
        "[green]vuelta a preparación[/]"
        + (f" · quedó atrás {left.summary}" if not left.empty else "")
    )


@session_app.command("close")
def session_close(
    config_path: Path = ConfigOption,
    session_id: str | None = SessionArgument,
    comment: str = CommentOption,
) -> None:
    """Cerrar una sesión. Es la única fase que se declara a mano."""
    workspace = _workspace(config_path)
    closed = evaluate.close_session(workspace, session_id, note=comment)
    console.print(f"[green]cerrada[/] {closed.id}")


@app.command("wizard")
def wizard_cmd(
    config_path: Path = ConfigOption,
    env_file: Path | None = RunEnvFileOption,
) -> None:
    """La otra interfaz: te guía, te pide lo que falta, y te devuelve la ontología enriquecida.

    Corre las etapas que se corren solas y **frena a preguntarte** en cada punto de decisión, en
    vez de saltearlo. Por detrás llama a las mismas funciones que los comandos de arriba: lo
    que cambia es quién contesta las preguntas y cuándo.
    """
    from .wizard import run as run_wizard

    run_wizard(console, config_path, session_id=_CHOSEN["session"], env_file=env_file)


def entrypoint() -> None:
    """El punto de entrada real.

    Un solo lugar convierte un `StageError` —algo que el usuario tiene que arreglar— en un
    mensaje y un código de salida. La alternativa era cuarenta `try` idénticos, uno por
    comando, y el primero que se olvidara mostraría un traceback en vez de la explicación.
    """
    try:
        app()
    except StageError as exc:
        console.print(f"[red]{exc}[/]")
        raise SystemExit(2) from exc


if __name__ == "__main__":
    entrypoint()
