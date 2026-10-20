"""Problems dbt or the warehouse would stop at, reported while the model is
being written.

dbt-ls never writes YAML itself: a diagnostic says what is missing and where
it belongs, and declaring it is left to the user.

Column checks only speak when they can be sure. A relation whose columns came
from YAML alone may document only some of them, and a `*` over such a
relation hides the rest, so either one silences every check that would need
its full column list.
"""

import os
import re

import sqlglot.expressions as exp
from lsprotocol import types
from sqlglot.optimizer.scope import Scope, traverse_scope

from dbt_ls.model import Model, WarehouseTable
from dbt_ls.pattern import SOURCE_FULL_RE
from dbt_ls.relations import PLAIN_NAME, Relations, find_source, visible
from dbt_ls.scope import PLACEHOLDER_COLUMN, ParsedDocument, neutralise_jinja
from dbt_ls.source import SourceTable

SOURCE = "dbt-ls"
UNDECLARED = "undeclared-source"
UNKNOWN_REF = "unknown-ref"
UNKNOWN_COLUMN = "unknown-column"
AMBIGUOUS_COLUMN = "ambiguous-column"
UNKNOWN_ALIAS = "unknown-alias"

Error = types.DiagnosticSeverity.Error
Warning = types.DiagnosticSeverity.Warning

# dbt never renders what is inside a Jinja comment, so neither is checked.
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
# Only the one-argument form: `ref('package', 'model')` and `ref('m', v=2)`
# name things this check does not model.
SINGLE_REF_RE = re.compile(r"""\bref\(\s*(['"])(?P<model>\w+)\1\s*\)""")

Problem = tuple[str, types.DiagnosticSeverity, str]


def diagnose(text: str, parsed: ParsedDocument | None, state) -> list[types.Diagnostic]:
    """Everything wrong with `text` that dbt-ls can tell.

    Columns need the AST, and only one parsed from this very text: a repaired
    or older parse would put the diagnostics in the wrong place.
    """
    found = source_diagnostics(text, state.sources, state.undocumented, state.dbt_root)
    found += ref_diagnostics(text, state.ref_names | {m.name for m in state.models})
    if parsed is not None and parsed.source == neutralise_jinja(text):
        found += column_diagnostics(
            text, parsed.ast, state.models, state.sources, state.undocumented
        )
    return found


def _position(text: str, offset: int) -> types.Position:
    line = text.count("\n", 0, offset)
    return types.Position(
        line=line, character=offset - (text.rfind("\n", 0, offset) + 1)
    )


def _diagnostic(text: str, start: int, end: int, problem: Problem) -> types.Diagnostic:
    message, severity, code = problem
    return types.Diagnostic(
        range=types.Range(start=_position(text, start), end=_position(text, end)),
        message=message,
        severity=severity,
        source=SOURCE,
        code=code,
    )


def _without_comments(text: str) -> str:
    """Jinja comments blanked out, keeping every offset where it was."""
    return JINJA_COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)


# ---------------------------------------------------------------- source()


def undeclared_message(
    source_name: str,
    table_name: str,
    sources: list[SourceTable],
    undocumented: list[WarehouseTable],
    dbt_root: str,
) -> tuple[str, types.DiagnosticSeverity] | None:
    """Why dbt would not compile `source(source_name, table_name)`, if it would not.

    A table the warehouse has is a warning: it is only waiting to be declared.
    One nothing knows about is more likely a typo, so it is an error.
    """
    found = find_source(source_name, table_name, sources, undocumented)
    if isinstance(found, SourceTable):
        return None

    owners = [s for s in sources if s.source_name.casefold() == source_name.casefold()]
    if owners:
        where = next((s.path for s in owners if s.path), None)
        message = f"Source '{source_name}' has no table '{table_name}'."
        if where:
            message += (
                f" Add it under '{source_name}' in {os.path.relpath(where, dbt_root)}."
            )
    else:
        message = f"No .yml file declares source '{source_name}'."
    if found is None:
        return message, Error
    message += f" The warehouse has it as {found.schema}.{found.name}."
    return message, Warning


def source_diagnostics(
    text: str,
    sources: list[SourceTable],
    undocumented: list[WarehouseTable],
    dbt_root: str,
) -> list[types.Diagnostic]:
    """One diagnostic for every `source()` call dbt would not compile."""
    diagnostics = []
    for m in SOURCE_FULL_RE.finditer(_without_comments(text)):
        problem = undeclared_message(
            m.group("source"), m.group("table"), sources, undocumented, dbt_root
        )
        if problem is not None:
            # From the quote before the source to the one after the table.
            start, end = m.start("source") - 1, m.end("table") + 1
            diagnostics.append(_diagnostic(text, start, end, (*problem, UNDECLARED)))
    return diagnostics


# ------------------------------------------------------------------- ref()


def ref_diagnostics(text: str, names: set[str]) -> list[types.Diagnostic]:
    """One error for every `ref()` naming no model, seed or snapshot."""
    known = {n.casefold() for n in names}
    return [
        _diagnostic(
            text,
            m.start("model") - 1,
            m.end("model") + 1,
            (
                f"No model, seed or snapshot is named '{m.group('model')}'.",
                Error,
                UNKNOWN_REF,
            ),
        )
        for m in SINGLE_REF_RE.finditer(_without_comments(text))
        if m.group("model").casefold() not in known
    ]


# ----------------------------------------------------------------- columns


def _using(scope: Scope) -> set[str]:
    """Columns joined with USING, which name one column, not two."""
    joins = scope.expression.args.get("joins") or []
    return {
        ident.name.casefold()
        for join in joins
        for ident in join.args.get("using") or []
        if isinstance(ident, (exp.Identifier, exp.Column))
    }


def _qualified(
    relations: Relations, scope: Scope, column: exp.Column
) -> Problem | None:
    qualifier, name = column.table, column.name
    source = visible(scope, qualifier)
    if source is None:
        # `s.field` can be a field of a struct column `s`; only a qualifier
        # that is no column of any relation in scope is certainly unknown.
        known = relations.selected(scope)
        if known is None or any(qualifier.casefold() in k.names for k in known):
            return None
        return (
            f"No table is named or aliased '{qualifier}' here.",
            Error,
            UNKNOWN_ALIAS,
        )
    known = relations.of_source(source)
    if known is None or known.columns is None or name.casefold() in known.names:
        return None
    return f"Column '{name}' is not in {known.label}.", Warning, UNKNOWN_COLUMN


def _bare(relations: Relations, scope: Scope, column: exp.Column) -> Problem | None:
    name = column.name.casefold()
    select = scope.expression
    # A select-list alias used later in the same query (WHERE, GROUP BY):
    # several warehouses allow it.
    if isinstance(select, exp.Select) and name in {
        p.alias.casefold() for p in select.selects if isinstance(p, exp.Alias)
    }:
        return None
    known = relations.selected(scope)
    if not known:
        return None
    holders = [k for k in known if name in k.names]
    if len(holders) == 1:
        return None
    if holders:
        if name in _using(scope):
            return None
        both = " and ".join(k.label for k in holders)
        return (
            f"Column '{column.name}' is ambiguous: {both} both have it.",
            Warning,
            AMBIGUOUS_COLUMN,
        )
    # A subquery can read the columns of the query around it.
    outer = scope
    while outer.is_subquery and outer.parent is not None:
        outer = outer.parent
        around = relations.selected(outer)
        if around is None or any(name in k.names for k in around):
            return None
    labels = ", ".join(k.label for k in known)
    return f"Column '{column.name}' is in none of: {labels}.", Warning, UNKNOWN_COLUMN


def column_diagnostics(
    text: str,
    ast: exp.Expr,
    models: list[Model],
    sources: list[SourceTable],
    undocumented: list[WarehouseTable],
) -> list[types.Diagnostic]:
    """Columns the relations they are read from do not have."""
    relations = Relations(models, sources, undocumented)
    diagnostics = []
    for scope in traverse_scope(ast):
        for column in scope.columns:
            if (
                not PLAIN_NAME.fullmatch(column.name)
                or column.name == PLACEHOLDER_COLUMN
            ):
                continue
            check = _qualified if column.table else _bare
            problem = check(relations, scope, column)
            meta = column.this.meta
            if problem is not None and "start" in meta:
                # sqlglot's end offset is inclusive.
                start, end = meta["start"], meta["end"] + 1
                diagnostics.append(_diagnostic(text, start, end, problem))
    return diagnostics
