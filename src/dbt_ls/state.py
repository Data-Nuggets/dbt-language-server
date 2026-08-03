import json
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path

import sqlglot

from dbt_ls.column import Column
from dbt_ls.model import (
    Model,
    SourcedList,
    discover_models,
    enrich_models_from_catalog,
    enrich_models_from_config,
    enrich_models_from_database,
    filter_documented_database_sources,
)
from dbt_ls.profiles import ProfileTarget
from dbt_ls.project import Project
from dbt_ls.resolve import compiled_relation_lookup, resolve_cte
from dbt_ls.scope import dbt_dialect, parse_ast, parse_ctes
from dbt_ls.source import SourceTable, discover_sources, enrich_sources_from_catalog

log = logging.getLogger("dbt_ls")


@dataclass
class ProjectState:
    """Discovered dbt project data shared across LSP handlers.

    Held on the language server instance instead of module-level globals so it
    can be rebuilt on reload and constructed in isolation for tests.
    """

    project: Project
    profile_target: ProfileTarget
    models: list[Model] = field(default_factory=list)
    sources: list[SourceTable] = field(default_factory=list)
    dbt_root: str = "."

    def __post_init__(self):
        if not self.models:
            self.models = discover_models(self.dbt_root, self.project.model_paths)
        if not self.sources:
            self.sources = discover_sources(self.dbt_root)

    def refresh_from_catalog(self):
        # Self-guarded so callers can treat every refresh_* method as an
        # interchangeable unit of enrichment.
        catalog_path = Path(self.dbt_root) / "target" / "catalog.json"
        if not catalog_path.is_file():
            log.info("No catalog.json at %s; skipping catalog enrichment", catalog_path)
            return

        models = enrich_models_from_catalog(self.models, catalog_path)
        if models:
            self.reconciliate_models(models)
        self.sources = enrich_sources_from_catalog(self.sources, catalog_path)

    def refresh_from_config(self):
        models = enrich_models_from_config(self.dbt_root)
        if models:
            self.reconciliate_models(models)

    def refresh_from_database(self):
        models, leftover_sources = enrich_models_from_database(
            models=self.models,
            profile_target=self.profile_target,
            project_root=self.dbt_root,
        )
        if models:
            self.reconciliate_models(models)
        if leftover_sources:
            documented_sources, undocumented_sources = (
                filter_documented_database_sources(self.sources, leftover_sources)
            )
            self.sources = documented_sources

    def refresh_from_run_results(self, result_path: str):

        rr = RunResults.from_fs_path(result_path)
        models: list[Model] = []

        for res in rr.results:
            if res.status != "success" or not (res.compiled_code or "").strip():
                continue
            ast = sqlglot.parse_one(res.compiled_code, read=dbt_dialect("postgres"))

            resolved = []
            for cte in parse_ctes(ast):
                lookup = compiled_relation_lookup(self.models, self.sources, resolved)
                resolved.append(resolve_cte(cte, lookup))

            lookup = compiled_relation_lookup(self.models, self.sources, resolved)
            mr = resolve_cte(parse_ast(ast, res.unique_id.split(".")[-1]), lookup)

            if result_model := Model(
                name=mr.name,
                path=Path("<run-result>"),
                columns=tuple(
                    Column(name=c.name, data_type=c.data_type) for c in mr.columns
                ),
            ):
                models.append(result_model)

            log.info([(c.name, c.data_type) for c in mr.columns])

        if models:
            self.reconciliate_models(models)

    def reconciliate_models(self, models: list[Model]):
        """
        Compares two lists of models and find duplicates.
        The output is deduplicated model list with most information.

        discover_models(...) finds all the model files and adds a path information
        parse_models_from_config(...) finds documented models with columns w/o data types
        enrich_models_from_catalog(...) finds all written models with columns w/ data types
        enrich_models_from_database(...) finds all the table information from the datasource

        TODO: Create some kind of priority for the above and compare on that
        """

        by_name: dict[str, Model] = {m.name: m for m in self.models}
        for model in models:
            existing = by_name.get(model.name)
            if existing is not None:
                by_name[model.name] = existing.merged_with(model)
            else:
                by_name[model.name] = model

        self.models = SourcedList(by_name.values(), source="reconciliate_models")


@dataclass
class RunResult:
    status: str
    relation_name: str
    compiled_code: str
    unique_id: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RunResult":
        kwargs = {}
        allowed_fields = {f.name for f in fields(cls)}

        for k, v in data.items():
            if k in allowed_fields:
                kwargs[k] = v

        return cls(**kwargs)

    @property
    def model_name(self) -> str:
        return self.relation_name.split(".")[-1]


@dataclass
class RunResults:
    metadata: dict
    elapsed_time: str
    args: dict
    results: list[RunResult] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "RunResults":
        kwargs = {}
        for k, v in data.items():
            if k == "results":
                kwargs[k] = [RunResult.from_dict(res) for res in v]
            else:
                kwargs[k] = v

        return cls(**kwargs)

    @classmethod
    def from_fs_path(cls, path: str) -> "RunResults":
        with open(path, "r") as f:
            data = json.load(f)
            return cls.from_dict(data)
