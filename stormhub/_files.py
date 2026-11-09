"""Local atomic publication and process-scoped filesystem serialization."""

from __future__ import annotations

import errno
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


class ImmutableFileConflict(ValueError):
    """An established file differs from the proposed immutable content."""


def _sync_directory(directory: Path) -> None:
    # Windows does not expose directory fsync through the standard library.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_content(stream: BinaryIO, content: bytes) -> None:
    if stream.write(content) != len(content):
        raise OSError("Incomplete staged file write")
    stream.flush()
    os.fsync(stream.fileno())


def atomic_write_bytes(path: Path, content: bytes, *, immutable: bool) -> None:
    """Publish complete bytes, refusing replacement when immutable is requested.

    Immutable publication requires same-filesystem hard links (for example local
    NTFS). Unsupported filesystems fail closed. Only this call's temporary file
    is removed; abandoned temporary files never count as completed artifacts.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_bytes() != content:
            raise ImmutableFileConflict(f"immutable artifact differs: {path.name}")
        return
    temporary = None
    try:
        candidate = path.parent / f".stormhub-{uuid.uuid4().hex}.tmp"
        # Exclusive creation keeps collisions harmless and uses the same
        # inherited permissions/umask as the previous direct file writes.
        with candidate.open("xb") as stream:
            temporary = candidate
            _write_content(stream, content)
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise ImmutableFileConflict(f"immutable artifact differs: {path.name}") from None
        else:
            os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_file_lock(path: Path, *, timeout: float = 30.0) -> Iterator[None]:
    """Serialize cooperating processes; process death releases the OS lock.

    Keep the lock file in place so another caller cannot lock a replacement
    inode. The containing directory must be trusted and support OS file locks.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for publication lock: {path.name}") from exc
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
