#!/usr/bin/env python3
"""Subir un caso de uso publicado al almacén, como un upload más.

Los casos de uso viven en `use_cases/<nombre>/` con un `corpus/` y un `ontology.*`, y el
despliegue no tiene ese directorio: la imagen no los lleva —son material de terceros, ver
`NOTICE.md`— y el disco del contenedor es efímero. Este script los pone donde la API los puede
usar, **por el mismo mecanismo que cualquier corpus subido** (`API-UPLOADED-AND-PUBLISHED`): no
tienen trato especial, y se borran como cualquier upload.

    uv run python scripts/seed_use_cases.py craft-cl --config config/default.yaml

Sin nombres sube todos los que tengan corpus. Con `--dry-run` dice qué subiría y no sube nada.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from onto_pipeline import uploads  # noqa: E402 — después de armar el path
from onto_pipeline.config import Config  # noqa: E402
from onto_pipeline.db import open_configured  # noqa: E402
from onto_pipeline.objectstore import open_configured as open_objectstore  # noqa: E402


def files_of(directory: Path) -> dict[str, Path]:
    """Los archivos del caso de uso, con el nombre que van a tener adentro del upload.

    El corpus conserva su estructura relativa porque el id de documento sale de ella: el mismo
    material subido dos veces tiene que dar los mismos ids.
    """
    found = {
        f"{item.relative_to(directory)}": item
        for item in sorted((directory / "corpus").rglob("*"))
        if item.is_file()
    }
    ontology = next(
        (item for item in sorted(directory.glob("ontology.*")) if item.is_file()), None
    )
    if ontology is not None:
        found[ontology.name] = ontology
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help="Casos de uso a subir; vacío es todos")
    parser.add_argument("--config", default="config/default.yaml", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = Config.load(args.config)
    root = config.paths.use_cases_root
    names = args.names or sorted(
        item.name for item in root.iterdir()
        if item.is_dir() and (item / "corpus").is_dir()
    )
    if not names:
        print(f"no hay casos de uso con corpus bajo {root}")
        return 1

    conn = open_configured(config.database, config.paths.work_dir)
    store = open_objectstore(config.storage, config.paths.work_dir)
    for name in names:
        directory = root / name
        found = files_of(directory)
        if not found:
            print(f"{name}: sin archivos, salteado")
            continue
        if args.dry_run:
            print(f"{name}: {len(found)} archivo(s)")
            continue
        upload = uploads.create(
            conn, name=name, filenames=list(found), note=f"caso de uso publicado {name}"
        )
        for filename, source in found.items():
            # `copy_in` y no leer a memoria: un corpus en PDF pesa, y el destino sabe cómo
            # subirlo sin pasarlo entero por acá.
            store.copy_in(upload.key(filename), source)
        print(f"{name}: upload {upload.id} con {len(found)} archivo(s)")
        print(f"  onto-pipeline session new --upload {upload.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
