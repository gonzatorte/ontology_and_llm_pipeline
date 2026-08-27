# CLAUDE.md — `pipeline/`

Guía para Claude Code trabajando en este directorio. La documentación completa está en los
archivos que indexa este documento; acá va sólo lo que hace falta para no romper nada ni
redescubrir lo ya decidido.

**Este directorio es su propio repositorio git** (`master`), independiente de `../repo/`. El
CLAUDE.md del workspace padre dice que "only `repo/` is a git repository"; eso quedó
desactualizado y no aplica acá.

## Qué es

`onto-pipeline`: enriquecimiento ontológico asistido por LLM. Toma un corpus de PDFs y una
ontología semilla, y produce versiones sucesivas de la ontología con procedencia textual. Python
con `uv`, ~15.200 líneas en 45 módulos, 546 tests, CLI con ~40 comandos.

El sistema opera en inglés (prompts, esquemas, logs, docstrings). La documentación y los
comentarios de configuración son en castellano. El corpus y las glosas son bilingües es/en.

⚠️ **El entregable es el sistema y su caracterización, sin dominio objetivo.** El spec lo dice en
`SCOPE-PURPOSE` ("sin tarea downstream comprometida"). **Todos los pares (corpus, ontología) son
instrumentos**: se usan para medir cómo se comporta el pipeline, y lo que lo califica es su
comportamiento *a través* de pares, no cómo le va en uno. No hay un "caso de aplicación" al que
volver — la documentación afirmó lo contrario hasta el 2026-09-09 y se corrigió en 15 lugares.
El par de metodología cualitativa fue el andamio inicial y **está retirado**.

## Los documentos, y cuál leer

| Documento | Qué contesta | Cuándo leerlo |
|---|---|---|
| [`especificacion_pipeline_ontologia.md`](especificacion_pipeline_ontologia.md) | El diseño entero, con las 26 decisiones vinculantes | **Primero, siempre.** Es la referencia canónica, y desde el 2026-09-10 sus partes se citan por nombre (`ITER-MATCH`, `GRADED-FEEDBACK`), no por número |
| [`README.md`](README.md) | Cómo se usa cada comando y en qué estado está cada etapa | Antes de tocar el CLI o de decir que algo falta |
| [`HALLAZGOS.md`](HALLAZGOS.md) | Qué se **midió**, qué se **decidió**, y **qué se probó y no funcionó**, con el n de cada número | Antes de proponer cambiar un umbral, un encoder o una política — y **antes de proponer una idea**, porque su `LAYERS` lista las que ya se descartaron con datos |
| [`DEUDA_TECNICA.md`](DEUDA_TECNICA.md) | Qué falta, y qué conviene rehacer cuando haya evidencia | Antes de "arreglar" algo que quizás ya está registrado como deuda deliberada |
| [`plan_reglas_de_mapeo.md`](plan_reglas_de_mapeo.md) | El contrato de las reglas de mapeo: cómo la capa de menciones se vuelve ABox | Al tocar `mapping.py` o la regeneración |
| [`plan_cambio_corpus_calibracion.md`](plan_cambio_corpus_calibracion.md) | Por qué el corpus de calibración está separado del de aplicación, y las tareas la tarea «cablear el matcher»–la tarea «más pares» | Al tocar `calibration.py` o interpretar un barrido |

Los cuatro últimos son enmiendas o complementos del spec, no lo reemplazan.

**Dos documentos viven fuera del repo**, junto a los pares de calibración, porque describen datos
que no se versionan acá:

| Documento | Qué contesta |
|---|---|
| [`../calibration/README.md`](../calibration/README.md) | Qué pares hay, cuál es el primario y cuáles son tareas pendientes |
| [`../calibration/craft-cl/NOTA_FASE0.md`](../calibration/craft-cl/NOTA_FASE0.md) | De dónde salió el par primario, su licencia, y qué se decidió al importarlo |

⚠️ **Los valores de configuración que aparecen en el spec (`CONFIG`) son históricos.**
`auto_merge_threshold: 0.92` y `grey_zone_lower: 0.70` son los defaults con los que se escribió
el diseño, antes de que hubiera con qué medirlos; hoy son 0,95 y 0,80. Los vigentes están
**siempre** en `config/default.yaml`. El spec no se edita: es el registro de lo que se decidió
antes de ver datos.

## Comandos

```bash
uv sync --extra dev --extra reasoning --extra matching --extra validation
uv run pytest -q                       # 546 tests, ~5 s, sin red ni Docker
uv run ruff check .                    # line-length 100, reglas E,F,I,UP,B
./scripts/fetch-jars.sh                # OWL API + ELK + HermiT en lib/ (~80 jars)
uv run onto-pipeline --help
uv run onto-pipeline next              # qué corresponde correr, y qué espera al usuario
```

Las etapas que llaman al modelo necesitan `--env-file opencode.env` **antes** del subcomando:
`uv run onto-pipeline --env-file opencode.env extract`.

## Invariantes que no se negocian

Cada uno costó un bug o está en el spec como decisión de diseño.

1. **"El LLM clasifica y nombra. El código arma la lógica. El razonador rechaza."** Al modelo
   nunca se le pide OWL: se le hace una pregunta atómica y el código escribe el axioma.
2. **A un modelo nunca se le piden alternativas de rama** (`ITER-BRANCH`). Es la única prohibición
   explícita del spec para esa etapa. Los ejes salen del razonador y de un catálogo enumerado.
3. **ELK nunca devuelve `OK`.** Su silencio sólo significa que el axioma ofensor pudo haber sido
   ignorado: `REJECTED` / `INCONCLUSIVE` / `SKIPPED`, jamás una aprobación.
4. **Toda propiedad de anotación que se escriba tiene que estar en `seed.DECLARED_ANNOTATIONS`.**
   Escribir una que no está saca la ontología de OWL 2 DL, y el síntoma no es un error: es ELK
   salteándose en silencio. Ya pasó dos veces. Hay un test que lo fija por etapa.
5. **Ningún umbral se escribe fuera de `config/default.yaml`.** Y todo umbral nuevo se documenta
   ahí con lo que se midió o con que no se midió nada.
6. **La capa de menciones es inmutable salvo por extensión.** El ABox se deriva; la TBox se
   versiona en un DAG. `regenerate` es función pura de (menciones, tipados, reglas): no consulta
   modelo ni razonador, y nunca escribe en la capa de menciones.
7. **Nada de algoritmos de grafo sobre la serialización RDF de la TBox.** La disjointness invierte
   el signo bajo similitud estructural: dos clases declaradas incompatibles se ven conectadas
   (`LAYERS-ONTOLOGY-NOT-GRAPH`). Clustering sobre el grafo de menciones huérfanas sí está permitido.
8. **Mundo abierto.** No asertar X y asertar ¬X son cosas distintas. Ausencia de contraejemplo no
   es prueba; sólo el contraejemplo es conocimiento.

## Convenciones de trabajo

- **Identificadores legibles — y siempre identificar.** No quedan códigos ni números de sección
  en ningún lado: ni `A0.1`, ni `B5`, ni `D9`, ni `§6.6`. Cada parte del diseño tiene un nombre y
  **el nombre es el identificador**. Las tres reglas:
  1. **El id de una sección lleva el de su padre**: `PREP-NORMALIZE-IRIS` está dentro de
     `PREP-NORMALIZE`, que está dentro de `PREP`. Se lee dónde vive sin abrir nada.
  2. **Número sólo cuando los hermanos son pasos de una secuencia y el orden pesa**:
     `ITER-VALIDATE-1-ELK` … `ITER-VALIDATE-7-STRUCTURE`, `BUILD-STEP-1` … `BUILD-STEP-5`.
     Un número que sólo dice «se registró antes» no va: eso lo dice el orden del índice.
  3. **Sacar el identificador y dejar la frase suelta es peor que el código.** «La revisión que
     el pipeline existe para evitar» no se puede buscar ni citar, y dos párrafos que hablan de lo
     mismo dejan de parecerse. Se escribe la frase **y** `BRANCH-ONLY-REVIEW`.

  Vale para todo lo que se cite: secciones del spec, decisiones (`GRADED-FEEDBACK`), riesgos
  (`RISKS-FALSE-ORPHANS`), tareas, entradas de deuda (`DEBT-FEEDBACK-HISTORY`) y hallazgos
  (`FINDINGS-MEASURED-RETRIEVAL-CEILING`). **El índice está en el [README](README.md)**, y todo
  nombre nuevo se da de alta ahí. Los comandos del CLI son la hoja de su id: `ITER-EXTRACT` se
  corre con `extract`.
- **Commitear después de cada hito**, no al final. El mensaje explica *por qué*, no *qué*.
- **`DEUDA_TECNICA.md` es para mejoras a futuro, no para bugs.** Lo que está roto se arregla.
  Cada entrada lleva un id `DEBT-…`, así que dos sesiones en paralelo no colisionan como
  colisionaban los números.
- **No leer fuera de `pipeline/` sin preguntar.** El corpus, la ontología semilla y los pares de
  calibración viven afuera y el config los apunta; leer otra cosa del workspace es pedir permiso
  primero.
- **Los tests describen el porqué.** Los nombres son frases (`test_a_forced_parent_is_worse...`)
  y el docstring dice qué decisión de diseño fija. Un test nuevo que sólo verifica mecánica no
  está a tono con el resto.
- **`pkill -f` se matchea a sí mismo.** Ya colgó dos shells en este proyecto. Matar por PID.

## Dónde está cada cosa

```
src/onto_pipeline/
  cli.py            todos los comandos (Typer). Grande a propósito: una etapa, un comando
  config.py         la superficie de configuración; rechaza valores no implementados
  seed.py           `PREP-NORMALIZE`: IRIs opacos, etiquetas, erratas, DECLARED_ANNOTATIONS
  parse.py ingest.py classify.py boilerplate.py chunking.py     corpus -> bloques -> chunks
  extraction.py coreference.py                                   chunks -> menciones
  matching.py typing_store.py embeddings.py                      menciones -> clases + zona gris
  bridging.py induction.py axiomatization.py                     huérfanas -> clases nuevas
  branching.py                                                   ejes de decisión y ramas
  enrichment.py glosses.py                                       glosas: bootstrap y corpus
  conflicts.py                                                   documentos que se contradicen
  validation.py ontoclean.py structural.py reasoning.py          la cadena `ITER-VALIDATE` completa
  functional.py                                                  propiedades funcionales (`ITER-APPLY`)
  mapping.py versioning.py                                       ABox y DAG de versiones
  cq.py cq_generation.py stopping.py                             CQ y criterios de parada
  orchestration.py                                               qué corresponde correr
  review.py                                                      hallazgos esperando decisión
  annotate.py annotation.py                                      conjunto de retención (`EVAL-PIPELINE`)
  calibration.py                                                 barrido contra corpus publicado
  llm.py providers.py telemetry.py                               proveedor, caché y costos
  db.py language.py terms.py report.py                           almacén y utilidades
config/default.yaml   TODA la configuración, con el porqué de cada valor en comentarios
tests/                un archivo por módulo; sin red, sin Docker, sin JVM
lib/                  jars del razonador (gitignored, los baja fetch-jars.sh)
data/                 almacén SQLite, artefactos derivados (gitignored)
```

**El ledger de unidades de trabajo (`telemetry.py`) es una sola cosa haciendo tres**: caché,
checkpoint y telemetría de costos. La clave de caché cubre el prompt, la temperatura, **el modelo
y su reasoning effort** — omitir el modelo servía en silencio resultados de otro tier, y hay un
test que lo fija.

## Cómo trabaja este usuario

Preferencias expresadas explícitamente. No son estilo: cambiaron el rumbo del trabajo cuando se
ignoraron.

- **Preguntar no es pedir que se implemente.** Cuando hace una pregunta, quiere la respuesta —
  no la respuesta y además el cambio ya hecho. Empezar a implementar sin que lo pida es la
  corrección que más veces tuvo que hacer.
- **Commitear después de cada hito**, con el log de los cambios en el mensaje. No una tanda al
  final.
- **Nombres mnemotécnicos, no códigos — pero nombres, no ausencia de nombre.** Lo pidió cuatro
  veces. La tercera fue con fastidio; la cuarta fue para corregir que, al sacar los códigos, se
  habían quedado frases sin identificador, y eso empobrece el texto en vez de mejorarlo.
- **Documentar mientras se construye**: README, deuda técnica y el porqué de cada decisión,
  no como paso final separado.
- **La deuda técnica es para mejoras a futuro**, no para llevar la cuenta de bugs.
- **Verificar contra el repo antes de contestar.** "Lee el estado del repositorio antes de
  modificar o contestar" — dicho tal cual, más de una vez, y en general porque la respuesta
  anterior había salido de la memoria y no de los archivos.
- **Localidad.** No leer fuera de `pipeline/` sin preguntar primero. Los pares de calibración y
  el corpus están afuera y el config los apunta; cualquier otra cosa se pide.
- **Nada de correr trabajos de horas en esta máquina.** Si un ajuste o un barrido no termina
  en minutos, se propone y se espera: puede configurar un proveedor en la nube.
- **Castellano para hablar y documentar**, inglés para el código.

Dos cosas sobre el proveedor, para que nadie las vuelva a plantear:

- La credencial de `opencode.ai` vive en `opencode.env` (gitignored, y `example.env` es la
  plantilla). El usuario decidió **no rotarla** y usarla así; está decidido, no hace falta
  volver a advertirlo.
- Pidió una vez cambiar el `USER_AGENT` para presentarse como otro cliente ante el proveedor.
  **No se hizo**, y el `USER_AGENT` honesto de `providers.py` se queda: hacerlo sería mentirle
  a un tercero sobre quién lo está llamando. No presionó, y siguió usando el proveedor como
  estaba. Si vuelve a salir, la respuesta es la misma.

## Qué falta, en una línea

**La lista de lo comprometido y pendiente está en la sección "En cola" del
[README](README.md)**, con el porqué de cada uno y a qué entrada de deuda mirar. Empezar por ahí
antes de proponer trabajo nuevo.


La secuencia de construcción del spec (`BUILD`) está dada en sus cinco pasos y la tabla de estado del
README lo detalla etapa por etapa. Lo que **no** está, y conviene saberlo antes de prometer nada:

- **`ITER-TUNE`, el ajuste del matcher (LoRA)** — bloqueado por datos, no por código: hace falta que
  alguien conteste unos cientos de pares de zona gris. Deuda 8i.
- **`ITER-FEEDBACK`, el historial de feedback** — **cerrado el 2026-09-10**: esquema `GRADED-FEEDBACK`, forma normal y
  precedentes en el prompt de `axiomatize`. Lo que falta no es código sino una segunda iteración
  con feedback humano real, para ver si algún precedente mueve un juicio. Deuda 20.
- **La compuerta no-go de `BUILD-NO-GO-GATE` sigue abierta**, y es la que decide si tiene sentido seguir
  construyendo encima. Ver el README.

## Antes de proponer una idea

**Mirar `LAYERS` de [`HALLAZGOS.md`](HALLAZGOS.md), "Lo que se probó y no funcionó".** Siete formas de
meterle más texto a la comparación están medidas y todas empeoran; la cobertura léxica como
veredicto de alineación da el resultado invertido; reusar un re-ranker entre dominios resta. Cada
una parecía razonable antes de medirla, y por eso están anotadas.

## Antes de decir que algo falta

Mirar la tabla de estado del README y las entradas de `DEUDA_TECNICA.md`. Varias cosas que
parecen faltantes son decisiones: el catálogo de patrones de modelado tiene tres entradas y una
sin detector a propósito, el filtro de pitfalls es un subconjunto local de OOPS! y no OOPS!, y
`next` no ejecuta nada por una razón escrita.
