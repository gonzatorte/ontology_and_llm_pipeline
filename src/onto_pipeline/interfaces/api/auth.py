"""Quién puede llamar: un token estático en `X-Auth-Key` (`API-AUTH-KEY`).

**Falla cerrado.** Sin token configurado la app no arranca, en vez de arrancar abierta: una API
que queda sin autenticación por una variable que nadie exportó no da ningún error, y eso es lo
peor que puede pasarle. Es un token para todo el despliegue, así que quien lo tiene ve todas las
sesiones — `DEBT-API-USERS` es donde vive esa limitación.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Header, HTTPException, status

HEADER = "X-Auth-Key"


class MissingToken(RuntimeError):
    """No hay token configurado. Se levanta al construir la app, no al atender un request."""


def token(env_name: str, environ=None) -> str:
    environ = os.environ if environ is None else environ
    value = environ.get(env_name, "")
    if not value:
        raise MissingToken(
            f"{env_name} no está en el entorno. La API no arranca sin token: arrancar abierta no "
            "daría ningún error y nadie se enteraría."
        )
    return value


def guard(expected: str):
    """La dependencia que protege todo menos `healthz`."""

    def check(x_auth_key: str = Header(default="")) -> None:
        # `compare_digest` y no `==`: comparar tokens carácter a carácter filtra por tiempo
        # cuánto prefijo acertó quien prueba.
        if not secrets.compare_digest(x_auth_key, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=f"falta o no coincide {HEADER}"
            )

    return check
