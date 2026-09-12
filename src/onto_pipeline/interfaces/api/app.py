"""La app y sus rutas.

Cada ruta hace lo mismo: abre su workspace, llama a un servicio, devuelve lo que salió. Lo que
tarda se encola (`API-JOBS`) y lo que contesta en un parpadeo se contesta; quién es cuál lo dice
`services/catalog.py` y no esta capa.

**Lo que el usuario tiene que arreglar viaja en el cuerpo.** `StageError` es 400 con su mensaje,
que es el mismo que el CLI pinta con `rich`. Un bug del servidor es 500 y no lleva traza: el
traceback dice cosas del servidor que no son de quien llamó.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, status
from fastapi.responses import JSONResponse

from ... import artifacts as artifacts_module
from ... import orchestration, sessions
from ...config import Config
from ...services import StageError, Workspace, catalog, evaluate, iterate
from . import auth, jobs, uploads
from .deps import workspace as workspace_for
from .models import (
    ArtifactOut,
    ArtifactUrlOut,
    BranchChoiceIn,
    GreyAnswer,
    JobOut,
    NewSession,
    NewUpload,
    PlanOut,
    ReviewDecision,
    RunStage,
    SessionOut,
    StageResultOut,
    StepOut,
    UploadOut,
)

DEFAULT_CONFIG = Path("config/default.yaml")


def create_app(config_path: Path = DEFAULT_CONFIG, *, start_workers: bool = True) -> FastAPI:
    """Armar la app. Sin token configurado no arranca (`API-AUTH-KEY`)."""
    config = Config.load(config_path)
    expected = auth.token(config.api.auth_key_env)
    protected = [Depends(auth.guard(expected))]
    app = FastAPI(title="onto-pipeline", version="1")
    app.state.config_path = config_path
    app.state.config = config
    app.state.pool = None

    _install_error_handlers(app)
    _install_routes(app, config_path, protected)

    @app.on_event("startup")
    def _start() -> None:
        with workspace_for(config_path) as opened:
            # Lo que quedó en `running` es de un proceso que ya no está: con una tarea fija
            # (`API-ECS-ONE-TASK`) nadie más puede estar corriéndolo. Sin este barrido, el índice
            # parcial deja esa sesión trabada para siempre.
            stale = jobs.sweep_stale(opened.conn)
            if stale:
                print(f"[jobs] {len(stale)} job(s) colgados de una corrida anterior, cerrados")
        if not start_workers:
            return
        pool = jobs.Pool(
            lambda: Workspace.open(config_path).conn,
            _runner(config_path),
            workers=config.api.worker_count,
            poll_s=config.api.poll_s,
            backend=config.database.backend,
        )
        pool.start()
        app.state.pool = pool

    @app.on_event("shutdown")
    def _stop() -> None:
        if app.state.pool is not None:
            app.state.pool.stop()

    return app


def _runner(config_path: Path):
    """Cómo corre un job: su propio workspace, su propia conexión, y limpieza al final."""

    def run(job: jobs.Job, progress) -> dict:
        with workspace_for(config_path, job.session_id) as opened:
            result = catalog.run(opened, job.stage, job.params, progress=progress)
            return catalog.summarize(result)

    return run


# ─────────────────────────────  errores  ─────────────────────────────


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StageError)
    def _stage_error(_request, exc: StageError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST, content={"error": str(exc)}
        )

    @app.exception_handler(jobs.JobInFlight)
    def _in_flight(_request, exc: jobs.JobInFlight) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"error": str(exc)})

    for missing in (sessions.UnknownSession, uploads.UnknownUpload, jobs.UnknownJob):
        app.add_exception_handler(missing, _not_found)


def _not_found(_request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND, content={"error": str(exc).strip("'")}
    )


# ─────────────────────────────  rutas  ─────────────────────────────


def _install_routes(app: FastAPI, config_path: Path, protected: list) -> None:

    @app.get("/healthz")
    def healthz() -> dict:
        """Lo único sin auth: es lo que mira el balanceador, que no tiene token."""
        return {"status": "ok"}

    @app.get("/stages", dependencies=protected)
    def stages() -> dict:
        """Qué etapas hay y cuáles se encolan. Sale del catálogo, que es de la capa de
        servicios: la lista no puede ser de una interfaz."""
        return {
            name: {
                "params": sorted(spec.params),
                "job": spec.long,
                "needs_provider": spec.needs_provider,
                "plan_step": spec.plan_step,
            }
            for name, spec in sorted(catalog.STAGES.items())
        }

    # ── uploads ──

    @app.post("/uploads", dependencies=protected, status_code=status.HTTP_201_CREATED)
    def create_upload(body: NewUpload) -> UploadOut:
        with workspace_for(config_path) as opened:
            try:
                upload = uploads.create(
                    opened.conn, name=body.name, filenames=body.files, note=body.note
                )
            except ValueError as exc:
                raise StageError(str(exc)) from exc
            urls = uploads.put_urls(
                opened.artifacts.store, upload,
                expires_s=opened.config.api.presigned_expiry_s,
            )
            return _upload_out(opened, upload, put_urls=urls)

    @app.get("/uploads", dependencies=protected)
    def list_uploads() -> list[UploadOut]:
        with workspace_for(config_path) as opened:
            return [_upload_out(opened, item) for item in uploads.all_uploads(opened.conn)]

    @app.get("/uploads/{upload_id}", dependencies=protected)
    def get_upload(upload_id: str) -> UploadOut:
        with workspace_for(config_path) as opened:
            return _upload_out(opened, uploads.load(opened.conn, upload_id))

    @app.delete("/uploads/{upload_id}", dependencies=protected)
    def delete_upload(upload_id: str) -> dict:
        """Borrar es **global** (`API-SHARED-UPLOADS`): la respuesta dice qué sesiones quedan sin
        corpus, que es lo que lo vuelve explícito y no un accidente."""
        with workspace_for(config_path) as opened:
            affected = uploads.delete(opened.conn, opened.artifacts.store, upload_id)
            return {"deleted": upload_id, "sessions_left_without_corpus": affected}

    # ── sesiones ──

    @app.post("/sessions", dependencies=protected, status_code=status.HTTP_201_CREATED)
    def create_session(body: NewSession) -> SessionOut:
        """Sobre un caso publicado o sobre un upload: los dos son el par (corpus, ontología) y
        conviven sin distinción (`API-UPLOADED-AND-PUBLISHED`).

        Quién resuelve cuál es distinto, y a propósito: el caso publicado lo valida el servicio,
        que es el que sabe qué hay en `use_cases/`; el upload lo resuelve esta capa, porque el
        core no sabe qué es un upload.
        """
        if bool(body.use_case) == bool(body.upload_id):
            raise StageError("una sesión corre sobre un caso de uso o sobre un upload")
        with workspace_for(config_path) as opened:
            if body.upload_id:
                upload = uploads.load(opened.conn, body.upload_id)
                created = sessions.create(
                    opened.conn, use_case=upload.use_case, name=body.name
                )
            else:
                created = evaluate.new_session(
                    opened, use_case=body.use_case, name=body.name
                )
            return _session_out(created)

    @app.get("/sessions", dependencies=protected)
    def list_sessions() -> list[SessionOut]:
        with workspace_for(config_path) as opened:
            return [_session_out(item) for item in sessions.all_sessions(opened.conn)]

    @app.get("/sessions/{session_id}", dependencies=protected)
    def get_session(session_id: str) -> SessionOut:
        with workspace_for(config_path, session_id) as opened:
            return _session_out(sessions.load(opened.conn, session_id))

    @app.get("/sessions/{session_id}/plan", dependencies=protected)
    def plan(session_id: str, version: str | None = None) -> PlanOut:
        """Qué corresponde correr y qué espera al usuario. Es lo mismo que contesta `next`."""
        with workspace_for(config_path, session_id) as opened:
            sessions.load(opened.conn, session_id)
            version_id = opened.plan_version(version)
            found = orchestration.survey(
                opened.conn, version_id, session_id=session_id,
                has_provider=opened.has_provider(),
            )
            return PlanOut(
                version=version_id,
                steps=[_step_out(step) for step in found.steps],
                blocking=[_step_out(step) for step in orchestration.blocking(found)],
            )

    # ── etapas ──

    @app.post("/sessions/{session_id}/stages/{stage}", dependencies=protected)
    def run_stage(session_id: str, stage: str, body: RunStage | None = None) -> JSONResponse:
        """Lo que tarda se encola y devuelve un job; lo que no, contesta acá mismo.

        La compuerta del plan es `DECISION-NEVER-CROSSED` sobre HTTP: una etapa que el plan da como
        WAITING no se encola, y la respuesta dice qué decisión falta.
        """
        params = (body or RunStage()).params
        with workspace_for(config_path, session_id) as opened:
            sessions.load(opened.conn, session_id)
            spec = catalog.stage(stage)
            blocked = catalog.blocked_by(opened, spec)
            if blocked:
                return JSONResponse(
                    status_code=status.HTTP_409_CONFLICT, content={"error": blocked}
                )
            if not spec.long:
                result = catalog.run(opened, stage, params)
                summary = catalog.summarize(result)
                return JSONResponse(
                    status_code=status.HTTP_200_OK,
                    content=StageResultOut(
                        stage=stage, result=summary,
                        warnings=list(summary.get("warnings") or []),
                    ).model_dump(),
                )
            catalog.clean(spec, params)  # que un parámetro inválido falle ahora y no en el worker
            job = jobs.enqueue(
                opened.conn, session_id=session_id, stage=stage, params=params
            )
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED, content=_job_out(job).model_dump()
            )

    @app.get("/jobs/{job_id}", dependencies=protected)
    def get_job(job_id: str) -> JobOut:
        with workspace_for(config_path) as opened:
            return _job_out(jobs.load(opened.conn, job_id))

    @app.get("/sessions/{session_id}/jobs", dependencies=protected)
    def session_jobs(session_id: str) -> list[JobOut]:
        with workspace_for(config_path, session_id) as opened:
            return [_job_out(item) for item in jobs.for_session(opened.conn, session_id)]

    # ── decisiones: síncronas, y rechazadas mientras la sesión tiene un job en vuelo ──

    @app.get("/sessions/{session_id}/grey", dependencies=protected)
    def grey(session_id: str, version: str | None = None, limit: int | None = None) -> dict:
        with workspace_for(config_path, session_id) as opened:
            return catalog.summarize(
                iterate.grey_pending(opened, version=version, limit=limit)
            )

    @app.post("/sessions/{session_id}/grey/{mention_id}", dependencies=protected)
    def answer_grey(session_id: str, mention_id: str, body: GreyAnswer) -> dict:
        with workspace_for(config_path, session_id) as opened:
            jobs.require_idle(opened.conn, session_id)
            chosen = iterate.grey_answer(
                opened, mention_id, version=body.version, to=body.to,
                none_of_these=body.none_of_these, comment=body.comment,
            )
            return {"mention": mention_id, "typed_as": chosen}

    @app.get("/sessions/{session_id}/branches", dependencies=protected)
    def branches(session_id: str, version: str | None = None) -> dict:
        with workspace_for(config_path, session_id) as opened:
            return catalog.summarize(iterate.survey_branches(opened, version=version))

    @app.post("/sessions/{session_id}/branches/{branch_id}", dependencies=protected)
    def choose_branch(session_id: str, branch_id: str, body: BranchChoiceIn) -> dict:
        with workspace_for(config_path, session_id) as opened:
            jobs.require_idle(opened.conn, session_id)
            return catalog.summarize(iterate.choose_branch(
                opened, branch_id, version=body.version, why=body.why,
                invalid=body.invalid or None,
                override_structural=body.override_structural,
            ))

    @app.get("/sessions/{session_id}/review", dependencies=protected)
    def review(session_id: str, status_filter: str = "open", kind: str | None = None) -> dict:
        with workspace_for(config_path, session_id) as opened:
            return {
                "items": evaluate.review_items(opened, status=status_filter, kind=kind),
                "counts": evaluate.review_counts(opened),
            }

    @app.post("/sessions/{session_id}/review/{item_id}", dependencies=protected)
    def resolve_review(session_id: str, item_id: str, body: ReviewDecision) -> dict:
        with workspace_for(config_path, session_id) as opened:
            jobs.require_idle(opened.conn, session_id)
            evaluate.resolve_review(opened, item_id, body.decision, comment=body.comment)
            return {"item": item_id, "decision": body.decision}

    # ── artefactos ──

    @app.get("/sessions/{session_id}/artifacts", dependencies=protected)
    def artifacts(session_id: str) -> list[ArtifactOut]:
        with workspace_for(config_path, session_id) as opened:
            return [
                ArtifactOut(key=item.key, name=item.name)
                for item in opened.artifacts.listing()
            ]

    @app.get("/sessions/{session_id}/artifacts/{key:path}", dependencies=protected)
    def artifact_url(session_id: str, key: str) -> ArtifactUrlOut:
        """Un pre-signed de lectura (`API-PRESIGNED-GET`). El cliente descarga del almacén: un
        export de cien megas no tiene por qué pasar por el proceso que atiende HTTP."""
        with workspace_for(config_path, session_id) as opened:
            store = opened.artifacts.store
            # Sólo lo de esta sesión: la clave la propone el cliente, y sin esto pediría la de
            # otra. Es `SESSION-SCOPED-DATA` en el borde de HTTP.
            if not key.startswith(opened.artifacts.prefix + "/"):
                key = f"{opened.artifacts.prefix}/{key}"
            artifact = artifacts_module.Artifact(store, key)
            if not artifact.exists():
                raise StageError(f"no hay artefacto {key}")
            expires = opened.config.api.presigned_expiry_s
            return ArtifactUrlOut(
                key=key, url=artifact.url(expires_s=expires), expires_s=expires
            )


# ─────────────────────────────  conversiones  ─────────────────────────────


def _upload_out(opened: Workspace, upload, *, put_urls=None) -> UploadOut:
    return UploadOut(
        id=upload.id, name=upload.name, note=upload.note, created_at=upload.created_at,
        files=upload.filenames, ontology=upload.ontology,
        missing=uploads.missing(opened.artifacts.store, upload), put_urls=put_urls,
    )


def _session_out(session) -> SessionOut:
    return SessionOut(
        id=session.id, use_case=session.use_case, phase=session.phase, name=session.name,
        note=session.note, created_at=session.created_at, updated_at=session.updated_at,
    )


def _step_out(step) -> StepOut:
    return StepOut(
        name=step.name, command=step.command, state=step.state, detail=step.detail,
        decision=step.decision,
    )


def _job_out(job: jobs.Job) -> JobOut:
    return JobOut(
        id=job.id, session_id=job.session_id, stage=job.stage, status=job.status,
        params=job.params, progress=job.progress, result=job.result, warnings=job.warnings,
        error=job.error, created_at=job.created_at, started_at=job.started_at,
        finished_at=job.finished_at,
    )
