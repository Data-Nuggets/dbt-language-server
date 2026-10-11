"""Jinja the SQL parser cannot see past must not cost a file its completions."""

from test.conftest import complete, labels, model

import pytest

from dbt_ls.scope import AstCache, neutralise_jinja

CARDS = model("stg_cards", "card_id", ("mana_cost", "INTEGER"))
ARTISTS = model("stg_artists", "artist_id", ("name", "VARCHAR"))

BODY = """SELECT
    c.|
FROM {{ ref("stg_cards") }} c"""


@pytest.mark.parametrize(
    "header",
    [
        "{{ config(materialized='table') }}",
        '{{config(materialized="table")}}',
        "{{- config(materialized='table') -}}",
        "{{\n  config(\n    materialized='table',\n    tags=['a', 'b']\n  )\n}}",
        # A dict argument that ends in `}}`, the same two characters that
        # close the jinja expression.
        "{{ config(meta={'owner': {'team': 'data'}}) }}",
        # A `)` inside a string argument does not close the call.
        "{{ config(post_hook='grant select on {{ this }} to role(x)') }}",
        "{{ config(alias='it\\'s') }}",
        "-- a comment first\n{{ config(materialized='view') }}",
        "{{ config(materialized='table') }}\n{% set limit = 10 %}",
    ],
)
@pytest.mark.parametrize(
    "dialect", ["duckdb", "postgres", "tsql", "databricks", "trino", "snowflake"]
)
def test_columns_complete_below_a_config_header(header, dialect):
    items = complete(f"{header}\n\n{BODY}", models=[CARDS], dialect=dialect)
    assert labels(items) == ["card_id", "mana_cost"]


def test_cte_names_complete_below_a_config_header():
    sql = """{{ config(materialized='table') }}
WITH cards AS (
    SELECT card_id FROM {{ ref("stg_cards") }}
)
SELECT * FROM |"""
    assert labels(complete(sql, models=[CARDS])) == ["cards"]


def test_scope_positions_are_not_shifted_by_the_header():
    # Two CTEs reuse the alias `a`; picking the right one depends on block
    # offsets still lining up with the buffer after the header is blanked.
    sql = """{{
  config(
    materialized='table'
  )
}}
WITH cards AS (
    SELECT a.card_id FROM {{ ref("stg_cards") }} a
),
artists AS (
    SELECT
        a.|
    FROM {{ ref("stg_artists") }} a
)
SELECT 1"""
    assert labels(complete(sql, models=[CARDS, ARTISTS])) == ["artist_id", "name"]


def test_config_between_statements_is_handled_too():
    sql = (
        """{% set x = 1 %}
{{ config(materialized='table') }}
{{ config(tags=['nightly']) }}
"""
        + BODY
    )
    assert labels(complete(sql, models=[CARDS])) == ["card_id", "mana_cost"]


class TestStandaloneExpressions:
    @pytest.mark.parametrize(
        "header",
        [
            "{{ log('building', info=True) }}",
            "{{ dbt_utils.some_setup() }}",
            "{{ config(materialized='table') }}\n{{ log('x') }}",
            "select 1;\n{{ log('second statement') }}",
        ],
    )
    def test_an_expression_opening_a_statement_is_ignored(self, header):
        items = complete(f"{header}\n{BODY}", models=[CARDS])
        assert labels(items) == ["card_id", "mana_cost"]

    @pytest.mark.parametrize(
        "text",
        [
            # Alone on its line, but it is the relation, not a header.
            'SELECT c.card_id\nFROM\n    {{ ref("stg_cards") }} c',
            "SELECT\n    {{ dbt_utils.star(ref('stg_cards')) }}\nFROM {{ ref('stg_cards') }} c",
            "SELECT c.card_id FROM {{ ref('stg_cards') }} c WHERE c.d > {{ var('d') }}",
        ],
    )
    def test_an_expression_inside_a_query_is_kept(self, text):
        assert neutralise_jinja(text) == text

    def test_ref_on_its_own_line_still_completes(self):
        sql = """SELECT
    c.|
FROM
    {{ ref("stg_cards") }} c"""
        assert labels(complete(sql, models=[CARDS])) == ["card_id", "mana_cost"]

    def test_dict_ending_in_braces_inside_a_query(self):
        sql = """SELECT
    {{ pivot({'a': {'b': 1}}) }} AS p,
    c.|
FROM {{ ref("stg_cards") }} c"""
        assert labels(complete(sql, models=[CARDS])) == ["card_id", "mana_cost"]


class TestBlocks:
    @pytest.mark.parametrize(
        "block",
        [
            "{% set latest %}\n  select max(card_id) from {{ ref('stg_cards') }}\n{% endset %}",
            "{%- set cols -%}\n  a, b, c\n{%- endset -%}",
            "{% call statement('x', fetch_result=True) %}\n  select 1\n{% endcall %}",
            # A block's body is not scanned for branches of its own.
            "{% set q %}\n {% if a %} x {% else %} y\n{% endset %}",
        ],
    )
    def test_set_and_call_bodies_are_ignored(self, block):
        items = complete(f"{block}\n{BODY}", models=[CARDS])
        assert labels(items) == ["card_id", "mana_cost"]

    def test_inline_set_is_not_a_block(self):
        text = "{% set n = 10 %}\nselect 1 {% endset %}"
        assert neutralise_jinja(text) == text


class TestBranches:
    SQL = """SELECT
    c.card_id
FROM {{ ref("stg_cards") }} c
{% if is_incremental() %}
WHERE c.FIRST
{% elif var('full') %}
WHERE c.SECOND
{% else %}
WHERE c.THIRD
{% endif %}"""

    @pytest.mark.parametrize("marker", ["FIRST", "SECOND", "THIRD"])
    def test_columns_complete_in_every_branch(self, marker):
        # Only the first branch reaches the parser, but the cursor may be in
        # any of them.
        sql = self.SQL.replace(f"c.{marker}", "c.|")
        sql = sql.replace("c.FIRST", "c.card_id > 1").replace(
            "c.SECOND", "c.card_id > 2"
        )
        sql = sql.replace("c.THIRD", "c.card_id > 3")
        assert labels(complete(sql, models=[CARDS])) == ["card_id", "mana_cost"]

    def test_only_the_first_branch_is_kept(self):
        blanked = neutralise_jinja(self.SQL)
        assert "FIRST" in blanked
        assert "SECOND" not in blanked and "THIRD" not in blanked

    def test_branch_choosing_the_relation_parses(self):
        # Parses, which keeps CTE and scope information for the file. The
        # alias `c` itself is still not bound: the alias patterns want it
        # right after the `}}`, and here an `{% endif %}` sits in between.
        sql = """SELECT c.card_id
FROM {% if target.name == 'dev' %} {{ ref("stg_cards") }} {% else %} {{ ref("stg_artists") }} {% endif %} c"""
        parsed = AstCache("duckdb").refresh("file:///m.sql", sql)
        assert parsed is not None
        assert "stg_cards" in parsed.source and "stg_artists" not in parsed.source

    def test_nested_branches(self):
        text = "{% if a %} A {% if b %} B {% else %} C {% endif %} D {% else %} E {% endif %} F"
        blanked = neutralise_jinja(text)
        assert [w for w in "ABCDEF" if f" {w} " in f" {blanked} "] == list("ABDF")

    def test_for_else(self):
        blanked = neutralise_jinja("{% for c in cols %} A {% else %} B {% endfor %} C")
        assert " A " in blanked and " C" in blanked and " B " not in blanked

    def test_single_branch_is_untouched(self):
        text = "select 1 {% if is_incremental() %} where a > 1 {% endif %}"
        assert neutralise_jinja(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "{{ config(materialized='table') }}\nselect 1",
        "{{\n config(\n  a=1\n )\n}}\nselect 1",
        "select 1 {{ config(meta={'a': {'b': 1}}) }} from x",
        "{% set q %}\n select 1\n{% endset %}\nselect {{ m({'a': 1}) }} from x",
        "select 1\n{% if a %}\nwhere x\n{% else %}\nwhere y\n{% endif %}\n",
        TestBranches.SQL,
    ],
)
def test_every_offset_survives(text):
    blanked = neutralise_jinja(text)
    assert blanked != text
    assert len(blanked) == len(text)
    assert [i for i, ch in enumerate(blanked) if ch == "\n"] == [
        i for i, ch in enumerate(text) if ch == "\n"
    ]
    # Whatever was not blanked is still exactly where it was.
    assert all(b == " " or b == a for a, b in zip(text, blanked))


@pytest.mark.parametrize(
    "text",
    [
        "select a.id from {{ ref('stg_cards') }} a",  # nothing to do
        "{{ config(materialized='table'",  # still being typed
        "{{ config(materialized='table') ",  # parens closed, braces not
        "select 1 from {{ ref('\njoin {{ ref('x') }} b",  # open string, then a real ref
        "select 1 {% if a %} where x {% else %} where y",  # no endif yet
        "{% set q %}\n select 1",  # no endset yet
        "select '{{ not jinja to us }}' from x",  # inside a SQL string
        "select 1 -- {{ config() }} in a comment",
    ],
)
def test_unfinished_and_ordinary_text_is_left_alone(text):
    assert neutralise_jinja(text) == text
