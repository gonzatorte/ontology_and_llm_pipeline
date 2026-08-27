"""Axiomatization — proposed classes become axioms (ITER-AXIOMATIZE, 6.1).

The governing principle, stated once more because this is the stage where breaking it would do
the most damage:

    The LLM classifies and names. The code builds the logic. The reasoner rejects.

The model is **never** asked for OWL. It is asked one atomic question — is this concept a *kind
of* that one, an *example of* it, or neither — and the code turns the answer into an axiom. A
model that only ever answers that cannot confuse subsumption with instantiation, because it
never writes the axiom. That confusion is the first bias listed in 6.1 and this is where it
would enter.

Two consequences of the induction run feeding this:

The candidate parent that arrives with a proposal is the matcher's runner-up, and on real data
it is often nonsense — Startup Company under Interview Answer, four unrelated concepts under
Project. So the parent is *re-decided* here against several candidates with their definitions,
and "none of these" is a first-class answer that makes the class a root rather than forcing a
bad parent.

And an answer of "example of" is not a weaker subsumption: it means the thing was never a class.
The proposal is rejected rather than attached, because 6.1's bias is precisely to model an
instance as a subclass.

Provenance splits the way 6.2b requires: an axiom from induced mentions is `textual` and carries
the mentions that support it, one from bridging is `world_knowledge` and carries no citation.
Only the first is subject to the evidence filter — the rule "every axiom without a citation is
discarded" would kill exactly the bridges that make the seed useful.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from .llm import Prompt

STAGE = "iter_axiomatize"

SUBCLASS = "subclass_of"
INSTANCE = "instance_of"
UNRELATED = "unrelated"

TEXTUAL = "textual"
WORLD_KNOWLEDGE = "world_knowledge"

PROMPT = Prompt(
    stage=STAGE,
    version="v2",   # v2: el prompt muestra precedentes (ITER-FEEDBACK)
    template="""A new concept was found in a research corpus. Decide how it relates to the
concepts an existing ontology already has.

NEW CONCEPT: {label}
What it is: {gloss}
What distinguishes it: {criterion}
Phrases it was found as: {phrases}

CANDIDATES FROM THE EXISTING ONTOLOGY:
{candidates}

WHAT WAS DECIDED BEFORE, on proposals like this one:
{precedents}

Answer exactly one of:

- `"subclass_of"` — every instance of the new concept is also an instance of the candidate.
  Not "related to", not "usually a": every one, always.
- `"instance_of"` — the new concept is one particular example of the candidate rather than a
  kind of it. A named company is an example of Organization; "Startup Company" is a kind of it.
- `"unrelated"` — none of the candidates is a supertype. This is a normal answer and often the
  right one; a forced parent is worse than none.

The past decisions above are precedents, not rules. They were made by the user on this ontology,
so a rejection there is evidence that the same shape of answer was wrong before — read the
comment, which says why. If this concept differs from the precedent in a way that matters, say
so in `why` and decide on its merits.

Answer with JSON only:
{{"relation": "subclass_of" | "instance_of" | "unrelated",
  "candidate": "the exact label of the chosen candidate, or null",
  "why": "one sentence"}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposed_axioms (
  id           TEXT PRIMARY KEY,
  version_id   TEXT NOT NULL,     -- proposed against this state
  subject_iri  TEXT NOT NULL,
  predicate    TEXT NOT NULL,
  object_iri   TEXT,
  literal      TEXT,
  provenance   TEXT NOT NULL,     -- textual | world_knowledge
  support      TEXT,              -- JSON: the mention ids behind it, for textual ones
  note         TEXT,
  created_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_axioms_version ON proposed_axioms(version_id, provenance);
"""


@dataclass
class Judgement:
    relation: str
    candidate: str | None = None
    why: str = ""


@dataclass
class Axiom:
    subject_iri: str
    predicate: str
    object_iri: str | None = None
    literal: str | None = None
    language: str | None = None
    provenance: str = TEXTUAL
    support: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def id(self) -> str:
        import hashlib

        material = f"{self.subject_iri}|{self.predicate}|{self.object_iri}|{self.literal}"
        return hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]


@dataclass
class Assembly:
    axioms: list[Axiom] = field(default_factory=list)
    minted: dict[str, str] = field(default_factory=dict)   # proposal id -> new IRI
    rejected: dict[str, str] = field(default_factory=dict)  # proposal id -> why


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def payload(
    proposal: dict, candidates: list[dict], phrases: list[str],
    precedents: Sequence[dict] = (),
) -> dict[str, str]:
    rendered = "\n".join(
        f"- {item['label']}: {item.get('gloss') or '(no definition)'}" for item in candidates
    ) or "- (none)"
    return {
        "label": proposal["label"],
        "gloss": proposal.get("gloss") or "(none given)",
        "criterion": proposal.get("criterion") or "(none given)",
        "phrases": ", ".join(phrases[:12]) or "(none)",
        "candidates": rendered,
        "precedents": render_precedents(precedents),
    }


def render_precedents(precedents: Sequence[dict]) -> str:
    """Las decisiones anteriores, como texto para el prompt.

    Con el comentario del usuario, que es lo que las vuelve útiles: "se rechazó" dice el
    veredicto y "se rechazó porque el criterio ya lo cubre el padre" dice el motivo, que es lo
    único transferible a una propuesta distinta.
    """
    if not precedents:
        return "(ninguna todavía — es la primera iteración, o nada parecido se decidió antes)"
    lines = []
    for item in precedents:
        verdict = {"chosen": "se eligió", "not_chosen": "se descartó",
                   "invalid": "se marcó INVÁLIDA"}.get(item["status"], item["status"])
        because = f" — «{item['comment']}»" if item.get("comment") else ""
        lines.append(f"- {item['normalized_axioms']}: {verdict}{because}")
    return "\n".join(lines)


def parse(text: str, payload: dict[str, Any]) -> dict[str, Any]:
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())
    relation = str(data.get("relation", "")).strip()
    if relation not in (SUBCLASS, INSTANCE, UNRELATED):
        raise ValueError(f"unknown relation {relation!r}")
    candidate = data.get("candidate")
    if relation in (SUBCLASS, INSTANCE) and not candidate:
        raise ValueError(f"{relation} needs the candidate it refers to")
    return {
        "relation": relation,
        "candidate": str(candidate).strip() if candidate else None,
        "why": str(data.get("why", "")).strip(),
    }


def mint_iri(base_iri: str, label: str, proposal_id: str) -> str:
    """Opaque, like every other entity (PREP-NORMALIZE-IRIS), and derived so a re-run is stable."""
    import uuid

    from .seed import _IRI_NAMESPACE

    return base_iri + str(uuid.uuid5(_IRI_NAMESPACE, f"induced:{proposal_id}:{label}"))


def assemble(
    proposals: list[dict],
    judgements: dict[str, Judgement],
    *,
    base_iri: str,
    label_to_iri: dict[str, str],
    support: dict[str, list[str]] | None = None,
) -> Assembly:
    """Turn judgements into axioms. This is the only place OWL is written."""
    support = support or {}
    assembly = Assembly()

    for proposal in proposals:
        proposal_id = proposal["id"]
        judgement = judgements.get(proposal_id)
        if judgement is None:
            continue
        if judgement.relation == INSTANCE:
            # Not a weaker subsumption: it was never a class. Attaching it would be exactly
            # the confusion 6.1 lists first.
            assembly.rejected[proposal_id] = (
                f"an example of {judgement.candidate}, not a kind of it"
            )
            continue

        iri = mint_iri(base_iri, proposal["label"], proposal_id)
        assembly.minted[proposal_id] = iri
        mentions = support.get(proposal_id, [])
        provenance = TEXTUAL if mentions else WORLD_KNOWLEDGE

        assembly.axioms.append(Axiom(iri, "type", str(OWL.Class), provenance=provenance,
                                     support=mentions))
        assembly.axioms.append(Axiom(iri, "prefLabel", literal=proposal["label"], language="en",
                                     provenance=provenance, support=mentions))
        if proposal.get("gloss"):
            assembly.axioms.append(Axiom(iri, "definition", literal=proposal["gloss"],
                                         language="en", provenance=provenance,
                                         support=mentions))
        if proposal.get("criterion"):
            assembly.axioms.append(Axiom(iri, "scopeNote", literal=proposal["criterion"],
                                         language="en", provenance=provenance,
                                         support=mentions))

        if judgement.relation == SUBCLASS:
            parent = label_to_iri.get(judgement.candidate or "")
            if parent:
                assembly.axioms.append(
                    Axiom(iri, "subClassOf", parent, provenance=provenance,
                          support=mentions, note=judgement.why)
                )
            else:
                assembly.rejected[proposal_id] = (
                    f"named a parent that is not in the ontology: {judgement.candidate!r}"
                )
                assembly.minted.pop(proposal_id, None)
                assembly.axioms = [a for a in assembly.axioms if a.subject_iri != iri]
        # UNRELATED leaves the class a root, which the spec prefers to a forced parent.
    return assembly


def normal_form(axioms: Sequence[Axiom], labels: dict[str, str]) -> str:
    """La forma normal de una propuesta: qué dice, sin depender de los IRIs que le tocaron.

    Sin esto no se puede detectar una re-proposición (ITER-FEEDBACK). Un IRI acuñado sale de `uuid5`
    sobre
    el id de la propuesta, así que el **mismo** compromiso —la misma clase, con el mismo nombre,
    colgando del mismo padre— vuelve en la iteración siguiente con otro identificador y no se
    parece en nada al anterior. Lo que se conserva entre iteraciones es cómo se llaman las cosas,
    no cómo se las identifica.

    Así que cada IRI se reemplaza por su etiqueta —la del sujeto viene con la propuesta, la del
    objeto la trae la ontología— y las anotaciones se descartan salvo el nombre, porque una glosa
    reescrita no es otro compromiso. El resultado se ordena, así que el orden en que se armaron
    los axiomas no cambia nada.
    """
    # El sujeto es un IRI recién acuñado, así que su etiqueta no está en la ontología todavía:
    # está en el propio lote, en su axioma `prefLabel`.
    minted = {
        axiom.subject_iri: axiom.literal
        for axiom in axioms if axiom.predicate == "prefLabel" and axiom.literal
    }

    def name(iri: str | None) -> str:
        if iri is None:
            return ""
        return minted.get(iri) or labels.get(iri) or iri.rsplit("/", 1)[-1]

    statements = {
        f"{name(axiom.subject_iri)} {axiom.predicate} {name(axiom.object_iri)}"
        for axiom in axioms
        # Cómo se explica una clase no es qué se compromete con ella: una glosa reescrita no es
        # otra propuesta. La etiqueta sí importa, y entra como el nombre del sujeto.
        if axiom.predicate not in ("definition", "scopeNote", "historyNote", "altLabel",
                                   "prefLabel")
        and axiom.object_iri is not None
    }
    return " | ".join(sorted(statements))


_PREDICATES = {
    "type": RDF.type,
    "subClassOf": RDFS.subClassOf,
    "prefLabel": SKOS.prefLabel,
    "definition": SKOS.definition,
    "scopeNote": SKOS.scopeNote,
}


def apply(graph: Graph, axioms: list[Axiom]) -> Graph:
    """A new graph, never the one passed in: the caller still needs the previous state to
    diff against and to fall back to if the reasoner rejects this one."""
    extended = Graph()
    for prefix, namespace in graph.namespaces():
        extended.bind(prefix, namespace)
    for triple in graph:
        extended.add(triple)

    for axiom in axioms:
        predicate = _PREDICATES[axiom.predicate]
        if axiom.object_iri is not None:
            extended.add((URIRef(axiom.subject_iri), predicate, URIRef(axiom.object_iri)))
        else:
            extended.add((
                URIRef(axiom.subject_iri), predicate,
                Literal(axiom.literal, lang=axiom.language) if axiom.language
                else Literal(axiom.literal),
            ))
    return extended


def load(conn: sqlite3.Connection, version_id: str) -> list[Axiom]:
    """The axioms proposed against a version, as they were assembled.

    Branching reads them back rather than re-running the model: the judgements cost money and
    the branch is a decision about axioms that already exist, not a second opinion on them.
    """
    install(conn)
    return [
        Axiom(
            subject_iri=row["subject_iri"], predicate=row["predicate"],
            object_iri=row["object_iri"], literal=row["literal"],
            language="en" if row["literal"] else None,
            provenance=row["provenance"], support=json.loads(row["support"] or "[]"),
            note=row["note"] or "",
        )
        for row in conn.execute(
            "SELECT * FROM proposed_axioms WHERE version_id = ? ORDER BY subject_iri, predicate",
            (version_id,),
        )
    ]


def persist(conn: sqlite3.Connection, version_id: str, axioms: list[Axiom]) -> None:
    install(conn)
    conn.execute("DELETE FROM proposed_axioms WHERE version_id = ?", (version_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO proposed_axioms (id, version_id, subject_iri, predicate, "
        "object_iri, literal, provenance, support, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (a.id, version_id, a.subject_iri, a.predicate, a.object_iri, a.literal,
             a.provenance, json.dumps(a.support), a.note, _now())
            for a in axioms
        ],
    )
    conn.commit()
