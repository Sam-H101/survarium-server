"""Regression tests for the first real-client run (logs/server-20261004-203206.log):

  op 51 -> match session -> the client drops the lobby TCP and reconnects to the lobby while
  it loads level_03 -> the lobby must answer state 3 (in_match) with the op 51 values, not
  state 0 (which made lobby_menu switch back to the menu mid-load) -> Play again -> a second
  0x40 with the SAME session_id from a NEW port must replace the stale match session.

The lobby runs in-process, the match server on a real UDP socket, both wired the way
survarium_poc_server.py wires them (ticket lookup + on_event).

    python -m unittest tests.test_reconnect -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import lobby  # noqa: E402
import lobby_data as ld  # noqa: E402
from match import messages as M  # noqa: E402
from match.game_data import GameData  # noqa: E402
from match.match_state import MatchConfig  # noqa: E402
from match.server import start_match_server  # noqa: E402
from match_mock_client import MockMatchClient  # noqa: E402
from mock_client import ClientModel, MockLobbyClient, pk_query, pk_ready  # noqa: E402
from test_lobby import DICTS, EXTRACTED  # noqa: E402

DATA = GameData()


class UdpMatchDriver:
    """Drives one MockMatchClient over its own UDP socket (one ephemeral port)."""

    def __init__(self, loop, server_addr, client: MockMatchClient | None = None,
                 session_id: int = 0, load_delay_ms: int = 150):
        self.loop = loop
        self.server_addr = server_addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.client = client or MockMatchClient(self.send, session_id, DATA, load_delay_ms)
        self.t0 = loop.time()

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendto(data, self.server_addr)
        except OSError:
            pass

    def now(self) -> int:
        return int((self.loop.time() - self.t0) * 1000) + 1000

    def poll(self) -> None:
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except (BlockingIOError, ConnectionResetError):
                break
            self.client.datagram_received(data)
        self.client.tick(self.now())

    async def pump(self, until, timeout=10.0) -> bool:
        deadline = self.loop.time() + timeout
        while self.loop.time() < deadline:
            self.poll()
            if until(self.client):
                return True
            await asyncio.sleep(0.01)
        return bool(until(self.client))

    def close(self) -> None:
        self.sock.close()


class LobbyMatchHandoffTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        self.gd = ld.load(EXTRACTED, ld.DATA_DIR)
        store = lobby.Store(state / "lobby_state.json", self.gd,
                            {"money": 50000, "premium_money": 100, "skill_points": 10})
        loop = asyncio.get_running_loop()
        self.match = await start_match_server(loop, "127.0.0.1", 0, MatchConfig(),
                                              ticket_lookup=lobby.get_match_ticket)
        self.match_port = self.match.local_address[1]
        mm = lobby.Matchmaker("127.0.0.1", self.match_port, 1, 0.2, state / "match_tickets.json")
        self.lobby = lobby.LobbyServer(self.gd, store, mm, {1: "test"}, match_timeout=60.0)
        self.match.core.on_event = self.lobby.on_match_event
        self.server = await asyncio.start_server(self.lobby.handle, "127.0.0.1", 0)
        self.lobby_port = self.server.sockets[0].getsockname()[1]
        self.drivers: list[UdpMatchDriver] = []
        self.lobby_clients: list[MockLobbyClient] = []

    async def asyncTearDown(self):
        for c in self.lobby_clients:
            await c.close()
        for d in self.drivers:
            d.close()
        self.match.close()
        self.server.close()
        await self.server.wait_closed()
        self.tmp.cleanup()

    async def lobby_client(self, sid=1) -> MockLobbyClient:
        c = MockLobbyClient(ClientModel(DICTS))
        await c.connect("127.0.0.1", self.lobby_port, sid)
        self.lobby_clients.append(c)
        return c

    def driver(self, **kw) -> UdpMatchDriver:
        d = UdpMatchDriver(asyncio.get_running_loop(), ("127.0.0.1", self.match_port), **kw)
        self.drivers.append(d)
        return d

    async def test_lobby_reconnect_mid_match_then_second_match_connect(self):
        # --- Play -> op 51 ------------------------------------------------------------
        a = await self.lobby_client()
        await a.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles))
        await a.send(pk_ready(a.m.profiles[0]["profile_id"]))
        await a.pump(lambda m: m.connect_to_match is not None and m.order_id != 0xFFFFFFFF,
                     timeout=5)
        host, port, match_id, team = a.m.connect_to_match
        order_id = a.m.order_id
        self.assertEqual(port, self.match_port)

        # --- the client connects to the match server and starts loading ----------------
        m1 = self.driver(session_id=1, load_delay_ms=1500)      # level_03 load takes a while
        m1.client.connect(m1.now())
        self.assertTrue(await m1.pump(lambda c: c.options is not None and len(c.profiles) == 1))

        # --- it drops the lobby TCP and reconnects (sign-in) while loading -------------
        await a.close()
        self.lobby_clients.remove(a)
        b = await self.lobby_client()
        await b.pump(lambda m: m.status != 4, timeout=5)
        await b.pump(timeout=0.3)
        self.assertEqual(b.m.status, lobby.IN_MATCH, b.m.log)
        self.assertEqual((b.m.order_id, b.m.match_id, b.m.team), (order_id, match_id, team))
        self.assertFalse([e for e in b.m.log if e.startswith("54/0 state 0")], b.m.log)
        raw_state = [p for p in b.received if p[:2] == bytes([54, 0])]
        self.assertEqual(raw_state[0], bytes([54, 0, 3]) + struct.pack("<IIB", order_id, match_id, team))

        # --- the match keeps running ----------------------------------------------------
        self.assertTrue(await m1.pump(lambda c: c.controllable and c.inputs_sent >= 10))
        self.assertEqual(m1.client.faults, [])
        await b.send(pk_query(0))                                # a later status poll
        await b.pump(timeout=0.3)
        self.assertEqual(b.m.status, lobby.IN_MATCH)

        # --- second 0x40, same session_id, NEW port, fresh connection -------------------
        m2 = self.driver(session_id=1)
        m2.client.connect(m2.now())
        ok = await m2.pump(lambda c: c.controllable and c.inputs_sent >= 10)
        self.assertTrue(ok, f"faults={m2.client.faults} log={[hex(t) for _, t, _ in m2.client.log]}")
        self.assertEqual(m2.client.faults, [])
        self.assertEqual(m2.client.local_id, 0)
        core = self.match.core
        self.assertEqual(len(core.players), 1)                   # same roster slot
        self.assertEqual(core.players[0].session.addr, ("127.0.0.1", m2.port))
        self.assertNotIn(("127.0.0.1", m1.port), core.sessions)  # stale session dropped
        await b.send(pk_query(0))
        await b.pump(timeout=0.3)
        self.assertEqual(b.m.status, lobby.IN_MATCH)             # replacement is not "left"

        # --- third connect, the retail "Play again mid-match" path: connect() is re-entered
        # --- while the connection is still up, so the 0x40 continues the old sequence and
        # --- order numbering, and match_client_impl stays `handshaked` with the GAME
        # --- dispatcher installed (match_client_impl.cpp:58-63). A 0x80 now would hit the
        # --- unchecked jump table; the server must end that connection instead.
        old_conn = m2.client.conn
        game_layer = MockMatchClient(lambda d: None, 1, DATA, 150)
        game_layer.handshaked = True                             # dispatcher = game handler
        game_layer.options, game_layer.profiles = m2.client.options, m2.client.profiles
        m3 = self.driver(client=game_layer)
        old_conn._send_datagram = m3.send
        m3.client.conn = old_conn
        packet = old_conn.new_packet(M.C_CONNECTION_REQUEST).append(struct.pack("<I", 1))
        old_conn._enqueue_impl(packet)                           # ASSERT(disconnected) is compiled out
        self.assertGreater(packet.order_id, 0)
        old_conn.send_queued_packets(m3.now())
        ok = await m3.pump(lambda c: c.conn.is_disconnected(), timeout=5)
        self.assertTrue(ok, "server did not end the continued connection")
        self.assertEqual(m3.client.faults, [])                   # in particular: no 0x80
        self.assertNotIn(M.S_CONNECTION_SUCCESSFUL, [t for _, t, _ in m3.client.log])
        self.assertIsNone(core.players[0].session)               # slot kept, nobody bound
        self.assertEqual(len(core.players), 1)

        # --- the client then returns to the lobby (state 0) and can play again ---------
        for _ in range(50):
            await b.send(pk_query(0))
            await b.pump(timeout=0.05)
            if b.m.status == lobby.SURF_LOBBY_MENU:
                break
        self.assertEqual(b.m.status, lobby.SURF_LOBBY_MENU)
        m4 = self.driver(session_id=1)
        m4.client.connect(m4.now())
        ok = await m4.pump(lambda c: c.controllable and c.inputs_sent >= 5)
        self.assertTrue(ok, f"faults={m4.client.faults}")
        self.assertEqual(m4.client.faults, [])

        # --- leaving the match for real returns the lobby state to the menu -------------
        self.lobby.status["test"] = lobby.PlayStatus(lobby.IN_MATCH, order_id, match_id, team,
                                                     0.0, "", 0, 1, True)
        m4.client.conn.disconnect()
        await m4.pump(lambda c: c.conn.is_disconnected(), timeout=5)
        for _ in range(50):
            await b.send(pk_query(0))
            await b.pump(timeout=0.05)
            if b.m.status == lobby.SURF_LOBBY_MENU:
                break
        self.assertEqual(b.m.status, lobby.SURF_LOBBY_MENU)

    async def test_unreached_match_still_times_out(self):
        self.lobby.match_timeout = 0.4
        a = await self.lobby_client()
        await a.pump(lambda m: len(m.profiles) == 3)
        await a.send(pk_ready(a.m.profiles[0]["profile_id"]))
        await a.pump(lambda m: m.connect_to_match is not None, timeout=5)
        await a.close()
        self.lobby_clients.remove(a)
        b = await self.lobby_client()                            # re-sign-in keeps state 3 ...
        await b.pump(lambda m: m.status != 4, timeout=5)
        self.assertEqual(b.m.status, lobby.IN_MATCH)
        await asyncio.sleep(0.5)                                 # ... until match_timeout passes
        await b.send(pk_query(0))                                # with no match session seen
        await b.pump(lambda m: m.status == lobby.SURF_LOBBY_MENU, timeout=3)
        self.assertEqual(b.m.status, lobby.SURF_LOBBY_MENU)


if __name__ == "__main__":
    unittest.main()
