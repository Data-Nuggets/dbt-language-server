"""Which warehouse table belongs to which model or source.

A table is identified by where it lives, not by its name alone: two schemas
holding a `stg_cards` hold two different tables.
"""

from pathlib import Path

import pytest

from dbt_ls.column import Column
from dbt_ls.model import Model, WarehouseTable, match_models, match_sources
from dbt_ls.profiles import ProfileTarget
from dbt_ls.server import Settings, _load_project, parse_schema_sources
from dbt_ls.source import SourceTable, discover_sources


def table(location: str, *cols: str) -> WarehouseTable:
    """`table("analytics.stg_cards", "id")` or `table("main.analytics.stg_cards", "id")`."""
    *where, name = location.split(".")
    schema = where[-1] if where else None
    database = where[0] if len(where) == 2 else None
    return WarehouseTable(
        name=name,
        schema=schema,
        database=database,
        columns=tuple(Column(c, "int") for c in cols or ("id",)),
    )


def model(name: str, *cols: str) -> Model:
    return Model(
        name=name,
        path=Path(f"models/{name}.sql"),
        columns=tuple(Column(c) for c in cols),
    )


def source(source_name: str, name: str, **fields) -> SourceTable:
    return SourceTable(name=name, source_name=source_name, **fields)


def names(relation) -> list[str]:
    return [c.name for c in relation.columns]


# database `main`, schema `analytics`
DATABRICKS = ProfileTarget.from_dict(
    {
        "type": "databricks",
        "catalog": "main",
        "schema": "analytics",
        "host": "https://x",
        "http_path": "/x",
        "token": "t",
    }
)
# schema `analytics`, no database level known to the reader
GLUE = ProfileTarget.from_dict(
    {
        "type": "glue",
        "role_arn": "arn",
        "region": "eu-west-1",
        "workers": 2,
        "worker_type": "G.1X",
        "schema": "analytics",
        "session_provisioning_timeout_in_seconds": 1,
        "location": "s3://b/",
    }
)


class TestModels:
    def matched(self, tables, target=GLUE, models=None) -> dict[str, list[str]]:
        models = models or [model("stg_cards")]
        return {m.name: names(m) for m in match_models(models, tables, target)}

    @pytest.mark.parametrize("order", [1, -1])
    def test_target_schema_wins_whatever_the_listing_order(self, order):
        tables = [
            table("analytics.stg_cards", "right"),
            table("dev_someone.stg_cards", "wrong"),
            table("zzz.stg_cards", "wrong_too"),
        ][::order]
        assert self.matched(tables) == {"stg_cards": ["right"]}

    def test_custom_schema_is_found_by_dbts_default_naming(self):
        # `+schema: staging` builds into `analytics_staging`.
        tables = [
            table("analytics_staging.stg_cards", "right"),
            table("other.stg_cards", "wrong"),
        ]
        assert self.matched(tables) == {"stg_cards": ["right"]}

    def test_a_single_table_of_that_name_anywhere_is_accepted(self):
        # Covers projects that rename schemas with their own macro.
        assert self.matched([table("staging.stg_cards", "only")]) == {
            "stg_cards": ["only"]
        }

    @pytest.mark.parametrize(
        "tables",
        [
            [table("a.stg_cards"), table("b.stg_cards")],
            [table("analytics_x.stg_cards"), table("analytics_y.stg_cards")],
        ],
    )
    def test_nothing_to_choose_between_means_no_match(self, tables):
        assert self.matched(tables) == {}

    def test_another_database_is_not_the_models(self):
        tables = [table("other_catalog.analytics.stg_cards", "wrong")]
        assert self.matched(tables, DATABRICKS) == {}
        tables.append(table("main.analytics.stg_cards", "right"))
        assert self.matched(tables, DATABRICKS) == {"stg_cards": ["right"]}

    def test_names_compare_case_insensitively(self):
        tables = [table("ANALYTICS.STG_CARDS", "ID")]
        assert self.matched(tables) == {"stg_cards": ["ID"]}

    def test_existing_columns_survive_when_the_warehouse_has_nothing(self):
        empty = WarehouseTable(name="stg_cards", schema="analytics")
        assert match_models([model("stg_cards", "documented")], [empty], GLUE) == []

    def test_identity_is_kept(self):
        (found,) = match_models(
            [model("stg_cards")], [table("analytics.stg_cards")], GLUE
        )
        assert found.path == Path("models/stg_cards.sql")


class TestSources:
    def matched(self, src: SourceTable, *tables) -> list[str]:
        (result,) = match_sources([src], list(tables))
        return names(result)

    def test_looked_up_in_the_schema_it_declares(self):
        src = source("erp", "orders", schema="raw")
        assert self.matched(
            src, table("analytics.orders", "wrong"), table("raw.orders", "right")
        ) == ["right"]

    def test_schema_defaults_to_the_source_name(self):
        # dbt's own rule when a source gives no `schema:`.
        src = source("erp", "orders")
        assert self.matched(
            src, table("raw.orders", "wrong"), table("erp.orders", "right")
        ) == ["right"]

    def test_a_table_in_another_schema_is_not_this_source(self):
        src = source("erp", "orders", schema="raw", columns=(Column("documented"),))
        assert self.matched(src, table("analytics.orders", "wrong")) == ["documented"]

    def test_database_is_respected_when_both_sides_know_it(self):
        # Models in one database, sources in another — a warehouse next to a
        # lakehouse.
        src = source("lake", "orders", schema="dbo", database="lakehouse")
        assert self.matched(src, table("warehouse.dbo.orders", "wrong")) == []
        assert self.matched(
            src,
            table("warehouse.dbo.orders", "wrong"),
            table("lakehouse.dbo.orders", "right"),
        ) == ["right"]

    def test_database_is_ignored_when_the_reader_does_not_report_one(self):
        src = source("lake", "orders", schema="dbo", database="lakehouse")
        assert self.matched(src, table("dbo.orders", "found")) == ["found"]

    def test_identifier_is_the_name_in_the_warehouse(self):
        src = source("erp", "orders", schema="raw", identifier="tbl_orders_v2")
        assert self.matched(
            src, table("raw.orders", "wrong"), table("raw.tbl_orders_v2", "right")
        ) == ["right"]

    def test_jinja_schema_falls_back_to_a_unique_name(self):
        src = source("erp", "orders", schema="{{ var('raw_schema') }}")
        assert self.matched(src, table("raw_prod.orders", "only")) == ["only"]
        assert self.matched(src, table("a.orders"), table("b.orders")) == []

    def test_jinja_database_is_treated_as_unknown(self):
        src = source("erp", "orders", schema="raw", database="{{ target.database }}")
        assert self.matched(src, table("anything.raw.orders", "found")) == ["found"]

    def test_sources_the_warehouse_lacks_are_kept(self):
        declared = [
            source("erp", "orders", schema="raw"),
            source("erp", "gone", schema="raw"),
        ]
        result = match_sources(declared, [table("raw.orders", "id")])
        assert [(s.name, names(s)) for s in result] == [
            ("orders", ["id"]),
            ("gone", []),
        ]

    def test_identifier_is_read_from_yaml(self, tmp_path):
        (tmp_path / "sources.yml").write_text(
            "sources:\n  - name: erp\n    schema: raw\n    tables:\n"
            "      - name: orders\n        identifier: tbl_orders_v2\n      - name: plain\n"
        )
        assert [
            (s.name, s.identifier, s.schema) for s in discover_sources(str(tmp_path))
        ] == [
            ("orders", "tbl_orders_v2", "raw"),
            ("plain", None, "raw"),
        ]


class TestEndToEndOnDuckDB:
    """A real database holding the same table name in three schemas."""

    @pytest.fixture
    def project(self, tmp_path):
        import duckdb

        con = duckdb.connect(str(tmp_path / "dev.duckdb"))
        con.execute("create schema dev_someone; create schema raw; create schema aaa")
        con.execute("create table aaa.stg_cards as select 1 as wrong_first")
        con.execute("create table main.stg_cards as select 1 as card_id, 'x' as name")
        con.execute("create table dev_someone.stg_cards as select 1 as wrong_last")
        con.execute("create table raw.cards as select 1 as raw_id")
        con.execute("create table main.cards as select 1 as not_the_source")
        con.close()

        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "stg_cards.sql").write_text("select 1")
        (tmp_path / "models" / "sources.yml").write_text(
            "sources:\n  - name: erp\n    schema: raw\n    tables:\n"
            "      - name: cards\n      - name: not_loaded_yet\n"
        )
        (tmp_path / "dbt_project.yml").write_text(
            "name: demo\nprofile: demo\nmodel-paths: [models]\n"
        )
        (tmp_path / "profiles.yml").write_text(
            "demo:\n  target: dev\n  outputs:\n    dev:\n"
            "      type: duckdb\n      path: dev.duckdb\n"
        )
        return tmp_path

    def test_model_and_source_each_get_their_own_table(self, project):
        state = _load_project(str(project), Settings(parse_schema_sources("database")))

        assert [(m.name, names(m)) for m in state.models] == [
            ("stg_cards", ["card_id", "name"])
        ]
        assert [(s.name, names(s)) for s in state.sources] == [
            ("cards", ["raw_id"]),
            # Declared but absent from the warehouse: still a source.
            ("not_loaded_yet", []),
        ]

    def test_source_location_survives_the_catalog_step(self, project):
        (project / "target").mkdir()
        (project / "target" / "catalog.json").write_text('{"nodes": {}, "sources": {}}')
        state = _load_project(
            str(project), Settings(parse_schema_sources("catalog,database"))
        )
        assert [(s.name, s.schema, names(s)) for s in state.sources][0] == (
            "cards",
            "raw",
            ["raw_id"],
        )
