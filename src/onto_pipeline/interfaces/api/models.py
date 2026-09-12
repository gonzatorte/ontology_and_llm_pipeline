"""Lo que entra y lo que sale, declarado.

Los cuerpos de request son explícitos para que lo que llega por HTTP no se pase como `**kwargs` a
una función de servicio. Los de respuesta son finos a propósito: el resultado de una etapa lo
arma `catalog.summarize`, que sabe qué de un resultado se puede serializar y qué no.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NewUpload(BaseModel):
    name: str = ""
    # Con la forma de un caso de uso: el corpus bajo `corpus/` y la ontología como `ontology.*`.
    files: list[str] = Field(min_length=1)
    note: str = ""


class UploadOut(BaseModel):
    id: str
    name: str
    note: str
    created_at: str
    files: list[str]
    ontology: str | None
    missing: list[str]
    # Sólo al crearlo: el cliente sube contra el almacén, no contra la API.
    put_urls: dict[str, str] | None = None


class NewSession(BaseModel):
    use_case: str = ""
    upload_id: str = ""
    name: str = ""


class SessionOut(BaseModel):
    id: str
    use_case: str
    phase: str
    name: str
    note: str
    created_at: str
    updated_at: str


class StepOut(BaseModel):
    name: str
    command: str
    state: str
    detail: str
    decision: bool


class PlanOut(BaseModel):
    version: str
    steps: list[StepOut]
    # Lo que espera una decisión del usuario. Ninguna interfaz cruza un punto de decisión
    # (invariante 13), así que esto es lo que hay que contestar antes de seguir.
    blocking: list[StepOut]


class RunStage(BaseModel):
    params: dict = Field(default_factory=dict)


class JobOut(BaseModel):
    id: str
    session_id: str
    stage: str
    status: str
    params: dict
    progress: str
    result: dict | None
    warnings: list[str]
    error: str
    created_at: str
    started_at: str | None
    finished_at: str | None


class StageResultOut(BaseModel):
    """Lo que devuelve una etapa síncrona, sin pasar por la cola."""

    stage: str
    result: dict
    warnings: list[str] = Field(default_factory=list)


class GreyAnswer(BaseModel):
    to: str | None = None
    # «Ninguna» es una respuesta de verdad y a menudo la correcta: la mención queda huérfana y
    # llega a la inducción, que es donde un concepto genuinamente nuevo pertenece.
    none_of_these: bool = False
    comment: str = ""
    version: str | None = None


class BranchChoiceIn(BaseModel):
    why: str = ""
    invalid: list[str] = Field(default_factory=list)
    version: str | None = None
    override_structural: bool = False


class ReviewDecision(BaseModel):
    decision: str
    comment: str = ""


class ArtifactOut(BaseModel):
    key: str
    name: str


class ArtifactUrlOut(BaseModel):
    key: str
    url: str
    expires_s: int
