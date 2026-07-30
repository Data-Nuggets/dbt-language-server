import re

REF_RE = re.compile(r"""ref\(\s*(?P<q>['"])(?P<model>[^'"]*)$""")
# Full ref('model') call, used to find the model the cursor is *inside of*.
REF_FULL_RE = re.compile(r"""ref\(\s*['"](?P<model>[^'"]+)['"]""")
SOURCE_RE = re.compile(
    r"""source\(\s*(?P<q1>['"])(?P<src>[^'"]*)"""
    r"""(?:(?P=q1)\s*,\s*(?P<q2>['"])(?P<tbl>[^'"]*))?$"""
)
COLUMN_RE = re.compile(r"(?P<alias>[a-zA-Z_]\w*)\.(?P<col>[a-zA-Z0-9_]*)$")
FROM_RE = re.compile(r"\b(?:from|join)\s+(?P<name>\w*)$", re.IGNORECASE)


def completion_context(line_prefix: str):
    """What is the cursor currently completing? None if not in a ref/source."""
    if m := SOURCE_RE.search(line_prefix):
        # second arg started -> completing the table within a known source
        if m.group("tbl") is not None:
            return ("source_table", {"source": m.group("src")})
        # still in first arg -> completing the source name
        return ("source_name", {})
    if m := REF_RE.search(line_prefix):
        return ("ref", {})
    if m := COLUMN_RE.search(line_prefix):
        return ("column", {"alias": m.group("alias")})
    if m := FROM_RE.search(line_prefix):
        return ("cte", {})
    return None


def ref_model_at(line: str, character: int) -> str | None:
    """
    Check if cursor is on a model
    """
    for m in REF_FULL_RE.finditer(line):
        if m.start("model") <= character <= m.end("model"):
            return m.group("model")
    return None
