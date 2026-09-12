"""PREP-NORMALIZE-GLOSSES — gloss bootstrap.

A label is a name; a gloss is a definition. The matcher (ITER-MATCH) compares a mention's text
against the definition, not against the name, so denormalizing an identifier does not produce
one. The prompt is built from the structural neighbourhood: superclass, subclasses, the
properties the class is domain or range of, and the classes it is disjoint with. The label is
included so the sentence can name its subject, but the substance has to come from the
neighbourhood.

The gloss is not a fixed value of PREP-NORMALIZE. ITER-AXIOMATIZE-ENRICH enriches it from
definitional passages every
iteration, which closes a self-correcting loop: a better gloss means better matching, which
means fewer false orphans. A mention orphaned at iteration 3 can be typed correctly at 8.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import SKOS

from .initial_ontology import GlossContext
from .llm import Prompt

STAGE = "prep_normalize_glosses"

PROMPT = Prompt(
    stage=STAGE,
    version="v2",
    template="""You are writing a dictionary definition of one concept, for a reader who has
never seen the ontology it comes from.

Concept label: {label}
Parent concepts: {superclasses}
Child concepts: {subclasses}
Properties it is the domain of: {domain_of}
Properties it is the range of: {range_of}
Incompatible with: {disjoint_with}

The structure above is evidence about what the concept means. Use it to work out the meaning,
then describe the concept itself. Do NOT describe the ontology.

Hard constraints:
- Never mention a property or relation name, and never write "relation", "linked via",
  "the range of", "the domain of", "the target of" or "the source of".
- Write the words a research paper would actually use for this thing. The definition is what
  a passage of text gets compared against, so it must read like the domain, not like a schema.
- Say what it is and what separates it from its sibling concepts.
- One sentence per language, at most 40 words. Do not restate the label as its own definition.

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


def carry_over(previous: Graph, graph: Graph) -> int:
    """The glosses a previous version already has, onto a freshly derived graph.

    `normalize_initial_ontology` re-reads the seed from disk, so re-normalizing produces a graph
    with no `skos:definition` at all. Serializing that over the artifact — or committing it —
    drops every gloss until someone runs the bootstrap again, and the matcher compares against
    names in the meantime. Opaque IRIs are a uuid5 of the original, so they line up across
    derivations and the definitions can simply be carried.

    A definition already in `graph` wins: a re-glossed or enriched entity is not overwritten
    with an older sentence.
    """
    # Which subjects already have one is read before writing any: a gloss is two literals, one
    # per language, and checking as we go would carry the first and skip the second.
    defined = {subject for subject, _, _ in graph.triples((None, SKOS.definition, None))}
    carried = 0
    for subject, _, obj in previous.triples((None, SKOS.definition, None)):
        if subject in defined:
            continue
        graph.add((subject, SKOS.definition, obj))
        carried += 1
    return carried


def write(graph: Graph, glosses: list[Gloss]) -> None:
    for gloss in glosses:
        iri = URIRef(gloss.iri)
        graph.remove((iri, SKOS.definition, None))
        graph.add((iri, SKOS.definition, Literal(gloss.en, lang="en")))
        graph.add((iri, SKOS.definition, Literal(gloss.es, lang="es")))
