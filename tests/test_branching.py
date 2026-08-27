from __future__ import annotations

from onto_pipeline import branching as br
from onto_pipeline.axiomatization import Axiom
from onto_pipeline.db import connect

PARENT = "c:Interview"
LABELS = {PARENT: "Interview", "c:Technique": "Technique"}


def subclass(child: str, parent: str = PARENT) -> Axiom:
    return Axiom(child, "subClassOf", parent)


def situation(children: dict[str, tuple[str, str]], parent: str = PARENT) -> br.Situation:
    """`children` maps a proposal id to its (label, criterion)."""
    proposals = {pid: {"label": label, "criterion": criterion}
                 for pid, (label, criterion) in children.items()}
    minted = {pid: f"c:{pid}" for pid in children}
    return br.Situation(
        proposals=proposals,
        parent_of=dict.fromkeys(children, parent),
        minted=minted,
        axioms=[subclass(iri, parent) for iri in minted.values()],
        labels=LABELS,
    )


# ─────────────────────────  the usual path: no axis  ─────────────────────────


def test_no_axis_means_no_question():
    """Multi-branch is the exceptional path. Asking on every iteration without a real
    conflict is the manual work the pipeline exists to avoid (AUTO-APPLY-WHEN-NO-AXES)."""
    result = br.plan([], [subclass("c:A")])
    assert result.automatic and result.decisions == []


def test_an_axis_with_one_option_is_not_an_axis():
    axis = br.Axis("a", br.KIND_LOGICAL, "?", [br.Option("only", "only")])
    assert not axis.real
    assert br.plan([axis], [subclass("c:A")]).automatic


# ─────────────────────────  logical axes  ─────────────────────────


def test_a_justification_with_no_candidate_is_a_pre_existing_defect():
    """Offering a choice between this iteration's axioms would hide that the ontology was
    already broken before it ran."""
    axioms = [subclass("c:New")]
    conflicts, pre_existing = br.conflict_sets(
        axioms, {"c:Old": [["SubClassOf(<c:Old> <c:Other>)", "DisjointClasses(<c:Old> <c:X>)"]]}
    )
    assert conflicts == [] and pre_existing == ["c:Old"]


def test_the_conflict_set_is_the_justification_restricted_to_the_candidates():
    first, second = subclass("c:A"), subclass("c:B")
    explanation = [
        f"SubClassOf(<{first.subject_iri}> <{PARENT}>)",
        f"SubClassOf(<{second.subject_iri}> <{PARENT}>)",
        "DisjointClasses(<c:A> <c:B>)",          # asserted before, not a candidate
    ]
    conflicts, pre_existing = br.conflict_sets([first, second], {"c:A": [explanation]})
    assert conflicts == [frozenset({first.id, second.id})] and not pre_existing


def test_matching_a_justification_line_needs_both_iris_and_the_keyword():
    axiom = subclass("c:A")
    assert br.mentions_axiom(f"SubClassOf(<c:A> <{PARENT}>)", axiom)
    assert not br.mentions_axiom(f"SubClassOf(<c:A> <{PARENT}>)", subclass("c:B"))
    assert not br.mentions_axiom(f"DisjointClasses(<c:A> <{PARENT}>)", axiom)


def test_the_options_of_a_logical_axis_are_the_minimal_diagnoses():
    """Reiter: the ways out of a conflict are the minimal hitting sets over it."""
    assert br.hitting_sets([frozenset({"x", "y"})]) == [frozenset({"x"}), frozenset({"y"})]


def test_a_diagnosis_is_never_a_superset_of_another():
    diagnoses = br.hitting_sets([frozenset({"x", "y"}), frozenset({"x", "z"})])
    assert frozenset({"x"}) in diagnoses
    assert not any(frozenset({"x"}) < item for item in diagnoses)


def test_conflicts_sharing_an_axiom_become_one_question():
    """They cannot be settled independently, so presenting them apart would be a lie about
    the choice. Three axioms in pairwise conflict: no single one is at fault, and the ways
    out are the three pairs."""
    first, second, third = subclass("c:A"), subclass("c:B"), subclass("c:C")
    pairs = [
        frozenset({first.id, second.id}), frozenset({second.id, third.id}),
        frozenset({first.id, third.id}),
    ]
    axes = br.logical_axes(pairs, [first, second, third], LABELS)
    assert len(axes) == 1
    assert axes[0].governs == frozenset({first.id, second.id, third.id})
    assert len(axes[0].options) == 3


def test_a_conflict_that_shares_nothing_is_its_own_question():
    axioms = [subclass(f"c:{name}") for name in "ABCD"]
    ids = [axiom.id for axiom in axioms]
    groups = br._components([frozenset(ids[:2]), frozenset(ids[2:])])
    assert len(groups) == 2


def test_one_way_out_of_a_conflict_is_not_a_branch():
    """A chain of conflicts through one axiom has a single diagnosis: that axiom. There is
    nothing to choose, so the caller drops it and says so in the log."""
    first, second, third = subclass("c:A"), subclass("c:B"), subclass("c:C")
    axes = br.logical_axes(
        [frozenset({first.id, second.id}), frozenset({second.id, third.id})],
        [first, second, third], LABELS,
    )
    assert axes == []


# ─────────────────────  modelling axes: the catalogue  ─────────────────────


def test_a_qualified_subclass_raises_the_attribute_axis():
    """`RedProduct` as a class or `hasColor red` as a value — consistent either way, and no
    reasoner separates them. This is the axis that justifies the mechanism."""
    axes = br.attribute_as_class(situation({
        "p1": ("Semi-Structured Interview", "the schedule is partly fixed"),
        "p2": ("Structured Interview", "the schedule is fixed"),
    }))
    assert len(axes) == 1 and axes[0].kind == br.KIND_MODELLING
    assert {option.id for option in axes[0].options} == {"as_class", "as_attribute"}
    assert "Interview" in axes[0].question


def test_one_qualified_subclass_is_a_class_not_a_decision():
    axes = br.attribute_as_class(situation({"p1": ("Group Interview", "several at once")}))
    assert axes == []


def test_the_attribute_option_drops_exactly_the_qualified_subclasses():
    axes = br.attribute_as_class(situation({
        "p1": ("Group Interview", "several at once"), "p2": ("Phone Interview", "by phone"),
    }))
    as_attribute = next(o for o in axes[0].options if o.id == "as_attribute")
    assert len(as_attribute.drop) == 2 and not next(
        o for o in axes[0].options if o.id == "as_class"
    ).drop


def test_a_subclass_that_is_not_the_parent_qualified_is_not_the_pattern():
    axes = br.attribute_as_class(situation({
        "p1": ("Focus Group", "several informants"), "p2": ("Questionnaire", "written"),
    }))
    assert axes == []


def test_two_criteria_under_one_parent_raise_the_division_axis():
    """Spec 8.2's own example: the parent is being cut two ways at once, and each option is
    named by a criterion the proposals declared rather than by an invented label."""
    def similarity(left: str, right: str) -> float:
        return 1.0 if left.split()[0] == right.split()[0] else 0.0

    axes = br.division_criterion(situation({
        "p1": ("Phone Interview", "medium is the telephone"),
        "p2": ("Video Interview", "medium is a video call"),
        "p3": ("Diagnostic Interview", "purpose is to diagnose"),
        "p4": ("Screening Interview", "purpose is to screen"),
    }), similarity=similarity)
    assert len(axes) == 1
    assert {option.label for option in axes[0].options} == {
        "medium is a video call", "purpose is to screen"
    }, "the shortest criterion of each group names it"
    assert all("wait for another iteration" in option.why for option in axes[0].options)


def test_the_division_axis_is_skipped_without_an_encoder():
    """Losing the axis is acceptable; guessing at it is not."""
    assert br.division_criterion(situation({
        "p1": ("A Interview", "x"), "p2": ("B Interview", "y"),
        "p3": ("C Interview", "z"), "p4": ("D Interview", "w"),
    })) == []


def test_the_cut_is_taken_from_the_criteria_not_from_a_constant():
    """Two criteria written as full sentences score high against each other whatever they say.
    A fixed threshold would either merge them or split everything; the cut is chosen per
    parent so that neither happens."""
    scores = {
        ("medium-a", "medium-b"): 0.80, ("purpose-a", "purpose-b"): 0.38,
        ("medium-a", "purpose-a"): 0.11, ("medium-a", "purpose-b"): 0.19,
        ("medium-b", "purpose-a"): 0.11, ("medium-b", "purpose-b"): 0.25,
    }

    def similarity(left: str, right: str) -> float:
        return scores.get((left, right)) or scores.get((right, left), 1.0)

    axes = br.division_criterion(situation({
        "p1": ("Telephone Survey", "medium-a"), "p2": ("Video Session", "medium-b"),
        "p3": ("Diagnostic Session", "purpose-a"), "p4": ("Screening Session", "purpose-b"),
    }), similarity=similarity)
    assert len(axes) == 1
    assert {option.label for option in axes[0].options} == {"medium-a", "purpose-a"}


def test_a_split_no_clearer_than_its_surroundings_is_not_a_split():
    """Four criteria that all read alike: the sweep can always find some cut, and taking it
    would invent an axis out of noise."""
    def similarity(left: str, right: str) -> float:
        return 0.90 if left[0] == right[0] else 0.87

    axes = br.division_criterion(situation({
        "p1": ("A One Interview", "a1"), "p2": ("A Two Interview", "a2"),
        "p3": ("B One Interview", "b1"), "p4": ("B Two Interview", "b2"),
    }), similarity=similarity, min_separation=0.10)
    assert axes == []
    assert len(br.division_criterion(situation({
        "p1": ("A One Interview", "a1"), "p2": ("A Two Interview", "a2"),
        "p3": ("B One Interview", "b1"), "p4": ("B Two Interview", "b2"),
    }), similarity=similarity, min_separation=0.02)) == 1


def test_criteria_that_are_all_unrelated_are_not_two_cuts():
    axes = br.division_criterion(situation({
        "p1": ("A Interview", "a"), "p2": ("B Interview", "b"),
        "p3": ("C Interview", "c"), "p4": ("D Interview", "d"),
    }), similarity=lambda left, right: 0.05)
    assert axes == []


def test_a_detector_only_receives_the_options_it_declares():
    """The catalogue is extended by adding a function, so the wiring has to be by signature."""
    axes = br.modelling_axes(situation({
        "p1": ("Group Interview", "several at once"), "p2": ("Phone Interview", "by phone"),
    }), min_group=2, similarity=None, min_separation=0.1)
    assert [axis.kind for axis in axes] == [br.KIND_MODELLING]


def test_one_criterion_under_a_parent_is_not_a_decision():
    axes = br.division_criterion(situation({
        "p1": ("Phone Interview", "medium is the telephone"),
        "p2": ("Video Interview", "medium is a video call"),
        "p3": ("Mail Interview", "medium is the post"),
        "p4": ("Chat Interview", "medium is a chat"),
    }), similarity=lambda left, right: 1.0)
    assert axes == []


def test_the_catalogue_is_enumerated_never_generated():
    """The spec's one prohibition for this stage: no prompt asks for alternatives."""
    source = (br.__file__ and open(br.__file__, encoding="utf-8").read()) or ""
    assert "Prompt(" not in source and "llm" not in source.split("import")[0]
    assert br.UNDETECTED, "a catalogued axis with no detector stays visible as a gap"


# ─────────────────────────  branches and the explosion  ─────────────────────────


def two_axes(coupled: bool) -> list[br.Axis]:
    shared = "x" if coupled else "z"
    return [
        br.Axis("first", br.KIND_MODELLING, "?",
                [br.Option("a", "a"), br.Option("b", "b", drop=frozenset({"x"}))],
                governs=frozenset({"x", "y"})),
        br.Axis("second", br.KIND_MODELLING, "?",
                [br.Option("c", "c"), br.Option("d", "d", drop=frozenset({shared}))],
                governs=frozenset({shared, "w"})),
    ]


def test_independent_axes_are_separate_questions_not_a_cross_product():
    """k independent binary axes are k questions, not 2^k branches."""
    result = br.plan(two_axes(coupled=False), [])
    assert len(result.decisions) == 2
    assert all(len(decision.branches) == 2 for decision in result.decisions)


def test_coupled_axes_are_expanded_together():
    result = br.plan(two_axes(coupled=True), [])
    assert len(result.decisions) == 1 and result.decisions[0].coupled
    assert len(result.decisions[0].branches) == 4


def test_the_branch_ceiling_holds():
    axes = [
        br.Axis(f"axis{index}", br.KIND_MODELLING, "?",
                [br.Option("keep", "keep"), br.Option("drop", "drop", drop=frozenset({"x"}))],
                governs=frozenset({"x"}))
        for index in range(4)
    ]
    result = br.plan(axes, [], max_branches=5)
    assert len(result.decisions[0].branches) == 5


def test_a_branch_adds_everything_its_options_did_not_drop():
    keep, lose = subclass("c:A"), subclass("c:B")
    axis = br.Axis("a", br.KIND_MODELLING, "?", [
        br.Option("both", "both"), br.Option("one", "one", drop=frozenset({lose.id})),
    ], governs=frozenset({lose.id}))
    branches = {b.choices["a"]: b for b in br.plan([axis], [keep, lose]).decisions[0].branches}
    assert set(branches["both"].add_axioms) == {keep.id, lose.id}
    assert set(branches["one"].add_axioms) == {keep.id}


def test_branch_ids_are_stable_across_runs():
    axis = br.Axis("a", br.KIND_MODELLING, "?",
                   [br.Option("x", "x"), br.Option("y", "y", drop=frozenset({"q"}))],
                   governs=frozenset({"q"}))
    first = [b.id for b in br.plan([axis], []).decisions[0].branches]
    again = [b.id for b in br.plan([axis], []).decisions[0].branches]
    assert first == again


# ─────────────────────────────  scoring  ─────────────────────────────


def test_coverage_counts_the_orphans_the_branch_would_type():
    branch = br.Branch("b1", "d1", add_axioms=["a1"])
    result = br.score(branch, support={"a1": ["m1", "m2"], "a2": ["m3", "m4"]},
                      orphan_total=4, entities=1)
    assert result.coverage == 0.5 and result.abox_regen_cost == 2


def test_the_history_dependent_scores_are_absent_in_cold_start_not_zero():
    """Section 11: a zero would rank a branch below one that has merely been seen before."""
    result = br.score(br.Branch("b1", "d1", choices={"a": "x"}), support={},
                      orphan_total=1, entities=0)
    assert result.historical_affinity is None and result.parsimony is None


def test_a_branch_with_more_coverage_ranks_first():
    poor = br.Branch("b1", "d1", score=br.Score(coverage=0.2, reorg_cost=0))
    rich = br.Branch("b2", "d1", score=br.Score(coverage=0.9, reorg_cost=3))
    assert [b.id for b in br.rank([poor, rich], limit=2)] == ["b2", "b1"]


# ─────────────────────────────  persistence  ─────────────────────────────


def stored_decision() -> br.Decision:
    axis = br.Axis("division_criterion:p", br.KIND_MODELLING, "?",
                   [br.Option("by_medium", "medium"), br.Option("by_purpose", "purpose")],
                   governs=frozenset({"a1"}))
    return br.plan([axis], [subclass("c:A")]).decisions[0]


def test_a_branch_records_the_alternatives_it_was_taken_over(tmp_path):
    """Without them the record cannot justify the choice, which is what it is for (8.2)."""
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])

    rows = br.load(conn, "v1")
    assert len(rows) == 2
    import json
    axes = json.loads(rows[0]["axes"])
    assert axes[0]["axis"] == "division_criterion:p"
    assert axes[0]["alternatives"] == ["by_purpose"] or axes[0]["alternatives"] == ["by_medium"]


def test_choosing_one_branch_rejects_its_siblings(tmp_path):
    """What was accepted is in the ontology; what was rejected exists nowhere else (6.7)."""
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    chosen = decision.branches[0]
    br.settle(conn, chosen.id, note="the medium is the cut")

    status = {row["id"]: row["status"] for row in br.load(conn, "v1")}
    assert status.pop(chosen.id) == br.CHOSEN
    assert set(status.values()) == {br.NOT_CHOSEN}


def test_history_is_empty_before_anything_was_decided(tmp_path):
    conn = connect(tmp_path)
    br.persist(conn, "v1", [stored_decision()])
    assert br.history(conn) == {}


def test_a_rejected_option_counts_against_itself(tmp_path):
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id)

    tally = br.history(conn)
    assert sorted(tally.values()) == [-1, 1]


def test_reproposing_against_the_same_version_replaces_the_proposals(tmp_path):
    conn = connect(tmp_path)
    br.persist(conn, "v1", [stored_decision()])
    br.persist(conn, "v1", [stored_decision()])
    assert len(br.load(conn, "v1")) == 2


def test_a_settled_decision_is_not_re_opened_by_proposing_again(tmp_path):
    """Branch ids are deterministic, so a plain replace would overwrite the chosen branch and
    its siblings' rejections — the only record of what was turned down."""
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id)
    br.persist(conn, "v1", [decision])

    assert len(br.history(conn)) == 2
    assert {row["status"] for row in br.load(conn, "v1")} == {br.CHOSEN, br.NOT_CHOSEN}


# ─────────────  el registro de decisiones (ITER-FEEDBACK, esquema GRADED-FEEDBACK)  ─────────────


def settled(tmp_path, **kwargs):
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id, note="porque sí", **kwargs)
    return conn, decision


def rows(conn):
    return [dict(row) for row in conn.execute("SELECT * FROM decisions ORDER BY status")]


def test_choosing_writes_the_d9_record_not_only_the_branch_status(tmp_path):
    """La tabla `decisions` estaba en el esquema con cero filas mientras `branches` guardaba una
    copia más pobre. Una sola."""
    conn, _ = settled(tmp_path)
    assert len(rows(conn)) == 2
    assert {row["status"] for row in rows(conn)} == {br.CHOSEN, br.NOT_CHOSEN}


def test_invalid_is_a_different_signal_from_rejected(tmp_path):
    """"Elegí otra" y "esto no puede ser" son señales de fuerza distinta; colapsadas se pierde
    la segunda, que es la que sirve para descartar de entrada."""
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id, invalid=[decision.branches[1].id])
    assert {row["status"] for row in rows(conn)} == {br.CHOSEN, br.INVALID}


def test_an_invalid_weighs_more_against_its_option_than_a_rejection(tmp_path):
    plain = settled(tmp_path / "a")[0]
    strong = connect(tmp_path / "b")
    decision = stored_decision()
    br.persist(strong, "v1", [decision])
    br.settle(strong, decision.branches[0].id, invalid=[decision.branches[1].id])
    assert min(br.history(strong).values()) < min(br.history(plain).values())


def test_the_axis_is_recorded_as_one_of_the_six_fixed_categories(tmp_path):
    """El eje detectado es de la iteración (`attribute_as_class:6c6b32…`); la categoría fija es
    lo que hace comparables dos decisiones de iteraciones distintas."""
    conn, _ = settled(tmp_path)
    assert {row["axis"] for row in rows(conn)} == {br.DIVISION_CRITERION}
    assert br.DIVISION_CRITERION in br.CATEGORIES


def test_an_unknown_axis_lands_in_a_generic_category_not_an_invented_one():
    assert br.category_of("algo_que_no_existe:abc") == br.SCOPE
    assert br.category_of("attribute_as_class:6c6b") == br.PROPERTY_VS_CLASS


def test_the_comment_travels_with_the_decision(tmp_path):
    """ITER-FEEDBACK dice que es el campo que más rinde en recuperación."""
    conn, _ = settled(tmp_path)
    assert any(row["comment"] == "porque sí" for row in rows(conn))


def test_precedents_come_back_by_category(tmp_path):
    conn, _ = settled(tmp_path)
    found = br.precedents(conn, br.DIVISION_CRITERION)
    assert len(found) == 2
    assert br.precedents(conn, br.TERMINOLOGY) == []


def test_precedents_are_capped(tmp_path):
    conn, _ = settled(tmp_path)
    assert len(br.precedents(conn, br.DIVISION_CRITERION, limit=1)) == 1


def test_the_resulting_state_is_recorded_with_the_decision(tmp_path):
    """Sin él no se puede saber contra qué estado de la ontología se decidió, que es lo que la
    expiración de rechazos va a necesitar."""
    conn, _ = settled(tmp_path, state_hash="sha256:abc")
    assert all(row["ontology_state"] == "sha256:abc" for row in rows(conn))


# ─────────  re-proposición: lo mismo con otros IRIs (forma normal)  ─────────


def with_forms(tmp_path, forms):
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id, note="no va", normal_forms=forms)
    return conn, decision


def test_a_proposal_already_rejected_is_found_by_its_normal_form(tmp_path):
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    forms = {b.id: "Focus Group subClassOf Technique" for b in decision.branches}
    br.settle(conn, decision.branches[0].id, note="ya se probó", normal_forms=forms)
    found = br.already_rejected(conn, "Focus Group subClassOf Technique")
    assert found and found["status"] in (br.NOT_CHOSEN, br.INVALID)


def test_the_chosen_one_is_not_reported_as_rejected(tmp_path):
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id,
              normal_forms={decision.branches[0].id: "elegida", decision.branches[1].id: "otra"})
    assert br.already_rejected(conn, "elegida") is None


def test_invalid_wins_over_rejected_when_both_exist(tmp_path):
    """Lo que interesa es si ya se dijo que estaba mal, no sólo que no se eligió."""
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id, invalid=[decision.branches[1].id],
              normal_forms={b.id: "misma forma" for b in decision.branches})
    assert br.already_rejected(conn, "misma forma")["status"] == br.INVALID


def test_an_empty_normal_form_matches_nothing(tmp_path):
    conn, _ = with_forms(tmp_path, {})
    assert br.already_rejected(conn, "") is None


def test_precedents_come_back_by_similarity(tmp_path):
    decision = stored_decision()
    conn = connect(tmp_path)
    br.persist(conn, "v1", [decision])
    br.settle(conn, decision.branches[0].id, note="porque sí",
              normal_forms={b.id: f"forma {i}" for i, b in enumerate(decision.branches)})

    def similarity(left, right):
        return [[1.0 if a == b else 0.0 for b in right] for a in left]

    found = br.precedents_like(conn, "forma 0", similarity)
    assert [row["normalized_axioms"] for row in found] == ["forma 0"]


def test_a_precedent_below_the_floor_is_not_offered(tmp_path):
    conn, decision = with_forms(tmp_path, {b.id: "algo" for b in stored_decision().branches})
    assert br.precedents_like(conn, "x", lambda left, right: [[0.1] * len(right)]) == []
