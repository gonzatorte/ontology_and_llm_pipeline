"""Cómo se ve cada resultado en una terminal. Nada acá decide nada.

La capa de servicios devuelve resultados tipados y no imprime; esto los pinta. Vive aparte de
`cli.py` porque las **dos** interfaces muestran las mismas cosas: el wizard enseña la misma
tabla de veredictos que el comando `validate`, y una segunda copia se habría separado de la
primera en la primera corrección.

Cada función recibe la `Console` en vez de tener una propia: así el wizard puede pintar dentro
de su propio flujo, y un test puede capturar la salida sin parchear un global.
"""

from __future__ import annotations

import statistics
from typing import Any

from rich.console import Console
from rich.table import Table

from . import calibration, versioning
from .services import deliver, evaluate, iterate, prep
from .services.deliver import readable

# ─────────────────────────────  PREP  ─────────────────────────────


def ingestion(console: Console, result: prep.Ingestion) -> None:
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


def normalization(console: Console, result: prep.Normalization) -> None:
    if result.committed is not None:
        console.print(
            f"[green]committed[/] version {result.committed.id} "
            f"{result.committed.state_hash[:19]}"
        )
    else:
        console.print(
            f"[yellow]same state[/] as version {result.same_state_as.id}; nothing committed"
        )

    summary = Table("what", "count")
    for kind, count in sorted(result.kinds.items()):
        summary.add_row(kind, str(count))
    summary.add_row("classes awaiting a gloss", str(result.pending_glosses))
    summary.add_row("review: new findings", str(result.review_added))
    summary.add_row("review: already decided or open", str(result.review_known))
    summary.add_row("review: superseded", str(result.review_superseded))
    console.print(summary)
    console.print(f"[green]wrote[/] {result.target}")
    console.print("run [bold]onto-pipeline review list[/] to see what needs a decision")


def glosses(console: Console, result: prep.GlossBootstrap) -> None:
    console.print(
        f"[green]PREP-NORMALIZE-GLOSSES[/]: {result.written} glosses "
        f"({result.executed} generated, {result.cached} cached, "
        f"{len(result.failures)} failed) · "
        f"{result.in_tokens} in / {result.out_tokens} out tokens"
    )
    for iri, error in list(result.failures.items())[:5]:
        console.print(f"  [red]{iri}[/]: {error}")
    console.print(
        f"[green]committed[/] {result.committed.id} (parent {result.parent_id or '-'})"
    )


def alignment(console: Console, result: prep.Alignment, *, limit: int | None = None) -> None:
    report = result.report
    multiword = report.multiword
    present = [item for item in multiword if not item.absent]

    table = Table("qué", "cuántas", "de")
    table.add_row("clases sin una sola aparición", str(len(report.absent)), str(result.n_classes))
    table.add_row(
        "clases en un solo documento", str(len(report.single_document)), str(result.n_classes)
    )
    table.add_row("cobertura léxica", f"{report.coverage:.0%}", "")
    table.add_row("  de etiquetas multipalabra", f"{len(present)}/{len(multiword)}", "")
    console.print(table)
    console.print(
        "[dim]Diagnóstico, no veredicto: una ontología publicada cubre un dominio entero y un "
        "corpus cubre una franja, así que baja cobertura no prueba desajuste. Medido: esta "
        "cifra daba 20% sobre un par bueno y 50% sobre uno roto.[/]"
    )
    if report.absent:
        console.print("\n[dim]las que no aparecen (las primeras):[/]")
        for item in report.absent[: limit or 12]:
            console.print(f"  [yellow]{item.label}[/]")

    if not result.decided:
        console.print(
            "\nPara que esto **decida** algo, nombrá el vocabulario que define el dominio: "
            "`--term 'field note' --term informant`. Si lo que alguien declara central no está "
            "en el corpus, no hay matcher que lo arregle."
        )
        return

    check = Table("término declarado central", "apariciones")
    for term, count in sorted(result.declared.items(), key=lambda item: item[1]):
        check.add_row(term, "[red]0[/]" if count == 0 else str(count))
    console.print(check)
    if result.missing:
        console.print(
            f"[red]desalineado[/]: {len(result.missing)} de {len(result.declared)} términos "
            f"centrales no aparecen ni una vez en {result.n_documents} documentos. Cambiá el "
            "corpus o la ontología; iterar sobre este par mide el desajuste, no el pipeline."
        )
    else:
        console.print(
            f"[green]los {len(result.declared)} términos centrales aparecen[/]. Que estén no "
            "prueba que el corpus hable de ellos en el sentido de la ontología — eso es el eco "
            "léxico —, pero que faltaran sí lo habría descartado."
        )


def proposed_questions(console: Console, result: prep.ProposedQuestions) -> None:
    table = Table("stratum", "passages")
    for name, count in result.strata.items():
        table.add_row(name, str(count))
    console.print(table)
    if result.missing_strata:
        console.print(
            f"[yellow]no passage found for[/] {', '.join(result.missing_strata)} — the "
            "question types those strata support cannot be grounded in this corpus"
        )
    if not result.deduplicated:
        console.print(
            "[dim]  near-duplicate detection needs the encoder; the text fallback lets two "
            "questions differing by a word through, which is a review cost[/]"
        )

    summary = Table("what", "count")
    summary.add_row("questions generated", str(result.generated))
    summary.add_row("kept", str(result.kept))
    summary.add_row("dropped by the filter", str(len(result.dropped)))
    summary.add_row("failed", str(len(result.failures)))
    console.print(summary)
    for question, why in result.dropped[:8]:
        console.print(f"  [yellow]dropped[/] {question[:60]} — {why}")
    if result.shortfall:
        console.print(
            "[yellow]below quota[/]: "
            + ", ".join(
                f"{name} needs {count} more" for name, count in sorted(result.shortfall.items())
            )
            + ". Reported, never topped up with another type — the inferential and negative "
            "quotas exist because a model does not write those on its own."
        )
    console.print(
        "\n[dim]These measure completeness against the corpus, not the domain. Write your own "
        "(PREP-CQ-USER) before reading them, or that limitation goes unmitigated.[/]"
    )


# ─────────────────────────────  ITER  ─────────────────────────────


def extraction(console: Console, result: iterate.Extraction) -> None:
    if result.skipped:
        console.print(
            f"[dim]reload '{result.reload}': re-processing {len(result.documents)}, "
            f"leaving {len(result.skipped)} as they are[/]"
        )
    table = Table(
        "document", "chunks", "mentions", "rejected", "unlocatable", "in tok", "out tok"
    )
    for item in result.documents:
        table.add_row(
            item.document_id[:38], str(item.chunks), str(item.mentions), str(item.rejected),
            f"[red]{item.unlocatable}[/]" if item.unlocatable else "0",
            str(item.in_tokens), str(item.out_tokens),
        )
        for chunk_id, error in list(item.failures.items())[:3]:
            console.print(f"  [red]{chunk_id}[/]: {error}")
    console.print(table)


def coreference(console: Console, result: iterate.Coreference) -> None:
    table = Table("document", "mentions", "groups", "linked", "rejected", "in tok", "out tok")
    for item in result.documents:
        table.add_row(
            item.document_id[:32], str(item.mentions), str(item.groups), str(item.linked),
            f"[red]{item.rejected}[/]" if item.rejected else "0",
            str(item.in_tokens), str(item.out_tokens),
        )
        for label, error in item.failures.items():
            console.print(f"  [red]{label}[/]: {error}")
    console.print(table)


def matching(console: Console, result: iterate.Matching) -> None:
    table = Table("what", "count", "note")
    table.add_row("mentions", str(result.total), f"against version {result.version_id}")
    table.add_row("typed automatically", str(result.typed), "")
    table.add_row("grey zone", str(result.grey), "needs a decision from you")
    table.add_row(
        "orphans", str(result.orphan),
        f"{result.orphan_rate:.0%} — false vs genuine needs the retention set (EVAL-PIPELINE)",
    )
    table.add_row("merge decisions", str(result.merges), "")
    table.add_row("pairs to ask about", str(result.unresolved), "possible_duplicate_unresolved")
    console.print(table)
    console.print(
        "[yellow]calibrado contra otro par[/]: los umbrales salieron de `craft-cl`, no de este "
        "corpus. Ver Limitaciones en el README."
    )


def grey_queue(console: Console, result: iterate.GreyQueue) -> None:
    table = Table("mention", "phrase", "candidate", "score", "runner-up", "document")
    for pair in result.pairs:
        table.add_row(
            pair.mention_id, pair.surface_text[:32], pair.candidate or "—",
            f"{pair.score:.3f}", pair.runner_up or "—", pair.document_id[:24],
        )
    console.print(table)
    console.print(
        f"{len(result.pairs)} waiting. Answer one with `onto-pipeline grey answer <mention> "
        f"--to <class>` or `--none`."
    )


def bridging(console: Console, result: iterate.Bridging) -> None:
    if not result.asked:
        console.print(
            f"[yellow]{result.orphans} orphans, none with a class above the floor[/]; "
            "nothing to ask about"
        )
        return
    table = Table("phrase", "relation", "seed class", "mentions", "score")
    for bridge in sorted(result.bridges, key=lambda item: -len(item.mention_ids))[:15]:
        table.add_row(
            bridge.surface[:34], bridge.relation,
            result.labels.get(bridge.target_iri, bridge.target_iri)[:26],
            str(len(bridge.mention_ids)), f"{bridge.score:.2f}",
        )
    console.print(table)
    console.print(
        f"[green]{len(result.bridges)} bridges[/] covering {result.covered} of "
        f"{result.orphans} orphan mentions · {result.declined} phrases the model declined to "
        f"connect\nmarked [bold]world_knowledge[/]: no citation is possible, so the evidence "
        f"filter does not apply to them (ITER-BRIDGE)\n"
        f"{result.executed} executed, {result.cached} cached, {len(result.failures)} failed · "
        f"{result.in_tokens} in / {result.out_tokens} out tokens"
    )
    for identifier, error in list(result.failures.items())[:5]:
        console.print(f"  [red]{identifier}[/]: {error}")


def induction(console: Console, result: iterate.Induction) -> None:
    if result.bridged:
        console.print(f"[dim]{result.bridged} mentions covered by a bridge; skipped[/]")
    if result.no_bridges:
        console.print(
            "[yellow]no bridges recorded[/] for this version. Run `bridge` first, or every "
            "mention the matcher missed becomes a proposed class (ITER-BRIDGE)."
        )
    if not result.orphans:
        console.print("[yellow]every orphan was bridged[/]; nothing left to induce")
        return
    if not result.clusters:
        console.print(
            f"[yellow]{result.orphans} orphans, no cluster reached the support floor[/]"
        )
        return

    table = Table("proposed class", "support", "criterion", "nearest existing")
    for proposal in sorted(result.proposals, key=lambda item: -item.support)[:20]:
        nearest = result.labels.get(proposal.nearest_iri)
        table.add_row(
            proposal.label[:28], str(proposal.support), proposal.criterion[:44],
            nearest.label if nearest else "-",
        )
    console.print(table)
    console.print(
        f"{result.orphans} orphans -> {result.clusters} clusters -> "
        f"[green]{len(result.proposals)}[/] proposed classes, {result.declined} groups the "
        f"model declined to name · {len(result.failures)} failed · "
        f"{result.in_tokens}/{result.out_tokens} tokens"
    )
    if result.duplicates:
        console.print(
            f"[yellow]{len(result.duplicates)} de {len(result.proposals)} propuestas nombran "
            "una clase que ya existe[/] — sus menciones eran falsos huérfanos, y acuñarlas "
            "duplicaría:"
        )
        for proposal in result.proposals:
            if proposal.cluster_id in result.duplicates:
                console.print(
                    f"  [yellow]{proposal.label}[/] ~ {result.duplicates[proposal.cluster_id]} "
                    f"({proposal.support} menciones)"
                )
        console.print(
            "[dim]Quedan propuestas a propósito: que la inducción reencuentre una clase que ya "
            "está es un diagnóstico sobre el matcher, y borrarlo en silencio pierde la única "
            "señal de que pasó. ITER-AXIOMATIZE decide.[/]"
        )
    console.print("[dim]nothing applied; ITER-AXIOMATIZE decides the axioms[/]")


# ─────────────────────────────  la cadena de validación  ─────────────────────────────


def chain(console: Console, result: iterate.Chain) -> None:
    """La tabla de veredictos. **ELK nunca dice `OK`**, y la tabla no lo inventa."""
    from .reasoning import REJECTED

    if result.missing_imports:
        console.print(
            "[yellow]importaciones sin resolver[/]: "
            + ", ".join(result.missing_imports)
            + ". Todo lo que el razonador diga vale sobre menos axiomas de los que la "
            "ontología declara."
        )
    table = Table("filter", "result", "detail")
    table.add_row("ELK", result.elk.verdict, result.elk.note[:52])
    table.add_row(
        "HermiT",
        "not run" if not getattr(result.hermit, "ran", True)
        else ("consistent" if result.hermit.consistent else REJECTED),
        "ELK already rejected; its finding is real"
        if not getattr(result.hermit, "ran", True)
        else f"{len(result.hermit.unsatisfiable)} unsatisfiable",
    )
    table.add_row("OntoClean", result.ontoclean.decision, result.ontoclean.note[:52])
    table.add_row("pitfalls", result.pitfalls.decision, result.pitfalls.note[:52])
    table.add_row(
        "structural", REJECTED if result.structural.rejected else "clean",
        f"depth {result.structural.depth} · {len(result.structural.findings)} finding(s)",
    )
    console.print(table)
    for finding in result.ontoclean.findings[:6]:
        console.print(f"  [red]OntoClean[/] {finding}")


def application(console: Console, result: iterate.Application) -> None:
    """Qué hizo la cadena con los axiomas: los aplicó, o dijo por qué no."""
    chain(console, result.chain)
    if result.refused == iterate.REASONER:
        for iri, justifications in result.chain.hermit.justifications.items():
            console.print(f"[red]unsatisfiable[/] {iri}")
            for axiom in (justifications[0] if justifications else []):
                console.print(f"    {axiom}")
        console.print("[red]not applied[/]: the reasoner rejected it")
        return
    if result.refused == iterate.ONTOCLEAN:
        console.print("[red]not applied[/]: OntoClean rejected a subsumption")
        return
    if result.refused == iterate.STRUCTURE:
        for finding in result.chain.structural.findings[:8]:
            console.print(f"  [yellow]{finding.check}[/] {finding.subject} — {finding.detail}")
        console.print(
            "[yellow]not applied[/]: structural findings. Fix them or run with --apply to "
            "override, which the spec allows only because these are warnings about shape."
        )
        return
    if result.refused == iterate.LOOP:
        console.print(
            f"[yellow]loop[/]: this state is {result.loop_version.id}, already in the DAG. "
            "Returning is allowed, but explicitly — nothing was committed."
        )
        return
    console.print(
        f"[green]committed[/] {result.committed.id} · "
        f"{result.before} -> {result.after} triples"
    )
    if result.diff is not None:
        comparison(console, result.diff)


def axiomatization(console: Console, result: iterate.Axiomatization) -> None:
    table = Table("what", "count")
    table.add_row("proposals judged", str(len(result.judgements)))
    for relation, count in sorted(result.tally.items()):
        table.add_row(f"  {relation}", str(count))
    table.add_row("classes to mint", str(len(result.minted)))
    table.add_row("axioms assembled", str(len(result.axioms)))
    table.add_row("  dropped for lack of evidence", str(len(result.uncited)))
    table.add_row("  ya descartadas antes", str(len(result.repeats)))
    table.add_row("proposals refused", str(len(result.refused)))
    console.print(table)
    for proposal_id, why in list(result.refused.items())[:5]:
        console.print(f"  [yellow]refused[/] {proposal_id}: {why}")
    for previous in list(result.repeats.values())[:5]:
        comment = f" — «{previous['comment']}»" if previous["comment"] else ""
        console.print(
            f"  [yellow]ya se había descartado[/] {previous['normalized_axioms']}"
            f" ({previous['status']}){comment}"
        )
    if result.application is None:
        console.print("nothing to apply")
        return
    application(console, result.application)


def branches(console: Console, result: iterate.Branching) -> None:
    if result.no_reasoner:
        console.print(
            f"[yellow]no reasoner[/] ({result.no_reasoner}); logical axes not looked for"
        )
    if result.no_similarity:
        console.print(
            "[yellow]no encoder[/] (`uv sync --extra matching`): the division-criterion axis "
            "is one of the checks that are skipped"
        )
    for class_iri in result.pre_existing:
        console.print(
            f"[red]pre-existing[/] {versioning.short_name(class_iri, result.labels)} is "
            "unsatisfiable without any of this iteration's axioms; that is a defect, not a "
            "branch"
        )
    if result.automatic:
        console.print(
            f"[green]no decision axis[/] over {len(result.axioms)} proposed axioms: they are "
            "compatible and the catalogue recognizes no commitment among them."
        )
        if result.application is None:
            console.print("[dim]branching.auto_apply_when_no_axis is off; nothing applied[/]")
            return
        console.print("[dim]applying all of them — the usual path, per ITER-BRANCH[/]")
        application(console, result.application)
        return

    for decision in result.decisions:
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
                branch.id, iterate.choice_labels(decision, branch),
                f"{branch.score.coverage:.0%}",
                str(branch.score.reorg_cost), str(branch.score.abox_regen_cost),
                "—" if branch.score.historical_affinity is None
                else f"{branch.score.historical_affinity:.2f}",
                branch.note,
            )
        console.print(table)
    console.print(
        "\n[dim]affinity is empty until something has been decided before — the cold start of "
        "COLDSTART, reported as absent rather than as zero.[/]"
    )


def branch_choice(console: Console, result: iterate.BranchChoice) -> None:
    console.print(
        f"branch {result.branch_id}: {result.kept} axioms kept, {result.given_up} given up"
    )
    application(console, result.application)
    if not result.settled:
        console.print("[yellow]decision not recorded[/]: nothing was applied")
        return
    console.print(
        f"[green]chose[/] {result.branch_id}. Its siblings are recorded as rejected"
        + (f", {len(result.invalid)} of them as invalid" if result.invalid else "")
        + " — that record is what a later iteration reads so it does not propose the same "
        "thing again."
    )


def enrichment(console: Console, result: iterate.Enrichment, *, limit: int | None = None) -> None:
    console.print(
        f"[green]passages[/] {sum(len(items) for items in result.passages.values())} "
        f"definitional across {len(result.passages)} of {result.n_classes} classes, from "
        f"{result.n_blocks} blocks"
    )
    if not result.passages:
        console.print(
            "[yellow]nothing to enrich[/]: no passage in the corpus defines a class of this "
            "ontology. On a corpus and a seed about different things, that is the expected "
            "answer, not a failure."
        )
        return
    if result.dry_run:
        table = Table("class", "passages", "documents", "cues")
        for iri, passages in list(result.passages.items())[: limit or 20]:
            table.add_row(
                result.by_iri[iri].label, str(len(passages)),
                str(len({item.document_id for item in passages})),
                ", ".join(sorted({item.cue for item in passages})),
            )
        console.print(table)
        return

    table = Table("what", "count")
    table.add_row("classes asked", str(result.asked))
    table.add_row(
        "  definitions rewritten", str(sum(1 for item in result.changed if item.definition))
    )
    table.add_row(
        "  synonyms kept", str(sum(len(item.alt_labels) for item in result.changed))
    )
    table.add_row(
        "  synonyms dropped as unattested",
        str(sum(len(item.dropped) for item in result.changed)),
    )
    table.add_row("  unchanged", str(result.unchanged))
    table.add_row("failed", str(len(result.failures)))
    console.print(table)
    for item in result.changed[:8]:
        names = f" + {', '.join(item.alt_labels)}" if item.alt_labels else ""
        console.print(f"  [green]{result.by_iri[item.iri].label}[/]{names} — {item.why[:80]}")
    for iri, error in list(result.failures.items())[:5]:
        name = result.by_iri[iri].label if iri in result.by_iri else iri
        console.print(f"  [red]{name}[/]: {error}")

    if result.committed is None:
        console.print("nothing changed; no version committed")
        return
    console.print(f"[green]committed[/] {result.committed.id} (parent {result.version_id})")
    if result.diff is not None:
        comparison(console, result.diff)
    if result.orphans:
        console.print(
            f"\n[yellow]{result.orphans} orphan mentions[/] were typed against "
            f"{result.version_id}. The synonyms just added are what closes the loop, so re-run "
            f"`match --version {result.committed.id}` to give them another chance."
        )


def circularity(console: Console, result: iterate.Circularity, *, limit: int | None = None) -> None:
    if not result.typed:
        console.print(f"no typed mentions against {result.version_id}")
        return
    console.print(
        f"[bold]{len(result.flagged)}[/] of {result.typed} typed mentions against "
        f"{result.version_id} are circular ({result.rate:.1%})"
    )
    if not result.flagged:
        return
    table = Table("mention", "class", "document", "score", "zone")
    for row in result.flagged[: limit or 20]:
        table.add_row(
            row["surface_text"][:40], versioning.short_name(row["iri"], result.labels),
            row["document_id"][:30], f"{row['score']:.3f}", row["zone"],
        )
    console.print(table)


def metaproperties(console: Console, result: iterate.Metaproperties) -> None:
    if result.all_labelled:
        console.print(
            f"[green]all {result.n_classes} classes are labelled[/] · --refresh asks again"
        )
        return
    table = Table("what", "count")
    table.add_row("classes labelled", str(len(result.written)))
    for value, count in sorted(result.tally.items()):
        table.add_row(f"  rigidity {value}", str(count))
    table.add_row("failed", str(len(result.failures)))
    table.add_row("still unlabelled", str(result.unlabelled))
    console.print(table)
    for iri, error in list(result.failures.items())[:5]:
        console.print(f"  [red]{result.labels.get(iri, iri)}[/]: {error}")


def conflicts(console: Console, result: iterate.Conflicts, *, limit: int | None = None) -> None:
    for note in result.notes:
        console.print(f"[yellow]{note}[/]")
    table = Table("what", "count", "note")
    table.add_row("entities", str(result.entities), f"from {result.mentions} mentions")
    table.add_row("in conflict", str(len(result.found)), "typed to more than one class")
    table.add_row(
        f"  {result.conflict_policy}d silently", str(len(result.found) - len(result.breaking)),
        "no incompatibility, so no question",
    )
    table.add_row(
        "  sent to review", str(len(result.breaking)),
        f"incompatible per {result.incompatibility_source}",
    )
    table.add_row("patterns", str(result.patterns), "a recurring pair is a TBox question")
    table.add_row("review items added", str(result.review_added), f"{result.review_known} known")
    console.print(table)
    for item in result.items[: limit or 10]:
        console.print(f"  [yellow]{item.kind}[/] {item.summary}")
    if result.found and not result.breaking:
        console.print(
            f"[dim]The {len(result.found)} disagreements are kept with their provenance and "
            f"flagged in the ABox. Notarizing is the silent default because it is the only "
            "policy that destroys no information.[/]"
        )


def functional_candidates(
    console: Console, result: iterate.FunctionalCandidates, *, limit: int | None = None
) -> None:
    table = Table("property", "individuals", "distribution", "excluded", "verdict")
    for support in result.supports[: limit or 15]:
        table.add_row(
            versioning.short_name(support.property_iri, result.labels),
            str(support.individuals), support.rendered_distribution,
            str(support.excluded), "refuted" if support.refuted else "candidate",
        )
    console.print(table)
    if not result.supports:
        console.print(
            "[yellow]no domain properties in the ABox[/]: this pipeline extracts types and "
            "provenance, not properties, so there is nothing here to be functional yet."
        )
        return
    console.print(
        f"{result.review_added} question(s) added to review, {result.review_known} already "
        "there. A 'refuted' verdict is settled — a counterexample is knowledge. A 'candidate' "
        "one is not: it is the absence of a counterexample, which is silence."
    )


def functional_declaration(console: Console, result: iterate.FunctionalDeclaration) -> None:
    if result.merges:
        console.print(
            f"[red]declaring {result.name} functional would merge {len(result.merges)} "
            "group(s) of individuals[/], and the reasoner would raise no inconsistency doing "
            "it:"
        )
        for group in result.merges[:10]:
            console.print(
                "  " + " = ".join(versioning.short_name(iri, result.labels) for iri in group)
            )
    else:
        console.print(
            f"[green]declaring {result.name} functional merges nothing[/] in the ABox today"
        )
    console.print(
        "[dim]Today's ABox is not the argument. The question is whether the property is "
        "functional in the domain; this only shows what the mistake would cost here.[/]"
    )
    if result.committed is None:
        return
    console.print(f"[green]committed[/] {result.committed.id}")
    if result.diff is not None:
        comparison(console, result.diff)


def regeneration(console: Console, result: iterate.Regeneration) -> None:
    if result.already:
        console.print(
            f"[yellow]{result.version_id} already regenerated[/] under "
            f"{result.rules_hash[:19]}; nothing to do. --force writes it again."
        )
        return
    outcome = result.result
    table = Table("what", "count", "note")
    table.add_row("individuals", str(outcome.n_individuals), f"from {result.mentions} mentions")
    table.add_row("typed", str(outcome.n_typed), "")
    table.add_row("untyped", str(outcome.n_untyped), "orphans, or typed in a rejected zone")
    table.add_row(
        "possible duplicates", str(outcome.n_unresolved),
        "out of the functional-property support count",
    )
    table.add_row("type conflicts", str(outcome.n_conflicts), "")
    table.add_row("excluded by a mark", str(outcome.n_excluded), "refuted or misextracted")
    table.add_row("quads", str(outcome.n_triples), "")
    console.print(table)
    console.print(
        f"version [bold]{result.version_id}[/] · rules {result.rules_hash[:19]}\n"
        f"[green]wrote[/] {result.target}"
    )


def validation(console: Console, result: iterate.Validation) -> None:
    """Una sola tabla: el perfil, los filtros que corrieron, y el que no corrió.

    Que HermiT diga «not run» y no «consistent» es la misma asimetría que la de ELK: no
    haberle preguntado no es una aprobación.
    """
    from .reasoning import REJECTED

    chain_result = result.chain
    if chain_result.missing_imports:
        console.print(
            "[yellow]importaciones sin resolver[/]: "
            + ", ".join(chain_result.missing_imports)
            + ". Todo lo que el razonador diga vale sobre menos axiomas de los que la "
            "ontología declara."
        )

    table = Table("check", "result", "detail")
    table.add_row(
        "PREP-NORMALIZE-PROFILE profile", result.profile.detected,
        f"target {result.target_profile} · "
        + ", ".join(
            f"{name} {count}" for name, count in sorted(result.profile.violations.items())
        ),
    )
    table.add_row(
        "ELK", chain_result.elk.verdict,
        f"EL coverage {chain_result.elk.coverage:.0%} · {chain_result.elk.note}",
    )
    if result.elk_only:
        table.add_row("HermiT", "not run", "ELK already rejected; its finding is real")
    else:
        table.add_row(
            "HermiT", "consistent" if chain_result.hermit.consistent else REJECTED,
            f"{len(chain_result.hermit.unsatisfiable)} unsatisfiable class(es)",
        )
    for verdict in (result.shacl, chain_result.ontoclean, chain_result.pitfalls):
        table.add_row(verdict.name, verdict.decision, verdict.note[:52])
    metrics = chain_result.structural
    table.add_row(
        "structural", REJECTED if metrics.rejected else "clean",
        f"depth {metrics.depth} · max branching {metrics.max_branching} · "
        f"{metrics.n_classes} classes · {len(metrics.findings)} finding(s)",
    )
    console.print(table)

    for verdict in (result.shacl, chain_result.ontoclean, chain_result.pitfalls):
        for finding in verdict.findings[:12]:
            colour = "red" if verdict.rejected else "yellow"
            console.print(f"[{colour}]{verdict.name}[/] {finding}")
    for finding in metrics.findings:
        console.print(f"[yellow]{finding.check}[/] {finding.subject} — {finding.detail}")
    if result.elk_only:
        for iri in chain_result.elk.unsatisfiable:
            console.print(f"[red]unsatisfiable[/] {iri}")
        return
    for iri, justifications in chain_result.hermit.justifications.items():
        console.print(f"[red]unsatisfiable[/] {iri}")
        for index, axioms in enumerate(justifications, start=1):
            console.print(f"  justification {index}:")
            for axiom in axioms:
                console.print(f"    {axiom}")


# ─────────────────────────────  EVAL  ─────────────────────────────


def stopping(console: Console, result: evaluate.Stopping, *, curve: bool = False) -> None:
    from .stopping import MET, NOT_MET

    assessment = result.assessment
    table = Table("criterion", "role", "state", "value", "note")
    for criterion in assessment.criteria:
        colour = {MET: "green", NOT_MET: "yellow"}.get(criterion.state, "dim")
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
            plot.add_row(
                str(point.index), point.document_id[:40], str(point.new), str(point.cumulative)
            )
        console.print(plot)


def question_run(console: Console, result: evaluate.QuestionRun, *, target: float) -> None:
    if result.note:
        console.print(f"[yellow]{result.note}[/]")
    elif result.inferred:
        console.print(
            f"version [bold]{result.version_id}[/] · {result.asserted} asserted triples, "
            f"{result.entailed} with entailments"
        )
    evaluation = result.evaluation
    table = Table("cq", "type", "answered", "rows")
    for question in result.questions:
        answered = (
            "error" if question.id in evaluation.errored
            else "yes" if question.id in evaluation.passed
            else "no"
        )
        table.add_row(
            question.id, question.cq_type, answered,
            str(evaluation.n_rows.get(question.id, "")),
        )
    console.print(table)
    console.print(
        f"pass rate [bold]{evaluation.pass_rate:.0%}[/] (target {target:.0%})"
    )
    for cq_id, error in evaluation.errored.items():
        console.print(f"[red]{cq_id}[/]: {error}")


def retention(console: Console, result: evaluate.Retention) -> None:
    if result.changed != result.requested:
        console.print("[yellow]some ids did not match an ingested document[/]")
    table = Table("document", "role")
    for identifier in result.process:
        table.add_row(identifier[:52], "process")
    for identifier in result.held_out:
        table.add_row(identifier[:52], "[bold]retention set[/]")
    console.print(table)


def annotation_tool(console: Console, result: evaluate.AnnotationTool) -> None:
    for identifier in result.missing:
        console.print(f"[red]{identifier}[/]: not ingested")
    for path in result.written:
        console.print(f"[green]wrote[/] {path}")
    console.print(
        f"{result.classes} seed classes offered ({result.glossed} with a gloss). "
        "Open the file in a browser, annotate, export the JSONL, then: "
        "onto-pipeline export-annotations <archivo.jsonl>"
    )


def brat_export(console: Console, result: evaluate.BratExport) -> None:
    table = Table("document", "mentions", "in seed", "relations", "status")
    for document in result.documents:
        if document.error:
            table.add_row(document.doc_id, "", "", "", f"[red]{document.error}[/]")
            continue
        table.add_row(
            document.doc_id, str(document.mentions), str(document.in_inventory),
            str(document.relations), "[green]exported[/]",
        )
    console.print(table)
    console.print(f"output in {result.out_dir}")


def review_list(console: Console, items: list[dict], counts: dict) -> None:
    table = Table("id", "kind", "status", "finding")
    for item in items:
        table.add_row(item["id"], item["kind"], item["status"], item["summary"][:60])
    console.print(table)
    console.print(
        " · ".join(f"{k}/{s}: {n}" for (k, s), n in sorted(counts.items())) or "nothing yet"
    )


def sweep(console: Console, result: evaluate.Sweep) -> None:
    for use_case in result.described:
        _describe_use_case(console, use_case)
    for label, distribution in result.distributions:
        _distribution(console, label, distribution)
    _sweep_tables(console, result.reports)
    console.print(f"[dim]{result.path}[/]")


def _describe_use_case(console: Console, use_case) -> None:
    mentions = sum(len(document.mentions) for document in use_case.documents)
    skipped = sum(document.skipped for document in use_case.documents)
    glossed = sum(1 for target in use_case.targets if target.gloss)
    table = Table("use_case", use_case.name)
    table.add_row("documents", str(len(use_case.documents)))
    table.add_row("gold mentions", f"{mentions} ({skipped} discontinuous, skipped)")
    table.add_row("inventory", f"{len(use_case.targets)} classes, {glossed} with a definition")
    if use_case.withheld:
        table.add_row(
            "withheld", f"{len(use_case.withheld)} classes — their mentions are orphans"
        )
    if use_case.excluded_classes:
        fate = ("dropped from the inventory" if use_case.dropped_excluded
                else "kept, --keep-excluded")
        table.add_row("never correct", f"{', '.join(use_case.excluded_classes[:5])} — {fate}")
    console.print(table)


def _distribution(console: Console, label: str, dist) -> None:
    """El número que dice si existe un umbral, antes de preguntar dónde ponerlo."""
    if not dist.correct or not dist.wrong:
        console.print(f"[dim]{label}: not enough of both classes to compare distributions[/]")
        return
    console.print(
        f"{label}: correct top-1 median [bold]{statistics.median(dist.correct):.3f}[/] "
        f"(n={len(dist.correct)}) · wrong top-1 median "
        f"[bold]{statistics.median(dist.wrong):.3f}[/] (n={len(dist.wrong)}) · "
        f"separation [bold]{dist.separation:.2f}[/]"
    )


def _sweep_tables(console: Console, reports: list) -> None:
    """Todos los umbrales de la mejor variante, y el mejor umbral de cada variante.

    Imprimir el producto cartesiano serían cien filas que nadie lee.
    """
    if not reports:
        return
    best = max(reports, key=lambda run: run.report.typing_f1)
    table = Table(*calibration.COLUMNS, title="threshold sweep · best variant")
    for run in reports:
        if (run.match_against, run.use_cross_encoder) == (
            best.match_against, best.use_cross_encoder
        ):
            table.add_row(*run.row, style="bold" if run is best else None)
    console.print(table)

    variants = Table(*calibration.COLUMNS, title="each variant at its own best threshold")
    seen: dict[Any, Any] = {}
    for run in reports:
        key = (run.match_against, run.use_cross_encoder)
        if key not in seen or run.report.typing_f1 > seen[key].report.typing_f1:
            seen[key] = run
    for run in sorted(seen.values(), key=lambda item: -item.report.typing_f1):
        variants.add_row(*run.row)
    console.print(variants)


def tuning(console: Console, result: evaluate.Tuning) -> None:
    console.print(
        f"[bold]{result.name}[/]: {result.train_documents} documentos para entrenar, "
        f"{result.eval_documents} para evaluar · {result.targets} clases"
    )
    console.print(f"ejemplos: {result.examples} · {result.train_mentions} menciones")
    trained = result.trained
    console.print(
        f"método [bold]{trained.method}[/] · {trained.trainable_params:,} de "
        f"{trained.total_params:,} parámetros entrenados ({trained.trainable_fraction:.2%})"
    )
    if result.eval_on:
        console.print(
            f"[yellow]evaluando en {result.eval_on}[/]: mide si un modelo ajustado acá sirve "
            "en otro dominio"
        )
    outcome = result.result
    table = Table("qué", "@1", "@5")
    table.add_row("bi-encoder solo", f"{outcome.base_at_1:.1%}", f"{outcome.base_at_5:.1%}")
    table.add_row(
        "+ cross-encoder ajustado",
        f"[bold]{outcome.tuned_at_1:.1%}[/]", f"{outcome.tuned_at_5:.1%}",
    )
    table.add_row(f"techo (@{result.top_k} del bi-encoder)", f"{outcome.ceiling:.1%}", "")
    console.print(table)
    console.print(
        f"{outcome.gain:+.1%} en el primer puesto sobre {outcome.n} menciones de documentos no "
        f"vistos · captura el {outcome.headroom_taken:.0%} del margen disponible"
    )
    if result.saved is not None:
        console.print(f"[green]guardado[/] {result.saved}")
        console.print(
            "[dim]apuntá `matching.cross_encoder` a ese directorio y poné "
            "`use_cross_encoder: true` para usarlo[/]"
        )


# ─────────────────────────────  DELIVERABLES  ─────────────────────────────


def comparison(console: Console, result: deliver.Comparison, *, limit: int = 10) -> None:
    if result.root:
        console.print(
            f"[yellow]{result.target} is a root version[/]: no parent to diff against. "
            "Pass --against to compare it with any other version."
        )
        return
    table = Table("change", "count", title=f"{result.baseline} → {result.target}")
    table.add_row("axioms added", str(len(result.diff.added)))
    table.add_row("axioms removed", str(len(result.diff.removed)))
    table.add_row("annotations changed", str(len(result.diff.labels_changed)))
    console.print(table)
    if result.diff.empty:
        console.print("[yellow]no logical or annotation change[/]")
    else:
        for label, lines, colour in (
            ("+", result.diff.added, "green"), ("-", result.diff.removed, "red"),
            ("~", result.diff.labels_changed, "yellow"),
        ):
            shown = lines if limit == 0 else lines[:limit]
            for line in shown:
                console.print(f"[{colour}]{label}[/] {readable(line, result.labels)}")
            if len(lines) > len(shown):
                console.print(f"[dim]  … {len(lines) - len(shown)} more[/]")
    if result.path is not None:
        console.print(f"[green]wrote[/] {result.path}")


def versions(console: Console, rows: list[dict]) -> None:
    table = Table("version", "parent", "iteration", "branch", "state", "note")
    for row in rows:
        table.add_row(
            row["id"], row["parent_id"] or "-", str(row["iteration"]),
            row["branch_id"] or "-", row["state_hash"][7:19], row["note"] or "",
        )
    console.print(table)


def telemetry(console: Console, result: deliver.Telemetry) -> None:
    stages = Table("stage", "units", "done", "failed", "in tokens", "out tokens")
    for row in result.stages:
        stages.add_row(
            row["stage"],
            *(str(row[key] or 0) for key in ("units", "done", "failed", "in_tokens",
                                             "out_tokens")),
        )
    console.print(stages)

    classes = Table("page class", "pages", "reason")
    for row in result.page_classes:
        classes.add_row(row["class"], str(row["n"]), row["reason"] or "")
    console.print(classes)

    if not result.failures:
        return
    # Lo que dijo cada unidad que falló, con su etapa. Es lo que contesta «¿por qué se abortó?»,
    # y estaba guardado en `work_units.error` sin que nada lo mostrara.
    failures = Table("etapa", "unidad", "error")
    for row in result.failures:
        failures.add_row(row["stage"], row["key"][:12], (row["error"] or "")[:80])
    console.print(failures)


def chunks(console: Console, found: list) -> None:
    table = Table("chunk", "pages", "blocks", "chars", "context")
    for chunk in found:
        table.add_row(
            str(chunk.ordinal), ",".join(str(page) for page in chunk.pages),
            str(len(chunk.block_ids)), str(len(chunk.text)), str(len(chunk.context_block_ids)),
        )
    console.print(table)


def delivery(console: Console, result: deliver.Delivery) -> None:
    """La ontología terminada: qué salió, de dónde, y qué le hizo cada iteración."""
    history = Table("version", "note", "+axiomas", "-axiomas", "~anotaciones")
    for step in result.history:
        history.add_row(
            step.version_id, (step.note or "")[:46],
            str(step.added) if step.parent_id else f"{step.added} (raíz)",
            str(step.removed) if step.parent_id else "-",
            str(step.annotations) if step.parent_id else "-",
        )
    console.print(history)

    table = Table("what", "count")
    table.add_row("versions applied", f"{len(result.history)} ({result.iterations} iterations)")
    table.add_row("TBox triples", str(result.tbox_triples))
    table.add_row("ABox quads", str(result.abox_quads) if result.abox_included else "not included")
    table.add_row("classes", str(result.classes))
    table.add_row("  in the root version", str(result.inventory_classes))
    table.add_row("  minted by the pipeline", str(result.minted_classes))
    console.print(table)
    for warning in result.warnings:
        console.print(f"[yellow]{warning}[/]")
    if result.abox_regenerated:
        console.print("[dim]the ABox was regenerated from the mention layer before export[/]")
    console.print(f"[green]wrote[/] {result.path}")
    console.print(f"[dim]provenance: {result.manifest_path}[/]")


def session_list(console: Console, found: list, *, current: str = "") -> None:
    """Las sesiones que hay. La actual va marcada: es la que usan los comandos sin `--session`."""
    if not found:
        console.print(
            "[yellow]todavía no hay ninguna sesión[/] · "
            "`onto-pipeline session new --use-case <nombre>` crea una"
        )
        return
    table = Table("", "sesión", "caso de uso", "fase", "nombre", "última actividad")
    for item in found:
        table.add_row(
            "[green]▸[/]" if item.id == current else "",
            item.id, item.use_case, item.phase, item.name, item.updated_at,
        )
    console.print(table)


def session_detail(console: Console, session, events: list, *, limit: int | None = None) -> None:
    """Una sesión y su historial. El historial es append-only: se lee, no se corrige."""
    table = Table("qué", "valor", title=session.label)
    table.add_row("id", session.id)
    table.add_row("caso de uso", session.use_case)
    table.add_row("fase", session.phase)
    table.add_row("creada", session.created_at)
    table.add_row("última actividad", session.updated_at)
    if session.note:
        table.add_row("nota", session.note)
    console.print(table)

    history = Table("cuándo", "qué", "detalle")
    for event in events[: limit or 20]:
        history.add_row(event.at, event.kind, event.summary)
    console.print(history)
    if len(events) > (limit or 20):
        console.print(f"[dim]… {len(events) - (limit or 20)} eventos más[/]")


def stage_aborted(console: Console, aborted) -> None:
    """Por qué se cortó la etapa, unidad por unidad.

    La tasa es la consecuencia; lo que hace falta para arreglarlo es lo que dijo cada unidad que
    falló. Antes el mensaje decía «100% de 1 unidad» y nada más, y la causa quedaba en
    `work_units.error` sin que nada la mostrara.
    """
    console.print(f"[red]{aborted}[/]")
    if not aborted.failures:
        return
    table = Table("unidad", "error")
    for label, error in list(aborted.failures.items())[:10]:
        table.add_row(label[:36], error[:90])
    console.print(table)
    if len(aborted.failures) > 10:
        console.print(f"[dim]… {len(aborted.failures) - 10} más · `status` las muestra todas[/]")
    console.print(
        "[dim]`onto-pipeline status` lista las unidades fallidas de todas las etapas, con su "
        "error completo.[/]"
    )


def plan(console: Console, survey, version_id: str) -> None:
    """Qué corresponde correr, y qué está esperando a una persona (`orchestration`)."""
    from . import orchestration

    colours = {
        orchestration.DONE: "green", orchestration.READY: "bold",
        orchestration.WAITING: "yellow", orchestration.BLOCKED: "dim",
    }
    table = Table("stage", "state", "detail", title=f"against {version_id}")
    for step in survey.steps:
        table.add_row(step.name, f"[{colours[step.state]}]{step.state}[/]", step.detail)
    console.print(table)


def questions(console: Console, found: list, *, status: str, limit: int | None = None) -> None:
    table = Table("id", "type", "lang", "question", "cited")
    for question in found[: limit or 40]:
        citation = question.citation or {}
        table.add_row(
            question.id, question.cq_type, question.language, question.question[:60],
            f"{citation.get('document_id', '—')[:18]} p.{citation.get('page', '?')}",
        )
    console.print(table)
    console.print(f"{len(found)} with status {status}")


__all__ = [name for name in dir() if not name.startswith("_")]
