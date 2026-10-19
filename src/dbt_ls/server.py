import asyncio
import copy
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from uuid import uuid4

import yaml
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path
from pygls.workspace import TextDocument

from dbt_ls import __version__
from dbt_ls import hover as hover_text
from dbt_ls.alias import Alias, choose_alias, parse_alias_list
from dbt_ls.column import Column
from dbt_ls.exceptions import *
from dbt_ls.model import _DATABASE_METHOD_REGISTRY, Model
from dbt_ls.pattern import (
    completion_context,
    dotted_at,
    ref_at,
    ref_model_at,
    source_at,
)
from dbt_ls.profiles import Profiles, ProfileTarget
from dbt_ls.project import Project
from dbt_ls.report import LoadReport, SourceReport
from dbt_ls.resolve import resolve_ctes
from dbt_ls.scope import (
    AstCache,
    CTERef,
    Position,
    dialect_for_profile_type,
    query_blocks,
)
from dbt_ls.source import IGNORED_DIRS, SourceTable
from dbt_ls.state import ProjectState

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("DBT_LS_LOG_LEVEL", "INFO").upper(),
    force=True,  # tear down pygls' root handler and use ours
)

logging.getLogger("pygls").setLevel(logging.WARNING)
log = logging.getLogger("dbt_ls")


class SchemaSource(str, Enum):
    """A place model column information can be read from."""

    CONFIG = "config"
    CATALOG = "catalog"
    DATABASE = "database"


@dataclass(frozen=True)
class Settings:
    # Ascending priority: the last source that yields columns wins.
    # Absent == disabled, so ("config",) never touches the warehouse.
    schema_sources: tuple[SchemaSource, ...] = tuple(SchemaSource)
    # Where profiles.yml is; None leaves it to DBT_PROFILES_DIR, then the
    # project root, then ~/.dbt.
    profiles_dir: str | None = None


def parse_schema_sources(raw: str) -> tuple[SchemaSource, ...]:
    """'config,database' -> (CONFIG, DATABASE). Raises ValueError on typos."""
    names = [part.strip() for part in raw.split(",") if part.strip()]
    seen: dict[SchemaSource, None] = {}
    for name in names:
        try:
            source = SchemaSource(name.lower())
        except ValueError:
            valid = ", ".join(s.value for s in SchemaSource)
            raise ValueError(
                f"unknown schema source {name!r}; expected one of: {valid}"
            )
        # Re-listing a source moves it later — dedupe keeping the LAST
        # occurrence, since that's the run whose columns actually survive.
        seen.pop(source, None)
        seen[source] = None

    return tuple(seen)


class DbtLanguageServer(LanguageServer):
    """Language server that carries the discovered dbt project on `.state`."""

    def __init__(self):
        super().__init__("dbt-ls", __version__)
        self.state: ProjectState | None = None
        self.settings: Settings = Settings()
        self.ast_cache = AstCache()
        # Counts loads, so a warehouse read that finishes after a newer load
        # has started can tell it is stale.
        self.load_generation = 0
        # The last run_results.json seen, to re-apply after a warehouse read
        # that was still running when it arrived.
        self.run_results_path: str | None = None


server = DbtLanguageServer()


def find_dbt_project_root(root: str) -> str | None:
    # Mirror Neovim's root_markers: first walk UP from the opened path looking
    # for a dbt_project.yml, so opening a subfolder of the project still finds
    # the real root (VS Code sends the opened folder as-is, not the project root).
    start = Path(root).resolve()
    for directory in (start, *start.parents):
        if (directory / "dbt_project.yml").is_file():
            return str(directory)
    # Otherwise search downward — e.g. when a parent of the project was opened.
    for p in start.rglob("dbt_project.yml"):
        if not IGNORED_DIRS.intersection(p.parts):
            return str(p.parent)
    return None


# The extra that installs the driver for each profile type.
EXTRA_BY_PROFILE_TYPE = {
    "duckdb": "duckdb",
    "postgres": "postgres",
    "mysql": "mysql",
    "sqlserver": "sqlserver",
    "spark": "pyspark",
    "databricks": "databricks",
    "athena": "aws",
    "glue": "aws",
    "redshift": "aws",
}


def _resolve_profile_target(
    project: Project, report: LoadReport, profiles_dir: str | None = None
) -> ProfileTarget | None:
    """The active profile target, or None with the reason left on `report`.

    Never raises: a project without a usable profile still has models, sources
    and documented columns to offer, so a profile problem only costs the
    warehouse.
    """
    report.profile_name = project.profile
    if not project.profile:
        report.profile_problem = "dbt_project.yml has no `profile:` key"
        return None

    try:
        profiles = Profiles.locate(project.root, profiles_dir)
    except (OSError, yaml.YAMLError) as exc:
        report.profile_problem = f"could not read profiles.yml: {exc}"
        return None
    dirs, named_by = Profiles.search_dirs(project.root, profiles_dir)
    passed_over = Path(project.root) / "profiles.yml"
    # The lookup is dbt's, but an editor opened in the project makes it easy
    # to expect the file lying there.
    if (
        named_by
        and passed_over.exists()
        and passed_over.parent.resolve() != dirs[0].resolve()
    ):
        report.profile_note = f"{passed_over} is not used: {named_by} is {dirs[0]}"
        log.warning(report.profile_note)
    if profiles is None:
        if named_by:
            report.profile_problem = f"no profiles.yml in {dirs[0]} (from {named_by})"
        else:
            report.profile_problem = f"no profiles.yml in {project.root} or ~/.dbt"
        return None
    report.profile_path = str(profiles.path)

    try:
        target = profiles.resolve(project.profile)
    except (EnvVarError, ProfileTargetError) as exc:
        report.profile_problem = str(exc)
    except (KeyError, TypeError) as exc:
        report.profile_problem = (
            f"profile {project.profile!r} has no usable target in "
            f"{profiles.path} (missing {exc})"
        )
    else:
        report.profile_type = target.type
        return target
    return None


def _missing_dependency(exc: ImportError) -> str:
    # ibis wraps the real ModuleNotFoundError in its own ImportError, so walk
    # the cause chain to find the name of the package actually missing.
    err: BaseException | None = exc
    while err is not None:
        if isinstance(err, ImportError) and err.name:
            return err.name
        err = err.__cause__ or err.__context__
    return "unknown"


def _run_source(source: SchemaSource, state: ProjectState) -> SourceReport:
    """Run one schema source and say how it went. Never raises."""
    target = state.profile_target
    report = SourceReport(source=source.value, outcome="ok")

    if source is SchemaSource.CATALOG:
        catalog_path = Path(state.dbt_root) / "target" / "catalog.json"
        if not catalog_path.is_file():
            report.outcome = "skipped"
            report.detail = "no target/catalog.json (run `dbt docs generate`)"
    elif source is SchemaSource.DATABASE:
        if target is None:
            report.outcome = "failed"
            report.detail = "no profile target to connect with"
        elif target.type not in _DATABASE_METHOD_REGISTRY:
            report.outcome = "skipped"
            report.detail = f"no warehouse reader for profile type {target.type!r}"

    if report.outcome == "ok":
        refresh = {
            SchemaSource.CONFIG: state.refresh_from_config,
            SchemaSource.CATALOG: state.refresh_from_catalog,
            SchemaSource.DATABASE: state.refresh_from_database,
        }[source]
        started = time.perf_counter()
        try:
            refresh()
        except ImportError as exc:
            extra = EXTRA_BY_PROFILE_TYPE.get(target.type if target else "", "all")
            report.outcome = "failed"
            report.detail = (
                f"missing package {_missing_dependency(exc)!r}; "
                f"install dbt-ls[{extra}]"
            )
        except AWSLoginRequiredError:
            report.outcome = "failed"
            report.detail = "AWS login required"
        except Exception as exc:  # noqa: BLE001 — a source must never end the load
            log.exception("%s enrichment failed; continuing without it", source.value)
            report.outcome = "failed"
            report.detail = f"{type(exc).__name__}: {exc}".splitlines()[0]
        report.seconds = time.perf_counter() - started

    if report.outcome == "ok":
        log.info("Finished parsing column info from %s", source.value)
    else:
        log.warning("%s enrichment %s: %s", source.value, report.outcome, report.detail)

    report.models_with_columns = sum(1 for m in state.models if m.columns)
    report.models_with_types = sum(
        1 for m in state.models if any(c.data_type for c in m.columns)
    )
    report.sources_with_columns = sum(1 for s in state.sources if s.columns)
    return report


def _open_project(
    root_path: str | None, profiles_dir: str | None = None
) -> ProjectState:
    """Find the project and what it declares. No schema source has run yet.

    Raises only when there is no project to load. Anything short of that — no
    profile, a missing driver, a warehouse that refuses the connection — is
    recorded on `state.report` and the load goes on with whatever it could get.
    """
    if not root_path:
        log.warning("Initialize received no root_path; skipping project discovery")
        raise NoRootPathError("no workspace folder was opened")

    dbt_root = find_dbt_project_root(root_path)
    if not dbt_root:
        log.warning("No dbt project root found under %s; skipping discovery", root_path)
        raise NoDbtRootError(f"no dbt_project.yml at, above or under {root_path}")
    log.info("Resolved dbt project root: %s (from opened path %s)", dbt_root, root_path)

    try:
        project = Project(dbt_root)
    except (OSError, yaml.YAMLError) as exc:
        raise NoDbtRootError(f"could not read {dbt_root}/dbt_project.yml: {exc}")
    log.info(
        "Project %r uses profile %r", project.config.get("name", ""), project.profile
    )

    report = LoadReport()
    profile_target = _resolve_profile_target(project, report, profiles_dir)
    if profile_target is None:
        log.warning("No profile target: %s", report.profile_problem)
    else:
        log.info("Using profile: %s", profile_target)
    # Without a profile there is no adapter to name the dialect; postgres is
    # the closest thing to plain SQL.
    report.dialect = dialect_for_profile_type(report.profile_type or None)

    # __post_init__ discovers documented models and sources from `dbt_root`.
    state = ProjectState(
        project=project,
        profile_target=profile_target,
        dbt_root=dbt_root,
        report=report,
    )
    log.debug("Finished parsing documented models and sources")
    return state


def _run_sources(state: ProjectState, sources: tuple[SchemaSource, ...]) -> None:
    """Column enrichment, in ascending priority order.

    The last source that yields columns for a model is the one that wins. A
    source left out of `schema_sources` is never run at all.
    """
    for source in sources:
        log.debug("Enriching from %s", source.value)
        state.report.sources.append(_run_source(source, state))


def _load_project(root_path: str | None, settings: Settings) -> ProjectState:
    """Build the whole project state, warehouse included, before returning."""
    state = _open_project(root_path, settings.profiles_dir)
    if not settings.schema_sources:
        log.warning("No schema sources enabled; models will have no column info")
    else:
        log.info(f"Running with source settings: {settings}")
    _run_sources(state, settings.schema_sources)
    return state


def _split_at_warehouse(
    sources: tuple[SchemaSource, ...],
) -> tuple[tuple[SchemaSource, ...], tuple[SchemaSource, ...]]:
    """Sources read from disk first, then the warehouse and all after it.

    Whatever follows the warehouse has to wait for it, or it would lose its
    place in the priority order.
    """
    if SchemaSource.DATABASE not in sources:
        return sources, ()
    at = sources.index(SchemaSource.DATABASE)
    return sources[:at], sources[at:]


def _with_late_sources(
    state: ProjectState, sources: tuple[SchemaSource, ...]
) -> ProjectState:
    """A copy of `state` with `sources` applied; `state` itself is untouched.

    This is the part that runs off the main thread, so it must not change
    anything a completion request could be reading.
    """
    late = copy.copy(state)
    late.models = list(state.models)
    late.sources = list(state.sources)
    late.report = replace(state.report, sources=list(state.report.sources))
    _run_sources(late, sources)
    return late


def load_project(ls: DbtLanguageServer):
    """Load the project; the warehouse is read in the background.

    Everything that comes from disk is in place when this returns, so names
    and documented columns complete at once. Live columns follow when the
    warehouse has answered.
    """
    ls.load_generation += 1
    generation = ls.load_generation
    ls.run_results_path = None

    # Progress report setup
    token = "reload-project" + str(uuid.uuid4())
    reports_progress = bool(
        ls.client_capabilities.window
        and ls.client_capabilities.window.work_done_progress
    )

    if reports_progress:
        ls.work_done_progress.create(token)
        ls.work_done_progress.begin(
            token,
            types.WorkDoneProgressBegin(
                title="Loading dbt project", cancellable=False, percentage=0
            ),
        )

    def finish(message: str):
        if reports_progress:
            ls.work_done_progress.end(token, types.WorkDoneProgressEnd(message=message))

    def finish_with(state: ProjectState):
        problems = state.report.problems
        # Models and sources are loaded either way; a problem only means fewer
        # columns, so it is a warning rather than the old all-or-nothing ❌.
        finish(f"⚠️{problems[0]}" if problems else "Finished")

    settings = ls.settings
    if not settings.schema_sources:
        log.warning("No schema sources enabled; models will have no column info")
    else:
        log.info(f"Running with source settings: {settings}")
    from_disk, from_warehouse = _split_at_warehouse(settings.schema_sources)

    try:
        state = _open_project(ls.workspace.root_path, settings.profiles_dir)
    except (NoRootPathError, NoDbtRootError) as exc:
        finish(f"❌{exc}")
        return
    _run_sources(state, from_disk)
    ls.state = state
    # Parse in the adapter's own dialect now that it is known.
    ls.ast_cache.dialect = state.report.dialect

    if not from_warehouse:
        finish_with(state)
        return

    def adopt(late: ProjectState):
        # Back on the main thread. A reload since then has its own read coming.
        if generation != ls.load_generation:
            finish("Superseded by a newer load")
            return
        ls.state = late
        # A dbt run that finished meanwhile was applied to the old state.
        if ls.run_results_path:
            late.refresh_from_run_results(ls.run_results_path)
        finish_with(late)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop: not running as a server, so there is nobody to keep
        # responsive. Finish the load right here.
        adopt(_with_late_sources(state, from_warehouse))
        return

    if reports_progress:
        ls.work_done_progress.report(
            token,
            types.WorkDoneProgressReport(message="Reading columns from the warehouse"),
        )

    def read_warehouse():
        late = _with_late_sources(state, from_warehouse)
        loop.call_soon_threadsafe(adopt, late)

    # A daemon thread, so a warehouse that never answers cannot keep the
    # server alive after the editor has closed it.
    threading.Thread(target=read_warehouse, name="dbt-ls-warehouse", daemon=True).start()


@server.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def run_results(ls: DbtLanguageServer, params: types.DidChangeWatchedFilesParams):
    for change in params.changes:
        change_path = to_fs_path(change.uri)
        if change_path and change_path.endswith("run_results.json"):
            log.info(f"{change.type}: {to_fs_path(change.uri)}")
            ls.run_results_path = change_path
            if ls.state:
                ls.state.refresh_from_run_results(change_path)


@server.feature(types.INITIALIZED)
def on_initialized(ls: DbtLanguageServer, params: types.InitializedParams):
    # Discovery runs on the `initialized` notification, not the `initialize`
    # request: the client only services server-initiated requests (e.g.
    # window/workDoneProgress/create) once the handshake has completed.
    load_project(ls)
    ls.client_register_capability(
        params=types.RegistrationParams(
            [
                types.Registration(
                    id=str(uuid4()),
                    method=types.WORKSPACE_DID_CHANGE_WATCHED_FILES,
                    register_options=types.DidChangeWatchedFilesRegistrationOptions(
                        watchers=[
                            types.FileSystemWatcher(
                                glob_pattern="**/target/run_results.json",
                            )
                        ]
                    ),
                )
            ]
        )
    )


@server.command("dbt-ls.reload")
def reload(ls: DbtLanguageServer):
    load_project(ls)


@server.command("dbt-ls.current_model")
def current_model(ls: DbtLanguageServer, model_uri):
    if ls.state is None:
        return None
    path = to_fs_path(model_uri)
    candidate_model = [model for model in ls.state.models if path == str(model.path)]
    return (
        {
            "dbt_root": ls.state.dbt_root,
            "exec_path": candidate_model[0].get_exec_path(
                candidate_model[0].path, ls.state.project
            ),
        }
        if candidate_model
        else None
    )


def refresh_ast(ls: DbtLanguageServer, uri: str):
    """Keep the cached AST in step with the buffer.

    Every entry point into the cache is a document event, so completion never
    has to parse: by the time it runs the text is mid-edit and unparseable.
    """
    document = ls.workspace.get_text_document(uri)
    ls.ast_cache.refresh(uri, document.source)


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: DbtLanguageServer, params: types.DidOpenTextDocumentParams):
    refresh_ast(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: DbtLanguageServer, params: types.DidChangeTextDocumentParams):
    refresh_ast(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: DbtLanguageServer, params: types.DidSaveTextDocumentParams):
    refresh_ast(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: DbtLanguageServer, params: types.DidCloseTextDocumentParams):
    ls.ast_cache.discard(params.text_document.uri)


# How a warehouse table without YAML is marked in the completion list.
UNDOCUMENTED = "not in sources.yml"
WAREHOUSE_TABLE = "warehouse table"
# Sorts after every identifier, so what the project declares is offered first.
SORT_LAST = "~"


def _in_schema(source: SourceTable, schema: str) -> bool:
    return (source.schema or source.source_name).casefold() == schema.casefold()


def columns_for(
    declaration: Alias,
    ctes: tuple[CTERef, ...],
    models: list[Model],
    sources: list[SourceTable],
    undocumented: list[SourceTable] = (),
) -> tuple[Column, ...]:
    """Columns of the relation an alias is bound to."""
    found = relation_for(declaration, ctes, models, sources, undocumented)
    return found.columns if found else ()


def relation_for(
    declaration: Alias,
    ctes: tuple[CTERef, ...],
    models: list[Model],
    sources: list[SourceTable],
    undocumented: list[SourceTable] = (),
) -> CTERef | Model | SourceTable | None:
    """The relation an alias is bound to.

    `ref_type` records which pattern produced the alias, so "model" and
    "source" name their pool exactly. "cte" is a guess: TABLE_PATTERN matches
    any identifier after FROM/JOIN, which may just as well be a model, a
    source or a warehouse table referenced without ref()/source() — hence the
    extra pools it falls back to.

    `undocumented` holds warehouse tables the project has no YAML for. They
    come last everywhere: what the project declares wins.
    """
    # parse_alias_list lowercases the document, the pools keep the casing they
    # were discovered with, so the comparison has to be case-insensitive.
    wanted = declaration.ref.casefold()
    qualifier = declaration.qualifier

    def named(pool):
        return [r for r in pool if r.name.casefold() == wanted]

    if declaration.ref_type == "model":
        found = named(models)
    elif declaration.ref_type == "source":
        every = named(sources) + named(undocumented)
        # The source named in the call; any source with that table otherwise.
        found = [
            s
            for s in every
            if qualifier and s.source_name.casefold() == qualifier.casefold()
        ] or every
    elif qualifier:
        # `raw.cards`: the table in that schema, else whatever has the name.
        every = named(sources) + named(undocumented)
        found = (
            [s for s in every if _in_schema(s, qualifier)] or named(models) or every
        )
    else:
        found = named(ctes) or named(models) or named(sources)
        # An undocumented table only when the name points at exactly one.
        if not found and len(only := named(undocumented)) == 1:
            found = only
    return found[0] if found else None


def declaration_at(
    document: TextDocument, parsed, alias: str, pos: types.Position
) -> Alias | None:
    """The declaration of `alias` in scope at `pos`, from the last good parse."""
    # Block spans are offsets into `parsed.source`, so they have to be
    # resolved against that text rather than the live buffer.
    return choose_alias(
        # Alias.line_number is 1-based, LSP Position.line is 0-based.
        cur_pos=Position(pos.line + 1, pos.character),
        aliases=parse_alias_list(document.source),
        alias=alias,
        blocks=query_blocks(parsed.ast),
        sql=parsed.source,
    )


def serve_completions(
    state: ProjectState,
    document: TextDocument,
    ast_cache: AstCache,
    pos: types.Position,
):
    models = state.models
    sources = state.sources
    undocumented = state.undocumented
    dbt_root = state.dbt_root

    # A cursor after the final newline sits on a line `document.lines` does
    # not have.
    line = document.lines[pos.line] if pos.line < len(document.lines) else ""
    current_line = line.strip()
    line_prefix = line[: pos.character]

    ctx = completion_context(line_prefix)
    if ctx is None:
        log.debug("no pattern matched for %r (early exit)", current_line)
        return None

    log.debug("completion @ %d:%d | line=%r", pos.line, pos.character, current_line)

    kind, info = ctx

    if kind == "ref":
        log.info(
            "REF path matched %r → serving %d models: %s",
            current_line,
            len(models),
            [m.name for m in models[:15]],
        )
        return [
            types.CompletionItem(
                m.name,
                kind=types.CompletionItemKind(18),
                label_details=types.CompletionItemLabelDetails(
                    description=str(m.path).split(dbt_root)[-1]
                ),
            )
            for m in models
        ]
    elif kind == "source_name":
        # Close the first argument and open the second with the quote the user
        # is already typing with, so the result is balanced either way.
        quote = info["quote"]
        log.info(
            "SOURCE path matched %r → serving %d sources: %s",
            current_line,
            len(sources),
            [s.name for s in sources[:15]],
        )
        return [
            types.CompletionItem(
                s.name,
                kind=types.CompletionItemKind(10),
                label_details=types.CompletionItemLabelDetails(
                    description=s.source_name
                ),
                insert_text=f"{s.source_name}{quote}, {quote}{s.name}",
                insert_text_format=types.InsertTextFormat.PlainText,
            )
            for s in sources
        ] + [
            types.CompletionItem(
                s.name,
                kind=types.CompletionItemKind(10),
                label_details=types.CompletionItemLabelDetails(
                    description=f"{s.source_name} · {UNDOCUMENTED}"
                ),
                insert_text=f"{s.source_name}{quote}, {quote}{s.name}",
                insert_text_format=types.InsertTextFormat.PlainText,
                sort_text=SORT_LAST + s.name,
            )
            for s in undocumented
        ]
    elif kind == "source_table":
        wanted = info["source"].casefold()
        tables = [s for s in sources if s.source_name.casefold() == wanted]
        # Tables in the same schema that sources.yml does not list yet. The
        # typed name is taken as a schema too, for a source with no YAML at all.
        schemas = {(s.schema or s.source_name).casefold() for s in tables} | {wanted}
        listed = {(s.identifier or s.name).casefold() for s in tables}
        extra = [
            s
            for s in undocumented
            if s.schema.casefold() in schemas and s.name.casefold() not in listed
        ]
        log.info(
            "SOURCE TABLE path matched %r → serving %d tables of %r",
            current_line,
            len(tables),
            info["source"],
        )
        return [
            types.CompletionItem(
                s.name,
                kind=types.CompletionItemKind(10),
                label_details=types.CompletionItemLabelDetails(
                    description=s.source_name
                ),
            )
            for s in tables
        ] + [
            types.CompletionItem(
                s.name,
                kind=types.CompletionItemKind(10),
                label_details=types.CompletionItemLabelDetails(description=UNDOCUMENTED),
                sort_text=SORT_LAST + s.name,
            )
            for s in extra
        ]
    elif kind == "column":
        alias = info["alias"]
        # Alias.line_number is 1-based, LSP Position.line is 0-based.
        cursor = (pos.line + 1, pos.character)
        parsed = ast_cache.get(document.uri)
        if parsed is None:
            log.debug("COLUMN path: no usable AST for %s", document.uri)
            return []
        declaration = declaration_at(document, parsed, alias, pos)
        if declaration is None:
            log.debug("COLUMN path: alias=%r @ %s is not in scope", alias, cursor)
            return []

        # Resolved per request rather than at parse time: the types come from
        # state, which keeps being enriched after a document is first parsed.
        ctes = resolve_ctes(parsed.ctes, models, [*sources, *undocumented])
        columns = columns_for(declaration, ctes, models, sources, undocumented)
        log.info(
            "COLUMN path: alias=%r @ %s → %s %r, %d columns: %s",
            alias,
            cursor,
            declaration.ref_type,
            declaration.ref,
            len(columns),
            [c.name for c in columns[:15]],
        )

        return [
            types.CompletionItem(
                label=c.name,
                kind=types.CompletionItemKind(5),
                label_details=types.CompletionItemLabelDetails(description=c.data_type),
            )
            for c in columns
        ]
    elif kind == "cte":
        parsed = ast_cache.get(document.uri)
        # Warehouse tables do not depend on this file having parsed.
        ctes = parsed.ctes if parsed is not None else ()
        log.info(
            "CTE path matched %r → serving %d ctes: %s and %d warehouse tables",
            current_line,
            len(ctes),
            [c.name for c in ctes[:15]],
            len(undocumented),
        )
        return [
            types.CompletionItem(
                c.name,
                kind=types.CompletionItemKind(18),
                label_details=types.CompletionItemLabelDetails(description="CTE"),
            )
            for c in ctes
        ] + [
            types.CompletionItem(
                f"{s.schema}.{s.name}",
                kind=types.CompletionItemKind(22),
                label_details=types.CompletionItemLabelDetails(
                    description=WAREHOUSE_TABLE
                ),
                sort_text=SORT_LAST + f"{s.schema}.{s.name}",
            )
            for s in undocumented
        ]
    elif kind == "table":
        tables = [s for s in undocumented if _in_schema(s, info["schema"])]
        log.info(
            "TABLE path matched %r → serving %d tables of schema %r",
            current_line,
            len(tables),
            info["schema"],
        )
        return [
            types.CompletionItem(
                s.name,
                kind=types.CompletionItemKind(22),
                label_details=types.CompletionItemLabelDetails(
                    description=WAREHOUSE_TABLE
                ),
            )
            for s in tables
        ]
    else:
        log.debug("no pattern matched for %r", current_line)
        return []


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=["'", '"', "(", ".", " "]),
)
def completions(ls: DbtLanguageServer, params: types.CompletionParams):
    document = ls.workspace.get_text_document(params.text_document.uri)

    if ls.state:
        return serve_completions(
            state=ls.state,
            document=document,
            ast_cache=ls.ast_cache,
            pos=params.position,
        )


@server.feature(types.TEXT_DOCUMENT_DEFINITION)
def definition(ls: DbtLanguageServer, params: types.DefinitionParams):
    """Jump from a ref('model') to that model's .sql file."""
    if ls.state is None:
        return None
    document = ls.workspace.get_text_document(params.text_document.uri)
    pos = params.position
    line = document.lines[pos.line] if pos.line < len(document.lines) else ""

    model_name = ref_model_at(line, pos.character)
    if model_name is None:
        return None

    target = next((m for m in ls.state.models if m.name == model_name and m.path), None)
    if target is None:
        log.info("DEFINITION: no model file found for %r", model_name)
        return None

    log.info("DEFINITION: %r → %s", model_name, target.path)
    start = types.Position(line=0, character=0)
    return types.Location(
        uri=Path(target.path).as_uri(),
        range=types.Range(start=start, end=start),
    )


def serve_hover(
    state: ProjectState,
    document: TextDocument,
    ast_cache: AstCache,
    pos: types.Position,
) -> types.Hover | None:
    """What the ref, source or `alias.column` under the cursor points at."""
    line = document.lines[pos.line] if pos.line < len(document.lines) else ""

    def answer(markdown: str, start: int, end: int) -> types.Hover:
        return types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown, value=markdown
            ),
            range=types.Range(
                start=types.Position(line=pos.line, character=start),
                end=types.Position(line=pos.line, character=end),
            ),
        )

    if found := ref_at(line, pos.character):
        wanted = found.group("model").casefold()
        model = next((m for m in state.models if m.name.casefold() == wanted), None)
        if model is None:
            return None
        return answer(
            hover_text.relation_hover(model, dbt_root=state.dbt_root),
            *found.span("model"),
        )

    if found := source_at(line, pos.character):
        source_name = found.group("source").casefold()
        table = found.group("table").casefold()

        def named(s: SourceTable) -> bool:
            return (
                s.source_name.casefold() == source_name
                and s.name.casefold() == table
            )

        relation = next(filter(named, state.sources), None)
        undocumented = relation is None
        if undocumented:
            relation = next(filter(named, state.undocumented), None)
        if relation is None:
            return None
        return answer(
            hover_text.relation_hover(
                relation, dbt_root=state.dbt_root, undocumented=undocumented
            ),
            found.start("source"),
            found.end("table"),
        )

    if m := dotted_at(line, pos.character):
        parsed = ast_cache.get(document.uri)
        if parsed is None:
            return None
        alias_pos = types.Position(line=pos.line, character=m.start("alias"))
        declaration = declaration_at(document, parsed, m.group("alias"), alias_pos)
        if declaration is None:
            return None
        ctes = resolve_ctes(
            parsed.ctes, state.models, [*state.sources, *state.undocumented]
        )
        relation = relation_for(
            declaration, ctes, state.models, state.sources, state.undocumented
        )
        if relation is None:
            return None
        undocumented = any(relation is s for s in state.undocumented)
        if pos.character <= m.end("alias"):
            return answer(
                hover_text.relation_hover(
                    relation, dbt_root=state.dbt_root, undocumented=undocumented
                ),
                m.start("alias"),
                m.end("alias"),
            )
        return answer(
            hover_text.column_hover(
                m.group("column"), relation, undocumented=undocumented
            ),
            m.start("column"),
            m.end("column"),
        )
    return None


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: DbtLanguageServer, params: types.HoverParams):
    if ls.state is None:
        return None
    document = ls.workspace.get_text_document(params.text_document.uri)
    return serve_hover(ls.state, document, ls.ast_cache, params.position)
