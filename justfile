compose := "docker compose -f test/compose.yml"

# list the recipes
default:
    @just --list

# the default suite: unit tests, stand-in cloud clients, DuckDB. No containers.
test *args:
    uv run pytest {{ args }}

# start the test warehouses and wait until they are healthy
containers-up:
    {{ compose }} up -d --wait

# stop the test warehouses and delete their data
containers-down:
    {{ compose }} down -v

# only the container tests; the warehouses must be up
test-containers *args:
    DBT_LS_REQUIRE_CONTAINERS=1 uv run pytest -m containers {{ args }}

# every test: starts the warehouses, runs both suites, stops the warehouses
test-all:
    #!/usr/bin/env bash
    set -uo pipefail
    # Warehouses that were up before are yours; they are left running.
    if [ -z "$({{ compose }} ps -q --status running)" ]; then
        trap '{{ compose }} down -v' EXIT
    fi
    {{ compose }} up -d --wait || exit 1
    status=0
    uv run pytest || status=$?
    DBT_LS_REQUIRE_CONTAINERS=1 uv run pytest -m containers || status=$?
    exit $status
