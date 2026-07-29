import logging
from dataclasses import dataclass
from functools import lru_cache

import sqlglot
import sqlglot.expressions as exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokenizer_core import TokenType

log = logging.getLogger("dbt_ls")


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


def query_blocks(ast: exp.Expression) -> list[exp.Expression]:
    """Every query block in the tree, outermost and nested alike."""
    return list(ast.find_all(*BLOCK_TYPES))


def block_span(node: exp.Expression) -> Span | None:
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


@dataclass(frozen=True)
class ParsedDocument:
    """A successful parse, kept together with the text it was parsed from."""

    source: str
    ast: exp.Expression


class AstCache:
    """Last-known-good AST per document URI."""

    def __init__(self, dialect: str = "postgres"):
        self.dialect = dialect
        self._documents: dict[str, ParsedDocument] = {}

    def refresh(self, uri: str, source: str) -> ParsedDocument | None:
        """Re-parse `source`; on failure keep whatever was cached before."""
        try:
            ast = sqlglot.parse_one(source, read=dbt_dialect(self.dialect))
        except (
            Exception
        ) as exc:  # noqa: BLE001 — sqlglot raises several unrelated types
            log.debug("Parse failed for %s (%s); keeping previous AST", uri, exc)
            return self._documents.get(uri)

        parsed = ParsedDocument(source=source, ast=ast)
        self._documents[uri] = parsed
        return parsed

    def get(self, uri: str) -> ParsedDocument | None:
        return self._documents.get(uri)

    def discard(self, uri: str) -> None:
        self._documents.pop(uri, None)
