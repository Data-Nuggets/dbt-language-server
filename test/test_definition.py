import pytest
from lsprotocol import types

from src.dbt_ls.model import Model
from src.dbt_ls.server import definition
from src.dbt_ls.state import ProjectState


class FakeDoc:
    """Stand-in for pygls' TextDocument: the handler only reads `.lines`."""

    def __init__(self, text: str):
        self.lines = text.splitlines(keepends=True)


class FakeServer:
    """Minimal server exposing `.state` and workspace.get_text_document(uri)."""

    def __init__(self, doc: FakeDoc, state: ProjectState):
        self.state = state
        self.workspace = type(
            "FakeWorkspace", (), {"get_text_document": lambda self, uri: doc}
        )()


def make_params(line: str, character: int) -> types.DefinitionParams:
    return types.DefinitionParams(
        text_document=types.TextDocumentIdentifier(uri="file:///query.sql"),
        position=types.Position(line=0, character=character),
    )


@pytest.fixture
def model_file(tmp_path):
    f = tmp_path / "stg_customers.sql"
    f.write_text("select 1\n")
    return f


def _fake_ls(line: str, models: list[Model]) -> FakeServer:
    # ProjectState now requires `project` and `profile_target`. The definition
    # handler only reads `state.models`, so pass placeholders for those. Point
    # `dbt_root` at a nonexistent path so source/model discovery in
    # __post_init__ is a cheap no-op instead of scanning the repo.
    state = ProjectState(
        project=None,
        profile_target=None,
        models=models,
        dbt_root="/nonexistent",
    )
    return FakeServer(FakeDoc(line), state)


def test_definition_jumps_to_model_file(model_file):
    line = "select * from {{ ref('stg_customers') }}"
    ls = _fake_ls(line, [Model(name="stg_customers", path=str(model_file))])

    cursor = line.index("stg_customers") + 2
    result = definition(ls, make_params(line, cursor))

    assert result is not None
    assert result.uri == model_file.as_uri()
    # We always point at the top of the target file.
    assert result.range.start == types.Position(line=0, character=0)
    assert result.range.end == types.Position(line=0, character=0)


def test_definition_returns_none_when_cursor_not_on_ref(model_file):
    line = "select * from {{ ref('stg_customers') }}"
    ls = _fake_ls(line, [Model(name="stg_customers", path=str(model_file))])

    # cursor on the `select` keyword, outside any ref()
    result = definition(ls, make_params(line, 2))
    assert result is None


def test_definition_returns_none_for_unknown_model():
    line = "{{ ref('does_not_exist') }}"
    ls = _fake_ls(line, [Model(name="stg_customers", path="/somewhere.sql")])

    cursor = line.index("does_not_exist") + 2
    result = definition(ls, make_params(line, cursor))
    assert result is None


def test_definition_returns_none_when_model_has_no_path():
    line = "{{ ref('stg_customers') }}"
    ls = _fake_ls(line, [Model(name="stg_customers", path=None)])

    cursor = line.index("stg_customers") + 2
    result = definition(ls, make_params(line, cursor))
    assert result is None
