"""Central configuration (spec section 7)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class Paths(BaseModel):
    corpus_root: Path
    seed_ontology: Path
    work_dir: Path = Path("data")
    reasoner_lib: Path = Path("lib")
    # Where the calibration pairs live: one directory per (corpus, ontología), each with its
    # own `pair.yml`. Separate from `corpus_root` because they answer different questions —
    # that pair is the case of application, these are the instrument.
    calibration_root: Path = Path("../../calibration")


class OwlProfile(BaseModel):
    detected: str = "auto"
    target: str = "OWL_DL_no_cardinality"


class Reasoner(BaseModel):
    elk_filter_enabled: bool = True
    elk_coverage_threshold: float = 0.7
    hermit_timeout_s: int = 120


class Parser(BaseModel):
    born_digital: str = "pymupdf"
    scan: str = "mineru"
    uncertain: str = "mineru"


class Classification(BaseModel):
    min_visible_chars: int = 100
    garbled_ratio_threshold: float = 0.35
    image_coverage_scan_threshold: float = 0.65


class Boilerplate(BaseModel):
    page_frequency_threshold: float = 0.8
    bbox_tolerance_px: float = 12
    normalize_before_compare: list[str] = Field(default_factory=lambda: ["digits", "ips", "dates"])
    max_block_chars: int = 200


class Chunking(BaseModel):
    target_chars: int = 3000
    max_chars: int = 6000


class Extraction(BaseModel):
    max_mention_words: int = 8


class Bridging(BaseModel):
    """B2b — world-knowledge bridging (spec 6.2b), between matching and induction."""

    n_candidates: int = 5
    # A surface whose best class falls below this gets no candidates and is not asked about:
    # offering five classes none of which is close invites the forced connection the prompt
    # warns against. Below the matcher's own grey floor on purpose — the point of the stage is
    # to reach relations the encoder could not see.
    min_candidate_score: float = 0.45


class Induction(BaseModel):
    # Single-link over the orphan mentions: a concept's phrasings form a chain, so two ends
    # need only be close to something between them. Uncalibrated, like every other threshold
    # here — see DEUDA_TECNICA.md.
    similarity_threshold: float = 0.75
    min_support: int = 3          # one mention proposing a class is noise (spec 6.6)
    max_phrases_in_prompt: int = 30


class Enrichment(BaseModel):
    """B4b — gloss enrichment from definitional passages (spec 6.5, 4.3)."""

    # Passages per class, spread across documents before going deep: five from one paper
    # describe that paper's usage, and the circularity control is about that difference.
    max_passages: int = 6
    max_passage_chars: int = 1200
    # Skip a class with nothing to work from rather than asking the model to invent one.
    min_passages: int = 1


class Axiomatization(BaseModel):
    # Several candidates with their definitions, not the matcher's single runner-up: on real
    # data that one is often unrelated, and the parent has to be re-decided here.
    n_candidates: int = 6


class Seed(BaseModel):
    base_iri: str = "https://ontology.local/id/"
    label_divergence_threshold: float = 0.8


# What candidate generation implements. Both are real; `embedding` is spec 6.2's.
BLOCKING_STRATEGIES = frozenset({"embedding", "surface_and_keys"})


class Matching(BaseModel):
    auto_merge_threshold: float = 0.95   # medido: precisión 97,9% en craft-cl
    grey_zone_lower: float = 0.80        # medido: 82,9% precisión, 17,3% huérfanas falsas
    cross_language_always_grey: bool = True
    blocking_strategy: str = "embedding"
    respect_declared_haskey: bool = True
    bi_encoder: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    cross_encoder: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    use_cross_encoder: bool = False
    match_against: str = "label"   # label | gloss | label_and_gloss
    device: str | None = None

    @field_validator("blocking_strategy")
    @classmethod
    def _implemented_blocking(cls, value: str) -> str:
        """Each value names what candidate generation actually does, and nothing else passes.

        The thresholds are calibrated against these numbers, and a pair that was never formed
        is indistinguishable, in the metrics, from one the encoder scored too low — so a
        strategy that does not exist must not be accepted silently.
        """
        if value not in BLOCKING_STRATEGIES:
            raise ValueError(
                f"blocking_strategy {value!r} is not implemented; "
                f"available: {', '.join(sorted(BLOCKING_STRATEGIES))}."
            )
        return value


class Iteration(BaseModel):
    mode: str = "global"
    cache_extraction: bool = True
    trigger: str = "manual"
    batch_size: int = 5
    max_iterations: int = 20
    # What to do with documents already processed, when something invalidated their cache —
    # a prompt version, a threshold, a chunking change. A document never processed is always
    # processed; this governs re-processing only.
    reload: str = "all"                      # all | none | sample
    reload_sample: float = 0.2
    reload_seed: int = 0


class Tuning(BaseModel):
    """Ajuste del re-ranker (spec 6.3). El modelo que se ajusta es `matching.cross_encoder`."""

    # auto | full | lora. `auto` elige por tamaño: completo mientras entre, LoRA cuando no.
    method: str = "auto"
    # El umbral no es una verdad sobre los modelos, es una política sobre esta máquina.
    full_max_params: int = 150_000_000
    lora_rank: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    epochs: int = 1
    # LoRA entrena una fracción de los pesos y necesita más pasos para llegar al mismo lugar.
    # Medido: con 1 época da -6,3 puntos, con 4 da -5,4, con 12 da +7,0.
    lora_epochs: int = 12
    batch_size: int = 64
    max_length: int = 64
    # Negativos por mención, tomados de las clases que el bi-encoder puso entre las primeras y
    # no eran la correcta — el error que hay que corregir, no uno inventado.
    negatives: int = 4
    top_k: int = 10
    train_fraction: float = 0.8

    @field_validator("method")
    @classmethod
    def _implemented(cls, value: str) -> str:
        if value not in ("auto", "full", "lora"):
            raise ValueError(f"tuning.method es auto | full | lora, no {value!r}")
        return value


class Stopping(BaseModel):
    """When a round is exhausted, and when the ontology is enough (spec 10.3)."""

    # New concepts per document over the last `novelty_window` documents, below which the
    # corpus is saturated. Saturated *with respect to the corpus* — never to the domain.
    novelty_window: int = 5
    novelty_threshold: float = 1.0
    # `cq.target_pass_rate` is the primary criterion and `iteration.max_iterations` the hard
    # one; neither is repeated here, so there is one place to change each.


class Mapping(BaseModel):
    """How the mention layer becomes an ABox (plan_reglas_de_mapeo.md).

    The global policy. Per-case exceptions live in `review_items`, because section 6.4 scopes
    notarize/force/refute per case while 6.8 recomputes the whole ABox, and a per-case decision
    that survives a wholesale recompute cannot live in the code doing the recomputing.
    """

    individual_from: str = "entity"          # entity | mention
    type_from: list[str] = Field(default_factory=lambda: ["auto"])   # auto, grey
    provenance: str = "named_graph"          # named_graph | flat
    conflict_policy: str = "notarize"        # notarize | force | refute
    duplicate_policy: str = "separate"       # separate | merge
    # From how many entities a class pair colliding stops being cases and becomes one question
    # about the TBox (6.4). Contextualizing a property changes the shape of every query over
    # it, the CQs' SPARQL included, so that question belongs to branching and not here.
    conflict_pattern_threshold: int = 3
    # Below this many individuals, a functional-property candidate is not put to the user:
    # the answer would rest on evidence too thin to be worth their attention, and asking
    # anyway trains a person to say yes (6.8).
    functional_min_individuals: int = 5


class CompetencyQuestions(BaseModel):
    n_candidates: int = 60
    type_quota: dict[str, int] = Field(default_factory=dict)
    sparql_regeneration: str = "on_class_change"
    target_pass_rate: float = 0.90


class Branching(BaseModel):
    max_branches: int = 5
    present_independent_axes_separately: bool = True
    auto_apply_when_no_axis: bool = True
    # How many proposals under one parent it takes before a modelling pattern is a decision
    # rather than a class. One qualified subclass is a class; several are a commitment.
    min_group: int = 2
    # The division-criterion axis has no similarity threshold — the cut is chosen per parent.
    # This is how much clearer that cut has to be than the similarities it breaks.
    min_criterion_separation: float = 0.10


class StageModel(BaseModel):
    tier: str
    temperature: float
    # Every model on the configured gateway reasons, and the reasoning is billed as output.
    # Measured: 148 reasoning tokens of 168 for a trivial extraction, dropping to 2 of 22 at
    # "low". Extraction and classification do not need a chain of thought; axiomatization and
    # branching do.
    reasoning_effort: str | None = None      # None | low | medium | high
    max_tokens: int | None = None


class Llm(BaseModel):
    provider: str = "none"
    base_url: str = ""
    api_key_env: str = ""          # the variable name; never the key itself
    models: dict[str, str] = Field(default_factory=dict)   # tier -> model id
    A0_2_labels: StageModel = StageModel(tier="small", temperature=0.0)
    A0_4_glosses: StageModel = StageModel(tier="medium", temperature=0.3)
    A3_cq_generation: StageModel = StageModel(tier="large", temperature=0.3)
    B1_extraction: StageModel = StageModel(tier="medium", temperature=0.0)
    B1b_coreference: StageModel = StageModel(tier="medium", temperature=0.0)
    B2_matching: StageModel = StageModel(tier="small", temperature=0.0)
    B2b_bridging: StageModel = StageModel(tier="large", temperature=0.3)
    B3_naming: StageModel = StageModel(tier="large", temperature=0.3)
    B4_axiomatization: StageModel = StageModel(tier="large", temperature=0.7)
    B4b_enrichment: StageModel = StageModel(tier="medium", temperature=0.3)
    B5_ontoclean: StageModel = StageModel(tier="large", temperature=0.0)
    B6_branching: StageModel = StageModel(tier="large", temperature=0.3)
    regeneration_retry: StageModel = StageModel(tier="large", temperature=0.7)


class Execution(BaseModel):
    max_retries: int = 3
    backoff_base_s: float = 2
    stage_failure_rate_abort: float = 0.10
    request_timeout_s: float = 300


class Config(BaseModel):
    paths: Paths
    owl_profile: OwlProfile = OwlProfile()
    reasoner: Reasoner = Reasoner()
    upper_ontology: str = "none"
    parser: Parser = Parser()
    seed: Seed = Seed()
    chunking: Chunking = Chunking()
    extraction: Extraction = Extraction()
    bridging: Bridging = Bridging()
    induction: Induction = Induction()
    axiomatization: Axiomatization = Axiomatization()
    enrichment: Enrichment = Enrichment()
    classification: Classification = Classification()
    boilerplate: Boilerplate = Boilerplate()
    matching: Matching = Matching()
    mapping: Mapping = Mapping()
    iteration: Iteration = Iteration()
    cq: CompetencyQuestions = CompetencyQuestions()
    stopping: Stopping = Stopping()
    tuning: Tuning = Tuning()
    branching: Branching = Branching()
    llm: Llm = Llm()
    execution: Execution = Execution()

    @classmethod
    def load(cls, path: Path) -> Config:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        config = cls.model_validate(raw)
        # Relative paths resolve against the config file, so the CLI works from any cwd.
        base = Path(path).resolve().parent
        config.paths.corpus_root = (base / config.paths.corpus_root).resolve()
        config.paths.seed_ontology = (base / config.paths.seed_ontology).resolve()
        config.paths.work_dir = (base / config.paths.work_dir).resolve()
        config.paths.reasoner_lib = (base / config.paths.reasoner_lib).resolve()
        config.paths.calibration_root = (base / config.paths.calibration_root).resolve()
        return config
