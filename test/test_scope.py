"""Parsing a document that is still being typed.

Every state a CTE passes through while it is written is a parse error, so
without repair the cache holds an AST belonging to much older text — and the
offsets in it no longer describe the buffer the cursor is in.
"""

from typing import Iterator

import pytest

from dbt_ls.alias import choose_alias, parse_alias_list
from dbt_ls.scope import (
    PLACEHOLDER_COLUMN,
    AstCache,
    Position,
    query_blocks,
    repaired_variants,
)

URI = "file:///demo.sql"


def cache() -> AstCache:
    return AstCache("duckdb")


def resolve(parsed, text: str, pos: tuple[int, int], alias: str):
    """`choose_alias` wired the way the server wires it: aliases and the cursor
    come from the live buffer, blocks and offsets from the parse."""
    return choose_alias(
        cur_pos=Position(*pos),
        aliases=parse_alias_list(text),
        alias=alias,
        blocks=query_blocks(parsed.ast),
        sql=parsed.source,
    )


# The buffer at the moment the second CTE's select list is being typed: the
# final SELECT does not exist yet, and `a` is bound in both CTEs.
MID_EDIT = '''WITH cards AS (
    SELECT
        a.card_id AS id,
        a.mana_cost
    FROM {{ ref("int_cards") }} a
    WHERE a.mana_cost > 3
),
artists AS (
    SELECT
        a.
    FROM {{ ref("int_artists") }} a
    LEFT JOIN {{ source("src", "oracle_cards") }} oc
    ON a.id = oc.cardmarket_id
)'''


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(MID_EDIT, id="cte-only document, dangling dot"),
        pytest.param(
            'WITH cards AS (\n    SELECT\n    FROM {{ ref("int_cards") }} a\n)',
            id="empty select list",
        ),
        pytest.param(
            'WITH cards AS (\n    SELECT\n        a.id,\n    FROM {{ ref("x") }} a\n)',
            id="trailing comma",
        ),
        pytest.param("WITH cards AS (\n    SELECT\n        a.\n)", id="no from yet"),
    ],
)
def test_mid_edit_states_parse(text):
    parsed = cache().refresh(URI, text)
    assert parsed is not None
    # Repaired, not inherited from an earlier state: line count has to match
    # the buffer, or positions in it mean nothing.
    assert parsed.source.startswith(text.split("\n")[0])
    assert len(parsed.source.splitlines()) >= len(text.splitlines())


def test_reused_alias_resolves_to_the_enclosing_cte():
    """`a` is declared in both CTEs; the one in the cursor's CTE is the answer."""
    parsed = cache().refresh(URI, MID_EDIT)
    declaration = resolve(parsed, MID_EDIT, (10, 10), "a")
    assert declaration is not None
    assert declaration.ref == "int_artists"


def test_reused_alias_in_first_cte_still_resolves_there():
    parsed = cache().refresh(URI, MID_EDIT)
    declaration = resolve(parsed, MID_EDIT, (4, 10), "a")
    assert declaration is not None
    assert declaration.ref == "int_cards"


def test_placeholder_never_reaches_a_completion_list():
    parsed = cache().refresh(URI, MID_EDIT)
    exposed = [c.name for cte in parsed.ctes for c in cte.columns]
    assert PLACEHOLDER_COLUMN not in exposed


def test_a_parseable_document_is_left_alone():
    text = 'SELECT a.id FROM {{ ref("int_cards") }} a'
    parsed = cache().refresh(URI, text)
    assert parsed is not None
    assert parsed.source == text


def test_repair_ladder_is_lazy_and_skips_no_op_repairs():
    valid = 'SELECT a.id FROM {{ ref("int_cards") }} a'
    variants = repaired_variants(valid)
    # A generator, so nothing is substituted unless the raw parse failed.
    assert isinstance(variants, Iterator)
    # Text needing no repair does not get offered back unchanged; the only
    # candidate left is the one for a WITH still missing its final SELECT.
    assert list(variants) == [f"{valid}\nSELECT {PLACEHOLDER_COLUMN}"]


def test_unparseable_beyond_repair_keeps_the_previous_ast():
    c = cache()
    good = 'SELECT a.id FROM {{ ref("int_cards") }} a'
    c.refresh(URI, good)
    parsed = c.refresh(URI, "SELECT ))) FROM (((")
    assert parsed is not None
    assert parsed.source == good
