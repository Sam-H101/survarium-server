"""Match server tests: transport port, codecs vs. spec vectors, and the M2 sequence end to
end against a mock client, with loss / reorder / duplication.

    python -m unittest tests.test_match -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import heapq
import json
import random
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from match import messages as M  # noqa: E402
from match import seqnum  # noqa: E402
from match.connection import UdpMatchConnection  # noqa: E402
from match.game_data import GameData, default_loadout, make_ticket_lookup  # noqa: E402
from match.match_state import MatchConfig, MatchCore, ProtocolGuardError, Player  # noqa: E402
from match.server import start_match_server  # noqa: E402
from match_mock_client import MockMatchClient  # noqa: E402

DATA = GameData()
CLIENT_ADDR = ("127.0.0.1", 50001)


def h(s: str) -> bytes:
    return bytes.fromhex(s.replace("\n", " "))


# ------------------------------------------------------------------------ harness
class SimNet:
    """Virtual-time network between one MatchCore and N mock clients.

    loss: probability a datagram is dropped; jitter: max extra delay (ms), which reorders;
    dup: probability a datagram is delivered twice.  drop_fn(direction, data, now) can veto
    individual datagrams (direction 'c2s' / 's2c')."""

    def __init__(self, loss=0.0, jitter=0, dup=0.0, seed=1, base_delay=5, config=None,
                 ticket_lookup=None):
        self.rng = random.Random(seed)
        self.loss, self.jitter, self.dup, self.base_delay = loss, jitter, dup, base_delay
        self.now = 1000
        self.queue = []
        self.seq = 0
        self.drop_fn = None
        self.wire_s2c = []      # (addr, bytes) every datagram the server emitted
        self.wire_c2s = []
        cfg = config or MatchConfig(deterministic_spawns=True)
        self.server = MatchCore(self._server_send, cfg, DATA, ticket_lookup,
                                rng=random.Random(seed))
        self.clients = {}

    def add_client(self, session_id=1234, addr=CLIENT_ADDR, load_delay_ms=300):
        c = MockMatchClient(lambda d, a=addr: self._client_send(a, d), session_id, DATA,
                            load_delay_ms)
        self.clients[addr] = c
        c.connect(self.now)
        return c

    def _push(self, kind, addr, data):
        if self.drop_fn and self.drop_fn(kind, data, self.now):
            return
        if self.rng.random() < self.loss:
            return
        copies = 2 if self.rng.random() < self.dup else 1
        for _ in range(copies):
            t = self.now + self.base_delay + (self.rng.randint(0, self.jitter) if self.jitter else 0)
            self.seq += 1
            heapq.heappush(self.queue, (t, self.seq, kind, addr, bytes(data)))

    def _server_send(self, data, addr):
        self.wire_s2c.append((addr, bytes(data)))
        self._push("s2c", addr, data)

    def _client_send(self, addr, data):
        self.wire_c2s.append((addr, bytes(data)))
        self._push("c2s", addr, data)

    def run(self, ms, step=11, until=None):
        end = self.now + ms
        next_server_tick = self.now
        while self.now < end:
            while self.queue and self.queue[0][0] <= self.now:
                _, _, kind, addr, data = heapq.heappop(self.queue)
                if kind == "c2s":
                    self.server.datagram_received(data, addr, self.now)
                elif addr in self.clients:
                    self.clients[addr].datagram_received(data)
            if self.now >= next_server_tick:
                self.server.tick(self.now)
                next_server_tick = self.now + 33
            for c in self.clients.values():
                c.tick(self.now)
            if until and until():
                return True
            self.now += step
        return bool(until and until())


def parse_datagram(d: bytes):
    """Independent wire parser: (seq, ack, bits, [records]) with records = list of bytes
    (None for a low-level record)."""
    seq, ack, bits = struct.unpack_from("<HHH", d)
    body = d[6:]
    if not bits & 1:
        return seq, ack, bits, [("msg", body)]
    out, pos = [], 0
    while pos < len(body):
        n = body[pos]; pos += 1
        out.append(body[pos:pos + n]); pos += n
    if len(out) == 1:
        return seq, ack, bits, [("ll", out[0])]
    return seq, ack, bits, [("msg", r) for r in out]


class WireChecks:
    def check_server_wire(self, wire, addr=CLIENT_ADDR):
        first_order0 = None
        for a, d in wire:
            if a != addr:
                continue
            self.assertLessEqual(len(d), 256)
            seq, ack, bits, recs = parse_datagram(d)
            for kind, r in recs:
                if kind == "ll":
                    self.assertEqual(len(d), 8, "low-level datagram is 8 bytes")
                    self.assertIn(r[0], (0, 1, 2))
                    continue
                self.assertLessEqual(len(r), 250)
                mtype, order = r[0], r[1] | (r[2] << 8)
                if order == 0:
                    first_order0 = first_order0 or mtype
                    self.assertEqual(mtype, 0x80, "order 0 must be 0x80")
                if mtype == 0x80:
                    self.assertEqual(order, 0)
                    self.assertEqual(len(r), 3)
                else:
                    self.assertIn(mtype, M.SENDABLE_AFTER_HANDSHAKE)
        self.assertEqual(first_order0, 0x80)

    def check_client_delivery(self, c: MockMatchClient):
        self.assertEqual(c.faults, [])
        self.assertTrue(c.handshaked)
        self.assertFalse(c.forbidden)


# -------------------------------------------------------------------------- unit
class SeqnumTest(unittest.TestCase):
    def test_compare_and_wrap(self):
        self.assertTrue(seqnum.lt(0xFFFF, 0))
        self.assertTrue(seqnum.lt(0, 1))
        self.assertFalse(seqnum.lt(1, 0))
        self.assertTrue(seqnum.le(5, 5))
        self.assertTrue(seqnum.lt(0xFFF0, 0x0010))
        self.assertEqual(seqnum.diff(2, 0xFFFE), 4)
        self.assertEqual(seqnum.diff(0xFFFE, 2), -4)


class TransportGoldenTest(unittest.TestCase):
    """Datagram bytes from spec section 5 'Example first datagrams'."""

    def test_hello_and_reply(self):
        sent = []
        client = UdpMatchConnection(sent.append)
        p = client.new_packet(0x40)
        p.append(struct.pack("<I", 1234))
        client.connect(p)
        client.send_queued_packets(1000)
        self.assertEqual(sent, [h("00 00 FF FF 00 00 40 00 00 D2 04 00 00")])

        out = []
        core = MatchCore(lambda d, a: out.append(d), MatchConfig(), DATA)
        core.datagram_received(sent[0], CLIENT_ADDR, 1000)
        core.tick(1001)
        self.assertEqual(out, [h("00 00 00 00 00 00 80 00 00")])

    def test_keepalive_and_ack_bits(self):
        sent = []
        c = UdpMatchConnection(sent.append)
        c.connect(None)
        # receive peer datagrams 0..9 with a gap at 7
        for s in range(10):
            if s == 7:
                continue
            c.process_incoming_packet(struct.pack("<HHH", s, 0xFFFF, 1) + bytes((1, 2)),
                                      lambda *a: None)
        c.m_local_sequence_id = 4                     # next datagram is seq 5
        c.send_queued_packets(100)                    # idle -> continuous_flow
        d = sent[-1]
        self.assertEqual(len(d), 8)
        seq, ack, bits = struct.unpack_from("<HHH", d)
        self.assertEqual((seq, ack, d[6:]), (5, 9, b"\x01\x02"))
        self.assertEqual(bits & 1, 1)
        field = (bits >> 1) | 0x8000
        # bit 15-k = ack-k received; 9-2 == 7 is missing
        self.assertEqual(field & (1 << 13), 0)
        self.assertTrue(field & (1 << 14) and field & (1 << 12))

    def test_resend_after_500ms_with_new_sequence(self):
        sent = []
        c = UdpMatchConnection(sent.append)
        c.connect(None)
        c.enqueue(c.new_packet(0x81).append(b"xyz"))
        c.send_queued_packets(1000)
        c.send_queued_packets(1499)
        n = len(sent)
        c.send_queued_packets(1500)
        self.assertEqual(sent[0][6:], sent[n][6:])          # same message
        self.assertNotEqual(sent[0][:2], sent[n][:2])       # new sequence number
        # ack it: no further resends
        seq = struct.unpack_from("<H", sent[n])[0]
        c.process_incoming_packet(struct.pack("<HHH", 0, seq, 1) + bytes((1, 2)), lambda *a: None)
        self.assertEqual(c.unacknowledged_packets_count(), 0)

    def test_never_single_message_in_multi_form_and_bundle_limit(self):
        sent = []
        c = UdpMatchConnection(sent.append)
        c.connect(None)
        for i in range(12):
            c.enqueue(c.new_packet(0x86).append(bytes(40)))
        c.send_queued_packets(1000)
        for d in sent:
            self.assertLessEqual(len(d), 256)
            _, _, bits, recs = parse_datagram(d)
            self.assertTrue(all(k == "msg" for k, _ in recs))
        orders = sorted(r[1] | r[2] << 8 for d in sent for _, r in parse_datagram(d)[3])
        self.assertEqual(orders, list(range(12)))

    def test_oversize_message_refused(self):
        c = UdpMatchConnection(lambda d: None)
        with self.assertRaises(ValueError):
            c.new_packet(0x81).append(bytes(248))


class CodecGoldenTest(unittest.TestCase):
    """Payload vectors from spec 2.6 and 5 (generated by _wsl/mp_examples.py)."""

    def test_match_options(self):
        o = M.MatchOptions(0, "level_03", 2, 1, 0, 10, 600)
        self.assertEqual(M.encode_match_options(o),
                         h("00 08 6C 65 76 65 6C 5F 30 33 02 01 00 0A 58 02"))

    def test_player_profile(self):
        p = M.PlayerProfile("Test", 0, default_loadout())
        body = M.encode_player_profile(p, True)
        self.assertEqual(body, h("""00 01 04 54 65 73 74 00 00
            07 0D 00 01 00 00 00 1E 00
            08 07 00 02 00 00 00 1E 00 5A 00 00 00"""))
        self.assertEqual(len(body), 31)
        back, local = M.decode_player_profile(body)
        self.assertTrue(local)
        # weapon slot 7 is mode 0: amount_in_inventory is not on the wire
        self.assertEqual(back.slots[7], M.ItemInstance(13, 1, 30, 0))
        self.assertEqual(back.slots[8], p.slots[8])

    def test_spawn(self):
        from match.game_data import Ticket
        core = MatchCore(lambda d, a: None, MatchConfig(deterministic_spawns=True), DATA)
        pl = Player(0, Ticket(1, "Test", 0, default_loadout()))
        core._spawn(pl)
        body = core.spawn_message(pl).encode()
        expect = h("""00
            7F 6A F4 C1 AE 47 01 3F 2B 07 A1 C2
            00 00 00 00 00 00 00 00
            01 07 07
            00 00 C8 42 00 00 00 00 00 00 00 00 00
            1E 00 00 00 00 00 01 00 00 00 00 00 00 00 00 1E 00 00 00 00 08
            01 00 00 00 00 00 00 00 00 00 03 00
            1E 00""")
        self.assertEqual(len(body), 72)
        self.assertEqual(body, expect)

    def test_spawn_chambered_two_weapons_and_artefact(self):
        """rem_700 (chamber) active in weapon1, AK in weapon2 (inactive form), lifebone
        artefact (0 bytes) and medkit (u16) in quick slots."""
        from match.game_data import Ticket
        slots = {7: M.ItemInstance(14, 1, 5, 1), 8: M.ItemInstance(51, 2, 10, 40),
                 10: M.ItemInstance(13, 3, 30, 1), 11: M.ItemInstance(7, 4, 30, 90),
                 13: M.ItemInstance(54, 5, 1, 1), 14: M.ItemInstance(67, 6, 3, 3)}
        self.assertIsNone(DATA.validate_loadout(slots))
        core = MatchCore(lambda d, a: None, MatchConfig(deterministic_spawns=True), DATA)
        pl = Player(0, Ticket(1, "T", 0, slots))
        core._spawn(pl)
        body = core.spawn_message(pl).encode()
        # 1+12+8+3+13 header, rem: 21+1+12, ammo 2, ak inactive 21, ammo 2, artefact 0, medkit 2
        self.assertEqual(len(body), 37 + 34 + 2 + 21 + 2 + 0 + 2)

    def test_correction_rules(self):
        with self.assertRaises(ValueError):
            M.encode_server_player_input(5, [])
        e = M.CorrectionEntry(0, M.PlayerInput(), M.PlayerState(), M.WeaponStateSummary())
        self.assertEqual(len(M.encode_server_player_input(5, [e] * 5)), 4 + 5 * 44)
        self.assertEqual(M.MAX_CORRECTIONS_PER_MESSAGE, 5)


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.net = SimNet()
        self.c = self.net.add_client()
        self.net.run(100)
        self.s = self.net.server.sessions[CLIENT_ADDR]

    def test_crash_prone_sends_are_refused(self):
        with self.assertRaises(ProtocolGuardError):
            self.s.send(0x80)                          # second 0x80
        for bad in (0x8D, 0x8E, 0x8F, 0x90, 0x9F, 0xC0, 0x40):
            with self.assertRaises(ProtocolGuardError):
                self.s.send(bad, b"\0\0\0\0")
        with self.assertRaises(ProtocolGuardError):
            self.s.send(0x84, b"\0" * 40)               # before 0x42
        with self.assertRaises(ProtocolGuardError):
            self.s.send(0x82, b"\0\0\0\0")              # empty 0x82
        self.assertEqual(self.c.faults, [])


# ----------------------------------------------------------------- end to end
class M2EndToEndTest(unittest.TestCase, WireChecks):
    def assert_m2(self, net, c, budget_ms=20000):
        ok = net.run(budget_ms, until=lambda: c.controllable and c.inputs_sent >= 30
                     and net.server.players and net.server.players[0].inputs_received >= 30)
        self.assertTrue(ok, f"M2 not reached: faults={c.faults} status={c.game_status} "
                            f"synced={c.time_synced} inserted={list(c.inserted)}")
        self.check_client_delivery(c)
        self.check_server_wire(net.wire_s2c)
        p = net.server.players[0]
        # server tracked the walk (client-authoritative position)
        self.assertGreater(p.position[0], -30.552 + 0.5)
        return p

    def test_m2_sequence_clean_network(self):
        net = SimNet()
        c = net.add_client()
        self.assert_m2(net, c)
        types = [t for _, t, _ in c.log]
        self.assertEqual(types[:7], [0x80, 0x81, 0x92, 0x93, 0x84, 0x9a, 0x8b])
        s = net.server.sessions[CLIENT_ADDR]
        self.assertEqual(s.received_types[:6], [0x40, 0x41, 0x48, 0x42, 0x45, 0x46])
        self.assertTrue(s.synced)
        # periodic resync every ~4 s is answered
        net.run(9000)
        self.assertGreaterEqual(c.sync_responses, 3)
        self.assertEqual(c.faults, [])
        # single player: no 0x82 is ever sent (nothing to correct, never empty)
        self.assertNotIn(0x82, types)
        self.assertEqual(net.server.connected_mask(), 1)

    def test_m2_with_loss(self):
        net = SimNet(loss=0.3, seed=7)
        dropped = []

        def drop_first_options(kind, data, now):
            # also lose the first datagram carrying 0x81 so the server must resend it
            if kind == "s2c" and not dropped and any(
                    k == "msg" and r[0] == 0x81 for k, r in parse_datagram(data)[3]):
                dropped.append(now)
                return True
            return False

        net.drop_fn = drop_first_options
        c = net.add_client()
        self.assert_m2(net, c, budget_ms=60000)
        self.assertTrue(dropped)
        self.assertGreater(net.server.sessions[CLIENT_ADDR].conn.stats["resent_messages"], 0)
        self.assertGreater(c.conn.stats["resent_messages"], 0)

    def test_m2_with_reorder_and_duplicates(self):
        net = SimNet(jitter=250, dup=0.3, seed=3)
        c = net.add_client()
        self.assert_m2(net, c, budget_ms=60000)
        self.assertGreater(c.conn.stats["received_duplicates"]
                           + net.server.sessions[CLIENT_ADDR].conn.stats["received_duplicates"], 0)

    def test_m2_loss_reorder_dup_combined(self):
        for seed in range(5):
            with self.subTest(seed=seed):
                net = SimNet(loss=0.25, jitter=200, dup=0.15, seed=100 + seed)
                c = net.add_client()
                self.assert_m2(net, c, budget_ms=90000)

    def test_blackout_recovery(self):
        """Drop everything the server sends for 3 s right after the profile goes out; the
        reliable layer must still deliver 0x81..0x9a in order."""
        net = SimNet(seed=5)
        state = {"start": None}

        def drop(kind, data, now):
            if kind != "s2c":
                return False
            if state["start"] is None and any(r[:1] == b"\x92" for k, r in parse_datagram(data)[3]
                                              if k == "msg"):
                state["start"] = now
            return state["start"] is not None and now < state["start"] + 3000

        net.drop_fn = drop
        c = net.add_client()
        self.assert_m2(net, c, budget_ms=30000)
        self.assertIsNotNone(state["start"])

    def test_client_disconnect_is_confirmed_and_session_reaped(self):
        net = SimNet()
        c = net.add_client()
        self.assert_m2(net, c)
        c.conn.disconnect()
        net.run(1500)
        self.assertTrue(c.conn.is_disconnected())
        self.assertNotIn(CLIENT_ADDR, net.server.sessions)
        self.assertIsNone(net.server.players[0].session)

    def test_silent_client_times_out(self):
        net = SimNet()
        c = net.add_client()
        self.assert_m2(net, c)
        del net.clients[CLIENT_ADDR]                  # client vanishes
        net.run(121000, step=100)
        self.assertNotIn(CLIENT_ADDR, net.server.sessions)

    def test_two_players_m3_readiness(self):
        net = SimNet()
        a = net.add_client(1, ("127.0.0.1", 50001))
        net.run(3000)
        b = net.add_client(2, ("127.0.0.1", 50002))
        ok = net.run(20000, until=lambda: a.controllable and b.controllable and b.inputs_sent > 30)
        self.assertTrue(ok)
        self.assertEqual(a.faults, [])
        self.assertEqual(b.faults, [])
        self.assertEqual(len(b.profiles), 2)
        self.assertEqual(b.local_id, 1)
        self.assertEqual(sorted(b.inserted), [0, 1])
        # b sees a's movement through 0x82, timed in b's own clock
        self.assertTrue(b.corrections)
        self.assertTrue(all(e["id"] == 0 for _, es in b.corrections for e in es))
        self.assertEqual(net.server.connected_mask(), 0b11)


class TicketTest(unittest.TestCase, WireChecks):
    def test_ticket_file_loadout_and_team(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "match_tickets.json"
            path.write_text(json.dumps({"77": {
                "account": "bob", "profile_name": "Bob", "team": 2, "match_id": 5,
                "loadout": [{"slot": 7, "dict_id": 14, "id": 10, "condition_or_stack": 5, "amount": 1},
                            {"slot": 8, "dict_id": 51, "id": 11, "condition_or_stack": 10, "amount": 40},
                            {"slot": 2, "dict_id": 29, "id": 12, "condition_or_stack": 100, "amount": 1}],
                "issued_at": 0}}), encoding="utf-8")
            net = SimNet(ticket_lookup=make_ticket_lookup(path))
            c = net.add_client(session_id=77)
            ok = net.run(20000, until=lambda: c.controllable and c.inputs_sent > 5)
            self.assertTrue(ok, c.faults)
            self.assertEqual(c.faults, [])
            prof = c.profiles[0][0]
            self.assertEqual((prof.name, prof.team), ("Bob", 1))       # team 2 -> wire 1
            self.assertEqual(prof.slots[7].dict_id, 14)
            # spawned at a team_2 point (ids 0-20)
            pos = c.inserted[0]["spawn_position"]
            t2 = [p.position for p in DATA.respawn_points("level_03") if p.team == 1]
            self.assertIn(tuple(round(x, 3) for x in pos), [tuple(round(x, 3) for x in q) for q in t2])

    def test_lobby_template0_ticket_toz122_chamber_path(self):
        """The lobby's real starter loadout: TOZ-122 (chambered) in slot 7, AK-74u in slot
        10, armour, two ammo stacks, boosters; ticket in the final lobby.py shape."""
        slots = DATA.template_loadout(0)
        self.assertEqual((slots[7].dict_id, slots[10].dict_id), (12, 13))
        ticket = {"account": "alice", "profile_name": "Alice", "team": 2, "team_id": 1,
                  "match_id": 1, "issued_at": 0,
                  "loadout": [{"slot": k, "dict_id": v.dict_id, "id": v.id,
                               "condition_or_stack": v.condition_or_stack,
                               "amount": v.amount_in_inventory} for k, v in slots.items()],
                  "boosters": {"1": 0.05, "4": 0.1}}
        net = SimNet(ticket_lookup=lambda sid: ticket if sid == 31 else None)
        c = net.add_client(session_id=31)
        ok = net.run(20000, until=lambda: c.controllable and c.inputs_sent > 5)
        self.assertTrue(ok, c.faults)
        self.assertEqual(c.faults, [])            # includes the exact 0x84 length check
        prof = c.profiles[0][0]
        self.assertEqual(prof.team, 1)            # team_id used for the wire game_team_id
        spawn = next(p for _, t, p in c.log if t == 0x84)
        # header 37 + TOZ active (21 + chamber 1 + 12) + ammo 2 + AK inactive 21 + ammo 2
        self.assertEqual(len(spawn), 37 + 34 + 2 + 21 + 2)
        self.assertEqual(spawn[37 + 20], 8)       # TOZ ammo_slot
        self.assertEqual(spawn[37 + 21], 1)       # is_round_chambered
        profile = next(p for _, t, p in c.log if t == 0x92)
        self.assertEqual(struct.unpack_from("<H", profile, 3 + len("Alice"))[0], 0b1001)

    def test_weaponless_ticket_falls_back(self):
        lookup = lambda sid: {"profile_name": "NoGun", "team": 1,
                              "loadout": [{"slot": 2, "dict_id": 29, "id": 1,
                                           "condition_or_stack": 100, "amount": 1}]}
        net = SimNet(ticket_lookup=lookup)
        c = net.add_client(session_id=9)
        ok = net.run(20000, until=lambda: c.controllable)
        self.assertTrue(ok)
        self.assertEqual(c.faults, [])
        self.assertEqual(c.profiles[0][0].name, "NoGun")
        self.assertEqual(c.profiles[0][0].slots[7].dict_id, 13)

    def test_unknown_session_rejected_when_configured(self):
        net = SimNet(config=MatchConfig(accept_unknown_sessions=False))
        c = net.add_client(session_id=5)
        net.run(3000)
        self.assertTrue(c.forbidden)
        self.assertFalse(c.handshaked)


class RealUdpTest(unittest.TestCase):
    """The whole M2 sequence over a real UDP socket against start_match_server()."""

    def test_udp_end_to_end(self):
        asyncio.run(self._run())

    async def _run(self):
        loop = asyncio.get_running_loop()
        server = await start_match_server(loop, "127.0.0.1", 0,
                                          MatchConfig(deterministic_spawns=True),
                                          ticket_lookup=lambda sid: None)
        port = server.local_address[1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        senders = set()
        c = MockMatchClient(lambda d: sock.sendto(d, ("127.0.0.1", port)), 4321, DATA, 200)
        t0 = loop.time()
        now = lambda: int((loop.time() - t0) * 1000) + 1000
        c.connect(now())
        try:
            deadline = loop.time() + 15
            while loop.time() < deadline:
                while True:
                    try:
                        data, addr = sock.recvfrom(4096)
                    except (BlockingIOError, ConnectionResetError):
                        break
                    senders.add(addr)
                    c.datagram_received(data)
                c.tick(now())
                if c.controllable and c.inputs_sent >= 30:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
            self.assertEqual(c.faults, [])
            self.assertTrue(c.controllable, f"status={c.game_status} synced={c.time_synced}")
            self.assertEqual(senders, {("127.0.0.1", port)})       # replies from the bound port
            p = server.core.players[0]
            self.assertGreater(p.inputs_received, 10)
            self.assertEqual(p.ticket.name, "Stalker")
        finally:
            sock.close()
            server.close()


if __name__ == "__main__":
    unittest.main()
