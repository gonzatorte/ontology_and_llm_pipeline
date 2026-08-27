# Por qué estos pares y no otros

**Qué es este documento.** El registro de la elección de los pares (corpus anotado, ontología)
contra los que se mide este pipeline: qué criterios se aplicaron, qué se midió de cada candidato,
y por qué se descartó cada uno de los que no entraron. Nada más que eso.

**Qué no es.** No es un plan —el que hubo se ejecutó y se borró— ni el lugar donde viven los
resultados. Lo que cada par arrojó al medirse está en [`findings.md`](findings.md), con el n de
cada número; tener dos copias de una medición es cómo empiezan a divergir. Lo operativo de cada
par —dónde están los archivos, cómo se regeneran, con qué licencia— está en
[`calibration/README.md`](calibration/README.md) y en la `NOTA_FASE0.md` de cada par.

**Por qué se conserva.** Los datos de los pares no se versionan (pesan, son artefactos publicados
de terceros y se regeneran). Este documento es la parte que **no** se puede regenerar: el
razonamiento. Sin él, dentro de seis meses «¿por qué CRAFT y no MedMentions?» sólo se puede
contestar volviendo a hacer el trabajo.

**Todos los pares son instrumentos.** El proyecto no tiene dominio comprometido: el entregable es
el sistema y su caracterización *a través* de pares (`SCOPE-PURPOSE`). Un par se usa para medir
cómo se comporta el pipeline, y no al revés. El par de metodología cualitativa fue el andamio
inicial, para tener con qué probar mientras no existía esto, y está retirado.

## Los criterios

En orden de importancia, y el orden importa: el primero descarta más candidatos que los otros
cuatro juntos.

1. **La ontología trae definiciones.** Sin glosas no se puede correr etiqueta-contra-glosa, que
   era la pregunta abierta de `matching.match_against`. Descarta todo esquema que sea sólo
   etiquetas.
2. **La ontología trae axiomas más allá de la taxonomía.** Definiciones lógicas
   (`intersection_of` / `equivalentClass`), relaciones, disyunciones. Un árbol de `is_a` con
   glosas cumple el criterio 1 y no ejercita nada del lado simbólico. **Cuidado: la distribución
   que shippea el corpus puede no ser la que cumple este criterio** — es lo que pasó con CRAFT,
   abajo.
3. **Escala del inventario comparable.** Miles, no millones: con ~34 candidatos casi cualquier
   mención rankea en algún lado, y con los millones de UMLS el punto de operación es otro.
   Subsetear es aceptable si se documenta el criterio.
4. **Inglés**, o agregar marcadores a `language.py`.
5. **Licencia que permita el uso.**

### Qué transfiere de un par a otro

Es la pregunta que hace que medir afuera sirva para algo, y la respuesta no es «todo»:

- **Transfiere:** etiqueta contra glosa, la elección de encoder, si el cross-encoder aplasta la
  escala, y la *forma* de la distribución de puntajes — dónde se separan verdaderos de falsos.
  Son propiedades del método y del encoder, no del dominio.
- **Transfiere en parte:** los umbrales. El coseno entre dos textos no depende de cuántos
  candidatos haya, pero el *punto de operación* sí: con 40k clases hay más chances de que algo
  espurio supere un corte que con 34. Un umbral externo es punto de partida defendible, no final.
  Y está medido que se mueve —ver `FINDINGS-MEASURED-SIZE-CURVE`.
- **No transfiere:** nada más. Lo que no transfiere pide un chequeo chico por par, no una campaña
  de anotación.

El efecto secundario es el que más importa para el entregable: calibrar afuera convierte fijar
umbrales de tarea manual única en **procedimiento repetible** sobre cualquier par. Si lo que se
valida es el sistema y no el dominio, esa capacidad es parte de lo que hay que validar.

## Los pares elegidos

### Primario: CRAFT `CL+extensions` (verificado el 2026-09-08)

Licencia CC BY 3.0, release v5.0.2 (2022-07). 97 artículos biomédicos completos, 11 módulos, cada
uno en variante propia y `+extensions`. Formato: `articles/txt/*.txt` en texto plano más
Knowtator XML standoff (`<span start end>` + `<mentionClass id>`), con los offsets **sobre el
texto plano** — eso es lo que hace que el importador pueda saltear el parser entero.

**El repo de CRAFT distribuye las ontologías en OBO básico, sin axiomas lógicos.** Medido:

| Cell Ontology | clases | con `def:` | `is_a` | `relationship` | `intersection_of` | `disjoint_from` |
|---|---|---|---|---|---|---|
| la que shippea CRAFT (`cl-basic`, 2019) | 2.164 | 1.792 | 2.869 | 412 | **0** | 0 |
| release actual (`cl-base.obo`) | 3.540 | 3.368 | 4.880 | 4.247 | **5.023** | 39 |

El propio README de CRAFT lo dice: *«We have not implemented these as formal logical definitions
yet… In the future, we intend to distribute the ontologies in OWL rather than OBO format.»* Usar
el `.obo` del repo es quedarse justo con la taxonomía que el criterio 2 descarta. **Decisión:
anotaciones de CRAFT + ontología completa de OBO Foundry.** Los IRIs de clase son estables, así
que las anotaciones resuelven; hay que filtrar `is_obsolete: true`.

Elección de módulo, medida sobre los releases completos:

| Módulo | clases | def | `is_a` | `relationship` | `intersection_of` | veredicto |
|---|---|---|---|---|---|---|
| **CL** | 3.540 | 95% | 4.880 | 4.247 | **5.023** | ~1,4 definiciones lógicas por clase. **Elegido** |
| GO_CC | 4.077 | 100% | 4.699 | 1.983 | 697 | escala ideal, glosas completas, menos denso |
| GO_BP | 23.974 | 100% | 40.486 | 12.941 | 17.697 | muy axiomatizada, un orden de magnitud más grande |
| GO_MF | 10.041 | 100% | 12.274 | 1.254 | 249 | casi taxonomía pura |
| SO | 2.383 | 86% | 2.270 | 592 | 456 | densidad media |
| NCBITaxon | ~2,5M | ~0 | — | — | 0 | **descartada**: es el caso que el criterio 2 evita |
| CHEBI / PR | 200k / 380k | — | — | — | — | descartadas por escala |

Dos artefactos de CRAFT que el banco aprovecha:

- `unused_classes_for_CL_annotations.txt` — clases que los anotadores decidieron no usar nunca.
  Su README garantiza que toda anotación automática contra ellas es un falso positivo, o sea que
  es señal de precisión **sin anotar nada**. Es de donde salió
  `FINDINGS-MEASURED-LABEL-COLLISION`, que resultó ser la mitad del error medido.
- Las extension classes traen su definición lógica en sintaxis Manchester dentro del campo `def:`.

Existe además el **CRAFT Shared Task 2019**, con evaluador oficial y baselines publicados: los
números tienen contra qué compararse.

**Una trampa operativa, para cuando se corra el pipeline entero sobre este par:**
`seed_ontology` tiene que apuntar a `ontology/cl-base.owl` y **no** al `.obo` — rdflib no parsea
OBO; el `.owl` trae 123.864 tripletas y 7.159 clases. El `.obo` lo lee `calibration.py` con su
propio reader, que es otra cosa. Consecuencia: `CL+extensions.obo` **no tiene equivalente en
OWL**, así que la semilla del pipeline no incluye las extension classes que el banco sí usa. Hay
que decidir si eso importa antes de leer los números.

### Segundo: MaterioMiner (verificado el 2026-09-09)

428 clases, 4 publicaciones de mecánica de materiales y fatiga, 2.229 menciones gold anotadas por
tres expertos contra la Materials Mechanics Ontology. Entra por dos razones y ninguna es que sea
más rica que CL: es el **único dominio no biomédico** del conjunto, y con 428 clases es el
análogo más cercano que hay a una semilla chica — mismo orden de inventario, misma profundidad
corta. Es el punto bajo de la curva de tamaño.

Trajo dos cosas al banco: el lector `webanno`, y que `load_targets` lea inventarios en RDF además
de OBO — MaterioMiner publica en Turtle, y hasta entonces el banco sólo sabía leer OBO, con lo
que el inventario daba **cero clases sin decir por qué**.

Detalle en [`calibration/materiominer/NOTA_FASE0.md`](calibration/materiominer/NOTA_FASE0.md).

## Los pares que faltan, y para qué

Un solo par no dice si los umbrales transfieren; tres inventarios de tamaño muy distinto sí. Ésa
es la única pregunta que los pendientes contestan, y no es cuál es el mejor corpus.

| Par | Inventario | Anotaciones | Por qué |
|---|---|---|---|
| **HPO GSC+** | HPO, ~19k clases, definiciones lógicas vía PATO/UBERON | 228 abstracts, ~1.933 anotaciones, ~490 conceptos | Segundo punto limpio, y muy usado como benchmark |
| **CafeteriaFCD / CafeteriaSA** | FoodOn (axiomatizada, ~40k) + SNOMED-CT | ~7.400 y ~4.300 anotaciones FoodOn | El extremo de inventario grande, y dominio no clínico |

Cada uno trae su formato de anotación —standoff propio o brat, no Knowtator—, y por eso el
importador tiene el parser de formato desacoplado del mapeo a `Mention`: agregar un par es
escribir un reader y registrarlo en `READERS`.

## Los descartados, y por qué

| Candidato | Por qué no |
|---|---|
| **MedMentions** | UMLS es un metatesauro, no una ontología axiomatizada (criterio 2), y la escala es otra (criterio 3) |
| **BioNLP-OST Bacteria Biotope** | OntoBiotope: 3.602 conceptos con definiciones y escala perfecta, pero casi todo `is_a` — no pasa el criterio 2 |
| **Manifesto Project** | sin ontología |
| **NCBITaxon, CHEBI, PR** | ver la tabla de módulos: taxonomía pura, o escala equivocada |

**Fuera de biomedicina esto prácticamente no existe.** Lo publicado es tesauro SKOS (AGROVOC,
EuroVoc) o corpus diminuto. MaterioMiner es el mejor caso no biomédico que se encontró, y por eso
entra aunque sea más pobre que CL.

## Lo que ningún par de acá puede medir

`cross_language_always_grey: true` y la elección de un encoder multilingüe son decisiones sin
evidencia detrás, y **los cuatro pares son en inglés**, así que ninguno las toca. La vía existe
—los corpus clínicos en español del BSC contra SNOMED CT— pero pide tramitar una licencia y
subsetear 360k conceptos. Queda registrado en `DEBT-CROSS-LANGUAGE-GREY` de
[`technical_debt.md`](technical_debt.md).
