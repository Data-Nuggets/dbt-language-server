"""The relations a query reads and the columns each one exposes.

A column list is only ever "all of them" when it can be trusted to be: a
warehouse table, a model or source whose columns came from the warehouse or
the catalog, or a query whose every output is named outright. Anything else,
YAML columns above all, gives `None`, and callers treat that as unknown.
"""

import re
from dataclasses import dataclass
from functools import cached_property

import sqlglot.expressions as exp
from sqlglot.optimizer.scope import Scope, build_scope

from dbt_ls.column import Column
from dbt_ls.hover import kind_of, name_of
from dbt_ls.model import Model, WarehouseTable
from dbt_ls.pattern import REF_FULL_RE, SOURCE_FULL_RE
from dbt_ls.scope import PLACEHOLDER_COLUMN
from dbt_ls.source import SourceTable

PLAIN_NAME = re.compile(r"\w+")


def find_source(
    source_name: str,
    table_name: str,
    sources: list[SourceTable],
    undocumented: list[WarehouseTable],
) -> SourceTable | WarehouseTable | None:
    """What `source(source_name, table_name)` reads.

    The declared table; otherwise a warehouse table in the schema of the
    source so named, or in the schema the call names when no source has it.
    """
    wanted_source, wanted_table = source_name.casefold(), table_name.casefold()
    owners = [s for s in sources if s.source_name.casefold() == wanted_source]
    if declared := next((s for s in owners if s.name.casefold() == wanted_table), None):
        return declared
    schemas = {(s.relation_schema or "").casefold() for s in owners} or {wanted_source}
    return next(
        (
            t
            for t in undocumented
            if t.name.casefold() == wanted_table and t.schema.casefold() in schemas
        ),
        None,
    )


@dataclass(frozen=True)
class Known:
    """A relation a query reads: what to call it, and its columns when every
    one of them is known."""

    label: str
    columns: tuple[Column, ...] | None

    @cached_property
    def names(self) -> frozenset[str]:
        return frozenset(c.key for c in self.columns or ())


def _every_column(relation) -> tuple[Column, ...] | None:
    if not relation.columns:
        return None
    # The warehouse lists a table's columns whole; YAML may list a few.
    if isinstance(relation, WarehouseTable) or relation.all_columns:
        return relation.columns
    return None


class Relations:
    """The relations of one document's queries, each worked out once."""

    def __init__(self, models, sources, undocumented):
        self.models = models
        self.sources = sources
        self.undocumented = undocumented
        self._scopes: dict[int, Known] = {}

    def of_source(self, source) -> Known | None:
        """What a FROM/JOIN entry reads: a table, or a CTE or subquery."""
        if isinstance(source, Scope):
            return self.of_scope(source)
        if not isinstance(source, exp.Table):
            # UNNEST, table functions, VALUES: nothing to look up.
            return None
        relation = self._table(source)
        if relation is None:
            return None
        label = f"{kind_of(relation)} {name_of(relation)}"
        return Known(label, _every_column(relation))

    def _table(self, node: exp.Table):
        # A Jinja relation parses as one identifier holding the call.
        text = node.name
        if m := REF_FULL_RE.search(text):
            wanted = m.group("model").casefold()
            return next((r for r in self.models if r.name.casefold() == wanted), None)
        if m := SOURCE_FULL_RE.search(text):
            return find_source(
                m.group("source"), m.group("table"), self.sources, self.undocumented
            )
        if node.db:
            # A hard-coded schema.table: the table the warehouse has there.
            schema, name = node.db.casefold(), node.name.casefold()
            return next(
                (
                    t
                    for t in [*self.sources, *self.undocumented]
                    if (t.relation_schema or "").casefold() == schema
                    and t.relation_name.casefold() == name
                ),
                None,
            )
        return None

    def of_scope(self, scope: Scope) -> Known:
        if id(scope) not in self._scopes:
            self._scopes[id(scope)] = Known(_scope_label(scope), self._outputs(scope))
        return self._scopes[id(scope)]

    def _outputs(self, scope: Scope) -> tuple[Column, ...] | None:
        """The columns a query exposes, when all are known."""
        if scope.union_scopes:
            # A set operation exposes the names of its first query.
            return self.of_scope(scope.union_scopes[0]).columns
        select = scope.expression
        if not isinstance(select, exp.Select):
            return None
        columns: dict[str, Column] = {}
        for projection in select.selects:
            inner = projection.unalias()
            if isinstance(inner, exp.Star):
                expanded = self._star(scope.selected_sources)
            elif isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
                expanded = self._star(
                    {inner.table: scope.selected_sources.get(inner.table)}
                )
            else:
                name = projection.alias_or_name
                if not PLAIN_NAME.fullmatch(name) or name == PLACEHOLDER_COLUMN:
                    # An expression the warehouse names on its own, or Jinja.
                    return None
                expanded = (Column(name, self._type_of(scope, inner)),)
            if expanded is None:
                return None
            for column in expanded:
                # A `*` over a join can name a column twice; the first wins.
                columns.setdefault(column.key, column)
        return tuple(columns.values())

    def _type_of(self, scope: Scope, inner: exp.Expr) -> str | None:
        """A projection's type, where it is plain to see: a cast, or the type
        of the column it passes through."""
        if isinstance(inner, exp.Cast):
            return inner.to.sql()
        if not isinstance(inner, exp.Column):
            return None
        if inner.table:
            source = visible(scope, inner.table)
            holders = [k for k in [self.of_source(source)] if k is not None]
        else:
            holders = self.selected(scope) or []
        for known in holders:
            for column in known.columns or ():
                if column.key == inner.name.casefold():
                    return column.data_type
        return None

    def _star(self, selected: dict) -> tuple[Column, ...] | None:
        columns: list[Column] = []
        for entry in selected.values():
            known = self.of_source(entry[1]) if entry else None
            if known is None or known.columns is None:
                return None
            columns += known.columns
        return tuple(columns)

    def selected(self, scope: Scope) -> list[Known] | None:
        """The relations a scope selects from, or None if any is unknown."""
        found = []
        for _, source in scope.selected_sources.values():
            known = self.of_source(source)
            if known is None or known.columns is None:
                return None
            found.append(known)
        return found


def _scope_label(scope: Scope) -> str:
    parent = scope.expression.parent
    if isinstance(parent, exp.CTE):
        return f"cte {parent.alias}"
    if isinstance(parent, exp.Subquery) and parent.alias:
        return f"subquery {parent.alias}"
    return "subquery"


def visible(scope: Scope, name: str):
    """What `name` qualifies in `scope`: its own sources, then enclosing ones."""
    wanted = name.casefold()
    while scope is not None:
        for sources in (scope.sources, scope.selected_sources):
            for key, entry in sources.items():
                if key.casefold() == wanted:
                    return entry[1] if isinstance(entry, tuple) else entry
        scope = scope.parent
    return None


def names_every_column(ast: exp.Expr) -> bool:
    """Whether a query's final select names each output outright: no `*`,
    whose columns depend on what it reads, and no expression the warehouse
    would name on its own."""
    if not isinstance(ast, exp.Select):
        return False
    for projection in ast.selects:
        inner = projection.unalias()
        if isinstance(inner, exp.Star) or isinstance(inner.args.get("this"), exp.Star):
            return False
        if not PLAIN_NAME.fullmatch(projection.alias_or_name):
            return False
    return True


def model_columns(
    ast: exp.Expr,
    models: list[Model],
    sources: list[SourceTable],
    undocumented: list[WarehouseTable],
) -> tuple[Column, ...] | None:
    """Every column a model's SQL exposes, or None unless all are known.

    Explicit names in the final select are known outright; a `*` there is only
    as known as what it selects from.
    """
    root = build_scope(ast)
    if root is None:
        return None
    return Relations(models, sources, undocumented).of_scope(root).columns
