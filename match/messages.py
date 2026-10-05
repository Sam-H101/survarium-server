"""Match message ids and payload codecs.

Payload = the bytes after ``[u8 type][u16 order_id]``.  Everything is little-endian,
``str`` is ``[u8 len][bytes]`` (cp1251, no NUL), ``bool`` is one byte.

Layout status (see docs/match_protocol.md; [V] = verified against the client source,
[A] = assumption pending the spec):
  0x40 connection_request     [V] u32 session_id                  match_client.cpp:41-50
  0x80 connection_successful  [V] empty (ASSERT_U(reader.eof()))  match_client_impl.cpp:56-66
  0x81 match_options          [V] game_net_defines.h:143-154
  0x92 player_profile         [V] game_net_defines.h:57-83, inventory_item_instance.h:28-38
  0x84 spawn_player           [V] header + stamina (player.cpp:1226-1256, player_stamina.cpp:54-60)
                              [A] per-item inventory state (inventory.cpp:268-280,
                                  weapon_core.cpp:1024-1080): see encode_item_states
  0x43 client_player_update   [V] client_player_update.cpp, player_input.cpp, player_state.cpp
  0x82 server_player_input    [V] network_client_handler.cpp:26-33, server_player_update.cpp
  0x45 / 0x8b / 0x46          [V] network_client_processing.cpp:445-466
  0x9a game_status_changed    [V] u32 status                       network_client_processing.cpp:339-366
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- ids
# network/message_types.h
C_CONNECTION_REQUEST = 0x40
C_GET_STARTUP_INFO = 0x41
C_JOIN_MATCH = 0x42
C_PLAYER_UPDATE = 0x43
C_COMMIT_SUICIDE = 0x44
C_TIME_SYNC_REQUEST = 0x45
C_TIME_SYNC_CONFIRMATION = 0x46
C_BULLETS_INFO_REQUEST = 0x47
C_TEAM_BASES_INITIALIZE_INFO = 0x48
C_FORCE_FINISH_MATCH = 0x49
C_WORLD_SYNC_CONFIRMATION = 0x4A

S_CONNECTION_SUCCESSFUL = 0x80
S_MATCH_OPTIONS = 0x81
S_SERVER_PLAYER_INPUT = 0x82
S_KILL_PLAYER = 0x83
S_SPAWN_PLAYER = 0x84
S_TEAM_BASE_CAPTURE_PROGRESS = 0x85
S_MATCH_TIME_CHANGED = 0x86
S_RESPAWN_TIME_CHANGED = 0x87
S_PLAYER_KD_STATS_CHANGED = 0x88
S_HIT_PLAYER = 0x89
S_AFFECT_DAMAGE_MODEL = 0x8A
S_SYNC_RESPONSE = 0x8B
S_MATCH_FINISHED = 0x8C
S_PLAYER_VISIBILITY_CHANGED = 0x91
S_PLAYER_PROFILE = 0x92
S_TEAM_BASES = 0x93
S_INITIALIZE_VICTORY_ITEMS = 0x94
S_VICTORY_ITEM_TAKE_OR_PUT = 0x95
S_TRAP_PLACED = 0x96
S_TRAP_REMOVED = 0x97
S_TRAP_FIRED = 0x98
S_TRAP_DISARMED = 0x99
S_GAME_STATUS_CHANGED = 0x9A
S_MATCH_WAIT_TIME_CHANGED = 0x9B
S_GAME_WORLD_OBJECT_STATE = 0x9C
S_WORLD_SYNC_REQUEST = 0x9D
S_DAMAGE_MODEL_STATE = 0x9E

# The client's dispatch (network_client_handler.cpp) is an unchecked jump table over
# 0x81..0x9e: 0x8d-0x90 (bullets) hit NODEFAULT, anything outside the range is
# undefined, and 0x80 after the handshake is likewise not handled.  The server may only
# send ids from this set (0x80 exactly once, as order id 0).
SENDABLE_AFTER_HANDSHAKE = frozenset(
    set(range(0x81, 0x8D)) | set(range(0x91, 0x9F)))

CLIENT_MESSAGE_NAMES = {
    0x40: "connection_request", 0x41: "get_startup_info", 0x42: "join_match",
    0x43: "client_player_update", 0x44: "commit_suicide", 0x45: "time_sync_request",
    0x46: "time_sync_confirmation", 0x47: "bullets_info_request",
    0x48: "team_bases_initialize_info", 0x49: "force_finish_match",
    0x4A: "world_sync_confirmation",
}

# game_status.h
GAME_STATUS_INACTIVE = 0
GAME_STATUS_WAITING_FOR_FIRST_PLAYER = 1
GAME_STATUS_WAITING_FOR_PLAYERS = 2
GAME_STATUS_FINAL_COUNTDOWN = 3
GAME_STATUS_INPROCESS = 4

# game_mode_type.h
MODE_CAPTURE_ENEMY_BASE = 0
MODE_CAPTURE_NEUTRAL_BASE = 1
MODE_GATHER_VICTORY_ITEMS = 2

# game_team_id.h
TEAM_1, TEAM_2, TEAM_NEUTRAL, TEAM_UNDEFINED = 0, 1, 2, 3

MAX_PLAYERS = 20

# profile_slot_enum.h
SLOT_NAMES = [
    "helmet_slot", "mask_slot", "torso_slot", "back_slot", "pants_slot", "gloves_slot",
    "boots_slot", "weapon1_slot", "ammo1_weapon1_slot", "ammo2_weapon1_slot",
    "weapon2_slot", "ammo1_weapon2_slot", "ammo2_weapon2_slot", "quick_slot1",
    "quick_slot2", "quick_slot3", "quick_slot4", "quick_slot5", "quick_slot6",
]
SLOT_IDS = {name: i for i, name in enumerate(SLOT_NAMES)}
MAX_SLOTS = 19
INVALID_SLOT = 19
WEAPON1_SLOT, WEAPON2_SLOT = 7, 10
# inventory.cpp:23-31 ignored_slots_for_serialization: armour carries no spawn state
ARMOUR_SLOTS = frozenset(range(0, 7))

# slot_serialize_mode_enum + game_net_defines.h:34-55
SERIALIZE_JUST_CONDITION_STACK = 0
SERIALIZE_JUST_AMOUNT = 1
SERIALIZE_BOTH = 2
SLOT_SERIALIZE_MODE = [0, 0, 0, 0, 0, 0, 0, 0, 2, 2, 0, 2, 2, 2, 2, 2, 2, 2, 2]

STR_ENCODING = "cp1251"


# ----------------------------------------------------------------- writer/reader
class Writer:
    def __init__(self) -> None:
        self.buf = bytearray()

    def u8(self, v: int) -> "Writer":
        self.buf += struct.pack("<B", v & 0xFF); return self

    def s8(self, v: int) -> "Writer":
        self.buf += struct.pack("<b", v); return self

    def boolean(self, v: bool) -> "Writer":
        return self.u8(1 if v else 0)

    def u16(self, v: int) -> "Writer":
        self.buf += struct.pack("<H", v & 0xFFFF); return self

    def u32(self, v: int) -> "Writer":
        self.buf += struct.pack("<I", v & 0xFFFFFFFF); return self

    def s32(self, v: int) -> "Writer":
        self.buf += struct.pack("<i", v); return self

    def f32(self, v: float) -> "Writer":
        self.buf += struct.pack("<f", v); return self

    def float2(self, v: Sequence[float]) -> "Writer":
        self.buf += struct.pack("<2f", *v); return self

    def float3(self, v: Sequence[float]) -> "Writer":
        self.buf += struct.pack("<3f", *v); return self

    def string(self, s: str, max_len: int = 31) -> "Writer":
        """packet_reader::r_string: u8 length + bytes; the reader writes a NUL at
        [length] into a fixed buffer, so length must be < buffer size."""
        raw = s.encode(STR_ENCODING, errors="replace")[:max_len]
        self.u8(len(raw))
        self.buf += raw
        return self

    def raw(self, b: bytes) -> "Writer":
        self.buf += b; return self

    def bytes(self) -> bytes:
        return bytes(self.buf)


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data = bytes(data)
        self.pos = 0

    def _take(self, fmt: str):
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise struct.error(f"read {size} at {self.pos} past end {len(self.data)}")
        vals = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return vals

    def u8(self) -> int: return self._take("<B")[0]
    def s8(self) -> int: return self._take("<b")[0]
    def boolean(self) -> bool: return self.u8() != 0
    def u16(self) -> int: return self._take("<H")[0]
    def u32(self) -> int: return self._take("<I")[0]
    def s32(self) -> int: return self._take("<i")[0]
    def f32(self) -> float: return self._take("<f")[0]
    def float2(self) -> Tuple[float, float]: return self._take("<2f")
    def float3(self) -> Tuple[float, float, float]: return self._take("<3f")

    def string(self) -> str:
        n = self.u8()
        if self.pos + n > len(self.data):
            raise struct.error("string past end")
        s = self.data[self.pos:self.pos + n].decode(STR_ENCODING, errors="replace")
        self.pos += n
        return s

    def eof(self) -> bool:
        return self.pos == len(self.data)

    def rest(self) -> bytes:
        r = self.data[self.pos:]
        self.pos = len(self.data)
        return r


# ------------------------------------------------------------------- structures
@dataclass
class ItemInstance:
    """inventory_item_instance (wire: u16 dict_id, u32 id, [u16 cond], [u32 amount])."""
    dict_id: int
    id: int
    condition_or_stack: int = 0
    amount_in_inventory: int = 0


@dataclass
class Booster:
    id: int
    value: float


@dataclass
class PlayerProfile:
    name: str
    team: int
    slots: Dict[int, ItemInstance] = field(default_factory=dict)
    boosters: Dict[int, Booster] = field(default_factory=dict)     # index 0..10 -> booster


@dataclass
class MatchOptions:
    map_id: int
    map_name: str
    mode: int
    players_count: int
    victory_items_count: int
    respawn_time: int
    match_time: int


@dataclass
class PlayerInput:
    """player_input (20 bytes): float2 angular_velocity, float2 angular_acceleration,
    u32 actions_mask."""
    angular_velocity: Tuple[float, float] = (0.0, 0.0)
    angular_acceleration: Tuple[float, float] = (0.0, 0.0)
    actions_mask: int = 0

    def write(self, w: Writer) -> None:
        w.float2(self.angular_velocity).float2(self.angular_acceleration).u32(self.actions_mask)

    @classmethod
    def read(cls, r: Reader) -> "PlayerInput":
        return cls(r.float2(), r.float2(), r.u32())


@dataclass
class PlayerState:
    """player_state wire form (20 bytes): float3 position, f32 yaw, f32 look_pitch."""
    position: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    yaw: float = 0.0
    pitch: float = 0.0

    def write(self, w: Writer) -> None:
        w.float3(self.position).f32(self.yaw).f32(self.pitch)

    @classmethod
    def read(cls, r: Reader) -> "PlayerState":
        return cls(r.float3(), r.f32(), r.f32())


@dataclass
class ClientPlayerUpdate:
    """0x43 payload (44 bytes): input(20) + state(20) + u32 time_in_ms."""
    input: PlayerInput
    state: PlayerState
    time_in_ms: int

    SIZE = 44

    def encode(self) -> bytes:
        w = Writer()
        self.input.write(w)
        self.state.write(w)
        w.u32(self.time_in_ms)
        return w.bytes()

    @classmethod
    def decode(cls, payload: bytes) -> "ClientPlayerUpdate":
        # one unpack instead of a Reader walk (the hot path: every client sends 30/s);
        # the same field order and types as PlayerInput.read / PlayerState.read
        if len(payload) < cls.SIZE:
            raise struct.error(f"0x43 needs {cls.SIZE} bytes, got {len(payload)}")
        (avx, avy, aax, aay, actions, px, py, pz, yaw, pitch, t) = _CLIENT_UPDATE.unpack_from(payload)
        return cls(PlayerInput((avx, avy), (aax, aay), actions),
                   PlayerState((px, py, pz), yaw, pitch), t)


_CLIENT_UPDATE = struct.Struct("<2f2fI3fffI")       # 0x43: input(20) + state(20) + u32 time


@dataclass
class WeaponStateSummary:
    """weapon_state (3 bytes, each read as r<bool>): slot_id, ammo_slot_id, state."""
    slot_id: int = WEAPON1_SLOT
    ammo_slot_id: int = WEAPON1_SLOT + 1
    state: int = 0

    def write(self, w: Writer) -> None:
        w.u8(self.slot_id).u8(self.ammo_slot_id).u8(self.state)


# --------------------------------------------------------------------- encoders
def encode_u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def encode_match_options(o: MatchOptions) -> bytes:
    """match_options::deserialize (game_net_defines.h:143-154)."""
    w = Writer()
    w.u8(o.map_id).string(o.map_name, 31).u8(o.mode).u8(o.players_count)
    w.u8(o.victory_items_count).u8(o.respawn_time).u16(o.match_time)
    return w.bytes()


def decode_match_options(payload: bytes) -> MatchOptions:
    r = Reader(payload)
    return MatchOptions(r.u8(), r.string(), r.u8(), r.u8(), r.u8(), r.u8(), r.u16())


def encode_player_profile(p: PlayerProfile, is_local: bool) -> bytes:
    """player_profile::deserialize (game_net_defines.h:57-83).

    u8 team, u8 is_local, str name (<32), u16 boosters_mask, per set bit i (0..10)
    {u8 id, f32 value}, then slot records until end of message:
    {u8 slot, u16 dict_id, u32 id, [u16 condition_or_stack if mode != 1],
    [u32 amount_in_inventory if mode != 0]} with mode = SLOT_SERIALIZE_MODE[slot]."""
    w = Writer()
    w.u8(p.team).u8(1 if is_local else 0).string(p.name, 31)
    mask = 0
    for i in p.boosters:
        if not 0 <= i < 11:
            raise ValueError("booster index out of range")
        mask |= 1 << i
    w.u16(mask)
    for i in range(11):
        if mask & (1 << i):
            w.u8(p.boosters[i].id).f32(p.boosters[i].value)
    for slot in sorted(p.slots):
        if not 0 <= slot < MAX_SLOTS:
            raise ValueError(f"slot {slot} out of range")
        item = p.slots[slot]
        mode = SLOT_SERIALIZE_MODE[slot]
        w.u8(slot).u16(item.dict_id).u32(item.id)
        if mode != SERIALIZE_JUST_AMOUNT:
            w.u16(item.condition_or_stack)
        if mode != SERIALIZE_JUST_CONDITION_STACK:
            w.u32(item.amount_in_inventory)
    return w.bytes()


def decode_player_profile(payload: bytes) -> Tuple[PlayerProfile, bool]:
    r = Reader(payload)
    team = r.u8()
    is_local = r.u8() != 0
    name = r.string()
    mask = r.u16()
    boosters = {}
    for i in range(11):
        if mask & (1 << i):
            boosters[i] = Booster(r.u8(), r.f32())
    slots = {}
    while not r.eof():
        slot = r.u8()
        mode = SLOT_SERIALIZE_MODE[slot]
        dict_id, iid = r.u16(), r.u32()
        cond = r.u16() if mode != SERIALIZE_JUST_AMOUNT else 0
        amount = r.u32() if mode != SERIALIZE_JUST_CONDITION_STACK else 0
        slots[slot] = ItemInstance(dict_id, iid, cond, amount)
    return PlayerProfile(name, team, slots, boosters), is_local


@dataclass
class Stamina:
    """player_stamina::deserialize (player_stamina.cpp:54-60)."""
    value: float = 1.0
    last_spending_time_in_ms: int = 0
    last_tick_time_in_ms: int = 0
    lower_threshold_was_reached: bool = False

    def write(self, w: Writer) -> None:
        w.f32(self.value).u32(self.last_spending_time_in_ms)
        w.u32(self.last_tick_time_in_ms).boolean(self.lower_threshold_was_reached)


@dataclass
class WeaponSpawnState:
    """Per-weapon spawn state, weapon_core::serialize (weapon_core.cpp:982-1022).

    [A] ASSUMPTION pending the spec: field order is taken from weapon_core::(de)serialize;
    which weapons have a chamber_a_round state, which FSM state is current after
    player::insert, and the bytes of the current weapon_core_base_state and of the
    weapon_user_animations_selector state are not verified yet.  They are fields here
    so the spec's answer can be plugged in without touching the encoder."""
    amount: int = 0                       # inventory_item::m_amount (u16)
    random_seed: int = 0                  # u32
    normal_random_seed: int = 0           # s32
    target: int = 0                       # u8 weapon_targets (0 = inactive)
    old_actions_mask: int = 0             # u32
    ammo_in_magazine: int = 0             # u16
    bullets_in_queue: int = 0             # u16
    fire_queue_type: int = 0              # u8
    ammo_slot: int = INVALID_SLOT         # u8 profile_slot_enum
    has_chamber_state: bool = False       # m_is_there_chamber_a_round_state (per weapon cfg)
    round_chambered: bool = False
    # present only when m_logic->current_state() != NULL on the client (active weapon)
    has_logic_state: bool = False
    is_shown: bool = False
    ik_active_hands: int = 0
    ik_left_start_time: int = 0
    ik_right_start_time: int = 0
    logic_state_id: int = 0
    logic_state_payload: bytes = b""      # weapon_core_base_state::serialize bytes
    user_anim_state_id: int = 0
    user_anim_state_payload: bytes = b""  # player_logic_base_state::serialize bytes

    def write(self, w: Writer) -> None:
        w.u16(self.amount).u32(self.random_seed).s32(self.normal_random_seed)
        w.u8(self.target).u32(self.old_actions_mask).u16(self.ammo_in_magazine)
        w.u16(self.bullets_in_queue).u8(self.fire_queue_type).u8(self.ammo_slot)
        if self.has_chamber_state:
            w.boolean(self.round_chambered)
        if self.has_logic_state:
            w.boolean(self.is_shown)
            w.u8(self.ik_active_hands).u32(self.ik_left_start_time).u32(self.ik_right_start_time)
            w.u8(self.logic_state_id).raw(self.logic_state_payload)
            w.u8(self.user_anim_state_id).raw(self.user_anim_state_payload)


@dataclass
class SimpleItemSpawnState:
    """inventory_item::deserialize: u16 m_amount (ammo, medkit, oxygen tank, ...)."""
    amount: int = 0

    def write(self, w: Writer) -> None:
        w.u16(self.amount)


@dataclass
class SpawnPlayer:
    """0x84 payload: u8 id then player::deserialize (player.cpp:1226-1256)."""
    player_id: int
    position: Tuple[float, float, float]
    yaw: float
    pitch: float
    alive: bool
    current_slot: int
    target_slot: int
    stamina: Stamina
    # inventory::deserialize walks slots 0..18 in order and calls item->deserialize for
    # every present non-armour item; this list must be in that slot order.
    item_states: List[Tuple[int, object]] = field(default_factory=list)

    def encode(self) -> bytes:
        w = Writer()
        w.u8(self.player_id).float3(self.position).f32(self.yaw).f32(self.pitch)
        w.boolean(self.alive).u8(self.current_slot).u8(self.target_slot)
        self.stamina.write(w)
        for slot, state in sorted(self.item_states, key=lambda t: t[0]):
            if slot in ARMOUR_SLOTS:
                continue
            state.write(w)
        return w.bytes()


def encode_sync_response(connected_mask: int) -> bytes:
    """0x8b: u32 connected-players bitmask (bit i = player id i)."""
    return encode_u32(connected_mask)


def encode_game_status(status: int) -> bytes:
    """0x9a: u32 game_status."""
    return encode_u32(status)


@dataclass
class CorrectionEntry:
    player_id: int
    input: PlayerInput
    state: PlayerState
    weapon: WeaponStateSummary


def encode_server_player_input(time_in_ms: int, entries: Sequence[CorrectionEntry]) -> bytes:
    """0x82: u32 time, then do { u8 id, input(20), state(20), weapon_state(3) } while
    !eof.  The client loop is do/while, so an EMPTY entry list would read past the end:
    never send it."""
    if not entries:
        raise ValueError("0x82 needs at least one entry")
    w = Writer()
    w.u32(time_in_ms)
    for e in entries:
        w.u8(e.player_id)
        e.input.write(w)
        e.state.write(w)
        e.weapon.write(w)
    return w.bytes()


CORRECTION_ENTRY_SIZE = 1 + 20 + 20 + 3
MAX_CORRECTIONS_PER_MESSAGE = (250 - 3 - 4) // CORRECTION_ENTRY_SIZE      # 5

_CORRECTION_ENTRY = struct.Struct("<B2f2fI3fffBBB")
_U32 = struct.Struct("<I")
assert _CORRECTION_ENTRY.size == CORRECTION_ENTRY_SIZE


def encode_correction_entry(player_id: int, inp: PlayerInput, state: PlayerState,
                            slot_id: int, ammo_slot_id: int, weapon_state: int = 0) -> bytes:
    """One 0x82 entry, byte-identical to the CorrectionEntry path of
    encode_server_player_input. An entry does not depend on the recipient, so the server
    encodes each player once per tick and concatenates per recipient."""
    av, aa, pos = inp.angular_velocity, inp.angular_acceleration, state.position
    return _CORRECTION_ENTRY.pack(player_id & 0xFF, av[0], av[1], aa[0], aa[1],
                                  inp.actions_mask & 0xFFFFFFFF, pos[0], pos[1], pos[2],
                                  state.yaw, state.pitch, slot_id & 0xFF, ammo_slot_id & 0xFF,
                                  weapon_state & 0xFF)


def encode_server_player_input_raw(time_in_ms: int, entries: Sequence[bytes]) -> bytes:
    """0x82 from entries already encoded with encode_correction_entry."""
    if not entries:
        raise ValueError("0x82 needs at least one entry")
    return _U32.pack(time_in_ms & 0xFFFFFFFF) + b"".join(entries)


# ------------------------------------------------------------------- M3 messages
# Layouts from game/sources/network_client_processing.cpp (handler line in brackets) and
# spec 4.2; [R] = the spec checked the layout against retail.
BODY_PART_NAME_MAX = 15        # char[16] buffers (hit_info, process_affect_damage_model)
DAMAGE_TYPE_MAX = 15


def encode_kill_player(victim: int, killer: int, headshot: bool, item_dict_id: int) -> bytes:
    """0x83 (process_player_kill :182): u8 victim, u8 killer, bool headshot, u32 item dict id.
    game_world_ui::on_player_killed dereferences get_player(killer) and
    item_by_id(item_dict_id) when non-zero: killer must be a roster id, dict id valid or 0."""
    return Writer().u8(victim).u8(killer).boolean(headshot).u32(item_dict_id).bytes()


def encode_kd_stats(player_id: int, kills: int, deaths: int) -> bytes:
    """0x88 (process_player_kd_stats :368): u8 id, u32 kills, u32 deaths."""
    return Writer().u8(player_id).u32(kills).u32(deaths).bytes()


def encode_hit_player(initiator: int, victim: int, body_part: str, damage_type: str,
                      amount: float, armor_piercing: float) -> bytes:
    """0x89 (hit_info::deserialize, hit_initiator.cpp:41) [R]: u8 initiator (0xFF none),
    u8 victim, str body_part (<16), str damage_type (<16), f32 amount, f32 armor_piercing.
    The client looks both strings up unchecked (damage_model.cpp:130,
    body_part_parameters.cpp:137): they must name a body part / hit type of
    human_hit_params."""
    w = Writer().u8(initiator).u8(victim)
    w.string(body_part, BODY_PART_NAME_MAX).string(damage_type, DAMAGE_TYPE_MAX)
    return w.f32(amount).f32(armor_piercing).bytes()


def encode_affect_damage_model(player_id: int, body_part: str, affect: int, event: int) -> bytes:
    """0x8a (process_affect_damage_model :205) [R]: u8 id, str body_part, u32 affect,
    u32 event (both enums are 4 bytes)."""
    return Writer().u8(player_id).string(body_part, BODY_PART_NAME_MAX).u32(affect).u32(event).bytes()


def encode_visibility(player_id: int, visible: bool) -> bytes:
    """0x91 (player_visibility_change :668): u8 id, bool visible."""
    return Writer().u8(player_id).boolean(visible).bytes()


VICTORY_ITEM_IN_WORLD = 0xFF
NO_CONTAINER = 0xFF


def encode_initialize_victory_items(team1_points: int, team2_points: int,
                                    items: Sequence[Tuple[int, int, Tuple[float, float, float]]],
                                    containers: Sequence[Tuple[int, Sequence[int]]]) -> bytes:
    """0x94 (process_initialize_victory_items :236) [R]: s8 team_1 points, s8 team_2
    points, u8 n, n x {u8 holder (0xFF = lying in the world), u8 item, float3 pos}, u8 m,
    m x {u8 container_id, u8 k, k x u8 item}. With n = m = 0 it only sets the score."""
    w = Writer().s8(team1_points).s8(team2_points).u8(len(items))
    for holder, item, pos in items:
        w.u8(holder).u8(item).float3(pos)
    w.u8(len(containers))
    for cid, held in containers:
        w.u8(cid).u8(len(held))
        for item in held:
            w.u8(item)
    return w.bytes()


def encode_victory_item_take_or_put(player_id: int, item: int, is_take: bool,
                                    container_id: int = NO_CONTAINER,
                                    position: Tuple[float, float, float] = (0.0, 0.0, 0.0)) -> bytes:
    """0x95, RETAIL layout (spec 4.2; the decompiled :384 is wrong): u8 player, u8 item,
    bool is_take, u8 container (0xFF none), float3 only for a put into the world."""
    w = Writer().u8(player_id).u8(item).boolean(is_take).u8(container_id)
    if not is_take and container_id == NO_CONTAINER:
        w.float3(position)
    return w.bytes()


def encode_damage_model_state(player_id: int,
                              parts: Sequence[Tuple[float, int, Sequence[Tuple[int, int]]]]) -> bytes:
    """0x9e (damage_model::deserialize -> body_part_parameters::deserialize per part in
    config order): f32 health, u32 last_hit_time, u8 affects_count, n x {u8, u32}.
    body_part_parameters::serialize writes the count as u32 while deserialize reads u8;
    this follows the reader. NOT sent by default (see docs, 9.x)."""
    w = Writer().u8(player_id)
    for health, last_hit, affects in parts:
        w.f32(health).u32(last_hit).u8(len(affects))
        for affect, t in affects:
            w.u8(affect).u32(t)
    return w.bytes()
