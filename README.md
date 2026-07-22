# kura

kura is a small yt-dlp archive orchestrator for existing, manually managed channel and playlist archives. It keeps `config.yaml` as the source of truth, uses raw yt-dlp profile files, and stores only runtime state such as run history and caches in SQLite.

## Commands

```sh
kura --version
kura init
kura add NAME URL --dir /absolute/archive/dir [--profile PROFILE] [--schedule "0 */6 * * *"] [--strategy full|quick|rss] [--cookies "ARGS"]
kura list
kura import NAME [--dry-run]
kura run NAME [--force]
kura run --due
kura run --all
kura verify NAME
kura status [NAME]
kura watch [NAME] [--once] [--interval SEC]
kura pause NAME
kura resume NAME
kura suppress NAME VIDEO_ID [--reason TEXT] [--extractor EXT]
kura unsuppress NAME VIDEO_ID [--extractor EXT]
kura logs NAME [-n N] [--path]
kura search [QUERY...] [--source NAME] [--since X] [--until X] [--uploaded] [--sort downloaded|uploaded|title|size] [-n N] [--path] [--json] [-i] [--reindex]
kura show [SOURCE] VIDEO_ID
kura doctor
kura ui
```

`kura import NAME` scans the source's output directory for `*.info.json` sidecar files, computes the yt-dlp archive key for each video, and appends any missing keys to `archive.txt` (backing it up first).  Pass `--dry-run` to preview the report without writing anything.

`kura run NAME` invokes yt-dlp for the named source using its configured profile, streams output to the terminal and a timestamped log file, then records the run in the state database (exit code, new item count, yt-dlp version).  Pass `--force` to run a source even if it is disabled.

`kura run --due` checks every enabled source in config order and runs those whose cron schedule has elapsed since the last successful check (sources that have never run are always due).  This is the command used by the systemd timer.

`kura run --all` runs every enabled source regardless of schedule.

`kura verify NAME` performs a dry-run (`--simulate --skip-download`) to report how many items yt-dlp would download on the next real run without writing anything.

`kura status` prints a summary table for all configured sources showing enabled state, last-run timestamp, exit code, new item count, permanent failure count, and estimated next scheduled run.  When any source has >= 3 permanent failures a hint to use `kura suppress` is printed.

`kura status NAME` prints a detailed view for one source: last 5 runs (time, exit, new, failure summary), permanent failures list (video_id, attempts, last error), and active suppressions.

## Watching a live run

`kura watch [NAME]` shows in-place progress for running sources, including download percentage, speed, ETA, and the current video. It attaches to any kura run detected through its source lock and live log, including runs started manually or by the systemd timer. Without `NAME`, it watches all enabled sources.

Use `--once` to print one script-friendly snapshot and exit. Use `--interval SEC` to change the live polling interval from its 0.5-second default.

`kura pause NAME` / `kura resume NAME` flip the `enabled` flag in `config.yaml`, preserving all comments and formatting.  A paused source is skipped by `run --due` and `run --all`.

`kura logs NAME` prints the last 50 lines of the most recent run log for the named source.  Use `-n N` to override the line count.  Use `--path` to print only the absolute path of the log file (useful for piping into a pager: `less "$(kura logs NAME --path)"`).

`kura doctor` runs a series of health checks and reports each as `ok:`, `warn:`, or `FAIL:`.  Exits 0 when no `FAIL` lines were emitted (warnings are allowed), exits 1 otherwise.  Checks include: yt-dlp on PATH and version freshness (warns if > 90 days old), ffmpeg on PATH, config.yaml validity, per-source output_dir and archive accessibility, cookies file existence, state.db integrity, currently-running sources (lock probe), and systemd timer status.

## Searching the library

`kura search` queries an index of every downloaded video, built from the metadata sidecars on disk (`*.info.json` for yt-dlp, `*.json` for nndownload) and cached in the state database.  The index refreshes incrementally on every invocation — only sidecars written since the last scan are re-parsed — so searching stays fast on large archives.  Disk remains the source of truth: the cache is rebuildable at any time.

```sh
# free-text terms AND-match against title + uploader, case-insensitive
kura search live 2024

# everything downloaded in the last 7 days (also accepts YYYY-MM-DD, or 12h)
kura search --since 7d

# one source only, biggest files first
kura search --source test-channel --sort size

# pipe result directories straight into a player
kura search --since 24h --path | xargs -d '\n' mpv

# interactive picker (requires fzf); prints the selected video's directory
kura search -i
```

`--uploaded` switches `--since`/`--until` to match the video's upload date instead of the download time.  `--sort` accepts `downloaded` (default, newest first), `uploaded`, `title`, and `size`.  `-n N` limits the result count (default 20, `0` shows all).  `--json` emits the matching rows as a JSON array for scripting.  `--reindex` rebuilds the index for the searched sources from scratch instead of scanning incrementally.

`kura show SOURCE VIDEO_ID` (or just `kura show VIDEO_ID` when the id is unambiguous across sources) prints the indexed details for one video: title, uploader, upload/download dates, duration, file size, directory path, and the file extensions present in the video's folder.

Known behavior: rows for videos deleted from disk remain in the index until a `kura search --reindex` rebuilds it — the incremental scan only ever adds and updates rows.

## Interactive UI

`kura ui` opens a terminal interface over the same config, state, and library index used by the CLI. It starts in the Library browser, where search terms use the same case-insensitive AND matching as `kura search`; press `tab` to open the Sources dashboard and inspect or control configured sources.

Library keys:

| Key | Action |
|-----|--------|
| `/` / `escape` | Focus search / return to results |
| `enter` | Play the selected video |
| `o` / `y` | Open its folder / copy its path |
| `s` / `f` | Cycle sort order / source filter (`f` appears with multiple sources) |
| `R` | Fully rebuild the library index |
| `tab` / `q` | Open Sources / quit |

Sources keys:

| Key | Action |
|-----|--------|
| `enter` | Open the selected source in Library |
| `r` | Run the selected source now |
| `l` | Show or hide run output |
| `p` | Pause or resume the selected source |
| `tab` / `q` | Return to Library / quit |

## Update Strategies

Each source has an `update_strategy` that controls how `kura run` invokes yt-dlp:

| Strategy | URL requirement | Behaviour |
|----------|-----------------|-----------|
| `full` | any URL | Plain yt-dlp invocation with the source URL. Always valid. |
| `quick` | YouTube channel URL only | Derives three tab URLs (`/videos`, `/shorts`, `/streams`) and runs a single yt-dlp invocation with `--lazy-playlist --break-on-existing --break-per-input`. |
| `rss` | YouTube channel URL only | Fetches the channel RSS feed; if all items are already archived, records a no-op run without invoking yt-dlp. Otherwise falls through to `quick`. On feed errors, logs a warning and falls through to `quick`. |

Set the strategy in `config.yaml` or with `kura add --strategy STRATEGY`.

### Concurrency

`kura run --due` and `kura run --all` can run multiple sources in parallel.  Set `concurrency` in the `defaults` section of `config.yaml` (integer 1–8; default 1):

```yaml
defaults:
  concurrency: 4   # run up to 4 sources simultaneously
```

When `concurrency > 1` and more than one source is running, per-line console streaming is suppressed (each source's full output still goes to its log file) and only a start line and summary line are printed per source.  Single-source `kura run NAME` always streams output regardless of the concurrency setting.

### full_reindex_every

For `quick` and `rss` sources, kura periodically promotes a run to the `full` strategy to catch items that tab-based or RSS-based runs might miss.  The interval defaults to `7d` and can be overridden per source:

```yaml
defaults:
  full_reindex_every: 7d   # Nd (days) or Nh (hours)

sources:
  - name: my-channel
    full_reindex_every: 14d  # override for this source
```

When a promotion occurs, kura prints `promoted to full reindex` and runs the full URL instead of tabs.  The `last_full_run_at` timestamp in the state database is only updated when a `full` strategy run succeeds (including promoted runs).

## Failure Handling

kura classifies yt-dlp output lines into `permanent` and `transient` failure categories:

- **Permanent**: members-only, private video, video unavailable, account terminated, age restriction, removed by uploader.
- **Transient**: HTTP 403/429, connection reset, timeouts, temporary DNS failure, fragment not found, bot-check challenges.

Per-video failure records are stored in the `video_failures` table (with attempt counts).  A `failure_summary` string (e.g. `"2 permanent, 1 transient"`) is attached to each `runs` row.

The `kura status` summary table shows a `fails` column counting permanent failures with >= 3 attempts per source.

## Notifications

kura raises a Linux desktop notification (via `notify-send`/libnotify) after each source run when something worth knowing happened:

- **Run failed** (exit code other than `0` or `101`) — a critical-urgency notification: `kura: NAME run failed` / `exit CODE — see kura logs NAME`.
- **New videos archived** — `kura: NAME` / `N new video(s) archived` (with the failure summary appended if failures were also recorded that run).
- **No new videos, but failures recorded** — `kura: NAME` / `no new videos; ...`.
- **Nothing new and nothing failed** (including an `rss` strategy's no-op check) — no notification is sent.

Notifications are best-effort: if `notify-send` is not installed, or sending fails for any reason, the run still succeeds silently. Set `notify: false` under `defaults` in `config.yaml` to disable them entirely:

```yaml
defaults:
  notify: false
```

Requires libnotify (`notify-send` on PATH); `kura doctor` reports whether it is found. Because `kura run --due` normally executes inside a systemd user service (see [Scheduling](#scheduling)), which has no graphical session bus by default, kura falls back to `unix:path=$XDG_RUNTIME_DIR/bus` for `DBUS_SESSION_BUS_ADDRESS` when it isn't already set, so notifications still reach your desktop's notification daemon.

## Suppress / Unsuppress

To permanently prevent a video from being downloaded (e.g. it has been removed and will never be available):

```sh
# Add the video to the archive and record the suppression
kura suppress my-channel VIDEO_ID [--reason "gone forever"] [--extractor youtube]

# Remove the suppression and delete the archive entry
kura unsuppress my-channel VIDEO_ID [--extractor youtube]
```

`suppress` appends `EXTRACTOR VIDEO_ID` to `archive.txt` (with a backup), inserts a row in the `suppressions` table, and removes any `video_failures` record for that video.  It will warn (but not fail) if the entry is already in the archive.

`unsuppress` requires a matching `suppressions` row — kura will only remove entries it added.  It rewrites `archive.txt` atomically (with a backup) removing exactly that line, then deletes the suppression record.

## Locking

kura uses per-source advisory file locks (`<state_dir>/locks/<name>.lock`) so that concurrent executions of the same source are safe.  If a lock is already held:

- `kura run NAME` prints a skip message and exits 0 (an overlapping timer tick is normal).
- `kura import NAME` prints a skip message and exits 1 (a run/import collision warrants user attention).

## Niconico (nndownload)

kura can archive Niconico playlists, user channels, and series using [nndownload](https://github.com/niconico/nndownload) as the downloader backend.

### Prerequisites

Install the patched nndownload build (the patch adds `--download-archive` support required by kura):

```sh
uv tool install --force --from git+https://github.com/shovel-kun/nndownload nndownload
```

Verify with `kura doctor` — it checks that `--download-archive` appears in `nndownload --help`.

### Adding a Niconico source

```sh
kura add nico-mylist https://www.nicovideo.jp/mylist/12345678 \
  --dir /archive/niconico \
  --downloader nndownload \
  --auth "--session-cookie ~/.config/kura/nicovideo_session.txt" \
  --profile niconico-archive \
  --strategy full
```

The `--auth` value is passed verbatim to nndownload before its URL argument.  Common auth options:

| Option | When to use |
|--------|-------------|
| `--session-cookie /path/to/file` | Production use: paste the `user_session` cookie value into a file (`chmod 600`) |
| `-g` | Interactive login prompt (not suitable for automated runs) |
| `-n` | No login (public content only) |

### Session-cookie refresh

When `kura status` shows a cluster of transient auth failures (e.g. "authentication failed", "session expired"), the session cookie has expired.  To refresh:

1. Log into nicovideo.jp in your browser.
2. Use a cookie inspector (e.g. browser DevTools → Application → Cookies) to copy the value of `user_session`.
3. Paste the raw cookie value into the session-cookie file (overwrite the entire file — no key name, just the value).
4. Nothing else needs to change; the next `kura run` will pick it up automatically.

### Strategies

nndownload sources support `full` and `quick` strategies only (`rss` is not available):

| Strategy | Behaviour |
|----------|-----------|
| `full` | Downloads all items in the playlist/channel that are not already archived. Safe for all URL types. |
| `quick` | Passes `--break-on-existing` to nndownload — stops at the first already-archived item, so it only processes recent additions. Efficient for large lists where new content is always at the top. |

Use `full_reindex_every` to periodically promote `quick` runs to `full` (default `7d`), catching any gaps.

### Comments

When `--download-comments` is present in the `.nnconf` profile, nndownload writes a `{stem}.comments.json` alongside each video.  kura treats these as opaque data and never parses them as archive evidence.

### Importing an existing nndownload directory

If you have already downloaded videos with nndownload before adding the source to kura, run:

```sh
kura import nico-mylist
```

kura scans the output directory for nndownload metadata sidecars (`*.json`, excluding `*.info.json` and `*.comments.json`), extracts the video ID from each, and appends any missing `niconico <id>` entries to `archive.txt`.  yt-dlp `.info.json` sidecars in the same tree are also recognised, so mixed directories import correctly.  Pass `--dry-run` to preview without writing.

### suppress for Niconico sources

`kura suppress` automatically uses `niconico` as the extractor default when the source uses nndownload, so you do not need to pass `--extractor` manually:

```sh
kura suppress nico-mylist sm12345
# equivalent to:
kura suppress nico-mylist sm12345 --extractor niconico
```

## Scheduling

Use the shipped systemd user units to run `kura run --due` every 15 minutes:

```sh
# Copy unit files
mkdir -p ~/.config/systemd/user
cp contrib/systemd/kura.service ~/.config/systemd/user/
cp contrib/systemd/kura.timer ~/.config/systemd/user/

# Enable and start
systemctl --user enable --now kura.timer

# Allow the timer to survive logout
loginctl enable-linger $USER
```

The timer fires every 15 minutes (`OnCalendar=*:0/15`); `Persistent=true` means a missed tick (e.g. after suspend) is caught on the next boot.  kura then checks each source's cron schedule independently, so sources with e.g. `0 */6 * * *` only actually run every 6 hours.
