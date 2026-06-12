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
kura status
kura pause NAME
kura resume NAME
```

`kura import NAME` scans the source's output directory for `*.info.json` sidecar files, computes the yt-dlp archive key for each video, and appends any missing keys to `archive.txt` (backing it up first).  Pass `--dry-run` to preview the report without writing anything.

`kura run NAME` invokes yt-dlp for the named source using its configured profile, streams output to the terminal and a timestamped log file, then records the run in the state database (exit code, new item count, yt-dlp version).  Pass `--force` to run a source even if it is disabled.

`kura run --due` checks every enabled source in config order and runs those whose cron schedule has elapsed since the last successful check (sources that have never run are always due).  This is the command used by the systemd timer.

`kura run --all` runs every enabled source regardless of schedule.

`kura verify NAME` performs a dry-run (`--simulate --skip-download`) to report how many items yt-dlp would download on the next real run without writing anything.

`kura status` prints a summary table for all configured sources showing enabled state, last-run timestamp, exit code, new item count, and estimated next scheduled run.

`kura pause NAME` / `kura resume NAME` flip the `enabled` flag in `config.yaml`, preserving all comments and formatting.  A paused source is skipped by `run --due` and `run --all`.

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

Other planned commands are registered as stubs and exit with status 2 until a later release.
