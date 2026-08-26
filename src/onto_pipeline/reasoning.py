"""Reasoning stack: JPype + OWL API, ELK and HermiT in one persistent JVM (spec 9).

owlready2 is not used (D16): it keeps its own world in SQLite plus a translation towards the
OWL API, and two layers managing the same ontology is a source of desynchronization.

Why a persistent JVM rather than ROBOT as a subprocess: HermiT does not produce
justifications. It is a reasoner; the justifications are computed by the OWL API's black-box
explanation algorithm, which uses the reasoner as an oracle — asking it repeatedly whether the
ontology is still inconsistent without this axiom. One justification costs tens or hundreds of
reasoner invocations, not one. That is viable in a resident JVM and not as a subprocess.

Justifications are a requirement, not a luxury: B6 needs to know which subsets of axioms
conflict in order to group branches by decision axis. "It is inconsistent" is not enough.

The ELK asymmetry is the reason this module never returns an "OK":

    ELK does not fail on axioms outside OWL 2 EL — it ignores them and reasons over the
    remaining EL fragment. Logic is monotone, so reasoning over a subset can only weaken the
    conclusions. An inconsistency ELK finds is therefore real; silence from ELK is not
    approval.

Hence REJECTED / INCONCLUSIVE, never OK.
"""

from __future__ import annotations

import glob
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph

REJECTED = "REJECTED"
INCONCLUSIVE = "INCONCLUSIVE"
SKIPPED = "SKIPPED"

PROFILES = ("OWL2_EL", "OWL2_QL", "OWL2_RL", "OWL2_DL", "OWL2_FULL")

_jvm_started = False


class ReasonerUnavailable(RuntimeError):
    """The JVM or the OWL API jars are missing. `scripts/fetch-jars.sh` installs them."""


class InconsistentOntology(RuntimeError):
    """Everything is entailed, so materializing the inferences says nothing."""


@dataclass
class ProfileReport:
    """A0.0. Two uses: routing the reasoner (outside EL, ELK stops being a useful filter) and
    bounding B4, whose proposals are discarded before the reasoner when they leave the
    target profile."""

    detected: str
    el_coverage: float
    violations: dict[str, int] = field(default_factory=dict)


@dataclass
class ElkResult:
    verdict: str  # REJECTED | INCONCLUSIVE | SKIPPED
    unsatisfiable: list[str] = field(default_factory=list)
    coverage: float = 0.0
    note: str = ""


@dataclass
class HermitResult:
    consistent: bool
    unsatisfiable: list[str] = field(default_factory=list)
    justifications: dict[str, list[list[str]]] = field(default_factory=dict)


def start_jvm(lib_dir: Path) -> None:
    """One JVM for the process. Starting it twice is not possible, and every reasoner call
    after the first has to be cheap for justifications to be affordable."""
    global _jvm_started
    if _jvm_started:
        return
    try:
        import jpype
        import jpype.imports  # noqa: F401 - installs the `from org.semanticweb...` hook
    except ImportError as exc:  # pragma: no cover - the extra is optional
        raise ReasonerUnavailable(
            "jpype is not installed; `uv sync --extra reasoning`"
        ) from exc

    jars = sorted(glob.glob(str(Path(lib_dir) / "*.jar")))
    if not jars:
        raise ReasonerUnavailable(f"no jars under {lib_dir}; run scripts/fetch-jars.sh")
    if not jpype.isJVMStarted():
        jpype.startJVM(classpath=jars)
    _jvm_started = True


class Reasoners:
    def __init__(self, lib_dir: Path, *, hermit_timeout_s: int = 120) -> None:
        start_jvm(lib_dir)
        self.hermit_timeout_s = hermit_timeout_s

        from org.semanticweb.owlapi.apibinding import OWLManager

        self._manager = OWLManager.createOWLOntologyManager()

    def load(self, graph: Graph):
        """rdflib graph -> OWLOntology, without a temporary file."""
        from org.semanticweb.owlapi.formats import TurtleDocumentFormat
        from org.semanticweb.owlapi.io import StringDocumentSource
        from org.semanticweb.owlapi.model import IRI

        source = StringDocumentSource(
            graph.serialize(format="turtle"),
            IRI.create("urn:onto-pipeline:candidate"),
            TurtleDocumentFormat(),
            "text/turtle",
        )
        return self._manager.loadOntologyFromOntologyDocument(source)

    def unload(self, ontology) -> None:
        self._manager.removeOntology(ontology)

    def profile(self, ontology) -> ProfileReport:
        from org.semanticweb.owlapi.profiles import (
            OWL2DLProfile,
            OWL2ELProfile,
            OWL2QLProfile,
            OWL2RLProfile,
        )

        checks = {
            "OWL2_EL": OWL2ELProfile(),
            "OWL2_QL": OWL2QLProfile(),
            "OWL2_RL": OWL2RLProfile(),
            "OWL2_DL": OWL2DLProfile(),
        }
        violations = {
            name: int(check.checkOntology(ontology).getViolations().size())
            for name, check in checks.items()
        }
        detected = next(
            (name for name in PROFILES[:-1] if violations.get(name) == 0), "OWL2_FULL"
        )
        return ProfileReport(
            detected=detected,
            el_coverage=self._el_coverage(ontology, checks["OWL2_EL"]),
            violations=violations,
        )

    def _el_coverage(self, ontology, el_profile) -> float:
        """Fraction of logical axioms ELK does not ignore. Below the configured threshold ELK
        stops earning its place in the chain."""
        total = int(ontology.getLogicalAxiomCount())
        if not total:
            return 0.0
        offending = {
            str(violation.getAxiom())
            for violation in el_profile.checkOntology(ontology).getViolations()
            if violation.getAxiom() is not None
        }
        return max(0.0, 1.0 - len(offending) / total)

    def elk(self, ontology, *, coverage_threshold: float) -> ElkResult:
        """Filter, and only ever a filter. An inconsistency here is real; silence is not
        approval, so the verdict is INCONCLUSIVE, never OK."""
        from org.semanticweb.elk.owlapi import ElkReasonerFactory
        from org.semanticweb.owlapi.profiles import OWL2ELProfile

        coverage = self._el_coverage(ontology, OWL2ELProfile())
        if coverage < coverage_threshold:
            return ElkResult(
                verdict=SKIPPED,
                coverage=coverage,
                note=(
                    f"EL coverage {coverage:.0%} is below the {coverage_threshold:.0%} "
                    "threshold; ELK would reason over too small a fragment to filter anything"
                ),
            )

        reasoner = ElkReasonerFactory().createReasoner(ontology)
        try:
            if not reasoner.isConsistent():
                return ElkResult(verdict=REJECTED, coverage=coverage, note="inconsistent")
            unsatisfiable = [
                str(entity.getIRI())
                for entity in reasoner.getUnsatisfiableClasses().getEntitiesMinusBottom()
            ]
        finally:
            reasoner.dispose()

        if unsatisfiable:
            return ElkResult(verdict=REJECTED, coverage=coverage,
                             unsatisfiable=sorted(unsatisfiable))
        return ElkResult(
            verdict=INCONCLUSIVE,
            coverage=coverage,
            note="no violation in the EL fragment; the offending axiom may have been ignored",
        )

    def _hermit_configuration(self):
        from org.semanticweb.owlapi.reasoner import SimpleConfiguration

        return SimpleConfiguration(int(self.hermit_timeout_s * 1000))

    def hermit(self, ontology, *, with_justifications: bool = True) -> HermitResult:
        from org.semanticweb.HermiT import ReasonerFactory

        reasoner = ReasonerFactory().createReasoner(ontology, self._hermit_configuration())
        try:
            consistent = bool(reasoner.isConsistent())
            unsatisfiable = (
                sorted(
                    str(entity.getIRI())
                    for entity in reasoner.getUnsatisfiableClasses().getEntitiesMinusBottom()
                )
                if consistent
                else []
            )
        finally:
            reasoner.dispose()

        result = HermitResult(consistent=consistent, unsatisfiable=unsatisfiable)
        if with_justifications and unsatisfiable:
            result.justifications = {
                iri: self.justify(ontology, iri) for iri in unsatisfiable
            }
        return result

    def inferred_graph(self, graph: Graph) -> Graph:
        """Asserted triples plus what HermiT entails from them.

        SPARQL reads triples, and an entailment is not a triple until something writes it
        down. Without this, a competency question of the inferential type — a mandatory quota
        in spec 4.4, whose stated point is "que el razonador aporte" — can never pass: ask
        whether an Interview is a Technique and the asserted graph says no while the reasoner
        says yes.

        Refuses on an inconsistent ontology, where everything is entailed and the materialized
        graph would be both enormous and meaningless.
        """
        from org.semanticweb.HermiT import ReasonerFactory
        from org.semanticweb.owlapi.formats import TurtleDocumentFormat
        from org.semanticweb.owlapi.io import StringDocumentTarget
        from org.semanticweb.owlapi.util import (
            InferredClassAssertionAxiomGenerator,
            InferredEquivalentClassAxiomGenerator,
            InferredOntologyGenerator,
            InferredPropertyAssertionGenerator,
            InferredSubClassAxiomGenerator,
        )

        ontology = self.load(graph)
        reasoner = ReasonerFactory().createReasoner(ontology, self._hermit_configuration())
        try:
            if not reasoner.isConsistent():
                raise InconsistentOntology(
                    "the ontology is inconsistent, so it entails everything; "
                    "run `validate` and fix that before asking it questions"
                )
            from java.util import ArrayList

            generators = ArrayList()
            for generator in (
                InferredClassAssertionAxiomGenerator(),
                InferredSubClassAxiomGenerator(),
                InferredEquivalentClassAxiomGenerator(),
                InferredPropertyAssertionGenerator(),
            ):
                generators.add(generator)
            target = self._manager.createOntology()
            InferredOntologyGenerator(reasoner, generators).fillOntology(
                self._manager.getOWLDataFactory(), target
            )
            document = StringDocumentTarget()
            self._manager.saveOntology(target, TurtleDocumentFormat(), document)
        finally:
            reasoner.dispose()

        materialized = Graph()
        for triple in graph:
            materialized.add(triple)
        materialized.parse(data=str(document.toString()), format="turtle")
        return materialized

    def incompatible_pairs(
        self, graph: Graph, pairs: Sequence[tuple[str, str]]
    ) -> set[frozenset[str]]:
        """Which of these class pairs cannot both hold of one individual.

        Asked pair by pair rather than by enumerating the ontology's disjointness: what makes
        two classes incompatible is often not an `owl:disjointWith` at all but a combination
        of restrictions, and only the reasoner sees that. Bounded by the caller — the pairs
        that actually collided in the data are a handful, while every pair of classes is
        quadratic and unnecessary.
        """
        from org.semanticweb.HermiT import ReasonerFactory
        from org.semanticweb.owlapi.model import IRI

        ontology = self.load(graph)
        factory = self._manager.getOWLDataFactory()
        reasoner = ReasonerFactory().createReasoner(ontology, self._hermit_configuration())
        incompatible: set[frozenset[str]] = set()
        try:
            if not reasoner.isConsistent():
                # Everything is unsatisfiable, so every pair would come back incompatible.
                # That is not a finding about the pairs; it is a finding about the ontology.
                raise InconsistentOntology(
                    "the ontology is inconsistent, so every pair reads as incompatible; "
                    "run `validate` and fix that first"
                )
            for first, second in pairs:
                expression = factory.getOWLObjectIntersectionOf(
                    factory.getOWLClass(IRI.create(first)),
                    factory.getOWLClass(IRI.create(second)),
                )
                if not reasoner.isSatisfiable(expression):
                    incompatible.add(frozenset((first, second)))
        finally:
            reasoner.dispose()
        return incompatible

    def merged_individuals(self, graph: Graph) -> list[list[str]]:
        """Groups of individuals the reasoner says are the same thing.

        The one way to make a functional property's consequence visible. Declaring a property
        functional by mistake does not raise an inconsistency — it quietly entails `owl:sameAs`
        and merges two entities, so the only way to see it coming is to ask who would merge.
        """
        from org.semanticweb.HermiT import ReasonerFactory

        ontology = self.load(graph)
        reasoner = ReasonerFactory().createReasoner(ontology, self._hermit_configuration())
        groups: list[list[str]] = []
        try:
            if not reasoner.isConsistent():
                return []       # everything is entailed; the answer would be meaningless
            seen: set[str] = set()
            for individual in ontology.getIndividualsInSignature():
                name = str(individual.getIRI())
                if name in seen:
                    continue
                same = sorted(
                    str(other.getIRI())
                    for other in reasoner.getSameIndividuals(individual).getEntities()
                )
                seen.update(same)
                if len(same) > 1:
                    groups.append(same)
        finally:
            reasoner.dispose()
        return sorted(groups)

    def justify(self, ontology, class_iri: str, limit: int = 3) -> list[list[str]]:
        """Minimal axiom sets that make a class unsatisfiable — Reiter's hitting-set tree over
        a black-box explanation, with HermiT as the oracle. Never computed with ELK: a
        justification over the EL fragment can omit the axiom actually at fault."""
        from com.clarkparsia.owlapi.explanation import (
            BlackBoxExplanation,
            HSTExplanationGenerator,
        )
        from org.semanticweb.HermiT import ReasonerFactory
        from org.semanticweb.owlapi.model import IRI

        factory = ReasonerFactory()
        reasoner = factory.createReasoner(ontology, self._hermit_configuration())
        try:
            black_box = BlackBoxExplanation(ontology, factory, reasoner)
            generator = HSTExplanationGenerator(black_box)
            target = self._manager.getOWLDataFactory().getOWLClass(IRI.create(class_iri))
            return [
                sorted(str(axiom) for axiom in explanation)
                for explanation in generator.getExplanations(target, limit)
            ]
        finally:
            reasoner.dispose()
