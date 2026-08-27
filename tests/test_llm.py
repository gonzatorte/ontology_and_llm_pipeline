from __future__ import annotations

import pytest

from onto_pipeline import glosses
from onto_pipeline.config import Execution, Llm
from onto_pipeline.db import connect
from onto_pipeline.llm import (
    NoProvider,
    ProviderNotConfigured,
    ScriptedModel,
    build,
    settings,
)
from onto_pipeline.llm import run as run_stage
from onto_pipeline.seed import GlossContext
from onto_pipeline.telemetry import Ledger

_ANSWER = '{"en": "A procedure applied within a strategy.", "es": "Un procedimiento aplicado."}'


@pytest.fixture
def ledger(tmp_path):
    return Ledger(connect(tmp_path), Execution(backoff_base_s=0), sleep=lambda _: None)


def context() -> GlossContext:
    return GlossContext(
        iri="https://ontology.local/id/1", label="Technique", kind="class",
        superclasses=["Methodological Strategy"], subclasses=["Document Analysis"],
    )


def test_no_provider_fails_loudly_rather_than_returning_nothing():
    with pytest.raises(ProviderNotConfigured):
        NoProvider().complete("anything", temperature=0.0, tier="small")
    assert isinstance(build(Llm()), NoProvider)


def test_stage_settings_come_from_the_config():
    stage = settings(Llm(), "prep_normalize_glosses")
    assert (stage.tier, stage.temperature) == ("medium", 0.3)
    with pytest.raises(KeyError):
        settings(Llm(), "not_a_stage")


def test_gloss_prompt_is_built_from_the_neighbourhood():
    rendered = glosses.PROMPT.render(**glosses.payload(context()))
    assert "Methodological Strategy" in rendered
    assert "Document Analysis" in rendered
    assert "none declared" in rendered  # no disjointness asserted


def test_a_gloss_stage_run_is_cached_and_metered(ledger):
    model = ScriptedModel([_ANSWER])
    payloads = [("iri1", glosses.payload(context()))]
    stage = settings(Llm(), "prep_normalize_glosses")

    first = run_stage(ledger, model, glosses.PROMPT, stage, payloads, glosses.parse)
    second = run_stage(ledger, model, glosses.PROMPT, stage, payloads, glosses.parse)

    assert first.outputs["iri1"]["en"].startswith("A procedure")
    assert first.executed == 1 and second.cached == 1
    assert len(model.prompts) == 1, "the second run must not call the model again"
    assert first.in_tokens > 0


def test_editing_the_prompt_invalidates_the_cached_glosses(ledger):
    payloads = [("iri1", glosses.payload(context()))]
    stage = settings(Llm(), "prep_normalize_glosses")
    run_stage(ledger, ScriptedModel([_ANSWER]), glosses.PROMPT, stage, payloads, glosses.parse)

    # A version distinct from the production one, so the test does not break every time the
    # real prompt is revised.
    edited = glosses.Prompt(
        stage=glosses.STAGE, version="edited-for-test", template=glosses.PROMPT.template
    )
    model = ScriptedModel([_ANSWER])
    again = run_stage(ledger, model, edited, stage, payloads, glosses.parse)
    assert again.executed == 1 and len(model.prompts) == 1


def test_a_malformed_answer_is_a_unit_failure_not_a_silent_gloss(ledger):
    """A gloss that cannot be parsed is recorded as failed; the stage carries on and the
    failure propagates to the report."""
    payloads = [
        (f"iri{index}", glosses.payload(GlossContext(iri=str(index), label=f"C{index}",
                                                     kind="class")))
        for index in range(20)
    ]
    responses = ["I cannot answer that."] * 3 + [_ANSWER] * 19
    result = run_stage(
        ledger, ScriptedModel(responses), glosses.PROMPT, settings(Llm(), "prep_normalize_glosses"),
        payloads, glosses.parse,
    )
    assert set(result.failures) == {"iri0"}
    assert len(result.outputs) == 19


def test_glosses_are_written_as_bilingual_skos_definitions():
    from rdflib import Graph, URIRef
    from rdflib.namespace import SKOS

    graph = Graph()
    glosses.write(graph, [glosses.Gloss(iri="https://ontology.local/id/1", en="A.", es="Un.")])
    languages = {
        literal.language
        for literal in graph.objects(URIRef("https://ontology.local/id/1"), SKOS.definition)
    }
    assert languages == {"en", "es"}
