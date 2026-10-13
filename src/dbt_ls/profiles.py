import difflib
import logging
import os
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import yaml
from jinja2.sandbox import SandboxedEnvironment

from dbt_ls.exceptions import EnvVarError, ProfileTargetError

log = logging.getLogger("dbt_ls")


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
        unknown = []
        for k, v in data.items():
            if k not in allowed:
                # dbt accepts plenty of connection keys we do not model
                # (`sslmode`, `retries`, ...), so an unrecognised key is not an
                # error on its own — it is only kept around to explain a
                # missing required field below.
                unknown.append(k)
                continue
            rendered = render_profile_value(_env, v)
            try:
                kwargs[k] = _coerce(rendered, hints.get(k, str))
            except (ValueError, TypeError) as exc:
                raise ProfileTargetError(f"profile field {k!r}: {exc}") from exc

        if unknown:
            log.debug(
                "Ignoring unrecognised profile key(s) for target type %r: %s",
                data.get("type", ""),
                ", ".join(sorted(unknown)),
            )

        required = {
            f.name
            for f in fields(target_cls)
            if f.default is MISSING and f.default_factory is MISSING
        }
        if missing := sorted(required - kwargs.keys()):
            raise ProfileTargetError(
                cls._missing_fields_message(data, missing, unknown)
            )

        SECRET_FIELDS = {"password", "token", "client_secret", "aws_secret_access_key"}
        for name in SECRET_FIELDS:
            if isinstance(kwargs.get(name), str):
                kwargs[name] = Secret(kwargs[name])

        try:
            return target_cls(**kwargs)
        except (TypeError, ValueError) as exc:
            # e.g. `DatabricksTarget.__post_init__` rejecting a token/OAuth mix.
            raise ProfileTargetError(
                f"invalid {data.get('type', '')!r} profile target: {exc}"
            ) from exc

    @staticmethod
    def _missing_fields_message(
        data: dict, missing: list[str], unknown: list[str]
    ) -> str:
        message = (
            f"{data.get('type', '')!r} profile target is missing required "
            f"field(s): {', '.join(missing)}"
        )
        hints = []
        for key in sorted(unknown):
            # Cross-adapter spellings (`database` vs `dbname`) are too far apart
            # for difflib at any cutoff that does not also match unrelated
            # fields, so they are listed explicitly; difflib then covers plain
            # typos (`databse`, `passwd`).
            candidates = [f for f in missing if key in _FIELD_ALIASES.get(f, ())]
            if not candidates:
                candidates = difflib.get_close_matches(key, missing, n=1)
            if candidates:
                hints.append(f"{key!r} (did you mean {candidates[0]!r}?)")
        if hints:
            message += f". Unrecognised key(s) in profiles.yml: {', '.join(hints)}"
        return message


@dataclass(kw_only=True)
class DuckDBTarget(ProfileTarget):
    path: str
    schema: str = "main"


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
    aws_profile_name: str | None = None


@dataclass(kw_only=True)
class RedshiftTarget(ProfileTarget):
    port: int
    database: str
    schema: str
    method: str
    host: str
    user: str
    iam_profile: str = "default"
    region: str


_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "dbname": ("database", "db", "catalog"),
    "database": ("dbname", "db", "catalog"),
    "server": ("host", "hostname"),
    "host": ("server", "hostname"),
    "schema": ("dataset",),
    "user": ("username", "uid"),
    "password": ("pass", "pwd"),
    "http_path": ("httppath",),
    "region": ("region_name",),
    "region_name": ("region",),
}


_TARGET_REGISTRY: dict[str, type[ProfileTarget]] = {
    "duckdb": DuckDBTarget,
    "postgres": DatabaseTarget,
    "mysql": MySQLTarget,
    "sqlserver": MSSQLTarget,
    "spark": SparkTarget,
    "databricks": DatabricksTarget,
    "athena": AthenaTarget,
    "glue": GlueTarget,
    "redshift": RedshiftTarget,
}


class Profiles:
    def __init__(self, path: Path):
        self.path = path
        self.config: dict[str, Any] = yaml.safe_load(self.path.read_text()) or {}

    def __repr__(self) -> str:
        return f"DBT Profile at {self.path}"

    @classmethod
    def locate(
        cls, project_root: str, profiles_dir: str | None = None
    ) -> "Profiles | None":
        dirs, _ = cls.search_dirs(project_root, profiles_dir)
        for directory in dirs:
            candidate = directory / "profiles.yml"
            if candidate.exists():
                return cls(candidate)

        return None

    @staticmethod
    def search_dirs(
        project_root: str, profiles_dir: str | None = None
    ) -> tuple[list[Path], str | None]:
        """Where profiles.yml is looked for, in dbt's own order, and what named it.

        A directory given by `--profiles-dir` or `DBT_PROFILES_DIR` is the only
        one searched, as in dbt: falling back to the project or ~/.dbt would
        read a different warehouse than `dbt run` does.
        """
        if profiles_dir:
            return [Path(profiles_dir).expanduser()], "--profiles-dir"
        if from_env := os.environ.get("DBT_PROFILES_DIR"):
            return [Path(from_env).expanduser()], "DBT_PROFILES_DIR"
        return [Path(project_root), Path.home() / ".dbt"], None

    def resolve(self, profile_name: str, target: str | None = None) -> ProfileTarget:
        if not target:
            target = self.config[profile_name]["target"]

        profile_target = self.config[profile_name]["outputs"][target]

        return ProfileTarget.from_dict(profile_target)
