from __future__ import annotations

import os
import shutil
import subprocess


def send_notification(title: str, body: str, *, critical: bool = False) -> None:
    """Send a desktop notification via ``notify-send``, best-effort.

    Silently no-ops when ``notify-send`` is not on PATH.  Any failure —
    missing DBUS session, nonzero exit code, timeout, or any other
    exception — is swallowed.  Notifications must never fail a run.

    Parameters
    ----------
    title:
        Notification title.
    body:
        Notification body text.
    critical:
        When *True*, pass ``--urgency=critical`` so the notification is
        more likely to persist/stand out (used for run failures).
    """
    notify_send = shutil.which("notify-send")
    if notify_send is None:
        return

    cmd = [notify_send, "--app-name=kura"]
    if critical:
        cmd.append("--urgency=critical")
    cmd.extend([title, body])

    env = dict(os.environ)
    # systemd user services run outside a graphical session bus by default.
    # When DBUS_SESSION_BUS_ADDRESS isn't set, fall back to the standard
    # per-user runtime bus socket so notify-send can still reach the
    # user's running notification daemon.
    if "DBUS_SESSION_BUS_ADDRESS" not in env and env.get("XDG_RUNTIME_DIR"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={env['XDG_RUNTIME_DIR']}/bus"

    try:
        subprocess.run(cmd, env=env, timeout=10, capture_output=True, check=False)
    except Exception:  # noqa: BLE001, S110 — a failed notification must never fail the run
        pass
