import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from dbt_ls.column import Column

# Directories to skip when walking a project tree: build artifacts, installed
# packages and VCS metadata — notably `.venv`, which ships its own bundled
# dbt_project.yml files inside the dbt package.
IGNORED_DIRS = {"target", ".venv", "venv", ".git", "dbt_packages", "node_modules"}


@dataclass(frozen=True)
class SourceTable:
    name: str
    source_name: str
    database: str | None = None
    schema: str | None = None
    columns: tuple[Column, ...] = ()


def discover_sources(root: str) -> list:
    sources = []
    for p in Path(root).rglob("*.yml"):
        if IGNORED_DIRS.intersection(p.parts) or not p.is_file():
            continue
        if any(part.startswith(".") for part in p.relative_to(root).parts[:-1]):
            continue
        try:
            doc = yaml.safe_load(p.read_text())
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
    return [
        SourceTable(
            name=s.name,
            source_name=s.source_name,
            columns=columns_by_name.get(s.name) or s.columns,
        )
        for s in sources
    ]
