import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from typing import Callable, Iterator

import ibis
import yaml
from ibis import BaseBackend
from ibis.expr.schema import Schema
from ibis.expr.types.relations import Table

from dbt_ls.column import Column, HasColumns
from dbt_ls.exceptions import AWSLoginRequiredError
from dbt_ls.profiles import (
    AthenaTarget,
    DatabaseTarget,
    DatabricksTarget,
    DuckDBTarget,
    GlueTarget,
    MSSQLTarget,
    MySQLTarget,
    ProfileTarget,
    RedshiftTarget,
    SparkTarget,
)
from dbt_ls.project import Project
from dbt_ls.source import SourceTable
from dbt_ls import IGNORED_DIRS, load_yaml


log = logging.getLogger("dbt_ls")


SYSTEM_SCHEMAS: dict[str, frozenset[str]] = {
    "duckdb": frozenset({"information_schema", "pg_catalog"}),
    "postgres": frozenset({"information_schema", "pg_catalog", "pg_toast", "meta"}),
    "redshift": frozenset(
        {
            "information_schema",
            "pg_catalog",
            "pg_toast",
            "pg_internal",
            "pg_automv",
            "catalog_history",
        }
    ),
    "mysql": frozenset({"information_schema", "mysql", "performance_schema", "sys"}),
    "sqlserver": frozenset(
        {
            "sys",
            "information_schema",
            "guest",
            "db_owner",
            "db_accessadmin",
            "db_securityadmin",
            "db_ddladmin",
            "db_backupoperator",
            "db_datareader",
            "db_datawriter",
            "db_denydatareader",
            "db_denydatawriter",
        }
    ),
    "spark": frozenset({"information_schema", "global_temp"}),
    "databricks": frozenset(
        {"information_schema", "global_temp", "__databricks_internal"}
    ),
    "athena": frozenset({"information_schema"}),
    "glue": frozenset({"information_schema"}),
}

SYSTEM_SCHEMA_PREFIXES: dict[str, tuple[str, ...]] = {
    "postgres": ("pg_temp", "pg_toast_temp"),
    "redshift": ("pg_temp", "pg_toast_temp"),
}


def is_system_schema(adapter: str, schema: str | None) -> bool:
    if not schema:
        return False
    name = schema.casefold()
    if name in SYSTEM_SCHEMAS.get(adapter, frozenset()):
        return True
    return name.startswith(SYSTEM_SCHEMA_PREFIXES.get(adapter, ()))


class SourcedList(list):
    def __init__(self, iterable=(), *, source: str):
        super().__init__(iterable)
        self.source = source

    def __contains__(self, item) -> bool:
        if isinstance(item, str):
            return any(m.name == item for m in self)
        return super().__contains__(item)

    def __getitem__(self, key):
        if isinstance(key, str):
            for m in self:
                if m.name == key:
                    return m
            raise KeyError(key)
        return super().__getitem__(key)


LOCATION_FIELDS = ("alias", "schema", "database")


@dataclass(frozen=True)
class Model(HasColumns):
    name: str
    path: Path
    columns: tuple[Column, ...] = ()
    model_type: str = ""
    # Where the project says the model is built, when it says so with a
    # literal: `alias` replaces the table name, `schema` is dbt's *custom*
    # schema (by default appended to the target's), `database` replaces the
    # target's. None means "not declared", not "the default".
    alias: str | None = None
    schema: str | None = None
    database: str | None = None

    def __repr__(self) -> str:
        return f"Model: {self.name}, Path: {self.path}, Model type: {self.model_type}"

    @staticmethod
    def get_exec_path(path: Path, project: Project) -> str | None:
        """
        Return model exec path relative to dbt project root
        /home/me/repos/datainc/dbt_datainc/models/staging/stg_cards.sql
            => staging.stg_cards
        """
        for model_path in project.model_paths:
            full_model_path = os.path.join(project.root, model_path)
            if full_model_path in str(path):
                return ".".join(path.relative_to(full_model_path).with_suffix("").parts)

        return None

    def merged_with(self, other: "Model") -> "Model":
        """Combine two discovered views of the same model.

        Keeps this model's identity (name/path) but adopts `other`'s columns
        when `other` has any — so a richer source (config/catalog/db) fills in
        column info without clobbering the real file path. A declared
        location is adopted the same way, field by field.
        """
        updates = {
            field: value
            for field in LOCATION_FIELDS
            if (value := getattr(other, field)) is not None
        }
        if other.columns:
            updates["columns"] = other.columns
        return replace(self, **updates) if updates else self


@dataclass(frozen=True)
class WarehouseTable(HasColumns):
    """A table or view as the warehouse reports it, with where it lives.

    The name alone does not identify a table: `analytics.stg_cards` and
    `dev_someone.stg_cards` are two tables. `database` is the level above the
    schema — a catalog on Databricks and Athena — and is None for readers
    that only ever see one.
    """

    name: str
    schema: str | None = None
    database: str | None = None
    columns: tuple[Column, ...] = ()


def _declared(value) -> str | None:
    """A literal from a YAML file, or None when it is jinja we cannot render."""
    if not isinstance(value, str) or "{{" in value or "{%" in value:
        return None
    return value or None


def _declared_location(config: dict) -> dict[str, str]:
    """The literal `alias` / `schema` / `database` entries of a config mapping."""
    return {
        field: value
        for field in LOCATION_FIELDS
        if (value := _declared(config.get(field))) is not None
    }


_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
_CONFIG_CALL = re.compile(r"\bconfig\s*\(")
# A location argument whose value is one plain string and nothing else:
# `alias='fct_orders'`, with the string already swapped for a numbered marker
# by `_top_level`. A value that is built up (`'fct_' ~ suffix`) or looked up
# (`var('x')`) is not followed directly by the end of the argument, and does
# not match.
_LOCATION_ARGUMENT = re.compile(
    r"\b(alias|schema|database)\s*=\s*\x00(\d+)\x00\s*(?=[,)])"
)


def _top_level(arguments: str) -> tuple[str, list[str]]:
    """`arguments` with everything that is not a top-level keyword hidden.

    Top-level strings become `\\x00<n>\\x00` markers indexing the returned
    list; whatever sits inside brackets is blanked. So `meta={'alias': ..}`
    and `post_hook="... schema='x' ..."` cannot be mistaken for arguments.
    """
    out, strings, depth, i = [], [], 0, 0
    while i < len(arguments):
        ch = arguments[i]
        if ch in "'\"":
            end = i + 1
            while end < len(arguments) and arguments[end] != ch:
                end += 2 if arguments[end] == "\\" else 1
            if depth == 0:
                out.append(f"\x00{len(strings)}\x00")
                strings.append(arguments[i + 1 : end])
            i = end + 1
            continue
        if ch in "([{":
            depth += 1
            out.append(" ")
        elif ch in ")]}":
            depth -= 1
            # Below zero is the call's own closing parenthesis.
            out.append(")" if depth < 0 else " ")
        else:
            out.append(ch if depth == 0 else " ")
        i += 1
    return "".join(out), strings


def _config_arguments(text: str) -> Iterator[str]:
    """The argument text of each `config(...)` call, parentheses matched."""
    for call in _CONFIG_CALL.finditer(text):
        depth, quote, i = 1, "", call.end()
        while i < len(text) and depth:
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        if depth == 0:
            yield text[call.end() : i]


def inline_location(sql: str) -> dict[str, str]:
    """Location a model file declares for itself in `{{ config(...) }}`.

    A text search, not a parse: this runs over every model file at load.
    """
    if "config" not in sql or not any(field in sql for field in LOCATION_FIELDS):
        return {}
    found: dict[str, str] = {}
    for arguments in _config_arguments(_JINJA_COMMENT.sub("", sql)):
        visible, strings = _top_level(arguments)
        for match in _LOCATION_ARGUMENT.finditer(visible):
            if (value := _declared(strings[int(match.group(2))])) is not None:
                found[match.group(1)] = value
    return found


def read_inline_locations(models: list[Model]) -> list[Model]:
    """The models whose own file declares where they are built.

    Takes precedence over a YAML properties file, as it does in dbt.
    """
    located = []
    for model in models:
        try:
            sql = Path(model.path).read_text()
        except (OSError, UnicodeDecodeError, ValueError):
            # Models known only from YAML or a run result have no file.
            continue
        if location := inline_location(sql):
            located.append(replace(model, columns=(), **location))
    return SourcedList(located, source="read_inline_locations")


def discover_models(root: str, model_paths: list[str]) -> list[Model]:
    return SourcedList(
        [
            Model(name=p.stem, path=p)
            for model_path in model_paths
            for p in (Path(root) / model_path).rglob("*.sql")
        ],
        source="discover_models",
    )


def enrich_models_from_catalog(models: list[Model], catalog_path: Path) -> list[Model]:
    path = Path(catalog_path)
    if not path.is_file():
        return models

    catalog = json.loads(path.read_text())
    nodes = catalog.get("nodes", {})

    # Build a lookup: model name -> columns
    columns_by_name: dict[str, tuple[Column, ...]] = {}
    for node in nodes.values():
        if not node.get("unique_id", "").startswith("model."):
            continue
        name = node["metadata"]["name"]
        columns_by_name[name] = tuple(
            Column(name=c["name"], data_type=c.get("type"))
            for c in node.get("columns", {}).values()
        )

    return SourcedList(
        [
            Model(
                name=m.name, path=m.path, columns=columns_by_name.get(m.name, m.columns)
            )
            for m in models
        ],
        source="enrich_models_from_catalog",
    )


def read_duckdb_tables(
    profile_target: DuckDBTarget, project_root: str | Path
) -> list[WarehouseTable]:
    ibis.set_backend("duckdb")

    connection_path = (
        profile_target.path
        if Path(profile_target.path).is_absolute()
        else Path(project_root).joinpath(profile_target.path)
    )
    con = ibis.duckdb.connect(connection_path)

    return _ibis_tables(con, profile_target.type)


def read_postgres_tables(
    profile_target: DatabaseTarget, project_root: str | Path
) -> list[WarehouseTable]:

    con = ibis.postgres.connect(
        user=profile_target.user,
        password=profile_target.password.reveal(),
        host=profile_target.host,
        port=profile_target.port,
        database=profile_target.dbname,
    )

    return _ibis_tables(con, profile_target.type)


def read_mysql_tables(
    profile_target: MySQLTarget, project_root: str | Path
) -> list[WarehouseTable]:

    con = ibis.mysql.connect(
        user=profile_target.user,
        password=profile_target.password.reveal(),
        host=profile_target.server,
        port=profile_target.port,
        database=profile_target.schema,
    )

    return _ibis_tables(con, profile_target.type)


def read_sqlserver_tables(
    profile_target: MSSQLTarget, project_root: str | Path
) -> list[WarehouseTable]:
    con = ibis.mssql.connect(
        user=profile_target.user,
        password=profile_target.password.reveal(),
        host=profile_target.server,
        port=profile_target.port,
        database=profile_target.database,
        schema=profile_target.schema,
        driver=profile_target.driver,
        TrustServerCertificate="yes" if not profile_target.encrypt else "no",
    )

    return _ibis_tables(con, profile_target.type)


def read_spark_tables(
    profile_target: SparkTarget, project_root: str | Path
) -> list[WarehouseTable]:
    from pyspark.sql import SparkSession

    spark = (
        SparkSession.builder.remote(profile_target.host).appName("dbt-ls").getOrCreate()
    )

    con = ibis.pyspark.connect(session=spark)

    return _ibis_tables(con, profile_target.type)


# Each cloud reader is split in two: a `_*_client` that signs in, and a
# `_*_tables` that takes the client as an argument. The second half holds all
# the logic and can be run against a stand-in client, so it is testable
# without an account.


def _databricks_client(profile_target: DatabricksTarget):
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config

    if profile_target.token:
        return WorkspaceClient(
            host=profile_target.host,
            token=profile_target.token.reveal(),
            auth_type="pat",
        )
    return WorkspaceClient(
        config=Config(
            host=profile_target.host,
            client_id=profile_target.client_id,
            client_secret=profile_target.client_secret.reveal(),
        )
    )


def _databricks_tables(
    workspace, profile_target: DatabricksTarget
) -> list[WarehouseTable]:
    """`workspace` is a databricks-sdk WorkspaceClient, or anything shaped like one."""
    catalog = profile_target.catalog
    schemas = [
        s.name
        for s in workspace.schemas.list(catalog)
        if not is_system_schema(profile_target.type, s.name)
    ]
    return [
        WarehouseTable(
            name=t.name,
            schema=db_schema,
            database=catalog,
            columns=tuple(
                Column(name=c.name, data_type=c.type_text) for c in (t.columns or [])
            ),
        )
        for db_schema in schemas
        for t in workspace.tables.list(catalog_name=catalog, schema_name=db_schema)
    ]


def read_databricks_tables(
    profile_target: DatabricksTarget, project_root: str | Path
) -> list[WarehouseTable]:
    return _databricks_tables(_databricks_client(profile_target), profile_target)


def translates_aws_login_errors(fn):
    """Re-raise botocore's expired-login error as the dbt-ls one.
    Fixes ImportError when not using aws backend
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            from botocore.exceptions import LoginRefreshRequired
        except ImportError:
            return fn(*args, **kwargs)
        try:
            return fn(*args, **kwargs)
        except LoginRefreshRequired as exc:
            raise AWSLoginRequiredError from exc

    return wrapper


def _aws_session(profile_name: str | None):
    import boto3

    return boto3.Session(profile_name=profile_name or "default")


def _athena_tables(client, profile_target: AthenaTarget) -> list[WarehouseTable]:
    """`client` is a boto3 Athena client."""
    catalog = profile_target.database

    # Both listings are paged, 50 entries at a time; a single call would
    # silently stop there.
    schemas = [
        s["Name"]
        for page in client.get_paginator("list_databases").paginate(CatalogName=catalog)
        for s in page["DatabaseList"]
        if not is_system_schema(profile_target.type, s["Name"])
    ]

    tables: list[WarehouseTable] = []
    for db_schema in schemas:
        pages = client.get_paginator("list_table_metadata").paginate(
            CatalogName=catalog, DatabaseName=db_schema
        )
        for page in pages:
            for t in page["TableMetadataList"]:
                tables.append(
                    WarehouseTable(
                        name=t["Name"],
                        schema=db_schema,
                        database=catalog,
                        columns=tuple(
                            Column(name=c["Name"], data_type=c.get("Type"))
                            for c in (t.get("Columns") or [])
                        ),
                    )
                )
    return tables


@translates_aws_login_errors
def read_athena_tables(
    profile_target: AthenaTarget, project_root: str | Path
) -> list[WarehouseTable]:
    client = _aws_session(profile_target.aws_profile_name).client("athena")
    return _athena_tables(client, profile_target)


def _glue_tables(client, profile_target: GlueTarget) -> list[WarehouseTable]:
    """`client` is a boto3 Glue client."""
    schemas = [
        s["Name"]
        for page in client.get_paginator("get_databases").paginate()
        for s in page["DatabaseList"]
        if not is_system_schema(profile_target.type, s["Name"])
    ]

    tables: list[WarehouseTable] = []
    for db_schema in schemas:
        for page in client.get_paginator("get_tables").paginate(DatabaseName=db_schema):
            for t in page["TableList"]:
                # Not every catalog entry has a storage descriptor.
                storage = t.get("StorageDescriptor") or {}
                tables.append(
                    WarehouseTable(
                        name=t["Name"],
                        schema=db_schema,
                        columns=tuple(
                            Column(name=c["Name"], data_type=c.get("Type"))
                            for c in (storage.get("Columns") or [])
                        ),
                    )
                )
    return tables


@translates_aws_login_errors
def read_glue_tables(
    profile_target: GlueTarget, project_root: str | Path
) -> list[WarehouseTable]:
    client = _aws_session(profile_target.aws_profile_name).client("glue")
    return _glue_tables(client, profile_target)


_REDSHIFT_COLUMNS_SQL = """
    select table_schema, table_name, column_name, data_type
    from svv_columns
    where table_schema not in ({system_schemas})
      and table_schema not like 'pg\\_temp%'
      and table_schema not like 'pg\\_toast\\_temp%'
    order by table_schema, table_name, ordinal_position
""".format(
    system_schemas=", ".join(f"'{s}'" for s in sorted(SYSTEM_SCHEMAS["redshift"]))
)


def _redshift_client(profile_target: RedshiftTarget):
    import boto3

    session = boto3.Session(
        profile_name=profile_target.iam_profile or "default",
        region_name=profile_target.region,
    )
    return session.client("redshift-data")


def _redshift_rows(client, profile_target: RedshiftTarget, sql: str) -> list[list]:
    """`client` is a boto3 Redshift Data API client."""
    name = profile_target.host.split(".", 1)[0]
    target = (
        {"WorkgroupName": name}
        if "redshift-serverless" in profile_target.host
        else {"ClusterIdentifier": name, "DbUser": profile_target.user}
    )

    sid = client.execute_statement(Database=profile_target.database, Sql=sql, **target)[
        "Id"
    ]

    # Back off so a slow statement doesn't get throttled by its own polling.
    # Cap at a minute
    interval = 0.1
    while (desc := client.describe_statement(Id=sid))["Status"] != "FINISHED":
        if desc["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError(f"redshift {desc['Status']}: {desc.get('Error', '')}")
        time.sleep(interval)
        interval = min(interval * 2, 60.0)

    return [
        [None if c.get("isNull") else next(iter(c.values()), None) for c in record]
        for page in client.get_paginator("get_statement_result").paginate(Id=sid)
        for record in page["Records"]
    ]


def _redshift_tables(client, profile_target: RedshiftTarget) -> list[WarehouseTable]:
    rows = _redshift_rows(client, profile_target, _REDSHIFT_COLUMNS_SQL)

    # The query orders by schema, table, ordinal position, and a dict keeps
    # first-seen order, so both tables and their columns stay in that order.
    by_relation: dict[tuple[str, str], list[Column]] = {}
    for schema, table, column, data_type in rows:
        by_relation.setdefault((schema, table), []).append(
            Column(name=column, data_type=data_type)
        )

    return [
        WarehouseTable(
            name=table,
            schema=schema,
            database=profile_target.database,
            columns=tuple(columns),
        )
        for (schema, table), columns in by_relation.items()
    ]


@translates_aws_login_errors
def read_redshift_tables(
    profile_target: RedshiftTarget, project_root: str | Path
) -> list[WarehouseTable]:
    return _redshift_tables(_redshift_client(profile_target), profile_target)


_DATABASE_METHOD_REGISTRY: dict[str, Callable[..., list[WarehouseTable]]] = {
    "duckdb": read_duckdb_tables,
    "postgres": read_postgres_tables,
    "mysql": read_mysql_tables,
    "sqlserver": read_sqlserver_tables,
    "spark": read_spark_tables,
    "databricks": read_databricks_tables,
    "athena": read_athena_tables,
    "glue": read_glue_tables,
    "redshift": read_redshift_tables,
}


# One statement for a whole database. The alternative, asking ibis for each
# table's schema, is a round trip and a type conversion per table: about a
# hundred times slower on a warehouse with thousands of tables.
_INFORMATION_SCHEMA_SQL = """
select table_schema, table_name, column_name, {data_type}
from information_schema.columns
{where}
order by table_schema, table_name, ordinal_position
"""
_ALL_COLUMNS_SQL: dict[str, str] = {
    # An attached DuckDB file shows up as another catalog; stay in our own.
    "duckdb": _INFORMATION_SCHEMA_SQL.format(
        data_type="data_type", where="where table_catalog = current_database()"
    ),
    # The system catalog rather than information_schema: it has materialized
    # views, and format_type() keeps lengths and precision (`numeric(18,2)`).
    "postgres": """
select n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod)
from pg_catalog.pg_attribute a
join pg_catalog.pg_class c on c.oid = a.attrelid
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where a.attnum > 0 and not a.attisdropped and c.relkind in ('r', 'p', 'v', 'm', 'f')
order by n.nspname, c.relname, a.attnum
""",
    # `column_type` carries the length and precision, `data_type` does not.
    "mysql": _INFORMATION_SCHEMA_SQL.format(data_type="column_type", where=""),
    "sqlserver": _INFORMATION_SCHEMA_SQL.format(data_type="data_type", where=""),
}


def _tables_in_one_statement(con: BaseBackend, backend_type: str) -> list[WarehouseTable]:
    cursor = con.raw_sql(_ALL_COLUMNS_SQL[backend_type])
    try:
        rows = cursor.fetchall()
    finally:
        # DuckDB hands back the connection itself, which must stay open.
        if backend_type != "duckdb":
            cursor.close()

    # Rows arrive ordered, and a dict keeps first-seen order.
    by_relation: dict[tuple[str, str], list[Column]] = {}
    for schema, table, column, data_type in rows:
        by_relation.setdefault((schema, table), []).append(
            Column(name=column, data_type=str(data_type))
        )
    return [
        WarehouseTable(name=table, schema=schema, columns=tuple(columns))
        for (schema, table), columns in by_relation.items()
        if not is_system_schema(backend_type, schema)
    ]


def _tables_one_by_one(con: BaseBackend, backend_type: str) -> list[WarehouseTable]:
    schemas = [s for s in con.list_databases() if not is_system_schema(backend_type, s)]

    tables: list[WarehouseTable] = []
    for db_schema in schemas:
        for t in con.list_tables(database=db_schema):
            table: Table = con.table(t, database=db_schema)
            schema: Schema = table.schema()
            tables.append(
                WarehouseTable(
                    name=t,
                    schema=db_schema,
                    columns=tuple(
                        Column(name=name, data_type=str(dtype))
                        for name, dtype in schema.items()
                    ),
                )
            )
    return tables


def _ibis_tables(con: BaseBackend, backend_type: str) -> list[WarehouseTable]:
    if backend_type in _ALL_COLUMNS_SQL:
        try:
            return _tables_in_one_statement(con, backend_type)
        except Exception as exc:
            log.warning(
                "Reading all columns in one statement failed (%s); "
                "falling back to one request per table",
                exc,
            )
    return _tables_one_by_one(con, backend_type)


def read_warehouse_tables(
    profile_target: ProfileTarget, project_root: str | Path
) -> list[WarehouseTable]:
    """Every table the profile's warehouse reports, each with its schema.

    Empty when there is no reader for the profile's type.
    """
    fn = _DATABASE_METHOD_REGISTRY.get(profile_target.type)
    if fn is None:
        return []
    return fn(profile_target, project_root)


# ---------------------------------------------------------------------------
# Matching what the warehouse has to what the project declares.
#
# All comparisons are case-insensitive: warehouses differ in how they fold
# identifiers, and dbt projects in how they spell them.
# ---------------------------------------------------------------------------


def _same(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a.casefold() == b.casefold()


def _differs(a: str | None, b: str | None) -> bool:
    """Known on both sides and unequal. An unknown side never disqualifies."""
    return a is not None and b is not None and a.casefold() != b.casefold()


def _tables_by_name(tables: list[WarehouseTable]) -> dict[str, list[WarehouseTable]]:
    by_name: dict[str, list[WarehouseTable]] = {}
    for table in tables:
        by_name.setdefault(table.name.casefold(), []).append(table)
    return by_name


def target_location(profile_target: ProfileTarget) -> tuple[str | None, str | None]:
    """(database, schema) dbt builds into by default, as far as the profile says."""
    database = next(
        (
            value
            for key in ("catalog", "database", "dbname")
            if (value := getattr(profile_target, key, None))
        ),
        None,
    )
    return database, getattr(profile_target, "schema", None)


def find_model_table(
    model: Model,
    by_name: dict[str, list[WarehouseTable]],
    database: str | None,
    schema: str | None,
) -> WarehouseTable | None:
    """The warehouse table a model was built as, or None rather than a guess.

    The table is named by the model's alias when it declares one, and sought
    in the database it declares, otherwise the target's. Then, by schema:

    1. With a declared custom schema: `<target schema>_<custom>`, dbt's
       default, then `<custom>` on its own, which is what the common
       override produces.
    2. The target schema. Also right for a model with a custom schema in a
       project that only applies custom schemas in production.
    3. A schema named `<target schema>_<something>`, if there is exactly one:
       a custom schema set somewhere this did not read, like dbt_project.yml.
    4. The only table of that name anywhere.

    Several same-named tables with nothing to choose between them match
    nothing: showing another table's columns is worse than showing none.
    """
    name = model.alias or model.name
    database = model.database or database
    candidates = [
        t
        for t in by_name.get(name.casefold(), [])
        if not _differs(t.database, database)
    ]

    def in_schema(wanted: str | None) -> WarehouseTable | None:
        return next((t for t in candidates if _same(t.schema, wanted)), None)

    if model.schema:
        default = f"{schema}_{model.schema}" if schema else None
        if (found := in_schema(default) or in_schema(model.schema)) is not None:
            return found
    if schema:
        if (found := in_schema(schema)) is not None:
            return found
        prefix = schema.casefold() + "_"
        custom = [
            t for t in candidates if (t.schema or "").casefold().startswith(prefix)
        ]
        if custom:
            return custom[0] if len(custom) == 1 else None
    return candidates[0] if len(candidates) == 1 else None


def match_models(
    models: list[Model], tables: list[WarehouseTable], profile_target: ProfileTarget
) -> list[Model]:
    """The models that were found in the warehouse, carrying its columns."""
    by_name = _tables_by_name(tables)
    database, schema = target_location(profile_target)
    matched = []
    for model in models:
        table = find_model_table(model, by_name, database, schema)
        if table is not None and table.columns:
            matched.append(replace(model, columns=table.columns))
    return SourcedList(matched, source="enrich_models_from_database")


def find_source_table(
    source: SourceTable, by_name: dict[str, list[WarehouseTable]]
) -> WarehouseTable | None:
    """The warehouse table a documented source points at.

    A source says where it lives, so it is looked up there and nowhere else:
    schema as declared (dbt's default is the source's own name), database too
    when both sides know it. Only a schema written as jinja, which cannot be
    evaluated here, falls back to a unique match by name.
    """
    database = _declared(source.database)
    candidates = [
        t
        for t in by_name.get((source.identifier or source.name).casefold(), [])
        if not _differs(t.database, database)
    ]
    if source.schema is not None and _declared(source.schema) is None:
        return candidates[0] if len(candidates) == 1 else None
    schema = source.schema or source.source_name
    return next((t for t in candidates if _same(t.schema, schema)), None)


def match_sources(
    sources: list[SourceTable], tables: list[WarehouseTable]
) -> list[SourceTable]:
    """Every documented source, with warehouse columns where it was found.

    A source the warehouse does not have is kept as it was: it is still
    declared, still valid in `source()`, and may well exist in a database
    this reader did not look at.
    """
    by_name = _tables_by_name(tables)
    result = []
    for source in sources:
        table = find_source_table(source, by_name)
        if table is not None and table.columns:
            source = replace(source, columns=table.columns)
        result.append(source)
    return result


def undocumented_tables(
    models: list[Model],
    sources: list[SourceTable],
    tables: list[WarehouseTable],
    profile_target: ProfileTarget,
) -> list[SourceTable]:
    """Warehouse tables that are neither a model nor a documented source.

    Shaped like sources, named after their schema, so that everything which
    completes for a documented source also works for a table nobody has
    written YAML for yet.
    """
    by_name = _tables_by_name(tables)
    database, schema = target_location(profile_target)
    claimed = {
        id(table)
        for model in models
        if (table := find_model_table(model, by_name, database, schema)) is not None
    }
    claimed |= {
        id(table)
        for source in sources
        if (table := find_source_table(source, by_name)) is not None
    }
    return [
        SourceTable(
            name=table.name,
            source_name=table.schema,
            database=table.database,
            schema=table.schema,
            columns=table.columns,
        )
        for table in tables
        if id(table) not in claimed and table.schema
    ]


def enrich_models_from_config(dbt_root: str) -> list[Model]:

    def _parse_config(config: dict):
        if "models" not in config.keys():
            return []

        return [
            Model(
                m["name"],
                Path(""),
                # Plenty of entries only carry a description or tests.
                tuple(Column(c["name"]) for c in m.get("columns") or []),
                **_declared_location(m.get("config") or {}),
            )
            for m in config["models"] or []
            if isinstance(m, dict) and "name" in m
        ]

    models: list[Model] = []
    config_files = []

    for root, dirs, files in os.walk(dbt_root):
        # Prune build artifacts / installed packages (e.g. `.venv`) in place so
        # os.walk never descends into them.
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for file in files:
            if file.endswith(".yml") or file.endswith(".yaml"):
                if file != "dbt_project.yml":
                    config_files.append(os.path.join(root, file))

    for cf in config_files:
        try:
            with open(cf) as f:
                config = load_yaml(f) or {}
        except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
            log.warning("Skipping unreadable config file %s: %s", cf, exc)
            continue

        # A .yml whose top level is a list/scalar isn't a dbt config; skip it
        # rather than letting `_parse_config` hit `.keys()` on a non-dict.
        if not isinstance(config, dict):
            continue

        if parsed_models := _parse_config(config):
            models.extend(parsed_models)

    return SourcedList(models, source="parse_models_from_config")
