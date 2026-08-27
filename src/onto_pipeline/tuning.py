"""Ajuste del matcher (spec 6.3) — el único componente del pipeline que se entrena.

La razón es estructural y el spec la enuncia: esto es clasificación de pares, no generación.
Con cientos de ejemplos etiquetados un cross-encoder mejora de forma medible; un generador no.

**Qué corrige, exactamente.** El bi-encoder recupera; el cross-encoder reordena lo que aquél
trajo. Así que el techo de esta etapa es la diferencia entre el acierto en el primer puesto y el
acierto en los primeros k, y conviene medirla antes de entrenar nada: sobre CRAFT es +10,9
puntos y sobre MaterioMiner +18,5. Lo que no está en el top-k no lo alcanza ningún
reordenamiento.

**Los negativos son los candidatos equivocados que el propio bi-encoder puso arriba**, no
negativos al azar. Un negativo al azar es una clase que el recuperador nunca iba a proponer, y
entrenar contra eso enseña a distinguir lo que ya estaba distinguido. El error que hay que
corregir es el que el sistema comete.

**La partición es por documento, nunca por mención.** Dos menciones del mismo paper comparten
vocabulario, autores y tema; separarlas al azar deja la respuesta del lado del entrenamiento y
mide memoria.

**No transfiere entre dominios, y está medido.** Entrenado sobre tres documentos de mecánica de
materiales rinde +10,6 puntos sobre ese dominio; entrenado sobre setenta y siete de biomedicina
rinde +2,1 sobre el mismo. O sea que las etiquetas tienen que salir del dominio en el que se va
a usar — que es exactamente de dónde el spec dice que salen: de las decisiones de aceptar o
rechazar del usuario.

**Ajuste completo y no LoRA**, apartándose de la letra del spec. LoRA existe para no tocar todos
los pesos de un modelo grande; acá el modelo tiene 33 millones de parámetros y entrena en 79
segundos en una GPU de notebook. Agregar `peft` para evitar un costo que no existe sería
complejidad sin contrapartida. La sustancia —ajustar el re-ranker con las etiquetas que el
proceso produce solo— es la misma.
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


def train(
    examples: Sequence[Example], base_model: str, *, epochs: int = 1, batch_size: int = 64,
    max_length: int = 64, device: str | None = None,
):
    """Devuelve el cross-encoder ajustado. Importa adentro para que el resto del módulo se
    pueda leer, testear y documentar sin el extra instalado."""
    try:
        from sentence_transformers import CrossEncoder, InputExample
        from torch.utils.data import DataLoader
    except ImportError as exc:  # pragma: no cover - el extra es opcional
        raise TrainerUnavailable(str(exc)) from exc

    model = CrossEncoder(base_model, num_labels=1, max_length=max_length, device=device)
    loader = DataLoader(
        [InputExample(texts=[e.mention, e.target], label=e.label) for e in examples],
        shuffle=True, batch_size=batch_size,
    )
    model.fit(
        loader, epochs=epochs, warmup_steps=min(100, len(examples) // batch_size),
        show_progress_bar=False,
    )
    return model


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


def save(model, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    model.save(str(directory))
    return directory
