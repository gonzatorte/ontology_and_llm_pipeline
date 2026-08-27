# Plan: el contrato de las reglas de mapeo

Enmienda a `especificacion_pipeline_ontologia.md`. Define lo que el spec nombra cinco veces y
no especifica nunca.

## Problema

Las "reglas de mapeo" son la flecha que va de la capa de menciones al ABox, y son el mecanismo
del que depende la afirmación central del diseño: **ninguna reorganización de la TBox requiere
script de migración**. El spec las invoca en cinco lugares:

| Dónde | Qué compromete |
|---|---|
| `LAYERS`, diagrama | Son la flecha menciones → ABox, y son **versionadas** |
| `LAYERS`, regla de regeneración (`SEPARATE-UNTIL-CONFIRMED` + `SCOPE-PURPOSE`) | "Se cambian las reglas de mapeo y se recomputa" — sustituyen al script de migración |
| `ITER-MATCH` | "Revisar una decisión de fusión no requiere migración. Se cambia la regla de mapeo y se recomputa" |
| `ITER-CONFLICTS` | Notarizar, forzar y refutar viven en el nivel "ABox / reglas de mapeo", con alcance **por caso** |
| `ITER-APPLY` | Tras aplicar una rama: "recomputar el ABox completo desde la capa de menciones con las reglas de mapeo actualizadas" |

Y una sexta referencia indirecta: el esquema de rama (`SCHEMAS-BRANCH`) puntúa `abox_regen_cost: 340`,
"instancias remapeadas" — el spec asume que el efecto de las reglas es contable **antes** de
aplicarlas.

Ninguna de las seis dice qué forma tienen, dónde se guardan, quién las escribe ni cómo se
versionan. La omisión es sistemática, no un párrafo olvidado: `SCHEMAS` define esquema para menciones,
ramas, unidades de trabajo, decisiones, tipados, versiones y hallazgos de revisión, y **no
define uno para las reglas de mapeo**.

## Lo que el spec sí determina sin decirlo

Dos afirmaciones, cruzadas, fijan la naturaleza de las reglas:

- `ITER-CONFLICTS` le da alcance **por caso** a notarizar, forzar y refutar.
- `ITER-APPLY` dice que la regeneración recomputa el ABox **entero**.

Una decisión por caso que tiene que sobrevivir a una recomputación total no puede vivir dentro
del código que recomputa. **Las reglas son datos, no código.** No es una preferencia de diseño:
es lo único compatible con las dos afirmaciones a la vez.

## Decisiones

### a. Identidad de los individuos: uuid5 sobre la mención ancla

**El problema.** Un individuo del ABox no es una mención: es un **grupo de menciones** que la
resolución de entidades juzgó que hablan de la misma cosa. Ese individuo necesita un IRI, y el
IRI tiene que ser reproducible: regenerar dos veces el mismo estado tiene que dar el mismo
ABox, o toda clave de caché aguas abajo se mueve. Por eso `uuid5` y nunca `uuid4`, igual que en
la normalización de la semilla, que ya acuña así los IRIs de las clases (`seed.py:121`).

**Lo que hay que elegir no es el algoritmo, es el insumo del `uuid5`.** Dos candidatos: los ids
de todas las menciones del grupo, o el id de una sola —la **ancla**, definida como la menor bajo
un orden total sobre los ids, para que no dependa de en qué orden se procesó nada—.

La diferencia aparece cuando el grupo cambia, que es lo que pasa todo el tiempo:

```
iteración 3   grupo {m17, m42}          ancla m17    IRI = uuid5("m17")
iteración 4   un documento nuevo aporta m91 a la misma entidad
              grupo {m17, m42, m91}     ancla m17    IRI = uuid5("m17")   ← el mismo
```

Con el grupo entero como insumo, ese mismo caso da `uuid5("m17,m42,m91")`, distinto de
`uuid5("m17,m42")`: **la entidad cambia de identidad por haber sido mejor documentada**, que es
justo lo que no queremos. Y sumar menciones es el caso frecuente: cada iteración trae más.

Con ancla, el IRI cambia sólo cuando el grupo **se parte** —revisás una fusión y resulta que
eran dos entidades—, y ahí el cambio es correcto: efectivamente apareció una entidad nueva.

| Insumo del uuid5 | El grupo crece | El grupo se parte |
|---|---|---|
| El grupo entero | el IRI cambia | el IRI cambia en las dos mitades |
| **La mención ancla** | **el IRI se conserva** | el IRI se conserva en la mitad que retiene el ancla |

**Por qué el ancla y no otra cosa:** la capa de menciones es inmutable salvo por extensión (`LAYERS`),
así que los ids de mención son lo único del sistema que no se mueve nunca. Anclar ahí es anclar
a lo estable. Al ABox en sí esto no le importa —se regenera entero—, pero el historial de
decisiones y las anotaciones **sí referencian individuos**, y esas referencias se romperían en
cada fusión revisada.

**El caso que empeora**, que se documenta y se acepta: si al partirse el grupo el ancla queda en
la mitad chica, la mitad grande recibe un IRI nuevo aunque conceptualmente sea la misma entidad
de siempre.

### b. Granularidad: política global en el config, excepciones por caso en el store

Satisface las dos restricciones a la vez: `CONFIG` quiere todo umbral en `config/default.yaml`, `ITER-CONFLICTS`
quiere decisiones por caso.

Las excepciones **no necesitan tabla nueva**. `review_items` (`review.py:43`) ya tiene las tres
propiedades que hacen falta:

- identidad derivada del contenido, así que re-correr no duplica ni reabre lo ya decidido;
- estado `open | accepted | rejected | superseded`, con lo rechazado sobreviviendo (`ITER-FEEDBACK`);
- relativa a una versión de ontología, así que una excepción sobre un estado que ya no existe
  queda `superseded` en vez de acumularse como backlog.

Y su docstring ya separa registrar la decisión de actuar sobre ella, que es exactamente la
separación entre una regla de mapeo y la regeneración que la consume. Los conflictos fácticos
de `ITER-CONFLICTS` y la pregunta por propiedad funcional de `ITER-APPLY` entran como `kind` nuevos.

### c. Versionado: hash del conjunto efectivo, al lado del hash de estado

Sin esto la regeneración no es reproducible: la misma versión de TBox con otro conjunto de
reglas da otro ABox, y no habría forma de notarlo.

Se serializa el **conjunto efectivo** —política global más las excepciones resueltas y vigentes—
en forma canónica, se hashea, y el hash se guarda en `versions` junto a `state_hash`. Dos
consecuencias que se ganan de arriba:

- El par `(state_hash, rules_hash)` identifica un ABox. Regenerar con el par ya visto es un
  no-op comprobable.
- `abox_regen_cost` (`SCHEMAS-BRANCH`) se vuelve computable: es el diff entre el ABox del par actual y el
  del par resultante, sin aplicar nada.

## El contrato

Una regla de mapeo es un registro declarativo con estos campos. La política global los fija
para todo el corpus; una excepción los sobrescribe para un caso.

| Campo | Qué decide | Default propuesto |
|---|---|---|
| `individual_from` | Qué constituye un individuo | `entity` — el componente conexo de las fusiones. La alternativa, `mention`, no resuelve nada y sólo sirve para diagnosticar |
| `type_from` | Qué zonas de tipado producen `rdf:type` | `auto` — la zona gris no tipa hasta que la contestás. Es la política conservadora de `ITER-MATCH` llevada al ABox |
| `provenance` | Cómo cada tripleta apunta a las menciones que la sostienen | Grafo con nombre por documento. Sin esto no hay procedencia verificable y `LAYERS` deja de sostenerse |
| `conflict_policy` | `ITER-CONFLICTS`: `notarize` / `force` / `refute` | `notarize` — el único que no destruye información, y el default silencioso que el spec pide |
| `duplicate_policy` | Qué pasa con `possible_duplicate_unresolved` | Individuos separados, marcados, y **fuera del conteo de soporte funcional** (`ITER-APPLY`) |

### Propiedades que la regeneración debe cumplir

- **Determinista.** Mismos insumos, mismas tripletas, byte a byte tras canonicalizar.
- **Idempotente.** Correrla dos veces sobre el mismo par `(state_hash, rules_hash)` no cambia
  nada.
- **Total.** Nunca escribe en la capa de menciones. La flecha del diagrama de `LAYERS` va en un solo
  sentido, y ésta es la única etapa que podría violarlo por descuido.

### La función

```
regenerate(menciones, tipados[version], entidades, reglas) -> Graph
```

Pura. No consulta el LLM, no consulta al razonador, no pregunta nada. Todo lo que necesita
decidir ya está decidido: los tipados los produjo el matcher, las entidades el union-find, y lo
que ninguno de los dos pudo resolver está en `review_items` esperando, no bloqueando.

## Qué ya está y qué falta

**Está:** menciones inmutables con procedencia; `mention_typing` por versión de ontología, ya
deliberadamente fuera de la tabla `mentions`; `entities_from` derivando componentes;
`possible_duplicate_unresolved` marcado; `review_items` como store de excepciones.

**Implementado** (`mapping.py`, `versioning.record_rules`, comando `regenerate`): el registro
de reglas con su serialización canónica y su hash; la columna `rules_hash` en `versions`, con
migración para stores que la preceden; la función de regeneración, pura y sólo-lectura sobre la
capa de menciones; y la política global en `mapping:` del config.

**Falta:** las excepciones por caso escritas en `review_items` —hoy `MappingRules.exceptions`
existe, entra en el hash y nada la puebla, porque las interfaces de decisión no existen—; y el
disparador tras aplicar una rama, que llega con la aplicación y versionado (`apply`) y no
antes.

## Orden

El comando `regenerate` sobre la **versión actual** se puede construir ya: no espera a la
axiomatización, porque no necesita que la TBox cambie para emitir el ABox de la TBox que hay.
Eso es lo que destraba el filtro SHACL de la cadena de validación (`ITER-BRANCH`), que opera sobre el ABox y hoy no tiene sobre
qué operar.

La *re*-generación tras un cambio de TBox —el disparador— sí espera a la axiomatización y a
`apply`. Es la distinción
que conviene no perder: la derivación se puede construir hoy, el ciclo no.

## Fuera de alcance

- Reglas escritas por el modelo. Las reglas son decisiones del usuario o defaults del sistema;
  la extracción de candidatos (`ITER-EXTRACT`) ya prohíbe que el LLM escriba OWL, y esto es la misma
  frontera.
- Contextualizar (`ITER-CONFLICTS`) — reificar una propiedad con su fuente. Es TBox, es por propiedad y no
  por caso, y pertenece a la construcción de ramas (`branch`) como eje de modelado.
