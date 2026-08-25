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

**4. B2 no está cableado.** `Matcher` solo lo ejercitan los tests; no hay construcción de
`Target` en `src/`. La compuerta del §12.1 nunca corrió end-to-end.

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
2. **Escala del inventario comparable.** Miles, no millones: con los ~34 candidatos actuales casi
   cualquier mención rankea en algún lado, y con los millones de UMLS el punto de operación es
   otro. Subsetear es aceptable si se documenta el criterio.
3. **Inglés**, o agregar marcadores a `language.py` (~10 líneas, o usar el `/Lang` declarado).
4. Licencia que permita el uso.

Candidato recomendado: **CRAFT** — artículos biomédicos completos anotados contra GO, ChEBI,
NCBI Taxonomy, Cell Ontology; esas ontologías traen definiciones. Alternativas: MedMentions
(demasiado grande para calibrar con este inventario), Manifesto Project (análisis de contenido
estricto, multilingüe con español, pero sin ontología con glosas).

**Verificar licencia y formato antes de comprometerse.** No dar por sentada la disponibilidad.

Salida de la fase: un directorio con el corpus, su ontología en RDF, y una nota de una página
sobre formato de anotación y criterio de subseteo si lo hubo.

### Fase 1 — Cablear B2 (necesario aunque no se cambie el corpus)

Es el trabajo que falta con o sin corpus nuevo, y es prerrequisito de todo lo demás.

- Construir `Target` desde la semilla normalizada + las glosas de A0.4 (hoy no existe ese código).
- Comando CLI `match` que corra `type_mentions` sobre las menciones almacenadas y persista los
  `Typing` en SQLite (§8.1).
- Respetar `match_against`, `auto_merge_threshold`, `grey_zone_lower`, `use_cross_encoder`,
  `cross_language_always_grey` desde config — nada de umbrales en el código (§7).

Estimación: ~150 líneas, medio día a un día.

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
| El corpus elegido resulta inaccesible o con licencia incompatible | Verificar en fase 0 antes de escribir código; tener un segundo candidato |
| El punto de operación no transfiere al inventario de 34 clases | Documentar el umbral como punto de partida; la fase 5 lo revisa sobre el par real |
| La fase 1 destapa que el matcher necesita trabajo antes de calibrar nada | Es el resultado correcto y llega antes que con el plan actual |
| Convenciones de granularidad de mención distintas entre corpus | Comparar contra la guía de anotación del corpus; no mezclar métricas de corpus distintos en un promedio |

## Orden y esfuerzo

Fase 0 bloquea a la 2. La fase 1 es independiente y puede empezar ya.

Total estimado, fases 1–3: **2 a 3 días** de implementación. Fases 4–5 son medición y decisión.
