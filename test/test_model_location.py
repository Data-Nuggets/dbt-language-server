"""Where a model says it is built: alias, custom schema, database."""

from pathlib import Path

import pytest

from dbt_ls.column import Column
from dbt_ls.model import (
    Model,
    WarehouseTable,
    enrich_models_from_config,
    inline_location,
    match_models,
    read_inline_locations,
)
from dbt_ls.profiles import ProfileTarget
from dbt_ls.server import Settings, _load_project, parse_schema_sources

# schema `analytics`, no database level
TARGET = ProfileTarget.from_dict(
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


def table(location: str, *cols: str) -> WarehouseTable:
    *where, name = location.split(".")
    return WarehouseTable(
        name=name,
        schema=where[-1],
        database=where[0] if len(where) == 2 else None,
        columns=tuple(Column(c, "int") for c in cols or ("id",)),
    )


def names(relation) -> list[str]:
    return [c.name for c in relation.columns]


class TestInlineConfig:
    @pytest.mark.parametrize(
        "sql, expected",
        [
            ("{{ config(alias='fct_orders') }}", {"alias": "fct_orders"}),
            (
                '{{ config(materialized="table", alias = "fct_orders", schema="staging") }}',
                {"alias": "fct_orders", "schema": "staging"},
            ),
            (
                "{{\n  config(\n    alias='a',\n    database='lake'\n  )\n}}\nselect 1",
                {"alias": "a", "database": "lake"},
            ),
            ("{{- config(alias='a') -}}", {"alias": "a"}),
            # Two calls in one file are merged by dbt, so here too.
            (
                "{{ config(alias='a') }}{{ config(schema='s') }}",
                {"alias": "a", "schema": "s"},
            ),
            # The live call, not the commented-out one.
            (
                "{# {{ config(alias='old') }} #}{{ config(alias='new') }}",
                {"alias": "new"},
            ),
        ],
    )
    def test_literal_values_are_read(self, sql, expected):
        assert inline_location(sql) == expected

    @pytest.mark.parametrize(
        "sql",
        [
            "select 1",
            "{{ config(materialized='view') }} select 1 as alias",
            "select alias, schema from x",
            # Not literals: only dbt can say what these come to.
            "{{ config(alias=var('x')) }}",
            "{{ config(alias='fct_' ~ suffix) }}",
            "{{ config(alias=my_alias) }}",
            "{{ config(alias='{{ nested }}') }}",
            "{{ config(alias='') }}",
            # The word appears, but not as an argument of config().
            "{{ config(meta={'alias': 'no'}) }}",
            "{{ config(grants=dict(schema='no')) }}",
            "{{ config(post_hook=\"comment on schema='x', alias='no'\") }}",
            "{{ config(post_hook='x', tags=['alias', 'schema']) }}",
            # Unfinished call.
            "{{ config(alias='a'",
        ],
    )
    def test_everything_else_declares_nothing(self, sql):
        assert inline_location(sql) == {}

    def test_argument_next_to_a_nested_one(self):
        sql = (
            "{{ config(meta={'alias': 'no'}, alias='yes', post_hook=\"alias='no'\") }}"
        )
        assert inline_location(sql) == {"alias": "yes"}

    def test_files_are_read_and_fileless_models_skipped(self, tmp_path):
        aliased = tmp_path / "orders.sql"
        aliased.write_text("{{ config(alias='fct_orders') }}\nselect 1")
        plain = tmp_path / "plain.sql"
        plain.write_text("select 1")
        models = [
            Model("orders", aliased, columns=(Column("documented"),)),
            Model("plain", plain),
            Model("yaml_only", Path("")),
            Model("from_run", Path("<run-result>")),
            Model("deleted", tmp_path / "gone.sql"),
        ]
        located = read_inline_locations(models)
        assert [(m.name, m.alias) for m in located] == [("orders", "fct_orders")]
        # Carries no columns of its own, so merging it cannot wipe any.
        assert located[0].columns == ()


class TestYamlConfig:
    def load(self, tmp_path, yaml_text: str) -> dict[str, Model]:
        (tmp_path / "schema.yml").write_text(yaml_text)
        return {m.name: m for m in enrich_models_from_config(str(tmp_path))}

    def test_location_is_read_from_the_config_block(self, tmp_path):
        models = self.load(
            tmp_path,
            "models:\n"
            "  - name: orders\n"
            "    config:\n"
            "      alias: fct_orders\n"
            "      schema: marts\n"
            "      database: \"{{ env_var('DB') }}\"\n"
            "    columns:\n"
            "      - name: id\n",
        )
        orders = models["orders"]
        assert (orders.alias, orders.schema, orders.database) == (
            "fct_orders",
            "marts",
            None,
        )
        assert names(orders) == ["id"]

    def test_entry_without_columns_does_not_break_the_file(self, tmp_path):
        # Used to raise KeyError and take every documented column in the
        # project down with it.
        models = self.load(
            tmp_path,
            "models:\n"
            "  - name: only_described\n"
            "    description: no columns here\n"
            "  - name: documented\n"
            "    columns:\n"
            "      - name: id\n"
            "  - name: empty_columns\n"
            "    columns:\n",
        )
        assert names(models["documented"]) == ["id"]
        assert models["only_described"].columns == ()
        assert models["empty_columns"].columns == ()


class TestMatching:
    def matched(self, model: Model, *tables) -> list[str] | None:
        found = match_models([model], list(tables), TARGET)
        return names(found[0]) if found else None

    def test_alias_names_the_table(self):
        orders = Model("orders", Path("m.sql"), alias="fct_orders")
        assert self.matched(
            orders,
            # A different table that happens to carry the model's name.
            table("analytics.orders", "wrong"),
            table("analytics.fct_orders", "right"),
        ) == ["right"]

    def test_aliased_model_does_not_fall_back_to_its_own_name(self):
        orders = Model("orders", Path("m.sql"), alias="fct_orders")
        assert self.matched(orders, table("analytics.orders", "wrong")) is None

    def test_custom_schema_uses_dbts_default_name_first(self):
        stg = Model("stg_cards", Path("m.sql"), schema="staging")
        assert self.matched(
            stg,
            table("staging.stg_cards", "override_style"),
            table("analytics_staging.stg_cards", "default_style"),
            table("analytics_other.stg_cards", "wrong"),
        ) == ["default_style"]

    def test_custom_schema_on_its_own(self):
        # A project whose generate_schema_name uses the custom name as is.
        stg = Model("stg_cards", Path("m.sql"), schema="staging")
        assert self.matched(
            stg,
            table("staging.stg_cards", "right"),
            table("elsewhere.stg_cards", "wrong"),
        ) == ["right"]

    def test_custom_schema_falls_back_to_the_target_schema(self):
        # Projects that only apply custom schemas in production build
        # everything into the target schema in development.
        stg = Model("stg_cards", Path("m.sql"), schema="staging")
        assert self.matched(stg, table("analytics.stg_cards", "dev")) == ["dev"]

    def test_declared_database_replaces_the_targets(self):
        lake = Model("events", Path("m.sql"), database="lake")
        assert self.matched(
            lake,
            table("warehouse.analytics.events", "wrong"),
            table("lake.analytics.events", "right"),
        ) == ["right"]

    def test_alias_and_custom_schema_together(self):
        orders = Model("orders", Path("m.sql"), alias="fct_orders", schema="marts")
        assert self.matched(
            orders,
            table("analytics.fct_orders", "wrong_schema"),
            table("analytics_marts.fct_orders", "right"),
        ) == ["right"]


class TestMerging:
    def test_location_survives_later_column_sources(self):
        base = Model("orders", Path("m.sql"), alias="fct_orders")
        with_columns = Model("orders", Path(""), columns=(Column("id"),))
        merged = base.merged_with(with_columns)
        assert (merged.alias, names(merged), merged.path) == (
            "fct_orders",
            ["id"],
            Path("m.sql"),
        )

    def test_a_later_declaration_wins_field_by_field(self):
        yaml = Model("orders", Path("m.sql"), alias="from_yaml", schema="marts")
        inline = Model("orders", Path("m.sql"), alias="from_file")
        merged = yaml.merged_with(inline)
        assert (merged.alias, merged.schema) == ("from_file", "marts")


class TestEndToEndOnDuckDB:
    @pytest.fixture
    def project(self, tmp_path):
        import duckdb

        con = duckdb.connect(str(tmp_path / "dev.duckdb"))
        con.execute("create schema main_staging")
        con.execute("create table main.orders as select 1 as not_the_model")
        con.execute("create table main.fct_orders as select 1 as order_id, 2 as total")
        con.execute("create table main.dim_customers as select 1 as customer_id")
        con.execute("create table main_staging.stg_cards as select 1 as card_id")
        con.execute("create table main.stg_cards as select 1 as leftover")
        con.close()

        models = tmp_path / "models"
        models.mkdir()
        (models / "orders.sql").write_text(
            "{{ config(materialized='table', alias='fct_orders') }}\nselect 1"
        )
        (models / "customers.sql").write_text("select 1")
        (models / "stg_cards.sql").write_text(
            "{{ config(schema='staging') }}\nselect 1"
        )
        (models / "schema.yml").write_text(
            "models:\n"
            "  - name: customers\n"
            "    config:\n"
            "      alias: dim_customers\n"
            "  - name: orders\n"
            "    description: aliased in its own file\n"
        )
        (tmp_path / "dbt_project.yml").write_text(
            "name: demo\nprofile: demo\nmodel-paths: [models]\n"
        )
        (tmp_path / "profiles.yml").write_text(
            "demo:\n  target: dev\n  outputs:\n    dev:\n"
            "      type: duckdb\n      path: dev.duckdb\n"
        )
        return tmp_path

    def test_each_model_gets_the_table_it_is_built_as(self, project):
        state = _load_project(
            str(project), Settings(parse_schema_sources("config,database"))
        )
        assert {m.name: names(m) for m in state.models} == {
            "orders": ["order_id", "total"],  # alias in the file
            "customers": ["customer_id"],  # alias in YAML
            "stg_cards": ["card_id"],  # custom schema, not the leftover in main
        }
