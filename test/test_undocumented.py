"""Warehouse tables the project has no YAML for still complete."""

import duckdb

from test.conftest import complete, labels, model, source
from dbt_ls.column import Column
from dbt_ls.model import WarehouseTable
from dbt_ls.server import Settings, _load_project, parse_schema_sources
from dbt_ls.source import SourceTable


def table(schema: str, name: str, *columns: str | tuple[str, str]) -> WarehouseTable:
    """An undocumented warehouse table, the way the state holds it."""
    return WarehouseTable(
        name=name,
        schema=schema,
        columns=tuple(Column(c) if isinstance(c, str) else Column(*c) for c in columns),
    )


ERP_CARDS = source("erp", "cards", "id")
RAW_ORDERS = table("raw", "orders", ("order_id", "BIGINT"), ("total", "DECIMAL(18,2)"))
RAW_REFUNDS = table("raw", "refunds", "refund_id")
CRM_ORDERS = table("crm", "orders", "crm_order_id")
UNDOCUMENTED = [RAW_ORDERS, RAW_REFUNDS, CRM_ORDERS]


def details(items):
    return [(i.label, i.label_details.description) for i in items]


class TestSourceCall:
    def test_first_argument_offers_them_after_the_documented_ones(self):
        items = complete(
            "select * from {{ source('|",
            sources=[ERP_CARDS],
            undocumented=[RAW_ORDERS],
        )

        assert details(items) == [
            ("cards", "erp"),
            ("orders", "raw · not in sources.yml"),
        ]
        assert items[1].insert_text == "raw', 'orders"
        # Clients sort by sort_text, then label: `~` is after every identifier.
        assert items[0].sort_text is None
        assert items[1].sort_text == "~orders"

    def test_second_argument_lists_the_tables_of_a_schema_without_yaml(self):
        items = complete(
            "select * from {{ source('raw', '|", undocumented=UNDOCUMENTED
        )

        assert details(items) == [
            ("orders", "not in sources.yml"),
            ("refunds", "not in sources.yml"),
        ]

    def test_second_argument_adds_what_a_documented_source_leaves_out(self):
        erp = SourceTable(name="orders", source_name="erp", schema="raw")

        items = complete(
            "select * from {{ source('erp', '|",
            sources=[erp],
            undocumented=UNDOCUMENTED,
        )

        # `orders` is the documented one; `refunds` is in the same schema.
        assert details(items) == [("orders", "erp"), ("refunds", "not in sources.yml")]

    def test_columns_through_an_undocumented_source_call(self):
        items = complete(
            "select\n    o.|\nfrom {{ source('raw', 'orders') }} o\n",
            undocumented=UNDOCUMENTED,
        )

        assert details(items) == [("order_id", "BIGINT"), ("total", "DECIMAL(18,2)")]

    def test_the_source_named_in_the_call_picks_between_same_named_tables(self):
        items = complete(
            "select\n    o.|\nfrom {{ source('crm', 'orders') }} o\n",
            undocumented=UNDOCUMENTED,
        )

        assert labels(items) == ["crm_order_id"]


class TestPlainTableName:
    def test_from_offers_them_after_the_ctes_as_source_calls(self):
        items = complete(
            "with recent as (select 1 as id)\nselect * from |",
            undocumented=[RAW_ORDERS, CRM_ORDERS],
        )

        assert details(items) == [
            ("recent", "CTE"),
            ("raw.orders", "not in sources.yml"),
            ("crm.orders", "not in sources.yml"),
        ]
        assert [i.sort_text for i in items] == [None, "~raw.orders", "~crm.orders"]
        # Never a hard-coded schema.table: the schema names the source.
        assert items[1].text_edit.new_text == "{{ source('raw', 'orders') }}"

    def test_from_works_in_a_file_that_does_not_parse(self):
        items = complete("select ((( from |", undocumented=[RAW_ORDERS])

        assert labels(items) == ["raw.orders"]

    def test_after_a_schema_only_its_tables(self):
        items = complete("select * from raw.|", undocumented=UNDOCUMENTED)

        assert details(items) == [
            ("orders", "not in sources.yml"),
            ("refunds", "not in sources.yml"),
        ]

    def test_the_typed_schema_is_replaced_too(self):
        line = "select * from raw.ord"
        (orders, _) = complete(line + "|", undocumented=UNDOCUMENTED)

        assert orders.text_edit.new_text == "{{ source('raw', 'orders') }}"
        assert orders.text_edit.range.start.character == line.index("raw")
        # Clients match the filter text against everything being replaced.
        assert orders.filter_text == "raw.orders"

    def test_after_a_schema_declared_sources_in_it_come_first(self):
        erp = SourceTable(name="orders", source_name="erp", schema="raw")

        items = complete(
            "select * from raw.|", sources=[erp], undocumented=[RAW_REFUNDS]
        )

        assert details(items) == [("orders", "erp"), ("refunds", "not in sources.yml")]
        assert items[0].text_edit.new_text == "{{ source('erp', 'orders') }}"

    def test_a_table_joins_the_declared_source_over_its_schema(self):
        # `raw` is read through the `erp` source, so dbt knows the table as
        # erp.refunds once it is added there.
        erp = SourceTable(name="orders", source_name="erp", schema="raw")

        items = complete("select * from |", sources=[erp], undocumented=[RAW_REFUNDS])

        assert labels(items) == ["erp.orders", "erp.refunds"]
        assert items[1].text_edit.new_text == "{{ source('erp', 'refunds') }}"

    def test_schema_dot_is_not_mistaken_for_an_alias_elsewhere(self):
        items = complete(
            "select\n    raw.|\nfrom {{ ref('stg_cards') }} raw\n",
            models=[model("stg_cards", "card_id")],
            undocumented=UNDOCUMENTED,
        )

        assert labels(items) == ["card_id"]

    def test_columns_of_a_schema_qualified_table(self):
        items = complete(
            "select\n    o.|\nfrom raw.orders o\n", undocumented=UNDOCUMENTED
        )

        assert details(items) == [("order_id", "BIGINT"), ("total", "DECIMAL(18,2)")]

    def test_the_schema_picks_between_same_named_tables(self):
        items = complete(
            "select\n    o.|\nfrom crm.orders as o\n", undocumented=UNDOCUMENTED
        )

        assert labels(items) == ["crm_order_id"]

    def test_a_bare_name_works_when_only_one_table_has_it(self):
        items = complete("select\n    r.|\nfrom refunds r\n", undocumented=UNDOCUMENTED)

        assert labels(items) == ["refund_id"]

    def test_a_bare_name_shared_by_two_schemas_is_not_guessed(self):
        items = complete("select\n    o.|\nfrom orders o\n", undocumented=UNDOCUMENTED)

        assert labels(items) == []

    def test_what_the_project_declares_wins_over_the_warehouse(self):
        items = complete(
            "select\n    o.|\nfrom orders o\n",
            models=[model("orders", "model_column")],
            undocumented=[RAW_ORDERS],
        )

        assert labels(items) == ["model_column"]

    def test_star_from_a_warehouse_table_expands_inside_a_cte(self):
        items = complete(
            "with o as (\n    select * from raw.orders\n)\nselect\n    o.|\nfrom o\n",
            undocumented=UNDOCUMENTED,
        )

        assert details(items) == [("order_id", "BIGINT"), ("total", "DECIMAL(18,2)")]

    def test_star_in_a_cte_takes_the_table_of_the_schema_named(self):
        items = complete(
            "with o as (\n    select * from crm.orders\n)\nselect\n    o.|\nfrom o\n",
            undocumented=UNDOCUMENTED,
        )

        assert labels(items) == ["crm_order_id"]

    def test_columns_qualified_by_the_table_name_keep_their_types_in_a_cte(self):
        items = complete(
            "with o as (\n    select orders.total from raw.orders\n)\n"
            "select\n    o.|\nfrom o\n",
            undocumented=UNDOCUMENTED,
        )

        assert details(items) == [("total", "DECIMAL(18,2)")]


def test_the_warehouse_read_keeps_what_matches_nothing(tmp_path):
    con = duckdb.connect(str(tmp_path / "dev.duckdb"))
    con.execute("create schema raw; create schema crm")
    con.execute("create table main.stg_cards (card_id bigint)")
    con.execute("create table raw.cards (id bigint)")
    con.execute("create table raw.orders (order_id bigint, total decimal(18,2))")
    con.execute("create table crm.accounts (account_id bigint)")
    con.close()
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "stg_cards.sql").write_text("select 1")
    (tmp_path / "models" / "sources.yml").write_text(
        "sources:\n  - name: erp\n    schema: raw\n    tables:\n      - name: cards\n"
    )
    (tmp_path / "dbt_project.yml").write_text(
        "name: demo\nprofile: demo\nmodel-paths: [models]\n"
    )
    (tmp_path / "profiles.yml").write_text(
        "demo:\n  target: dev\n  outputs:\n    dev:\n"
        "      type: duckdb\n      path: dev.duckdb\n"
    )

    state = _load_project(str(tmp_path), Settings(parse_schema_sources("database")))

    # The model's table and the documented source's table are spoken for.
    assert sorted(
        (t.schema, t.name, [(c.name, c.data_type) for c in t.columns])
        for t in state.undocumented
    ) == [
        ("crm", "accounts", [("account_id", "BIGINT")]),
        ("raw", "orders", [("order_id", "BIGINT"), ("total", "DECIMAL(18,2)")]),
    ]
    assert [s.name for s in state.sources] == ["cards"]


def test_without_a_warehouse_read_there_are_none(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: demo\nprofile: demo\n")

    state = _load_project(str(tmp_path), Settings(parse_schema_sources("config")))

    assert state.undocumented == []
