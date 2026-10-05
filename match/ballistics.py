"""Shot direction: the client's aim frame, dispersion and recoil, ported from game_core.

How the client aims a round (weapon_core::update_bones_matrices, weapon_core.cpp:885-945):
the bullet leaves along the k axis of character_head_transform, the animated Head bone
(animation::calculated_head_matrix). Neither look_pitch nor the recoil are applied to the
view as plain angles; both are additive ANIMATIONS on the Head chain:
  * look:   time fraction = look_pitch / 2 + 0.5 of the 1st-view "*_look" clip
            (weapon_user_animations_selector.cpp:279-301); look_pitch is clamped to [-1, 1]
            (player.cpp:300). Measured from the clips (match/data/view_animation_angles.json,
            match/tools/extract_view_animation_angles.py): -1 -> -75.4 deg, +1 -> +70.7 deg
            standing, piecewise linear. look_pitch is NOT radians.
  * recoil: weapon_core::selected_animations (weapon_core.cpp:326-344) feeds the
            weapon_recoil_calculator coefficients as time fractions of recoil_vert
            (clamp(v) + 0.5) and recoil_horiz (0.5 - clamp(h)), |v|,|h| < 0.5; the clips turn
            the camera by about +-21.5 deg pitch and +-18.7 deg yaw at their ends; recoil_back
            does not touch the Head.
So the 0x43 look_pitch/yaw never contain recoil (player_input_handler.cpp / player.cpp:300
only add mouse deltas): the server adds it itself, from its own replay of the recoil
calculator, and nothing is applied twice.

Dispersion (weapon_core::get_dispersed_bullet_dir, weapon_core.cpp:510-523), per pellet:
    angle  = clamp(normal_random.rand_n(1), -1, 1)                 normal_random.h:42-56
    amount = dispersion_calculator::get_dispersion() * angle        (degrees)
    axis   = rotate(head.i about head.k by random32.random_f(2 pi))
    dir    = rotate(head.k about axis by deg2rad(amount))
The two PRNGs are the weapon's own, seeded by the server in 0x84 (weapon_core::deserialize,
weapon_core.cpp:1029-1030), so the server replays exactly the sequence the client draws.
[A] the decompiled second create_rotation reads (k, angle).transform_direction(axis),
which would fire perpendicular to the view; the function is a 90.7% match whose residual
is exactly that argument's evaluation order, so the physical reading above is used.
get_dispersion (dispersion_calculator.cpp:34-43):
    base_dispersion * ammo.dispersion * (aimed ? aim_multiplier : from_the_hip_multiplier)
      + (weapon_dispersion_calculator + character_dispersion_calculator) * shooting_skill
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

Vec3 = Tuple[float, float, float]

STAND, CROUCH, SPRINT, JUMP = "stand", "crouch", "sprint", "jump"
AIM_BIT = 0x80                         # weapon_core::is_trying_to_aim (weapon_core.cpp:1238)
PI_X2 = 6.2831855                      # math::pi_x2 as float
ANGLES_PATH = Path(__file__).resolve().parent / "data" / "view_animation_angles.json"


def _f32(x: float) -> float:
    return struct.unpack("<f", struct.pack("<f", x))[0]


# ============================================================================ PRNGs
class Random32:
    """vostok::math::random32 (math_randoms_generator.h:13-28)."""

    def __init__(self, seed: int = 0) -> None:
        self.seed = seed & 0xFFFFFFFF

    def random(self, rng: int) -> int:
        self.seed = (0x08088405 * self.seed + 1) & 0xFFFFFFFF
        return (self.seed * rng) >> 32

    def random_f(self, rng: float) -> float:
        return _f32(rng * _f32(self.random(1024 * 1024) / (1024.0 * 1024.0)))


class NormalRandom:
    """survarium::normal_random (normal_random.h): MSVC rand() LCG + exponential rejection."""

    def __init__(self, seed: int = 1) -> None:
        self.seed = seed & 0xFFFFFFFF

    def rand_i(self) -> int:
        self.seed = (self.seed * 0x343FD + 0x269EC3) & 0xFFFFFFFF
        return (self.seed >> 16) & 0x7FFF

    def rand_f(self) -> float:
        return _f32(self.rand_i() / 32767.0)

    def rand_n(self, sigma: float) -> float:
        if sigma == 0.0:
            return 0.0
        while True:
            r = self.rand_f()
            y = _f32(-math.log(r)) if r > 0.0 else math.inf
            limit = _f32(math.exp(_f32(-_f32((y - 1.0) ** 2) * 0.5))) if y != math.inf else 0.0
            if not self.rand_f() > limit:
                break
        k = _f32(y * sigma * 1.2539185)
        return k if self.rand_i() & 1 else -k


# ============================================================================ params
def _get(cfg: Optional[dict], key: str, default: float) -> float:
    if cfg and key in cfg and cfg[key] is not None:
        return float(cfg[key])
    return default


@dataclass
class WeaponDispersionParams:
    """weapon_dispersion_params(cfg["dispersion"]) (weapon_dispersion_params.cpp:24-62)."""
    base_dispersion: float = 0.0
    from_the_hip_multiplier: float = 1.0
    aim_multiplier: float = 1.0
    speed_of_aiming: float = 1.0
    one_shoot_dispersion_amount: float = 0.0
    config_one_shoot_dispersion_amount: float = 0.0
    reload_dispersion_amount: float = 1.0
    growth_speed: float = 1.0
    max_dispersion: float = 2.0

    @classmethod
    def from_cfg(cls, cfg: Optional[dict]) -> "WeaponDispersionParams":
        return cls(_get(cfg, "base_dispersion", 0.0), _get(cfg, "from_the_hip_multiplier", 1.0),
                   _get(cfg, "aim_multiplier", 1.0), _get(cfg, "speed_of_aiming", 1.0),
                   0.0,       # the retail ctor clears it after reading (see the .cpp:57-61)
                   _get(cfg, "one_shoot_dispersion_amount", 1.0),
                   _get(cfg, "reload_dispersion_amount", 1.0), _get(cfg, "growth_speed", 1.0),
                   _get(cfg, "max_dispersion", 2.0))


@dataclass
class WeaponRecoilParams:
    """weapon_recoil_params(cfg["recoil"]) (weapon_recoil_params.cpp)."""
    first_shoot_side_recoil: float = 0.0
    shoot_side_recoil: float = 0.0
    first_shoot_back_recoil: float = 0.0
    shoot_back_recoil: float = 0.0
    shoot_recoil_min_angle: float = 0.0
    shoot_recoil_angle_range: float = 0.0
    additive_recoil_time: float = 0.001
    additive_side_recoil: float = 0.0
    additive_back_recoil: float = 0.0
    additive_recoil_min_angle: float = 0.0
    additive_recoil_angle_range: float = 0.0
    side_compensation_speed: float = 0.0
    back_compensation_speed: float = 0.0

    @classmethod
    def from_cfg(cls, cfg: Optional[dict]) -> "WeaponRecoilParams":
        p = cls()
        cfg = cfg or {}
        for k in ("first_shoot_side_recoil", "shoot_side_recoil", "first_shoot_back_recoil",
                  "shoot_back_recoil", "additive_recoil_time", "additive_side_recoil",
                  "additive_back_recoil", "side_compensation_speed", "back_compensation_speed"):
            if k in cfg:
                setattr(p, k, float(cfg[k]))
        if "shoot_recoil_min_angle" in cfg and "shoot_recoil_max_angle" in cfg:
            p.shoot_recoil_min_angle = float(cfg["shoot_recoil_min_angle"])
            p.shoot_recoil_angle_range = float(cfg["shoot_recoil_max_angle"]) - p.shoot_recoil_min_angle
        if "additive_recoil_min_angle" in cfg and "additive_recoil_max_angle" in cfg:
            p.additive_recoil_min_angle = float(cfg["additive_recoil_min_angle"])
            p.additive_recoil_angle_range = float(cfg["additive_recoil_max_angle"]) - p.additive_recoil_min_angle
        return p


@dataclass
class CharacterParams:
    """gameplay/players/default.player: character_dispersion_params / character_recoil_params
    (character_dispersion_params.cpp, character_recoil_params.cpp; defaults 1.0)."""
    dispersion: Dict[str, float]
    recoil: Dict[str, float]

    @classmethod
    def from_cfg(cls, player: Optional[dict]) -> "CharacterParams":
        player = player or {}
        dkeys = ("idle_multiplier", "idle_aim_multiplier", "walk_multiplier", "walk_aim_multiplier",
                 "run_multiplier", "jump_multiplier", "crouch_multiplier", "crouch_aim_multiplier",
                 "crouch_walk_multiplier", "crouch_walk_aim_multiplier", "prone_multiplier",
                 "prone_aim_multiplier", "injury_penalty_for_double_handed",
                 "injury_penalty_for_one_handed")
        rkeys = ("crouch_multiplier", "stand_multiplier", "aimed_crouch_multiplier", "aimed_stand_multiplier")
        dc = player.get("character_dispersion_params") or {}
        rc = player.get("character_recoil_params") or {}
        return cls({k: _get(dc, k, 1.0) for k in dkeys}, {k: _get(rc, k, 1.0) for k in rkeys})


# ============================================================================ dispersion
class WeaponDispersionCalculator:
    """weapon_dispersion_calculator.cpp. growth_speed / max_value are never set from the
    config by the client (ctor constants 5 and 1); one_shoot comes in as 0 (see params)."""

    def __init__(self, one_shoot: float, reload_amount: float, aiming_speed: float) -> None:
        self.one_shoot = one_shoot
        self.reload_amount = reload_amount
        self.growth_speed = 5.0
        self.aiming_speed = aiming_speed
        self.max_value = 1.0
        self.target = 0.0
        self.current = 0.0
        self.time = 0

    def tick(self, now_ms: int) -> None:
        if not self.time:
            self.time = now_ms
            return
        if self.time >= now_ms:
            return
        dt = (now_ms - self.time) * 0.001
        self.time = now_ms
        self.target = max(self.target - self.aiming_speed * dt, 0.0)
        if dt != 0.0 and self.current != self.target:
            if self.current > self.target:
                self.current = max(self.current - self.aiming_speed * dt, self.target)
            elif self.current < self.target:
                self.current = min(self.current + self.growth_speed * dt, self.target)

    def fire(self) -> None:
        self.target = min(self.target + self.one_shoot, self.max_value)

    def reload(self) -> None:
        self.target = min(self.target + self.reload_amount, self.max_value)


class CharacterDispersionCalculator:
    """character_dispersion_calculator.cpp."""

    def __init__(self, params: Dict[str, float], aiming_speed: float) -> None:
        self.p = params
        self.target = self.current = self.value = params["idle_multiplier"]
        self.smoothing = 5.0
        self.aiming_speed = aiming_speed
        self.time = 0

    def target_koef(self, state: str, moving: bool, aiming: bool) -> float:
        p = self.p
        if state == STAND:
            if moving:
                return p["walk_aim_multiplier"] if aiming else p["walk_multiplier"]
            return p["idle_aim_multiplier"] if aiming else p["idle_multiplier"]
        if state == CROUCH:
            if moving:
                return p["crouch_walk_aim_multiplier"] if aiming else p["crouch_walk_multiplier"]
            return p["crouch_aim_multiplier"] if aiming else p["crouch_multiplier"]
        if state == SPRINT:
            return p["run_multiplier"]
        if state == JUMP:
            return p["jump_multiplier"]
        return 1.0

    def broken_hands_penalty(self, broken: int, double_handed: bool) -> float:
        if broken <= 0:
            return 1.0
        if broken == 1:
            return self.p["injury_penalty_for_double_handed"] if double_handed else 1.0
        return self.p["injury_penalty_for_double_handed"] if double_handed \
            else self.p["injury_penalty_for_one_handed"]

    def tick(self, state: str, moving: bool, aiming: bool, broken: int, double_handed: bool,
             now_ms: int) -> None:
        if self.time == 0:
            self.time = now_ms
            return
        if self.time >= now_ms:
            return
        dt = (now_ms - self.time) / 1000.0
        self.time = now_ms
        self.target = self.target_koef(state, moving, aiming) * self.broken_hands_penalty(broken, double_handed)
        self.current = max(self.target, self.current - self.aiming_speed * dt)
        if self.value > self.current:
            self.value = max(self.current, self.value - self.smoothing * dt)
        elif self.current > self.value:
            self.value = min(self.current, self.value + self.smoothing * dt)


# ============================================================================ recoil
class CharacterRecoilCalculator:
    """character_recoil_calculator.cpp (all multipliers are 1.0 in default.player)."""

    def __init__(self, params: Dict[str, float]) -> None:
        self.p = params
        self.target = 0.0
        self.current = 0.0
        self.time = 0

    def tick(self, state: str, aiming: bool, now_ms: int) -> None:
        if state == CROUCH:
            self.target = self.p["aimed_crouch_multiplier"] if aiming else self.p["crouch_multiplier"]
        else:
            self.target = self.p["aimed_stand_multiplier"] if aiming else self.p["stand_multiplier"]
        dt = (now_ms - self.time) * 0.001 if now_ms > self.time else 0.0
        self.time = now_ms
        if self.current != self.target:
            if self.target < self.current:
                self.current = max(self.current - dt, self.target)
            else:
                self.current = min(self.current + dt, self.target)


class WeaponRecoilCalculator:
    """weapon_recoil_calculator.cpp (tick: 38/38 statements matched). The coefficient
    interpolation and the compensation only run while the additive-recoil timer is
    pending, exactly as retail. random32 is constructed with seed 0 and never reseeded."""

    INTERPOLATION_TIME = 0.1           # m_interpolator( 0.1f ), linear_interpolator.cpp:19-28

    def __init__(self, params: WeaponRecoilParams, rng: Optional[Random32] = None) -> None:
        self.p = params
        self.rng = rng if rng is not None else Random32(0)
        self.player_multiplier = 1.0
        self.compensation_multiplier = 1.0
        self.time_since_shoot = 0.0
        self.additive_timer = 0.0
        self.time_since_change = 0.0
        self.vertical = 0.0
        self.horizontal = 0.0
        self.back = 0.0
        self.target_vertical = 0.0
        self.target_horizontal = 0.0
        self.target_back = 0.0
        self.last_ms = 0

    def _angle(self, rng_range: float) -> float:
        return self.rng.random_f(rng_range)

    def _amount(self, rng_range: float) -> float:
        return max(0.25, self.rng.random_f(1.0)) * rng_range

    def tick(self, now_ms: int) -> None:
        if not self.last_ms:
            self.last_ms = now_ms
            return
        if self.last_ms >= now_ms:
            return
        dt = (now_ms - self.last_ms) * 0.001
        self.last_ms = now_ms
        self.time_since_shoot += dt
        self.time_since_change = min(self.time_since_change + dt, self.INTERPOLATION_TIME)
        if self.additive_timer != 0.0:
            p = self.p
            if dt < self.additive_timer:
                self.additive_timer -= dt
            else:
                angle = math.radians(p.additive_recoil_min_angle + self._angle(p.additive_recoil_angle_range))
                force = self._amount(1.0)
                amount = self.player_multiplier * force * p.additive_side_recoil
                self.target_vertical = self.vertical + math.cos(angle) * amount
                self.target_horizontal = self.horizontal + math.sin(angle) * amount
                self._normalize_target()
                self.target_back = max(self.target_back - p.additive_back_recoil * force, 0.0)
                self.additive_timer = 0.0
                self.back = max(self.back - amount, 0.0)
            k = self.time_since_change / self.INTERPOLATION_TIME
            self.vertical = self.target_vertical * k + self.vertical * (1.0 - k)
            self.horizontal = self.target_horizontal * k + self.horizontal * (1.0 - k)
            self.back = self.target_back * k + self.back * (1.0 - k)
            self._compensate(dt)

    def _normalize_target(self) -> None:
        sq = self.target_vertical ** 2 + self.target_horizontal ** 2
        if sq > 1.0:
            n = math.sqrt(sq)
            self.target_vertical /= n
            self.target_horizontal /= n

    def _compensate(self, dt: float) -> None:
        p = self.p
        add = math.sqrt(self.target_vertical ** 2 + self.target_horizontal ** 2)
        c = (p.side_compensation_speed + add) * dt * self.compensation_multiplier
        tv, th = self.target_vertical, self.target_horizontal
        self.target_vertical = tv - math.copysign(c, tv) if abs(tv) > c else 0.0
        self.target_horizontal = th - math.copysign(c, th) if abs(th) > c else 0.0
        add_b = math.sqrt(2.0 * self.target_back ** 2)
        cb = (p.back_compensation_speed + add_b) * dt * self.compensation_multiplier
        self.target_back = self.target_back - cb if cb < self.target_back else 0.0

    def fire(self) -> None:
        p = self.p
        angle_deg = p.shoot_recoil_min_angle + self._angle(p.shoot_recoil_angle_range)
        force = self._amount(1.0)
        first = self.target_vertical == 0.0 and self.target_horizontal == 0.0
        recoil = self.player_multiplier * force * (p.first_shoot_side_recoil if first else p.shoot_side_recoil)
        a = math.radians(angle_deg)
        self.target_vertical = self.vertical + math.cos(a) * recoil
        self.target_horizontal = self.horizontal + math.sin(a) * recoil
        self._normalize_target()
        back = self.player_multiplier * force * (p.first_shoot_back_recoil if first else p.shoot_back_recoil)
        self.target_back = min(self.target_back + back, 1.0)
        self.time_since_change = 0.0
        self.time_since_shoot = 0.0
        self.additive_timer = p.additive_recoil_time

    def reset(self) -> None:
        """reload / chamber_a_round (weapon_recoil_calculator.cpp reset)."""
        self.time_since_change = 0.0
        self.time_since_shoot = 0.0
        self.additive_timer = 0.0
        self.target_vertical = 0.0
        self.target_horizontal = 0.0


# ============================================================================ view angles
class ViewAngles:
    """Camera pitch for a look_pitch and the camera turn for recoil coefficients, from the
    1st-view animation clips (match/data/view_animation_angles.json)."""

    # fallbacks = the measured AK-family tables (identical for all 11 weapons)
    LOOK = {STAND: [(-1.0, -75.4334), (0.0, 0.0), (1.0, 70.7402)],
            CROUCH: [(-1.0, -70.54), (0.0, 0.0), (1.0, 72.97)]}
    VERT = [(0.0, -21.3449), (0.5, 0.0), (1.0, 21.604)]          # clip fraction -> pitch deg
    HORIZ = [(0.0, 18.7063), (0.5, 0.0), (1.0, -18.6253)]        # clip fraction -> yaw deg (+ = right)

    def __init__(self, path: Path = ANGLES_PATH) -> None:
        self.look = dict(self.LOOK)
        self.vert = list(self.VERT)
        self.horiz = list(self.HORIZ)
        self.source = "built-in"
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            w = data["ak_74u"]
            self.look = {STAND: [tuple(r) for r in w["look"]["stand_hip"]["fine_table_lookpitch_pitch"]],
                         CROUCH: [tuple(r) for r in w["look"]["crouch_hip"]["fine_table_lookpitch_pitch"]]}
            self.vert = [(r[0], r[1]) for r in w["recoil"]["stand_hip"]["vert"]]
            self.horiz = [(r[0], r[2]) for r in w["recoil"]["stand_hip"]["horiz"]]
            self.source = str(path)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass

    @staticmethod
    def _interp(table: Sequence[Tuple[float, float]], x: float) -> float:
        if x <= table[0][0]:
            return table[0][1]
        for (x0, y0), (x1, y1) in zip(table, table[1:]):
            if x <= x1:
                return y0 + (y1 - y0) * (x - x0) / (x1 - x0) if x1 != x0 else y1
        return table[-1][1]

    def camera_pitch_deg(self, look_pitch: float, crouched: bool) -> float:
        lp = min(max(look_pitch, -1.0), 1.0)
        return self._interp(self.look[CROUCH if crouched else STAND], lp)

    def look_pitch_for(self, pitch_deg: float, crouched: bool = False) -> float:
        """Inverse of camera_pitch_deg (tools and tests)."""
        inv = [(y, x) for x, y in self.look[CROUCH if crouched else STAND]]
        return self._interp(inv, pitch_deg)

    def recoil_deg(self, vertical: float, horizontal: float) -> Tuple[float, float]:
        """(pitch up, yaw toward the camera's right) for weapon_recoil_calculator coefficients."""
        e = 1e-7
        tv = min(max(vertical, -0.5 + e), 0.5 - e) + 0.5
        th = 0.5 - min(max(horizontal, -0.5 + e), 0.5 - e)
        return self._interp(self.vert, tv), self._interp(self.horiz, th)


VIEW = ViewAngles()


# ============================================================================ geometry
def create_rotation_axis(axis: Vec3, angle: float):
    """vostok::math::create_rotation(float3 axis, float angle) (math_float4x4.cpp:322-345)."""
    s, c = math.sin(angle), math.cos(angle)
    ic = 1.0 - c
    x, y, z = axis
    return ((x * x + (1 - x * x) * c, x * y * ic - z * s, x * z * ic + y * s),
            (x * y * ic + z * s, y * y + (1 - y * y) * c, y * z * ic - x * s),
            (x * z * ic - y * s, y * z * ic + x * s, z * z + (1 - z * z) * c))


def transform_direction(m, v: Vec3) -> Vec3:
    """float4x4::transform_direction: row vectors (v.x * i + v.y * j + v.z * k)."""
    i, j, k = m
    return (v[0] * i[0] + v[1] * j[0] + v[2] * k[0],
            v[0] * i[1] + v[1] * j[1] + v[2] * k[1],
            v[0] * i[2] + v[1] * j[2] + v[2] * k[2])


def aim_frame(yaw: float, pitch_deg: float, yaw_right_deg: float = 0.0) -> Tuple[Vec3, Vec3]:
    """(forward k, right i) of the head camera. create_rotation_y(yaw).k = (-sin, 0, cos)
    (math_float4x4_inline.h:298); turning right lowers the yaw."""
    y = yaw - math.radians(yaw_right_deg)
    p = math.radians(pitch_deg)
    cp = math.cos(p)
    return (-math.sin(y) * cp, math.sin(p), math.cos(y) * cp), (math.cos(y), 0.0, math.sin(y))


def dispersed_direction(forward: Vec3, right: Vec3, dispersion_deg: float,
                        rng: Random32, normal: NormalRandom) -> Vec3:
    angle = min(max(normal.rand_n(1.0), -1.0), 1.0)
    amount = dispersion_deg * angle
    random_k = rng.random_f(PI_X2)
    axis = transform_direction(create_rotation_axis(forward, random_k), right)
    d = transform_direction(create_rotation_axis(axis, math.radians(amount)), forward)
    n = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
    return (d[0] / n, d[1] / n, d[2] / n)


# ============================================================================ per weapon
class ShotModel:
    """Everything that turns the reported view into the directions of one shot: the
    weapon's dispersion_calculator, recoil_calculator and its two PRNGs."""

    def __init__(self, dispersion: WeaponDispersionParams, recoil: WeaponRecoilParams,
                 character: CharacterParams, seeds: Tuple[int, int],
                 recoil_rng: Optional[Random32] = None, double_handed: bool = True,
                 shooting_skill: float = 1.0, aiming_speed_coeff: float = 1.0,
                 spread_growth_from_config: bool = False) -> None:
        self.params = dispersion
        aiming_speed = dispersion.speed_of_aiming * aiming_speed_coeff
        one_shoot = dispersion.config_one_shoot_dispersion_amount if spread_growth_from_config \
            else dispersion.one_shoot_dispersion_amount
        self.weapon_disp = WeaponDispersionCalculator(one_shoot, dispersion.reload_dispersion_amount,
                                                      aiming_speed)
        self.char_disp = CharacterDispersionCalculator(character.dispersion, aiming_speed)
        self.recoil = WeaponRecoilCalculator(recoil, recoil_rng)
        self.char_recoil = CharacterRecoilCalculator(character.recoil)
        self.rng = Random32(seeds[0])
        self.normal = NormalRandom(seeds[1])
        self.double_handed = double_handed
        self.shooting_skill = shooting_skill
        self.aimed = False
        self.state = STAND

    def tick(self, now_ms: int, state: str, moving: bool, aiming: bool, broken_hands: int) -> None:
        """weapon_core::update_dispersion + update_recoil for the client time now_ms."""
        self.state, self.aimed = state, aiming
        self.weapon_disp.tick(now_ms)
        self.char_disp.tick(state, moving, aiming, broken_hands, self.double_handed, now_ms)
        self.char_recoil.tick(state, aiming, now_ms)          # recoil_calculator::tick order
        self.recoil.tick(now_ms)
        self.recoil.player_multiplier = self.char_recoil.current

    def dispersion_deg(self, ammo_dispersion: float) -> float:
        p = self.params
        return p.base_dispersion * ammo_dispersion * (p.aim_multiplier if self.aimed else p.from_the_hip_multiplier) \
            + (self.weapon_disp.current + self.char_disp.value) * self.shooting_skill

    def view(self, yaw: float, look_pitch: float, crouched: bool, use_recoil: bool = True
             ) -> Tuple[Vec3, Vec3]:
        pitch = VIEW.camera_pitch_deg(look_pitch, crouched)
        right_deg = 0.0
        if use_recoil:
            dp, dy = VIEW.recoil_deg(self.recoil.vertical, self.recoil.horizontal)
            pitch += dp
            right_deg = dy
        return aim_frame(yaw, max(-89.9, min(89.9, pitch)), right_deg)

    def shoot(self, yaw: float, look_pitch: float, crouched: bool, pellets: int,
              ammo_dispersion: float, use_dispersion: bool = True, use_recoil: bool = True
              ) -> List[Vec3]:
        """weapon_core::instant_fire: one direction per pellet, then recoil/dispersion fire()."""
        forward, right = self.view(yaw, look_pitch, crouched, use_recoil)
        disp = self.dispersion_deg(ammo_dispersion)
        out = []
        for _ in range(max(1, pellets)):
            if use_dispersion:
                out.append(dispersed_direction(forward, right, disp, self.rng, self.normal))
            else:
                out.append(forward)
        self.recoil.fire()
        self.weapon_disp.fire()
        return out

    def on_reload(self) -> None:
        """weapon_core::instant_reload: recoil reset + dispersion reload bump."""
        self.recoil.reset()
        self.weapon_disp.reload()

    def on_chamber(self) -> None:
        self.recoil.reset()
