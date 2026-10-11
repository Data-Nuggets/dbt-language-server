import pytest

from dbt_ls import cli


def test_help_mentions_the_check_subcommand(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dbt-ls", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert "check [path]" in out
    assert "dbt-ls check --help" in out


def test_check_has_its_own_help(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dbt-ls", "check", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    assert "--schema-sources" in capsys.readouterr().out
