"""Quick-slot and equipment items the server simulates: booby traps, medkit-class drugs,
the lifebone artefact and the oxygen tank (game_core/sources/booby_trap_*.cpp, medkit.cpp,
artefact_lifebone_core.cpp, oxygen_tank.cpp). Match (game.py) owns the state; this module
holds the geometry and the per-item records.

In a networked match the client never places, fires or defuses a trap itself:
booby_trap_set::action calls try_place_trap only when the network client has no
bandwidth (offline), booby_trap::register_tick (the collision sensor that fires the trap)
and booby_trap::defuse_completed return early when it has, and the trap's state changes
only through the server's 0x96-0x99 / 0x9c (game/sources/booby_trap.cpp,
booby_trap_set.cpp). Placement, triggering, damage and disarming are therefore decided
here. [V]
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from . import messages as M
from .combat import DamageProtector
from .game_data import TrapInfo

Vec3 = Tuple[float, float, float]

# booby_trap_state (booby_trap_core.h)
TRAP_REMOVED, TRAP_ARMED, TRAP_FIRED, TRAP_DISARMED = 0, 1, 2, 3

USE_DETECTION_M = 1.0          # player.cpp:44 s_usable_objects_detection_distance
BACK_SLOT = 3
BACK_SLOT_USE_BIT = 0x4000000  # player_input_handler.cpp:315 (kBACK_SLOT_USE)
# [A] the sensor reacts to the victim's physics bodies (filter mask 0x420); the server has
# only the feet position, so a foot is modelled as a disc of this radius at the feet.
FOOT_RADIUS_M = 0.15
FOOT_REACH_BELOW_M = 0.3       # [A] feet up to this far below the sensor still count

# game_materials/game.materials "mine": can_place false. The level cache keeps only
# the material names, so the flag is listed here (read from the game data). [V]
NON_PLACEABLE_MATERIALS = frozenset({
    "bullet", "actor", "artefact", "body", "mud", "foliage", "water", "glass", "paper",
    "deep_water", "cloth", "rabitz", "glass_hard", "actor_local"})
NON_STICKABLE_MATERIALS = frozenset({
    "bullet", "artefact", "mud", "foliage", "water", "glass", "paper", "deep_water", "cloth",
    "rabitz", "glass_hard"})

# artefact_lifebone_core.cpp: protected_body_patrs / protected_affects
LIFEBONE_PARTS = ("left_hand", "right_hand", "left_leg", "right_leg")
AFFECT_HAND_DAMAGE, AFFECT_LEG_DAMAGE = 3, 4


@dataclass
class Trap:
    owner: int                      # player id
    slot: int                       # quick slot of the booby_trap_set
    index: int                      # booby_trap_set_core::trap_index
    dict_id: int
    info: TrapInfo
    position: Vec3
    angles: Vec3                    # float4x4::get_angles(rotation_zxy) of the transform
    state: int = TRAP_ARMED
    timer_end_ms: Optional[int] = None    # server ms; None = no state timer

    @property
    def key(self) -> Tuple[int, int, int]:
        return self.owner, self.slot, self.index

    def box(self, which) -> Tuple[Vec3, Vec3]:
        """World-space (min, max) of one of the trap's boxes. [A] axis aligned around the
        trap position: the offsets are along the trap's up axis, which is the world up
        on the gentle slopes a trap may be placed on."""
        (ox, oy, oz), (hx, hy, hz) = which
        x, y, z = self.position
        cx, cy, cz = x + ox, y + oy, z + oz
        return (cx - hx, cy - hy, cz - hz), (cx + hx, cy + hy, cz + hz)

    def encode_header(self) -> bytes:
        return bytes((self.owner, self.slot, self.index))

    def encode_placed(self) -> bytes:
        """0x96 (network_client::on_trap_placed): u8 player, u8 slot, u8 trap index,
        float3 position, float3 angles."""
        return M.Writer().raw(self.encode_header()).float3(self.position).float3(self.angles).bytes()

    def encode_state(self) -> bytes:
        """0x9c (base_player::send_game_world_object + booby_trap_core::serialize): u8
        player, u8 slot, u8 trap index (serialize_game_world_object_header), u8 state,
        float3 position, float3 angles."""
        return M.Writer().raw(self.encode_header()).u8(self.state).float3(self.position) \
            .float3(self.angles).bytes()


# ------------------------------------------------------------------------ geometry
def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: Vec3) -> Vec3:
    n = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    return (a[0] / n, a[1] / n, a[2] / n)


def place_matrix(normal: Vec3, head_forward: Vec3, head_right: Vec3) -> Tuple[Vec3, Vec3, Vec3]:
    """Rows (i right, j up, k forward) of create_place_matrix_for_looking_point
    (booby_trap_set_core.cpp): up = surface normal, forward follows the view."""
    rc = _cross(normal, head_forward)
    if math.sqrt(rc[0] ** 2 + rc[1] ** 2 + rc[2] ** 2) > 1e-3:
        right = _norm(rc)
        forward = _norm(_cross(right, normal))
    else:
        forward = _norm(_cross(head_right, normal))
        right = _norm(_cross(normal, forward))
    return right, normal, forward


def angles_zxy(i: Vec3, j: Vec3, k: Vec3) -> Vec3:
    """float4x4::get_angles(rotation_zxy) (math_float4x4.cpp:143), as booby_trap_core::
    serialize sends it. The client rebuilds the transform with create_rotation(angles)
    (xyz order); both agree for a trap on level ground (only y is non-zero)."""
    ky = max(-1.0, min(1.0, k[1]))
    if ky < 1.0:
        if ky > -1.0:
            return (math.asin(ky), math.atan2(-k[0], k[2]), math.atan2(-i[1], j[1]))
        return (-math.pi / 2, 0.0, -math.atan2(i[2], i[0]))
    return (math.pi / 2, 0.0, math.atan2(i[2], i[0]))


def triangle_normal(collision, tri: int) -> Vec3:
    t, o = collision.tris, 9 * tri
    n = _cross((t[o + 3], t[o + 4], t[o + 5]), (t[o + 6], t[o + 7], t[o + 8]))
    return _norm(n)


def ray_box(origin: Vec3, direction: Vec3, lo: Vec3, hi: Vec3, max_dist: float) -> Optional[float]:
    """Distance along the ray to an axis-aligned box (slab test), or None."""
    t0, t1 = 0.0, max_dist
    for a in range(3):
        d = direction[a]
        if abs(d) < 1e-12:
            if origin[a] < lo[a] or origin[a] > hi[a]:
                return None
            continue
        ta, tb = (lo[a] - origin[a]) / d, (hi[a] - origin[a]) / d
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1:
            return None
    return t0


def feet_in_sensor(trap: Trap, feet: Vec3) -> bool:
    """[A] collision_sensor overlap of the trap's sensor box with a foot (see
    FOOT_RADIUS_M): the feet within the box grown by the foot radius horizontally, at
    most FOOT_REACH_BELOW_M below its bottom and not above its top."""
    lo, hi = trap.box(trap.info.sensor)
    x, y, z = feet
    if y < lo[1] - FOOT_REACH_BELOW_M or y > hi[1]:
        return False
    dx = max(lo[0] - x, 0.0, x - hi[0])
    dz = max(lo[2] - z, 0.0, z - hi[2])
    return dx * dx + dz * dz <= FOOT_RADIUS_M * FOOT_RADIUS_M


def lifebone_protector() -> DamageProtector:
    """artefact_lifebone_core::protect_affect / reduce_damage: hand and leg damage are
    never applied; damage passes unchanged."""
    return DamageProtector(lambda part, htype, amount, ap: amount,
                           lambda part, affect: affect in (AFFECT_HAND_DAMAGE, AFFECT_LEG_DAMAGE))


@dataclass
class ActiveMedkit:
    """medkit::set_active(true) .. set_active(false): the protectors are registered for
    the whole time, the influences heal after activation_delay over activity_time."""
    slot: int
    start_ms: int                   # server ms the delay ends
    end_ms: int
    info: object                    # game_data.MedkitInfo
    protectors: List[Tuple[str, DamageProtector]]


@dataclass
class OxygenTank:
    """oxygen_tank: action(true) toggles while time is left; active_tick spends it."""
    amount_ms: int
    protectors: Sequence[Tuple[str, DamageProtector]]
    active: bool = False
