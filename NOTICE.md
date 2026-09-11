# Licencia y atribución

## Este repositorio

`onto-pipeline` —el código, la documentación y los descriptores de casos de uso— se publica bajo
**Creative Commons Attribution 4.0 International (CC BY 4.0)**. El texto legal completo está en
[`LICENSE`](LICENSE); el resumen legible, en <https://creativecommons.org/licenses/by/4.0/>.

Al reusarlo, la atribución que pide la licencia es: **Gonzalo Torterolo, `onto-pipeline`**, con
un enlace a este repositorio y una nota de si se lo modificó.

## Lo que **no** cubre esta licencia

**Los datos de los casos de uso no se distribuyen acá y no son míos.** `use_cases/` versiona sólo
el `use_case.yml` y el `PROCEDENCIA.md` de cada uno; los corpus y las ontologías quedan
gitignoreados y se bajan de su fuente. Cada uno tiene su licencia, y la nota de procedencia dice
cuál y cómo obtenerlo:

| Caso de uso | Corpus | Ontología | Licencia del corpus |
|---|---|---|---|
| [`craft-cl`](use_cases/craft-cl/PROCEDENCIA.md) | CRAFT v5.0.2 | Cell Ontology (OBO Foundry) | anotaciones CC BY 3.0; artículos del subconjunto Open Access de PMC; la ontología, CC BY 4.0 |
| [`materiominer`](use_cases/materiominer/PROCEDENCIA.md) | MaterioMiner v1.0.1 | Materials Mechanics Ontology | CC BY 4.0, anotaciones y publicaciones |
| `qualitative` | corpus de trabajo, no publicado | ontología inicial de metodología cualitativa | no se distribuye |

Los jars del razonador (OWL API, ELK, HermiT) los baja `scripts/fetch-jars.sh` de Maven Central y
tampoco se versionan: cada uno conserva su licencia.

**Nada de lo que produce el pipeline hereda esta licencia automáticamente.** Una ontología
enriquecida es obra derivada del corpus y de la ontología inicial que se le dieron de entrada;
qué se puede hacer con ella lo dicen las licencias de **esas** dos cosas, no ésta.
