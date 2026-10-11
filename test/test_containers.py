"""The warehouse readers against real Postgres, MySQL, SQL Server and Spark.

Not part of the default run. Start the warehouses, then select the marker:

    just containers-up
    just test-containers        (or: uv run pytest -m containers)

A warehouse that is not reachable is skipped, so one can be run alone. Set
DBT_LS_REQUIRE_CONTAINERS=1 (CI does) to make that a failure instead.

Every warehouse gets the same three tables:

    analytics.stg_cards   the table of the model `stg_cards`
    raw.cards             a source documented in YAML
    crm.cards             documented nowhere, and a second table called `cards`
"""

import json
import os
import socket
from dataclasses import dataclass, field
from typing import Callable

import ibis
import pytest
import yaml

from dbt_ls import check
from dbt_ls.model import read_warehouse_tables
from dbt_ls.profiles import ProfileTarget
from dbt_ls.server import Settings, _load_project, parse_schema_sources

pytestmark = pytest.mark.containers

HOST = os.environ.get("DBT_LS_TEST_HOST", "127.0.0.1")
REQUIRED = os.environ.get("DBT_LS_REQUIRE_CONTAINERS") == "1"
SQLSERVER_PASSWORD = "Dbt-ls-test-1"
SQLSERVER_DRIVER = os.environ.get(
    "DBT_LS_TEST_SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server"
)

SCHEMAS = ("analytics", "raw", "crm")
TABLES = (
    "create table analytics.stg_cards (card_id bigint, price decimal(18,2), name varchar(50))",
    "create table raw.cards (id bigint, name varchar(50))",
    "create table crm.cards (crm_id bigint, owner varchar(50))",
)


def port(service: str, default: int) -> int:
    return int(os.environ.get(f"DBT_LS_TEST_{service}_PORT", default))


@dataclass
class Warehouse:
    type: str
    port: int
    # The `outputs.dev` block of profiles.yml, as a user would write it.
    profile: dict
    seed: Callable[[], None]
    # (card_id, price, name) of analytics.stg_cards, as the reader reports them.
    types: tuple[str, str, str]
    extra_tables: set = field(default_factory=set)

    @property
    def target(self) -> ProfileTarget:
        return ProfileTarget.from_dict(self.profile)


def run(con, *statements: str) -> None:
    for statement in statements:
        cursor = con.raw_sql(statement)
        if hasattr(cursor, "close"):
            cursor.close()


def seed_postgres() -> None:
    con = ibis.postgres.connect(
        host=HOST, port=port("POSTGRES", 55432), user="postgres", password="postgres", database="dbt_ls"
    )
    run(
        con,
        *(f"drop schema if exists {s} cascade" for s in SCHEMAS),
        *(f"create schema {s}" for s in SCHEMAS),
        *TABLES,
        "create materialized view analytics.card_totals as "
        "select name, sum(price)::numeric(20,2) as total from analytics.stg_cards group by name",
    )
    con.disconnect()


def seed_mysql() -> None:
    # In MySQL a schema is a database.
    con = ibis.mysql.connect(host=HOST, port=port("MYSQL", 53306), user="root", password="root")
    run(
        con,
        *(f"drop database if exists {s}" for s in SCHEMAS),
        *(f"create database {s}" for s in SCHEMAS),
        *TABLES,
    )
    con.disconnect()


def _sqlserver(database: str):
    con = ibis.mssql.connect(
        host=HOST,
        port=port("SQLSERVER", 51433),
        user="sa",
        password=SQLSERVER_PASSWORD,
        database=database,
        driver=SQLSERVER_DRIVER,
        TrustServerCertificate="yes",
    )
    # `create database` refuses to run inside a transaction.
    con.con.autocommit = True
    return con


def seed_sqlserver() -> None:
    con = _sqlserver("master")
    run(
        con,
        "if db_id('dbt_ls') is not null alter database dbt_ls set single_user with rollback immediate",
        "drop database if exists dbt_ls",
        "create database dbt_ls",
    )
    con.disconnect()
    con = _sqlserver("dbt_ls")
    run(con, *(f"create schema {s}" for s in SCHEMAS), *TABLES)
    con.disconnect()


def seed_spark() -> None:
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.remote(f"sc://{HOST}:{port('SPARK', 55002)}").getOrCreate()
    for schema in SCHEMAS:
        spark.sql(f"drop schema if exists {schema} cascade")
        spark.sql(f"create schema {schema}")
    for table in TABLES:
        spark.sql(f"{table} using parquet")


WAREHOUSES = [
    Warehouse(
        type="postgres",
        port=port("POSTGRES", 55432),
        profile={
            "type": "postgres",
            "host": HOST,
            "port": port("POSTGRES", 55432),
            "user": "postgres",
            "password": "postgres",
            "dbname": "dbt_ls",
            "schema": "analytics",
        },
        seed=seed_postgres,
        types=("bigint", "numeric(18,2)", "character varying(50)"),
        extra_tables={("analytics", "card_totals")},
    ),
    Warehouse(
        type="mysql",
        port=port("MYSQL", 53306),
        profile={
            "type": "mysql",
            "server": HOST,
            "port": port("MYSQL", 53306),
            "user": "root",
            "password": "root",
            "schema": "analytics",
        },
        seed=seed_mysql,
        types=("bigint", "decimal(18,2)", "varchar(50)"),
    ),
    Warehouse(
        type="sqlserver",
        port=port("SQLSERVER", 51433),
        profile={
            "type": "sqlserver",
            "server": HOST,
            "port": port("SQLSERVER", 51433),
            "user": "sa",
            "password": SQLSERVER_PASSWORD,
            "database": "dbt_ls",
            "schema": "analytics",
            "driver": SQLSERVER_DRIVER,
            "encrypt": False,
        },
        seed=seed_sqlserver,
        types=("bigint", "decimal(18,2)", "varchar(50)"),
    ),
    Warehouse(
        type="spark",
        port=port("SPARK", 55002),
        profile={
            "type": "spark",
            "method": "session",
            "host": f"sc://{HOST}:{port('SPARK', 55002)}",
            "schema": "analytics",
        },
        seed=seed_spark,
        types=("int64", "decimal(18, 2)", "string"),
    ),
]


def listening(port_number: int) -> bool:
    try:
        with socket.create_connection((HOST, port_number), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", params=WAREHOUSES, ids=lambda w: w.type)
def warehouse(request) -> Warehouse:
    found: Warehouse = request.param
    if not listening(found.port):
        message = (
            f"no {found.type} on {HOST}:{found.port}; "
            "start it with `just containers-up`"
        )
        if REQUIRED:
            pytest.fail(message)
        pytest.skip(message)
    found.seed()
    return found


@pytest.fixture
def project(warehouse, tmp_path) -> str:
    """A dbt project whose profile points at the warehouse."""
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "stg_cards.sql").write_text(
        "select * from {{ source('erp', 'cards') }}"
    )
    (tmp_path / "models" / "sources.yml").write_text(
        "sources:\n  - name: erp\n    schema: raw\n    tables:\n      - name: cards\n"
    )
    (tmp_path / "dbt_project.yml").write_text(
        "name: demo\nprofile: demo\nmodel-paths: [models]\n"
    )
    (tmp_path / "profiles.yml").write_text(
        yaml.safe_dump({"demo": {"target": "dev", "outputs": {"dev": warehouse.profile}}})
    )
    return str(tmp_path)


def test_the_reader_returns_every_table_with_its_schema(warehouse, caplog):
    tables = {(t.schema, t.name): t for t in read_warehouse_tables(warehouse.target, ".")}

    assert set(tables) == {
        ("analytics", "stg_cards"),
        ("raw", "cards"),
        ("crm", "cards"),
    } | warehouse.extra_tables
    # Two tables share a name; each keeps its own columns, in declared order.
    assert [c.name for c in tables["raw", "cards"].columns] == ["id", "name"]
    assert [c.name for c in tables["crm", "cards"].columns] == ["crm_id", "owner"]
    # The one-statement read worked; nothing fell back to a request per table.
    assert "falling back" not in caplog.text


def test_types_read_as_the_warehouse_names_them(warehouse):
    tables = {(t.schema, t.name): t for t in read_warehouse_tables(warehouse.target, ".")}

    columns = tables["analytics", "stg_cards"].columns

    assert [c.name for c in columns] == ["card_id", "price", "name"]
    assert tuple(c.data_type for c in columns) == warehouse.types


def test_a_project_load_matches_models_and_sources_and_keeps_the_rest(warehouse, project):
    state = _load_project(project, Settings(parse_schema_sources("database")))

    assert state.report.problems == []
    assert state.profile_target.type == warehouse.type
    [model] = state.models
    assert [(c.name, c.data_type) for c in model.columns] == list(
        zip(["card_id", "price", "name"], warehouse.types)
    )
    [source] = state.sources
    assert [c.name for c in source.columns] == ["id", "name"]
    assert ("crm", "cards", ["crm_id", "owner"]) in [
        (t.schema, t.name, [c.name for c in t.columns]) for t in state.undocumented
    ]
    # The documented source's table is spoken for, whatever the source is called.
    assert ("raw", "cards") not in [(t.schema, t.name) for t in state.undocumented]


def test_check_reports_the_warehouse_as_read(warehouse, project, capsys):
    status = check.main([project, "--schema-sources", "database", "--json"])

    report = json.loads(capsys.readouterr().out)
    assert status == 0, report
    [database] = [s for s in report["sources"] if s["source"] == "database"]
    assert database["outcome"] == "ok"
    assert database["models_with_types"] == 1
    assert database["sources_with_columns"] == 1


@pytest.mark.parametrize("warehouse", WAREHOUSES[:1], ids=["postgres"], indirect=True)
def test_postgres_lists_materialized_views(warehouse):
    tables = {(t.schema, t.name): t for t in read_warehouse_tables(warehouse.target, ".")}

    assert [(c.name, c.data_type) for c in tables["analytics", "card_totals"].columns] == [
        ("name", "character varying(50)"),
        ("total", "numeric(20,2)"),
    ]
