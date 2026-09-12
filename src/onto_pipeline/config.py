"""Central configuration (spec section 7)."""

from __future__ import annotations

import collections.abc
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# Con qué se nombra una variable de entorno que pisa la configuración: sección y campo.
ENV_PREFIX = "ONTO_PIPELINE_"


class Paths(BaseModel):
    # Los dos apuntan adentro de `use_cases/`, que es donde vive todo par (corpus, ontología).
    # Que el material sea un symlink a algo de afuera es asunto del caso de uso, no de acá.
    corpus_root: Path
    initial_ontology: Path
    work_dir: Path = Path("data")
    reasoner_lib: Path = Path("lib")
    # Dónde viven los casos de uso: un directorio por (corpus, ontología), cada uno con su
    # `use_case.yml`. Aparte de `corpus_root` porque no es lo mismo el corpus sobre el que se
    # corre el pipeline que los pares publicados contra los que se lo mide.
    use_cases_root: Path = Path("../use_cases")


class Database(BaseModel):
    """Contra qué motor corre el almacén.

    **En runtime es siempre `postgres`**, también en local: SQLite da un escritor y muchos
    lectores, que no alcanza para dos sesiones escribiendo a la vez ni para más de un worker de
    la API. `sqlite` sigue existiendo para los tests, que corren sin servidor, y por eso el
    default del modelo lo sigue siendo; lo que manda en una corrida es `config/default.yaml`, y
    ahí dice `postgres`. Los módulos no saben cuál está activo — eso vive en `store.py`.
    """

    backend: str = "sqlite"
    # Sólo para postgres. Nunca lleva la contraseña en claro si se puede evitar: acepta la forma
    # `postgresql://usuario@host/base` y que el resto salga de `~/.pgpass` o del entorno.
    dsn: str = ""

    @field_validator("backend")
    @classmethod
    def _implemented(cls, value: str) -> str:
        from .store import BACKENDS

        if value not in BACKENDS:
            raise ValueError(
                f"database.backend es {' | '.join(sorted(BACKENDS))}, no {value!r}"
            )
        return value


class Storage(BaseModel):
    """Dónde se guardan los artefactos derivados. Un solo sustrato: S3.

    En la nube el disco del proceso es efímero y todo lo que se escriba derecho ahí desaparece
    cuando el contenedor se recicla, sin avisar. En local es MinIO, que habla la misma API y se
    apunta con `endpoint_url`: correr local contra otra implementación sería probar un código y
    desplegar otro. Los módulos no saben nada de esto — vive en `objectstore.py`.
    """

    backend: str = "s3"
    bucket: str = ""
    # El prefijo permite compartir un bucket entre despliegues.
    prefix: str = ""
    region: str = ""
    # Vacío es AWS. Con valor, cualquier cosa que hable S3: MinIO en local.
    endpoint_url: str = ""

    @field_validator("backend")
    @classmethod
    def _implemented(cls, value: str) -> str:
        from .objectstore import BACKENDS

        if value not in BACKENDS:
            raise ValueError(f"storage.backend es {' | '.join(sorted(BACKENDS))}, no {value!r}")
        return value


class Api(BaseModel):
    """La interfaz REST: cómo escucha y cuántos jobs corre a la vez.

    Todo esto se sobreescribe por entorno (`API-ENV-FIRST`), que es lo que usa la imagen: un
    contenedor no lleva archivo de configuración propio y ECS pasa variables.
    """

    host: str = "0.0.0.0"  # noqa: S104 - en un contenedor, escuchar sólo en loopback es no escuchar
    port: int = 8000
    # Cuántos jobs corren a la vez en el proceso. Más de uno exige `database.backend: postgres`
    # y falla al arrancar si no: SQLite da un escritor y N workers ahí son una cola de esperas.
    # El default es 1 por costo, no por miedo — el mecanismo de `jobs.py` es el mismo para N.
    worker_count: int = 1
    # Cuánto vive una URL firmada. Una hora alcanza para subir un corpus o bajar un export, y no
    # tanto como para que el link sirva de credencial permanente si se filtra.
    presigned_expiry_s: int = 3600
    # De qué variable de entorno sale el token de `X-Auth-Key`. El token nunca está en el
    # archivo de configuración: los secretos se leen del entorno, como la credencial del modelo.
    auth_key_env: str = "ONTO_PIPELINE_API_KEY"
    # Cada cuánto mira la cola un worker que no tiene nada que hacer.
    poll_s: float = 0.5


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
    """ITER-BRIDGE — world-knowledge bridging, between matching and induction."""

    n_candidates: int = 5
    # A surface whose best class falls below this gets no candidates and is not asked about:
    # offering five classes none of which is close invites the forced connection the prompt
    # warns against. Below the matcher's own grey floor on purpose — the point of the stage is
    # to reach relations the encoder could not see.
    min_candidate_score: float = 0.45


class Induction(BaseModel):
    # Single-link over the orphan mentions: a concept's phrasings form a chain, so two ends
    # need only be close to something between them. Uncalibrated, like every other threshold
    # here — see technical_debt.md.
    similarity_threshold: float = 0.75
    min_support: int = 3          # one mention proposing a class is noise (ITER-BRANCH)
    max_phrases_in_prompt: int = 30
    # Desde qué parecido con una clase existente una propuesta es un duplicado — o sea, sus
    # menciones eran falsos huérfanos. No se descarta sola: es el diagnóstico del matcher que la
    # compuerta de BUILD-NO-GO-GATE pide. Medido: `Test specimen` dio 1,00 y `Grain boundary` 0,99.
    redundant_threshold: float = 0.90


class Enrichment(BaseModel):
    """ITER-AXIOMATIZE-ENRICH — gloss enrichment from definitional passages
    (ITER-AXIOMATIZE, PREP-NORMALIZE).
    """

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


class InitialOntology(BaseModel):
    """La ontología que se enriquece, y cómo se la normaliza (`PREP-NORMALIZE`).

    Se llamaba «ontología inicial». El nombre decía de dónde parte y no qué es: lo que entra es una
    ontología a la que el corpus le agrega clases y glosas, y sale otra versión de ella.
    """

    base_iri: str = "https://ontology.local/id/"
    label_divergence_threshold: float = 0.8


# What candidate generation implements. Both are real; `embedding` is ITER-MATCH's.
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
    """Ajuste del re-ranker (ITER-TUNE). El modelo que se ajusta es `matching.cross_encoder`."""

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
    # Cuántas reordena el cross-encoder al usarse.
    top_k: int = 10
    # De cuántas se sacan los negativos al entrenar. Más ancho que `top_k` a propósito: ver
    # sólo las que va a tener que puntuar generaliza peor. Medido: +11,2 contra +10,4.
    negative_pool: int = 50
    train_fraction: float = 0.8

    @field_validator("method")
    @classmethod
    def _implemented(cls, value: str) -> str:
        if value not in ("auto", "full", "lora"):
            raise ValueError(f"tuning.method es auto | full | lora, no {value!r}")
        return value


class Stopping(BaseModel):
    """When a round is exhausted, and when the ontology is enough (EVAL-STOPPING)."""

    # New concepts per document over the last `novelty_window` documents, below which the
    # corpus is saturated. Saturated *with respect to the corpus* — never to the domain.
    novelty_window: int = 5
    novelty_threshold: float = 1.0
    # `cq.target_pass_rate` is the primary criterion and `iteration.max_iterations` the hard
    # one; neither is repeated here, so there is one place to change each.


class Mapping(BaseModel):
    """How the mention layer becomes an ABox (mapping_rules_plan.md).

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
    prep_normalize_labels: StageModel = StageModel(tier="small", temperature=0.0)
    prep_normalize_glosses: StageModel = StageModel(tier="medium", temperature=0.3)
    prep_cq_generated: StageModel = StageModel(tier="large", temperature=0.3)
    iter_extract: StageModel = StageModel(tier="medium", temperature=0.0)
    iter_corefer: StageModel = StageModel(tier="medium", temperature=0.0)
    iter_match: StageModel = StageModel(tier="small", temperature=0.0)
    iter_bridge: StageModel = StageModel(tier="large", temperature=0.3)
    iter_induce: StageModel = StageModel(tier="large", temperature=0.3)
    iter_axiomatize: StageModel = StageModel(tier="large", temperature=0.7)
    iter_axiomatize_enrich: StageModel = StageModel(tier="medium", temperature=0.3)
    iter_validate_ontoclean: StageModel = StageModel(tier="large", temperature=0.0)
    iter_branch: StageModel = StageModel(tier="large", temperature=0.3)
    regeneration_retry: StageModel = StageModel(tier="large", temperature=0.7)


class Execution(BaseModel):
    max_retries: int = 3
    backoff_base_s: float = 2
    stage_failure_rate_abort: float = 0.10
    request_timeout_s: float = 300


class Config(BaseModel):
    paths: Paths
    database: Database = Database()
    storage: Storage = Storage()
    api: Api = Api()
    owl_profile: OwlProfile = OwlProfile()
    reasoner: Reasoner = Reasoner()
    upper_ontology: str = "none"
    parser: Parser = Parser()
    initial_ontology: InitialOntology = InitialOntology()
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
        config.paths.initial_ontology = (base / config.paths.initial_ontology).resolve()
        config.paths.work_dir = (base / config.paths.work_dir).resolve()
        config.paths.reasoner_lib = (base / config.paths.reasoner_lib).resolve()
        config.paths.use_cases_root = (base / config.paths.use_cases_root).resolve()
        config.apply_environment()
        return config

    def apply_environment(
        self, environ: collections.abc.Mapping[str, str] | None = None
    ) -> list[str]:
        """Sobreescribir con lo que diga el entorno, y decir qué cambió.

        `ONTO_PIPELINE_API_PORT`, `ONTO_PIPELINE_STORAGE_BUCKET`: sección y campo, en mayúsculas.
        Existe por `API-ENV-FIRST` — la imagen no lleva archivo de configuración propio y lo que
        ECS pasa son variables—, pero vale para cualquier corrida, porque tener dos mecanismos
        según quién arranca es cómo el despliegue termina corriendo con otra configuración que la
        que se probó.

        El valor pasa por la validación del campo, así que un `storage.backend` inventado se
        rechaza acá igual que en el archivo. Los secretos no entran por acá: el token y la
        credencial del modelo se leen del entorno directo y nunca viven en la configuración.
        """
        environ = os.environ if environ is None else environ
        applied = []
        for section, model in self:
            if not isinstance(model, BaseModel):
                continue
            for field in type(model).model_fields:
                name = f"{ENV_PREFIX}{section}_{field}".upper()
                if name not in environ:
                    continue
                # `model_validate` sobre un solo campo: valida el tipo y corre el validador que
                # tenga, que es lo que hace que esto no sea una puerta de atrás a la validación.
                patched = type(model).model_validate(
                    {**model.model_dump(), field: environ[name]}
                )
                setattr(model, field, getattr(patched, field))
                applied.append(f"{section}.{field}")
        return applied
