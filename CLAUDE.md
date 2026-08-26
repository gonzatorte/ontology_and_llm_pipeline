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
con `uv`, ~13.500 líneas en 43 módulos, 460 tests, CLI con ~30 comandos.

El sistema opera en inglés (prompts, esquemas, logs, docstrings). La documentación y los
comentarios de configuración son en castellano. El corpus y las glosas son bilingües es/en.

## Los documentos, y cuál leer

| Documento | Qué contesta | Cuándo leerlo |
|---|---|---|
| [`especificacion_pipeline_ontologia.md`](especificacion_pipeline_ontologia.md) | El diseño: 14 secciones, decisiones D1–D25, secuencia de construcción §12 | **Primero, siempre.** Es la referencia canónica; las etapas se citan por su §. 1.353 líneas |
| [`README.md`](README.md) | Cómo se usa cada comando y en qué estado está cada etapa | Antes de tocar el CLI o de decir que algo falta |
| [`HALLAZGOS.md`](HALLAZGOS.md) | Qué se **midió** y qué se **decidió** implementando, con el n de cada número | Antes de proponer cambiar un umbral, un encoder o una política. Ahí está por qué son como son |
| [`DEUDA_TECNICA.md`](DEUDA_TECNICA.md) | Qué falta, y qué conviene rehacer cuando haya evidencia | Antes de "arreglar" algo que quizás ya está registrado como deuda deliberada |
| [`plan_reglas_de_mapeo.md`](plan_reglas_de_mapeo.md) | El contrato de las reglas de mapeo: cómo la capa de menciones se vuelve ABox | Al tocar `mapping.py` o la regeneración |
| [`plan_cambio_corpus_calibracion.md`](plan_cambio_corpus_calibracion.md) | Por qué el corpus de calibración está separado del de aplicación, y las tareas C1–C5 | Al tocar `calibration.py` o interpretar un barrido |

Los cuatro últimos son enmiendas o complementos del spec, no lo reemplazan.

## Comandos

```bash
uv sync --extra dev --extra reasoning --extra matching --extra validation
uv run pytest -q                       # 460 tests, ~3 s, sin red ni Docker
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
2. **A un modelo nunca se le piden alternativas de rama** (§6.6). Es la única prohibición
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
   (§3.1). Clustering sobre el grafo de menciones huérfanas sí está permitido.
8. **Mundo abierto.** No asertar X y asertar ¬X son cosas distintas. Ausencia de contraejemplo no
   es prueba; sólo el contraejemplo es conocimiento.

## Convenciones de trabajo

- **Evitar la nomenclatura `A1`, `A2`, `A0.1`, `B2`** en texto nuevo que lea una persona. Los
  códigos del spec siguen siendo la referencia cruzada canónica y se pueden citar como tal, pero
  las etapas se nombran por su nombre mnemotécnico: `extract`, `match`, `bridge`, `induce`,
  `axiomatize`, `branch`, `enrich`, `validate`. La tabla de equivalencias está en el README.
- **Commitear después de cada hito**, no al final. El mensaje explica *por qué*, no *qué*.
- **`DEUDA_TECNICA.md` es para mejoras a futuro, no para bugs.** Lo que está roto se arregla.
  Al agregar una entrada, mirar el último `###`: los números colisionan cuando dos sesiones
  escriben en paralelo, y conviene referenciar por título y no por número.
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
  seed.py           A0: IRIs opacos, etiquetas, erratas, DECLARED_ANNOTATIONS
  parse.py ingest.py classify.py boilerplate.py chunking.py     corpus -> bloques -> chunks
  extraction.py coreference.py                                   chunks -> menciones
  matching.py typing_store.py embeddings.py                      menciones -> clases + zona gris
  bridging.py induction.py axiomatization.py                     huérfanas -> clases nuevas
  branching.py                                                   ejes de decisión y ramas
  enrichment.py glosses.py                                       glosas: bootstrap y corpus
  conflicts.py                                                   documentos que se contradicen
  validation.py ontoclean.py structural.py reasoning.py          la cadena B5 completa
  functional.py                                                  propiedades funcionales (§6.8)
  mapping.py versioning.py                                       ABox y DAG de versiones
  cq.py cq_generation.py stopping.py                             CQ y criterios de parada
  orchestration.py                                               qué corresponde correr
  review.py                                                      hallazgos esperando decisión
  annotate.py annotation.py                                      conjunto de retención (§10.1)
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

## Antes de decir que algo falta

Mirar la tabla de estado del README y las entradas de `DEUDA_TECNICA.md`. Varias cosas que
parecen faltantes son decisiones: el catálogo de patrones de modelado tiene tres entradas y una
sin detector a propósito, el filtro de pitfalls es un subconjunto local de OOPS! y no OOPS!, y
`next` no ejecuta nada por una razón escrita.
