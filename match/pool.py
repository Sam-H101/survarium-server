"""Match simulation in worker processes (main-process side).

Each worker (match/worker.py) is a separate Python process with its own GIL, event loop
and UDP socket on port ``base_port + index`` (an ephemeral port if that one is taken; the
first worker must get ``base_port``). The lobby places every new match on the worker with
the fewest placed players: the tickets go to that worker first, and only when it has
confirmed them does op 51 go out, with that worker's port (op 51 carries a u16 port; the
client accepts any). A slow match therefore only slows the matches of its own worker, never
the login, lobby or chat loop, and N workers use N cores.

Events of the workers' MatchCore (session_connected / session_ended / match_finished) are
handed to ``on_event`` on the main loop exactly as the in-process server calls it. A worker
that dies is restarted on the same port; the lobby is told that its matches finished.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import multiprocessing as mp
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

log = logging.getLogger("match.pool")

PLACE_TIMEOUT_S = 5.0


@dataclass
class _Worker:
    index: int
    process: Optional[mp.process.BaseProcess] = None
    ctrl: object = None                  # main end of the main -> worker pipe
    port: int = 0
    pid: int = 0
    ready: bool = False
    matches: Dict[int, int] = field(default_factory=dict)      # match_id -> players placed
    sessions: Dict[int, List[int]] = field(default_factory=dict)   # match_id -> session ids
    stats: dict = field(default_factory=dict)
    restarts: int = 0

    @property
    def load(self) -> int:
        return sum(self.matches.values())


class MatchPool:
    def __init__(self, workers: int, host: str, base_port: int, config,
                 on_event: Optional[Callable[..., None]] = None, log_level: int = logging.INFO,
                 stats_interval: float = 1.0, rate_limit_logs: bool = True) -> None:
        self.n = max(1, workers)
        self.host = host
        self.base_port = base_port
        self.config = config
        self.on_event = on_event
        self.log_level = log_level
        self.stats_interval = stats_interval
        self.rate_limit_logs = rate_limit_logs
        self.workers = [_Worker(i) for i in range(self.n)]
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ctx = mp.get_context("spawn")
        self._req = itertools.count(1)
        self._pending: Dict[int, tuple] = {}          # request -> (worker, callback, timer)
        self._ready_waiters: Dict[int, asyncio.Future] = {}
        self._closing = False
        self.on_match_removed: Optional[Callable[[int], None]] = None
        self.stats_pending: List[dict] = []           # drained by the main stats writer
        self._deferred: List[tuple] = []              # placements waiting for a ready worker

    # ------------------------------------------------------------------ lifecycle
    async def start(self, timeout: float = 120.0) -> None:
        self.loop = asyncio.get_running_loop()
        waits = [self._spawn(w) for w in self.workers]
        try:
            ports = await asyncio.wait_for(asyncio.gather(*waits), timeout)
        except Exception:
            self.close()
            raise
        log.info("match workers: %d processes, udp ports %s", self.n, ", ".join(map(str, ports)))

    def _spawn(self, w: _Worker) -> asyncio.Future:
        from .worker import worker_main
        ctrl_r, ctrl_w = self._ctx.Pipe(duplex=False)
        evt_r, evt_w = self._ctx.Pipe(duplex=False)
        port = self.base_port + w.index if self.base_port else 0
        if w.port:                                    # restart: keep the port clients know
            port = w.port
        p = self._ctx.Process(target=worker_main, name=f"match-worker-{w.index}",
                              args=(w.index, self.host, port, self.config, ctrl_r, evt_w,
                                    self.log_level, self.stats_interval, self.rate_limit_logs),
                              daemon=True)
        p.start()
        ctrl_r.close()
        evt_w.close()
        w.process, w.ctrl, w.ready = p, ctrl_w, False
        fut = self.loop.create_future()
        self._ready_waiters[w.index] = fut
        threading.Thread(target=self._reader, args=(w, evt_r, p), name=f"match-pool-{w.index}",
                         daemon=True).start()
        return fut

    def _reader(self, w: _Worker, conn, process) -> None:
        """Blocking reads of one worker's events; everything is handled on the main loop."""
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                break
            try:
                self.loop.call_soon_threadsafe(self._on_message, w, process, msg)
            except RuntimeError:                      # loop closed
                return
        try:
            self.loop.call_soon_threadsafe(self._on_worker_exit, w, process)
        except RuntimeError:
            pass

    def close(self) -> None:
        self._closing = True
        for w in self.workers:
            try:
                if w.ctrl is not None:
                    w.ctrl.send(("stop",))
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + 3.0
        for w in self.workers:
            if w.process is not None:
                w.process.join(max(0.0, deadline - time.monotonic()))
                if w.process.is_alive():
                    w.process.terminate()

    # ------------------------------------------------------------------ messages
    def _on_message(self, w: _Worker, process, msg) -> None:
        if process is not w.process:
            return                                    # from a replaced worker
        kind = msg[0]
        if kind == "log":
            _, level, name, text = msg
            logging.getLogger(name).log(level, "%s", text, extra={"forwarded": True})
        elif kind == "event":
            _, ev, session_id, match_id = msg[:4]
            extra = {"result": msg[4]} if len(msg) > 4 and msg[4] is not None else {}
            if self.on_event is not None:
                try:
                    self.on_event(ev, session_id=session_id, match_id=match_id, **extra)
                except Exception:
                    log.exception("match event %s for session %d failed", ev, session_id)
        elif kind == "placed":
            _, req, _match_id = msg
            pending = self._pending.pop(req, None)
            if pending is not None:
                _, callback, timer = pending
                timer.cancel()
                callback(w.port)
        elif kind == "stats":
            w.stats = msg[1]
            self.stats_pending.append(msg[1])
            if len(self.stats_pending) > 1000:
                del self.stats_pending[:500]
        elif kind == "match_removed":
            match_id = msg[1]
            w.matches.pop(match_id, None)
            w.sessions.pop(match_id, None)
            if self.on_match_removed is not None:
                self.on_match_removed(match_id)
        elif kind == "ready":
            _, index, port, pid = msg
            w.port, w.pid, w.ready = port, pid, True
            fut = self._ready_waiters.pop(index, None)
            if fut is not None and not fut.done():
                fut.set_result(port)
            deferred, self._deferred = self._deferred, []
            for match_id, tickets, callback, timer in deferred:
                timer.cancel()
                self.place(match_id, tickets, callback)
        elif kind == "error":
            log.error("match worker %d: %s", w.index, msg[1])
            fut = self._ready_waiters.pop(w.index, None)
            if fut is not None and not fut.done():
                fut.set_exception(RuntimeError(f"match worker {w.index}: {msg[1]}"))

    def _on_worker_exit(self, w: _Worker, process) -> None:
        if process is not w.process or self._closing:
            return
        process.join(0.5)
        log.error("match worker %d (pid %s) exited (code %s); %d matches lost, restarting",
                  w.index, w.pid, process.exitcode, len(w.matches))
        lost = dict(w.sessions)
        w.matches.clear()
        w.sessions.clear()
        w.ready = False
        for req, (pw, callback, timer) in list(self._pending.items()):
            if pw is w:
                del self._pending[req]
                timer.cancel()
                callback(None)
        for match_id, sids in lost.items():
            for sid in sids:
                if self.on_event is not None:
                    try:
                        self.on_event("match_finished", session_id=sid, match_id=match_id)
                    except Exception:
                        log.exception("match_finished for session %d failed", sid)
            if self.on_match_removed is not None:
                self.on_match_removed(match_id)
        if w.restarts < 20:
            w.restarts += 1
            self.loop.call_later(1.0, self._restart, w, process)

    def _restart(self, w: _Worker, process) -> None:
        if self._closing or process is not w.process:
            return                                    # shutting down, or already replaced
        fut = self._spawn(w)
        fut.add_done_callback(lambda f: f.exception())         # logged by "error" already

    # ------------------------------------------------------------------ placement
    def place(self, match_id: int, tickets: Dict[str, dict],
              callback: Callable[[Optional[int]], None]) -> None:
        """Send a new match's tickets to the least loaded worker; callback(udp_port) once
        that worker has them (op 51 may go out then), callback(None) if none could."""
        ready = [w for w in self.workers if w.ready]
        if not ready:
            if self._closing or self.loop is None:
                callback(None)
                return
            # workers still starting (or restarting): wait for the first one
            entry: list = []

            def give_up() -> None:
                if entry and entry[0] in self._deferred:
                    self._deferred.remove(entry[0])
                    log.error("no match worker ready for match %d", match_id)
                    callback(None)
            timer = self.loop.call_later(60.0, give_up)
            entry.append((match_id, tickets, callback, timer))
            self._deferred.append(entry[0])
            return
        w = min(ready, key=lambda x: (x.load, x.index))
        sids = [int(s) for s in tickets]
        w.matches[match_id] = len(sids)
        w.sessions[match_id] = sids
        req = next(self._req)

        def timed_out() -> None:
            if self._pending.pop(req, None) is not None:
                log.error("match worker %d did not confirm match %d within %.0f s",
                          w.index, match_id, PLACE_TIMEOUT_S)
                w.matches.pop(match_id, None)
                w.sessions.pop(match_id, None)
                callback(None)
        timer = self.loop.call_later(PLACE_TIMEOUT_S, timed_out)
        self._pending[req] = (w, callback, timer)
        try:
            w.ctrl.send(("tickets", req, match_id, tickets))
        except (OSError, ValueError) as exc:
            log.error("match worker %d unreachable: %s", w.index, exc)
            self._pending.pop(req, None)
            timer.cancel()
            w.matches.pop(match_id, None)
            w.sessions.pop(match_id, None)
            callback(None)

    # ------------------------------------------------------------------ stats
    def summary(self) -> dict:
        out = {"workers": self.n, "matches": 0, "sessions": 0, "players": 0}
        for w in self.workers:
            for k in ("matches", "sessions", "players"):
                out[k] += int(w.stats.get(k, 0))
        return out

    @property
    def ports(self) -> List[int]:
        return [w.port for w in self.workers]
