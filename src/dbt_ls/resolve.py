import logging
from copy import deepcopy
from typing import Callable, Iterable, TypeVar

from dbt_ls.column import Column, HasColumns
from dbt_ls.model import Model
from dbt_ls.pattern import REF_FULL_RE, SOURCE_FULL_RE
from dbt_ls.scope import CTERef, ModelRef, QueryColumn, QueryRef
from dbt_ls.source import SourceTable

log = logging.getLogger("dbt_ls")

RelationLookup = Callable[[str], HasColumns | None]

# What resolve_cte can resolve: anything shaped like a query block, i.e.
# carrying `tables` and `selects`.
Resolvable = TypeVar("Resolvable", CTERef, ModelRef)


def relation_lookup(
    models: Iterable[Model], sources: Iterable[SourceTable]
) -> RelationLookup:
    """Resolve the raw text of a FROM/JOIN to the relation it names.
    ref('albums') -> 'albums'
    """
    models_by_name = {m.name.casefold(): m for m in models}
    # Keyed by pair: the same table name can appear under more than one source.
    sources_by_name = {
        (s.source_name.casefold(), s.name.casefold()): s for s in sources
    }

    def lookup(table: str) -> HasColumns | None:
        if m := REF_FULL_RE.search(table):
            return models_by_name.get(m.group("model").casefold())
        if m := SOURCE_FULL_RE.search(table):
            key = (m.group("source").casefold(), m.group("table").casefold())
            return sources_by_name.get(key)
        return None

    return lookup


class ColumnPool(HasColumns):
    """A bare relation, for standing a CTE's resolved columns in as an upstream."""

    def __init__(self, columns: tuple[Column, ...]):
        self.columns = columns


def compiled_relation_lookup(
    models: Iterable[Model],
    sources: Iterable[SourceTable],
    ctes: Iterable[CTERef] = (),
) -> RelationLookup:
    """Resolve a FROM/JOIN in *compiled* SQL, where jinja is already rendered:
    'dbtlsgluedatabase.int_cards' -> the int_cards model."""
    jinja = relation_lookup(models, sources)
    by_name: dict[str, HasColumns] = {m.name.casefold(): m for m in models}
    for s in sources:
        by_name.setdefault(s.name.casefold(), s)
    cte_by_name = {c.name.casefold(): c for c in ctes}

    def lookup(table: str) -> HasColumns | None:
        if (hit := jinja(table)) is not None:  # so it still works on raw model SQL
            return hit
        key = table.split(".")[-1].strip('"`[] ').casefold()
        if key in cte_by_name:
            return ColumnPool(cte_by_name[key].columns)
        return by_name.get(key)

    return lookup


def splice(
    columns: list[QueryColumn], target: QueryColumn, replacement: list[QueryColumn]
) -> None:
    """Swap `target` for `replacement` in place, keeping its position.

    By identity, not equality: two selects can compare equal without being the
    same entry, and only the one actually shared with the table is meant here.
    """
    for i, column in enumerate(columns):
        if column is target:
            columns[i : i + 1] = replacement
            return


def expand_star(
    query_column: QueryColumn, table: QueryRef, relation: HasColumns
) -> list[QueryColumn]:
    """`a.*` -> one QueryColumn per column of the relation `a` is bound to."""
    return [
        QueryColumn(
            name=column.name,
            ref_alias=table.alias or None,
            alias=column.name,
            data_type=column.data_type or "",
        )
        for column in relation.columns
    ]


def resolve_cte(cte: Resolvable, lookup: RelationLookup) -> Resolvable:
    """A copy of `cte` with each column's type taken from its upstream relation."""
    resolved = deepcopy(cte)
    for table in resolved.tables:
        relation = lookup(table.table)
        if relation is None:
            log.debug("No relation found for %r in CTE %r", table.table, cte.name)
            continue
        for query_column in list(table.columns):
            if query_column.name == "*":
                expanded = expand_star(query_column, table, relation)
                # Spliced into both views, because the star is one shared
                # object and expanding it changes how many columns there are.
                splice(table.columns, query_column, expanded)
                splice(resolved.selects, query_column, expanded)
            elif (column := relation.column(query_column.name)) is not None:
                query_column.data_type = column.data_type or ""
    return resolved


def resolve_ctes(
    ctes: Iterable[CTERef],
    models: Iterable[Model],
    sources: Iterable[SourceTable],
) -> tuple[CTERef, ...]:
    lookup = relation_lookup(models, sources)
    return tuple(resolve_cte(cte, lookup) for cte in ctes)
