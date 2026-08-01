from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import paths


@contextmanager
def source_lock(name: str, state_dir: Path | None = None) -> Iterator[bool]:
    """Context manager that attempts a non-blocking exclusive lock on a source.

    Yields *True* if the lock was acquired, *False* if another process holds it.
    The lock file is kept open for the duration of the context so the lock
    remains held until the context exits.

    Parameters
    ----------
    name:
        The source name (used as the lock file stem).
    state_dir:
        Override the state directory; defaults to ``paths.state_dir()``.
    """
    lock_dir = paths.locks_dir(state_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{name}.lock"

    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
