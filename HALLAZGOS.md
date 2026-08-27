# Hallazgos y decisiones de implementación

Lo que se **midió** y lo que se **decidió** construyendo el pipeline, con el número que sostiene
cada decisión. Es el complemento del registro de decisiones de diseño del spec (§2, D1–D25):
aquéllas se tomaron antes de ver datos, éstas después.

Tres documentos vecinos y qué contesta cada uno, para no duplicarlos acá:

- [`README.md`](README.md) — cómo se usa lo construido y en qué estado está cada etapa.
- [`DEUDA_TECNICA.md`](DEUDA_TECNICA.md) — qué falta y qué convendría rehacer con más evidencia.
- [`especificacion_pipeline_ontologia.md`](especificacion_pipeline_ontologia.md) — el diseño.

**Regla de lectura.** Cada hallazgo dice sobre qué se midió y con qué n. Un número sin su
población es una anécdota, y este proyecto ya se equivocó dos veces por sacar conclusiones de
n=10 (ver *Conclusiones que hubo que retirar*).

**Los valores de configuración del spec (§7) son históricos.** `auto_merge_threshold: 0.92` y
`grey_zone_lower: 0.70` son los defaults con los que se escribió el diseño, antes de que hubiera
con qué medirlos. Los valores vigentes están en `config/default.yaml` y el porqué en 1.1. El spec
no se editó: es el registro de lo que se decidió antes de ver datos, y reescribirlo borraría
justamente la diferencia que este documento existe para mostrar.

**Sobre la procedencia.** Este repositorio lo escribieron tres sesiones en paralelo, y los
hallazgos vienen de las tres. Todo está commiteado bajo un solo autor, así que la atribución no
se lee del historial:

| Sesión | De qué se ocupó | Dónde quedó |
|---|---|---|
| `f0449040` (la que escribe) | Fase B completa, cadena B5, criterios de parada, calibración con holdout | 1.1, 1.2, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9 y todo §2 |
| `e167c718` | Elegir e importar el par de calibración; CRAFT/CL como primario | [`../calibration/craft-cl/NOTA_FASE0.md`](../calibration/craft-cl/NOTA_FASE0.md) y `plan_cambio_corpus_calibracion.md` |
| `c19367ed` | Blocking por embeddings, diff semántico, reglas de mapeo, `bridge`, homónimos | `plan_reglas_de_mapeo.md`, deuda 19, y 1.3 |

Hay dos transcripts más y ninguno aporta decisiones: `b8c7e99c` es **la misma conversación que
`f0449040`, bifurcada** —mismo timestamp inicial, mismo primer mensaje— y `7994cb22` es una
consulta sobre el funcionamiento de Claude Code, sin contenido del proyecto. La única bifurcación
real fue "crear enlaces simbólicos al corpus" contra "dejar todo como está", y el usuario eligió
lo segundo en el mismo turno; no hay symlinks y las rutas externas se documentan en el config y
el README.

El modo de falla de trabajar así —una edición anclada a texto que falla abierta cuando otra
sesión movió el contexto— está en la sección de coordinación de
[`DEUDA_TECNICA.md`](DEUDA_TECNICA.md), con lo que hay que hacer para evitarlo.

---

## 0. Para qué es todo esto

**El entregable es el sistema y su caracterización, sin dominio comprometido.** El spec lo dice
en §1.4 —"sin tarea downstream comprometida"— y conviene tenerlo a la vista porque durante un
tiempo la documentación de este repositorio afirmó lo contrario: que había un "caso de
aplicación" (la semilla de metodología cualitativa) al que el proyecto debía volver después de
calibrar. **No lo hay.** Todos los pares (corpus, ontología) son instrumentos, y lo que califica
al sistema es cómo se comporta a través de ellos, no cómo le va en uno.

Corregido el 2026-09-09, en 15 lugares de 7 archivos. Venía de la tarea C7 del plan de
calibración, que decía "volver al par de aplicación"; esa tarea está retirada y en su lugar entró
C8, correr el pipeline entero sobre un par publicado.

---

## 1. Mediciones

### 1.1 El matcher, contra CRAFT/CL — n = 8.723 menciones gold

Medido sobre un par publicado —corpus anotado contra su propia ontología—, donde la respuesta
correcta se conoce para cada mención. `onto-pipeline calibrate craft-cl`.

**`match_against: label`, contra la premisa del spec.** Con 3.418 clases candidatas, 96% de
ellas con definición escrita por curadores —la condición bajo la que la premisa del spec
debería haber ganado— recall@1: etiqueta 69,8%, etiqueta+glosa 13,3%, glosa sola 7,9%. Lo que
decide no es el recall sino el **signo de la separación** entre aciertos y errores, en
desviaciones estándar agrupadas: **+1,61 con etiquetas, −0,42 con glosas**. Negativa significa
que los errores puntúan más alto que los aciertos, y entonces ningún umbral ayuda: subirlo
conserva preferentemente los errores. Una mención es un sintagma corto y una etiqueta también;
una glosa es una oración, y un encoder simétrico pierde más por esa diferencia de forma que lo
que gana en significado.

**El cross-encoder queda apagado, medido dos veces.** Separación **−0,56** sobre el inventario
completo y **−0,50** en la corrida con holdout; F1 **0,127** contra 0,774 del bi-encoder, cada
uno en su mejor umbral. Mediana del top-1 correcto 0,100 contra 0,258 del equivocado. No es que
haga falta otro umbral: el orden que produce está anticorrelacionado con el correcto.

**Los umbrales, con el inventario completo:**

| umbral | precisión | recall | F1 |
|---|---|---|---|
| 0,70 | 72,3% | 68,9% | 0,706 ← el default del spec: 3 de cada 10 aceptadas, mal |
| 0,80 | 82,9% | 68,5% | 0,750 |
| 0,90 | 91,2% | 67,2% | **0,774** ← pico de F1 |
| 0,95 | 97,9% | 58,0% | 0,729 |

El recall casi no se mueve entre 0,30 y 0,90 (69,8% → 67,2%): el ranking ya es correcto y el
umbral sólo filtra mistypes. Por eso subir es casi gratis en recall y caro en huérfanas.
`auto_merge_threshold: 0.95`, `grey_zone_lower: 0.80`.

### 1.2 Huérfanas genuinas — la corrida con `--holdout 0.2`

Un corpus anotado contra su propia ontología **no tiene huérfanas genuinas por construcción**:
toda clase gold está en la ontología, así que toda huérfana es falsa y la tasa no dice nada sobre
un par donde sí falten conceptos. Reteniendo 683 de las 3.418 clases, las **546 menciones** de
esas clases pasan a ser huérfanas genuinas con respuesta conocida.

Dónde cae una mención de un concepto que la ontología **no** tiene:

| destino | menciones | |
|---|---:|---|
| fusionada en silencio contra una clase equivocada (≥0,95) | 15 | 2,7% |
| zona gris 0,80–0,95: llega a revisión, recuperable | 398 | 72,9% |
| huérfana (<0,80): llega a inducción, que es su destino | 133 | 24,4% |

**El diseño de tres zonas hace lo que promete**: las dos zonas atajan el 97,3% y el error caro
—fusionar en silencio un concepto nuevo contra uno viejo— es marginal.

Dos precauciones al leer esto. El holdout achica el inventario a 2.736 clases y con menos
distractores la precisión sube (99,2% en 0,95, 87,4% en 0,80 sobre las menciones cubiertas), así
que los números del inventario completo siguen siendo el caso duro. Y **la tasa de falsos
huérfanos casi no se movió** —18,0% contra 17,3% en 0,80— porque sólo 546 de 8.723 menciones
cambiaron de lado: la cifra anterior era del orden correcto **por accidente**, no porque el
holdout no importara.

### 1.3 El par corpus/semilla no se corresponde — n = 495.213 caracteres

La semilla es de metodología cualitativa; el corpus son papers de política de ciencia abierta.
`field note`, `informant`, `ethnograph`, `coding scheme`, `thematic analysis`,
`content analysis`, `grounded theory` y `theoretical framework` aparecen **cero veces**.

Confirmado por segunda vía al construir `enrich`: sobre 1.000 bloques utilizables, el filtro de
señales definitorias encuentra 5 pasajes en 3 documentos para "open science" y **cero** para las
34 clases de la semilla. No es una falla de la etapa; es la etapa reportando el desajuste.

Consecuencia: una tasa de falsos huérfanos medida sobre este par no sería mala, **sería sin
significado**. Por eso el instrumento de calibración es un par publicado y separado.

### 1.4 Subsunción contada como desacuerdo — 4 conflictos aparentes, 1 real

Sobre el caso de prueba de `conflicts`: cuatro entidades tipadas a dos clases cada una. Tres de
esos pares eran `Interview` y `Technique`, con la primera subclase de la segunda — un hecho
dicho a dos niveles de detalle, no un desacuerdo. Con el cierre de subclases aplicado queda **una**
contradicción real (`Interview` / `Organization`, incompatibles por herencia de disjointness, que
encontró el razonador y no la disjointness asertada).

### 1.5 Criterios de división: por qué el corte se elige por padre — n = 6

Similitudes entre criterios escritos a mano, con el bi-encoder configurado:

| | medio-a | medio-b | propósito-a | propósito-b |
|---|---|---|---|---|
| medio-a | — | **0,80** | 0,11 | 0,19 |
| propósito-a | 0,11 | 0,11 | — | **0,38** |

Dentro de un corte: 0,80 y 0,38. Entre cortes: ≤0,25. **No hay un umbral fijo que sirva para los
dos**: cuán parecidos se leen dos criterios depende de cómo los escribieron, no de una constante.
De ahí que el corte se elija por padre y la única constante sea `min_criterion_separation`,
cuánto más nítido tiene que ser el corte que el ruido que rompe. Ese 0,10 sale de **estos seis
casos** y no está calibrado.

### 1.6 El riesgo silencioso de una propiedad funcional, hecho visible

Declarando `bornIn` funcional sobre un ABox donde un individuo tiene dos valores distintos, el
razonador entiende que los dos valores son la misma cosa:

```
declaring born in functional would merge 1 group(s) of individuals, and the
reasoner would raise no inconsistency doing it:
  …oslo = …oslo_city
```

Ninguna inconsistencia, ninguna advertencia: la ontología queda consistente diciendo en silencio
que dos cosas son una. Es exactamente el riesgo asimétrico que el spec nombra en D2.

### 1.7 OntoClean con etiquetas del modelo

Sobre el caso de libro: el modelo etiquetó `Person` como **+R** y `Student` como **~R** sin que
se le nombrara la notación, y `validate` rechazó `Person ⊑ Student` por dos restricciones
—rigidez y dependencia—. Es la subsunción mal formada que ningún razonador puede enunciar:
perfectamente consistente en OWL y equivocada.

### 1.8 A3 sobre el corpus real

Cuatro preguntas inferenciales generadas desde dos pasajes por estrato, las cuatro sobrevivieron
el filtro mecánico. Una pregunta si unas entrevistas son Estrategias Metodológicas dado
`Interview ⊑ Technique ⊑ Methodological Strategy` — el tipo que el spec dice que un modelo nunca
escribe solo, y que el prompt sí consiguió.

**Límite observado en la misma corrida:** la cita se verifica que **exista**, no que **sostenga**.
Una de las preguntas, correcta, citaba un pasaje que anuncia las secciones del paper.

### 1.9 Estado del par cualitativo — retirado el 2026-09-09

Estos números son de la dupla semilla cualitativa + corpus de ciencia abierta, que **ya no se
usa**: el proyecto no tiene dominio comprometido y todos los pares son instrumentos (ver
[`DEUDA_TECNICA.md`](DEUDA_TECNICA.md), entrada 9). Quedan acá porque son la única corrida de
punta a punta que hubo hasta ahora, y porque el eco léxico que muestran es sobre el método.

Contra `v5`, que era la versión vigente: 10 documentos (5 en el conjunto de retención), **848
menciones**, de las cuales 18 tipadas automáticamente, **131 en zona gris** y 699 huérfanas
(82%). Más 53 items de revisión abiertos de A0 —13 etiquetas divergentes, 27 chequeos semánticos
pendientes, 13 erratas—. La zona gris es a la vez el trabajo pendiente del usuario y el insumo
que falta para LoRA (§6.3): hoy hay **cero** etiquetas acumuladas.

**Cuidado al leer números de versiones viejas.** `v2` tiene 1.725 tipados contra una capa de
menciones que después se volvió a extraer, así que 116 de sus 219 pares de zona gris apuntan a
menciones que ya no existen. Los tipados son función de (menciones, versión) y se recomputan;
los de una versión que no se volvió a matchear quedan como estaban. Al citar un número, decir
contra qué versión.

### 1.10 Eco léxico, y dónde está de verdad el problema del homónimo

**El eco léxico es el modo de falla que ningún umbral filtra.** Sobre la semilla, 24 de 34 clases
superan 0,70 y **11 de esas 24 son eco léxico**: la mención es la palabra corriente que da nombre
a la clase, no una instanciación. Sobre el corpus real no mejora al cambiar de versión: en `v2`
las 32 automáticas eran **todas** eco léxico (`question` 0.998, `information` 0.998,
`support` 0.993, `subject` 0.992) y en `v5` las 18 siguen igual — `information` 0.998,
`support` 0.993, `researchers` 0.946. La única que parece una instanciación real es
`data collection` 0.995.

Verificado al analizar el caso del homónimo (`cell` de biología contra `cell` de una
organización clandestina): **el riesgo no está donde parece**. El `uuid5` de un individuo se
computa sobre el id de la mención, único por ocurrencia —`researchers` aparece 12 veces y tiene
12 ids distintos—, y la resolución de entidades ya manda el caso peligroso a zona gris. El
agujero está en el **tipado**, y la vía más directa para cerrarlo —meter la oración de la mención
en la comparación— está sin implementar. Detalle completo en la deuda 19.

### 1.11 El cuello es la recuperación, y meter contexto no la arregla

Dos mediciones encadenadas, y la segunda cierra una línea de trabajo.

**Recall@k, para saber qué techo tiene re-rankear.** Un re-ranker sólo puede reordenar lo que la
recuperación trajo, así que cuánto puede aportar es exactamente la diferencia entre @1 y @k:

| | @1 | @5 | @20 | @50 |
|---|---|---|---|---|
| CRAFT/CL (3.418 clases) | 69,8% | 78,8% | 84,6% | 86,7% |
| MaterioMiner (428 clases) | 25,3% | 38,0% | 51,5% | 62,6% |

O sea: re-rankear el top-5 tiene un techo de **+9 puntos** en CRAFT y **+12,7** en MaterioMiner.
Y en MaterioMiner **el 37% de las clases correctas no está ni en el top-50** de un inventario de
428 — no hay reordenamiento que las alcance.

**Meter el contexto de la mención: medido en cuatro formas, las cuatro peores.** La idea era que
comparar la oración y no el sintagma pelado desambiguaría —`lifetime` hacia `FatigueLifetime`—.
Sobre MaterioMiner, n=2.229:

| Qué se compara contra la clase | @1 | @5 |
|---|---|---|
| el sintagma solo | **25,3%** | **38,0%** |
| sintagma + oración, concatenados | 11,3% | 24,6% |
| una ventana de ±50 caracteres | 7,4% | 19,2% |
| fusión de puntajes, α=0,9 | 25,7% | 37,0% |
| fusión de puntajes, α=0,5 | 23,1% | 36,2% |

Sobre CRAFT la concatenación es todavía peor: @1 cae de 69,8% a **14,3%**. Reproducible con
`calibrate --context sentence`, que reporta separación **0,56** contra 1,50.

**Es el mismo hallazgo que el de las glosas, otra vez.** Un encoder simétrico compara textos por
su forma, y agregarle una oración a un sintagma lo convierte en una oración. La fusión de
puntajes lo evita —el sintagma se sigue comparando solo— y entonces no aporta nada: +0,4 puntos
en @1 y **pierde** un punto en @5, o sea ruido.

Lo que queda dicho es dónde **no** está la solución: no es la representación de la mención. Es el
encoder. Un modelo asimétrico o entrenado es la vía, y es lo mismo que ya decía la conclusión
sobre glosas.

### 1.12 Ajustar el re-ranker: la mejora más grande medida, y no transfiere

El cross-encoder de fábrica arruinaba el orden —separación −0,50—. Ajustado con las anotaciones
del propio par, es la mejora más grande que este pipeline midió. Partición **por documento**, y
evaluado sobre documentos que el entrenamiento nunca vio:

| Par | bi-encoder solo | + ajustado | techo (@10) | ganancia | del margen |
|---|---|---|---|---|---|
| CRAFT/CL (n=1.237) | 66,3% | **76,2%** | 77,5% | **+9,9** | 88% |
| MaterioMiner (n=653) | 28,5% | **40,1%** | 47,0% | **+11,6** | 63% |

79 segundos de entrenamiento sobre una GPU de notebook, 37 mil ejemplos.

**Y no transfiere entre dominios.** Entrenado en CRAFT y aplicado a MaterioMiner (n=2.229):
**−2,1 puntos**, o sea peor que no usarlo. Tres documentos de mecánica de materiales le ganan a
setenta y siete de biomedicina por catorce puntos sobre el mismo conjunto de evaluación.

Eso cierra una pregunta de diseño con datos: las etiquetas tienen que salir del dominio donde se
va a usar el matcher. Es exactamente de donde el spec dice que salen —las decisiones de aceptar
o rechazar del usuario en la zona gris— sólo que ahora se sabe cuánto rinde y cuánto no se puede
tomar prestado.

**Dos cosas de método que hacen que el número signifique algo.** Los negativos son los candidatos
equivocados que el propio bi-encoder puso arriba, no negativos al azar: entrenar contra una clase
que el recuperador nunca iba a proponer enseña a distinguir lo que ya estaba distinguido. Y la
partición es por documento y nunca por mención — dos menciones del mismo paper comparten
vocabulario y tema, y separarlas al azar mide memoria.

**Se reporta el techo junto al resultado**, porque subir 9,9 puntos cuando había 11,2 disponibles
es otra cosa que subir 9,9 cuando había 40. El re-ranker sólo reordena lo que la recuperación
trajo; lo que no está en el top-k no lo alcanza.

---

## 2. Decisiones tomadas, y por qué

### 2.1 El matcher

- **`use_cross_encoder: false`, cerrado.** Dos mediciones independientes con separación
  negativa. Vuelve a discutirse recién con un re-ranker tuneado sobre etiquetas propias (§6.3),
  que es un instrumento distinto de un re-ranker de IR genérico.
- **Los umbrales no se movieron con el holdout.** Los números del inventario completo son el caso
  duro y quedan como referencia; los del holdout se anotaron aparte para que nadie compare
  precisiones entre poblaciones distintas.

### 2.2 Ramas (§6.6)

- **Ningún eje sale de un modelo.** Es la única prohibición explícita del spec para la etapa:
  pedir tres alternativas devuelve tres correlacionadas. Salen del razonador (hitting sets
  mínimos sobre las justificaciones, diagnóstico de Reiter) y de un **catálogo enumerado** de
  compromisos de modelado.
- **Dos patrones con detector mecánico**, `attribute_as_class` y `division_criterion`. El tercero
  del spec, reificar vs. propiedad directa, queda **catalogado y sin detector a propósito**, para
  que el hueco se vea en vez de insinuarse. El catálogo es el techo de lo que el sistema puede
  preguntar.
- **El corte del criterio de división se elige por padre**, no con un umbral fijo — ver 1.5.
- **La rama se commitea antes de registrar la decisión.** El razonador todavía puede rechazarla, y
  una decisión registrada sobre un estado que nunca se aplicó sería mentira.
- **Volver a proponer no reabre una decisión ya tomada.** Los ids de rama son deterministas, así
  que un reemplazo directo pisaría la rama elegida y los rechazos de sus hermanas, que son el
  único registro de lo que se descartó (§6.7).

### 2.3 Enriquecimiento de glosas (§6.5 B4b)

- **Los pasajes se encuentran mecánicamente**, por señal definitoria, no preguntándole a un modelo
  cuáles son definitorios: un corpus tiene muchos más párrafos que presupuesto tiene pedidos, y un
  filtro que cuesta un pedido por párrafo no es un filtro.
- **Un sinónimo que no aparece literalmente en los pasajes se descarta.** Puede ser correcto, pero
  quedaría grabado con una procedencia que no se cumple, y el control de circularidad se apoya en
  que esa procedencia diga la verdad.
- **Lo que realimenta al matching es el `altLabel`, no la `definition`** — consecuencia directa de
  1.1. El bucle autocorrectivo de §4.3 existe, pero pasa por los sinónimos.
- **`circular` cuenta, no descuenta.** Los matches contra documentos que escribieron la glosa no
  son evidencia independiente; hacerlos visibles es el primer paso, restarlos de una métrica de
  cobertura es trabajo pendiente.

### 2.4 Conflictos fácticos (§6.4)

- **Notarizar es el default silencioso** porque es la única política que no destruye información.
  Sólo los conflictos que rompen al razonador llegan a revisión; decidir caso por caso es la
  revisión manual que el pipeline existe para evitar (D5).
- **La subsunción no es desacuerdo** — ver 1.4. Sin ese filtro el reporte se llena de la jerarquía
  discutiendo consigo misma.
- **Un patrón sobre un par de clases es una pregunta sobre la TBox, no N casos.** Contextualizar
  una propiedad cambia la forma de todas las consultas sobre ella, las SPARQL de las CQ incluidas,
  así que pertenece a `branch`.
- **`refuted` y `misextracted` no se mezclan.** Parecen iguales en una interfaz y son señales
  opuestas; mezclarlas pierde la única fuente gratuita de etiquetas de error de extracción.
- **Las marcas viajan como excepciones de las reglas de mapeo**, para entrar al `rules_hash`. Una
  decisión que no cambiara ninguna regla sería una decisión que el ABox nunca nota, porque
  `regenerate` es idempotente sobre (estado, reglas).

### 2.5 La cadena de validación (§6.6 B5)

- **El filtro de evidencia se aplica a `textual` y a nada más.** "Todo axioma sin cita se descarta"
  borraría justamente los puentes que hacen útil a la semilla: un axioma `world_knowledge` no
  tiene cita por construcción, y eso es para lo que existe.
- **El filtro 5 es un subconjunto local del catálogo OOPS!, no OOPS!.** El scanner real es un
  servicio web, y mandarle la ontología de alguien a un tercero es una decisión de su dueño, no un
  paso que un pipeline dé por su cuenta. Qué pitfalls quedan afuera está en la deuda.
- **Las shapes de SHACL se escriben a mano y no se derivan de la TBox.** OWL dice qué tiene que ser
  verdad y SHACL qué tiene que estar dicho; bajo mundo abierto, generar una desde la otra
  convertiría cada silencio en una violación. Sin shapes el filtro reporta que **no corrió**, que
  no es lo mismo que pasar.
- **OntoClean pregunta en castellano llano, nunca en notación**, a temperatura 0. Y una clase sin
  etiquetar **no produce violación**: el comando reporta cuántas subsunciones verificó y cuántas
  salteó, porque un filtro que revisara en silencio un décimo de la jerarquía estaría reportando un
  resultado limpio que nunca estableció.

### 2.6 Propiedades funcionales (§6.8)

- **Nada se declara solo.** Detectar funcionalidad desde el ABox es inválido en principio bajo
  mundo abierto: un valor por entidad prueba que no se observó contraejemplo, no que no exista.
  La única dirección sólida es la opuesta —**un contraejemplo refuta**— y es la única conclusión
  que la etapa saca por su cuenta.
- **`--declare` muestra qué fusionaría antes de commitear** — ver 1.6.
- **La distribución va en la pregunta**, no sólo la conclusión: "1 valor en 3 individuos" y "1 valor
  en 400" son la misma señal cualitativa y decisiones opuestas.
- **Los duplicados sin resolver quedan fuera del conteo**: dos duplicados con un valor cada uno se
  ven exactamente como confirmación de funcionalidad, que es la única forma en que el relevamiento
  podría fabricar su propia evidencia.

### 2.7 Criterios de parada (§10.3)

- **La cobertura de menciones no es criterio, a propósito.** El sistema optimiza lo que se mide, y
  una clase paraguas maximiza cobertura destruyendo el valor conceptual.
- **La curva de acumulación no detiene nada**, lea como lea. Es diagnóstico: la única que distingue
  si el problema está en el pipeline o en los datos, y la única que rompe la circularidad de las
  otras tres, que todas miran el corpus.
- **Con menos de dos ventanas dice "desconocido", no "aplanó".** La cola *es* el principio, y
  compararlas es comparar un número consigo mismo.

### 2.8 Orquestación

- **`next` guía pero no ejecuta.** Una decisión pendiente le gana a cualquier etapa que podría
  correr, porque todo lo posterior estaría construido sobre una respuesta que nadie dio. Que
  ejecute requiere extraer diez comandos de sus wrappers de Typer; se dejó sin hacer en vez de a
  medias, porque un runner que se saltea un punto de decisión es peor que ninguno.
- **Las respuestas de zona gris viven en su propia tabla**, no en `mention_typing`, que el matcher
  reescribe entera cada corrida. Volver a preguntar lo mismo todas las veces es cómo un sistema
  entrena a alguien a dejar de contestar.

### 2.9 Competency questions (§4.4)

- **La advertencia de circularidad va primero.** Las CQ generadas miden completitud respecto al
  corpus; la mitigación es A4 y por eso `cq import` va antes que `cq propose` en la documentación.
- **El muestreo es estratificado y con semilla.** Los estratos son lo que hace posibles los tipos de
  pregunta; y "los primeros N" serían los abstracts de los primeros documentos, que producen
  preguntas sobre abstracts.
- **Una cuota incumplida se reporta y no se rellena.** Los tipos inferencial y negativo son los que
  un modelo no escribe solo; taparlos con definicionales escondería justo lo que la cuota fuerza.
- **La SPARQL se parsea antes de juzgar su forma.** Contar patrones de tripleta en algo que no es
  una consulta no mide nada.

### 2.10 LoRA (§6.3) — decidido no escribirlo todavía

Es la única pieza del plan bloqueada por **datos**, no por código. `grey labels --export` ya
escribe el formato; hay una etiqueta. Un script de fine-tuning que nunca corrió sobre datos reales
es código que parece listo y no lo está. El orden: contestar zona gris → exportar → medir el
re-ranker tuneado contra `calibrate` → recién ahí decidir si `use_cross_encoder` vuelve a `true`.

---

## 3. Conclusiones que hubo que retirar

Dos veces se sacó una conclusión con evidencia insuficiente y hubo que desdecirla. Van acá porque
el patrón importa más que los casos.

1. **"Las glosas van a cerrar la brecha de falsos huérfanos."** Medido: la empeoraron. La
   separación con glosas es negativa (1.1).
2. **"`match_against: label` está resuelto"**, dicho sobre diez pares hechos a mano y elegidos por
   recall. Después se verificó que las etiquetas pierden precisión por eco léxico. El barrido de
   n=8.723 terminó confirmando las etiquetas — pero la evidencia bajo la afirmación original no
   alcanzaba, y eso es lo que estuvo mal.

**La lección operativa:** decir sobre qué población se midió, en la misma oración que el número.

---

## 4. Fallas silenciosas encontradas

No son bugs a arreglar —ya están arreglados y fijados con test— sino una clase de falla que este
sistema produce con facilidad: **ninguna de las cuatro lanzó un error**. Todas devolvieron un
resultado que parecía correcto.

| Falla | Qué parecía | Qué era |
|---|---|---|
| `any(graph.objects(...))` | un chequeo de anotaciones faltantes | `objects` devuelve un generador, y un generador siempre es verdadero: el chequeo no podía fallar nunca |
| TriG parseado en `Graph` | SHACL conformando | parsear TriG en un `Graph` conserva sólo el grafo por defecto; el ABox guarda la procedencia en grafos nombrados, así que ninguna shape encontraba target |
| subsunción contada como conflicto | 4 desacuerdos | 1 desacuerdo y 3 veces la jerarquía (1.4) |
| `set(A) \| {b} - set(c)` | los estratos faltantes | `-` liga más fuerte que `\|`: nombraba estratos que sí se habían encontrado |

Y de más atrás en el proyecto, la misma clase: propiedades de anotación SKOS sin declarar
—que sacaban la ontología de OWL 2 DL y hacían que ELK se salteara, **sin error**, con la
cobertura EL cayendo de 84% a 0%— y una clave de caché que omitía el modelo, de modo que cambiar
de tier servía en silencio los resultados de otro modelo.

**Regla que salió de esto:** cuando un chequeo pasa, preguntarse si podría haber fallado.
