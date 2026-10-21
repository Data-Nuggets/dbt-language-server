"""Model files that come and go while the server runs."""

import asyncio
import shutil
from pathlib import Path
from test.conftest import URI, complete, labels
from test.test_background_load import fake_server, held_warehouse, until  # noqa: F401
from types import SimpleNamespace

import duckdb
from lsprotocol import types

from dbt_ls.column import Column
from dbt_ls.model import Model
from dbt_ls.server import (
    Settings,
    _load_project,
    load_project,
    parse_schema_sources,
    watched_files,
)

PROJECT = Path(__file__).parent.parent / "testdata" / "project"
EXAMPLE = Path("models") / "example"


def copy_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT, root, ignore=shutil.ignore_patterns("target", "logs"))
    return root


def loaded(root: Path, sources: str = "config,database") -> SimpleNamespace:
    """A server whose project, warehouse included, has finished loading."""
    return SimpleNamespace(
        state=_load_project(str(root), Settings(parse_schema_sources(sources))),
        settings=Settings(parse_schema_sources(sources)),
        yaml_changed=False,
        files_changed=False,
        sql_columns={},
        sql_before={},
        workspace=SimpleNamespace(text_documents={}),
    )


def notify(ls, path: Path, change: types.FileChangeType):
    watched_files(
        ls, types.DidChangeWatchedFilesParams([types.FileEvent(path.as_uri(), change)])
    )


def model_named(state, name: str) -> Model | None:
    return next((m for m in state.models if m.name == name), None)


class TestCreated:
    def test_a_new_file_completes_in_ref(self, tmp_path):
        root = copy_project(tmp_path)
        ls = loaded(root)
        new = root / EXAMPLE / "stg_payments.sql"
        new.write_text("select 1 as payment_id")

        notify(ls, new, types.FileChangeType.Created)

        assert model_named(ls.state, "stg_payments").path == new
        items = complete("select * from {{ ref('|", models=ls.state.models)
        assert "stg_payments" in labels(items)

    def test_a_table_already_built_gives_it_columns_at_once(self, tmp_path):
        # As after a `git pull` of a model someone else already ran.
        root = copy_project(tmp_path)
        con = duckdb.connect(str(root / "dev.duckdb"))
        con.execute("create table main.stg_payments (payment_id bigint, amount double)")
        con.close()
        ls = loaded(root)
        assert "stg_payments" in {t.name for t in ls.state.undocumented}
        new = root / EXAMPLE / "stg_payments.sql"
        new.write_text("select 1 as payment_id, 2.0 as amount")

        notify(ls, new, types.FileChangeType.Created)

        model = model_named(ls.state, "stg_payments")
        assert [(c.name, c.data_type) for c in model.columns] == [
            ("payment_id", "BIGINT"),
            ("amount", "DOUBLE"),
        ]
        # The model claims its table, so it is no longer offered as a source.
        assert "stg_payments" not in {t.name for t in ls.state.undocumented}

    def test_models_already_known_keep_their_columns(self, tmp_path):
        root = copy_project(tmp_path)
        ls = loaded(root)
        before = model_named(ls.state, "my_first_dbt_model")
        new = root / EXAMPLE / "stg_payments.sql"
        new.write_text("select 1")

        notify(ls, new, types.FileChangeType.Created)

        assert model_named(ls.state, "my_first_dbt_model") == before
        assert before.columns


class TestDeleted:
    def test_a_removed_file_leaves_completion(self, tmp_path):
        root = copy_project(tmp_path)
        ls = loaded(root)
        gone = root / EXAMPLE / "my_second_dbt_model.sql"
        gone.unlink()

        notify(ls, gone, types.FileChangeType.Deleted)

        assert model_named(ls.state, "my_second_dbt_model") is None
        assert "my_second_dbt_model" not in ls.state.ref_names

    def test_its_table_is_left_as_an_undocumented_one(self, tmp_path):
        root = copy_project(tmp_path)
        ls = loaded(root)
        gone = root / EXAMPLE / "my_second_dbt_model.sql"
        gone.unlink()

        notify(ls, gone, types.FileChangeType.Deleted)

        assert "my_second_dbt_model" in {t.name for t in ls.state.undocumented}

    def test_columns_taken_from_its_sql_are_forgotten(self, tmp_path):
        root = copy_project(tmp_path)
        ls = loaded(root)
        ls.sql_columns["my_second_dbt_model"] = model_named(
            ls.state, "my_second_dbt_model"
        )
        gone = root / EXAMPLE / "my_second_dbt_model.sql"
        gone.unlink()

        notify(ls, gone, types.FileChangeType.Deleted)

        assert ls.sql_columns == {}

    def test_a_moved_file_keeps_the_model_at_its_new_path(self, tmp_path):
        root = copy_project(tmp_path)
        ls = loaded(root)
        old = root / EXAMPLE / "my_second_dbt_model.sql"
        new = root / "models" / "marts" / "my_second_dbt_model.sql"
        new.parent.mkdir()
        old.rename(new)

        notify(ls, old, types.FileChangeType.Deleted)
        notify(ls, new, types.FileChangeType.Created)

        model = model_named(ls.state, "my_second_dbt_model")
        assert model.path == new
        assert model.columns


def test_a_file_added_during_the_warehouse_read_is_kept(
    tmp_path, held_warehouse  # noqa: F811
):
    root = copy_project(tmp_path)
    ls = fake_server(root)

    async def scenario():
        load_project(ls)
        early = ls.state
        new = root / EXAMPLE / "stg_payments.sql"
        new.write_text("select 1")
        notify(ls, new, types.FileChangeType.Created)
        assert model_named(early, "stg_payments") is not None

        held_warehouse.release.set()
        await until(lambda: ls.state is not early)

        assert model_named(ls.state, "stg_payments") is not None

    asyncio.run(scenario())


class TestNotItself:
    def this(self):
        return Model(
            name="demo",
            path=Path(URI.removeprefix("file://")),
            columns=(Column("id"),),
        )

    def other(self):
        return Model(name="stg_cards", path=Path("/project/models/stg_cards.sql"))

    def test_ref_does_not_offer_the_model_being_edited(self):
        items = complete("select * from {{ ref('|", models=[self.this(), self.other()])

        assert labels(items) == ["stg_cards"]

    def test_from_does_not_offer_it_either(self):
        items = complete("select * from |", models=[self.this(), self.other()])

        assert labels(items) == ["stg_cards"]
