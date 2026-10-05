"""Match server core: sessions, routing to matches, the client message dispatch.

MatchCore is transport-agnostic and clock-agnostic: feed it datagrams with
``datagram_received(data, addr, now_ms)`` and call ``tick(now_ms)`` every ~33 ms; it sends
through the ``sendto(data, addr)`` callback. server.py wraps it in asyncio.

Routing: the 0x40 session_id selects a lobby ticket. A ticket with a ``roster`` (every
session_id of a lobby-formed match, in roster order) puts the session into that lobby
match, created on the first connect with the whole roster (game.Match). Ticketless or
roster-less sessions share one "open" match whose roster grows as they connect (M2).

Per-client flow (game.Match has the details):
  0x40 session_id -> ticket, match, player slot      -> 0x80 (order 0, exactly once)
  0x41                                               -> 0x81, 0x92 x players_count
  0x48                                               -> 0x93 (count 0)
  0x42                                               -> 0x84 own, 0x84 others, 0x9a ...
  0x45 u32                                           -> 0x8b connected mask
  0x46                                               -> time-synced (+0x94 once attached)
  0x43 x N                                           -> movement (trusted), fire, use
  0x44                                               -> suicide
  every tick: 0x82 per recipient with the OTHER alive players (never empty, <= 5 each)
"""

from __future__ import annotations

import logging
import random
import struct
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import messages as M
from .connection import MessageTooLarge
from .game import FINISHED, Match
from .game_data import (DEFAULT_PLAYER_NAME, DEFAULT_TEAM, GameData, Ticket, default_loadout,
                        loadout_summary, ticket_from_dict)
from .model import (Addr, ClientSession, MatchConfig, Player, ProtocolGuardError,  # noqa: F401
                    _append_capped, ammo_slot_for, find_connection_request, u32_lt)

log = logging.getLogger("match")

OPEN_MATCH_KEY = ("open",)

__all__ = ["MatchCore", "MatchConfig", "Player", "ClientSession", "ProtocolGuardError"]


class MatchCore:
    def __init__(self, sendto: Callable[[bytes, Addr], None],
                 config: Optional[MatchConfig] = None,
                 game_data: Optional[GameData] = None,
                 ticket_lookup: Optional[Callable[[int], Optional[dict]]] = None,
                 rng: Optional[random.Random] = None,
                 on_event: Optional[Callable[..., None]] = None) -> None:
        self.sendto = sendto
        # on_event(kind, session_id=, match_id=): "session_connected" when a session_id is
        # bound to a player, "session_ended" when that player's session is gone
        # (disconnect / timeout; not when a reconnect replaces it), "match_finished" per
        # roster player when a lobby match is closed (with result=Match.player_result for the
        # lobby's rewards). The in-process lobby uses these to keep a player in state
        # in_match exactly that long.
        self.on_event = on_event
        self.config = config or MatchConfig()
        self.data = game_data or GameData()
        self.ticket_lookup = ticket_lookup or (lambda sid: None)
        self.rng = rng or random.Random()
        self.sessions: Dict[Addr, ClientSession] = {}
        self.matches: Dict[tuple, Match] = {}
        self.now_ms = 0
        self._scratch: Optional[Match] = None
        # optional hooks (match/server.py, match/worker.py): on_match_removed(match) after a
        # match is dropped; tick_timer(match_id, seconds) with each match's tick + 0x82 time
        self.on_match_removed: Optional[Callable[[Match], None]] = None
        self.tick_timer: Optional[Callable[[int, float], None]] = None
        self.pending_dropped = 0

    # ------------------------------------------------------- compatibility
    @property
    def players(self) -> List[Player]:
        """Roster of the first match (M2 tests and single-match tools)."""
        for m in self.matches.values():
            return m.players
        return []

    def connected_mask(self) -> int:
        for m in self.matches.values():
            return m.connected_mask()
        return 0

    def _scratch_match(self) -> Match:
        if self._scratch is None:
            self._scratch = Match(self, ("scratch",), 0, None, False)
        return self._scratch

    def _spawn(self, p: Player) -> None:
        m = self._scratch_match()
        if p.damage is None:
            m._setup_player(p)
        m.spawn(p)

    def spawn_message(self, p: Player) -> M.SpawnPlayer:
        return self._scratch_match().spawn_message(p)

    # ------------------------------------------------------------ transport
    def datagram_received(self, data: bytes, addr: Addr, now_ms: int) -> None:
        self.now_ms = now_ms
        session = self.sessions.get(addr)
        if session is None:
            if len(self.sessions) >= self.config.max_pending_sessions and sum(
                    1 for s in self.sessions.values() if not s.handshaked) \
                    >= self.config.max_pending_sessions:
                self.pending_dropped += 1          # an endpoint flood: no state for it
                return
            # udp_match_server::process_incoming_packet: one session per endpoint,
            # created on its first datagram (no transport hello).
            session = ClientSession(self, addr)
            # start the 120 s timeout from now even if this is the only datagram ever
            # received (the C++ only arms it after the first send attempt)
            session.conn.m_last_send_attempt_time_in_ms = now_ms
            self.sessions[addr] = session
            log.debug("%s:%d: new match session", addr[0], addr[1])
        if not session.handshaked and not session.recovery_checked:
            self._recover_continued_connection(session, data)
        session.conn.process_incoming_packet(
            data, lambda t, p: self._on_message(session, t, p))

    def tick(self, now_ms: int) -> None:
        """udp_match_server::tick: reap disconnected sessions, game update, flush."""
        self.now_ms = now_ms
        handshake_deadline = now_ms - 1000 * self.config.handshake_timeout_s
        for addr, s in list(self.sessions.items()):
            if not s.handshaked and s.created_ms < handshake_deadline and not s.conn.is_disconnected():
                log.info("%s:%d: no connection_request within %.0f s; dropped", addr[0], addr[1],
                         self.config.handshake_timeout_s)
                s.conn.instant_disconnect("handshake timeout")
            if s.conn.is_disconnected():
                del self.sessions[addr]
        timer = self.tick_timer
        for key, m in list(self.matches.items()):
            t0 = time.perf_counter() if timer else 0.0
            try:
                keep = m.tick(now_ms)
            except (ProtocolGuardError, MessageTooLarge) as exc:
                log.error("match %d: refusing to send: %s", m.match_id, exc)
                keep = True
            if self.config.send_corrections:
                m.send_corrections()
            if self.config.debug_tick_stall_ms:
                end = time.perf_counter() + self.config.debug_tick_stall_ms / 1000.0
                while time.perf_counter() < end:
                    pass
            if timer:
                timer(m.match_id, time.perf_counter() - t0)
            if not keep:
                self._remove_match(key, m)
        for s in list(self.sessions.values()):
            s.conn.send_queued_packets(now_ms)

    def _remove_match(self, key, m: Match) -> None:
        del self.matches[key]
        m.removed = True
        log.info("match %d removed (%s)", m.match_id, m.result or "abandoned")
        for p in m.players:
            if p.session is not None:
                p.session.player = None
                p.session.match = None
                p.session.conn.disconnect()
                p.session = None
            self.emit("match_finished", p, result=m.player_result(p) if m.fixed else None)
        if self.on_match_removed is not None:
            try:
                self.on_match_removed(m)
            except Exception:
                log.exception("match %d: on_match_removed failed", m.match_id)

    def _recover_continued_connection(self, session: ClientSession, data: bytes) -> None:
        """A new endpoint whose 0x40 has order_id != 0 is a client that re-ran
        udp_match_client::connect while its connection was still up (Play pressed again
        mid-match; the ASSERT in udp_match_connection::connect is compiled out). Its
        match_client_impl is still `handshaked` with the GAME dispatcher installed
        (match_client_impl.cpp:58-63; only disconnect()/on_disconnect() reset it), so a
        0x80 now would hit the unchecked jump table. Continue the old numbering (so our
        datagrams are accepted) and end that connection gracefully instead; see
        _on_connection_request."""
        found = find_connection_request(data)
        if found is None:
            return
        session.recovery_checked = True        # the 0x40 may follow resent records
        order_id, session_id = found
        if order_id == 0:
            return
        old = self._live_session_for(session_id)
        log.info("%s:%d: 0x40 for session %d arrives with order_id %d (client kept its old "
                 "connection state); continuing %s", session.addr[0], session.addr[1],
                 session_id, order_id,
                 f"the numbering of {old.addr[0]}:{old.addr[1]}" if old else "from that order")
        session.conn.adopt_peer_state(old.conn if old else None, order_id)
        session.continued = True

    def _live_session_for(self, session_id: int) -> Optional[ClientSession]:
        for m in self.matches.values():
            for p in m.players:
                if p.ticket.session_id == session_id and p.session is not None:
                    return p.session
        return None

    def _replace_stale_session(self, player: Player, new: ClientSession) -> None:
        """A 0x40 for a session_id already bound to a player on another endpoint (the
        client aborted its socket and reconnected) replaces the stale session. The player
        keeps its roster slot; the stale transport is dropped without a session_ended
        event, so the lobby keeps the player in state in_match."""
        old = player.session
        log.info("session %d reconnected from %s:%d: replacing stale session %s:%d of "
                 "player %d %r", player.ticket.session_id, new.addr[0], new.addr[1],
                 old.addr[0], old.addr[1], player.id, player.ticket.name)
        match = old.match
        old.player = None
        old.match = None
        player.session = None
        old.conn.on_disconnect = None
        old.conn.instant_disconnect("replaced")
        self.sessions.pop(old.addr, None)
        if match is not None:
            match.on_player_gone(player, replaced=True)

    def on_session_disconnected(self, session: ClientSession, reason: str) -> None:
        p, m = session.player, session.match
        session.player = None
        session.match = None
        if p is not None and p.session is session:
            log.info("player %d (%s) left: %s", p.id, p.ticket.name, reason)
            p.session = None
            if m is not None and not m.removed:
                m.on_player_gone(p, replaced=False)
            # a player leaving after the final whistle already has its result
            self.emit("session_ended", p, result=m.player_result(p)
                      if m is not None and m.fixed and m.state == FINISHED else None)

    def kick(self, addr: Addr) -> None:
        s = self.sessions.get(addr)
        if s:
            s.conn.disconnect()

    def emit(self, kind: str, player: Player, result: Optional[dict] = None) -> None:
        if self.on_event is None:
            return
        extra = {} if result is None else {"result": result}
        try:
            self.on_event(kind, session_id=player.ticket.session_id,
                          match_id=player.ticket.match_id, **extra)
        except Exception:
            log.exception("match event %s for session %d failed", kind, player.ticket.session_id)

    # ------------------------------------------------------------- dispatch
    def _on_message(self, s: ClientSession, mtype: int, payload: bytes) -> None:
        _append_capped(s.received_types, mtype)
        name = M.CLIENT_MESSAGE_NAMES.get(mtype, f"0x{mtype:02x}")
        if mtype != M.C_PLAYER_UPDATE:
            log.debug("%s:%d -> %s %s", s.addr[0], s.addr[1], name, payload.hex())
        if mtype == M.C_CONNECTION_REQUEST:
            handler = self._on_connection_request
        else:
            handler = {
                M.C_GET_STARTUP_INFO: lambda s, p: s.match.on_startup_info(s),
                M.C_TEAM_BASES_INITIALIZE_INFO: lambda s, p: s.match.on_team_bases_info(s),
                M.C_JOIN_MATCH: lambda s, p: s.match.on_join(s),
                M.C_PLAYER_UPDATE: self._on_player_update,
                M.C_TIME_SYNC_REQUEST: self._on_time_sync_request,
                M.C_TIME_SYNC_CONFIRMATION: lambda s, p: s.match.on_sync_confirmation(s),
                M.C_COMMIT_SUICIDE: lambda s, p: s.match.on_suicide(s),
                M.C_WORLD_SYNC_CONFIRMATION: lambda s, p: None,
            }.get(mtype)
            if handler is None:
                log.warning("%s:%d: unexpected client message %s (%d bytes)",
                            s.addr[0], s.addr[1], name, len(payload))
                return
            if not s.handshaked or s.match is None or s.player is None:
                log.warning("%s:%d: %s before connection_request, ignored",
                            s.addr[0], s.addr[1], name)
                return
        try:
            handler(s, payload)
        except struct.error as exc:
            log.warning("%s:%d: malformed %s: %s (%s)", s.addr[0], s.addr[1], name, exc, payload.hex())
        except (ProtocolGuardError, MessageTooLarge) as exc:
            log.error("%s:%d: refusing to send while handling %s: %s",
                      s.addr[0], s.addr[1], name, exc)

    def _on_time_sync_request(self, s: ClientSession, payload: bytes) -> None:
        M.Reader(payload).u32()
        s.match.on_sync_request(s)

    def _on_player_update(self, s: ClientSession, payload: bytes) -> None:
        if len(payload) != M.ClientPlayerUpdate.SIZE:
            raise struct.error(f"0x43 is {len(payload)} bytes, expected 44")
        s.match.on_player_update(s, M.ClientPlayerUpdate.decode(payload))

    # -------------------------------------------------------------- tickets
    def _resolve_ticket(self, session_id: int, quiet: bool = False) -> Optional[Ticket]:
        raw = None
        try:
            raw = self.ticket_lookup(session_id)
        except Exception as exc:
            log.warning("ticket lookup for session %d failed: %s", session_id, exc)
        if raw:
            t = ticket_from_dict(session_id, raw)
            # keep what is safe to send; one bad item must not replace the whole equipped
            # loadout with the AK-74u default
            t.slots, dropped = self.data.sanitize_loadout(t.slots)
            if dropped and not quiet:
                log.warning("session %d: ticket items dropped from the loadout: %s",
                            session_id, "; ".join(dropped))
            err = self.data.validate_loadout(t.slots)
            if err is None:
                err = self._fit_profile(t)
            if err is None:
                if not quiet:
                    log.debug("session %d: lobby ticket for %r (team %d, loadout %s)",
                              session_id, t.name, t.team, loadout_summary(t.slots))
                return t
            log.warning("session %d: ticket loadout rejected (%s); using the default loadout",
                        session_id, err)
            t.slots = default_loadout()
            return t
        if quiet or not self.config.accept_unknown_sessions:
            return None
        log.warning("session %d: no lobby ticket; using default player %r, team_1, "
                    "AK-74u + 5.45 FMJ", session_id, DEFAULT_PLAYER_NAME)
        return Ticket(session_id, DEFAULT_PLAYER_NAME, DEFAULT_TEAM, default_loadout())

    @staticmethod
    def _fit_profile(t: Ticket) -> Optional[str]:
        """0x92 has no fragmentation: type+order+body must fit 250 bytes."""
        def size() -> int:
            return 3 + len(M.encode_player_profile(
                M.PlayerProfile(t.name, t.team, t.slots, t.boosters), True))
        if size() <= 250:
            return None
        log.warning("session %d: profile too large for one message; dropping boosters",
                    t.session_id)
        t.boosters = {}
        return None if size() <= 250 else "profile exceeds 250 bytes"

    def _match_for(self, ticket: Ticket) -> Optional[Match]:
        if ticket.roster and ticket.session_id in ticket.roster:
            key = ("lobby", ticket.match_id)
            m = self.matches.get(key)
            if m is None:
                roster = []
                for i, sid in enumerate(ticket.roster):
                    t = ticket if sid == ticket.session_id else self._resolve_ticket(sid, quiet=True)
                    if t is None:
                        log.warning("match %d: no ticket for roster session %d; placeholder",
                                    ticket.match_id, sid)
                        t = Ticket(sid, f"player{i + 1}", i % 2, default_loadout(),
                                   match_id=ticket.match_id)
                    roster.append(t)
                m = Match(self, key, ticket.match_id, roster, rules=True)
                self.matches[key] = m
            return m
        m = self.matches.get(OPEN_MATCH_KEY)
        if m is None:
            m = Match(self, OPEN_MATCH_KEY, ticket.match_id, None, self.config.open_match_rules)
            self.matches[OPEN_MATCH_KEY] = m
        return m

    # ------------------------------------------------------------ handshake
    def _on_connection_request(self, s: ClientSession, payload: bytes) -> None:
        r = M.Reader(payload)
        session_id = r.u32()
        if s.handshaked:
            log.warning("%s:%d: repeated connection_request ignored", *s.addr)
            return
        ticket = self._resolve_ticket(session_id)
        player = match = None
        was_bound = False
        if ticket is not None:
            match = self._match_for(ticket)
            player = match.player_for(ticket) if match else None
            # a reconnecting session id takes back its roster slot, replacing a stale
            # session on another endpoint if the old one is still alive
            if player is not None and player.session is not None and player.session is not s:
                was_bound = True
                self._replace_stale_session(player, s)
        if player is not None and s.continued:
            # never 0x80 here (see _recover_continued_connection): initiate_disconnection
            # makes the client's on_disconnect run close_current_match(true) -> lobby,
            # whose op 39 then frees the order so Play works again
            log.info("%s:%d: session %d continued its old connection without a reset; it "
                     "cannot be handshaked again, disconnecting it (client returns to the "
                     "lobby)", s.addr[0], s.addr[1], session_id)
            s.handshaked = True
            if not was_bound:
                match.on_player_gone(player, replaced=False)
            self.emit("session_ended", player)
            s.conn.disconnect()
            return
        if player is None:
            # spec 3: any first message other than 0x80 = "connection forbidden"
            log.warning("%s:%d: session %d rejected", s.addr[0], s.addr[1], session_id)
            s.handshaked = True             # allow the one rejection message
            s.send(M.S_GAME_STATUS_CHANGED, M.encode_game_status(M.GAME_STATUS_INACTIVE))
            self._reject_later(s)
            return
        player.session = s
        s.player = player
        s.match = match
        log.info("%s:%d: session %d -> player %d %r team %d (match %d, loadout %s)", s.addr[0],
                 s.addr[1], session_id, player.id, player.ticket.name, player.team,
                 match.match_id, loadout_summary(player.ticket.slots))
        s.send(M.S_CONNECTION_SUCCESSFUL)
        match.on_connected(s)
        self.emit("session_connected", player)

    def _reject_later(self, s: ClientSession) -> None:
        # flush the rejection message, then start the graceful disconnect
        s.conn.send_queued_packets(self.now_ms)
        s.conn.disconnect()


# kept for callers of the M2 module layout
_u32_lt = u32_lt
_ammo_slot_for = ammo_slot_for
_find_connection_request = find_connection_request
