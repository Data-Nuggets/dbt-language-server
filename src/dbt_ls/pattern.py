import re

REF_RE = re.compile(r"""ref\(\s*(?P<q>['"])(?P<model>[^'"]*)$""")
# Full ref('model') call, used to find the model the cursor is *inside of*.
REF_FULL_RE = re.compile(r"""ref\(\s*['"](?P<model>[^'"]+)['"]""")
# Full source('src', 'table') call. Unlike SOURCE_RE below it is not anchored,
# so it also matches a completed call sitting anywhere in the text.
SOURCE_FULL_RE = re.compile(
    r"""source\(\s*['"](?P<source>[^'"]+)['"]\s*,\s*['"](?P<table>[^'"]+)['"]"""
)
SOURCE_RE = re.compile(
    r"""source\(\s*(?P<q1>['"])(?P<src>[^'"]*)"""
    r"""(?:(?P=q1)\s*,\s*(?P<q2>['"])(?P<tbl>[^'"]*))?$"""
)
COLUMN_RE = re.compile(r"(?P<alias>[a-zA-Z_]\w*)\.(?P<col>[a-zA-Z0-9_]*)$")
FROM_RE = re.compile(r"\b(?:from|join)\s+(?P<name>\w*)$", re.IGNORECASE)
# `from raw.` — a schema has been named, the table is being typed.
FROM_SCHEMA_RE = re.compile(
    r"\b(?:from|join)\s+(?P<schema>\w+)\.(?P<name>\w*)$", re.IGNORECASE
)


def completion_context(line_prefix: str):
    """What is the cursor currently completing? None if not in a ref/source."""
    if m := SOURCE_RE.search(line_prefix):
        # second arg started -> completing the table within a known source
        if m.group("tbl") is not None:
            return ("source_table", {"source": m.group("src")})
        # still in first arg -> completing the source name
        return ("source_name", {"quote": m.group("q1")})
    if m := REF_RE.search(line_prefix):
        return ("ref", {})
    # Before the column pattern, which would read `raw.` as an alias.
    # `start` is where the typed relation begins, so the completion can
    # replace it with a ref() or source() call.
    if m := FROM_SCHEMA_RE.search(line_prefix):
        return ("table", {"schema": m.group("schema"), "start": m.start("schema")})
    if m := COLUMN_RE.search(line_prefix):
        return ("column", {"alias": m.group("alias")})
    if m := FROM_RE.search(line_prefix):
        return ("cte", {"start": m.start("name")})
    return None


def ref_at(line: str, character: int) -> re.Match | None:
    """The ref('model') call whose model name the cursor is on."""
    for m in REF_FULL_RE.finditer(line):
        if m.start("model") <= character <= m.end("model"):
            return m
    return None


def ref_model_at(line: str, character: int) -> str | None:
    """
    Check if cursor is on a model
    """
    m = ref_at(line, character)
    return m.group("model") if m else None


def source_at(line: str, character: int) -> re.Match | None:
    """The source('src', 'table') call the cursor is on, on either argument."""
    for m in SOURCE_FULL_RE.finditer(line):
        if m.start("source") <= character <= m.end("table"):
            return m
    return None


# `alias.column` anywhere on a line. Unlike COLUMN_RE it is not anchored to
# the cursor, so it finds a reference the cursor is in the middle of.
DOTTED_RE = re.compile(r"\b(?P<alias>[a-zA-Z_]\w*)\.(?P<column>[a-zA-Z_]\w*)")


def dotted_at(line: str, character: int) -> re.Match | None:
    """The `alias.column` the cursor is on, on either name."""
    for m in DOTTED_RE.finditer(line):
        if m.start() <= character <= m.end():
            return m
    return None
