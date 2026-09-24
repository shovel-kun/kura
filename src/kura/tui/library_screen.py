from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Input, Static

from kura import library, ui
from kura.archive import MEDIA_EXTENSIONS

from . import launchers


def _truncate_cells(text: str, max_cells: int) -> str:
    """Truncate *text* to *max_cells* terminal cells (CJK chars are 2 cells wide)."""
    if cell_len(text) <= max_cells:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        w = cell_len(ch)
        if used + w > max_cells - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


class LibraryScreen(Screen):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("/", "focus_search", "Search"),
        Binding("escape", "focus_table", "Results", show=False),
        Binding("enter", "play", "Play", priority=True),
        Binding("o", "open_folder", "Open folder"),
        Binding("y", "copy_path", "Copy path"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("R", "full_reindex", "Reindex"),
        Binding("tab", "sources", "Sources"),
        Binding("q", "quit", "Quit"),
    ]

    SORTS = ("downloaded", "uploaded", "title", "size")

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self.sort = "downloaded"
        self.source_filter: str | None = None
        self._search_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        yield Input(
            placeholder="search titles and uploaders…",
            id="search",
        )
        yield Static(id="status")
        with Horizontal(id="library-body"):
            yield DataTable(id="library-table", cursor_type="row", zebra_stripes=True)
            yield Static("No video selected", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#library-table", DataTable)
        table.focus()
        if len(self.app.config.sources) > 1:
            self._bindings.bind("f", "cycle_source", description="Source")
            self.refresh_bindings()
        self.refresh_rows()
        self.run_worker(self._refresh_indexes, thread=True, exit_on_error=False)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "search":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(0.15, self.refresh_rows)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if 0 <= event.cursor_row < len(self.rows):
            self._show_detail(self.rows[event.cursor_row])

    def refresh_rows(self) -> None:
        source_names = (
            [self.source_filter]
            if self.source_filter is not None
            else [source.name for source in self.app.config.sources]
        )
        terms = self.query_one("#search", Input).value.split()
        self.rows = library.query_videos(
            self.app.db_path,
            source_names,
            terms=terms,
            sort=self.sort,
        )

        table = self.query_one("#library-table", DataTable)
        single_source = len(self.app.config.sources) == 1

        dates = [library._date_col(row["downloaded_at"]) for row in self.rows]
        metas = [library._meta_cell(row, single_source=single_source) for row in self.rows]

        # Size columns to the actual table width; the title column takes
        # whatever remains, truncated by terminal cells (CJK-aware).
        date_w = max([cell_len(d) for d in dates] + [6])
        meta_w = min(max([cell_len(m) for m in metas] + [10]), 26)
        avail = table.size.width or int(self.app.size.width * 0.6)
        # 2 cells of cell-padding per column + room for the vertical scrollbar.
        title_w = max(avail - date_w - meta_w - 3 * 2 - 2, 16)

        table.clear(columns=True)
        table.add_column("date", width=date_w, key="date")
        table.add_column("title", width=title_w, key="title")
        table.add_column("meta", width=meta_w, key="meta")
        for index, row in enumerate(self.rows):
            title = row["title"] or row["video_id"] or ""
            table.add_row(
                Text(dates[index], style="dim"),
                _truncate_cells(title, title_w),
                Text(metas[index], style="dim"),
                key=str(index),
            )
        if self.rows:
            table.move_cursor(row=0)
            self._show_detail(self.rows[0])
        else:
            self.query_one("#detail", Static).update("No matching videos")

        terms = self.query_one("#search", Input).value.split()
        parts = [
            f"{len(self.rows)} {'match' if terms else 'video'}{'' if len(self.rows) == 1 else 'es' if terms else 's'}",
            f"sort {self.sort}",
            self.source_filter or "all sources",
        ]
        self.query_one("#status", Static).update(Text(f" {ui.DOT} ".join(parts), style="dim"))

    def on_resize(self, _event) -> None:
        if self.is_mounted and self.rows is not None:
            self.refresh_rows()

    def set_source_filter(self, name: str) -> None:
        self.source_filter = name
        if self.is_mounted:
            self.refresh_rows()

    def _selected_row(self) -> dict | None:
        table = self.query_one("#library-table", DataTable)
        if not self.rows or not 0 <= table.cursor_row < len(self.rows):
            return None
        return self.rows[table.cursor_row]

    def _show_detail(self, row: dict) -> None:
        detail = Text()
        detail.append(row["title"] or row["video_id"] or "", style="bold")
        detail.append("\n\n")

        def add(label: str, value: str | None) -> None:
            if value:
                detail.append(f"{label:<11}", style="dim")
                detail.append(value)
                detail.append("\n")

        add("uploader", row["uploader"])
        add("uploaded", row["upload_date"])
        downloaded = ui.rel_time(ui.local_dt(row["downloaded_at"])) if row["downloaded_at"] else None
        add("downloaded", downloaded)

        length = []
        if row["duration_s"] is not None:
            length.append(library.fmt_duration(row["duration_s"], precise=True))
        if row["size_bytes"] is not None:
            length.append(library.fmt_size(row["size_bytes"]))
        add("length", f" {ui.DOT} ".join(length))

        directory = Path(row["dir"])
        path = row["dir"] + ("" if row["dir"].endswith(os.sep) else os.sep)
        add("path", path)
        extensions: list[str] = []
        if directory.is_dir():
            for child in sorted(directory.iterdir()):
                if child.is_file():
                    extension = library._file_ext(child)
                    if extension and extension not in extensions:
                        extensions.append(extension)
        extensions.sort(key=lambda ext: (ext.lstrip(".").lower() not in MEDIA_EXTENSIONS, ext))
        add("files", f" {ui.DOT} ".join(extensions))
        add("source", row["source"])
        self.query_one("#detail", Static).update(detail)

    def _refresh_indexes(self, full: bool = False) -> None:
        for source in self.app.config.sources:
            library.refresh_index(source, full=full, db_path=self.app.db_path)
        self.app.call_from_thread(self._index_finished, full)

    def _index_finished(self, full: bool) -> None:
        self.refresh_rows()
        if full:
            self.app.notify("Library reindex complete")

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_focus_table(self) -> None:
        self.query_one("#library-table", DataTable).focus()

    def action_play(self) -> None:
        if not self.query_one("#library-table", DataTable).has_focus:
            return
        row = self._selected_row()
        if row:
            launchers.play(Path(row["dir"]))

    def action_open_folder(self) -> None:
        row = self._selected_row()
        if row:
            launchers.open_folder(Path(row["dir"]))

    def action_copy_path(self) -> None:
        row = self._selected_row()
        if row:
            copied = launchers.copy_path(Path(row["dir"]))
            self.app.notify("Path copied" if copied else "No clipboard tool found", severity="information" if copied else "warning")

    def action_cycle_sort(self) -> None:
        index = (self.SORTS.index(self.sort) + 1) % len(self.SORTS)
        self.sort = self.SORTS[index]
        self.refresh_rows()

    def action_cycle_source(self) -> None:
        names = [source.name for source in self.app.config.sources]
        filters: list[str | None] = [None, *names]
        index = (filters.index(self.source_filter) + 1) % len(filters)
        self.source_filter = filters[index]
        self.refresh_rows()

    def action_full_reindex(self) -> None:
        self.app.notify("Reindexing library…")
        self.run_worker(lambda: self._refresh_indexes(True), thread=True, exit_on_error=False)

    def action_sources(self) -> None:
        self.app.switch_screen("sources")

    def action_quit(self) -> None:
        self.app.exit()
