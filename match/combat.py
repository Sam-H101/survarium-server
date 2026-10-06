"""Server-side combat: a mirror of the client's damage model, the weapon state machine
reduced to what decides *when* a round leaves the barrel, and a hitscan against simple
capsules.

Authority (spec 6): the client never reports shots or hits. The server derives fire from
the 0x43 action bits, traces the shot itself and sends 0x89 hit_player to every client;
each client then runs the very same damage formula on its copy of the victim
(player::apply_hit_directly -> damage_model::hit_body_part). The server runs that formula
too (DamageModel below, a port of game_core/sources/damage_model.cpp,
body_part_parameters.cpp and hit_type_parameters.cpp) so that it knows when the victim
dies: death = the "death" affect (0) being applied to any body part, exactly the thresholds
the client evaluates. Client and server therefore agree on health without 0x9e.

Hitscan: each pellet's direction comes from match/ballistics.py (the client's look
animation, replayed recoil and seeded dispersion); it is traced against capsules around the
other players and, with the level cache loaded, against the static collision of the level
(match/level_collision.py: walls, terrain and props stop it, thin materials let it
through as in bullet.cpp). Still approximated: capsules instead of hit boxes, a straight
line instead of the ballistic trajectory (no drop or travel time).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .game_data import ArmourMods, BodyPartParams, GameData, WeaponInfo

Vec3 = Tuple[float, float, float]

# hit_affects_type_enum + affects_durations (game_core/hit_affects_type_enum.h)
AFFECT_DEATH = 0
AFFECT_COUNT = 9
AFFECT_DURATIONS_S = (10, 10, 10, 100, 10, 120, 10, 10, 10)
# affect_event_type_enum
AFFECT_APPLYING, AFFECT_RECALLING, AFFECT_CANCELING = 0, 1, 2

DAMAGE_TYPE_BULLET = "injury"        # bullet::collide_front_face always hits with "injury"

# boosters_enum ids used by player_parameters_modifyer::apply
BOOSTER_HEALTH_REGEN = 3
BOOSTER_PAIN_HEALTH = 7
BOOSTER_ANOMALY_DAMAGE = 9
# player_parameters_modifyer::apply: anomaly_damage_corr_perc scales these hit types
ANOMALY_DAMAGE_TYPES = ("irradiation", "ambustion", "intoxication", "electric_shock")


class DamageProtector:
    """damage_protector: reduce(body part, hit type, amount, armor piercing) -> amount and
    protect(body part, affect) -> True when the affect may not be applied. Items register
    one per body part (damage_model::register_body_part_damage_protector)."""

    def __init__(self, reduce=None, protect=None) -> None:
        self.reduce = reduce
        self.protect = protect


def threshold_protector(hit_type: str, hit_coeff: float, threshold: float) -> DamageProtector:
    """medkit::reduce_damage / oxygen_tank::reduce_damage for one (body part, hit type):
    0 below the threshold, else (amount - threshold) * hit_coeff; other hit types pass."""
    def reduce(part: str, htype: str, amount: float, ap: float) -> float:
        if htype != hit_type:
            return amount
        if threshold > amount:
            return 0.0
        return (amount - threshold) * hit_coeff
    return DamageProtector(reduce)


# =============================================================================== damage
@dataclass
class _HitType:
    armor: float
    reduce: float
    absorption: float
    bdb: Tuple[Tuple[str, float], ...]


class _BodyPart:
    def __init__(self, p: BodyPartParams) -> None:
        self.name = p.name
        self.max_health = p.health
        self.health = p.health
        self.regeneration_speed = p.regeneration_speed
        # body_part_parameters ctor: floor(1000 * regeneration_timeout)
        self.regeneration_timeout = int(math.floor(1000.0 * p.regeneration_timeout))
        self.last_hit_time = 0
        self.hit_types = {k: _HitType(v.armor, v.reduce, v.absorption, v.bdb)
                          for k, v in p.hit_types.items()}
        self.thresholds = p.thresholds
        self.affects: List[Tuple[int, int]] = []         # (affect, expiry time)
        self.protectors: List[DamageProtector] = []      # kept across damage_model::reset

    def reset(self) -> None:
        self.health = self.max_health
        self.last_hit_time = 0
        self.affects.clear()

    def has_affect(self, affect: int) -> bool:
        return any(a == affect for a, _ in self.affects)


class DamageModel:
    """damage_model with affects_applying_type == type_apply_directly (the victim's own
    client), plus the player_parameters_modifyer from armour and boosters."""

    def __init__(self, parts: Sequence[BodyPartParams], armour: Optional[ArmourMods] = None,
                 boosters: Optional[Dict[int, float]] = None) -> None:
        self.parts: Dict[str, _BodyPart] = {}
        self.order: List[str] = []
        for p in parts:
            self.parts[p.name] = _BodyPart(p)
            self.order.append(p.name)
        self.events: List[Tuple[str, int, int]] = []     # (part, affect, event) since last drain
        # damage_model::m_damage_protectors: per hit type (reduce, absorb), from boosters
        self.type_protectors: Dict[str, List[float]] = {}
        self._apply_modifiers(armour, boosters or {})
        self.reset()

    # player_parameters_modifyer::apply
    def _apply_modifiers(self, armour: Optional[ArmourMods], boosters: Dict[int, float]) -> None:
        for name, (health, regen, hit_types) in (armour.parts if armour else {}).items():
            part = self.parts.get(name)
            if part is None:
                continue
            part.max_health += health
            part.regeneration_speed += regen
            for htype, (armor, reduce, absorption) in hit_types.items():
                ht = part.hit_types.get(htype)
                if ht is not None:           # set_parameters REPLACES the base values
                    ht.armor, ht.reduce, ht.absorption = armor, reduce, absorption
        pain = self.parts.get("pain")
        if pain is not None:
            pain.max_health *= 1.0 + boosters.get(BOOSTER_PAIN_HEALTH, 0.0) / 100.0
        scale = 1.0 + boosters.get(BOOSTER_HEALTH_REGEN, 0.0) / 100.0
        for part in self.parts.values():
            part.regeneration_speed *= scale
        anomaly = boosters.get(BOOSTER_ANOMALY_DAMAGE, 0.0)
        if anomaly != 0.0:
            for htype in ANOMALY_DAMAGE_TYPES:
                self.add_type_protector(htype, 1.0 + anomaly / 100.0, 0.0)

    def add_type_protector(self, hit_type: str, reduce: float, absorb: float) -> None:
        """damage_model::add_damage_protector: one booster_damage_protector per hit type;
        a second call multiplies reduce and adds absorb."""
        p = self.type_protectors.get(hit_type)
        if p is None:
            self.type_protectors[hit_type] = [reduce, absorb]
        else:
            p[0] *= reduce
            p[1] += absorb

    def register_protector(self, part_name: str, protector: DamageProtector) -> None:
        part = self.parts.get(part_name)
        if part is not None and protector not in part.protectors:
            part.protectors.append(protector)

    def unregister_protector(self, part_name: str, protector: DamageProtector) -> None:
        part = self.parts.get(part_name)
        if part is not None and protector in part.protectors:
            part.protectors.remove(protector)

    def reset(self) -> None:
        """damage_model::reset (player::insert_alive)."""
        for part in self.parts.values():
            part.reset()
        self.events.clear()

    @property
    def dead(self) -> bool:
        return any(p.has_affect(AFFECT_DEATH) for p in self.parts.values())

    def can_hit(self, part_name: str, damage_type: str = DAMAGE_TYPE_BULLET) -> bool:
        """The client dereferences get_body_part() / get_hit_parameters() unchecked."""
        part = self.parts.get(part_name)
        return part is not None and damage_type in part.hit_types

    def total_health_percent(self) -> int:
        """damage_model::get_total_health, including the retail truncation bug."""
        result = 100
        for p in self.parts.values():
            if any(AFFECT_DEATH in t[2] for t in p.thresholds):
                pct = 100 * int(p.health / p.max_health) if p.max_health else 0
                result = min(result, pct)
        return result

    # damage_model::hit_body_part (bullet == NULL on the network path)
    def hit(self, part_name: str, damage_type: str, amount: float, armor_piercing: float,
            now_ms: int) -> None:
        part = self.parts[part_name]
        self._hit_by_type(part, damage_type, now_ms, amount, armor_piercing,
                          self.type_protectors.get(damage_type))

    def _hit_by_type(self, part: _BodyPart, hit_type: str, now_ms: int, amount: float,
                     armor_piercing: float, prot: Optional[List[float]] = None) -> None:
        """body_part_parameters::hit_by_type: armour, then the part's protectors (each only
        while the amount is > 0), then the hit type's booster protector (direct hits only:
        the bdb hits of apply_damage pass NULL)."""
        params = part.hit_types[hit_type]
        if params.armor == 0.0:
            arp_arm_coeff = 1.0
        else:
            arp_arm_coeff = min(1.0, armor_piercing / params.armor - 1.0)
        e_wnd = max(0.0, arp_arm_coeff)
        delta = amount * e_wnd + max(0.0, (1.0 - params.reduce) * amount * (1.0 - e_wnd)
                                     - params.absorption)
        for protector in part.protectors:
            if delta > 0.0 and protector.reduce is not None:
                delta = protector.reduce(part.name, hit_type, delta, armor_piercing)
        delta = max(0.0, delta)
        if prot is not None:
            delta = max(0.0, delta * prot[0] - prot[1])      # booster_damage_protector
        part.health = min(max(part.health - delta, 0.0), part.max_health)
        part.last_hit_time = now_ms
        self._check_affects(part, now_ms)
        # hit_type_parameters::apply_damage: bdb coefficients, armor_piercing 0
        for bdb_name, coeff in params.bdb:
            target = self.parts.get(bdb_name)
            if coeff > 0.0 and target is not None and hit_type in target.hit_types:
                self._hit_by_type(target, hit_type, now_ms, coeff * delta, 0.0)

    def _check_affects(self, part: _BodyPart, now_ms: int) -> None:
        for value, target_name, affects in part.thresholds:
            if part.health <= part.max_health * value:
                target = self.parts.get(target_name)
                if target is None:
                    continue
                for affect in affects:
                    # body_part_parameters::apply_affects: not applied yet, no protector
                    if not target.has_affect(affect) and not any(
                            p.protect is not None and p.protect(target.name, affect)
                            for p in target.protectors):
                        self.events.append((target.name, affect, AFFECT_APPLYING))
                        target.affects.append((affect, now_ms + 1000 * AFFECT_DURATIONS_S[affect]))

    def tick(self, time_delta_ms: int, now_ms: int) -> None:
        """damage_model::tick -> body_part_parameters::regenerate (+ update_affects)."""
        for part in self.parts.values():
            delta = time_delta_ms
            if part.regeneration_timeout:
                next_regen = part.last_hit_time + part.regeneration_timeout
                if now_ms <= next_regen:
                    continue
                delta = min(now_ms - next_regen, time_delta_ms)
            amount = delta * part.regeneration_speed / 1000.0
            part.health = min(max(part.health + amount, 0.0), part.max_health)
            for i in range(len(part.affects) - 1, -1, -1):
                affect, expiry = part.affects[i]
                if expiry <= now_ms and affect != AFFECT_DEATH:
                    self.events.append((part.name, affect, AFFECT_RECALLING))
                    del part.affects[i]

    def heal(self, part_name: str, amount: float) -> None:
        """damage_model::apply_med_kit -> increase_health."""
        part = self.parts.get(part_name)
        if part is not None:
            part.health = min(max(part.health + amount, 0.0), part.max_health)

    def reset_part(self, part_name: str) -> None:
        """body_part_parameters::reset (artefact_lifebone_core::activate_impl): full
        health, no affects. The client drops the affects silently; the events here only
        tell the other clients' read-only copies (see Match._send_affects)."""
        part = self.parts.get(part_name)
        if part is None:
            return
        for affect, _ in part.affects:
            if affect != AFFECT_DEATH:
                self.events.append((part.name, affect, AFFECT_CANCELING))
        part.reset()

    def cancel_affect(self, part_name: str, affect: int) -> None:
        part = self.parts.get(part_name)
        if part is None:
            return
        for i in range(len(part.affects) - 1, -1, -1):
            if part.affects[i][0] == affect:
                self.events.append((part.name, affect, AFFECT_CANCELING))
                del part.affects[i]

    def drain_events(self) -> List[Tuple[str, int, int]]:
        out, self.events = self.events, []
        return out


# =============================================================================== weapons
SHOW_TIME_MS = 700            # weapon show after spawn / switch (fire is not ready)
CHAMBER_TIME_MS = 900         # bolt / pump cycle of chamber_a_round weapons
SPRINT_BIT, JUMP_BIT, CROUCH_BIT = 0x200, 0x10, 0x100


def is_sprinting(actions: int) -> bool:
    """player_input_inline.h:8-13."""
    return bool(actions & 0x200) and bool(actions & 0x1) and not (actions & 0x16E)


@dataclass
class WeaponSim:
    """The parts of weapon_core that decide when a round is fired (weapon_core.cpp:368-705):
    set_target(fire) needs ammo and readiness, reset_fire_queue on release, fire interval
    from rounds_per_minute, reload = load_magazine from the ammo slot."""
    slot: int
    dict_id: int
    info: WeaponInfo
    ammo_slot: int                      # 8/9/11/12, 19 = none
    magazine: int
    chambered: bool = False
    fire_queue_type: int = 0
    bullets_in_queue: int = 0
    next_fire_ms: Optional[int] = None  # client clock
    reload_end_ms: Optional[int] = None
    ready_ms: int = 0
    shots_fired: int = 0
    shot: Optional[object] = None       # ballistics.ShotModel: dispersion/recoil/PRNGs

    def queue_length(self) -> int:
        q = self.info.fire_queue_types[self.fire_queue_type % len(self.info.fire_queue_types)]
        return 0xFF if q < 0 or q >= 0xFF else q

    def rounds_available(self) -> int:
        return self.magazine + (1 if self.chambered else 0)

    def reset_fire_queue(self) -> None:
        if self.queue_length() == 0xFF:
            self.bullets_in_queue = self.rounds_available()
        else:
            self.bullets_in_queue = min(self.queue_length(), self.rounds_available())


# =============================================================================== geometry
STAND_HEIGHT, CROUCH_HEIGHT = 1.80, 1.20
EYE_STAND, EYE_CROUCH = 1.62, 1.05
CAPSULE_RADIUS = 0.35


def view_direction(yaw: float, pitch: float) -> Vec3:
    """create_rotation_y(yaw).k = (-sin, 0, cos) (math_float4x4_inline.h:298), pitch in
    radians. Used for facing (body sides); shots use ballistics.ShotModel.view, which maps
    look_pitch through the look animation (look_pitch is not an angle)."""
    cp = math.cos(pitch)
    return (-math.sin(yaw) * cp, math.sin(pitch), math.cos(yaw) * cp)


def right_vector(yaw: float) -> Vec3:
    return (math.cos(yaw), 0.0, math.sin(yaw))


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def ray_vs_capsule(origin: Vec3, direction: Vec3, feet: Vec3, height: float,
                   radius: float = CAPSULE_RADIUS, max_dist: float = 1000.0
                   ) -> Optional[Tuple[float, float]]:
    """Closest approach of a ray and a vertical capsule. Returns (distance along the ray,
    hit height above the feet) or None."""
    a = (feet[0], feet[1] + radius, feet[2])
    seg_len = max(0.0, height - 2 * radius)
    # ray P(t) = origin + t*D (|D| = 1, t >= 0); segment Q(s) = a + s*(0,1,0), s in [0, L]
    w = _sub(origin, a)
    b = direction[1]                           # D . U
    d = _dot(direction, w)                     # D . W
    e = w[1]                                   # U . W
    denom = 1.0 - b * b
    t = (b * e - d) / denom if denom > 1e-9 else 0.0
    for _ in range(2):                         # clamp, then re-project both ways
        t = max(0.0, t)
        s = min(max(e + b * t, 0.0), seg_len)
        t = max(0.0, _dot(direction, _sub((a[0], a[1] + s, a[2]), origin)))
    s = min(max(e + b * t, 0.0), seg_len)
    p_ray = (origin[0] + direction[0] * t, origin[1] + direction[1] * t, origin[2] + direction[2] * t)
    p_seg = (a[0], a[1] + s, a[2])
    diff = _sub(p_ray, p_seg)
    dist2 = _dot(diff, diff)
    if dist2 > radius * radius or t > max_dist:
        return None
    back = math.sqrt(max(0.0, radius * radius - dist2))
    t_hit = max(0.0, t - back)
    hit_y = origin[1] + direction[1] * t_hit - feet[1]
    return t_hit, min(max(hit_y, 0.0), height)


def body_part_for(hit_height: float, height: float, shot_dir: Vec3, hit_point: Vec3,
                  victim_feet: Vec3, victim_yaw: float) -> str:
    """Maps a capsule hit to one of the human_hit_params body part names (heights scale
    with crouching). front = the shot comes from the victim's front hemisphere."""
    h = hit_height * (STAND_HEIGHT / height)
    fwd = view_direction(victim_yaw, 0.0)
    front = _dot(fwd, shot_dir) < 0.0
    rel = _sub(hit_point, victim_feet)
    lateral = _dot(rel, right_vector(victim_yaw))
    side = "right" if lateral >= 0.0 else "left"
    if h >= 1.50:
        return "face" if front else "head"
    if h >= 0.95:
        if abs(lateral) > 0.24:
            return f"{side}_arm"
        return "body" if front else "back"
    if h >= 0.20:
        return f"{side}_leg"
    return f"{side}_foot"


def eye_position(feet: Vec3, actions: int) -> Vec3:
    return (feet[0], feet[1] + (EYE_CROUCH if actions & CROUCH_BIT else EYE_STAND), feet[2])


def capsule_height(actions: int) -> float:
    return CROUCH_HEIGHT if actions & CROUCH_BIT else STAND_HEIGHT
