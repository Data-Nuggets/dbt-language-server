import logging
import os
import sys
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from dbt_ls import __version__
from dbt_ls.alias import choose_alias, parse_alias_list
from dbt_ls.exceptions import *
from dbt_ls.pattern import completion_context, ref_model_at
from dbt_ls.profiles import Profiles
from dbt_ls.project import Project
from dbt_ls.scope import AstCache, Position, dialect_for_profile_type, query_blocks
from dbt_ls.source import IGNORED_DIRS
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


def find_dbt_project_root(root: str) -> str:
    # Mirror Neovim's root_markers: first walk UP from the opened path looking
    # for a dbt_project.yml, so opening a subfolder of the project still finds
    # the real root (VS Code sends the opened folder as-is, not the project root).
    start = Path(root)
    for directory in (start, *start.parents):
        if (directory / "dbt_project.yml").is_file():
            return str(directory)
    # Otherwise search downward — e.g. when a parent of the project was opened.
    for p in Path(root).rglob("dbt_project.yml"):
        if not IGNORED_DIRS.intersection(p.parts):
            return str(p.parent)
    return "."


def _load_project(root_path: str | None, settings: Settings) -> ProjectState:

    if not root_path:
        log.warning("Initialize received no root_path; skipping project discovery")
        raise NoRootPathError

    dbt_root = find_dbt_project_root(root_path)
    if not dbt_root:
        log.warning("No dbt project root found under %s; skipping discovery", root_path)
        raise NoDbtRootError
    log.info("Resolved dbt project root: %s (from opened path %s)", dbt_root, root_path)

    project = Project(dbt_root)
    log.info(
        "Project %r uses profile %r", project.config.get("name", ""), project.profile
    )

    # ProjectState requires a resolved profile target up front, so resolve the
    # profile before constructing it.
    profile = Profiles.locate(project.root)
    if not profile:
        log.info("No dbt profile located; skipping database enrichment")
        raise NoDbtProfileError

    if not project.profile:
        log.info("dbt_project.yml has no `profile:` key; skipping database enrichment")
        raise NoDbtProfileTargetError

    try:
        profile_target = profile.resolve(project.profile)
    except EnvVarError as e:
        log.error("Environment variable not set for profile %r", project.profile)
        raise NoDbtProfileTargetError from e
    except KeyError as e:
        log.error(
            "Profile %r has no matching profile/target %s in profiles.yml",
            project.profile,
            e,
        )
        raise NoDbtProfileTargetError from e
    else:
        if not profile_target:
            log.info(
                "Profile %r resolved to an empty target; skipping database enrichment",
                project.profile,
            )
            raise NoDbtProfileTargetError
        log.info("Using profile: %s", profile_target)

    # __post_init__ discovers documented models and sources from `dbt_root`.
    state = ProjectState(
        project=project,
        profile_target=profile_target,
        dbt_root=dbt_root,
    )
    log.debug("Finished parsing documented models and sources")

    # Column enrichment. Each source is run in ascending priority order, so the
    # last one that yields columns for a model is the one that wins. A source
    # left out of `schema_sources` is never run at all
    refreshers = {
        SchemaSource.CONFIG: state.refresh_from_config,
        SchemaSource.CATALOG: state.refresh_from_catalog,
        SchemaSource.DATABASE: state.refresh_from_database,
    }
    if not settings.schema_sources:
        log.warning("No schema sources enabled; models will have no column info")
    else:
        log.info(f"Running with source settings: {settings}")

    for source in settings.schema_sources:
        log.debug("Enriching from %s", source.value)
        try:
            refreshers[source]()
        except ImportError as exc:
            # ibis wraps the real ModuleNotFoundError in its own ImportError, so
            # walk the cause chain to find the name of the package actually missing.
            missing = "unknown"
            err: BaseException | None = exc
            while err is not None:
                if isinstance(err, ImportError) and err.name:
                    missing = err.name
                    break
                err = err.__cause__ or err.__context__
            log.warning(
                "%s enrichment skipped: missing dependency %r (%s). "
                "Install the matching extra, e.g. `pip install dbt-ls[postgres]`, "
                "then restart. Continuing with documented models only.",
                source.value,
                missing,
                exc,
            )
            # Surfaced by load_project as a "missing dependency" progress message.
            raise
        except Exception:  # noqa: BLE001 — enrichment must never crash initialize
            log.exception("%s enrichment failed; continuing without it", source.value)
        else:
            log.info("Finished parsing column info from %s", source.value)

    return state


def load_project(ls: DbtLanguageServer):

    # Progress report setup
    token = "reload-project" + str(uuid.uuid4())

    if (
        ls.client_capabilities.window
        and ls.client_capabilities.window.work_done_progress
    ):
        ls.work_done_progress.create(token)
        ls.work_done_progress.begin(
            token,
            types.WorkDoneProgressBegin(
                title="Loading dbt project", cancellable=False, percentage=0
            ),
        )

    try:
        ls.state = _load_project(ls.workspace.root_path, ls.settings)
    except NoDbtProfileError:
        ls.work_done_progress.end(
            token, types.WorkDoneProgressEnd(message="❌No profile found")
        )
        return
    except NoDbtProfileTargetError:
        ls.work_done_progress.end(
            token, types.WorkDoneProgressEnd(message="❌Target empty")
        )
    except ImportError:
        ls.work_done_progress.end(
            token, types.WorkDoneProgressEnd(message="❌Missing dependency (see logs)")
        )
    else:
        # Now that the profile is known, parse in the adapter's own dialect.
        ls.ast_cache.dialect = dialect_for_profile_type(ls.state.profile_target.type)
        ls.work_done_progress.end(token, types.WorkDoneProgressEnd(message="Finished"))


@server.feature(types.INITIALIZED)
def on_initialized(ls: DbtLanguageServer, params: types.InitializedParams):
    # Discovery runs on the `initialized` notification, not the `initialize`
    # request: the client only services server-initiated requests (e.g.
    # window/workDoneProgress/create) once the handshake has completed.
    load_project(ls)


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


@server.feature(
    types.TEXT_DOCUMENT_COMPLETION,
    types.CompletionOptions(trigger_characters=["'", '"', "(", "."]),
)
def completions(ls: DbtLanguageServer, params: types.CompletionParams):
    if ls.state is None:
        return None
    models = ls.state.models
    sources = ls.state.sources
    dbt_root = ls.state.dbt_root

    document = ls.workspace.get_text_document(params.text_document.uri)
    current_line = document.lines[params.position.line].strip()
    pos = params.position
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
        [log.debug(c) for m in (*models, *sources) for c in m.columns]
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
        parsed = ls.ast_cache.get(params.text_document.uri)
        if parsed is None:
            log.debug("COLUMN path: no usable AST for %s", params.text_document.uri)
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
        model_name = declaration.ref if declaration else None
        log.info("COLUMN path: alias=%r @ %s → model=%r", alias, cursor, model_name)

        return [
            types.CompletionItem(
                label=c.name,
                kind=types.CompletionItemKind(5),
                label_details=types.CompletionItemLabelDetails(description=c.data_type),
            )
            for m in (*models, *sources)
            for c in m.columns
            if m.name == model_name
        ]
    else:
        log.debug("no pattern matched for %r", current_line)
        return []


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
