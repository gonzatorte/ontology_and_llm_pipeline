# Cómo trabajar en este repositorio

## Poner el proyecto en marcha

```bash
uv sync --extra dev --extra reasoning --extra matching --extra validation
uv run pytest -q                       # la suite entera, sin red ni Docker
uv run ruff check .                    # line-length 100, reglas E,F,I,UP,B
./scripts/fetch-jars.sh                # OWL API + ELK + HermiT en lib/ (Java 11+)
```

Los tests no necesitan red, ni Docker, ni JVM, ni credencial, ni servidor de base: los que
dependen de algo de eso se saltean con un mensaje que dice qué falta.

## El almacén: SQLite o Postgres

Ningún módulo sabe contra qué motor corre. El SQL se escribe con `?` y las filas se leen por
nombre; `store.py` traduce lo poco que difiere entre los dos —el marcador de parámetro, las filas
como mapping, `executescript`, y las dos consultas de introspección—. Todo lo demás se escribe
portable: `COALESCE` y no `IFNULL`, `CASE WHEN` y no `SUM(booleano)`, y el JSON se lee en Python.

`sqlite` es el default y alcanza para una sesión de usuario por vez. `postgres` es lo que admite
**dos escribiendo a la vez**:

```yaml
database:
  backend: postgres
  dsn: postgresql://usuario@host/base
```

```bash
uv sync --extra postgres
docker run -d --rm --name onto-pg -e POSTGRES_PASSWORD=onto -e POSTGRES_USER=onto \
    -e POSTGRES_DB=onto -p 55432:5432 postgres:16-alpine
ONTO_PIPELINE_TEST_DSN=postgresql://onto:onto@127.0.0.1:55432/onto uv run pytest -q
```

`tests/test_store.py` corre **el mismo contrato contra los dos** motores, parametrizado: sin
`ONTO_PIPELINE_TEST_DSN` los casos de Postgres se saltean y la suite sigue sin necesitar
servidor. Si tocás `store.py`, es el archivo que tiene que seguir pasando en verde con la
variable puesta.

**Correrla contra Postgres al menos una vez por cambio de esquema vale la pena.** La primera vez
encontró tres cosas que la suite en SQLite no podía ver: `rowid`, que no existe allá; filas
leídas por posición, que `dict_row` no permite; y un comentario SQL con apóstrofe que
desbalanceaba el separador de sentencias. Está todo en `DEBT-POSTGRES-UNTESTED`.

**Lo que hace falta para correr el pipeline, no para desarrollarlo:** una credencial de proveedor
en un archivo de entorno (`cp example.env opencode.env`, ver `README.md`) y los jars del
razonador. Ninguna de las dos se versiona.

## Qué se versiona y qué no

`data/` y `lib/` son derivados y regenerables. De `use_cases/` se versionan **sólo** el
`use_case.yml` y el `PROCEDENCIA.md` de cada caso de uso: los corpus y las ontologías son
artefactos publicados de terceros que pesan 40 MB, cada nota dice cómo bajarlos, y cada uno tiene
su propia licencia (ver [`NOTICE.md`](NOTICE.md)). Las reglas están en `.gitignore`.

## Varias sesiones a la vez: un worktree cada una

Este repo se escribe desde más de una sesión en paralelo, y **dos sesiones sobre el mismo working
tree ya produjo una falla silenciosa**: una edición anclada a texto exacto falla *abierta* cuando
otra movió el contexto — no rompe, no avisa, y los tests siguen pasando porque prueban otra cosa.
Está registrada en `FINDINGS-SILENT-FAILURES` de [`findings.md`](findings.md).

```bash
git worktree add ../pipeline-<nombre> -b <nombre>
```

Un checkout por sesión y nadie puede pisar a nadie; el desacuerdo aparece al mergear, que es
ruidoso. **El repositorio es el único canal entre sesiones:** lo que tiene que llegar a otra se
commitea, no se deja en el árbol.

## Sesiones de usuario

Casi todo lo que el pipeline produce pertenece a una **sesión**: menciones, versiones, decisiones
y artefactos. Una sesión corre sobre un **caso de uso** —el par (ontología inicial, corpus) de
`use_cases/`—, que es material de entrada y se comparte entre sesiones.

Al escribir una consulta nueva sobre una tabla por sesión, **filtrala**. Olvidarse no rompe nada
visible: devuelve filas de más, o borra las de otra sesión, y los tests de esa etapa siguen
pasando porque corren sobre una sola. `tests/test_session_scope.py` lee el código y falla si
alguna se olvida; si tu consulta es una excepción legítima, el test dice cuáles son y por qué.

## Commits

[Conventional commits](https://www.conventionalcommits.org) en el asunto —
`tipo(alcance): descripción` — y el porqué en el cuerpo. El asunto dice qué se tocó; **el cuerpo
sigue siendo lo que importa**, porque un commit sin cuerpo es un commit sin explicación.

Tipos: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `chore`, `build`. Los alcances son las
partes de este repo y la lista es cerrada: está en [`CLAUDE.md`](CLAUDE.md), junto al resto de
las convenciones.

Commitear después de cada hito, no una tanda al final.

## Los tests dicen por qué

Los nombres son frases y el docstring dice qué decisión de diseño fija el test. Un test que sólo
verifica mecánica está fuera de tono con el resto. Hay un archivo por módulo.

**Y uno de punta a punta**, `tests/test_end_to_end.py`: corpus y ontología inicial adentro,
ontología enriquecida afuera. Sustituye sólo el modelo y el encoder —lo que cuesta plata o
red— y deja real todo lo demás, razonador incluido, así que se saltea sin los jars. Existe porque
los tests de módulo no miran la **composición**, y ahí vivían los bugs más caros: su primera
corrida encontró un historial que perdía eventos. Si agregás una etapa al camino feliz, va ahí.

## Antes de proponer un cambio

Tres lecturas, en este orden:

1. [`main_plan.md`](main_plan.md) — el diseño, con sus decisiones vinculantes. Es la referencia.
2. [`findings.md`](findings.md) — qué se midió, y sobre todo **qué se probó y no funcionó**:
   siete formas de meterle más texto a la comparación están medidas y todas empeoran.
3. [`technical_debt.md`](technical_debt.md) — varias cosas que parecen faltantes son decisiones
   registradas.

Y los **invariantes que no se negocian** están en [`CLAUDE.md`](CLAUDE.md). Cada uno costó un bug
o está en el diseño como decisión; los diez se leen en dos minutos.

## Agregar una etapa

Va en `services/` —una función que recibe un `Workspace` y devuelve un resultado tipado, sin
importar `typer` ni `rich`—, se muestra en `render.py`, y la llaman las dos interfaces: `cli.py`
y `wizard.py`. Hay un test que verifica que ningún servicio importe una interfaz.
