"""The cloud warehouse readers, run against stand-in clients.

No account or network is involved. The AWS clients are real boto3 clients with
botocore's Stubber attached, which checks every request and every canned
response against the service's own API model — so a misspelled parameter or
a response shape AWS would never send fails here. Databricks gets a plain
fake shaped like the two SDK calls the reader makes.

Readers only report what the warehouse has, each table with its schema;
deciding which table belongs to which model is `test_warehouse_match.py`.

What this does not cover: signing in, and whether the real services still
answer the way the stubs say they do.
"""

from types import SimpleNamespace

import boto3
import pytest
from botocore.stub import Stubber

from dbt_ls import model as model_module
from dbt_ls.model import (
    _athena_tables,
    _databricks_client,
    _databricks_tables,
    _glue_tables,
    _redshift_tables,
    read_athena_tables,
)
from dbt_ls.profiles import ProfileTarget


def located(tables) -> list[tuple]:
    """(database, schema, name, [(column, type), ...]) per table, in order."""
    return [
        (t.database, t.schema, t.name, [(c.name, c.data_type) for c in t.columns])
        for t in tables
    ]


def stubbed(service: str):
    client = boto3.client(
        service,
        region_name="eu-west-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    return client, Stubber(client)


# --------------------------------------------------------------------------
# Databricks
# --------------------------------------------------------------------------


def databricks_target(**overrides):
    return ProfileTarget.from_dict(
        {
            "type": "databricks",
            "catalog": "main",
            "schema": "analytics",
            "host": "https://example.cloud.databricks.com",
            "http_path": "/sql/1.0/warehouses/abc",
            "token": "dapi-secret",
            **overrides,
        }
    )


def dbx_table(name: str, *cols: tuple[str, str], no_columns: bool = False):
    return SimpleNamespace(
        name=name,
        columns=(
            None
            if no_columns
            else [SimpleNamespace(name=n, type_text=t) for n, t in cols]
        ),
    )


class FakeWorkspace:
    """Shaped like the part of databricks-sdk's WorkspaceClient the reader uses."""

    def __init__(self, tables_by_schema: dict[str, list]):
        self._tables = tables_by_schema
        self.calls: list[tuple] = []
        self.schemas = SimpleNamespace(list=self._list_schemas)
        self.tables = SimpleNamespace(list=self._list_tables)

    def _list_schemas(self, catalog_name):
        self.calls.append(("schemas", catalog_name))
        # The SDK returns iterators, not lists.
        return iter(SimpleNamespace(name=s) for s in self._tables)

    def _list_tables(self, catalog_name, schema_name):
        self.calls.append(("tables", catalog_name, schema_name))
        return iter(self._tables[schema_name])


class TestDatabricks:
    def test_every_table_comes_back_with_its_catalog_and_schema(self):
        workspace = FakeWorkspace(
            {
                "analytics": [
                    dbx_table("stg_cards", ("card_id", "bigint"), ("name", "string")),
                    dbx_table("raw_cards", ("id", "bigint")),
                ],
                "dev_someone": [dbx_table("stg_cards", ("old_id", "int"))],
            }
        )
        assert located(_databricks_tables(workspace, databricks_target())) == [
            (
                "main",
                "analytics",
                "stg_cards",
                [("card_id", "bigint"), ("name", "string")],
            ),
            ("main", "analytics", "raw_cards", [("id", "bigint")]),
            # Same name, different schema: a different table, kept apart.
            ("main", "dev_someone", "stg_cards", [("old_id", "int")]),
        ]

    def test_only_the_profiles_catalog_is_read_and_system_schemas_are_skipped(self):
        workspace = FakeWorkspace(
            {"analytics": [], "information_schema": [], "INFORMATION_SCHEMA": []}
        )
        _databricks_tables(workspace, databricks_target(catalog="prod"))
        assert workspace.calls == [
            ("schemas", "prod"),
            ("tables", "prod", "analytics"),
        ]

    def test_table_without_column_metadata(self):
        # Seen for some views and foreign tables: `columns` is None.
        workspace = FakeWorkspace(
            {"analytics": [dbx_table("stg_cards", no_columns=True)]}
        )
        assert located(_databricks_tables(workspace, databricks_target())) == [
            ("main", "analytics", "stg_cards", [])
        ]

    def test_token_profile_signs_in_with_the_token(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "databricks.sdk.WorkspaceClient", lambda **kw: seen.update(kw) or "client"
        )
        assert _databricks_client(databricks_target()) == "client"
        assert seen == {
            "host": "https://example.cloud.databricks.com",
            "token": "dapi-secret",
            "auth_type": "pat",
        }

    def test_service_principal_profile_signs_in_with_oauth(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "databricks.sdk.core.Config", lambda **kw: SimpleNamespace(**kw)
        )
        monkeypatch.setattr(
            "databricks.sdk.WorkspaceClient", lambda **kw: seen.update(kw) or "client"
        )
        target = databricks_target(
            token=None, client_id="sp-id", client_secret="sp-secret"
        )
        _databricks_client(target)
        assert vars(seen["config"]) == {
            "host": "https://example.cloud.databricks.com",
            "client_id": "sp-id",
            "client_secret": "sp-secret",
        }


# --------------------------------------------------------------------------
# Athena
# --------------------------------------------------------------------------


def athena_target(**overrides):
    return ProfileTarget.from_dict(
        {
            "type": "athena",
            "s3_staging_dir": "s3://bucket/staging/",
            "region_name": "eu-west-1",
            "schema": "analytics",
            "database": "awsdatacatalog",
            **overrides,
        }
    )


def athena_table(name: str, *cols: tuple[str, str]):
    return {"Name": name, "Columns": [{"Name": n, "Type": t} for n, t in cols]}


class TestAthena:
    def test_every_table_comes_back_with_its_catalog_and_schema(self):
        client, stub = stubbed("athena")
        stub.add_response(
            "list_databases",
            {"DatabaseList": [{"Name": "analytics"}, {"Name": "information_schema"}]},
            {"CatalogName": "awsdatacatalog"},
        )
        stub.add_response(
            "list_table_metadata",
            {
                "TableMetadataList": [
                    athena_table("stg_cards", ("card_id", "bigint")),
                    athena_table("raw_cards", ("id", "bigint"), ("payload", "string")),
                ]
            },
            {"CatalogName": "awsdatacatalog", "DatabaseName": "analytics"},
        )
        with stub:
            tables = _athena_tables(client, athena_target())
            # information_schema was never asked for its tables.
            stub.assert_no_pending_responses()

        assert located(tables) == [
            ("awsdatacatalog", "analytics", "stg_cards", [("card_id", "bigint")]),
            (
                "awsdatacatalog",
                "analytics",
                "raw_cards",
                [("id", "bigint"), ("payload", "string")],
            ),
        ]

    def test_every_page_is_read(self):
        client, stub = stubbed("athena")
        catalog = {"CatalogName": "awsdatacatalog"}
        stub.add_response(
            "list_databases",
            {"DatabaseList": [{"Name": "analytics"}], "NextToken": "db-2"},
            catalog,
        )
        stub.add_response(
            "list_databases",
            {"DatabaseList": [{"Name": "staging"}]},
            {**catalog, "NextToken": "db-2"},
        )
        analytics = {**catalog, "DatabaseName": "analytics"}
        stub.add_response(
            "list_table_metadata",
            {
                "TableMetadataList": [athena_table("t1", ("a", "int"))],
                "NextToken": "p2",
            },
            analytics,
        )
        stub.add_response(
            "list_table_metadata",
            {"TableMetadataList": [athena_table("t2", ("a", "int"))]},
            {**analytics, "NextToken": "p2"},
        )
        stub.add_response(
            "list_table_metadata",
            {"TableMetadataList": [athena_table("t3", ("a", "int"))]},
            {**catalog, "DatabaseName": "staging"},
        )
        with stub:
            tables = _athena_tables(client, athena_target())
            stub.assert_no_pending_responses()

        # t2 is on the second page of tables, t3 in the second page of databases.
        assert [(t.schema, t.name) for t in tables] == [
            ("analytics", "t1"),
            ("analytics", "t2"),
            ("staging", "t3"),
        ]

    def test_table_without_columns(self):
        client, stub = stubbed("athena")
        stub.add_response("list_databases", {"DatabaseList": [{"Name": "analytics"}]})
        stub.add_response(
            "list_table_metadata", {"TableMetadataList": [{"Name": "stg_cards"}]}
        )
        with stub:
            tables = _athena_tables(client, athena_target())
        assert located(tables) == [("awsdatacatalog", "analytics", "stg_cards", [])]

    @pytest.mark.parametrize(
        "profile_name, expected",
        [(None, "default"), ("analytics-dev", "analytics-dev")],
    )
    def test_the_aws_profile_named_in_the_dbt_profile_is_used(
        self, monkeypatch, profile_name, expected
    ):
        client, stub = stubbed("athena")
        stub.add_response("list_databases", {"DatabaseList": []})
        seen = {}

        def session(profile_name=None):
            seen["profile_name"] = profile_name
            return SimpleNamespace(client=lambda service: client)

        monkeypatch.setattr(boto3, "Session", session)
        overrides = {"aws_profile_name": profile_name} if profile_name else {}
        with stub:
            read_athena_tables(athena_target(**overrides), ".")
        assert seen == {"profile_name": expected}


# --------------------------------------------------------------------------
# Glue
# --------------------------------------------------------------------------


def glue_target(**overrides):
    return ProfileTarget.from_dict(
        {
            "type": "glue",
            "role_arn": "arn:aws:iam::123456789012:role/dbt",
            "region": "eu-west-1",
            "workers": 2,
            "worker_type": "G.1X",
            "schema": "analytics",
            "session_provisioning_timeout_in_seconds": 120,
            "location": "s3://bucket/dbt/",
            **overrides,
        }
    )


def glue_table(name: str, *cols: tuple[str, str]):
    return {
        "Name": name,
        "StorageDescriptor": {"Columns": [{"Name": n, "Type": t} for n, t in cols]},
    }


class TestGlue:
    def test_every_table_comes_back_with_its_schema(self):
        client, stub = stubbed("glue")
        stub.add_response(
            "get_databases",
            {"DatabaseList": [{"Name": "analytics"}, {"Name": "information_schema"}]},
            {},
        )
        stub.add_response(
            "get_tables",
            {
                "TableList": [
                    glue_table("stg_cards", ("card_id", "bigint")),
                    glue_table("raw_cards", ("id", "bigint")),
                ]
            },
            {"DatabaseName": "analytics"},
        )
        with stub:
            tables = _glue_tables(client, glue_target())
            stub.assert_no_pending_responses()

        # Glue has no level above the database, so `database` stays unknown.
        assert located(tables) == [
            (None, "analytics", "stg_cards", [("card_id", "bigint")]),
            (None, "analytics", "raw_cards", [("id", "bigint")]),
        ]

    def test_every_page_is_read(self):
        client, stub = stubbed("glue")
        stub.add_response(
            "get_databases",
            {"DatabaseList": [{"Name": "analytics"}], "NextToken": "db-2"},
            {},
        )
        stub.add_response(
            "get_databases",
            {"DatabaseList": [{"Name": "staging"}]},
            {"NextToken": "db-2"},
        )
        stub.add_response(
            "get_tables",
            {"TableList": [glue_table("t1", ("a", "int"))], "NextToken": "p2"},
            {"DatabaseName": "analytics"},
        )
        stub.add_response(
            "get_tables",
            {"TableList": [glue_table("t2", ("a", "int"))]},
            {"DatabaseName": "analytics", "NextToken": "p2"},
        )
        stub.add_response(
            "get_tables",
            {"TableList": [glue_table("t3", ("a", "int"))]},
            {"DatabaseName": "staging"},
        )
        with stub:
            tables = _glue_tables(client, glue_target())
            stub.assert_no_pending_responses()

        assert [(t.schema, t.name) for t in tables] == [
            ("analytics", "t1"),
            ("analytics", "t2"),
            ("staging", "t3"),
        ]

    def test_table_without_a_storage_descriptor(self):
        client, stub = stubbed("glue")
        stub.add_response("get_databases", {"DatabaseList": [{"Name": "analytics"}]})
        stub.add_response(
            "get_tables",
            {
                "TableList": [
                    {"Name": "stg_cards"},
                    glue_table("int_cards", ("id", "int")),
                ]
            },
        )
        with stub:
            tables = _glue_tables(client, glue_target())
        assert located(tables) == [
            (None, "analytics", "stg_cards", []),
            (None, "analytics", "int_cards", [("id", "int")]),
        ]


# --------------------------------------------------------------------------
# Redshift (Data API)
# --------------------------------------------------------------------------

CLUSTER_HOST = "my-cluster.abc123.eu-west-1.redshift.amazonaws.com"
SERVERLESS_HOST = (
    "my-workgroup.123456789012.eu-west-1.redshift-serverless.amazonaws.com"
)


def redshift_target(**overrides):
    return ProfileTarget.from_dict(
        {
            "type": "redshift",
            "port": 5439,
            "database": "dev",
            "schema": "analytics",
            "method": "iam",
            "host": CLUSTER_HOST,
            "user": "dbt_user",
            "region": "eu-west-1",
            **overrides,
        }
    )


def record(schema: str, table: str, column: str, data_type: str | None):
    last = {"isNull": True} if data_type is None else {"stringValue": data_type}
    return [
        {"stringValue": schema},
        {"stringValue": table},
        {"stringValue": column},
        last,
    ]


def answer(stub, *pages: list, statuses=("FINISHED",), statement_params=None):
    """Queue one statement: submit, poll through `statuses`, fetch `pages`."""
    stub.add_response("execute_statement", {"Id": "st-1"}, statement_params)
    for status in statuses:
        stub.add_response(
            "describe_statement",
            {"Id": "st-1", "Status": status, "Error": "syntax error"},
            {"Id": "st-1"},
        )
    for i, records in enumerate(pages):
        response = {"Records": records}
        expected = {"Id": "st-1"}
        if i + 1 < len(pages):
            response["NextToken"] = f"page-{i + 2}"
        if i:
            expected["NextToken"] = f"page-{i + 1}"
        stub.add_response("get_statement_result", response, expected)


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """The reader backs off between polls; tests should not wait for it."""
    naps = []
    monkeypatch.setattr(model_module.time, "sleep", naps.append)
    return naps


class TestRedshift:
    def test_every_table_comes_back_with_its_database_and_schema(self):
        client, stub = stubbed("redshift-data")
        answer(
            stub,
            [
                record("analytics", "stg_cards", "card_id", "bigint"),
                record("analytics", "stg_cards", "name", "character varying"),
                record("dev_someone", "stg_cards", "old_id", "integer"),
                record("raw", "cards", "id", "bigint"),
            ],
        )
        with stub:
            tables = _redshift_tables(client, redshift_target())
            stub.assert_no_pending_responses()

        assert located(tables) == [
            (
                "dev",
                "analytics",
                "stg_cards",
                [("card_id", "bigint"), ("name", "character varying")],
            ),
            ("dev", "dev_someone", "stg_cards", [("old_id", "integer")]),
            ("dev", "raw", "cards", [("id", "bigint")]),
        ]

    def test_a_provisioned_cluster_is_addressed_by_cluster_and_user(self):
        client, stub = stubbed("redshift-data")
        answer(
            stub,
            [],
            statement_params={
                "Database": "dev",
                "Sql": model_module._REDSHIFT_COLUMNS_SQL,
                "ClusterIdentifier": "my-cluster",
                "DbUser": "dbt_user",
            },
        )
        with stub:
            _redshift_tables(client, redshift_target())
            stub.assert_no_pending_responses()

    def test_serverless_is_addressed_by_workgroup(self):
        client, stub = stubbed("redshift-data")
        answer(
            stub,
            [],
            statement_params={
                "Database": "dev",
                "Sql": model_module._REDSHIFT_COLUMNS_SQL,
                "WorkgroupName": "my-workgroup",
            },
        )
        with stub:
            _redshift_tables(client, redshift_target(host=SERVERLESS_HOST))
            stub.assert_no_pending_responses()

    def test_polls_with_backoff_until_the_statement_finishes(self, no_sleeping):
        client, stub = stubbed("redshift-data")
        answer(
            stub,
            [record("analytics", "stg_cards", "card_id", "bigint")],
            statuses=("SUBMITTED", "STARTED", "STARTED", "FINISHED"),
        )
        with stub:
            tables = _redshift_tables(client, redshift_target())
            stub.assert_no_pending_responses()
        assert [t.name for t in tables] == ["stg_cards"]
        assert no_sleeping == [0.1, 0.2, 0.4]

    @pytest.mark.parametrize("status", ["FAILED", "ABORTED"])
    def test_a_failed_statement_raises_with_the_reason(self, status):
        client, stub = stubbed("redshift-data")
        answer(stub, statuses=("STARTED", status))
        with (
            stub,
            pytest.raises(RuntimeError, match=f"redshift {status}: syntax error"),
        ):
            _redshift_tables(client, redshift_target())

    def test_every_result_page_is_read(self):
        client, stub = stubbed("redshift-data")
        answer(
            stub,
            [record("analytics", "stg_cards", "card_id", "bigint")],
            [record("analytics", "stg_cards", "name", "text")],
            [record("analytics", "int_cards", "card_id", "bigint")],
        )
        with stub:
            tables = _redshift_tables(client, redshift_target())
            stub.assert_no_pending_responses()
        # A table split across pages is still one table.
        assert located(tables) == [
            (
                "dev",
                "analytics",
                "stg_cards",
                [("card_id", "bigint"), ("name", "text")],
            ),
            ("dev", "analytics", "int_cards", [("card_id", "bigint")]),
        ]

    def test_null_data_type(self):
        client, stub = stubbed("redshift-data")
        answer(stub, [record("analytics", "stg_cards", "card_id", None)])
        with stub:
            tables = _redshift_tables(client, redshift_target())
        assert located(tables) == [
            ("dev", "analytics", "stg_cards", [("card_id", None)])
        ]

    def test_system_schemas_are_excluded_in_the_query_itself(self):
        sql = model_module._REDSHIFT_COLUMNS_SQL
        for schema in ("information_schema", "pg_catalog", "pg_internal"):
            assert f"'{schema}'" in sql
