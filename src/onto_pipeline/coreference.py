"""ITER-COREFER — intra-document coreference.

A different problem from cross-document linking, solved earlier and by another method
(COREF-INTRA-DOCUMENT).
An anaphoric definite phrase — "the system", "these researchers" — is resolved here, inside
the document, and never reaches ITER-MATCH's cross-document entity resolution.

Method: the LLM over the whole document. Ten pages is roughly ten thousand tokens and fits in
context, and with no confidentiality constraint (API-LLM-ALLOWED) that is the pragmatic route.
Dedicated
tools (CorPipe, CorefUD derivatives) are research infrastructure with fragile installs and are
out of scope for v1; reconsider at thousands of documents.

**The critical implementation detail: never ask for spans.** ITER-EXTRACT already extracted the
mentions
with their offsets. They are numbered, the document is passed with the markers in place, and
the model is asked to group *identifiers*:

    [[M3, M17, M42], [M8, M11]]

which is mechanically verifiable — do all the identifiers exist, is any of them in two groups —
without the fragile offset parsing that asking for spans would require.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from .llm import Prompt

STAGE = "iter_corefer"

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""The mentions in this document are numbered with markers like [M12].

Group the markers that refer to **the same individual thing**. "The system" and "the platform"
in one paragraph may be the same system; two separate studies are not the same study even
though both are studies.

Same *kind* is not the same *thing*: three different interviews are three individuals, not one
group. Group only what the document itself says is one and the same.

Leave out anything that stands alone — a marker in no group is the normal case. Use only
markers that appear below, and never put one in two groups.

DOCUMENT:
{text}

Answer with JSON only: {{"groups": [["M3", "M17"], ["M8", "M11"]]}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_MARKER_RE = re.compile(r"M\d+")


@dataclass
class Marked:
    text: str
    order: list[str] = field(default_factory=list)   # marker index -> mention id


@dataclass
class Grouping:
    groups: list[list[str]] = field(default_factory=list)   # mention ids
    unknown_markers: list[str] = field(default_factory=list)
    duplicated_markers: list[str] = field(default_factory=list)

    @property
    def assignments(self) -> dict[str, str]:
        """mention id -> coref group id. A singleton is not a group."""
        return {
            mention_id: f"g{index}"
            for index, group in enumerate(self.groups)
            for mention_id in group
        }


def mark(markdown: str, mentions: list[dict]) -> Marked:
    """Insert `[Mn]` after each mention, working backwards so earlier offsets stay valid."""
    ordered = sorted(mentions, key=lambda m: m["span_start"])
    order = [m["id"] for m in ordered]
    text = markdown
    for index in range(len(ordered) - 1, -1, -1):
        end = ordered[index]["span_end"]
        text = f"{text[:end]}[M{index}]{text[end:]}"
    return Marked(text=text, order=order)


def payload(marked: Marked) -> dict[str, str]:
    return {"text": marked.text}


def parse(text: str, payload: dict) -> list[list[str]]:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())
    if not isinstance(data.get("groups"), list):
        raise ValueError("the answer has no 'groups' list")
    return [
        [str(marker).strip() for marker in group]
        for group in data["groups"]
        if isinstance(group, list)
    ]


def resolve(marked: Marked, groups: list[list[str]]) -> Grouping:
    """Translate markers back to mention ids, rejecting what does not check out.

    Verifiable by construction, which is the whole reason for asking about identifiers rather
    than spans: a marker that does not exist, or one claimed by two groups, is caught here
    instead of silently linking the wrong mentions.
    """
    result = Grouping()
    seen: dict[str, int] = {}

    for group in groups:
        members: list[str] = []
        for marker in group:
            if not _MARKER_RE.fullmatch(marker):
                result.unknown_markers.append(marker)
                continue
            index = int(marker[1:])
            if not 0 <= index < len(marked.order):
                result.unknown_markers.append(marker)
                continue
            if marker in seen:
                result.duplicated_markers.append(marker)
                continue
            seen[marker] = index
            members.append(marked.order[index])
        # A group of one links nothing; the spec's normal case is a mention on its own.
        if len(members) > 1:
            result.groups.append(members)
    return result


def persist(conn: sqlite3.Connection, assignments: dict[str, str]) -> None:
    conn.executemany(
        "UPDATE mentions SET coref_group = ? WHERE id = ?",
        [(group, mention_id) for mention_id, group in assignments.items()],
    )
    conn.commit()
