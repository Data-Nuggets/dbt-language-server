# DBT-Language-Server

<img alt="DBT Language Server" src="https://vhs.charm.sh/vhs-1IEY4fW47S53ZaFfI9ri8B.gif" />

## Features

| Feature | LSP method / command | Description |
| --- | --- | --- |
| Model completion | `textDocument/completion` | Suggests dbt models inside `ref('...')` |
| Source completion | `textDocument/completion` | Suggests sources inside `source('...')`, auto-inserting the `source_name`, `table` pair   |
| Column completion | `textDocument/completion` | Suggests columns for an aliased model/source (`alias.<column>`), with the column's data type shown as a detail. |
| Go to definition | `textDocument/definition` | Jumps from `ref('model')` to that model's `.sql` file. |
| Config enrichment | on `initialize` | Reads documented columns from the hand-written `schema.yml` files. |
| Catalog enrichment | on `initialize` | Reads model and source column info from `target/catalog.json` when available. |
| Database enrichment | on `initialize` | Reads column names and data types for models directly from the connected warehouse. |
| Profile resolution | on `initialize` | Locates the dbt profile, resolves the active target    |
| Auto project discovery | on `initialize` | Finds the dbt project root by locating `dbt_project.yml` (ignoring `target/`). |
| Project reload | `dbt-ls.reload` command | Re-discovers models, sources, and re-runs enrichment without restarting the server. |
| Current model info | `dbt-ls.current_model` command | Returns the dbt project root and the model's execution path for the current file. (Can be used to run models)   |


## Installation
~~~sh
uv tool install dbt-ls

# Install each version with 
uv tool install dbt-ls[duckdb]

# or all supported backends with
uv tool install dbt-ls[all]
~~~

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

## Supported backends
- DuckDB
- SQL Server / MSSQL
- PostgreSQL
- MySQL
- Spark (Spark Connect, no auth)
- Databricks
- Athena
- Glue

> [!IMPORTANT]
> Databricks supports two auth methods, resolved from your dbt profile:
>
> - **Personal access token** — set `token` in the target.
> - **Service principal OAuth (M2M)** — set `client_id` and `client_secret` in the target. Used automatically when no `token` is present.
>
> AWS authentication uses the default profile unless you have specified 'aws_profile_name' in your DBT profile
