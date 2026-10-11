from pathlib import Path

from dbt_ls.server import Settings, _load_project, parse_schema_sources

PROJECT = str(Path(__file__).parent.parent / "testdata" / "project")


def test_database_source_fills_columns_from_duckdb():
    """The whole load path against the bundled DuckDB file.

    Only the warehouse is enabled, so a reader that fails — which
    `_load_project` logs and swallows — shows up here as missing columns.
    """
    state = _load_project(PROJECT, Settings(parse_schema_sources("database")))

    assert state.profile_target.type == "duckdb"
    columns = {m.name: m.columns for m in state.models}
    assert set(columns) == {"my_first_dbt_model", "my_second_dbt_model"}
    for name, model_columns in columns.items():
        assert [c.name for c in model_columns] == ["id"], name
        assert model_columns[0].data_type, name
