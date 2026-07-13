"""Interactive Textual interface for kura."""


def run_ui() -> int:
    from .app import KuraApp

    KuraApp().run()
    return 0
