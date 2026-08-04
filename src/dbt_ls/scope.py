import logging
import re
from dataclasses import dataclass, field
from functools import cached_property, lru_cache
from itertools import chain
from typing import Iterator, Literal

import sqlglot
import sqlglot.expressions as exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokenizer_core import TokenType

from dbt_ls.column import Column

log = logging.getLogger("dbt_ls")

# Placeholder column name for when parsing a mid-edit document
PLACEHOLDER_COLUMN = "__dbt_ls_incomplete__"


@dataclass(frozen=True)
class Position:
    line: int
    column: int

    def __gt__(self, other):
        return (self.line, self.column).__gt__((other.line, other.column))

    def __lt__(self, other):
        return (self.line, self.column).__lt__((other.line, other.column))

    def __ge__(self, other):
        return (self.line, self.column).__ge__((other.line, other.column))

    def __le__(self, other):
        return (self.line, self.column).__le__((other.line, other.column))

    def __eq__(self, other):
        return (self.line, self.column).__eq__((other.line, other.column))


@dataclass(frozen=True)
class Range:
    start: Position
    end: Position

    def position_in_range(self, cur_pos: Position) -> bool:
        if cur_pos >= self.start and cur_pos <= self.end:
            return True
        return False


@dataclass(frozen=True)
class Span:
    """Character offsets, so `text[span.start:span.end]` is the section."""

    start: int
    end: int


def position_from_offset(text: str, offset: int) -> Position:
    """Offset -> Position in Alias's convention: 1-based line, 0-based column."""
    line = text.count("\n", 0, offset) + 1
    column = offset - (text.rfind("\n", 0, offset) + 1)
    return Position(line, column)


def line_number_from_span(text: str, span: Span) -> int:
    return position_from_offset(text, span.start).line


def column_number_from_span(text: str, span: Span) -> int:
    return position_from_offset(text, span.start).column


def span_to_range(text: str, span: Span) -> Range:
    # Range.end is inclusive, span.end is not, hence the -1.
    last = max(span.start, span.end - 1)
    return Range(
        position_from_offset(text, span.start), position_from_offset(text, last)
    )


BLOCK_TYPES = (exp.Select, exp.CTE, exp.Subquery)


def query_blocks(ast: exp.Expr) -> list[exp.Expr]:
    """Every query block in the tree, outermost and nested alike."""
    return list(ast.find_all(*BLOCK_TYPES))


def block_span(node: exp.Expr) -> Span | None:
    """Span of a query block, minus the WITH clause of a top-level SELECT.

    `WITH a AS (...) SELECT ...` parses as one Select that carries the CTEs,
    so its raw span starts at the top of the file. Left in, the main body
    would look like it begins before the CTEs it follows, and everything after
    the last CTE would be attributed to that CTE instead.
    """
    if isinstance(node, exp.Select):
        # sqlglot renamed the arg `with` -> `with_`; accept either.
        with_clause = node.args.get("with_") or node.args.get("with")
    else:
        with_clause = None
    return node_span(node, exclude=[with_clause] if with_clause else [])


def node_span(node, exclude=()) -> Span | None:
    """Span of a subtree = min(start)..max(end)+1 over descendants that carry offsets."""
    exclude_ids = {id(x) for e in exclude for x in e.walk()}
    starts, ends = [], []
    for n in node.walk():
        if id(n) in exclude_ids:
            continue
        if "start" in n.meta and "end" in n.meta:
            starts.append(n.meta["start"])
            ends.append(n.meta["end"])
    return (
        Span(min(starts), max(ends) + 1) if starts else None
    )  # +1: sqlglot end is inclusive


DIALECT_BY_PROFILE_TYPE = {
    "duckdb": "duckdb",
    "postgres": "postgres",
    "redshift": "redshift",
    "mysql": "mysql",
    "sqlserver": "tsql",
    "synapse": "tsql",
    "fabric": "tsql",
    "spark": "spark",
    "databricks": "databricks",
    "athena": "trino",
    "glue": "trino",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "trino": "trino",
}


def dialect_for_profile_type(profile_type: str | None) -> str:
    if not profile_type:
        return "postgres"
    return DIALECT_BY_PROFILE_TYPE.get(profile_type.lower(), "postgres")


@lru_cache(maxsize=None)
def dbt_dialect(name: str):
    """A jinja-tolerant subclass of the named sqlglot dialect.

    `{{ ... }}` becomes a single raw-string token usable where an identifier is
    expected, and `{% ... %}` is treated as a comment so control blocks are
    skipped.
    """
    base = Dialect.get_or_raise(name).__class__

    class DbtDialect(base):  # type: ignore[valid-type,misc]
        class Tokenizer(base.tokenizer_class):  # type: ignore[name-defined]
            RAW_STRINGS = [
                *base.tokenizer_class.RAW_STRINGS,
                ("{{", "}}"),
                ("{{-", "}}"),
            ]
            COMMENTS = [
                *base.tokenizer_class.COMMENTS,
                ("{%", "%}"),
                ("{%-", "%}"),
            ]

        class Parser(base.parser_class):  # type: ignore[name-defined]
            ID_VAR_TOKENS = {
                *base.parser_class.ID_VAR_TOKENS,
                TokenType.RAW_STRING,
            }

    return DbtDialect


@dataclass
class QueryColumn:
    """One entry of a SELECT list.

    `name` is the column as written upstream, `alias` the name the query
    exposes it under — they differ whenever the select is aliased.
    """

    name: str
    ref_alias: str | None
    alias: str | None
    data_type: str = field(default_factory=str)  # In case of casting

    def __post_init__(self):
        if not self.ref_alias:
            self.ref_alias = self.name


@dataclass
class QueryRef:
    """A relation in FROM/JOIN, with the selected columns attributed to it."""

    table: str
    ref_type: Literal["from", "join"]
    alias: str = field(default_factory=str)
    columns: list[QueryColumn] = field(default_factory=list)
    is_cte: bool = False


@dataclass
class CTERef:
    name: str
    tables: list[QueryRef] = field(default_factory=list)
    selects: list[QueryColumn] = field(default_factory=list)

    @property
    def columns(self) -> tuple[Column, ...]:
        """What the CTE exposes downstream, under the names it exposes them as.

        Lets a CTERef stand in wherever a Model or SourceTable does as a pool
        of completable columns.
        """
        return tuple(
            Column(name=c.alias or c.name, data_type=c.data_type or None)
            for c in self.selects
            if (c.alias or c.name) != PLACEHOLDER_COLUMN
        )


@dataclass
class ModelRef:
    name: str
    tables: list[QueryRef] = field(default_factory=list)
    selects: list[QueryColumn] = field(default_factory=list)

    @property
    def columns(self) -> tuple[Column, ...]:
        return tuple(
            Column(name=c.alias or c.name, data_type=c.data_type or None)
            for c in self.selects
            if (c.alias or c.name) != PLACEHOLDER_COLUMN
        )


def table_ref(node: exp.Expr, ref_type: Literal["from", "join"]) -> QueryRef | None:
    """QueryRef for a relation in FROM/JOIN. None for anything but a plain table.

    A jinja table parses as an Identifier holding the raw `{{ ... }}` body, so
    the name still carries the padding the tokenizer kept: " ref('albums') ".
    """
    if not isinstance(node, exp.Table):
        return None
    return QueryRef(table=node.name.strip(), ref_type=ref_type, alias=node.alias)


def parse_cte(cte: exp.CTE) -> CTERef:
    select = cte.this
    cte_ref = CTERef(name=cte.alias)

    # QueryRefs, in source order: FROM first, then each JOIN.
    # sqlglot renamed the arg `from` -> `from_`; accept either.
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        if (ref := table_ref(from_clause.this, "from")) is not None:
            cte_ref.tables.append(ref)
    for join in select.args.get("joins") or []:
        if (ref := table_ref(join.this, "join")) is not None:
            cte_ref.tables.append(ref)

    # A table with no alias is qualified by its own name instead.
    by_alias = {t.alias or t.table: t for t in cte_ref.tables}
    # An unqualified column is only attributable when there is one candidate.
    sole_table = cte_ref.tables[0] if len(cte_ref.tables) == 1 else None

    for col in select.selects:
        inner = col.unalias()
        out = col.alias_or_name
        if isinstance(inner, exp.Column):
            tbl, name = inner.table or None, inner.name
            owner = by_alias.get(tbl) if tbl else sole_table
        elif isinstance(inner, exp.Star):
            # A bare `*` is every column of every relation in scope, so it is
            # only attributable when there is exactly one to attribute it to.
            tbl, name, owner = None, out, sole_table
        else:
            # Computed selects — literals, calls, CASE — trace back to no
            # single upstream column, so they are exposed but attributed to
            # no table.
            tbl, name, owner = None, out, None

        column = QueryColumn(name=name, ref_alias=tbl, alias=out)
        cte_ref.selects.append(column)
        # The same object, so anything that resolves a type through `tables`
        # is visible to consumers reading `selects`.
        if owner is not None:
            owner.columns.append(column)

    return cte_ref


def parse_ast(ast: exp.Expr, name: str = "") -> ModelRef:
    model_ref = ModelRef(name=name)
    if not isinstance(ast, exp.Select):
        # Only a SELECT carries a column list. Anything else — an INSERT from a
        # run result, or the bare identifier a half-typed buffer parses to —
        # has no model shape to describe, and `.selects` is not there to read.
        return model_ref

    select = ast
    ctes = parse_ctes(ast)

    # QueryRefs, in source order: FROM first, then each JOIN.
    # sqlglot renamed the arg `from` -> `from_`; accept either.
    from_clause = select.args.get("from_") or select.args.get("from")
    if from_clause is not None:
        if (ref := table_ref(from_clause.this, "from")) is not None:
            cte = next(
                (c for c in ctes if c.name.casefold() == ref.table.casefold()), None
            )
            # cte is not None -> the FROM points at a CTE, not a model/source
            ref.is_cte = bool(cte)
            model_ref.tables.append(ref)
    for join in select.args.get("joins") or []:
        if (ref := table_ref(join.this, "join")) is not None:
            cte = next(
                (c for c in ctes if c.name.casefold() == ref.table.casefold()), None
            )
            # cte is not None -> the FROM points at a CTE, not a model/source
            ref.is_cte = bool(cte)
            model_ref.tables.append(ref)

    # A table with no alias is qualified by its own name instead.
    by_alias = {t.alias or t.table: t for t in model_ref.tables}
    # An unqualified column is only attributable when there is one candidate.
    sole_table = model_ref.tables[0] if len(model_ref.tables) == 1 else None

    for col in select.selects:
        inner = col.unalias()
        out = col.alias_or_name
        if isinstance(inner, exp.Column):
            tbl, name = inner.table or None, inner.name
            owner = by_alias.get(tbl) if tbl else sole_table
        elif isinstance(inner, exp.Star):
            # A bare `*` is every column of every relation in scope, so it is
            # only attributable when there is exactly one to attribute it to.
            tbl, name, owner = None, out, sole_table
        else:
            # Computed selects — literals, calls, CASE — trace back to no
            # single upstream column, so they are exposed but attributed to
            # no table.
            tbl, name, owner = None, out, None

        column = QueryColumn(name=name, ref_alias=tbl, alias=out)

        model_ref.selects.append(column)
        # The same object, so anything that resolves a type through `tables`
        # is visible to consumers reading `selects`.
        if owner is not None:
            owner.columns.append(column)

    return model_ref


# TODO: concolidate the two methods above


def parse_ctes(ast: exp.Expr) -> tuple[CTERef, ...]:
    return tuple(parse_cte(cte) for cte in ast.find_all(exp.CTE))


# Possible repairs for parsed sql files.
# Every repair is line-preserving: it adds or removes characters only at the
# end of an existing line, or appends whole lines at EOF.
_TRAILING_CLAUSE = r"FROM|LEFT|RIGHT|INNER|FULL|CROSS|JOIN|WHERE|GROUP|ORDER|HAVING|LIMIT|QUALIFY|WINDOW|UNION"

# `a.` — a column reference whose name has not been typed yet.
_DANGLING_DOT = re.compile(r"\.(?=[ \t]*$)", re.M)
# `SELECT` with its list still empty, the next clause already below it.
_EMPTY_SELECT = re.compile(
    rf"(?im)^([ \t]*SELECT)[ \t]*$(?=\n[ \t]*(?:{_TRAILING_CLAUSE}|\)))"
)
# `a.id,` with the next item not written yet.
_TRAILING_COMMA = re.compile(
    rf"(?i),(?=[ \t]*\n[ \t]*(?:{_TRAILING_CLAUSE}|\)))",
)


def repaired_variants(source: str) -> Iterator[str]:
    """Parseable rewrites of a mid-edit document, most faithful first.

    Lazy so that a document which already parses costs nothing: the caller
    stops at the first candidate that parses.
    """
    repaired = _DANGLING_DOT.sub(f".{PLACEHOLDER_COLUMN}", source)
    repaired = _TRAILING_COMMA.sub("", repaired)
    repaired = _EMPTY_SELECT.sub(rf"\1 {PLACEHOLDER_COLUMN}", repaired)
    if repaired != source:
        yield repaired
    # A WITH whose final SELECT has not been written yet: every CTE-only
    # document is a parse error until the body arrives, which is most of the
    # time spent writing one.
    yield f"{repaired}\nSELECT {PLACEHOLDER_COLUMN}"


@dataclass(frozen=True)
class ParsedDocument:
    """A successful parse, kept together with the text it was parsed from."""

    source: str
    ast: exp.Expr

    @cached_property
    def ctes(self) -> tuple[CTERef, ...]:
        return parse_ctes(self.ast)


class AstCache:
    """Last-known-good AST per document URI."""

    def __init__(self, dialect: str = "postgres"):
        self.dialect = dialect
        self._documents: dict[str, ParsedDocument] = {}

    def refresh(self, uri: str, source: str) -> ParsedDocument | None:
        """Re-parse `source`, repairing it if need be; on failure keep the cache."""
        for candidate in chain((source,), repaired_variants(source)):
            try:
                ast = sqlglot.parse_one(candidate, read=dbt_dialect(self.dialect))
            except Exception:  # noqa: BLE001 — sqlglot raises several unrelated types
                continue
            parsed = ParsedDocument(source=candidate, ast=ast)
            self._documents[uri] = parsed
            return parsed

        log.debug("Parse failed for %s even repaired; keeping previous AST", uri)
        return self._documents.get(uri)

    def get(self, uri: str) -> ParsedDocument | None:
        return self._documents.get(uri)

    def discard(self, uri: str) -> None:
        self._documents.pop(uri, None)
