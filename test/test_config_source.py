"""Documented columns from YAML properties files."""

from dbt_ls.model import enrich_models_from_config


def load(tmp_path, yaml_text: str) -> dict[str, list[str]]:
    (tmp_path / "schema.yml").write_text(yaml_text)
    return {
        m.name: [c.name for c in m.columns]
        for m in enrich_models_from_config(str(tmp_path))
    }


def test_entry_without_columns_does_not_break_the_project(tmp_path):
    # Used to raise KeyError, which failed the whole `config` source: one
    # model with only a description cost every model its documented columns.
    assert load(
        tmp_path,
        "models:\n"
        "  - name: only_described\n"
        "    description: no columns here\n"
        "  - name: documented\n"
        "    columns:\n"
        "      - name: id\n"
        "      - name: total\n"
        "  - name: empty_columns\n"
        "    columns:\n",
    ) == {"only_described": [], "documented": ["id", "total"], "empty_columns": []}


def test_malformed_entries_are_skipped(tmp_path):
    assert load(
        tmp_path,
        "models:\n"
        "  - just a string\n"
        "  - description: no name\n"
        "  - name: fine\n"
        "    columns:\n"
        "      - name: id\n",
    ) == {"fine": ["id"]}


def test_empty_models_key(tmp_path):
    assert load(tmp_path, "models:\n") == {}


def test_other_files_are_unaffected_by_a_columnless_entry(tmp_path):
    (tmp_path / "a.yml").write_text("models:\n  - name: bare\n")
    (tmp_path / "b.yml").write_text(
        "models:\n  - name: full\n    columns:\n      - name: id\n"
    )
    models = {m.name: m for m in enrich_models_from_config(str(tmp_path))}
    assert [c.name for c in models["full"].columns] == ["id"]
