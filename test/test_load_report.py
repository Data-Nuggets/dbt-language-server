"""A load that cannot reach the warehouse still loads everything else."""

from pathlib import Path

import pytest

from dbt_ls import check
from dbt_ls.exceptions import NoDbtRootError, NoRootPathError
from dbt_ls.server import Settings, _load_project, parse_schema_sources
from dbt_ls.state import ProjectState

BUNDLED = str(Path(__file__).parent.parent / "testdata" / "project")

DUCKDB_PROFILE = """
demo:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: dev.duckdb
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A minimal dbt project with no profiles.yml, and no ~/.dbt to fall back on."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)

    root = tmp_path / "proj"
    (root / "models").mkdir(parents=True)
    (root / "dbt_project.yml").write_text(
        "name: demo\nprofile: demo\nmodel-paths: [models]\n"
    )
    (root / "models" / "stg_cards.sql").write_text("select 1 as card_id")
    (root / "models" / "schema.yml").write_text(
        "models:\n  - name: stg_cards\n    columns:\n      - name: card_id\n"
        "sources:\n  - name: raw\n    tables:\n      - name: cards\n"
    )
    return root


def load(root, sources: str = "config,catalog,database") -> ProjectState:
    return _load_project(str(root), Settings(parse_schema_sources(sources)))


def outcomes(state: ProjectState) -> dict[str, str]:
    return {s.source: s.outcome for s in state.report.sources}


def assert_disk_content_loaded(state: ProjectState):
    assert [m.name for m in state.models] == ["stg_cards"]
    assert [c.name for c in state.models[0].columns] == ["card_id"]
    assert [(s.source_name, s.name) for s in state.sources] == [("raw", "cards")]


class TestProfileProblems:
    def test_no_profiles_yml(self, project):
        state = load(project)
        assert_disk_content_loaded(state)
        assert state.profile_target is None
        assert "no profiles.yml" in state.report.profile_problem
        assert outcomes(state) == {
            "config": "ok",
            "catalog": "skipped",
            "database": "failed",
        }
        # Nothing names the dialect, so the parser gets the default.
        assert state.report.dialect == "postgres"

    def test_no_profile_key_in_the_project(self, project):
        (project / "dbt_project.yml").write_text("name: demo\nmodel-paths: [models]\n")
        state = load(project)
        assert_disk_content_loaded(state)
        assert "no `profile:` key" in state.report.profile_problem

    def test_profile_name_not_in_profiles_yml(self, project):
        (project / "profiles.yml").write_text("other:\n  target: dev\n  outputs: {}\n")
        state = load(project)
        assert_disk_content_loaded(state)
        assert "'demo'" in state.report.profile_problem

    def test_unset_env_var(self, project, monkeypatch):
        monkeypatch.delenv("DBT_LS_TEST_PATH", raising=False)
        (project / "profiles.yml").write_text(
            DUCKDB_PROFILE.replace(
                "dev.duckdb", "\"{{ env_var('DBT_LS_TEST_PATH') }}\""
            )
        )
        state = load(project)
        assert_disk_content_loaded(state)
        assert "DBT_LS_TEST_PATH" in state.report.profile_problem

    def test_missing_required_field(self, project):
        (project / "profiles.yml").write_text(
            "demo:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        )
        state = load(project)
        assert_disk_content_loaded(state)
        assert "path" in state.report.profile_problem

    def test_unreadable_profiles_yml(self, project):
        (project / "profiles.yml").write_text("demo: [unclosed")
        state = load(project)
        assert_disk_content_loaded(state)
        assert "could not read profiles.yml" in state.report.profile_problem

    def test_problem_is_only_reported_when_the_warehouse_is_wanted(self, project):
        state = load(project, "config")
        assert outcomes(state) == {"config": "ok"}
        # The profile problem is still stated, it just cost nothing here.
        assert state.report.problems == [f"profile: {state.report.profile_problem}"]


class TestSourceProblems:
    @pytest.fixture
    def with_profile(self, project):
        (project / "profiles.yml").write_text(DUCKDB_PROFILE)
        return project

    def test_missing_driver_names_the_extra(self, with_profile, monkeypatch):
        def no_driver(self):
            raise ImportError("no duckdb") from ModuleNotFoundError(
                "No module named 'duckdb'", name="duckdb"
            )

        monkeypatch.setattr(ProjectState, "refresh_from_database", no_driver)
        state = load(with_profile)
        assert_disk_content_loaded(state)
        database = state.report.sources[-1]
        assert database.outcome == "failed"
        assert database.detail == "missing package 'duckdb'; install dbt-ls[duckdb]"

    def test_any_other_failure_is_reported_and_survived(
        self, with_profile, monkeypatch
    ):
        def refuses(self):
            raise ConnectionError("warehouse says no\nwith a second line")

        monkeypatch.setattr(ProjectState, "refresh_from_database", refuses)
        state = load(with_profile)
        assert_disk_content_loaded(state)
        assert state.report.sources[-1].detail == "ConnectionError: warehouse says no"

    def test_a_failed_source_does_not_stop_later_ones(self, with_profile, monkeypatch):
        def broken(self):
            raise RuntimeError("bad yaml somewhere")

        monkeypatch.setattr(ProjectState, "refresh_from_config", broken)
        state = load(with_profile, "config,database")
        assert outcomes(state) == {"config": "failed", "database": "ok"}

    def test_adapter_without_a_reader_is_skipped_not_failed(self, project):
        (project / "profiles.yml").write_text(
            "demo:\n  target: dev\n  outputs:\n    dev:\n      type: snowflake\n"
        )
        state = load(project)
        assert state.report.ok
        assert state.report.dialect == "snowflake"
        database = state.report.sources[-1]
        assert database.outcome == "skipped"
        assert "'snowflake'" in database.detail


class TestNoProject:
    def test_no_root_path(self):
        with pytest.raises(NoRootPathError):
            _load_project(None, Settings())

    def test_no_dbt_project_yml(self, tmp_path):
        with pytest.raises(NoDbtRootError):
            load(tmp_path)

    def test_root_is_found_from_a_subfolder(self, project):
        assert load(project / "models", "config").dbt_root == str(project)


class TestCheckCommand:
    def test_bundled_project_passes(self, capsys):
        assert check.main([BUNDLED, "--schema-sources", "config,database"]) == 0
        out = capsys.readouterr().out
        assert "(duckdb)" in out
        assert "result: ok" in out
        # One row per source, with the warehouse supplying the types.
        row = next(
            line.split() for line in out.splitlines() if line.startswith("database")
        )
        assert row[1:3] == ["2", "2"] and row[-1] == "ok"

    def test_problems_give_exit_status_1(self, project, capsys):
        assert check.main([str(project)]) == 1
        out = capsys.readouterr().out
        assert "result: problems found" in out
        assert "no profiles.yml" in out

    def test_models_without_columns_are_named(self, project, capsys):
        (project / "models" / "int_bare.sql").write_text("select 1")
        check.main([str(project), "--schema-sources", "config"])
        out = capsys.readouterr().out
        assert "1 models have no columns from any source:\n  int_bare" in out

    def test_no_project_gives_exit_status_2(self, tmp_path, capsys):
        assert check.main([str(tmp_path)]) == 2
        assert "no dbt_project.yml" in capsys.readouterr().err

    def test_json(self, project, capsys):
        import json

        assert check.main([str(project), "--json", "--schema-sources", "config"]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data["ok"] is False
        assert data["models"] == 1
        assert data["sources"][0]["source"] == "config"
        assert data["sources"][0]["outcome"] == "ok"

    def test_unknown_schema_source_is_a_usage_error(self, project):
        with pytest.raises(SystemExit) as exit_info:
            check.main([str(project), "--schema-sources", "nonsense"])
        assert exit_info.value.code == 2
