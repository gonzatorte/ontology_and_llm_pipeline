"""Structure-aware chunking (spec 4.2). The chunk is B1's unit of extraction (spec 6.1).

Two rules the spec states outright:

- Never chunk across a table. A specification table is a set of triples — the densest source
  of typed relations in the corpus — and splitting it destroys the row/column pairing that
  makes it one.
- The table, its caption and the paragraph that references it are one unit.

The referencing paragraph is attached as context rather than moved: it belongs to its own
chunk, mentions are anchored on blocks, and copying its text into two chunks would extract
the same mention twice.

Chunks are derived, never stored: the spec keeps only what costs money or real time (8.3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .parse import CAPTION, FIGURE, PARAGRAPH, TABLE, UNPARSED

_TABLE_REFERENCE_RE = re.compile(r"\b(?:table|tabla|cuadro)\.?\s*(\d+)", re.IGNORECASE)
_CAPTION_NUMBER_RE = re.compile(
    r"^\s*(?:fig(?:ure|ura)?|tab(?:le|la)|cuadro|gr[áa]fico|chart)\.?\s*(\d+)", re.IGNORECASE
)


@dataclass
class Chunk:
    document_id: str
    ordinal: int
    block_ids: list[str] = field(default_factory=list)
    context_block_ids: list[str] = field(default_factory=list)
    text: str = ""
    context_text: str = ""
    pages: list[int] = field(default_factory=list)
    # (offset within this chunk, block id, that block's start in the Markdown). A chunk is not
    # a contiguous slice of the Markdown — boilerplate and unparsed blocks are skipped — so an
    # offset can only be translated through the block it falls in.
    layout: list[tuple[int, str, int]] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.document_id}:c{self.ordinal}"

    def absolute_offset(self, chunk_offset: int) -> tuple[str, int]:
        """Chunk offset -> (block id, offset into the document Markdown)."""
        for start, block_id, block_start in reversed(self.layout):
            if chunk_offset >= start:
                return block_id, block_start + (chunk_offset - start)
        raise ValueError(f"offset {chunk_offset} is before the start of chunk {self.id}")


def chunk_document(blocks: list, target_chars: int, max_chars: int) -> list[Chunk]:
    """`blocks` are the parsed blocks of one document in reading order."""
    usable = [
        block for block in blocks
        if not block.is_boilerplate and block.block_type != UNPARSED and block.text.strip()
    ]
    if not usable:
        return []

    groups = _atomic_groups(usable)
    references = _reference_index(usable)

    chunks: list[Chunk] = []
    current: list = []
    for group in groups:
        group_length = sum(len(block.text) for block in group)
        current_length = sum(len(block.text) for block in current)
        if current and current_length + group_length > max_chars:
            chunks.append(_build(current, len(chunks), references))
            current = []
        current.extend(group)
        if sum(len(block.text) for block in current) >= target_chars:
            chunks.append(_build(current, len(chunks), references))
            current = []
    if current:
        chunks.append(_build(current, len(chunks), references))
    return chunks


def _atomic_groups(blocks: list) -> list[list]:
    """A table or figure travels with its caption; neither may be split from the other."""
    groups: list[list] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        group = [block]
        if block.block_type == CAPTION and index + 1 < len(blocks):
            if blocks[index + 1].block_type in (TABLE, FIGURE):
                group.append(blocks[index + 1])
                index += 1
        elif block.block_type in (TABLE, FIGURE) and index + 1 < len(blocks):
            if blocks[index + 1].block_type == CAPTION:
                group.append(blocks[index + 1])
                index += 1
        groups.append(group)
        index += 1
    return groups


def _reference_index(blocks: list) -> dict[str, list]:
    """Maps a caption's block id to the paragraphs citing its number."""
    numbered: dict[str, str] = {}
    for block in blocks:
        if block.block_type != CAPTION:
            continue
        match = _CAPTION_NUMBER_RE.match(block.text)
        if match:
            numbered[block.id] = match.group(1)

    index: dict[str, list] = {block_id: [] for block_id in numbered}
    for block in blocks:
        if block.block_type != PARAGRAPH:
            continue
        cited = {match.group(1) for match in _TABLE_REFERENCE_RE.finditer(block.text)}
        for block_id, number in numbered.items():
            if number in cited:
                index[block_id].append(block)
    return index


def _build(blocks: list, ordinal: int, references: dict[str, list]) -> Chunk:
    own = {block.id for block in blocks}
    context = [
        paragraph
        for block in blocks
        for paragraph in references.get(block.id, [])
        if paragraph.id not in own
    ]
    layout, cursor = [], 0
    for block in blocks:
        layout.append((cursor, block.id, block.span_start))
        cursor += len(block.render()) + 2      # the "\n\n" joiner
    return Chunk(
        document_id=blocks[0].document_id,
        ordinal=ordinal,
        block_ids=[block.id for block in blocks],
        context_block_ids=[paragraph.id for paragraph in context],
        text="\n\n".join(block.render() for block in blocks),
        context_text="\n\n".join(paragraph.text for paragraph in context),
        pages=sorted({block.page for block in blocks}),
        layout=layout,
    )
