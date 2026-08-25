# Plan: separar el corpus de calibración del corpus de aplicación

Enmienda al §12 (secuencia de construcción) de `especificacion_pipeline_ontologia.md`.

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
| **MaterioMiner** | ontología de mecánica de materiales, 179 clases | 2.191 entidades, 4 publicaciones | **No** es más rica que CL; es el control de inventario chico, el análogo más cercano a la semilla de 34 |
| **CafeteriaFCD / CafeteriaSA** | FoodOn (axiomatizada, ~40k) + SNOMED-CT | ~7.400 y ~4.300 anotaciones FoodOn | Extremo de inventario grande, y dominio no clínico |

Cada uno trae su formato: HPO GSC+ y las Cafeteria usan standoff propio / brat, no Knowtator. La
tarea C2 tiene que dejar el importador con el parser de formato desacoplado del mapeo a `Mention`.

Descartados y por qué: **MedMentions** (UMLS es metatesauro, no ontología axiomatizada, y la
escala es otra), **BioNLP-OST Bacteria Biotope** (OntoBiotope, 3.602 conceptos con definiciones,
escala perfecta, pero casi todo `is_a` — no pasa 1b), **Manifesto Project** (sin ontología).

Fuera de biomedicina esto prácticamente no existe: lo publicado es tesauro SKOS (AGROVOC, EuroVoc)
o corpus diminuto. MaterioMiner es el mejor caso no biomédico encontrado.

#### Opción abierta: calibrar también el caso cross-lingüe

`cross_language_always_grey: true` y el encoder multilingüe son decisiones **sin ninguna evidencia
detrás**, y ningún par de arriba las toca porque todos son en inglés. Los corpus del BSC
(**DisTEMIST**, **SympTEMIST**, **MedProcNER**: 1.000 casos clínicos en español cada uno,
normalizados a SNOMED CT, brat standoff) son la única vía encontrada. SNOMED CT es lo más
axiomatizado disponible (EL++, definiciones lógicas en casi todo el vocabulario) y **Argentina es
país miembro de SNOMED International, con lo cual la Affiliate License es gratuita**. Contra: 360k
conceptos, hay que subsetear y documentar el criterio. No está en el camino crítico; queda como
C8.

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

### Fase 4 — Re-decidir las dos opciones de config

Con n real en lugar de n=10, resolver `match_against` y `use_cross_encoder`, y **reescribir los
comentarios de `config/default.yaml`** citando la nueva evidencia. Los comentarios actuales
documentan honestamente que la medición fue sobre diez pares; deben dejar de decir eso.

Si `gloss` gana acá, revisar A0.4: la etapa existe para alimentar un matcher que hoy no la usa.

### Fase 5 — Volver al corpus de aplicación

- Aplicar la config calibrada a la semilla cualitativa + corpus de ciencia abierta.
- Correr la curva de acumulación del §10.3 sobre ese par, que es la métrica que distingue si el
  problema está en el pipeline o en los datos.
- Evaluar la compuerta del §12.1 **sabiendo** que el matcher está calibrado, así una tasa alta de
  falsos huérfanos se lee como lo que es: señal sobre el par corpus/semilla, no sobre el matcher.

Con el desajuste ya medido, el resultado esperado es tasa alta. Eso deja de ser un no-go del
sistema y pasa a ser un resultado sobre el caso de aplicación: la salida es cambiar el corpus de
aplicación (uno que reporte estudios cualitativos) o cambiar la semilla. Decisión de dominio, no
de ingeniería.

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

Registro de trabajo a ejecutar, en el mismo espíritu que el §14.2 de la spec (T1–T4). C1 está
hecha; el resto no está implementado.

| # | Tarea | Fase | Bloqueada por | Estimación |
|---|---|---|---|---|
| ~~C1~~ | ~~Cablear B2~~ — **hecha** en `a4df6f0`: `Target`, comando `match`, persistencia y umbrales desde config | 1 | — | — |
| C2 | Importador de corpus anotado: parser de formato desacoplado del mapeo a `annotation.Mention` / `matching.Mention`. Empezar por Knowtator XML (CRAFT); dejar lugar para brat y TSV. Saltea ingest/parse/chunking/extracción: el corpus ya es texto y ya trae las menciones | 2 | — | ~100 líneas + ~40 CLI |
| C3 | Banco de calibración: barrido de umbrales sobre las métricas que `annotation.py` ya calcula, reportando la **distribución de scores** separando verdaderos de falsos, no solo el agregado | 3 | C2 | ~120 líneas |
| C4 | **Par primario: CRAFT `CL+extensions` + `cl.owl` completo de OBO Foundry** (no el `.obo` del repo). Correr C3 bajo `match_against` ∈ {label, gloss, label_and_gloss} × `use_cross_encoder` ∈ {true, false}. Usar `unused_classes_for_CL_annotations.txt` como falsos positivos garantizados | 3 | C3 | 4–6 corridas |
| C5 | **Pares adicionales: HPO GSC+, MaterioMiner, CafeteriaFCD/CafeteriaSA.** Mismo barrido. Objetivo declarado: medir cómo se mueve el punto de operación entre inventarios de 179, 3.540 y ~40k clases, en vez de suponerlo. Requiere los parsers de formato extra de C2 | 3 | C4 | ~60 líneas de parsers + 3 barridos |
| C6 | Re-decidir `match_against` y `use_cross_encoder` con la evidencia de C4/C5 y **reescribir los comentarios de `config/default.yaml`**, que hoy documentan honestamente un n=10 que dejará de ser cierto | 4 | C5 | medición |
| C7 | Volver al par de aplicación: config calibrada sobre semilla cualitativa + corpus de ciencia abierta, curva de acumulación del §10.3, evaluar la compuerta del §12.1 | 5 | C6 | medición |
| C8 | *Opcional.* Par cross-lingüe (SympTEMIST + subset de SNOMED CT) para calibrar `cross_language_always_grey`, hoy sin evidencia. Fuera del camino crítico | 3 | C3 | Affiliate License + subseteo |

C2 puede empezar ya: la fase 0 está resuelta y C1 está hecha. C4 es la compuerta: si ahí el
matcher no separa verdaderos de falsos en ningún punto de operación, C5 en adelante no se corre —
el resultado es
que el matcher necesita trabajo, que es el hallazgo correcto y llega antes que con el plan previo.
