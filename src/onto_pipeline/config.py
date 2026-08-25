"""Central configuration (spec section 7)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Paths(BaseModel):
    corpus_root: Path
    seed_ontology: Path
    work_dir: Path = Path("data")


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


class Matching(BaseModel):
    auto_merge_threshold: float = 0.92
    grey_zone_lower: float = 0.70
    cross_language_always_grey: bool = True
    blocking_strategy: str = "embedding"
    respect_declared_haskey: bool = True


class Iteration(BaseModel):
    mode: str = "global"
    cache_extraction: bool = True
    trigger: str = "manual"
    batch_size: int = 5
    max_iterations: int = 20


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


class Llm(BaseModel):
    provider: str = "none"
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


class Config(BaseModel):
    paths: Paths
    owl_profile: OwlProfile = OwlProfile()
    reasoner: Reasoner = Reasoner()
    upper_ontology: str = "none"
    parser: Parser = Parser()
    classification: Classification = Classification()
    boilerplate: Boilerplate = Boilerplate()
    matching: Matching = Matching()
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
        return config
