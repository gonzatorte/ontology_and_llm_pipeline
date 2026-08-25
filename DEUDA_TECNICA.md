# Deuda técnica y mejoras

Documento vivo. Registra **mejoras a futuro** —cosas que hoy funcionan pero podrían estar
mejor, y decisiones tomadas con evidencia insuficiente que conviene rehacer cuando la haya—.
No es una lista de bugs: lo que está roto se arregla, no se documenta.

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

### 7. `matching.match_against` y `matching.use_cross_encoder`

Ambas se decidieron sobre **10 pares armados a mano, midiendo solo recall**, y sobre un par
corpus/semilla que después resultó estar temáticamente desalineado. La medición de recall no
dice nada de precisión, y con etiquetas se pierde precisión por eco léxico: `subject` en
sentido de *tema* tipa como la clase **Subject a 0.992**, dentro de la zona de auto-merge, donde
ningún umbral lo filtra.

Ninguna de las dos opciones está resuelta. Ver el plan en
[`plan_cambio_corpus_calibracion.md`](plan_cambio_corpus_calibracion.md).

### 8. Los umbrales 0.92 / 0.70

Son los defaults del spec, nunca medidos. Y no son universales: dependen del encoder. Deberían
ser **salida** de una calibración, no entrada escrita a mano.

### 9. El par corpus/semilla

La semilla es de metodología cualitativa; el corpus son papers de política de ciencia abierta.
Medido sobre 495.213 caracteres: `field note`, `informant`, `coding scheme`,
`thematic analysis`, `grounded theory` y `theoretical framework` aparecen **cero veces**. Es un
caso de aplicación válido, pero no sirve como instrumento de calibración.

---

## Alcance pendiente del spec

### 10. Sesión interactiva

El spec tiene cinco puntos donde decide el usuario —elegir rama (§6.6), zona gris del matcher
(§6.2), propiedad funcional (§6.8), validación de CQ (§4.4), revisión de erratas (§4.3)— y
ninguno tiene interfaz. La mitad del camino ya está: los hallazgos de A0 viven en
`review_items` con estado y decisiones que sobreviven a re-correr. Falta lo mismo para la zona
gris de B2 (219 menciones esperando) y una interfaz encima.

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
