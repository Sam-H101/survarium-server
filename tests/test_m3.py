"""M3 tests: fixed-roster lobby matches with 2-4 mock clients in virtual time, remote player
sync, server-authoritative fire/hit/death/respawn, gather_victory_items to the end, packet
loss and reordering, and a real-UDP lobby -> match -> lobby round trip.

    python -m unittest tests.test_m3 -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import lobby  # noqa: E402
import lobby_data as ld  # noqa: E402
from match import ballistics as B  # noqa: E402
from match import combat as C  # noqa: E402
from match import messages as M  # noqa: E402
from match.game import USE_BIT  # noqa: E402
from match.game_data import default_loadout  # noqa: E402
from match.match_state import MatchConfig  # noqa: E402
from match.server import start_match_server  # noqa: E402
from match_mock_client import MockMatchClient  # noqa: E402
from mock_client import ClientModel, MockLobbyClient, pk_query, pk_ready  # noqa: E402
from test_lobby import DICTS, EXTRACTED  # noqa: E402
from test_match import DATA, SimNet, WireChecks  # noqa: E402
from test_reconnect import UdpMatchDriver  # noqa: E402

FIRE = 0x20


def roster_tickets(n, match_id=7, base_sid=100, slots=None):
    sids = [base_sid + i for i in range(n)]
    tickets = {}
    for i, sid in enumerate(sids):
        sl = slots or default_loadout()
        tickets[sid] = {
            "account": f"acc{i}", "profile_name": f"P{i}", "team_id": i % 2, "team": i % 2 + 1,
            "match_id": match_id, "roster": sids,
            "loadout": [{"slot": k, "dict_id": v.dict_id, "id": v.id,
                         "condition_or_stack": v.condition_or_stack,
                         "amount": v.amount_in_inventory} for k, v in sl.items()]}
    return sids, tickets


def m3_config(**kw):
    # protocol/flow tests shoot along the exact view across the map; the ballistics
    # (world collision, dispersion, recoil) have their own tests in test_ballistics.py
    base = dict(deterministic_spawns=True, respawn_time=3, countdown_s=2, join_timeout_s=10,
                match_time=300, victory_items_count=2, end_delay_s=1.0, finish_grace_s=3.0,
                world_collision=False, dispersion=False, recoil=False)
    base.update(kw)
    return MatchConfig(**base)


def aim(src, dst, eye=C.EYE_STAND):
    """(yaw, look_pitch) that point the camera from src's eye at dst; look_pitch goes
    through the look animation (ballistics.VIEW), it is not an angle."""
    dx, dy, dz = dst[0] - src[0], dst[1] - (src[1] + eye), dst[2] - src[2]
    pitch_deg = math.degrees(math.atan2(dy, math.hypot(dx, dz)))
    return math.atan2(-dx, dz), B.VIEW.look_pitch_for(pitch_deg)


def step_towards(pos, dst, dist):
    d = [dst[i] - pos[i] for i in range(3)]
    n = math.sqrt(sum(x * x for x in d))
    if n <= dist:
        return tuple(dst), True
    return tuple(pos[i] + d[i] * dist / n for i in range(3)), False


class Lobbyless:
    """SimNet with a fixed lobby roster and an event log."""

    def __init__(self, n, config=None, loss=0.0, jitter=0, dup=0.0, seed=1, slots=None,
                 stagger_ms=0, skip=()):
        self.sids, self.tickets = roster_tickets(n, slots=slots)
        self.events = []
        self.net = SimNet(loss=loss, jitter=jitter, dup=dup, seed=seed, config=config or m3_config(),
                          ticket_lookup=lambda sid: self.tickets.get(sid))
        self.net.server.on_event = lambda kind, **kw: self.events.append((kind, kw["session_id"]))
        self.clients = []
        for i, sid in enumerate(self.sids):
            if i in skip:
                continue
            if i and stagger_ms:
                self.net.run(stagger_ms)
            self.clients.append(self.net.add_client(sid, ("127.0.0.1", 51000 + i), 300 + 50 * i))

    @property
    def core(self):
        return self.net.server

    @property
    def match(self):
        return next(iter(self.core.matches.values()))

    def run(self, ms, until=None, step=11):
        return self.net.run(ms, step=step, until=until)

    def all_controllable(self):
        return all(c.controllable for c in self.clients)


def hold(position, yaw=0.0, pitch=0.0, actions=0):
    return lambda c, now: (position, yaw, pitch, actions)


class RosterTest(unittest.TestCase, WireChecks):
    def test_two_to_four_players_see_each_other(self):
        for n in (2, 3, 4):
            with self.subTest(players=n):
                w = Lobbyless(n, stagger_ms=1500)       # later joiners connect 1.5 s apart
                first = w.clients[0]
                self.assertTrue(w.run(30000, until=lambda: w.all_controllable() and all(
                    len(c.remote) == n - 1 for c in w.clients)), [c.faults for c in w.clients])
                w.run(1000)
                for i, c in enumerate(w.clients):
                    self.assertEqual(c.faults, [])
                    self.assertEqual(c.options.players_count, n)        # same roster for all
                    self.assertEqual([p.name for p, _ in c.profiles], [f"P{k}" for k in range(n)])
                    self.assertEqual(c.local_id, i)
                    self.assertEqual(sorted(c.inserted), list(range(n)))
                    self.assertEqual(sorted(c.remote), [k for k in range(n) if k != i])
                    self.assertEqual(c.own_corrections, 0)             # never the local player
                    self.check_server_wire(w.net.wire_s2c, ("127.0.0.1", 51000 + i))
                    # every remote is shown where that client actually is
                    for k, r in c.remote.items():
                        actual = w.clients[k].position
                        self.assertLess(math.dist(r["position"], actual), 1.0)
                    # 0x82 time is in the recipient's own clock and never decreases
                    times = [t for t, _ in c.corrections]
                    self.assertEqual(times, sorted(times))
                    self.assertLessEqual(times[-1], c.now)
                    self.assertTrue(all(len(es) <= 5 for _, es in c.corrections))
                # the first joiner waited: waiting_for_players, final countdown, in process
                self.assertEqual(first.statuses[:3], [2, 3, 4])
                self.assertIsNotNone(first.wait_time)
                self.assertEqual(w.core.connected_mask(), (1 << n) - 1)

    def test_twenty_players_split_corrections(self):
        w = Lobbyless(7)
        self.assertTrue(w.run(30000, until=lambda: w.all_controllable() and all(
            len(c.remote) == 6 for c in w.clients)))
        c = w.clients[0]
        self.assertEqual(c.faults, [])
        self.assertTrue(any(len(es) == 5 for _, es in c.corrections))   # 6 others -> 5 + 1

    def test_absent_player_is_in_roster_but_never_inserted(self):
        w = Lobbyless(3, m3_config(join_timeout_s=3), skip=(2,))   # player 2 never connects
        self.assertTrue(w.run(20000, until=lambda: w.clients[0].controllable and w.clients[1].controllable))
        for c in w.clients:
            self.assertEqual(c.faults, [])
            self.assertEqual(c.options.players_count, 3)
            self.assertNotIn(2, c.inserted)          # no 0x84 for an absent player
            self.assertEqual(c.connected_mask & 0b100, 0)

    def test_disconnect_hides_player_and_rejoin_shows_it(self):
        w = Lobbyless(2)
        a, b = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        b.conn.disconnect()
        self.assertTrue(w.run(3000, until=lambda: a.visible.get(1) is False))
        self.assertFalse(a.inserted[1]["alive"])
        self.assertEqual(a.connected_mask & 0b10, 0)
        self.assertIn(("session_ended", w.sids[1]), w.events)
        # the same session_id comes back from a new port and takes its slot again
        del w.net.clients[("127.0.0.1", 51001)]
        b2 = w.net.add_client(w.sids[1], ("127.0.0.1", 51009))
        self.assertTrue(w.run(20000, until=lambda: b2.controllable and a.inserted[1]["alive"]))
        self.assertEqual(a.faults + b2.faults, [])
        self.assertEqual(b2.local_id, 1)
        w.run(500)
        self.assertIn(1, a.remote)


class CombatTest(unittest.TestCase):
    def duel(self, **cfg):
        w = Lobbyless(2, m3_config(**cfg))
        a, b = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        return w, a, b

    def shoot_bot(self, target_client, chest=1.2):
        def bot(c, now):
            me = c.local["position"]
            tgt = c.remote.get(target_client.local_id)
            if tgt is None or not c.inserted[target_client.local_id]["alive"]:
                return me, 0.0, 0.0, 0
            yaw, pitch = aim(me, (tgt["position"][0], tgt["position"][1] + chest, tgt["position"][2]))
            return me, yaw, pitch, FIRE
        return bot

    def test_fire_hit_death_respawn(self):
        w, a, b = self.duel()
        b_spawn = b.local["position"]
        b.bot = hold(b_spawn, yaw=math.pi)               # facing a: body, not back
        a.bot = self.shoot_bot(b)
        self.assertTrue(w.run(10000, until=lambda: a.kills and b.kills))
        a.bot = hold(a.local["position"])
        server_b = w.match.players[1]
        # 3 x 0.4 'injury' on body (1.0 health) kills: exactly three hits, all on the body
        for c in (a, b):
            self.assertEqual(c.faults, [])
            hits = [h for h in c.hits if h[1] == 1]
            self.assertEqual(len(hits), 3, hits)
            self.assertTrue(all(h[0] == 0 and h[2] == "body" and h[3] == "injury" for h in hits))
            self.assertAlmostEqual(hits[0][4], 0.4, places=5)
            self.assertAlmostEqual(hits[0][5], 0.5, places=5)
            self.assertEqual(c.kills[0], (1, 0, False, 13))           # victim, killer, AK dict
            self.assertEqual(c.kd[0], (1, 0))
            self.assertEqual(c.kd[1], (0, 1))
        self.assertFalse(b.local["alive"])
        self.assertFalse(server_b.alive)
        # respawn countdown on the victim, then a new 0x84 at a team spawn point
        self.assertTrue(w.run(6000, until=lambda: b.local["alive"]))
        self.assertEqual(b.inserted[1]["spawns"], 2)
        self.assertEqual(a.inserted[1]["spawns"], 2)
        self.assertEqual(b.respawn_time, 0)
        countdown = [M.Reader(p).u32() for _, t, p in b.log if t == M.S_RESPAWN_TIME_CHANGED]
        self.assertEqual(countdown[:4], [3, 2, 1, 0])
        t2 = [q.position for q in DATA.respawn_points("level_03") if q.team == 1]
        self.assertIn(b.local["spawn_position"], [tuple(map(float, p)) for p in t2] +
                      [b.local["spawn_position"]])
        self.assertTrue(server_b.alive)
        self.assertEqual(server_b.damage.parts["body"].health, server_b.damage.parts["body"].max_health)
        self.assertTrue(w.run(5000, until=lambda: b.controllable))
        self.assertEqual(a.faults + b.faults, [])

    def test_headshot_and_affects(self):
        w, a, b = self.duel()
        b.bot = hold(b.local["position"], yaw=math.pi)   # faces a: the shot hits the face
        a.bot = self.shoot_bot(b, chest=1.62)
        self.assertTrue(w.run(10000, until=lambda: a.kills and b.kills))
        a.bot = hold(a.local["position"])
        self.assertEqual(a.faults + b.faults, [])
        victim_hits = [h for h in a.hits if h[1] == 1]
        self.assertEqual(victim_hits[0][2], "face")        # face 0.3 health: one 0.4 hit
        self.assertEqual(len(victim_hits), 1)
        self.assertTrue(a.kills[0][2])                     # headshot flag
        # face <= 50% applies blindness (8); 0x8a goes to the OTHER clients only, the
        # victim's client applies its own affects (type_apply_directly)
        self.assertIn((1, "face", 8, 0), a.affects)
        self.assertFalse([x for x in b.affects if x[0] == 1])

    def test_friendly_fire_off_and_suicide(self):
        w = Lobbyless(3)                                   # 0 and 2 are both team_1
        a, b, c = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        c.bot = hold(c.local["position"])
        a.bot = self.shoot_bot(c)
        w.run(3000)
        self.assertEqual([h for h in a.hits if h[1] == 2], [])
        self.assertGreater(w.match.players[0].shots_fired, 5)
        a.bot = hold(a.local["position"])
        c.commit_suicide()
        self.assertTrue(w.run(3000, until=lambda: a.kills))
        self.assertEqual(a.kills[0], (2, 2, False, 0))
        self.assertEqual(a.kd[2], (0, 1))
        self.assertTrue(w.run(6000, until=lambda: c.local["alive"]))
        self.assertEqual(a.faults + b.faults + c.faults, [])

    def test_fire_rate_magazine_and_reload(self):
        w, a, b = self.duel()
        b.bot = hold(b.local["position"])
        sky = a.local["position"]
        a.bot = hold(sky, 0.0, 1.0, FIRE)                  # shoot at the sky
        p = w.match.players[0]
        w.run(1500)
        weapon = p.weapons[7]
        fired_1s = p.shots_fired
        # 650 rpm = 92.3 ms per round, after the 0.7 s show delay
        self.assertTrue(5 <= fired_1s <= 11, fired_1s)
        self.assertTrue(w.run(5000, until=lambda: weapon.reload_end_ms is not None))
        self.assertEqual(p.shots_fired, 30)                # the magazine, then reload
        self.assertEqual(weapon.magazine, 0)
        self.assertTrue(w.run(4000, until=lambda: weapon.reload_end_ms is None and weapon.magazine))
        # reloaded to 30 (the held trigger may already have fired the next round)
        self.assertEqual(weapon.magazine + (p.shots_fired - 30), 30)
        self.assertEqual(p.reserve[8], 0)                  # 30 + 30 rounds in the loadout
        w.run(8000)
        self.assertEqual(p.shots_fired, 60)                # nothing left after the second mag
        a.bot = hold(sky)
        w.run(500)
        self.assertEqual(a.faults + b.faults, [])

    def test_single_fire_mode_needs_release(self):
        w, a, b = self.duel()
        b.bot = hold(b.local["position"])
        sky = a.local["position"]
        p = w.match.players[0]
        phase = {"t": 0}

        def bot(c, now):
            phase["t"] += 1
            if phase["t"] == 3:
                return sky, 0.0, 1.0, 0x400            # next fire mode: single
            return sky, 0.0, 1.0, FIRE if phase["t"] > 120 else 0
        a.bot = bot
        w.run(3000)
        self.assertEqual(p.weapons[7].fire_queue_type, 1)
        self.assertEqual(p.shots_fired, 1)                 # held trigger: one round


class VictoryItemsTest(unittest.TestCase):
    def gatherer(self, w, idx, order):
        """Bot: for each (kind, target) in order walk there (30 m/s) and tap use."""
        plan = list(order)
        state = {"tap": 0}
        cont = {c.container_id: c for c in DATA.victory_containers("level_03")}

        def bot(c, now):
            pos = c.local["position"]
            if not plan:
                return pos, 0.0, 0.0, 0
            kind, target = plan[0]
            dst = w.match.items[target].position if kind == "item" else cont[target].position
            pos, arrived = step_towards(pos, dst, 0.33)
            if not arrived:
                return pos, 0.0, 0.0, 0
            state["tap"] += 1
            if state["tap"] == 2:
                return pos, 0.0, 0.0, USE_BIT
            if state["tap"] > 4:
                holding = c.local_id in c.items_held.values()
                done = {"item": c.items_held.get(target) == c.local_id,
                        "steal": holding, "container": not holding}[kind]
                if done:
                    plan.pop(0)
                    state["tap"] = 0
                elif state["tap"] > 30:
                    state["tap"] = 0
            return pos, 0.0, 0.0, 0
        return bot

    def test_full_match_to_the_end(self):
        w = Lobbyless(2)
        a, b = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        for c in (a, b):
            self.assertEqual(c.items_initialized, 1)       # one 0x94 snapshot, after attach
            self.assertEqual(sorted(c.items_world), [0, 1])
            self.assertIsNotNone(c.match_time_ms)
        b.bot = hold(b.local["position"])
        a.bot = self.gatherer(w, 0, [("item", 0), ("container", 1), ("item", 1), ("container", 1)])
        self.assertTrue(w.run(60000, until=lambda: a.finished and b.finished),
                        f"{w.match.state} {a.score} {a.faults}")
        for c in (a, b):
            self.assertEqual(c.faults, [])
            self.assertEqual(c.score, (2, 0))
            self.assertEqual(c.containers[1], [0, 1])
            self.assertEqual(c.match_time_ms, 0)          # "MATCH TIME IS UP"
            self.assertEqual(c.log[-1][1], M.S_MATCH_FINISHED)
        self.assertEqual(w.match.winner, M.TEAM_1)
        self.assertTrue(w.run(15000, until=lambda: not w.core.matches))
        self.assertEqual(sorted(e for e in w.events if e[0] == "match_finished"),
                         [("match_finished", s) for s in w.sids])
        self.assertTrue(all(c.conn.is_disconnected() for c in (a, b)))

    def test_timer_end_draw(self):
        w = Lobbyless(2, m3_config(match_time=4))
        a, b = w.clients
        self.assertTrue(w.run(30000, until=lambda: a.finished and b.finished))
        self.assertEqual(a.faults + b.faults, [])
        self.assertIsNone(w.match.winner)
        seconds = [M.Reader(p).u32() for _, t, p in a.log if t == M.S_MATCH_TIME_CHANGED]
        self.assertEqual(seconds[-1], 0)
        self.assertTrue(all(x >= y for x, y in zip(seconds, seconds[1:])))

    def test_carrier_death_drops_and_steal(self):
        w = Lobbyless(2)
        a, b = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        b.bot = hold(b.local["position"])
        a.bot = self.gatherer(w, 0, [("item", 0)])
        self.assertTrue(w.run(20000, until=lambda: b.items_held.get(0) == 0))
        a.bot = hold(a.local["position"])
        w.run(300)
        # b kills the carrier: the item is dropped where a died
        b.bot = CombatTest.shoot_bot(None, a)
        self.assertTrue(w.run(10000, until=lambda: b.kills and a.kills))
        self.assertIn(0, b.items_world)
        self.assertLess(math.dist(b.items_world[0], w.match.players[0].position), 0.01)
        self.assertNotIn(0, b.items_held)
        b.bot = hold(b.local["position"])
        # a respawns, picks the item up again and stores it (score 1-0)
        self.assertTrue(w.run(8000, until=lambda: a.controllable))
        a.bot = self.gatherer(w, 0, [("item", 0), ("container", 1)])
        self.assertTrue(w.run(30000, until=lambda: b.score == (1, 0)))
        # b steals it from team_1's container (pops the last item) and stores it at home
        b.bot = self.gatherer(w, 1, [("steal", 1), ("container", 0)])
        self.assertTrue(w.run(40000, until=lambda: b.score == (0, 1)), b.score)
        for c in (a, b):
            self.assertEqual(c.faults, [])
            self.assertEqual(c.containers[1], [])
            self.assertEqual(c.containers[0], [0])


class NetworkConditionsTest(unittest.TestCase):
    def test_three_players_fight_with_loss_and_reordering(self):
        for seed in (11, 12):
            with self.subTest(seed=seed):
                w = Lobbyless(3, loss=0.2, jitter=150, dup=0.1, seed=seed)
                a, b, c = w.clients
                self.assertTrue(w.run(60000, until=lambda: w.all_controllable() and all(
                    len(x.remote) == 2 for x in w.clients)), [x.faults for x in w.clients])
                b.bot = hold(b.local["position"])
                c.bot = hold(c.local["position"])
                a.bot = CombatTest.shoot_bot(None, b)
                self.assertTrue(w.run(30000, until=lambda: all(x.kills for x in w.clients)))
                a.bot = hold(a.local["position"])
                w.run(8000)
                kills = [x.kills for x in w.clients]
                self.assertEqual(kills[0], kills[1])
                self.assertEqual(kills[1], kills[2])
                hits = [[h for h in x.hits] for x in w.clients]
                self.assertEqual(hits[0], hits[1])
                self.assertEqual(hits[1], hits[2])
                for x in w.clients:
                    self.assertEqual(x.faults, [])
                    self.assertEqual(x.own_corrections, 0)
                self.assertTrue(b.local["alive"])            # respawned through the loss


class SmoothnessTest(unittest.TestCase):
    """What the client expects for its own player (player_tick.cpp:155-219, 297-393):
    no 0x82 entry about itself (time_warp would rewind and re-simulate its history through
    the physics controller), prompt acks so its reliable 0x43 are never resent, and every
    0x45 answered within a server tick."""

    def test_local_player_is_never_echoed_and_never_resends(self):
        w = Lobbyless(2)
        a, b = w.clients

        def jumper(c, now):
            pos = c.local["position"]
            t = (now // 600) % 2
            return pos, 0.0, 0.0, 0x1 | (0x10 if t else 0)
        a.bot = jumper
        b.bot = jumper
        self.assertTrue(w.run(20000, until=w.all_controllable))
        w.run(15000)
        for c in (a, b):
            self.assertEqual(c.faults, [])
            self.assertEqual(c.own_corrections, 0)
            self.assertEqual(c.conn.stats["resent_messages"], 0)
            self.assertGreater(len(c.corrections), 300)     # the other player at ~30 Hz
        sync = [(t, ty) for t, ty, _ in a.log if ty == M.S_SYNC_RESPONSE]
        self.assertGreaterEqual(len(sync), 4)               # ~ every 4 s


class LobbyRoundTripTest(unittest.IsolatedAsyncioTestCase):
    """Real lobby + real UDP: two players queue, one match forms with a fixed roster and
    alternating teams, both play until the timer ends, the client returns to the lobby
    and the lobby lets them play again."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        gd = ld.load(EXTRACTED, ld.DATA_DIR)
        store = lobby.Store(state / "lobby_state.json", gd,
                            {"money": 50000, "premium_money": 100, "skill_points": 10})
        loop = asyncio.get_running_loop()
        self.match = await start_match_server(
            loop, "127.0.0.1", 0, MatchConfig(match_time=4, countdown_s=1, end_delay_s=0.5,
                                              join_timeout_s=10),
            ticket_lookup=lobby.get_match_ticket)
        port = self.match.local_address[1]
        mm = lobby.Matchmaker("127.0.0.1", port, 40, 30.0, state / "match_tickets.json",
                              match_size=2)
        self.lobby = lobby.LobbyServer(gd, store, mm, {201: "alice", 202: "bob"})
        self.match.core.on_event = self.lobby.on_match_event
        self.server = await asyncio.start_server(self.lobby.handle, "127.0.0.1", 0)
        self.lobby_port = self.server.sockets[0].getsockname()[1]
        self.cleanup = []

    async def asyncTearDown(self):
        for c in self.cleanup:
            r = c()
            if asyncio.iscoroutine(r):
                await r
        self.match.close()
        self.server.close()
        await self.server.wait_closed()
        self.tmp.cleanup()

    async def lobby_client(self, sid):
        c = MockLobbyClient(ClientModel(DICTS))
        await c.connect("127.0.0.1", self.lobby_port, sid)
        self.cleanup.append(c.close)
        await c.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles))
        return c

    async def test_queue_match_end_and_back_to_lobby(self):
        alice, bob = await self.lobby_client(201), await self.lobby_client(202)
        await alice.send(pk_ready(alice.m.profiles[0]["profile_id"]))
        await alice.pump(lambda m: m.status == lobby.IN_MATCH_MAKING)
        await asyncio.sleep(0.2)
        self.assertIsNone(alice.m.connect_to_match)          # waits for a second player
        await bob.send(pk_ready(bob.m.profiles[1]["profile_id"]))
        await bob.pump(lambda m: m.connect_to_match is not None, timeout=5)
        await alice.pump(lambda m: m.connect_to_match is not None, timeout=5)
        ha, pa, mida, ta = alice.m.connect_to_match
        hb, pb, midb, tb = bob.m.connect_to_match
        self.assertEqual(mida, midb)                         # one match, fixed roster
        self.assertNotEqual(ta, tb)                          # teams alternate
        ticket = lobby.get_match_ticket(201)
        self.assertEqual(ticket["roster"], [201, 202])
        loop = asyncio.get_running_loop()
        drivers = []
        for sid in (201, 202):
            d = UdpMatchDriver(loop, ("127.0.0.1", pa), session_id=sid)
            self.cleanup.append(d.close)
            d.client.connect(d.now())
            drivers.append(d)

        async def pump_all(until, timeout):
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                for d in drivers:
                    d.poll()
                if until():
                    return True
                await asyncio.sleep(0.01)
            return until()

        ok = await pump_all(lambda: all(d.client.controllable and len(d.client.remote) == 1
                                        for d in drivers), 20)
        self.assertTrue(ok, [d.client.faults for d in drivers])
        # the lobby keeps both in_match while they play
        await alice.send(pk_query(0))
        await alice.pump(timeout=0.3)
        self.assertEqual(alice.m.status, lobby.IN_MATCH)
        ok = await pump_all(lambda: all(d.client.finished and d.client.conn.is_disconnected()
                                        for d in drivers), 20)
        self.assertTrue(ok)
        for d in drivers:
            self.assertEqual(d.client.faults, [])
            self.assertEqual(d.client.match_time_ms, 0)
        # back in the lobby: state 0 and Play works again
        for c in (alice, bob):
            for _ in range(60):
                await c.send(pk_query(0))
                await c.pump(timeout=0.05)
                if c.m.status == lobby.SURF_LOBBY_MENU:
                    break
            self.assertEqual(c.m.status, lobby.SURF_LOBBY_MENU)
        await alice.send(pk_ready(alice.m.profiles[0]["profile_id"]))
        await alice.pump(lambda m: m.status == lobby.IN_MATCH_MAKING, timeout=5)

    async def test_leave_mid_match_with_lobby_up_gets_menu_pushed(self):
        """Real client case: leaving a match while the lobby TCP stays up. The client only
        sends its deferred discard after a lobby reconnect and never polls state 0, so the
        lobby must push the menu state itself or Play stays dead."""
        alice, bob = await self.lobby_client(201), await self.lobby_client(202)
        await alice.send(pk_ready(alice.m.profiles[0]["profile_id"]))
        await bob.send(pk_ready(bob.m.profiles[1]["profile_id"]))
        await alice.pump(lambda m: m.connect_to_match is not None, timeout=5)
        await bob.pump(lambda m: m.connect_to_match is not None, timeout=5)
        port = alice.m.connect_to_match[1]
        loop = asyncio.get_running_loop()
        drivers = []
        for sid in (201, 202):
            d = UdpMatchDriver(loop, ("127.0.0.1", port), session_id=sid)
            self.cleanup.append(d.close)
            d.client.connect(d.now())
            drivers.append(d)

        async def pump_all(until, timeout):
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                for d in drivers:
                    d.poll()
                if until():
                    return True
                await asyncio.sleep(0.01)
            return until()

        self.assertTrue(await pump_all(lambda: all(d.client.controllable for d in drivers), 20))
        await alice.pump(timeout=0.2)
        self.assertEqual(alice.m.status, lobby.IN_MATCH)
        drivers[0].client.conn.disconnect()                 # alice quits to the lobby
        self.assertTrue(await pump_all(lambda: drivers[0].client.conn.is_disconnected(), 10))
        # no status queries from alice: the menu state must arrive on its own
        await alice.pump(lambda m: m.status == lobby.SURF_LOBBY_MENU, timeout=5)
        self.assertEqual(alice.m.status, lobby.SURF_LOBBY_MENU)
        await alice.send(pk_ready(alice.m.profiles[0]["profile_id"]))
        await alice.pump(lambda m: m.status == lobby.IN_MATCH_MAKING, timeout=5)
        self.assertEqual(alice.m.status, lobby.IN_MATCH_MAKING)


if __name__ == "__main__":
    unittest.main()
