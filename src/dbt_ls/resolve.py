import logging
from copy import deepcopy
from typing import Callable, Iterable

from dbt_ls.column import HasColumns
from dbt_ls.model import Model
from dbt_ls.pattern import REF_FULL_RE, SOURCE_FULL_RE
from dbt_ls.scope import CTERef
from dbt_ls.source import SourceTable

log = logging.getLogger("dbt_ls")

RelationLookup = Callable[[str], HasColumns | None]


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


def resolve_cte(cte: CTERef, lookup: RelationLookup) -> CTERef:
    """A copy of `cte` with each column's type taken from its upstream relation."""
    resolved = deepcopy(cte)
    for table in resolved.tables:
        relation = lookup(table.table)
        if relation is None:
            log.debug("No relation found for %r in CTE %r", table.table, cte.name)
            continue
        for query_column in table.columns:
            if (column := relation.column(query_column.name)) is not None:
                query_column.data_type = column.data_type or ""
    return resolved


def resolve_ctes(
    ctes: Iterable[CTERef],
    models: Iterable[Model],
    sources: Iterable[SourceTable],
) -> tuple[CTERef, ...]:
    lookup = relation_lookup(models, sources)
    return tuple(resolve_cte(cte, lookup) for cte in ctes)
