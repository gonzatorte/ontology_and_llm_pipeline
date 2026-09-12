"""Las interfaces: lo que traduce entre un protocolo y la capa de servicios.

Una interfaz recibe lo que llega por su protocolo —la terminal para `cli` y `wizard`, HTTP para
`api`—, llama a una función de `services/` y muestra el resultado con `render`. No tiene lógica
de dominio: si algo hay que decidirlo sobre los datos, se decide en el servicio, porque si no
las interfaces empiezan a diferir en lo que hacen y no sólo en cómo preguntan.

La regla recíproca es la invariante 9 de `CLAUDE.md`: ningún servicio importa `typer`, `rich`,
`fastapi` ni `uvicorn`. `tests/test_services.py` la fija leyendo los imports.
"""
