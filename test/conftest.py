from dataclasses import dataclass

from lsprotocol import types
from pygls.workspace import TextDocument

from dbt_ls.column import Column
from dbt_ls.model import Model
from dbt_ls.scope import AstCache
from dbt_ls.server import serve_completions
from dbt_ls.source import SourceTable
from dbt_ls.state import ProjectState

URI = "file:///project/models/demo.sql"
DBT_ROOT = "/project"
CURSOR = "|"


@dataclass
class FakeProject:
    """`ProjectState.__post_init__` only asks the project for `model_paths`."""

    model_paths: tuple[str, ...] = ()


def split_cursor(text: str) -> tuple[str, types.Position]:
    """Strip the `|` marker and return the LSP position it sat at."""
    if CURSOR not in text:
        raise AssertionError(f"no {CURSOR!r} cursor marker in fixture text")
    before, _, after = text.partition(CURSOR)
    line = before.count("\n")
    character = len(before) - (before.rfind("\n") + 1)
    return before + after, types.Position(line=line, character=character)


def model(name: str, *columns: str | tuple[str, str], path: str = "") -> Model:
    """`model("stg_cards", "id", ("mana_cost", "int"))`"""
    return Model(
        name=name,
        path=path or f"{DBT_ROOT}/models/{name}.sql",
        columns=tuple(Column(c) if isinstance(c, str) else Column(*c) for c in columns),
    )


def source(source_name: str, name: str, *columns: str | tuple[str, str]) -> SourceTable:
    return SourceTable(
        name=name,
        source_name=source_name,
        columns=tuple(Column(c) if isinstance(c, str) else Column(*c) for c in columns),
    )


def complete(
    text: str,
    *,
    models: list[Model] | None = None,
    sources: list[SourceTable] | None = None,
    dialect: str = "duckdb",
) -> list[types.CompletionItem] | None:
    """Run completion on `text` at its `|` marker."""
    src, pos = split_cursor(text)

    document = TextDocument(uri=URI, source=src)
    ast_cache = AstCache(dialect)
    # The server keeps the cache in step via did_open/did_change, so by the time
    # completion runs the buffer has always been parsed at least once.
    ast_cache.refresh(URI, src)

    state = ProjectState(
        project=FakeProject(),
        profile_target=None,
        models=models or [],
        sources=sources or [],
        dbt_root=DBT_ROOT,
    )

    return serve_completions(
        state=state, document=document, ast_cache=ast_cache, pos=pos
    )


def labels(items: list[types.CompletionItem] | None) -> list[str]:
    return [i.label for i in items or []]
