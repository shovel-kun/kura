from __future__ import annotations

import argparse
import sys

from .commands import (
    add_command,
    handle_config_error,
    import_command,
    init_command,
    list_command,
    run_command,
    status_command,
    stub_command,
    verify_command,
)
from .config import ConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kura")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="create config, profile, and state database")
    init_parser.set_defaults(func=init_command)

    add_parser = subcommands.add_parser("add", help="add a source to config.yaml")
    add_parser.add_argument("name")
    add_parser.add_argument("url")
    add_parser.add_argument("--dir", required=True, help="absolute output directory")
    add_parser.add_argument("--profile")
    add_parser.add_argument("--schedule")
    add_parser.add_argument("--strategy", choices=["full", "quick", "rss"])
    add_parser.add_argument("--cookies")
    add_parser.set_defaults(func=add_command)

    list_parser = subcommands.add_parser("list", help="list configured sources")
    list_parser.set_defaults(func=list_command)

    import_parser = subcommands.add_parser("import", help="adopt an existing yt-dlp archive directory")
    import_parser.add_argument("name", help="source name as defined in config.yaml")
    import_parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    import_parser.set_defaults(func=import_command)

    run_parser = subcommands.add_parser("run", help="run yt-dlp for a source")
    run_parser.add_argument("name", help="source name as defined in config.yaml")
    run_parser.set_defaults(func=run_command)

    verify_parser = subcommands.add_parser("verify", help="dry-run yt-dlp to report pending downloads")
    verify_parser.add_argument("name", help="source name as defined in config.yaml")
    verify_parser.set_defaults(func=verify_command)

    status_parser = subcommands.add_parser("status", help="show source status summary")
    status_parser.set_defaults(func=status_command)

    stubs = {
        "logs": "a later release",
        "pause": "a future release",
        "resume": "a future release",
        "suppress": "a future release",
        "unsuppress": "a future release",
        "doctor": "a later release",
    }
    for command, note in stubs.items():
        stub_parser = subcommands.add_parser(command)
        stub_parser.set_defaults(func=stub_command(command, note))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as error:
        return handle_config_error(error)


if __name__ == "__main__":
    sys.exit(main())
