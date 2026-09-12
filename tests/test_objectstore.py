"""El almacén de objetos: lo que este módulo le pone encima a S3.

No prueba a S3 —eso no es nuestro— sino lo único que podemos romper nosotros: el prefijado de
claves, el paginado del listado, qué pasa cuando una clave no está, y que una clave que podría
salirse del prefijo se rechace. El cliente es el doble en memoria de `conftest.py`, que entra por
el mismo `S3ObjectStore` que corre en producción: no hay una implementación de tests y otra de
despliegue, porque entonces la probada no sería la desplegada.
"""

from __future__ import annotations

import pytest
from conftest import FakeS3Client

from onto_pipeline.objectstore import MissingObject, S3ObjectStore, check_key


@pytest.fixture
def store(object_store):
    return object_store


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
