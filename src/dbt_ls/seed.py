"""Seeds: CSV files dbt loads as tables, `ref()`-able like any model.

A seed is kept as a `Model` with `model_type` "seed", so everything that
handles models — completion, hover, warehouse matching, diagnostics — handles
seeds too. Its header names every column, which is more than YAML can promise
for a model; the types come later, from the catalog or the warehouse.
"""

import csv
import logging
from pathlib import Path

from dbt_ls.column import Column
from dbt_ls.model import Model, SourcedList

log = logging.getLogger("dbt_ls")

SEED = "seed"


def seed_columns(path: str | Path) -> tuple[Column, ...]:
    """The columns a seed's header names, in order."""
    try:
        # utf-8-sig: a spreadsheet's export often starts with a byte order mark.
        with open(path, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        log.warning("Could not read the header of seed %s: %s", path, exc)
        return ()
    return tuple(Column(name.strip()) for name in header if name.strip())


def seed(path: Path) -> Model:
    columns = seed_columns(path)
    return Model(
        name=path.stem,
        path=path,
        columns=columns,
        model_type=SEED,
        all_columns=bool(columns),
    )


def discover_seeds(root: str, seed_paths: list[str]) -> list[Model]:
    return SourcedList(
        [
            seed(p)
            for seed_path in seed_paths
            for p in sorted((Path(root) / seed_path).rglob("*.csv"))
        ],
        source="discover_seeds",
    )
