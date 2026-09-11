"""Retention set: annotation format, BRAT export, orphan metrics (EVAL-PIPELINE).

The exporter is DELIVERABLES-PENDING-BRAT-EXPORTER; the format is EVAL-ANNOTATION-FORMAT.

The format is the project's own JSONL for one reason: the annotation carries a field no
standard contemplates — `in_inventory` — and it is exactly the field that defines the false-orphan
metric. `gold_class: null` is a third state, a valid mention with no assignable class, which
is a different signal from `misextracted`.

The false orphan is the metric that matters most and the one no standard suite reports.
Aggregated with everything else it disappears:

    false orphan   the class existed in the seed and the matcher missed it — an error
    genuine orphan the seed does not cover the concept — normal, and it feeds ITER-INDUCE

The exporter is written from the start even though nothing uses it yet: it keeps the door open
to BRAT/INCEpTION if the retention set outgrows ten documents. The importer is deliberately
absent (BUILD-OUT-OF-SCOPE) — standard formats anchor offsets on plain text while these are offsets
into the parser's Markdown, so a parser version change shifts them. `markdown_hash` travels
with the export so a reimport can be refused rather than silently misaligned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

FALSE_ORPHAN = "false_orphan"
GENUINE_ORPHAN = "genuine_orphan"
MISTYPED = "mistyped"
CORRECT = "correct"


class OffsetMismatch(ValueError):
    """The Markdown the annotation was made against is not the Markdown on disk."""


@dataclass
class Mention:
    id: str
    page: int
    span: tuple[int, int]
    text: str
    gold_class: str | None = None
    in_inventory: bool = False
    entity_id: str | None = None


@dataclass
class Relation:
    subject: str
    predicate: str
    object: str
    evidence_page: int | None = None


@dataclass
class AnnotatedDocument:
    doc_id: str
    markdown_hash: str
    mentions: list[Mention] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)


@dataclass
class OrphanReport:
    """Split in two, because the aggregate says nothing (ITER-BRIDGE)."""

    false_orphans: list[str] = field(default_factory=list)
    genuine_orphans: list[str] = field(default_factory=list)
    mistyped: list[str] = field(default_factory=list)
    correct: list[str] = field(default_factory=list)

    @property
    def false_orphan_rate(self) -> float:
        """Over the mentions whose class the seed did in fact cover — the only ones that
        could have been missed."""
        recoverable = len(self.false_orphans) + len(self.mistyped) + len(self.correct)
        return len(self.false_orphans) / recoverable if recoverable else 0.0

    @property
    def typing_f1(self) -> float:
        predicted = len(self.correct) + len(self.mistyped)
        gold = len(self.correct) + len(self.mistyped) + len(self.false_orphans)
        if not predicted or not gold or not self.correct:
            return 0.0
        precision = len(self.correct) / predicted
        recall = len(self.correct) / gold
        return 2 * precision * recall / (precision + recall)


def read_jsonl(path: Path) -> list[AnnotatedDocument]:
    documents = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        documents.append(_document(json.loads(line)))
    return documents


def _document(entry: dict) -> AnnotatedDocument:
    return AnnotatedDocument(
        doc_id=entry["doc_id"],
        markdown_hash=entry["markdown_hash"],
        mentions=[
            Mention(
                id=mention["id"],
                page=mention["page"],
                span=(mention["span"][0], mention["span"][1]),
                text=mention["text"],
                gold_class=mention.get("gold_class"),
                in_inventory=bool(mention.get("in_inventory", False)),
                entity_id=mention.get("entity_id"),
            )
            for mention in entry.get("mentions", [])
        ],
        relations=[
            Relation(
                subject=relation["subject"],
                predicate=relation["predicate"],
                object=relation["object"],
                evidence_page=relation.get("evidence_page"),
            )
            for relation in entry.get("relations", [])
        ],
    )


def validate(document: AnnotatedDocument, markdown: str, markdown_hash: str) -> None:
    """RISKS-RETENTION-OFFSETS: if the parser changed, the offsets have shifted and the
    set has to be re-anchored,
    not reimported blind."""
    if document.markdown_hash != markdown_hash:
        raise OffsetMismatch(
            f"{document.doc_id}: annotated against {document.markdown_hash}, "
            f"Markdown on disk is {markdown_hash}"
        )
    for mention in document.mentions:
        start, end = mention.span
        if markdown[start:end] != mention.text:
            raise OffsetMismatch(
                f"{document.doc_id}/{mention.id}: span {mention.span} holds "
                f"{markdown[start:end]!r}, annotation says {mention.text!r}"
            )


def score(document: AnnotatedDocument, predicted: dict[str, str | None]) -> OrphanReport:
    """`predicted` maps mention id to the class the matcher assigned, or None for an orphan."""
    report = OrphanReport()
    for mention in document.mentions:
        if mention.gold_class is None:
            continue  # a valid mention with no assignable class: not the matcher's failure
        assigned = predicted.get(mention.id)
        if assigned is None:
            target = report.false_orphans if mention.in_inventory else report.genuine_orphans
            target.append(mention.id)
        elif assigned == mention.gold_class:
            report.correct.append(mention.id)
        else:
            report.mistyped.append(mention.id)
    return report


def export_brat(document: AnnotatedDocument, markdown: str, out_dir: Path) -> list[Path]:
    """`.txt` + `.ann`, plus the `markdown_hash` the offsets belong to.

    `in_inventory` survives only as an ad-hoc attribute — the single loss against the standard
    format, and the reason the project's own JSONL stays the source of truth.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    text_path = out_dir / f"{document.doc_id}.txt"
    ann_path = out_dir / f"{document.doc_id}.ann"
    hash_path = out_dir / f"{document.doc_id}.markdown_hash"

    lines: list[str] = []
    term_of: dict[str, str] = {}
    for index, mention in enumerate(document.mentions, start=1):
        term = f"T{index}"
        term_of[mention.id] = term
        kind = mention.gold_class or "Unassigned"
        start, end = mention.span
        lines.append(f"{term}\t{kind} {start} {end}\t{mention.text}")

    attribute = 1
    for mention in document.mentions:
        if mention.in_inventory:
            lines.append(f"A{attribute}\tInInventory {term_of[mention.id]}")
            attribute += 1

    # A shared entity_id is a coreference chain; BRAT writes those as equivalence groups.
    chains: dict[str, list[str]] = {}
    for mention in document.mentions:
        if mention.entity_id:
            chains.setdefault(mention.entity_id, []).append(term_of[mention.id])

    # Relations hold between entities, while BRAT relates text-bound annotations. Each entity
    # is represented by its first mention.
    representative = {entity: members[0] for entity, members in chains.items()}
    for index, relation in enumerate(document.relations, start=1):
        subject = representative.get(relation.subject) or term_of.get(relation.subject)
        obj = representative.get(relation.object) or term_of.get(relation.object)
        if subject and obj:
            lines.append(f"R{index}\t{relation.predicate} Arg1:{subject} Arg2:{obj}")

    for members in chains.values():
        if len(members) > 1:
            lines.append("*\tCoreference " + " ".join(members))

    text_path.write_text(markdown, encoding="utf-8")
    ann_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hash_path.write_text(document.markdown_hash + "\n", encoding="utf-8")
    return [text_path, ann_path, hash_path]
