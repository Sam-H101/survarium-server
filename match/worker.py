"""Match worker process: one MatchServer (its own UDP port, its own event loop and GIL)
hosting the matches the main process places on it. Started by match/pool.py.

Messages (multiprocessing pipes, pickled tuples)
  main -> worker  ("tickets", request_id, match_id, {str(session_id): ticket})
                  ("stop",)
  worker -> main  ("ready", index, udp_port, pid)
                  ("placed", request_id, match_id)        the tickets are in; send op 51 now
                  ("event", kind, session_id, match_id, result)  MatchCore.on_event for the lobby
                                                          (result: Match.player_result or None)
                  ("match_removed", match_id)
                  ("stats", {...})                        every stats_interval seconds
                  ("error", text)                         startup failed
                  ("log", levelno, logger_name, text)     a log line for the main log

A worker writes nothing to its own stdout/stderr: it releases the inherited handles at
start (Windows duplicates the parent's standard handles into it; holding the server's log
file open would keep it locked after the server is gone) and sends its log lines to the
main process, which writes them with a "[match-wN]" prefix.

The worker exits when the main process goes away (its end of the pipe breaks), so a
killed server never leaves orphaned workers holding the UDP ports.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("match.worker")


class _PipeLogHandler(logging.Handler):
    def __init__(self, send, index: int) -> None:
        super().__init__()
        self.send, self.index = send, index

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self.send(("log", record.levelno, record.name, f"[match-w{self.index}] {text}"))
        except Exception:                            # noqa: BLE001
            pass


def _release_std_handles() -> None:
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
        os.close(devnull)
        sys.stdout = sys.stderr = open(os.devnull, "w")
    except OSError:
        pass


def worker_main(index: int, host: str, port: int, config, ctrl, events, log_level: int,
                stats_interval: float, rate_limit_logs: bool = True) -> None:
    _release_std_handles()
    # Ctrl+C reaches every process of the console: only the main process handles it (it
    # stops the workers); a worker also ends when the main process is gone (pipe EOF)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    import runtime
    lock = threading.Lock()

    def send(msg) -> None:
        try:
            with lock:
                events.send(msg)
        except (OSError, EOFError, BrokenPipeError):
            os._exit(0)                              # main process is gone

    root = logging.getLogger()
    root.handlers[:] = []
    handler = _PipeLogHandler(send, index)
    if rate_limit_logs:
        handler.addFilter(runtime.RateLimitFilter())
    root.addHandler(handler)
    root.setLevel(log_level)
    runtime.high_resolution_timers()

    try:
        asyncio.run(_run(index, host, port, config, ctrl, send, stats_interval))
    except KeyboardInterrupt:
        pass
    except BaseException as exc:                     # noqa: BLE001
        log.exception("worker %d failed", index)
        send(("error", f"{type(exc).__name__}: {exc}"))
    os._exit(0)


async def _run(index, host, port, config, ctrl, send, stats_interval) -> None:
    import runtime
    from match.server import start_match_server

    loop = asyncio.get_running_loop()
    tickets: dict = {}
    stop = asyncio.Event()

    def on_event(kind: str, session_id: int, match_id: int = 0, result=None, **_) -> None:
        send(("event", kind, session_id, match_id, result))

    try:
        server = await start_match_server(loop, host, port, config,
                                          ticket_lookup=lambda sid: tickets.get(str(sid)),
                                          tickets_path=None, on_event=on_event)
    except OSError as exc:
        if index == 0 or port == 0:
            raise
        log.info("udp port %d busy (%s); using an ephemeral port", port, exc)
        server = await start_match_server(loop, host, 0, config,
                                          ticket_lookup=lambda sid: tickets.get(str(sid)),
                                          tickets_path=None, on_event=on_event)

    def on_match_removed(m) -> None:
        for p in m.players:
            key = str(p.ticket.session_id)
            t = tickets.get(key)
            if t is not None and t.get("match_id") == m.match_id:
                del tickets[key]
        send(("match_removed", m.match_id))
    server.core.on_match_removed = on_match_removed

    def reader() -> None:
        while True:
            try:
                msg = ctrl.recv()
            except (EOFError, OSError):
                os._exit(0)                          # main process is gone
            kind = msg[0]
            if kind == "tickets":
                _, req, match_id, batch = msg
                tickets.update(batch)                # atomic under the GIL
                send(("placed", req, match_id))
            elif kind == "stop":
                loop.call_soon_threadsafe(stop.set)
                return

    threading.Thread(target=reader, name="match-ctrl", daemon=True).start()
    lag = runtime.LoopLagMonitor().start()
    send(("ready", index, server.local_address[1], os.getpid()))

    async def stats_loop() -> None:
        while True:
            await asyncio.sleep(stats_interval)
            out = server.drain_stats()
            out.update(index=index, pid=os.getpid(), port=server.local_address[1],
                       cpu_s=runtime.cpu_seconds(), rss_mb=runtime.rss_mb(),
                       loop_lag_ms=lag.samples.drain(), tickets=len(tickets), t=time.time(),
                       pending_dropped=server.core.pending_dropped)
            send(("stats", out))

    task = loop.create_task(stats_loop())
    try:
        await stop.wait()
    finally:
        task.cancel()
        lag.stop()
        server.close()
