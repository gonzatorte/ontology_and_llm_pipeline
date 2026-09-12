# La imagen de `onto-pipeline`, sin nada de ningún proveedor adentro (`API-NEUTRAL-CONTAINER`).
#
# **Nunca Alpine.** `reasoning.py` arranca una JVM con jpype y eso pide glibc; musl no alcanza.
# Es el error que parece ahorro de doscientos megas y termina en un contenedor sin razonador.
#
# Dos etapas, y la primera existe por los jars: `scripts/fetch-jars.sh` resuelve el árbol con
# Maven —son decenas de jars y elegirlos a mano da un classpath que falla de a una clase por
# vez— y para eso necesita un JDK y curl, que no tienen por qué viajar en la imagen final.

FROM python:3.12-slim-bookworm AS jars

# Maven del sistema: el script se lo baja solo si falta, pero hacer que el build dependa de que
# un mirror todavía publique una versión vieja es una falla esperando.
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates maven \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY scripts/fetch-jars.sh scripts/
COPY reasoning/ reasoning/
RUN bash scripts/fetch-jars.sh


FROM python:3.12-slim-bookworm AS runtime

# El JRE, no el JDK: la imagen corre la JVM, no compila Java.
RUN apt-get update && apt-get install -y --no-install-recommends \
      default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# jpype busca la JVM por `JAVA_HOME`. Sin esto, el razonador no arranca y la cadena de
# validación reporta SKIPPED — que no es lo mismo que pasar, pero se le parece de lejos.
ENV JAVA_HOME=/usr/lib/jvm/default-java \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# El paquete entra antes de instalar porque hatchling construye la rueda desde `src/`. Es la
# capa cara —torch entra acá— y se invalida con cualquier cambio de código: si el ciclo de build
# molesta, lo que corresponde es una capa previa con sólo las dependencias, no bajar a Alpine.
COPY pyproject.toml ./
COPY src/ src/
# Torch de CPU, explícito y primero. El default de PyPI arrastra las ruedas de CUDA —varios
# gigas— y una tarea de ECS no tiene GPU: es la mitad del tamaño de la imagen, comprada sin nada
# a cambio. Lo que viene después ya lo encuentra satisfecho.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir ".[api,reasoning,matching,validation,postgres]"

COPY config/ config/
COPY scripts/ scripts/
COPY --from=jars /build/lib/ lib/

# La configuración de verdad llega por entorno (`API-ENV-FIRST`): el archivo que viaja acá es el
# de defaults, y lo que cambia en el despliegue son variables — `ONTO_PIPELINE_STORAGE_BACKEND`,
# `ONTO_PIPELINE_STORAGE_BUCKET`, `ONTO_PIPELINE_DATABASE_BACKEND`, `ONTO_PIPELINE_DATABASE_DSN`.
# El token de la API y la credencial del modelo son secretos y nunca están en un archivo.
ENV ONTO_PIPELINE_STORAGE_BACKEND=s3 \
    ONTO_PIPELINE_DATABASE_BACKEND=postgres

EXPOSE 8000
CMD ["onto-pipeline", "serve", "--config", "config/default.yaml"]
