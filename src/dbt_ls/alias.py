import re
from dataclasses import InitVar, dataclass, field
from uuid import UUID, uuid4

REF_PATTERN = re.compile(r"""\{{\s*ref\((['"])(\w+)\1\)\s*}}\s+(?:as\s+)?(\w+)""")
SOURCE_PATTERN = re.compile(
    r"""\{{\s*source\((['"])(\w+)\1,\s*(['"])(\w+)\3\)\s*}}\s+(?:as\s+)?(\w+)"""
)


def line_number_from_match(text: str, match: re.Match) -> int:
    return text.count("\n", 0, match.start()) + 1


def column_number_from_match(text: str, match: re.Match) -> int:
    return match.start() - (text.rfind("\n", 0, match.start()) + 1)


@dataclass
class Alias:
    """An alias bound to a model or source table, and where it was declared."""

    ref: str
    alias: str
    source_text: InitVar[str]
    match: InitVar[re.Match]
    line_number: int = field(init=False)
    column_number: int = field(init=False)
    start: int = field(init=False)
    # Excluded from __eq__ so two Aliases parsed from the same text compare equal.
    identifier: UUID = field(default_factory=uuid4, compare=False)

    def __post_init__(self, source_text: str, match: re.Match):
        self.line_number = line_number_from_match(source_text, match)
        self.column_number = column_number_from_match(source_text, match)
        self.start = match.start()


def parse_alias_list(text: str) -> list[Alias]:
    """Find every alias declaration in `text`, keeping duplicates.

    Returned in document order. The two patterns are scanned separately, so
    the sort is what interleaves sources back among the refs.
    """

    text = text.lower()
    aliases = [
        Alias(ref=m.group(2), alias=m.group(3), source_text=text, match=m)
        for m in REF_PATTERN.finditer(text)
    ]
    # group(4) is the source's table name; group(2) is the source_name.
    aliases += [
        Alias(ref=m.group(4), alias=m.group(5), source_text=text, match=m)
        for m in SOURCE_PATTERN.finditer(text)
    ]
    return sorted(aliases, key=lambda a: a.start)


def choose_alias(
    aliases: list[Alias], pos: tuple[int, int], alias: str
) -> Alias | None:
    """Pick which declaration of `alias` applies at `pos`.

    `pos` is `(line, column)` in Alias's convention: 1-based line, 0-based
    column. Callers holding an LSP Position (0-based on both) must add 1 to
    the line.

    A single declaration wins regardless of position. Otherwise the first one
    at or after the cursor is taken, since the FROM clause that binds an alias
    comes after the SELECT that uses it.

    `aliases` need not be ordered; the candidates are sorted here.
    """

    filtered_by_alias = sorted(
        (a for a in aliases if a.alias == alias),
        key=lambda a: (a.line_number, a.column_number),
    )
    if len(filtered_by_alias) == 1:
        return filtered_by_alias[0]

    for a in filtered_by_alias:
        if pos <= (a.line_number, a.column_number):
            return a

    return None


def parse_aliases(text: str) -> dict[str, Alias]:
    """Map each alias in `text` to the model or source table it refers to.
    Keyed by alias name, so the last declaration of a repeated alias wins.
    """
    text = text.lower()
    aliases: dict[str, Alias] = {}
    for match in REF_PATTERN.finditer(text):
        aliases[match.group(3)] = Alias(
            ref=match.group(2),
            alias=match.group(3),
            source_text=text,
            match=match,
        )
    for match in SOURCE_PATTERN.finditer(text):
        # group(4) is the source's table name; group(2) is the source_name.
        aliases[match.group(5)] = Alias(
            ref=match.group(4),
            alias=match.group(5),
            source_text=text,
            match=match,
        )
    return aliases
