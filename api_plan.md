# Plan: la interfaz REST

Agrega una tercera interfaz sobre la capa de servicios, hermana del CLI y de `wizard`: una API
REST desplegable en AWS, sin archivos locales durables, con el corpus subido por pre-signed URLs
y apoyada en la sesión de usuario que ya existe.

Escrito para que lo ejecute otra sesión. **Leer antes:** `CLAUDE.md` (invariantes y convenciones),
`CONTRIBUTING.md` (el almacén, los worktrees, las sesiones de usuario) y `main_plan.md` en
`LAYERS` y `BUILD-OUT-OF-SCOPE`. Trabajar en su propio worktree:

```bash
git worktree add ../pipeline-api -b api
```

## Procedencia

Quién decidió qué, porque el `git log` no lo dice.

**Enunciado por el usuario** (lo que abrió esto): que el pipeline se pueda usar por HTTP desde
AWS, sin escritura de archivos locales durables ni SQLite, con el corpus subido por pre-signed
URLs, y que la API sea una interfaz más sobre la capa de servicios y no un pipeline paralelo.

**Avalado por el usuario** (elegido explícitamente en la conversación): las trece decisiones de
la tabla de abajo. Además, cuatro correcciones sobre el borrador del plan: las interfaces van en
su propia carpeta y el core queda en la raíz del paquete; la regla de no citar historia de git se
escribe en un solo archivo y primero; la configuración de la API es por variables de entorno; y
los casos de uso publicados no tienen trato especial en la API.

**Generado por el modelo, sin aval explícito** — se puede discutir sin romper nada de lo
anterior: el reclamo de jobs por compare-and-set y el índice parcial único; la invariante de
conexión por unidad de trabajo; el rechazo con 409 de las decisiones síncronas mientras hay un
job en vuelo; la lista de tests; el orden de los hitos; los nombres (`artifacts`, `objectstore`,
`uploads`, `jobs`, los ids `API-*` y `DEBT-API-*`).

## Las decisiones

| Id | Decisión |
|---|---|
| `API-FASTAPI` | FastAPI y uvicorn |
| `API-JOBS` | Las etapas largas son jobs asíncronos con polling; nada de un request de cuarenta minutos |
| `API-S3-ARTIFACTS` | Los artefactos van a object storage, no a disco |
| `API-AUTH-KEY` | Header `X-Auth-Key` contra un token estático de entorno, y falla cerrado |
| `API-SCOPE-CORE` | La v1 no expone `calibrate`, `tune`, `report`, `annotate` ni brat |
| `API-NEUTRAL-CONTAINER` | La imagen no depende de ningún proveedor |
| `API-ECS-ONE-TASK` | ECS con una tarea fija: mínimo 1, máximo 1, autoscaling apagado |
| `API-UPLOADED-AND-PUBLISHED` | El corpus subido y los casos publicados conviven sin distinción |
| `API-SHARED-UPLOADS` | Los uploads se comparten entre sesiones, y borrarlos es global |
| `API-PRESIGNED-GET` | Los artefactos se descargan con un pre-signed de lectura |
| `API-ENV-FIRST` | La API se configura por variables de entorno; los archivos de config siguen válidos, pero la imagen no los usa |
| `API-NO-GIT-DOCS` | La documentación no referencia historia de git |
| `API-PARALLEL-SAFE` | El mecanismo de jobs es parallel-safe por construcción y con pruebas que lo fijan. El default es un worker por costo, pero subirlo es configuración y no una apuesta |

Si algo obliga a desviarse de una de las trece, **parar y preguntar**: son acuerdos con el
usuario, no preferencias del plan.

## Lo que ya está verificado, y no hay que volver a investigar

- `services/` no importa `typer` ni `rich`, y un test lo fija leyendo los imports (invariante 9).
  Hay que extender esa lista a `fastapi` y `uvicorn`, y la invariante a tres interfaces.
- **No hay sistema de migraciones.** Las tablas nuevas siguen el patrón `install(conn: Store)` con
  `CREATE TABLE IF NOT EXISTS`. **No agregar columnas a `user_sessions`**: el upload se guarda en
  la columna existente `use_case`, con la forma `upload:<id>`.
- `Store.execute` devuelve el cursor, y `cursor.rowcount` es utilizable — `review.py:195` ya lo
  usa así para saber si un `UPDATE` tocó una fila.
- `work_units` tiene clave primaria `(session_id, key)` (`db.py:87-101`): dos sesiones con la
  misma pregunta son dos filas, no un choque. El **resultado** se comparte; la contabilidad no.
- Las conexiones de `sqlite3` son **afines al thread** que las creó: `store.py:186` no pasa
  `check_same_thread`, así que usar una conexión desde otro thread tira `ProgrammingError`.
  Verificado a mano.
- Los índices parciales con `WHERE status IN (...)` se aceptan igual en SQLite y en Postgres, y
  sobreviven `store.statements()` y `store.to_postgres()`. Verificado a mano, con las tres
  variantes (`IN`, `OR`, `<>`).
- Los encoders ya tienen `@lru_cache` de proceso (`embeddings.py:28-43`): no hace falta cachear
  entre jobs.
- `reasoning.py:97` hace `jpype.startJVM(classpath=jars)`, así que la imagen necesita glibc
  —**nunca Alpine**— y `JAVA_HOME`. `scripts/fetch-jars.sh` se baja Maven si no está, así que el
  build stage necesita curl y un JDK.
- `orchestration.survey(conn, version_id, *, session_id, has_provider)` da los estados
  READY/WAITING/DONE, y `orchestration.blocking(plan)` los WAITING. `cli.py:861-873` usa el
  centinela `"(sin versión todavía)"` cuando no hay ninguna versión.
- Ningún módulo del core importa `render`, y en tests sólo `tests/test_wizard.py` importa una
  interfaz: mover las interfaces es barato.
- **`DEBT-POSTGRES-UNTESTED` ya está resuelta.** Postgres corrió contra un servidor de verdad, con
  dos sesiones de usuario **ingestando en paralelo** y con los ids de documento repetidos entre
  las dos. La escritura concurrente al almacén y el ledger ya tienen prueba: este plan **no** la
  cierra ni la re-abre. Lo único que sigue sin probarse ahí es dos sesiones corriendo **etapas de
  modelo** a la vez, que cuesta plata. No escribir que este trabajo lo resuelve.
- De esa misma verificación sale un dato que importa para los uploads: **`ingest` quedó fuera del
  caché compartido**, porque su worker parsea *y* persiste, y en un acierto de caché no corre.

## Sobre AWS, para no volver a averiguarlo

**App Runner no sirve**, por dos razones independientes: está cerrado a clientes nuevos y su
almacenamiento efímero son 3 GB *incluyendo la imagen*, y esta imagen —con los jars y los
encoders— no entra. El destino es **ECS** (Express Mode o Fargate) con una tarea fija; Elastic
Beanstalk queda como alternativa con la misma imagen. La palanca de costo es la pausa programada:
`desired 0` en el servicio y la base detenida.

---

## Hito 1 — La regla de documentación y los alcances de commit

Va primero para no tener que corregir después la documentación que los hitos siguientes escriben.

**Archivo.** `CLAUDE.md`.

1. En "Convenciones de trabajo", agregar `API-NO-GIT-DOCS`: la documentación describe el estado
   actual del código y el porqué de las decisiones, nunca la historia de git. Ni hashes, ni «el
   commit tal», ni «antes de tal fecha». Si algo cambió y el cambio importa, se explica el estado
   nuevo y su razón, sin el rastro. **En un solo lugar**: es la única copia de la regla.
2. En la misma sección, la lista de alcances de commit es **cerrada y se amplía a mano**, y no
   tiene con qué nombrar este trabajo. Agregar `interfaces` (la carpeta con las tres) y `api` (la
   interfaz REST y su despliegue). Los commits de este plan usan esos dos, más los que ya existen.

**Verificación.** `uv run pytest -q` y `uv run ruff check .`.

**Commit.** `docs(spec): la documentación no referencia historia de git`

---

## Hito 2 — Las interfaces a su propia carpeta

**Objetivo.** `src/onto_pipeline/interfaces/` con las tres interfaces; el core queda en la raíz
del paquete.

1. Mover `cli.py`, `wizard.py` y `render.py` a `src/onto_pipeline/interfaces/`, con un
   `__init__.py` que diga qué es una interfaz: traduce entre un protocolo —terminal, HTTP— y la
   capa de servicios, y no tiene lógica de dominio.
2. Corregir los imports: lo del core pasa de `.foo` a `..foo`; entre las tres quedan relativos
   dentro de `interfaces`.
3. `pyproject.toml`: el entry point pasa a `onto_pipeline.interfaces.cli:entrypoint`.
4. `tests/test_wizard.py`: `from onto_pipeline.interfaces import wizard`.
5. Barrer el resto: `rg -n "onto_pipeline\.(cli|wizard|render)|from \.(cli|wizard|render)"` sobre
   código, tests y documentación (`README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, los `*_plan.md`).
6. `CLAUDE.md`: el mapa de "Dónde está cada cosa" y la invariante 9, que hoy dice «hay dos
   interfaces» y pasa a tres — y a prohibir también `fastapi` y `uvicorn`.

**Verificación.** `uv run pytest -q`, `uv run ruff check .`, y **el efecto, no la ausencia de
error**: `uv run onto-pipeline --help` y `uv run onto-pipeline next` tienen que seguir andando.

**Commit.** `refactor(interfaces): cli, wizard y render a su propia carpeta`

---

## Hito 3 — La capa de artefactos

**Objetivo.** Que ninguna etapa escriba a disco durable: todas pasan por una capa que en local es
el filesystem y en AWS es S3. Es el hito más caro y el que más riesgo tiene, porque lo que quede
sin refactorar falla recién en AWS, cuando el contenedor se recicla y el archivo no está.

**Archivos nuevos.** `src/onto_pipeline/objectstore.py` (el backend: `local` y `s3`, con la misma
interfaz — `put`, `get`, `list`, `delete`, `presigned_get`) y `src/onto_pipeline/artifacts.py` (el
vocabulario del dominio: qué artefacto es cuál, con qué clave, de qué sesión).

1. `objectstore.py`: interfaz mínima, dos implementaciones. La de S3 importa boto3 perezosamente y
   falla con un mensaje claro si falta, como `store.open_postgres` con psycopg. Nada del proveedor
   asoma en la interfaz.
2. `artifacts.py`: los nombres de artefacto y su clave, derivada de `(session_id, tipo, nombre)`.
   Los **artefactos** son las salidas derivadas —markdown, crops, ontología normalizada, ABox
   TriG, diffs, export, manifiesto—; los **casos de uso** son insumos y no son esto.
3. Refactorar las escrituras. Están todas verificadas: `ingest.py:131` (markdown), `parse.py:291`
   (crops PNG), `services/prep.py:103` y `:179` (normalize y glosas `.ttl`),
   `services/iterate.py:1815` (ABox), `:763`, `:769-778` y `:1640-1649` (shapes),
   `services/deliver.py:79` (diffs), `:312-334` (refresh del ABox), `:336` (export), `:361`
   (manifiesto).
4. Refactorar los lectores de markdown: `services/prep.py:237`, `services/iterate.py:233`,
   `services/evaluate.py:203` y `:243`.
5. `services/workspace.py:208-215`: `session_dir`, `ontology_dir` y `abox_path` se resuelven por
   la capa de artefactos. El marker `current_session` (`:328-329`) es estado del CLI y no del
   core: queda andando en local, y la API no depende de él.
6. **Arreglar el desalineo latente**, que es un bug y por eso se arregla y no se anota: `normalize`
   escribe en `sessions/<sid>/ontology/initial_normalized.ttl`, pero `services/prep.py:361` y
   `services/evaluate.py:190` leen de `work_dir/ontology/initial_normalized.ttl`. Unificar por la
   capa de artefactos.

**Tests.** `tests/test_artifacts.py` y `tests/test_objectstore.py`: el mismo contrato contra el
backend local y contra un doble de S3 en memoria, sin red — el molde es `tests/test_store.py`, que
corre un contrato contra dos motores. Más `test_normalize_writes_where_evaluate_reads`, que fija
el desalineo: es el canario de este hito.

**Verificación.** `uv run pytest -q`, `uv run ruff check .`.

**Commit.** `feat(core): capa de artefactos sobre object storage`

---

## Hito 4 — Uploads

**Objetivo.** El corpus entra por pre-signed PUT y se comparte entre sesiones.

**Archivos.** `src/onto_pipeline/uploads.py`, `scripts/seed_use_cases.py`.

1. `uploads.py`: tabla con `install(conn: Store)`. Un upload es un conjunto de documentos bajo un
   prefijo del bucket. Crear —devuelve un pre-signed PUT por archivo—, listar, obtener, borrar.
   Borrar es **global** y afecta a cualquier sesión que lo use (`API-SHARED-UPLOADS`): explícito y
   documentado, no un accidente.
2. Sesiones sobre uploads: `new_session` acepta `upload_id` y lo guarda como `upload:<id>` en la
   columna `use_case`. Sin columnas nuevas, porque no hay migraciones.
3. Materialización: `ingest.discover` trabaja sobre un `corpus_root` local, así que antes de
   ingestar hay que bajar el upload a un directorio efímero, acotado al job y limpiado al
   terminar. Ojo con el caché: `ingest` está afuera del caché compartido, así que dos sesiones
   sobre el mismo upload parsean cada una lo suyo — es lo correcto, no lo optimices.
4. `scripts/seed_use_cases.py`: siembra los casos publicados al bucket **por el mismo mecanismo de
   upload** (`API-UPLOADED-AND-PUBLISHED`). No tienen trato especial en la API, y se borran como
   cualquier upload.

**Tests.** `tests/test_uploads.py` con el objectstore local: crear, listar, borrar, y que una
sesión quede atada a `upload:<id>` y se lea de vuelta.

**Verificación.** `uv run pytest -q`, `uv run ruff check .`.

**Commit.** `feat(core): uploads compartidos como origen de corpus`

---

## Hito 5 — Jobs, workers y paralelismo

**Objetivo.** El mecanismo asíncrono, parallel-safe desde el principio y no como parche posterior.

**Archivo.** `src/onto_pipeline/jobs.py`.

1. Tabla `jobs` con `install(conn: Store)`: id, `session_id`, etapa, `status` (`queued`, `running`,
   `done`, `failed`), parámetros, progreso, resultado, warnings, error, timestamps, worker. Más
   dos índices:

   ```sql
   CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, created_at);
   CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active
     ON jobs(session_id) WHERE status IN ('queued','running');
   ```

   El índice parcial es lo que hace que dos `POST` simultáneos sobre la misma sesión no produzcan
   dos jobs activos. Chequear-y-después-insertar tiene una carrera; el índice no.
2. **Reclamo atómico por compare-and-set**, en SQL portable, y gana quien obtiene
   `cursor.rowcount == 1`:

   ```sql
   UPDATE jobs SET status='running', worker=?, started_at=?
    WHERE id=? AND status='queued'
      AND NOT EXISTS (SELECT 1 FROM jobs j2
                       WHERE j2.session_id=? AND j2.status='running')
   ```

   Arbitra la base y no la memoria del proceso, así que el mismo mecanismo vale para N threads y
   para varias tareas. Deliberadamente **sin** `FOR UPDATE SKIP LOCKED`: metería dialecto de
   Postgres fuera de `store.py` y rompería la invariante 10. La guardia `NOT EXISTS` es la segunda
   línea: el índice cubre el insert, la guardia cubre un almacén creado antes del índice.
3. Compuerta del plan: un job se acepta sólo si `orchestration.survey` da la etapa READY. Si está
   WAITING, la respuesta dice qué falta, con `orchestration.blocking`. Es la invariante 13
   —ninguna interfaz cruza un punto de decisión— aplicada a HTTP.
4. Worker: reclama, corre la función de servicio, va escribiendo progreso, y al final guarda
   resultado o error. `api.worker_count` workers como threads del proceso, default 1.
5. **Guardia de arranque**: `worker_count > 1` exige `database.backend: postgres`. SQLite da un
   escritor y N workers ahí son una cola de esperas. Falla cerrado, con mensaje claro.
6. **Una conexión por unidad de trabajo** — request o job —, abierta y cerrada en el mismo thread.
   Nunca una conexión global en el ciclo de vida de la app. En SQLite eso explota por afinidad de
   thread; en Postgres es peor, porque **no** explota: compartir conexión es compartir transacción,
   y dos jobs terminan commiteándose mutuamente trabajo a medio hacer.

**Tests herméticos** (`tests/test_jobs.py`, SQLite, sin red ni servidor):

| Test | Qué fija |
|---|---|
| `test_two_workers_cannot_claim_the_same_job` | dos threads, cada uno con su conexión, compiten por la misma fila: gana exactamente uno |
| `test_a_session_cannot_have_two_active_jobs` | el índice parcial rechaza el segundo job de una sesión, y libera el lugar cuando el primero termina |
| `test_a_decision_is_refused_while_a_job_runs_on_that_session` | contestar la zona gris con un job en vuelo da 409; después del job, pasa |
| `test_more_than_one_worker_requires_postgres` | la guardia de arranque falla cerrado |
| `test_each_request_opens_and_closes_its_own_store` | con la apertura monkeypatcheada, dos requests dan dos aperturas y dos cierres |
| `test_two_sessions_run_model_free_stages_at_once` | dos workers, dos sesiones, dos corpus mínimos, `ingest` y `regenerate` en simultáneo: las dos terminan y cada sesión cuenta sólo lo propio |

**Tests opt-in contra Postgres**, con `ONTO_PIPELINE_TEST_DSN` y el patrón de `tests/test_store.py`
—la suite por defecto sigue sin servidor—: los cuatro primeros de arriba contra el motor que se
despliega, porque el reclamo y el índice son SQL nuevo. **No** agregar un test de escritura
concurrente al ledger: eso ya está probado y cerrado en `DEBT-POSTGRES-UNTESTED`.

**Tests salteados si falta el entorno**, con `pytest.mark.skipif`:
`test_two_reasoner_instances_classify_at_once` (necesita `lib/` y JVM; fija que JPype tolera
threads y que la instancia por llamada es la unidad de aislamiento) y
`test_the_shared_encoder_encodes_from_two_threads` (necesita el extra `matching`; fija que
encodear concurrente da los mismos vectores que en serie).

**Verificación.** `uv run pytest -q`, `uv run ruff check .`, y con un Postgres a mano
`ONTO_PIPELINE_TEST_DSN=... uv run pytest -q`.

**Commit.** `feat(core): jobs asíncronos con workers parallel-safe`

---

## Hito 6 — La API

**Objetivo.** `src/onto_pipeline/interfaces/api/` traduciendo HTTP a la capa de servicios, y nada
más: ninguna lógica de dominio vive acá.

**Archivos.** `app.py` (la app y los routers), `auth.py`, `deps.py`, `models.py` (los Pydantic de
request y response).

1. `auth.py`: `X-Auth-Key` contra un token estático de entorno. **Sin token configurado la app no
   arranca** — falla cerrado, no abre sin auth (`API-AUTH-KEY`).
2. `deps.py`: una dependencia que abre el almacén y el objectstore por request y los cierra al
   terminar, respetando la invariante de conexión del hito 5.
3. Los endpoints. La lista de etapas sale de los ids de `orchestration.survey` y de los comandos
   de `interfaces/cli.py`, menos lo que `API-SCOPE-CORE` deja afuera:
   - `GET /healthz`, sin auth.
   - Uploads: crear con pre-signed PUT, listar, obtener, borrar.
   - Sesiones: crear con `upload_id`, listar, obtener.
   - `GET /sessions/{id}/plan`: `orchestration.survey`, con el centinela de «sin versión todavía»
     que usa `cli.py:861-873`.
   - Etapas: un `POST` por etapa, que encola un job y devuelve su id; `GET /jobs/{id}` para el
     polling; `GET /sessions/{id}/jobs` para el historial.
   - Decisiones síncronas (zona gris, feedback): rechazadas con 409 mientras la sesión tiene un
     job corriendo. Los `GET` no se bloquean nunca.
   - Artefactos: listar, y un `GET` que devuelve el pre-signed de lectura (`API-PRESIGNED-GET`).
4. **Los warnings y los errores de usuario viajan en la respuesta**, no sólo al log. `StageError` y
   las advertencias que hoy el CLI pinta con `rich` tienen que llegar al cliente en el cuerpo.
   `cli.py:1029-1043` muestra qué distingue un error de usuario de uno interno; no filtrar trazas
   internas al cliente.
5. Extender el test de la invariante 9 para que `services/` tampoco pueda importar `fastapi` ni
   `uvicorn`.

**Tests.** `tests/test_api.py` con `TestClient`, objectstore local y SQLite: `healthz` sin auth,
todo lo demás 401 sin el header, el plan de una sesión vacía, encolar un job y verlo terminar con
un worker de un tiro, la compuerta que rechaza una etapa WAITING diciendo qué falta, y un
`StageError` que sale en el cuerpo.

**Verificación.** `uv run pytest -q`, `uv run ruff check .`.

**Commit.** `feat(api): interfaz REST sobre la capa de servicios`

---

## Hito 7 — Configuración, imagen y despliegue

1. `config.py` y `config/default.yaml`: secciones `storage` (backend, bucket, prefijo, región) y
   `api` (host, puerto, `worker_count`, vida de los pre-signed). **Overrides por entorno con
   prefijo**, que es lo que usa la imagen (`API-ENV-FIRST`); los archivos siguen sirviendo en
   local. Ningún umbral nuevo fuera de `config/default.yaml` (invariante 5).
2. `pyproject.toml`: extra `api` con fastapi, uvicorn y boto3.
3. `Dockerfile` multi-etapa: build stage con JDK y curl para `scripts/fetch-jars.sh`; runtime con
   glibc —**nunca Alpine**, por `jpype.startJVM`— y `JAVA_HOME`. Nada específico de AWS adentro
   (`API-NEUTRAL-CONTAINER`).
4. Documentar el despliegue en ECS: una tarea fija con autoscaling apagado, Postgres administrado,
   el bucket, el token por secreto, y la pausa programada como palanca de costo. Decir que App
   Runner no sirve y por qué, para que nadie lo reintente.
5. Sizing: la restricción que manda es la memoria —razonador y encoders por job en vuelo—. Con la
   JVM y los encoders compartidos, **una tarea con N workers cuesta menos que N tareas**: el
   paralelismo dentro del proceso es el primer paso y escalar tareas el segundo. Medirlo en el
   humo antes de recomendar un número, y anotarlo en `findings.md` con su fecha y contra qué se
   midió.

**Verificación.** `uv run pytest -q`, `uv run ruff check .`. El build de la imagen es opcional acá
—es lento y pesado—; si se corre, que `docker build .` termine y que el `startJVM` no explote.

**Commit.** `feat(api): configuración por entorno e imagen para ECS`

---

## Hito 8 — Documentación y deuda

1. Este archivo queda como el diseño de la interfaz; actualizarlo con lo que la implementación
   haya cambiado, porque un plan que miente es peor que no tenerlo.
2. `README.md`: la sección de la API, su lugar en el índice, y **el alta de los nombres nuevos**
   —`API-*` y `DEBT-API-*`— porque el índice del README es donde viven los identificadores.
   Actualizar "En cola" (línea 113): sale lo que este trabajo cierra, entra lo que queda.
3. `CLAUDE.md`: el mapa con `interfaces/`, las invariantes 9, 11 y 13 al día, la invariante nueva
   de conexión por unidad de trabajo, y `api` en los comandos si corresponde.
4. `technical_debt.md`:
   - `DEBT-API-CANCEL`, `DEBT-API-DOCUMENTS-PATH`, `DEBT-API-SSE`, `DEBT-API-USERS`: nuevas.
   - `DEBT-API-PARALLEL-WORKERS`: **no** es deuda de seguridad —eso queda probado en el hito 5—
     sino de operación: más de una tarea multiplica la memoria de JVM y encoders, y hay que
     dimensionar.
   - `DEBT-API-CONNECTION-POOL`: no es «falta un pool», es «cada unidad de trabajo paga un
     connect, y cuando eso moleste, el pool tiene que respetar la invariante de conexión».
   - **`DEBT-POSTGRES-UNTESTED` no se toca**: está resuelta, y este trabajo no la cierra ni le
     agrega nada.

**Verificación.** `uv run pytest -q`, `uv run ruff check .`.

**Commit.** `docs(api): diseño, mapa y deuda de la interfaz REST`

---

## Verificación manual al final

Con uvicorn, SQLite, storage local y objectstore local, y sin proveedor de modelo:

```
healthz → crear upload → subir un corpus mínimo → crear sesión sobre el upload
→ POST ingest → polling hasta done → GET plan → POST export
→ GET artifacts → descargar por el pre-signed
```

Qué hay que ver, y es lo que los tests no prueban: que los estados del plan cambien como en el
CLI, que los warnings de usuario aparezcan en el cuerpo de las respuestas, y que **no haya quedado
ningún artefacto escrito fuera del objectstore**.

## Trampas conocidas

- **El hito 3 es el que puede fallar en silencio.** Son once puntos de escritura y cuatro de
  lectura; el que quede sin refactorar no rompe ningún test y aparece recién en AWS. El canario es
  `test_normalize_writes_where_evaluate_reads` más el humo manual sin nada en disco.
- **Una edición anclada a texto exacto falla abierta** si otra sesión movió el contexto
  (`FINDINGS-SILENT-FAILURES`): de ahí el worktree propio, y verificar el efecto y no la ausencia
  de error.
- **No escribir cifras que el repo ya calcula** —cuántos tests, cuántos endpoints, cuánto tarda la
  suite— ni en la documentación ni en los comentarios. Va el comando que lo dice. La excepción son
  las mediciones, que van a `findings.md` con su n y su fecha.
- **El tamaño de la imagen** con los jars y los encoders. Si el build se vuelve inmanejable, la
  salida es cachear los jars en su propia capa, no bajar a Alpine: sin glibc no hay JVM.
