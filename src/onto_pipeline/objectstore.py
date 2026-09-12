"""Dónde viven los bytes, sin que nadie sepa dónde.

El mismo papel que `store.py` hace con el motor de base. Un artefacto se nombra con una **clave**
—segmentos separados por `/`, sin barra inicial— y el que la resuelve es este módulo.

**Hay un solo sustrato: S3.** No porque haga falta la nube para trabajar, sino porque dos
implementaciones son dos comportamientos, y el que se prueba termina no siendo el que se
despliega: las URL firmadas, el paginado del listado y los errores no se parecen entre un
filesystem y un bucket. En local eso es MinIO, que habla la misma API —`storage.endpoint_url` lo
apunta—; en los tests es un cliente falso en memoria, sin red, que entra por el mismo
`S3ObjectStore`. Escribir en disco directo no es una opción soportada en ningún lado.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

BACKENDS = ("s3",)


class MissingObject(KeyError):
    """La clave no está. Que un artefacto falte es normal —todavía no se generó—, así que esto
    se pregunta con `exists` y sólo se levanta cuando alguien lee a ciegas."""


def check_key(key: str) -> str:
    """Una clave es relativa, no tiene `..` ni segmentos vacíos, y no empieza con `/`.

    En S3 cualquier cosa es una clave válida y `..` es un segmento literal; en el filesystem es
    salir del directorio. Una sola validación para los dos, y en el borde de entrada, porque la
    clave de un upload la propone el cliente.
    """
    if not key or key.startswith("/") or key.endswith("/"):
        raise ValueError(f"clave inválida: {key!r}")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"clave inválida: {key!r}")
    return key


class ObjectStore(ABC):
    """Nada del proveedor asoma acá: ni bucket, ni ruta, ni credencial."""

    @abstractmethod
    def put(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def get(self, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def presigned_get(self, key: str, *, expires_s: int) -> str: ...

    @abstractmethod
    def presigned_put(self, key: str, *, expires_s: int) -> str: ...

    def copy_in(self, key: str, source: Path) -> None:
        """Subir un archivo que ya está en disco. S3 lo sobreescribe con `upload_file`, que no
        lo lee entero a memoria: un corpus en PDF pesa."""
        self.put(key, Path(source).read_bytes())


class S3ObjectStore(ObjectStore):
    """S3, o cualquier cosa que hable su API. boto3 se importa tarde y a propósito: es un extra,
    y quien corre local no tiene por qué tenerlo instalado — el mismo trato que psycopg en
    `store.open_postgres`."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        region: str = "",
        endpoint_url: str = "",
        client=None,
    ) -> None:
        if not bucket:
            raise ValueError("storage.bucket es obligatorio con storage.backend: s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        # El cliente se puede pasar hecho: es lo que permite probar el prefijado de claves y el
        # paginado contra un doble en memoria, sin red y sin credenciales.
        if client is not None:
            self.client = client
            return
        try:
            import boto3  # noqa: PLC0415 — extra opcional, ver el docstring
        except ImportError as exc:  # pragma: no cover - depende del entorno
            raise RuntimeError(
                "storage.backend: s3 necesita boto3. `uv sync --extra api` lo instala."
            ) from exc
        # `endpoint_url` vacío es AWS; con valor es cualquier cosa que hable S3 —MinIO en
        # local—. Es un parámetro y no un backend nuevo a propósito: el código que corre contra
        # MinIO tiene que ser el mismo que corre contra AWS, o probar uno no dice nada del otro.
        self.client = boto3.client(
            "s3", region_name=region or None, endpoint_url=endpoint_url or None
        )

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{check_key(key)}" if self.prefix else check_key(key)

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except self.client.exceptions.NoSuchKey as exc:
            raise MissingObject(key) from exc

    def exists(self, key: str) -> bool:
        # `client.exceptions.ClientError` y no el import de botocore: así el doble de los tests
        # no tiene que traerse media biblioteca para decir que algo no está.
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.ClientError:
            return False
        return True

    def list(self, prefix: str = "") -> list[str]:
        full = self._key(prefix) if prefix else self.prefix
        cut = len(self.prefix) + 1 if self.prefix else 0
        keys: list[str] = []
        # Paginado y no `list_objects_v2` a secas: mil claves es poco para un corpus.
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=full
        ):
            keys.extend(item["Key"][cut:] for item in page.get("Contents", []))
        return sorted(keys)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def presigned_get(self, key: str, *, expires_s: int) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._key(key)},
            ExpiresIn=expires_s,
        )

    def presigned_put(self, key: str, *, expires_s: int) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": self._key(key)},
            ExpiresIn=expires_s,
        )

    def copy_in(self, key: str, source: Path) -> None:
        self.client.upload_file(str(source), self.bucket, self._key(key))


def open_configured(storage) -> ObjectStore:
    """El almacén que diga la configuración. Es el **único** lugar donde se construye uno, y por
    eso es el único que los tests reemplazan por el doble en memoria."""
    return S3ObjectStore(
        storage.bucket,
        prefix=storage.prefix,
        region=storage.region,
        endpoint_url=storage.endpoint_url,
    )
