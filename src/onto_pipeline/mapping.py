"""Mapping rules and ABox regeneration (LAYERS, ITER-CONFLICTS, ITER-APPLY;
see mapping_rules_plan.md).

The spec names "mapping rules" in five places and defines them in none, while resting its
central claim on them: because the ABox derives from the mention layer and not from outside
sources, reorganizing the TBox never needs a migration script — the rules change and it is
recomputed.

Two of the spec's own statements settle what they have to be. Section 6.4 gives notarize,
force and refute a *per-case* scope; 6.8 says regeneration recomputes the *whole* ABox. A
per-case decision that must survive a wholesale recompute cannot live inside the code doing the
recomputing, so the rules are data: a global policy in the config, exceptions in the store.

Regeneration is a pure function of (mention layer, typings for one ontology version, rules).
It consults no model and no reasoner: everything it would ask was already decided upstream, and
what nobody could decide is waiting in `review_items` rather than blocking here. Three
properties it has to keep, in the order they matter:

    deterministic  the same inputs give the same triples, so a version's ABox is reproducible
    idempotent     running it twice over one (state_hash, rules_hash) changes nothing
    total          it never writes to the mention layer. Section 3's arrow points one way, and
                   this is the one stage that could violate it by accident

Individual IRIs are `uuid5` over the *anchor* mention — the smallest id in the entity's group
under a total order — never over the group as a whole. An individual is a group of mentions,
and groups grow every iteration as new documents arrive; hashing the whole group would change
an entity's identity for the sole reason that it became better attested. Anchored, the IRI only
moves when the group *splits*, which is the case where a new identity is the right answer.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, PROV, RDF, RDFS, XSD

from . import typing_store
from .store import Store
from .typing_store import POSSIBLE_DUPLICATE

AUTO = "auto"
GREY = "grey"

ENTITY = "entity"
MENTION = "mention"

NAMED_GRAPH = "named_graph"
FLAT = "flat"

NOTARIZE = "notarize"
FORCE = "force"
REFUTE = "refute"

SEPARATE = "separate"
MERGE = "merge"

# Falsity marks, carried in as per-case exceptions so they reach the rules hash — a decision
# that changed no rule would be a decision the ABox never notices. Named here rather than
# imported from `conflicts` so regeneration keeps depending on nothing but its arguments.
FALSITY_MARKS = frozenset({"refuted", "misextracted"})

# Same namespace as the seed normalizer's, so an individual's IRI and a class's are minted by
# one scheme. uuid5, never uuid4: regeneration has to be reproducible.
_IRI_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

ONTO = "https://ontology.local/vocab#"
DERIVED_FROM = URIRef(ONTO + "derivedFromMention")
SURFACE = URIRef(ONTO + "surfaceForm")
PAGE = URIRef(ONTO + "page")
SPAN_START = URIRef(ONTO + "spanStart")
SPAN_END = URIRef(ONTO + "spanEnd")
UNRESOLVED = URIRef(ONTO + "possibleDuplicateUnresolved")
DOCUMENT = URIRef(ONTO + "document")
# Notarizing keeps both facts *and* the disagreement. Without a flag the result is
# indistinguishable from an entity that simply belongs to two classes, and the whole point of
# the policy is that the conflict stays findable (6.4).
NOTARIZED = URIRef(ONTO + "notarizedTypeConflict")
REFUTED_TYPES = URIRef(ONTO + "refutedTypeConflict")


class UnknownPolicy(ValueError):
    """A rule field was given a value that is not implemented."""


_ALLOWED: dict[str, frozenset[str]] = {
    "individual_from": frozenset({ENTITY, MENTION}),
    "provenance": frozenset({NAMED_GRAPH, FLAT}),
    "conflict_policy": frozenset({NOTARIZE, FORCE, REFUTE}),
    "duplicate_policy": frozenset({SEPARATE, MERGE}),
}


@dataclass(frozen=True)
class MappingRules:
    """How the mention layer becomes an ABox. The defaults are the spec's own positions.

    `type_from` is the conservative policy of 6.2 carried into the ABox: the grey zone does not
    type until it is answered. `duplicate_policy` keeps 6.8's hidden conflict visible — two
    duplicates with one value each look like confirmation of functionality — by leaving them as
    separate individuals, marked, and excluded from that count.
    """

    individual_from: str = ENTITY
    type_from: tuple[str, ...] = (AUTO,)
    provenance: str = NAMED_GRAPH
    conflict_policy: str = NOTARIZE
    duplicate_policy: str = SEPARATE
    base_iri: str = "https://ontology.local/id/"
    exceptions: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        for name, allowed in _ALLOWED.items():
            value = getattr(self, name)
            if value not in allowed:
                raise UnknownPolicy(
                    f"{name}={value!r} is not implemented; "
                    f"available: {', '.join(sorted(allowed))}"
                )
        unknown = set(self.type_from) - {AUTO, GREY}
        if unknown:
            raise UnknownPolicy(f"type_from: unknown zone(s) {sorted(unknown)}")

    def canonical(self) -> str:
        """The effective rule set as one canonical string: global policy plus the exceptions
        in force. Sorted and separator-fixed, so the hash depends on content and not on how a
        dict happened to be ordered."""
        payload = asdict(self)
        payload["type_from"] = sorted(self.type_from)
        payload["exceptions"] = sorted([list(item) for item in self.exceptions])
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def rules_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical().encode("utf-8")).hexdigest()

    def with_exceptions(self, exceptions: dict[str, str]) -> MappingRules:
        merged = {**dict(self.exceptions), **exceptions}
        return MappingRules(
            individual_from=self.individual_from,
            type_from=self.type_from,
            provenance=self.provenance,
            conflict_policy=self.conflict_policy,
            duplicate_policy=self.duplicate_policy,
            base_iri=self.base_iri,
            exceptions=tuple(sorted(merged.items())),
        )


def individual_iri(anchor_mention_id: str, base_iri: str) -> URIRef:
    """The entity's IRI, minted from its anchor mention.

    Anchored rather than derived from the whole group: mention ids are the one thing in the
    system that never moves, so an entity keeps its identity when new documents add mentions
    to it, and only takes a new one when the group splits.
    """
    return URIRef(base_iri + str(uuid.uuid5(_IRI_NAMESPACE, anchor_mention_id)))


@dataclass
class MentionRow:
    id: str
    document_id: str
    page: int
    surface_text: str
    span_start: int | None
    span_end: int | None
    candidate_entity: str | None
    status: str


@dataclass
class Regeneration:
    dataset: Dataset
    rules_hash: str
    n_individuals: int = 0
    n_typed: int = 0
    n_untyped: int = 0
    n_unresolved: int = 0
    n_conflicts: int = 0
    n_excluded: int = 0      # mentions a falsity mark kept out of the ABox (6.4)

    @property
    def n_triples(self) -> int:
        return sum(1 for _ in self.dataset.quads((None, None, None, None)))


def anchors(rows: list[MentionRow], rules: MappingRules) -> dict[str, list[MentionRow]]:
    """Group the mentions into individuals, keyed by the anchor mention's id.

    A mention no merge decision touched is its own entity: separate individuals until confirmed
    (SEPARATE-UNTIL-CONFIRMED) is the default, and a singleton group is what that looks like here.
    """
    groups: dict[str, list[MentionRow]] = {}
    for row in rows:
        if rules.individual_from == MENTION:
            key = row.id
        else:
            key = row.candidate_entity or row.id
        groups.setdefault(key, []).append(row)
    # The stored entity key is one member's id, but not necessarily the smallest: re-key on the
    # anchor so the IRI does not depend on which member the union-find happened to elect.
    anchored: dict[str, list[MentionRow]] = {}
    for members in groups.values():
        members.sort(key=lambda item: item.id)
        anchored[members[0].id] = members
    return anchored


def regenerate(
    rows: list[MentionRow],
    typings: dict[str, tuple[str | None, str]],
    rules: MappingRules,
    above: dict[str, set[str]] | None = None,
) -> Regeneration:
    """The mention layer plus one version's typings, as an ABox.

    `typings` maps a mention id to (class IRI, zone). A mention typed in a zone the rules do
    not accept is emitted as an untyped individual rather than dropped: it exists, it has
    provenance, and what is missing is only its class.

    `above` is the subclass closure, used only to tell a disagreement from a hierarchy: two
    documents typing one entity at two levels of the same branch agree.
    """
    dataset = Dataset()
    default = dataset.graph(URIRef(ONTO + "abox"))
    result = Regeneration(dataset=dataset, rules_hash=rules.rules_hash())
    accepted = set(rules.type_from)

    exceptions = dict(rules.exceptions)
    kept = [row for row in rows if exceptions.get(f"mention:{row.id}") not in FALSITY_MARKS]
    result.n_excluded = len(rows) - len(kept)
    rows = kept

    for anchor, members in sorted(anchors(rows, rules).items()):
        individual = individual_iri(anchor, rules.base_iri)
        result.n_individuals += 1
        default.add((individual, RDF.type, OWL.NamedIndividual))
        default.add((individual, RDFS.label, Literal(members[0].surface_text)))

        by_class: dict[str, int] = {}
        for member in members:
            iri, zone = typings.get(member.id, (None, ""))
            if iri and zone in accepted:
                by_class[iri] = by_class.get(iri, 0) + 1

        classes = sorted(by_class)
        # A superclass of another class in the same set is entailed, not a second opinion.
        # Passed in rather than derived: this stage reads the TBox through its arguments or
        # not at all, and it never calls a reasoner.
        disagreeing = [
            iri for iri in classes
            if not any(other != iri and iri in (above or {}).get(other, ()) for other in classes)
        ]
        if len(disagreeing) > 1:
            result.n_conflicts += 1
            classes = _settle(disagreeing, by_class, anchor, rules, default, individual)
        for iri in classes:
            default.add((individual, RDF.type, URIRef(iri)))
        result.n_typed += bool(classes)
        result.n_untyped += not classes

        if any(member.status == POSSIBLE_DUPLICATE for member in members):
            # Kept visible rather than resolved: an unresolved duplicate is what contaminates
            # the functional-property support count (6.8), and the count reads this flag.
            default.add((individual, UNRESOLVED, Literal(True)))
            result.n_unresolved += 1

        for member in members:
            target = (
                dataset.graph(URIRef(ONTO + f"doc/{member.document_id}"))
                if rules.provenance == NAMED_GRAPH else default
            )
            mention = URIRef(f"{rules.base_iri}mention/{member.id}")
            target.add((individual, DERIVED_FROM, mention))
            target.add((mention, RDF.type, PROV.Entity))
            target.add((mention, SURFACE, Literal(member.surface_text)))
            target.add((mention, DOCUMENT, Literal(member.document_id)))
            target.add((mention, PAGE, Literal(member.page, datatype=XSD.integer)))
            if member.span_start is not None:
                target.add((mention, SPAN_START, Literal(member.span_start,
                                                        datatype=XSD.integer)))
                target.add((mention, SPAN_END, Literal(member.span_end, datatype=XSD.integer)))
    return result


def _settle(
    classes: list[str], support: dict[str, int], anchor: str, rules: MappingRules,
    graph, individual: URIRef,
) -> list[str]:
    """Two documents typed one entity two ways. Which facts survive, per 6.4's policies.

    `notarize` is the silent default because it is the only one that destroys no information:
    both types stay, the provenance already says which document contributed each, and a flag
    keeps the disagreement findable instead of letting it read as an entity that simply
    belongs to two classes.
    """
    override = dict(rules.exceptions).get(f"entity:{anchor}", "")
    if override.startswith("force:"):
        chosen = override.split(":", 1)[1]
        return [chosen] if chosen in classes else classes

    if rules.conflict_policy == FORCE:
        # No per-case winner was named, so the best defensible global rule: the class the most
        # mentions support, ties broken by IRI so the output stays reproducible.
        return [max(classes, key=lambda iri: (support[iri], iri))]
    if rules.conflict_policy == REFUTE:
        graph.add((individual, REFUTED_TYPES, Literal(True)))
        return []
    graph.add((individual, NOTARIZED, Literal(True)))
    return classes


def load_inputs(
    conn: Store, version_id: str, *, session_id: str
) -> tuple[list[MentionRow], dict[str, tuple[str | None, str]]]:
    """Read-only over the mention layer, by contract: regeneration never writes to it.

    Installing the typing schema is not an exception to that — it creates the table if the
    store predates it, so that a version with no typings reads as "no mention is typed" rather
    than raising.
    """
    typing_store.install(conn)
    rows = [
        MentionRow(
            id=row["id"], document_id=row["document_id"], page=row["page"],
            surface_text=row["surface_text"], span_start=row["span_start"],
            span_end=row["span_end"], candidate_entity=row["candidate_entity"],
            status=row["status"],
        )
        for row in conn.execute(
            "SELECT id, document_id, page, surface_text, span_start, span_end, "
            "candidate_entity, status FROM mentions WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
    ]
    typings = {
        row["mention_id"]: (row["iri"], row["zone"])
        for row in conn.execute(
            "SELECT mention_id, iri, zone FROM mention_typing WHERE version_id = ?",
            (version_id,),
        )
    }
    return rows, typings


def rules_from_config(section: Any, base_iri: str) -> MappingRules:
    return MappingRules(
        individual_from=section.individual_from,
        type_from=tuple(section.type_from),
        provenance=section.provenance,
        conflict_policy=section.conflict_policy,
        duplicate_policy=section.duplicate_policy,
        base_iri=base_iri,
    )


def flatten(dataset: Dataset) -> Graph:
    """One graph with every quad's triple, for the consumers that take a Graph — the reasoner
    and the SHACL filter among them. Provenance survives as triples; only the partition by
    document is lost."""
    graph = Graph()
    for subject, predicate, obj, _ in dataset.quads((None, None, None, None)):
        graph.add((subject, predicate, obj))
    return graph
