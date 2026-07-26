import os
from dataclasses import dataclass, field
from pathlib import Path

from dbt_ls.model import (
    Model,
    discover_models,
    enrich_models_from_catalog,
    enrich_models_from_config,
    enrich_models_from_database,
    filter_documented_database_sources,
)
from dbt_ls.profiles import ProfileTarget
from dbt_ls.project import Project
from dbt_ls.source import SourceTable, discover_sources


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
        self.models = enrich_models_from_catalog(
            self.models, Path(os.path.join(self.dbt_root, "target", "catalog.json"))
        )

    def refresh_from_config(self):
        self.models = enrich_models_from_config(self.dbt_root)

    def refresh_from_database(self):
        models, leftover_sources = enrich_models_from_database(
            models=self.models,
            profile_target=self.profile_target,
            project_root=self.dbt_root,
        )
        if models:
            self.models = models
        if leftover_sources:
            documented_sources, undocumented_sources = (
                filter_documented_database_sources(self.sources, leftover_sources)
            )
            self.sources = documented_sources
