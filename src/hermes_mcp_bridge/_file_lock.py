"""Process-local exclusive advisory file locking helper (fcntl.flock).

Used by restore and secret rotation to serialize mutating operations against a
fixed, private, canonical lock file (mode 0600). The lock is released and the
file descriptor closed on context exit, so a crash or exception never leaves a
functional orphan lock.
"""

from __future__ import annotations

import errno
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress


class FileLockError(RuntimeError):
    """Raised when an exclusive file lock cannot be acquired."""


@contextmanager
def exclusive_file_lock(lock_path: str, *, blocking: bool = False) -> Iterator[int]:
    """Acquire an exclusive advisory lock on ``lock_path``.

    The lock file is created with mode 0600 if it does not exist. The path is
    canonicalized with ``os.path.realpath`` so that symlinked or non-normalized
    paths always map to the same lock, and the parent directory is created with
    mode 0700 (private) when the bridge owns it. Yields the open file descriptor
    and releases the lock (and closes the fd) on exit.
    """
    abs_path = os.path.realpath(os.path.abspath(os.path.expanduser(lock_path)))
    # Reject symlinked paths: a symlink could redirect the lock outside the
    # intended private directory, defeating exclusive locking.
    if os.path.islink(abs_path):
        raise FileLockError(f"lock path is a symlink (refused): {abs_path}")
    parent = os.path.dirname(abs_path)
    if parent:
        existed = os.path.isdir(parent)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        if not existed:
            with suppress(OSError):
                os.chmod(parent, 0o700)
        # If the parent itself resolves through a symlink, refuse.
        real_parent = os.path.realpath(parent)
        if os.path.islink(parent) or real_parent != os.path.abspath(parent):
            raise FileLockError(f"lock parent is not a safe directory: {parent}")
    fd = os.open(abs_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        with suppress(OSError):
            os.fchmod(fd, 0o600)
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                msg = f"rotation lock busy: {abs_path}"
                raise FileLockError(msg) from exc
            raise
        yield fd
    finally:
        with suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
