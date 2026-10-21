"""Seeds: discovered from CSV, ref()-able, and treated like models from there."""

import json
from pathlib import Path
from types import SimpleNamespace

from lsprotocol import types

from dbt_ls.column import Column
from dbt_ls.diagnostics import diagnose
from dbt_ls.model import Model, enrich_models_from_catalog, enrich_models_from_config
from dbt_ls.project import Project
from dbt_ls.scope import AstCache
from dbt_ls.seed import discover_seeds, seed_columns
from dbt_ls.server import Settings, watched_files
from dbt_ls.state import ProjectState
from test.conftest import complete, hover, labels

PROJECT = Path(__file__).parent.parent / "testdata" / "project"


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def project(tmp_path: Path, extra: str = "") -> Path:
    write(tmp_path / "dbt_project.yml", f"name: p\nprofile: p\n{extra}")
    return tmp_path


def state_of(root: Path) -> ProjectState:
    return ProjectState(
        project=Project(str(root)), profile_target=None, dbt_root=str(root)
    )


class TestHeader:
    def test_names_every_column_in_order(self, tmp_path):
        path = write(tmp_path / "s.csv", "id, name ,price\n1,a,2\n")

        assert [c.name for c in seed_columns(path)] == ["id", "name", "price"]

    def test_a_byte_order_mark_is_not_part_of_the_first_name(self, tmp_path):
        path = tmp_path / "s.csv"
        path.write_bytes("﻿id,name\n".encode("utf-8"))

        assert [c.name for c in seed_columns(path)] == ["id", "name"]

    def test_an_empty_file_has_no_columns(self, tmp_path):
        assert seed_columns(write(tmp_path / "s.csv", "")) == ()


class TestDiscovery:
    def test_seeds_under_the_seed_paths(self, tmp_path):
        root = project(tmp_path, "seed-paths: ['data']\n")
        write(root / "data" / "lookups" / "rarities.csv", "id,rarity\n")
        write(root / "seeds" / "ignored.csv", "id\n")

        (seed,) = discover_seeds(str(root), Project(str(root)).seed_paths)

        assert seed.name == "rarities"
        assert seed.model_type == "seed"
        # The header is every column, unlike a YAML list.
        assert seed.all_columns

    def test_dbt_defaults_apply_when_the_project_leaves_paths_out(self, tmp_path):
        root = project(tmp_path)
        write(root / "models" / "m.sql", "select 1 as id")
        write(root / "seeds" / "s.csv", "id\n")

        names = {m.name: m.model_type for m in state_of(root).models}

        assert names == {"m": "", "s": "seed"}

    def test_the_bundled_project_has_one(self):
        seeds = [m for m in state_of(PROJECT).models if m.model_type == "seed"]

        assert [s.name for s in seeds] == ["card_rarities"]


class TestEnrichment:
    def test_yaml_says_where_it_is_loaded_but_not_its_columns(self, tmp_path):
        write(
            tmp_path / "seeds" / "_seeds.yml",
            "seeds:\n  - name: rarities\n    config:\n      schema: lookups\n"
            "    columns:\n      - name: id\n",
        )

        (entry,) = enrich_models_from_config(str(tmp_path))

        assert (entry.name, entry.schema, entry.columns) == ("rarities", "lookups", ())

    def test_the_catalog_types_a_seed(self, tmp_path):
        root = project(tmp_path)
        write(root / "seeds" / "rarities.csv", "id,rarity\n")
        catalog = {
            "nodes": {
                "seed.p.rarities": {
                    "unique_id": "seed.p.rarities",
                    "metadata": {"name": "rarities"},
                    "columns": {
                        "id": {"name": "id", "type": "integer"},
                        "rarity": {"name": "rarity", "type": "text"},
                    },
                }
            }
        }
        path = write(root / "target" / "catalog.json", json.dumps(catalog))
        state = state_of(root)

        (seed,) = enrich_models_from_catalog(state.models, path)

        assert [(c.name, c.data_type) for c in seed.columns] == [
            ("id", "integer"),
            ("rarity", "text"),
        ]


RARITIES = Path("/project/seeds/rarities.csv")


class TestLikeAModel:
    def seed(self):
        return Model(
            name="rarities",
            path=RARITIES,
            columns=(Column("id", "integer"), Column("rarity", "text")),
            model_type="seed",
            all_columns=True,
        )

    def test_ref_offers_it(self):
        assert labels(complete("select * from {{ ref('|", models=[self.seed()])) == [
            "rarities"
        ]

    def test_from_inserts_a_ref_marked_as_a_seed(self):
        (item,) = complete("select * from rar|", models=[self.seed()])

        assert item.label_details.description == "seed"
        assert item.text_edit.new_text == "{{ ref('rarities') }}"

    def test_hover_calls_it_a_seed(self):
        result = hover("select * from {{ ref('rar|ities') }}", models=[self.seed()])

        assert "(seed) rarities" in result.contents.value
        assert "`seeds/rarities.csv`" in result.contents.value

    def test_a_column_its_header_lacks_is_flagged(self):
        text = "select r.colour from {{ ref('rarities') }} r"
        state = ProjectState(
            project=SimpleNamespace(model_paths=(), seed_paths=()),
            profile_target=None,
            models=[self.seed()],
        )
        parsed = AstCache("duckdb").refresh("file:///m.sql", text)

        assert [d.message for d in diagnose(text, parsed, state)] == [
            "Column 'colour' is not in seed rarities."
        ]


class TestRefresh:
    def test_a_changed_header_keeps_the_types_already_found(self, tmp_path):
        root = project(tmp_path)
        csv = write(root / "seeds" / "rarities.csv", "id,rarity\n")
        state = state_of(root)
        # As the warehouse and a YAML location would leave it.
        typed = Model(
            name="rarities",
            path=Path(""),
            columns=(Column("id", "integer"), Column("rarity", "text")),
            schema="lookups",
            all_columns=True,
        )
        state.reconciliate_models([typed])
        csv.write_text("id,rarity,sort_order\n")

        state.refresh_seeds()

        (seed,) = state.models
        assert [(c.name, c.data_type) for c in seed.columns] == [
            ("id", "integer"),
            ("rarity", "text"),
            ("sort_order", None),
        ]
        assert seed.schema == "lookups"

    def test_the_watcher_adds_and_removes_seeds(self, tmp_path):
        root = project(tmp_path)
        old = write(root / "seeds" / "old.csv", "id\n")
        state = state_of(root)
        ls = SimpleNamespace(
            state=state,
            settings=Settings(),
            yaml_changed=False,
            files_changed=False,
            workspace=SimpleNamespace(text_documents={}),
        )
        old.unlink()
        new = write(root / "seeds" / "new.csv", "id\n")

        watched_files(
            ls,
            types.DidChangeWatchedFilesParams(
                [
                    types.FileEvent(old.as_uri(), types.FileChangeType.Deleted),
                    types.FileEvent(new.as_uri(), types.FileChangeType.Created),
                ]
            ),
        )

        assert [m.name for m in state.models] == ["new"]
        assert "new" in state.ref_names and "old" not in state.ref_names
