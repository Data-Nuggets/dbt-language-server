"""Markdown shown on hover: what a ref, a source or an `alias.column` points at."""

import os

from dbt_ls.column import Column
from dbt_ls.model import Model
from dbt_ls.scope import CTERef
from dbt_ls.source import SourceTable

# A wide warehouse table would fill the screen; the rest are named by count.
MAX_COLUMNS = 40

NO_COLUMNS = "No columns known. They come from YAML, `target/catalog.json` or the warehouse."


def describe(
    relation: Model | SourceTable | CTERef, *, dbt_root: str, undocumented: bool
) -> str:
    """Name, kind and location of a relation."""
    if isinstance(relation, Model):
        lines = [f"**{relation.name}** · model"]
        if relation.path:
            lines.append(f"`{os.path.relpath(relation.path, dbt_root)}`")
        built = [
            f"{label} `{value}`"
            for label, value in (
                ("database", relation.database),
                ("schema", relation.schema),
                ("alias", relation.alias),
            )
            if value
        ]
        if built:
            lines.append("Configured " + ", ".join(built))
        return "\n\n".join(lines)
    if isinstance(relation, SourceTable):
        if undocumented:
            title = f"**{relation.schema}.{relation.name}** · warehouse table, not in sources.yml"
        else:
            title = f"**{relation.source_name}.{relation.name}** · source"
        lines = [title]
        where = ".".join(
            part
            for part in (
                relation.database,
                relation.schema or relation.source_name,
                relation.identifier or relation.name,
            )
            if part
        )
        if not undocumented:
            lines.append(f"`{where}`")
        return "\n\n".join(lines)
    return f"**{relation.name}** · CTE in this file"


def column_table(columns: tuple[Column, ...]) -> str:
    if not columns:
        return NO_COLUMNS
    rows = [f"| `{c.name}` | {c.data_type or ''} |" for c in columns[:MAX_COLUMNS]]
    more = len(columns) - MAX_COLUMNS
    table = "\n".join(["| column | type |", "| --- | --- |", *rows])
    return table + (f"\n\n…and {more} more" if more > 0 else "")


def relation_hover(relation, *, dbt_root: str, undocumented: bool = False) -> str:
    head = describe(relation, dbt_root=dbt_root, undocumented=undocumented)
    return f"{head}\n\n{column_table(relation.columns)}"


def column_hover(name: str, relation, *, undocumented: bool = False) -> str:
    owner = _owner(relation, undocumented)
    column = next((c for c in relation.columns if c.key == name.casefold()), None)
    if column is None:
        if not relation.columns:
            return f"**{name}**\n\nColumn of {owner}. {NO_COLUMNS}"
        return (
            f"**{name}**\n\nNot among the {len(relation.columns)} "
            f"known columns of {owner}."
        )
    data_type = f" `{column.data_type}`" if column.data_type else " (type unknown)"
    return f"**{column.name}**{data_type}\n\nColumn of {owner}"


def _owner(relation, undocumented: bool) -> str:
    if isinstance(relation, Model):
        return f"model `{relation.name}`"
    if isinstance(relation, SourceTable):
        if undocumented:
            return f"warehouse table `{relation.schema}.{relation.name}`"
        return f"source `{relation.source_name}.{relation.name}`"
    return f"CTE `{relation.name}`"
