# Los pares

Corpus publicados anotados contra una ontología. Se usan para **fijar los umbrales del matcher
con datos** en vez de escribirlos a mano, y para correr el pipeline entero contra una respuesta
conocida.

**Todos los pares son instrumentos.** El proyecto no tiene un dominio comprometido: el entregable
es el sistema y su caracterización *a través* de pares (`SCOPE-PURPOSE`, "sin tarea downstream
comprometida"). El par de metodología cualitativa fue el andamio inicial, para tener con qué
probar mientras no existía esto; **está retirado** y no es el destino de nada.

Lo que hace posible medir acá: en un corpus anotado contra una ontología O, **`in_seed` es
decidible por construcción** —la clase está en O o no está—, así que la tasa de falsos huérfanos
que gobierna `BUILD-NO-GO-GATE` se obtiene sin campaña de anotación.

**Este README es el compañero operativo de los datos, no el registro de la decisión.** Por qué se
eligieron estos pares y no otros está en [`../pair_selection.md`](../pair_selection.md); qué
arrojó cada uno al medirse, en [`../findings.md`](../findings.md).

## Qué se versiona de acá, y qué no

El directorio está adentro del repo, pero **sus datos no se versionan**: son 40 MB de artefactos
publicados de terceros y cada `NOTA_FASE0.md` dice exactamente cómo regenerarlos. Lo que sí queda
versionado es lo único que no se puede regenerar: **el `pair.yml` de cada par y su nota**. Las
reglas están en el `.gitignore` de `pipeline/`.

Los directorios que empiezan con `_` son la descarga cruda —`_craft/` trae su propio `.git`— y
quedan afuera enteros.

## Los pares

| Directorio | Corpus | Ontología | Inventario | Estado |
|---|---|---|---|---|
| [`craft-cl/`](craft-cl/NOTA_FASE0.md) | CRAFT v5.0.2, 97 artículos biomédicos completos, 8.723 menciones | Cell Ontology (`cl-base`) + extension classes de CRAFT | 3.418 clases, 96% con definición | **primario**, listo |
| [`materiominer/`](materiominer/NOTA_FASE0.md) | MaterioMiner v1.0.1, 4 publicaciones de mecánica de materiales, 2.229 menciones | Materials Mechanics Ontology | 428 clases, 420 con definición | **segundo**, listo |
| `hpo-gsc-plus/` | GSC+, 228 abstracts, ~1.933 anotaciones | Human Phenotype Ontology | ~19k clases | pendiente |
| `cafeteria/` | CafeteriaFCD + CafeteriaSA, recetas y abstracts | FoodOn | ~40k clases | pendiente |

Los dos pendientes existen para una sola pregunta, y no es cuál es el mejor corpus: **cómo se
mueve el punto de operación con el tamaño del inventario**. Con 428, 3.418 y ~40k candidatos se
puede medir esa curva en vez de suponerla — hoy hay dos puntos y un salto entre ellos, que no es
una forma (`FINDINGS-MEASURED-SIZE-CURVE`).

## Cómo se agrega un par

Un par es un directorio con un `pair.yml` y una nota de fase 0. Ver
[`craft-cl/pair.yml`](craft-cl/pair.yml) como referencia:

```yaml
name: craft-cl
language: en
documents:   {dir: ../_craft/articles/txt, glob: "*.txt"}
annotations: {format: knowtator, dir: ..., suffix: .txt.knowtator.xml}
ontology:    {files: [ontology/cl-base.obo, ontology/CL+extensions.obo]}
excluded_classes: ...      # opcional
holdout_classes: null      # opcional
```

Formatos de anotación implementados: `knowtator` (CRAFT), `brat` y `webanno`. Agregar uno es
escribir un reader en [`../src/onto_pipeline/calibration.py`](../src/onto_pipeline/calibration.py)
y registrarlo en `READERS`; el resto del banco no cambia. Las decisiones de importación viven en
los docstrings de ese módulo.

Después, desde `pipeline/`:

```bash
uv run onto-pipeline calibrate <directorio>
```
