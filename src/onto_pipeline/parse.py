"""A2 — parsing and ingestion (spec 4.2).

Priorities, in the spec's order: table fidelity, per-element provenance (page, bbox, block
type), structure-aware chunking. The Markdown is assembled here rather than taken from a
library so that every block's span offsets into it are exact — mention offsets (spec 8.1) are
anchored on them and `markdown_hash` has to validate them on reimport.

Only the born-digital route is implemented. Pages classified `scan` or `uncertain` are the
VLM route and are recorded as unparsed rather than silently emitted as empty.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import boilerplate as bp
from . import language as lang
from .classify import BORN_DIGITAL, PageClass, classify_document
from .config import Config

PARAGRAPH = "paragraph"
HEADING = "heading"
TABLE = "table"
CAPTION = "caption"
FIGURE = "figure"
UNPARSED = "unparsed"

_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure|ura)?|tab(?:le|la)|cuadro|gr[áa]fico|chart)\.?\s*\d+", re.IGNORECASE
)
_TABLE_CAPTION_RE = re.compile(r"^\s*(tab(?:le|la)|cuadro)\.?\s*\d+", re.IGNORECASE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_WORD = r"[^\W\d_]+"
_HYPHENATED_RE = re.compile(rf"{_WORD}(?:-{_WORD})+", re.UNICODE)
_TAIL_WORD_RE = re.compile(rf"{_WORD}$", re.UNICODE)
_HEAD_WORD_RE = re.compile(rf"^{_WORD}", re.UNICODE)
_HEADING_MAX_CHARS = 120


@dataclass
class Block:
    document_id: str
    page: int
    ordinal: int
    bbox: tuple[float, float, float, float]
    block_type: str
    text: str
    span_start: int | None = None
    span_end: int | None = None
    language: str | None = None
    language_source: str | None = None
    is_boilerplate: bool = False
    asset_path: str | None = None

    @property
    def id(self) -> str:
        return f"{self.document_id}:p{self.page}:b{self.ordinal}"

    def render(self) -> str:
        if self.block_type == HEADING:
            return f"## {self.text}"
        if self.block_type == FIGURE:
            return f"![figure p{self.page}]({self.asset_path})"
        if self.block_type == UNPARSED:
            return f"<!-- page {self.page}: {self.text} -->"
        return self.text


@dataclass
class ParsedDocument:
    document_id: str
    path: Path
    content_hash: str
    n_pages: int
    parser_used: str
    parser_version: str
    markdown: str
    markdown_hash: str
    blocks: list[Block] = field(default_factory=list)
    page_classes: list[PageClass] = field(default_factory=list)

    @property
    def unparsed_pages(self) -> list[int]:
        return sorted({b.page for b in self.blocks if b.block_type == UNPARSED})


def is_table_caption(text: str) -> bool:
    """A page carrying a table caption but no table block means the table came out as prose.

    Borderless tables are the common case in this corpus and `find_tables` only sees ruled
    ones; the `text` strategy grids the whole page instead. Rather than guess, the parse
    reports the gap so those pages can be routed to the VLM parser (spec 4.2 puts table
    fidelity first).
    """
    return bool(_TABLE_CAPTION_RE.match(text))


def document_id(path: Path, corpus_root: Path) -> str:
    try:
        relative = path.relative_to(corpus_root)
    except ValueError:
        relative = path
    slug = _SLUG_RE.sub("_", path.stem.lower()).strip("_")[:60]
    digest = hashlib.sha1(str(relative).encode("utf-8")).hexdigest()[:6]
    return f"{slug}_{digest}"


# Formatos que ya vienen en texto: los corpus anotados que sirven de instrumento se publican
# así, no en PDF.
TEXT_SUFFIXES = frozenset({".txt", ".text", ".md"})

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")


def paragraphs(text: str) -> list[tuple[int, int]]:
    """Spans de cada párrafo, como offsets en `text`. Nunca reescribe un carácter.

    Devuelve posiciones y no cadenas a propósito: lo que hace útil a un corpus anotado es que
    sus anotaciones traen offsets sobre este mismo archivo, y cualquier normalización —recortar
    espacios, re-flowear líneas, unir guiones— los desalinea en silencio.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        if (chunk := text[cursor:match.start()]).strip():
            spans.append((cursor + len(chunk) - len(chunk.lstrip()),
                          cursor + len(chunk.rstrip())))
        cursor = match.end()
    if (chunk := text[cursor:]).strip():
        spans.append((cursor + len(chunk) - len(chunk.lstrip()),
                      cursor + len(chunk.rstrip())))
    return spans


def parse_text(path: Path, config: Config, *, doc_id: str | None = None) -> ParsedDocument:
    """Un documento que ya es texto. El Markdown que se entrega **es el archivo, literal**.

    Es la única diferencia que importa con la ruta de PDF, y es deliberada. En un corpus anotado
    las anotaciones gold indexan caracteres de este archivo; si la etapa reflowea, recorta o
    normaliza algo, las mediciones posteriores comparan contra offsets corridos y el error no
    se manifiesta como error, sino como una tasa de acierto peor sin explicación. Por eso acá no
    hay des-hyphenación, ni detección de encabezados —`Block.render()` le agregaría `## ` y
    cambiaría el texto—, ni supresión de boilerplate: eso se detecta por repetición entre
    páginas y acá no hay páginas.

    Los bloques son vistas sobre ese texto: `markdown[b.span_start:b.span_end] == b.text`, que
    es el invariante que un test fija.
    """
    doc_id = doc_id or document_id(path, config.paths.corpus_root)
    raw = path.read_bytes()
    markdown = raw.decode("utf-8")

    blocks = [
        Block(
            document_id=doc_id,
            page=1,          # el formato no tiene páginas; una sola, y el ordinal desempata
            ordinal=ordinal,
            bbox=(0.0, 0.0, 0.0, 0.0),
            block_type=PARAGRAPH,
            text=markdown[start:end],
            span_start=start,
            span_end=end,
        )
        for ordinal, (start, end) in enumerate(paragraphs(markdown))
    ]
    _assign_languages(blocks, None)

    return ParsedDocument(
        document_id=doc_id,
        path=path,
        content_hash=hashlib.sha256(raw).hexdigest(),
        n_pages=0,           # no las tiene, y ponerle 1 diría que sí
        parser_used="text",
        parser_version="1",
        markdown=markdown,
        markdown_hash="sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        blocks=blocks,
        page_classes=[],
    )


def parse_document(path: Path, config: Config, *, doc_id: str | None = None) -> ParsedDocument:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return parse_text(path, config, doc_id=doc_id)
    doc_id = doc_id or document_id(path, config.paths.corpus_root)
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    assets_dir = config.paths.work_dir / "assets" / doc_id
    asset_root = config.paths.work_dir

    with pymupdf.open(path) as doc:
        page_classes = classify_document(doc, config.classification)
        declared = lang.normalize_declared(_declared_language(doc))
        hyphenated = _hyphenated_vocabulary(doc)
        blocks: list[Block] = []
        for page, page_class in zip(doc, page_classes, strict=True):
            if page_class.label == BORN_DIGITAL:
                blocks.extend(
                    _page_blocks(
                        page, page_class.page, doc_id, assets_dir, asset_root, hyphenated
                    )
                )
            else:
                blocks.append(
                    Block(
                        document_id=doc_id,
                        page=page_class.page,
                        ordinal=0,
                        bbox=tuple(page.rect),
                        block_type=UNPARSED,
                        text=f"classified {page_class.label}, needs the VLM route",
                    )
                )
        n_pages = doc.page_count

    _assign_languages(blocks, declared)
    _flag_boilerplate(blocks, n_pages, config)
    markdown = _assemble_markdown(blocks)

    return ParsedDocument(
        document_id=doc_id,
        path=path,
        content_hash=content_hash,
        n_pages=n_pages,
        parser_used=config.parser.born_digital,
        parser_version=".".join(str(part) for part in pymupdf.version[:2]),
        markdown=markdown,
        markdown_hash="sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        blocks=blocks,
        page_classes=page_classes,
    )


def _declared_language(doc: pymupdf.Document) -> str | None:
    try:
        kind, value = doc.xref_get_key(-1, "Lang")
    except Exception:  # noqa: BLE001 - absent catalog key is the normal case
        return None
    return value if kind == "string" else None


def _page_blocks(
    page: pymupdf.Page, number: int, doc_id: str, assets_dir: Path, asset_root: Path,
    hyphenated: frozenset[str],
) -> list[Block]:
    tables = _table_blocks(page, number, doc_id)
    table_rects = [pymupdf.Rect(block.bbox) for block in tables]
    figures = _figure_blocks(page, number, doc_id, assets_dir, asset_root)
    text = _text_blocks(page, number, doc_id, table_rects, hyphenated)

    ordered = _reading_order(page, text + tables + figures)
    for ordinal, block in enumerate(ordered):
        block.ordinal = ordinal
    return ordered


def _table_blocks(page: pymupdf.Page, number: int, doc_id: str) -> list[Block]:
    blocks = []
    for table in page.find_tables().tables:
        markdown = table.to_markdown().strip()
        if not markdown:
            continue
        blocks.append(
            Block(
                document_id=doc_id,
                page=number,
                ordinal=0,
                bbox=tuple(table.bbox),
                block_type=TABLE,
                text=markdown,
            )
        )
    return blocks


def _figure_blocks(
    page: pymupdf.Page, number: int, doc_id: str, assets_dir: Path, asset_root: Path
) -> list[Block]:
    """No parser reads figures. They come out as a crop plus a placeholder (spec 4.2); the
    VLM captioning second pass consumes the crops."""
    blocks = []
    for index, image in enumerate(page.get_image_info()):
        rect = pymupdf.Rect(image["bbox"]) & page.rect
        if rect.is_empty or rect.get_area() < 2500:
            continue
        assets_dir.mkdir(parents=True, exist_ok=True)
        asset = assets_dir / f"p{number}_f{index}.png"
        page.get_pixmap(clip=rect, dpi=150).save(asset)
        blocks.append(
            Block(
                document_id=doc_id,
                page=number,
                ordinal=0,
                bbox=tuple(rect),
                block_type=FIGURE,
                text="",
                # Relative to the work directory: an absolute path would put this machine's
                # filesystem into the Markdown, which is exported and annotated elsewhere.
                asset_path=str(asset.relative_to(asset_root)),
            )
        )
    return blocks


def _text_blocks(
    page: pymupdf.Page, number: int, doc_id: str, table_rects: list[pymupdf.Rect],
    hyphenated: frozenset[str],
) -> list[Block]:
    raw = page.get_text("dict")["blocks"]
    sizes = [
        span["size"]
        for block in raw
        if block["type"] == 0
        for line in block["lines"]
        for span in line["spans"]
    ]
    body_size = _median(sizes)

    blocks = []
    for block in raw:
        if block["type"] != 0:
            continue
        rect = pymupdf.Rect(block["bbox"])
        if any(rect.intersects(table) and _mostly_inside(rect, table) for table in table_rects):
            continue
        text = _block_text(block, hyphenated)
        if not text:
            continue
        blocks.append(
            Block(
                document_id=doc_id,
                page=number,
                ordinal=0,
                bbox=tuple(rect),
                block_type=_text_block_type(block, text, body_size),
                text=text,
            )
        )
    return blocks


def _block_text(block: dict, hyphenated: frozenset[str]) -> str:
    lines = []
    for line in block["lines"]:
        lines.append("".join(span["text"] for span in line["spans"]).strip())
    joined = ""
    for line in lines:
        if not line:
            continue
        if joined.endswith("-"):
            joined = _join_hyphenated(joined, line, hyphenated)
        elif joined:
            joined += " " + line
        else:
            joined = line
    return joined.strip()


def _join_hyphenated(joined: str, line: str, hyphenated: frozenset[str]) -> str:
    """A hyphen at a line break is usually syllabic ("Eco-nomic") but sometimes lexical
    ("university-based"), and dropping it silently corrupts exactly the compound terms the
    matcher works on. The document itself decides: if the compound appears hyphenated within
    a line elsewhere, the hyphen is real."""
    left = _TAIL_WORD_RE.search(joined[:-1])
    right = _HEAD_WORD_RE.match(line)
    if left and right and f"{left.group()}-{right.group()}".lower() in hyphenated:
        return joined + line
    return joined[:-1] + line


def _hyphenated_vocabulary(doc: pymupdf.Document) -> frozenset[str]:
    """Compounds seen hyphenated inside a line — where no line break could have produced
    the hyphen."""
    vocabulary: set[str] = set()
    for page in doc:
        for line in page.get_text().splitlines():
            stripped = line.rstrip()
            if stripped.endswith("-"):
                stripped = stripped[:-1]
            vocabulary.update(match.group().lower() for match in _HYPHENATED_RE.finditer(stripped))
    return frozenset(vocabulary)


def _text_block_type(block: dict, text: str, body_size: float) -> str:
    if _CAPTION_RE.match(text):
        return CAPTION
    max_size = max(
        span["size"] for line in block["lines"] for span in line["spans"]
    )
    if max_size >= body_size * 1.15 and len(text) <= _HEADING_MAX_CHARS:
        return HEADING
    return PARAGRAPH


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _mostly_inside(rect: pymupdf.Rect, container: pymupdf.Rect) -> bool:
    overlap = rect & container
    return not overlap.is_empty and overlap.get_area() >= 0.6 * rect.get_area()


def _reading_order(page: pymupdf.Page, blocks: list[Block]) -> list[Block]:
    """XY-cut lite: full-width blocks split the page into bands; inside a band, the left
    column precedes the right one. Straight y-sorting interleaves the columns of a
    two-column paper, which is most of this corpus."""
    if not blocks:
        return []

    centre = (page.rect.x0 + page.rect.x1) / 2
    margin = page.rect.width * 0.05
    ordered = sorted(blocks, key=lambda block: (block.bbox[1], block.bbox[0]))

    def spans_centre(block: Block) -> bool:
        return block.bbox[0] < centre - margin and block.bbox[2] > centre + margin

    result: list[Block] = []
    band: list[Block] = []
    for block in ordered:
        if spans_centre(block):
            result.extend(_order_band(band, centre))
            band = []
            result.append(block)
        else:
            band.append(block)
    result.extend(_order_band(band, centre))
    return result


def _top_left(block: Block) -> tuple[float, float]:
    return block.bbox[1], block.bbox[0]


def _order_band(band: list[Block], centre: float) -> list[Block]:
    if not band:
        return []
    left = [block for block in band if (block.bbox[0] + block.bbox[2]) / 2 <= centre]
    right = [block for block in band if (block.bbox[0] + block.bbox[2]) / 2 > centre]
    if not left or not right:
        return sorted(band, key=_top_left)
    return sorted(left, key=_top_left) + sorted(right, key=_top_left)


def _assign_languages(blocks: list[Block], declared: str | None) -> None:
    for block in blocks:
        block.language = lang.detect(block.text)
        block.language_source = lang.INFERRED

    counts: dict[str, int] = {}
    for block in blocks:
        if block.language:
            counts[block.language] = counts.get(block.language, 0) + 1
    majority = max(counts, key=lambda key: counts[key]) if counts else None

    for block in blocks:
        if block.language:
            continue
        if declared:
            block.language, block.language_source = declared, lang.DECLARED
        else:
            block.language = majority


def _flag_boilerplate(blocks: list[Block], n_pages: int, config: Config) -> None:
    candidates = [
        block for block in blocks if block.block_type in (PARAGRAPH, HEADING, CAPTION)
    ]
    flagged = bp.find_boilerplate(
        candidates,
        n_pages,
        page_frequency_threshold=config.boilerplate.page_frequency_threshold,
        bbox_tolerance_px=config.boilerplate.bbox_tolerance_px,
        normalize_before_compare=config.boilerplate.normalize_before_compare,
        max_block_chars=config.boilerplate.max_block_chars,
    )
    for index in flagged:
        candidates[index].is_boilerplate = True


def _assemble_markdown(blocks: list[Block]) -> str:
    """Boilerplate stays in the block store with provenance but out of the Markdown, so it
    never reaches extraction. Those blocks get no span."""
    parts: list[str] = []
    offset = 0
    for block in blocks:
        if block.is_boilerplate:
            block.span_start = block.span_end = None
            continue
        rendered = block.render()
        if not rendered:
            continue
        block.span_start = offset
        block.span_end = offset + len(rendered)
        parts.append(rendered)
        offset = block.span_end + 2  # the "\n\n" joiner
    return "\n\n".join(parts)
