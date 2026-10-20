"""`load_project`: disk sources at once, the warehouse off the main thread."""

import asyncio
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbt_ls import model
from dbt_ls.scope import AstCache
from dbt_ls.server import (
    SchemaSource,
    Settings,
    _split_at_warehouse,
    load_project,
    parse_schema_sources,
)

PROJECT = Path(__file__).parent.parent / "testdata" / "project"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    shutil.copytree(PROJECT, root, ignore=shutil.ignore_patterns("target", "logs"))
    return root


def fake_server(root, sources="config,catalog,database"):
    return SimpleNamespace(
        state=None,
        settings=Settings(parse_schema_sources(sources)),
        ast_cache=AstCache(),
        load_generation=0,
        run_results_path=None,
        yaml_changed=False,
        sql_columns={},
        # No documents open, so nothing to publish diagnostics for.
        workspace=SimpleNamespace(root_path=str(root), text_documents={}),
        client_capabilities=SimpleNamespace(window=None),
    )


@pytest.fixture
def held_warehouse(monkeypatch):
    """The DuckDB reader, blocked until the test lets it go."""
    release = threading.Event()
    started = threading.Event()
    read = model._DATABASE_METHOD_REGISTRY["duckdb"]

    def slow(profile_target, project_root):
        started.set()
        assert release.wait(timeout=10)
        return read(profile_target, project_root)

    monkeypatch.setitem(model._DATABASE_METHOD_REGISTRY, "duckdb", slow)
    return SimpleNamespace(release=release, started=started)


def types_of(state):
    return {c.data_type for m in state.models for c in m.columns}


async def until(condition, timeout=10.0):
    waited = 0.0
    while not condition():
        await asyncio.sleep(0.01)
        waited += 0.01
        assert waited < timeout, "timed out"


def test_split_keeps_everything_after_the_warehouse_with_it():
    config, catalog, database = (
        SchemaSource.CONFIG,
        SchemaSource.CATALOG,
        SchemaSource.DATABASE,
    )
    assert _split_at_warehouse((config, catalog, database)) == (
        (config, catalog),
        (database,),
    )
    assert _split_at_warehouse((database, config)) == ((), (database, config))
    assert _split_at_warehouse((config, catalog)) == ((config, catalog), ())


def test_names_and_documented_columns_do_not_wait_for_the_warehouse(
    project, held_warehouse
):
    ls = fake_server(project)

    async def scenario():
        load_project(ls)

        # Returned while the warehouse is still being read.
        assert not held_warehouse.release.is_set()
        early = ls.state
        assert {m.name for m in early.models} == {
            "my_first_dbt_model",
            "my_second_dbt_model",
        }
        assert any(m.columns for m in early.models)
        assert types_of(early) == {None}
        assert [s.source for s in early.report.sources] == ["config", "catalog"]

        held_warehouse.release.set()
        await until(lambda: ls.state is not early)

        assert None not in types_of(ls.state)
        assert [s.source for s in ls.state.report.sources] == [
            "config",
            "catalog",
            "database",
        ]
        # The state handed out earlier was never modified behind its back.
        assert types_of(early) == {None}

    asyncio.run(scenario())


def test_a_reload_discards_the_older_warehouse_read(project, held_warehouse):
    ls = fake_server(project)

    async def scenario():
        load_project(ls)
        await until(held_warehouse.started.is_set)
        # A reload that does not touch the warehouse at all.
        ls.settings = Settings(parse_schema_sources("config"))
        load_project(ls)
        reloaded = ls.state

        held_warehouse.release.set()
        await asyncio.sleep(0.5)

        assert ls.state is reloaded
        assert types_of(ls.state) == {None}

    asyncio.run(scenario())


def test_run_results_seen_during_the_read_are_applied_again(
    project, held_warehouse, monkeypatch
):
    ls = fake_server(project)
    applied = []
    monkeypatch.setattr(
        "dbt_ls.state.ProjectState.refresh_from_run_results",
        lambda self, path: applied.append((self, path)),
    )

    async def scenario():
        load_project(ls)
        early = ls.state
        ls.run_results_path = "/project/target/run_results.json"

        held_warehouse.release.set()
        await until(lambda: ls.state is not early)

        assert applied == [(ls.state, "/project/target/run_results.json")]

    asyncio.run(scenario())


def test_without_an_event_loop_the_load_is_complete_on_return(project):
    ls = fake_server(project)

    load_project(ls)

    assert None not in types_of(ls.state)


def test_no_project_leaves_the_state_empty(tmp_path):
    ls = fake_server(tmp_path)

    load_project(ls)

    assert ls.state is None
