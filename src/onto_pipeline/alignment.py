"""¿El corpus habla de lo que la semilla nombra? (DEBT-QUALITATIVE-PAIR)

Un par (ontología, corpus) desalineado no se ve mirando la tasa de huérfanas: da alta, y "alta"
es también lo que da un matcher malo sobre un par bien alineado. Las dos causas piden cosas
opuestas —cambiar el corpus, o arreglar el matcher— y confundirlas cuesta semanas.

Lo que sí las distingue es contar. Si el vocabulario que la ontología usa para nombrar sus
clases no aparece en el corpus, ninguna calidad de matcher lo va a encontrar: no hay nada que
encontrar. Es un `grep` y debería correrse **antes** de la primera iteración, no después de
mirar resultados raros.

**Cuenta apariciones literales, y eso es a propósito.** No usa el encoder. Un chequeo que
dependa del mismo componente que se está evaluando no puede dar evidencia sobre él, y además la
pregunta acá es más burda: no es "¿el matcher relaciona bien?" sino "¿la palabra está?".

**Lo que mide y lo que NO, medido.** La primera versión de esto emitía un veredicto sobre la
cobertura global —qué fracción de las etiquetas aparece— y **daba mal**: sobre MaterioMiner, un
par real anotado por expertos, decía "desalineado" con 20% de cobertura, mientras que sobre el
par cualitativo que sí estaba roto decía "alineado" con 50%.

La razón es estructural: una ontología publicada cubre un dominio entero y un corpus cubre una
franja. MaterioMiner tiene 428 clases para toda la mecánica de materiales y cuatro papers, así
que la mayoría de las clases **no tiene por qué** aparecer. Y el par cualitativo llegaba a 50%
por eco léxico — `question`, `subject`, `information` son palabras corrientes—. Se probó
restringir a etiquetas multipalabra, que no deberían aparecer por casualidad, y tampoco separa:
9% contra 18%.

Así que **la cobertura global es diagnóstico, no veredicto**. La pregunta de alineación mira las
menciones, no las clases —¿lo que el corpus nombra tiene clase?— y eso necesita anotaciones o el
matcher, o sea justamente lo que este chequeo quería evitar.

**Donde sí decide es con términos nombrados.** Si alguien dice "este dominio se trata de
`field note`, `informant`, `coding scheme`" y ninguno aparece en 495 mil caracteres, no hay
matcher que lo arregle. La ausencia de un término que se declaró central es concluyente; la
presencia sigue sin serlo.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class Presence:
    label: str
    iri: str
    occurrences: int = 0
    documents: int = 0

    @property
    def absent(self) -> bool:
        return self.occurrences == 0


@dataclass
class Report:
    classes: list[Presence] = field(default_factory=list)
    n_documents: int = 0

    @property
    def absent(self) -> list[Presence]:
        return [item for item in self.classes if item.absent]

    @property
    def single_document(self) -> list[Presence]:
        """Presentes en un solo documento: casi siempre eco léxico o una casualidad."""
        return [item for item in self.classes if item.documents == 1]

    @property
    def coverage(self) -> float:
        if not self.classes:
            return 0.0
        return 1 - len(self.absent) / len(self.classes)

    @property
    def multiword(self) -> list[Presence]:
        """Las etiquetas de más de una palabra, que no aparecen por casualidad. Se reportan
        aparte porque una de una sola palabra puede ser eco léxico."""
        return [item for item in self.classes if len(item.label.split()) > 1]


def _pattern(label: str) -> re.Pattern | None:
    words = [word for word in re.split(r"\W+", label.lower()) if word]
    if not words:
        return None
    # Tolerante al espaciado y a los saltos de línea, porque un PDF parte los sintagmas donde
    # se le termina la columna.
    return re.compile(r"\b" + r"\s+".join(re.escape(word) for word in words) + r"\b")


def check_terms(terms: Sequence[str], documents: Sequence[str]) -> dict[str, int]:
    """Cuántas veces aparece cada término declarado central.

    Es la única parte de este módulo que decide algo: si el que conoce el dominio dice que se
    trata de estas cosas y ninguna está en el corpus, el par no sirve y ninguna calidad de
    matcher lo arregla.
    """
    lowered = [text.lower() for text in documents]
    found: dict[str, int] = {}
    for term in terms:
        pattern = _pattern(term)
        found[term] = (
            sum(len(pattern.findall(text)) for text in lowered) if pattern else 0
        )
    return found


def survey(labels: Sequence[tuple[str, str]], documents: Sequence[str]) -> Report:
    """`labels` son pares (IRI, etiqueta); `documents`, el texto de cada uno."""
    lowered = [text.lower() for text in documents]
    report = Report(n_documents=len(documents))
    for iri, label in labels:
        pattern = _pattern(label)
        presence = Presence(label=label, iri=iri)
        if pattern is not None:
            for text in lowered:
                found = len(pattern.findall(text))
                presence.occurrences += found
                presence.documents += bool(found)
        report.classes.append(presence)
    report.classes.sort(key=lambda item: (item.occurrences, item.label))
    return report
