# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown>=3.6", "pygments>=2.18"]
# ///
"""Build docs/index.html, one page with tabs.

    uv run docs/build.py             everything, for yourself
    uv run docs/build.py --public    what GitHub Pages serves

Overview, How it works and Findings are the fragments in docs/src.
Use it is rendered from README.md, Tasks from todo.md.

The public page leaves out the Findings and Tasks tabs, which are working
notes, and every element marked `data-internal` in the other tabs.
"""

import argparse
import datetime
import html
import json
import re
from pathlib import Path

import markdown
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

DOCS = Path(__file__).parent
SRC = DOCS / "src"
ROOT = DOCS.parent

TABS = [
    ("overview", "Overview"),
    ("use", "Use it"),
    ("internals", "How it works"),
    ("findings", "Findings"),
    ("tasks", "Tasks"),
]
# README sections the Overview tab already covers
README_SKIP = {"Features"}
# Working notes, not user documentation: left out of the public page.
INTERNAL_TABS = {"findings", "tasks"}
INTERNAL_ELEMENT = re.compile(r"<(\w+)\b[^>]*\sdata-internal\b[^>]*>.*?</\1>", re.S)
REPO = "https://github.com/Data-Nuggets/dbt-language-server"
WINDOW_TITLES = {"sh": "shell", "lua": "init.lua", "json": "settings.json", "sql": "model.sql"}
ALERT_NAMES = {"NOTE": "Note", "IMPORTANT": "Important", "WARNING": "Warning", "TIP": "Tip"}


# --- README.md -> the "Use it" tab -------------------------------------------


def slug(text: str, limit: int = 70) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit]


def code_window(language: str, code: str) -> str:
    try:
        lexer = get_lexer_by_name(language or "text")
    except ClassNotFound:
        lexer = get_lexer_by_name("text")
    body = highlight(code, lexer, HtmlFormatter(nowrap=True)).rstrip("\n")
    title = WINDOW_TITLES.get(language, language or "text")
    return (
        '<div class="window"><div class="window-bar"><span class="dots"><i></i><i></i><i></i></span>'
        f'<span>{title}</span><button type="button" data-copy>Copy</button></div>'
        f'<pre class="hl"><code>{body}</code></pre></div>'
    )


def pairs_or_table(match: re.Match) -> str:
    """A two-column table reads better as a list of pairs; wider ones scroll."""
    table = match.group(0)
    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
    cells = [re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S) for row in rows]
    if cells and all(len(row) == 2 for row in cells):
        items = "".join(f"<div><dt>{a}</dt><dd>{b}</dd></div>" for a, b in cells[1:])
        return f'<dl class="pairs">{items}</dl>'
    return f'<div class="tablewrap">{table}</div>'


def alert(match: re.Match) -> str:
    kind = match.group(1)
    return f'<div class="callout {kind.lower()}"><b>{ALERT_NAMES[kind]}</b><p>{match.group(2).strip()}'


def prose(text: str) -> str:
    out = markdown.markdown(text, extensions=["tables"])
    # links into the repository only work on GitHub
    out = re.sub(r'href="(?!https?:|#)([^"]+)"', rf'href="{REPO}/blob/main/\1"', out)
    out = re.sub(r"<table>.*?</table>", pairs_or_table, out, flags=re.S)
    out = re.sub(r"<blockquote>\s*<p>\[!(\w+)\]\s*(.*?)</blockquote>", lambda m: alert(m) + "</div>", out, flags=re.S)
    return out.replace("<h3>", '<h3 class="sub">')


def render_markdown(text: str) -> str:
    parts = re.split(r"^~~~(\w*)\n(.*?)^~~~\s*$", text, flags=re.S | re.M)
    out = []
    for i in range(0, len(parts), 3):
        if parts[i].strip():
            out.append(prose(parts[i]))
        if i + 2 < len(parts):
            out.append(code_window(parts[i + 1], parts[i + 2]))
    return "\n".join(out)


def use_tab(readme: str, version: str) -> str:
    sections = re.split(r"^## (.+)$", readme, flags=re.M)[1:]
    out = [
        '<header class="hero compact"><span class="eyebrow">dbt-ls · use it</span>'
        "<h1>Install, configure, check</h1>"
        f'<p class="lede">The README of release {version}, as it is in the repository.</p></header>'
    ]
    for title, body in zip(sections[::2], sections[1::2]):
        title = title.strip()
        if title in README_SKIP:
            continue
        out.append(
            f'<section id="use-{slug(title)}" data-toc="{html.escape(title)}">'
            f"<h2>{html.escape(title)}</h2>\n{render_markdown(body)}</section>"
        )
    return "\n".join(out)


# --- todo.md -> the data behind the "Tasks" tab -------------------------------

ITEM = re.compile(r"^(?P<indent> *)- \[(?P<mark>[ x])\] (?P<text>.*)$")
TASK = re.compile(r"^\*\*(?:(?P<id>T\d+)\. )?(?P<title>.+?)\.?\*\*\s*(?P<rest>.*)$", re.S)
FINDINGS = re.compile(r"^\((?P<list>F\d+(?:, F\d+)*), (?P<kind>confirmed|read)\)\s*")


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


def plain(text: str) -> str:
    return re.sub(r"[`*]", "", text)


def read_todo(text: str) -> dict:
    lines = text.rstrip("\n").split("\n")
    groups, group, task, item = [], None, None, None
    for number, line in enumerate(lines):
        if line.startswith("## "):
            group = {"title": line[3:].strip(), "tasks": []}
            groups.append(group)
            task = item = None
        elif (found := ITEM.match(line)) and group is not None:
            item = {"line": number, "checked": found["mark"] == "x", "raw": found["text"]}
            if found["indent"]:
                task["subs"].append(item)
            else:
                task = item | {"subs": []}
                item = task
                group["tasks"].append(task)
        elif item is not None and line.strip():
            item["raw"] += " " + line.strip()
        elif not line.strip():
            item = None

    for group in groups:
        for task in group["tasks"]:
            raw = task.pop("raw")
            found = TASK.match(raw)
            task_id, title, rest = (found["id"], found["title"], found["rest"]) if found else (None, raw.rstrip("."), "")
            findings = []
            if tagged := FINDINGS.match(rest):
                findings = [{"id": f, "kind": tagged["kind"]} for f in tagged["list"].split(", ")]
            body = rest[tagged.end() :] if tagged else rest
            task |= {
                "id": task_id,
                "title": inline(title),
                "body": inline(body),
                "findings": findings,
                "key": slug(plain(title)),
                "text": f"{task_id or ''} {plain(raw)}",
            }
            for sub in task["subs"]:
                raw = sub.pop("raw")
                sub |= {"html": inline(raw), "key": slug(plain(raw)), "text": plain(raw)}
    return {"groups": groups, "lines": lines}


# --- the page ----------------------------------------------------------------


def build(artifact: bool = False, public: bool = False) -> str:
    version = re.search(r'^version = "(.+?)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    today = datetime.date.today()
    stamp = f"{today.day} {today:%B %Y}"
    shown = [(key, label) for key, label in TABS if not (public and key in INTERNAL_TABS)]
    panels = {
        key: use_tab((ROOT / "README.md").read_text(), version)
        if key == "use"
        else (SRC / f"{key}.html").read_text()
        for key, _ in shown
    }
    tabs = "".join(
        f'<a role="tab" id="tab-{key}" href="#{key}" data-tab="{key}" aria-controls="panel-{key}">{label}</a>'
        for key, label in shown
    )
    body = "\n".join(
        f'<div class="panel" role="tabpanel" id="panel-{key}" data-panel="{key}" aria-labelledby="tab-{key}" hidden>\n'
        f"{panels[key]}\n</div>"
        for key, _ in shown
    )
    data, script = "", (SRC / "app.js").read_text()
    if "tasks" in panels:
        todo = json.dumps(read_todo((ROOT / "todo.md").read_text())).replace("</", "<\\/")
        data = f'<script id="data" type="application/json">{todo}</script>\n'
        script = (SRC / "tasks.js").read_text() + "\n" + script
    page = (
        (SRC / "shell.html")
        .read_text()
        .replace("{{style}}", (SRC / "style.css").read_text())
        .replace("{{tabs}}", tabs)
        .replace("{{panels}}", body)
        .replace("{{data}}", data)
        .replace("{{script}}", script)
        .replace("{{version}}", version)
        .replace("{{date}}", stamp)
    )
    if public:
        page = INTERNAL_ELEMENT.sub("", page)
    if artifact:
        return page
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"{page}"
    ).replace("</style>\n", "</style>\n</head>\n<body>\n", 1) + "</body>\n</html>\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path, default=DOCS / "index.html")
    parser.add_argument(
        "--artifact", action="store_true", help="leave out the html, head and body tags, for publishing as an artifact"
    )
    parser.add_argument(
        "--public", action="store_true", help="leave out the Findings and Tasks tabs and everything marked data-internal"
    )
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build(args.artifact, args.public))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
