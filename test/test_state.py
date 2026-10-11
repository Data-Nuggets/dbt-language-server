import json
from test.conftest import DBT_ROOT, FakeProject, model

from dbt_ls.state import ProjectState


def state(*models) -> ProjectState:
    return ProjectState(
        project=FakeProject(),
        profile_target=None,
        models=list(models),
        dbt_root=DBT_ROOT,
    )


def write_run_results(tmp_path, *results: dict, **top_level) -> str:
    path = tmp_path / "run_results.json"
    path.write_text(json.dumps({"results": list(results), **top_level}))
    return str(path)


def test_run_results_without_compiled_code_are_skipped(tmp_path):
    # Older dbt versions, and every test or seed node, leave these fields out.
    path = write_run_results(
        tmp_path,
        {"status": "success", "unique_id": "model.p.stg_cards"},
        {"status": "pass", "unique_id": "test.p.not_null", "relation_name": None},
        metadata={},
        elapsed_time=0.1,
        args={},
        some_future_key=True,
    )
    cards = model("stg_cards", ("card_id", "INTEGER"))
    s = state(cards)
    s.refresh_from_run_results(path)
    assert s.models == [cards]


def test_run_result_columns_reach_the_model(tmp_path):
    path = write_run_results(
        tmp_path,
        {
            "status": "success",
            "unique_id": "model.p.int_cards",
            "compiled_code": "select c.card_id, 1 as one from db.main.stg_cards c",
        },
    )
    s = state(model("stg_cards", ("card_id", "INTEGER")), model("int_cards"))
    s.refresh_from_run_results(path)
    columns = s.models["int_cards"].columns
    assert [(c.name, c.data_type) for c in columns] == [
        ("card_id", "INTEGER"),
        ("one", None),  # computed, so no upstream column to take a type from
    ]


def test_unparseable_compiled_code_is_skipped(tmp_path):
    path = write_run_results(
        tmp_path,
        {"status": "success", "unique_id": "model.p.a", "compiled_code": "select ("},
    )
    s = state(model("a", "id"))
    s.refresh_from_run_results(path)
    assert [c.name for c in s.models[0].columns] == ["id"]


def test_half_written_file_is_ignored(tmp_path):
    path = tmp_path / "run_results.json"
    path.write_text('{"results": [')
    s = state(model("a", "id"))
    s.refresh_from_run_results(str(path))
    assert [c.name for c in s.models[0].columns] == ["id"]
