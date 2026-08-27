# Plan: calibrar contra corpus publicados anotados

Enmienda al §12 (secuencia de construcción) de `especificacion_pipeline_ontologia.md`. Los
resultados de los barridos que este plan hizo posibles están en
[`HALLAZGOS.md`](HALLAZGOS.md) §1.1–1.2.

> Este documento se llamaba "separar el corpus de calibración del corpus de aplicación" y esa
> separación resultó ser una premisa equivocada: no hay corpus de aplicación. Ver la nota de
> corrección en la sección de C7.

## Problema

El paso 3 del §12 hace pivotar la decisión no-go sobre la **tasa de falsos huérfanos**, medida
contra el conjunto de retención: 5 documentos del corpus de ciencia abierta anotados a mano
contra la semilla de metodología cualitativa. Tres hallazgos hacen que esa medición no pueda
sostener la decisión.

**1. El par corpus/semilla está desalineado.** Medido sobre las 1.311 menciones únicas que B1
extrajo de Caulfield 2012, contra las 34 clases de la semilla:

- 24/34 clases superan el umbral de 0,70, pero **11 de esas 24 son eco léxico** — la mención es
  el nombre de la clase apareciendo en prosa corriente (`Question`←"question" 0,998,
  `Subject`←"subject" 0,992, donde "subject" significa *tema*, no sujeto de investigación).
- Del núcleo cualitativo de la semilla (Interview, Interview Answer, Field Note, Analytic
  Category, Descriptive Category, Document Analysis, Reformulation, Observation, Participant,
  Theoretical Framework), **7 de 10 no llegan al umbral**, y las 3 que llegan lo hacen por
  accidente (`Observation`←"a view", `Participant`←"participation").

Los papers *hablan sobre* investigación, no *reportan* estudios cualitativos. Una tasa de falsos
huérfanos medida acá mide el desajuste temático, no la calidad del matcher: es un número sin
significado, que es peor que un número malo.

**2. Hay un modo de falla activo que ningún umbral filtra.** Con `match_against: label` el
matcher hace en buena medida coincidencia difusa de cadenas. Sobre este corpus produce tipados
confiadamente equivocados **a 0,99**. Subirle el umbral no los saca.

**3. Dos decisiones centrales de config descansan en n=10.** Según los comentarios de
`config/default.yaml`: `match_against: label` (7/10 contra 2/10 recall@1) y
`use_cross_encoder: false`. Ambas medidas sobre diez pares, sobre el par desalineado. Si
etiqueta-vs-glosa está mal resuelto, toda la etapa A0.4 de generación de glosas es trabajo tirado.

**4. ~~B2 no está cableado.~~** *Superado por `a4df6f0`.* Al escribirse este plan, `Matcher`
solo lo ejercitaban los tests. Hoy `cli.py:434` (`match`) construye `Target`, persiste los
`Typing` y lee todos los umbrales de config. La compuerta del §12.1 ya corre end-to-end; lo que
sigue faltando es contra qué medirla, que es de lo que trata el resto de este documento.

## Decisión

Separar dos roles que hoy cumple un solo par de artefactos:

| Rol | Artefactos | Para qué |
|---|---|---|
| **Calibración** | corpus anotado publicado + su ontología | fijar umbrales, resolver etiqueta-vs-glosa, decidir cross-encoder, medir falsos huérfanos |
| **Aplicación** | corpus de ciencia abierta + semilla cualitativa | el caso de uso; consume la config ya calibrada |

Lo que habilita el cambio: en un corpus anotado contra una ontología O, **`in_seed` es decidible
por construcción** — la clase está en O o no está. La métrica que define la compuerta no-go se
obtiene sin campaña de anotación.

### Qué transfiere entre pares

- **Transfiere:** etiqueta vs. glosa, elección de encoder, si el cross-encoder aplasta la escala,
  la *forma* de la distribución de scores (dónde se separan verdaderos de falsos). Son propiedades
  del método y del encoder.
- **Transfiere parcialmente:** los umbrales. El coseno entre dos textos no depende de cuántos
  candidatos haya, así que 0,70 y 0,92 no se mueven tanto; lo que sí depende del tamaño del
  inventario es el *punto de operación* (con 40k clases hay más chances de que algo espurio supere
  0,70 que con 34). El valor externo es punto de partida defendible, no final.
- **No transfiere:** nada más. Lo que no transfiere requiere un chequeo chico por despliegue, no
  200 menciones anotadas.

Efecto secundario que importa: calibrar afuera convierte fijar umbrales de tarea manual única en
**procedimiento repetible** sobre cualquier par (corpus, ontología). Si lo que se valida es el
sistema y no el dominio, esa capacidad es parte de lo que hay que validar.

## Fases

### Fase 0 — Elegir el corpus de calibración (bloqueante, decisión de dominio)

Criterios, en orden de importancia:

1. **La ontología trae definiciones.** Sin glosas no se puede correr etiqueta-vs-glosa, que es la
   pregunta abierta en `matching.match_against`. Descarta corpus cuyo esquema sea solo etiquetas.
1b. **La ontología trae axiomas más allá de la taxonomía.** Definiciones lógicas
   (`intersection_of` / `equivalentClass`), relaciones y disyunciones. Un árbol de `is_a` con
   glosas cumple el criterio 1 y no sirve para nada del lado simbólico. **Cuidado: la
   distribución que shippea el corpus puede no ser la que cumple este criterio** (ver abajo).
2. **Escala del inventario comparable.** Miles, no millones: con los ~34 candidatos actuales casi
   cualquier mención rankea en algún lado, y con los millones de UMLS el punto de operación es
   otro. Subsetear es aceptable si se documenta el criterio.
3. **Inglés**, o agregar marcadores a `language.py` (~10 líneas, o usar el `/Lang` declarado).
4. Licencia que permita el uso.

#### Verificado (2026-09-08)

**Par primario: CRAFT `CL+extensions`.** Licencia CC BY 3.0. Release v5.0.2 (2022-07). 97
artículos, 11 módulos, cada uno en variante propia y `+extensions`. Formato: `articles/txt/*.txt`
texto plano + Knowtator XML standoff (`<span start end>` + `<mentionClass id>`), offsets sobre el
texto plano — confirma el argumento de la fase 2 contra el §12.2. Muestreo de un artículo: 104
anotaciones CL sobre 8 clases.

**El repo de CRAFT distribuye las ontologías en OBO básico, sin axiomas lógicos.** Medido:

| Cell Ontology | clases | con `def:` | `is_a` | `relationship` | `intersection_of` | `disjoint_from` |
|---|---|---|---|---|---|---|
| la que shippea CRAFT (`cl-basic`, 2019) | 2.164 | 1.792 | 2.869 | 412 | **0** | 0 |
| release actual (`cl-base.obo`) | 3.540 | 3.368 | 4.880 | 4.247 | **5.023** | 39 |

El propio README de CRAFT lo dice: *“We have not implemented these as formal logical definitions
yet… In the future, we intend to distribute the ontologies in OWL rather than OBO format.”* Usar el
`.obo` del repo es quedarse justo con la taxonomía de términos que el criterio 1b descarta.
**Decisión: anotaciones de CRAFT + ontología completa de OBO Foundry.** Los IRIs de clase son
estables, así que las anotaciones resuelven; hay que filtrar `is_obsolete: true`.

Elección de módulo, medida sobre los releases completos:

| Módulo | clases | def | `is_a` | `relationship` | `intersection_of` | veredicto |
|---|---|---|---|---|---|---|
| **CL** | 3.540 | 95% | 4.880 | 4.247 | **5.023** | ~1,4 definiciones lógicas por clase. Elegido |
| GO_CC | 4.077 | 100% | 4.699 | 1.983 | 697 | escala ideal, glosas completas, menos denso |
| GO_BP | 23.974 | 100% | 40.486 | 12.941 | 17.697 | muy axiomatizada, un orden de magnitud más grande |
| GO_MF | 10.041 | 100% | 12.274 | 1.254 | 249 | casi taxonomía pura |
| SO | 2.383 | 86% | 2.270 | 592 | 456 | densidad media |
| NCBITaxon | ~2,5M | ~0 | — | — | 0 | **descartada**: es el caso que el criterio 1b evita |
| CHEBI / PR | 200k / 380k | — | — | — | — | descartadas por escala |

Dos artefactos de CRAFT que el resto del plan debería aprovechar:

- `unused_classes_for_CL_annotations.txt` — clases que los anotadores decidieron no usar nunca.
  El README garantiza que toda anotación automática contra ellas es un falso positivo. Es señal de
  precisión **sin anotar nada**.
- Las extension classes traen su definición lógica en sintaxis Manchester dentro del campo `def:`.
  Material directo para la pregunta etiqueta-vs-glosa.

Existe además el **CRAFT Shared Task 2019**, con evaluador oficial y baselines publicados: los
números de la fase 3 tienen contra qué compararse.

#### Pares adicionales (barrido, tarea C5)

Un solo par no dice si los umbrales transfieren. Tres inventarios de tamaño muy distinto sí, y eso
ataca de frente el riesgo declarado abajo (*“el punto de operación no transfiere al inventario de
34 clases”*): con 179, 3.540 y ~40k candidatos se puede **medir la curva** en vez de suponerla.

| Par | Inventario | Anotaciones | Por qué |
|---|---|---|---|
| **HPO GSC+** | HPO, ~19k clases, definiciones lógicas vía PATO/UBERON | 228 abstracts, ~1.933 anotaciones, ~490 conceptos | Segundo punto limpio y muy usado como benchmark |
| **MaterioMiner** | ontología de mecánica de materiales, 179 clases | 2.191 entidades, 4 publicaciones | **No** es más rica que CL, y entra igual: es el único dominio no biomédico del conjunto, y con 179 clases es el análogo más cercano que hay a la semilla cualitativa de 34 — mismo orden de inventario, misma profundidad corta |
| **CafeteriaFCD / CafeteriaSA** | FoodOn (axiomatizada, ~40k) + SNOMED-CT | ~7.400 y ~4.300 anotaciones FoodOn | Extremo de inventario grande, y dominio no clínico |

Cada uno trae su formato: HPO GSC+ y las Cafeteria usan standoff propio / brat, no Knowtator. La
tarea C2 tiene que dejar el importador con el parser de formato desacoplado del mapeo a `Mention`.

Descartados y por qué: **MedMentions** (UMLS es metatesauro, no ontología axiomatizada, y la
escala es otra), **BioNLP-OST Bacteria Biotope** (OntoBiotope, 3.602 conceptos con definiciones,
escala perfecta, pero casi todo `is_a` — no pasa 1b), **Manifesto Project** (sin ontología).

Fuera de biomedicina esto prácticamente no existe: lo publicado es tesauro SKOS (AGROVOC, EuroVoc)
o corpus diminuto. MaterioMiner es el mejor caso no biomédico encontrado.

#### Lo que ningún par de acá puede medir

`cross_language_always_grey: true` y el encoder multilingüe son decisiones sin evidencia detrás, y
los cuatro pares son en inglés, así que ninguno las toca. La vía existe —los corpus clínicos en
español del BSC contra SNOMED CT— pero pide tramitar una licencia y subsetear 360k conceptos, y
no conviene que eso bloquee C2–C7. Queda registrado como **deuda técnica 17** en
[`DEUDA_TECNICA.md`](DEUDA_TECNICA.md).

Salida de la fase: un directorio con el corpus, su ontología en RDF, y una nota de una página
sobre formato de anotación y criterio de subseteo si lo hubo.

### Fase 1 — Cablear B2 — **HECHA** (`a4df6f0`)

Era el prerrequisito de todo lo demás y ya está:

- `Target` se construye desde la versión de ontología vía `typing_store.targets_from(graph,
  config.matching.match_against)`.
- Comando CLI `match` (`cli.py:434`): corre `type_mentions`, persiste `Typing` y las entidades
  resueltas en SQLite (§8.1).
- Todos los umbrales salen de config; no hay ninguno en el código (§7).

El propio comando imprime `uncalibrated` al terminar. Esa advertencia es lo que las fases 2–4
existen para poder borrar.

### Fase 2 — Importador de corpus anotado

- Formato de entrada: texto plano + standoff (el del corpus elegido).
- Mapear a `annotation.Mention` (`gold_class`, `in_seed`) y a `matching.Mention`.
- Construir `Target` desde la ontología del corpus vía `seed.normalize_seed`, que ya acepta
  cualquier RDF con clases OWL y `rdfs:label`.
- **Saltear ingest/parse/chunking/extracción por completo.** El corpus ya es texto y ya trae las
  menciones; B1 no interviene.

Nota sobre el §12.2, que deja el importador fuera de v1: la razón declarada es que los formatos
estándar anclan offsets en texto plano mientras los del pipeline apuntan al Markdown del parser,
así que un cambio de versión del parser los corre. **Ese argumento no aplica acá** — al saltear
el parser, el texto es su propia referencia y `markdown_hash` no entra en juego. La fila del
§12.2 se refiere a reimportar exports propios desde BRAT, que sigue fuera de v1.

Estimación: ~100 líneas + ~40 de CLI.

### Fase 3 — Banco de calibración

- Barrido de umbrales sobre las métricas que `annotation.py` ya calcula (correcto, mistyped,
  falso huérfano, huérfano genuino, precisión, recall).
- Reportar la **distribución de scores** separando verdaderos de falsos, no solo el agregado: es
  lo que dice si hay un umbral que separe, además de dónde ponerlo.
- Correr el barrido bajo `match_against: label | gloss | label_and_gloss` y con
  `use_cross_encoder` en ambos valores. Cuatro a seis corridas.

Estimación: ~120 líneas.

### Fase 4 — Re-decidir las dos opciones de config — **HECHA**

Con n = 8.723 en lugar de n = 10, las dos quedaron resueltas y los comentarios de
`config/default.yaml` reescritos. Ver [Resultados de C4](#resultados-de-c4-2026-09-09).

La pregunta condicional que dejaba abierta esta fase —«si `gloss` gana acá, revisar A0.4»— se
respondió al revés y con margen: `gloss` no gana, pierde por 9× y con separación negativa. **A0.4
no se toca, pero deja de justificarse por B2**: la etapa genera glosas que el matcher no usa y no
hay evidencia de que deba. Su justificación queda siendo B4b y la lectura humana.

### Fase 5 — Volver al corpus de aplicación

> **Corregido el 2026-09-09.** Lo que sigue en esta sección describía "volver al par de
> aplicación", y ese marco era equivocado: **no hay par de aplicación**. El entregable del
> proyecto es el sistema y su caracterización, sin dominio comprometido (§1.4 del spec: "sin
> tarea downstream comprometida"), así que **todos los pares son instrumentos** y el par
> cualitativo era andamio para tener con qué probar mientras no había otra cosa. Queda archivado
> abajo lo que decía, porque explica de dónde salió la tarea C7 y por qué se retiró.

<details>
<summary>Lo que decía antes (archivado)</summary>

- Aplicar la config calibrada a la semilla cualitativa + corpus de ciencia abierta.
- Correr la curva de acumulación del §10.3 sobre ese par.
- Evaluar la compuerta del §12.1 sabiendo que el matcher está calibrado.

Con el desajuste ya medido, el resultado esperado es tasa alta. Eso deja de ser un no-go del
sistema y pasa a ser un resultado sobre el caso de aplicación: la salida es cambiar el corpus de
aplicación o cambiar la semilla.

</details>

**Lo que reemplaza a eso.** Sin dominio comprometido, la compuerta del §12.1 no es un semáforo
del proyecto: una tasa alta de falsos huérfanos sobre un par es un resultado *sobre ese par*, y
lo que califica al sistema es cómo se comporta **a través** de pares. Eso mueve el centro de
gravedad del plan a C5 —la curva de tamaño de inventario— y agrega una tarea que antes no tenía
sentido: correr el pipeline **entero** sobre un par publicado, no sólo el matcher, que es lo
único que hasta ahora se midió con respuesta conocida.

## Resultados de C4 (2026-09-09)

Par: CRAFT `CL+extensions`, 97 documentos, **8.723 menciones** gold (424 discontinuas salteadas),
inventario de **3.418 clases, 3.281 con definición (96%)**. Cuatro corridas,
`onto-pipeline calibrate craft-cl`. Crudo en `pipeline/data/calibration/craft-cl.json`.

### La decisión de etiqueta-vs-glosa, con n real

| variante | recall@1 | separación | mejor F1 (umbral) |
|---|---|---|---|
| **label · bi** | **6.090 (69,8%)** | **+1,61** | **0,774 (0,90)** |
| label_and_gloss · bi | 1.161 (13,3%) | −0,49 | 0,135 (0,45) |
| gloss · bi | 686 (7,9%) | −0,42 | 0,080 (0,40) |
| label · cross | 2.220 (25,4%) | −0,56 | 0,138 (0,30) |

*Separación* es la distancia entre la mediana del score del top-1 correcto y la del top-1
equivocado, en desvíos estándar agrupados. Es la pregunta que va **antes** de dónde poner el
umbral: si las distribuciones se pisan, ningún umbral ayuda.

**`match_against: label` queda resuelto, y en contra del §6.2.** No por poco: 69,8% contra 7,9%.
Y la condición era favorable a la glosa —96% del inventario trae definición real escrita por
curadores, no glosas generadas por un LLM—, así que la hipótesis del spec se probó donde debía
ganar. Lo que decide no es el recall sino el signo: **con glosas la separación es negativa**, los
errores puntúan más alto que los aciertos. Un umbral más exigente ahí conserva preferentemente lo
equivocado. La explicación sigue siendo la de forma: una mención es un sintagma corto y una
etiqueta también; una glosa es una oración. Se revisa con un encoder asimétrico, no con un umbral.

Consecuencia para A0.4: la etapa de generación de glosas **no alimenta al matcher** y no hay
evidencia de que deba. Sigue justificada por B4b y por lectura humana, no por B2.

**`use_cross_encoder: false` queda resuelto.** Re-rankeando el top-5 del bi-encoder, el recall@1
cae de 6.090 a 2.220 y la separación se va a −0,56. No es solo que aplaste la escala —que la
aplasta, todo entre 0,1 y 0,3, que es exactamente R1—: además ordena peor. El reranker genérico de
IR es el instrumento equivocado acá.

### Los umbrales

El F1 de tipado tiene su máximo en 0,774 con el corte en 0,90, así que **0,92 del spec está
prácticamente en el óptimo**. Lo que el F1 esconde es el intercambio: en 0,90 la tasa de falsos
huérfanos es 26,3% con 563 mal tipados; en 0,70 es 4,7% con 2.300 mal tipados. Cuál duele más es
decisión de diseño —un falso huérfano induce clases espurias en B3— y no algo que el barrido
resuelva.

**Limitación que hay que nombrar:** el barrido usa un corte, el pipeline usa dos. Lo medido es el
corte de huérfano. Dónde parte `auto` de zona gris es la pregunta separada de cuánta revisión
humana se acepta, y no se calibra contra un corpus.

### El hallazgo que no estaba buscado

`CL:0000000` está etiquetada *cell*. Los anotadores de CRAFT no la usan: usan la extension class
`CL_GO_EXT:cell`, etiquetada *cell* también, y esa clase sola es **3.262 de las 8.723 menciones
(37%)**. Con las dos en el pool el encoder no puede distinguirlas, y **2.759 menciones caían en la
que CRAFT garantiza equivocada**. El efecto sobre el número principal:

| inventario | recall@1 | separación |
|---|---|---|
| con `CL:0000000` | 3.343 (38,3%) | +0,39 |
| sin ella (default) | 6.090 (69,8%) | +1,61 |

La mitad del error medido era una colisión de modelado, no el matcher. Por eso las clases que el
corpus declara nunca-correctas salen del inventario por defecto: dejarlas es envenenar el pool a
sabiendas. `--keep-excluded` reproduce la corrida de arriba.

Es también el argumento más fuerte a favor de calibrar afuera: ese modo de falla —dos clases con
etiqueta idéntica, una correcta y otra no— es invisible en un inventario de 34 clases y aparece
solo cuando el inventario es grande de verdad.

### Huérfanos genuinos fabricados

Con `--holdout 0.2` (683 clases retenidas, determinista): separación +1,69, y la fila de huérfanos
genuinos deja de ser cero — 415 en el corte 0,90 contra 2.154 falsos. El matcher **sí** se abstiene
más sobre clases que no están en el inventario que sobre las que sí. Es evidencia débil pero es la
primera que hay sobre esa mitad de la compuerta del §12.1.

### Efecto colateral: el tipado no escalaba

Tipar 8.723 menciones contra 3.418 clases son 30 millones de productos punto que `matching.py`
hacía en el intérprete. La corrida no terminaba. Está resuelto con el mismo patrón numpy por
bloques que ya usaba `_neighbour_pairs`, con un test que fija que los dos caminos rankeen igual.
Con 34 clases el problema no existía; es el segundo hallazgo que solo aparece con un inventario
realista.

## Qué NO se tira

- Las 1.725 menciones de B1 y las anotaciones ya hechas: siguen siendo el conjunto de retención
  del corpus de **aplicación**, y el insumo de la fase 5.
- `annotation.py` completo: el modelo de datos, las métricas de huérfanos y el exportador BRAT no
  cambian. `in_seed` es el campo correcto y sigue siéndolo.
- `seed.py`, `glosses.py`, `matching.py`: sin cambios de dominio. El grep de términos cualitativos
  sobre `src/` da solo un ejemplo en un prompt (`extraction.py:42`).

## Cambios de config

```yaml
paths:
  corpus_root:        # → corpus de aplicación (sin cambio)
  seed_ontology:      # → semilla de aplicación (sin cambio)
  calibration_corpus: # NUEVO: texto + standoff
  calibration_ontology: # NUEVO: RDF con definiciones
```

Los umbrales de `matching:` pasan a ser **salida** de la fase 3, no entrada escrita a mano.

## Riesgos

| Riesgo | Mitigación |
|---|---|
| El corpus elegido resulta inaccesible o con licencia incompatible | **Resuelto en fase 0**: CRAFT verificado, CC BY 3.0, formato inspeccionado. C5 aporta los candidatos de respaldo |
| El punto de operación no transfiere al inventario de 34 clases | **C5 lo mide** en vez de suponerlo: tres inventarios de 179, 3.540 y ~40k clases dan la curva. El umbral se documenta como punto de partida y la fase 5 lo revisa sobre el par real |
| La fase 1 destapa que el matcher necesita trabajo antes de calibrar nada | Es el resultado correcto y llega antes que con el plan actual |
| Convenciones de granularidad de mención distintas entre corpus | Comparar contra la guía de anotación del corpus; no mezclar métricas de corpus distintos en un promedio |

## Orden y esfuerzo

Fase 0 está hecha (arriba) y la fase 1 también (`a4df6f0`). El camino crítico arranca en C2.

Total estimado, C2–C3: **1 a 2 días** de implementación; C5 agrega ~medio día de parsers. C6–C7
son medición y decisión.

## Tareas pendientes

Registro de trabajo a ejecutar, en el mismo espíritu que el §14.2 de la spec (T1–T4). C1–C4 y C6
están hechas; queda C5, C7 y C9.

| # | Tarea | Fase | Bloqueada por | Estimación |
|---|---|---|---|---|
| ~~C1~~ | ~~Cablear B2~~ — **hecha** en `a4df6f0`: `Target`, comando `match`, persistencia y umbrales desde config | 1 | — | — |
| ~~C2~~ | ~~Importador de corpus anotado~~ — **hecha**: `calibration.py`, readers `knowtator` y `brat` tras un registro, `pair.yml` por par, offsets validados contra el texto fuente en los 97 documentos | 2 | — | — |
| ~~C3~~ | ~~Banco de calibración~~ — **hecha**: comando `calibrate`, una pasada de encoding por variante y los umbrales aplicados encima, distribución de scores y separación reportadas aparte del agregado | 3 | C2 | — |
| ~~C4~~ | ~~Par primario CRAFT `CL+extensions`~~ — **hecha**, cuatro corridas. Ver [Resultados de C4](#resultados-de-c4-2026-09-09) | 3 | C3 | — |
| C5 | **Pares adicionales.** MaterioMiner **hecho** (ver abajo); quedan HPO GSC+ y CafeteriaFCD/CafeteriaSA. Mismo barrido. Objetivo: medir cómo se mueve el punto de operación entre inventarios de 179, 3.418 y ~40k clases, en vez de suponerlo. El reader `brat` ya está; falta el de HPO GSC+ y bajar los tres corpus | 3 | C4 | 3 pair.yml + 3 barridos |
| ~~C6~~ | ~~Re-decidir `match_against` y `use_cross_encoder`~~ — **hecha con la evidencia de C4**: ambas resueltas, comentarios de `config/default.yaml` reescritos. C5 puede refinar los umbrales, no estas dos | 4 | C4 | — |
| ~~C7~~ | ~~Volver al par de aplicación~~ — **retirada**: no hay par de aplicación, todos los pares son instrumentos. Ver la nota de corrección más arriba | 5 | — | — |
| ~~C8~~ | ~~Correr el pipeline entero sobre un par publicado~~ — **hecha** sobre MaterioMiner: de la semilla a `v2` con 45 clases inducidas. Ver [`HALLAZGOS.md`](HALLAZGOS.md) 1.13 y 1.14 | 5 | C4 | — |
| C9 | El barrido mide un corte y el pipeline usa dos. Falta decidir dónde parte `auto` de zona gris, que es cuánta revisión humana se acepta y no se calibra contra un corpus | 4 | C4 | decisión |

### Segundo par: MaterioMiner — hecho (2026-09-09)

428 clases, 4 publicaciones, 2.229 menciones gold, no biomédico. Lector `webanno` nuevo, y
`load_targets` ahora lee inventarios en RDF además de OBO — MaterioMiner publica su ontología en
Turtle, y hasta ahora el banco sólo sabía leer OBO, con lo que el inventario daba **cero clases**
sin decir por qué. Detalle completo en [`../calibration/materiominer/NOTA_FASE0.md`](../calibration/materiominer/NOTA_FASE0.md).

| | MaterioMiner | CRAFT/CL |
|---|---|---|
| Inventario | 428 clases | 3.418 |
| Separación | **1,50** | 1,69 |
| recall@1 | **21,5%** | 68,5% |
| Mejor F1 | 0,277 (0,75) | 0,774 (0,90) |

**El punto de operación cambia con cada par, y no en la dirección esperable**: un inventario ocho veces
más chico no es más fácil. Y las dos cifras se separan — la separación aguanta, el recall se cae—
lo que dice que acá el cuello no es el umbral sino la **recuperación**: el encoder distingue
acierto de error, pero la clase correcta casi nunca está primera. Es un modo de falla distinto
del de CRAFT y refuerza las deudas 17 y 19.

Falta el tercer punto de la curva (~40k clases) para tener la forma.

### Ingesta de texto plano — hecha (2026-09-09)

Primera mitad de C8. `parse_text` toma un `.txt` y **el Markdown que entrega es el archivo,
literal**: sin des-hyphenación, sin detección de encabezados, sin supresión de boilerplate. No es
minimalismo, es el requisito: las anotaciones gold indexan caracteres de ese archivo, y cualquier
normalización corre los offsets sin producir ningún error — la medición posterior simplemente da
peor y no dice por qué.

Verificado sobre los 97 artículos de CRAFT, 4.091.461 caracteres:

| Chequeo | Resultado |
|---|---|
| Markdown en disco idéntico al `.txt` fuente | 97 de 97 |
| Bloques que recuperan su texto por offset | 9.771 de 9.771 |
| Menciones gold cuyo offset cae donde dice el Markdown | **8.723 de 8.723 (100%)** |
| Menciones gold que caen enteras dentro de un bloque | **8.723 de 8.723 (100%)** |

El corpus ya está ingestado y chunkeado. Lo que falta de C8 son las etapas que cuestan llamadas
al modelo.

**Un detalle para cuando se corra:** `seed_ontology` tiene que apuntar a
`ontology/cl-base.owl`, no al `.obo` — rdflib no parsea OBO, y el `.owl` trae 123.864 tripletas
y 7.159 clases. El `.obo` lo lee `calibration.py` con su propio reader, que es otra cosa.
Consecuencia: `CL+extensions.obo` **no tiene equivalente en OWL**, así que la semilla del
pipeline no incluye las clases de extensión de CRAFT que sí usa el banco de calibración. Hay que
decidir si eso importa antes de leer los números.

C4 era la compuerta —si el matcher no separaba en ningún punto de operación, no había nada que
calibrar— y la pasó: separación +1,61 con etiquetas. Quedan **C5**, que ya no decide
`match_against` ni `use_cross_encoder` sino cuánto se mueve el punto de operación con el tamaño
del inventario, y **C8**, que saca la medición del matcher y la extiende al pipeline completo.
Las dos son ahora el trabajo principal, no tareas laterales: con el entregable siendo el sistema
y su caracterización, medir a través de pares *es* el producto.
