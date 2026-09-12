# Plan: verificación de etiquetas y su idioma

Enmienda a `main_plan.md`. Implementa la mitad que falta de `PREP-NORMALIZE-LABELS` —"comparar
**semánticamente** con la etiqueta declarada"— y arregla la detección de idioma que hoy la
precede.

Escrito el 2026-09-12 para que lo ejecute otra sesión. **Leer antes:** `CLAUDE.md` (invariantes y
convenciones), `main_plan.md` en `PREP-NORMALIZE-LABELS`, y `findings.md` en `FINDINGS-FAILED`.
Trabajar en su propio worktree (`git worktree add ../pipeline-label-verify -b label-verify`).

## Procedencia

Quién decidió qué, porque el `git log` no lo dice.

**Enunciado por el usuario** (la observación que abrió esto): el wizard le preguntó por
`divergent_label · Valor (en) | value (en)` y no se dio cuenta de que "Valor" está en español.
De ahí sus tres ideas: poder corregir el idioma de las etiquetas divergentes; traducir el término
al inglés y al español y comparar contra la traducción; y comparar normalizando snake_case,
camelCase y kebab-case.

**Avalado por el usuario** (elegido explícitamente en la conversación):

1. Cierre **híbrido por umbral**: si la traducción confirma el par con similitud muy alta, el
   hallazgo se resuelve solo con procedencia; debajo de eso queda abierto con la evidencia.
2. La corrección de idioma que detecta el modelo se **aplica automáticamente**, con procedencia.
3. Se hacen **las dos piezas**: corrección de idioma y verificación por traducción.
4. Se **agrega el plegado de diacríticos**.
5. La detección de idioma corre sobre **todo el inventario**, no sólo sobre los pares marcados.
6. Una etiqueta que es **nombre propio o término único de un área** —lo que no se puede decir si
   es español o inglés— lleva por defecto el idioma del documento donde se la menciona; para una
   etiqueta de la ontología, el idioma de la ontología. Es `UNDETERMINED-LANGUAGE`.
7. La tabla de overrides es la **fuente**, pero `normalize` sigue teniendo **un solo canal**:
   `LabelDecisions` crece un campo `languages` que se llena desde ella. Nada de un parámetro
   aparte que diga lo mismo.

**Enunciado por el modelo y aceptado por el usuario**, sin agregado suyo: la precedencia de la
`LANGUAGE-PRECEDENCE` tiene cuatro niveles y no dos, porque el `xml:lang` declarado en la semilla
es un
dato de la fuente y no una conjetura.

**Generado por el modelo, sin aval explícito** — se puede discutir sin romper nada de lo
anterior: el loteo, su tamaño y el corte por contenido; el valor 0,95 del umbral nuevo; hacer un
solo pase en vez de dos; la clave `(session_id, texto)` de la tabla y lo que se sigue de ella para
los homógrafos; el reintento partido; los nombres
(`PREP-NORMALIZE-LABELS-VERIFY`, comando `verify-labels`); dejar afuera la emisión de etiquetas
bilingües; resolver como `rejected` y reformular el texto del wizard.

## El problema, medido

`terms.guess_language` marca `es` sólo con ortografía española —acentos, o terminaciones
`ci[oó]n|dad|miento|mente|iv[oa]|at[oa]|ncia|eza|ura`— o con palabras función de una lista corta.
Todo lo demás cae al default `en`. Las terminaciones **sí** son insensibles a la tilde, y por eso
funcionan sobre una ontología que escribe el español sin tildes; lo que no cubren es la palabra
cuya españolidad no está en la terminación.

Medido el 2026-09-12 corriendo `terms.guess_language` sobre la semilla cualitativa:

| término | idioma detectado | |
|---|---|---|
| `interpretacion` | `es` | la terminación alcanza sin tilde |
| `clasificacion descriptiva` | `es` | idem |
| `año` | `es` | por la ñ |
| `valor` | **`en`** | falso |
| `tecnica` | **`en`** | falso |
| `tipo pauta` | **`en`** | falso |
| `metodologia` | **`en`** | falso |

Consecuencia sobre los hallazgos: cuando las dos etiquetas del par quedan taggeadas igual,
`_assess_divergence` las clasifica como `same_language_mismatch` → `divergent_label`, que afirma
una divergencia real, en vez de `cross_language_unverified` → `pending_semantic_check`, que sólo
afirma que nadie la verificó. En el almacén del usuario (`data/pipeline.sqlite3`, 2026-09-12) hay
3 `divergent_label` abiertas y **las tres son ese falso positivo**:

```
divergent_label | Relacionado a tecnica (en) | isApplicationTechnique (en)
divergent_label | Tipo pauta (en)            | patternType (en)
divergent_label | Valor (en)                 | value (en)
```

Más 27 `pending_semantic_check` abiertas, que sí están bien taggeadas porque sus derivados llevan
marcadores (`Es…`, `Tiene…`, `De…`), y 11 `divergent_label` que el usuario ya resolvió — **ésas
son el ground truth contra el que se mide el umbral nuevo**.

El tag errado no es cosmético: alimenta los léxicos por idioma de `detect_typos` (mezclados, una
traducción se lee como errata) y la regla `cross_language_always_grey` del matcher (una mención en
español contra una etiqueta mal taggeada `en` se fuerza a zona gris).

**El segundo defecto, del lado de la comparación:** `terms.tokens` no pliega diacríticos, así que
la misma palabra con y sin tilde no compara igual. Medido:

| par | `terms.similarity` | contra `label_divergence_threshold: 0.8` |
|---|---|---|
| `día` / `dia` | 0,667 | **se marca como divergente** |
| `año` / `ano` | 0,667 | **se marca como divergente** |
| `método` / `metodo` | 0,833 | pasa |
| `técnico` / `tecnico` | 0,857 | pasa |

La semilla actual no lo sufre porque escribe todo sin tildes y es consistente consigo misma; una
que mezcle etiquetas acentuadas con IRIs sin acentuar produce estos falsos positivos.

## Lo que ya está y no hay que tocar

- **snake_case, camelCase y kebab-case ya se normalizan.** `terms.denormalize` parte límites de
  camelCase (incluidas siglas: `hasHTTPServer` → `has HTTP Server`), `_`, `-` y espacios, y
  `tokens` pasa a minúsculas antes de comparar. Verificado: `divergent_label` contra
  `divergentLabel` da 1,0, y `has_subject` contra `has-subject` da 1,0. La tercera idea del
  usuario ya está implementada; no hay trabajo ahí. El único lugar que compara texto crudo es
  `_capitalization_anomaly`, y es a propósito.
- **La configuración de la etapa ya existe y no tiene consumidor.** `llm.prep_normalize_labels`
  está en `config.py` y en `config/default.yaml` (`{tier: small, temperature: 0.0,
  reasoning_effort: low}`, tier `small` = `glm-5.3-flash`). El `reasoning_effort` sólo está en el
  YAML: el default de `config.py` no lo trae, y como la clave de caché del ledger cubre la
  configuración entera, correr sin ese YAML es otra clave. Verificado con `rg`: ningún módulo la
  lee. Es el gancho reservado para esta etapa.

## Restricción de máquina: el ledger es secuencial

`telemetry.Ledger.run` recorre las unidades en un `for`, sin concurrencia. Con una llamada por
etiqueta, craft-cl (~3.418 clases, más las declaradas) son **1 a 3 horas**, contra la regla del
usuario de no correr trabajos de horas en esta máquina. Base de la estimación: los promedios del
ledger viejo están inflados porque `_plan` inserta todas las unidades antes de ejecutar, así que
la señal usable son los mínimos —3 s por llamada en glosas (tier medium), 6-7 s en extracción y
axiomatización—.

**Por eso el pase va loteado**, con precedente en `cq_generation`, que ya mete un pool por
llamada. Con lotes de 40: semilla cualitativa (34 entidades) una llamada; materiominer (428)
~11 llamadas; craft-cl ~170 llamadas, 6-12 minutos. La primera corrida mide la latencia real por
lote y de ahí se extrapola **antes** de tocar craft-cl.

### BATCH-BY-CONTENT — el lote es función del contenido, no de la posición

Con lotes, **la unidad del ledger es el lote**: su clave de caché sale del payload entero
(`telemetry.unit_key`). Si el lote se arma cortando la lista de entidades de a 40 en el orden en
que vienen, agregar una entidad o aceptar una errata que cambia una etiqueta corre todo un lugar
y **se pierde la caché de todos los lotes que siguen**. En craft-cl eso es re-pagar ~170
llamadas por un cambio de una palabra.

Tres reglas, y las tres son parte del contrato de la etapa:

1. **`BATCH-DEDUPE` — deduplicar por texto.** La etiqueta se consulta una vez aunque la tengan
   diez entidades: el idioma y la traducción son propiedad de la cadena.
   `FINDINGS-MEASURED-LABEL-COLLISION` ya midió que las etiquetas repetidas existen y no son un
   caso raro.
2. **`BATCH-SORT` — ordenar por texto**, no por entidad: el orden no depende de en qué orden
   `_mint_opaque_iris` recorrió el grafo.
3. **`BATCH-CUT` — cortar el lote donde lo dice el contenido.** Empieza lote nuevo cuando
   `sha256(texto) % label_batch_size == 0`, con corte forzado al doble de `label_batch_size` para
   que ninguno se dispare. El tamaño medio queda en `label_batch_size` y una etiqueta que entra o
   cambia **invalida su lote y ninguno más**, porque los cortes de los vecinos no se mueven. Es el
   mismo truco que usa el chunking definido por contenido, por la misma razón.

Sin `BATCH-CUT` las otras dos no alcanzan: ordenar hace el corte reproducible, pero no local.

## Decisiones

### FOLD-DIACRITICS — el plegado va en `terms.tokens`, no en `guess_language`

Plegar con `unicodedata` (NFKD y sacar las marcas combinantes) dentro de `tokens`, que es por
donde pasan `similarity`, los léxicos de erratas y el chequeo de palabras función. `guess_language`
sigue funcionando porque su test de ortografía corre sobre el término crudo (`_SPANISH_ORTHOGRAPHY.search(term)`)
antes de tokenizar, y su lista de marcadores no tiene tildes.

**El costo que se acepta y se documenta:** el plegado vuelve idénticos los pares mínimos que sólo
una tilde separa (`año`/`ano`, `término`/`terminó`). Está contenido porque la comparación es entre
dos grafías **de la misma entidad** —la derivada del IRI contra la declarada—, donde la misma
palabra es abrumadoramente más probable que un par mínimo. Va con test que lo fija como riesgo
conocido, no como sorpresa.

Esto **no** arregla `valor` ni `pauta`: esas no tienen tilde. Las arregla
`PREP-NORMALIZE-LABELS-VERIFY`.

### ONE-PASS-WHOLE-INVENTORY — un solo pase, sobre todo el inventario, loteado

Por cada etiqueta —la derivada del IRI y cada `rdfs:label` declarada— el modelo devuelve su idioma
y sus formas `es` y `en`. Con las formas ya calculadas no hace falta un segundo pase: la
comparación de traducciones de los pares marcados se hace en código sobre esa misma salida.

Pedir la traducción de todo el inventario y no sólo de los pares marcados cuesta unos tokens de
salida más por etiqueta y compra dos cosas: el tag de idioma correcto en todas las etiquetas (que
es lo que el usuario pidió), y el material que `main_plan.md` pide en `PREP-NORMALIZE-LABELS`
—emitir `rdfs:label@es` y `@en`— ya cacheado para cuando se decida escribirlo.

### VERDICT-IN-CODE — el código compara; el modelo nombra

El veredicto **no** se le pide al modelo. Con las formas devueltas, el código calcula

```
similitud = max(similarity(a.en, b.en), similarity(a.es, b.es))
```

y decide contra los umbrales. `a` es la etiqueta derivada del identificador y `b` la declarada
—y **hay una `b` por cada `rdfs:label`**: el veredicto es el máximo sobre todas las declaradas,
igual que `_assess_divergence`, que ya usa `any()` sobre ellas. Una entidad con dos etiquetas
declaradas se cierra si **alguna** confirma el par, porque el hallazgo afirma que ninguna nombra
al concepto. Es `MODEL-NAMES-CODE-BUILDS`, y acá hay además una razón
medible: el ejemplo que el propio spec usa para justificar la salvedad —`Aplica_una_o_varias`
frente a `appliesTechnique`— **sobrevive a la traducción** ("applies one or several" contra
"applies technique", ~0,4) y por lo tanto sigue marcado, mientras que un modelo al que se le pide
veredicto directo puede contestar "mismo concepto" y cerrarlo mal.

### TWO-THRESHOLDS — dos umbrales, y el nuevo no está medido

| Similitud entre traducciones | Qué pasa |
|---|---|
| ≥ `translation_verified_threshold` (0,95) | el hallazgo se resuelve con procedencia: veredicto, formas es/en y la similitud en el comentario |
| entre 0,8 y 0,95 | queda **abierto**, con las traducciones en el payload como evidencia, y el wizard las muestra |
| < 0,8 | queda abierto: la divergencia se sostiene incluso traducida |

`translation_verified_threshold` es nuevo y **no está medido**: se documenta así en
`config/default.yaml` y la corrida de validación lo contrasta contra las 11 decisiones que el
usuario ya tomó.

### LANGUAGE-PRECEDENCE — los overrides son datos: `user > declarado > model > guess`

`normalize` es función determinista de la semilla en disco: si la corrección no es **entrada** de
esa función, se pierde en la próxima corrida. Y como el id de un hallazgo deriva de su contenido
—que incluye los idiomas—, una corrección aplicada sin persistir no reabre el mismo hallazgo:
crea otro. Por eso una tabla, escrita por dos lados (el modelo y el usuario) y leída por
`normalize`.

**Los cuatro niveles, de mayor a menor:**

| nivel | de dónde sale | por qué gana |
|---|---|---|
| `user` | `review correct-language` | es la única corrección que nadie puede recomputar |
| `declarado` | el `xml:lang` del literal en la semilla | es **dato de la fuente**, no conjetura: craft-cl trae 110 `rdfs:label` con `xml:lang="en"`, y pisarlos sería contradecir a quien publicó la ontología |
| `model` | este pase | mira las palabras, no la ortografía |
| `guess` | `terms.guess_language` | la regla de terminaciones, que es lo que hay sin proveedor |

`_entity` ya implementa los dos últimos niveles al revés de como se leen: usa `literal.language`
si el literal lo trae y si no adivina. Lo que se agrega son `user` y `model`, y el modelo
**no se consulta para pisar un tag declarado** — se lo consulta igual, porque su traducción se
usa después, pero su campo `language` se descarta para esas etiquetas.

**La tabla es la fuente; el canal hacia `normalize` sigue siendo uno solo.** `LabelDecisions`
—que ya lleva `typo_fixes` y `dropped_derived`, y existe exactamente por este argumento— crece un
tercer campo `languages`, que `prep.label_decisions` arma leyendo la tabla.
`normalize_initial_ontology`
no recibe un parámetro aparte: dos parámetros que dicen lo mismo se desincronizan, y el que se
olvide de pasar uno no rompe nada visible.

La fila se indexa por **`(session_id, texto)`**, no por entidad: el idioma es propiedad de la
cadena, la misma etiqueta la pueden tener diez clases, y así la clave sobrevive al re-minteo de
IRIs. Si una errata aceptada cambia el texto, el override deja de aplicar y se cae al nivel de
abajo, que es el modo de falla correcto. Columnas: el texto, el idioma, `source` (`user` | `model`)
y cuándo se escribió.

Que el hallazgo viejo quede `superseded` y aparezca uno nuevo con el kind correcto **es el
comportamiento buscado**, no un efecto colateral: el par cambió de naturaleza al corregirse el
idioma.

### UNDETERMINED-LANGUAGE — lo que no es ni español ni inglés lleva el idioma de donde está

Un nombre propio, una sigla o un término único de un área —`Nanog`, `Drosophila`, `RNA-seq`— no
tiene idioma que decidir, y forzar `es` o `en` es inventar un dato. El modelo puede contestar
`und` para esos, y **el código resuelve el default**: el idioma del documento donde se menciona
el término —los `documents` y `blocks` ya llevan `language` y `language_source`— y, para una
etiqueta de la ontología, el idioma de la ontología, que es el mayoritario de sus etiquetas ya
resueltas por `LANGUAGE-PRECEDENCE`.

Es `VERDICT-IN-CODE` otra vez: el modelo nombra —dice «esto no es ninguno de los dos»— y el código
arma. `parse` acepta `es`, `en` y `und`, y **nada más**; un cuarto valor sigue fallando la unidad.

**El homógrafo entra por acá, y es lo único que hay que hacer con él.** `control`, `material`,
`general`, `actual`, `final`, `normal`, `editor`, `simple` se escriben igual en los dos idiomas —y
con `FOLD-DIACRITICS` se suman algunos más, como `region`/`región`—. La cadena sola no
alcanza para decidir, ni para el código ni para el modelo, que ve exactamente lo mismo. Dos
consecuencias, y las dos se atajan sin maquinaria nueva:

1. **El prompt pide `und` también para eso**: una etiqueta cuya grafía es la misma en los dos
   idiomas y que no trae ninguna pista se contesta `und`, no se elige una al azar.
2. **Un `und` no puede producir `same_language_mismatch`.** Si cualquiera de los dos lados del par
   quedó indeterminado, la razón es `cross_language_unverified`: nadie verificó nada, que es lo
   único cierto. Afirmar una divergencia real sobre un idioma que no se determinó es asertar lo
   que no se sabe (`OPEN-WORLD`). Para eso el `Label` tiene que recordar que vino `und` **además**
   del idioma con el que se lo escribe en el grafo: un literal necesita un tag, y ése sale del
   default de esta misma decisión.

Lo que **no** se hace: indexar el override por `(iri, texto)` para que la misma cadena pueda ser
`es` en una entidad e `en` en otra. Costaría renunciar a `BATCH-DEDUPE` y
multiplicar las llamadas por las etiquetas repetidas, a cambio de un caso que el override manual
del usuario ya cubre cuando importa. El canje es deduplicar contra homógrafo, y se elige
deduplicar.

## Fuera de alcance, a propósito

- **Escribir las traducciones como `rdfs:label@es` / `@en`** para toda entidad, que es lo que
  `PREP-NORMALIZE-LABELS` pide. El pase produce el material, pero esas etiquetas son la superficie
  contra la que compara el matcher: agregar una etiqueta en español a cada clase cambia el volumen
  de zona gris y los resultados de tipado, y eso merece su propia medición. Queda como entrada de
  `technical_debt.md` con el material ya cacheado en el ledger. (Es prometedor en una dirección
  concreta: con las dos lenguas presentes, una mención en español deja de chocar contra
  `cross_language_always_grey`.)
- **Extender la regex de idioma** con más terminaciones. Es jugar a la mancha y ya está registrado
  como `DEBT-LANGUAGE-HARDCODED`; lo que cubre el hueco es la detección del modelo.
- **Aplicar las correcciones de erratas.** Hoy nada consume una decisión de `review_items`
  —`review.py` registra, actuar es otro paso— y este plan no cambia eso para los `typo`.

## El contrato de la etapa

**Id:** `PREP-NORMALIZE-LABELS-VERIFY`, hijo de `PREP-NORMALIZE-LABELS`. **Comando:**
`verify-labels`. **Stage del ledger:** `prep_normalize_labels` (la clave que ya existe).

Sus partes se citan por nombre y no por número de orden: las decisiones son `FOLD-DIACRITICS`,
`ONE-PASS-WHOLE-INVENTORY`, `VERDICT-IN-CODE`, `TWO-THRESHOLDS`, `LANGUAGE-PRECEDENCE` y
`UNDETERMINED-LANGUAGE`; el armado de los lotes es `BATCH-BY-CONTENT`, con `BATCH-DEDUPE`,
`BATCH-SORT` y `BATCH-CUT`; los pasos del servicio van de `VERIFY-1-SESSION` a
`VERIFY-8-REPORT`; y los commits, de `COMMIT-FOLD` a `COMMIT-MEASUREMENT`. Todos se dan de alta
en el índice del `README.md` cuando este plan se commitea.

Módulo nuevo `src/onto_pipeline/label_verification.py`, con la forma de `glosses.py`: `STAGE`,
`PROMPT` versionado, `payload()`, `parse()` y las funciones puras del veredicto. Nada de I/O ni de
almacén acá.

### El prompt

Ilustrativo, en inglés como todos los prompts. Numerar las etiquetas del lote y exigir la
respuesta indexada es lo que permite detectar que el modelo descartó ítems:

```
You are identifying the language of ontology labels and translating them.

Each numbered line is one label: a short noun phrase or a property name, already split from
its identifier (`appliesTechnique` arrives as `applies Technique`).

{labels}

For each number, answer three things:
- "language": the language the label is written in, "es" or "en". Judge the words, not the
  spelling — Spanish written without accents is still Spanish. Answer "und" when the label is a
  proper name, an acronym or a technical term that belongs to no language in particular
  ("Drosophila", "RNA-seq"), and also when it is spelled the same in both languages and nothing in
  it tells them apart ("control", "material"): do not guess one for it.
- "en": the label in English. If it already is English, repeat it unchanged.
- "es": the label in Spanish. If it already is Spanish, repeat it unchanged.

Translate the term; do not explain it and do not expand it. A translation of the same length
is what makes two labels comparable. Keep the word order of the original.

Answer with JSON only, keyed by number:
{"1": {"language": "es", "en": "...", "es": "..."}, "2": {...}}
```

`parse` **falla** si falta algún índice del lote, si falta una de las tres claves o si el idioma
no es `es`, `en` ni `und` (`UNDETERMINED-LANGUAGE`). Sin eso el modelo descarta etiquetas en
silencio, que es exactamente la clase de falla que registra `FINDINGS-SILENT-FAILURES`; la unidad
fallada la contabiliza `_check_failure_rate` como cualquier otra.

### El orden interno del servicio

`prep.verify_labels(workspace, normalization, *, progress=silent) -> LabelVerification`

Recibe la `Normalization` y no la calcula, como `generate_glosses`: **quien llama corre
`prep.normalize(workspace)` primero**. Es barato —no toca el modelo— y vale para los tres
caminos: el comando suelto `verify-labels`, la llamada dentro de `normalize_cmd`, y el wizard
cuando ya hay versión, que es el caso de la sesión del usuario y hoy retorna antes de llegar acá.
Que la etapa no re-normalice por su cuenta es lo que hace que las tres vean exactamente la misma
semilla, con las mismas decisiones ya aplicadas.

Los pasos llevan nombre porque se citan desde otras partes de este plan, y el número porque el
orden pesa: `VERIFY-4-OVERRIDES` antes de `VERIFY-5-REDERIVE` no es una preferencia.

1. **`VERIFY-1-SESSION`** — `workspace.require_session()`; sin proveedor, `ProviderMissing`.
2. **`VERIFY-2-ASK`** — juntar todas las etiquetas de `normalization.seed.entities`, armar los
   lotes con `BATCH-BY-CONTENT` y correr `llm.run(workspace.ledger(), workspace.model(), PROMPT,
   llm.settings(config.llm, STAGE), …)`.
3. **`VERIFY-3-SPLIT` — reintentar partido lo que falló.** Un lote que `parse` rechaza se vuelve a
   correr en dos mitades, y cada mitad que falla se parte otra vez, hasta una etiqueta sola. La
   que falla sola queda fallada y la cuenta `_check_failure_rate` como cualquier unidad. Partir no
   puede pasar por el reintento del ledger —que repite la **misma** unidad `max_retries: 3` veces
   con el mismo payload—: una mitad es otro payload y por lo tanto otra unidad, con su propia
   clave. Lo que compra: un solo ítem mal contestado no se lleva puestas 39 etiquetas buenas. Lo
   que cuesta: el lote entero se vuelve a pedir en la corrida siguiente aunque las mitades estén
   cacheadas, porque quedó `failed` y el ledger re-ejecuta lo que no está `done`.
4. **`VERIFY-4-OVERRIDES`** — escribir los overrides de idioma detectados con `source: model`, sin
   pisar los `user`.
5. **`VERIFY-5-REDERIVE`** — re-derivar con `normalize_initial_ontology(...)`, con los overrides
   ya fusionados, **volver a escribir las glosas que ya están en el ledger**, re-serializar a
   `normalization.target` y commitear la versión **conservando el hash del padre**, como hace
   `generate_glosses`: cambiar un tag de idioma cambia el artefacto, no el estado lógico.

   Las glosas no son un detalle: `normalize_initial_ontology` re-lee la semilla del disco, así
   que el grafo re-derivado **no tiene `skos:definition`**, y commitearlo así deja el tip sin
   definiciones — justo el caso de la sesión del usuario, que ya las generó. Son todos aciertos
   de caché, no cuestan una llamada, y el camino es el mismo que ya corre `generate_glosses`.
6. **`VERIFY-6-SYNC`** — re-sincronizar hallazgos con `review.sync(...)`, y que `kinds` incluya
   **también `TYPO`**: re-taggear cambia los léxicos por idioma y por lo tanto la salida de
   `detect_typos`, así que sin eso quedan erratas viejas abiertas sobre un léxico que ya no
   existe.
7. **`VERIFY-7-VERDICT`** — por cada hallazgo abierto de kind `divergent_label` o
   `pending_semantic_check`, calcular la similitud entre traducciones y aplicar la tabla de
   `TWO-THRESHOLDS`. Resolver es `review.resolve(..., REJECTED, comment=...)`: para estos dos
   kinds, `rejected` se lee como "el hallazgo no se sostiene", y el comentario dice por qué.
   Anotar evidencia necesita una función nueva en `review.py` (`annotate(conn, item_id, patch)`),
   porque `sync` sólo inserta.
8. **`VERIFY-8-REPORT`** — devolver el reporte tipado; lo pinta `render.label_verification`.

**La trampa de `VERIFY-7-VERDICT`:** `Finding.id` deriva del payload que arma
`findings_from_initial`. Escribir evidencia en el payload de la **fila** es seguro, porque el id
se recalcula desde el hallazgo generado y no desde la fila. Meter la evidencia en
`findings_from_initial` **no** lo es: cambiaría todos los ids y dejaría huérfana cada decisión ya
tomada.

**`normalize` también lee los overrides, y ésa es la mitad que los hace durar.** Esta etapa los
escribe, pero la que corre todo el tiempo —sola, desde el CLI y desde el wizard— es
`prep.normalize`, y hoy le pasa a `normalize_initial_ontology` únicamente
`decisions=label_decisions(workspace)`. Si no los leyera, la corrida siguiente volvería a
etiquetar con el guess, el par volvería a `same_language_mismatch`, y como el id del hallazgo
deriva de los idiomas aparecería **otro** hallazgo, abierto, idéntico al que se acaba de resolver.
Es el mismo argumento que fundó `LabelDecisions`, aplicado a un insumo nuevo.

## Cambios por archivo

| Archivo | Qué |
|---|---|
| `src/onto_pipeline/terms.py` | `fold()` con `unicodedata`; `tokens()` la aplica. `guess_language` sigue leyendo el término crudo |
| `src/onto_pipeline/label_verification.py` | **nuevo**: `STAGE`, `PROMPT`, `payload`, `parse`, veredicto |
| `src/onto_pipeline/initial_ontology.py` | `LabelDecisions.languages`, aplicado en `_entity` con `LANGUAGE-PRECEDENCE`; llega a `_assess_divergence` —donde un `und` fuerza `cross_language_unverified`— y a `_write_labels` |
| `src/onto_pipeline/label_overrides.py` o dentro de `initial_ontology.py` | la tabla, con `SCHEMA` + `install()` como hace `review.py`; clave `(session_id, texto)`, columnas idioma y `source`; lectura y escritura con precedencia |
| `src/onto_pipeline/review.py` | `annotate(conn, item_id, patch)` para sumar evidencia sin tocar el estado |
| `src/onto_pipeline/services/prep.py` | `verify_labels(...)` y su dataclass de resultado; y que **`label_decisions` lea la tabla** y llene `LabelDecisions.languages`, que es lo que hace que `normalize` los re-aplique sola |
| `src/onto_pipeline/services/evaluate.py` | `correct_label_language(workspace, item_id, label_text, language)`, y darla de alta en la lista de `__all__`; cierra con `workspace.note("decision", …)` como `resolve_review` |
| `src/onto_pipeline/interfaces/api/app.py` | si la corrección se expone por HTTP, su ruta llama `jobs.require_idle(conn, session_id)` antes, como las demás decisiones. La guarda **no** puede vivir en el servicio: `jobs` está en `interfaces/api/` y un servicio no importa de una interfaz (`SERVICES-NO-INTERFACE`) |
| `src/onto_pipeline/interfaces/render.py` | `label_verification(...)`; y que `review_list` y el wizard muestren la evidencia del payload |
| `src/onto_pipeline/interfaces/cli.py` | comando `verify-labels`; llamada dentro de `normalize_cmd` entre `normalize` y las glosas; `review correct-language` |
| `src/onto_pipeline/interfaces/wizard.py` | ofrecer la verificación en `_normalize_initial`, **y también cuando ya hay versión** (hoy retorna temprano en la línea del `existing is not None`, que es el caso de la sesión del usuario); opción de corregir idioma en `_decide_review` |
| `src/onto_pipeline/orchestration.py` | fila de la etapa **antes** de la fila `review`, con el estado **derivado de los datos**: `done` si hay unidades `done` del stage `prep_normalize_labels` para la sesión (`telemetry.stage_report`), y el detalle dice cuántas divergencias abiertas quedaron sin verificar |
| `src/onto_pipeline/config.py` | `translation_verified_threshold: float = 0.95`, `label_batch_size: int = 40` en `InitialOntology`; y `prep_normalize_labels` con su `reasoning_effort`, que hoy sólo está en el YAML |
| `config/default.yaml` | los dos valores, con el porqué: 0,95 **no medido**; el lote, por el ledger secuencial — y que `label_batch_size` es el tamaño **medio**, porque el corte lo define el contenido |

## Tests

Nombres en frase y docstring que diga qué decisión fija, como el resto de la suite.

**`tests/test_terms.py`** (nuevo, `terms.py` no tenía archivo propio)

- `test_the_same_word_with_and_without_an_accent_compares_as_itself` — `día`/`dia` pasa de 0,667 a
  1,0.
- `test_folding_accents_does_not_break_the_language_guess` — `guess_language("año")` sigue dando
  `es`.
- `test_an_accent_is_the_only_thing_separating_some_words` — `año`/`ano` plegados coinciden: el
  costo que acepta `FOLD-DIACRITICS`.
- `test_case_conventions_already_compare_equal` — fija lo que ya funciona, para que no se rompa al
  plegar.

**`tests/test_initial_ontology.py`**

- `test_a_language_override_beats_the_guess`
- `test_an_overridden_language_moves_the_divergence_across_languages` — el caso `Valor`: con
  override `es`, la razón pasa a `cross_language_unverified`.
- `test_retagging_a_label_separates_the_typo_lexicons` — la traducción deja de leerse como errata.

**`tests/test_label_verification.py`** (nuevo)

- `test_every_label_in_the_batch_has_to_come_back` — `parse` levanta si el modelo descarta un
  índice.
- `test_a_pair_that_matches_once_translated_resolves_the_finding` — con `llm.ScriptedModel`.
- `test_the_spec_example_survives_verification` — `Aplica_una_o_varias` contra `appliesTechnique`
  sigue abierto; cita la salvedad de `PREP-NORMALIZE-LABELS`.
- `test_a_pair_between_the_two_thresholds_stays_open_with_its_evidence`
- `test_a_correction_made_by_the_user_is_not_overwritten_by_the_model`
- `test_a_declared_language_tag_is_not_overwritten_by_the_model` — `LANGUAGE-PRECEDENCE`.
- `test_a_name_that_belongs_to_no_language_takes_the_ontologys` — `und` resuelto por código,
  `UNDETERMINED-LANGUAGE`.
- `test_a_word_spelled_the_same_in_both_languages_is_never_a_same_language_mismatch` — el
  homógrafo no afirma una divergencia que nadie verificó.
- `test_adding_one_label_only_reruns_its_own_batch` — el corte por contenido: con una etiqueta
  más, las claves de los otros lotes no se mueven.
- `test_a_batch_the_model_mangles_is_retried_in_halves` — y las etiquetas sanas de ese lote se
  resuelven igual.
- `test_a_single_label_that_keeps_failing_is_one_failed_unit` — el piso de la partición, contado
  por `_check_failure_rate`.

**`tests/test_services.py`** (no hay `test_prep.py`: las de `prep` viven ahí)

- `test_verifying_labels_keeps_the_glosses_of_the_previous_version` — `VERIFY-5-REDERIVE`, y el
  bug que arregla `COMMIT-GLOSSES`.

**`tests/test_services.py`** — `verify_labels` sin proveedor levanta `ProviderMissing`.
**`tests/test_orchestration.py`** — `test_the_label_check_is_done_when_the_ledger_says_so`: el
estado de la fila sale de las unidades del stage, no de una columna que alguien tenga que
acordarse de escribir (`PHASE-DERIVED`).
**`tests/test_review.py`** — `test_evidence_can_be_added_without_touching_the_decision`.
**`tests/test_session_scope.py`** y **`tests/test_store.py`** tienen que seguir pasando: la tabla
nueva lleva `session_id` y se filtra en toda consulta (`SESSION-SCOPED-DATA`), y su SQL es portable
—`?`, `ON CONFLICT (...) DO NOTHING` como ya usan `review.py` y `telemetry.py`, JSON leído en
Python— (`STORE-NO-DIALECT`).

## Verificación manual

Que la suite pase no prueba que la edición se aplicó: correr el comando y mirar la salida.

```bash
uv run pytest -q
uv run ruff check .

uv run onto-pipeline review list --kind divergent_label        # 3 abiertas antes
uv run onto-pipeline --env-file opencode.env verify-labels
uv run onto-pipeline review list --kind divergent_label        # qué se resolvió
uv run onto-pipeline review list --status rejected --json      # el comentario con la procedencia
uv run onto-pipeline status                                    # unidades y tokens de prep_normalize_labels

rg '@es' data/ontology/initial_normalized.ttl | head           # los tags corregidos en el grafo
```

Lo que tiene que haber pasado: `Valor | value` resuelto con su traducción en el comentario;
`Relacionado a tecnica | isApplicationTechnique` abierto con evidencia; ninguna `divergent_label`
abierta cuyo par sea la misma palabra en dos idiomas.

## Medición a registrar

Entrada nueva en `findings.md`, `FINDINGS-MEASURED-LABEL-VERIFICATION`, con fecha, n y contra qué
se midió: etiquetas consultadas y lotes; cuántas cambiaron de idioma; cuántos hallazgos se
resolvieron por traducción, cuántos quedaron con evidencia y cuántos siguen divergentes;
latencia por lote, tokens y costo; y el **contraste contra las 11 `divergent_label` que el usuario
ya había decidido**, que es lo que dice si 0,95 está bien puesto. Si alguna de esas 11 se
contradice con el veredicto, el número va igual y el umbral se mueve con él.

Aparte, y opcional porque pide re-correr el matcher: cuántos pares de zona gris se mueven al
corregirse los idiomas. `DEBT-CROSS-LANGUAGE-GREY` dice que esa regla nunca se midió.

## Orden de commits

Uno por hito, con cuerpo que diga el porqué.

1. **`COMMIT-FOLD`** · `feat(core): plegar diacríticos al comparar términos`
2. **`COMMIT-GLOSSES`** · `fix(services): conservar las glosas al re-normalizar` — el agujero que
   ya existe hoy, el mismo que necesita `VERIFY-5-REDERIVE`
3. **`COMMIT-OVERRIDES`** · `feat(services): overrides de idioma de etiqueta, con corrección
   manual`
4. **`COMMIT-VERIFY`** · `feat(services): verificar etiquetas divergentes traduciéndolas`
5. **`COMMIT-PLAN`** · `docs(spec): plan de verificación de etiquetas` — este archivo, su fila en
   el índice de documentos del `README.md`, y el alta de cada nombre nuevo de acá en el índice de
   identificadores
6. **`COMMIT-MEASUREMENT`** · `docs: medición de la verificación de etiquetas` — `findings.md`, la
   tabla de estado del `README.md`, y la entrada de `technical_debt.md` por las etiquetas
   bilingües que quedaron afuera

`COMMIT-FOLD` y `COMMIT-GLOSSES` son independientes entre sí y de todo lo demás. `COMMIT-VERIFY`
depende de `COMMIT-OVERRIDES`, y también de `COMMIT-GLOSSES` para no commitear una versión sin
definiciones.

## Trampas conocidas

- **No agregar propiedades de anotación.** La etapa escribe `rdfs:label` y `skos:prefLabel`, que
  es lo que ya escribe `_write_labels`. Cualquier otra tiene que estar en
  `initial_ontology.DECLARED_ANNOTATIONS` o la ontología se sale de OWL 2 DL y el síntoma es ELK
  salteándose en silencio (`DECLARED-ANNOTATIONS-ONLY`).
- **Una llamada por etiqueta convierte esto en un trabajo de horas.** El ledger es secuencial; el
  loteo no es una optimización, es la condición para que corra acá.
- **`Finding.id` deriva del payload.** Ver la trampa de `VERIFY-7-VERDICT`.
- **Re-normalizar pisa las glosas, y eso ya pasa hoy.** `prep.normalize` re-lee la semilla del
  disco y re-serializa el artefacto: corrido después de `generate_glosses`, el
  `initial_normalized.ttl` queda sin `skos:definition` hasta que alguien vuelva a glosar. Es un
  bug, no deuda, y lo arregla `COMMIT-GLOSSES` —el mismo camino que necesita `VERIFY-5-REDERIVE`—.
- **Ningún servicio importa `typer` ni `rich`** (`SERVICES-NO-INTERFACE`): el reporte es una
  dataclass y lo pinta `render.py`.
- **El wizard retorna temprano cuando ya hay versión.** Si la etapa sólo se engancha en el camino
  de la normalización fresca, la sesión que ya normalizó —la del usuario, con 30 hallazgos
  abiertos— no puede correrla nunca.
- **Re-correr `verify-labels` es barato pero no gratis:** con el ledger cacheado son aciertos de
  caché, pero el re-derivado y el re-sync corren igual. Que sea idempotente entra en los tests.
