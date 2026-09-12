"""La capa de servicios: una etapa, una función, ninguna interfaz.

**Por qué existe.** Hay tres interfaces sobre el mismo pipeline —`cli`, de banderas, `wizard`,
línea por línea, y la API REST— y el cuerpo de una etapa no puede vivir en ninguna sin que las
otras lo copien. Antes vivía en `cli.py`: cargar config, llamar al dominio y pintar tablas de
`rich` estaban en la misma función, y eso hacía imposible correr una etapa desde otro lado.

**El contrato.**

1. Toda función recibe un `Workspace` (config + almacén) y devuelve un resultado tipado.
2. Ninguna imprime, y **nada acá importa `typer`, `rich`, `fastapi` ni `uvicorn`** —ni nada
   de `interfaces/`—. Hay un test que lo fija leyendo los imports.
3. Lo que el usuario tiene que arreglar viaja como `StageError`, no como `typer.BadParameter`.
4. El progreso de una etapa larga sale por `progress(...)`, que por defecto no hace nada.
5. Los puntos de decisión están partidos en dos —una función *plantea*, otra *registra*—
   porque el CLI cruza ese corte entre dos comandos y el wizard entre pregunta y respuesta.

Quien muestra los resultados es `render`; quien los pide, `cli` o `wizard`.
"""

from __future__ import annotations

from . import deliver, evaluate, iterate, prep
from .workspace import (
    Progress,
    ProviderMissing,
    StageError,
    Workspace,
    silent,
)

__all__ = [
    "Progress", "ProviderMissing", "Workspace", "StageError", "silent",
    "deliver", "evaluate", "iterate", "prep",
]
