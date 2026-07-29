import re
from dataclasses import InitVar, dataclass, field
from uuid import UUID, uuid4

import sqlglot.expressions as exp

from dbt_ls.scope import (
    Position,
    Range,
    Span,
    block_span,
    position_from_offset,
    span_to_range,
)

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
    position: Position = field(init=False)
    start: int = field(init=False)
    # Excluded from __eq__ so two Aliases parsed from the same text compare equal.
    identifier: UUID = field(default_factory=uuid4, compare=False)

    def __post_init__(self, source_text: str, match: re.Match):
        self.line_number = line_number_from_match(source_text, match)
        self.column_number = column_number_from_match(source_text, match)
        self.position = Position(self.line_number, self.column_number)
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


def find_block_range(
    blocks: list[exp.Expression], cur_pos: Position, sql: str
) -> Range | None:
    """The region the cursor is in: this block's start up to the next one's.

    Not the block's own span. sqlglot's offsets stop at the last token it
    managed to parse, so a WHERE clause being typed after an otherwise
    complete SELECT lands past the end of the span and would match nothing.
    Where the *next* block begins is stable under editing in a way the
    current block's end is not.
    """
    starts = sorted(
        span.start for block in blocks if (span := block_span(block)) is not None
    )

    # The innermost block wins: nested blocks start later, so the last
    # start at or before the cursor is the one the cursor is really in.
    current = None
    for i, start in enumerate(starts):
        if position_from_offset(sql, start) > cur_pos:
            break
        current = i

    if current is None:
        return None

    # Runs to the next block, or to end of file for the last one.
    end = starts[current + 1] if current + 1 < len(starts) else len(sql)
    return span_to_range(sql, Span(starts[current], end))


def choose_alias(
    cur_pos: Position,
    aliases: list[Alias],
    alias: str,
    blocks: list[exp.Expression],
    sql: str,
) -> Alias | None:
    """Pick the declaration of `alias` that is in scope at `cur_pos`.

    Scope wins: if the cursor is inside a known block, only a declaration in
    that same block counts, and an alias bound elsewhere resolves to nothing.

    Falling back to a lone declaration when no block covers the cursor is not
    laziness — `blocks` comes from the last version of the buffer that parsed,
    so a block being typed right now doesn't exist in it yet. Without the
    fallback, a new CTE would offer no completions until it was finished.
    """

    filtered_by_alias = sorted(
        (a for a in aliases if a.alias == alias),
        key=lambda a: (a.line_number, a.column_number),
    )

    block_range = find_block_range(blocks, cur_pos, sql)
    if block_range:
        for a in filtered_by_alias:
            if block_range.position_in_range(a.position):
                return a
        return None

    return filtered_by_alias[0] if len(filtered_by_alias) == 1 else None


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
