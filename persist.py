"""Coalesced, atomic JSON state files (lobby_state.json, match_tickets.json, chat_state.json).

``JsonWriter.save()`` only marks the document dirty. Within ``delay`` seconds the document
is serialised once on the event loop (the C JSON encoder; no indent) and written to
``<name>.tmp`` + ``os.replace`` on a background thread, so a burst of changes (100 new
accounts at once) costs one write instead of 100 and the event loop never waits for the
disk. ``flush()`` writes synchronously; it runs at exit, when the server stops, and before
anything in this process loads the same file again (``load_json``), so a restart in the same
process always sees the newest state. Without a running event loop ``save()`` writes at once.
"""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import threading
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("poc.persist")

_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()
_WRITERS: "weakref.WeakValueDictionary[str, JsonWriter]" = weakref.WeakValueDictionary()


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            # one thread: writes of every file stay in submission order
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="state-writer")
        return _EXECUTOR


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def flush_pending(path: Optional[Path]) -> None:
    """Write out a pending (debounced) save of this file, if this process has one."""
    w = _WRITERS.get(_key(path)) if path else None
    if w is not None:
        w.flush()


def load_json(path: Path):
    """json.loads of the file, after flushing a pending writer of the same path."""
    flush_pending(path)
    return json.loads(Path(path).read_text(encoding="utf-8"))


class JsonWriter:
    def __init__(self, path: Optional[Path], snapshot: Callable[[], object], delay: float = 1.0,
                 indent: Optional[int] = None) -> None:
        self.path = Path(path) if path else None
        self.snapshot = snapshot
        self.delay = delay
        self.indent = indent
        self.dirty = False
        self.writes = 0
        self._handle: Optional[asyncio.TimerHandle] = None
        self._inflight: Optional[Future] = None
        self._lock = threading.Lock()
        if self.path is not None:
            _WRITERS[_key(self.path)] = self

    def _text(self) -> str:
        return json.dumps(self.snapshot(), indent=self.indent)

    def save(self) -> None:
        """Mark dirty and schedule one coalesced write."""
        if self.path is None:
            return
        self.dirty = True
        if self._handle is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()
            return
        self._handle = loop.call_later(self.delay, self._write_soon)

    def save_now(self) -> None:
        """Serialise now and hand the write to the writer thread (still off the loop)."""
        if self.path is None:
            return
        self.dirty = True
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._write_soon()

    def _write_soon(self) -> None:
        self._handle = None
        if not self.dirty or self.path is None:
            return
        self.dirty = False
        try:
            text = self._text()
        except (TypeError, ValueError) as exc:
            log.error("cannot serialise %s: %s", self.path, exc)
            return
        self.writes += 1
        self._inflight = _executor().submit(self._write, text)

    def _write(self, text: str) -> None:
        with self._lock:
            try:
                write_atomic(self.path, text)
            except OSError as exc:
                log.error("cannot write %s: %s", self.path, exc)

    def flush(self) -> None:
        """Write the newest state now (synchronously) and wait for any write in flight."""
        if self.path is None:
            return
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        inflight = self._inflight
        if inflight is not None:
            try:
                inflight.result(timeout=30)
            except Exception:  # noqa: BLE001
                pass
        if self.dirty:
            self.dirty = False
            self.writes += 1
            self._write(self._text())


@atexit.register
def flush_all() -> None:
    for w in list(_WRITERS.values()):
        try:
            w.flush()
        except Exception:  # noqa: BLE001
            pass
