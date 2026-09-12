"""El CLI de la API: levantarla y administrarla.

Aparte del `onto-pipeline` de siempre a propósito. **Ése es la interfaz de uso** —enriquecer una
ontología— y nadie que la enriquezca necesita crear un upload, mirar la cola ni destrabar una
sesión. Mezclarlos haría que el comando que documenta el pipeline tuviera la mitad de sus verbos
hablando de otra cosa.

Lo que hace acá es gestión: arrancar el servidor, mirar y limpiar los jobs, y borrar uploads. La
capa de servicios no se toca desde este archivo — para correr una etapa está el otro CLI o la API.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ...config import Config
from .deps import workspace as workspace_for

app = typer.Typer(
    add_completion=False,
    help="Levantar y administrar la API de onto-pipeline.",
    no_args_is_help=True,
)
console = Console()

ConfigOption = typer.Option(
    Path("config/default.yaml"), "--config", "-c", metavar="<path>", help="Config file."
)


@app.command("serve")
def serve(
    config_path: Path = ConfigOption,
    host: str | None = typer.Option(None, "--host", help="Sobreescribe api.host."),
    port: int | None = typer.Option(None, "--port", help="Sobreescribe api.port."),
) -> None:
    """Levantar la API.

    Sin token en el entorno no arranca (`API-AUTH-KEY`): una API que queda abierta no da ningún
    error y nadie se entera.
    """
    import uvicorn  # noqa: PLC0415 — el extra es opcional; ver el docstring del módulo

    from .app import create_app  # noqa: PLC0415

    config = Config.load(config_path)
    try:
        instance = create_app(config_path)
    except Exception as exc:  # noqa: BLE001 - lo que falta se dice, no se tracea
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc
    uvicorn.run(instance, host=host or config.api.host, port=port or config.api.port)


jobs_app = typer.Typer(help="La cola de trabajos.")
app.add_typer(jobs_app, name="jobs")


@jobs_app.command("list")
def jobs_list(
    config_path: Path = ConfigOption,
    session: str = typer.Option("", "--session", help="Sólo los de esta sesión."),
) -> None:
    """Qué hay en la cola y qué está corriendo."""
    from . import jobs  # noqa: PLC0415

    with workspace_for(config_path) as opened:
        jobs.install(opened.conn)
        rows = (
            jobs.for_session(opened.conn, session) if session
            else [jobs.load(opened.conn, row["id"]) for row in opened.conn.execute(
                "SELECT id FROM jobs ORDER BY created_at DESC LIMIT 50")]
        )
    if not rows:
        console.print("[dim]no hay jobs[/]")
        return
    table = Table(show_header=True)
    for column in ("id", "sesión", "etapa", "estado", "progreso", "creado"):
        table.add_column(column)
    for job in rows:
        table.add_row(
            job.id, job.session_id, job.stage,
            _coloured(job.status), (job.progress or job.error)[:40], job.created_at,
        )
    console.print(table)


@jobs_app.command("sweep")
def jobs_sweep(config_path: Path = ConfigOption) -> None:
    """Cerrar los jobs que quedaron corriendo cuando murió el proceso.

    La API lo hace sola al arrancar. Esto es para destrabar una sesión sin reiniciar: mientras
    hay un job activo el índice parcial no deja encolar otro, y uno de un proceso muerto no va a
    terminar nunca.
    """
    from . import jobs  # noqa: PLC0415

    with workspace_for(config_path) as opened:
        swept = jobs.sweep_stale(opened.conn)
    console.print(
        f"[green]{len(swept)}[/] job(s) cerrados" if swept else "[dim]no había ninguno colgado[/]"
    )


uploads_app = typer.Typer(help="Los corpus subidos.")
app.add_typer(uploads_app, name="uploads")


@uploads_app.command("list")
def uploads_list(config_path: Path = ConfigOption) -> None:
    """Qué uploads hay, y cuáles están incompletos."""
    from . import uploads  # noqa: PLC0415

    with workspace_for(config_path) as opened:
        found = uploads.all_uploads(opened.conn)
        pending = {item.id: uploads.missing(opened.artifacts.store, item) for item in found}
        sessions = {item.id: uploads.sessions_on(opened.conn, item.id) for item in found}
    if not found:
        console.print("[dim]no hay uploads[/]")
        return
    table = Table(show_header=True)
    for column in ("id", "nombre", "archivos", "faltan", "sesiones"):
        table.add_column(column)
    for item in found:
        table.add_row(
            item.id, item.name, str(len(item.filenames)),
            str(len(pending[item.id])) if pending[item.id] else "-",
            ", ".join(sessions[item.id]) or "-",
        )
    console.print(table)


@uploads_app.command("delete")
def uploads_delete(upload_id: str, config_path: Path = ConfigOption) -> None:
    """Borrar un upload. **Es global** (`API-SHARED-UPLOADS`): se lo saca a toda sesión que
    corra sobre él, y por eso se dice cuáles quedan sin corpus."""
    from . import uploads  # noqa: PLC0415

    with workspace_for(config_path) as opened:
        affected = uploads.delete(opened.conn, opened.artifacts.store, upload_id)
    console.print(f"[green]borrado[/] {upload_id}")
    if affected:
        console.print(f"[yellow]sin corpus:[/] {', '.join(affected)}")


def _coloured(status: str) -> str:
    colour = {"done": "green", "failed": "red", "running": "cyan"}.get(status, "dim")
    return f"[{colour}]{status}[/]"


def entrypoint() -> None:
    app()
