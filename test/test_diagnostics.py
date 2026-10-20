"""A source() call dbt would not compile is flagged, and says where it belongs."""

from types import SimpleNamespace

from lsprotocol import types

from dbt_ls.column import Column
from dbt_ls.diagnostics import diagnose, source_diagnostics
from dbt_ls.model import Model, WarehouseTable, discover_ref_names
from dbt_ls.scope import AstCache
from dbt_ls.server import SchemaSource, Settings, reload_sources, watched_files
from dbt_ls.source import SourceTable
from dbt_ls.state import ProjectState
from test.conftest import DBT_ROOT, FakeProject

ERP_ORDERS = SourceTable(
    name="orders",
    source_name="erp",
    schema="raw",
    path=f"{DBT_ROOT}/models/staging/_sources.yml",
)
RAW_REFUNDS = WarehouseTable(name="refunds", schema="raw")
CRM_CARDS = WarehouseTable(name="cards", schema="crm")

Warning = types.DiagnosticSeverity.Warning
Error = types.DiagnosticSeverity.Error


def check(text: str) -> list[types.Diagnostic]:
    return source_diagnostics(text, [ERP_ORDERS], [RAW_REFUNDS, CRM_CARDS], DBT_ROOT)


def test_a_declared_source_is_fine():
    assert check("select * from {{ source('erp', 'orders') }}") == []


def test_names_are_matched_case_insensitively():
    assert check("select * from {{ source('ERP', 'Orders') }}") == []


def test_a_warehouse_table_under_no_source_is_a_warning():
    (d,) = check("select * from {{ source('crm', 'cards') }}")

    assert d.severity == Warning
    assert d.message == (
        "No .yml file declares source 'crm'. The warehouse has it as crm.cards."
    )
    assert d.source == "dbt-ls"


def test_a_table_missing_from_a_declared_source_says_which_file():
    (d,) = check("select * from {{ source('erp', 'refunds') }}")

    # `erp` reads schema raw, where the warehouse has refunds.
    assert d.severity == Warning
    assert d.message == (
        "Source 'erp' has no table 'refunds'. "
        "Add it under 'erp' in models/staging/_sources.yml. "
        "The warehouse has it as raw.refunds."
    )


def test_what_nothing_knows_is_an_error():
    (d,) = check("select * from {{ source('erp', 'ordres') }}")

    assert d.severity == Error
    assert "The warehouse" not in d.message


def test_a_table_in_another_schema_is_not_offered_as_the_match():
    # crm.cards exists, but `erp` reads raw.
    (d,) = check("select * from {{ source('erp', 'cards') }}")

    assert d.severity == Error


def test_the_range_covers_both_arguments():
    text = "select *\nfrom {{ source('crm', 'cards') }} c"

    (d,) = check(text)

    line = text.split("\n")[1]
    assert (d.range.start.line, d.range.end.line) == (1, 1)
    assert d.range.start.character == line.index("'crm'")
    assert d.range.end.character == line.index("'cards'") + len("'cards'")


def test_every_call_is_checked():
    text = (
        "select * from {{ source('crm', 'cards') }}\n"
        "join {{ source('erp', 'orders') }} using (id)\n"
        "join {{ source('nope', 'x') }} using (id)\n"
    )

    assert [d.range.start.line for d in check(text)] == [0, 2]


def test_jinja_comments_are_skipped():
    text = "{# {{ source('nope', 'x') }} #}\nselect * from {{ source('crm', 'cards') }}"

    (d,) = check(text)

    assert d.range.start.line == 1


class TestReloadSources:
    YAML = """
version: 2
sources:
  - name: crm
    tables:
      - name: cards
"""

    def state(self, tmp_path):
        state = ProjectState(
            project=FakeProject(),
            profile_target=None,
            sources=[ERP_ORDERS],
            dbt_root=str(tmp_path),
        )
        state.warehouse = [WarehouseTable(name="cards", schema="crm")]
        state.undocumented = list(state.warehouse)
        return state

    def test_a_declared_table_stops_being_undocumented(self, tmp_path, monkeypatch):
        (tmp_path / "sources.yml").write_text(self.YAML)
        state = self.state(tmp_path)
        # Matching needs the target; this test is about the reload, not it.
        monkeypatch.setattr(
            "dbt_ls.state.undocumented_tables",
            lambda models, sources, tables, target: [
                t for t in tables if t.name not in {s.name for s in sources}
            ],
        )

        reload_sources(state, (SchemaSource.DATABASE,))

        assert [(s.source_name, s.name) for s in state.sources] == [("crm", "cards")]
        assert state.sources[0].path == str(tmp_path / "sources.yml")
        assert state.undocumented == []
        assert check_state(state, "{{ source('crm', 'cards') }}") == []

    def test_without_the_warehouse_it_is_left_alone(self, tmp_path):
        (tmp_path / "sources.yml").write_text(self.YAML)
        state = self.state(tmp_path)

        reload_sources(state, (SchemaSource.CONFIG,))

        assert state.undocumented == state.warehouse


def check_state(state: ProjectState, text: str) -> list[types.Diagnostic]:
    return source_diagnostics(text, state.sources, state.undocumented, state.dbt_root)


# ---------------------------------------------------------------- diagnose()

STG_CARDS = Model(
    name="stg_cards",
    path=f"{DBT_ROOT}/models/stg_cards.sql",
    columns=(Column("card_id"), Column("price"), Column("name")),
    all_columns=True,
)
STG_SETS = Model(
    name="stg_sets",
    path=f"{DBT_ROOT}/models/stg_sets.sql",
    columns=(Column("set_id"), Column("name")),
    all_columns=True,
)
# Columns from YAML only: maybe not all of them.
DOCUMENTED = Model(
    name="documented", path=f"{DBT_ROOT}/models/documented.sql", columns=(Column("id"),)
)
ERP_CARDS = SourceTable(
    name="cards",
    source_name="erp",
    columns=(Column("id"), Column("owner")),
    all_columns=True,
)


def problems(text: str, **pools) -> list[tuple[str, str]]:
    """(code, message) of each diagnostic for `text`, in document order."""
    pools.setdefault("models", [STG_CARDS, STG_SETS, DOCUMENTED])
    pools.setdefault("sources", [ERP_CARDS])
    state = ProjectState(project=FakeProject(), profile_target=None, **pools)
    state.dbt_root = DBT_ROOT
    cache = AstCache("duckdb")
    parsed = cache.refresh("file:///m.sql", text)
    return [(d.code, d.message) for d in diagnose(text, parsed, state)]


def codes(text: str, **pools) -> list[str]:
    return [code for code, _ in problems(text, **pools)]


class TestRef:
    def test_a_model_that_exists_is_fine(self):
        assert codes("select * from {{ ref('stg_cards') }}") == []

    def test_a_name_nothing_has_is_an_error(self):
        assert problems("select * from {{ ref('stg_crads') }}") == [
            ("unknown-ref", "No model, seed or snapshot is named 'stg_crads'.")
        ]

    def test_seeds_and_snapshots_count(self):
        text = "select * from {{ ref('country_codes') }}"

        assert codes(text, ref_names={"country_codes"}) == []

    def test_package_and_versioned_refs_are_not_judged(self):
        text = (
            "select * from {{ ref('dbt_utils', 'date_spine') }}\n"
            "join {{ ref('stg_crads', v=2) }} using (id)"
        )

        assert "unknown-ref" not in codes(text)


class TestQualifiedColumn:
    def test_a_column_the_model_has_is_fine(self):
        assert codes("select c.price from {{ ref('stg_cards') }} c") == []

    def test_a_column_the_model_does_not_have(self):
        assert problems("select c.colour from {{ ref('stg_cards') }} c") == [
            ("unknown-column", "Column 'colour' is not in model stg_cards.")
        ]

    def test_the_range_is_the_column_name(self):
        text = "select c.colour from {{ ref('stg_cards') }} c"
        state = ProjectState(
            project=FakeProject(), profile_target=None, models=[STG_CARDS]
        )
        parsed = AstCache("duckdb").refresh("file:///m.sql", text)

        (d,) = diagnose(text, parsed, state)

        assert (d.range.start.character, d.range.end.character) == (9, 15)
        assert d.severity == Warning

    def test_a_source_column(self):
        assert problems("select s.ownr from {{ source('erp', 'cards') }} s") == [
            ("unknown-column", "Column 'ownr' is not in source erp.cards.")
        ]

    def test_columns_documented_in_yaml_alone_are_not_judged(self):
        assert codes("select d.anything from {{ ref('documented') }} d") == []

    def test_a_warehouse_table_lists_every_column(self):
        raw = WarehouseTable(name="orders", schema="raw", columns=(Column("id"),))

        assert problems("select o.total from raw.orders o", undocumented=[raw]) == [
            ("unknown-column", "Column 'total' is not in warehouse table raw.orders.")
        ]

    def test_an_unknown_alias(self):
        assert problems("select x.price from {{ ref('stg_cards') }} c") == [
            ("unknown-alias", "No table is named or aliased 'x' here.")
        ]

    def test_the_table_name_qualifies_without_an_alias(self):
        raw = WarehouseTable(name="orders", schema="raw", columns=(Column("id"),))

        assert codes("select orders.id from raw.orders", undocumented=[raw]) == []

    def test_a_struct_field_is_not_an_unknown_alias(self):
        # `name.first` reads a field of the struct column `name`.
        assert codes("select name.first from {{ ref('stg_cards') }} c") == []

    def test_a_correlated_subquery_sees_the_outer_alias(self):
        text = (
            "select c.card_id from {{ ref('stg_cards') }} c\n"
            "where exists (select 1 from {{ ref('stg_sets') }} s "
            "where s.name = c.name)"
        )

        assert codes(text) == []


class TestBareColumn:
    def test_a_column_one_relation_has_is_fine(self):
        assert codes("select price from {{ ref('stg_cards') }}") == []

    def test_a_column_no_relation_has(self):
        text = (
            "select colour from {{ ref('stg_cards') }} c\n"
            "join {{ ref('stg_sets') }} s on s.set_id = c.card_id"
        )

        assert problems(text) == [
            (
                "unknown-column",
                "Column 'colour' is in none of: model stg_cards, model stg_sets.",
            )
        ]

    def test_a_column_two_relations_have_is_ambiguous(self):
        text = (
            "select name from {{ ref('stg_cards') }} c\n"
            "join {{ ref('stg_sets') }} s on s.set_id = c.card_id"
        )

        assert problems(text) == [
            (
                "ambiguous-column",
                "Column 'name' is ambiguous: model stg_cards and model stg_sets "
                "both have it.",
            )
        ]

    def test_a_using_column_is_not_ambiguous(self):
        text = (
            "select name from {{ ref('stg_cards') }}\n"
            "join {{ ref('stg_sets') }} using (name)"
        )

        assert codes(text) == []

    def test_a_relation_with_unknown_columns_silences_it(self):
        text = (
            "select colour from {{ ref('stg_cards') }} c\n"
            "join {{ ref('documented') }} d on d.id = c.card_id"
        )

        assert codes(text) == []

    def test_select_aliases_can_be_reused(self):
        text = (
            "select price * 2 as doubled from {{ ref('stg_cards') }}\n"
            "where doubled > 1 order by doubled"
        )

        assert codes(text) == []


class TestThroughCtes:
    def test_a_cte_exposes_what_it_selects(self):
        text = (
            "with priced as (select card_id, price * 2 as doubled "
            "from {{ ref('stg_cards') }})\n"
            "select p.doubled, p.price from priced p"
        )

        assert problems(text) == [
            ("unknown-column", "Column 'price' is not in cte priced.")
        ]

    def test_a_star_is_expanded(self):
        text = (
            "with c as (select * from {{ ref('stg_cards') }})\n"
            "select c.price, c.colour from c"
        )

        assert codes(text) == ["unknown-column"]

    def test_a_star_over_unknown_columns_silences_it(self):
        text = (
            "with d as (select * from {{ ref('documented') }})\n"
            "select d.anything from d"
        )

        assert codes(text) == []

    def test_an_unnamed_expression_silences_it(self):
        text = (
            "with c as (select card_id, count(*) from {{ ref('stg_cards') }} "
            "group by 1)\n"
            "select c.count_star from c"
        )

        assert codes(text) == []

    def test_inside_the_cte_too(self):
        text = (
            "with c as (select colour from {{ ref('stg_cards') }})\n"
            "select * from c"
        )

        assert codes(text) == ["unknown-column"]


def test_columns_wait_for_a_buffer_that_parses():
    # Mid-edit, the AST is from a repaired text: positions would be off.
    assert codes("select c.colour from {{ ref('stg_cards') }} c\nwhere c.") == []


def test_jinja_comments_are_not_checked():
    assert codes("{# {{ ref('gone') }} #}\nselect 1 as x") == []


class TestRefNames:
    def test_models_seeds_snapshots_and_packages(self, tmp_path):
        files = {
            "models/stg_cards.sql": "select 1",
            "models/py_model.py": "def model(dbt, session): ...",
            "seeds/country_codes.csv": "code\nFI",
            "snapshots/cards.sql": "{% snapshot cards_history %}select 1{% endsnapshot %}",
            "models/snapshots.yml": "snapshots:\n  - name: snap_orders\n",
            "dbt_packages/utils/models/util_model.sql": "select 1",
            "target/compiled/leftover.sql": "select 1",
        }
        for name, text in files.items():
            (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / name).write_text(text)

        names = discover_ref_names(str(tmp_path))

        assert {"stg_cards", "py_model", "country_codes", "util_model"} <= names
        assert {"cards_history", "snap_orders"} <= names
        assert "leftover" not in names

    def test_a_new_file_is_picked_up(self, tmp_path):
        state = ProjectState(
            project=FakeProject(), profile_target=None, dbt_root=str(tmp_path)
        )
        ls = SimpleNamespace(
            state=state,
            settings=Settings(),
            yaml_changed=False,
            workspace=SimpleNamespace(text_documents={}),
        )
        new = tmp_path / "models" / "fresh.sql"
        new.parent.mkdir()
        new.write_text("select 1")

        watched_files(
            ls,
            types.DidChangeWatchedFilesParams(
                [types.FileEvent(new.as_uri(), types.FileChangeType.Created)]
            ),
        )

        assert "fresh" in state.ref_names
