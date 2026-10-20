import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from dbt_ls.column import Column, HasColumns
from dbt_ls import IGNORED_DIRS, load_yaml



@dataclass(frozen=True)
class SourceTable(HasColumns):
    name: str
    source_name: str
    database: str | None = None
    schema: str | None = None
    columns: tuple[Column, ...] = ()
    # The table's real name in the warehouse, when it differs from `name`.
    identifier: str | None = None
    # The .yml file that declares it, to say where a missing table goes.
    path: str | None = field(default=None, compare=False)
    # True when `columns` is every column, as the warehouse or the catalog
    # lists them; YAML often documents only some.
    all_columns: bool = field(default=False, compare=False)

    @property
    def relation_schema(self) -> str:
        """The schema dbt reads it from: the source's name unless YAML says."""
        return self.schema or self.source_name

    @property
    def relation_name(self) -> str:
        return self.identifier or self.name


def declared(table) -> bool:
    """A source from YAML, as opposed to a warehouse table nobody declared."""
    return isinstance(table, SourceTable)


def discover_sources(root: str) -> list:
    sources = []
    for p in Path(root).rglob("*.yml"):
        if IGNORED_DIRS.intersection(p.parts) or not p.is_file():
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        try:
            doc = load_yaml(p.read_text())
        except yaml.YAMLError:
            continue

        if not doc or not isinstance(doc, dict) or "sources" not in doc:
            continue
        for src in doc["sources"]:
            source_name = src.get("name", "")
            for table in src.get("tables", []):
                sources.append(
                    SourceTable(
                        name=table["name"],
                        source_name=source_name,
                        database=src.get("database"),
                        schema=src.get("schema"),
                        columns=tuple(
                            [
                                Column(name=c["name"], data_type=c.get("data_type"))
                                for c in table.get("columns", [])
                            ]
                        ),
                        identifier=table.get("identifier"),
                        path=str(p),
                    )
                )
    return sources


def enrich_sources_from_catalog(
    sources: list[SourceTable], catalog_path: Path
) -> list[SourceTable]:
    path = Path(catalog_path)
    if not path.is_file():
        return sources
    catalog = json.loads(path.read_text())
    catalog_sources = catalog.get("sources", {})
    # Build a lookup: source name -> columns
    columns_by_name: dict[str, tuple[Column, ...]] = {}
    for source in catalog_sources.values():
        if not source.get("unique_id", "").startswith("source."):
            continue
        name = source["metadata"]["name"]
        columns_by_name[name] = tuple(
            Column(name=c["name"], data_type=c.get("type"))
            for c in source.get("columns", {}).values()
        )
    # `replace`, not a new SourceTable: where the source lives must survive,
    # it is what the warehouse lookup goes by.
    return [
        (
            replace(s, columns=columns_by_name[s.name], all_columns=True)
            if columns_by_name.get(s.name)
            else s
        )
        for s in sources
    ]
