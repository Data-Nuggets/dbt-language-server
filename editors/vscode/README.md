# dbt Language Server — VS Code extension

Thin VS Code client for [`dbt-ls`](https://github.com/Data-Nuggets/dbt-ls). The
extension only launches the Python language server and connects to it over
stdio; all features (ref/source completion, go-to-definition, database-aware
column info) live in the server.

## Install

The server is a Python package, so installation is two parts:

### 1. Install the server (requires Python 3.10+)

```bash
pipx install "dbt-ls[postgres]"   # recommended: isolated, puts `dbt-ls` on PATH
# or: uv tool install dbt-ls
# or: pip install "dbt-ls[postgres]"
```

Pick the extra that matches your warehouse: `duckdb`, `postgres`, `mysql`,
`sqlserver`, `pyspark`, `databricks`, `aws`, or `all`.

Verify it is on your PATH:

```bash
dbt-ls --help
```

### 2. Install the extension

- From a packaged file: `code --install-extension dbt-ls-<version>.vsix`, or
  **Extensions panel → ⋯ → Install from VSIX…**
- From the Marketplace (once published): search "dbt Language Server".

If `dbt-ls` is not on your PATH (e.g. it lives in a project venv), set
`dbtLs.serverCommand` to its absolute path in your VS Code settings:

```json
{
  "dbtLs.serverCommand": "/path/to/venv/bin/dbt-ls"
}
```

## Settings

| Setting                | Default    | Description                                  |
| ---------------------- | ---------- | -------------------------------------------- |
| `dbtLs.serverCommand`  | `"dbt-ls"` | Command used to launch the language server.  |
| `dbtLs.serverArgs`     | `[]`       | Extra arguments passed to the command.       |

## Develop / package

```bash
cd editors/vscode
npm install
npm install -g @vscode/vsce

# Try it: open this folder in VS Code and press F5 (Extension Development Host).

vsce package        # -> dbt-ls-<version>.vsix
vsce publish        # -> VS Code Marketplace (needs publisher + PAT)
# ovsx publish      # -> Open VSX (VSCodium / Cursor)
```
