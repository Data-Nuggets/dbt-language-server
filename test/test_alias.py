import pytest
import sqlglot

from dbt_ls.alias import choose_alias, parse_alias_list, parse_aliases
from dbt_ls.scope import Position, dbt_dialect, query_blocks

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


def resolve(
    text: str, pos: tuple[int, int], alias: str, parsed_text: str | None = None
):
    """`choose_alias` with the AST wiring the server does for it.

    `pos` is (1-based line, 0-based column), matching Alias. `parsed_text` is
    the version the AST is built from — in the server that is the last buffer
    that parsed, which lags `text` while an expression is being typed. It
    defaults to `text` for the cases where nothing is mid-edit.
    """
    parsed_text = text if parsed_text is None else parsed_text
    ast = sqlglot.parse_one(parsed_text, read=dbt_dialect("postgres"))
    return choose_alias(
        cur_pos=Position(*pos),
        aliases=parse_alias_list(text),
        alias=alias,
        blocks=query_blocks(ast),
        sql=parsed_text,
    )


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
        # The trailing statement is what makes this parseable at all; sqlglot
        # rejects a WITH that is followed by nothing.
        "select * from x\n"
    )
    assert resolve(text, (2, 12), "e").ref == "events"
    assert resolve(text, (6, 12), "e").ref == "errors"


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
    chosen = resolve(CTES, pos, "a")
    assert (chosen.ref if chosen else None) == expected


def test_choose_alias_is_scoped_even_when_unambiguous():
    """`o` is declared once, but only in the final SELECT — being the only
    declaration is not a licence to offer it inside the CTEs above."""
    assert resolve(CTES, (11, 7), "o").ref == "orders"  # the block binding it
    assert resolve(CTES, (2, 12), "o") is None  # artists CTE
    assert resolve(CTES, (7, 13), "o") is None  # albums CTE


def test_choose_alias_falls_back_when_no_block_covers_the_cursor():
    """The AST lags the buffer, so a block being typed is not in it yet.

    Here the cursor is above every block the parse knows about — the case a
    jinja header or a half-written CTE at the top of the file produces. A lone
    declaration is the best available answer; an ambiguous one still isn't.
    """
    assert resolve(CTES, (1, 0), "o").ref == "orders"
    assert resolve(CTES, (1, 0), "a") is None


def test_choose_alias_unknown_alias():
    assert resolve(CTES, (2, 12), "nope") is None


def test_choose_alias_in_a_clause_typed_past_the_parsed_span():
    """A block reaches to where the next one starts, not to its own last token.

    sqlglot's offsets stop at whatever it managed to parse, so a WHERE typed
    after an otherwise complete SELECT sits beyond the CTE's own span. The AST
    here is the file without that WHERE, which is what the cache would hold.
    """
    live = (
        "with artists as (\n"
        "    select a.name\n"
        '    from {{ ref("int_artists") }} a\n'
        "    where a.\n"
        "),\n"
        "albums as (\n"
        "    select a.title\n"
        '    from {{ ref("int_albums") }} a\n'
        ")\n"
        "select * from albums\n"
    )
    parsed = live.replace("    where a.\n", "")

    chosen = resolve(live, (4, 12), "a", parsed_text=parsed)
    assert chosen is not None and chosen.ref == "int_artists"


def test_choose_alias_without_any_cte():
    """Blocks are not just CTEs — a plain SELECT binds aliases the same way."""
    text = (
        "select a.name\n"
        'from {{ ref("int_artists") }} a\n'
        "union all\n"
        "select a.title\n"
        'from {{ ref("int_albums") }} a\n'
    )
    assert resolve(text, (1, 8), "a").ref == "int_artists"
    assert resolve(text, (4, 8), "a").ref == "int_albums"


def test_repeated_alias_keeps_the_last_declaration():
    """Keyed by alias name, so a repeated alias collapses. Documented here
    because it is the limit a position-aware lookup would have to lift."""
    text = "{{ ref('accounts') }} a join {{ ref('orders') }} a"
    aliases = parse_aliases(text)
    assert list(aliases) == ["a"]
    assert aliases["a"].ref == "orders"
