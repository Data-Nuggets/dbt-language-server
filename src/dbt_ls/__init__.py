from importlib.metadata import version
IGNORED_DIRS = {"target", ".venv", "venv", ".git", "dbt_packages", "node_modules"}

__version__ = version("dbt-ls")
