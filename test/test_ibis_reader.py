"""The shared reader for DuckDB, Postgres, MySQL and SQL Server."""

import ibis

from dbt_ls import load_yaml
from dbt_ls.model import _ibis_tables


def _duckdb(tmp_path):
    con = ibis.duckdb.connect(tmp_path / "w.duckdb")
    con.raw_sql("create schema raw")
    con.raw_sql("create table raw.cards (id bigint, price decimal(18,2), name varchar)")
    con.raw_sql("create table main.stg_cards (name varchar, id bigint)")
    return con


def test_all_tables_come_from_one_statement(tmp_path, monkeypatch):
    con = _duckdb(tmp_path)
    # The per-table path must not be needed.
    monkeypatch.setattr(con, "table", None)

    tables = {(t.schema, t.name): t for t in _ibis_tables(con, "duckdb")}

    assert set(tables) == {("raw", "cards"), ("main", "stg_cards")}
    assert [(c.name, c.data_type) for c in tables["raw", "cards"].columns] == [
        ("id", "BIGINT"),
        ("price", "DECIMAL(18,2)"),
        ("name", "VARCHAR"),
    ]
    # Declared order, not alphabetical.
    assert [c.name for c in tables["main", "stg_cards"].columns] == ["name", "id"]


def test_an_attached_database_is_not_read(tmp_path):
    con = _duckdb(tmp_path)
    con.raw_sql(f"attach '{tmp_path / 'other.duckdb'}' as other")
    con.raw_sql("create table other.main.elsewhere (id int)")

    names = {t.name for t in _ibis_tables(con, "duckdb")}

    assert "elsewhere" not in names


def test_falls_back_to_one_request_per_table(tmp_path, monkeypatch, caplog):
    con = _duckdb(tmp_path)

    raw_sql = con.raw_sql

    def refuse_ours(query, *args, **kwargs):
        # ibis sends its own statements as expressions; ours is the only text.
        if isinstance(query, str):
            raise RuntimeError("permission denied for information_schema")
        return raw_sql(query, *args, **kwargs)

    monkeypatch.setattr(con, "raw_sql", refuse_ours)

    tables = {(t.schema, t.name) for t in _ibis_tables(con, "duckdb")}

    assert tables == {("raw", "cards"), ("main", "stg_cards")}
    assert "falling back" in caplog.text


def test_load_yaml_reads_like_safe_load():
    assert load_yaml("models:\n  - name: a\n    columns: [{name: id}]\n") == {
        "models": [{"name": "a", "columns": [{"name": "id"}]}]
    }
    assert load_yaml("") is None
