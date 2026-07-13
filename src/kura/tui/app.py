from __future__ import annotations

from textual.app import App

from kura import paths, state
from kura.config import load_config

from .library_screen import LibraryScreen
from .sources_screen import SourcesScreen


class KuraApp(App):
    TITLE = "kura"
    CSS = """
    Screen {
        background: #101318;
        color: #d8dee9;
    }

    Input {
        border: tall #303846;
        height: 3;
        padding: 0 1;
    }

    Input:focus {
        border: tall #6aa9ff;
    }

    #status {
        height: 1;
        padding: 0 2;
    }

    #library-body {
        height: 1fr;
    }

    #library-table {
        width: 3fr;
    }

    #detail {
        width: 2fr;
        padding: 1 2;
        background: #151a22;
        overflow-y: auto;
    }

    DataTable {
        background: #101318;
        scrollbar-color: #39475a;
        scrollbar-background: #101318;
    }

    DataTable > .datatable--header {
        background: #151a22;
        color: #8994a7;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: #214b73;
        color: #ffffff;
    }

    Footer {
        background: #151a22;
        color: #8994a7;
    }

    #run-log-panel {
        height: 0;
        background: #090b0f;
        overflow: hidden;
        transition: height 150ms;
    }

    #run-log-panel.visible {
        height: 12;
    }

    #run-log-title {
        height: 1;
        padding: 0 1;
        color: #8994a7;
    }

    #run-log {
        height: 1fr;
        padding: 0 1;
        scrollbar-color: #39475a;
        scrollbar-background: #090b0f;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.db_path = paths.state_db()
        state.init_db(self.db_path)

    def on_mount(self) -> None:
        self.theme = "textual-dark"
        self.install_screen(LibraryScreen(), "library")
        self.install_screen(SourcesScreen(), "sources")
        self.push_screen("library")
