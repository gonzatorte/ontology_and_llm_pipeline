"""La interfaz HTTP. Traduce requests a llamadas a `services/` y nada más.

Como el CLI y como el wizard: acá no vive ninguna lógica de dominio. Lo que decide si una etapa
puede correr es `services/catalog.py`, lo que decide si dos cosas pueden correr a la vez es
`jobs.py`, y lo que sabe dónde están los artefactos es `artifacts.py`. Si algo de eso se
escribiera acá, la API sería un pipeline paralelo — que es exactamente lo que no se quiso.
"""

from .app import create_app

__all__ = ["create_app"]
