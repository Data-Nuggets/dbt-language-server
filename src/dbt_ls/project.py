import os

import yaml


class Project:
    def __init__(self, root: str = "."):
        self.root = root
        self.config = self._load_config()

    def _load_config(self) -> dict:
        path = os.path.join(self.root, "dbt_project.yml")
        with open(path) as f:
            return yaml.safe_load(f) or {}

    @property
    def model_paths(self) -> list[str]:
        return self.config.get("model-paths", [])

    @property
    def profile(self) -> str:
        return self.config.get("profile", "")

    def __repr__(self) -> str:
        return f"DBT Project at {self.root}"
