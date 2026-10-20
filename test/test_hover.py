"""Hover over a ref, a source or an `alias.column` shows what it points at."""

from lsprotocol import types

from dbt_ls.column import Column
from dbt_ls.hover import MAX_COLUMNS
from dbt_ls.model import WarehouseTable
from dbt_ls.source import SourceTable
from test.conftest import DBT_ROOT, hover, model

CARDS = model("stg_cards", ("card_id", "bigint"), ("price", "decimal(18,2)"), "name")
ERP = SourceTable(
    name="cards",
    source_name="erp",
    database="lake",
    schema="raw",
    identifier="cards_v2",
    columns=(Column("id", "bigint"), Column("name", "varchar")),
)
CRM = WarehouseTable(
    name="cards",
    schema="crm",
    columns=(Column("crm_id", "bigint"), Column("owner")),
)


def text(result: types.Hover | None) -> str:
    assert result is not None, "no hover"
    return result.contents.value


def span(result: types.Hover) -> tuple[int, int]:
    return result.range.start.character, result.range.end.character


def signature(shown: str) -> str:
    """The first line inside the opening ```sql block."""
    assert shown.startswith("```sql\n"), shown
    return shown.split("\n")[1]


def columns(shown: str) -> list[str]:
    """The lines of the column block, the second ```sql block."""
    return shown.split("```sql\n")[2].split("\n```")[0].split("\n")


class TestRef:
    def test_shows_the_model_path_and_columns(self):
        shown = text(hover("select * from {{ ref('stg_c|ards') }}", models=[CARDS]))

        assert signature(shown) == "(model) stg_cards"
        assert "`models/stg_cards.sql`" in shown
        # Names padded so the types line up; an untyped column keeps its line.
        assert columns(shown) == [
            "card_id  bigint",
            "price    decimal(18,2)",
            "name",
        ]
        assert shown.endswith("*3 columns*")

    def test_range_covers_the_model_name(self):
        line = "select * from {{ ref('stg_cards') }}"
        result = hover(line.replace("stg_cards", "stg_|cards"), models=[CARDS])

        start = line.index("stg_cards")
        assert span(result) == (start, start + len("stg_cards"))

    def test_shows_the_configured_location(self):
        aliased = model("stg_cards")
        aliased = type(aliased)(
            name="stg_cards", path=aliased.path, schema="staging", alias="cards"
        )

        shown = text(hover("{{ ref('|stg_cards') }}", models=[aliased]))

        assert "configured with schema `staging` · alias `cards`" in shown

    def test_says_where_columns_would_come_from(self):
        shown = text(hover("{{ ref('|stg_cards') }}", models=[model("stg_cards")]))

        assert "No columns known" in shown

    def test_unknown_model_shows_nothing(self):
        assert hover("{{ ref('nope|') }}", models=[CARDS]) is None

    def test_wide_tables_are_cut_short(self):
        wide = model("wide", *(f"c{i}" for i in range(MAX_COLUMNS + 5)))

        shown = text(hover("{{ ref('wi|de') }}", models=[wide]))

        assert len(columns(shown)) == MAX_COLUMNS
        assert f"*first {MAX_COLUMNS} of {MAX_COLUMNS + 5} columns*" in shown

    def test_one_column_is_singular(self):
        shown = text(hover("{{ ref('on|e') }}", models=[model("one", "id")]))

        assert shown.endswith("*1 column*")


class TestSource:
    def test_shows_the_relation_and_columns(self):
        shown = text(
            hover("select * from {{ source('erp', 'ca|rds') }}", sources=[ERP, CRM])
        )

        assert signature(shown) == "(source) erp.cards"
        assert "reads `lake.raw.cards_v2`" in shown
        assert columns(shown) == ["id    bigint", "name  varchar"]

    def test_works_on_the_source_name_too(self):
        line = "{{ source('erp', 'cards') }}"
        result = hover(line.replace("erp", "e|rp"), sources=[ERP, CRM])

        assert signature(text(result)) == "(source) erp.cards"
        assert span(result) == (line.index("erp"), line.index("cards") + len("cards"))

    def test_an_undocumented_table_says_so(self):
        shown = text(hover("{{ source('crm', 'ca|rds') }}", undocumented=[CRM]))

        assert signature(shown) == "(warehouse table) crm.cards"
        assert "Not in sources.yml" in shown
        assert columns(shown) == ["crm_id  bigint", "owner"]

    def test_a_documented_source_wins_over_the_warehouse(self):
        shown = text(
            hover("{{ source('erp', 'ca|rds') }}", sources=[ERP], undocumented=[ERP])
        )

        assert signature(shown) == "(source) erp.cards"


class TestAliasColumn:
    def test_column_shows_its_type_and_owner(self):
        result = hover("select c.pri|ce from {{ ref('stg_cards') }} c", models=[CARDS])

        assert text(result) == (
            "```sql\n(column) price decimal(18,2)\n```\n\n---\n\n"
            "from model `stg_cards`"
        )
        assert span(result) == (9, 14)

    def test_alias_shows_the_relation(self):
        shown = text(
            hover("select |c.price from {{ ref('stg_cards') }} c", models=[CARDS])
        )

        assert signature(shown) == "(model) stg_cards"

    def test_column_of_a_source(self):
        shown = text(
            hover("select s.na|me from {{ source('erp', 'cards') }} s", sources=[ERP])
        )

        assert signature(shown) == "(column) name varchar"
        assert shown.endswith("from source `erp.cards`")

    def test_column_of_a_warehouse_table(self):
        shown = text(hover("select x.ow|ner from crm.cards x", undocumented=[CRM]))

        assert signature(shown) == "(column) owner"
        assert shown.endswith("from warehouse table `crm.cards`")

    def test_column_of_a_cte(self):
        sql = (
            "with priced as (select card_id, price from {{ ref('stg_cards') }})\n"
            "select p.pri|ce from priced p"
        )

        shown = text(hover(sql, models=[CARDS]))

        assert signature(shown) == "(column) price decimal(18,2)"
        assert shown.endswith("from cte `priced`")

    def test_column_the_relation_does_not_have(self):
        shown = text(
            hover("select c.co|lour from {{ ref('stg_cards') }} c", models=[CARDS])
        )

        assert signature(shown) == "(column) colour"
        assert "**Not found** among the 3 known columns of model `stg_cards`" in shown

    def test_column_of_a_relation_without_columns(self):
        shown = text(
            hover("select c.pri|ce from {{ ref('bare') }} c", models=[model("bare")])
        )

        assert "model `bare`, which has no known columns" in shown

    def test_alias_in_another_block_is_not_used(self):
        sql = (
            "with a as (select * from {{ ref('stg_cards') }} c)\n"
            "select c.pri|ce from a"
        )

        assert hover(sql, models=[CARDS]) is None

    def test_schema_dot_table_is_not_an_alias(self):
        assert hover("select * from ra|w.cards", sources=[ERP]) is None


def test_plain_sql_shows_nothing():
    assert hover("sel|ect 1", models=[CARDS]) is None


def test_model_path_is_relative_to_the_project():
    nested = model("stg_cards", path=f"{DBT_ROOT}/models/staging/stg_cards.sql")

    shown = text(hover("{{ ref('|stg_cards') }}", models=[nested]))

    assert "`models/staging/stg_cards.sql`" in shown
