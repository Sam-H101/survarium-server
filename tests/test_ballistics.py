"""World collision, dispersion and recoil of server-side hitscan (match/level_collision.py,
match/ballistics.py). Needs match/data/level_03.collision
(python match/tools/build_level_collision.py)."""

from __future__ import annotations

import json
import math
import random
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from match import ballistics as B  # noqa: E402
from match import combat as C  # noqa: E402
from match.level_collision import default_cache_path, load_level  # noqa: E402
from test_m3 import FIRE, Lobbyless, aim, m3_config  # noqa: E402
from test_match import DATA  # noqa: E402

COL = load_level("level_03")
RAW = Path(__file__).resolve().parents[2] / "game_data" / "json" / "raw" / "gameplay"

# respawn points 23 and 26 (team_1 safe area): open line of sight, 15.3 m apart
OPEN_A = (-37.86, 0.86, -101.23)
OPEN_B = None                       # filled from maps data in setUpModule
# 5.3 m east of point 23 is the barracks' concrete wall; behind it, ground at y 0.526
WALL_VICTIM = (-31.1, 0.526, -101.23)


def setUpModule():
    global OPEN_B
    pts = {p.point_id: p for p in DATA.respawn_points("level_03")}
    OPEN_B = tuple(pts[26].position)


def eye(p):
    return (p[0], p[1] + C.EYE_STAND, p[2])


def chest(p):
    return (p[0], p[1] + 1.2, p[2])


def angle_deg(a, b):
    d = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(d))


def weapon_params(name):
    raw = json.loads((RAW / "weapons" / f"{name}.options.json").read_text(encoding="utf-8"))
    return B.WeaponDispersionParams.from_cfg(raw["dispersion"]), B.WeaponRecoilParams.from_cfg(raw["recoil"])


def character():
    return B.CharacterParams.from_cfg(DATA.character)


@unittest.skipIf(COL is None, f"no collision cache at {default_cache_path('level_03')}")
class LevelCollisionTest(unittest.TestCase):
    def test_cache_coverage(self):
        s = COL.header["stats"]
        self.assertGreater(COL.triangle_count, 500000)
        self.assertGreater(s["objects_with_geometry"], 1100)
        self.assertGreater(s["by_kind"]["terrain"]["triangles"], 10000)

    def test_respawn_points_stand_on_ground(self):
        for p in DATA.respawn_points("level_03"):
            g = COL.ground_below(tuple(p.position), up=0.5, depth=50.0)
            self.assertIsNotNone(g, p.point_id)
            self.assertTrue(-0.5 <= p.position[1] - g <= 2.0, (p.point_id, p.position, g))

    def test_wall_blocks_and_open_line_is_clear(self):
        self.assertFalse(COL.line_of_sight(eye(OPEN_A), chest(WALL_VICTIM)))
        self.assertTrue(COL.line_of_sight(eye(OPEN_A), chest(OPEN_B)))
        d = [b - a for a, b in zip(eye(OPEN_A), chest(WALL_VICTIM))]
        n = math.sqrt(sum(x * x for x in d))
        hit = COL.trace(eye(OPEN_A), tuple(x / n for x in d), n, pierce=0.5)
        self.assertIsNotNone(hit)
        self.assertEqual(COL.material_names[hit.material], "concrete")
        self.assertLess(hit.distance, n)

    def test_back_faces_and_thin_materials(self):
        # from behind the wall the same triangle is a back face: a ray leaving the barracks
        # interior towards OPEN_A from outside still stops at the OUTER face of the wall
        d = [b - a for a, b in zip(chest(WALL_VICTIM), eye(OPEN_A))]
        n = math.sqrt(sum(x * x for x in d))
        hit = COL.trace(chest(WALL_VICTIM), tuple(x / n for x in d), n, pierce=0.5)
        self.assertIsNotNone(hit)
        # resistance rules (bullet.cpp collide_front_face): glass 0.1 lets a 0.5 pierce through
        self.assertLessEqual(COL.resistance[29], 0.5)
        self.assertGreater(COL.resistance[19], 0.5)

    def test_rays_per_tick_stay_under_budget(self):
        """24 rays of 150 m (8 shooters x 3 pellets) per 33 ms tick, from the respawn
        areas in random directions, averaged over 20 ticks."""
        rng = random.Random(5)
        pts = [tuple(p.position) for p in DATA.respawn_points("level_03")]
        ticks = []
        for _ in range(20):
            t0 = time.perf_counter()
            for _ in range(24):
                p = rng.choice(pts)
                a, e = rng.uniform(0, 2 * math.pi), rng.uniform(-0.25, 0.25)
                COL.trace(eye(p), (math.cos(a) * math.cos(e), math.sin(e), math.sin(a) * math.cos(e)),
                          150.0, pierce=0.5)
            ticks.append(time.perf_counter() - t0)
        avg = sum(ticks) / len(ticks)
        print(f"\n  24 rays/tick: avg {avg * 1e3:.1f} ms, worst tick {max(ticks) * 1e3:.1f} ms")
        self.assertLess(avg, 0.033)


class PrngTest(unittest.TestCase):
    def test_client_generators(self):
        n = B.NormalRandom(1)
        self.assertEqual([n.rand_i() for _ in range(3)], [41, 18467, 6334])   # MSVC rand()
        r = B.Random32(0)
        self.assertEqual(r.random(1 << 20), 0)
        self.assertEqual(r.seed, 1)
        self.assertEqual(r.random(100), (0x08088406 * 100) >> 32)


class SpreadTest(unittest.TestCase):
    def converge(self, model, state=B.STAND, moving=False, aiming=False):
        t = 1000
        for _ in range(200):
            t += 33
            model.tick(t, state, moving, aiming, 0)
        return t

    def sample(self, model, n, pellets=1, ammo_dispersion=1.0):
        fwd, _ = model.view(0.3, 0.1, False, use_recoil=False)
        angles = []
        for _ in range(n // pellets):
            for d in model.shoot(0.3, 0.1, False, pellets, ammo_dispersion, True, False):
                angles.append(angle_deg(fwd, d))
        return angles

    def check(self, name, expected, state=B.STAND, moving=False, aiming=False, pellets=1, ammo=1.0):
        disp, rec = weapon_params(name)
        m = B.ShotModel(disp, rec, character(), (12345, 678))
        self.converge(m, state, moving, aiming)
        self.assertAlmostEqual(m.dispersion_deg(ammo), expected, places=4)
        angles = self.sample(m, 6000, pellets, ammo)
        # rand_n(1) has E|x| = 1 before clamp(-1, 1): P(|x| >= 1) = 2(1 - Phi(0.7975)) = 0.425
        at_cap = sum(1 for a in angles if a >= expected * 0.999) / len(angles)
        mean = sum(angles) / len(angles) / expected
        self.assertLessEqual(max(angles), expected * 1.0001 + 1e-6)
        self.assertAlmostEqual(at_cap, 0.425, delta=0.03)
        self.assertAlmostEqual(mean, 0.698, delta=0.03)   # E[min(1.2539|N|, 1)]

    def test_configured_spread(self):
        # base_dispersion * ammo.dispersion + character multiplier (default.player)
        self.check("ak_74u", 0.3 + 0.5)                                    # idle, hip
        self.check("ak_74u", 0.3 + 0.1, aiming=True)                       # idle_aim
        self.check("ak_74u", 0.3 + 1.0, moving=True)                       # walk
        self.check("ak_74u", 0.3 + 0.05, state=B.CROUCH, aiming=True)      # crouch_aim
        self.check("ak_74u", 0.3 + 5.0, state=B.SPRINT)                    # run
        self.check("ak_74u", 0.3 + 3.0, state=B.JUMP)                      # jump
        self.check("rem_700", 0.08 + 0.1, aiming=True)
        self.check("rem_870", 0.6 * 3 + 0.5, pellets=12, ammo=3.0)          # 12 mm buck

    def test_azimuth_is_uniform(self):
        disp, rec = weapon_params("ak_74u")
        m = B.ShotModel(disp, rec, character(), (99, 7))
        self.converge(m)
        fwd, right = m.view(0.0, 0.0, False, use_recoil=False)
        up = (0.0, 1.0, 0.0)
        sx = sy = 0.0
        for d in m.shoot(0.0, 0.0, False, 4000, 1.0, True, False):
            sx += sum(a * b for a, b in zip(d, right))
            sy += sum(a * b for a, b in zip(d, up))
        lim = 4000 * math.radians(0.8) * 0.05
        self.assertLess(abs(sx), lim)
        self.assertLess(abs(sy), lim)

    def test_same_seeds_same_pellets(self):
        disp, rec = weapon_params("rem_870")
        runs = []
        for _ in range(2):
            m = B.ShotModel(disp, rec, character(), (777, 4242))
            self.converge(m)
            runs.append(m.shoot(1.0, 0.0, False, 12, 3.0))
        self.assertEqual(runs[0], runs[1])

    def test_reload_bumps_weapon_dispersion(self):
        disp, rec = weapon_params("ak_74u")
        m = B.ShotModel(disp, rec, character(), (1, 1))
        t = self.converge(m)
        base = m.dispersion_deg(1.0)
        m.on_reload()                    # reload_dispersion_amount 3, capped at 1
        t += 100
        m.tick(t, B.STAND, False, False, 0)
        self.assertGreater(m.dispersion_deg(1.0), base + 0.2)
        for _ in range(30):
            t += 33
            m.tick(t, B.STAND, False, False, 0)
        self.assertAlmostEqual(m.dispersion_deg(1.0), base, places=6)


class RecoilTest(unittest.TestCase):
    def test_burst_grows_and_recovers(self):
        disp, rec = weapon_params("ak_74u")
        m = B.ShotModel(disp, rec, character(), (1, 1))
        t = 1000
        m.tick(t, B.STAND, False, False, 0)
        kicks = []
        for _ in range(12):                              # 650 rpm: 92 ms per round
            m.shoot(0.0, 0.0, False, 1, 1.0, False, True)
            for _ in range(9):
                t += 10
                m.tick(t, B.STAND, False, False, 0)
            t += 2
            kicks.append(B.VIEW.recoil_deg(m.recoil.vertical, m.recoil.horizontal)[0])
        peak = max(kicks)
        self.assertGreater(peak, kicks[0] + 1.0)          # the view climbs over the burst
        self.assertGreater(peak, 2.0)
        for _ in range(100):                             # release the trigger for 1 s
            t += 10
            m.tick(t, B.STAND, False, False, 0)
        after = B.VIEW.recoil_deg(m.recoil.vertical, m.recoil.horizontal)[0]
        self.assertLess(after, peak)                      # compensation + additive pull-down
        self.assertEqual(m.recoil.additive_timer, 0.0)
        # retail: interpolation/compensation only run while the additive timer is pending,
        # so the residual kick then stays until the next shot (weapon_recoil_calculator::tick)
        for _ in range(100):
            t += 10
            m.tick(t, B.STAND, False, False, 0)
        self.assertEqual(B.VIEW.recoil_deg(m.recoil.vertical, m.recoil.horizontal)[0], after)
        m.on_reload()                                    # reload: targets reset (reset())
        self.assertEqual((m.recoil.target_vertical, m.recoil.target_horizontal), (0.0, 0.0))

    def test_recoil_turns_the_shot_not_the_reported_view(self):
        disp, rec = weapon_params("ak_74u")
        m = B.ShotModel(disp, rec, character(), (1, 1))
        t = 1000
        m.tick(t, B.STAND, False, False, 0)
        for _ in range(6):
            m.shoot(0.0, 0.0, False, 1, 1.0, False, True)
            t += 92
            m.tick(t, B.STAND, False, False, 0)
        d = m.shoot(0.0, 0.0, False, 1, 1.0, False, True)[0]
        pitch = math.degrees(math.asin(d[1]))
        expected = B.VIEW.camera_pitch_deg(0.0, False) + \
            B.VIEW.recoil_deg(m.recoil.vertical, m.recoil.horizontal)[0]
        self.assertGreater(pitch, 0.5)                    # look_pitch 0, the kick raised it
        self.assertAlmostEqual(pitch, expected, delta=0.2)

    def test_magnum_has_no_recoil(self):
        disp, rec = weapon_params("magnum")
        m = B.ShotModel(disp, rec, character(), (1, 1))
        t = 1000
        for _ in range(5):
            m.shoot(0.0, 0.0, False, 1, 1.0, False, True)
            t += 600
            m.tick(t, B.STAND, False, False, 0)
        self.assertEqual((m.recoil.vertical, m.recoil.horizontal), (0.0, 0.0))
        for v in B.VIEW.recoil_deg(m.recoil.vertical, m.recoil.horizontal):
            self.assertAlmostEqual(v, 0.0, places=2)

    def test_look_pitch_is_not_radians(self):
        self.assertAlmostEqual(B.VIEW.camera_pitch_deg(1.0, False), 70.74, places=1)
        self.assertAlmostEqual(B.VIEW.camera_pitch_deg(-1.0, False), -75.43, places=1)
        self.assertAlmostEqual(B.VIEW.look_pitch_for(B.VIEW.camera_pitch_deg(0.37, False)), 0.37, places=4)


@unittest.skipIf(COL is None, "no collision cache")
class WorldHitscanTest(unittest.TestCase):
    """Two real players over the simulated network: the server's hitscan respects walls."""

    def duel(self, **cfg):
        w = Lobbyless(2, m3_config(world_collision=True, **cfg))
        a, b = w.clients
        self.assertTrue(w.run(20000, until=w.all_controllable))
        return w, a, b

    def shooter(self, pos, target_pos):
        yaw, pitch = aim(pos, chest(target_pos), eye=C.EYE_STAND)
        return lambda c, now: (pos, yaw, pitch, FIRE)

    def test_open_line_hits(self):
        w, a, b = self.duel(dispersion=False, recoil=False)
        b.bot = lambda c, now: (OPEN_B, math.pi, 0.0, 0)
        w.run(500)
        a.bot = self.shooter(OPEN_A, OPEN_B)
        self.assertTrue(w.run(10000, until=lambda: a.kills))
        self.assertEqual(w.match.players[0].shots_blocked, 0)
        self.assertEqual(a.faults + b.faults, [])

    def test_wall_blocks_the_shot(self):
        w, a, b = self.duel(dispersion=False, recoil=False)
        b.bot = lambda c, now: (WALL_VICTIM, math.pi, 0.0, 0)
        w.run(500)
        a.bot = self.shooter(OPEN_A, WALL_VICTIM)
        w.run(3000)
        p = w.match.players[0]
        self.assertGreater(p.shots_fired, 10)
        self.assertEqual(p.shots_blocked, p.shots_fired)   # every round met the wall
        self.assertEqual([h for h in a.hits if h[1] == 1], [])
        self.assertTrue(w.match.players[1].alive)
        # the same shots without world collision would have hit
        w.match.collision = None
        w.run(3000)
        self.assertTrue([h for h in a.hits if h[1] == 1])

    def test_full_ballistics_still_kill_at_close_range(self):
        w, a, b = self.duel(dispersion=True, recoil=True)
        self.assertTrue(w.match.config.dispersion and w.match.config.recoil)
        b.bot = lambda c, now: (OPEN_B, math.pi, 0.0, 0)
        w.run(500)
        a.bot = self.shooter(OPEN_A, OPEN_B)
        self.assertTrue(w.run(15000, until=lambda: a.kills))
        p = w.match.players[0]
        self.assertGreaterEqual(p.shots_fired, p.hits_dealt)
        self.assertEqual(a.faults + b.faults, [])


if __name__ == "__main__":
    unittest.main()
