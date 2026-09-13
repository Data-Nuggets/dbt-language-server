import pytest

from dbt_ls.exceptions import ProfileTargetError
from dbt_ls.profiles import (
    AthenaTarget,
    DatabaseTarget,
    DatabricksTarget,
    DuckDBTarget,
    GlueTarget,
    MSSQLTarget,
    MySQLTarget,
    ProfileTarget,
    Secret,
    SparkTarget,
)


@pytest.mark.parametrize(
    "target_dict, expected",
    [
        pytest.param(
            {"type": "duckdb", "threads": 1, "path": "dev.duckdb"},
            DuckDBTarget(type="duckdb", threads=1, path="dev.duckdb"),
        ),
        pytest.param(
            {
                "type": "mysql",
                "threads": 4,
                "user": "root",
                "password": "passwd",
                "server": "127.0.0.1",
                "port": 1433,
                "schema": "dev",
            },
            MySQLTarget(
                type="mysql",
                threads=4,
                user="root",
                password=Secret("passwd"),
                server="127.0.0.1",
                port=1433,
                schema="dev",
            ),
        ),
        pytest.param(
            {
                "type": "postgres",
                "threads": 4,
                "user": "postgres",
                "password": "passwd",
                "host": "127.0.0.1",
                "port": 5432,
                "schema": "dev",
                "dbname": "mydb",
            },
            DatabaseTarget(
                type="postgres",
                threads=4,
                user="postgres",
                password=Secret("passwd"),
                host="127.0.0.1",
                port=5432,
                schema="dev",
                dbname="mydb",
            ),
        ),
        pytest.param(
            {
                "type": "sqlserver",
                "threads": 4,
                "user": "sa",
                "password": "root",
                "server": "127.0.0.1",
                "port": 1234,
                "schema": "dev",
                "database": "mydb",
                "driver": "{MSSQL DRIVER}",
                "encrypt": False,
            },
            MSSQLTarget(
                type="sqlserver",
                threads=4,
                user="sa",
                password=Secret("root"),
                server="127.0.0.1",
                port=1234,
                schema="dev",
                database="mydb",
                driver="{MSSQL DRIVER}",
                encrypt=False,
            ),
        ),
        pytest.param(
            {
                "type": "spark",
                "method": "session",
                "host": "localhost",
                "schema": "default",
            },
            SparkTarget(
                type="spark", method="session", host="localhost", schema="default"
            ),
        ),
        pytest.param(
            {
                "type": "databricks",
                "catalog": "mycatalog",
                "schema": "default",
                "host": "https://adb-23452935842.2.azuredatabricks.net",
                "http_path": "/sql/1.0/warehouses/29348rw7edf",
                "token": "mytoken",
                "threads": 1,
            },
            DatabricksTarget(
                type="databricks",
                catalog="mycatalog",
                schema="default",
                host="https://adb-23452935842.2.azuredatabricks.net",
                http_path="/sql/1.0/warehouses/29348rw7edf",
                token=Secret("mytoken"),
                threads=1,
            ),
        ),
        pytest.param(
            {
                "type": "databricks",
                "catalog": "mycatalog",
                "schema": "default",
                "host": "https://adb-23452935842.2.azuredatabricks.net",
                "http_path": "/sql/1.0/warehouses/29348rw7edf",
                "client_id": "clientid123",
                "client_secret": "clientsecret123",
                "threads": 1,
            },
            DatabricksTarget(
                type="databricks",
                catalog="mycatalog",
                schema="default",
                host="https://adb-23452935842.2.azuredatabricks.net",
                http_path="/sql/1.0/warehouses/29348rw7edf",
                client_id="clientid123",
                client_secret=Secret("clientsecret123"),
                threads=1,
            ),
        ),
        pytest.param(
            {
                "type": "athena",
                "s3_staging_dir": "s3://mybucket/staging/",
                "s3_data_dir": "s3://mybucket/data/",
                "region_name": "eu-west-1",
                "schema": "dev",
                "database": "awsdatacatalog",
                "aws_profile_name": "myprofile",
                "aws_access_key_id": "AKIA123",
                "aws_secret_access_key": "secretkey123",
                "threads": 4,
            },
            AthenaTarget(
                type="athena",
                s3_staging_dir="s3://mybucket/staging/",
                s3_data_dir="s3://mybucket/data/",
                region_name="eu-west-1",
                schema="dev",
                database="awsdatacatalog",
                aws_profile_name="myprofile",
                aws_access_key_id="AKIA123",
                aws_secret_access_key=Secret("secretkey123"),
                threads=4,
            ),
        ),
        pytest.param(
            {
                "type": "glue",
                "project_name": "myproject",
                "role_arn": "arn:aws:iam::123456789012:role/GlueRole",
                "region": "eu-west-1",
                "workers": 2,
                "worker_type": "G.1X",
                "schema": "dev",
                "session_provisioning_timeout_in_seconds": 120,
                "location": "s3://mybucket/warehouse/",
                "threads": 1,
            },
            GlueTarget(
                type="glue",
                project_name="myproject",
                role_arn="arn:aws:iam::123456789012:role/GlueRole",
                region="eu-west-1",
                workers=2,
                worker_type="G.1X",
                schema="dev",
                session_provisioning_timeout_in_seconds=120,
                location="s3://mybucket/warehouse/",
                threads=1,
            ),
        ),
    ],
    ids=[
        "duckdb",
        "mysql",
        "postgres",
        "sqlserver",
        "spark",
        "databrickstoken",
        "databricksoauth",
        "athena",
        "glue",
    ],
)
def test_target_from_dict(target_dict, expected):
    assert ProfileTarget.from_dict(target_dict) == expected


def test_missing_required_field_raises_profile_target_error():
    with pytest.raises(ProfileTargetError) as exc_info:
        ProfileTarget.from_dict(
            {"type": "postgres", "user": "postgres", "host": "127.0.0.1"}
        )

    message = str(exc_info.value)
    assert "'postgres' profile target is missing required field(s)" in message
    for field in ("dbname", "password", "port", "schema"):
        assert field in message


def test_misspelled_key_is_suggested_for_the_field_it_leaves_missing():
    with pytest.raises(ProfileTargetError) as exc_info:
        ProfileTarget.from_dict(
            {
                "type": "postgres",
                "user": "postgres",
                "password": "passwd",
                "host": "127.0.0.1",
                "port": 5432,
                "schema": "dev",
                "database": "mydb",  # postgres targets spell this `dbname`
            }
        )

    assert "'database' (did you mean 'dbname'?)" in str(exc_info.value)


def test_unknown_keys_are_ignored_when_nothing_is_missing():
    target = ProfileTarget.from_dict(
        {
            "type": "duckdb",
            "path": "dev.duckdb",
            "sslmode": "require",  # not modelled; must not fail the parse
        }
    )

    assert target == DuckDBTarget(type="duckdb", threads=1, path="dev.duckdb")


def test_uncoercible_field_raises_profile_target_error():
    with pytest.raises(ProfileTargetError):
        ProfileTarget.from_dict(
            {
                "type": "postgres",
                "user": "postgres",
                "password": "passwd",
                "host": "127.0.0.1",
                "port": "not-a-port",
                "schema": "dev",
                "dbname": "mydb",
            }
        )


def test_databricks_token_and_oauth_mix_raises_profile_target_error():
    with pytest.raises(ProfileTargetError):
        ProfileTarget.from_dict(
            {
                "type": "databricks",
                "catalog": "main",
                "schema": "dev",
                "host": "adb.example.net",
                "http_path": "/sql/1.0/endpoints/abc",
                "token": "tok",
                "client_id": "cid",
                "client_secret": "csec",
            }
        )
