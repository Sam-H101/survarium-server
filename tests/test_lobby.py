"""Lobby tests: a mock of the shipped client (tests/mock_client.py) against LobbyServer.

    python -m unittest discover -s tests -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import binary_config as bc  # noqa: E402
import lobby  # noqa: E402
import lobby_data as ld  # noqa: E402
from mock_client import (ClientModel, MockLobbyClient, pk_buy, pk_discard, pk_move, pk_ping,  # noqa: E402
                         pk_query, pk_ready, pk_reroll, pk_skills)

EXTRACTED = HERE.parent.parent / "game_data" / "extracted"
DICTS_FILE = EXTRACTED / "gameplay" / "db_static_dictionaries"
DICTS = bc.load(DICTS_FILE.read_bytes()) if DICTS_FILE.is_file() else None


class LobbyTestBase(unittest.IsolatedAsyncioTestCase):
    use_game_data = True

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.gd = ld.load(EXTRACTED, ld.DATA_DIR) if self.use_game_data else ld.load(None, None)
        self.sessions = {7: "tester", 8: "other"}
        self.clients: list[MockLobbyClient] = []
        await self.start_server()

    async def start_server(self, match_timeout=60.0):
        store = lobby.Store(self.state / "lobby_state.json", self.gd,
                            {"money": 50000, "premium_money": 100, "skill_points": 10})
        mm = lobby.Matchmaker("127.0.0.1", 25103, 1, 0.2, self.state / "match_tickets.json")
        self.lobby = lobby.LobbyServer(self.gd, store, mm, self.sessions, match_timeout=match_timeout)
        self.server = await asyncio.start_server(self.lobby.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop_server(self):
        for c in self.clients:
            await c.close()
        self.clients.clear()
        self.server.close()
        await self.server.wait_closed()

    async def asyncTearDown(self):
        await self.stop_server()
        self.tmp.cleanup()

    async def client(self, sid=7) -> MockLobbyClient:
        c = MockLobbyClient(ClientModel(DICTS))
        await c.connect("127.0.0.1", self.port, sid)
        await c.pump(lambda m: len(m.profiles) > 0 and all("slots" in p for p in m.profiles)
                     and m.skills_tree is not None and len(m.prices) == 4 and m.leveling is not None
                     and m.reputations)
        self.clients.append(c)
        return c


class TestLobbyEntry(LobbyTestBase):
    async def test_initial_load_matches_client_sequence(self):
        c = await self.client()
        m = c.m
        # exact client sequence: sign in, static info, prices 1..4, state, account data, ...
        self.assertEqual(c.sent[0], bytes([38, 7, 0, 0, 0]))
        self.assertEqual(c.sent[1:5], [pk_query(4), pk_query(5), pk_query(9), pk_query(10)])
        self.assertEqual(c.sent[5:9], [bytes([33, 6, f]) for f in range(1, 5)])
        self.assertEqual(c.sent[9], pk_query(0))
        self.assertEqual(c.sent[10:14], [pk_query(3), pk_query(7), pk_query(8), pk_query(11)])
        self.assertEqual(m.status, lobby.SURF_LOBBY_MENU)
        self.assertEqual(len(m.profiles), 3)
        self.assertEqual(m.nickname, "tester")
        self.assertEqual(m.profiles[0]["name"], "tester")
        self.assertEqual(m.money, 50000)
        self.assertEqual(m.skill_points, 10)
        self.assertEqual(m.service_prices, self.gd.service_prices)
        self.assertTrue(m.restrictions and m.compat and m.inventory)
        self.assertEqual(len(m.reputations), 6)
        p0 = m.profiles[0]
        self.assertEqual(p0["team"], lobby.TEAM_UNDEFINED)
        self.assertIn(ld.WEAPON1, p0["slots"])
        if self.gd.sources.get("player_templates.json") != "built-in":   # profile 0 = player_templates[0]
            self.assertEqual({s: it["dict_id"] for s, it in p0["slots"].items()},
                             {2: 29, 4: 28, 5: 41, 6: 36, 7: 12, 8: 51, 10: 13, 11: 7})
        ammo = p0["slots"][ld.AMMO1_W1]
        self.assertEqual(ammo["cond"], ammo["amount"])       # setup_from_profile takes min()
        # every equipped item obeys the restrictions the client itself enforces
        cats = {d: e["item_category"] for e in (DICTS or {"items_dict": {}})["items_dict"].values()
                for d in [e["dict_id"]]}
        for p in m.profiles:
            for slot, it in p["slots"].items():
                if cats:
                    self.assertIn((slot, cats[it["dict_id"]]), m.restrictions)
        # unique item ids across the account
        ids = [it["id"] for it in m.inventory] + [it["id"] for p in m.profiles for it in p["slots"].values()]
        self.assertEqual(len(ids), len(set(ids)))

    async def test_ping(self):
        c = await self.client()
        await c.send(pk_ping(123456))
        await c.pump(lambda m: m.pings)
        self.assertEqual(c.m.pings, [123456])

    async def test_state_persists_across_restart(self):
        c = await self.client()
        await c.send(pk_buy(66, 3, 5))
        await c.pump(lambda m: m.money != 50000)
        money = c.m.money
        await c.close()
        await self.stop_server()
        await self.start_server()
        c2 = await self.client()
        self.assertEqual(c2.m.money, money)
        doc = json.loads((self.state / "lobby_state.json").read_text())
        self.assertIn("tester", doc["accounts"])


class TestShop(LobbyTestBase):
    async def test_buy_stack_and_single(self):
        c = await self.client()
        m = c.m
        bandage = next(it for it in m.inventory if it["dict_id"] == 65)   # painkiller stack in storage
        before = bandage["cond"]
        faction, cost = next((f, cst) for f, rows in sorted(self.gd.prices.items()) for d, cst, _ in rows if d == 65)
        await c.send(pk_buy(65, 4, faction))
        await c.pump(lambda m: any(p[0] == 36 for p in m.permitted) and m.money < 50000)
        self.assertEqual(bandage["cond"], before + 4)          # merged by instance id, as the client does
        self.assertEqual(m.money, 50000 - 4 * cost)
        n_items = len(m.inventory)
        await c.send(pk_buy(13, 2, 0))                         # faction 0: server finds a trader
        await c.pump(lambda m: len(m.inventory) == n_items + 2)
        await c.pump()
        # the server's storage agrees with the client's incremental view
        await c.send(pk_query(3))
        await c.pump()
        self.assertEqual(len(m.inventory), n_items + 2)

    async def test_buy_denied(self):
        c = await self.client()
        await c.send(pk_buy(13, 1, 0, premium=True))           # 100 premium is not enough
        await c.pump(lambda m: m.denied)
        self.assertEqual(c.m.denied[0][0], 36)
        await c.send(pk_buy(9999, 1, 1))
        await c.pump(lambda m: len(m.denied) == 2)


class TestInventory(LobbyTestBase):
    async def test_equip_unequip_swap(self):
        self.lobby.store.account("tester")["reputation"]["2"] = 800     # Vityaz is earned, not given
        c = await self.client()
        m = c.m
        pr = m.profiles[0]
        pid = pr["profile_id"]
        await c.send(pk_buy(19, 1, 2))
        await c.pump(lambda m: any(it["dict_id"] == 19 for it in m.inventory))
        await c.pump()
        vityaz = next(it for it in m.inventory if it["dict_id"] == 19)
        ak = pr["slots"][ld.WEAPON1]
        # storage -> weapon1: the AK goes back to storage
        await c.send(pk_move([(pid, vityaz["id"], 19, ld.STORAGE_SLOT, ld.WEAPON1, 1)]))
        await c.pump(lambda m: (35, b"") in m.permitted)
        await c.pump()
        self.assertEqual(m.profiles[0]["slots"][ld.WEAPON1]["id"], vityaz["id"])
        self.assertIn(ak["id"], [it["id"] for it in m.inventory])
        # the 5.45 in weapon1's ammo slot no longer fits the Vityaz and goes back to storage;
        # the 9x19 from storage takes its place (attach_ammo: 4 boxes of 50 = the whole stack)
        auto = m.profiles[0]["slots"][ld.AMMO1_W1]
        self.assertEqual((auto["dict_id"], auto["cond"]), (53, 200))
        self.assertNotIn(53, [it["dict_id"] for it in m.inventory])
        self.assertIn(7, [it["dict_id"] for it in m.inventory])
        # slot -> storage splits the stack
        await c.send(pk_move([(pid, auto["id"], 53, ld.AMMO1_W1, ld.STORAGE_SLOT, 60)]))
        await c.pump(lambda m: m.permitted.count((35, b"")) == 2)
        await c.pump()
        self.assertEqual(m.profiles[0]["slots"][ld.AMMO1_W1]["cond"], 140)
        ammo9 = next(it for it in m.inventory if it["dict_id"] == 53)
        self.assertEqual(ammo9["cond"], 60)
        # storage -> the second ammo slot
        await c.send(pk_move([(pid, ammo9["id"], 53, ld.STORAGE_SLOT, ld.AMMO2_W1, 60)]))
        await c.pump(lambda m: m.permitted.count((35, b"")) == 3)
        await c.pump()
        slot = m.profiles[0]["slots"][ld.AMMO2_W1]
        self.assertEqual((slot["dict_id"], slot["cond"], slot["amount"]), (53, 60, 60))
        self.assertNotIn(53, [it["dict_id"] for it in m.inventory])
        # slot -> storage merges the stack back
        await c.send(pk_move([(pid, slot["id"], 53, ld.AMMO2_W1, ld.STORAGE_SLOT, 60)]))
        await c.pump(lambda m: m.permitted.count((35, b"")) == 4)
        await c.pump()
        self.assertNotIn(ld.AMMO2_W1, m.profiles[0]["slots"])
        self.assertEqual(next(it for it in m.inventory if it["dict_id"] == 53)["cond"], 60)

    async def test_equipped_weapon_takes_ammo_from_storage(self):
        """Buying a weapon onto its slot relocates only the weapon (InventoryList's post-buy
        relocation; no PaperDoll autofill), so the server attaches compatible ammo. A move
        batch that already carries ammo (the UI's own autofill) is left as sent."""
        self.lobby.store.account("tester")["reputation"]["2"] = 800
        c = await self.client()
        m = c.m
        pid = m.profiles[0]["profile_id"]
        for dict_id, count in ((19, 1), (71, 2)):
            await c.send(pk_buy(dict_id, count, 2))
            await c.pump(lambda m: any(it["dict_id"] == dict_id for it in m.inventory))
            await c.pump()
        stock = {d: next(it for it in m.inventory if it["dict_id"] == d)["cond"] for d in (53, 71)}
        vityaz = next(it for it in m.inventory if it["dict_id"] == 19)
        await c.send(pk_move([(pid, vityaz["id"], 19, ld.STORAGE_SLOT, ld.WEAPON2, 1)]))
        await c.pump(lambda m: (35, b"") in m.permitted)
        await c.pump()
        slots = m.profiles[0]["slots"]
        got = {s: (slots[s]["dict_id"], slots[s]["cond"]) for s in (ld.AMMO1_W2, ld.AMMO2_W2)}
        self.assertEqual(got, {ld.AMMO1_W2: (53, min(200, stock[53])),
                               ld.AMMO2_W2: (71, min(200, stock[71]))})
        left = {it["dict_id"]: it["cond"] for it in m.inventory if it["dict_id"] in (53, 71)}
        self.assertEqual({d: left.get(d, 0) for d in (53, 71)},
                         {d: stock[d] - min(200, stock[d]) for d in (53, 71)})
        # the 0x23 compatibility table lists (ammo, weapon), as the shop's ammo filter reads it
        self.assertIn((53, 19), self.gd.compatibilities())
        self.assertNotIn((19, 53), self.gd.compatibilities())

    async def equip(self, c, pid, dict_id, slot, amount, permitted=True):
        item = next(it for it in c.m.inventory if it["dict_id"] == dict_id)
        n, d = len(c.m.permitted), len(c.m.denied)
        await c.send(pk_move([(pid, item["id"], dict_id, ld.STORAGE_SLOT, slot, amount)]))
        await c.pump(lambda m: len(m.permitted) > n if permitted else len(m.denied) > d)
        await c.pump()

    def weight(self, pid) -> float:
        conn = next(iter(self.lobby.live_conns))
        return conn.profile_weight(next(p for p in self.lobby.store.account("tester")["profiles"]
                                        if p["profile_id"] == pid))

    @unittest.skipUnless(DICTS, "game data missing")
    async def test_weight_limit(self):
        """player_parameters_modifyer_cook weighs count x weight per equipped slot against
        default.player's max_carried_weight (30 kg); the client only paints it red, the server
        denies it with --weight-limit."""
        self.assertEqual(self.gd.max_carried_weight, 30.0)
        self.lobby.weight_limit = True
        c = await self.client()
        pid = c.m.profiles[0]["profile_id"]
        self.assertAlmostEqual(self.weight(pid), 14.19, places=2)          # the starter loadout
        await self.equip(c, pid, 65, 13, 3)                                  # 3 painkillers, 5 kg each
        self.assertAlmostEqual(self.weight(pid), 29.19, places=2)
        await self.equip(c, pid, 54, 14, 1, permitted=False)                 # a 5 kg artefact: too heavy
        self.assertEqual(c.m.denied[-1][0], 35)
        self.assertIn("too heavy", c.m.denied[-1][1])
        self.assertNotIn(14, c.m.profiles[0]["slots"])
        self.assertTrue(any(it["dict_id"] == 54 for it in c.m.inventory))    # rolled back
        self.lobby.weight_limit = False                                      # default: allowed
        await self.equip(c, pid, 54, 14, 1)
        self.assertIn(14, c.m.profiles[0]["slots"])
        self.lobby.weight_limit = True                                       # lightening is fine
        pk = c.m.profiles[0]["slots"][13]
        n = len(c.m.permitted)
        await c.send(pk_move([(pid, pk["id"], 65, 13, ld.STORAGE_SLOT, 3)]))
        await c.pump(lambda m: len(m.permitted) > n)
        self.assertAlmostEqual(self.weight(pid), 19.19, places=2)

    @unittest.skipUnless(DICTS, "game data missing")
    async def test_attached_ammo_fits_the_weight(self):
        """PaperDollSlot.tryFillAmmo: whole clips in half the weight still free."""
        self.lobby.store.account("tester")["reputation"]["2"] = 800
        c = await self.client()
        pid = c.m.profiles[0]["profile_id"]
        for dict_id, count in ((19, 1), (71, 200)):
            await c.send(pk_buy(dict_id, count, 2))
            await c.pump(lambda m: any(it["dict_id"] == dict_id for it in m.inventory))
            await c.pump()
        await self.equip(c, pid, 65, 13, 3)                                  # 29.19 kg
        await self.equip(c, pid, 19, ld.WEAPON2, 1)                          # Vityaz for the AK-74u
        slots = c.m.profiles[0]["slots"]
        # 27.89 kg: 2.11 free -> 2 clips of 9x19 (0.4 kg); then 1.31 free -> 1 clip of HP
        got = {s: (slots[s]["dict_id"], slots[s]["cond"]) for s in (ld.AMMO1_W2, ld.AMMO2_W2)}
        self.assertEqual(got, {ld.AMMO1_W2: (53, 100), ld.AMMO2_W2: (71, 50)})
        self.assertLessEqual(self.weight(pid), 30.0)

    async def test_ammo_moved_with_the_weapon_is_not_doubled(self):
        self.lobby.store.account("tester")["reputation"]["2"] = 800
        c = await self.client()
        m = c.m
        pid = m.profiles[0]["profile_id"]
        await c.send(pk_buy(19, 1, 2))
        await c.pump(lambda m: any(it["dict_id"] == 19 for it in m.inventory))
        await c.pump()
        vityaz = next(it for it in m.inventory if it["dict_id"] == 19)
        ammo9 = next(it for it in m.inventory if it["dict_id"] == 53)
        await c.send(pk_move([(pid, vityaz["id"], 19, ld.STORAGE_SLOT, ld.WEAPON2, 1),
                              (pid, ammo9["id"], 53, ld.STORAGE_SLOT, ld.AMMO1_W2, 30)]))
        await c.pump(lambda m: (35, b"") in m.permitted)
        await c.pump()
        slots = m.profiles[0]["slots"]
        self.assertEqual((slots[ld.AMMO1_W2]["dict_id"], slots[ld.AMMO1_W2]["cond"]), (53, 30))
        self.assertNotIn(ld.AMMO2_W2, slots)

    async def test_move_denied(self):
        c = await self.client()
        m = c.m
        pid = m.profiles[0]["profile_id"]
        torso = next(it for it in m.inventory if it["dict_id"] == 32)
        await c.send(pk_move([(pid, torso["id"], 32, ld.STORAGE_SLOT, ld.WEAPON1, 1)]))
        await c.pump(lambda m: m.denied)
        self.assertEqual(m.denied[0][0], 35)
        await c.pump()
        self.assertIn(torso["id"], [it["id"] for it in m.inventory])   # storage pushed back unchanged


class TestSkills(LobbyTestBase):
    async def test_skills_tree_blob(self):
        c = await self.client()
        root = bc.load(c.m.skills_tree)
        self.assertEqual(sorted(root), [f"skill_{i}" for i in range(1, 6)])

    async def test_set_and_reroll(self):
        c = await self.client()
        m = c.m
        await c.send(pk_skills([(1, 3), (2, 3)], [1, 8]))
        await c.pump(lambda m: (37, b"\x00") in m.permitted and m.skills)
        await c.pump()
        self.assertEqual(sorted(m.skills), [(1, 3), (2, 3)])
        self.assertEqual(m.perks, [1, 8])
        # boosters in the profile follow the skills
        await c.send(bytes([33, 2]) + struct.pack("<I", m.profiles[0]["profile_id"]))
        await c.pump()
        boosters = {b[0]: b[1] for b in m.profiles[0]["boosters"] if b[0]}
        self.assertTrue(boosters)
        await c.send(pk_skills([(1, 2)], [1]))                 # perk 1 needs sniper level 3
        await c.pump(lambda m: m.denied)
        await c.send(pk_skills([(1, 6), (2, 6)], []))           # 12 > 10 points
        await c.pump(lambda m: len(m.denied) == 2)
        await c.send(pk_reroll())
        reroll = self.gd.service_prices[0]
        await c.pump(lambda m: (37, b"\x01") in m.permitted and not m.skills and m.money == 50000 - reroll)
        self.assertEqual(m.perks, [])


class TestPlay(LobbyTestBase):
    async def test_ready_for_match_to_connect(self):
        c = await self.client()
        m = c.m
        pid = m.profiles[0]["profile_id"]
        await c.send(pk_ready(pid))
        await c.pump(lambda m: m.connect_to_match is not None, timeout=5)
        self.assertEqual(m.permitted[0], (32, b""))
        self.assertEqual(m.connect_to_match, ("127.0.0.1", 25103, 1, 0))
        self.assertEqual(m.status, lobby.IN_MATCH)
        # exact op 51 bytes
        raw = next(p for p in c.received if p[0] == 51)
        self.assertEqual(raw, bytes([51, 9]) + b"127.0.0.1" + struct.pack("<HIB", 25103, 1, 0))
        # the 1 s status poll after 52 answers in_match (client then stops polling)
        await c.pump(lambda m: m.log[-1].startswith("54/0 state 3"), timeout=3)
        tickets = json.loads((self.state / "match_tickets.json").read_text())
        t = tickets["7"]
        self.assertEqual(t, lobby.get_match_ticket(7))
        self.assertEqual((t["account"], t["profile_name"], t["team"], t["match_id"]), ("tester", "tester", 1, 1))
        self.assertIsInstance(t["issued_at"], int)
        self.assertTrue(any(e["slot"] in (7, 10) for e in t["loadout"]))   # no weapon = crash at spawn
        for e in t["loadout"]:
            self.assertEqual(set(e), {"slot", "dict_id", "id", "condition_or_stack", "amount"})
            self.assertTrue(0 <= e["slot"] <= 18 and e["id"] and e["condition_or_stack"] <= 0xFFFF)
            self.assertIn(e["dict_id"], self.gd.items)
        ammo = next(e for e in t["loadout"] if e["slot"] == ld.AMMO1_W1)
        self.assertEqual(ammo["amount"], ammo["condition_or_stack"])
        self.assertEqual(len(bytes.fromhex(t["player_profile_hex"])), 0x1B8)
        # a second player alone after the fill timeout: a match of its own, which starts at team 0
        # again (teams alternate within a match; test_chat checks a shared one)
        c2 = await self.client(8)
        await c2.send(pk_ready(c2.m.profiles[2]["profile_id"]))
        await c2.pump(lambda m: m.connect_to_match is not None, timeout=5)
        self.assertEqual(c2.m.connect_to_match[2:], (2, 0))
        self.assertEqual(lobby.get_match_ticket(8)["team"], 1)
        # The client drops the lobby TCP when it reaches the match and reconnects while it
        # plays: the lobby keeps it in_match (state 3) until the match server reports the
        # session gone (see test_reconnect for the real-client sequence) ...
        match_id = c.m.connect_to_match[2]
        self.lobby.on_match_event("session_connected", session_id=7, match_id=match_id)
        await c.close()
        c3 = MockLobbyClient(ClientModel(DICTS))
        await c3.connect("127.0.0.1", self.port, 7)
        self.clients.append(c3)
        await c3.pump(lambda m: m.status != 4)
        self.assertEqual(c3.m.status, lobby.IN_MATCH)
        # ... back from the match: once the match server reports the session ended, a new
        # lobby connection starts in the menu again
        await c3.close()
        self.lobby.on_match_event("session_ended", session_id=7, match_id=match_id)
        c4 = await self.client()
        self.assertEqual(c4.m.status, lobby.SURF_LOBBY_MENU)

    async def test_leave_queue(self):
        self.lobby.mm.delay = 30
        c = await self.client()
        await c.send(pk_ready(c.m.profiles[0]["profile_id"]))
        await c.pump(lambda m: m.status == lobby.IN_MATCH_MAKING)
        await c.send(pk_discard(c.m.order_id))
        await c.pump(lambda m: m.status == lobby.SURF_LOBBY_MENU)
        await c.send(pk_ready(c.m.profiles[0]["profile_id"]))   # can queue again
        await c.pump(lambda m: m.permitted.count((32, b"")) == 2)

    async def test_unreachable_match_unlocks_play(self):
        self.lobby.match_timeout = 0.5
        c = await self.client()
        await c.send(pk_ready(c.m.profiles[0]["profile_id"]))
        await c.pump(lambda m: m.connect_to_match is not None)
        await asyncio.sleep(0.6)
        await c.send(pk_query(0))                               # F5 / lobby re-activation
        await c.pump(lambda m: m.status == lobby.SURF_LOBBY_MENU)

    async def test_ready_twice_denied(self):
        self.lobby.mm.delay = 30
        c = await self.client()
        pid = c.m.profiles[0]["profile_id"]
        await c.send(pk_ready(pid))
        await c.send(pk_ready(pid))
        await c.pump(lambda m: m.denied)
        self.assertEqual(c.m.denied[0][0], 32)


class TestFallbackData(LobbyTestBase):
    use_game_data = False

    async def test_builtin_table_serves_lobby(self):
        c = await self.client()
        self.assertEqual(len(c.m.profiles), 3)
        self.assertEqual(self.gd.source, "built-in snapshot")
        if DICTS:   # the snapshot matches the shipped dictionary
            live = {e["dict_id"]: (e["item_category"], bool(e["is_stack"]), e["cfg_name"])
                    for e in DICTS["items_dict"].values()}
            self.assertEqual(live, ld.FALLBACK_ITEMS)


class TestBinaryConfig(unittest.TestCase):
    @unittest.skipUnless(DICTS_FILE.is_file(), "game_data not unpacked")
    def test_roundtrip_shipped_dictionary(self):
        d = bc.load(DICTS_FILE.read_bytes())
        self.assertEqual(bc.load(bc.dump(d)), d)


if __name__ == "__main__":
    unittest.main()
