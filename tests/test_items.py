"""Quick-slot items, booby traps, artefacts and boosters in the match server, and the
match -> lobby consumption report.

* booby traps (0x96 placed, 0x97 removed, 0x98 fired, 0x99 disarmed, 0x9c state): the
  networked client never places, fires or defuses a trap itself, the server does, from
  the quick-slot up bit, the players' positions, the use key and the shots;
* medkit-class drugs (medkit, bandages, painkiller): one activation per slot at a time,
  the painkiller's damage protection while it is active;
* the lifebone artefact (passive hand/leg protection, reset on use), the oxygen tank;
* boosters 9 (anomaly damage) and 10 (engineer use time);
* what a player fired and used is reported in the match result and taken out of the
  lobby account.

    python -m unittest tests.test_items -v        (from poc-server/)
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from match import ballistics as B  # noqa: E402
from match import combat as C  # noqa: E402
from match import items as I  # noqa: E402
from match import messages as M  # noqa: E402
from match.game import USE_BIT  # noqa: E402
from test_m3 import FIRE, Lobbyless, m3_config  # noqa: E402
from test_match import DATA  # noqa: E402

AK, AMMO_545 = 13, 7
PAINKILLER, BANDAGES, MEDKIT, TRAP, LIFEBONE, OXYGEN = 65, 66, 67, 68, 54, 9
QS1_DOWN, QS1_UP = 0x4000, 0x8000
QS2_DOWN, QS2_UP = 0x10000, 0x20000
CROUCH = 0x100


def loadout(extra=None):
    """AK-74u + 90 rounds (30 a life), a trap set of 2 in quick slot 1, medkits in 2."""
    I_ = M.ItemInstance
    slots = {7: I_(AK, 1, 30, 1), 8: I_(AMMO_545, 2, 30, 90),
             13: I_(TRAP, 3, 2, 2), 14: I_(MEDKIT, 4, 2, 2)}
    slots.update(extra or {})
    return slots


def look(src, dst, crouched=False):
    """(yaw, look_pitch) from src's eye at dst (look_pitch goes through the look clip)."""
    eye = C.EYE_CROUCH if crouched else C.EYE_STAND
    dx, dy, dz = dst[0] - src[0], dst[1] - (src[1] + eye), dst[2] - src[2]
    deg = math.degrees(math.atan2(dy, math.hypot(dx, dz)))
    return math.atan2(-dx, dz), B.VIEW.look_pitch_for(deg, crouched)


class Script:
    """A bot that runs a list of (frames, position, yaw, pitch, actions) steps and then
    holds the last one (position None = stay where the client is)."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.frame = 0

    def __call__(self, c, now):
        self.frame += 1
        n = self.frame
        for frames, pos, yaw, pitch, actions in self.steps:
            if n <= frames:
                break
            n -= frames
        pos = pos or c.local["position"]
        return pos, yaw, pitch, actions


def duel(slots=None, **cfg):
    w = Lobbyless(2, m3_config(**cfg), slots=slots or loadout())
    a, b = w.clients
    assert w.run(20000, until=w.all_controllable)
    return w, a, b


def place_trap(w, a, distance=0.9, slot_down=QS1_DOWN, slot_up=QS1_UP):
    """a looks at the ground `distance` m ahead (+z) and taps quick slot 1 (the 2 m
    deploy ray from a standing eye reaches the ground up to about 1.17 m ahead)."""
    pos = a.local["position"]
    target = (pos[0], pos[1], pos[2] + distance)
    yaw, pitch = look(pos, target)
    a.bot = Script([(5, pos, yaw, pitch, 0), (1, pos, yaw, pitch, slot_down),
                    (3, pos, yaw, pitch, 0), (1, pos, yaw, pitch, slot_up),
                    (10 ** 9, pos, yaw, pitch, 0)])
    return target


class TrapTest(unittest.TestCase):
    def assertClean(self, *clients):
        for c in clients:
            self.assertEqual(c.faults, [])

    def test_place_broadcast_and_amount(self):
        w, a, b = duel()
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in a.traps and key in b.traps))
        w.run(300)
        self.assertClean(a, b)
        trap = w.match.traps[key]
        self.assertEqual(trap.state, I.TRAP_ARMED)
        self.assertLess(math.dist(trap.position, target), 0.05)
        self.assertAlmostEqual(trap.angles[1], 0.0, places=3)      # facing +z: yaw 0
        self.assertLess(math.dist(a.traps[key]["position"], target), 0.05)
        self.assertEqual(a.local["weapons"][13], 1)                # --m_amount on 0x96
        self.assertEqual(w.match.players[0].quick[13], 1)
        self.assertEqual(w.match.players[0].used[13], 1)
        self.assertEqual([e for e in a.trap_events], [(0x96, key)])

    def test_too_far_or_looking_up_places_nothing(self):
        w, a, b = duel()
        place_trap(w, a, distance=3.0)                  # beyond max_deploy_distance (2 m)
        w.run(1500)
        pos = a.local["position"]
        a.bot = Script([(2, pos, 0.0, 0.3, 0), (1, pos, 0.0, 0.3, QS1_UP),
                        (10 ** 9, pos, 0.0, 0.3, 0)])           # looking up
        w.run(1500)
        self.assertEqual(w.match.traps, {})
        self.assertEqual(a.trap_events + b.trap_events, [])
        self.assertEqual(w.match.players[0].quick[13], 2)
        self.assertClean(a, b)

    def test_enemy_steps_on_it_fires_and_removes(self):
        w, a, b = duel()
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        b.bot = lambda c, now: (target, 0.0, 0.0, 0)
        self.assertTrue(w.run(3000, until=lambda: (0x98, key) in b.trap_events))
        hits = [h for h in b.hits if h[0] == 0 and h[1] == 1]
        self.assertEqual([(h[2], h[3]) for h in hits],
                         [("right_foot", "injury"), ("left_foot", "injury"), ("pain", "injury")])
        self.assertEqual([h[4] for h in hits], [1.0, 1.0, 1.5])
        # broken feet -> leg damage on both legs (thresholds of right/left_foot)
        pb = w.match.players[1]
        self.assertTrue(pb.damage.parts["left_leg"].has_affect(4))
        self.assertTrue(pb.damage.parts["right_leg"].has_affect(4))
        self.assertTrue(pb.alive)
        self.assertEqual(w.match.traps[key].state, I.TRAP_FIRED)
        # fired_life_time 3 s, then removed everywhere
        self.assertTrue(w.run(4000, until=lambda: key not in a.traps and key not in b.traps))
        self.assertEqual(b.trap_events, [(0x96, key), (0x98, key), (0x97, key)])
        self.assertEqual(w.match.traps, {})
        self.assertClean(a, b)

    def test_owner_and_team_do_not_trigger_without_friendly_fire(self):
        w, a, b = duel()
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in a.traps))
        a.bot = lambda c, now: (target, 0.0, 0.0, 0)        # the owner walks over it
        w.run(1500)
        self.assertEqual(w.match.traps[key].state, I.TRAP_ARMED)
        self.assertEqual(a.hits, [])
        self.assertClean(a, b)

    def test_owner_triggers_with_friendly_fire(self):
        w, a, b = duel(friendly_fire=True)
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in a.traps))
        a.bot = lambda c, now: (target, 0.0, 0.0, 0)
        self.assertTrue(w.run(1500, until=lambda: (0x98, key) in a.trap_events))
        self.assertTrue(any(h[0] == 0 and h[1] == 0 for h in a.hits))
        self.assertClean(a, b)

    def defuse_bot(self, trap_pos, start, hold_frames=10 ** 9):
        """b crouches 0.5 m from the trap and holds use on the near part of its box."""
        toward = (trap_pos[0], trap_pos[1], trap_pos[2] + 0.5)
        aim_at = (trap_pos[0], trap_pos[1] + 0.21, trap_pos[2] + 0.2)
        yaw, pitch = look(toward, aim_at, crouched=True)
        return Script([(start, toward, yaw, pitch, CROUCH),
                       (hold_frames, toward, yaw, pitch, CROUCH | USE_BIT),
                       (10 ** 9, toward, yaw, pitch, CROUCH)])

    def test_enemy_defuses_with_the_use_key(self):
        w, a, b = duel()
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        b.bot = self.defuse_bot(target, 5)
        w.run(4000)                                      # defuse_time is 5 s
        self.assertEqual(w.match.traps[key].state, I.TRAP_ARMED)
        self.assertIsNotNone(w.match.players[1].defusing)
        self.assertTrue(w.run(2000, until=lambda: (0x99, key) in b.trap_events))
        self.assertEqual(b.traps[key]["state"], 3)
        self.assertEqual(b.hits, [])                     # never stepped on it
        self.assertTrue(w.run(4000, until=lambda: key not in b.traps))
        self.assertEqual(a.trap_events, [(0x96, key), (0x99, key), (0x97, key)])
        self.assertClean(a, b)

    def test_releasing_use_restarts_the_defuse(self):
        w, a, b = duel()
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        # 3 s (about 270 frames) of use, released, then 3 s more: never 5 s in a row
        bot = self.defuse_bot(target, 5, hold_frames=270)
        bot.steps.insert(2, (20, bot.steps[0][1], bot.steps[0][2], bot.steps[0][3], CROUCH))
        bot.steps.insert(3, (270, bot.steps[1][1], bot.steps[1][2], bot.steps[1][3], CROUCH | USE_BIT))
        b.bot = bot
        w.run(7000)
        self.assertEqual(w.match.traps[key].state, I.TRAP_ARMED)
        self.assertClean(a, b)

    def test_engineer_booster_shortens_the_defuse(self):
        w = Lobbyless(2, m3_config(), slots=loadout())
        w.tickets[w.sids[1]]["boosters"] = {"10": -50.0}      # engineer_use_time -50 %
        a, b = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        b.bot = self.defuse_bot(target, 5)
        self.assertTrue(w.run(3500, until=lambda: (0x99, key) in b.trap_events))
        self.assertClean(a, b)

    def test_teammate_cannot_defuse(self):
        w = Lobbyless(3, m3_config(), slots=loadout())       # players 0 and 2: team 1
        a, b, c = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in c.traps))
        c.bot = self.defuse_bot(target, 5)
        w.run(7000)
        self.assertEqual(w.match.traps[key].state, I.TRAP_ARMED)
        self.assertClean(a, b, c)

    def test_shooting_the_trap_disarms_it(self):
        w, a, b = duel()
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        stand = (target[0] + 3.0, target[1], target[2])
        yaw, pitch = look(stand, (target[0], target[1] + 0.12, target[2]))
        b.bot = Script([(5, stand, yaw, pitch, 0), (10 ** 9, stand, yaw, pitch, FIRE)])
        self.assertTrue(w.run(3000, until=lambda: (0x99, key) in a.trap_events))
        self.assertEqual(a.hits, [])
        self.assertClean(a, b)

    def test_late_joiner_gets_the_trap_state(self):
        w = Lobbyless(3, m3_config(join_timeout_s=3), slots=loadout(), skip=(2,))
        a, b = w.clients
        self.assertTrue(w.run(20000, until=lambda: a.controllable and b.controllable))
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        c = w.net.add_client(w.sids[2], ("127.0.0.1", 51002))
        self.assertTrue(w.run(20000, until=lambda: c.controllable and key in c.traps))
        self.assertEqual(c.trap_events, [(0x9c, key)])
        self.assertEqual(c.traps[key]["state"], 1)
        self.assertLess(math.dist(c.traps[key]["position"], target), 0.05)
        self.assertEqual(c.inserted[0]["weapons"][13], 1)     # 0x84 already counts it
        self.assertClean(a, b, c)

    def test_respawn_clears_the_owners_traps_and_the_stack_stays_spent(self):
        w, a, b = duel()
        place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        a.bot = None
        a.commit_suicide()
        self.assertTrue(w.run(8000, until=lambda: a.inserted[0]["spawns"] == 2 and a.controllable))
        w.run(200)
        self.assertNotIn(key, a.traps)              # the 0x84 removed it (no 0x97 needed)
        self.assertNotIn(key, b.traps)
        self.assertEqual(w.match.traps, {})
        self.assertEqual(a.local["weapons"][13], 1)  # one of two traps is gone for good
        self.assertEqual(b.trap_events, [(0x96, key)])
        self.assertClean(a, b)

    def test_stack_runs_out(self):
        w, a, b = duel()
        for k in range(3):
            place_trap(w, a, distance=0.6 + 0.25 * k)
            w.run(800)
        self.assertEqual(sorted(w.match.traps), [(0, 13, 0), (0, 13, 1)])
        self.assertEqual(w.match.players[0].quick[13], 0)
        self.assertEqual(len([e for e in b.trap_events if e[0] == 0x96]), 2)
        self.assertClean(a, b)


class TrapGeometryTest(unittest.TestCase):
    def test_place_matrix_on_level_ground(self):
        fwd, right = B.aim_frame(0.7, -40.0)
        i, j, k = I.place_matrix((0.0, 1.0, 0.0), fwd, right)
        ang = I.angles_zxy(i, j, k)
        self.assertAlmostEqual(ang[0], 0.0, places=6)
        self.assertAlmostEqual(ang[1], 0.7, places=6)        # the view's yaw
        self.assertAlmostEqual(ang[2], 0.0, places=6)

    def test_place_matrix_looking_straight_down(self):
        fwd, right = B.aim_frame(0.0, -90.0)
        i, j, k = I.place_matrix((0.0, 1.0, 0.0), fwd, right)
        self.assertAlmostEqual(abs(k[2]), 1.0, places=5)

    def test_sensor(self):
        trap = I.Trap(0, 13, 0, TRAP, DATA.traps[TRAP], (10.0, 2.0, 5.0), (0.0, 0.0, 0.0))
        self.assertTrue(I.feet_in_sensor(trap, (10.0, 2.0, 5.0)))
        self.assertTrue(I.feet_in_sensor(trap, (10.35, 2.0, 5.0)))
        self.assertFalse(I.feet_in_sensor(trap, (10.5, 2.0, 5.0)))
        self.assertFalse(I.feet_in_sensor(trap, (10.0, 3.0, 5.0)))


def tap(slot_bit, frames_before=5):
    """A bot that stays put and presses one key for one frame."""
    return lambda pos, yaw=0.0, pitch=0.0: Script(
        [(frames_before, pos, yaw, pitch, 0), (1, pos, yaw, pitch, slot_bit), (10 ** 9, pos, yaw, pitch, 0)])


def press(c, bit, frames_before=5):
    c.bot = tap(bit, frames_before)(c.local["position"])


class DrugTest(unittest.TestCase):
    def test_painkiller_protects_pain_while_active_and_cannot_stack(self):
        w, a, b = duel(loadout({14: M.ItemInstance(PAINKILLER, 4, 3, 3)}))
        pa = w.match.players[0]
        pos = a.local["position"]
        a.bot = Script([(5, pos, 0.0, 0.0, 0), (1, pos, 0.0, 0.0, QS2_DOWN), (20, pos, 0.0, 0.0, 0),
                        (1, pos, 0.0, 0.0, QS2_DOWN), (10 ** 9, pos, 0.0, 0.0, 0)])
        w.run(600)
        self.assertEqual(pa.quick[14], 2)                   # the second press: m_active
        self.assertEqual(len(pa.medkits), 1)
        self.assertEqual(len(pa.damage.parts["pain"].protectors), 1)   # from set_active(true)
        # pain injury through the protector: (amount - threshold 0) x hit_coeff 0.2
        plain = C.DamageModel(DATA.body_parts, DATA.armour_mods(pa.ticket.slots), {})
        before, before_plain = pa.damage.parts["pain"].health, plain.parts["pain"].health
        pa.damage.hit("pain", "injury", 1.0, 0.0, w.core.now_ms)
        plain.hit("pain", "injury", 1.0, 0.0, w.core.now_ms)
        drop, drop_plain = before - pa.damage.parts["pain"].health, before_plain - plain.parts["pain"].health
        self.assertGreater(drop_plain, 0.0)
        self.assertAlmostEqual(drop, 0.2 * drop_plain, places=5)
        # activation_delay 1 s + activity_time 5 s, then set_active(false)
        w.run(6000)
        self.assertEqual(pa.medkits, [])
        self.assertEqual(pa.damage.parts["pain"].protectors, [])
        press(a, QS2_DOWN)
        w.run(300)
        self.assertEqual((pa.quick[14], pa.used[14]), (1, 2))
        self.assertEqual(a.faults + b.faults, [])

    def test_bandages_mend_broken_legs_and_tell_the_others(self):
        w, a, b = duel(loadout({14: M.ItemInstance(BANDAGES, 4, 2, 2)}))
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        b.bot = lambda c, now: (target, 0.0, 0.0, 0)
        self.assertTrue(w.run(3000, until=lambda: (0x98, key) in b.trap_events))
        pb = w.match.players[1]
        self.assertTrue(pb.damage.parts["left_leg"].has_affect(4))
        self.assertIn((1, "left_leg", 4, 0), a.affects)            # applying, to the others
        press(b, QS2_DOWN)
        self.assertTrue(w.run(2000, until=lambda: not pb.damage.parts["left_leg"].has_affect(4)))
        self.assertFalse(pb.damage.parts["right_leg"].has_affect(4))
        w.run(200)
        # medkit::remove_affects cancels; a read-only copy only acts on "recalling"
        self.assertIn((1, "left_leg", 4, 1), a.affects)
        self.assertNotIn((1, "left_leg", 4, 2), a.affects)
        self.assertEqual(pb.quick[14], 1)
        self.assertEqual(a.faults + b.faults, [])


class LifeboneTest(unittest.TestCase):
    def test_protector_blocks_leg_and_hand_damage(self):
        plain = C.DamageModel(DATA.body_parts)
        guarded = C.DamageModel(DATA.body_parts)
        prot = I.lifebone_protector()
        for part in I.LIFEBONE_PARTS:
            guarded.register_protector(part, prot)
        for dm in (plain, guarded):
            dm.hit("left_foot", "injury", 5.0, 5.0, 1000)
            dm.hit("right_arm", "injury", 5.0, 5.0, 1000)
        self.assertTrue(plain.parts["left_leg"].has_affect(4))
        self.assertTrue(plain.parts["right_hand"].has_affect(3))
        self.assertFalse(guarded.parts["left_leg"].has_affect(4))
        self.assertFalse(guarded.parts["right_hand"].has_affect(3))
        self.assertEqual(guarded.parts["left_foot"].health, 0.0)      # damage itself passes
        guarded.reset()                                                # damage_model::reset
        self.assertEqual(guarded.parts["left_leg"].protectors, [prot])

    def test_lifebone_in_a_match(self):
        w, a, b = duel(loadout({15: M.ItemInstance(LIFEBONE, 5, 100, 0)}))
        target = place_trap(w, a)
        key = (0, 13, 0)
        self.assertTrue(w.run(3000, until=lambda: key in b.traps))
        b.bot = lambda c, now: (target, 0.0, 0.0, 0)
        self.assertTrue(w.run(3000, until=lambda: (0x98, key) in b.trap_events))
        pb = w.match.players[1]
        self.assertFalse(pb.damage.parts["left_leg"].has_affect(4))   # passive mode
        self.assertNotIn((1, "left_leg", 4, 0), a.affects)
        pb.damage.hit("left_leg", "injury", 0.6, 0.0, w.core.now_ms)
        self.assertLess(pb.damage.parts["left_leg"].health, pb.damage.parts["left_leg"].max_health)
        press(b, 0x40000)                                  # quick slot 3: action(true)
        self.assertTrue(w.run(1000, until=lambda: pb.damage.parts["left_leg"].health
                              == pb.damage.parts["left_leg"].max_health))
        self.assertNotIn(15, pb.used)                      # amount -1: unlimited
        self.assertEqual(a.faults + b.faults, [])


class OxygenTankTest(unittest.TestCase):
    def test_back_slot_key_toggles_the_tank(self):
        w, a, b = duel(loadout({3: M.ItemInstance(OXYGEN, 6, 100, 0)}))
        pa = w.match.players[0]
        self.assertEqual(pa.oxygen.amount_ms, 60000)
        self.assertFalse(pa.oxygen.active)
        press(a, I.BACK_SLOT_USE_BIT)
        w.run(2100)
        self.assertTrue(pa.oxygen.active)
        self.assertLess(pa.oxygen.amount_ms, 58500)
        self.assertEqual(len(pa.damage.parts["radiation"].protectors), 1)
        # irradiation on radiation: (16 - threshold 15) x 0.5
        before = pa.damage.parts["radiation"].health
        pa.damage.hit("radiation", "irradiation", 16.0, 0.0, w.core.now_ms)
        self.assertAlmostEqual(before - pa.damage.parts["radiation"].health, 0.5, places=4)
        press(a, I.BACK_SLOT_USE_BIT)
        w.run(300)
        self.assertFalse(pa.oxygen.active)
        left = pa.oxygen.amount_ms
        w.run(1000)
        self.assertEqual(pa.oxygen.amount_ms, left)
        self.assertEqual(pa.damage.parts["radiation"].protectors, [])
        self.assertEqual(a.faults + b.faults, [])


class BoosterTest(unittest.TestCase):
    def test_anomaly_damage_booster_scales_anomaly_hit_types(self):
        plain = C.DamageModel(DATA.body_parts)
        boosted = C.DamageModel(DATA.body_parts, None, {C.BOOSTER_ANOMALY_DAMAGE: -50.0})
        for dm in (plain, boosted):
            dm.hit("body", "irradiation", 0.4, 0.0, 1000)
            dm.hit("back", "injury", 0.4, 0.0, 1000)
        lost = lambda dm, part: dm.parts[part].max_health - dm.parts[part].health  # noqa: E731
        self.assertAlmostEqual(lost(boosted, "body"), 0.5 * lost(plain, "body"), places=5)
        self.assertAlmostEqual(lost(boosted, "back"), lost(plain, "back"), places=6)


class UsageTest(unittest.TestCase):
    def test_used_items_in_the_result_and_finite_supply(self):
        w, a, b = duel(loadout({8: M.ItemInstance(AMMO_545, 2, 30, 40)}))
        pa = w.match.players[0]
        pos = a.local["position"]
        # (a weapon is shown 0.7 s after the spawn) 0.6 s of fire into the sky, a medkit, a trap
        a.bot = Script([(80, pos, 0.0, 0.9, 0), (55, pos, 0.0, 0.9, FIRE), (5, pos, 0.0, 0.9, 0),
                        (1, pos, 0.0, 0.9, QS2_DOWN), (10 ** 9, pos, 0.0, 0.9, 0)])
        w.run(2000)
        fired = pa.shots_fired
        self.assertGreater(fired, 3)
        place_trap(w, a)
        w.run(1000)
        used = {e["slot"]: e for e in w.match.player_result(pa)["used"]}
        self.assertEqual(sorted(used), [8, 13, 14])
        self.assertEqual((used[8]["id"], used[8]["dict_id"], used[8]["count"]), (2, AMMO_545, fired))
        self.assertEqual((used[13]["dict_id"], used[13]["count"]), (TRAP, 1))
        self.assertEqual((used[14]["dict_id"], used[14]["count"]), (MEDKIT, 1))
        # the next life gets what is left: min(30 a life, 40 - fired) rounds, 1 medkit, 1 trap
        a.bot = None
        a.commit_suicide()
        self.assertTrue(w.run(8000, until=lambda: a.inserted[0]["spawns"] == 2))
        self.assertEqual(a.local["weapons"][8], min(30, 40 - fired))
        self.assertEqual(a.local["weapons"][13], 1)
        self.assertEqual(a.local["weapons"][14], 1)
        self.assertEqual(a.faults + b.faults, [])


if __name__ == "__main__":
    unittest.main()
