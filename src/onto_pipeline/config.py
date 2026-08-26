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


class Seed(BaseModel):
    base_iri: str = "https://ontology.local/id/"
    label_divergence_threshold: float = 0.8


# What candidate generation implements. Both are real; `embedding` is spec 6.2's.
BLOCKING_STRATEGIES = frozenset({"embedding", "surface_and_keys"})


class Matching(BaseModel):
    auto_merge_threshold: float = 0.92
    grey_zone_lower: float = 0.70
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


class CompetencyQuestions(BaseModel):
    n_candidates: int = 60
    type_quota: dict[str, int] = Field(default_factory=dict)
    sparql_regeneration: str = "on_class_change"
    target_pass_rate: float = 0.90


class Branching(BaseModel):
    max_branches: int = 5
    present_independent_axes_separately: bool = True
    auto_apply_when_no_axis: bool = True


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
    classification: Classification = Classification()
    boilerplate: Boilerplate = Boilerplate()
    matching: Matching = Matching()
    mapping: Mapping = Mapping()
    iteration: Iteration = Iteration()
    cq: CompetencyQuestions = CompetencyQuestions()
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
