"""Mock of the v0.100b match client for tests.

Transport: the C++ client and the original server shared network_core::udp_match_connection,
so the mock drives the same port (match/connection.py) exactly the way
udp_match_client/match_client do: connect(0x40 packet) then send_queued_packets every 33 ms
or whenever something is queued.

Game layer: an emulation of network_client's handlers (game/sources/
network_client_processing.cpp, game_world_ui.cpp), with every crash condition turned into a
recorded fault instead of a crash:
  * first message must be 0x80 with an empty payload (match_client_impl.cpp:55-78)
  * afterwards only ids the client's jump table handles (0x81..0x9e minus 0x8d..0x90)
  * 0x84 / 0x9c / 0x9e before the client has sent 0x42 dereference a NULL player
  * 0x92 slot ids >= 19, names >= 32 bytes, unknown dict_ids, weaponless profiles
  * 0x84 active-slot bytes naming an empty / non-weapon slot
  * 0x82 with no entries, or ids beyond players_count
  * 0x83 with a killer/victim outside the roster or an unknown item dict id
    (game_world_ui::on_player_killed dereferences both players and item_by_id)
  * 0x89 / 0x8a body part or damage type unknown to human_hit_params (unchecked lookups)
  * 0x94 / 0x95 while the client has no current player (add/set_victory_points call
    get_current_player()->team()), items/containers out of range, putting an item that is
    already in the world, taking one that is not, or a container take that would pop a
    different item than the one named (victory_items_container_core::take_item pops LAST)
The 0x84 inventory tail is parsed independently from the spec tables (2.5) using the
item classes from items.json and must consume the message exactly.

When the client is attached and alive, ``bot(client, now)`` may return
(position, yaw, pitch, actions_mask) to steer it; otherwise it walks along +x.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from match import messages as M  # noqa: E402
from match.connection import UdpMatchConnection  # noqa: E402
from match.game_data import GameData, ITEM_ARTEFACT, ITEM_WEAPON  # noqa: E402

DISPATCHABLE = set(range(0x81, 0x8D)) | set(range(0x91, 0x9F))

# spec 2.5: weapon state payload sizes by fsm state id
WEAPON_STATE_PAYLOAD = {0: 0, 1: 8, 2: 8, 3: 0, 4: 1, 5: 8, 6: 0, 7: 0, 8: 1, 9: 1}


class ClientFault(AssertionError):
    pass


class MockMatchClient:
    def __init__(self, send_datagram: Callable[[bytes], None], session_id: int,
                 game_data: GameData, load_delay_ms: int = 300) -> None:
        self.conn = UdpMatchConnection(send_datagram, logging_id="mock client")
        self.session_id = session_id
        self.data = game_data
        self.hit_parts = {p.name: set(p.hit_types) for p in game_data.body_parts}
        self.containers_on_map = {c.container_id for c in game_data.victory_containers("level_03")}
        self.load_delay_ms = load_delay_ms
        self.faults: List[str] = []
        self.log: List[Tuple[int, int, bytes]] = []       # (time, type, payload) delivered
        self.handshaked = False
        self.forbidden = False
        self.options: Optional[M.MatchOptions] = None
        self.profiles: List[Tuple[M.PlayerProfile, bool]] = []
        self.local_id: Optional[int] = None
        self.load_done_at: Optional[int] = None
        self.sent_join = False
        self.inserted: Dict[int, dict] = {}
        self.player_ticked = False
        self.time_synced = False
        self.game_status = 0
        self.current: Optional[int] = None                 # m_current_player (attached)
        self.sync_responses = 0
        self.last_sync_request = 0
        self.corrections: List[Tuple[int, List[dict]]] = []
        self.own_corrections = 0                           # 0x82 entries about ourselves
        self.remote: Dict[int, dict] = {}                  # latest 0x82 state per player
        self.inputs_sent = 0
        self.now = 0
        self.last_flush = 0
        self.packets_pending = False
        self.position = None
        self.on_tick_hook: Optional[Callable[["MockMatchClient"], None]] = None
        self.bot: Optional[Callable[["MockMatchClient", int], Optional[tuple]]] = None
        # M3 state
        self.kills: List[Tuple[int, int, bool, int]] = []  # (victim, killer, headshot, item)
        self.kd: Dict[int, Tuple[int, int]] = {}
        self.hits: List[Tuple[int, int, str, str, float, float]] = []
        self.affects: List[Tuple[int, str, int, int]] = []
        self.match_time_ms: Optional[int] = None
        self.respawn_time: Optional[int] = None
        self.wait_time: Optional[int] = None
        self.visible: Dict[int, bool] = {}
        self.score: Tuple[int, int] = (0, 0)
        self.items_world: Dict[int, Tuple[float, float, float]] = {}
        self.items_held: Dict[int, int] = {}               # item -> player
        self.containers: Dict[int, List[int]] = {}
        self.items_initialized = 0
        self.finished = False
        self.connected_mask = 0
        self.statuses: List[int] = []

    # ------------------------------------------------------------ transport
    def connect(self, now: int) -> None:
        """match_client::connect: 0x40 + u32 session_id, enqueued by connect()."""
        self.now = now
        packet = self.conn.new_packet(M.C_CONNECTION_REQUEST)
        packet.append(struct.pack("<I", self.session_id))
        self.conn.m_last_send_attempt_time_in_ms = now
        self.conn.connect(packet)
        self.conn.send_queued_packets(now)
        self.last_flush = now

    def datagram_received(self, data: bytes) -> None:
        if len(data) > 256:
            self.fault(f"datagram of {len(data)} bytes > 256-byte receive buffer")
            data = data[:256]
        self.conn.process_incoming_packet(data, self._on_message)

    def enqueue(self, mtype: int, payload: bytes = b"") -> None:
        p = self.conn.new_packet(mtype)
        p.append(payload)
        self.conn.enqueue(p)
        self.packets_pending = True

    @property
    def local(self) -> Optional[dict]:
        return self.inserted.get(self.local_id) if self.local_id is not None else None

    def tick(self, now: int) -> None:
        """network_client::tick, reduced to the match-client parts."""
        self.now = now
        if self.conn.is_disconnected():
            return
        if self.load_done_at is not None and not self.sent_join and now >= self.load_done_at:
            # on_players_ready: 0x48 then 0x42, flushed immediately
            self.enqueue(M.C_TEAM_BASES_INITIALIZE_INFO)
            self.enqueue(M.C_JOIN_MATCH)
            self.sent_join = True
        local = self.local
        if local is not None and not self.player_ticked:
            self.player_ticked = True
            self.send_sync_request()
        if self.time_synced and now - self.last_sync_request > 4000:
            self.send_sync_request()
        # player::tick runs for the local player once time-synced; it sends input while
        # alive (zero input until a controller is attached)
        if self.time_synced and local is not None and local["alive"] and not self.finished:
            pos, yaw, pitch, actions = local["position"], local["yaw"], local.get("pitch", 0.0), 0
            if self.controllable:
                steer = self.bot(self, now) if self.bot else None
                if steer is None:
                    x, y, z = pos
                    pos, actions = (x + 0.05, y, z), 0x1
                else:
                    pos, yaw, pitch, actions = steer
            local["position"], local["yaw"], local["pitch"] = pos, yaw, pitch
            self.position = pos
            upd = M.ClientPlayerUpdate(M.PlayerInput((0.0, 0.0), (0.0, 0.0), actions),
                                       M.PlayerState(pos, yaw, pitch), now)
            self.enqueue(M.C_PLAYER_UPDATE, upd.encode())
            self.inputs_sent += 1
        if self.on_tick_hook:
            self.on_tick_hook(self)
        if self.packets_pending or self.last_flush + 33 <= now:
            self.conn.send_queued_packets(now)
            self.last_flush = now
            self.packets_pending = False

    def send_sync_request(self) -> None:
        self.last_sync_request = self.now
        self.enqueue(M.C_TIME_SYNC_REQUEST, struct.pack("<I", self.now))

    def commit_suicide(self) -> None:
        """initiate_kill_current_player: only with a local and a current player."""
        if self.local_id is not None and self.current is not None:
            self.enqueue(M.C_COMMIT_SUICIDE)

    @property
    def controllable(self) -> bool:
        local = self.local
        return bool(self.time_synced and self.game_status == 4 and local and local["alive"]
                    and self.current == self.local_id)

    def fault(self, msg: str) -> None:
        self.faults.append(msg)

    def _attach(self) -> None:
        if self.local_id is not None and self.local_id in self.inserted:
            self.current = self.local_id

    def _valid_player(self, pid: int, what: str) -> bool:
        if self.options is None or not self.sent_join or pid >= len(self.profiles):
            self.fault(f"{what}: player {pid} does not exist on the client (NULL player)")
            return False
        return True

    # ------------------------------------------------------------- dispatch
    def _on_message(self, mtype: int, payload: bytes) -> None:
        self.log.append((self.now, mtype, payload))
        if not self.handshaked:
            # match_client_impl::on_packet_received
            if mtype == M.S_CONNECTION_SUCCESSFUL:
                if payload:
                    self.fault("0x80 with a payload (ASSERT_U(reader.eof()))")
                self.handshaked = True
                self.enqueue(M.C_GET_STARTUP_INFO)        # on_connected_to_match
            else:
                self.forbidden = True
            return
        if mtype == M.S_CONNECTION_SUCCESSFUL:
            self.fault("0x80 after the handshake: unchecked jump table")
            return
        if mtype not in DISPATCHABLE:
            self.fault(f"0x{mtype:02x}: outside the client's jump table")
            return
        try:
            handler = getattr(self, f"_h_{mtype:02x}", None)
            if handler:
                r = M.Reader(payload)
                handler(r)
                if mtype not in (0x92,) and not r.eof() and mtype not in (0x84,):
                    self.fault(f"0x{mtype:02x}: {len(r.data) - r.pos} unread bytes")
        except struct.error as exc:
            self.fault(f"0x{mtype:02x}: read past end ({exc})")

    def _h_81(self, r: M.Reader) -> None:
        o = M.MatchOptions(r.u8(), r.string(), r.u8(), r.u8(), r.u8(), r.u8(), r.u16())
        if len(o.map_name.encode("cp1251")) > 31:
            self.fault("map_name overflows char[32]")
        if not 1 <= o.players_count <= 20:
            self.fault(f"players_count {o.players_count}")
        self.options = o
        self.profiles = []

    def _h_92(self, r: M.Reader) -> None:
        if self.options is None:
            self.fault("0x92 before 0x81 (received_players_count is 0xFF)")
            return
        team, is_local = r.u8(), r.u8() != 0
        n = r.u8()
        if n >= 32:
            self.fault("profile_name overflows char[32]")
        name = r.data[r.pos:r.pos + n].decode("cp1251"); r.pos += n
        mask = r.u16()
        for i in range(11):
            if mask & (1 << i):
                r.u8(); r.f32()
        if mask >> 11:
            self.fault("booster mask bits above 10")
        slots: Dict[int, M.ItemInstance] = {}
        while not r.eof():
            slot = r.u8()
            if slot >= 19:
                self.fault(f"profile slot {slot} >= 19 (out-of-bounds write)")
                return
            mode = M.SLOT_SERIALIZE_MODE[slot]
            dict_id, iid = r.u16(), r.u32()
            cond = r.u16() if mode != 1 else 0
            amount = r.u32() if mode != 0 else 0
            if dict_id not in self.data.items:
                self.fault(f"unknown dict_id {dict_id}")
            if iid:
                slots[slot] = M.ItemInstance(dict_id, iid, cond, amount)
        pid = len(self.profiles)
        if pid >= self.options.players_count:
            self.fault("more profiles than players_count")
            return
        if not any(s in slots and self.data.items.get(slots[s].dict_id) and
                   self.data.items[slots[s].dict_id].kind == ITEM_WEAPON for s in (7, 10)):
            self.fault(f"profile {pid} has no weapon: NULL activate() at spawn")
        self.profiles.append((M.PlayerProfile(name, team, slots), is_local))
        if is_local:
            if self.local_id is not None:
                self.fault("two local profiles")
            self.local_id = pid
        if len(self.profiles) == self.options.players_count:
            if self.local_id is None:
                self.fault("no local profile")
            self.load_done_at = self.now + self.load_delay_ms   # game::load

    def _h_93(self, r: M.Reader) -> None:
        count = r.u32()
        for _ in range(count):
            r.u32(); r.u32(); r.u32(); r.u32()

    def _h_84(self, r: M.Reader) -> None:
        if not self.sent_join:
            self.fault("0x84 before 0x42: NULL player")
            return
        pid = r.u8()
        if self.options is None or pid >= len(self.profiles):
            self.fault(f"0x84 for unknown player {pid}: NULL player")
            return
        profile = self.profiles[pid][0]
        pos, yaw, pitch, alive = r.float3(), r.f32(), r.f32(), r.boolean()
        cur, tgt = r.u8(), r.u8()
        expect = 7 if 7 in profile.slots else 10
        for s in (cur, tgt):
            if s != expect:
                self.fault(f"active slot byte {s}, insert() picked {expect}")
        stamina = (r.f32(), r.u32(), r.u32(), r.boolean())
        weapons = {}
        for slot in range(7, 19):
            item = profile.slots.get(slot)
            if not item:
                continue
            info = self.data.items[item.dict_id]
            if info.kind == ITEM_ARTEFACT:
                continue
            if info.kind != ITEM_WEAPON:
                weapons[slot] = r.u16()
                continue
            r.u16(); r.u32(); r.s32(); r.u8(); r.u32()
            mag = r.u16(); r.u16(); r.u8()
            ammo_slot = r.u8()
            if ammo_slot != 19 and ammo_slot >= 19:
                self.fault(f"weapon ammo_slot {ammo_slot}")
            if info.has_chamber:
                r.u8()
            weapons[slot] = mag
            if slot == cur:
                r.u8(); r.u8(); r.u32(); r.u32()
                state = r.u8()
                states = 8 + (2 if info.has_chamber else 0)
                if state >= states:
                    self.fault(f"weapon fsm state {state} out of range")
                    return
                r.pos += WEAPON_STATE_PAYLOAD[state]
                if r.u8() > 3:
                    self.fault("user animation state out of range")
        if not r.eof():
            self.fault(f"0x84 has {len(r.data) - r.pos} unread bytes: layout mismatch")
        # process_player_respawn: remove() (detaches the current player), insert(), attach
        if pid in self.inserted and self.current == pid:
            self.current = None
        old = self.inserted.get(pid, {})
        self.inserted[pid] = {"position": pos, "spawn_position": pos, "yaw": yaw, "pitch": pitch,
                              "alive": alive, "stamina": stamina, "weapons": weapons,
                              "shown_slot": cur, "spawns": old.get("spawns", 0) + 1}
        self.visible[pid] = True
        if pid == self.local_id and self.game_status == 4 and self.time_synced:
            self._attach()

    def _h_8b(self, r: M.Reader) -> None:
        self.connected_mask = r.u32()
        self.sync_responses += 1
        self.enqueue(M.C_TIME_SYNC_CONFIRMATION)
        self.time_synced = True
        local = self.local
        if self.current is None and self.game_status == 4 and local and local["alive"]:
            self._attach()

    def _h_9a(self, r: M.Reader) -> None:
        status = r.u32()
        if status > 4:
            self.fault(f"game status {status}")
        if status != self.game_status and status == 4 and self.local_id is not None \
                and self.time_synced:
            self._attach()                       # retail process_game_status
        self.game_status = status
        self.statuses.append(status)

    def _h_9b(self, r: M.Reader) -> None:
        self.wait_time = r.u32()

    def _h_86(self, r: M.Reader) -> None:
        self.match_time_ms = r.u32()

    def _h_87(self, r: M.Reader) -> None:
        self.respawn_time = r.u32()

    def _h_88(self, r: M.Reader) -> None:
        pid, kills, deaths = r.u8(), r.u32(), r.u32()
        if self.options is None or pid >= self.options.players_count:
            self.fault(f"0x88 for player {pid} beyond players_count")
        self.kd[pid] = (kills, deaths)

    def _h_82(self, r: M.Reader) -> None:
        t = r.u32()
        entries = []
        if r.eof():
            self.fault("empty 0x82: do/while reads past the end")
            return
        while True:
            pid = r.u8()
            if self.options is None or pid >= len(self.profiles):
                self.fault(f"0x82 entry for player {pid} beyond players_count")
            inp = M.PlayerInput.read(r)
            st = M.PlayerState.read(r)
            slot, ammo_slot, wstate = r.u8(), r.u8(), r.u8()
            if pid == self.local_id:
                # player::time_warp on the LOCAL player rewinds and replays its history
                self.own_corrections += 1
            entries.append({"id": pid, "position": st.position, "yaw": st.yaw,
                            "actions": inp.actions_mask, "slot": slot, "ammo_slot": ammo_slot})
            self.remote[pid] = {"position": st.position, "yaw": st.yaw, "time": t,
                                "actions": inp.actions_mask, "slot": slot,
                                "ammo_slot": ammo_slot}
            if pid != self.local_id and pid in self.inserted:
                self.inserted[pid]["position"] = st.position      # target transform
                self._proxy_quick_slots(pid, inp.actions_mask)
            if r.eof():
                break
        self.corrections.append((t, entries))

    def _proxy_quick_slots(self, pid: int, actions: int) -> None:
        """player::process_quick_slots_for_proxy_player: a remote player changes weapon only
        from bits 0x1000 / 0x2000 of the relayed input (the entry's slot id is not read),
        and only to a slot that holds a weapon."""
        if pid >= len(self.profiles):
            return
        slots = self.profiles[pid][0].slots
        shown = self.inserted[pid]
        for bit, slot in ((0x1000, 7), (0x2000, 10)):
            if actions & bit and slot in slots and self.data.items[slots[slot].dict_id].kind == ITEM_WEAPON:
                shown["shown_slot"] = slot

    def _h_83(self, r: M.Reader) -> None:
        victim, killer, headshot, item = r.u8(), r.u8(), r.boolean(), r.u32()
        if not (self._valid_player(victim, "0x83 victim") and self._valid_player(killer, "0x83 killer")):
            return
        if item and item not in self.data.items:
            self.fault(f"0x83 item_by_id({item}) of an unknown dict id")
        if self.local_id is None:
            self.fault("0x83 without a local player (get_local_player()->team())")
        self.kills.append((victim, killer, headshot, item))
        p = self.inserted.get(victim)
        if p and p["alive"]:
            p["alive"] = False                    # player::kill

    def _h_89(self, r: M.Reader) -> None:
        initiator, victim = r.u8(), r.u8()
        part, dtype = r.string(), r.string()
        amount, ap = r.f32(), r.f32()
        if len(part.encode()) > 15 or len(dtype.encode()) > 15:
            self.fault("0x89 string overflows char[16]")
        if not self._valid_player(victim, "0x89 victim"):
            return
        if part not in self.hit_parts:
            self.fault(f"0x89 body part {part!r}: get_body_part() returns NULL")
        elif dtype not in self.hit_parts[part]:
            self.fault(f"0x89 hit type {dtype!r} on {part!r}: get_hit_parameters() NULL")
        if initiator != 0xFF:
            self._valid_player(initiator, "0x89 initiator")
        self.hits.append((initiator, victim, part, dtype, amount, ap))

    def _h_8a(self, r: M.Reader) -> None:
        pid, part, affect, event = r.u8(), r.string(), r.u32(), r.u32()
        if not self._valid_player(pid, "0x8a"):
            return
        if part not in self.hit_parts:
            self.fault(f"0x8a body part {part!r}: get_body_part() returns NULL")
        if affect >= 9 or event >= 3:
            self.fault(f"0x8a affect {affect} event {event} out of range")
        self.affects.append((pid, part, affect, event))

    def _h_8c(self, r: M.Reader) -> None:
        # process_match_finished -> close_current_match(false): disconnect, back to lobby
        self.finished = True
        self.conn.disconnect()

    def _h_91(self, r: M.Reader) -> None:
        pid, visible = r.u8(), r.boolean()
        if pid < len(self.profiles):
            self.visible[pid] = visible

    def _require_current(self, what: str) -> bool:
        if self.current is None:
            self.fault(f"{what} while the client has no current player "
                       "(get_current_player()->team() dereferences NULL)")
            return False
        return True

    def _item_ok(self, item: int, what: str) -> bool:
        n = self.options.victory_items_count if self.options else 0
        if item >= n:
            self.fault(f"{what}: victory item {item} >= victory_items_count {n}")
            return False
        return True

    def _h_94(self, r: M.Reader) -> None:
        if not self._require_current("0x94"):
            return
        self.score = (r.s8(), r.s8())
        n = r.u8()
        for _ in range(n):
            holder, item, pos = r.u8(), r.u8(), r.float3()
            if not self._item_ok(item, "0x94"):
                continue
            if holder == 0xFF:
                if item in self.items_world:
                    self.fault(f"0x94 puts item {item} that is already in the world")
                self.items_world[item] = pos
            elif self._valid_player(holder, "0x94 holder"):
                self.items_held[item] = holder
        m = r.u8()
        for _ in range(m):
            cid, k = r.u8(), r.u8()
            if cid not in self.containers_on_map:
                self.fault(f"0x94 container {cid} does not exist (NULL container)")
            stack = self.containers.setdefault(cid, [])
            for _ in range(k):
                item = r.u8()
                if self._item_ok(item, "0x94 container"):
                    self.items_world.pop(item, None)
                    stack.append(item)
        if n or m:
            self.items_initialized += 1

    def _h_95(self, r: M.Reader) -> None:
        if not self._require_current("0x95"):
            return
        pid, item, is_take, cid = r.u8(), r.u8(), r.boolean(), r.u8()
        if not is_take and cid == 0xFF:
            r.float3()
        if not (self._valid_player(pid, "0x95") and self._item_ok(item, "0x95")):
            return
        container = self.containers.get(cid) if cid in self.containers_on_map else None
        if is_take:
            if container is not None:
                if not container:
                    self.fault("0x95 take from an empty container (vector::back)")
                    return
                popped = container.pop()
                if popped != item:
                    self.fault(f"0x95 names item {item} but take_item() pops {popped}")
            else:
                if item not in self.items_world:
                    self.fault(f"0x95 takes item {item} that is not in the world")
                self.items_world.pop(item, None)
            self.items_held[item] = pid
        else:
            self.items_held.pop(item, None)
            if container is not None:
                container.append(item)
            else:
                if item in self.items_world:
                    self.fault(f"0x95 puts item {item} that is already in the world")
                p = self.inserted.get(pid)
                self.items_world[item] = p["position"] if p else (0.0, 0.0, 0.0)

    def _h_9e(self, r: M.Reader) -> None:
        if not self.sent_join:
            self.fault("0x9e before 0x42: NULL player")
        pid = r.u8()
        for _ in self.data.body_parts:
            r.f32(); r.u32()
            for _ in range(r.u8()):
                r.u8(); r.u32()
