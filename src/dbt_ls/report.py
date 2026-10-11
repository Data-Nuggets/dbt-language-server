from dataclasses import asdict, dataclass, field
from typing import Literal

Outcome = Literal["ok", "skipped", "failed"]


@dataclass
class SourceReport:
    """What one schema source did during a load.

    The counts are the state of the project *after* this source ran, not what
    it alone contributed: sources overwrite each other, so only the running
    total is well defined.
    """

    source: str
    outcome: Outcome
    detail: str = ""
    seconds: float = 0.0
    models_with_columns: int = 0
    models_with_types: int = 0
    sources_with_columns: int = 0


@dataclass
class LoadReport:
    """How a project load went, for the progress message and `dbt-ls check`."""

    profile_name: str = ""
    profile_path: str = ""
    profile_type: str = ""
    # Why there is no usable profile target. Empty when there is one.
    profile_problem: str = ""
    dialect: str = ""
    sources: list[SourceReport] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        """Everything that went wrong, most fundamental first."""
        found = [f"profile: {self.profile_problem}"] if self.profile_problem else []
        found += [
            f"{s.source}: {s.detail}" for s in self.sources if s.outcome == "failed"
        ]
        return found

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> dict:
        return asdict(self)
