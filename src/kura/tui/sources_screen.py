from __future__ import annotations

import shutil
import subprocess
import sys

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, RichLog, Static

from kura import commands, state, ui
from kura.config import load_config, set_source_enabled


class SourcesScreen(Screen):
    BINDINGS = [
        Binding("enter", "library", "Library", priority=True),
        Binding("r", "run_now", "Run now"),
        Binding("l", "toggle_log", "Log"),
        Binding("p", "toggle_paused", "Pause/resume"),
        Binding("tab", "library_screen", "Library"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.sources = []
        self.in_flight: set[str] = set()
        self._log_visible = False

    def compose(self) -> ComposeResult:
        with Vertical():
            yield DataTable(id="sources-table", cursor_type="row", zebra_stripes=True)
            with Container(id="run-log-panel"):
                yield Static("run output", id="run-log-title")
                yield RichLog(id="run-log", highlight=True, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sources-table", DataTable)
        table.add_column("", width=3, key="state")
        table.add_column("source", key="source")
        table.add_column("last run", key="last")
        table.add_column("next due", key="next")
        table.add_column("fails", width=7, key="fails")
        table.focus()
        self.refresh_rows()

    def refresh_rows(self) -> None:
        self.app.config = load_config()
        self.sources = list(self.app.config.sources)
        db = state.connect(self.app.db_path) if self.app.db_path.exists() else None
        try:
            table = self.query_one("#sources-table", DataTable)
            cursor = min(table.cursor_row, max(len(self.sources) - 1, 0))
            table.clear()
            for index, source in enumerate(self.sources):
                source_state = commands.query_source_state(db, source.name)
                last_run = commands.query_last_run(db, source.name)
                failures = commands.query_permanent_fails(db, source.name)

                glyph = Text(ui.ON, style="green") if source.enabled else Text(ui.OFF, style="dim")
                if last_run:
                    timestamp = ui.local_ts(last_run["ended_at"], "%b %d %H:%M")
                    if last_run["exit_code"] in (0, 101):
                        outcome = f"{ui.OK} +{last_run['new_count']}" if last_run["new_count"] else ui.OK
                    else:
                        outcome = f"{ui.FAIL} exit {last_run['exit_code']}"
                    last_text = f"{timestamp} {outcome}"
                else:
                    last_text = "—"

                if not source.enabled:
                    next_due = Text("paused", style="dim")
                elif source_state.get("last_check_at"):
                    due = commands.compute_next_due(source.schedule, source_state["last_check_at"])
                    next_due = Text(ui.rel_time(due))
                else:
                    next_due = Text("now")
                fail_text = Text(str(failures) if failures else "—", style="red" if failures else "dim")
                table.add_row(glyph, source.name, last_text, next_due, fail_text, key=str(index))
            if self.sources:
                table.move_cursor(row=cursor)
        finally:
            if db is not None:
                db.close()

    def _selected_source(self):
        table = self.query_one("#sources-table", DataTable)
        if not self.sources or not 0 <= table.cursor_row < len(self.sources):
            return None
        return self.sources[table.cursor_row]

    def _show_log(self) -> None:
        self._log_visible = True
        self.query_one("#run-log-panel", Container).add_class("visible")

    def action_library(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        library_screen = self.app.get_screen("library")
        library_screen.set_source_filter(source.name)
        self.app.switch_screen("library")

    def action_library_screen(self) -> None:
        self.app.switch_screen("library")

    def action_toggle_log(self) -> None:
        self._log_visible = not self._log_visible
        self.query_one("#run-log-panel", Container).set_class(self._log_visible, "visible")

    def action_toggle_paused(self) -> None:
        source = self._selected_source()
        if source is None:
            return
        set_source_enabled(source.name, enabled=not source.enabled)
        self.refresh_rows()

    def action_run_now(self) -> None:
        source = self._selected_source()
        if source is None or source.name in self.in_flight:
            return
        self.in_flight.add(source.name)
        self._show_log()
        self.query_one("#run-log-title", Static).update(f"running {source.name}")
        self.query_one("#run-log", RichLog).write(f"$ kura run {source.name}")
        self.run_worker(
            lambda: self._run_source(source.name),
            thread=True,
            exit_on_error=False,
        )

    def _run_source(self, name: str) -> None:
        executable = shutil.which("kura")
        argv = [executable, "run", name] if executable else [sys.executable, "-m", "kura.cli", "run", name]
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            self.app.call_from_thread(self.query_one("#run-log", RichLog).write, line.rstrip())
        return_code = process.wait()
        self.app.call_from_thread(self._run_finished, name, return_code)

    def _run_finished(self, name: str, return_code: int) -> None:
        self.in_flight.discard(name)
        self.query_one("#run-log-title", Static).update(f"last run: {name}")
        if return_code in (0, 101):
            self.app.notify(f"{name} run complete")
        else:
            self.app.notify(f"{name} failed (exit {return_code})", severity="error")
        self.refresh_rows()

    def action_quit(self) -> None:
        self.app.exit()
