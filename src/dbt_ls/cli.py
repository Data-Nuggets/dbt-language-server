import argparse
import logging
import sys

from dbt_ls import __version__
from dbt_ls.server import server


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
    args = p.parse_args()
    if args.tcp:
        server.start_tcp(args.host, args.port)
    else:
        server.start_io()
    logging.info("DBT Language Server started")


if __name__ == "__main__":
    main()
