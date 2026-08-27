# CRAFT · CL+extensions — nota de salida de Fase 0

Documenta de dónde salió este par, en qué formato está y qué decisiones se tomaron al
importarlo, para que dentro de un año se pueda auditar por qué los umbrales quedaron donde
quedaron. Por qué se eligió **éste** y no otro está en
[`pair_selection.md`](../../pair_selection.md).

Este par es **uno de los instrumentos, y no hay caso de aplicación**: el proyecto no tiene
dominio comprometido, así que todos los pares se usan para medir el sistema. Corregido el
2026-09-09; antes esta línea decía que el caso de aplicación era la semilla cualitativa.

## Procedencia

| Artefacto | Origen | Versión / fecha |
|---|---|---|
| Corpus + anotaciones | `github.com/UCDenver-ccp/CRAFT`, clone esparso de `articles/txt` y `concept-annotation/CL` | v5.0.2 (2022-07-25) |
| `ontology/cl-base.obo` | `purl.obolibrary.org/obo/cl/cl-base.obo` | descargado 2026-09-08 |
| `ontology/cl-base.owl` | `purl.obolibrary.org/obo/cl/cl-base.owl` | descargado 2026-09-08 |
| `ontology/CL+extensions.obo` | del propio release de CRAFT, `concept-annotation/CL/CL+extensions/` | 2019-05-10 |

Licencia de las anotaciones: **CC BY 3.0** (`LICENSE.txt` del repo). Los artículos vienen del
subconjunto Open Access de PubMed Central. La Cell Ontology es CC BY 4.0.

El clone vive en `../_craft` y está compartido con cualquier otro par que use CRAFT (GO_CC, SO).
No está versionado: se regenera con

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/UCDenver-ccp/CRAFT.git _craft
cd _craft && git sparse-checkout set articles/txt concept-annotation/CL
```

## Por qué la ontología no es la que shippea CRAFT

**Es el hallazgo que corrige el plan.** CRAFT distribuye la ontología en formato OBO *básico*,
que es la versión con los axiomas lógicos removidos. Medido sobre los dos archivos:

| Cell Ontology | clases | con `def:` | `is_a` | `relationship` | `intersection_of` | `disjoint_from` |
|---|---|---|---|---|---|---|
| la de CRAFT (`cl-basic`, 2019) | 2.164 | 1.792 | 2.869 | 412 | **0** | 0 |
| `cl-base` (release actual) | 3.540 | 3.368 | 4.880 | 4.247 | **5.023** | 39 |

El README de CRAFT lo dice explícitamente: *«We have not implemented these as formal logical
definitions yet… In the future, we intend to distribute the ontologies in OWL rather than OBO
format.»* Usar el archivo del repo sería calibrar contra exactamente la taxonomía de términos
que el criterio de selección descartaba.

Los IRIs de clase de OBO son estables, así que las anotaciones de 2019 resuelven contra el
release actual. Lo que no sobrevivió: **6 de las 285 clases CL anotadas fueron obsoletadas o
fusionadas** entre 2019 y hoy. Las cubren las extension classes de CRAFT, que por eso siguen
en el par.

`cl-base.owl` está al lado y **no lo lee el importador**: es el archivo con los axiomas, insumo
del lado simbólico (ELK/HermiT). El importador lee el `.obo`, del que solo necesita etiqueta,
definición y sinónimos; cargar el OWL por rdflib serían minutos y cientos de MB para recuperar
cuatro campos.

## Formato de anotación

Texto plano en `_craft/articles/txt/<pmid>.txt`, standoff en
`_craft/concept-annotation/CL/CL+extensions/knowtator/<pmid>.txt.knowtator.xml`:

```xml
<annotation>
  <mention id="CL_basic_2014_02_21_Instance_12805" />
  <span start="975" end="981" />
  <spannedText>neuron</spannedText>
</annotation>
<classMention id="CL_basic_2014_02_21_Instance_12805">
  <mentionClass id="CL:0000540">neuron</mentionClass>
</classMention>
```

Los dos bloques se unen por el id de mención. **Los offsets indexan el texto plano**, no el
Markdown de ningún parser: es lo que vuelve inaplicable acá la objeción del §12.2 a los
importadores, y por eso `markdown_hash` guarda el SHA-256 del texto fuente. El importador valida
los 97 documentos contra él antes de medir nada.

## Decisiones de importación

**1. Las anotaciones discontinuas se saltean y se cuentan.** 424 de 9.147 (4,6%) tienen más de
un `<span>`. Aplanarlas a `(primer inicio, último fin)` le daría al encoder el texto que el
anotador dejó afuera a propósito; descartarlas en silencio subestimaría el corpus. Quedan 8.723
menciones utilizables.

**2. El inventario es la unión de dos archivos, con el release primero.** `cl-base.obo` (3.335
clases no obsoletas) ∪ las 84 extension classes que CRAFT define y el release no tiene = 3.419,
menos la clase de la decisión 6 = **3.418 targets, 3.281 con definición (96%)**. El orden importa:
si una clase está en los dos, gana la redacción del release, que es la que se está midiendo.

**3. Las clases obsoletas nunca son target.** `is_obsolete: true` las saca; si no, una mención
podría tiparse correctamente contra algo que la ontología retiró.

**4. `in_seed` es verdadero por construcción.** Es el punto entero del cambio de corpus: la clase
gold está en la ontología o no está, y acá siempre está. Consecuencia que hay que tener presente
al leer los números: **este par no tiene huérfanos genuinos**, así que mide la mitad de la
compuerta del §12.1 y no la otra.

**5. Por eso existe la retención de clases.** `calibrate --holdout 0.2` retiene una fracción
determinista del inventario; toda mención de una clase retenida pasa a ser huérfano genuino *de
respuesta conocida*. Es la única forma de medir si el matcher se abstiene cuando debe, en vez de
empujar la mención a la clase sobreviviente más parecida. Apagado por defecto, porque cambia el
significado del número principal.

**6. Las clases que nunca pueden ser correctas salen del inventario.** CRAFT publica, por
conjunto de anotación, las clases que sus anotadores decidieron no usar; su README garantiza que
toda predicción contra ellas es un falso positivo. Para `CL+extensions` la lista es una sola
clase, `CL:0000000` (*cell*), sustituida por la extension class `CL_GO_EXT:cell`.

Parece un detalle y no lo es. **Las dos clases tienen la etiqueta idéntica, *cell*, y
`CL_GO_EXT:cell` sola es 3.262 de las 8.723 menciones (37% del corpus).** Con las dos en el pool
el encoder no puede distinguirlas y 2.759 menciones caían en la equivocada. Dejarlas como
candidatas es envenenar el pool a sabiendas: lo que se mediría es una colisión de modelado, no el
matcher. Por eso se descartan por defecto; `--keep-excluded` corre la comparación y muestra
cuánto error causaban.

## Sin subseteo

No se recortó el inventario más allá de la clase de la decisión 6. Las 3.418 clases entran en el
criterio de escala del plan («miles, no millones») y el corpus usa 290 de ellas — 265 entre las
menciones continuas. Esa brecha entre
inventario y clases efectivamente anotadas es parte de lo que se está midiendo: es el escenario
realista en el que casi todo el inventario es distractor.

## Cómo se corre

```bash
cd pipeline
uv run onto-pipeline calibrate craft-cl -m label -m gloss -m label_and_gloss
uv run onto-pipeline calibrate craft-cl --cross-encoder     # con y sin re-ranker
uv run onto-pipeline calibrate craft-cl --holdout 0.2       # fabricar huérfanos genuinos
```

Resultados en `pipeline/data/calibration/craft-cl.json`.

## Qué dio (2026-09-09)

Resumen; el desarrollo está en la sección **Resultados de C4** del plan, y el crudo en
`pipeline/data/calibration/craft-cl.json`.

| variante | recall@1 sobre 8.723 | separación | mejor F1 |
|---|---|---|---|
| **label · bi** | **6.090 (69,8%)** | **+1,61** | **0,774** en 0,90 |
| label_and_gloss · bi | 1.161 (13,3%) | −0,49 | 0,135 |
| gloss · bi | 686 (7,9%) | −0,42 | 0,080 |
| label · cross | 2.220 (25,4%) | −0,56 | 0,138 |

Las dos decisiones de config que el plan traía sin resolver quedaron resueltas acá:
`match_against: label` y `use_cross_encoder: false`, las dos por márgenes grandes. Con glosas la
separación es **negativa** —los errores puntúan más alto que los aciertos— y eso ningún umbral lo
arregla.

Dos cosas que este par destapó y que un inventario de 34 clases no podía destapar:

1. **La colisión `cell`.** Descrita arriba en la decisión 6. Con `CL:0000000` en el pool el
   recall@1 era 3.343 y la separación 0,39; sacándola, 6.090 y 1,61. La mitad del error medido
   era un choque de etiquetas idénticas, no el matcher.
2. **El tipado no escalaba.** 8.723 menciones × 3.418 clases son 30 millones de productos punto
   que `matching.py` hacía en el intérprete, y la corrida no terminaba. Se arregló con el mismo
   patrón numpy por bloques que el módulo ya usaba para blocking.

## Contra qué compararse

El **CRAFT Shared Task 2019** (`sites.google.com/view/craft-shared-task-2019`) publicó tarea de
concept annotation, evaluador oficial y baselines sobre estos mismos 97 artículos. Los números de
acá no flotan solos: hay estado del arte para el mismo par. Ojo con la comparación directa — el
shared task evalúa reconocimiento *y* normalización sobre un split train/test, mientras el banco
acá mide solo tipado sobre menciones dadas, que es la etapa B2 aislada.
