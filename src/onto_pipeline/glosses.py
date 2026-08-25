"""A0.4 — gloss bootstrap (spec 4.3).

A label is a name; a gloss is a definition. The matcher (B2) compares a mention's text
against the definition, not against the name, so denormalizing an identifier does not produce
one. The prompt is built from the structural neighbourhood: superclass, subclasses, the
properties the class is domain or range of, and the classes it is disjoint with. The label is
included so the sentence can name its subject, but the substance has to come from the
neighbourhood.

The gloss is not a fixed value of A0. B4b enriches it from definitional passages every
iteration, which closes a self-correcting loop: a better gloss means better matching, which
means fewer false orphans. A mention orphaned at iteration 3 can be typed correctly at 8.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SKOS

from .llm import Prompt
from .seed import GlossContext

STAGE = "A0_4_glosses"

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""You are defining one concept from an ontology.

Concept label: {label}
Parent concepts: {superclasses}
Child concepts: {subclasses}
Properties it is the domain of: {domain_of}
Properties it is the range of: {range_of}
Incompatible with: {disjoint_with}

Write a definition of this concept, grounded in the structure above rather than in the
wording of the label. State what the concept is and what distinguishes it from its siblings.
One sentence per language, no more than 40 words each. Do not restate the label as a
definition of itself.

Answer with JSON only: {{"en": "...", "es": "..."}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class Gloss:
    iri: str
    en: str
    es: str


def payload(context: GlossContext) -> dict[str, str]:
    return {
        "label": context.label,
        "superclasses": _render(context.superclasses),
        "subclasses": _render(context.subclasses),
        "domain_of": _render(context.domain_of),
        "range_of": _render(context.range_of),
        "disjoint_with": _render(context.disjoint_with),
    }


def _render(values: list[str]) -> str:
    return ", ".join(values) if values else "none declared"


def parse(text: str, payload: dict[str, str]) -> dict[str, str]:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())
    missing = {"en", "es"} - data.keys()
    if missing:
        raise ValueError(f"gloss is missing {sorted(missing)}")
    return {"en": str(data["en"]).strip(), "es": str(data["es"]).strip()}


def write(graph: Graph, glosses: list[Gloss]) -> None:
    for gloss in glosses:
        iri = URIRef(gloss.iri)
        graph.remove((iri, SKOS.definition, None))
        graph.add((iri, SKOS.definition, Literal(gloss.en, lang="en")))
        graph.add((iri, SKOS.definition, Literal(gloss.es, lang="es")))
