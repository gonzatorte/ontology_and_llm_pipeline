# Deuda técnica y mejoras

Documento vivo. Registra **mejoras a futuro** —cosas que hoy funcionan pero podrían estar
mejor, y decisiones tomadas con evidencia insuficiente que conviene rehacer cuando la haya—.
No es una lista de bugs: lo que está roto se arregla, no se documenta.

> **Al agregar una entrada:** los números colisionan cuando dos sesiones escriben en paralelo —
> ya pasó con el 17—. Antes de numerar, mirá el último `###` que hay. Y al referenciar otra
> entrada, mejor por título que por número, porque los números se corren.

El estado de implementación por etapa está en el [README](README.md) y los números que
sostienen cada decisión en [HALLAZGOS.md](HALLAZGOS.md); acá va lo que no se ve mirando ninguno
de los dos.

---

## Coordinación entre conversaciones

**Hay más de una sesión trabajando sobre este repo.** Al momento de escribir esto: tres
(`pipeline-a4`, `pipeline-26`, y la que escribe). Eso está bien, pero tiene un modo de falla
concreto que ya ocurrió.

### El conflicto que ya pasó

Una sesión editó `cq eval` con un reemplazo de texto anclado a esta línea:

```python
console.print(f"evaluating against version [bold]{row['id']}[/]")
```

Otra sesión, en paralelo, había extraído `_resolve_version()` y refactorizado los tres lugares
que resolvían la versión (`validate`, `match`, `cq eval`), con lo que `row['id']` pasó a ser
`version_id`. El reemplazo **no matcheó y no hizo nada**, en silencio. El código siguió con el
comportamiento anterior, los tests siguieron pasando —porque probaban otra cosa— y el problema
solo se detectó al notar que el comando imprimía el mensaje viejo en una corrida manual.

### Por qué importa más de lo que parece

No es un error de tipeo: es una clase de falla. Una edición anclada a texto exacto **falla
abierta** cuando otro cambió el contexto — no rompe nada, no avisa, deja el código como estaba.
Es exactamente el modo de falla más difícil de detectar, porque no produce ninguna señal.

### Qué hacer

- **Verificar el efecto, no la ausencia de error.** Después de editar un comando, correrlo y
  mirar la salida. Que los tests pasen no prueba que la edición se aplicó.
- **Preferir herramientas que fallan cerradas.** `Edit` sobre un archivo recién leído aborta si
  el texto no está; un `str.replace` en un script no. Cuando haya que usar un script, verificar
  después con un `grep` de lo que se esperaba escribir.
- **Zonas calientes**: `cli.py` es el archivo que todas las sesiones tocan, porque cada etapa
  nueva agrega un comando. Es donde este conflicto va a volver a pasar.
- **Commitear seguido.** Un working tree con 126 líneas sin commitear de otra sesión —lo que
  pasó con `matching.py`— hace imposible distinguir trabajo en progreso de trabajo terminado.
  Durante un rato esas líneas rompían 6 tests; era un estado intermedio, pero desde afuera no
  se podía saber.

### Los transcripts tienen fecha de vencimiento

Verificado: `cleanupPeriodDays` no está configurado, así que rige el default de **30 días**. Los
transcripts de las sesiones son el único registro del razonamiento que no llegó al repositorio —
por qué se descartó una alternativa, qué se midió y no se anotó, qué preguntó el usuario— y a los
30 días no están más.

La consecuencia práctica: **lo que importa se escribe en el repo antes de cerrar una sesión**, no
se deja "por si hace falta mirar la conversación". Esta ronda de auditoría encontró un hallazgo
sustantivo que sólo vivía en un transcript (el análisis del homónimo, hoy `DEBT-CONTEXT-DISAMBIGUATION`) y estuvo a
semanas de perderse. Si se prefiere la otra vía, hay que subir `cleanupPeriodDays` en
`~/.claude/settings.json`, pero eso conserva el transcript, no lo vuelve encontrable.

### Cómo leer el historial de sesiones, si hace falta

Los transcripts están en `~/.claude-personal/projects/-home-gonzalo-workspace-propio-ontology-and-llm-pipeline/*.jsonl`,
uno por sesión. Dos advertencias al auditarlos:

- **Un fork comparte el principio con su origen.** `f0449040` y `b8c7e99c` arrancan con el mismo
  timestamp y el mismo mensaje: son la misma conversación, bifurcada. Contarlas como dos sesiones
  independientes lleva a buscar conflictos donde no hay más que una rama abandonada.
- **La atribución no se lee del historial de git.** Los 44 commits tienen un solo autor y un solo
  trailer de sesión, aunque el trabajo salió de tres. Quién decidió qué está en la tabla de
  procedencia de [`HALLAZGOS.md`](HALLAZGOS.md), no en `git log`.

---

## Mejoras estructurales

### DEBT-DATA-ACCESS-LAYER — Capa de acceso a datos

Hoy el SQL crudo está repartido en 8 módulos y `sqlite3` se importa en cada uno. Funciona, pero
tiene dos costos: migrar a otro motor sería un barrido manual por todos ellos (~13 lugares con
SQL específico de SQLite: `executescript`, `IFNULL`, `SUM(status = 'done')`, `json_extract`,
`sqlite3.Row`), y los tests que tocan persistencia necesitan una base real.

Una capa fina de acceso a datos resuelve las dos cosas a la vez. **No es urgente**: SQLite
alcanza de sobra para este workload y `BUILD-OUT-OF-SCOPE` deja Fuseki fuera de v1 explícitamente. Pero si
alguna vez aparece la necesidad de Postgres, la inversión que rinde es ésta, no la migración.

### DEBT-PARALLEL-EXTRACTION — Paralelizar las llamadas de `ITER-EXTRACT`

Una pasada de `extract` sobre 100 documentos son ~1.320 llamadas secuenciales de varios
segundos cada una. El cuello de botella es la latencia de red, no la base: las escrituras al
ledger son microsegundos. Con WAL ya activado, un pool acotado de hilos es viable y el ledger
ya serializa correctamente.

### DEBT-CACHE-INFERRED-GRAPH — Cachear el grafo inferido por versión

`cq eval --infer` materializa las entailments con HermiT en cada corrida. Sobre la semilla son
1.087 → 1.426 tripletas y tarda poco, pero es determinista dado el estado de la ontología: la
versión ya tiene un hash de estado, así que el grafo inferido se puede guardar contra él y
recomputar solo cuando el estado cambia.

### DEBT-SKIP-LOW-YIELD-SECTIONS — Saltear secciones de bajo rendimiento en `ITER-EXTRACT`

Bibliografía y referencias son ~20% de un paper y no producen menciones útiles. Un filtro
determinista por tipo de sección cortaría ~20% de las llamadas de `ITER-EXTRACT` sin perder nada.

### DEBT-MEMORY-FOOTPRINT — Footprint de memoria: representación primero, infraestructura después

Todo vive en memoria: el grafo en rdflib (`RDFLIB-IN-MEMORY`) y los vectores en un dict dentro del `Matcher`.
La pregunta natural es si eso escala y si conviene mover cosas a almacenes externos —triplestore,
base de vectores, Postgres—. **Medido, la respuesta corta es que a la escala declarada no hace
falta, y que la mejora que sí rinde no es infraestructura sino representación.**

Lo medido sobre el estado actual:

| Qué | Ahora | Proyección |
|---|---|---|
| Grafo rdflib | 1.087 tripletas · 1,5 MB (1.386 B/tripleta) | 20k tripletas → **28 MB** · 200k → 277 MB |
| Un vector como `list[float]` | **12,1 KB** | 1.000 documentos → **1,0 GB** |
| El mismo en `numpy float32` | **1,5 KB** (8× menos) | 1.000 documentos → **0,13 GB** |

Menciones medidas: 85 por documento. `BUILD-OUT-OF-SCOPE` estima que un ABox de 1.000 páginas da 5k–20k
tripletas, así que a la escala del spec —100 documentos, 30–50 clases— el grafo son ~28 MB y
los vectores ~0,1 GB. Nada de eso justifica un servicio aparte.

**Lo que sí conviene hacer, y es barato:** el caché de vectores del `Matcher` guarda
`dict[str, list[float]]`. En numpy `float32` ocupa 8× menos, sin dependencia nueva ni servicio
nuevo — numpy ya está instalado con el extra `matching`. Es un cambio local que convierte 1 GB
en 130 MB en el peor caso proyectado.

**Cuándo sí cambiaría la respuesta.** El spec ya fijó los umbrales y conviene respetarlos en vez
de anticiparse:

- **Fuseki / triplestore** (`BUILD-OUT-OF-SCOPE`): "reconsiderar cuando el ABox no entre en memoria". Con
  1.386 B/tripleta eso son millones de tripletas, dos órdenes de magnitud más que lo previsto.
- **Base de vectores**: el criterio análogo es cuando la búsqueda exhaustiva deje de servir.
  Hoy son 34 clases objetivo; el blocking ya acota los pares de resolución de entidades y el
  cálculo de similaridad se materializa por bloques de 512 filas, así que el pico no es n².
  Una base de vectores con ANN paga recién cuando los objetivos sean decenas de miles.
- **Embeddings de grafos de conocimiento** (`KG-EMBEDDINGS-OUT-OF-SCOPE`, `BUILD-OUT-OF-SCOPE`): explícitamente fuera de v1 hasta que el
  grafo crezca uno o dos órdenes de magnitud.

**Postgres es otro eje, no éste.** Migrar el store no baja el footprint: el grafo y los vectores
siguen en el proceso. Solo ayudaría si además se mueve el *cómputo* al motor —por ejemplo
pgvector resolviendo la búsqueda por similaridad del lado del servidor—, y eso recién rinde a
escalas muy superiores a ésta. Para la discusión de motor de base ver el punto 1.

### DEBT-PROMPT-CACHING — Medir si el prompt caching del proveedor se activa

El gateway reporta `prompt_tokens_details.cached_tokens` y los prompts ya tienen el prefijo
estático adelante, que es la forma correcta para caching por prefijo. Nunca se midió si se
activa. Impacto acotado —la entrada no es el driver de costo, el razonamiento sí— pero es
información gratis.

---

## Decisiones tomadas con evidencia insuficiente

Estas no son mejoras opcionales: son decisiones que hoy están apoyadas en muy poco y que
condicionan todo lo que viene después.

### DEBT-MATCH-AGAINST — `matching.match_against` y `matching.use_cross_encoder` — RESUELTO

**Medido sobre `craft-cl`**: 8.723 menciones gold contra 3.418 clases candidatas, no diez pares
hechos a mano. `match_against: label` era correcto y por amplio margen — F1 **0,774** contra
0,135 de etiqueta+glosa y 0,080 de glosa sola. La conclusión sacada con n=10 se sostuvo; la
evidencia que la sostenía, no.

`use_cross_encoder` queda cerrado en **off**, medido dos veces: separación -0,56 sobre el
inventario completo y **-0,50** en la corrida con holdout, F1 0,127 contra 0,774. Negativa
quiere decir que el top-1 correcto puntúa *más bajo* que el equivocado (mediana 0,100 contra
0,258): no hay umbral que lo arregle, porque el que sube deja preferentemente los errores.

Lo que sigue abierto no es el interruptor sino la mejora que lo reemplazaría: un re-ranker
entrenado sobre los accept/reject acumulados de la zona gris (`ITER-TUNE`), que es un instrumento
distinto de un re-ranker de IR genérico. Ver la entrada de LoRA más abajo.


### DEBT-THRESHOLDS — Los umbrales — MEDIDOS, y el punto de operación cambia con cada par

Ya no son los defaults del spec. Sobre `craft-cl`, el 0,70 que traía aceptaba mal 3 de cada 10
menciones (precisión 72,3%); ahora `auto_merge` está en 0,95 (precisión 97,9%) y
`grey_zone_lower` en 0,80 (82,9%), elegido por debajo del pico de F1 para mandar la duda al
usuario en vez de descartarla como huérfana.

**Lo que sigue sin resolverse es transferirlos.** Se midieron contra un inventario de 3.418
clases y la semilla de aplicación tiene 34: con más candidatos hay más chances de que algo
espurio supere el umbral, así que el punto de operación se mueve con el tamaño y no se sabe
cuánto. Esa curva es exactamente la tarea «más pares» del plan —179, 3.419 y ~40k clases— y hasta
medirla, los valores de arriba son un punto de partida defendible, no un valor final.

**Las huérfanas genuinas ya no son cero.** El barrido con `--holdout 0.2` retiene 683 de las
3.418 clases; las 546 menciones que quedan sin clase en el inventario son huérfanas genuinas
con respuesta conocida. Dónde cae una de ellas, con los dos cortes configurados:

| destino | menciones | |
|---|---:|---|
| fusionada en silencio contra una clase equivocada (≥0,95) | 15 | 2,7% |
| zona gris 0,80–0,95: llega a revisión, recuperable | 398 | 72,9% |
| huérfana (<0,80): llega a inducción, que es su destino | 133 | 24,4% |

El error caro es el primero, y es marginal. Las dos zonas juntas atajan el 97,3% de los
conceptos que la ontología no tiene: el diseño de tres zonas hace lo que promete, y quien
decide bajar `grey_zone_lower` está eligiendo cuánta revisión hacer, no cuántos conceptos
nuevos perder.

Dos precauciones al leer esos números. El holdout achica el inventario a 2.736 clases, y con
menos distractores la precisión sube —99,2% en 0,95, 87,4% en 0,80 sobre las menciones que el
inventario sí cubría— así que los valores del inventario completo, que son los que quedaron en
`config/default.yaml`, siguen siendo el caso más duro. Y la tasa de falsos huérfanos casi no se
movió (18,0% contra 17,3% en 0,80): sólo 546 de 8.723 menciones cambiaron de lado, así que la
cifra anterior era del orden correcto por accidente, no porque el holdout no importara.


### DEBT-PATTERN-CATALOGUE — El catálogo de patrones de modelado tiene dos entradas

`branch` enumera los ejes de compromiso de modelado en vez de pedírselos a un modelo, que es lo
correcto y lo que el spec exige. El costo es que **un eje que el código no reconoce no se
ofrece**: hoy detecta `attribute_as_class` y `division_criterion`, y deja catalogado sin
detector `reify_vs_direct_property`. Ese último necesita extracción de propiedades, que el spec
pone fuera de v1, así que la deuda no es escribir el detector sino recordar que el catálogo es
el techo de lo que el sistema puede preguntar.

Dos parámetros nuevos sin calibrar. `min_group: 2` decide desde cuántas subclases un patrón es
una decisión y no una clase; se eligió por argumento, no por medición. Y
`min_criterion_separation: 0.10` es el único número del eje de criterio de división —el corte
en sí se elige por padre, precisamente para no tener un umbral de similitud más— pero ese 0,10
sale de seis criterios escritos a mano, donde el par que sí era un corte quedó en 0,38 y el
ruido cruzado en 0,25. Es la misma clase de evidencia insuficiente que esta sección existe para
marcar, y se mide igual que los umbrales del matcher: contra un par de calibración, viendo
cuántos ejes espurios aparecen.

Falta también lo que el spec pide después de elegir: **la regeneración del ABox no se dispara
sola** al aplicar una rama (`regenerate` existe y hay que correrlo a mano), y el `cq_delta` de
`SCHEMAS-BRANCH` queda en blanco porque las CQ no se re-corren contra el estado que la rama produciría.
Ninguna de las dos es difícil; las dos hacen que el puntaje de la rama sea menos informativo de
lo que el spec pretende.


### DEBT-GLOSS-SIGNALS — El enriquecimiento de glosas depende de un catálogo de señales, y del par

`enrich` encuentra los pasajes definitorios por patrón —doce señales enumeradas, en inglés y
español— y ese catálogo es el techo de lo que la etapa puede ver. Una definición escrita como
"llamamos X a…" o con la señal en una nota al pie no la encuentra. La mejora natural no es
agregar patrones de a uno sino medir el recall del filtro contra un corpus con definiciones
anotadas; hasta entonces, cuántos pasajes se pierden es desconocido, no cero.

Verificado sobre el corpus real: 1.000 bloques utilizables, y "open science" da 5 pasajes en 3
documentos con las señales `is a` y `means`. Sobre las 34 clases de la semilla da **cero**, que
es el desajuste temático de la `DEBT-QUALITATIVE-PAIR` y no una falla del filtro.

Falta lo que cierra el bucle: **`match` no se re-ejecuta solo sobre las huérfanas cuando las
glosas cambian**, como pide `PREP-NORMALIZE`. `enrich` avisa y deja el comando escrito, pero el disparo es
manual. Y el control de circularidad hoy sólo se consulta (`circular`); no descuenta esas
menciones de ninguna métrica de cobertura, que es para lo que el spec lo pide.


### DEBT-CONFLICT-ASSERTION-TYPES — Los conflictos fácticos ven un solo tipo de aserción

`conflicts` detecta el desacuerdo que este pipeline puede tener hoy: dos documentos que tipan a
la misma entidad a dos clases. Es el único porque el ABox sólo contiene tipos y procedencia —
**no hay extracción de propiedades**, así que "el documento 12 dice que X ocurrió en 2019 y el 47
dice 2021" no es representable y por lo tanto tampoco detectable. Cuando existan propiedades,
`detect` necesita una segunda familia de conflictos y `_settle` una política por propiedad, no
sólo por entidad.

Dos cosas que hoy quedan a mano:

- **La contextualización no está implementada.** Es la cuarta política de `ITER-CONFLICTS`, la única de nivel
  TBox, y es cara y global. `conflicts` reporta el patrón que la dispara y dice que pertenece a
  `branch`, pero el catálogo de `branch` no tiene todavía un eje `contextualize:<propiedad>` —
  ver `DEBT-PATTERN-CATALOGUE`. Es el enganche natural entre las dos etapas y está sin hacer.
- **`conflict_pattern_threshold: 3` no está calibrado.** Sale de un argumento, no de una medición,
  y como todos los demás umbrales del proyecto habría que verlo contra un par de calibración.

Y una limitación del instrumento: cuando el razonador no está disponible, la incompatibilidad se
calcula sólo con la disjointness asertada más su herencia, que es una **cota inferior**. Dos
clases pueden ser incompatibles por una combinación de restricciones que ningún `owl:disjointWith`
declara. El comando dice cuál de los dos tests usó, precisamente para no reportar la ausencia del
instrumento como la ausencia del hallazgo.


### DEBT-VALIDATION-CHAIN — La cadena `ITER-VALIDATE` está completa, y su eslabón flojo es de dónde salen las etiquetas

**OntoClean (filtro 4) está, y su punto débil es de dónde salen las etiquetas.** Las cuatro
restricciones son mecánicas y no tienen deuda; el insumo sí. Con ontología superior las
metapropiedades se heredan, y sin ella las etiqueta el LLM — que el propio spec reconoce como
"factible, menos confiable, y trabajo adicional que contradice parcialmente `BRANCH-ONLY-REVIEW`". Nadie ha medido
todavía cuán confiable es: haría falta un conjunto de clases con metapropiedades anotadas a mano
y comparar. Hasta entonces, un rechazo de este filtro es tan bueno como la etiqueta que lo
produjo, y por eso el comando reporta cuántas subsunciones verificó y cuántas salteó.

Dos consecuencias operativas. Las clases inducidas en una iteración **llegan sin etiquetar**, así
que sus subsunciones se saltean hasta que se vuelva a correr `metaproperties`; el disparo
automático tras `axiomatize` no está. Y las etiquetas viajan entre versiones a propósito, lo que
es correcto para un concepto estable y **equivocado si una clase cambia de significado**
conservando el IRI — un caso que hoy nada detecta.

**El filtro 5 no es OOPS!, es un subconjunto local.** Están implementados P06 (ciclos en la
jerarquía), P08 (clase sin etiqueta o sin definición), P11 (propiedad sin dominio o sin rango),
P19 (varios dominios, que OWL lee como intersección) y P24 (definición recursiva). Quedan afuera
los que necesitan juicio semántico —P02 sinónimos como clases, P03 subclase donde iba instancia,
P07 conceptos distintos en una clase, P13 inversas no declaradas, P30 equivalentes no
declaradas— y ésos son buena parte del valor del catálogo real. El scanner de verdad es un
servicio web: correrlo significa mandarle la ontología del usuario a un tercero, y esa es una
decisión suya, no un default. Si alguna vez se agrega, va detrás de un flag explícito.

**Las shapes de SHACL no existen todavía.** El filtro corre pero no hay ninguna escrita, así que
hoy siempre reporta SKIPPED. Escribir el primer juego —procedencia obligatoria, cardinalidad de
las etiquetas, individuos sin tipo— es trabajo pendiente y barato.

**Y hay que escribirlas sabiendo que SHACL es de mundo cerrado y OWL de mundo abierto.** No es
una contradicción del formalismo: es una contradicción de la *lectura*. `sh:minCount 1` sobre una
propiedad no dice «esta cosa no tiene valor», dice «este grafo no registra ninguno», que en
mundo abierto son afirmaciones distintas. Mientras el resultado se lea como lo segundo, no hay
conflicto.

De ahí sale la regla para elegir qué escribir:

- **Seguras — completitud sobre un artefacto que el pipeline controla.** Todo individuo lleva
  `derivedFromMention`; toda mención lleva documento y desplazamientos; una sola etiqueta
  preferida por idioma; ningún individuo sin tipo. Acá el mundo cerrado es *verdad*: el ABox lo
  escribió `regenerate` en esta corrida, y si algo falta es porque el pipeline no lo escribió, no
  porque el mundo no lo sepa. La shape está diagnosticando el generador.
- **Un error de categoría — verdades del dominio.** «Todo Experimento tiene un Resultado» como
  shape marca inválido cualquier experimento cuyo resultado el corpus no mencione, que es la
  mayoría. Eso en OWL es una restricción de cardinalidad y ahí *pertenece*: el razonador infiere
  que el resultado existe aunque no esté nombrado, que es exactamente lo contrario de lo que hace
  la shape.

Por eso el spec pone este filtro sobre el ABox y no sobre el TBox: sobre datos generados el
mundo cerrado es la suposición correcta; sobre la ontología no lo es. La regla práctica al
escribir una shape: si el remedio a una violación es *escribir mejor código*, va en SHACL; si el
remedio es *conseguir más texto*, no va.


### DEBT-FUNCTIONAL-CANDIDATES — Las propiedades funcionales están listas y no tienen qué mirar

La maquinaria de `ITER-APPLY` está: relevamiento con la distribución, exclusión de duplicados sin
resolver, pregunta al usuario, y `--declare` corriendo el razonador para mostrar qué se
fusionaría antes de commitear nada. **Lo que falta es el insumo**: el ABox tiene tipos y
procedencia, no propiedades de dominio, así que hoy el relevamiento devuelve vacío en el caso de
aplicación. Extraer propiedades es una etapa nueva de `ITER-EXTRACT` y el spec la deja fuera de v1; hasta que
exista, ésta es una etapa correcta sin trabajo que hacer.

Dos límites del relevamiento mismo, para cuando lo tenga:

- **`functional_min_individuals: 5` no está calibrado**, como todos los demás. El spec dice que
  "1 valor en 3 individuos" y "1 valor en 400" son decisiones opuestas, pero no dice dónde está
  el corte, y no hay forma de medirlo sin propiedades reales.
- **`--declare` mide el costo sobre el ABox de hoy**, que no es el argumento — la propiedad es o
  no es funcional en el dominio. Lo que muestra es lo que la equivocación costaría *acá*, que es
  útil y no es lo mismo. El comando lo dice, pero conviene tenerlo presente al leerlo.


### DEBT-NEXT-RUNS — `next` guía y ejecuta — RESUELTO

> **Resuelto el 2026-09-10.** `next --run` corre **una** etapa, la siguiente, y frena. Frente a
> un punto de decisión no corre nada y sale con error.
>
> La solución no fue el refactor que esta entrada daba por necesario: ejecuta el comando como
> **subproceso**, el mismo que imprimiría. Extraer los diez comandos de sus envoltorios de Typer
> era mucho trabajo a cambio de nada visible — el subproceso conserva la salida, el manejo de
> errores y el código de retorno tal cual—, y cuesta un par de segundos de arranque contra
> minutos de llamadas al modelo. Lo que se ejecuta se reconstruye desde el mismo texto que se le
> mostró al usuario, así que no pueden divergir.
>
> De paso se arregló algo peor: `next` **no corría sobre un almacén nuevo**, porque exigía una
> versión de ontología que todavía no existe. El comando que dice qué hacer primero fallaba
> justo cuando no se había hecho nada.

<details>
<summary>El diagnóstico original</summary>

### `next` guía pero no ejecuta

El orquestador contesta qué corresponde hacer y se detiene donde hace falta una persona, que es
la parte difícil y la que importa. Lo que no hace es **correr la etapa por vos**, y no por
diseño sino por una razón mecánica: los comandos de etapa viven dentro de sus wrappers de Typer,
así que llamarlos desde Python pasa objetos `OptionInfo` en lugar de valores. Para tener `--run`
hay que extraer primero cada comando en (wrapper delgado + función común), que son unos diez
comandos.

Se dejó sin hacer en vez de resolverlo a medias porque un runner que se saltea un punto de
decisión es peor que no tener runner: los cinco puntos donde decide el usuario son justamente
donde el sistema no debe elegir solo. Cuando se haga, `--run` tiene que avanzar **de a una
etapa** y frenar en el primer `waiting on you`.

</details>

Dos cosas que `next` todavía no mira: si el `rules_hash` cambió desde la última regeneración (hoy
siempre sugiere `regenerate`, que es conservador pero ruidoso) y si las glosas cambiaron desde el
último `match`, que es lo que cierra el bucle de `PREP-NORMALIZE`.


### DEBT-CQ-CORPUS-BIAS — Las CQ generadas heredan el sesgo del corpus, y eso no se arregla acá

Es la advertencia del propio spec y conviene tenerla escrita como deuda y no sólo como nota: las
CQ de `PREP-CQ-GENERATED` miden completitud **respecto al corpus**. Si el corpus no habla de algo, no va a haber
una CQ que lo pida, y la tasa de aprobación va a subir sin que la ontología mejore en el dominio.
La mitigación es `PREP-CQ-USER` —las CQ que el usuario escribe sin mirar las generadas, 20–30% del total— y
hoy **no hay ninguna escrita**: `examples/competency_questions.json` tiene cinco de ejemplo. Sin
ese 20–30%, el criterio de parada primario está midiendo el corpus contra sí mismo.

**La cita se verifica que exista, no que sostenga.** El filtro comprueba que el número de pasaje
citado sea uno de los que se le mostraron al modelo, y eso descarta las citas inventadas — pero
no que el pasaje diga algo que justifique la pregunta. Observado en la primera corrida real: una
pregunta inferencial correcta sobre `Interview ⊑ Technique ⊑ Methodological Strategy` citando un
pasaje que anuncia las secciones del paper. La pregunta sirve; la cita no la sostiene. Verificar
eso mecánicamente no es obvio —haría falta algo como el chequeo de solapamiento léxico entre
pregunta y pasaje, con su propio umbral sin calibrar— así que por ahora es carga de la revisión
humana del paso 4, y conviene que quien revise lo sepa.

Dos huecos concretos en la etapa:

- **La regeneración de consultas bajo reorganización (`SEED-REORGANIZABLE`) no está.** El spec elige "regenerar la
  consulta cuando cambian las clases involucradas", y `cq.sparql_regeneration: on_class_change`
  está en el config sin nada que lo lea. Hoy una CQ cuya clase se dividió en una iteración pasa a
  fallar por una razón que no es la que el criterio quiere medir.
- **La deduplicación cae a texto normalizado sin encoder.** Dos preguntas que difieren en una
  palabra sobreviven, lo que es costo de revisión y no un criterio de parada equivocado — pero
  conviene saberlo antes de leer 60 candidatas.


### DEBT-MATCHER-TUNING — Ajuste del matcher (`ITER-TUNE`) — HECHO, con un resultado que cambia el default

> **Resuelto el 2026-09-10.** El comando es `tune`. Ajustado con las anotaciones del propio par
> da **+9,9 puntos** en CRAFT y **+11,6** en MaterioMiner sobre documentos no vistos — la mejora
> más grande que se midió acá— y **sólo sirve en su propio dominio**: el de CRAFT aplicado a MaterioMiner
> resta 2,1 puntos. Ver [`HALLAZGOS.md`](HALLAZGOS.md) 1.12.
>
> **Ajuste completo en vez de LoRA**, apartándose de la letra del spec: LoRA existe para no tocar
> todos los pesos de un modelo grande, y éste tiene 33 millones de parámetros y entrena en 79
> segundos. Agregar `peft` para evitar un costo que no existe sería complejidad sin
> contrapartida; la sustancia es la misma.
>
> Lo que queda abierto es el circuito que el spec describe: hoy las etiquetas salen de un par
> anotado, y la idea era que salieran solas de las decisiones de zona gris del usuario.
> `grey labels --export` ya escribe ese formato y hay **cero** respuestas acumuladas, así que esa
> mitad sigue esperando a que alguien conteste.

<details>
<summary>Lo que decía antes de medirlo</summary>

### LoRA (`ITER-TUNE`) — la única pieza del plan bloqueada por falta de datos, no de código

El spec pone el ajuste del matcher como el arreglo de la compuerta no-go: si la tasa de falsos
huérfanos es alta, mejor modelo, mejores glosas, **LoRA con las primeras etiquetas**. Las
primeras dos ya se probaron —las glosas empeoraron el matching y el encoder es el que hay— así
que queda la tercera, y es la única del plan que no se puede escribir todavía.

Lo que falta es el insumo, y ahora se sabe exactamente cuál: las respuestas de zona gris que
`grey answer` acumula. Hoy hay **una**. `grey labels --export` ya las escribe en el formato que
un entrenamiento necesita (mención, clase ofrecida, puntaje, si se aceptó), así que la
infraestructura de datos está; falta que alguien conteste unos cientos de pares.

**No escribir el entrenador antes de tener con qué probarlo.** Un script de fine-tuning que
nunca corrió sobre datos reales es código que parece listo y no lo está, y el proyecto ya
documenta esa clase de falla (la edición silenciosa de la entrada de coordinación). El orden es:
contestar zona gris → exportar → medir el cross-encoder tuneado contra el barrido de `calibrate`
→ recién ahí decidir si `use_cross_encoder` vuelve a `true`.

Cuánto hace falta es desconocido, y el techo disponible es más bajo de lo que parecía: contra
`v5`, que es la versión vigente, hay **131 pares** esperando respuesta, no los 219 de `v2` —esa
versión quedó tipada contra una capa de menciones que después se volvió a extraer—. Un re-ranker
entrenado con ciento y pico de ejemplos es una apuesta, no una medición.

</details>

**La fuente de etiquetas que no requiere trabajo humano, que resultó ser la buena:** el par de
calibración trae 8.723 menciones gold. Entrenar el re-ranker ahí y evaluarlo sobre el holdout es
medible hoy mismo, sin que nadie conteste nada. Lo que no dice es cuánto **transfiere** a otro
dominio, y con el entregable siendo la caracterización del sistema esa pregunta deja de ser una
salvedad y pasa a ser parte del resultado: entrenar en un par y evaluar en otro es justamente lo
que hay que medir. Los pares de la tarea «más pares» son el banco para eso.


### DEBT-QUALITATIVE-PAIR — El par cualitativo, retirado — y lo que sí dejó

La semilla de metodología cualitativa contra el corpus de política de ciencia abierta **está
fuera de circulación** desde el 2026-09-09. No es un caso de aplicación al que haya que volver:
el proyecto no tiene dominio comprometido —el entregable es el sistema y su caracterización a
través de pares— y esa dupla fue el andamio para tener con qué probar mientras no existía un par
anotado. Los pares publicados lo reemplazan por completo.

Lo que dejó, y que sigue valiendo porque es sobre el método y no sobre el par:

- **El desajuste temático es medible, y el comando `alignment` lo hace** — pero sólo cuando se
  le nombra el vocabulario. Sobre 495.213 caracteres, `field note`, `informant`, `coding scheme`,
  `thematic analysis` y `grounded theory` aparecen **cero veces**: 0 de 5, contra 5 de 5 sobre
  MaterioMiner. Ésa es la parte que decide.

  Lo que **no** funciona, medido: la cobertura global —qué fracción de las etiquetas de la
  ontología aparece— no sirve de veredicto. Daba **20% sobre MaterioMiner**, un par real anotado
  por expertos, y **50% sobre el par roto**. Es estructural: una ontología publicada cubre un
  dominio entero y un corpus cubre una franja, así que la mayoría de las clases no tiene por qué
  aparecer. Restringir a etiquetas multipalabra tampoco separa (9% contra 18%). Queda como
  diagnóstico y el comando lo dice.

  Lo que faltaría para decidir sin que nadie nombre términos es mirar del lado de las
  **menciones** —¿lo que el corpus nombra tiene clase?— y eso pide anotaciones o el matcher, que
  es justo lo que este chequeo quería evitar.
- **El eco léxico se hace visible cuando el par está desalineado**, y por eso este par sirvió:
  las 18 automáticas de `v5` son casi todas la palabra corriente que da nombre a la clase. Ver
  la `DEBT-CONTEXT-DISAMBIGUATION`.

Los datos derivados (`data/`, versiones `v0`–`v5`) quedan como están: son historia, y los
números que se citaron de ahí están fechados en [`HALLAZGOS.md`](HALLAZGOS.md).

---

## Alcance pendiente del spec

### DEBT-INTERACTIVE-SESSION — Sesión interactiva

El spec tiene cinco puntos donde decide el usuario —elegir rama (`ITER-BRANCH`), zona gris del matcher
(`ITER-MATCH`), propiedad funcional (`ITER-APPLY`), validación de CQ (`PREP-CQ-GENERATED`), revisión de erratas (`PREP-NORMALIZE`)— y los
cinco tienen ahora por dónde contestarse: `branch --choose`, `grey answer`, `functional
--declare`, `cq`, `review resolve`. Las decisiones sobreviven a re-correr en los cinco casos.

Lo que falta es **una interfaz encima**, no la maquinaria. Contestar 219 pares de zona gris de a
uno por CLI es correcto y es tedioso; el anotador de navegador del conjunto de retención ya
demuestra que la forma existe, y aplicarla acá es trabajo conocido. Y falta que `next` pueda
ejecutar la etapa siguiente además de nombrarla — ver `DEBT-NEXT-RUNS`.

Cuando exista, `review_items` también es donde viven las excepciones por caso de las reglas de
mapeo — ver [`plan_reglas_de_mapeo.md`](plan_reglas_de_mapeo.md).

### DEBT-OPEN-WORLD — Mundo abierto: lo que falta

- **Propiedades funcionales (`ITER-APPLY`)**: la etapa está (`functional`) y no tiene propiedades que
  mirar — ver `DEBT-FUNCTIONAL-CANDIDATES`. Lo que sí quedó resuelto es hacer visible el riesgo silencioso: `--declare`
  corre el razonador y muestra qué individuos se fusionarían antes de commitear nada.
- **`NegativePropertyAssertion` (`ITER-CONFLICTS`)**: `refuted` ya se escribe (`mark --mark refuted`) y saca
  la aserción del ABox, que es lo que el spec pide. Lo que sigue sin existir es la aserción
  negativa explícita, y con razón: bajo OWA sólo corresponde cuando **se sabe** que algo es
  falso, no cuando hay duda, y no hay propiedades donde ponerla todavía.
- **CQ negativas**: `cq_a4_05` en los ejemplos pregunta por completitud del grafo con
  `FILTER NOT EXISTS`, que es mundo cerrado. Sirve como diagnóstico del artefacto, pero `PREP-CQ-GENERATED`
  define el tipo negativo como "mundo abierto explícito". Mal precedente para quien escriba CQ
  nuevas.

### DEBT-VLM-ROUTE — Ruta VLM

Páginas `scan`/`uncertain` quedan sin parsear, las figuras sin captioning y las fórmulas sin
extraer. El pipeline lo registra en vez de fingir que las procesó.

### DEBT-BORDERLESS-TABLES — Tablas sin bordes

`find_tables` solo ve tablas con líneas; la estrategia por texto devuelve la página entera como
tabla. El hueco se **mide** —columna "table gap"— pero no se cubre. La respuesta del spec es
rutear esas páginas a MinerU.

### DEBT-LANGUAGE-HARDCODED — `language.py` está cableado a es/en

Los marcadores de palabras función están hardcodeados. Otro idioma son ~10 líneas más, o
apoyarse en el `/Lang` declarado del PDF.

### DEBT-CODE-WITHOUT-CALLER — Código sin consumidor

Tres cosas implementadas y probadas que nada invoca. No están rotas: están desconectadas, y
cada una es o bien un cable que falta o bien código a borrar.

- **`versioning.nearest_state`** — detección de loops *parciales* (`ITER-APPLY`): la rama vuelve *casi*
  al estado anterior, mismo compromiso de modelado con IRIs distintos, y el hash exacto no lo ve.
  Ahora sí hay dónde enchufarlo: `branch` computa el hash de cada rama y avisa cuando es un
  retorno exacto, pero usa `find_by_hash` y no esto. Falta el umbral en el config y una línea en
  el llamador.
- **`iteration.trigger | batch_size`** — cuándo se dispara una iteración y de a cuántos
  documentos. Describen un loop automático que sigue sin existir: `next` dice qué corresponde y
  las etapas se corren a mano, una por comando (ver `DEBT-NEXT-RUNS`). `max_iterations` **sí** se consume
  ahora, como criterio duro de `stop`.
- **`llm.iter_branch`** — `branch` no llama a ningún modelo, y no puede: la única prohibición
  explícita del spec para esa etapa es pedirle alternativas a un LLM. La clave quedó de cuando
  se pensaba que haría falta. Es candidata a borrar, no a cablear.
- **`cq.sparql_regeneration`** — la política `SEED-REORGANIZABLE` de regenerar consultas cuando cambian las clases
  involucradas. Nada la lee todavía; ver `DEBT-CQ-CORPUS-BIAS`.

Lo que hay que evitar es que crezcan en silencio: una clave de config que nadie lee afirma algo
falso sobre lo que el sistema hace. `matching.blocking_strategy` fue el caso —decía `embedding`
y bloqueaba por prefijo de 4 caracteres— y se resolvió haciendo que el config **rechace** un
valor no implementado en vez de aceptarlo. Ese es el patrón para las que quedan.

### DEBT-MULTI-BRANCH-QUESTIONS — Multi-rama: las preguntas que el spec deja abiertas

`ITER-BRANCH` ya está implementado (`branch`). Lo que va acá es distinto: el diseño multi-rama tiene
preguntas que **el spec mismo declara sin resolver**, y siguen sin resolverse — implementar la
etapa no las contesta, sólo las vuelve alcanzables.

- **Expiración de rechazos (`ITER-FEEDBACK`, riesgo `RISKS-REJECTION-EXPIRY`).** Con semilla reorganizable un rechazo no es
  permanente: lo rechazado en la iteración 3 puede ser correcto en la 9 porque la estructura
  cambió. Bloquearlo para siempre acorrala el proceso; no bloquearlo produce un loop. La
  política elegida —registrar el rechazo relativo al estado de la ontología y expirarlo cuando
  las clases involucradas se reorganizan— está marcada textualmente como **"no es una regla
  limpia, requiere ajuste empírico"**. Es la deuda más profunda del aparato y no se resuelve
  leyendo: se resuelve con iteraciones reales encima.
- **Comparación por forma normal (`ITER-FEEDBACK`).** Para detectar re-proposición hay que normalizar el
  axioma antes de comparar, o el mismo compromiso vuelve con IRIs distintos y no se detecta.
  La pieza existe —`versioning.logical_axioms` canonicaliza— pero no está conectada a la tabla
  `decisions`, que es donde vive el historial de rechazos.
- **Scoring de ramas en frío (`COLDSTART`).** `historical_affinity` y `parsimonia` requieren historial,
  y el spec dice explícitamente que se **omitan** en las primeras iteraciones en vez de
  calcularse con datos insuficientes. O sea: el scoring nace incompleto por diseño y hay que
  implementarlo sabiéndolo.
- **Techo de 3–5 ramas (`ITER-BRANCH`).** Es un número puesto a dedo contra una explosión de 2^k. El
  mecanismo real que lo evita es presentar los ejes independientes por separado y armar ramas
  completas sólo cuando están acoplados; el techo es la red, no la solución. **Implementado
  así**: `couple()` agrupa por axiomas compartidos y `max_branches` es sólo el corte final.
- **El umbral de `nearest_state`.** Lo que menciona el punto 15 como cable faltante tiene además
  un parámetro sin calibrar: cuánta distancia de Jaccard cuenta como "casi el mismo estado". No
  hay forma de fijarlo sin iteraciones reales, igual que la expiración de rechazos.

Todo esto comparte una propiedad incómoda: **no se puede calibrar contra un corpus externo**,
como sí se puede el matcher. Depende del historial de decisiones de este proyecto en particular,
que hoy tiene cero entradas.

### DEBT-HIERARCHY-AWARE-TYPING — Tipado consciente de la jerarquía

Hoy el matcher rankea cada mención contra las clases como si fueran independientes: no sabe que
`Interview ⊑ Technique`. Usar la estructura —preferir la clase más específica cuyos ancestros
también puntúan, penalizar una cuyos hermanos puntúan idéntico— es el mecanismo natural contra
el eco léxico, que es el modo de falla que ningún umbral filtra (punto 7).

No se puede medir sobre la semilla actual: 34 clases, profundidad 3, seis raíces. Sí sobre un
par de calibración con jerarquía profunda, donde entra como una variable más del barrido de
umbrales.

### DEBT-CROSS-LANGUAGE-GREY — `cross_language_always_grey` nunca se midió

`matching.cross_language_always_grey: true` y la elección de un bi-encoder multilingüe son
decisiones de config sin una sola medición detrás. El razonamiento declarado —un encoder
monolingüe empujaría todo par es/en a la zona gris por idioma solo— es plausible y nunca se
verificó, y la regla que lo acompaña es fuerte: manda a revisión humana *todo* par en idiomas
distintos, sin importar el score.

**Ningún par de calibración disponible la toca**, porque todos son en inglés. La única vía
encontrada son los corpus clínicos del BSC —**SympTEMIST**, **DisTEMIST**, **MedProcNER**: 1.000
casos clínicos en español cada uno, anotados y normalizados a SNOMED CT, en standoff BRAT, que
`calibration.read_brat` ya lee—. SNOMED CT es lo más axiomatizado disponible (EL++, definiciones
lógicas en casi todo el vocabulario) y **Argentina es país miembro de SNOMED International**, con
lo cual la Affiliate License es gratuita.

Lo que cuesta: la licencia hay que tramitarla, y SNOMED son ~360k conceptos, así que hay que
subsetear y documentar el criterio como pide la fase 0 del plan. Por eso está acá y no en la
tabla de tareas: no está en el camino crítico de la calibración, y no conviene que bloquee las tareas de calibración.

Mientras tanto el default se queda como está. Lo honesto es que se queda por falta de evidencia
en contra, no por evidencia a favor.

### DEBT-CONTEXT-DISAMBIGUATION — Desambiguación por contexto: el agujero que el eco léxico deja abierto

Salió analizando el caso del homónimo —`cell` de biología contra `cell` de una organización
clandestina—. Conviene separar dónde **no** está el problema, porque la intuición apunta al
lugar equivocado:

- **No está en cómo se acuñan los IRIs.** El `uuid5` se computa sobre el **id de la mención**,
  que es único por ocurrencia, no sobre la forma superficial: verificado sobre la base,
  `researchers` aparece 12 veces y tiene 12 ids distintos. Nunca se computa `uuid5("cell")`.
- **No está en la resolución de entidades.** Dos menciones homónimas terminan en el mismo
  individuo sólo si algo decide fusionarlas, y las reglas ya cubren el caso: nombre propio
  idéntico fusiona **sólo si además tipan a la misma clase**, y si no, la decisión es
  `identical_name_different_class` y va a zona gris. Un sintagma genérico de una palabra en
  minúscula se separa sin preguntar. El homónimo genérico ni siquiera llega a evaluarse.

**Está en el tipado.** Nada impide que `cell` en sentido de célula clandestina tipe a la clase
`Cell` de biología con coseno alto: es eco léxico puro, es el modo de falla que el barrido midió
—11 de 24 clases sobre umbral en la semilla, y las 32 automáticas del corpus real— y ningún
umbral lo filtra, porque la palabra coincide con el nombre de la clase y **el contexto no entra
en la comparación**.

Dos caminos, con costo distinto y ambos medibles sobre el banco que ya existe:

1. ~~**Meter contexto en la comparación.**~~ **Probado y descartado el 2026-09-10.** Se midió en
   cuatro formas sobre MaterioMiner (n=2.229) y en dos sobre CRAFT (n=8.723): concatenar la
   oración hunde @1 de 25,3% a 11,3% —y en CRAFT de 69,8% a **14,3%**—, una ventana angosta da
   7,4%, y la fusión de puntajes, que es la única que no rompe la forma del sintagma, aporta
   +0,4 puntos en @1 y pierde uno en @5. Reproducible con `calibrate --context sentence`.
   Es el mismo hallazgo que el de las glosas: un encoder simétrico compara por forma, y
   agregarle una oración a un sintagma lo convierte en una oración. **La solución no está en la
   representación de la mención, está en el encoder** — ver [`HALLAZGOS.md`](HALLAZGOS.md) 1.11.
2. **Usar la jerarquía**, que es la `DEBT-HIERARCHY-AWARE-TYPING`: preferir la clase cuyos ancestros también
   puntúan. Un `cell` biológico debería activar también `Anatomical Structure`; uno clandestino,
   nada del subárbol.

Lo que **no** arregla nada es tocar el esquema de IRIs. La identidad no es el problema; la
desambiguación sí.

### DEBT-FEEDBACK-HISTORY — El historial de feedback (`ITER-FEEDBACK`) — RESUELTO

> **Resuelto el 2026-09-10.** El registro y su destino, que era lo que lo justificaba.
>
> **El registro.** `decisions` se escribe: una fila por (rama, eje) con las seis categorías
> fijas, el estado `invalid` separado de `rejected`, el comentario y el hash del estado contra el
> que se decidió. `branches` sigue siendo la cola de propuestas —su trabajo— y dejó de ser el
> registro.
>
> **La forma normal.** `axiomatization.normal_form` nombra cada compromiso por etiquetas y no por
> IRIs, y saltea glosa, nota de alcance y etiquetas alternativas. El mismo compromiso vuelto a
> proponer con otros IRIs y otra redacción da la misma cadena; cambiarle el padre o el nombre la
> cambia. `settle(normal_forms=…)` la guarda en `decisions.normalized_axioms`, que antes tenía
> ids de axioma —inservibles para comparar entre iteraciones—.
>
> **Los precedentes en el prompt.** `axiomatize` calcula la forma de cada propuesta, consulta
> `branching.already_rejected` y `branching.precedents_like`, y el prompt (v2) lleva una sección
> «WHAT WAS DECIDED BEFORE» con veredicto y comentario. El comentario es lo que se transfiere: el
> veredicto dice qué pasó, el comentario dice por qué, y sólo el porqué aplica a una propuesta
> distinta. La última línea de esa sección es explícita: son precedentes, no reglas.
>
> Medido sobre MaterioMiner v1: 45 propuestas juzgadas, 0 ya descartadas antes —lo esperable en
> la primera iteración, y la prueba de que la consulta corre—. Falta la segunda iteración con
> feedback humano real para ver si los precedentes mueven algún juicio.

<details>
<summary>El diagnóstico original</summary>

`branch --choose` graba qué rama se eligió y marca rechazadas a sus hermanas, que es la mitad que
importa —lo aceptado ya está en la ontología, lo rechazado no está en ningún otro lado—. Lo que
falta es todo lo que el spec quiere hacer **con** ese registro.

**El esquema `GRADED-FEEDBACK` existe y nadie lo escribe.** La tabla `decisions` está en `db.py` con exactamente
los campos que pide `ITER-FEEDBACK` —`status`, `axis`, `comment`, `normalized_axioms`, `ontology_state`— y
**cero filas**: `branching` guarda su propia versión más pobre en `branches.status` y
`branches.note`. Son dos lugares para lo mismo, y el que se usa es el que menos guarda:

- `status` en `branches` es `chosen`/`rejected`; `GRADED-FEEDBACK` distingue además **`invalid`**, y esa
  distinción es el punto — separa la señal fuerte ("esto está mal") del rechazo blando ("elegí
  otra"), que colapsadas se pierden.
- `axis` en `branches` es el id del eje detectado (`attribute_as_class:6c6b32…`); `GRADED-FEEDBACK` quiere una de
  **seis categorías fijas** (`granularity`, `division_criterion`, `property_vs_class`,
  `directionality`, `scope`, `terminology`), que es lo que hace comparables dos decisiones de
  iteraciones distintas.
- `comment` sí está, y el spec dice que **es el campo que más rinde**.

**Falta el destino del feedback, que es lo que lo justifica.** `ITER-FEEDBACK` es explícito en que con
decenas o pocos cientos de decisiones no se ajusta un modelo: se hace **recuperación de ejemplos
en contexto** —ante una propuesta nueva, traer las 3–5 decisiones históricas más parecidas por
embedding e inyectarlas en el prompt con el comentario del usuario—. Nada de eso existe. Hoy
`branching.history()` cuenta (eje, opción) y alimenta `historical_affinity`, que es un número; el
spec quiere los precedentes, que además son **inspeccionables**: ante una propuesta rara se puede
ver qué casos usó.

**Y falta conectar la forma normal.** `versioning.logical_axioms` ya canonicaliza, pero no está
enchufado a `decisions`, así que el mismo compromiso vuelve con IRIs distintos y no se detecta
como re-proposición. Ver también la `DEBT-MULTI-BRANCH-QUESTIONS`, que reúne las preguntas que el spec deja abiertas
sobre este mismo aparato.

Orden razonable si se retoma: ~~unificar en `decisions`~~ → ~~mapear el eje a las seis
categorías~~ → ~~recuperación por embedding~~ → ~~forma normal~~. Los cuatro están hechos.

</details>

### DEBT-STATE-HASH — El hash de estado no escalaba a una ontología con axiomas de verdad — RESUELTO

> **Resuelto el 2026-09-09.** `logical_axioms` etiqueta los nodos en blanco por su estructura en
> vez de canonicalizar el grafo entero: `cl-base.owl` pasó de **no terminar en 7 minutos** a
> **1,96 s**, con los 23.162 nodos anónimos etiquetados y el hash estable frente a un
> re-parseo que les cambia el identificador a todos. Queda el registro de qué era, porque el
> diagnóstico vale para la próxima etapa que se tope con lo mismo.

Descubierto intentando usar la Cell Ontology como semilla.

`versioning.logical_axioms` canonicaliza los nodos en blanco con `to_canonical_graph` de rdflib
antes de hashear. Sobre la semilla de 34 clases es instantáneo. Sobre `cl-base.owl` —**123.864
tripletas, de las cuales el 73,2% involucra un nodo en blanco**, porque así se representan las
restricciones OWL y las anotaciones de axioma— **no termina en 7 minutos**. Ordenar las mismas
tripletas sin canonicalizar tarda **0,21 s**.

Y `normalize-seed` lo llama **tres veces**: dos para el hash de estado y una más en el diff.

**No alcanza con sacarlo.** La canonicalización está por una razón: dos grafos que difieren sólo
en los identificadores de sus nodos en blanco son el mismo estado, y de ese hash depende la
detección de loops del `ITER-APPLY`. Sin ella, re-serializar la misma ontología produciría un estado
"nuevo" y el DAG se llenaría de versiones que no cambian nada.

La salida es canonicalizar sólo lo que lo necesita. En una ontología OWL casi todo nodo en blanco
es **estructural**: una restricción cuelga de exactamente una clase nombrada, así que etiquetarlo
determinísticamente por su camino desde el nodo nombrado más cercano cubre la enorme mayoría, y
la canonicalización completa queda para el caso raro de nodos en blanco genuinamente cíclicos.
Hay que medir cuántos son de cada tipo antes de escribirlo.

**Cómo quedó.** Un hash de Merkle en las dos direcciones, iterado hasta punto fijo: cada nodo
anónimo se describe por las aristas que lo alcanzan desde algo ya identificado y por las que
salen hacia algo ya identificado. Hacen falta las dos direcciones —una restricción entra por
arriba, una reificación de axioma sólo por abajo—; con una sola quedaban 7.965 sin resolver. El
algoritmo general de rdflib sigue ahí como red, para el caso de nodos anónimos que no cuelgan de
ningún nombrado; sobre las ontologías probadas nunca se activa.

**Una trampa que costó encontrar y quedó fijada con test:** la primera versión leía las
etiquetas mientras las asignaba, así que el resultado dependía del orden de iteración sobre un
conjunto — o sea, de los identificadores que rdflib repartió al parsear, que es exactamente lo
único que había que neutralizar. El síntoma era que re-parsear la misma ontología daba otro
hash. La actualización tiene que ser **sincrónica**: todo lo de una vuelta se calcula contra las
etiquetas de la anterior.

### DEBT-ONTOLOGY-IMPORTS — Ontologías que importan otras ontologías

Una ontología publicada casi nunca viene sola: declara `owl:imports` hacia otras por IRI, y ese
IRI puede no responder. La Materials Mechanics Ontology importa `https://w3id.org/pmd/co/2.0.4`,
que hoy no contesta.

**Lo que hay:** la carga ya no aborta —antes, un import roto dejaba al pipeline entero sin
razonador— y se reporta cuáles faltaron, porque un veredicto calculado sin los axiomas de una
importada vale sobre menos de lo que la ontología declara. Degradar en silencio es el modo de
falla que este proyecto encontró tres veces y no conviene sumar la cuarta.

**Lo que falta, en orden de utilidad:**

1. **Que el usuario pueda resolver el import a mano.** Es lo más útil y lo más barato: quien
   tiene el archivo lo pone en un directorio y el sistema lo usa en vez de salir a la red. La
   OWL API tiene el mecanismo —un `OWLOntologyIRIMapper`, que traduce IRI a archivo local— así
   que es cablear, no inventar. Configurable como `paths.ontology_cache`, con un mensaje que
   diga exactamente qué IRI falta y dónde dejar el archivo.
2. **Caché local de lo que sí resolvió.** Hoy cada carga sale a la red por cada import que
   funciona, lo que hace lenta y frágil una etapa que no tiene por qué depender de internet.
   Bajarlo una vez y quedárselo es lo que hace `scripts/fetch-jars.sh` con los jars.
3. **Decir qué se perdió, no sólo qué faltó.** Un import roto no es igual de grave según lo que
   traía: si aportaba las clases de nivel superior, la jerarquía queda sin techo y OntoClean y
   las métricas estructurales miden otra cosa. Contar cuántas referencias del grafo quedan sin
   resolver da la magnitud, que es distinto del nombre del archivo que faltó.

Mientras tanto conviene leer los veredictos del razonador sobre una ontología con imports rotos
sabiendo que son sobre menos axiomas. `validate` lo dice arriba de la tabla.

### DEBT-ACADEMIC-METALANGUAGE — La extracción confunde el metalenguaje académico con el dominio — BAJO ESFUERZO, ALTA PRIORIDAD

De 45 clases inducidas sobre MaterioMiner, tres salieron de vocabulario sobre el paper y no
sobre la mecánica de materiales:

| Clase propuesta | De qué menciones salió |
|---|---|
| `Scholarly research` | `Previous studies`, `literature`, `research`, `researchers`, `publication` |
| `Table reference` | `Table 1`, `Table S1`, `Table 2` |
| `Results` | `results`, `corrected results` |

**No son encabezados de sección.** Verificado: `Abstract`, `Introduction`, `Conclusions` y
`References` aparecen **cero veces** entre las 1.309 menciones, así que la extracción no confunde
la estructura del documento con su contenido. Confunde el metalenguaje de escribir un paper con
el dominio del que el paper habla, que es más difícil de atajar.

**Por qué no lo atrapa nada de lo que hay.** No son falsos huérfanos —la ontología hace bien en
no tener `Table reference`—, así que el chequeo de redundancia no los ve. Son sintagmas
nominales legítimos con soporte suficiente, así que `min_support` tampoco. Y el razonador no
tiene nada que objetarle a una clase nueva sin padre. Pasan los siete filtros.

**Tres vías, de más barata a más cara:**

1. **Una lista de bloqueo de metalenguaje**, aplicada a la mención antes de agrupar: `table`,
   `figure`, `section`, `study`, `studies`, `literature`, `paper`, `results`, `reference`. Barato
   y frágil: `results` es basura en un paper de materiales y podría no serlo en uno de
   metodología, así que la lista tiene que ser configurable por par y no una constante del
   código.
2. **Decírselo al prompt de extracción.** Hoy pide sintagmas que denoten conceptos; agregar que
   el metalenguaje de la publicación no es el dominio es una línea. Más general que la lista y
   sin garantía: es una instrucción, no un filtro.
3. **Filtrar por distribución.** Un término del metalenguaje aparece parejo en todos los
   documentos de cualquier dominio; uno del dominio se concentra. Es el criterio más
   principiado y el que más cuesta, y necesita más de cuatro documentos para tener señal.

Empezar por la 2, medir sobre el mismo par, y recién ahí decidir si hace falta la 1.

### DEBT-SIZE-CURVE-THIRD-POINT — El tercer punto de la curva de tamaño — BAJO ESFUERZO, ALTA PRIORIDAD

Hay dos puntos medidos: 428 clases (MaterioMiner) y 3.418 (CRAFT/CL), y el comportamiento cambia
tanto entre ellos —recall@1 de 21,5% contra 68,5%— que dos puntos no dan una forma, dan una
recta imaginaria entre dos observaciones.

El tercero es un inventario grande: **CafeteriaFCD/CafeteriaSA contra FoodOn**, ~40k clases, que
la tarea «más pares» ya identifica. El lector `brat` está escrito, así que el trabajo es bajar el
corpus, escribir un `pair.yml` y correr el barrido. Es la tarea de mejor relación entre lo que
cuesta y lo que responde, porque **caracterizar el sistema a través de pares es el entregable**.

Ojo con una cosa antes de correrlo: con ~40k clases el hash de estado y la carga de la ontología
entran en un régimen que no se probó. El hash ya está arreglado (`DEBT-STATE-HASH`), pero 40k clases es
diez veces `cl-base.owl`.

### DEBT-AUTOMATIC-TRIGGERS — Disparos automáticos que hoy hay que recordar — BAJO ESFUERZO, ALTA PRIORIDAD

Tres lugares donde el pipeline sabe que algo quedó viejo y no hace nada:

- **`regenerate` después de aplicar una rama.** El ABox se deriva de las menciones y de una
  versión de la TBox; al cambiar la TBox queda viejo. `versioning.record_rules` ya sabe si algo
  cambió, así que es cablear el aviso o el disparo.
- **`metaproperties` después de inducir clases nuevas.** Las clases nuevas llegan sin etiquetar,
  así que OntoClean saltea sus subsunciones — que son justamente las que acaba de proponer el
  sistema y las que más conviene revisar.
- **`match` después de cambiar las glosas.** Es el que cierra el bucle autocorrectivo de `PREP-NORMALIZE`:
  mejor glosa, mejor matching, menos falsos huérfanos. Hoy `enrich` deja escrito el comando y
  nadie lo corre.

Los tres son la misma forma —comparar un hash contra el que quedó registrado— y `next` es el
lugar natural: ya reporta estado por etapa, y le faltan estas tres comparaciones para dejar de
sugerir `regenerate` siempre y empezar a sugerirlo cuando corresponde.
