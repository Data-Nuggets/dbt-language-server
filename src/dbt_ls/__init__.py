from importlib.metadata import version

import yaml

IGNORED_DIRS = {"target", ".venv", "venv", ".git", "dbt_packages", "node_modules"}

__version__ = version("dbt-ls")

# libyaml's loader when PyYAML was built with it: about ten times faster than
# the pure-Python one, which matters with thousands of documented models.
_YamlLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_yaml(text):
    """`yaml.safe_load`, with the fast loader when it is available."""
    return yaml.load(text, Loader=_YamlLoader)
