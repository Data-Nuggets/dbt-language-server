from test.conftest import complete, labels, model, source

from lsprotocol import types

CARDS = model("stg_cards", "card_id", ("mana_cost", "INTEGER"))
ARTISTS = model("stg_artists", "artist_id", ("name", "VARCHAR"))
ORACLE = source("src", "oracle_cards", "cardmarket_id", ("released_at", "DATE"))


class TestNoMatch:
    def test_cursor_outside_any_pattern(self):
        assert complete("sel|ect 1") is None

    def test_bare_string_is_not_a_ref(self):
        assert complete("select '|'", models=[CARDS]) is None


class TestRef:
    def test_offers_every_model(self):
        items = complete('select * from {{ ref("|") }}', models=[CARDS, ARTISTS])
        assert labels(items) == ["stg_cards", "stg_artists"]
        assert all(i.kind == types.CompletionItemKind.Reference for i in items)

    def test_partial_name_still_offers_every_model(self):
        # Filtering is the client's job; the server always serves the full pool.
        items = complete('{{ ref("stg_c|") }}', models=[CARDS, ARTISTS])
        assert labels(items) == ["stg_cards", "stg_artists"]

    def test_single_quotes_work_too(self):
        assert labels(complete("{{ ref('|') }}", models=[CARDS])) == ["stg_cards"]

    def test_detail_is_the_path_relative_to_the_dbt_root(self):
        items = complete("{{ ref('|') }}", models=[CARDS])
        assert items[0].label_details.description == "/models/stg_cards.sql"


class TestSource:
    def test_offers_every_source_table(self):
        items = complete('{{ source("|") }}', sources=[ORACLE])
        assert labels(items) == ["oracle_cards"]
        assert items[0].label_details.description == "src"

    def test_insert_text_fills_both_arguments(self):
        items = complete('{{ source("|") }}', sources=[ORACLE])
        assert items[0].insert_text == 'src", "oracle_cards'


class TestCte:
    SQL = """WITH cards AS (
    SELECT card_id FROM {{ ref("stg_cards") }}
),
artists AS (
    SELECT artist_id FROM {{ ref("stg_artists") }}
)
SELECT * FROM |"""

    def test_offers_ctes_declared_in_the_document(self):
        assert labels(complete(self.SQL, models=[CARDS, ARTISTS])) == [
            "cards",
            "artists",
        ]

    def test_after_join_too(self):
        sql = self.SQL.replace("SELECT * FROM |", "SELECT * FROM cards JOIN |")
        assert labels(complete(sql, models=[CARDS])) == ["cards", "artists"]


class TestColumn:
    def test_alias_bound_to_a_model(self):
        sql = """SELECT
    c.|
FROM {{ ref("stg_cards") }} c"""
        items = complete(sql, models=[CARDS])
        assert labels(items) == ["card_id", "mana_cost"]
        assert items[1].label_details.description == "INTEGER"
        assert all(i.kind == types.CompletionItemKind.Field for i in items)

    def test_alias_bound_to_a_source(self):
        sql = """SELECT
    o.|
FROM {{ source("src", "oracle_cards") }} o"""
        assert labels(complete(sql, sources=[ORACLE])) == [
            "cardmarket_id",
            "released_at",
        ]

    def test_alias_bound_to_a_cte_resolves_through_to_the_model(self):
        sql = """WITH cards AS (
    SELECT * FROM {{ ref("stg_cards") }}
)
SELECT
    c.|
FROM cards c"""
        assert labels(complete(sql, models=[CARDS])) == ["card_id", "mana_cost"]

    def test_same_alias_in_two_ctes_resolves_to_the_enclosing_one(self):
        sql = """WITH cards AS (
    SELECT a.card_id FROM {{ ref("stg_cards") }} a
),
artists AS (
    SELECT
        a.|
    FROM {{ ref("stg_artists") }} a
)
SELECT 1"""
        assert labels(complete(sql, models=[CARDS, ARTISTS])) == [
            "artist_id",
            "name",
        ]

    def test_unknown_alias_offers_nothing(self):
        sql = """SELECT
    zz.|
FROM {{ ref("stg_cards") }} c"""
        assert complete(sql, models=[CARDS]) == []

    def test_model_without_columns_offers_nothing(self):
        sql = """SELECT
    c.|
FROM {{ ref("stg_cards") }} c"""
        assert complete(sql, models=[model("stg_cards")]) == []
