"""Thread-safe workspace access for dynamically dispatched workers."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class _ReadWriteLock:
    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


class WorkspaceMutex:
    """Coordinate worker file operations without serializing unrelated reads."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        self.root = Path(root).resolve() if root is not None else None
        self._guard = threading.RLock()
        self._workspace = _ReadWriteLock()
        self._files: dict[str, _ReadWriteLock] = {}

    def _key(self, path: str | os.PathLike[str]) -> str:
        raw = Path(path)
        resolved = (self.root / raw if self.root and not raw.is_absolute() else raw).resolve()
        if self.root and not resolved.is_relative_to(self.root):
            raise PermissionError("worker path escapes workspace root")
        value = resolved.as_posix()
        return value.casefold() if os.name == "nt" else value

    def _file(self, path: str | os.PathLike[str]) -> _ReadWriteLock:
        key = self._key(path)
        with self._guard:
            return self._files.setdefault(key, _ReadWriteLock())

    @contextmanager
    def read_lock(self, path: str | os.PathLike[str]) -> Iterator[None]:
        with self._workspace.read():
            with self._file(path).read():
                yield

    @contextmanager
    def write_lock(self, path: str | os.PathLike[str]) -> Iterator[None]:
        with self._workspace.read():
            with self._file(path).write():
                yield

    @contextmanager
    def write_many(self, paths: list[str] | tuple[str, ...]) -> Iterator[None]:
        keys = sorted({self._key(path) for path in paths})
        locks = [self._file(key) for key in keys]
        with self._workspace.read():
            contexts = [lock.write() for lock in locks]
            entered = []
            try:
                for context in contexts:
                    context.__enter__()
                    entered.append(context)
                yield
            finally:
                for context in reversed(entered):
                    context.__exit__(None, None, None)

    @contextmanager
    def workspace_write(self) -> Iterator[None]:
        with self._workspace.write():
            yield

    @contextmanager
    def workspace_read(self) -> Iterator[None]:
        with self._workspace.read():
            yield

    @contextmanager
    def files(self, paths, *, write: bool = False) -> Iterator[None]:
        """Compatibility wrapper for the previous lock API."""
        if write:
            with self.write_many(list(paths)):
                yield
            return
        values = sorted({self._key(path) for path in paths})
        contexts = [self.read_lock(value) for value in values]
        entered = []
        try:
            for context in contexts:
                context.__enter__()
                entered.append(context)
            yield
        finally:
            for context in reversed(entered):
                context.__exit__(None, None, None)

    @contextmanager
    def patch(self, paths) -> Iterator[None]:
        with self.workspace_write():
            yield


class LockedWorkspace:
    """Wrap an existing workspace and put every operation behind ``WorkspaceMutex``."""

    def __init__(self, workspace: Any, mutex: WorkspaceMutex | None = None):
        self.workspace = workspace
        workspace_root = getattr(workspace, "root", None)
        self.mutex = mutex or WorkspaceMutex(workspace_root)

    def read(self, path: str):
        with self.mutex.read_lock(path):
            return self.workspace.read(path)

    def write(self, path: str, content: str | bytes):
        with self.mutex.write_lock(path):
            if hasattr(self.workspace, "write"):
                return self.workspace.write(path, content)
            target = self.mutex.root / path if self.mutex.root else Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content if isinstance(content, bytes) else content.encode("utf-8")
            target.write_bytes(data)
            return {"path": str(path)}

    def files(self):
        with self.mutex.workspace_read():
            return list(self.workspace.files())

    def apply(self, proposal):
        paths = [edit.path for edit in proposal.edits]
        with self.mutex.workspace_write():
            return self.workspace.apply(proposal)

    def __getattr__(self, name: str):
        return getattr(self.workspace, name)
