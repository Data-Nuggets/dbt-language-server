import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import ibis
import yaml
from ibis import BaseBackend
from ibis.expr.schema import Schema
from ibis.expr.types.relations import Table

from dbt_ls.column import Column
from dbt_ls.profiles import (
    AthenaTarget,
    DatabaseTarget,
    DatabricksTarget,
    DuckDBTarget,
    GlueTarget,
    MSSQLTarget,
    MySQLTarget,
    ProfileTarget,
    SparkTarget,
)
from dbt_ls.project import Project
from dbt_ls.source import IGNORED_DIRS, SourceTable, discover_sources

log = logging.getLogger("dbt_ls")


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


@dataclass(frozen=True)
class Model:
    name: str
    path: Path
    columns: tuple[Column, ...] = ()

    def __repr__(self) -> str:
        return f"Model: {self.name}, Path: {self.path}"

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
        column info without clobbering the real file path.
        """
        if not other.columns:
            return self
        return replace(self, columns=other.columns)


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


def get_duckdb_models(
    models: list[Model], profile_target: DuckDBTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:
    ibis.set_backend("duckdb")

    connection_path = (
        profile_target.path
        if Path(profile_target.path).is_absolute()
        else Path(project_root).joinpath(profile_target.path)
    )
    con = ibis.duckdb.connect(connection_path)

    return _get_database_schema(models, con)


def get_database_models(
    models: list[Model], profile_target: DatabaseTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:

    con = ibis.postgres.connect(
        user=profile_target.user,
        password=profile_target.password.reveal(),
        host=profile_target.host,
        port=profile_target.port,
        database=profile_target.dbname,
        schema=profile_target.schema,
    )

    return _get_database_schema(models, con)


def get_mysql_models(
    models: list[Model], profile_target: MySQLTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:

    con = ibis.mysql.connect(
        user=profile_target.user,
        password=profile_target.password.reveal(),
        host=profile_target.server,
        port=profile_target.port,
        database=profile_target.schema,
    )

    return _get_database_schema(models, con)


def get_mssql_models(
    models: list[Model], profile_target: MSSQLTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:
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

    return _get_database_schema(models, con)


def get_spark_models(
    models: list[Model], profile_target: SparkTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:
    from pyspark.sql import SparkSession

    spark = (
        SparkSession.builder.remote(profile_target.host).appName("dbt-ls").getOrCreate()
    )

    con = ibis.pyspark.connect(session=spark)

    return _get_database_schema(models, con)


def get_databricks_models(
    models: list[Model], profile_target: DatabricksTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:

    from databricks.sdk import WorkspaceClient
    from databricks.sdk.core import Config

    if profile_target.token:
        w = WorkspaceClient(
            host=profile_target.host,
            token=profile_target.token.reveal(),
            auth_type="pat",
        )
    else:
        w = WorkspaceClient(
            config=Config(
                host=profile_target.host,
                client_id=profile_target.client_id,
                client_secret=profile_target.client_secret.reveal(),
            )
        )

    def _get_databricks_schema(
        models: list[Model],
        workspace: WorkspaceClient,
        profile_target: DatabricksTarget,
    ) -> tuple[list[Model], list[SourceTable]]:

        columns_by_name: dict[str, tuple[Column, ...]] = {}

        tables = workspace.tables.list(
            catalog_name=profile_target.catalog, schema_name=profile_target.schema
        )

        for t in tables:
            columns_by_name[t.name] = tuple(
                Column(name=c.name, data_type=c.type_text) for c in (t.columns or [])
            )

        leftover_sources = columns_by_name.keys() - [m.name for m in models]

        return (
            SourcedList(
                [
                    Model(
                        name=m.name,
                        path=m.path,
                        columns=columns_by_name.get(m.name, ()),
                    )
                    for m in models
                ],
                source="enrich_models_from_database",
            ),
            [
                SourceTable(
                    name=s, source_name="<unknown>", columns=columns_by_name.get(s, ())
                )
                for s in leftover_sources
            ],
        )

    return _get_databricks_schema(models, w, profile_target)


def get_athena_models(
    models: list[Model], profile_target: AthenaTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:
    import boto3

    columns_by_name: dict[str, tuple[Column, ...]] = {}

    session = boto3.Session(profile_name=profile_target.aws_profile_name or "default")
    client = session.client("athena")

    tables = client.list_table_metadata(
        CatalogName=profile_target.database, DatabaseName=profile_target.schema
    )

    for t in tables["TableMetadataList"]:
        columns_by_name[t["Name"]] = tuple(
            Column(name=c["Name"], data_type=c["Type"]) for c in (t["Columns"] or [])
        )

    leftover_sources = columns_by_name.keys() - [m.name for m in models]

    return (
        SourcedList(
            [
                Model(name=m.name, path=m.path, columns=columns_by_name.get(m.name, ()))
                for m in models
            ],
            source="enrich_models_from_database",
        ),
        [
            SourceTable(
                name=s, source_name="<unknown>", columns=columns_by_name.get(s, ())
            )
            for s in leftover_sources
        ],
    )


def get_glue_models(
    models: list[Model], profile_target: GlueTarget, project_root: str | Path
) -> tuple[list[Model], list[SourceTable]]:
    import boto3

    columns_by_name: dict[str, tuple[Column, ...]] = {}

    session = boto3.Session(profile_name=profile_target.aws_profile_name or "default")
    client = session.client("glue")

    tables = client.get_tables(DatabaseName=profile_target.schema)

    for t in tables["TableList"]:
        columns_by_name[t["Name"]] = tuple(
            Column(name=c["Name"], data_type=c["Type"])
            for c in (t["StorageDescriptor"]["Columns"] or [])
        )

    leftover_sources = columns_by_name.keys() - [m.name for m in models]

    return (
        SourcedList(
            [
                Model(name=m.name, path=m.path, columns=columns_by_name.get(m.name, ()))
                for m in models
            ],
            source="enrich_models_from_database",
        ),
        [
            SourceTable(
                name=s, source_name="<unknown>", columns=columns_by_name.get(s, ())
            )
            for s in leftover_sources
        ],
    )


_DATABASE_METHOD_REGISTRY: dict[
    str,
    Callable[..., tuple[list[Model], list[SourceTable]]],
] = {
    "duckdb": get_duckdb_models,
    "postgres": get_database_models,
    "mysql": get_mysql_models,
    "sqlserver": get_mssql_models,
    "spark": get_spark_models,
    "databricks": get_databricks_models,
    "athena": get_athena_models,
    "glue": get_glue_models,
}


def _get_database_schema(
    models: list[Model], con: BaseBackend
) -> tuple[list[Model], list[SourceTable]]:

    columns_by_name: dict[str, tuple[Column, ...]] = {}

    tables = con.list_tables()
    for t in tables:
        table: Table = con.table(t)
        schema: Schema = table.schema()

        columns_by_name[t] = tuple(
            Column(name=name, data_type=str(dtype)) for name, dtype in schema.items()
        )

    leftover_sources = columns_by_name.keys() - [m.name for m in models]

    return (
        SourcedList(
            [
                Model(name=m.name, path=m.path, columns=columns_by_name.get(m.name, ()))
                for m in models
            ],
            source="enrich_models_from_database",
        ),
        [
            SourceTable(
                name=s, source_name="<unknown>", columns=columns_by_name.get(s, ())
            )
            for s in leftover_sources
        ],
    )


def filter_documented_database_sources(
    sources: list[SourceTable], database_sources: list[SourceTable]
):

    by_name = {t.name: t for t in database_sources}

    # Merged documented meaning that it is listed as a source and enriched with column information from data source
    merged_documented_source = [
        replace(by_name.pop(a.name), source_name=a.source_name)
        for a in sources
        if a.name in by_name
    ]

    undocumented_sources = list(by_name.values())

    return merged_documented_source, undocumented_sources


def enrich_models_from_database(
    models: list[Model],
    profile_target: (
        DuckDBTarget | DatabaseTarget | MSSQLTarget | MySQLTarget | ProfileTarget
    ),
    project_root: str | Path,
) -> tuple[list[Model], list[SourceTable]]:
    fn = _DATABASE_METHOD_REGISTRY.get(profile_target.type)
    if fn is None:
        return ([], [])
    return fn(models, profile_target, project_root)


def enrich_models_from_config(dbt_root: str) -> list[Model]:

    def _parse_config(config: dict):
        if "models" not in config.keys():
            return []

        return [
            Model(
                m["name"],
                Path("foopath"),
                tuple(Column(c["name"]) for c in m["columns"]),
            )
            for m in config["models"]
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
                config = yaml.safe_load(f) or {}
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
