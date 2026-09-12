"""Dónde viven los bytes, sin que nadie sepa dónde.

El mismo papel que `store.py` hace con el motor de base: lo que difiere entre el filesystem y S3
vive acá y en ningún otro módulo. Un artefacto se nombra con una **clave** —segmentos separados
por `/`, sin barra inicial— y esa clave es la misma en los dos backends, así que una corrida
local y una en la nube escriben lo mismo con otro sustrato debajo.

El backend local enraiza las claves en `paths.work_dir`, que es donde ya vivían los artefactos:
`sessions/s1/ontology/x.ttl` es `data/sessions/s1/ontology/x.ttl`. No es una comodidad de
migración sino la propiedad que hace verificable el corte — si algo quedó escribiendo derecho al
disco, el árbol local sigue igual y no se nota; lo que lo delata es correr con el backend de
objetos y ver qué falta.
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

BACKENDS = ("local", "s3")


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
        """Subir un archivo que ya está en disco. Los backends lo sobreescriben con lo suyo, que
        no lee el archivo entero a memoria: un corpus en PDF pesa."""
        self.put(key, Path(source).read_bytes())


class LocalObjectStore(ObjectStore):
    """El filesystem. Para correr sin nube, y para los tests."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path(self, key: str) -> Path:
        return self.root / check_key(key)

    def put(self, key: str, data: bytes) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError as exc:
            raise MissingObject(key) from exc

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def list(self, prefix: str = "") -> list[str]:
        base = self.root / prefix if prefix else self.root
        if not base.exists():
            return []
        if base.is_file():
            return [prefix]
        return sorted(
            str(item.relative_to(self.root)).replace("\\", "/")
            for item in base.rglob("*")
            if item.is_file()
        )

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def presigned_get(self, key: str, *, expires_s: int) -> str:
        """Un `file://` absoluto. No está firmado ni vence: en local el que puede leer el
        artefacto es el que puede leer el disco, y fingir una firma sería mentir sobre qué
        protege. Sirve para desarrollo; el que viaja a un cliente remoto es el de S3."""
        return self.path(key).resolve().as_uri()

    def presigned_put(self, key: str, *, expires_s: int) -> str:
        check_key(key)
        return (self.root / key).resolve().as_uri()

    def copy_in(self, key: str, source: Path) -> None:
        """Subir un archivo que ya está en disco sin leerlo entero a memoria. Es lo que usa la
        siembra de uploads, donde los PDF son grandes y el destino es local."""
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


class S3ObjectStore(ObjectStore):
    """S3, o cualquier cosa que hable su API. boto3 se importa tarde y a propósito: es un extra,
    y quien corre local no tiene por qué tenerlo instalado — el mismo trato que psycopg en
    `store.open_postgres`."""

    def __init__(
        self, bucket: str, *, prefix: str = "", region: str = "", client=None
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
        self.client = boto3.client("s3", region_name=region or None)

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


def open_configured(storage, work_dir: Path) -> ObjectStore:
    """El backend que diga la configuración. `work_dir` es la raíz del local y se ignora en s3."""
    if storage.backend == "local":
        return LocalObjectStore(Path(storage.root) if storage.root else Path(work_dir))
    return S3ObjectStore(storage.bucket, prefix=storage.prefix, region=storage.region)
