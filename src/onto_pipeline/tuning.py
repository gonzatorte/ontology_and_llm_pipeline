"""Ajuste del matcher (spec 6.3) — el único componente del pipeline que se entrena.

La razón es estructural y el spec la enuncia: esto es clasificación de pares, no generación.
Con cientos de ejemplos etiquetados un cross-encoder mejora de forma medible; un generador no.

**Qué corrige, exactamente.** El bi-encoder recupera; el cross-encoder reordena lo que aquél
trajo. Así que el techo de esta etapa es la diferencia entre el acierto en el primer puesto y el
acierto en los primeros k, y conviene medirla antes de entrenar nada: sobre CRAFT es +10,9
puntos y sobre MaterioMiner +18,5. Lo que no está en el top-k no lo alcanza ningún
reordenamiento.

**Los negativos son las clases que el bi-encoder puso entre las `top_k` primeras y no eran la
correcta.** Ordena todas las clases del inventario por parecido con la mención y se queda con
diez; nueve están mal, y ésas son exactamente las confusiones que el sistema comete. Una clase
tomada al azar es una que el recuperador nunca iba a proponer, y entrenar contra eso enseña a
distinguir lo que ya estaba distinguido.

**La partición es por documento, nunca por mención.** Dos menciones del mismo paper comparten
vocabulario, autores y tema; separarlas al azar deja la respuesta del lado del entrenamiento y
mide memoria.

**Un modelo ajustado sólo sirve en el dominio donde se ajustó, y está medido.** Entrenado sobre
tres documentos de mecánica de
materiales rinde +10,6 puntos sobre ese dominio; entrenado sobre setenta y siete de biomedicina
rinde +2,1 sobre el mismo. O sea que las etiquetas tienen que salir del dominio en el que se va
a usar — que es exactamente de dónde el spec dice que salen: de las decisiones de aceptar o
rechazar del usuario.

**Dos métodos, y el modelo es un parámetro.** El re-ranker que se ajusta sale de la
configuración, no está fijado acá, y de su tamaño depende qué método conviene:

* **completo** — se actualizan todos los pesos. Da el mejor resultado y necesita memoria
  proporcional al modelo. Con el de fábrica (117 millones de parámetros) son 79 segundos en una
  GPU de notebook.
* **LoRA** — los pesos base quedan congelados y se entrenan unas matrices de rango bajo
  inyectadas en la atención. Sobre ese mismo modelo son **443 mil parámetros entrenables, el
  0,38%**. La memoria deja de crecer con el modelo y crece con el adaptador, que es lo que
  permite ajustar uno grande donde el completo no entra.

`method: auto` elige por tamaño: completo por debajo del umbral configurado, LoRA por encima.
La idea es no obligar a decidir en cada corrida ni a pagar el método caro cuando no hace falta,
sin que eso ate el sistema a un modelo chico.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

STAGE = "B2_tuning"


class TrainerUnavailable(RuntimeError):
    """Falta el extra de entrenamiento: `uv sync --extra matching`."""


@dataclass
class Example:
    mention: str
    target: str
    label: float


@dataclass
class Split:
    """Menciones con sus candidatos, de un lado y del otro de la partición."""

    train_mentions: list = field(default_factory=list)
    train_candidates: list[list[str]] = field(default_factory=list)
    eval_mentions: list = field(default_factory=list)
    eval_candidates: list[list[str]] = field(default_factory=list)


@dataclass
class Comparison:
    """Lo que hay que reportar para que el número signifique algo: antes, después y techo."""

    n: int
    base_at_1: float
    base_at_5: float
    tuned_at_1: float
    tuned_at_5: float
    ceiling: float

    @property
    def gain(self) -> float:
        return self.tuned_at_1 - self.base_at_1

    @property
    def headroom_taken(self) -> float:
        """Qué fracción del margen disponible capturó. Es la cifra honesta: subir 9 puntos
        cuando había 11 es otra cosa que subir 9 cuando había 40."""
        available = self.ceiling - self.base_at_1
        return self.gain / available if available > 0 else 0.0


def split_by_document(documents: Sequence, fraction: float, seed: int = 0) -> tuple[list, list]:
    """Documentos de entrenamiento y de evaluación, barajados con semilla.

    Al menos uno de cada lado: con cuatro documentos, `fraction` redondeado hacia abajo puede
    dejar la evaluación vacía y el resultado sería un número sin conjunto de prueba.
    """
    shuffled = list(documents)
    random.Random(seed).shuffle(shuffled)
    cut = min(max(1, round(len(shuffled) * fraction)), len(shuffled) - 1)
    return shuffled[:cut], shuffled[cut:]


def examples_from(
    mentions: Sequence, candidates: Sequence[Sequence[str]], texts: dict[str, str],
    *, negatives: int = 4, seed: int = 0,
) -> list[Example]:
    """Un positivo por mención y `negatives` de los candidatos equivocados que el bi-encoder
    puso arriba."""
    generator = random.Random(seed)
    built: list[Example] = []
    for mention, offered in zip(mentions, candidates, strict=True):
        gold = getattr(mention, "gold_class", None)
        if gold is None or gold not in texts:
            continue
        built.append(Example(mention.text, texts[gold], 1.0))
        wrong = [iri for iri in offered if iri != gold and iri in texts]
        for iri in generator.sample(wrong, min(negatives, len(wrong))):
            built.append(Example(mention.text, texts[iri], 0.0))
    return built


FULL = "full"
LORA = "lora"
AUTO = "auto"
METHODS = frozenset({FULL, LORA, AUTO})


@dataclass
class Trained:
    model: object
    method: str
    total_params: int
    trainable_params: int

    @property
    def trainable_fraction(self) -> float:
        return self.trainable_params / self.total_params if self.total_params else 0.0


def choose_method(method: str, total_params: int, full_max_params: int) -> str:
    """`auto` decide por tamaño; cualquier otro valor se respeta tal cual.

    El umbral no es una verdad sobre los modelos, es una política sobre esta máquina: por debajo
    el ajuste completo entra y da más, por encima deja de entrar. Configurable por eso mismo.
    """
    if method not in METHODS:
        raise ValueError(f"method es {' | '.join(sorted(METHODS))}, no {method!r}")
    if method != AUTO:
        return method
    return FULL if total_params <= full_max_params else LORA


def train(
    examples: Sequence[Example], base_model: str, *, epochs: int = 1, batch_size: int = 64,
    max_length: int = 64, device: str | None = None, method: str = AUTO,
    full_max_params: int = 150_000_000, lora_rank: int = 16, lora_alpha: int = 32,
    lora_dropout: float = 0.05, lora_epochs: int | None = None,
) -> Trained:
    """El cross-encoder ajustado, con qué método y cuánto se entrenó.

    Importa adentro para que el resto del módulo se pueda leer, testear y documentar sin el
    extra instalado.
    """
    try:
        from sentence_transformers import CrossEncoder, InputExample
        from torch.utils.data import DataLoader
    except ImportError as exc:  # pragma: no cover - el extra es opcional
        raise TrainerUnavailable(str(exc)) from exc

    model = CrossEncoder(base_model, num_labels=1, max_length=max_length, device=device)
    total = sum(parameter.numel() for parameter in model.model.parameters())
    resolved = choose_method(method, total, full_max_params)

    if resolved == LORA:
        _add_lora(model, lora_rank, lora_alpha, lora_dropout)
        # LoRA mueve el 0,25% de los pesos, así que necesita bastantes más pasos para llegar a
        # donde el ajuste completo llega en una época. Medido sobre MaterioMiner: con una época
        # da -6,3 puntos —peor que no ajustar—, con cuatro -5,4, y recién con doce da +7,0.
        # Dejarle el mismo número que al completo es la forma de concluir que "LoRA no sirve".
        epochs = lora_epochs if lora_epochs is not None else max(epochs, 12)

    loader = DataLoader(
        [InputExample(texts=[e.mention, e.target], label=e.label) for e in examples],
        shuffle=True, batch_size=batch_size,
    )
    model.fit(
        loader, epochs=epochs, warmup_steps=min(100, len(examples) // batch_size),
        show_progress_bar=False,
    )
    trainable = sum(
        parameter.numel() for parameter in model.model.parameters() if parameter.requires_grad
    )
    return Trained(model=model, method=resolved, total_params=total, trainable_params=trainable)


def _add_lora(model, rank: int, alpha: int, dropout: float) -> None:
    """Los pesos base congelados y matrices de rango bajo en la atención.

    Por `CrossEncoder.add_adapter` y no envolviendo el modelo interno a mano: envolverlo rompe
    el entrenamiento de sentence-transformers, que espera encontrar el modelo de HuggingFace
    donde lo dejó. Ésta es la vía que la biblioteca soporta.

    `target_modules=None` deja que `peft` infiera cuáles son según la arquitectura, que es lo
    que hace que esto no quede atado a una familia de modelos. Sólo si no sabe inferirlo se cae
    a los nombres de la familia BERT, que es la del re-ranker por defecto.
    """
    try:
        from peft import LoraConfig
    except ImportError as exc:  # pragma: no cover - el extra es opcional
        raise TrainerUnavailable(
            f"LoRA necesita `peft`: uv sync --extra matching ({exc})"
        ) from exc

    settings = dict(r=rank, lora_alpha=alpha, lora_dropout=dropout, task_type="SEQ_CLS")
    try:
        model.add_adapter(LoraConfig(**settings))
    except (ValueError, TypeError):
        model.add_adapter(LoraConfig(**settings, target_modules=["query", "value"]))


def compare(
    model, mentions: Sequence, candidates: Sequence[Sequence[str]], texts: dict[str, str],
) -> Comparison:
    """Pareado sobre las mismas menciones: es lo único que aísla la variable (spec 6.3)."""
    usable = [
        (mention, list(offered))
        for mention, offered in zip(mentions, candidates, strict=True)
        if getattr(mention, "gold_class", None) in texts
    ]
    if not usable:
        return Comparison(0, 0.0, 0.0, 0.0, 0.0, 0.0)

    base1 = base5 = tuned1 = tuned5 = ceiling = 0
    for mention, offered in usable:
        gold = mention.gold_class
        base1 += offered[0] == gold
        base5 += gold in offered[:5]
        ceiling += gold in offered
        scores = model.predict(
            [[mention.text, texts[iri]] for iri in offered], show_progress_bar=False
        )
        ranked = sorted(zip(scores, offered, strict=True), key=lambda item: -item[0])
        reordered = [iri for _, iri in ranked]
        tuned1 += reordered[0] == gold
        tuned5 += gold in reordered[:5]

    total = len(usable)
    return Comparison(
        n=total, base_at_1=base1 / total, base_at_5=base5 / total,
        tuned_at_1=tuned1 / total, tuned_at_5=tuned5 / total, ceiling=ceiling / total,
    )


def save(trained: Trained, directory: Path) -> Path:
    """Guarda un cross-encoder que se puede cargar sin saber cómo se entrenó.

    Con LoRA, el adaptador se **fusiona** contra los pesos base antes de guardar. Así lo que
    queda en disco es un modelo común, que `matching.cross_encoder` levanta apuntándole el
    directorio y sin ninguna rama especial. El costo es el tamaño: un adaptador son unos megas
    y un modelo fusionado pesa lo que pese el modelo, lo que importa cuando haya un ajuste por
    dominio. Está anotado como mejora, no resuelto acá.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    inner = trained.model.model
    if hasattr(inner, "merge_and_unload"):
        trained.model.model = inner.merge_and_unload()
    trained.model.save(str(directory))
    return directory
