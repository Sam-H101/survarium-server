"""Progression: unlocking weapons end to end (docs/match_protocol.md section 14).

* the rules (levels, match rewards) and the data (every weapon is sold, by a trader the retail
  shop lists, at a reputation level the faction has; the match server knows every weapon);
* shop gating: the price list shows what the account has earned as buyable and the rest with
  its real level (the lock icon), a locked item cannot be bought, reputation reaches the client
  below the level that would unlock a whole trader (the retail client's quirk);
* rewards: a match result pays experience (levels -> skill points), money and reputation once,
  the client is told (stats chat line, unsolicited money / skills / reputation / price answers);
* the loop: play a match -> reward -> unlock -> buy -> equip -> the next match's ticket carries
  the new weapon, and every weapon the shop sells fires with its own rate of fire.

    python -m unittest tests.test_progression -v        (from poc-server/)
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "tools"))
sys.path.insert(0, str(HERE))

import gen_shop_prices  # noqa: E402
import lobby  # noqa: E402
import lobby_data as ld  # noqa: E402
import progression  # noqa: E402
from match import messages as M  # noqa: E402
from match.game_data import ticket_from_dict  # noqa: E402
from mock_client import pk_buy, pk_move, pk_query, pk_ready  # noqa: E402
from test_lobby import EXTRACTED, LobbyTestBase  # noqa: E402
from test_m3 import FIRE, Lobbyless, hold, m3_config  # noqa: E402
from test_match import DATA, SimNet  # noqa: E402

WEAPONS = (12, 13, 14, 15, 16, 17, 18, 19, 55, 56, 64)
VITYAZ, UZI, TOZ66, REM870, MAGNUM = 19, 55, 17, 15, 56
SELECT2 = 0x2000


def result(**kw):
    out = {"match_id": 5, "team": 0, "finished": True, "won": True, "draw": False,
           "present_at_end": True, "kills": 3, "deaths": 1, "items_stored": 1, "play_s": 300.0}
    out.update(kw)
    return out


class RulesTest(unittest.TestCase):
    def test_level_table(self):
        p = progression.Progression()
        self.assertEqual([p.level_for(x) for x in (0, 499, 500, 1199, 1200, 10 ** 9)], [1, 1, 2, 2, 3, 30])
        self.assertEqual(p.bounds(0), (0, 500))
        self.assertEqual(p.bounds(600), (500, 1200))
        top = p.level_start(30)
        self.assertEqual(top, 100 * 29 * 33)                       # 100 (L - 1)(L + 3)
        self.assertEqual(p.bounds(top + 5), (top, top))            # a full bar, no next level

    def test_match_reward_numbers(self):
        p = progression.Progression()
        win = p.reward(result())                                   # 3 kills, 1 victory item stored
        self.assertEqual((win.experience, win.money), (120 + 75 + 40 + 150, 250 + 180 + 120 + 400))
        self.assertEqual(win.reputation[1], 45 + 18 + 10 + 20)
        self.assertEqual(win.reputation[2], 25 + 36 + 15 + 15)
        draw = p.reward(result(won=False, draw=True, kills=0, items_stored=0))
        self.assertEqual((draw.experience, draw.money), (120 + 60, 250 + 150))
        loss = p.reward(result(won=False, kills=0, items_stored=0))
        self.assertEqual((loss.experience, loss.reputation[1]), (120, 45))
        left = p.reward(result(present_at_end=False, kills=2))     # left early: half, no win bonus
        self.assertEqual((left.experience, left.money, left.factor), (105, 245, 0.5))
        self.assertFalse(p.reward(result(play_s=30)))              # barely in the round
        self.assertEqual(progression.Progression(scale=2).reward(result()).experience, 2 * win.experience)

    def test_shipped_json_is_the_default_table(self):
        shipped = json.loads((HERE.parent / "data" / "progression.json").read_text(encoding="utf-8"))
        self.assertEqual(shipped, progression.DEFAULT_CONFIG)
        over = progression.Progression({"match": {"experience": {"base": 1}}})
        self.assertEqual(over.config["match"]["experience"]["per_kill"], 25)   # merged key by key


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gd = ld.load(EXTRACTED, ld.DATA_DIR)

    def test_every_weapon_has_one_offer_on_a_visible_trader(self):
        weapons = sorted(d for d, i in self.gd.items.items() if i.category in ld.PRIMARY_CATEGORIES + ld.PISTOL_CATEGORIES)
        self.assertEqual(weapons, sorted(WEAPONS))
        for d in weapons:
            offers = self.gd.offers(d)
            self.assertEqual(len(offers), 1, (d, offers))
            trader, cost, level = offers[0]
            self.assertIn(trader, (1, 2), "the retail shop window lists only traders 1 and 2")
            self.assertLess(level, len(self.gd.faction_levels[trader]))
            self.assertLessEqual(cost, 0xFFFF)

    def test_progression_has_weapons_to_unlock(self):
        locked = {d: (t, l) for d in WEAPONS for t, _, l in self.gd.offers(d) if l}
        self.assertGreaterEqual(len(locked), 5)
        self.assertEqual(sorted(locked), sorted([VITYAZ, UZI, TOZ66, REM870, MAGNUM, 14]))
        # a level is reached with the faction's own thresholds, and each trader has a ladder
        for trader in (1, 2):
            tiers = sorted({l for t, l in locked.values() if t == trader})
            self.assertEqual(tiers, list(range(1, tiers[-1] + 1)))

    def test_starter_loadouts_are_not_in_the_stash(self):
        stash = {d for d, _ in ld.STARTING_STORAGE}
        self.assertFalse(stash & set(WEAPONS))

    def test_every_weapon_has_ammo_for_sale_and_stats_in_the_match_server(self):
        sold = {d for rows in self.gd.prices.values() for d, _, _ in rows}
        for w in WEAPONS:
            ammo = [b for a, b in self.gd.compatibilities() if a == w and b != 69]
            self.assertTrue(ammo, w)
            self.assertTrue(set(ammo) & sold, (w, ammo))
            info = DATA.weapons[w]
            self.assertGreater(info.rounds_per_minute, 0)
            self.assertGreater(info.magazine_capacity, 0)
            self.assertTrue(info.fire_queue_types)
            self.assertIn(w, DATA.items)
            self.assertEqual(DATA.items[w].allowed_slots, (7, 10))
            for a in ammo:
                self.assertIn(a, DATA.ammo)

    def test_shop_prices_json_is_generated_from_the_offer_table(self):
        self.assertEqual(gen_shop_prices.main(["--check"]), 0, "run tools/gen_shop_prices.py")
        built = ld.build_prices(self.gd)
        for d, (trader, level, cost) in ld.WEAPON_OFFERS.items():
            self.assertIn((d, cost, level), built[trader])                # the built-in fallback agrees
        for prices in (self.gd.prices, built):
            for rows in prices.values():
                for d, cost, level in rows:
                    if d in ld.AMMO_PRICES:
                        self.assertEqual((cost, level), (ld.AMMO_PRICES[d], 0), d)


class ShopGatingTest(LobbyTestBase):
    async def test_price_lists_show_the_lock_and_buying_is_denied(self):
        c = await self.client()
        m = c.m
        bm = {d: lvl for d, _, lvl in m.prices[2]}
        scav = {d: lvl for d, _, lvl in m.prices[1]}
        self.assertEqual((bm[13], bm[12]), (0, 0))                       # starters: free to buy
        self.assertEqual((bm[UZI], bm[MAGNUM], bm[VITYAZ], bm[14]), (1, 1, 2, 2))
        self.assertEqual((scav[TOZ66], scav[REM870], scav[18]), (1, 2, 0))
        self.assertEqual(m.prices[3] and any(d in WEAPONS for d, _, _ in m.prices[3]), False)
        money = m.money
        await c.send(pk_buy(VITYAZ, 1, 2))
        await c.pump(lambda m: m.denied)
        op, text = m.denied[0]
        self.assertEqual(op, 36)
        self.assertIn("locked", text)
        self.assertIn("800", text)                                       # what it takes
        self.assertEqual(m.money, money)
        # asking another trader or "any" (faction 0, what the client sends) changes nothing
        for faction in (0, 1):
            n = len(m.denied)
            await c.send(pk_buy(VITYAZ, 1, faction))
            await c.pump(lambda m: len(m.denied) > n)
        self.assertFalse(any(it["dict_id"] == VITYAZ for it in m.inventory))

    async def test_reputation_is_reported_below_the_level_that_opens_a_whole_trader(self):
        acc = self.lobby.store.account("tester")
        acc["reputation"].update({"1": 700, "2": 900, "3": 5000})
        c = await self.client()
        for f, pts in c.m.reputations:
            values = self.gd.faction_levels[f]
            self.assertEqual(self.gd.reputation_level(f, pts), 0, (f, pts))   # the client reads level 0
            self.assertLess(pts, values[1])
        # ... while the server's own view is the real one
        self.assertEqual(self.gd.reputation_level(1, 700), 3)
        bm = {d: lvl for d, _, lvl in c.m.prices[2]}
        self.assertEqual(bm[VITYAZ], 0)                                  # earned (900 >= 800)
        self.assertEqual(bm[14], 0)

    async def test_unlocked_weapon_can_be_bought_equipped_and_carried_into_a_match(self):
        self.lobby.store.account("tester")["reputation"]["2"] = 800
        c = await self.client()
        m = c.m
        pr = m.profiles[0]
        pid = pr["profile_id"]
        money = m.money
        await c.send(pk_buy(VITYAZ, 1, 2))
        await c.pump(lambda m: any(it["dict_id"] == VITYAZ for it in m.inventory))
        await c.pump()
        cost = next(c_ for d, c_, _ in m.prices[2] if d == VITYAZ)
        self.assertEqual(m.money, money - cost)
        vityaz = next(it for it in m.inventory if it["dict_id"] == VITYAZ)
        # weapon2 slot: the AK-74u goes back to storage, and its 5.45 ammo no longer fits
        await c.send(pk_move([(pid, vityaz["id"], VITYAZ, ld.STORAGE_SLOT, ld.WEAPON2, 1)]))
        await c.pump(lambda m: (35, b"") in m.permitted)
        await c.pump()
        slots = m.profiles[0]["slots"]
        self.assertEqual(slots[ld.WEAPON2]["dict_id"], VITYAZ)
        self.assertNotIn(ld.AMMO1_W2, slots)
        self.assertTrue(any(it["dict_id"] == 13 for it in m.inventory))
        self.assertTrue(any(it["dict_id"] == 7 and it["cond"] >= 120 for it in m.inventory))
        ammo = next(it for it in m.inventory if it["dict_id"] == 53)
        await c.send(pk_move([(pid, ammo["id"], 53, ld.STORAGE_SLOT, ld.AMMO1_W2, 90)]))
        await c.pump(lambda m: m.permitted.count((35, b"")) == 2)
        await c.pump()
        self.assertEqual(m.profiles[0]["slots"][ld.AMMO1_W2]["dict_id"], 53)
        # the 9x19 pistol round does not fit the 7.62 rifle in weapon 1
        await c.send(pk_move([(pid, ammo["id"], 53, ld.STORAGE_SLOT, ld.AMMO1_W1, 10)]))
        await c.pump(lambda m: m.denied)
        self.assertEqual(m.denied[-1][0], 35)

        # Play: the ticket carries it, and the match server accepts it as it is
        await c.send(pk_ready(pid))
        await c.pump(lambda m: m.connect_to_match is not None)
        ticket = lobby.get_match_ticket(7)
        loadout = {e["slot"]: e for e in ticket["loadout"]}
        self.assertEqual((loadout[10]["dict_id"], loadout[11]["dict_id"], loadout[7]["dict_id"]), (VITYAZ, 53, 12))
        t = ticket_from_dict(7, ticket)
        kept, dropped = DATA.sanitize_loadout(t.slots)
        self.assertEqual(dropped, [])
        self.assertEqual(kept[10].dict_id, VITYAZ)
        self.assertIsNone(DATA.validate_loadout(kept))

        net = SimNet(config=m3_config(), ticket_lookup=lambda sid: {7: ticket}.get(sid))
        cl = net.add_client(7, ("127.0.0.1", 53000))
        self.assertTrue(net.run(20000, until=lambda: cl.controllable))
        got = cl.profiles[0][0].slots
        self.assertEqual((got[10].dict_id, got[11].dict_id), (VITYAZ, 53))
        state = {"n": 0}

        def bot(client, now):
            state["n"] += 1
            return client.local["position"], 0.0, 0.0, (SELECT2 if state["n"] <= 4 else FIRE)
        cl.bot = bot
        match = next(iter(net.server.matches.values()))
        player = match.players[0]
        net.run(3000)
        self.assertEqual(player.active_slot, 10)
        self.assertGreater(player.shots_fired, 10)                       # the Vityaz is automatic
        self.assertEqual(cl.faults, [])


class RewardTest(LobbyTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.notes = []
        self.lobby.notify = lambda acc, stats, lines: self.notes.append((acc, stats, lines))

    def acc(self):
        return self.lobby.store.doc["accounts"]["tester"]

    async def test_match_result_pays_once_and_the_client_is_told(self):
        c = await self.client()
        m = c.m
        acc = self.acc()
        money, xp, sp, rep1 = acc["money"], acc["experience"], acc["skill_points"], acc["reputation"]["1"]
        self.lobby.on_match_event("match_finished", 7, 5, result=result())
        self.assertEqual(acc["experience"], xp + 385)
        self.assertEqual(acc["money"], money + 950)
        self.assertEqual(acc["reputation"]["1"], rep1 + 93)
        self.assertEqual(acc["skill_points"], sp)                         # 385 xp: still level 1
        self.assertEqual(acc["stats"], {"matches": 1, "wins": 1, "kills": 3, "deaths": 1})
        # the same result again (session_ended and match_finished both carry it) pays nothing
        self.lobby.on_match_event("session_ended", 7, 5, result=result())
        self.assertEqual(acc["experience"], xp + 385)
        # unsolicited answers: money, experience bar, reputation, all four price lists
        await c.pump(lambda m: m.money == money + 950 and m.leveling[0] == xp + 385)
        await c.pump()
        self.assertEqual(m.leveling, (xp + 385, 500, 0))
        self.assertEqual(len(m.prices), 4)
        # the stats line the client parses and a readable summary
        (acct, stats, lines), = self.notes
        self.assertEqual(acct, "tester")
        self.assertTrue(stats.startswith("Player [ tester ] "))
        self.assertTrue(stats.endswith("#e:[385]"))
        self.assertTrue(any(line.startswith("Reputation:") for line in lines))

    async def test_levels_grant_skill_points_and_move_the_bar(self):
        c = await self.client()
        acc = self.acc()
        sp = acc["skill_points"]
        self.lobby.on_match_event("match_finished", 7, 1, result=result(kills=20, items_stored=3))
        # 120 + 500 + 120 + 150 = 890 xp: level 2 (500), next level at 1200
        self.assertEqual(acc["experience"], 890)
        self.assertEqual(acc["skill_points"], sp + 1)
        await c.pump(lambda m: m.skill_points == sp + 1 and m.leveling[0] == 890)
        await c.pump()
        self.assertEqual(c.m.leveling, (890, 1200, 500))
        self.assertTrue(any("Level 2" in line for line in self.notes[0][2]))
        self.lobby.on_match_event("match_finished", 7, 2, result=result(kills=30, items_stored=3))
        self.assertEqual(self.lobby.prog.level_for(acc["experience"]), 3)
        self.assertEqual(acc["skill_points"], sp + 2)

    async def test_short_or_unknown_results_pay_nothing(self):
        await self.client()
        acc = self.acc()
        before = (acc["experience"], acc["money"], dict(acc["reputation"]))
        self.lobby.on_match_event("match_finished", 7, 1, result=result(play_s=10))
        self.lobby.on_match_event("match_finished", 999, 2, result=result())      # no such session
        self.lobby.on_match_event("session_ended", 7, 3)                          # no result
        self.assertEqual((acc["experience"], acc["money"], acc["reputation"]), before)
        self.assertEqual(self.notes, [])

    async def test_reputation_unlocks_items_and_reaches_the_shop(self):
        c = await self.client()
        m = c.m
        bm = lambda: {d: lvl for d, _, lvl in m.prices[2]}                       # noqa: E731
        self.assertEqual(bm()[UZI], 1)
        self.lobby.prog = progression.Progression(scale=10)                      # 10 x the rewards
        self.lobby.on_match_event("match_finished", 7, 1, result=result(kills=0, items_stored=0, won=False))
        # 250 black market reputation: Uzi (500) still locked; Scavengers 450: TOZ-66 (250) is not
        self.assertEqual(self.acc()["reputation"]["2"], 200 + 250)
        await c.pump(lambda m: m.money > 50000)
        await c.pump()
        self.assertEqual(bm()[UZI], 1)
        scav = {d: lvl for d, _, lvl in m.prices[1]}
        self.assertEqual((scav[TOZ66], scav[REM870]), (0, 0))                    # 650 >= 400
        self.assertTrue(any("toz_66" in line for line in self.notes[0][2]))
        self.lobby.on_match_event("match_finished", 7, 2, result=result(kills=0, items_stored=0, won=False))
        await c.pump(lambda m: bm()[UZI] == 0)
        self.assertEqual((bm()[UZI], bm()[MAGNUM], bm()[VITYAZ]), (0, 0, 2))     # 700: Vityaz still wants 800
        self.assertTrue(any("uzi" in line for line in self.notes[1][2]))
        # the client keeps seeing level 0 reputations only
        await c.send(pk_query(11))
        await c.pump()
        for f, pts in m.reputations:
            self.assertEqual(self.gd.reputation_level(f, pts), 0)
        # bought: the Uzi
        await c.send(pk_buy(UZI, 1, 2))
        await c.pump(lambda m: any(it["dict_id"] == UZI for it in m.inventory))

    async def test_result_paid_while_still_in_match_reaches_the_shop_when_the_client_returns(self):
        """The real order: the match server reports session_ended (with the result) while the lobby
        still has the player in the match; the lobby then puts it back in the menu. The client
        sends no q_client_state after that, so the refresh must follow the state push."""
        self.lobby.prog = progression.Progression(scale=20)
        c = await self.client()
        m = c.m
        await c.send(pk_ready(m.profiles[0]["profile_id"]))
        await c.pump(lambda m: m.connect_to_match is not None)
        self.assertEqual(self.lobby.status["tester"].state, lobby.IN_MATCH)
        self.assertEqual({d: lvl for d, _, lvl in m.prices[2]}[UZI], 1)
        self.lobby.on_match_event("session_ended", 7, 1, result=result(match_id=1, kills=0, items_stored=0))
        self.assertEqual(self.lobby.status["tester"].state, lobby.SURF_LOBBY_MENU)
        await c.pump(lambda m: {d: lvl for d, _, lvl in m.prices[2]}[UZI] == 0)
        self.assertEqual(m.status, lobby.SURF_LOBBY_MENU)

    async def test_result_arrives_while_the_client_is_away(self):
        c = await self.client()
        acc = self.acc()
        await c.close()
        self.clients.clear()
        money = acc["money"]
        self.lobby.on_match_event("match_finished", 7, 5, result=result())
        self.assertIn("tester", self.lobby.dirty)
        c2 = await self.client()                                                  # signs in again
        await c2.pump(lambda m: m.money == money + 950)
        self.assertNotIn("tester", self.lobby.dirty)

    async def test_old_account_without_stats_is_migrated(self):
        await self.client()
        acc = self.acc()
        del acc["stats"]
        self.lobby.on_match_event("match_finished", 7, 5, result=result())
        self.assertEqual(acc["stats"]["matches"], 1)


class FullLoopTest(LobbyTestBase):
    """Queue -> a real match in virtual time -> the lobby pays it -> the unlocked weapon."""

    async def test_play_a_match_and_unlock_the_uzi(self):
        self.lobby.prog = progression.Progression(scale=20)        # one match is worth twenty
        c = await self.client()
        m = c.m
        pid = m.profiles[0]["profile_id"]
        await c.send(pk_ready(pid))
        await c.pump(lambda m: m.connect_to_match is not None)
        ticket = lobby.get_match_ticket(7)
        cfg = m3_config(match_time=75, victory_items_count=0)
        net = SimNet(config=cfg, ticket_lookup=lobby.get_match_ticket)
        net.server.on_event = self.lobby.on_match_event
        cl = net.add_client(7, ("127.0.0.1", 53100))
        self.assertTrue(net.run(20000, until=lambda: cl.controllable))
        cl.bot = hold(cl.local["position"])
        acc = self.lobby.store.doc["accounts"]["tester"]
        self.assertEqual(acc["experience"], 0)
        self.assertTrue(net.run(200000, until=lambda: acc["experience"] > 0), net.server.matches)
        self.assertEqual(ticket["match_id"], 1)
        # a lone player: nobody won, the match was played to the end -> draw bonus
        self.assertEqual(acc["experience"], 20 * (120 + 60))
        self.assertEqual(acc["reputation"]["2"], 200 + 20 * 25)
        await c.pump(lambda m: m.money == acc["money"] and m.leveling and m.leveling[0] == acc["experience"])
        await c.pump()
        bm = {d: lvl for d, _, lvl in m.prices[2]}
        self.assertEqual((bm[UZI], bm[MAGNUM], bm[VITYAZ]), (0, 0, 2))      # 700 reputation: Vityaz wants 800
        await c.send(pk_buy(UZI, 1, 2))
        await c.pump(lambda m: any(it["dict_id"] == UZI for it in m.inventory))
        await c.send(pk_buy(VITYAZ, 1, 2))
        await c.pump(lambda m: len(m.denied) > 0)
        self.assertEqual(m.denied[-1][0], 36)

    async def test_match_server_reports_a_result_per_player(self):
        events = []
        w = Lobbyless(2, m3_config(match_time=75, victory_items_count=0))
        w.net.server.on_event = lambda kind, **kw: events.append((kind, kw["session_id"], kw.get("result")))
        self.assertTrue(w.run(20000, until=w.all_controllable))
        for c in w.clients:
            c.bot = hold(c.local["position"])
        w.run(200000, until=lambda: any(e[0] == "match_finished" for e in events))
        finished = {sid: r for kind, sid, r in events if kind in ("session_ended", "match_finished") and r}
        self.assertEqual(sorted(finished), sorted(w.sids))
        for r in finished.values():
            self.assertTrue(r["finished"] and r["draw"] and not r["won"])
            self.assertGreaterEqual(r["play_s"], 60)
            self.assertEqual((r["kills"], r["deaths"], r["items_stored"]), (0, 0, 0))
            self.assertTrue(r["present_at_end"])


class EveryWeaponFiresTest(unittest.TestCase):
    """Every weapon the shop can sell works in a match: automatic ones keep firing while the
    trigger is held (fire_queue_types[0] == -1), the others fire once per press."""

    def loadout(self, weapon):
        I = M.ItemInstance
        gd = ld.load(EXTRACTED, ld.DATA_DIR)
        ammo = next(b for a, b in gd.compatibilities() if a == weapon and b != 69)
        clip = DATA.items[ammo].clip_size or 1
        return {7: I(weapon, 100, 100, 0), 8: I(ammo, 101, clip, 3 * clip)}

    def test_rate_of_fire_and_automatic_modes(self):
        for weapon in WEAPONS:
            with self.subTest(weapon=DATA.items[weapon].cfg_name):
                w = Lobbyless(2, m3_config(), slots=self.loadout(weapon))
                a, b = w.clients
                self.assertTrue(w.run(20000, until=w.all_controllable))
                a.bot = hold(a.local["position"], actions=FIRE)
                b.bot = hold(b.local["position"])
                w.run(2500)
                info = DATA.weapons[weapon]
                shots = w.match.players[0].shots_fired
                if info.fire_queue_types[0] == 0xFF or info.fire_queue_types[0] < 0:
                    self.assertGreaterEqual(shots, min(info.magazine_capacity, 5), shots)
                    self.assertLessEqual(shots, info.magazine_capacity + 1 + int(
                        info.rounds_per_minute / 60 * 2.5))
                else:
                    self.assertEqual(shots, 1, "semi-automatic: one round per trigger press")
                self.assertEqual(a.faults + b.faults, [])


if __name__ == "__main__":
    unittest.main()
