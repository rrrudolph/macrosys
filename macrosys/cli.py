"""Command-line entry point. Subcommands are registered by packages as they land (spec §13)."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="macrosys", description="Global macro research and trading system.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("version", help="print version")
    args = parser.parse_args(argv)
    if args.cmd == "version":
        from macrosys import __version__

        print(__version__)
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
