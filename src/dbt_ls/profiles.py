import os
from dataclasses import dataclass, fields
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import yaml
from jinja2.sandbox import SandboxedEnvironment


class EnvVarError(Exception):
    """Env var is unset and has no defaults"""


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "********"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        return self.__repr__()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)


def _env_var(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise EnvVarError(f"env_var({name!r}) is not set and has no default")
    return value


def render_profile_value(env: SandboxedEnvironment, value: str):
    if not isinstance(value, str) or ("{{" not in value and "{%" not in value):
        return value
    return env.from_string(value).render()


_TRUE = {"true", "1", "yes", "on", "t", "y"}
_FALSE = {"false", "0", "no", "off", "f", "n"}


def _coerce(value, hint):
    if value is None:
        return None

    if get_origin(hint) in (Union, UnionType):  # Optional[X] / X | None
        inner = [a for a in get_args(hint) if a is not type(None)]
        hint = inner[0] if len(inner) == 1 else object  # bail on ambiguous unions

    if hint is bool:
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        raise ValueError(f"{value!r} is not a valid boolean")

    if hint is int and not isinstance(value, bool):
        return int(value)

    if hint is float:
        return float(value)

    return value


@dataclass(kw_only=True)
class ProfileTarget:
    type: str
    threads: int = 1

    @classmethod
    def from_dict(cls, data: dict) -> "ProfileTarget":
        _env = SandboxedEnvironment()
        _env.globals["env_var"] = _env_var
        target_cls = _TARGET_REGISTRY.get(data.get("type", ""), cls)

        hints = get_type_hints(target_cls)
        allowed = {f.name for f in fields(target_cls)}

        kwargs = {}
        for k, v in data.items():
            if k not in allowed:
                continue
            rendered = render_profile_value(_env, v)
            try:
                kwargs[k] = _coerce(rendered, hints.get(k, str))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"profile field {k!r}: {exc}") from exc

        SECRET_FIELDS = {"password", "token", "client_secret", "aws_secret_access_key"}
        for name in SECRET_FIELDS:
            if isinstance(kwargs.get(name), str):
                kwargs[name] = Secret(kwargs[name])

        return target_cls(**kwargs)


@dataclass(kw_only=True)
class DuckDBTarget(ProfileTarget):
    path: str


@dataclass(kw_only=True)
class DatabaseTarget(ProfileTarget):
    user: str
    password: Secret
    host: str
    port: int
    dbname: str
    schema: str


@dataclass(kw_only=True)
class MySQLTarget(ProfileTarget):
    user: str
    password: Secret
    server: str  # TODO: Fix so takes server or host key
    port: int
    schema: str


@dataclass(kw_only=True)
class MSSQLTarget(ProfileTarget):
    user: str
    password: Secret
    server: str  # TODO: Fix so takes server or host key
    port: int
    database: str
    schema: str
    driver: str
    encrypt: bool


@dataclass(kw_only=True)
class SparkTarget(ProfileTarget):
    method: str = "session"
    host: str
    schema: str


@dataclass(kw_only=True)
class DatabricksTarget(ProfileTarget):
    catalog: str
    schema: str
    host: str
    http_path: str
    token: Secret | None = None
    client_id: str | None = None
    client_secret: Secret | None = None

    def __post_init__(self):
        has_token = self.token is not None
        has_oauth = self.client_id is not None and self.client_secret is not None
        if has_token == has_oauth:
            raise ValueError(
                "databricks target needs either `token` or "
                "`client_id`+`client_secret`, not both"
            )


@dataclass(kw_only=True)
class AthenaTarget(ProfileTarget):
    s3_staging_dir: str
    s3_data_dir: str | None = None
    region_name: str
    schema: str
    database: str
    aws_profile_name: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: Secret | None = None


@dataclass(kw_only=True)
class GlueTarget(ProfileTarget):
    project_name: str | None = None
    role_arn: str
    region: str
    workers: int
    worker_type: str
    schema: str
    session_provisioning_timeout_in_seconds: int
    location: str


_TARGET_REGISTRY: dict[str, type[ProfileTarget]] = {
    "duckdb": DuckDBTarget,
    "postgres": DatabaseTarget,
    "mysql": MySQLTarget,
    "sqlserver": MSSQLTarget,
    "spark": SparkTarget,
    "databricks": DatabricksTarget,
    "athena": AthenaTarget,
    "glue": GlueTarget,
}


class Profiles:
    def __init__(self, path: Path):
        self.path = path
        self.config: dict[str, Any] = yaml.safe_load(self.path.read_text()) or {}

    @classmethod
    def locate(cls, project_root: str) -> "Profiles | None":
        candidate = cls._search_dirs(project_root)
        if not candidate:
            return None
        if candidate.exists():
            return cls(candidate)

        return None

    @staticmethod
    def _search_dirs(project_root: str) -> Path | None:
        profile_paths = [Path(project_root), Path.home().joinpath(".dbt")]

        for profile_path in profile_paths:
            if Path(profile_path.joinpath("profiles.yml")).exists():
                return Path(profile_path.joinpath("profiles.yml"))

        return None

    def resolve(self, profile_name: str, target: str | None = None) -> ProfileTarget:
        if not target:
            target = self.config[profile_name]["target"]

        profile_target = self.config[profile_name]["outputs"][target]

        return ProfileTarget.from_dict(profile_target)
