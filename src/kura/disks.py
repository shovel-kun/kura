"""Health signals for the disks kura archives to, used by ``kura doctor``.

A dying disk shows up first in the kernel log (I/O errors, ATA ``UNC`` /
``IDNF`` errors) and in a few SMART counters, well before SMART's overall
verdict flips to FAILED.  Everything here is best-effort and read-only:
missing tools, no permission, or a non-block filesystem all yield "unknown"
rather than an error.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

_SYS_DEV_BLOCK = Path("/sys/dev/block")

# SMART attributes whose raw value should stay at zero on a healthy drive.
SMART_WATCH = {
    5: "reallocated sectors",
    197: "pending sectors",
    198: "offline uncorrectable sectors",
}


def disk_for_path(path: Path, *, sys_dev_block: Path = _SYS_DEV_BLOCK) -> str | None:
    """Return the whole-disk kernel name (``sda``, ``nvme0n1``) holding *path*, or None."""
    try:
        dev = path.stat().st_dev
    except OSError:
        return None
    node = sys_dev_block / f"{os.major(dev)}:{os.minor(dev)}"
    try:
        resolved = node.resolve(strict=True)
    except OSError:
        return None
    # A partition's sysfs directory sits inside its disk's and has a "partition" file.
    if (resolved / "partition").exists():
        resolved = resolved.parent
    return resolved.name


def ata_port(disk: str, *, sys_block: Path = Path("/sys/block")) -> str | None:
    """Return the libata port name (``ata6``) for *disk*, or None for non-ATA disks."""
    try:
        resolved = str((sys_block / disk).resolve(strict=True))
    except OSError:
        return None
    m = re.search(r"/(ata\d+)/", resolved)
    return m.group(1) if m else None


def match_kernel_errors(lines: list[str], disk: str, ata: str | None) -> list[str]:
    """Return the kernel log lines that report media or I/O errors for *disk*."""
    patterns = [
        re.compile(rf"I/O error, dev {re.escape(disk)},"),
        re.compile(rf"\[{re.escape(disk)}\].*(?:Medium Error|Unrecovered read error)"),
    ]
    if ata:
        patterns.append(re.compile(rf"\b{re.escape(ata)}\.\d+: error: \{{ .*\b(?:UNC|IDNF)\b"))
    return [line for line in lines if any(p.search(line) for p in patterns)]


def kernel_errors(disk: str, *, since: str = "-7d") -> list[str] | None:
    """Return recent kernel-log error lines for *disk*, or None if the log is unreadable."""
    if shutil.which("journalctl") is None:
        return None
    try:
        result = subprocess.run(
            ["journalctl", "-k", "--since", since, "--no-pager", "-q", "-o", "short"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return match_kernel_errors(result.stdout.splitlines(), disk, ata_port(disk))


def parse_smart_attributes(data: dict) -> dict[int, int]:
    """Return ``{attribute id: raw value}`` from ``smartctl --json`` output."""
    table = data.get("ata_smart_attributes", {}).get("table", [])
    values: dict[int, int] = {}
    for attr in table:
        try:
            values[int(attr["id"])] = int(attr["raw"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
    return values


def smart_counters(disk: str) -> dict[int, int] | None:
    """Return the watched SMART raw counters for *disk*, or None if unavailable.

    Needs read access to the device node (usually root); without it smartctl
    cannot open the disk and this returns None.
    """
    if shutil.which("smartctl") is None:
        return None
    try:
        result = subprocess.run(
            ["smartctl", "--json", "-A", f"/dev/{disk}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # Exit-status bit 1: device open failed (typically permission denied).
    if result.returncode & 0b10:
        return None
    try:
        attrs = parse_smart_attributes(json.loads(result.stdout))
    except json.JSONDecodeError:
        return None
    if not attrs:
        return None  # NVMe and other non-ATA disks report health differently
    return {attr_id: attrs.get(attr_id, 0) for attr_id in SMART_WATCH}
