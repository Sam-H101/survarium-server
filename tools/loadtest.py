"""Load test: N simulated v0.100b clients against a real server process.

Every simulated player runs the shipped client's whole path with the test mocks
(tests/mock_client.py, chat_mock_client.py, match_mock_client.py):

    TLS 1.0 login -> UDP keep-alive -> HTTP browser (type 2 and 4) -> lobby sign-in and
    the menu's queries -> chat sign-in -> Play -> op 51 -> UDP match (handshake, spawn,
    move and shoot at the nearest enemy) -> leave -> back in the lobby menu -> Play again

while chatting on the general channel and pinging the lobby (op 40). Players are spread
over several harness processes so the clients themselves are not the bottleneck.

    python tools/loadtest.py --players 50                       (from poc-server/)
    python tools/loadtest.py --players 100 --match-size 20 --rounds 2 --json out.json
    python tools/loadtest.py --players 10 --server-dir <other poc-server copy>

Measured
  client side: login latency (TCP connect to session id), lobby load, lobby ping RTT
               (op 40), chat delivery latency, Play -> op 51, time to controllable, the
               gap between consecutive 0x82 deliveries on every client (30 Hz = 33 ms;
               a stalled match tick shows up here), client faults and stage errors.
  server side: per-match tick time, whole tick cycle, tick interval, event-loop lag
               (main loop and match workers), CPU and memory of all server processes.
               A server with --stats-file reports these itself; an older server is run
               through a probe (`--probe`) that wraps its classes.

Exit status 1 if any client fault, stage error or unfinished player was recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import math
import multiprocessing as mp
import os
import random
import socket
import struct
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TESTS = ROOT / "tests"
SSL_DIR = ROOT.parent / "game" / "resources" / "ssl"
GAME_DATA = ROOT.parent / "game_data"

FIRE = 0x20


# =========================================================================== helpers
def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pct(values, q: float):
    if not values:
        return None
    v = sorted(values)
    k = min(len(v) - 1, max(0, int(math.ceil(q / 100.0 * len(v))) - 1))
    return v[k]


def summary(values, unit_scale=1.0) -> dict:
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50": round(pct(values, 50) * unit_scale, 2),
            "p90": round(pct(values, 90) * unit_scale, 2),
            "p99": round(pct(values, 99) * unit_scale, 2),
            "max": round(max(values) * unit_scale, 2)}


def rss_mb() -> float:
    """Working set (Windows) / max RSS (POSIX) of this process in MB."""
    if sys.platform == "win32":
        class PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                (n, ctypes.c_size_t) for n in (
                    "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / 1048576.0
        return 0.0
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# =========================================================================== probe
def probe_main(server_dir: str, stats_file: str, argv: list[str]) -> None:
    """Run an (older) survarium_poc_server.py with stats wrapped around its classes; writes
    the same JSON lines as the server's own --stats-file."""
    sys.path.insert(0, server_dir)
    os.chdir(server_dir)
    import survarium_poc_server as sps                    # noqa: E402
    from match import game as G, match_state as MS        # noqa: E402

    acc = {"tick": {}, "cycle": [], "interval": [], "dgram_s": 0.0, "lag": []}
    cur: dict = {}
    last_start = [None]
    pc = time.perf_counter

    orig_core_tick, orig_mtick, orig_corr = MS.MatchCore.tick, G.Match.tick, G.Match.send_corrections
    orig_dgram = MS.MatchCore.datagram_received

    def core_tick(self, now_ms):
        t0 = pc()
        if last_start[0] is not None:
            acc["interval"].append((t0 - last_start[0]) * 1000)
        last_start[0] = t0
        cur.clear()
        try:
            return orig_core_tick(self, now_ms)
        finally:
            acc["cycle"].append((pc() - t0) * 1000)
            for mid, dt in cur.items():
                acc["tick"].setdefault(str(mid), []).append(dt * 1000)

    def mtick(self, now):
        t0 = pc()
        try:
            return orig_mtick(self, now)
        finally:
            cur[self.match_id] = cur.get(self.match_id, 0.0) + pc() - t0

    def corr(self):
        t0 = pc()
        try:
            return orig_corr(self)
        finally:
            cur[self.match_id] = cur.get(self.match_id, 0.0) + pc() - t0

    def dgram(self, data, addr, now_ms):
        t0 = pc()
        try:
            return orig_dgram(self, data, addr, now_ms)
        finally:
            acc["dgram_s"] += pc() - t0

    MS.MatchCore.tick, G.Match.tick, G.Match.send_corrections = core_tick, mtick, corr
    MS.MatchCore.datagram_received = dgram

    if os.environ.get("LOADTEST_PROBE_UDP_FIX") and sys.platform == "win32":
        # Only for measuring an old server's performance: give its UDP sockets the
        # SIO_UDP_CONNRESET fix, without which it stops receiving after the first client
        # closes its socket (the Proactor transport stops reading on WSAECONNRESET).
        import asyncio.base_events as BE
        orig_cde = BE.BaseEventLoop.create_datagram_endpoint

        async def cde(self, factory, local_addr=None, remote_addr=None, **kw):
            if local_addr is not None and kw.get("sock") is None and remote_addr is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                flag, ret = ctypes.c_ulong(0), ctypes.c_ulong(0)
                ctypes.windll.ws2_32.WSAIoctl(s.fileno(), 0x9800000C, ctypes.byref(flag), 4, None, 0,
                                              ctypes.byref(ret), None, None)
                s.bind(local_addr)
                s.setblocking(False)
                return await orig_cde(self, factory, sock=s, **kw)
            return await orig_cde(self, factory, local_addr=local_addr, remote_addr=remote_addr, **kw)
        BE.BaseEventLoop.create_datagram_endpoint = cde

    async def lag_sampler():
        while True:
            t0 = pc()
            await asyncio.sleep(0.05)
            acc["lag"].append(max(0.0, (pc() - t0 - 0.05) * 1000))

    async def writer():
        f = open(stats_file, "a", encoding="utf-8")
        t_last = pc()
        while True:
            await asyncio.sleep(1.0)
            t = pc()
            line = {"t": time.time(), "interval_s": t - t_last, "cpu_s": time.process_time(),
                    "rss_mb": rss_mb(), "loop_lag_ms": acc["lag"], "tick_ms": acc["tick"],
                    "cycle_ms": acc["cycle"], "tick_interval_ms": acc["interval"],
                    "dgram_ms": acc["dgram_s"] * 1000, "source": "probe"}
            t_last = t
            acc.update({"tick": {}, "cycle": [], "interval": [], "dgram_s": 0.0, "lag": []})
            f.write(json.dumps(line) + "\n")
            f.flush()

    async def run():
        asyncio.get_running_loop().create_task(lag_sampler())
        asyncio.get_running_loop().create_task(writer())
        await sps.main(argv)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


# =========================================================================== clients
def _client_imports():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(TESTS))
    import warnings
    warnings.simplefilter("ignore", ResourceWarning)
    from chat_mock_client import MockChatClient            # noqa: E402
    from match import ballistics as B, combat as C         # noqa: E402
    from match.game_data import GameData                   # noqa: E402
    from match_mock_client import MockMatchClient          # noqa: E402
    from mock_client import ClientModel, MockLobbyClient, pk_ping, pk_query, pk_ready  # noqa
    from test_e2e import http_get, sign_in                 # noqa: E402
    from test_lobby import DICTS                           # noqa: E402
    return dict(MockChatClient=MockChatClient, B=B, C=C, GameData=GameData,
                MockMatchClient=MockMatchClient, ClientModel=ClientModel,
                MockLobbyClient=MockLobbyClient, pk_ping=pk_ping, pk_query=pk_query,
                pk_ready=pk_ready, http_get=http_get, sign_in=sign_in, DICTS=DICTS)


class Metrics:
    def __init__(self):
        self.lists = {k: [] for k in ("login_ms", "lobby_load_ms", "ping_ms", "chat_ms",
                                      "play_to_op51_ms", "to_controllable_ms",
                                      "corr_gap_ms", "back_to_menu_ms", "leave_ms")}
        self.errors: list[str] = []
        self.faults: list[str] = []
        self.rounds_done = 0
        self.players_done = 0
        self.shots = 0
        self.hits_seen = 0
        self.kills_seen = 0

    def add(self, key, v):
        self.lists[key].append(v)


class _MatchProto(asyncio.DatagramProtocol):
    def __init__(self, player):
        self.p = player

    def datagram_received(self, data, addr):
        p = self.p
        p.last_rx = time.perf_counter()
        c = p.match_client
        if c is None:
            return
        n = len(c.corrections)
        c.datagram_received(data)
        if len(c.corrections) > n and p.measuring:
            # one server tick may need several datagrams (5 entries per 0x82): measure the
            # gap between bursts, i.e. between ticks as the client sees them
            t = time.perf_counter()
            if p.last_corr is not None and t - p.last_corr >= 0.004:
                p.metrics.add("corr_gap_ms", (t - p.last_corr) * 1000)
            p.last_corr = t

    def error_received(self, exc):
        self.p.metrics.errors.append(f"{self.p.account}: udp error {exc!r}")


class Player:
    def __init__(self, idx, cfg, mods, metrics, data, ticker):
        self.idx = idx
        self.cfg = cfg
        self.M = mods
        self.metrics = metrics
        self.data = data
        self.ticker = ticker
        self.account = f"{cfg['prefix']}{idx:03d}"
        self.match_client = None
        self.measuring = False
        self.last_corr = None
        self.ping_sent: dict[int, float] = {}
        self.tasks: list[asyncio.Task] = []
        self.stage = "init"
        self.rng = random.Random(idx)
        self.last_rx = 0.0

    # ---------------------------------------------------------------- lobby / chat
    async def lobby_reader(self, c):
        M = self.M
        try:
            while True:
                payload = await c.recv_frame(timeout=3600)
                if payload[0] == 55:                     # ping answer
                    t = struct.unpack_from("<I", payload, 1)[0]
                    sent = self.ping_sent.pop(t, None)
                    if sent is not None:
                        self.metrics.add("ping_ms", (time.perf_counter() - sent) * 1000)
                    continue
                for action in c.m.on_packet(payload):
                    if isinstance(action, tuple):
                        _, delay, pk = action
                        self.tasks.append(asyncio.ensure_future(c._later(delay, pk)))
                    else:
                        await c.send(action)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except AssertionError as e:
            self.metrics.faults.append(f"{self.account} lobby: {e}")
        except Exception as e:  # noqa: BLE001
            self.metrics.errors.append(f"{self.account} lobby reader: {e!r}")
        _ = M

    async def chat_reader(self, chat):
        try:
            while True:
                payload = await chat.recv_frame(3600)
                n = len(chat.m.received)
                for pk in chat.on_packet(payload):
                    await chat.send(pk)
                for line in chat.m.received[n:]:
                    if line.text.startswith("lt "):
                        try:
                            self.metrics.add("chat_ms", (time.time() - float(line.text[3:])) * 1000)
                        except ValueError:
                            pass
                if len(chat.m.received) > 200:
                    del chat.m.received[:100]
                    del chat.m.lines[:100]
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError):
            pass
        except Exception as e:  # noqa: BLE001
            self.metrics.errors.append(f"{self.account} chat reader: {e!r}")

    async def pinger(self, c):
        t = self.idx * 100000
        try:
            while True:
                await asyncio.sleep(self.cfg["ping_interval"] * (0.75 + 0.5 * self.rng.random()))
                t += 1
                self.ping_sent[t] = time.perf_counter()
                await c.send(self.M["pk_ping"](t))
        except (ConnectionError, asyncio.CancelledError, AttributeError):
            pass

    async def chatter(self, chat):
        try:
            while True:
                await asyncio.sleep(self.cfg["chat_interval"] * (0.5 + self.rng.random()))
                await chat.type_message(f"lt {time.time():.6f}")
        except (ConnectionError, asyncio.CancelledError, AttributeError):
            pass

    @staticmethod
    async def wait_until(cond, timeout, step=0.02) -> bool:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            if cond():
                return True
            await asyncio.sleep(step)
        return bool(cond())

    # ---------------------------------------------------------------- the match
    def make_bot(self):
        B, C = self.M["B"], self.M["C"]
        duty = self.cfg["fire_duty"]
        engage = 15.0 + 15.0 * self.rng.random()
        phase = self.rng.random() * 3.0

        def aim(src, dst):
            dx, dy, dz = dst[0] - src[0], dst[1] - (src[1] + C.EYE_STAND), dst[2] - src[2]
            pitch_deg = math.degrees(math.atan2(dy, math.hypot(dx, dz)))
            return math.atan2(-dx, dz), B.VIEW.look_pitch_for(pitch_deg)

        state = {"spawn": None, "pos": None, "t": None}

        def bot(c, now):
            """Walk towards the nearest living enemy (movement is trusted, so straight
            through the map) until engage range, then strafe and fire bursts at its chest."""
            loc = c.local
            if state["spawn"] != loc["spawn_position"] or state["pos"] is None:
                state["spawn"] = loc["spawn_position"]
                state["pos"] = tuple(loc["spawn_position"])
            dt = 0.033 if state["t"] is None else min(0.2, max(0.0, (now - state["t"]) / 1000.0))
            state["t"] = now
            pos = state["pos"]
            s = now / 1000.0 + phase
            my_team = c.profiles[c.local_id][0].team
            best, bd = None, None
            for pid, r in c.remote.items():
                ins = c.inserted.get(pid)
                if not ins or not ins["alive"] or c.profiles[pid][0].team == my_team:
                    continue
                d = math.dist(pos, r["position"])
                if bd is None or d < bd:
                    best, bd = r, d
            yaw, pitch = loc.get("yaw", 0.0), loc.get("pitch", 0.0)
            actions = 0
            if best is not None:
                tp = best["position"]
                if bd > engage:                            # approach at walking speed
                    k = min(1.0, 3.5 * dt / bd)
                    pos = (pos[0] + (tp[0] - pos[0]) * k, pos[1] + (tp[1] - pos[1]) * k,
                           pos[2] + (tp[2] - pos[2]) * k)
                    actions = 0x1
                else:                                      # strafe
                    side = 1.0 if math.sin(s * 1.3) > 0 else -1.0
                    pos = (pos[0] + side * 1.5 * dt, pos[1], pos[2])
                    actions = 0x4 if side > 0 else 0x8
                yaw, pitch = aim(pos, (tp[0], tp[1] + 1.2, tp[2]))
                if duty > 0 and bd <= 2 * engage and (s % 3.0) < 3.0 * duty:
                    actions |= FIRE
            state["pos"] = pos
            return pos, yaw, pitch, actions
        return bot

    async def play_match(self, sid, host, port):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        now = lambda: int((loop.time() - t0) * 1000) + 1000          # noqa: E731
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sys.platform == "win32":                 # see runtime.udp_socket
            flag, ret = ctypes.c_ulong(0), ctypes.c_ulong(0)
            ctypes.windll.ws2_32.WSAIoctl(sock.fileno(), 0x9800000C, ctypes.byref(flag), 4, None, 0,
                                          ctypes.byref(ret), None, None)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(lambda: _MatchProto(self), sock=sock)
        server_addr = (host, port)

        def send(d):
            try:
                transport.sendto(d, server_addr)
            except OSError:
                pass
        c = self.M["MockMatchClient"](send, sid, self.data, 300)
        c.bot = self.make_bot()
        self.match_client = c
        self.last_corr = None
        c.connect(now())
        self.ticker.add(c, now)
        try:
            t_join = time.perf_counter()
            self.stage = "match join"
            if not await self.wait_until(lambda: c.controllable or c.forbidden or c.finished,
                                         self.cfg["join_timeout"] + 30, 0.05):
                self.metrics.errors.append(f"{self.account}: never controllable "
                                           f"(status {c.game_status}, synced {c.time_synced})")
                return
            if c.forbidden:
                self.metrics.errors.append(f"{self.account}: match connection forbidden")
                return
            self.metrics.add("to_controllable_ms", (time.perf_counter() - t_join) * 1000)
            self.stage = "match play"
            self.measuring = True
            end = time.perf_counter() + self.cfg["play_seconds"]
            while time.perf_counter() < end and not c.finished and not c.conn.is_disconnected():
                await asyncio.sleep(0.25)
                if len(c.log) > 4000:
                    del c.log[:2000]
                    del c.corrections[:1000]
            self.measuring = False
            self.metrics.hits_seen += sum(1 for h in c.hits if h[0] == c.local_id)
            self.metrics.kills_seen += sum(1 for k in c.kills if k[1] == c.local_id and k[0] != k[1])
            self.stage = "match leave"
            t_leave = time.perf_counter()
            if not c.conn.is_disconnected():
                c.conn.disconnect()                       # the user leaves the match
                if not await self.wait_until(c.conn.is_disconnected, 10, 0.02):
                    self.metrics.errors.append(
                        f"{self.account}: leave not confirmed (last datagram "
                        f"{time.perf_counter() - self.last_rx:.1f} s ago, state {c.conn.m_state})")
            self.metrics.add("leave_ms", (time.perf_counter() - t_leave) * 1000)
        finally:
            self.measuring = False
            self.ticker.remove(c)
            self.metrics.faults.extend(f"{self.account} match: {f}" for f in c.faults)
            self.match_client = None
            transport.close()

    # ---------------------------------------------------------------- full flow
    async def run(self):
        M, cfg = self.M, self.cfg
        lobby = chat = None
        try:
            self.stage = "login"
            t = time.perf_counter()
            _, query, sid, _ = await asyncio.wait_for(
                M["sign_in"](cfg["ports"]["login"], self.account, "pw",
                             SSL_DIR / "survarium_login_server.crt"), 60)
            self.metrics.add("login_ms", (time.perf_counter() - t) * 1000)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.sendto(struct.pack("<I", sid), ("127.0.0.1", cfg["ports"]["udp"]))
            self.stage = "browser"
            q = f"{query}&local_ip=127.0.0.1&login_ip=127.0.0.1"
            lobby_addr = await asyncio.wait_for(M["http_get"](cfg["ports"]["http"], q + "&type=2"), 30)
            chat_addr = await asyncio.wait_for(M["http_get"](cfg["ports"]["http"], q + "&type=4"), 30)
            self.stage = "lobby"
            t = time.perf_counter()
            lobby = M["MockLobbyClient"](M["ClientModel"](M["DICTS"]))
            host, _, port = lobby_addr.partition(":")
            await asyncio.wait_for(lobby.connect(host, int(port), sid), 30)
            await lobby.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles)
                             and m.reputations and m.skills_tree is not None, timeout=60)
            self.metrics.add("lobby_load_ms", (time.perf_counter() - t) * 1000)
            self.tasks.append(asyncio.ensure_future(self.lobby_reader(lobby)))
            self.tasks.append(asyncio.ensure_future(self.pinger(lobby)))
            if chat_addr != "x:0":
                self.stage = "chat"
                chat = M["MockChatClient"]()
                host, _, port = chat_addr.partition(":")
                await asyncio.wait_for(chat.connect(host, int(port), sid), 30)
                self.tasks.append(asyncio.ensure_future(self.chat_reader(chat)))
                if cfg["chat_interval"] > 0:
                    self.tasks.append(asyncio.ensure_future(self.chatter(chat)))
            for rnd in range(cfg["rounds"]):
                self.stage = f"play r{rnd}"
                lobby.m.connect_to_match = None
                t = time.perf_counter()
                await lobby.send(M["pk_ready"](lobby.m.profiles[rnd % 3]["profile_id"]))
                if not await self.wait_until(lambda: lobby.m.connect_to_match is not None,
                                             cfg["queue_timeout"], 0.05):
                    self.metrics.errors.append(f"{self.account}: no op 51 in round {rnd} "
                                               f"(state {lobby.m.status}, denied {lobby.m.denied[-1:]})")
                    return
                self.metrics.add("play_to_op51_ms", (time.perf_counter() - t) * 1000)
                mhost, mport, match_id, team = lobby.m.connect_to_match
                if chat is not None:
                    await chat.assign_match_channel_order(match_id, team)
                    chat.m.in_match = True
                    await chat.type_message(f"/all lt {time.time():.6f}")
                await self.play_match(sid, mhost, mport)
                if chat is not None:
                    chat.m.in_match = False
                self.stage = f"menu r{rnd}"
                t = time.perf_counter()
                # the lobby pushes state 0 when the match server reports the session gone
                if not await self.wait_until(lambda: lobby.m.status == 0, 20, 0.05):
                    self.metrics.errors.append(f"{self.account}: lobby still in state "
                                               f"{lobby.m.status} after leaving round {rnd}")
                    return
                self.metrics.add("back_to_menu_ms", (time.perf_counter() - t) * 1000)
                self.metrics.rounds_done += 1
            self.metrics.players_done += 1
            self.stage = "done"
            # stay signed in until the slowest player is done (lobby/chat load stays on)
            await asyncio.sleep(cfg["linger"])
        except asyncio.CancelledError:
            raise
        except AssertionError as e:
            self.metrics.faults.append(f"{self.account} {self.stage}: {e}")
        except Exception as e:  # noqa: BLE001
            self.metrics.errors.append(f"{self.account} {self.stage}: {e!r}")
        finally:
            for tk in self.tasks:
                tk.cancel()
            for x in (lobby, chat):
                if x is not None:
                    try:
                        await asyncio.wait_for(x.close(), 2)
                    except Exception:  # noqa: BLE001
                        pass
            if chat is not None:
                self.metrics.faults.extend(f"{self.account} chat: {f}" for f in chat.m.faults)


class Ticker:
    """One 33 ms loop for every match client of this harness process."""

    def __init__(self):
        self.clients: dict = {}

    def add(self, c, now):
        self.clients[c] = now

    def remove(self, c):
        self.clients.pop(c, None)

    async def run(self):
        while True:
            for c, now in list(self.clients.items()):
                try:
                    c.tick(now())
                except Exception as e:  # noqa: BLE001
                    c.faults.append(f"tick: {e!r}")
            await asyncio.sleep(0.033)


def client_worker(cfg: dict, indices: list[int], out: "mp.Queue") -> None:
    try:
        mods = _client_imports()
        data = mods["GameData"]()
        metrics = Metrics()

        async def main():
            ticker = Ticker()
            asyncio.get_running_loop().create_task(ticker.run())
            await asyncio.sleep(max(0.0, cfg["start_at"] - time.time()))
            tasks = []
            for k, idx in enumerate(indices):
                players = [Player(idx, cfg, mods, metrics, data, ticker)]
                delay = cfg["ramp"] * idx / max(1, cfg["players"])
                tasks.append(asyncio.ensure_future(_delayed(delay, players[0].run())))
            done, pending = await asyncio.wait(tasks, timeout=cfg["deadline"] - time.time())
            for t in pending:
                t.cancel()
            if pending:
                metrics.errors.append(f"{len(pending)} players still running at the deadline")
                await asyncio.gather(*pending, return_exceptions=True)

        asyncio.run(main())
        out.put({"lists": metrics.lists, "errors": metrics.errors, "faults": metrics.faults,
                 "rounds_done": metrics.rounds_done, "players_done": metrics.players_done,
                 "hits": metrics.hits_seen, "kills": metrics.kills_seen})
    except Exception:  # noqa: BLE001
        out.put({"lists": {}, "errors": ["worker crashed: " + traceback.format_exc()], "faults": [],
                 "rounds_done": 0, "players_done": 0, "hits": 0, "kills": 0})


async def _delayed(delay, coro):
    await asyncio.sleep(delay)
    return await coro


# =========================================================================== server
def start_server(args, tmp: Path, ports: dict, stats_file: Path):
    server_dir = Path(args.server_dir).resolve()
    script = server_dir / "survarium_poc_server.py"
    server_args = [
        "--ssl-dir", str(SSL_DIR), "--host", "127.0.0.1",
        "--login-port", str(ports["login"]), "--udp-port", str(ports["udp"]),
        "--http-port", str(ports["http"]), "--lobby-port", str(ports["lobby"]),
        "--chat-port", str(ports["chat"]), "--match-server", f"127.0.0.1:{ports['match']}",
        "--state-dir", str(tmp / "state"), "--game-data", str(GAME_DATA / "extracted"),
        "--match-size", str(args.match_size), "--matchmaking-delay", str(args.fill_timeout),
        "--join-timeout", str(args.join_timeout), "--respawn-time", "3",
        "--match-time", "600"] + list(args.server_arg or [])
    help_text = subprocess.run([sys.executable, str(script), "--help"], capture_output=True,
                               text=True, cwd=str(server_dir)).stdout
    native = "--stats-file" in help_text
    if native:
        cmd = [sys.executable, str(script)] + server_args + [
            "--stats-file", str(stats_file), "--stats-file-interval", "1", "--stats-interval", "10"]
        if "--login-burst" in help_text:          # every simulated player logs in from 127.0.0.1
            cmd += ["--login-burst", str(args.players + 50)]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--probe", str(server_dir),
               str(stats_file), "--"] + server_args
    env = dict(os.environ, SURVARIUM_GAME_DATA=str(GAME_DATA / "json"))
    if args.probe_udp_fix:
        env["LOADTEST_PROBE_UDP_FIX"] = "1"
    log = open(tmp / "server.log", "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(server_dir), env=env)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", ports["lobby"]), timeout=0.5):
                break
        except OSError:
            if proc.poll() is not None:
                raise SystemExit("server exited:\n" + (tmp / "server.log").read_text())
            time.sleep(0.2)
    else:
        raise SystemExit("server did not start")
    return proc, log, native


class StatsReader:
    def __init__(self, path: Path):
        self.path = path
        self.pos = 0
        self.lines: list[dict] = []

    def poll(self):
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as f:
            f.seek(self.pos)
            chunk = f.read()
            if not chunk.endswith("\n"):
                chunk = chunk[:chunk.rfind("\n") + 1]
            self.pos += len(chunk.encode("utf-8"))
        for ln in chunk.splitlines():
            try:
                self.lines.append(json.loads(ln))
            except ValueError:
                pass


def server_report(lines: list[dict], t_from: float, t_to: float) -> dict:
    sel = [x for x in lines if t_from <= x["t"] <= t_to] or lines
    lag, cycle, interval, wlag = [], [], [], []
    ticks: dict[str, list] = {}
    for x in sel:
        lag += x.get("loop_lag_ms", [])
        cycle += x.get("cycle_ms", [])
        interval += x.get("tick_interval_ms", [])
        wlag += x.get("worker_lag_ms", [])
        for k, v in x.get("tick_ms", {}).items():
            ticks.setdefault(k, []).extend(v)
    all_ticks = [v for vs in ticks.values() for v in vs]
    per_match_p99 = sorted(pct(v, 99) for v in ticks.values() if len(v) > 30)
    cpu = None
    if len(sel) >= 2:
        wall = sel[-1]["t"] - sel[0]["t"]
        if wall > 0:
            cpu = round(100.0 * (sel[-1]["cpu_s"] - sel[0]["cpu_s"]) / wall, 1)
    rep = {
        "loop_lag_ms": summary(lag),
        "tick_ms_all_matches": summary(all_ticks),
        "tick_p99_worst_match_ms": round(per_match_p99[-1], 2) if per_match_p99 else None,
        "tick_p99_median_match_ms": round(per_match_p99[len(per_match_p99) // 2], 2) if per_match_p99 else None,
        "matches_measured": len(ticks),
        "cycle_ms": summary(cycle),
        "tick_interval_ms": summary(interval),
        "cpu_percent_of_one_core": cpu,
        "rss_mb_peak": round(max(x.get("rss_mb", 0) for x in sel), 1) if sel else None,
        "server_processes": max((x.get("processes", 1) for x in sel), default=1),
    }
    if wlag:
        rep["worker_loop_lag_ms"] = summary(wlag)
    peaks = [x.get("players") for x in sel if x.get("players")]
    if peaks:
        rep["peak"] = {k: max(p.get(k, 0) for p in peaks) for k in peaks[-1]}
    return rep


def run_loadtest(args) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="survlt_"))
    ports = {"login": free_port(), "udp": free_port(socket.SOCK_DGRAM), "http": free_port(),
             "lobby": free_port(), "chat": free_port(), "match": free_port(socket.SOCK_DGRAM)}
    stats_file = tmp / "stats.jsonl"
    proc, log, native = start_server(args, tmp, ports, stats_file)
    stats = StatsReader(stats_file)
    procs = args.procs or max(1, min(8, (args.players + 24) // 25))
    t_start = time.time() + 2.0 + 0.4 * procs
    deadline = t_start + args.deadline
    cfg = {"ports": ports, "players": args.players, "rounds": args.rounds,
           "play_seconds": args.play_seconds, "join_timeout": args.join_timeout,
           "queue_timeout": args.fill_timeout + 60, "fire_duty": args.fire_duty,
           "chat_interval": args.chat_interval, "ping_interval": 2.0, "ramp": args.ramp,
           "start_at": t_start, "deadline": deadline, "linger": 0.0,
           "prefix": f"lt{os.getpid() % 10000}_"}
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    workers = []
    for w in range(procs):
        idx = list(range(w, args.players, procs))
        p = ctx.Process(target=client_worker, args=(cfg, idx, out), daemon=True)
        p.start()
        workers.append(p)
    print(f"server pid {proc.pid} ({'native stats' if native else 'probe'}), {args.players} players "
          f"in {procs} harness processes, match size {args.match_size}, {args.rounds} round(s) of "
          f"{args.play_seconds:.0f} s; logs in {tmp}", flush=True)
    results = []
    last_print = 0.0
    try:
        while len(results) < procs and time.time() < deadline + 30:
            try:
                results.append(out.get(timeout=1.0))
            except Exception:  # noqa: BLE001  (queue.Empty)
                pass
            stats.poll()
            if stats.lines and time.time() - last_print >= 10 and not args.quiet:
                last_print = time.time()
                x = stats.lines[-1]
                ticks = [v for vs in x.get("tick_ms", {}).values() for v in vs]
                print(f"  t+{time.time() - t_start:5.0f}s  matches {len(x.get('tick_ms', {}))}  "
                      f"tick p99 {pct(ticks, 99) or 0:.1f} ms  lag p99 "
                      f"{pct(x.get('loop_lag_ms', []), 99) or 0:.1f} ms  rss {x.get('rss_mb', 0):.0f} MB"
                      + (f"  {x['players']}" if x.get("players") else ""), flush=True)
            if proc.poll() is not None:
                print("SERVER EXITED", flush=True)
                break
        t_end = time.time()
        time.sleep(1.5)
        stats.poll()
    finally:
        for p in workers:
            p.join(5)
            if p.is_alive():
                p.terminate()
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    lists: dict[str, list] = {}
    errors, faults = [], []
    rounds = players_done = hits = kills = 0
    for r in results:
        for k, v in r["lists"].items():
            lists.setdefault(k, []).extend(v)
        errors += r["errors"]
        faults += r["faults"]
        rounds += r["rounds_done"]
        players_done += r["players_done"]
        hits += r["hits"]
        kills += r["kills"]
    if len(results) < procs:
        errors.append(f"{procs - len(results)} harness processes returned nothing")
    server_log = (tmp / "server.log").read_text(encoding="utf-8", errors="replace")
    tracebacks = server_log.count("Traceback")
    report = {
        "players": args.players, "match_size": args.match_size, "rounds": args.rounds,
        "server": str(Path(args.server_dir).resolve()), "stats": "native" if native else "probe",
        "players_finished": players_done, "rounds_finished": rounds,
        "hits_landed": hits, "kills": kills,
        "client": {k: summary(v) for k, v in lists.items()},
        "server_side": server_report(stats.lines, t_start, t_end),
        "server_log_lines": server_log.count("\n"),
        "server_tracebacks": tracebacks,
        "errors": errors[:40], "error_count": len(errors),
        "faults": faults[:40], "fault_count": len(faults),
        "tmp": str(tmp),
    }
    return report


def print_report(r: dict) -> None:
    c, s = r["client"], r["server_side"]

    def fmt(d):
        if not d or not d.get("n"):
            return "-"
        return f"p50 {d['p50']:.1f}  p99 {d['p99']:.1f}  max {d['max']:.1f}  (n={d['n']})"
    print(f"\n== {r['players']} players, match size {r['match_size']}, {r['rounds']} round(s); "
          f"stats: {r['stats']}")
    print(f"finished {r['players_finished']}/{r['players']} players, {r['rounds_finished']} rounds; "
          f"hits {r['hits_landed']}, kills {r['kills']}; errors {r['error_count']}, "
          f"faults {r['fault_count']}, server tracebacks {r['server_tracebacks']}, "
          f"server log lines {r['server_log_lines']}")
    for k in ("login_ms", "lobby_load_ms", "ping_ms", "chat_ms", "play_to_op51_ms",
              "to_controllable_ms", "corr_gap_ms", "leave_ms", "back_to_menu_ms"):
        print(f"  client {k:20s} {fmt(c.get(k))}")
    print(f"  server tick (all matches) {fmt(s['tick_ms_all_matches'])}; per-match p99: "
          f"median {s['tick_p99_median_match_ms']}, worst {s['tick_p99_worst_match_ms']} "
          f"({s['matches_measured']} matches)")
    print(f"  server tick cycle        {fmt(s['cycle_ms'])}")
    print(f"  server tick interval     {fmt(s['tick_interval_ms'])}")
    print(f"  server loop lag          {fmt(s['loop_lag_ms'])}")
    if "worker_loop_lag_ms" in s:
        print(f"  match worker loop lag    {fmt(s['worker_loop_lag_ms'])}")
    print(f"  server CPU {s['cpu_percent_of_one_core']}% of one core, peak RSS {s['rss_mb_peak']} MB "
          f"in {s['server_processes']} process(es)" + (f"; peak {s['peak']}" if s.get("peak") else ""))
    for e in r["errors"][:10]:
        print("  ERROR", e)
    for f in r["faults"][:10]:
        print("  FAULT", f)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--players", type=int, nargs="+", default=[10],
                    help="simulated players; several values run one test each (e.g. 10 50 100)")
    ap.add_argument("--match-size", type=int, default=10, help="server --match-size (max 20)")
    ap.add_argument("--fill-timeout", type=float, default=5.0, help="server --matchmaking-delay")
    ap.add_argument("--join-timeout", type=float, default=20.0, help="server --join-timeout")
    ap.add_argument("--rounds", type=int, default=2, help="Play -> match -> leave cycles per player")
    ap.add_argument("--play-seconds", type=float, default=20.0, help="seconds in each match")
    ap.add_argument("--fire-duty", type=float, default=0.5, help="fraction of the time bots fire")
    ap.add_argument("--chat-interval", type=float, default=10.0,
                    help="seconds between general-chat lines per player (0 = none)")
    ap.add_argument("--ramp", type=float, default=0.0,
                    help="spread the logins over this many seconds (0 = one burst)")
    ap.add_argument("--procs", type=int, default=0, help="harness processes (default players/25)")
    ap.add_argument("--deadline", type=float, default=600.0, help="abort after this many seconds")
    ap.add_argument("--server-dir", default=str(ROOT), help="poc-server directory to test")
    ap.add_argument("--server-arg", action="append", help="extra server argument (repeatable)")
    ap.add_argument("--probe-udp-fix", action="store_true",
                    help="old servers only: patch the Windows UDP connection-reset stall so their "
                         "performance can be measured past the first client that leaves")
    ap.add_argument("--json", type=Path, help="write the report(s) here")
    ap.add_argument("--quiet", action="store_true")
    return ap


def single_run_args(players: int, **overrides) -> argparse.Namespace:
    """Arguments for one run_loadtest() call (tests/test_load.py)."""
    args = build_parser().parse_args([])
    args.players = players
    for k, v in overrides.items():
        setattr(args, k, v)
    args.match_size = max(1, min(20, args.match_size))
    return args


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--probe":
        sep = argv.index("--")
        probe_main(argv[1], argv[2], argv[sep + 1:])
        return 0
    args = build_parser().parse_args(argv)
    args.match_size = max(1, min(20, args.match_size))
    reports = []
    ok = True
    for n in args.players:
        a = argparse.Namespace(**vars(args))
        a.players = n
        r = run_loadtest(a)
        print_report(r)
        reports.append(r)
        ok = ok and not r["error_count"] and not r["fault_count"] and r["players_finished"] == n
    if args.json:
        args.json.write_text(json.dumps(reports if len(reports) > 1 else reports[0], indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
