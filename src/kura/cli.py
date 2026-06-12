from __future__ import annotations

import argparse
import sys

from .commands import (
    add_command,
    handle_config_error,
    import_command,
    init_command,
    list_command,
    pause_command,
    resume_command,
    run_command,
    status_command,
    stub_command,
    suppress_command,
    unsuppress_command,
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

    # run: three mutually exclusive modes:
    #   kura run NAME [--force]
    #   kura run --due
    #   kura run --all
    run_parser = subcommands.add_parser("run", help="run yt-dlp for a source")
    run_mode = run_parser.add_mutually_exclusive_group()
    run_mode.add_argument("name", nargs="?", default=None, help="source name as defined in config.yaml")
    run_mode.add_argument("--due", action="store_true", help="run all enabled sources whose cron schedule has elapsed")
    run_mode.add_argument("--all", action="store_true", dest="all", help="run all enabled sources regardless of schedule")
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="run the named source even if it is disabled (only valid with NAME)",
    )
    run_parser.set_defaults(func=_run_command_wrapper)

    verify_parser = subcommands.add_parser("verify", help="dry-run yt-dlp to report pending downloads")
    verify_parser.add_argument("name", help="source name as defined in config.yaml")
    verify_parser.set_defaults(func=verify_command)

    status_parser = subcommands.add_parser("status", help="show source status summary")
    status_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="source name for detailed view (optional)",
    )
    status_parser.set_defaults(func=status_command)

    pause_parser = subcommands.add_parser("pause", help="disable a source (pause scheduling)")
    pause_parser.add_argument("name", help="source name as defined in config.yaml")
    pause_parser.set_defaults(func=pause_command)

    resume_parser = subcommands.add_parser("resume", help="enable a source (resume scheduling)")
    resume_parser.add_argument("name", help="source name as defined in config.yaml")
    resume_parser.set_defaults(func=resume_command)

    suppress_parser = subcommands.add_parser(
        "suppress", help="add a video to the archive to suppress future downloads"
    )
    suppress_parser.add_argument("name", help="source name as defined in config.yaml")
    suppress_parser.add_argument("video_id", help="video ID to suppress")
    suppress_parser.add_argument("--reason", default=None, help="optional reason for suppression")
    suppress_parser.add_argument(
        "--extractor", default="youtube", help="extractor name (default: youtube)"
    )
    suppress_parser.set_defaults(func=suppress_command)

    unsuppress_parser = subcommands.add_parser(
        "unsuppress", help="remove a suppressed video from the archive"
    )
    unsuppress_parser.add_argument("name", help="source name as defined in config.yaml")
    unsuppress_parser.add_argument("video_id", help="video ID to unsuppress")
    unsuppress_parser.add_argument(
        "--extractor", default="youtube", help="extractor name (default: youtube)"
    )
    unsuppress_parser.set_defaults(func=unsuppress_command)

    stubs = {
        "logs": "a later release",
        "doctor": "a later release",
    }
    for command, note in stubs.items():
        stub_parser = subcommands.add_parser(command)
        stub_parser.set_defaults(func=stub_command(command, note))

    return parser


def _run_command_wrapper(args) -> int:
    """Validate run-mode flag combinations then delegate to run_command."""
    force: bool = args.force
    due: bool = args.due
    all_mode: bool = args.all
    name: str | None = args.name

    if force and (due or all_mode):
        print("error: --force cannot be combined with --due or --all")
        return 2

    if name is None and not due and not all_mode:
        print("error: specify a source name, --due, or --all")
        return 2

    return run_command(args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as error:
        return handle_config_error(error)


if __name__ == "__main__":
    sys.exit(main())
