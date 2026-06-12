# kura

kura is a small yt-dlp archive orchestrator for existing, manually managed channel and playlist archives. It keeps `config.yaml` as the source of truth, uses raw yt-dlp profile files, and stores only runtime state such as run history and caches in SQLite.

Commands implemented so far:

```sh
kura init
kura add NAME URL --dir /absolute/archive/dir [--profile PROFILE] [--schedule "0 */6 * * *"] [--strategy full|quick|rss] [--cookies "ARGS"]
kura list
kura import NAME [--dry-run]
kura run NAME
kura verify NAME
kura status
```

`kura import NAME` scans the source's output directory for `*.info.json` sidecar files, computes the yt-dlp archive key for each video, and appends any missing keys to `archive.txt` (backing it up first).  Pass `--dry-run` to preview the report without writing anything.

`kura run NAME` invokes yt-dlp for the named source using its configured profile, streams output to the terminal and a timestamped log file, then records the run in the state database (exit code, new item count, yt-dlp version).

`kura verify NAME` performs a dry-run (`--simulate --skip-download`) to report how many items yt-dlp would download on the next real run without writing anything.

`kura status` prints a summary table for all configured sources showing enabled state, last-run timestamp, exit code, new item count, and estimated next scheduled run.

Other planned commands are registered as stubs and exit with status 2 until a later release.
