"""Tests for kura.disks — disk health signals used by `kura doctor`."""
from __future__ import annotations

from pathlib import Path

from kura import disks

# Real kernel log lines from a failing WD Red on ata6 / sda.
KERNEL_LOG = [
    "Oct 12 20:02:52 host kernel: ata6.00: failed command: WRITE FPDMA QUEUED",
    "Oct 12 20:02:52 host kernel: ata6.00: status: { DRDY ERR }",
    "Oct 12 20:02:52 host kernel: ata6.00: error: { IDNF }",
    "Oct 12 20:02:52 host kernel: ata6: EH complete",
    "Oct 12 20:42:16 host kernel: ata6.00: error: { UNC }",
    "Oct 12 20:42:16 host kernel: sd 5:0:0:0: [sda] tag#27 Sense Key : Medium Error [current] ",
    "Oct 12 20:42:16 host kernel: sd 5:0:0:0: [sda] tag#27 Add. Sense: Unrecovered read error - auto reallocate failed",
    "Oct 12 20:42:16 host kernel: I/O error, dev sda, sector 103428784 op 0x0:(READ) flags 0x0",
    # Other disks and benign lines must not match.
    "Oct 12 20:50:00 host kernel: I/O error, dev sdb, sector 1 op 0x0:(READ)",
    "Oct 12 20:50:00 host kernel: ata16.00: error: { UNC }",
    "Oct 12 20:50:00 host kernel: ata6.00: configured for UDMA/133",
]


def test_match_kernel_errors_for_failing_disk() -> None:
    matched = disks.match_kernel_errors(KERNEL_LOG, "sda", "ata6")
    assert matched == [KERNEL_LOG[2], KERNEL_LOG[4], KERNEL_LOG[5], KERNEL_LOG[6], KERNEL_LOG[7]]


def test_match_kernel_errors_without_ata_port() -> None:
    matched = disks.match_kernel_errors(KERNEL_LOG, "sda", None)
    assert matched == [KERNEL_LOG[5], KERNEL_LOG[6], KERNEL_LOG[7]]


def test_match_kernel_errors_quiet_disk() -> None:
    assert disks.match_kernel_errors(KERNEL_LOG, "nvme0n1", None) == []


def test_parse_smart_attributes() -> None:
    data = {
        "ata_smart_attributes": {
            "table": [
                {"id": 5, "raw": {"value": 0}},
                {"id": 197, "raw": {"value": 5}},
                {"id": 9, "raw": {"value": 9230}},
                {"id": "bogus"},
            ]
        }
    }
    assert disks.parse_smart_attributes(data) == {5: 0, 197: 5, 9: 9230}
    assert disks.parse_smart_attributes({}) == {}


def _fake_sysfs(tmp_path: Path, path: Path) -> Path:
    """Build /sys/dev/block/<maj:min> → .../sda/sda1 for the device holding *path*."""
    import os

    dev = path.stat().st_dev
    disk_dir = tmp_path / "devices" / "pci0000:00" / "ata6" / "host5" / "block" / "sda"
    part_dir = disk_dir / "sda1"
    part_dir.mkdir(parents=True)
    (part_dir / "partition").write_text("1\n")
    dev_block = tmp_path / "dev-block"
    dev_block.mkdir()
    (dev_block / f"{os.major(dev)}:{os.minor(dev)}").symlink_to(part_dir)
    return dev_block


def test_disk_for_path_resolves_partition_to_disk(tmp_path: Path) -> None:
    target = tmp_path / "archive"
    target.mkdir()
    dev_block = _fake_sysfs(tmp_path, target)
    assert disks.disk_for_path(target, sys_dev_block=dev_block) == "sda"


def test_disk_for_path_unknown_device(tmp_path: Path) -> None:
    assert disks.disk_for_path(tmp_path, sys_dev_block=tmp_path / "missing") is None
    assert disks.disk_for_path(tmp_path / "nope") is None


def test_ata_port(tmp_path: Path) -> None:
    disk_dir = tmp_path / "devices" / "pci0000:00" / "ata6" / "host5" / "block" / "sda"
    disk_dir.mkdir(parents=True)
    sys_block = tmp_path / "block"
    sys_block.mkdir()
    (sys_block / "sda").symlink_to(disk_dir)
    (sys_block / "nvme0n1").symlink_to(tmp_path / "devices")
    assert disks.ata_port("sda", sys_block=sys_block) == "ata6"
    assert disks.ata_port("nvme0n1", sys_block=sys_block) is None
    assert disks.ata_port("sdz", sys_block=sys_block) is None
