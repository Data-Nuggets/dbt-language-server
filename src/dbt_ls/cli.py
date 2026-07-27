import argparse
import logging
import sys

from dbt_ls import __version__
from dbt_ls.server import Settings, parse_schema_sources, server


def main():
    banner = f""" ╔═══════════════════════════════════════╗
   ║                                       ║
   ║      _ _     _        _               ║
   ║   __| | |__ | |_     | |___           ║
   ║  / _` | '_ \\| __|____| / __|          ║
   ║ | (_| | |_) | ||_____| \\__ \\          ║
   ║  \\__,_|_.__/ \\__|    |_|___/          ║
   ║                                       ║
   ║   {__version__:^5} · Language Server · stdio     ║
   ║                                       ║
   ╚═══════════════════════════════════════╝
    """
    print(banner, file=sys.stderr)

    p = argparse.ArgumentParser()
    # stdio is the default transport; --stdio is accepted (as a no-op) because
    # LSP clients such as VS Code pass it to select the stdio transport.
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--tcp", action="store_true")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--schema-sources",
        type=str,
        default="config,catalog,database",
        metavar="SOURCE[,SOURCE...]",
        help="Comma-separated model schema sources in ascending priority: the "
        "last one that yields columns wins. Omit a source to disable it "
        "entirely, e.g. `--schema-sources config,catalog` never connects to "
        "the warehouse. (default: %(default)s)",
    )
    args = p.parse_args()

    try:
        schema_sources = parse_schema_sources(args.schema_sources)
    except ValueError as e:
        p.error(str(e))
    server.settings = Settings(schema_sources=schema_sources)

    if args.tcp:
        server.start_tcp(args.host, args.port)
    else:
        server.start_io()
    logging.info("DBT Language Server started")


if __name__ == "__main__":
    main()
