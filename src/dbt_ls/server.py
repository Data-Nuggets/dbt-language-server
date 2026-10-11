import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4

import yaml
from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path
from pygls.workspace import TextDocument

from dbt_ls import __version__
from dbt_ls.alias import Alias, choose_alias, parse_alias_list
from dbt_ls.column import Column
from dbt_ls.exceptions import *
from dbt_ls.model import _DATABASE_METHOD_REGISTRY, Model
from dbt_ls.pattern import completion_context, ref_model_at
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
    project: Project, report: LoadReport
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
        profiles = Profiles.locate(project.root)
    except (OSError, yaml.YAMLError) as exc:
        report.profile_problem = f"could not read profiles.yml: {exc}"
        return None
    if profiles is None:
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


def _load_project(root_path: str | None, settings: Settings) -> ProjectState:
    """Build the project state. Raises only when there is no project to load.

    Anything short of that — no profile, a missing driver, a warehouse that
    refuses the connection — is recorded on `state.report` and the load goes
    on with whatever it could get.
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
    profile_target = _resolve_profile_target(project, report)
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

    # Column enrichment. Each source is run in ascending priority order, so the
    # last one that yields columns for a model is the one that wins. A source
    # left out of `schema_sources` is never run at all
    if not settings.schema_sources:
        log.warning("No schema sources enabled; models will have no column info")
    else:
        log.info(f"Running with source settings: {settings}")

    for source in settings.schema_sources:
        log.debug("Enriching from %s", source.value)
        report.sources.append(_run_source(source, state))

    return state


def load_project(ls: DbtLanguageServer):

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

    try:
        ls.state = _load_project(ls.workspace.root_path, ls.settings)
    except (NoRootPathError, NoDbtRootError) as exc:
        finish(f"❌{exc}")
        return

    # Parse in the adapter's own dialect now that it is known.
    ls.ast_cache.dialect = ls.state.report.dialect
    problems = ls.state.report.problems
    # Models and sources are loaded either way; a problem only means fewer
    # columns, so it is a warning rather than the old all-or-nothing ❌.
    finish(f"⚠️{problems[0]}" if problems else "Finished")


@server.feature(types.WORKSPACE_DID_CHANGE_WATCHED_FILES)
def run_results(ls: DbtLanguageServer, params: types.DidChangeWatchedFilesParams):
    for change in params.changes:
        change_path = to_fs_path(change.uri)
        if change_path and change_path.endswith("run_results.json"):
            log.info(f"{change.type}: {to_fs_path(change.uri)}")
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


def columns_for(
    declaration: Alias,
    ctes: tuple[CTERef, ...],
    models: list[Model],
    sources: list[SourceTable],
) -> tuple[Column, ...]:
    """Columns of the relation an alias is bound to.

    `ref_type` records which pattern produced the alias, so "model" and
    "source" name their pool exactly. "cte" is a guess: TABLE_PATTERN matches
    any bare identifier after FROM/JOIN, which may just as well be a model or
    source referenced without ref()/source() — hence the extra pools it falls
    back to.
    """
    pools: tuple[list | tuple, ...] = {
        "model": (models,),
        "source": (sources,),
        "cte": (ctes, models, sources),
    }.get(declaration.ref_type, (ctes, models, sources))

    # parse_alias_list lowercases the document, the pools keep the casing they
    # were discovered with, so the comparison has to be case-insensitive.
    wanted = declaration.ref.casefold()
    for pool in pools:
        for relation in pool:
            if relation.name.casefold() == wanted:
                return relation.columns
    return ()


def serve_completions(
    state: ProjectState,
    document: TextDocument,
    ast_cache: AstCache,
    pos: types.Position,
):
    models = state.models
    sources = state.sources
    dbt_root = state.dbt_root

    current_line = document.lines[pos.line].strip()
    line = document.lines[pos.line] if pos.line < len(document.lines) else ""
    line_prefix = line[: pos.character]

    ctx = completion_context(line_prefix)
    if ctx is None:
        log.debug("no pattern matched for %r", current_line, " (early exit)")
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
                insert_text=f'{s.source_name}", "{s.name}',
                insert_text_format=types.InsertTextFormat.PlainText,
            )
            for s in sources
        ]
    elif kind == "column":
        alias = info["alias"]
        # Alias.line_number is 1-based, LSP Position.line is 0-based.
        cursor = (pos.line + 1, pos.character)
        parsed = ast_cache.get(document.uri)
        if parsed is None:
            log.debug("COLUMN path: no usable AST for %s", document.uri)
            return []
        # Block spans are offsets into `parsed.source`, so they have to be
        # resolved against that text rather than the live buffer.
        declaration = choose_alias(
            cur_pos=Position(cursor[0], cursor[1]),
            aliases=parse_alias_list(document.source),
            alias=alias,
            blocks=query_blocks(parsed.ast),
            sql=parsed.source,
        )
        if declaration is None:
            log.debug("COLUMN path: alias=%r @ %s is not in scope", alias, cursor)
            return []

        # Resolved per request rather than at parse time: the types come from
        # state, which keeps being enriched after a document is first parsed.
        ctes = resolve_ctes(parsed.ctes, models, sources)
        columns = columns_for(declaration, ctes, models, sources)
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
        if parsed is None:
            log.debug("CTE path: no usable AST for %s", document.uri)
            return []
        ctes = parsed.ctes
        log.info(
            "CTE path matched %r → serving %d ctes: %s",
            current_line,
            len(ctes),
            [c.name for c in ctes[:15]],
        )
        return [
            types.CompletionItem(
                c.name,
                kind=types.CompletionItemKind(18),
                label_details=types.CompletionItemLabelDetails(description="CTE"),
            )
            for c in ctes
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
