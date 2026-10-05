"""Shared match-server types: configuration, roster players, client sessions."""

from __future__ import annotations

import logging
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Deque, Dict, List, Optional, Tuple

from . import messages as M
from .connection import UdpMatchConnection

if TYPE_CHECKING:
    from .combat import DamageModel, WeaponSim
    from .game import Match
    from .game_data import Ticket

log = logging.getLogger("match")

Addr = Tuple[str, int]


class ProtocolGuardError(RuntimeError):
    """Raised instead of sending something that would crash the client (spec 4.3)."""


@dataclass
class MatchConfig:
    map_name: str = "level_03"
    map_id: int = 0                       # ignored by the client
    mode: int = M.MODE_GATHER_VICTORY_ITEMS
    victory_items_count: int = 3          # lobby matches; also the score needed to win
    respawn_time: int = 10                # seconds (0x81 u8)
    match_time: int = 600                 # seconds (0x81 u16)
    max_players: int = M.MAX_PLAYERS
    send_corrections: bool = True         # 0x82 for the other players (never empty)
    deterministic_spawns: bool = False    # respawn points in id order, fixed seeds (tests)
    accept_unknown_sessions: bool = True  # no ticket -> default player in the open match
    # rounds (lobby matches with a fixed roster)
    join_timeout_s: float = 60.0          # waiting_for_players before starting anyway
    countdown_s: int = 5                  # final_countdown once everyone has joined
    end_delay_s: float = 5.0              # result shown in the HUD, then 0x8c
    finish_grace_s: float = 10.0          # then sessions that did not leave are kicked
    empty_match_timeout_s: float = 60.0   # a started match nobody is connected to ends
    # robustness: an endpoint that never completes the 0x40 handshake is dropped after
    # handshake_timeout_s (it would otherwise get 30 keep-alives a second for the 120 s
    # transport timeout); at most max_pending_sessions such endpoints exist at once
    handshake_timeout_s: float = 15.0
    max_pending_sessions: int = 256
    debug_tick_stall_ms: float = 0.0      # test hook: every match tick busy-waits this long
    open_match_rules: bool = False        # ticketless "open" match: items/timer/end too
    # combat
    friendly_fire: bool = False
    lag_compensation: bool = True
    max_rewind_ms: int = 350
    # shot direction and world (match/ballistics.py, match/level_collision.py)
    world_collision: bool = True          # walls, terrain and props stop hitscan
    collision_path: Optional[str] = None  # default match/data/<map_name>.collision
    solid_terrain: bool = True            # terrain_p_* stops bullets whatever its material
    dispersion: bool = True               # per-weapon spread with the 0x84 PRNG seeds
    recoil: bool = True                   # replayed weapon_recoil_calculator turns the view
    spread_growth_from_config: bool = False   # use one_shoot_dispersion_amount (retail zeroes it)
    los_respawn: bool = True              # prefer respawn points no living enemy can see
    send_affects: bool = True             # 0x8a to the other clients
    # victory items
    use_distance_m: float = 2.0           # pick up an item lying within this radius
    container_use_distance_m: float = 3.0


@dataclass
class Player:
    id: int
    ticket: "Ticket"
    session: Optional["ClientSession"] = None
    position: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    yaw: float = 0.0
    pitch: float = 0.0
    alive: bool = False
    spawned: bool = False                 # inserted on the clients (alive or as a body)
    last_input: M.PlayerInput = field(default_factory=M.PlayerInput)
    last_input_time: int = 0              # newest 0x43 time, in this player's own clock
    last_input_server_ms: int = 0         # server time that 0x43 was processed
    has_input: bool = False
    inputs_received: int = 0
    weapon_seeds: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    # M3
    damage: Optional["DamageModel"] = None
    weapons: Dict[int, "WeaponSim"] = field(default_factory=dict)
    active_slot: int = M.WEAPON1_SLOT
    switched: bool = False                # picked a weapon since the last spawn (see send_corrections)
    reserve: Dict[int, int] = field(default_factory=dict)     # ammo slot -> rounds
    quick: Dict[int, int] = field(default_factory=dict)       # quick slot -> amount
    kills: int = 0
    deaths: int = 0
    items_stored: int = 0                 # victory items this player put into its container
    play_ms: int = 0                      # connected time while the round was running
    present_at_end: bool = False          # still connected when the match finished
    respawn_at: Optional[int] = None      # server ms
    respawn_shown: int = -1               # last 0x87 value sent
    carrying: Optional[int] = None        # victory item index
    history: Deque[Tuple[int, Tuple[float, float, float], float, int]] = field(
        default_factory=lambda: deque(maxlen=64))       # (server ms, pos, yaw, actions)
    medkits: List[Tuple[int, int, object]] = field(default_factory=list)  # (start, end, info)
    shots_fired: int = 0
    hits_dealt: int = 0
    shots_blocked: int = 0                # capsule hits a wall stopped first
    recoil_rngs: Dict[int, object] = field(default_factory=dict)   # slot -> ballistics.Random32

    @property
    def connected(self) -> bool:
        return self.session is not None and self.session.conn.is_connected()

    @property
    def team(self) -> int:
        return self.ticket.team


class ClientSession:
    """One remote endpoint: transport + the guarded application send path."""

    def __init__(self, core, addr: Addr) -> None:
        self.core = core
        self.addr = addr
        self.conn = UdpMatchConnection(lambda d: core.sendto(d, addr),
                                       logging_id=f"server {addr[0]}:{addr[1]}")
        self.conn.connect(None)           # udp_match_client_session ctor: connect(NULL)
        self.conn.on_disconnect = self._on_disconnect
        self.handshaked = False
        self.recovery_checked = False     # MatchCore._recover_continued_connection ran
        self.continued = False            # peer kept its old connection numbering
        self.startup_sent = False
        self.joined = False               # 0x42 received: players exist on the client
        self.synced = False               # 0x8b sent and 0x46 received
        self.status_sent = False
        self.status_value: Optional[int] = None   # last 0x9a value sent
        self.local_spawn_sent = False     # its own player was sent in 0x84
        self.attached = False             # client has m_current_player (see Match.maybe_attach)
        self.items_sent = False           # 0x94 snapshot delivered
        self.finished_sent = False        # 0x8c sent
        self.last_corr_time = 0           # newest 0x82 time sent (recipient clock)
        self.sync_sent_ms: Optional[int] = None
        self.rtt_ms = 0
        self.player: Optional[Player] = None
        self.match: Optional["Match"] = None
        self.created_ms = core.now_ms
        self.roster_size = 0              # players_count this client was told in 0x81
        self.sent_types: List[int] = []   # for tests/diagnostics
        self.received_types: List[int] = []

    # -- guarded send ------------------------------------------------------
    def send(self, message_type: int, payload: bytes = b"") -> None:
        if message_type == M.S_CONNECTION_SUCCESSFUL:
            if self.handshaked or self.sent_types:
                raise ProtocolGuardError("0x80 is handshake-only and must be order 0")
        else:
            if not self.handshaked:
                raise ProtocolGuardError(f"0x{message_type:02x} before 0x80")
            if message_type not in M.SENDABLE_AFTER_HANDSHAKE:
                raise ProtocolGuardError(f"0x{message_type:02x} is not dispatchable by the client")
            if message_type in (M.S_SPAWN_PLAYER, M.S_GAME_WORLD_OBJECT_STATE,
                                M.S_DAMAGE_MODEL_STATE, M.S_HIT_PLAYER, M.S_KILL_PLAYER,
                                M.S_AFFECT_DAMAGE_MODEL, M.S_PLAYER_VISIBILITY_CHANGED) \
                    and not self.joined:
                raise ProtocolGuardError(f"0x{message_type:02x} before the client's 0x42")
            if message_type in (M.S_INITIALIZE_VICTORY_ITEMS, M.S_VICTORY_ITEM_TAKE_OR_PUT) \
                    and not self.attached:
                # game_world_ui::set/add_victory_points dereference get_current_player()
                raise ProtocolGuardError(f"0x{message_type:02x} before the client attached "
                                         "its local player")
            if message_type == M.S_SERVER_PLAYER_INPUT and len(payload) < 4 + M.CORRECTION_ENTRY_SIZE:
                raise ProtocolGuardError("empty 0x82")
        packet = self.conn.new_packet(message_type)
        packet.append(payload)            # raises MessageTooLarge past 250 bytes
        if not self.conn.enqueue(packet):
            return
        if message_type == M.S_CONNECTION_SUCCESSFUL:
            self.handshaked = True
        _append_capped(self.sent_types, message_type)

    def _on_disconnect(self, reason: str) -> None:
        log.info("%s:%d: transport disconnected (%s)", self.addr[0], self.addr[1], reason)
        self.core.on_session_disconnected(self, reason)


TYPES_HISTORY_HEAD = 64           # sent_types / received_types keep the first ids ...
TYPES_HISTORY_MAX = 4096          # ... and the newest ones (a match lasts 30 msgs/s)


def _append_capped(lst: List[int], v: int) -> None:
    lst.append(v)
    if len(lst) > TYPES_HISTORY_MAX:
        del lst[TYPES_HISTORY_HEAD:TYPES_HISTORY_HEAD + TYPES_HISTORY_MAX // 2]


def u32_lt(a: int, b: int) -> bool:
    return ((a - b) & 0xFFFFFFFF) >= 0x80000000


def ammo_slot_for(weapon_slot: int, slots: Dict[int, M.ItemInstance]) -> int:
    first = 8 if weapon_slot == M.WEAPON1_SLOT else 11
    for s in (first, first + 1):
        if s in slots:
            return s
    return M.INVALID_SLOT


def find_connection_request(data: bytes) -> Optional[Tuple[int, int]]:
    """(order_id, session_id) of a 0x40 record in a raw datagram (single or bundle)."""
    if len(data) < 6:
        return None
    bits = data[4] | (data[5] << 8)
    body = data[6:]
    records = []
    if not bits & 1:
        records.append(body)
    else:
        pos = 0
        while pos < len(body):
            n = body[pos]
            records.append(body[pos + 1:pos + 1 + n])
            pos += 1 + n
        if len(records) == 1:
            return None                       # low-level control record
    for r in records:
        if len(r) >= 7 and r[0] == M.C_CONNECTION_REQUEST:
            return r[1] | (r[2] << 8), struct.unpack_from("<I", r, 3)[0]
    return None
