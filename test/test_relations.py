"""A model's columns from its own SQL, and when those are all of them."""

from types import SimpleNamespace

import sqlglot
from pygls.workspace import TextDocument

from dbt_ls.column import Column
from dbt_ls.diagnostics import diagnose
from dbt_ls.model import Model
from dbt_ls.relations import model_columns, names_every_column
from dbt_ls.scope import AstCache, dbt_dialect, neutralise_jinja
from dbt_ls.server import columns_from_sql
from dbt_ls.state import ProjectState
from test.conftest import DBT_ROOT, FakeProject

STG_CARDS = Model(
    name="stg_cards",
    path=f"{DBT_ROOT}/models/stg_cards.sql",
    columns=(Column("card_id", "bigint"), Column("price", "decimal(18,2)")),
    all_columns=True,
)
DOCUMENTED = Model(
    name="documented", path=f"{DBT_ROOT}/models/documented.sql", columns=(Column("id"),)
)


def parse(sql: str):
    return sqlglot.parse_one(neutralise_jinja(sql), read=dbt_dialect("duckdb"))


def columns(sql: str):
    found = model_columns(parse(sql), [STG_CARDS, DOCUMENTED], [], [])
    return None if found is None else [(c.name, c.data_type) for c in found]


class TestModelColumns:
    def test_named_outright_with_the_types_they_pass_through(self):
        sql = (
            "select c.card_id, price, cast(price as int) as whole, 1 as one\n"
            "from {{ ref('stg_cards') }} c"
        )

        assert columns(sql) == [
            ("card_id", "bigint"),
            ("price", "decimal(18,2)"),
            ("whole", "INT"),
            ("one", None),
        ]

    def test_a_star_over_every_known_column(self):
        assert columns("select * from {{ ref('stg_cards') }}") == [
            ("card_id", "bigint"),
            ("price", "decimal(18,2)"),
        ]

    def test_a_star_over_columns_yaml_listed_is_unknown(self):
        assert columns("select * from {{ ref('documented') }}") is None

    def test_explicit_names_over_yaml_columns_are_still_known(self):
        assert columns("select id, other from {{ ref('documented') }}") == [
            ("id", None),
            ("other", None),
        ]

    def test_an_unnamed_expression_is_unknown(self):
        assert columns("select count(*) from {{ ref('stg_cards') }}") is None

    def test_through_ctes(self):
        sql = (
            "with c as (select * from {{ ref('stg_cards') }})\n"
            "select c.*, price * 2 as doubled from c"
        )

        assert columns(sql) == [
            ("card_id", "bigint"),
            ("price", "decimal(18,2)"),
            ("doubled", None),
        ]


def test_names_every_column():
    assert names_every_column(parse("select a, b as c from t"))
    assert not names_every_column(parse("select * from t"))
    assert not names_every_column(parse("select t.* from t"))
    assert not names_every_column(parse("select count(*) from t"))


class TestOnSave:
    UPSTREAM = "select card_id, price, colour from raw.cards"
    DOWNSTREAM = "select c.colour from {{ ref('stg_cards') }} c"

    def server(self, upstream_text: str):
        state = ProjectState(
            project=FakeProject(), profile_target=None, models=[STG_CARDS]
        )
        uri = f"file://{STG_CARDS.path}"
        cache = AstCache("duckdb")
        cache.refresh(uri, upstream_text)
        document = TextDocument(uri=uri, source=upstream_text)
        return uri, SimpleNamespace(
            state=state,
            ast_cache=cache,
            sql_columns={},
            workspace=SimpleNamespace(get_text_document=lambda _: document),
        )

    def downstream_codes(self, state):
        parsed = AstCache("duckdb").refresh("file:///d.sql", self.DOWNSTREAM)
        return [d.code for d in diagnose(self.DOWNSTREAM, parsed, state)]

    def test_a_column_added_upstream_is_known_downstream_at_once(self):
        uri, ls = self.server(self.UPSTREAM)
        assert self.downstream_codes(ls.state) == ["unknown-column"]

        assert columns_from_sql(ls, uri)

        (model,) = ls.state.models
        # Types the warehouse had are kept for what passes through.
        assert [(c.name, c.data_type) for c in model.columns] == [
            ("card_id", "bigint"),
            ("price", "decimal(18,2)"),
            ("colour", None),
        ]
        assert self.downstream_codes(ls.state) == []
        assert ls.sql_columns["stg_cards"].columns == model.columns

    def test_a_select_that_does_not_name_everything_changes_nothing(self):
        uri, ls = self.server("select * from raw.cards")

        assert not columns_from_sql(ls, uri)
        assert ls.state.models[0] is STG_CARDS

    def test_unchanged_columns_are_not_republished(self):
        uri, ls = self.server("select card_id, price from raw.cards")

        assert not columns_from_sql(ls, uri)
