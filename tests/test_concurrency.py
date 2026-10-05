"""Concurrency and robustness: many clients at once, several matches at once, the match
worker processes, and the limits that keep a busy server bounded.

    python -m unittest tests.test_concurrency -v        (from poc-server/)

The long load runs (100 players, tools/loadtest.py) are in tests/test_load.py and only run
with SURV_LOAD_TESTS=1.
"""

from __future__ import annotations

import asyncio
import math
import random
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chat  # noqa: E402
import lobby  # noqa: E402
import lobby_data as ld  # noqa: E402
import persist  # noqa: E402
import runtime  # noqa: E402
from chat_mock_client import MockChatClient, pk_sign_in as chat_pk_sign_in  # noqa: E402
from match import ballistics as B  # noqa: E402
from match import combat as C  # noqa: E402
from match.match_state import MatchConfig, MatchCore  # noqa: E402
from match.server import TICK_INTERVAL_S, start_match_server  # noqa: E402
from mock_client import ClientModel, MockLobbyClient, pk_ready, tcp_frame  # noqa: E402
from test_e2e import SSL_DIR, free_port, http_get, sign_in  # noqa: E402
from test_lobby import DICTS, EXTRACTED  # noqa: E402
from test_m3 import roster_tickets  # noqa: E402
from test_reconnect import UdpMatchDriver  # noqa: E402

TICK_BUDGET_MS = 1000 * TICK_INTERVAL_S          # 33 ms
FIRE = 0x20


def shooter_bot(rng: random.Random):
    """Walk towards the nearest living enemy, then strafe and fire at its chest."""
    state = {"spawn": None, "pos": None}

    def bot(c, now):
        loc = c.local
        if state["spawn"] != loc["spawn_position"]:
            state["spawn"] = state["pos"] = tuple(loc["spawn_position"])
        pos = state["pos"]
        team = c.profiles[c.local_id][0].team
        best = None
        for pid, r in c.remote.items():
            ins = c.inserted.get(pid)
            if ins and ins["alive"] and c.profiles[pid][0].team != team:
                d = math.dist(pos, r["position"])
                if best is None or d < best[0]:
                    best = (d, r["position"])
        if best is None:
            return pos, loc["yaw"], 0.0, 0
        d, tp = best
        if d > 20.0:
            k = min(1.0, 0.12 / d)
            pos = tuple(pos[i] + (tp[i] - pos[i]) * k for i in range(3))
        state["pos"] = pos
        dx, dz = tp[0] - pos[0], tp[2] - pos[2]
        dy = tp[1] + 1.2 - (pos[1] + C.EYE_STAND)
        yaw = math.atan2(-dx, dz)
        pitch = B.VIEW.look_pitch_for(math.degrees(math.atan2(dy, math.hypot(dx, dz))))
        return pos, yaw, pitch, FIRE | (0x4 if rng.random() < 0.5 else 0x8)
    return bot


async def pump_drivers(drivers, until, timeout: float) -> bool:
    """Receive continuously, tick every client at 30 Hz like the real one (asyncio.sleep
    below the 15.6 ms clock resolution of Windows would otherwise tick it hundreds of times
    a second, each tick sending a reliable 0x43)."""
    pc = time.perf_counter
    deadline = pc() + timeout
    next_tick = pc()
    while pc() < deadline:
        tick = pc() >= next_tick
        if tick:
            next_tick += 0.033
        for d in drivers:
            while True:
                try:
                    data, _ = d.sock.recvfrom(4096)
                except (BlockingIOError, ConnectionResetError):
                    break
                d.client.datagram_received(data)
            if tick:
                d.client.tick(d.now())
        if until():
            return True
        await asyncio.sleep(0.005)
    return until()


def match_config(**kw) -> MatchConfig:
    base = dict(countdown_s=1, join_timeout_s=10, respawn_time=2, match_time=120,
                end_delay_s=0.5)
    base.update(kw)
    return MatchConfig(**base)


class FiveMatchesInProcess(unittest.IsolatedAsyncioTestCase):
    """5 simultaneous lobby matches x 4 players on one in-process match server over real
    UDP, full ballistics (level collision, dispersion, recoil), everyone shooting: every
    client stays fault-free and every match's tick stays well inside the 33 ms budget."""

    async def test_five_matches_of_four_shooting(self):
        tickets = {}
        rosters = []
        for k in range(5):
            sids, t = roster_tickets(4, match_id=100 + k, base_sid=1000 + 10 * k)
            tickets.update(t)
            rosters.append(sids)
        loop = asyncio.get_running_loop()
        server = await start_match_server(loop, "127.0.0.1", 0, match_config(),
                                          ticket_lookup=tickets.get, tickets_path=None)
        port = server.local_address[1]
        drivers = []
        try:
            rng = random.Random(3)
            for sids in rosters:
                for sid in sids:
                    d = UdpMatchDriver(loop, ("127.0.0.1", port), session_id=sid)
                    d.client.bot = shooter_bot(rng)
                    d.client.connect(d.now())
                    drivers.append(d)
            ok = await pump_drivers(drivers, lambda: all(d.client.controllable for d in drivers), 30)
            self.assertTrue(ok, [d.client.faults for d in drivers if not d.client.controllable][:3])
            self.assertEqual(len(server.core.matches), 5)
            server.stats.drain()                         # measure the fighting only
            await pump_drivers(drivers, lambda: False, 6.0)
            stats = server.stats.drain()
        finally:
            for d in drivers:
                d.close()
            server.close()
        for d in drivers:
            self.assertEqual(d.client.faults, [])
        per_match = {k: v for k, v in stats["tick_ms"].items() if v}
        self.assertEqual(len(per_match), 5)
        for mid, samples in per_match.items():
            self.assertGreater(len(samples), 100, mid)
            self.assertLess(runtime.pct(samples, 99), TICK_BUDGET_MS / 2, mid)
        self.assertLess(runtime.pct(stats["cycle_ms"], 99), TICK_BUDGET_MS)
        # the fixed-rate tick holds 30 Hz on average
        intervals = stats["tick_interval_ms"]
        self.assertLess(abs(sum(intervals) / len(intervals) - TICK_BUDGET_MS), 3.0)
        shots = sum(p.shots_fired for m in server.core.matches.values() for p in m.players)
        self.assertGreater(shots, 20)
        print(f"\n  5x4 in-process: tick p99 {max(runtime.pct(v, 99) for v in per_match.values()):.2f} ms,"
              f" cycle p99 {runtime.pct(stats['cycle_ms'], 99):.2f} ms, interval mean "
              f"{sum(intervals) / len(intervals):.1f} ms, {shots} shots")


class MatchWorkerPool(unittest.IsolatedAsyncioTestCase):
    """The same 5 x 4 load on two worker processes: matches are spread over both, each
    worker answers on its own UDP port, the lobby events come back to the main loop."""

    async def test_pool_spreads_matches_and_relays_events(self):
        from match.pool import MatchPool
        events = []
        results = []
        removed = []

        def on_event(kind, session_id, match_id, result=None):
            events.append((kind, session_id, match_id))
            if result is not None:
                results.append((kind, session_id, result))
        pool = MatchPool(2, "127.0.0.1", 0, match_config(empty_match_timeout_s=1.0),
                         on_event=on_event, stats_interval=0.5)
        pool.on_match_removed = removed.append
        await pool.start(timeout=120)
        loop = asyncio.get_running_loop()
        drivers = []
        try:
            self.assertEqual(len(set(pool.ports)), 2)
            placed = {}
            for k in range(5):
                sids, t = roster_tickets(4, match_id=200 + k, base_sid=2000 + 10 * k)
                fut = loop.create_future()
                pool.place(200 + k, {str(s): v for s, v in t.items()}, fut.set_result)
                placed[200 + k] = (sids, await asyncio.wait_for(fut, 10))
            self.assertEqual(sorted({p for _, p in placed.values()}), sorted(pool.ports))
            rng = random.Random(4)
            for match_id, (sids, port) in placed.items():
                for sid in sids:
                    d = UdpMatchDriver(loop, ("127.0.0.1", port), session_id=sid)
                    d.client.bot = shooter_bot(rng)
                    d.client.connect(d.now())
                    drivers.append(d)
            ok = await pump_drivers(drivers, lambda: all(d.client.controllable for d in drivers), 40)
            self.assertTrue(ok, [d.client.faults for d in drivers if not d.client.controllable][:3])
            pool.stats_pending.clear()
            await pump_drivers(drivers, lambda: False, 4.0)
            stats = list(pool.stats_pending)
            for d in drivers:
                d.client.conn.disconnect()
            await pump_drivers(drivers, lambda: all(d.client.conn.is_disconnected() for d in drivers), 10)
            await pump_drivers(drivers, lambda: len(removed) == 5, 15)
        finally:
            for d in drivers:
                d.close()
            pool.close()
        for d in drivers:
            self.assertEqual(d.client.faults, [])
        connected = {s for kind, s, _ in events if kind == "session_connected"}
        ended = {s for kind, s, _ in events if kind == "session_ended"}
        every = {s for sids, _ in placed.values() for s in sids}
        self.assertEqual(connected, every)
        self.assertEqual(ended, every)
        self.assertEqual(sorted(removed), sorted(placed))
        # the lobby's rewards: every player's result crossed the process boundary on match_finished
        paid = {s: r for kind, s, r in results if kind == "match_finished"}
        self.assertEqual(set(paid), every)
        self.assertTrue(all(not r["finished"] and r["play_s"] > 0 for r in paid.values()))
        ticks = [v for s in stats for vs in s["tick_ms"].values() for v in vs]
        self.assertGreater(len(ticks), 300)
        self.assertLess(runtime.pct(ticks, 99), TICK_BUDGET_MS / 2)
        self.assertEqual({s["index"] for s in stats}, {0, 1})
        for w in pool.workers:
            self.assertFalse(w.process.is_alive())
        print(f"\n  5x4 on 2 workers: tick p99 {runtime.pct(ticks, 99):.2f} ms, worker lag p99 "
              f"{runtime.pct([v for s in stats for v in s['loop_lag_ms']], 99):.1f} ms")


class SlowMatchIsolation(unittest.IsolatedAsyncioTestCase):
    """A match whose every tick takes 80 ms (2.4x the budget) stalls only its worker: the
    main loop (login, lobby, chat) keeps answering lobby pings at once. The same match run
    in-process would hold the main loop for 80 ms every tick."""

    async def lobby_pings(self, port: int, n: int = 25) -> list:
        from mock_client import pk_ping
        c = MockLobbyClient(ClientModel(DICTS))
        await c.connect("127.0.0.1", port, 7)
        await c.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles))
        rtts = []
        for i in range(n):
            t0 = time.perf_counter()
            await c.send(pk_ping(i))
            await c.pump(lambda m: i in m.pings, timeout=5)
            rtts.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.04)
        await c.close()
        return rtts

    async def test_slow_match_does_not_stall_the_lobby(self):
        from match.pool import MatchPool
        gd = ld.load(EXTRACTED, ld.DATA_DIR)
        srv = lobby.LobbyServer(gd, lobby.Store(None, gd, {"money": 1, "premium_money": 1,
                                                           "skill_points": 1}),
                                lobby.Matchmaker("127.0.0.1", 25103, 1, 1.0, None), {7: "tester"})
        server = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
        lobby_port = server.sockets[0].getsockname()[1]
        pool = MatchPool(1, "127.0.0.1", 0, match_config(debug_tick_stall_ms=80),
                         stats_interval=0.5)
        await pool.start(timeout=120)
        loop = asyncio.get_running_loop()
        drivers = []
        try:
            sids, t = roster_tickets(2, match_id=300, base_sid=3000)
            fut = loop.create_future()
            pool.place(300, {str(s): v for s, v in t.items()}, fut.set_result)
            port = await asyncio.wait_for(fut, 10)
            for sid in sids:
                d = UdpMatchDriver(loop, ("127.0.0.1", port), session_id=sid)
                d.client.connect(d.now())
                drivers.append(d)
            pump = asyncio.create_task(pump_drivers(drivers, lambda: False, 60))
            await asyncio.sleep(1.5)                     # the match exists and ticks slowly
            pool.stats_pending.clear()
            lag = runtime.LoopLagMonitor(0.02).start()
            rtts = await self.lobby_pings(lobby_port)
            lag.stop()
            pump.cancel()
            cycles = [v for s in pool.stats_pending for v in s["cycle_ms"]]
        finally:
            for d in drivers:
                d.close()
            pool.close()
            server.close()
            await server.wait_closed()
        self.assertGreater(runtime.pct(cycles, 50), 75.0)           # the match really is slow
        self.assertLess(runtime.pct(rtts, 90), 25.0)                 # the lobby is not
        self.assertLess(runtime.pct(list(lag.samples.recent), 90), 25.0)
        print(f"\n  80 ms match ticks in a worker: lobby ping p50 {runtime.pct(rtts, 50):.1f} ms, "
              f"p90 {runtime.pct(rtts, 90):.1f} ms; main loop lag p90 "
              f"{runtime.pct(list(lag.samples.recent), 90):.1f} ms")


@unittest.skipUnless((SSL_DIR / "survarium_login_server.key").is_file(), "game ssl dir missing")
class ThirtyConcurrentLogins(unittest.IsolatedAsyncioTestCase):
    """30 clients at once through the real server process: TLS login, browser, lobby,
    chat, then Play together; three 10-player matches go to the match worker."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ports = {"login": free_port(), "udp": free_port(socket.SOCK_DGRAM), "http": free_port(),
                      "lobby": free_port(), "chat": free_port(), "match": free_port(socket.SOCK_DGRAM)}
        self.log = open(Path(self.tmp.name) / "server.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "survarium_poc_server.py"), "--ssl-dir", str(SSL_DIR),
             "--host", "127.0.0.1", "--login-port", str(self.ports["login"]),
             "--udp-port", str(self.ports["udp"]), "--http-port", str(self.ports["http"]),
             "--lobby-port", str(self.ports["lobby"]), "--chat-port", str(self.ports["chat"]),
             "--match-server", f"127.0.0.1:{self.ports['match']}", "--match-workers", "1",
             "--match-size", "10", "--matchmaking-delay", "5",
             "--state-dir", str(Path(self.tmp.name) / "state")],
            stdout=self.log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.ports["lobby"]), timeout=0.5):
                    break
            except OSError:
                self.assertIsNone(self.proc.poll(), self.server_log())
                await asyncio.sleep(0.2)
        else:
            self.fail("server did not start\n" + self.server_log())

    def server_log(self) -> str:
        self.log.flush()
        return (Path(self.tmp.name) / "server.log").read_text()

    async def asyncTearDown(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()
        self.tmp.cleanup()

    async def player(self, i: int, results: dict) -> None:
        crt = SSL_DIR / "survarium_login_server.crt"
        t0 = time.perf_counter()
        _, query, sid, _ = await sign_in(self.ports["login"], f"crowd{i:02d}", "pw", crt)
        results.setdefault("login", []).append(time.perf_counter() - t0)
        q = f"{query}&local_ip=127.0.0.1&login_ip=127.0.0.1"
        lobby_addr = await http_get(self.ports["http"], q + "&type=2")
        chat_addr = await http_get(self.ports["http"], q + "&type=4")
        c = MockLobbyClient(ClientModel(DICTS))
        ch = MockChatClient()
        try:
            host, _, port = lobby_addr.partition(":")
            await c.connect(host, int(port), sid)
            await c.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles)
                         and m.skills_tree is not None, timeout=30)
            host, _, port = chat_addr.partition(":")
            await ch.connect(host, int(port), sid, expect_name=f"crowd{i:02d}")
            results.setdefault("ready", []).append(i)
            while len(results["ready"]) < 30:            # everyone presses Play together
                await asyncio.sleep(0.02)
            await c.send(pk_ready(c.m.profiles[0]["profile_id"]))
            await c.pump(lambda m: m.connect_to_match is not None, timeout=30)
            results.setdefault("op51", []).append((sid, c.m.connect_to_match))
            results.setdefault("faults", []).extend(ch.m.faults)
        finally:
            await c.close()
            await ch.close()

    async def test_thirty_concurrent_logins_and_play(self):
        results: dict = {}
        outcome = await asyncio.gather(*(self.player(i, results) for i in range(30)),
                                       return_exceptions=True)
        errors = [o for o in outcome if isinstance(o, BaseException)]
        self.assertEqual(errors, [], self.server_log()[-3000:])
        self.assertEqual(len(results["login"]), 30)
        self.assertEqual(results["faults"], [])
        op51 = results["op51"]
        self.assertEqual(len(op51), 30)
        self.assertEqual({(h, p) for _, (h, p, _, _) in op51}, {("127.0.0.1", self.ports["match"])})
        matches = {m for _, (_, _, m, _) in op51}
        self.assertEqual(len(matches), 3)                # 30 players, match size 10
        log_text = self.server_log()
        self.assertNotIn("Traceback", log_text)
        self.assertNotIn(" ERROR ", log_text)
        logins = sorted(results["login"])
        print(f"\n  30 concurrent logins: p50 {logins[15] * 1000:.0f} ms, max {logins[-1] * 1000:.0f} ms")
        self.assertLess(logins[-1], 5.0)


class Limits(unittest.IsolatedAsyncioTestCase):
    async def test_login_rate_limit_per_ip(self):
        import survarium_poc_server as sps
        login = sps.LoginServer(sps.make_tls_context(SSL_DIR), "127.0.0.1", "/sb?v=1", {},
                                rate=0.01, burst=3)
        server = await asyncio.start_server(login.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            answers = []
            for _ in range(5):
                r, w = await asyncio.open_connection("127.0.0.1", port)
                w.write(bytes([1, 4]) + b"rate" + b"0.100b\0\0")
                await w.drain()
                try:
                    answers.append(await asyncio.wait_for(r.read(1), 5))
                except ConnectionError:             # closed with our bytes unread: a reset
                    answers.append(b"")
                w.close()
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(answers, [b"\x0b"] * 3 + [b""] * 2)   # valid_user_name, then refused
        self.assertEqual(login.refused, 2)

    async def test_trickling_lobby_frame_is_dropped(self):
        old = lobby.FRAME_TIMEOUT_S
        lobby.FRAME_TIMEOUT_S = 0.3
        tmp = tempfile.TemporaryDirectory()
        try:
            gd = ld.load(None, None)
            srv = lobby.LobbyServer(gd, lobby.Store(None, gd, {"money": 1, "premium_money": 1,
                                                               "skill_points": 1}),
                                    lobby.Matchmaker("127.0.0.1", 25103, 1, 1.0, None), {})
            server = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(bytes([5, 38]))                       # a 5-byte frame, 1 byte of it
            await w.drain()
            self.assertEqual(await asyncio.wait_for(r.read(1), 5), b"")   # closed by the server
            self.assertEqual(srv.live_conns, set())
            w.close()
            server.close()
            await server.wait_closed()
        finally:
            lobby.FRAME_TIMEOUT_S = old
            tmp.cleanup()

    async def test_chat_reader_that_stops_reading_is_dropped(self):
        old = chat.MAX_PENDING_BYTES
        chat.MAX_PENDING_BYTES = 64 * 1024
        try:
            srv = chat.ChatServer({1: "talker", 2: "deaf", 3: "listener"})
            server = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            talker, listener = MockChatClient(), MockChatClient()
            await talker.connect("127.0.0.1", port, 1)
            await listener.connect("127.0.0.1", port, 3)

            async def keep_reading():                   # the listener reads all the time
                try:
                    while True:
                        listener.on_packet(await listener.recv_frame(30))
                except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError):
                    pass
            reading = asyncio.create_task(keep_reading())
            r, w = await asyncio.open_connection("127.0.0.1", port)       # never reads
            w.transport.pause_reading()
            w.write(tcp_frame(chat_pk_sign_in(2)))
            await w.drain()
            await asyncio.sleep(0.2)
            for i in range(20000):
                await talker.type_message(f"spam {i} " + "x" * 200)
                if i % 50 == 0:
                    await asyncio.sleep(0.001)
                if all(c.account != "deaf" for c in srv.conns):
                    break
            self.assertTrue(all(c.account != "deaf" for c in srv.conns))      # dropped
            await asyncio.sleep(0.3)
            self.assertTrue(any(c.account == "listener" for c in srv.conns))  # others unaffected
            self.assertGreater(len(listener.m.received), 10)
            self.assertEqual(listener.m.faults, [])
            reading.cancel()
            for c in (talker, listener):
                await c.close()
            w.close()
            server.close()
            await server.wait_closed()
        finally:
            chat.MAX_PENDING_BYTES = old


class FastCodecs(unittest.TestCase):
    """The per-tick fast paths produce exactly the bytes of the original encoders."""

    def test_correction_entries_and_player_update(self):
        from match import messages as M
        rng = random.Random(9)
        for _ in range(200):
            inp = M.PlayerInput((rng.uniform(-9, 9), rng.uniform(-9, 9)),
                                (rng.uniform(-9, 9), rng.uniform(-9, 9)), rng.getrandbits(32))
            st = M.PlayerState(tuple(rng.uniform(-500, 500) for _ in range(3)),
                               rng.uniform(-4, 4), rng.uniform(-1, 1))
            pid, slot, ammo = rng.randrange(20), rng.choice((7, 10)), rng.choice((8, 9, 11, 12, 19))
            old = M.encode_server_player_input(
                1234, [M.CorrectionEntry(pid, inp, st, M.WeaponStateSummary(slot, ammo, 0))])
            new = M.encode_server_player_input_raw(
                1234, [M.encode_correction_entry(pid, inp, st, slot, ammo, 0)])
            self.assertEqual(old, new)
            upd = M.ClientPlayerUpdate(inp, st, rng.getrandbits(32))
            raw = upd.encode()
            r = M.Reader(raw)
            slow = M.ClientPlayerUpdate(M.PlayerInput.read(r), M.PlayerState.read(r), r.u32())
            self.assertEqual(M.ClientPlayerUpdate.decode(raw), slow)


class Bounds(unittest.TestCase):
    def test_unhandshaked_udp_endpoints_are_reaped_and_capped(self):
        sent = []
        core = MatchCore(lambda d, a: sent.append(a), MatchConfig(handshake_timeout_s=5,
                                                                  max_pending_sessions=10))
        for i in range(30):                              # junk from 30 endpoints
            core.datagram_received(b"\x00\x00\xff\xff\x00\x00\x02", ("10.0.0.1", 1000 + i), 1000)
        self.assertEqual(len(core.sessions), 10)
        self.assertEqual(core.pending_dropped, 20)
        core.tick(3000)
        self.assertEqual(len(core.sessions), 10)
        core.tick(6100)                                  # past the handshake timeout
        self.assertEqual(len(core.sessions), 0)
        sent.clear()
        core.tick(7000)
        self.assertEqual(sent, [])                       # no more keep-alives to them

    def test_tickets_and_menu_state_do_not_grow(self):
        gd = ld.load(None, None)
        mm = lobby.Matchmaker("127.0.0.1", 25103, 1, 1.0, None)
        srv = lobby.LobbyServer(gd, lobby.Store(None, gd, {"money": 1, "premium_money": 1,
                                                           "skill_points": 1}), mm, {})
        lobby._TICKETS.clear()
        try:
            for sid in range(50):
                mm.tickets[str(sid)] = {"match_id": 7 if sid < 40 else 8, "issued_at": int(time.time())}
            mm.tickets["999"] = {"match_id": 9, "issued_at": 0}           # ancient
            srv.forget_match(7)
            self.assertEqual(sorted(int(k) for k in mm.tickets), list(range(40, 50)) + [999])
            mm.prune_tickets()
            self.assertNotIn("999", mm.tickets)
            srv.status["gone"] = lobby.PlayStatus()
            srv._forget_idle_status("gone")
            self.assertNotIn("gone", srv.status)
            srv.status["playing"] = lobby.PlayStatus(lobby.IN_MATCH)
            srv._forget_idle_status("playing")
            self.assertIn("playing", srv.status)          # kept: its match is still running
        finally:
            lobby._TICKETS.clear()

    def test_session_table_is_bounded(self):
        import survarium_poc_server as sps
        t = sps.SessionTable(limit=100)
        for sid in range(1000):
            t[sid] = f"acc{sid}"
        self.assertEqual(len(t), 100)
        self.assertEqual(t.get(999), "acc999")
        self.assertIsNone(t.get(5))

    def test_state_saves_are_coalesced(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                path = Path(d) / "lobby_state.json"
                gd = ld.load(None, None)
                store = lobby.Store(path, gd, {"money": 1, "premium_money": 1, "skill_points": 1})
                for i in range(100):                     # a burst of 100 new accounts
                    store.account(f"burst{i}")
                self.assertFalse(path.exists())          # nothing written on the loop yet
                await asyncio.sleep(lobby.Store.SAVE_DELAY_S + 0.5)
                self.assertEqual(store.writer.writes, 1)
                doc = persist.load_json(path)
                self.assertEqual(len(doc["accounts"]), 100)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
