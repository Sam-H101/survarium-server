"""Equipped loadout, weapon switching and weapon addons.

* the profile the player picked in the lobby (both weapons, ammo, quick slots, artefacts)
  is what the match server sends in 0x92 / 0x84, also through the real lobby ticket;
* one unusable item no longer replaces the whole loadout with the AK-74u default;
* input bits 0x1000 / 0x2000 switch the active weapon: fire, magazine, reload and ammo
  come from weapon 2's slots (10, 11 / 12), and every other client is told through the
  relayed input bits (the only thing a remote client reads);
* the retail client has no weapon upgrade/attachment protocol (docs/match_protocol.md
  section 13): a scope can neither be bought nor equipped, and cannot reach a match.

    python -m unittest tests.test_weapons -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import json
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
from match.game_data import default_loadout, ticket_from_dict  # noqa: E402
from match.match_state import MatchConfig  # noqa: E402
from match.server import start_match_server  # noqa: E402
from mock_client import ClientModel, MockLobbyClient, pk_buy, pk_move, pk_ready  # noqa: E402
from test_lobby import DICTS, EXTRACTED, LobbyTestBase  # noqa: E402
from test_m3 import FIRE, Lobbyless, hold, m3_config, roster_tickets  # noqa: E402
from test_match import DATA, SimNet  # noqa: E402
from test_reconnect import UdpMatchDriver  # noqa: E402

TOZ, AK, REM700, TT33 = 12, 13, 14, 18
AMMO_762, AMMO_545 = 51, 7
PAINKILLER, TRAP, LIFEBONE, SCOPE = 65, 68, 54, 69
SELECT1, SELECT2 = 0x1000, 0x2000


def two_weapon_loadout():
    """TOZ-122 + 7.62 in weapon1, AK-74u + 5.45 in weapon2, armour, quick slots, artefact."""
    I = M.ItemInstance
    return {
        2: I(29, 1000, 100, 0), 4: I(28, 1001, 100, 0), 5: I(41, 1002, 100, 0), 6: I(36, 1003, 100, 0),
        7: I(TOZ, 1004, 100, 0), 8: I(AMMO_762, 1005, 30, 30),
        10: I(AK, 1006, 100, 0), 11: I(AMMO_545, 1007, 90, 90),
        13: I(PAINKILLER, 1008, 3, 3), 14: I(LIFEBONE, 1009, 100, 1), 15: I(TRAP, 1010, 2, 2),
    }


def duel_world(slots, **cfg):
    w = Lobbyless(2, m3_config(**cfg), slots=slots)
    a, b = w.clients
    assert w.run(20000, until=w.all_controllable)
    return w, a, b


class LoadoutDataTest(unittest.TestCase):
    def test_sanitize_keeps_the_rest_of_the_loadout(self):
        slots = two_weapon_loadout()
        slots[16] = M.ItemInstance(SCOPE, 2000, 1, 1)               # a scope in a quick slot
        slots[17] = M.ItemInstance(9999, 2001, 1, 1)                # unknown dict_id
        slots[9] = M.ItemInstance(AK, 2002, 1, 1)                   # a weapon in an ammo slot
        kept, dropped = DATA.sanitize_loadout(slots)
        self.assertEqual(sorted(kept), sorted(set(two_weapon_loadout())))
        self.assertEqual(len(dropped), 3, dropped)
        self.assertIsNone(DATA.validate_loadout(kept))

    def test_scope_is_not_slottable(self):
        for slot in range(M.MAX_SLOTS):
            self.assertIsNotNone(DATA.slot_problem(slot, M.ItemInstance(SCOPE, 5, 1, 1)), slot)

    def test_ticket_with_a_bad_item_keeps_both_weapons(self):
        slots = two_weapon_loadout()
        sids, tickets = roster_tickets(1, slots=slots)
        tickets[sids[0]]["loadout"].append({"slot": 16, "dict_id": SCOPE, "id": 2000,
                                            "condition_or_stack": 1, "amount": 1})
        net = SimNet(config=m3_config(), ticket_lookup=lambda sid: tickets.get(sid))
        c = net.add_client(sids[0], ("127.0.0.1", 52000))
        self.assertTrue(net.run(20000, until=lambda: c.controllable))
        got = c.profiles[0][0].slots
        self.assertEqual(sorted(got), sorted(slots))
        self.assertEqual(sorted(c.local["weapons"]), [7, 8, 10, 11, 13, 15])   # artefact: no bytes
        self.assertEqual(c.faults, [])

    def test_ticket_without_any_usable_weapon_falls_back_to_default(self):
        sids, tickets = roster_tickets(1, slots={8: M.ItemInstance(AMMO_545, 1, 30, 30)})
        net = SimNet(config=m3_config(), ticket_lookup=lambda sid: tickets.get(sid))
        c = net.add_client(sids[0], ("127.0.0.1", 52001))
        self.assertTrue(net.run(20000, until=lambda: c.controllable))
        self.assertEqual(sorted(c.profiles[0][0].slots), sorted(default_loadout()))


class FullLoadoutTest(unittest.TestCase):
    def test_both_weapons_ammo_and_quick_slots_reach_the_client(self):
        slots = two_weapon_loadout()
        w, a, b = duel_world(slots)
        for c in (a, b):
            self.assertEqual(c.faults, [])
        profile = a.profiles[a.local_id][0]
        self.assertEqual({s: (i.dict_id, i.id, i.condition_or_stack, i.amount_in_inventory)
                          for s, i in profile.slots.items()},
                         {s: (i.dict_id, i.id, i.condition_or_stack, i.amount_in_inventory)
                          for s, i in slots.items()})
        # 0x84: weapon1 is the active one, weapon2 and its ammo are carried as well
        weapons = a.local["weapons"]
        self.assertEqual(weapons[10], 30)                      # AK magazine
        self.assertEqual(weapons[11], 90)
        self.assertEqual(weapons[8], 30)
        self.assertEqual(weapons[7], 5)                        # TOZ-122: 5 + 1 chambered
        self.assertEqual(weapons[13], 3)
        self.assertEqual(weapons[15], 2)
        self.assertNotIn(14, weapons)                          # lifebone writes no bytes
        # b sees a's profile too (remote player), same slots
        self.assertEqual(sorted(b.profiles[0][0].slots), sorted(slots))
        server = w.match.players[0]
        self.assertEqual(server.active_slot, 7)
        self.assertEqual(sorted(server.weapons), [7, 10])
        self.assertEqual((server.weapons[10].ammo_slot, server.weapons[7].ammo_slot), (11, 8))
        self.assertEqual((server.reserve[8], server.reserve[11]), (30, 90))


class NoAmmoWeaponTest(unittest.TestCase):
    def test_weapon_without_ammo_spawns_empty_and_cannot_fire(self):
        """TT-33 in weapon1 and nothing in its ammo slots (a pistol equipped over a rifle
        whose ammo was moved away). The client's weapon_core::activate leaves m_ammunition
        NULL, so any round in the magazine or chamber would reach instant_fire's
        (*m_ammunition).buck_shot() and crash the client."""
        I = M.ItemInstance
        slots = {2: I(31, 1000, 100, 0), 4: I(45, 1001, 100, 0), 5: I(25, 1002, 100, 0),
                 6: I(37, 1003, 100, 0), 7: I(TT33, 1004, 100, 0)}
        w, a, b = duel_world(slots)
        pa = w.match.players[0]
        tt = pa.weapons[7]
        self.assertEqual((tt.ammo_slot, tt.magazine, tt.chambered), (M.INVALID_SLOT, 0, False))
        self.assertEqual(a.local["weapons"][7], 0)            # 0x84 ammo_in_magazine
        b.bot = hold(b.local["position"])
        sky = a.local["position"]
        a.bot = lambda c, now: (sky, 0.0, 1.0, FIRE)
        w.run(3000)
        self.assertEqual((pa.shots_fired, tt.reload_end_ms), (0, None))
        self.assertEqual(a.faults + b.faults, [])


class WeaponSwitchTest(unittest.TestCase):
    def pulse(self, bit, ticks=4):
        """Hold a select key for a few input frames, then nothing."""
        state = {"n": 0}

        def bot(c, now):
            state["n"] += 1
            pos = c.local["position"]
            return pos, 0.0, 0.0, bit if state["n"] <= ticks else 0
        return bot

    def test_switch_to_weapon2_and_back(self):
        w, a, b = duel_world(two_weapon_loadout())
        pa = w.match.players[0]
        a.bot = self.pulse(SELECT2)
        self.assertTrue(w.run(3000, until=lambda: pa.active_slot == 10))
        w.run(500)
        # the other client is told: entry slot id, ammo slot of weapon 2 ...
        self.assertEqual(b.remote[0]["slot"], 10)
        self.assertEqual(b.remote[0]["ammo_slot"], 11)
        # ... and, as it reads only the relayed input bits, the select bit stays on after
        # the key was released
        self.assertTrue(b.remote[0]["actions"] & SELECT2)
        self.assertEqual(b.inserted[0]["shown_slot"], 10)
        a.bot = self.pulse(SELECT1)
        self.assertTrue(w.run(3000, until=lambda: pa.active_slot == 7))
        w.run(500)
        self.assertEqual((b.remote[0]["slot"], b.remote[0]["ammo_slot"]), (7, 8))
        self.assertTrue(b.remote[0]["actions"] & SELECT1)
        self.assertEqual(b.inserted[0]["shown_slot"], 7)
        self.assertEqual(a.faults + b.faults, [])

    def test_no_select_bit_until_a_switch_and_not_for_an_empty_slot(self):
        slots = default_loadout()                              # weapon1 only
        w, a, b = duel_world(slots)
        pa = w.match.players[0]
        a.bot = self.pulse(SELECT2)                            # nothing in slot 10
        w.run(1500)
        self.assertEqual(pa.active_slot, 7)
        self.assertFalse(pa.switched)
        self.assertFalse(b.remote[0]["actions"] & (SELECT1 | SELECT2))
        self.assertEqual(b.inserted[0]["shown_slot"], 7)

    def test_late_joiner_follows_the_switched_player(self):
        w = Lobbyless(3, m3_config(join_timeout_s=3), slots=two_weapon_loadout(), skip=(2,))
        a, b = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        pa = w.match.players[0]
        a.bot = self.pulse(SELECT2)
        self.assertTrue(w.run(3000, until=lambda: pa.active_slot == 10))
        a.bot = hold(a.local["position"])
        late = w.net.add_client(w.sids[2], ("127.0.0.1", 51002), 300)
        self.assertTrue(w.run(20000, until=lambda: late.controllable))
        w.run(500)
        # its 0x84 for player 0 names weapon1 (insert()), the relayed bit moves it to weapon2
        self.assertEqual(late.inserted[0]["shown_slot"], 10)
        self.assertEqual(late.faults, [])

    def test_fire_reload_and_ammo_come_from_weapon2(self):
        w, a, b = duel_world(two_weapon_loadout())
        pa = w.match.players[0]
        b.bot = hold(b.local["position"])
        sky = a.local["position"]
        state = {"n": 0}

        def bot(c, now):
            state["n"] += 1
            bits = SELECT2 if state["n"] <= 4 else (FIRE if state["n"] > 60 else 0)
            return sky, 0.0, 1.0, bits
        a.bot = bot
        self.assertTrue(w.run(15000, until=lambda: pa.weapons[10].reload_end_ms is not None))
        ak, toz = pa.weapons[10], pa.weapons[7]
        self.assertEqual(pa.active_slot, 10)
        self.assertEqual(pa.shots_fired, 30)                   # the AK magazine
        self.assertEqual(ak.magazine, 0)
        self.assertEqual((toz.magazine, toz.chambered), (5, True))   # weapon1 untouched
        self.assertEqual((pa.reserve[8], pa.reserve[11]), (30, 90))
        self.assertTrue(w.run(4000, until=lambda: ak.reload_end_ms is None and ak.magazine))
        self.assertEqual(ak.magazine + (pa.shots_fired - 30), 30)
        self.assertEqual(pa.reserve[11], 60)                   # 30 loaded from the slot-11 stack
        self.assertEqual(pa.reserve[8], 30)                    # weapon1's ammo never moved
        self.assertEqual(a.faults + b.faults, [])

    def test_switch_cancels_reload_and_next_ammo_uses_weapon2_pair(self):
        slots = two_weapon_loadout()
        slots[12] = M.ItemInstance(AMMO_545, 1011, 30, 30)     # second ammo type of weapon2
        w, a, b = duel_world(slots)
        pa = w.match.players[0]
        state = {"n": 0}
        pos = a.local["position"]

        def bot(c, now):
            state["n"] += 1
            n = state["n"]
            if n <= 3:
                return pos, 0.0, 1.0, SELECT2
            if n == 70:
                return pos, 0.0, 1.0, 0x800                    # next ammo type: slot 11 -> 12
            return pos, 0.0, 1.0, 0
        a.bot = bot
        self.assertTrue(w.run(6000, until=lambda: pa.weapons[10].ammo_slot == 12))
        self.assertEqual(pa.weapons[7].ammo_slot, 8)
        w.run(200)
        self.assertEqual(a.faults + b.faults, [])

    def test_kill_with_weapon2_names_its_dict_id(self):
        w, a, b = duel_world(two_weapon_loadout())
        pb = w.match.players[1]
        b.bot = hold(b.local["position"], yaw=3.14159265)
        state = {"n": 0}

        def bot(c, now):
            from test_m3 import aim
            state["n"] += 1
            me = c.local["position"]
            tgt = c.remote.get(b.local_id)
            if tgt is None:
                return me, 0.0, 0.0, 0
            yaw, pitch = aim(me, (tgt["position"][0], tgt["position"][1] + 1.2, tgt["position"][2]))
            return me, yaw, pitch, SELECT2 if state["n"] <= 3 else (FIRE if state["n"] > 40 else 0)
        a.bot = bot
        self.assertTrue(w.run(15000, until=lambda: a.kills))
        self.assertEqual(a.kills[0][3], AK)                    # killed with the weapon-2 AK
        self.assertFalse(pb.alive)
        self.assertEqual(a.faults + b.faults, [])


class LobbyToMatchLoadoutTest(unittest.IsolatedAsyncioTestCase):
    """The loadout equipped in the lobby is the one the real match server sends: through the
    lobby's own ticket, over UDP, with weapon switching on top."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        state = Path(self.tmp.name)
        self.gd = ld.load(EXTRACTED, ld.DATA_DIR)
        self.store = lobby.Store(state / "lobby_state.json", self.gd,
                                 {"money": 50000, "premium_money": 100, "skill_points": 10})
        loop = asyncio.get_running_loop()
        self.match = await start_match_server(loop, "127.0.0.1", 0, MatchConfig(),
                                              ticket_lookup=lobby.get_match_ticket)
        self.port = self.match.local_address[1]
        mm = lobby.Matchmaker("127.0.0.1", self.port, 1, 0.2, state / "match_tickets.json")
        self.lobby = lobby.LobbyServer(self.gd, self.store, mm, {1: "test"}, match_timeout=60.0)
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

    async def test_equipped_profile_reaches_the_match_and_weapons_switch(self):
        c = MockLobbyClient(ClientModel(DICTS))
        await c.connect("127.0.0.1", self.lobby_port, 1)
        self.cleanup.append(c.close)
        await c.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles))
        m = c.m
        pid = m.profiles[0]["profile_id"]
        # equip quick slots from the storage stash
        for dict_id, slot, amount in ((PAINKILLER, 13, 3), (LIFEBONE, 14, 1), (TRAP, 15, 2)):
            item = next(it for it in m.inventory if it["dict_id"] == dict_id)
            n = len(m.permitted)
            await c.send(pk_move([(pid, item["id"], dict_id, ld.STORAGE_SLOT, slot, amount)]))
            await c.pump(lambda m: len(m.permitted) > n)
            await c.pump()
        acc = self.store.doc["accounts"]["test"]
        profile = next(p for p in acc["profiles"] if p["profile_id"] == pid)
        lobby_slots = {int(k): v for k, v in profile["slots"].items()}
        self.assertTrue({7, 8, 10, 11, 13, 14, 15} <= set(lobby_slots), sorted(lobby_slots))

        await c.send(pk_ready(pid))
        await c.pump(lambda m: m.connect_to_match is not None, timeout=5)
        ticket = lobby.get_match_ticket(1)
        self.assertEqual({e["slot"]: e["dict_id"] for e in ticket["loadout"]},
                         {s: v["dict_id"] for s, v in lobby_slots.items()})

        d = UdpMatchDriver(asyncio.get_running_loop(), ("127.0.0.1", self.port), session_id=1,
                           load_delay_ms=150)
        self.cleanup.append(d.close)
        d.client.connect(d.now())
        self.assertTrue(await d.pump(lambda cl: cl.controllable, timeout=20), d.client.faults)
        cl = d.client
        got = cl.profiles[0][0].slots
        self.assertEqual({s: (i.dict_id, i.id) for s, i in got.items()},
                         {s: (v["dict_id"], v["id"]) for s, v in lobby_slots.items()})
        for slot in (7, 8, 10, 11, 13, 15):
            self.assertIn(slot, cl.local["weapons"], slot)
        self.assertEqual(cl.local["weapons"][11], 90)

        # press 2: the server's active weapon changes
        state = {"n": 0}

        def bot(client, now):
            state["n"] += 1
            return client.local["position"], 0.0, 0.0, SELECT2 if state["n"] <= 6 else 0
        cl.bot = bot
        match = next(iter(self.match.core.matches.values()))
        self.assertTrue(await d.pump(lambda _: match.players[0].active_slot == 10, timeout=5))
        self.assertEqual(cl.faults, [])


class WeaponAddonTest(LobbyTestBase):
    """The retail client has no addon protocol: the scope of the rem_700 is baked into its
    weapon config and nothing in the lobby or the profile can attach one."""

    async def test_scope_is_not_sold_and_cannot_be_equipped(self):
        for rows in self.gd.prices.values():
            self.assertNotIn(SCOPE, [d for d, _, _ in rows])
        self.assertFalse(self.gd.equippable(SCOPE))
        c = await self.client()
        m = c.m
        await c.send(pk_buy(SCOPE, 1, 0))
        await c.pump(lambda m: m.denied)
        self.assertEqual(m.denied[0][0], 36)
        pid = m.profiles[0]["profile_id"]
        scope = next(it for it in m.inventory if it["dict_id"] == SCOPE)    # starter stash item
        for slot in (ld.WEAPON1, ld.WEAPON2, ld.AMMO1_W1, 13, 18):
            n = len(m.denied)
            await c.send(pk_move([(pid, scope["id"], SCOPE, ld.STORAGE_SLOT, slot, 1)]))
            await c.pump(lambda m: len(m.denied) > n)
            self.assertEqual(m.denied[-1][0], 35, slot)
        await c.pump()
        self.assertIn(scope["id"], [it["id"] for it in m.inventory])         # still in storage

    def test_only_the_rem700_config_names_a_scope(self):
        raw = Path(__file__).resolve().parents[2] / "game_data" / "json" / "raw" / "gameplay"
        if not raw.is_dir():
            self.skipTest("game data json not present")
        scoped = {}
        for f in sorted((raw / "weapons").glob("*.options.json")):
            cfg = json.loads(f.read_text(encoding="utf-8"))
            if "addons" in cfg:
                scoped[f.name] = cfg["addons"]
        self.assertEqual(scoped, {"rem_700.options.json": {"rifle_scope_dict_id": SCOPE}})
        scope_cfg = json.loads((raw / "items" / "scopes" / "leupold.json").read_text(encoding="utf-8"))
        self.assertEqual(scope_cfg["data"]["type"], 5)           # item_type_rifle_scope
        self.assertEqual(self.gd.items[SCOPE].cfg_name, "gameplay/items/scopes/leupold")
        self.assertEqual(DATA.items[REM700].cfg_name, "gameplay/weapons/rem_700.options")

    def test_scope_in_a_ticket_never_reaches_the_client(self):
        slots = two_weapon_loadout()
        slots[16] = M.ItemInstance(SCOPE, 3000, 1, 1)
        t = ticket_from_dict(1, {"loadout": [{"slot": s, "dict_id": i.dict_id, "id": i.id,
                                              "condition_or_stack": i.condition_or_stack,
                                              "amount": i.amount_in_inventory}
                                             for s, i in slots.items()]})
        kept, dropped = DATA.sanitize_loadout(t.slots)
        self.assertNotIn(16, kept)
        self.assertEqual(len(dropped), 1)


if __name__ == "__main__":
    unittest.main()
