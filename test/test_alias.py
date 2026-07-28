import pytest

from dbt_ls.alias import choose_alias, parse_alias_list, parse_aliases

CTES = """with artists as (
    select a.name
    from {{ ref("int_artists") }} a
),

albums as (
    select a.title
    from {{ ref("int_albums") }} a
)

select * from albums
left join {{ ref("orders") }} o
"""


def refs(text: str) -> dict[str, str]:
    """alias -> referenced model/table name, dropping the position data."""
    return {alias: a.ref for alias, a in parse_aliases(text).items()}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("{{ ref('accounts') }} a", {"a": "accounts"}),
        ('{{ ref("orders") }} o', {"o": "orders"}),
        ("{{ source('src', 'my_table') }} t", {"t": "my_table"}),
        (
            "{{ ref('accounts') }} a join {{ ref('orders') }} o",
            {"a": "accounts", "o": "orders"},
        ),
        ("select 1", {}),
        ("{{ ref('accounts') }} as a", {"a": "accounts"}),
        (
            "{{ ref('accounts') }} AS a join {{ ref('orders') }} o",
            {"a": "accounts", "o": "orders"},
        ),
        ("{{ source('src', 'my_table') }} as t", {"t": "my_table"}),
        (
            "{{ source('src', 'accounts') }} AS a join {{ ref('orders') }} o",
            {"a": "accounts", "o": "orders"},
        ),
        (
            """
        SELECT
            *
        FROM {{ ref('orders') }} o
         """,
            {"o": "orders"},
        ),
    ],
)
def test_parse_aliases(text, expected):
    assert refs(text) == expected


def test_alias_carries_its_own_name():
    (alias,) = parse_aliases("{{ ref('accounts') }} a").values()
    assert alias.alias == "a"
    assert alias.ref == "accounts"


def test_source_alias_is_an_alias_too():
    """Both branches must produce the same type, not Alias vs. bare str."""
    (alias,) = parse_aliases("{{ source('src', 'my_table') }} t").values()
    assert alias.alias == "t"
    assert alias.ref == "my_table"
    assert alias.start == 0


@pytest.mark.parametrize(
    "text, line_number, column_number, start",
    [
        ("{{ ref('orders') }} o", 1, 0, 0),
        ("  {{ ref('orders') }} o", 1, 2, 2),
        ("select 1\nfrom {{ ref('orders') }} o", 2, 5, 14),
        ("\n\n    {{ ref('orders') }} o", 3, 4, 6),
    ],
)
def test_alias_position(text, line_number, column_number, start):
    (alias,) = parse_aliases(text).values()
    assert (alias.line_number, alias.column_number, alias.start) == (
        line_number,
        column_number,
        start,
    )


def test_start_indexes_into_the_source_text():
    text = "select 1\nfrom {{ ref('orders') }} o"
    (alias,) = parse_aliases(text).values()
    assert text[alias.start :].startswith("{{ ref('orders') }}")


def test_parse_alias_list_keeps_repeated_aliases():
    """The dict form collapses these; the list form is what choose_alias needs."""
    aliases = parse_alias_list(CTES)
    assert [(a.alias, a.ref) for a in aliases] == [
        ("a", "int_artists"),
        ("a", "int_albums"),
        ("o", "orders"),
    ]


def test_parse_alias_list_includes_sources():
    aliases = parse_alias_list("{{ source('raw', 'events') }} e")
    assert [(a.alias, a.ref) for a in aliases] == [("e", "events")]


def test_parse_alias_list_is_in_document_order():
    """Refs and sources are scanned in separate passes, so without the sort the
    source below would land after the ref despite appearing first."""
    text = (
        "select 1\n"
        "from {{ source('raw', 'events') }} e\n"
        "join {{ ref('orders') }} o\n"
        "join {{ source('raw', 'users') }} u\n"
    )
    aliases = parse_alias_list(text)
    assert [a.alias for a in aliases] == ["e", "o", "u"]
    assert [a.start for a in aliases] == sorted(a.start for a in aliases)


def test_choose_alias_resolves_a_source():
    """Sources go through the same position-aware path as refs now."""
    text = (
        "with x as (\n"
        "    select e.id\n"
        "    from {{ source('raw', 'events') }} e\n"
        "),\n"
        "y as (\n"
        "    select e.id\n"
        "    from {{ source('raw', 'errors') }} e\n"
        ")\n"
    )
    aliases = parse_alias_list(text)
    assert choose_alias(aliases, (2, 12), "e").ref == "events"
    assert choose_alias(aliases, (6, 12), "e").ref == "errors"


@pytest.mark.parametrize(
    "pos, expected",
    [
        # `a.name` in the artists CTE; its FROM is on line 3.
        ((2, 12), "int_artists"),
        # `a.title` in the albums CTE; its FROM is on line 8.
        ((7, 13), "int_albums"),
        # Exactly on a declaration.
        ((3, 9), "int_artists"),
        # Past every declaration of `a`.
        ((20, 0), None),
    ],
)
def test_choose_alias_picks_by_position(pos, expected):
    """`pos` is (1-based line, 0-based column), matching Alias."""
    chosen = choose_alias(parse_alias_list(CTES), pos, "a")
    assert (chosen.ref if chosen else None) == expected


def test_choose_alias_ignores_position_when_unambiguous():
    """`o` is declared once, so any cursor resolves it — including one above."""
    for pos in [(1, 0), (12, 0), (99, 0)]:
        chosen = choose_alias(parse_alias_list(CTES), pos, "o")
        assert chosen is not None and chosen.ref == "orders"


def test_choose_alias_unknown_alias():
    assert choose_alias(parse_alias_list(CTES), (2, 12), "nope") is None


def test_repeated_alias_keeps_the_last_declaration():
    """Keyed by alias name, so a repeated alias collapses. Documented here
    because it is the limit a position-aware lookup would have to lift."""
    text = "{{ ref('accounts') }} a join {{ ref('orders') }} a"
    aliases = parse_aliases(text)
    assert list(aliases) == ["a"]
    assert aliases["a"].ref == "orders"
