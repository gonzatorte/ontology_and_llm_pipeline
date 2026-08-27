# MaterioMiner — nota de salida de Fase 0

Segundo caso de uso. Es el punto bajo de la curva de tamaño de inventario y el único dominio no
biomédico del conjunto; por qué entró está en
[`use_case_selection.md`](../../use_case_selection.md).

## Procedencia

| Artefacto | Origen | Versión |
|---|---|---|
| Corpus + anotaciones | `gitlab.cc-asp.fraunhofer.de/iwm-micro-mechanics-public/datasets/materio-miner` | v1.0.1 |
| Ontología | `gitlab.cc-asp.fraunhofer.de/iwm-micro-mechanics-public/ontologies/materials-mechanics-ontology` | archivo `materials-mechanics-ontology.ttl` |
| Archivo alternativo | Fordatis, DOI `10.24406/fordatis/329.2` | — |
| Paper | *Scientific Data* (2024), `10.1038/s41597-024-03926-5`; arXiv 2408.04661 | — |

Licencia **CC-BY 4.0**, tanto de las anotaciones como de las cuatro publicaciones, que son
open-access. Los clones viven en `../_materiominer` y `../_mmo` y no se versionan; se regeneran
con el `archive.tar.gz` de la API de GitLab.

## Qué trae

| | |
|---|---|
| Documentos | 4 publicaciones de mecánica de materiales y fatiga |
| Anotaciones | **2.229** menciones sobre **185** clases distintas, 2 salteadas |
| Ontología | **428** clases, 420 con definición |
| Anotadores | tres expertos de dominio |

El paper reporta 2.191 entidades sobre 179 clases; la diferencia es de conteo, no de datos: acá
se cuentan todos los spans del conjunto *fine-grained* sin filtrar.

## Formato, y lo que hubo que resolver

Las anotaciones vienen en **WebAnno TSV 3.3**, una fila por token con offsets absolutos y la
clase como IRI en la columna `identifier`. Un span de varios tokens se marca con `*[n]` repetido.
El lector es `use_cases.read_webanno`.

**El corpus no incluye los documentos como texto.** Sólo están los tokens con sus posiciones, así
que el texto se reconstruye: cada token se coloca en su offset y los huecos se rellenan. Eso
garantiza `texto[inicio:fin] == token` para los 12.155 tokens —verificado, cero mal ubicados— y
es lo único que tiene que quedar exacto, porque es contra esas posiciones que se mide.

Una consecuencia que costó una corrida entera descubrir: rellenar los huecos **sólo con
espacios** deja el documento sin ninguna estructura, y el chunker no tiene por dónde partirlo.
Los cuatro documentos salían como un bloque de treinta mil caracteres cada uno, la extracción
recibía todo junto y 156 menciones quedaban sin ubicar. Escribiendo el corte de oración en el
hueco previo al primer token de cada una —nunca encima de un token, así los offsets no se
mueven— los mismos documentos dan **476 bloques y 23 chunks**, y las menciones sin ubicar bajan
a 13.

## Primer barrido (2026-09-09)

`onto-pipeline calibrate materiominer -m label --holdout 0.2`

| | MaterioMiner | CRAFT/CL |
|---|---|---|
| Inventario | 428 clases | 3.418 |
| Menciones gold | 2.229 | 8.723 |
| **Separación** | **1,50** | 1,69 |
| **recall@1** | **21,5%** | 68,5% |
| Mejor F1 | 0,277 (umbral 0,75) | 0,774 (umbral 0,90) |

**Lo que dice, y es la pregunta que este caso de uso vino a contestar:** el punto de operación no transfiere, y
no se mueve en la dirección que uno supondría. Un inventario ocho veces más chico no es más
fácil.

Y las dos cifras se separan: **la separación se mantiene, el recall se derrumba.** El encoder
sigue distinguiendo un acierto de un error —por eso hay separación— pero la mayoría de las veces
la clase correcta no está en el primer puesto. O sea que acá el problema **no es el umbral, es la
recuperación**, que es un modo de falla distinto del que muestra CRAFT.

La lectura probable, sin confirmar: en CRAFT la mención es casi literalmente la etiqueta de la
clase —un nombre de célula—, mientras que acá una palabra corriente como `lifetime` tiene que
llegar a `FatigueLifetime`. Es el mismo argumento que sostienen la deuda 19 (meter el contexto en
la comparación) y la 17 (tipado consciente de la jerarquía), ahora con un segundo caso de uso que lo
respalda.
