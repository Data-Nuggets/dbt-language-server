# DBT-Language-Server

<img alt="DBT Language Server" src="https://vhs.charm.sh/vhs-Tcmu3EkCB3f83K6U4W6fB.gif" />

## Features

| Feature | LSP method / command | Description |
| --- | --- | --- |
| Model completion | `textDocument/completion` | Suggests dbt models inside `ref('...')` |
| Source completion | `textDocument/completion` | Suggests sources inside `source('...')`, auto-inserting the `source_name`, `table` pair. In the second argument, suggests the tables of that source. |
| CTE completion | `textDocument/completion` | Suggests the CTEs of the current file after `from` / `join`. |
| Column completion | `textDocument/completion` | Suggests columns for an aliased model, source, CTE or `schema.table` (`alias.<column>`), with the column's data type shown as a detail. |
| Undocumented tables | `textDocument/completion` | Warehouse tables the project has no YAML for still complete: inside `source('...')` (marked `not in sources.yml`), as `schema.table` after `from` / `join`, and their columns after an alias. Needs the `database` schema source. |
| Go to definition | `textDocument/definition` | Jumps from `ref('model')` to that model's `.sql` file. |
| Config enrichment | on `initialize` | Reads documented columns from the hand-written `schema.yml` files. |
| Catalog enrichment | on `initialize` | Reads model and source column info from `target/catalog.json` when available. |
| Database enrichment | in the background after `initialize` | Reads column names and data types for models and sources directly from the connected warehouse. |
| Profile resolution | on `initialize` | Locates the dbt profile, resolves the active target    |
| Auto project discovery | on `initialize` | Finds the dbt project root by locating `dbt_project.yml` (ignoring `target/`). |
| Project reload | `dbt-ls.reload` command | Re-discovers models, sources, and re-runs enrichment without restarting the server. |
| Current model info | `dbt-ls.current_model` command | Returns the dbt project root and the model's execution path for the current file. (Can be used to run models)   |
| Project check | `dbt-ls check` in a terminal | Loads a project without an editor and prints what each schema source returned. |


## Installation

For VSCode installation, see [VSCode installation](editors/vscode/README.md)

~~~sh
uv tool install dbt-ls

# with the driver for one backend:
# duckdb, postgres, mysql, sqlserver, pyspark, databricks or aws
uv tool install "dbt-ls[duckdb]"

# every backend
uv tool install "dbt-ls[all,aws]"
~~~

The `aws` extra covers Athena, Glue and Redshift. `all` does not include it
yet, so name both.

## Configuration
~~~lua
vim.lsp.config("dbt_ls", {
    cmd = { "dbt-ls" },
    filetypes = { "sql" },
    root_markers = { "dbt_project.yml" },
})
vim.lsp.enable("dbt_ls")

~~~

### Schema sources

Column information can come from three places. Pick which ones to use, and in
what order, with `--schema-sources`:

| Source | Reads from | Provides |
| --- | --- | --- |
| `config` | your `*.yml` files with 'models' key | documented column names (no data types) |
| `catalog` | `target/catalog.json` | column names and data types, as of the last `dbt docs generate` |
| `database` | the warehouse in your dbt profile | live column names and data types |

The list is in **ascending priority — the last source that returns columns for a
model wins.** The default is every source, warehouse last:

~~~sh
dbt-ls --schema-sources config,catalog,database
~~~

Leaving a source out disables it entirely. This never opens a warehouse
connection, which is useful without credentials or on a slow link:

~~~sh
dbt-ls --schema-sources config,catalog
~~~

Reversing the order makes your documented `schema.yml` win over what the
warehouse reports:

~~~sh
dbt-ls --schema-sources database,config
~~~

> [!NOTE]
> `config` carries column *names* but not data types, so putting it last means
> completions show no type detail. Put it first unless that's what you want.

Pass it as separate `cmd` arguments — there is no shell to split the string:

~~~lua
vim.lsp.config("dbt_ls", {
    cmd = { "dbt-ls", "--schema-sources", "config,catalog" },
    filetypes = { "sql" },
    root_markers = { "dbt_project.yml" },
})
vim.lsp.enable("dbt_ls")
~~~

In VS Code, use the extension's `dbtLs.serverArgs` setting:

~~~json
{
  "dbtLs.serverArgs": ["--schema-sources", "config,catalog"]
}
~~~

## Tables without YAML

With the `database` schema source on, the server reads every schema of the
warehouse, not only the ones the project mentions. A table that matches no
model and no documented source is still offered, so nothing has to be
documented before it completes:

~~~sql
select o.                           -- columns of raw.orders, with types
from {{ source('raw', 'orders') }} o   -- offered as "raw · not in sources.yml"
join crm.                           -- the tables of schema crm
~~~

| You type | You get |
| --- | --- | 
| `source('` | Documented sources first, then every other warehouse table. Accepting one inserts the schema as the source name. |
| `source('raw', '` | The tables of `raw`, including the ones `sources.yml` leaves out. |
| `from ` / `join ` | The CTEs of the file, then `schema.table` for each undocumented table. |
| `from raw.` | The tables of schema `raw`. |
| `alias.` | Typed columns, for `from raw.orders o` as well as for a `source()` call. |

What the project declares always wins: an undocumented table is used only when
no CTE, model or documented source has the name.

> [!NOTE]
> dbt itself rejects a `source()` call until the source is declared in YAML.
> The completion gets you the names and columns; add the source before
> `dbt run`. The plain `schema.table` form runs as it is.

Only the database named in the profile is read. Sources that live in another
database are completed from YAML and the catalog only.

## Startup

Model, source and CTE names, and columns from YAML and `catalog.json`,
complete as soon as the project files are read. The warehouse is read after
that in the background, in one statement per database for DuckDB, PostgreSQL,
MySQL and SQL Server, and live columns appear when it finishes. On a generated
project with 2,000 models and a 6,000-table DuckDB warehouse the first
completion arrives after about 2 seconds and live columns after about 5.

`scripts/generate_bench_project.py` builds such a project if you want to
measure your own changes.

## Checking a project without an editor

`dbt-ls check` loads a project exactly as the server does and prints what each
schema source returned, so a profile or warehouse connection can be verified
from a terminal:

~~~sh
dbt-ls check                      # the project in the current directory
dbt-ls check path/to/project --schema-sources config,catalog
dbt-ls check --json               # machine-readable
dbt-ls check -v                   # also print the server log and tracebacks
~~~

It exits with `0` when everything loaded, `1` when the project loaded but
something went wrong (no usable profile, a missing driver, a failed
connection), and `2` when no `dbt_project.yml` was found.

Model, source and CTE completion do not need a profile or a warehouse. When
one of those is unavailable the server still starts and reports the reason in
its progress message; only live column information is missing.

## Development

The shortcuts are recipes for [just](https://github.com/casey/just):

~~~sh
uv sync
just test          # unit tests, stand-in cloud clients, DuckDB; no containers
just test-all      # everything: starts the warehouses, tests, stops them
~~~

The readers for PostgreSQL, MySQL, SQL Server and Spark are tested against
real instances in containers. Those tests are left out of `just test` and of
a plain `uv run pytest`. To keep the warehouses up while you work on them:

~~~sh
just containers-up
just test-containers          # extra arguments go to pytest, e.g. -k mysql
just containers-down
~~~

`just test-all` leaves the warehouses running if they were already up when it
started.

The containers listen on ports 55432, 53306, 51433 and 55002, so they do not
collide with a database you already run. The SQL Server tests need "ODBC Driver 18 for SQL Server" on the
machine.

## Supported backends
- DuckDB
- SQL Server / MSSQL
- PostgreSQL
- MySQL
- Spark (Spark Connect, no auth)
- Databricks
- Athena
- Glue
- Redshift (IAM sign-in through the Redshift Data API)

> [!IMPORTANT]
> Databricks supports two auth methods, resolved from your dbt profile:
>
> - **Personal access token** — set `token` in the target.
> - **Service principal OAuth (M2M)** — set `client_id` and `client_secret` in the target. Used automatically when no `token` is present.
>
> AWS authentication uses the default profile unless you have specified 'aws_profile_name' in your DBT profile
