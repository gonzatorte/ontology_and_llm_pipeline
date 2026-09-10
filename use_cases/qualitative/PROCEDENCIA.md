# Metodología cualitativa — el corpus de trabajo

El par sobre el que corre el pipeline por defecto: papers de política de ciencia abierta contra
una ontología semilla de metodología cualitativa, escrita a mano.

**No se distribuye.** Los dos artefactos viven fuera del repositorio y acá hay sólo dos symlinks,
que el `.gitignore` deja afuera como a cualquier otro dato de caso de uso:

```
corpus       -> ../../../Corpus-08052026/General/files
ontology.rdf -> ../../../qualitative_ontology.rdf
```

Está acá y no apuntado desde `config/default.yaml` a media raíz de distancia **por uniformidad**:
todo par (corpus, ontología) vive bajo `use_cases/`, se llame como se llame, y qué se versiona de
él lo decide una sola regla. Clonar el repo sin esos dos artefactos deja los symlinks rotos, que
es la falla correcta — el pipeline dice que no encuentra el corpus en vez de correr sobre nada.

## No tiene `use_case.yml`, y es a propósito

Un `use_case.yml` describe un corpus **anotado**: dónde están los documentos, en qué formato
están sus anotaciones gold, y contra qué ontología se anotaron. Este par no tiene anotaciones
gold, así que un descriptor sería un archivo que `load_use_case` no puede cargar. Escribir uno
igual sería peor que no tenerlo: haría creer que se lo puede pasar a `calibrate`.

Lo que sí tiene es un **conjunto de retención** —documentos parseados y anotados a mano que nunca
entran al proceso— y eso vive en el almacén, no acá. Ver `EVAL-PIPELINE` y el comando
`hold-out`.

## Está retirado como instrumento

El proyecto no tiene dominio comprometido: el entregable es el sistema y su caracterización *a
través* de casos de uso (`SCOPE-PURPOSE`). Este par fue el andamio inicial, para tener con qué
probar mientras no existían los publicados, y **está retirado** — medido: el corpus habla *sobre*
investigación en vez de *reportar* estudios cualitativos, así que una tasa de falsos huérfanos
medida acá no da mala, da **sin significado** (`FINDINGS-MEASURED-PAIR-MISMATCH`,
`FINDINGS-MEASURED-QUALITATIVE-PAIR`).

Sigue siendo el default de `config/default.yaml` porque es sobre lo que está construido el
almacén de `data/`, no porque sea un destino.
