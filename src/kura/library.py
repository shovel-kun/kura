"""Indexed video library: sidecar scanning, `kura search`, and `kura show`.

The ``videos`` table in the state database is a rebuildable cache of the
metadata sidecars on disk (yt-dlp ``*.info.json`` and nndownload ``*.json``).
Disk stays the source of truth: the index is refreshed incrementally on every
search/show invocation and can be rebuilt from scratch with ``--reindex``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import paths, state, ui
from .archive import MEDIA_EXTENSIONS, archive_key, nndownload_sidecar_info
from .config import ConfigError, load_config, parse_duration

VIDEO_COLUMNS = (
    "source",
    "archive_key",
    "video_id",
    "title",
    "uploader",
    "upload_date",
    "duration_s",
    "size_bytes",
    "dir",
    "downloaded_at",
)

# Files modified within this window before last_scan_at are re-parsed anyway,
# so a write racing the previous scan is never missed.
_SCAN_EPSILON_S = 2.0

_UPSERT_SQL = (
    f"INSERT INTO videos ({', '.join(VIDEO_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(VIDEO_COLUMNS))}) "
    "ON CONFLICT(source, archive_key) DO UPDATE SET "
    + ", ".join(f"{col} = excluded.{col}" for col in VIDEO_COLUMNS[2:])
)


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


def refresh_index(source, *, full: bool = False, db_path: Path | None = None) -> int:
    """Scan *source*'s output_dir sidecars into the ``videos`` table.

    Incremental by default: only sidecars modified since the last scan
    (minus a 2s epsilon) are parsed.  ``full=True`` deletes the source's rows
    first and re-parses everything.

    Returns the number of rows upserted.
    """
    db_path = db_path or paths.state_db()
    state.init_db(db_path)
    upserted = 0
    conn = state.connect(db_path)
    try:
        with conn:
            row = conn.execute(
                "SELECT last_scan_at FROM video_index_state WHERE source = ?",
                (source.name,),
            ).fetchone()
            last_scan_at = row[0] if row else None

            if full:
                conn.execute("DELETE FROM videos WHERE source = ?", (source.name,))
                last_scan_at = None

            cutoff: float | None = None
            if last_scan_at:
                try:
                    cutoff = datetime.fromisoformat(last_scan_at).timestamp() - _SCAN_EPSILON_S
                except ValueError:
                    cutoff = None

            scan_started = datetime.now(tz=UTC).isoformat()

            output_dir = source.output_dir
            if output_dir.is_dir():
                for sidecar in sorted(output_dir.rglob("*.json")):
                    if sidecar.name.endswith(".comments.json"):
                        continue
                    try:
                        mtime = sidecar.stat().st_mtime
                    except OSError:
                        continue
                    if cutoff is not None and mtime <= cutoff:
                        continue
                    values = _extract_row(source.name, sidecar, mtime)
                    if values is None:
                        continue
                    conn.execute(_UPSERT_SQL, values)
                    upserted += 1

            conn.execute(
                "INSERT INTO video_index_state (source, last_scan_at) VALUES (?, ?) "
                "ON CONFLICT(source) DO UPDATE SET last_scan_at = excluded.last_scan_at",
                (source.name, scan_started),
            )
    finally:
        conn.close()
    return upserted


def _extract_row(source_name: str, sidecar: Path, mtime: float) -> tuple | None:
    """Extract a ``videos`` row tuple from a sidecar file, or None to skip."""
    if sidecar.name.endswith(".info.json"):
        parsed = _parse_ytdlp_sidecar(sidecar)
        stem = sidecar.name[: -len(".info.json")]
    else:
        parsed = _parse_nndownload_sidecar(sidecar)
        stem = sidecar.name[: -len(".json")]
    if parsed is None:
        return None
    key, video_id, title, uploader, upload_date, duration_s = parsed
    size_bytes = _sibling_media_size(sidecar.parent, stem)
    downloaded_at = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
    return (
        source_name,
        key,
        video_id,
        title,
        uploader,
        upload_date,
        duration_s,
        size_bytes,
        str(sidecar.parent.resolve()),
        downloaded_at,
    )


def _parse_ytdlp_sidecar(path: Path) -> tuple | None:
    """Parse a yt-dlp ``*.info.json``; parse errors are tolerated silently."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    # Skip playlist-level info.json files (same rule as `kura import`).
    if info.get("_type", "") in ("playlist", "multi_video"):
        return None
    key = archive_key(info)
    if key is None:
        return None
    video_id = info.get("id")
    title = info.get("title") if isinstance(info.get("title"), str) else None
    uploader = info.get("uploader") if isinstance(info.get("uploader"), str) else None
    upload_date = None
    raw_date = info.get("upload_date")
    if isinstance(raw_date, str) and re.fullmatch(r"\d{8}", raw_date):
        upload_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    return key, str(video_id), title, uploader, upload_date, _as_seconds(info.get("duration"))


def _parse_nndownload_sidecar(path: Path) -> tuple | None:
    """Parse an nndownload sidecar via the shared qualification helper."""
    data = nndownload_sidecar_info(path)
    if data is None:
        return None
    video_id = data["id"]
    title = data.get("title") if isinstance(data.get("title"), str) else None
    uploader = data.get("uploader") if isinstance(data.get("uploader"), str) else None
    upload_date = None
    published = data.get("published")
    if isinstance(published, str):
        m = re.match(r"\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})", published)
        if m:
            year, month, day = (int(g) for g in m.groups())
            upload_date = f"{year:04d}-{month:02d}-{day:02d}"
    return f"niconico {video_id}", video_id, title, uploader, upload_date, _as_seconds(data.get("duration"))


def _as_seconds(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _sibling_media_size(directory: Path, stem: str) -> int | None:
    """Size of the media file sharing *stem* in *directory*, or None."""
    for ext in sorted(MEDIA_EXTENSIONS):
        candidate = directory / f"{stem}.{ext}"
        try:
            if candidate.is_file():
                return candidate.stat().st_size
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_duration(seconds: int, *, precise: bool = False) -> str:
    """``57s`` / ``12m`` / ``1h 3m`` — with ``precise=True``, ``12m 4s``."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if precise:
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs:
            parts.append(f"{secs}s")
        return " ".join(parts)
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def fmt_size(size_bytes: int) -> str:
    """Human SI size: ``98 MB``, ``1.2 GB`` (1 decimal below 10)."""
    value = float(size_bytes)
    unit = "B"
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            break
        value /= 1000
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"


def _date_col(downloaded_at: str | None, now: datetime | None = None) -> str:
    if not downloaded_at:
        return ""
    local = ui.local_dt(downloaded_at)
    local_now = (now or datetime.now(tz=UTC)).astimezone()
    if local.year == local_now.year:
        return local.strftime("%b %d")
    return local.strftime("%b %d %Y")


def _meta_cell(row: dict, *, single_source: bool) -> str:
    parts: list[str] = []
    if not single_source:
        parts.append(row["source"])
    if row["duration_s"] is not None:
        parts.append(fmt_duration(row["duration_s"]))
    if row["size_bytes"] is not None:
        parts.append(fmt_size(row["size_bytes"]))
    return f" {ui.DOT} ".join(parts)


# ---------------------------------------------------------------------------
# Query pipeline
# ---------------------------------------------------------------------------


def _load_rows(db_path: Path, source_names: list[str]) -> list[dict]:
    if not source_names:
        return []
    conn = state.connect(db_path)
    try:
        placeholders = ", ".join("?" * len(source_names))
        cur = conn.execute(
            f"SELECT {', '.join(VIDEO_COLUMNS)} FROM videos WHERE source IN ({placeholders})",
            source_names,
        )
        return [dict(zip(VIDEO_COLUMNS, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _parse_bound(value: str, *, end: bool = False) -> tuple[datetime, date]:
    """Parse ``YYYY-MM-DD`` or a duration (``7d``/``12h``) into a bound.

    Returns ``(datetime_utc, date)``.  Date bounds are interpreted in local
    time; ``end=True`` extends a date bound to the end of that day so
    ``--until DATE`` is inclusive.  Duration bounds mean "now minus X".
    """
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            d = date.fromisoformat(text)
        except ValueError as exc:
            raise ConfigError(f"invalid date {value!r}: {exc}") from exc
        local = datetime(d.year, d.month, d.day).astimezone()
        if end:
            local = local + timedelta(days=1) - timedelta(microseconds=1)
        return local.astimezone(UTC), d
    seconds = parse_duration(text)  # raises ConfigError on bad input
    bound = datetime.now(tz=UTC) - timedelta(seconds=seconds)
    return bound, bound.astimezone().date()


def _apply_filters(
    rows: list[dict],
    *,
    terms: list[str],
    since: str | None,
    until: str | None,
    uploaded: bool,
) -> list[dict]:
    if terms:
        lowered = [t.lower() for t in terms]
        rows = [
            r
            for r in rows
            if all(
                t in (r["title"] or "").lower() or t in (r["uploader"] or "").lower()
                for t in lowered
            )
        ]

    if not since and not until:
        return rows

    since_bound = _parse_bound(since) if since else None
    until_bound = _parse_bound(until, end=True) if until else None

    filtered: list[dict] = []
    for r in rows:
        if uploaded:
            if not r["upload_date"]:
                continue
            try:
                d = date.fromisoformat(r["upload_date"])
            except ValueError:
                continue
            if since_bound and d < since_bound[1]:
                continue
            if until_bound and d > until_bound[1]:
                continue
        else:
            if not r["downloaded_at"]:
                continue
            dt = ui.local_dt(r["downloaded_at"])
            if since_bound and dt < since_bound[0].astimezone():
                continue
            if until_bound and dt > until_bound[0].astimezone():
                continue
        filtered.append(r)
    return filtered


def _sort_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort == "title":
        return sorted(rows, key=lambda r: ((r["title"] or "").casefold(), r["video_id"] or ""))
    if sort == "size":
        key = "size_bytes"
    elif sort == "uploaded":
        key = "upload_date"
    else:  # downloaded (default)
        key = "downloaded_at"
    return sorted(
        rows,
        key=lambda r: (r[key] is not None, r[key] if r[key] is not None else ""),
        reverse=True,
    )


def query_videos(
    db_path: Path,
    source_names: list[str],
    *,
    terms: list[str] = (),
    since: str | None = None,
    until: str | None = None,
    uploaded: bool = False,
    sort: str = "downloaded",
) -> list[dict]:
    """Load indexed videos, apply search filters, and sort the result."""
    rows = _load_rows(db_path, source_names)
    rows = _apply_filters(
        rows,
        terms=terms,
        since=since,
        until=until,
        uploaded=uploaded,
    )
    return _sort_rows(rows, sort)


def _select_sources(config, names: list[str] | None):
    """Resolve ``--source`` names to Source objects (all when None/empty)."""
    if not names:
        return list(config.sources)
    selected = []
    for name in names:
        source = next((s for s in config.sources if s.name == name), None)
        if source is None:
            known = ", ".join(s.name for s in config.sources)
            print(ui.red(f"{ui.FAIL} unknown source {name!r}" + (f"; known: {known}" if known else "")))
            return None
        selected.append(source)
    return selected


# ---------------------------------------------------------------------------
# kura search
# ---------------------------------------------------------------------------


def search_command(args) -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    selected = _select_sources(config, getattr(args, "sources", None))
    if selected is None:
        return 1

    db_path = paths.state_db()
    for source in selected:
        refresh_index(source, full=bool(getattr(args, "reindex", False)), db_path=db_path)

    source_names = [s.name for s in selected]
    total_indexed = len(_load_rows(db_path, source_names))

    try:
        rows = query_videos(
            db_path,
            source_names,
            terms=list(getattr(args, "query", []) or []),
            since=getattr(args, "since", None),
            until=getattr(args, "until", None),
            uploaded=bool(getattr(args, "uploaded", False)),
            sort=getattr(args, "sort", "downloaded"),
        )
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    single_source = len(config.sources) == 1

    if getattr(args, "interactive", False):
        return _run_fzf(rows, single_source=single_source)

    limit = getattr(args, "n", 20)
    shown = rows if limit == 0 else rows[:limit]
    hidden = len(rows) - len(shown)

    if getattr(args, "path", False):
        for r in shown:
            print(r["dir"])
        return 0

    if getattr(args, "as_json", False):
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        return 0

    indexed_text = f"{total_indexed} indexed"
    if not rows:
        print(ui.dim(f"{indexed_text} {ui.DOT} no matches"))
        return 0

    noun = "match" if len(rows) == 1 else "matches"
    print(ui.dim(f"{indexed_text} {ui.DOT} {len(rows)} {noun}"))
    print()

    dates = [_date_col(r["downloaded_at"]) for r in shown]
    metas = [_meta_cell(r, single_source=single_source) for r in shown]
    date_width = max((len(d) for d in dates), default=0)
    meta_width = max((len(m) for m in metas), default=0)

    term_width = shutil.get_terminal_size(fallback=(120, 24)).columns
    # "  date  title   meta"
    overhead = 2 + date_width + 2 + 3 + meta_width
    title_budget = max(term_width - overhead, 16)

    titles = []
    for r in shown:
        title = r["title"] or r["video_id"] or ""
        if len(title) > title_budget:
            title = title[: title_budget - 1] + "…"
        titles.append(title)
    title_width = max((len(t) for t in titles), default=0)

    for d, title, meta in zip(dates, titles, metas):
        line = f"  {ui.dim(ui.pad(d, date_width))}  {ui.pad(title, title_width)}   {ui.dim(meta)}"
        print(line.rstrip())

    if hidden > 0:
        print(ui.dim(f"  … and {hidden} more (-n 0 for all)"))
    return 0


# ---------------------------------------------------------------------------
# fzf interactive mode
# ---------------------------------------------------------------------------


def _run_fzf(rows: list[dict], *, single_source: bool) -> int:
    if shutil.which("fzf") is None:
        print(f"{ui.red(f'{ui.FAIL} fzf not found')} {ui.dim('— install fzf for interactive mode')}")
        return 1

    argv0 = sys.argv[0] or ""
    executable = argv0 if os.path.isabs(argv0) else "kura"
    preview = f"{shlex.quote(executable)} show {{1}} {{2}}"
    cmd = [
        "fzf",
        "--delimiter", "\t",
        "--with-nth", "3,4",
        "--nth", "3",
        "--preview", preview,
        "--preview-window", "right,50%,wrap",
        "--ansi",
    ]

    def clean(value: str | None) -> str:
        return (value or "").replace("\t", " ").replace("\n", " ")

    by_key: dict[tuple[str, str], dict] = {}
    lines: list[str] = []
    for r in rows:
        meta = _meta_cell(r, single_source=single_source)
        lines.append(
            f"{r['source']}\t{r['video_id']}\t{clean(r['title']) or r['video_id']}\t{ui.dim(meta)}"
        )
        by_key[(r["source"], r["video_id"])] = r

    proc = subprocess.run(
        cmd,
        input="\n".join(lines) + ("\n" if lines else ""),
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode == 130:  # esc / ctrl-c
        return 0
    if proc.returncode != 0:
        return 1

    selection = proc.stdout.strip().splitlines()
    if not selection:
        return 0
    fields = selection[0].split("\t")
    row = by_key.get((fields[0], fields[1])) if len(fields) >= 2 else None
    if row is None:
        return 1
    print(row["dir"])
    return 0


# ---------------------------------------------------------------------------
# kura show
# ---------------------------------------------------------------------------


def show_command(args) -> int:
    try:
        config = load_config()
    except ConfigError as error:
        print(ui.red(f"{ui.FAIL} {error}"))
        return 1

    source_name: str | None = args.source_or_id
    video_id: str | None = getattr(args, "video_id", None)
    if video_id is None:
        # Bare-id form: kura show VIDEO_ID
        video_id = source_name
        source_name = None

    if source_name is not None:
        source = next((s for s in config.sources if s.name == source_name), None)
        if source is None:
            known = ", ".join(s.name for s in config.sources)
            print(ui.red(f"{ui.FAIL} unknown source {source_name!r}" + (f"; known: {known}" if known else "")))
            return 1
        sources = [source]
    else:
        sources = list(config.sources)

    db_path = paths.state_db()
    for source in sources:
        refresh_index(source, db_path=db_path)

    rows = [
        r
        for r in _load_rows(db_path, [s.name for s in sources])
        if r["video_id"] == video_id
    ]

    if not rows:
        where = f" in source {source_name!r}" if source_name else ""
        print(ui.red(f"{ui.FAIL} no indexed video with id {video_id!r}{where}"))
        return 1

    if len(rows) > 1:
        print(ui.red(f"{ui.FAIL} ambiguous video id {video_id!r} — candidates:"))
        for r in sorted(rows, key=lambda r: r["source"]):
            print(f"  {r['source']} {r['video_id']}  {ui.dim(r['title'] or '')}".rstrip())
        print(ui.dim("  disambiguate with `kura show SOURCE VIDEO_ID`"))
        return 1

    _print_show(rows[0])
    return 0


def _file_ext(path: Path) -> str:
    """Display extension for a file, keeping known multi-part suffixes."""
    name = path.name
    for special in (".info.json", ".comments.json"):
        if name.endswith(special):
            return special
    suffixes = path.suffixes
    if (
        len(suffixes) >= 2
        and suffixes[-1] in {".vtt", ".srt", ".ass", ".lrc"}
        and re.fullmatch(r"\.[A-Za-z-]{2,10}", suffixes[-2])
    ):
        return suffixes[-2] + suffixes[-1]
    return path.suffix


def _print_show(row: dict) -> None:
    label_width = 8  # len("uploader")

    def label(text: str) -> str:
        return ui.dim(f"{text:<{label_width}}")

    print(ui.bold(row["title"] or row["video_id"] or ""))

    extractor = (row["archive_key"] or "").split(" ", 1)[0]
    id_meta = f" {ui.DOT} ".join(p for p in (extractor, row["source"]) if p)
    suffix = f" {ui.dim(f'{ui.DOT} {id_meta}')}" if id_meta else ""
    print(f"  {label('id')}  {row['video_id']}{suffix}")

    if row["uploader"]:
        print(f"  {label('uploader')}  {row['uploader']}")

    date_parts = []
    if row["upload_date"]:
        date_parts.append(f"{label('uploaded')}  {row['upload_date']:<10}")
    if row["downloaded_at"]:
        date_parts.append(f"{ui.dim('downloaded')}  {ui.local_ts(row['downloaded_at'])}")
    if date_parts:
        print("  " + "    ".join(date_parts))

    length_parts = []
    if row["duration_s"] is not None:
        length_parts.append(fmt_duration(row["duration_s"], precise=True))
    if row["size_bytes"] is not None:
        length_parts.append(fmt_size(row["size_bytes"]))
    if length_parts:
        print(f"  {label('length')}  {f' {ui.dim(ui.DOT)} '.join(length_parts)}")

    directory = Path(row["dir"])
    print(f"  {label('path')}  {row['dir']}{os.sep if not row['dir'].endswith(os.sep) else ''}")

    if directory.is_dir():
        extensions: list[str] = []
        for child in sorted(directory.iterdir()):
            if not child.is_file():
                continue
            ext = _file_ext(child)
            if ext and ext not in extensions:
                extensions.append(ext)
        media_first = sorted(
            extensions,
            key=lambda e: (e.lstrip(".").lower() not in MEDIA_EXTENSIONS, e),
        )
        if media_first:
            print(f"  {label('files')}  {ui.dim(f' {ui.DOT} '.join(media_first))}")
