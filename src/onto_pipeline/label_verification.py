"""PREP-NORMALIZE-LABELS-VERIFY — the semantic half of the label check.

`PREP-NORMALIZE-LABELS` flags a pair whose two spellings do not look alike and stops there:
string similarity cannot tell a divergence from a translation. This asks the model for the
language of every label and for its Spanish and English forms, and then **the code** compares
the translations and decides (VERDICT-IN-CODE). The model is never asked for the verdict: the
example the spec itself uses — `Aplica_una_o_varias` against `appliesTechnique` — survives the
translation and has to stay flagged, and a model asked to judge it directly can close it.

The pass runs over the whole inventory rather than over the flagged pairs alone, because the
language tag feeds the typo lexicons and the matcher's `cross_language_always_grey` rule, not
only the divergence finding.

Nothing here touches the store or the model: payloads in, parsed answers out, verdicts from
pure functions. `services/prep.verify_labels` is what runs it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from . import terms
from .label_overrides import LANGUAGES
from .llm import Prompt

STAGE = "prep_normalize_labels"

PROMPT = Prompt(
    stage=STAGE,
    version="v1",
    template="""You are identifying the language of ontology labels and translating them.

Each numbered line is one label: a short noun phrase or a property name, already split from
its identifier (`appliesTechnique` arrives as `applies Technique`).

{labels}

For each number, answer three things:
- "language": the language the label is written in, "es" or "en". Judge the words, not the
  spelling — Spanish written without accents is still Spanish. Answer "und" when the label is a
  proper name, an acronym or a technical term that belongs to no language in particular
  ("Drosophila", "RNA-seq"), and also when it is spelled the same in both languages and nothing
  in it tells them apart ("control", "material"): do not guess one for it.
- "en": the label in English. If it already is English, repeat it unchanged.
- "es": the label in Spanish. If it already is Spanish, repeat it unchanged.

Translate the term; do not explain it and do not expand it. A translation of the same length
is what makes two labels comparable. Keep the word order of the original.

Answer with JSON only, keyed by number:
{{"1": {{"language": "es", "en": "...", "es": "..."}}, "2": {{...}}}}""",
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class MangledBatch(ValueError):
    """El modelo contestó, y la respuesta no sirve para este lote.

    Tipo propio porque es la **única** falla que se reintenta partiendo el lote: si el problema
    fue del proveedor —un 429, un timeout— partir multiplica las llamadas contra algo que ya
    está rechazando, que es exactamente lo contrario de lo que hay que hacer. El ledger registra
    la falla como `<tipo>: <mensaje>`, así que el nombre de esta clase es lo que distingue un
    caso del otro.
    """


@dataclass(frozen=True)
class Reading:
    """What the model says about one label.

    `parse` devuelve diccionarios y no estas instancias: el ledger guarda la salida como JSON y
    la sirve así en la corrida siguiente, y un tipo que sólo existe en el camino sin caché es un
    tipo que el segundo camino no tiene. Se arma acá, al leerla.
    """

    language: str
    en: str
    es: str

    @classmethod
    def of(cls, value: dict) -> Reading:
        return cls(language=value["language"], en=value["en"], es=value["es"])


@dataclass
class Batch:
    """One ledger unit: the labels that travel in a single call."""

    labels: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """El nombre de la unidad. Del contenido, para que dos corridas con el mismo lote lo
        llamen igual aunque el inventario haya crecido alrededor."""
        digest = hashlib.sha1("\n".join(self.labels).encode("utf-8")).hexdigest()[:12]
        return f"labels-{digest}"


def payload(batch: Batch) -> dict[str, str]:
    numbered = "\n".join(f"{index}. {text}" for index, text in enumerate(batch.labels, start=1))
    return {"labels": numbered}


def parse(text: str, payload: dict[str, str]) -> dict[str, dict]:
    """El lote entero o nada.

    Numerar y exigir la respuesta indexada es lo que permite ver que el modelo descartó ítems.
    Sin esto los descarta en silencio, que es la clase de falla que registra
    FINDINGS-SILENT-FAILURES: el resultado se ve bien y le faltan etiquetas.
    """
    expected = [line.split(". ", 1)[1] for line in payload["labels"].split("\n")]
    match = _JSON_RE.search(text)
    if not match:
        raise MangledBatch(f"no JSON object in the model's answer: {text[:120]!r}")
    data = json.loads(match.group())

    readings: dict[str, dict] = {}
    for index, label in enumerate(expected, start=1):
        entry = data.get(str(index))
        if entry is None:
            raise MangledBatch(f"the answer skips label {index} ({label!r})")
        missing = {"language", "en", "es"} - set(entry)
        if missing:
            raise MangledBatch(f"label {index} is missing {sorted(missing)}")
        language = str(entry["language"]).strip().lower()
        if language not in LANGUAGES:
            raise MangledBatch(
                f"label {index}: {language!r} is not one of {', '.join(LANGUAGES)}"
            )
        readings[label] = {
            "language": language, "en": str(entry["en"]).strip(),
            "es": str(entry["es"]).strip(),
        }
    return readings


# ─────────────────────────────  BATCH-BY-CONTENT  ─────────────────────────────


def batches(labels: list[str], *, size: int) -> list[Batch]:
    """Los lotes, como función del contenido y no de la posición (BATCH-BY-CONTENT).

    La unidad del ledger es el lote, y su clave de caché sale del payload entero: cortando la
    lista de a `size` en el orden en que vienen las entidades, agregar una etiqueta corre todo un
    lugar y se pierde la caché de todos los lotes que siguen. Sobre craft-cl eso es re-pagar
    ~170 llamadas por una palabra.

    Tres reglas. `BATCH-DEDUPE`: la etiqueta se consulta una vez aunque la tengan diez clases,
    porque el idioma y la traducción son propiedad de la cadena. `BATCH-SORT`: el orden sale del
    texto y no de cómo el grafo devolvió las entidades. `BATCH-CUT`: el corte lo decide el hash
    del texto, con tope al doble de `size`, así una etiqueta que entra o cambia invalida su lote
    y ninguno más.
    """
    if size < 1:
        raise ValueError("a batch holds at least one label")
    unique = sorted(set(labels))
    grouped, current = [], Batch()
    for text in unique:
        current.labels.append(text)
        if _is_boundary(text, size) or len(current.labels) >= size * 2:
            grouped.append(current)
            current = Batch()
    if current.labels:
        grouped.append(current)
    return grouped


def _is_boundary(text: str, size: int) -> bool:
    digest = hashlib.sha1(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % size == 0


def is_mangled(error: str) -> bool:
    """Si la falla fue del modelo contestando mal, y no del proveedor.

    Medido, y caro: con el proveedor devolviendo 429 por límite de uso, partir cada lote fallado
    convirtió 4 unidades en 87 —cada mitad vuelve a fallar y vuelve a partirse— contra un
    servicio que ya estaba rechazando. Partir es la respuesta a una respuesta mala, nunca a que
    no haya respuesta.
    """
    return error.startswith(MangledBatch.__name__)


def split(batch: Batch) -> list[Batch]:
    """Un lote que el modelo contestó mal, en dos mitades (VERIFY-3-SPLIT).

    No puede hacerlo el reintento del ledger: ése repite la **misma** unidad con el mismo
    payload, y lo que hay que cambiar es qué etiquetas van juntas. Una sola mal contestada no
    puede llevarse puestas las otras 39.
    """
    if len(batch.labels) < 2:
        return []
    middle = len(batch.labels) // 2
    return [Batch(batch.labels[:middle]), Batch(batch.labels[middle:])]


# ─────────────────────────────  VERDICT-IN-CODE  ─────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """Lo que la traducción dice de un par, y con qué evidencia."""

    similarity: float
    verified: bool
    derived: Reading
    declared: Reading
    declared_text: str

    def as_evidence(self) -> dict:
        return {
            "similarity": round(self.similarity, 3),
            "verified": self.verified,
            "derived": {"en": self.derived.en, "es": self.derived.es},
            "declared": {"text": self.declared_text,
                         "en": self.declared.en, "es": self.declared.es},
        }

    @property
    def comment(self) -> str:
        return (
            f"traducido coinciden ({self.similarity:.2f}): "
            f"«{self.derived.en}» / «{self.declared.en}» · "
            f"«{self.derived.es}» / «{self.declared.es}»"
        )


def verdict(
    derived: str, declared: list[str], readings: dict[str, dict], *, threshold: float
) -> Verdict | None:
    """El máximo sobre las etiquetas declaradas, comparando lengua contra lengua.

    Hay una declarada por cada `rdfs:label`, y el hallazgo afirma que **ninguna** nombra al
    concepto: alcanza con que una lo confirme para que no se sostenga. Devuelve `None` cuando
    falta alguna lectura — un lote fallado deja el hallazgo como estaba, sin inventarle un
    veredicto.
    """
    if derived not in readings:
        return None
    left = Reading.of(readings[derived])
    best = None
    for text in declared:
        if text not in readings:
            continue
        right = Reading.of(readings[text])
        score = max(terms.similarity(left.en, right.en), terms.similarity(left.es, right.es))
        if best is None or score > best.similarity:
            best = Verdict(
                similarity=score, verified=score >= threshold,
                derived=left, declared=right, declared_text=text,
            )
    return best


def languages(readings: dict[str, dict]) -> dict[str, str]:
    """El idioma de cada etiqueta tal como lo guarda la tabla, `und` incluido."""
    return {text: reading["language"] for text, reading in readings.items()}


__all__ = [
    "Batch", "MangledBatch", "PROMPT", "Reading", "STAGE", "Verdict",
    "batches", "is_mangled", "languages", "parse", "payload", "split", "verdict",
]
