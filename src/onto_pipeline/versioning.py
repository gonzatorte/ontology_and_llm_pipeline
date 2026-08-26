"""B7–B8 — version DAG, semantic diff and loop detection (spec 6.8).

Not postponable (spec 12): even before multi-branch exists, the versioning, the history and
the state hash have to be in place. Retrofitting a DAG over fifteen already-applied iterations
means losing that history.

A directed graph, not a linear list: branches that were not chosen are kept, so iteration 4's
option C stays reachable.

The store is its own, not Git. Git over Turtle files is tempting, but a textual diff is
useless here — a semantic diff is needed either way, and Git would contribute only storage.

The state hash is computed over logical axioms with canonical blank nodes, never over labels:
otherwise a pure rename counts as a new state. Because the store is a DAG, an A->B->A
oscillation is exactly a cycle and is detected exactly and cheaply.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rdflib import Graph
from rdflib.compare import to_canonical_graph
from rdflib.namespace import DC, DCTERMS, OWL, RDF, RDFS, SKOS, XSD

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
  id           TEXT PRIMARY KEY,
  parent_id    TEXT,
  iteration    INTEGER,
  branch_id    TEXT,
  state_hash   TEXT NOT NULL,
  turtle       TEXT NOT NULL,
  note         TEXT,
  created_at   TEXT,
  rules_hash   TEXT,             -- which mapping rules produced this version's ABox
  FOREIGN KEY (parent_id) REFERENCES versions(id)
);
CREATE INDEX IF NOT EXISTS idx_versions_hash ON versions(state_hash);
"""

# Annotation predicates carry naming, not logic. A rename must not read as a new state.
ANNOTATION_PREDICATES = frozenset({
    RDFS.label, RDFS.comment, RDFS.seeAlso, RDFS.isDefinedBy,
    SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel, SKOS.definition,
    SKOS.note, SKOS.historyNote, SKOS.editorialNote, SKOS.scopeNote, SKOS.example,
    OWL.versionInfo, OWL.deprecated, DC.title, DC.description, DCTERMS.title,
    DCTERMS.description,
})


@dataclass
class Version:
    id: str
    state_hash: str
    parent_id: str | None = None
    iteration: int = 0
    branch_id: str | None = None
    note: str = ""


@dataclass
class Diff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    labels_changed: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.labels_changed)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def install(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so a column added after the
    # fact needs this. `rules_hash` records which mapping rules produced a version's ABox:
    # without it the same TBox under different rules gives different ABoxes and nothing says
    # so (plan_reglas_de_mapeo.md).
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(versions)")}
    if "rules_hash" not in columns:
        conn.execute("ALTER TABLE versions ADD COLUMN rules_hash TEXT")
    conn.commit()


def logical_axioms(graph: Graph) -> set[str]:
    """Canonical N-Triples of the logical content: blank nodes canonicalized, annotations
    dropped, order irrelevant because the result is a set."""
    logical = Graph()
    for subject, predicate, obj in graph:
        if predicate not in ANNOTATION_PREDICATES:
            logical.add((subject, predicate, obj))
    canonical = to_canonical_graph(logical)
    return {line.strip() for line in canonical.serialize(format="nt").splitlines()
            if line.strip()}


def state_hash(graph: Graph) -> str:
    material = "\n".join(sorted(logical_axioms(graph)))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def diff(before: Graph, after: Graph) -> Diff:
    """Semantic, not textual: a reordered serialization is not a change, and a rename shows
    up as a label change rather than as axiom churn."""
    old, new = logical_axioms(before), logical_axioms(after)
    return Diff(
        added=sorted(new - old),
        removed=sorted(old - new),
        labels_changed=sorted(_labels(after) ^ _labels(before)),
    )


def _labels(graph: Graph) -> set[str]:
    return {
        f"{subject} {predicate} {obj}"
        for subject, predicate, obj in graph
        if predicate in ANNOTATION_PREDICATES
    }


# Vocabulary IRIs have no label in the ontology and never will; a prefix reads better than a
# truncation for them.
WELL_KNOWN_PREFIXES = {
    str(OWL): "owl", str(RDF): "rdf", str(RDFS): "rdfs", str(SKOS): "skos",
    str(DC): "dc", str(DCTERMS): "dcterms", str(XSD): "xsd",
}


def short_name(iri: str, labels: dict[str, str]) -> str:
    """How an IRI is written when a diff is read by a person: its label if the ontology gives
    one, a prefixed name for standard vocabulary, and a truncation as the last resort."""
    label = labels.get(iri)
    if label is not None:
        return label
    for namespace, prefix in WELL_KNOWN_PREFIXES.items():
        if iri.startswith(namespace):
            return f"{prefix}:{iri[len(namespace):]}"
    tail = iri.rstrip("/#").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return f"…{tail}"


def label_index(*graphs: Graph) -> dict[str, str]:
    """IRI to preferred label, across every graph given.

    A0.1 mints opaque IRIs, so a diff printed as raw IRIs is unreadable by construction. The
    labels come from both sides of the comparison: a class removed in the newer version still
    has to be nameable, and its label only exists in the older one.
    """
    index: dict[str, str] = {}
    for graph in graphs:
        for predicate in (SKOS.prefLabel, RDFS.label):
            for subject, _, obj in graph.triples((None, predicate, None)):
                key = str(subject)
                if key not in index or predicate == SKOS.prefLabel:
                    index[key] = str(obj)
    return index


def diff_with_parent(
    conn: sqlite3.Connection, version_id: str
) -> tuple[Version, Diff] | None:
    """What a version changed against the state it came from.

    A new ontology is published whole, not as a delta — every version stores its full Turtle.
    This is the reading aid: the whole artifact answers "what is the ontology now", the diff
    answers "what did this iteration do", and only the second one is reviewable. Returns None
    for a root version, which has nothing to be compared against.
    """
    install(conn)
    version, graph = load(conn, version_id)
    if version.parent_id is None:
        return None
    parent, parent_graph = load(conn, version.parent_id)
    return parent, diff(parent_graph, graph)


def commit(
    conn: sqlite3.Connection,
    graph: Graph,
    *,
    version_id: str,
    parent_id: str | None = None,
    iteration: int = 0,
    branch_id: str | None = None,
    note: str = "",
) -> Version:
    install(conn)
    version = Version(
        id=version_id,
        state_hash=state_hash(graph),
        parent_id=parent_id,
        iteration=iteration,
        branch_id=branch_id,
        note=note,
    )
    conn.execute(
        "INSERT INTO versions (id, parent_id, iteration, branch_id, state_hash, turtle, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version.id, parent_id, iteration, branch_id, version.state_hash,
            graph.serialize(format="turtle"), note, _now(),
        ),
    )
    conn.commit()
    return version


def load(conn: sqlite3.Connection, version_id: str) -> tuple[Version, Graph]:
    install(conn)
    row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise KeyError(f"no version {version_id!r}")
    version = Version(
        id=row["id"], state_hash=row["state_hash"], parent_id=row["parent_id"],
        iteration=row["iteration"], branch_id=row["branch_id"], note=row["note"] or "",
    )
    return version, Graph().parse(data=row["turtle"], format="turtle")


def record_rules(conn: sqlite3.Connection, version_id: str, rules_hash: str) -> bool:
    """Stamp a version with the mapping rules its ABox was regenerated under.

    Returns whether this changed anything: a version already stamped with the same hash has
    nothing to regenerate, and that is the check that makes regeneration idempotent rather
    than merely deterministic.
    """
    install(conn)
    row = conn.execute(
        "SELECT rules_hash FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no version {version_id!r}")
    if row["rules_hash"] == rules_hash:
        return False
    conn.execute(
        "UPDATE versions SET rules_hash = ? WHERE id = ?", (rules_hash, version_id)
    )
    conn.commit()
    return True


def find_by_hash(conn: sqlite3.Connection, hash_value: str) -> Version | None:
    """Loop detection: if the resulting state's hash is already in the DAG, the branch is a
    return to an existing version, not a novelty. Returning is allowed — but explicitly."""
    install(conn)
    row = conn.execute(
        "SELECT * FROM versions WHERE state_hash = ? ORDER BY created_at LIMIT 1",
        (hash_value,),
    ).fetchone()
    if row is None:
        return None
    return Version(
        id=row["id"], state_hash=row["state_hash"], parent_id=row["parent_id"],
        iteration=row["iteration"], branch_id=row["branch_id"], note=row["note"] or "",
    )


def nearest_state(
    conn: sqlite3.Connection, graph: Graph, threshold: float
) -> tuple[Version, float] | None:
    """The case the exact hash misses: the branch returns *almost* to an earlier state — same
    modelling commitment, different IRIs. Jaccard distance over the normalized axiom sets.
    Less clean than the hash; it covers what the hash cannot."""
    install(conn)
    axioms = logical_axioms(graph)
    best: tuple[Version, float] | None = None
    for row in conn.execute("SELECT * FROM versions"):
        other = logical_axioms(Graph().parse(data=row["turtle"], format="turtle"))
        union = axioms | other
        distance = 1.0 - (len(axioms & other) / len(union)) if union else 0.0
        if distance <= threshold and (best is None or distance < best[1]):
            version = Version(
                id=row["id"], state_hash=row["state_hash"], parent_id=row["parent_id"],
                iteration=row["iteration"], branch_id=row["branch_id"], note=row["note"] or "",
            )
            best = (version, distance)
    return best


def lineage(conn: sqlite3.Connection, version_id: str) -> list[str]:
    """Path back to the root. The DAG keeps unchosen branches, so this is one path, not the
    whole history."""
    install(conn)
    path = []
    current: str | None = version_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        path.append(current)
        row = conn.execute(
            "SELECT parent_id FROM versions WHERE id = ?", (current,)
        ).fetchone()
        current = row["parent_id"] if row else None
    return path
