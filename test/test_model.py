from pathlib import Path

import pytest

from src.dbt_ls.model import Model, discover_models


def test_discover_models():
    models = discover_models("testdata/project", ["models"])
    assert set(models) == {
        Model(
            name="my_first_dbt_model",
            path=Path("testdata/project/models/example/my_first_dbt_model.sql"),
        ),
        Model(
            name="my_second_dbt_model",
            path=Path("testdata/project/models/example/my_second_dbt_model.sql"),
        ),
    }


def test_aws_login_errors_are_translated():
    """server.py must never name a botocore type: it is not installed without
    the `aws` extra, and importing it there broke `pip install dbt-ls[duckdb]`."""
    from botocore.exceptions import LoginRefreshRequired

    from dbt_ls.exceptions import AWSLoginRequiredError
    from dbt_ls.model import translates_aws_login_errors

    @translates_aws_login_errors
    def expired():
        raise LoginRefreshRequired(error_msg="token expired")

    with pytest.raises(AWSLoginRequiredError) as exc_info:
        expired()
    assert isinstance(exc_info.value.__cause__, LoginRefreshRequired)


def test_unrelated_errors_are_not_translated():
    from dbt_ls.model import translates_aws_login_errors

    @translates_aws_login_errors
    def boom():
        raise ValueError("unrelated")

    with pytest.raises(ValueError):
        boom()
