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
from dbt_ls.diagnostics import diagnose
from dbt_ls.exceptions import *
from dbt_ls.model import (
    _DATABASE_METHOD_REGISTRY,
    Model,
    WarehouseTable,
    discover_ref_names,
)
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
from dbt_ls.relations import model_columns
from dbt_ls.scope import (
    AstCache,
    CTERef,
    Position,
    dialect_for_profile_type,
    neutralise_jinja,
    query_blocks,
)
from dbt_ls.source import (
    IGNORED_DIRS,
    SourceTable,
    declared,
    discover_sources,
    enrich_sources_from_catalog,
)
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
        # Set when a .yml file changes, so a warehouse read that was still
        # running reads its sources again before it is adopted.
        self.yaml_changed = False
        # Models whose columns were taken from their saved SQL during this
        # load, so a warehouse read still running cannot undo it.
        self.sql_columns: dict[str, Model] = {}


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
    ls.yaml_changed = False
    ls.sql_columns = {}

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
    publish_all(ls)

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
        # So was a .yml file saved meanwhile, and so were files added.
        if ls.yaml_changed:
            reload_sources(late, ls.settings.schema_sources)
        late.ref_names = state.ref_names
        # Newer than the warehouse's columns for the same models.
        if ls.sql_columns:
            late.reconciliate_models(list(ls.sql_columns.values()))
        publish_all(ls)
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


def _is_project_yaml(path: str | None) -> bool:
    """A .yml file dbt reads, as opposed to one under target/ or a package."""
    if not path or not path.endswith(".yml"):
        return False
    return not IGNORED_DIRS.intersection(Path(path).parts)


def _is_refable(path: str | None) -> bool:
    """A file whose name ref() can take, packages' included."""
    if not path or not path.endswith((".sql", ".py", ".csv")):
        return False
    return not (IGNORED_DIRS - {"dbt_packages"}).intersection(Path(path).parts)


def reload_sources(state: ProjectState, schema_sources: tuple[SchemaSource, ...]):
    """Sources from YAML again, enriched from what was already read.

    Neither the catalog nor the warehouse is new, so the warehouse is not
    queried again: the tables of its last read are matched once more.
    """
    state.sources = discover_sources(state.dbt_root)
    for source in schema_sources:
        if source is SchemaSource.CATALOG and state.catalog_path.is_file():
            state.sources = enrich_sources_from_catalog(
                state.sources, state.catalog_path
            )
        elif source is SchemaSource.DATABASE and state.warehouse:
            state.match_warehouse()
    log.info("Sources reloaded: %d declared tables", len(state.sources))


@server.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def watched_files(ls: DbtLanguageServer, params: types.DidChangeWatchedFilesParams):
    paths = [to_fs_path(change.uri) for change in params.changes]
    for path in paths:
        if path and path.endswith("run_results.json"):
            log.info("run results changed: %s", path)
            ls.run_results_path = path
            if ls.state:
                ls.state.refresh_from_run_results(path)
                # Columns of the models just run changed under open files.
                publish_all(ls)
    yaml_changed = any(_is_project_yaml(path) for path in paths)
    # A new or removed model, seed or snapshot changes what ref() can name.
    refable = any(
        change.type != types.FileChangeType.Changed and _is_refable(path)
        for change, path in zip(params.changes, paths)
    )
    seeds_changed = any(path and path.endswith(".csv") for path in paths)
    ls.yaml_changed |= yaml_changed
    if ls.state is None or not (yaml_changed or refable or seeds_changed):
        return
    if yaml_changed:
        reload_sources(ls.state, ls.settings.schema_sources)
    if seeds_changed:
        ls.state.refresh_seeds()
    ls.state.ref_names = discover_ref_names(ls.state.dbt_root)
    publish_all(ls)


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
                            ),
                            # Sources are declared in any .yml file.
                            types.FileSystemWatcher(glob_pattern="**/*.yml"),
                            # Only coming and going: an edit renames nothing.
                            types.FileSystemWatcher(
                                glob_pattern="**/*.{sql,py}",
                                kind=types.WatchKind.Create | types.WatchKind.Delete,
                            ),
                            # Seeds, edits included: the header is the columns.
                            types.FileSystemWatcher(glob_pattern="**/*.csv"),
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


def publish(ls: DbtLanguageServer, uri: str):
    """What dbt would stop at in this document, replacing what was shown."""
    if ls.state is None or not uri.endswith(".sql"):
        return
    text = ls.workspace.get_text_document(uri).source
    diagnostics = diagnose(text, ls.ast_cache.get(uri), ls.state)
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics)
    )


def publish_all(ls: DbtLanguageServer):
    """Every open document again, after what the project declares changed."""
    for uri in list(ls.workspace.text_documents):
        publish(ls, uri)


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: DbtLanguageServer, params: types.DidOpenTextDocumentParams):
    refresh_ast(ls, params.text_document.uri)
    publish(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: DbtLanguageServer, params: types.DidChangeTextDocumentParams):
    refresh_ast(ls, params.text_document.uri)
    publish(ls, params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: DbtLanguageServer, params: types.DidSaveTextDocumentParams):
    refresh_ast(ls, params.text_document.uri)
    if columns_from_sql(ls, params.text_document.uri):
        publish_all(ls)


def columns_from_sql(ls: DbtLanguageServer, uri: str) -> bool:
    """Take a saved model's columns from its SQL, when that names them all.

    The warehouse knows a model as it was last built, so a column just added
    upstream would be "not in" it downstream until the next `dbt run`. A
    select that names every output says the same without the run. True when
    the model's columns changed.
    """
    state = ls.state
    path = to_fs_path(uri)
    if state is None or path is None:
        return False
    model = next((m for m in state.models if str(m.path) == path), None)
    parsed = ls.ast_cache.get(uri)
    document = ls.workspace.get_text_document(uri)
    # A seed's columns come from its header, read by the file watcher.
    if model is None or parsed is None or model.model_type == "seed":
        return False
    if parsed.source != neutralise_jinja(document.source):
        # Only a repaired or older parse: not what the file says.
        return False
    columns = model_columns(parsed.ast, state.models, state.sources, state.undocumented)
    if columns is None:
        return False
    # The warehouse's types are better than none for what still passes
    # through unchanged.
    columns = tuple(
        c if c.data_type else model.column(c.name) or c for c in columns
    )
    if model.all_columns and columns == model.columns:
        return False
    log.info("Columns of %s from its SQL: %s", model.name, [c.name for c in columns])
    updated = replace(model, columns=columns, all_columns=True)
    ls.sql_columns[model.name] = updated
    state.reconciliate_models([updated])
    return True


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: DbtLanguageServer, params: types.DidCloseTextDocumentParams):
    ls.ast_cache.discard(params.text_document.uri)
    # A closed file is not checked, so nothing about it should linger.
    ls.text_document_publish_diagnostics(
        types.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
    )


# How a warehouse table without YAML is marked in the completion list.
UNDOCUMENTED = "not in sources.yml"
MODEL = "model"
# Sorts after every identifier, so what the project declares is offered first.
SORT_LAST = "~"


def _in_schema(table: SourceTable | WarehouseTable, schema: str) -> bool:
    return table.relation_schema.casefold() == schema.casefold()


def _table_item(table, description: str, **fields) -> types.CompletionItem:
    """A table inside `source(...)`; one nobody declared sorts after the rest."""
    return types.CompletionItem(
        table.name,
        kind=types.CompletionItemKind(10),
        label_details=types.CompletionItemLabelDetails(description=description),
        sort_text=None if declared(table) else SORT_LAST + table.name,
        **fields,
    )


def source_name_for(
    table: SourceTable | WarehouseTable, sources: list[SourceTable]
) -> str:
    """The name `source()` should call a table by.

    A table nobody declared belongs with a declared source over the same
    schema, if there is one; otherwise its schema names it.
    """
    if declared(table):
        return table.source_name
    owner = next((s for s in sources if _in_schema(s, table.schema)), None)
    return owner.source_name if owner else table.schema


def _typed(pos: types.Position, start: int) -> types.Range:
    """From where the relation's name starts up to the cursor: what a ref() or
    source() call replaces, so no hard-coded schema.table is left behind."""
    return types.Range(start=types.Position(line=pos.line, character=start), end=pos)


def _relation_item(
    label: str,
    call: str,
    description: str,
    replace: types.Range,
    *,
    filter_text: str,
    last: bool = False,
) -> types.CompletionItem:
    """A relation after FROM/JOIN, written as dbt reads it."""
    return types.CompletionItem(
        label,
        kind=types.CompletionItemKind(22),
        label_details=types.CompletionItemLabelDetails(description=description),
        filter_text=filter_text,
        text_edit=types.TextEdit(range=replace, new_text=call),
        sort_text=SORT_LAST + label if last else None,
    )


def _ref_item(m: Model, replace: types.Range) -> types.CompletionItem:
    return _relation_item(
        m.name,
        f"{{{{ ref('{m.name}') }}}}",
        m.model_type or MODEL,
        replace,
        filter_text=m.name,
    )


def _source_item(
    table: SourceTable | WarehouseTable,
    sources: list[SourceTable],
    replace: types.Range,
    typed_schema: str | None = None,
) -> types.CompletionItem:
    """`erp.cards` after FROM; just `cards` once `raw.` is typed, which the
    client still has to match as typed."""
    source_name = source_name_for(table, sources)
    call = f"{{{{ source('{source_name}', '{table.name}') }}}}"
    if typed_schema is None:
        label = filter_text = f"{source_name}.{table.name}"
    else:
        label, filter_text = table.name, f"{typed_schema}.{table.name}"
    if declared(table):
        return _relation_item(label, call, source_name, replace, filter_text=filter_text)
    return _relation_item(
        label, call, UNDOCUMENTED, replace, filter_text=filter_text, last=True
    )


def relation_for(
    declaration: Alias,
    ctes: tuple[CTERef, ...],
    models: list[Model],
    tables: list[SourceTable | WarehouseTable],
) -> CTERef | Model | SourceTable | WarehouseTable | None:
    """The relation an alias is bound to.

    `ref_type` records which pattern produced the alias, so "model" and
    "source" name their pool exactly. "cte" is a guess: TABLE_PATTERN matches
    any identifier after FROM/JOIN, which may just as well be a model, a
    source or a warehouse table referenced without ref()/source() — hence the
    extra pools it falls back to.

    `tables` is `ProjectState.tables`: declared sources before warehouse
    tables nobody declared, so the first match is what the project declares.
    """
    # parse_alias_list lowercases the document, the pools keep the casing they
    # were discovered with, so the comparison has to be case-insensitive.
    wanted = declaration.ref.casefold()
    qualifier = (declaration.qualifier or "").casefold()

    def named(pool):
        return [r for r in pool if r.name.casefold() == wanted]

    every = named(tables)
    if declaration.ref_type == "model":
        found = named(models)
    elif declaration.ref_type == "source":
        # The source named in the call; any source with that table otherwise.
        found = [
            t for t in every if qualifier and t.source_name.casefold() == qualifier
        ] or every
    elif qualifier:
        # `raw.cards`: the table in that schema, else whatever has the name.
        found = [t for t in every if _in_schema(t, qualifier)] or named(models) or every
    else:
        found = named(ctes) or named(models) or [t for t in every if declared(t)]
        # A table nobody declared only when the name points at exactly one.
        if not found and len(every) == 1:
            found = every
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
            _table_item(
                s,
                s.source_name if declared(s) else f"{s.source_name} · {UNDOCUMENTED}",
                insert_text=f"{s.source_name}{quote}, {quote}{s.name}",
                insert_text_format=types.InsertTextFormat.PlainText,
            )
            for s in state.tables
        ]
    elif kind == "source_table":
        wanted = info["source"].casefold()
        tables = [s for s in sources if s.source_name.casefold() == wanted]
        # Tables in the same schema that sources.yml does not list yet. The
        # typed name is taken as a schema too, for a source with no YAML at all.
        schemas = {s.relation_schema.casefold() for s in tables} | {wanted}
        listed = {s.relation_name.casefold() for s in tables}
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
        return [_table_item(s, s.source_name) for s in tables] + [
            _table_item(s, UNDOCUMENTED) for s in extra
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
        ctes = resolve_ctes(parsed.ctes, models, state.tables)
        relation = relation_for(declaration, ctes, models, state.tables)
        columns = relation.columns if relation else ()
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
        replace = _typed(pos, info["start"])
        parsed = ast_cache.get(document.uri)
        # Models and tables do not depend on this file having parsed.
        ctes = parsed.ctes if parsed is not None else ()
        log.info(
            "FROM path matched %r → serving %d ctes, %d models and %d tables",
            current_line,
            len(ctes),
            len(models),
            len(state.tables),
        )
        return (
            [
                types.CompletionItem(
                    c.name,
                    kind=types.CompletionItemKind(18),
                    label_details=types.CompletionItemLabelDetails(description="CTE"),
                )
                for c in ctes
            ]
            + [_ref_item(m, replace) for m in models]
            + [_source_item(t, sources, replace) for t in state.tables]
        )
    elif kind == "table":
        schema, replace = info["schema"], _typed(pos, info["start"])
        tables = [t for t in state.tables if _in_schema(t, schema)]
        log.info(
            "TABLE path matched %r → serving %d tables of schema %r",
            current_line,
            len(tables),
            schema,
        )
        return [_source_item(t, sources, replace, schema) for t in tables]
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
    where = f"{pos.line}:{pos.character}"

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
            log.info("HOVER @ %s: ref %r matches no model", where, found["model"])
            return None
        log.info(
            "HOVER @ %s: ref %r → model, %d columns",
            where,
            model.name,
            len(model.columns),
        )
        return answer(
            hover_text.relation_hover(model, dbt_root=state.dbt_root),
            *found.span("model"),
        )

    if found := source_at(line, pos.character):
        call = (found["source"].casefold(), found["table"].casefold())
        relation = next(
            (
                t
                for t in state.tables
                if (t.source_name.casefold(), t.name.casefold()) == call
            ),
            None,
        )
        if relation is None:
            log.info("HOVER @ %s: source %r matches no table", where, call)
            return None
        log.info(
            "HOVER @ %s: source %r → %s, %d columns",
            where,
            call,
            hover_text.kind_of(relation),
            len(relation.columns),
        )
        return answer(
            hover_text.relation_hover(relation, dbt_root=state.dbt_root),
            found.start("source"),
            found.end("table"),
        )

    if m := dotted_at(line, pos.character):
        alias = m.group("alias")
        parsed = ast_cache.get(document.uri)
        if parsed is None:
            log.debug("HOVER @ %s: no usable AST for %s", where, document.uri)
            return None
        alias_pos = types.Position(line=pos.line, character=m.start("alias"))
        declaration = declaration_at(document, parsed, alias, alias_pos)
        if declaration is None:
            # Also every `schema.table`, so not worth more than debug.
            log.debug("HOVER @ %s: %r is not an alias in scope", where, alias)
            return None
        ctes = resolve_ctes(parsed.ctes, state.models, state.tables)
        relation = relation_for(declaration, ctes, state.models, state.tables)
        if relation is None:
            log.info(
                "HOVER @ %s: alias %r → %s %r, which is not known",
                where,
                alias,
                declaration.ref_type,
                declaration.ref,
            )
            return None
        log.info(
            "HOVER @ %s: %s.%s → %s %r, %d columns",
            where,
            alias,
            m.group("column"),
            declaration.ref_type,
            relation.name,
            len(relation.columns),
        )
        if pos.character <= m.end("alias"):
            return answer(
                hover_text.relation_hover(relation, dbt_root=state.dbt_root),
                m.start("alias"),
                m.end("alias"),
            )
        return answer(
            hover_text.column_hover(m.group("column"), relation),
            m.start("column"),
            m.end("column"),
        )
    log.debug("HOVER @ %s: nothing to show on %r", where, line.strip())
    return None


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: DbtLanguageServer, params: types.HoverParams):
    if ls.state is None:
        return None
    document = ls.workspace.get_text_document(params.text_document.uri)
    return serve_hover(ls.state, document, ls.ast_cache, params.position)
