"""asyncio UDP front end for MatchCore.

Integration (in-process, from the main server):

    from match import start_match_server
    match_server = await start_match_server(loop, "0.0.0.0", 25103)
    ...
    match_server.close()

Standalone:  python -m match --match-port 25103
Worker processes (one UDP port each, several matches per process): match/pool.py.

The tick runs at a fixed rate (30 Hz): each tick is scheduled 33 ms after the previous
one's *scheduled* start, not 33 ms after it ended, and Windows timers are set to 1 ms
(runtime.high_resolution_timers); a plain asyncio.sleep(0.033) on Windows lands on the
15.6 ms timer grid and gave ~47 ms ticks (21 Hz).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from .game_data import DEFAULT_TICKETS_PATH, GameData, make_ticket_lookup
from .match_state import MatchConfig, MatchCore

try:                                               # poc-server/runtime.py
    import runtime as _rt
except ImportError:                                # pragma: no cover - match used on its own
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import runtime as _rt

log = logging.getLogger("match.server")

DEFAULT_MATCH_PORT = 25103
TICK_INTERVAL_S = 0.033          # 30 Hz, the client's own send cadence (spec 7)
MAX_CATCH_UP_S = 0.25            # further behind than this: skip ahead instead of bursting


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, server: "MatchServer") -> None:
        self.server = server

    def datagram_received(self, data: bytes, addr) -> None:
        srv = self.server
        t0 = time.perf_counter()
        try:
            srv.core.datagram_received(data, (addr[0], addr[1]), srv.now_ms())
        except Exception:
            log.exception("datagram from %s:%d failed", addr[0], addr[1])
        srv.datagram_s += time.perf_counter() - t0
        srv.datagrams += 1

    def error_received(self, exc: Exception) -> None:
        # Windows reports ICMP port-unreachable as WSAECONNRESET on the next recv
        # (runtime.udp_socket turns that off); the session's own timeout handles a
        # vanished client.
        log.debug("udp error: %s", exc)


class MatchStats:
    """Tick measurements: per-match simulation time (tick + 0x82), the whole cycle
    (all matches + flushing every session) and the interval between tick starts."""

    def __init__(self) -> None:
        self.match_ms: Dict[int, _rt.Samples] = {}
        self.cycle_ms = _rt.Samples()
        self.interval_ms = _rt.Samples()
        self.ticks = 0
        self.late_ticks = 0

    def match_tick(self, match_id: int, seconds: float) -> None:
        s = self.match_ms.get(match_id)
        if s is None:
            s = self.match_ms[match_id] = _rt.Samples(512)
        s.add(seconds * 1000.0)

    def drain(self, live_match_ids=None) -> dict:
        """Samples since the last drain; then forgets matches that no longer exist."""
        out = {"tick_ms": {str(k): v.drain() for k, v in self.match_ms.items()},
               "cycle_ms": self.cycle_ms.drain(), "tick_interval_ms": self.interval_ms.drain()}
        if live_match_ids is not None:
            for k in [k for k in self.match_ms if k not in live_match_ids]:
                del self.match_ms[k]
        return out

    def tick_p(self, q: float) -> Optional[float]:
        return _rt.pct([x for s in self.match_ms.values() for x in s.recent], q)


class MatchServer:
    def __init__(self, config: Optional[MatchConfig] = None,
                 ticket_lookup: Optional[Callable] = None,
                 game_data: Optional[GameData] = None,
                 on_event: Optional[Callable] = None) -> None:
        self._t0 = time.monotonic()
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.core = MatchCore(self._sendto, config, game_data, ticket_lookup, on_event=on_event)
        self.stats = MatchStats()
        self.core.tick_timer = self.stats.match_tick
        self._tick_task: Optional[asyncio.Task] = None
        self.datagrams = 0
        self.datagram_s = 0.0

    def now_ms(self) -> int:
        # never 0: the connection treats a 0 receive time as "timer not armed"
        return int((time.monotonic() - self._t0) * 1000) + 1000

    def _sendto(self, data: bytes, addr) -> None:
        if self.transport is not None:
            self.transport.sendto(data, addr)

    async def _tick_loop(self) -> None:
        pc = time.perf_counter
        next_t = pc()
        last_start = None
        while True:
            start = pc()
            if last_start is not None:
                self.stats.interval_ms.add((start - last_start) * 1000.0)
            last_start = start
            try:
                self.core.tick(self.now_ms())
            except Exception:
                log.exception("match tick failed")
            self.stats.cycle_ms.add((pc() - start) * 1000.0)
            self.stats.ticks += 1
            next_t += TICK_INTERVAL_S
            delay = next_t - pc()
            if delay < -MAX_CATCH_UP_S:
                self.stats.late_ticks += 1
                next_t = pc()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))

    @property
    def local_address(self):
        return self.transport.get_extra_info("sockname") if self.transport else None

    def drain_stats(self) -> dict:
        out = self.stats.drain({m.match_id for m in self.core.matches.values()})
        out["dgram_ms"] = self.datagram_s * 1000.0
        out["datagrams"] = self.datagrams
        self.datagram_s, self.datagrams = 0.0, 0
        out.update(self.summary())
        return out

    def summary(self) -> dict:
        core = self.core
        return {"matches": len(core.matches),
                "sessions": len(core.sessions),
                "players": sum(1 for m in core.matches.values() for p in m.players
                               if p.session is not None)}

    def close(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        if self.transport:
            self.transport.close()


async def start_match_server(loop: Optional[asyncio.AbstractEventLoop] = None,
                             host: str = "0.0.0.0", port: int = DEFAULT_MATCH_PORT,
                             config: Optional[MatchConfig] = None,
                             ticket_lookup: Optional[Callable] = None,
                             tickets_path: Optional[Path] = DEFAULT_TICKETS_PATH,
                             on_event: Optional[Callable] = None) -> MatchServer:
    """Bind the match UDP socket on host:port and start the 30 Hz tick.

    ticket_lookup(session_id) -> dict|None overrides the default, which tries
    lobby.get_match_ticket and then reads tickets_path (state/match_tickets.json)."""
    loop = loop or asyncio.get_running_loop()
    _rt.high_resolution_timers()
    _rt.precise_loop_clock(loop)
    server = MatchServer(config, ticket_lookup or make_ticket_lookup(tickets_path),
                         on_event=on_event)
    sock = _rt.udp_socket(host, port)
    transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(server), sock=sock)
    server.transport = transport
    server._tick_task = loop.create_task(server._tick_loop())
    cfg = server.core.config
    if cfg.world_collision:
        from .level_collision import load_level
        col = load_level(cfg.map_name, cfg.collision_path)      # cached for every match
        if col is None:
            log.warning("no level collision for %s: walls will not stop bullets "
                        "(python match/tools/build_level_collision.py)", cfg.map_name)
        else:
            log.info("level collision %s: %d triangles, %d objects", cfg.map_name,
                     col.triangle_count, col.header["stats"]["objects_with_geometry"])
    log.info("match server listening on udp %s:%d (map %s, mode %d)",
             *server.local_address[:2], server.core.config.map_name, server.core.config.mode)
    return server
