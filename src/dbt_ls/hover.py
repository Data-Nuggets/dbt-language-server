"""Markdown shown on hover: what a ref, a source or an `alias.column` points at.
"""

import os

from dbt_ls.column import Column
from dbt_ls.model import Model, WarehouseTable
from dbt_ls.scope import CTERef
from dbt_ls.source import SourceTable

# A wide warehouse table would fill the screen; the rest are named by count.
MAX_COLUMNS = 40
# Past this, one long name would push every type off to the right.
MAX_NAME_WIDTH = 32

NO_COLUMNS = (
    "No columns known yet. They come from YAML, `target/catalog.json` "
    "or the warehouse."
)
RULE = "---"


def sql(text: str) -> str:
    return f"```sql\n{text}\n```"


KINDS = {
    Model: "model",
    SourceTable: "source",
    WarehouseTable: "warehouse table",
    CTERef: "cte",
}


def kind_of(relation) -> str:
    return KINDS[type(relation)]


def name_of(relation) -> str:
    """As `source()` names a table: a source's name, or a warehouse table's schema."""
    if isinstance(relation, (SourceTable, WarehouseTable)):
        return f"{relation.source_name}.{relation.name}"
    return relation.name


def details(relation, *, dbt_root: str) -> list[str]:
    """Where the relation lives, one Markdown paragraph per fact."""
    if isinstance(relation, Model):
        lines = []
        if relation.path:
            lines.append(f"`{os.path.relpath(relation.path, dbt_root)}`")
        configured = [
            f"{label} `{value}`"
            for label, value in (
                ("database", relation.database),
                ("schema", relation.schema),
                ("alias", relation.alias),
            )
            if value
        ]
        if configured:
            lines.append("configured with " + " · ".join(configured))
        return lines
    if isinstance(relation, WarehouseTable):
        return [
            "Not in sources.yml: dbt will not compile "
            f"`source('{relation.source_name}', '{relation.name}')` "
            "until it is declared."
        ]
    if isinstance(relation, SourceTable):
        parts = (relation.database, relation.relation_schema, relation.relation_name)
        return [f"reads `{'.'.join(p for p in parts if p)}`"]
    return ["defined in this file"]


def column_block(columns: tuple[Column, ...]) -> str:
    if not columns:
        return NO_COLUMNS
    shown = columns[:MAX_COLUMNS]
    width = min(max(len(c.name) for c in shown), MAX_NAME_WIDTH)
    block = sql(
        "\n".join(
            f"{c.name:<{width}}  {c.data_type}".rstrip() if c.data_type else c.name
            for c in shown
        )
    )
    if len(shown) < len(columns):
        count = f"first {len(shown)} of {len(columns)} columns"
    else:
        count = "1 column" if len(columns) == 1 else f"{len(columns)} columns"
    return f"{block}\n\n*{count}*"


def relation_hover(relation, *, dbt_root: str) -> str:
    head = sql(f"({kind_of(relation)}) {name_of(relation)}")
    body = details(relation, dbt_root=dbt_root)
    return "\n\n".join([head, RULE, *body, column_block(relation.columns)])


def column_hover(name: str, relation) -> str:
    owner = f"{kind_of(relation)} `{name_of(relation)}`"
    column = next((c for c in relation.columns if c.key == name.casefold()), None)
    if column is None:
        if not relation.columns:
            note = f"from {owner}, which has no known columns. {NO_COLUMNS}"
        else:
            count = len(relation.columns)
            note = f"**Not found** among the {count} known columns of {owner}."
        return "\n\n".join([sql(f"(column) {name}"), RULE, note])
    signature = f"(column) {column.name}"
    if column.data_type:
        signature += f" {column.data_type}"
    return "\n\n".join([sql(signature), RULE, f"from {owner}"])
