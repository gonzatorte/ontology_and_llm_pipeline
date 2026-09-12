"""El almacén de objetos: un contrato, dos sustratos.

El molde es `tests/test_store.py`, que corre el mismo contrato contra SQLite y contra Postgres.
La razón es la misma: lo que se prueba con uno tiene que valer con el otro, porque el código que
los usa no sabe cuál está debajo — y si se entera, el corte se rompió.

El doble de S3 vive acá y es deliberadamente tonto: no prueba a S3, prueba lo que este módulo le
pone encima —el prefijado de claves, el paginado del listado, qué pasa cuando una clave no está—,
que es lo único que podemos romper nosotros.
"""

from __future__ import annotations

import pytest

from onto_pipeline.objectstore import (
    LocalObjectStore,
    MissingObject,
    S3ObjectStore,
    check_key,
)


class _NoSuchKey(Exception):
    pass


class _ClientError(Exception):
    pass


class FakeS3Client:
    """Lo mínimo de la API de S3 que usa `S3ObjectStore`, en un diccionario."""

    class exceptions:  # noqa: N801 - el nombre lo fija boto3
        NoSuchKey = _NoSuchKey
        ClientError = _ClientError

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body):  # noqa: N803 - la firma la fija boto3
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _NoSuchKey(Key)
        return {"Body": _Body(self.objects[Key])}

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _ClientError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def get_paginator(self, _operation):
        return _Paginator(self)

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):  # noqa: N803
        return f"https://fake.s3/{operation}/{Params['Key']}?expires={ExpiresIn}"


class _Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


class _Paginator:
    def __init__(self, client: FakeS3Client) -> None:
        self.client = client

    def paginate(self, *, Bucket, Prefix):  # noqa: N803
        # De a una clave por página a propósito: un listado que sólo anduviera con una página
        # pasaría igual, y el corpus de un upload no entra en una.
        for key in sorted(self.client.objects):
            if key.startswith(Prefix):
                yield {"Contents": [{"Key": key}]}


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalObjectStore(tmp_path)
    return S3ObjectStore("bucket", prefix="deploy", client=FakeS3Client())


def test_what_was_written_comes_back_the_same(store):
    """Bytes, no texto: los recortes de figuras son PNG y pasan por el mismo camino."""
    store.put("a/b.png", b"\x89PNG\r\n")

    assert store.get("a/b.png") == b"\x89PNG\r\n"
    assert store.exists("a/b.png")


def test_a_key_that_is_not_there_is_asked_about_and_not_guessed(store):
    """Que un artefacto falte es normal —todavía no se generó—, así que `exists` contesta que no
    en vez de romper, y leer a ciegas levanta algo que se puede distinguir."""
    assert not store.exists("no/esta.ttl")
    with pytest.raises(MissingObject):
        store.get("no/esta.ttl")


def test_listing_is_by_prefix_and_returns_keys_not_paths(store):
    """La clave que vuelve es la que se pasó, sin la raíz local ni el prefijo del bucket: el que
    lista después lee, y tendría que traducir."""
    store.put("sessions/s1/ontology/a.ttl", b"a")
    store.put("sessions/s1/ontology/b.ttl", b"b")
    store.put("sessions/s2/ontology/c.ttl", b"c")

    assert store.list("sessions/s1") == [
        "sessions/s1/ontology/a.ttl",
        "sessions/s1/ontology/b.ttl",
    ]


def test_deleting_something_that_is_not_there_is_not_an_error(store):
    """Borrar es idempotente: un upload que se borra dos veces no es un caso de error."""
    store.delete("ni/estuvo.txt")
    store.put("estuvo.txt", b"x")
    store.delete("estuvo.txt")

    assert not store.exists("estuvo.txt")


def test_overwriting_replaces_and_does_not_append(store):
    """`normalize` vuelve a escribir la misma clave en cada corrida."""
    store.put("x.ttl", b"primero")
    store.put("x.ttl", b"segundo")

    assert store.get("x.ttl") == b"segundo"


def test_a_url_is_handed_out_for_reading_without_streaming_the_bytes(store):
    """El cliente descarga del almacén, no de la API: un export de cien megas no tiene por qué
    pasar por el proceso que atiende HTTP."""
    store.put("sessions/s1/ontology/v1.trig", b"@prefix : <x> .")

    assert store.presigned_get("sessions/s1/ontology/v1.trig", expires_s=60)


@pytest.mark.parametrize("key", ["", "/absoluta", "con//vacio", "sale/../de/la/raiz", "termina/"])
def test_a_key_that_could_escape_the_root_is_refused(key):
    """En S3 `..` es un segmento literal y en el filesystem es salir del directorio. La clave de
    un upload la propone el cliente, así que la validación va en el borde y no en el backend."""
    with pytest.raises(ValueError, match="clave inválida"):
        check_key(key)


def test_the_bucket_prefix_does_not_leak_into_the_keys():
    """Compartir un bucket entre despliegues es lo que el prefijo permite; que se vea del otro
    lado sería que la clave dependa de dónde está desplegado."""
    client = FakeS3Client()
    store = S3ObjectStore("bucket", prefix="deploy", client=client)

    store.put("sessions/s1/a.ttl", b"a")

    assert list(client.objects) == ["deploy/sessions/s1/a.ttl"]
    assert store.list("sessions") == ["sessions/s1/a.ttl"]
