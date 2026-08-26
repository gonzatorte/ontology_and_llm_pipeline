# Deuda técnica y mejoras

Documento vivo. Registra **mejoras a futuro** —cosas que hoy funcionan pero podrían estar
mejor, y decisiones tomadas con evidencia insuficiente que conviene rehacer cuando la haya—.
No es una lista de bugs: lo que está roto se arregla, no se documenta.

> **Al agregar una entrada:** los números colisionan cuando dos sesiones escriben en paralelo —
> ya pasó con el 17—. Antes de numerar, mirá el último `###` que hay. Y al referenciar otra
> entrada, mejor por título que por número, porque los números se corren.

El estado de implementación por etapa está en el [README](README.md); acá va lo que no se ve
mirando la tabla de estado.

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

---

## Mejoras estructurales

### 1. Capa de acceso a datos

Hoy el SQL crudo está repartido en 8 módulos y `sqlite3` se importa en cada uno. Funciona, pero
tiene dos costos: migrar a otro motor sería un barrido manual por todos ellos (~13 lugares con
SQL específico de SQLite: `executescript`, `IFNULL`, `SUM(status = 'done')`, `json_extract`,
`sqlite3.Row`), y los tests que tocan persistencia necesitan una base real.

Una capa fina de acceso a datos resuelve las dos cosas a la vez. **No es urgente**: SQLite
alcanza de sobra para este workload y §12.2 deja Fuseki fuera de v1 explícitamente. Pero si
alguna vez aparece la necesidad de Postgres, la inversión que rinde es ésta, no la migración.

### 2. Paralelizar las llamadas de B1

Una pasada de `extract` sobre 100 documentos son ~1.320 llamadas secuenciales de varios
segundos cada una. El cuello de botella es la latencia de red, no la base: las escrituras al
ledger son microsegundos. Con WAL ya activado, un pool acotado de hilos es viable y el ledger
ya serializa correctamente.

### 3. Cachear el grafo inferido por versión

`cq eval --infer` materializa las entailments con HermiT en cada corrida. Sobre la semilla son
1.087 → 1.426 tripletas y tarda poco, pero es determinista dado el estado de la ontología: la
versión ya tiene un hash de estado, así que el grafo inferido se puede guardar contra él y
recomputar solo cuando el estado cambia.

### 4. Saltear secciones de bajo rendimiento en B1

Bibliografía y referencias son ~20% de un paper y no producen menciones útiles. Un filtro
determinista por tipo de sección cortaría ~20% de las llamadas de B1 sin perder nada.

### 5. Footprint de memoria: representación primero, infraestructura después

Todo vive en memoria: el grafo en rdflib (D17) y los vectores en un dict dentro del `Matcher`.
La pregunta natural es si eso escala y si conviene mover cosas a almacenes externos —triplestore,
base de vectores, Postgres—. **Medido, la respuesta corta es que a la escala declarada no hace
falta, y que la mejora que sí rinde no es infraestructura sino representación.**

Lo medido sobre el estado actual:

| Qué | Ahora | Proyección |
|---|---|---|
| Grafo rdflib | 1.087 tripletas · 1,5 MB (1.386 B/tripleta) | 20k tripletas → **28 MB** · 200k → 277 MB |
| Un vector como `list[float]` | **12,1 KB** | 1.000 documentos → **1,0 GB** |
| El mismo en `numpy float32` | **1,5 KB** (8× menos) | 1.000 documentos → **0,13 GB** |

Menciones medidas: 85 por documento. §12.2 estima que un ABox de 1.000 páginas da 5k–20k
tripletas, así que a la escala del spec —100 documentos, 30–50 clases— el grafo son ~28 MB y
los vectores ~0,1 GB. Nada de eso justifica un servicio aparte.

**Lo que sí conviene hacer, y es barato:** el caché de vectores del `Matcher` guarda
`dict[str, list[float]]`. En numpy `float32` ocupa 8× menos, sin dependencia nueva ni servicio
nuevo — numpy ya está instalado con el extra `matching`. Es un cambio local que convierte 1 GB
en 130 MB en el peor caso proyectado.

**Cuándo sí cambiaría la respuesta.** El spec ya fijó los umbrales y conviene respetarlos en vez
de anticiparse:

- **Fuseki / triplestore** (§12.2): "reconsiderar cuando el ABox no entre en memoria". Con
  1.386 B/tripleta eso son millones de tripletas, dos órdenes de magnitud más que lo previsto.
- **Base de vectores**: el criterio análogo es cuando la búsqueda exhaustiva deje de servir.
  Hoy son 34 clases objetivo; el blocking ya acota los pares de resolución de entidades y el
  cálculo de similaridad se materializa por bloques de 512 filas, así que el pico no es n².
  Una base de vectores con ANN paga recién cuando los objetivos sean decenas de miles.
- **Embeddings de grafos de conocimiento** (D4, §12.2): explícitamente fuera de v1 hasta que el
  grafo crezca uno o dos órdenes de magnitud.

**Postgres es otro eje, no éste.** Migrar el store no baja el footprint: el grafo y los vectores
siguen en el proceso. Solo ayudaría si además se mueve el *cómputo* al motor —por ejemplo
pgvector resolviendo la búsqueda por similaridad del lado del servidor—, y eso recién rinde a
escalas muy superiores a ésta. Para la discusión de motor de base ver el punto 1.

### 6. Medir si el prompt caching del proveedor se activa

El gateway reporta `prompt_tokens_details.cached_tokens` y los prompts ya tienen el prefijo
estático adelante, que es la forma correcta para caching por prefijo. Nunca se midió si se
activa. Impacto acotado —la entrada no es el driver de costo, el razonamiento sí— pero es
información gratis.

---

## Decisiones tomadas con evidencia insuficiente

Estas no son mejoras opcionales: son decisiones que hoy están apoyadas en muy poco y que
condicionan todo lo que viene después.

### 7. `matching.match_against` y `matching.use_cross_encoder` — RESUELTO

**Medido sobre `craft-cl`**: 8.723 menciones gold contra 3.418 clases candidatas, no diez pares
hechos a mano. `match_against: label` era correcto y por amplio margen — F1 **0,774** contra
0,135 de etiqueta+glosa y 0,080 de glosa sola. La conclusión sacada con n=10 se sostuvo; la
evidencia que la sostenía, no.

`use_cross_encoder` queda cerrado en **off**, medido dos veces: separación -0,56 sobre el
inventario completo y **-0,50** en la corrida con holdout, F1 0,127 contra 0,774. Negativa
quiere decir que el top-1 correcto puntúa *más bajo* que el equivocado (mediana 0,100 contra
0,258): no hay umbral que lo arregle, porque el que sube deja preferentemente los errores.

Lo que sigue abierto no es el interruptor sino la mejora que lo reemplazaría: un re-ranker
entrenado sobre los accept/reject acumulados de la zona gris (§6.3), que es un instrumento
distinto de un re-ranker de IR genérico. Ver la entrada de LoRA más abajo.


### 8. Los umbrales — MEDIDOS, y el punto de operación no transfiere

Ya no son los defaults del spec. Sobre `craft-cl`, el 0,70 que traía aceptaba mal 3 de cada 10
menciones (precisión 72,3%); ahora `auto_merge` está en 0,95 (precisión 97,9%) y
`grey_zone_lower` en 0,80 (82,9%), elegido por debajo del pico de F1 para mandar la duda al
usuario en vez de descartarla como huérfana.

**Lo que sigue sin resolverse es transferirlos.** Se midieron contra un inventario de 3.418
clases y la semilla de aplicación tiene 34: con más candidatos hay más chances de que algo
espurio supere el umbral, así que el punto de operación se mueve con el tamaño y no se sabe
cuánto. Esa curva es exactamente la tarea C5 del plan —179, 3.419 y ~40k clases— y hasta
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


### 8b. El catálogo de patrones de modelado tiene dos entradas

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
§8.2 queda en blanco porque las CQ no se re-corren contra el estado que la rama produciría.
Ninguna de las dos es difícil; las dos hacen que el puntaje de la rama sea menos informativo de
lo que el spec pretende.


### 8c. El enriquecimiento de glosas depende de un catálogo de señales, y del par

`enrich` encuentra los pasajes definitorios por patrón —doce señales enumeradas, en inglés y
español— y ese catálogo es el techo de lo que la etapa puede ver. Una definición escrita como
"llamamos X a…" o con la señal en una nota al pie no la encuentra. La mejora natural no es
agregar patrones de a uno sino medir el recall del filtro contra un corpus con definiciones
anotadas; hasta entonces, cuántos pasajes se pierden es desconocido, no cero.

Verificado sobre el corpus real: 1.000 bloques utilizables, y "open science" da 5 pasajes en 3
documentos con las señales `is a` y `means`. Sobre las 34 clases de la semilla da **cero**, que
es el desajuste temático de la entrada 9 y no una falla del filtro.

Falta lo que cierra el bucle: **`match` no se re-ejecuta solo sobre las huérfanas cuando las
glosas cambian**, como pide §4.3. `enrich` avisa y deja el comando escrito, pero el disparo es
manual. Y el control de circularidad hoy sólo se consulta (`circular`); no descuenta esas
menciones de ninguna métrica de cobertura, que es para lo que el spec lo pide.


### 8d. Los conflictos fácticos ven un solo tipo de aserción

`conflicts` detecta el desacuerdo que este pipeline puede tener hoy: dos documentos que tipan a
la misma entidad a dos clases. Es el único porque el ABox sólo contiene tipos y procedencia —
**no hay extracción de propiedades**, así que "el documento 12 dice que X ocurrió en 2019 y el 47
dice 2021" no es representable y por lo tanto tampoco detectable. Cuando existan propiedades,
`detect` necesita una segunda familia de conflictos y `_settle` una política por propiedad, no
sólo por entidad.

Dos cosas que hoy quedan a mano:

- **La contextualización no está implementada.** Es la cuarta política de §6.4, la única de nivel
  TBox, y es cara y global. `conflicts` reporta el patrón que la dispara y dice que pertenece a
  `branch`, pero el catálogo de `branch` no tiene todavía un eje `contextualize:<propiedad>` —
  ver 8b. Es el enganche natural entre las dos etapas y está sin hacer.
- **`conflict_pattern_threshold: 3` no está calibrado.** Sale de un argumento, no de una medición,
  y como todos los demás umbrales del proyecto habría que verlo contra un par de calibración.

Y una limitación del instrumento: cuando el razonador no está disponible, la incompatibilidad se
calcula sólo con la disjointness asertada más su herencia, que es una **cota inferior**. Dos
clases pueden ser incompatibles por una combinación de restricciones que ningún `owl:disjointWith`
declara. El comando dice cuál de los dos tests usó, precisamente para no reportar la ausencia del
instrumento como la ausencia del hallazgo.


### 8e. La cadena B5 tiene seis de siete filtros, y el que falta es el caro

**OntoClean (filtro 4) está, y su punto débil es de dónde salen las etiquetas.** Las cuatro
restricciones son mecánicas y no tienen deuda; el insumo sí. Con ontología superior las
metapropiedades se heredan, y sin ella las etiqueta el LLM — que el propio spec reconoce como
"factible, menos confiable, y trabajo adicional que contradice parcialmente D5". Nadie ha medido
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
hoy siempre reporta SKIPPED en el caso de aplicación. Escribir el primer juego —procedencia
obligatoria, cardinalidad de las etiquetas, individuos sin tipo— es trabajo pendiente y barato.


### 8f. Las propiedades funcionales están listas y no tienen qué mirar

La maquinaria de §6.8 está: relevamiento con la distribución, exclusión de duplicados sin
resolver, pregunta al usuario, y `--declare` corriendo el razonador para mostrar qué se
fusionaría antes de commitear nada. **Lo que falta es el insumo**: el ABox tiene tipos y
procedencia, no propiedades de dominio, así que hoy el relevamiento devuelve vacío en el caso de
aplicación. Extraer propiedades es una etapa nueva de B1 y el spec la deja fuera de v1; hasta que
exista, ésta es una etapa correcta sin trabajo que hacer.

Dos límites del relevamiento mismo, para cuando lo tenga:

- **`functional_min_individuals: 5` no está calibrado**, como todos los demás. El spec dice que
  "1 valor en 3 individuos" y "1 valor en 400" son decisiones opuestas, pero no dice dónde está
  el corte, y no hay forma de medirlo sin propiedades reales.
- **`--declare` mide el costo sobre el ABox de hoy**, que no es el argumento — la propiedad es o
  no es funcional en el dominio. Lo que muestra es lo que la equivocación costaría *acá*, que es
  útil y no es lo mismo. El comando lo dice, pero conviene tenerlo presente al leerlo.


### 8g. `next` guía pero no ejecuta

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

Dos cosas que `next` todavía no mira: si el `rules_hash` cambió desde la última regeneración (hoy
siempre sugiere `regenerate`, que es conservador pero ruidoso) y si las glosas cambiaron desde el
último `match`, que es lo que cierra el bucle de §4.3.


### 9. El par corpus/semilla

La semilla es de metodología cualitativa; el corpus son papers de política de ciencia abierta.
Medido sobre 495.213 caracteres: `field note`, `informant`, `coding scheme`,
`thematic analysis`, `grounded theory` y `theoretical framework` aparecen **cero veces**. Es un
caso de aplicación válido, pero no sirve como instrumento de calibración.

---

## Alcance pendiente del spec

### 10. Sesión interactiva

El spec tiene cinco puntos donde decide el usuario —elegir rama (§6.6), zona gris del matcher
(§6.2), propiedad funcional (§6.8), validación de CQ (§4.4), revisión de erratas (§4.3)— y los
cinco tienen ahora por dónde contestarse: `branch --choose`, `grey answer`, `functional
--declare`, `cq`, `review resolve`. Las decisiones sobreviven a re-correr en los cinco casos.

Lo que falta es **una interfaz encima**, no la maquinaria. Contestar 219 pares de zona gris de a
uno por CLI es correcto y es tedioso; el anotador de navegador del conjunto de retención ya
demuestra que la forma existe, y aplicarla acá es trabajo conocido. Y falta que `next` pueda
ejecutar la etapa siguiente además de nombrarla — ver 8g.

Cuando exista, `review_items` también es donde viven las excepciones por caso de las reglas de
mapeo — ver [`plan_reglas_de_mapeo.md`](plan_reglas_de_mapeo.md).

### 11. Mundo abierto: lo que falta

- **Propiedades funcionales (§6.8)**: no implementado, y es donde el spec dice que la evidencia
  del ABox es inválida *en principio* bajo OWA. El riesgo que nombra es silencioso: una
  funcional declarada por error hace que el razonador infiera `owl:sameAs` y fusione entidades
  distintas sin lanzar ninguna inconsistencia.
- **`refuted` / `NegativePropertyAssertion` (§6.4)**: el estado existe en el esquema, nada lo
  escribe, y todavía no hay ABox donde poner la aserción negativa.
- **CQ negativas**: `cq_a4_05` en los ejemplos pregunta por completitud del grafo con
  `FILTER NOT EXISTS`, que es mundo cerrado. Sirve como diagnóstico del artefacto, pero §4.4
  define el tipo negativo como "mundo abierto explícito". Mal precedente para quien escriba CQ
  nuevas.

### 12. Ruta VLM

Páginas `scan`/`uncertain` quedan sin parsear, las figuras sin captioning y las fórmulas sin
extraer. El pipeline lo registra en vez de fingir que las procesó.

### 13. Tablas sin bordes

`find_tables` solo ve tablas con líneas; la estrategia por texto devuelve la página entera como
tabla. El hueco se **mide** —columna "table gap"— pero no se cubre. La respuesta del spec es
rutear esas páginas a MinerU.

### 14. `language.py` está cableado a es/en

Los marcadores de palabras función están hardcodeados. Otro idioma son ~10 líneas más, o
apoyarse en el `/Lang` declarado del PDF.

### 15. Código sin consumidor

Tres cosas implementadas y probadas que nada invoca. No están rotas: están desconectadas, y
cada una es o bien un cable que falta o bien código a borrar.

- **`versioning.nearest_state`** — detección de loops parciales (§6.8): la rama vuelve *casi* al
  estado anterior, mismo compromiso de modelado con IRIs distintos, y el hash exacto no lo ve.
  Distancia de Jaccard sobre los conjuntos de axiomas normalizados. Falta el umbral en el config
  y el llamador, que es B6 al presentar una rama. Antes de B6 no hay dónde enchufarlo.
- **`iteration.mode | trigger | batch_size | max_iterations`** — `mode` elige entre re-correr
  la extracción sobre todo el corpus o sólo sobre lo nuevo (D14, default global); `trigger` y
  `batch_size`, cuándo se dispara una iteración; `max_iterations`, el corte duro de §10.3.
  Los cuatro describen un loop que hoy no existe: las etapas se corren a mano, una por comando.
  Se consumen cuando exista el orquestador, no antes.
- **`branching.*`** — `max_branches`, `present_independent_axes_separately`,
  `auto_apply_when_no_axis`. Configuran B6, que no está implementado.

Lo que hay que evitar es que crezcan en silencio: una clave de config que nadie lee afirma algo
falso sobre lo que el sistema hace. `matching.blocking_strategy` fue el caso —decía `embedding`
y bloqueaba por prefijo de 4 caracteres— y se resolvió haciendo que el config **rechace** un
valor no implementado en vez de aceptarlo. Ese es el patrón para las que quedan.

### 16. Multi-rama: las preguntas que el spec deja abiertas

Que B6 no esté implementado lo dice el README. Lo que va acá es distinto: el diseño multi-rama
tiene preguntas que **el spec mismo declara sin resolver**, y quien lo implemente necesita
encontrarlas antes de empezar, no descubrirlas a mitad de camino.

- **Expiración de rechazos (§6.7, riesgo R2).** Con semilla reorganizable un rechazo no es
  permanente: lo rechazado en la iteración 3 puede ser correcto en la 9 porque la estructura
  cambió. Bloquearlo para siempre acorrala el proceso; no bloquearlo produce un loop. La
  política elegida —registrar el rechazo relativo al estado de la ontología y expirarlo cuando
  las clases involucradas se reorganizan— está marcada textualmente como **"no es una regla
  limpia, requiere ajuste empírico"**. Es la deuda más profunda del aparato y no se resuelve
  leyendo: se resuelve con iteraciones reales encima.
- **Comparación por forma normal (§6.7).** Para detectar re-proposición hay que normalizar el
  axioma antes de comparar, o el mismo compromiso vuelve con IRIs distintos y no se detecta.
  La pieza existe —`versioning.logical_axioms` canonicaliza— pero no está conectada a la tabla
  `decisions`, que es donde vive el historial de rechazos.
- **Scoring de ramas en frío (§11).** `historical_affinity` y `parsimonia` requieren historial,
  y el spec dice explícitamente que se **omitan** en las primeras iteraciones en vez de
  calcularse con datos insuficientes. O sea: el scoring nace incompleto por diseño y hay que
  implementarlo sabiéndolo.
- **Techo de 3–5 ramas (§6.6).** Es un número puesto a dedo contra una explosión de 2^k. El
  mecanismo real que lo evita es presentar los ejes independientes por separado y armar ramas
  completas sólo cuando están acoplados; el techo es la red, no la solución.
- **El umbral de `nearest_state`.** Lo que menciona el punto 15 como cable faltante tiene además
  un parámetro sin calibrar: cuánta distancia de Jaccard cuenta como "casi el mismo estado". No
  hay forma de fijarlo sin iteraciones reales, igual que la expiración de rechazos.

Todo esto comparte una propiedad incómoda: **no se puede calibrar contra un corpus externo**,
como sí se puede el matcher. Depende del historial de decisiones de este proyecto en particular,
que hoy tiene cero entradas.

### 17. Tipado consciente de la jerarquía

Hoy el matcher rankea cada mención contra las clases como si fueran independientes: no sabe que
`Interview ⊑ Technique`. Usar la estructura —preferir la clase más específica cuyos ancestros
también puntúan, penalizar una cuyos hermanos puntúan idéntico— es el mecanismo natural contra
el eco léxico, que es el modo de falla que ningún umbral filtra (punto 7).

No se puede medir sobre la semilla actual: 34 clases, profundidad 3, seis raíces. Sí sobre un
par de calibración con jerarquía profunda, donde entra como una variable más del barrido de
umbrales.

### 18. `cross_language_always_grey` nunca se midió

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
tabla de tareas: no está en el camino crítico de la calibración, y no conviene que bloquee C2–C7.

Mientras tanto el default se queda como está. Lo honesto es que se queda por falta de evidencia
en contra, no por evidencia a favor.
