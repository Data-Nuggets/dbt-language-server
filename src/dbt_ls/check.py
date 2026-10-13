"""`dbt-ls check`: load a project the way the server does and report on it.

For verifying a profile, a warehouse connection or a platform without opening
an editor.
"""

import argparse
import json
import logging
import sys

from dbt_ls.exceptions import NoDbtRootError, NoRootPathError
from dbt_ls.server import Settings, _load_project, parse_schema_sources
from dbt_ls.state import ProjectState

# How many column-less models to name before summarising the rest.
MAX_LISTED = 10


def _table(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in rows
    ]


def render(state: ProjectState) -> str:
    report = state.report
    if report.profile_problem:
        profile = f"{report.profile_name or '(none)'}: {report.profile_problem}"
    else:
        profile = (
            f"{report.profile_name} ({report.profile_type}) "
            f"from {report.profile_path}"
        )
    source_names = {s.source_name for s in state.sources}
    lines = _table(
        [
            ("project", state.dbt_root),
            ("profile", profile),
            ("dialect", report.dialect),
            ("models", f"{len(state.models)}"),
            (
                "sources",
                f"{len(state.sources)} tables in {len(source_names)} sources",
            ),
            (
                "undocumented",
                f"{len(state.undocumented)} more warehouse tables, offered in completion",
            ),
        ]
    )

    lines.append("")
    if report.sources:
        # Counts are running totals: each source overwrites the ones before.
        rows = [
            (
                "after source",
                "models w/ columns",
                "w/ types",
                "sources w/ columns",
                "time",
                "result",
            )
        ]
        rows += [
            (
                s.source,
                str(s.models_with_columns),
                str(s.models_with_types),
                str(s.sources_with_columns),
                f"{s.seconds:.1f} s",
                s.outcome + (f": {s.detail}" if s.detail else ""),
            )
            for s in report.sources
        ]
        lines += _table(rows)
    else:
        lines.append("no schema sources enabled")

    bare = sorted(m.name for m in state.models if not m.columns)
    if bare:
        lines.append("")
        lines.append(f"{len(bare)} models have no columns from any source:")
        listed = ", ".join(bare[:MAX_LISTED])
        more = f", and {len(bare) - MAX_LISTED} more" if len(bare) > MAX_LISTED else ""
        lines.append(f"  {listed}{more}")

    if report.profile_note:
        lines.append("")
        lines.append(f"note: {report.profile_note}")

    lines.append("")
    lines.append("result: ok" if report.ok else "result: problems found")
    lines += [f"  {problem}" for problem in report.problems]
    return "\n".join(lines)


def as_json(state: ProjectState) -> str:
    return json.dumps(
        {
            "project": state.dbt_root,
            "ok": state.report.ok,
            "models": len(state.models),
            "source_tables": len(state.sources),
            "undocumented_tables": len(state.undocumented),
            "models_without_columns": sorted(
                m.name for m in state.models if not m.columns
            ),
            **state.report.to_dict(),
        },
        indent=2,
    )


def main(argv: list[str]) -> int:
    """Exit status: 0 all fine, 1 loaded with problems, 2 no project to load."""
    p = argparse.ArgumentParser(
        prog="dbt-ls check",
        description="Load a dbt project the way the language server does and "
        "report what each schema source returned.",
    )
    p.add_argument(
        "path", nargs="?", default=".", help="project directory (default: .)"
    )
    p.add_argument(
        "--schema-sources",
        default="config,catalog,database",
        metavar="SOURCE[,SOURCE...]",
        help="same meaning as for the server (default: %(default)s)",
    )
    p.add_argument(
        "--profiles-dir",
        metavar="DIR",
        help="same meaning as for the server (default: DBT_PROFILES_DIR, then "
        "the project, then ~/.dbt)",
    )
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also show the server's log, including tracebacks, on stderr",
    )
    args = p.parse_args(argv)

    try:
        settings = Settings(
            schema_sources=parse_schema_sources(args.schema_sources),
            profiles_dir=args.profiles_dir,
        )
    except ValueError as e:
        p.error(str(e))

    # The report says everything the log would; keep stderr quiet unless asked.
    logging.getLogger("dbt_ls").setLevel(
        logging.INFO if args.verbose else logging.CRITICAL
    )

    try:
        state = _load_project(args.path, settings)
    except (NoDbtRootError, NoRootPathError) as exc:
        print(f"dbt-ls check: {exc}", file=sys.stderr)
        return 2

    print(as_json(state) if args.json else render(state))
    return 0 if state.report.ok else 1
