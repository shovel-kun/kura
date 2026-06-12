# kura

kura is a small yt-dlp archive orchestrator for existing, manually managed channel and playlist archives. It keeps `config.yaml` as the source of truth, uses raw yt-dlp profile files, and stores only runtime state such as run history and caches in SQLite.

## Commands

```sh
kura init
kura add NAME URL --dir /absolute/archive/dir [--profile PROFILE] [--schedule "0 */6 * * *"] [--strategy full|quick|rss] [--cookies "ARGS"]
kura list
kura import NAME [--dry-run]
kura run NAME [--force]
kura run --due
kura run --all
kura verify NAME
kura status [NAME]
kura pause NAME
kura resume NAME
kura suppress NAME VIDEO_ID [--reason TEXT] [--extractor EXT]
kura unsuppress NAME VIDEO_ID [--extractor EXT]
```

`kura import NAME` scans the source's output directory for `*.info.json` sidecar files, computes the yt-dlp archive key for each video, and appends any missing keys to `archive.txt` (backing it up first).  Pass `--dry-run` to preview the report without writing anything.

`kura run NAME` invokes yt-dlp for the named source using its configured profile, streams output to the terminal and a timestamped log file, then records the run in the state database (exit code, new item count, yt-dlp version).  Pass `--force` to run a source even if it is disabled.

`kura run --due` checks every enabled source in config order and runs those whose cron schedule has elapsed since the last successful check (sources that have never run are always due).  This is the command used by the systemd timer.

`kura run --all` runs every enabled source regardless of schedule.

`kura verify NAME` performs a dry-run (`--simulate --skip-download`) to report how many items yt-dlp would download on the next real run without writing anything.

`kura status` prints a summary table for all configured sources showing enabled state, last-run timestamp, exit code, new item count, permanent failure count, and estimated next scheduled run.  When any source has >= 3 permanent failures a hint to use `kura suppress` is printed.

`kura status NAME` prints a detailed view for one source: last 5 runs (time, exit, new, failure summary), permanent failures list (video_id, attempts, last error), and active suppressions.

`kura pause NAME` / `kura resume NAME` flip the `enabled` flag in `config.yaml`, preserving all comments and formatting.  A paused source is skipped by `run --due` and `run --all`.

## Update Strategies

Each source has an `update_strategy` that controls how `kura run` invokes yt-dlp:

| Strategy | URL requirement | Behaviour |
|----------|-----------------|-----------|
| `full` | any URL | Plain yt-dlp invocation with the source URL. Always valid. |
| `quick` | YouTube channel URL only | Derives three tab URLs (`/videos`, `/shorts`, `/streams`) and runs a single yt-dlp invocation with `--lazy-playlist --break-on-existing --break-per-input`. |
| `rss` | YouTube channel URL only | Fetches the channel RSS feed; if all items are already archived, records a no-op run without invoking yt-dlp. Otherwise falls through to `quick`. On feed errors, logs a warning and falls through to `quick`. |

Set the strategy in `config.yaml` or with `kura add --strategy STRATEGY`.

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

`logs` and `doctor` are registered as stubs and exit with status 2 until a later release.
