from dataclasses import dataclass
from functools import cached_property


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str | None = None

    @property
    def key(self) -> str:
        return self.name.casefold()


class HasColumns:
    """By-name column access for any relation carrying a `columns` tuple."""

    columns: tuple[Column, ...]

    @cached_property
    def columns_by_name(self) -> dict[str, Column]:
        return {c.key: c for c in self.columns}

    def column(self, name: str) -> Column | None:
        return self.columns_by_name.get(name.casefold())
