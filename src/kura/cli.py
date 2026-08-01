from __future__ import annotations

import argparse
import importlib.metadata
import sys

from . import ui
from .commands import (
    add_command,
    doctor_command,
    handle_config_error,
    import_command,
    init_command,
    list_command,
    logs_command,
    pause_command,
    resume_command,
    run_command,
    status_command,
    suppress_command,
    unsuppress_command,
    verify_command,
    watch_command,
)
from .config import ConfigError
from .library import search_command, show_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kura")
    try:
        _version = importlib.metadata.version("kura")
    except importlib.metadata.PackageNotFoundError:
        _version = "unknown"
    parser.add_argument(
        "--version",
        action="version",
        version=f"kura {_version}",
    )
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
    add_parser.add_argument("--downloader", choices=["ytdlp", "nndownload"], default=None,
                            help="downloader to use (default: ytdlp)")
    add_parser.add_argument("--auth", default=None,
                            help="verbatim nndownload auth args (e.g. '--session-cookie /path/to/file')")
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

    watch_parser = subcommands.add_parser("watch", help="watch live progress for running sources")
    watch_parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="source name to watch (default: all enabled sources)",
    )
    watch_parser.add_argument(
        "--once",
        action="store_true",
        help="print one snapshot and exit",
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=0.5,
        metavar="SEC",
        help="polling interval in seconds (default: 0.5)",
    )
    watch_parser.add_argument(
        "--wait",
        action="store_true",
        help="wait for a run to start instead of exiting when nothing is running",
    )
    watch_parser.set_defaults(func=watch_command)

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
        "--extractor",
        default=None,
        help="extractor name (default: derived from source — 'niconico' for nndownload sources, 'youtube' otherwise)",
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

    logs_parser = subcommands.add_parser(
        "logs",
        help="print recent log lines for the latest run of a source",
    )
    logs_parser.add_argument("name", help="source name as defined in config.yaml")
    logs_parser.add_argument(
        "-n",
        type=int,
        default=50,
        metavar="N",
        help="number of lines to show (default: 50)",
    )
    logs_parser.add_argument(
        "--path",
        action="store_true",
        help="print only the absolute path of the latest log file",
    )
    logs_parser.set_defaults(func=logs_command)

    search_parser = subcommands.add_parser(
        "search",
        help="search the indexed video library",
    )
    search_parser.add_argument(
        "query",
        nargs="*",
        help="free-text terms; AND-matched against title and uploader (case-insensitive)",
    )
    search_parser.add_argument(
        "--source",
        action="append",
        dest="sources",
        metavar="NAME",
        default=None,
        help="limit to a source (repeatable)",
    )
    search_parser.add_argument(
        "--since",
        metavar="X",
        default=None,
        help="only videos downloaded on/after X (YYYY-MM-DD or a duration like 7d/12h)",
    )
    search_parser.add_argument(
        "--until",
        metavar="X",
        default=None,
        help="only videos downloaded on/before X (YYYY-MM-DD or a duration like 7d/12h)",
    )
    search_parser.add_argument(
        "--uploaded",
        action="store_true",
        help="match --since/--until against the upload date instead of the download time",
    )
    search_parser.add_argument(
        "--sort",
        choices=["downloaded", "uploaded", "title", "size"],
        default="downloaded",
        help="sort order (default: downloaded, newest first)",
    )
    search_parser.add_argument(
        "-n",
        type=int,
        default=20,
        metavar="N",
        help="number of results to show (default: 20, 0 = all)",
    )
    search_parser.add_argument(
        "--path",
        action="store_true",
        help="print one directory per line, nothing else (for piping to mpv/xargs)",
    )
    search_parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print matches as a JSON array",
    )
    search_parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="pick a video interactively with fzf; prints the selected directory",
    )
    search_parser.add_argument(
        "--reindex",
        action="store_true",
        help="rebuild the index from disk instead of scanning incrementally",
    )
    search_parser.set_defaults(func=search_command)

    show_parser = subcommands.add_parser(
        "show",
        help="show indexed details for one video",
    )
    show_parser.add_argument(
        "source_or_id",
        metavar="SOURCE",
        help="source name (or just VIDEO_ID when unambiguous)",
    )
    show_parser.add_argument(
        "video_id",
        nargs="?",
        default=None,
        metavar="VIDEO_ID",
        help="video ID as recorded in the metadata sidecar",
    )
    show_parser.set_defaults(func=show_command)

    doctor_parser = subcommands.add_parser(
        "doctor",
        help="run health checks and report any problems",
    )
    doctor_parser.set_defaults(func=doctor_command)

    ui_parser = subcommands.add_parser("ui", help="browse the library interactively")
    ui_parser.set_defaults(func=_ui_command)

    return parser


_COMMAND_GROUPS = [
    ("Setup", [
        ("init", "create config, profiles, and the state database"),
        ("add", "add a channel or playlist source"),
        ("import", "adopt an existing archive directory"),
    ]),
    ("Archiving", [
        ("run", "download new videos (NAME, --due, or --all)"),
        ("verify", "show what would download, without downloading"),
        ("watch", "live progress of a running download"),
        ("pause", "pause a source's schedule"),
        ("resume", "resume a source's schedule"),
    ]),
    ("Library", [
        ("list", "list configured sources"),
        ("status", "source overview and recent runs"),
        ("search", "search the archived video library"),
        ("show", "show details for one video"),
        ("ui", "interactive terminal browser"),
        ("logs", "view a source's run log"),
    ]),
    ("Maintenance", [
        ("suppress", "mark a video as archived (skip future downloads)"),
        ("unsuppress", "undo a suppression"),
        ("doctor", "check environment health"),
    ]),
]


def _print_overview() -> None:
    try:
        version = importlib.metadata.version("kura")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    print(f"{ui.bold('kura')} {ui.dim(f'· YouTube/niconico archive orchestrator · {version}')}")
    print()
    print(f"{ui.dim('usage:')} kura {ui.cyan('<command>')} [options]")
    width = max(len(cmd) for _, cmds in _COMMAND_GROUPS for cmd, _ in cmds)
    for title, cmds in _COMMAND_GROUPS:
        print()
        print(f"  {ui.bold(title)}")
        for cmd, desc in cmds:
            print(f"    {ui.cyan(ui.pad(cmd, width))}   {ui.dim(desc)}")
    print()
    print(ui.dim("run `kura <command> -h` for details on any command"))


def _run_command_wrapper(args) -> int:
    """Validate run-mode flag combinations then delegate to run_command."""
    force: bool = args.force
    due: bool = args.due
    all_mode: bool = args.all
    name: str | None = args.name

    if force and (due or all_mode):
        print(ui.red(f"{ui.FAIL} --force cannot be combined with --due or --all"))
        return 2

    if name is None and not due and not all_mode:
        print(ui.red(f"{ui.FAIL} specify a source name, --due, or --all"))
        return 2

    return run_command(args)


def _ui_command(_args) -> int:
    if not sys.stdout.isatty():
        print(ui.red(f"{ui.FAIL} kura ui needs an interactive terminal"))
        return 1
    from .tui import run_ui

    return run_ui()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("help", "-h", "--help"):
        # `kura help CMD` → that command's detailed argparse help.
        if argv and argv[0] == "help" and len(argv) > 1:
            return main([argv[1], "--help"])
        _print_overview()
        return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as error:
        return handle_config_error(error)


if __name__ == "__main__":
    sys.exit(main())
