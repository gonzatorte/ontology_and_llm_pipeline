# CLAUDE.md — `pipeline/`

Guía para Claude Code trabajando en este directorio. La documentación completa está en los
archivos que indexa este documento; acá va sólo lo que hace falta para no romper nada ni
redescubrir lo ya decidido.

**Este directorio es su propio repositorio git** (`main`), independiente de `../repo/`. El
CLAUDE.md del workspace padre dice que "only `repo/` is a git repository"; eso quedó
desactualizado y no aplica acá.

## Qué es

`onto-pipeline`: enriquecimiento ontológico asistido por LLM. Toma un corpus de PDFs y una
ontología inicial, y produce versiones sucesivas de la ontología con procedencia textual. Python
con `uv`, con una suite que corre sin red ni Docker e incluye un test de punta a punta. **Tres
interfaces sobre el mismo pipeline**: el CLI de banderas (`onto-pipeline --help` las lista),
`wizard`, que recorre el mismo plan preguntando en cada punto de decisión, y la API REST, que
hace lo mismo por HTTP y encola lo que tarda. Las tres llaman a `services/`.

El sistema opera en inglés (prompts, esquemas, logs, docstrings). La documentación y los
comentarios de configuración son en castellano. El corpus y las glosas son bilingües es/en.

⚠️ **El entregable es el sistema y su caracterización, sin dominio objetivo.** El spec lo dice en
`SCOPE-PURPOSE` ("sin tarea downstream comprometida"). **Todos los casos de uso —cada uno un par
(corpus, ontología)— son instrumentos**: se usan para medir cómo se comporta el pipeline, y lo
que lo califica es su comportamiento *a través* de ellos, no cómo le va en uno. No hay un "caso
de aplicación" al que volver — la documentación afirmó lo contrario hasta el 2026-09-09 y se
corrigió en 15 lugares.
El caso de uso de metodología cualitativa fue el andamio inicial y **está retirado**.

## Los documentos, y cuál leer

| Documento | Qué contesta | Cuándo leerlo |
|---|---|---|
| [`main_plan.md`](main_plan.md) | El diseño entero, con las 26 decisiones vinculantes | **Primero, siempre.** Es la referencia canónica, y desde el 2026-09-10 sus partes se citan por nombre (`ITER-MATCH`, `GRADED-FEEDBACK`), no por número |
| [`README.md`](README.md) | Cómo se usa cada comando y en qué estado está cada etapa | Antes de tocar el CLI o de decir que algo falta |
| [`findings.md`](findings.md) | Qué se **midió**, qué se **decidió**, y **qué se probó y no funcionó**, con el n de cada número | Antes de proponer cambiar un umbral, un encoder o una política — y **antes de proponer una idea**, porque su `LAYERS` lista las que ya se descartaron con datos |
| [`technical_debt.md`](technical_debt.md) | Qué falta, y qué conviene rehacer cuando haya evidencia | Antes de "arreglar" algo que quizás ya está registrado como deuda deliberada |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Cómo se trabaja sobre este repo: setup, worktrees por sesión, commits, dónde va una etapa nueva | Al entrar al repo por primera vez, y antes de escribir en paralelo con otra sesión |
| [`NOTICE.md`](NOTICE.md) | Qué licencia tiene esto y qué **no** cubre: los corpus y ontologías de los casos de uso son de terceros | Antes de redistribuir cualquier cosa que salga de acá |
| [`mapping_rules_plan.md`](mapping_rules_plan.md) | El contrato de las reglas de mapeo: cómo la capa de menciones se vuelve ABox | Al tocar `mapping.py` o la regeneración |
| [`use_case_selection.md`](use_case_selection.md) | Por qué **estos** casos de uso y no otros: los criterios, lo que se midió de cada candidato y por qué se descartó cada descarte | Antes de agregar uno, y antes de proponer uno que ya se descartó |
| [`api_plan.md`](api_plan.md) | El diseño de la interfaz REST: sus decisiones (`API-*`), qué se dejó afuera y qué falta para desplegarla | Al tocar `interfaces/api/`, `jobs.py`, `uploads.py` o la capa de artefactos |

Los dos últimos son enmiendas o complementos del spec, no lo reemplazan.

**Dos documentos viven al lado de los casos de uso**, en `use_cases/`, porque describen datos que
están adentro del repo pero **no se versionan** — 40 MB de artefactos publicados de terceros,
gitignoreados salvo estos dos y los `use_case.yml`:

| Documento | Qué contesta |
|---|---|
| [`use_cases/README.md`](use_cases/README.md) | Qué casos de uso hay, cuál es el primario, y cómo se agrega uno |
| [`use_cases/craft-cl/PROCEDENCIA.md`](use_cases/craft-cl/PROCEDENCIA.md) | De dónde salió el caso de uso primario, su licencia, y qué se decidió al importarlo |

⚠️ **Los valores de configuración que aparecen en el spec (`CONFIG`) son históricos.**
`auto_merge_threshold: 0.92` y `grey_zone_lower: 0.70` son los defaults con los que se escribió
el diseño, antes de que hubiera con qué medirlos; hoy son 0,95 y 0,80. Los vigentes están
**siempre** en `config/default.yaml`. El spec no se edita: es el registro de lo que se decidió
antes de ver datos.

## Comandos

```bash
uv sync --extra dev --extra reasoning --extra matching --extra validation
uv run pytest -q                       # la suite entera, sin red ni Docker
uv run ruff check .                    # line-length 100, reglas E,F,I,UP,B
./scripts/fetch-jars.sh                # OWL API + ELK + HermiT en lib/
uv run onto-pipeline --help
uv run onto-pipeline session list      # las sesiones de usuario que hay
uv run onto-pipeline next              # qué corresponde correr, y qué espera al usuario
uv run onto-pipeline wizard            # lo mismo, pero preguntando en vez de frenar
uv run onto-pipeline export            # la ontología terminada: TBox + ABox + manifiesto
```

**Hacen falta Postgres y un bucket, también en local**: `docker compose up -d` los levanta —MinIO
para lo segundo— y crea el bucket. No hay modo «sin servicios»: dos implementaciones significan
que la que se prueba no es la que se despliega. La suite sí corre sin nada.

La API es **otro comando**, porque `onto-pipeline` es la interfaz de uso y no la de gestión:

```bash
uv sync --extra api
uv run onto-pipeline-api serve         # levantar la API; sin token en el entorno no arranca
uv run onto-pipeline-api jobs list     # la cola; `jobs sweep` destraba lo que quedó colgado
uv run onto-pipeline-api uploads list  # los corpus subidos; `uploads delete <id>` los borra
```

Las etapas que llaman al modelo necesitan `--env-file opencode.env` **antes** del subcomando:
`uv run onto-pipeline --env-file opencode.env extract`.

## Invariantes que no se negocian

Cada uno costó un bug o está en el spec como decisión de diseño.


### `MODEL-NAMES-CODE-BUILDS`

**"El LLM clasifica y nombra. El código arma la lógica. El razonador rechaza."** Al modelo
nunca se le pide OWL: se le hace una pregunta atómica y el código escribe el axioma.

### `NO-MODEL-BRANCHES`

**A un modelo nunca se le piden alternativas de rama** (`ITER-BRANCH`). Es la única prohibición
explícita del spec para esa etapa. Los ejes salen del razonador y de un catálogo enumerado.

### `ELK-NEVER-APPROVES`

**ELK nunca devuelve `OK`.** Su silencio sólo significa que el axioma ofensor pudo haber sido
ignorado: `REJECTED` / `INCONCLUSIVE` / `SKIPPED`, jamás una aprobación.

### `DECLARED-ANNOTATIONS-ONLY`

**Toda propiedad de anotación que se escriba tiene que estar en `initial_ontology.DECLARED_ANNOTATIONS`.**
Escribir una que no está saca la ontología de OWL 2 DL, y el síntoma no es un error: es ELK
salteándose en silencio. Ya pasó dos veces. Hay un test que lo fija por etapa.

### `THRESHOLDS-IN-CONFIG`

**Ningún umbral se escribe fuera de `config/default.yaml`.** Y todo umbral nuevo se documenta
ahí con lo que se midió o con que no se midió nada.

### `MENTIONS-APPEND-ONLY`

**La capa de menciones es inmutable salvo por extensión.** El ABox se deriva; la TBox se
versiona en un DAG. `regenerate` es función pura de (menciones, tipados, reglas): no consulta
modelo ni razonador, y nunca escribe en la capa de menciones.

### `LAYERS-ONTOLOGY-NOT-GRAPH`

**Nada de algoritmos de grafo sobre la serialización RDF de la TBox.** Lleva el nombre de la
sección del spec que lo funda, porque es la misma idea y no dos. La disjointness invierte el
signo bajo similitud estructural: dos clases declaradas incompatibles se ven conectadas.
Clustering sobre el grafo de menciones huérfanas sí está permitido.

### `OPEN-WORLD`

**Mundo abierto.** No asertar X y asertar ¬X son cosas distintas. Ausencia de contraejemplo no
es prueba; sólo el contraejemplo es conocimiento.

### `SERVICES-NO-INTERFACE`

**Ningún servicio importa `typer`, `rich`, `fastapi` ni `uvicorn` — ni nada de `interfaces/`.**
Hay más de una interfaz sobre el mismo pipeline y el cuerpo de una etapa no puede pertenecer
a ninguna: las de terminal son el CLI de banderas y `wizard`, y la de HTTP es la API REST.
Todas viven en `interfaces/`, que es la carpeta que traduce un protocolo a la capa de
servicios y no tiene lógica de dominio. El contrato está en `services/__init__.py`;
`tests/test_services.py` lo fija leyendo los imports, y prohíbe `interfaces` además de las
bibliotecas, porque importar `render` es importar `rich` con un rodeo. Etapa nueva: va en
`services/`, se muestra en `interfaces/render.py`, y la llaman todas las interfaces.

### `STORE-NO-DIALECT`

**Ningún módulo sabe contra qué motor corre el almacén.** El SQL se escribe con `?` y las
filas se leen por nombre; lo que difiere entre motores vive en `store.py` y en ningún otro lado.
En runtime el motor es uno —Postgres, ver `ONE-SUBSTRATE-PER-CONCERN`—, y el corte sigue
existiendo porque los tests corren sobre SQLite: si se rompe, la suite deja de decir algo sobre
lo que se despliega. Lo demás se escribe portable: `COALESCE` y no `IFNULL`, `CASE WHEN` y no
`SUM(booleano)`, el JSON se lee en Python y no con `json_extract`. `tests/test_store.py`
corre el mismo contrato contra los dos.

### `SESSION-SCOPED-DATA`

**Todo dato derivado pertenece a una sesión de usuario, y toda consulta lo filtra.** Los ids
de documento y de mención derivan del corpus, así que dos sesiones sobre el mismo generan
los mismos: sin el filtro, la segunda le **borra** las menciones a la primera. Las
excepciones son tres y están escritas: lo que cuelga de `version_id` —que es
`<sesión>:v<N>`, único globalmente—, el **resultado** de `work_units`, que es
content-addressed y se comparte para no pagar dos veces, y lo que la API guarda como material
de entrada —los **uploads**, que se comparten como cualquier caso de uso publicado
(`API-SHARED-UPLOADS`)—. `tests/test_session_scope.py` lee el código y falla si alguna consulta
se olvida; las excepciones están ahí escritas, y la cola de `jobs` es una: un worker reclama el
más viejo de cualquier sesión, y la vista de administración las mira todas.

### `PHASE-DERIVED`

**La fase de una sesión se deriva de los datos.** Hay una columna `phase`, pero es una
afirmación: `sessions.observed_phase` cuenta filas y `sync_phase` la corrige antes de que
alguien la lea. Volver a `PREP` desde `ITER` **no borra**: dice qué queda atrás y ramifica.

### `DECISION-NEVER-CROSSED`

**Ninguna interfaz cruza un punto de decisión.** `next` frena ante uno, `wizard` lo
pregunta y la API no encola lo que el plan da como bloqueado: las tres cosas son la misma
regla. Correr lo que viene después de una decisión que nadie tomó es tomarla por default,
que es lo que `BRANCH-ONLY-REVIEW` nombra. Por lo mismo, una decisión no se registra
mientras la sesión tiene un job en vuelo: se estaría decidiendo sobre un estado que cambia
debajo.

### `USAGE-CLI-NOT-ADMIN`

**El CLI de `onto-pipeline` es la interfaz de uso, no la de gestión.** Sirve para enriquecer una
ontología: quien la enriquece no crea uploads, no mira la cola de jobs ni destraba una sesión.
Administrar un despliegue es `onto-pipeline-api`, que es otro comando y otro entry point. El
corte no es estético — con los dos mezclados, la mitad de los verbos del comando que documenta
el pipeline hablarían de otra cosa, y lo que es un detalle de cómo se lo expone parecería parte
de lo que hace.

De ahí se sigue dónde vive cada módulo: lo que existe **sólo** para que se pueda usar por HTTP
—`jobs`, `uploads`— vive en `interfaces/api/` y el core no lo importa. Lo que toda etapa
necesita para escribir —`objectstore`, `artifacts`— es del core aunque lo haya traído la API, por
la misma razón que `store.py` lo es: un servicio no puede importar de una interfaz
(`SERVICES-NO-INTERFACE`).

### `ARTIFACTS-NOT-FILES`

**Ningún módulo del core abre un archivo para escribir una salida.** Toda salida derivada —el
Markdown, los recortes, la ontología normalizada, el ABox, los diffs, el export, el informe del
parser, la herramienta de anotación, el par de brat, el barrido de calibración— se nombra con
una clave y se escribe por `artifacts`. Dónde caen los bytes lo decide `objectstore` según la
configuración, y el que escribe no se entera: es el mismo corte que `store.py` hace con el motor
de base.

**No es lo mismo que `ONE-SUBSTRATE-PER-CONCERN`.** Esa dice que en runtime hay una sola
implementación; ésta dice que el core no la puede saltear, que es lo que la vuelve cierta. Una
etapa con un `Path.write_text` adentro no rompe ningún test y anda perfecto en la máquina de
quien la escribió: falla cuando el contenedor se recicla, o cuando otro proceso va a leer lo que
no está. Pasó con las cuatro salidas que quedaron afuera del primer barrido —y con ellas se
fue también el informe del parser, que resolvía los recortes contra el disco y escribía «figure
crop missing» en vez de fallar.

**Que el archivo sea el entregable no es una excepción, es presentación.** Un informe HTML se
abre en el navegador y el par de brat se carga en la herramienta: para eso, el **core escribe el
artefacto** y la **interfaz materializa una copia** donde se la pidan (`--out` en `report`,
`annotate`, `export-annotations` y `export`). Lo contrario —que el core escriba donde el usuario
dijo— es lo que había, y es lo que ata una etapa al filesystem de quien la corre.

Lo que sigue siendo un archivo local a propósito: lo que el usuario **trae** —el corpus, la
ontología inicial, un JSONL para importar— y el marcador `current_session`, que es estado del
CLI. Entrada y salida no son simétricas: la entrada ya existe en algún lado antes de que el
pipeline la vea.

### `ONE-SUBSTRATE-PER-CONCERN`

**Un solo motor de base y un solo almacén de objetos en runtime, también en local**: Postgres y
S3 —MinIO cuando es local—. Dos implementaciones vivas son dos comportamientos, y la que se
prueba termina no siendo la que se despliega: entre un filesystem y un bucket no se parecen las
URL firmadas, el paginado del listado ni los errores; entre SQLite y Postgres, ni el SQL ni
cuántos pueden escribir. `docker compose up` levanta las dos piezas.

La excepción son **los tests**, y es lo que hace que la regla se pueda sostener: corren sobre
SQLite y sobre un doble de S3 en memoria que entra por el mismo `S3ObjectStore` que corre en
producción. Reemplaza al cliente de boto3 y nada más, así que lo que se ejercita es el código de
verdad. Por eso `objectstore.open_configured` es el **único** lugar donde se construye un
almacén: es el único que hay que reemplazar.

### `ONE-CONNECTION-PER-UNIT`

**Una conexión por unidad de trabajo** —un request, un job—, abierta y cerrada en el hilo
que la usa. Nunca una conexión global en el ciclo de vida de un proceso. En SQLite eso
explota por afinidad de hilo; en Postgres es peor, porque **no** explota: compartir conexión
es compartir transacción, y dos unidades terminan commiteándose mutuamente trabajo a medio
hacer. Lo mismo vale para lo efímero: lo que un job baja a disco lo borra al terminar.
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
- **Commitear después de cada hito**, no al final, y con [conventional
  commits](https://www.conventionalcommits.org) en el asunto: `tipo(alcance): descripción`.

  **El asunto dice qué se tocó; el cuerpo sigue diciendo *por qué*.** El prefijo no reemplaza al
  cuerpo, se le suma: existe para que el log se pueda filtrar y agrupar, que es lo único que un
  asunto en prosa no permite. Un commit sin cuerpo sigue siendo un commit sin explicación.

  Tipos: `feat` (capacidad nueva), `fix` (algo que estaba roto), `refactor` (misma conducta,
  otra forma), `perf`, `test`, `docs`, `chore` (dependencias, scripts, config del repo),
  `build`.

  Alcances — **son las partes de este repo, no categorías abstractas**, y por eso la lista es
  cerrada y se amplía a mano: `services`, `cli`, `wizard`, `render`, `core` (los módulos de
  dominio), `config`, `matching`, `reasoning`, `tuning`, `use-cases`, `calibration`, `eval`,
  `spec` (el spec y los planes que lo enmiendan), `deps`, `interfaces` (la carpeta con las tres
  interfaces, y mover algo entre ellas), `api` (la interfaz REST, su configuración y su
  despliegue). Uno solo por commit; si un cambio toca tres, el alcance es el que explica el
  porqué, y si no hay uno así se omite.

  El asunto va en castellano como el resto de la documentación, sin punto final, y entra en 72
  caracteres. Un cambio que rompe algo lleva `!` antes de los dos puntos y un `BREAKING CHANGE:`
  al pie.
- **`technical_debt.md` es para mejoras a futuro, no para bugs.** Lo que está roto se arregla.
  Cada entrada lleva un id `DEBT-…`, así que dos sesiones en paralelo no colisionan como
  colisionaban los números.
- **No leer fuera de `pipeline/` sin preguntar.** El corpus y la ontología inicial viven afuera
  y el config los apunta; leer otra cosa del workspace es pedir permiso primero. Los casos de
  uso **ya no**: desde el 2026-09-10 están en `use_cases/`, adentro.
- **Los tests describen el porqué.** Los nombres son frases (`test_a_forced_parent_is_worse...`)
  y el docstring dice qué decisión de diseño fija. Un test nuevo que sólo verifica mecánica no
  está a tono con el resto.
- **Comentarios que no repiten el código.** No se documenta lo obvio: un docstring que dice lo
  que la firma ya dice es una copia más que hay que mantener. Se comenta el porqué, lo raro, y lo
  que alguna vez costó un bug.
- **Nada de cifras que el repo ya calcula.** Cuántos tests, cuántas líneas, cuántos módulos,
  cuántos jars, cuántos comandos, cuánto tarda la suite: eso no se escribe en la documentación ni
  en un comentario. Envejece sin que nadie se entere —el README dijo «126 tests» hasta el
  2026-09-11, con la suite en 602— y no cambia ninguna decisión de quien lee. Va el comando que
  lo dice (`uv run pytest -q`, `onto-pipeline --help`), o la propiedad que sí importa («corre sin
  red ni Docker» en vez de «~6 s»). **La excepción son las mediciones**, y por eso viven en
  [`findings.md`](findings.md): un número medido lleva su n, su fecha y contra qué se midió, y
  eso no envejece porque dice cuándo se tomó.
- **La documentación no referencia historia de git** (`API-NO-GIT-DOCS`). Describe el estado
  actual del código y el porqué de cada decisión, y nada más: ni hashes, ni «el commit tal», ni
  «esto lo cambió tal rama», ni «antes de tal fecha esto decía otra cosa». Si algo cambió y el
  cambio importa, se explica el estado nuevo **y su razón**, sin el rastro: el rastro ya está en
  el `git log`, y repetirlo acá es una copia que envejece. Esta es la única copia de la regla; no
  se repite en otro archivo. **No es lo mismo que fechar una medición**: `findings.md` lleva la
  fecha de cuándo se midió porque eso es parte del dato, no una referencia a la historia.
- **El tipo va en la firma, no en el docstring.** El lenguaje ya tiene anotaciones; repetirlas en
  prosa duplica algo que se desactualiza sin que nadie se entere.
- **Una sesión, un worktree.** Hay más de una sesión trabajando sobre este repo, y dos sesiones
  escribiendo el mismo working tree ya produjo la falla que `FINDINGS-SILENT-FAILURES` registra:
  una edición anclada a texto exacto **falla abierta** cuando otro movió el contexto — no rompe,
  no avisa, deja el código como estaba, y los tests siguen pasando porque prueban otra cosa.

  ```bash
  git worktree add ../pipeline-<nombre> -b <nombre>
  ```

  Con un checkout por sesión nadie puede pisar a nadie, y el desacuerdo aparece al mergear, que
  es ruidoso: se invierte el modo de falla. **El repositorio es el único canal entre sesiones** —
  lo que tiene que llegar a la otra se commitea, no se deja en el árbol. Un subagente que va a
  **escribir** necesita su propio worktree por la misma razón; para leer —búsqueda, revisión,
  auditoría— no hace falta, porque leer en paralelo no se pisa.
- **Verificar el efecto, no la ausencia de error.** Después de editar un comando, correrlo y
  mirar la salida. Que los tests pasen no prueba que la edición se aplicó. Y preferir
  herramientas que **fallen cerradas**: `Edit` sobre un archivo recién leído aborta si el texto
  no está; un `str.replace` en un script no. Si hay que usar un script, verificar después con un
  `grep` de lo que se esperaba escribir.
- **Lo que importa se escribe en el repo antes de cerrar la sesión.** El transcript de una sesión
  es el único registro del razonamiento que no llegó al repositorio —por qué se descartó una
  alternativa, qué se midió y no se anotó— y **vence a los 30 días** con la configuración por
  defecto del cliente. Una auditoría encontró un hallazgo sustantivo que sólo vivía ahí (el
  análisis del homónimo, hoy `DEBT-CONTEXT-DISAMBIGUATION`) y estuvo a semanas de perderse.
  Conservar el transcript no lo vuelve encontrable, así que no es una alternativa a escribirlo.
- **La atribución no se lee de `git log`.** Los commits tienen un solo autor aunque el trabajo
  haya salido de varias sesiones. Quién decidió qué está en la tabla de procedencia de
  [`findings.md`](findings.md).
- **`pkill -f` se matchea a sí mismo.** Ya colgó dos shells en este proyecto. Matar por PID.

## Dónde está cada cosa

```
src/onto_pipeline/
  services/         **los cuerpos de las etapas, sin interfaz.** Una etapa, una función; recibe
                    un Workspace, devuelve un resultado tipado, y no importa typer ni rich
    workspace.py    config + almacén + **sesión** + versión + modelo; StageError
    prep.py         PREP: ingesta, ontología inicial, glosas, alineación, CQ
    iterate.py      ITER: menciones, tipado, puentes, clases, axiomas, ramas, validación
    evaluate.py     EVAL: parada, CQ, retención, calibración, ajuste
    deliver.py      DELIVERABLES: diff, DAG, telemetría y `export`
    catalog.py      qué etapas hay, sus parámetros, cuáles se encolan y la compuerta del plan
  interfaces/       **traducen un protocolo a la capa de servicios.** Sin lógica de dominio
    render.py       cómo se ve cada resultado. Compartido por las interfaces de terminal
    cli.py          **la interfaz de uso**: un comando por etapa, para enriquecer una ontología
    wizard.py       la interfaz guiada: el mismo plan, preguntando en vez de frenar
    api/            la interfaz HTTP, y lo que existe sólo para ella
      app.py        rutas: leer el request, llamar a un servicio, responder
      admin.py      `onto-pipeline-api`: levantar la API y administrarla
      deps.py       el workspace de cada unidad de trabajo; acá se baja el upload
      jobs.py       la cola: reclamo atómico, workers, barrido
      uploads.py    corpus y ontología subidos, con forma de caso de uso
  config.py         la superficie de configuración; rechaza valores no implementados
  initial_ontology.py  `PREP-NORMALIZE`: IRIs opacos, etiquetas, erratas, DECLARED_ANNOTATIONS
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
  use_cases.py                                                   cargar un caso de uso
  calibration.py                                                 el banco: barrer umbrales sobre uno
  llm.py providers.py telemetry.py                               proveedor, caché y costos
  sessions.py                                                    la sesión de usuario: fase, historial
  store.py                                                       el almacén sin dialecto: postgres (sqlite en tests)
  objectstore.py artifacts.py                                    los bytes en S3, y qué artefacto es cuál
  db.py language.py terms.py report.py                           esquema y utilidades
config/default.yaml   TODA la configuración, con el porqué de cada valor en comentarios
tests/                un archivo por módulo, más `test_end_to_end.py`; sin red ni Docker
lib/                  jars del razonador (gitignored, los baja fetch-jars.sh)
data/                 casi vacío: sólo el marcador de sesión, que es de esta máquina
docker-compose.yml    Postgres y MinIO para correr local, con el bucket ya creado
Dockerfile            la imagen: jars en una etapa, glibc en la otra — **nunca Alpine**
```

**El ledger de unidades de trabajo (`telemetry.py`) es una sola cosa haciendo tres**: caché,
checkpoint y telemetría de costos. La clave de caché cubre el prompt, la temperatura, **el modelo
y su reasoning effort** — omitir el modelo servía en silencio resultados de otro tier, y hay un
test que lo fija.

## Cómo trabaja este usuario

Preferencias expresadas explícitamente. No son estilo: cambiaron el rumbo del trabajo cuando se
ignoraron.

- **Atribución estricta.** Toda afirmación dice de dónde sale: del modelo, del usuario, de la
  literatura, o de un advisor que el usuario hizo generar. No se mezclan las suyas con las del
  modelo, ni en un párrafo ni en un resumen. Y tres estados que no son lo mismo: **enunciado**
  (alguien lo dijo), **avalado** (el usuario lo hizo suyo), **generado** (lo produjo el modelo).
  **Preguntar no es avalar**: traer una idea para discutirla no la vuelve la posición de quien la
  trae. Ante duda sobre qué prefiere o qué avala, se pregunta.
  - **Una reformulación se confirma si va a operar como premisa.** Cuando el modelo reformula lo
    que el usuario opinó y esa reformulación puede después sostener una decisión, se pide
    confirmación. Las de bajo impacto pasan sin trámite. Sin confirmación la reformulación queda
    en **zona gris**: ni del modelo ni establecida — y en zona gris tampoco se la puede usar para
    decidir.
- **Preguntar ante la duda.** Duda razonable, o chica pero que pesa en lo que sigue: se pregunta,
  no se asume.
- **Respuestas cortas.** Sin introducción, sin cortesías, sin cierre que repita lo ya dicho.
- **Lenguaje simple y concreto.** Esquemático antes que creativo. Sin vocabulario rebuscado:
  ni neologismos ni anglicismos que tienen palabra en castellano.
- **Sin autoglorificación.** No se anuncia la calidad de la respuesta antes de darla, ni se
  comenta el propio tono o enfoque. Los adjetivos absolutos para calificarse a uno mismo
  («honesto», «real», «concreto») no van: los juzga el que lee.
- **Términos oscuros y siglas se expanden la primera vez** que aparecen.
- **Los insultos no se comentan.** Si el usuario putea al modelo, el modelo sigue con el tema. Ni
  reclamo, ni acuse de recibo, ni nota al pie: la cortesía sobre eso no agrega nada.
- **Preguntar no es pedir que se implemente.** Cuando hace una pregunta, quiere la respuesta —
  no la respuesta y además el cambio ya hecho. Empezar a implementar sin que lo pida es la
  corrección que más veces tuvo que hacer. **No usa ironía ni sarcasmo**: si pregunta, es porque
  quiere que algo se aclare, y nada más. Primero se contesta; recién después, y si lo pide, se
  toca el repo.
- **Commitear después de cada hito**, con el log de los cambios en el mensaje. No una tanda al
  final. El formato del asunto está en Convenciones de trabajo, arriba: conventional commits.
- **Nombres mnemotécnicos, no códigos — pero nombres, no ausencia de nombre.** Lo pidió cuatro
  veces. La tercera fue con fastidio; la cuarta fue para corregir que, al sacar los códigos, se
  habían quedado frases sin identificador, y eso empobrece el texto en vez de mejorarlo.
- **Documentar mientras se construye**: README, deuda técnica y el porqué de cada decisión,
  no como paso final separado.
- **La deuda técnica es para mejoras a futuro**, no para llevar la cuenta de bugs.
- **Verificar contra el repo antes de contestar.** "Lee el estado del repositorio antes de
  modificar o contestar" — dicho tal cual, más de una vez, y en general porque la respuesta
  anterior había salido de la memoria y no de los archivos.
- **Localidad.** No leer fuera de `pipeline/` sin preguntar primero. El corpus y la ontología inicial
  están afuera y el config los apunta; cualquier otra cosa se pide.
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

- **`ITER-TUNE`, el ajuste del matcher** — el entrenamiento está hecho (`tune`) y lo que sigue
  bloqueado es el circuito que el spec describe: que las etiquetas salgan solas de las
  decisiones de zona gris en vez de un caso de uso anotado. Bloqueado por datos, no por código
  — hace falta que alguien conteste unos cientos de pares de zona gris. Ver
  `DEBT-MATCHER-TUNING`.
- **`ITER-FEEDBACK`, el historial de feedback** — **cerrado el 2026-09-10**: esquema `GRADED-FEEDBACK`, forma normal y
  precedentes en el prompt de `axiomatize`. Lo que falta no es código sino una segunda iteración
  con feedback humano real, para ver si algún precedente mueve un juicio. Ver
  `DEBT-FEEDBACK-HISTORY`.
- **La compuerta no-go de `BUILD-NO-GO-GATE` sigue abierta**, y es la que decide si tiene sentido seguir
  construyendo encima. Ver el README.

## Antes de proponer una idea

**Mirar `LAYERS` de [`findings.md`](findings.md), "Lo que se probó y no funcionó".** Siete formas de
meterle más texto a la comparación están medidas y todas empeoran; la cobertura léxica como
veredicto de alineación da el resultado invertido; reusar un re-ranker entre dominios resta. Cada
una parecía razonable antes de medirla, y por eso están anotadas.

## Antes de decir que algo falta

Mirar la tabla de estado del README y las entradas de `technical_debt.md`. Varias cosas que
parecen faltantes son decisiones: el catálogo de patrones de modelado tiene tres entradas y una
sin detector a propósito, el filtro de pitfalls es un subconjunto local de OOPS! y no OOPS!, y
`next` no ejecuta nada por una razón escrita.
