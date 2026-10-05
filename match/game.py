"""One match: roster, rounds (gather_victory_items), combat, respawns, victory items.

A *lobby match* has a fixed roster built from the lobby tickets before anyone connects
(every client gets the same 0x81 players_count and the same 0x92 profiles in the same
order). Players who have not connected yet are in the roster but never inserted: no
0x84 is sent for them until they join (spec 2.2, 2.4). The *open match* takes ticketless
sessions (dev / tests) and grows its roster as they connect; a client only ever learns
about the players that existed when it received 0x81 (the M2 behaviour).

Round flow of a lobby match (spec 9):
    waiting_for_players (0x9a 2, 0x9b join timeout) until the whole roster has joined or
    join_timeout_s passed -> final_countdown (0x9a 3, 0x9b) -> inprocess (0x9a 4, 0x86
    match time every second) -> a team stores victory_items_count items in its container
    or the timer runs out -> finished: final score (0x94), 0x86 0, then after end_delay_s
    0x8c match_finished; the client returns to the lobby.
A one-player roster and the open match start inprocess at the first 0x42 (M2 order).
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from . import ballistics as B
from . import combat as C
from . import level_collision
from . import messages as M
from .game_data import (ITEM_ARTEFACT, ITEM_WEAPON, AmmoInfo, GameData, MedkitInfo, Ticket,
                        WeaponInfo)
from .model import ClientSession, MatchConfig, Player, ammo_slot_for, u32_lt

log = logging.getLogger("match.game")

# Optional movement trace for client-smoothness comparisons (SURV_TRACE_INPUT=<csv path>): one row
# per accepted 0x43 with server arrival time, client time, position, view and action mask.
_TRACE_PATH = os.environ.get("SURV_TRACE_INPUT")
_TRACE = open(_TRACE_PATH, "a", buffering=1, encoding="ascii") if _TRACE_PATH else None


def _trace_input(match_id: int, player_id: int, server_ms: float, u: "M.ClientPlayerUpdate") -> None:
    x, y, z = u.state.position
    _TRACE.write("%d,%d,%.1f,%d,%.4f,%.4f,%.4f,%.4f,%.4f,%d\n" % (
        match_id, player_id, server_ms, u.time_in_ms, x, y, z, u.state.yaw, u.state.pitch,
        u.input.actions_mask))


WAITING, COUNTDOWN, INPROCESS, FINISHED = "waiting", "countdown", "inprocess", "finished"
USE_BIT = 0x10000000
SELECT_WEAPON_BITS = {M.WEAPON1_SLOT: 0x1000, M.WEAPON2_SLOT: 0x2000}
QUICK_SLOT_DOWN_BITS = [(13 + k, 0x4000 << (2 * k)) for k in range(6)]
HEADSHOT_PARTS = ("head", "face")
SILENT_PEER_MS = 3000          # no datagram for this long: stop queueing 0x82 to that peer
RESPAWN_LOS_BUDGET_S = 0.006   # wall-clock cap of the respawn line-of-sight rays per respawn
_NO_INPUT = M.PlayerInput()


@dataclass
class VictoryItem:
    index: int
    position: Tuple[float, float, float]
    holder: Optional[int] = None          # player id
    container: Optional[int] = None       # container id


class Match:
    def __init__(self, core, key, match_id: int, roster: Optional[List[Ticket]],
                 rules: bool) -> None:
        self.core = core
        self.key = key
        self.match_id = match_id
        self.config: MatchConfig = core.config
        self.data: GameData = core.data
        self.rng = core.rng
        self.fixed = roster is not None
        self.rules = rules
        self.collision = None
        if self.config.world_collision:
            self.collision = level_collision.load_level(self.config.map_name, self.config.collision_path)
            if self.collision is None:
                log.warning("match %d: no collision cache for %s (run match/tools/"
                            "build_level_collision.py); walls will not stop bullets",
                            match_id, self.config.map_name)
        self.character = B.CharacterParams.from_cfg(self.data.character)
        self.players: List[Player] = [Player(i, t) for i, t in enumerate(roster or [])]
        for p in self.players:
            self._setup_player(p)
        self.created_ms = core.now_ms
        self.state = INPROCESS if not self.fixed else WAITING
        self.state_since = core.now_ms
        self.countdown_end = 0
        self.match_end_ms = 0
        self.last_second = None
        self.finished_at: Optional[int] = None
        self.result = ""
        self.winner: Optional[int] = None
        self.last_tick_ms = core.now_ms
        self.ever_connected = False
        self.empty_since: Optional[int] = None
        self.removed = False
        self._pose_cache: Dict[tuple, tuple] = {}
        # victory items (mode 2)
        n = self.items_count
        self.containers: Dict[int, List[int]] = {}
        self.container_info = {c.container_id: c for c in self.data.victory_containers(self.config.map_name)}
        self.items: List[VictoryItem] = []
        if n:
            spawners = self.data.victory_spawners(self.config.map_name)
            picks = spawners[:n] if self.config.deterministic_spawns else self.rng.sample(spawners, n)
            self.items = [VictoryItem(i, pos) for i, pos in enumerate(picks)]
            self.containers = {cid: [] for cid in sorted(self.container_info)}
        if self.fixed:
            log.info("match %d created: %d players %s, %d victory items, %d s", match_id,
                     len(self.players), [(p.ticket.name, p.team) for p in self.players], n,
                     self.config.match_time)

    # ------------------------------------------------------------- roster
    @property
    def items_count(self) -> int:
        if not (self.rules and self.config.mode == M.MODE_GATHER_VICTORY_ITEMS):
            return 0
        n = len(self.data.victory_spawners(self.config.map_name))
        return max(0, min(self.config.victory_items_count, n, 10))

    def sessions(self) -> List[ClientSession]:
        return [p.session for p in self.players if p.session is not None]

    def joined_sessions(self) -> List[ClientSession]:
        return [s for s in self.sessions() if s.joined]

    def player_for(self, ticket: Ticket) -> Optional[Player]:
        p = next((p for p in self.players if p.ticket.session_id == ticket.session_id), None)
        if p is not None or self.fixed:
            return p
        if len(self.players) >= self.config.max_players:
            return None
        p = Player(len(self.players), ticket)
        self._setup_player(p)
        self.players.append(p)
        return p

    def _setup_player(self, p: Player) -> None:
        p.damage = C.DamageModel(self.data.body_parts, self.data.armour_mods(p.ticket.slots),
                                 {b.id: b.value for b in p.ticket.boosters.values()})
        p.active_slot = self.data.active_weapon_slot(p.ticket.slots) or M.WEAPON1_SLOT

    @staticmethod
    def visible(s: ClientSession, p: Player) -> bool:
        """Only ids below the players_count this client got in 0x81 exist on it."""
        return p.id < s.roster_size

    def connected_mask(self) -> int:
        mask = 0
        for p in self.players:
            if p.connected:
                mask |= 1 << p.id
        return mask

    # -------------------------------------------------------- connection
    def on_connected(self, s: ClientSession) -> None:
        self.ever_connected = True
        self.empty_since = None
        if self.state == FINISHED:
            s.send(M.S_MATCH_FINISHED)             # too late: straight back to the lobby
            s.finished_sent = True

    def on_player_gone(self, p: Player, replaced: bool) -> None:
        """Session ended (disconnect / timeout) or replaced by a reconnect: the player
        stays in the roster; its body is hidden on the other clients until it rejoins."""
        if p.carrying is not None:
            self._drop_item(p)
        if p.spawned:
            p.alive = False
            p.respawn_at = None
            for s in self.joined_sessions():
                if self.visible(s, p):
                    s.send(M.S_SPAWN_PLAYER, self.spawn_message(p).encode())
                    s.send(M.S_PLAYER_VISIBILITY_CHANGED, M.encode_visibility(p.id, False))
        p.has_input = False
        p.medkits.clear()
        self._broadcast_connected_mask()
        log.info("match %d: player %d %r %s", self.match_id, p.id, p.ticket.name,
                 "reconnecting" if replaced else "left")

    # -------------------------------------------------------- handlers
    def on_startup_info(self, s: ClientSession) -> None:
        if s.startup_sent:
            return
        cfg = self.config
        opts = M.MatchOptions(cfg.map_id, cfg.map_name, cfg.mode, len(self.players),
                              self.items_count, cfg.respawn_time, cfg.match_time)
        s.send(M.S_MATCH_OPTIONS, M.encode_match_options(opts))
        for p in self.players:
            profile = M.PlayerProfile(p.ticket.name, p.ticket.team, dict(p.ticket.slots),
                                      dict(p.ticket.boosters))
            s.send(M.S_PLAYER_PROFILE, M.encode_player_profile(profile, p is s.player))
        s.startup_sent = True
        s.roster_size = len(self.players)

    def on_team_bases_info(self, s: ClientSession) -> None:
        s.send(M.S_TEAM_BASES, M.encode_u32(0))        # level_03 has no base points

    def on_join(self, s: ClientSession) -> None:
        if s.joined or s.player is None:
            return
        s.joined = True
        me = s.player
        self.spawn(me)
        # the joiner's own spawn first, then everyone else it knows about
        s.send(M.S_SPAWN_PLAYER, self.spawn_message(me).encode())
        s.local_spawn_sent = True
        for p in self.players:
            if p is me or not p.spawned or not self.visible(s, p):
                continue
            s.send(M.S_SPAWN_PLAYER, self.spawn_message(p).encode())
            if not p.connected:
                s.send(M.S_PLAYER_VISIBILITY_CHANGED, M.encode_visibility(p.id, False))
        for other in self.joined_sessions():
            if other is not s and self.visible(other, me):
                other.send(M.S_SPAWN_PLAYER, self.spawn_message(me).encode())
        if self.state == WAITING:
            self._check_start()                    # may start / count down right away
        self._send_status(s)
        for p in self.players:
            if (p.kills or p.deaths) and self.visible(s, p):
                s.send(M.S_PLAYER_KD_STATS_CHANGED, M.encode_kd_stats(p.id, p.kills, p.deaths))
        self._broadcast_connected_mask(exclude=s)
        self.maybe_attach(s)

    def on_sync_request(self, s: ClientSession) -> None:
        s.send(M.S_SYNC_RESPONSE, M.encode_sync_response(self.connected_mask()))
        s.sync_sent_ms = self.core.now_ms

    def on_sync_confirmation(self, s: ClientSession) -> None:
        if s.sync_sent_ms is not None:
            s.rtt_ms = max(0, self.core.now_ms - s.sync_sent_ms)
            s.sync_sent_ms = None
        if not s.synced:
            s.synced = True
            log.debug("%s:%d: player %s time-synced", s.addr[0], s.addr[1],
                     s.player.id if s.player else "?")
            self.maybe_attach(s)

    def on_player_update(self, s: ClientSession, u: M.ClientPlayerUpdate) -> None:
        p = s.player
        if p is None or not p.spawned or not p.alive:
            return              # a dead player's late inputs (sent before 0x83 arrived)
        # Client-authoritative movement (spec 6). Only accept non-decreasing times.
        if p.has_input and u32_lt(u.time_in_ms, p.last_input_time):
            return
        prev = p.last_input.actions_mask if p.has_input else 0
        p.position = u.state.position
        p.yaw = u.state.yaw
        p.pitch = u.state.pitch
        p.last_input = u.input
        p.last_input_time = u.time_in_ms
        p.last_input_server_ms = self.core.now_ms
        p.has_input = True
        p.inputs_received += 1
        p.history.append((self.core.now_ms, p.position, p.yaw, u.input.actions_mask))
        if _TRACE is not None:
            _trace_input(self.match_id, p.id, self.core.now_ms, u)
        if self.state != INPROCESS:
            return
        self._weapon_input(p, u, prev)
        rising = u.input.actions_mask & ~prev
        if rising & USE_BIT:
            self._use(p)
        for slot, bit in QUICK_SLOT_DOWN_BITS:
            if rising & bit:
                self._quick_slot(p, slot)

    def on_suicide(self, s: ClientSession) -> None:
        p = s.player
        if p is not None and p.alive and self.state != FINISHED:
            log.info("match %d: player %d commits suicide", self.match_id, p.id)
            self.kill(p, p, False, 0)

    # ------------------------------------------------------------- status
    def _status_value(self) -> int:
        return {WAITING: M.GAME_STATUS_WAITING_FOR_PLAYERS,
                COUNTDOWN: M.GAME_STATUS_FINAL_COUNTDOWN}.get(self.state, M.GAME_STATUS_INPROCESS)

    def _wait_seconds(self) -> int:
        if self.state == WAITING:
            left = self.created_ms + 1000 * self.config.join_timeout_s - self.core.now_ms
        elif self.state == COUNTDOWN:
            left = self.countdown_end - self.core.now_ms
        else:
            return 0
        return max(0, int(math.ceil(left / 1000.0)))

    def _send_status(self, s: ClientSession) -> None:
        value = self._status_value()
        if s.status_value != value:
            s.send(M.S_GAME_STATUS_CHANGED, M.encode_game_status(value))
            s.status_value = value
            s.status_sent = True
        if value in (M.GAME_STATUS_WAITING_FOR_PLAYERS, M.GAME_STATUS_FINAL_COUNTDOWN):
            s.send(M.S_MATCH_WAIT_TIME_CHANGED, M.encode_u32(self._wait_seconds()))
        self.maybe_attach(s)

    def maybe_attach(self, s: ClientSession) -> None:
        """Mirror of when the client gets m_current_player (retail process_game_status /
        process_sync_response / process_player_respawn, spec 5): status 4 + time-synced +
        its own player inserted. Only then may 0x94/0x95 be sent (game_world_ui::
        set_victory_points dereferences get_current_player())."""
        if s.attached or s.status_value != M.GAME_STATUS_INPROCESS or not s.synced \
                or not s.local_spawn_sent:
            return
        s.attached = True
        if self.items:
            s.send(M.S_INITIALIZE_VICTORY_ITEMS, self._items_snapshot())
            s.items_sent = True
        if self.rules and self.state in (INPROCESS, FINISHED):
            s.send(M.S_MATCH_TIME_CHANGED, M.encode_u32(self._time_left_ms()))

    def _time_left_ms(self) -> int:
        if self.state == FINISHED:
            return 0
        return max(0, self.match_end_ms - self.core.now_ms)

    def _check_start(self) -> None:
        joined = [p for p in self.players if p.session is not None and p.session.joined]
        if not joined:
            return
        everyone = len(joined) == len(self.players)
        timed_out = self.core.now_ms >= self.created_ms + 1000 * self.config.join_timeout_s
        if not (everyone or timed_out):
            return
        if len(self.players) > 1 and self.config.countdown_s > 0:
            self._set_state(COUNTDOWN)
            self.countdown_end = self.core.now_ms + 1000 * self.config.countdown_s
            for s in self.joined_sessions():
                self._send_status(s)
        else:
            self._start()

    def _start(self) -> None:
        self._set_state(INPROCESS)
        self.match_end_ms = self.core.now_ms + 1000 * self.config.match_time
        log.info("match %d in process: %d/%d players joined", self.match_id,
                 len(self.joined_sessions()), len(self.players))
        for s in self.joined_sessions():
            self._send_status(s)

    def _set_state(self, state: str) -> None:
        self.state = state
        self.state_since = self.core.now_ms
        self.last_second = None

    # --------------------------------------------------------------- tick
    def tick(self, now: int) -> bool:
        """Returns False once the match can be removed."""
        dt = max(0, now - self.last_tick_ms)
        self.last_tick_ms = now
        second = now // 1000
        new_second = second != self.last_second
        self.last_second = second
        if self.state == WAITING:
            self._check_start()
            if new_second and self.state == WAITING:
                for s in self.joined_sessions():
                    s.send(M.S_MATCH_WAIT_TIME_CHANGED, M.encode_u32(self._wait_seconds()))
        elif self.state == COUNTDOWN:
            if now >= self.countdown_end:
                self._start()
            elif new_second:
                for s in self.joined_sessions():
                    s.send(M.S_MATCH_WAIT_TIME_CHANGED, M.encode_u32(self._wait_seconds()))
        elif self.state == INPROCESS:
            self._tick_players(now, dt)
            if self.rules:
                if now >= self.match_end_ms:
                    self.finish("time is up")
                elif new_second:
                    for s in self.sessions():
                        if s.attached:
                            s.send(M.S_MATCH_TIME_CHANGED, M.encode_u32(self._time_left_ms()))
        if self.state == FINISHED:
            return self._tick_finished(now)
        return self._tick_empty(now)

    def _tick_empty(self, now: int) -> bool:
        if not self.fixed:
            return True
        if any(p.session is not None for p in self.players):
            self.empty_since = None
            return True
        if self.empty_since is None:
            self.empty_since = now
        limit = self.config.empty_match_timeout_s if self.ever_connected \
            else max(self.config.empty_match_timeout_s, 2 * self.config.join_timeout_s)
        if now - self.empty_since >= 1000 * limit:
            log.info("match %d: nobody connected for %.0f s; closing", self.match_id, limit)
            return False
        return True

    def _tick_finished(self, now: int) -> bool:
        assert self.finished_at is not None
        if now >= self.finished_at + 1000 * self.config.end_delay_s:
            for s in self.sessions():
                if s.handshaked and not s.finished_sent:
                    s.send(M.S_MATCH_FINISHED)
                    s.finished_sent = True
        if now >= self.finished_at + 1000 * (self.config.end_delay_s + self.config.finish_grace_s):
            for s in self.sessions():
                if s.conn.is_connected():
                    log.info("%s:%d: did not leave after match_finished; disconnecting", *s.addr)
                    s.conn.disconnect()
        return any(p.session is not None for p in self.players) or \
            now < self.finished_at + 1000 * (self.config.end_delay_s + 2 * self.config.finish_grace_s)

    def _tick_players(self, now: int, dt: int) -> None:
        for p in self.players:
            if p.connected:
                p.play_ms += dt
            if p.alive and p.damage is not None:
                p.damage.tick(dt, now)
                self._tick_medkits(p, now, dt)
                self._send_affects(p)
            elif not p.alive and p.respawn_at is not None and p.connected and p.session.joined:
                left = max(0, int(math.ceil((p.respawn_at - now) / 1000.0)))
                if now >= p.respawn_at:
                    self.respawn(p)
                elif left != p.respawn_shown:
                    p.session.send(M.S_RESPAWN_TIME_CHANGED, M.encode_u32(left))
                    p.respawn_shown = left

    # ---------------------------------------------------------- spawning
    def spawn(self, p: Player) -> None:
        point = self._pick_point(p)
        p.position = point.position
        p.yaw = point.yaw
        p.pitch = 0.0
        p.alive = True
        p.spawned = True
        p.respawn_at = None
        p.has_input = False
        p.history.clear()
        p.medkits.clear()
        p.damage.reset()
        for slot, item in p.ticket.slots.items():
            if self.data.items[item.dict_id].kind == ITEM_WEAPON:
                if self.config.deterministic_spawns:
                    p.weapon_seeds[slot] = (0, 1)
                else:
                    p.weapon_seeds[slot] = (self.rng.getrandbits(32),
                                            self.rng.randint(1, 0x7FFFFFFF))
        self._reset_inventory(p)
        log.debug("match %d: player %d %r spawned at respawn point %d %s yaw %.3f",
                 self.match_id, p.id, p.ticket.name, point.point_id,
                 tuple(round(c, 3) for c in point.position), point.yaw)

    def _pick_point(self, p: Player):
        if self.config.deterministic_spawns:
            points = sorted((q for q in self.data.respawn_points(self.config.map_name)
                             if q.team == p.team), key=lambda q: q.point_id)
            k = sum(1 for o in self.players if o.team == p.team and o.id < p.id)
            return points[k % len(points)] if points else \
                self.data.first_respawn(self.config.map_name, p.team)
        if self.collision is not None and self.config.los_respawn:
            point = self._unseen_respawn(p)
            if point is not None:
                return point
        return self.data.pick_respawn(self.config.map_name, p.team, self.rng)

    def _unseen_respawn(self, p: Player):
        """A respawn point of p's team that no living enemy can see (eye to chest), front
        points (priority 2) first; None when every point is watched. [A] the original
        server's choice is unknown."""
        points = [q for q in self.data.respawn_points(self.config.map_name) if q.team == p.team]
        enemies = [C.eye_position(e.position, e.last_input.actions_mask if e.has_input else 0)
                   for e in self.players if e.alive and e.team != p.team and e is not p]
        if not points or not enemies:
            return None
        front = [q for q in points if q.priority == 2]
        rest = [q for q in points if q.priority != 2]
        self.rng.shuffle(front)
        self.rng.shuffle(rest)
        budget = 48                      # rays; a respawn must not stall the 30 Hz tick
        deadline = time.perf_counter() + RESPAWN_LOS_BUDGET_S     # ~0.5 ms a ray, up to 5
        for q in front + rest:
            chest = (q.position[0], q.position[1] + 1.2, q.position[2])
            seen = False
            for e in enemies:
                if budget <= 0 or time.perf_counter() > deadline:
                    return None
                budget -= 1
                if self.collision.line_of_sight(e, chest):
                    seen = True
                    break
            if not seen:
                return q
        return None

    def respawn(self, p: Player) -> None:
        self.spawn(p)
        msg = self.spawn_message(p).encode()
        for s in self.joined_sessions():
            if self.visible(s, p):
                s.send(M.S_SPAWN_PLAYER, msg)
        if p.connected:
            p.session.send(M.S_RESPAWN_TIME_CHANGED, M.encode_u32(0))
        p.respawn_shown = -1

    def _reset_inventory(self, p: Player) -> None:
        """Server copy of what 0x84 gives the client (spawn_message uses the same)."""
        slots = p.ticket.slots
        p.weapons.clear()
        p.switched = False
        p.reserve.clear()
        p.quick.clear()
        p.active_slot = self.data.active_weapon_slot(slots) or M.WEAPON1_SLOT
        for slot, item in slots.items():
            info = self.data.items[item.dict_id]
            if slot in (8, 9, 11, 12):
                p.reserve[slot] = min(item.condition_or_stack, item.amount_in_inventory)
            elif 13 <= slot <= 18 and info.kind != ITEM_ARTEFACT:
                p.quick[slot] = min(item.condition_or_stack, item.amount_in_inventory)
        for slot, item in slots.items():
            if self.data.items[item.dict_id].kind != ITEM_WEAPON:
                continue
            winfo = self.data.weapons.get(item.dict_id) or WeaponInfo()
            ammo_slot = ammo_slot_for(slot, slots)
            # No ammo in either slot: the client's weapon_core::activate sets m_ammunition to
            # NULL, so it must spawn empty - a round in the magazine or chamber lets it fire,
            # and instant_fire dereferences the NULL ammunition (client ACCESS_VIOLATION).
            no_ammo = ammo_slot == M.INVALID_SLOT
            clip = 0 if no_ammo else self.data.items[slots[ammo_slot].dict_id].clip_size
            magazine = 0 if no_ammo else \
                min(winfo.magazine_capacity, clip) if clip else item.condition_or_stack
            w = C.WeaponSim(slot, item.dict_id, winfo, ammo_slot, magazine,
                            chambered=not no_ammo and winfo.has_chamber
                            and self.data.items[item.dict_id].has_chamber)
            w.ready_ms = 0
            w.shot = self._shot_model(p, slot, winfo)
            w.reset_fire_queue()
            p.weapons[slot] = w

    def _shot_model(self, p: Player, slot: int, winfo: WeaponInfo) -> B.ShotModel:
        """The weapon's dispersion_calculator + recoil_calculator with the PRNG seeds this
        spawn sends in 0x84. The recoil calculator's random32 starts at 0 when the client
        creates the weapon and is never reseeded, so it lives for the whole match here."""
        boosters = {b.id: b.value for b in p.ticket.boosters.values()}
        rng = p.recoil_rngs.setdefault(slot, B.Random32(0))
        return B.ShotModel(B.WeaponDispersionParams.from_cfg(winfo.dispersion),
                           B.WeaponRecoilParams.from_cfg(winfo.recoil), self.character,
                           p.weapon_seeds.get(slot, (0, 1)), rng, winfo.double_handed,
                           1.0 + boosters.get(1, 0.0) / 100.0,        # dispersion_correction_perc
                           1.0 + boosters.get(2, 0.0) / 100.0,        # aiming_speed_correction_perc
                           self.config.spread_growth_from_config)

    def spawn_message(self, p: Player) -> M.SpawnPlayer:
        slots = p.ticket.slots
        active = self.data.active_weapon_slot(slots)
        assert active is not None, "validated loadouts always have a weapon"
        if not p.weapons:
            self._reset_inventory(p)
        states = []
        for slot in sorted(slots):
            if slot in M.ARMOUR_SLOTS:
                continue
            item = slots[slot]
            info = self.data.items[item.dict_id]
            if info.kind == ITEM_ARTEFACT:
                continue                                  # 0 bytes (spec 2.5)
            if info.kind == ITEM_WEAPON:
                w = p.weapons[slot]
                seed, nseed = p.weapon_seeds.get(slot, (0, 1))
                states.append((slot, M.WeaponSpawnState(
                    amount=item.condition_or_stack, random_seed=seed, normal_random_seed=nseed,
                    ammo_in_magazine=w.magazine,
                    ammo_slot=w.ammo_slot, has_chamber_state=info.has_chamber,
                    round_chambered=info.has_chamber and w.chambered,
                    has_logic_state=(slot == active), is_shown=True,
                    logic_state_id=3, user_anim_state_id=0)))     # idle, stand
            else:
                amount = p.reserve.get(slot, p.quick.get(slot, min(item.condition_or_stack,
                                                                    item.amount_in_inventory)))
                states.append((slot, M.SimpleItemSpawnState(min(amount, 0xFFFF))))
        # current/target active slot must be the slot player::insert picked (spec 2.4)
        return M.SpawnPlayer(p.id, p.position, p.yaw, p.pitch, p.alive, active, active,
                             M.Stamina(100.0, 0, 0, False), states)

    # ------------------------------------------------------------ weapons
    def _weapon_input(self, p: Player, u: M.ClientPlayerUpdate, prev: int) -> None:
        t = u.time_in_ms
        a = u.input.actions_mask
        rising = a & ~prev
        for bit, slot in ((0x1000, M.WEAPON1_SLOT), (0x2000, M.WEAPON2_SLOT)):
            if rising & bit and slot in p.weapons and p.active_slot != slot:
                old = p.weapons[p.active_slot]
                old.reload_end_ms = None
                p.active_slot = slot
                p.switched = True
                p.weapons[slot].ready_ms = t + C.SHOW_TIME_MS
                p.weapons[slot].next_fire_ms = None
        w = p.weapons.get(p.active_slot)
        if w is None:
            return
        if not w.ready_ms:
            w.ready_ms = t + C.SHOW_TIME_MS           # first input after a (re)spawn
        if w.reload_end_ms is not None and not u32_lt(t, w.reload_end_ms):
            self._finish_reload(p, w)
        reloading = w.reload_end_ms is not None
        self._tick_shot(p, w, a, t, reloading)
        if rising & 0x400 and not (a & 0x20) and not reloading and len(w.info.fire_queue_types) > 1:
            w.fire_queue_type = (w.fire_queue_type + 1) % len(w.info.fire_queue_types)
            w.reset_fire_queue()
        if rising & 0x800 and not reloading:
            self._next_ammo(p, w, t)
            return
        if a & 0x20:
            if reloading or u32_lt(t, w.ready_ms) or C.is_sprinting(a):
                return
            if w.rounds_available() == 0:
                self._start_reload(p, w, t)
                return
            if not (prev & 0x20) or w.next_fire_ms is None:
                w.reset_fire_queue()
                if w.next_fire_ms is None or u32_lt(w.next_fire_ms, t):
                    w.next_fire_ms = t
            elif u32_lt(w.next_fire_ms, t - int(w.info.fire_interval_ms)):
                # the trigger was held while the weapon could not fire (reload, show,
                # sprint): resume now instead of catching up on the missed rounds
                w.next_fire_ms = t
            while w.bullets_in_queue > 0 and w.rounds_available() > 0 \
                    and not u32_lt(t, w.next_fire_ms):
                self._tick_shot(p, w, a, w.next_fire_ms, reloading)
                self._fire(p, w, u)
                w.bullets_in_queue -= 1
                w.next_fire_ms = int(w.next_fire_ms + w.info.fire_interval_ms)
            if w.rounds_available() == 0:
                self._start_reload(p, w, t)
        else:
            w.reset_fire_queue()
            if w.next_fire_ms is not None and u32_lt(w.next_fire_ms, t):
                w.next_fire_ms = None
            if a & 0x40 and w.magazine < w.info.magazine_capacity:
                self._start_reload(p, w, t)

    def _tick_shot(self, p: Player, w: C.WeaponSim, a: int, t: int, reloading: bool) -> None:
        """weapon_core::update_dispersion / update_recoil at client time t. The character
        state follows weapon_user_state_enum: sprint (player_input_inline.h), jump (bit
        0x10 or the feet more than 0.3 m above the level collision) [A], crouch (0x100),
        stand; moving = the update_bones_matrices test (weapon_core.cpp:899); aiming =
        bit 0x80 while not sprinting or reloading [A: the aim transition is not timed]."""
        if w.shot is None:
            return
        if C.is_sprinting(a):
            state = B.SPRINT
        elif a & C.JUMP_BIT or self._airborne(p):
            state = B.JUMP
        elif a & C.CROUCH_BIT:
            state = B.CROUCH
        else:
            state = B.STAND
        moving = (a & 0x1) != (a & 0x2) or (a & 0x8) != (a & 0x4)
        aiming = bool(a & B.AIM_BIT) and state != B.SPRINT and not reloading
        broken = sum(1 for arm in ("left_arm", "right_arm")
                     if arm in p.damage.parts and p.damage.parts[arm].has_affect(3))
        w.shot.tick(t, state, moving, aiming, broken)

    def _airborne(self, p: Player) -> bool:
        if self.collision is None or len(p.history) < 2:
            return False
        if abs(p.history[-1][1][1] - p.history[-2][1][1]) < 0.01:
            return False                 # no vertical motion since the last input: grounded
        g = self.collision.ground_below(p.position, up=0.3, depth=1.0)
        return g is None or p.position[1] - g > 0.3

    def _start_reload(self, p: Player, w: C.WeaponSim, t: int) -> None:
        if w.reload_end_ms is not None or p.reserve.get(w.ammo_slot, 0) <= 0:
            return
        w.reload_end_ms = t + int(1000 * w.info.reload_time)

    def _finish_reload(self, p: Player, w: C.WeaponSim) -> None:
        """weapon_core::load_magazine (+ chamber_a_round on reload)."""
        w.reload_end_ms = None
        w.next_fire_ms = None
        reserve = p.reserve.get(w.ammo_slot, 0)
        load = min(reserve, w.info.magazine_capacity - w.magazine)
        w.magazine += max(0, load)
        p.reserve[w.ammo_slot] = reserve - max(0, load)
        if w.info.has_chamber and not w.chambered and w.magazine > 0:
            w.magazine -= 1
            w.chambered = True
        w.reset_fire_queue()
        if w.shot is not None:
            w.shot.on_reload()

    def _next_ammo(self, p: Player, w: C.WeaponSim, t: int) -> None:
        """weapon_core::set_next_ammo_type: unload into the current slot, switch, reload."""
        pair = (8, 9) if w.slot == M.WEAPON1_SLOT else (11, 12)
        other = pair[1] if w.ammo_slot == pair[0] else pair[0]
        if other not in p.reserve:
            return
        p.reserve[w.ammo_slot] = p.reserve.get(w.ammo_slot, 0) + w.rounds_available()
        w.magazine, w.chambered = 0, False
        w.ammo_slot = other
        self._start_reload(p, w, t)

    def _fire(self, p: Player, w: C.WeaponSim, u: M.ClientPlayerUpdate) -> None:
        # weapon_core::instant_fire: the round comes out of the chamber or the magazine
        if w.info.has_chamber and w.chambered:
            w.chambered = False
            if w.magazine > 0:
                w.magazine -= 1
                w.chambered = True
        else:
            w.magazine -= 1
        w.shots_fired += 1
        p.shots_fired += 1
        ammo_item = p.ticket.slots.get(w.ammo_slot)
        ammo = self.data.ammo.get(ammo_item.dict_id) if ammo_item else None
        ammo = ammo or AmmoInfo()
        amount = w.info.bullet_damage * ammo.k_damage          # per pellet (bullet.cpp:545)
        ap = w.info.bullet_pierce * ammo.k_arp
        actions = u.input.actions_mask
        origin = C.eye_position(u.state.position, actions)
        crouched = bool(actions & C.CROUCH_BIT)
        if w.shot is not None:
            dirs = w.shot.shoot(u.state.yaw, u.state.pitch, crouched, ammo.buck_shot, ammo.dispersion,
                                self.config.dispersion, self.config.recoil)
            if w.info.has_chamber and w.chambered:
                w.shot.on_chamber()          # the bolt cycle runs instant_chamber_a_round
        else:
            dirs = [C.view_direction(u.state.yaw, u.state.pitch)] * ammo.buck_shot
        for direction in dirs:
            hit = self._trace(p, origin, direction, ammo.distance_m, ap,
                              math.radians(ammo.ricochet_angle_deg))
            if hit is not None:
                victim, part = hit
                self.apply_hit(p, victim, part, C.DAMAGE_TYPE_BULLET, amount, ap, w.dict_id)

    def _trace(self, shooter: Player, origin, direction, max_dist: float,
               pierce: Optional[float] = None, ricochet_angle: float = 0.0
               ) -> Optional[Tuple[Player, str]]:
        """Hitscan against capsules at each candidate's current position and, with lag
        compensation, where it was rewind ms ago (the shooter sees remote players late);
        then the level collision between the muzzle and that capsule hit decides whether
        a wall/terrain/prop stopped the round first (only traced when a capsule is hit)."""
        rewind = 0
        if self.config.lag_compensation and shooter.session is not None:
            rewind = min(self.config.max_rewind_ms, shooter.session.rtt_ms // 2 + 100)
        best = None
        for v in self.players:
            if v is shooter or not v.alive or not v.connected:
                continue
            if v.team == shooter.team and not self.config.friendly_fire:
                continue
            if not (self.visible(v.session, shooter) if v.session else False):
                continue
            for pos, yaw, actions in self._poses(v, rewind):
                height = C.capsule_height(actions)
                r = C.ray_vs_capsule(origin, direction, pos, height, max_dist=max_dist)
                if r is None:
                    continue
                dist, hit_h = r
                if best is None or dist < best[0]:
                    point = (origin[0] + direction[0] * dist, origin[1] + direction[1] * dist,
                             origin[2] + direction[2] * dist)
                    part = C.body_part_for(hit_h, height, direction, point, pos, yaw)
                    best = (dist, v, part)
        if best is None:
            return None
        if self.collision is not None:
            wall = self.collision.trace(origin, direction, best[0], pierce, ricochet_angle,
                                        self.config.solid_terrain)
            if wall is not None:
                shooter.shots_blocked += 1
                return None
        return best[1], best[2]

    def _poses(self, v: Player, rewind: int):
        actions = v.last_input.actions_mask if v.has_input else 0
        yield v.position, v.yaw, actions
        if rewind and v.history:
            target = self.core.now_ms - rewind
            # every pellet of every shot this tick asks for the same rewound pose: look it
            # up once per (victim, time, input count)
            key = (v.id, target, v.inputs_received)
            old = self._pose_cache.get(key)
            if old is None:
                if len(self._pose_cache) > 256:
                    self._pose_cache.clear()
                old = self._pose_cache[key] = min(v.history, key=lambda h: abs(h[0] - target))
            if old[1] != v.position:
                yield old[1], old[2], old[3]

    # ------------------------------------------------------------- damage
    def apply_hit(self, shooter: Optional[Player], victim: Player, part: str, damage_type: str,
                  amount: float, ap: float, item_dict_id: int) -> None:
        if not victim.alive or not victim.damage.can_hit(part, damage_type):
            return
        initiator = shooter.id if shooter is not None else 0xFF
        payload = M.encode_hit_player(initiator, victim.id, part, damage_type, amount, ap)
        for s in self.joined_sessions():
            if self.visible(s, victim) and (shooter is None or self.visible(s, shooter)):
                s.send(M.S_HIT_PLAYER, payload)
        victim.damage.hit(part, damage_type, amount, ap, self.core.now_ms)
        if shooter is not None:
            shooter.hits_dealt += 1
        self._send_affects(victim)
        if victim.damage.dead:
            self.kill(victim, shooter or victim, part in HEADSHOT_PARTS, item_dict_id)

    def _send_affects(self, p: Player) -> None:
        events = p.damage.drain_events()
        if not self.config.send_affects:
            return
        for part, affect, event in events:
            if affect == C.AFFECT_DEATH:
                continue                                  # 0x83 handles death
            payload = M.encode_affect_damage_model(p.id, part, affect, event)
            for s in self.joined_sessions():
                # the victim's own client applies affects itself (type_apply_directly)
                if s is not p.session and self.visible(s, p):
                    s.send(M.S_AFFECT_DAMAGE_MODEL, payload)

    def kill(self, victim: Player, killer: Player, headshot: bool, item_dict_id: int) -> None:
        victim.alive = False
        victim.deaths += 1
        victim.medkits.clear()
        if killer is not victim and killer.team != victim.team:
            killer.kills += 1
        if victim.carrying is not None:
            self._drop_item(victim)
        log.info("match %d: %r killed by %r%s", self.match_id, victim.ticket.name,
                 killer.ticket.name, " (headshot)" if headshot else "")
        kill = M.encode_kill_player(victim.id, killer.id, headshot, item_dict_id)
        for s in self.joined_sessions():
            if self.visible(s, victim) and self.visible(s, killer):
                s.send(M.S_KILL_PLAYER, kill)
                s.send(M.S_PLAYER_KD_STATS_CHANGED,
                       M.encode_kd_stats(victim.id, victim.kills, victim.deaths))
                if killer is not victim:
                    s.send(M.S_PLAYER_KD_STATS_CHANGED,
                           M.encode_kd_stats(killer.id, killer.kills, killer.deaths))
        if self.state != FINISHED:
            victim.respawn_at = self.core.now_ms + 1000 * self.config.respawn_time
            victim.respawn_shown = -1

    # ------------------------------------------------------------ medkits
    def _quick_slot(self, p: Player, slot: int) -> None:
        item = p.ticket.slots.get(slot)
        info = self.data.medkits.get(item.dict_id) if item else None
        if info is None or p.quick.get(slot, 0) <= 0:
            return
        p.quick[slot] -= 1
        start = self.core.now_ms + info.delay_ms
        p.medkits.append((start, start + info.activity_time_ms, info))

    def _tick_medkits(self, p: Player, now: int, dt: int) -> None:
        """medkit::active_tick, approximated: the influences are spread linearly over
        activity_time after activation_delay; affects are removed on activation."""
        keep = []
        for start, end, info in p.medkits:
            assert isinstance(info, MedkitInfo)
            lo, hi = max(start, now - dt), min(end, now)
            if now - dt < start <= now:
                for part, affect in info.remove_affects:
                    p.damage.cancel_affect(part, affect)
            if hi > lo:
                frac = (hi - lo) / max(1, end - start)
                for part, amount in info.influences:
                    p.damage.heal(part, amount * frac)
            if now < end:
                keep.append((start, end, info))
        p.medkits = keep

    # ------------------------------------------------------- victory items
    def _items_snapshot(self, score_only: bool = False) -> bytes:
        t1, t2 = self.scores()
        if score_only:
            return M.encode_initialize_victory_items(t1, t2, [], [])
        items = []
        for it in self.items:
            if it.container is not None:
                continue
            holder = it.holder if it.holder is not None else M.VICTORY_ITEM_IN_WORLD
            items.append((holder, it.index, it.position))
        containers = [(cid, list(stack)) for cid, stack in sorted(self.containers.items())]
        return M.encode_initialize_victory_items(t1, t2, items, containers)

    def scores(self) -> Tuple[int, int]:
        out = [0, 0]
        for cid, stack in self.containers.items():
            team = self.container_info[cid].team
            if team in (M.TEAM_1, M.TEAM_2):
                out[team] += len(stack)
        return out[0], out[1]

    def _broadcast_item(self, payload: bytes, score_changed: bool) -> None:
        score = self._items_snapshot(score_only=True) if score_changed else None
        for s in self.sessions():
            if s.attached and s.items_sent:
                s.send(M.S_VICTORY_ITEM_TAKE_OR_PUT, payload)
                if score is not None:
                    # the client's add_victory_points deltas follow container->team()
                    # (:400); resend the authoritative score so the HUD cannot drift
                    s.send(M.S_INITIALIZE_VICTORY_ITEMS, score)

    def _near(self, p: Player, pos, radius: float) -> bool:
        dx, dy, dz = pos[0] - p.position[0], pos[1] - p.position[1], pos[2] - p.position[2]
        return dx * dx + dz * dz <= radius * radius and abs(dy) <= 2.5

    def _use(self, p: Player) -> None:
        """The client sends only the use bit; picking up, storing and stealing are
        decided here (victory_item::use_info / victory_items_container::use_info)."""
        if not self.items or self.state != INPROCESS:
            return
        if p.carrying is not None:
            for cid, stack in self.containers.items():
                c = self.container_info[cid]
                if c.team == p.team and self._near(p, c.position, self.config.container_use_distance_m):
                    item = self.items[p.carrying]
                    p.carrying = None
                    p.items_stored += 1
                    item.holder, item.container = None, cid
                    stack.append(item.index)
                    log.info("match %d: %r stores item %d (score %s)", self.match_id,
                             p.ticket.name, item.index, self.scores())
                    self._broadcast_item(M.encode_victory_item_take_or_put(
                        p.id, item.index, False, cid), True)
                    self._check_victory()
                    return
            return
        for cid, stack in self.containers.items():
            c = self.container_info[cid]
            if c.team != p.team and stack and \
                    self._near(p, c.position, self.config.container_use_distance_m):
                # victory_items_container_core::take_item pops the LAST item: steal that one
                index = stack.pop()
                item = self.items[index]
                item.container, item.holder = None, p.id
                p.carrying = index
                log.info("match %d: %r steals item %d", self.match_id, p.ticket.name, index)
                self._broadcast_item(M.encode_victory_item_take_or_put(p.id, index, True, cid), True)
                return
        for item in self.items:
            if item.holder is None and item.container is None and \
                    self._near(p, item.position, self.config.use_distance_m):
                item.holder = p.id
                p.carrying = item.index
                log.info("match %d: %r picks up item %d", self.match_id, p.ticket.name, item.index)
                self._broadcast_item(M.encode_victory_item_take_or_put(p.id, item.index, True), False)
                return

    def _drop_item(self, p: Player) -> None:
        item = self.items[p.carrying]
        p.carrying = None
        item.holder = None
        item.position = p.position
        self._broadcast_item(M.encode_victory_item_take_or_put(
            p.id, item.index, False, M.NO_CONTAINER, p.position), False)

    def _check_victory(self) -> None:
        t1, t2 = self.scores()
        n = self.items_count
        if t1 >= n or t2 >= n:
            self.finish("all items gathered")

    def player_result(self, p: Player) -> dict:
        """What the lobby needs to reward this player (lobby.LobbyServer.award_match)."""
        finished = self.state == FINISHED
        return {"match_id": self.match_id, "team": p.team, "finished": finished,
                "won": finished and self.winner == p.team,
                "draw": finished and self.winner is None,
                "present_at_end": finished and p.present_at_end,
                "kills": p.kills, "deaths": p.deaths, "items_stored": p.items_stored,
                "play_s": round(p.play_ms / 1000.0, 1)}

    def finish(self, reason: str) -> None:
        if self.state == FINISHED:
            return
        t1, t2 = self.scores()
        self.winner = M.TEAM_1 if t1 > t2 else M.TEAM_2 if t2 > t1 else None
        self.result = f"{reason}: team_1 {t1} - team_2 {t2}, " + \
            ("draw" if self.winner is None else f"team_{self.winner + 1} wins")
        log.info("match %d finished (%s)", self.match_id, self.result)
        self._set_state(FINISHED)
        self.finished_at = self.core.now_ms
        for p in self.players:
            p.respawn_at = None
            p.present_at_end = p.connected
        for s in self.sessions():
            if s.attached:
                if self.items:
                    s.send(M.S_INITIALIZE_VICTORY_ITEMS, self._items_snapshot(score_only=True))
                s.send(M.S_MATCH_TIME_CHANGED, M.encode_u32(0))   # "MATCH TIME IS UP"

    # -------------------------------------------------------- connectivity
    def _broadcast_connected_mask(self, exclude: Optional[ClientSession] = None) -> None:
        """Unsolicited 0x8b refreshes the client's is_connected flags; the client just
        answers 0x46 (process_sync_response)."""
        mask = self.connected_mask()
        for s in self.sessions():
            if s is not exclude and s.synced:
                s.send(M.S_SYNC_RESPONSE, M.encode_sync_response(mask))
                s.sync_sent_ms = self.core.now_ms

    # --------------------------------------------------------- corrections
    def send_corrections(self) -> None:
        """0x82 per recipient: the OTHER alive players' latest input/state. The time is
        the recipient's own clock (its newest 0x43 time advanced by the server time
        since; never decreasing): for remote players the client clamps it to its current
        time and ignores anything older than its last correction (player_tick.cpp:157-176).

        The recipient's OWN player is never echoed: for the local player time_warp
        rewinds to that time and re-simulates every newer history item through the
        physics controller (replay_history, player_tick.cpp:142-219). Echoing the
        client's own reported state is therefore not a no-op; it re-runs jumps with the
        controller's present jump state and snaps the result (visible as jerky jumps)."""
        now = self.core.now_ms
        sessions = self.joined_sessions()
        if not sessions:
            return
        # Every entry is the same for all recipients: encode each alive player once per
        # tick (bytes identical to encode_server_player_input's CorrectionEntry path).
        encoded = []
        for p in self.players:
            if not p.alive or not p.spawned:
                continue
            w = p.weapons.get(p.active_slot)
            inp = p.last_input if p.has_input else _NO_INPUT
            if p.switched:
                # A remote client changes weapon only from bits 0x1000/0x2000 of the relayed
                # input (process_quick_slots_for_proxy_player; the weapon_state slot id is
                # not read). The key is down for a few frames at most, so a single relayed
                # 0x82 can miss it, and a late joiner never saw it: keep repeating the bit
                # of the active slot. inventory::action is a no-op once that slot is active.
                inp = M.PlayerInput(inp.angular_velocity, inp.angular_acceleration,
                                    inp.actions_mask | SELECT_WEAPON_BITS[p.active_slot])
            encoded.append((p, M.encode_correction_entry(
                p.id, inp,
                M.PlayerState(p.position, p.yaw, p.pitch),
                p.active_slot, w.ammo_slot if w else M.INVALID_SLOT, 0)))
        if not encoded:
            return
        per_msg = M.MAX_CORRECTIONS_PER_MESSAGE
        for s in sessions:
            me = s.player
            if me is None or not me.has_input:
                continue
            last_rx = s.conn.m_last_receive_time_in_ms
            if last_rx and now - last_rx > SILENT_PEER_MS:
                # Nothing heard for a while (the client's HUD already says "connection
                # lost"): every 0x82 is reliable, so queueing more would only pile up
                # resends until the 120 s timeout. Resume once the peer is heard again.
                continue
            roster_size = s.roster_size
            entries = [e for p, e in encoded if p is not me and p.id < roster_size]
            if not entries:
                continue
            t = (me.last_input_time + (now - me.last_input_server_ms)) & 0xFFFFFFFF
            if s.last_corr_time and u32_lt(t, s.last_corr_time):
                t = s.last_corr_time
            s.last_corr_time = t
            for i in range(0, len(entries), per_msg):
                s.send(M.S_SERVER_PLAYER_INPUT,
                       M.encode_server_player_input_raw(t, entries[i:i + per_msg]))
