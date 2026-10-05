"""Lobby server for the shipped Survarium v0.100b client (game/sources/lobby_client.cpp,
network_client_lobby.cpp, lobby_menu*.cpp). Stdlib only.

Framing: [u8 len][payload], or [0][u16 len][payload] when len >= 256. Little-endian.
Strings are [u8 len][bytes] (no terminator). Structs marked "raw" are memcpy'd by the
client (packet_reader::r(ptr, size)) and so include MSVC padding.

C->S  (lobby_client_message_types_enum)
  38 sign_in_info          u32 session_id                                -> 48
  33 query_client_status   u32 type            (types 0,1,3,4,5,7..11)   -> 54 type ...
                           u8 2, u32 profile_id (q_profile_contents)     -> 54 2 player_profile
                           u8 6, u8 faction_id  (q_price_items)          -> 54 6 ...
  32 set_status_ready_for_match  u32 profile_id                         -> 52 32 | 53 32 u8 faction str
  35 inventory_action      u8 0, u8 n, n x {u32 profile_id, u32 item_id, u32 item_dict_id,
                           u32 source_slot, u32 target_slot, u16 amount}  -> 52 35 | 53 35 u8 faction str
  36 shop_action           u8 0, u16 dict_id, u32 count, u8 faction, u8 premium
                                                    -> 52 36 0 u16 dict u32 id u32 count | 53 36 u8 faction str
                           (denied while the trader's reputation is below the item's level, 14.2)
  37 skills_tree_action    u8 0, u8 n, n x {u8 skill, u8 points}, u8 m, m x u8 perk
                           u8 1 (reroll)            -> 52 37 u8 sub | 53 37 u8 faction str
  39 discard_playing_order u32 match_order_id
  40 ping_server           u32 time                                      -> 55 u32 time
S->C  (lobby_server_message_types_enum)
  51 connect_to_match_server  str host(<64), u16 port, u32 match_id, u8 team
"""

from __future__ import annotations

import asyncio
import collections
import ipaddress
import itertools
import json
import logging
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import lobby_data as ld
import persist
import progression
import runtime

log = logging.getLogger("poc.lobby")

FRAME_TIMEOUT_S = 30.0       # the rest of a frame must follow its length byte within this
TICKET_MAX_AGE_S = 6 * 3600  # tickets of matches nobody reported finished are dropped after this
TICKETS_MAX = 20000

# lobby_client_message_types_enum
SET_STATUS_READY_FOR_MATCH = 32
QUERY_CLIENT_STATUS = 33
INVENTORY_ACTION = 35
SHOP_ACTION = 36
SKILLS_TREE_ACTION = 37
LOBBY_SIGN_IN_INFO = 38
DISCARD_PLAYING_ORDER = 39
PING_SERVER = 40
# lobby_server_message_types_enum
CONNECTION_SUCCESSFUL = 48
CONNECT_TO_MATCH_SERVER = 51
OPERATION_PERMITTED = 52
OPERATION_DENIED = 53
CLIENT_STATUS = 54
PING_SERVER_ANSWER = 55

# lobby::query_info_types
Q_CLIENT_STATE, Q_ENUMERATE_PROFILES, Q_PROFILE_CONTENTS, Q_ENUMERATE_INVENTORY = 0, 1, 2, 3
Q_SLOT_RESTRICTIONS, Q_ITEMS_COMPATIBILITY, Q_PRICE_ITEMS, Q_ACCOUNT_MONEY = 4, 5, 6, 7
Q_PLAYER_SKILLS, Q_PLAYER_SKILLS_TREE, Q_SERVICE_PRICES, Q_PLAYER_REPUTATIONS = 8, 9, 10, 11

# lobby::client_state_enum
SURF_LOBBY_MENU, IN_MATCH_MAKING_ORDER, IN_MATCH_MAKING, IN_MATCH = 0, 1, 2, 3
TEAM_UNDEFINED = 3          # game_team_id

FACTION_NAMES = {1: "Scavengers", 2: "Black Market", 3: "Renaissance", 4: "Border",
                 5: "Scientists", 6: "Mercenaries"}
SHOP_TRADERS = (1, 2, 3, 4)  # lobby_menu::on_shop_ui_ready asks for these; the shop lists only 1 and 2
AWARDED_MAX = 5000          # (session, match) pairs remembered so a result is paid once

MAX_PROFILES = 3            # lobby_client::m_profiles[3]
NAME_MAX = 31               # char[32] buffers (profile_name, account_nickname_)
STATUS_MSG_MAX = 127        # fixed_string<128>
HOST_MAX = 63               # char host[64] in the op 51 handler

PROFILE_STRUCT_SIZE = 0x1B8
_PROFILE_HEAD = struct.Struct("<II32s")
_BOOSTER = struct.Struct("<B3xf")
_SLOT = struct.Struct("<IIIH2x")          # inventory_item_instance / profile_slot (raw)
_PROFILE_TAIL = struct.Struct("<IB3x")


def frame(payload: bytes) -> bytes:
    if len(payload) < 256:
        return bytes([len(payload)]) + payload
    return b"\0" + struct.pack("<H", len(payload)) + payload


def wstr(s: str, limit: int = 254) -> bytes:
    b = s.encode("cp1251", errors="replace")[:limit]
    return bytes([len(b)]) + b


# ----------------------------------------------------------------------------------------
# persistent per-account state
# ----------------------------------------------------------------------------------------

class Store:
    """All accounts in one JSON document. A change marks it dirty; it is written atomically
    at most once a second, off the event loop (persist.JsonWriter), and at shutdown."""

    SAVE_DELAY_S = 1.0

    def __init__(self, path: Path | None, gd: ld.GameData, defaults: dict):
        self.path = path
        self.gd = gd
        self.defaults = defaults
        self.doc = {"next_account_id": 1, "next_item_id": 1000, "accounts": {}}
        persist.flush_pending(path)          # a restart in this process sees the newest state
        if path and path.is_file():
            try:
                self.doc = persist.load_json(path)
                log.info("lobby state loaded from %s (%d accounts)", path, len(self.doc["accounts"]))
            except (OSError, ValueError, KeyError) as e:
                log.warning("cannot read %s (%r); starting with empty state", path, e)
        self.writer = persist.JsonWriter(path, lambda: self.doc, self.SAVE_DELAY_S)

    def save(self) -> None:
        self.writer.save()

    def flush(self) -> None:
        self.writer.flush()

    def new_item(self, dict_id: int, cond: int) -> dict:
        iid = self.doc["next_item_id"]
        self.doc["next_item_id"] += 1
        return {"id": iid, "dict_id": dict_id, "cond": cond}

    def account(self, name: str) -> dict:
        acc = self.doc["accounts"].get(name)
        if acc is None:
            acc = self._create(name)
            self.doc["accounts"][name] = acc
            self.save()
            log.debug("created account %r (id %d) with %d profiles", name, acc["account_id"], len(acc["profiles"]))
        elif "stats" not in acc:                 # an account saved before progression existed
            self.migrate(acc)
        return acc

    def migrate(self, acc: dict) -> None:
        acc["stats"] = {"matches": 0, "wins": 0, "kills": 0, "deaths": 0}
        rep = acc.setdefault("reputation", {})
        for f, lv in self.gd.faction_levels.items():
            rep.setdefault(str(f), lv[0] if lv else 0)
        self.save()

    def _create(self, name: str) -> dict:
        aid = self.doc["next_account_id"]
        self.doc["next_account_id"] += 1
        nick = _clean_name(name)
        profiles = []
        for k, (suffix, loadout) in enumerate(self.gd.loadouts[:MAX_PROFILES]):
            slots = {str(slot): self.new_item(d, c) for slot, d, c in loadout if d in self.gd.items}
            pname = (nick[:NAME_MAX - len(suffix)] + suffix) if suffix else nick
            profiles.append({"profile_id": aid * 16 + k + 1, "name": pname, "slots": slots})
        storage = [self.new_item(d, c) for d, c in ld.STARTING_STORAGE if d in self.gd.items]
        return {
            "account_id": aid, "nickname": nick,
            "money": self.defaults["money"], "premium_money": self.defaults["premium_money"],
            "skill_points": self.defaults["skill_points"], "experience": 0,
            "skills": {}, "perks": [],
            "reputation": {str(f): lv[0] for f, lv in self.gd.faction_levels.items() if lv},
            "stats": {"matches": 0, "wins": 0, "kills": 0, "deaths": 0},
            "storage": storage, "profiles": profiles,
        }


def _clean_name(name: str) -> str:
    out = "".join(ch for ch in name if ch.isprintable()).strip() or "Stalker"
    while len(out.encode("cp1251", errors="replace")) > NAME_MAX:
        out = out[:-1]
    return out


# ----------------------------------------------------------------------------------------
# matchmaking
# ----------------------------------------------------------------------------------------

@dataclass
class PlayStatus:
    state: int = SURF_LOBBY_MENU
    order_id: int = 0xFFFFFFFF
    match_id: int = 0xFFFFFFFF
    team: int = TEAM_UNDEFINED
    since: float = 0.0
    message: str = ""
    profile_id: int = 0
    session_id: int = 0
    reached_match: bool = False   # the match server reported this session connected
    match_port: int = 0           # UDP port of the match's server (a match worker); 0 = default


_TICKETS: dict[str, dict] = {}


def get_match_ticket(session_id: int) -> dict | None:
    """Ticket of the player the lobby sent to the match server with this login session_id
    (the u32 the client puts in its 0x40 connect packet). For an in-process match server;
    the same data is in state/match_tickets.json."""
    return _TICKETS.get(str(session_id))


@dataclass
class QueueEntry:
    conn: "LobbyConnection"
    account: str
    order_id: int
    profile_id: int
    since: float


class Matchmaker:
    """Queue-based matchmaking with a FIXED roster per match.

    A match is formed when ``match_size`` players are queued, or, once the oldest queued
    player has waited ``delay`` seconds (the fill timeout), with everyone queued if that is
    at least ``min_players``. Teams alternate in queue order. All tickets of a match are
    written BEFORE any op 51 goes out, so the match server can build the full roster (same
    0x81 players_count and 0x92 profiles for every client) from the first connect.

    Ticket (state/match_tickets.json, keyed by decimal session_id):
      account, profile_name, team (1 = team_1, 2 = team_2; team_id is the game_team_id 0/1
      sent in op 51), match_id, issued_at, loadout [{slot, dict_id, id, condition_or_stack,
      amount}], roster (every session_id of the match, in roster order), match_size, plus
      account_id, profile_id, order_id, boosters and the raw 0x1B8 player_profile the lobby
      serves (player_profile_hex)."""

    def __init__(self, host: str, port: int, match_id: int, delay: float, tickets: Path | None,
                 match_size: int = 2, min_players: int = 1):
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            raise SystemExit(f"match host {host!r} must be a numeric IPv4 address (the client does not resolve it)")
        self.host, self.port, self.delay = host, port, delay
        self.match_ids = itertools.count(match_id)
        self.match_size = max(1, min(20, match_size))
        self.min_players = max(1, min(min_players, self.match_size))
        self.tickets_path = tickets
        self.orders = itertools.count(1)
        self.teams = itertools.cycle((0, 1))   # team_1, team_2
        self.tickets = _TICKETS
        self.queue: list[QueueEntry] = []
        # placer(match_id, {str(session_id): ticket}, done): hands a new match to a match
        # worker (match/pool.py) and calls done(udp_port) once it has the tickets, or
        # done(None). None: the match server at host:port gets everything (in-process or
        # `python -m match`, which reads the tickets file).
        self.placer = None
        # True: the tickets file is written before op 51 goes out (a separate `python -m
        # match` reads it). False: an in-process / worker match server has the tickets in
        # memory, and the (informational) file is written off the event loop.
        self.sync_tickets = True
        self.writer = persist.JsonWriter(tickets, lambda: self.tickets, 0.5)

    def issue_ticket(self, session_id: int, ticket: dict) -> None:
        self.tickets[str(session_id)] = ticket
        self.save_tickets()

    def prune_tickets(self) -> None:
        """Bounded: drop tickets older than TICKET_MAX_AGE_S, then the oldest beyond
        TICKETS_MAX (a match the match server removed drops its own at once, forget_match)."""
        cutoff = time.time() - TICKET_MAX_AGE_S
        for k in [k for k, t in self.tickets.items() if t.get("issued_at", 0) < cutoff]:
            del self.tickets[k]
        if len(self.tickets) > TICKETS_MAX:
            for k, _ in sorted(self.tickets.items(), key=lambda kv: kv[1].get("issued_at", 0)
                               )[:len(self.tickets) - TICKETS_MAX]:
                del self.tickets[k]

    def forget_match(self, match_id: int) -> None:
        """The match server removed this match: its tickets are no longer needed."""
        dead = [k for k, t in self.tickets.items() if t.get("match_id") == match_id]
        for k in dead:
            del self.tickets[k]
        if dead:
            self.writer.save()

    def save_tickets(self) -> None:
        self.prune_tickets()
        if self.sync_tickets:
            self.writer.save_now()
            self.writer.flush()
        else:
            self.writer.save()


# ----------------------------------------------------------------------------------------
# server
# ----------------------------------------------------------------------------------------

class Denied(Exception):
    pass


class LobbyServer:
    def __init__(self, gd: ld.GameData, store: Store, matchmaker: Matchmaker,
                 sessions: dict[int, str] | None = None, fallback_account: str = "Stalker",
                 service_prices: tuple[int, int, int] | None = None, match_timeout: float = 60.0,
                 serve_skills_tree: bool = True, rules: progression.Progression | None = None):
        self.gd = gd
        self.prog = rules or progression.Progression()
        # notify(account, stats_line, info_lines): the chat server shows the match result
        # (set by survarium_poc_server; None without chat)
        self.notify: Callable[[str, str, list[str]], None] | None = None
        self.dirty: set[str] = set()         # accounts whose menu data changed outside a query
        self._awarded: collections.OrderedDict = collections.OrderedDict()
        self.store = store
        self.mm = matchmaker
        self.sessions = sessions if sessions is not None else {}
        self.fallback_account = fallback_account
        self.service_prices = service_prices or gd.service_prices
        self.match_timeout = match_timeout
        self.serve_skills_tree = serve_skills_tree
        self.status: dict[str, PlayStatus] = {}
        self.live_conns: set[LobbyConnection] = set()

    # --- matchmaking --------------------------------------------------------------------
    def enqueue(self, conn: "LobbyConnection", order_id: int, profile_id: int) -> None:
        self.mm.queue.append(QueueEntry(conn, conn.account_name, order_id, profile_id, time.monotonic()))
        self.form_matches()
        try:
            asyncio.get_running_loop().call_later(self.mm.delay + 0.01, self.form_matches)
        except RuntimeError:
            pass

    def form_matches(self) -> None:
        mm = self.mm
        mm.queue = [e for e in mm.queue if not e.conn.closed and e.account in self.status
                    and self.status[e.account].state == IN_MATCH_MAKING
                    and self.status[e.account].order_id == e.order_id]
        while len(mm.queue) >= mm.match_size:
            self._form(mm.queue[:mm.match_size])
        if mm.queue and len(mm.queue) >= mm.min_players \
                and time.monotonic() - mm.queue[0].since >= mm.delay - 0.005:
            self._form(mm.queue[:mm.match_size])

    def _form(self, entries: list[QueueEntry]) -> None:
        mm = self.mm
        for e in entries:
            mm.queue.remove(e)
        entries = [e for e in entries if e.conn.find_profile(e.profile_id) is not None]
        if not entries:
            return
        match_id = next(mm.match_ids)
        roster = [e.conn.session_id or 0 for e in entries]
        log.info("match %d formed: %s", match_id,
                 ", ".join(f"{e.account!r} (session {sid})" for e, sid in zip(entries, roster)))
        now = time.monotonic()
        for e in entries:                      # every ticket first ...
            play = self.status[e.account]
            play.state, play.since, play.message = IN_MATCH, now, ""
            play.match_id, play.team = match_id, next(mm.teams)
            play.session_id = e.conn.session_id or 0
            e.conn.issue_ticket(e.conn.find_profile(e.profile_id), play, roster, save=False)
        mm.save_tickets()

        def placed(port: int | None) -> None:  # ... then op 51
            for e in entries:
                play = self.status.get(e.account)
                if play is None or play.state != IN_MATCH or play.match_id != match_id:
                    continue                   # left meanwhile (op 39 / reconnect)
                if port is None:
                    self.status[e.account] = PlayStatus(message="no match server available")
                    if not e.conn.closed:
                        e.conn.push_client_state()
                    continue
                play.match_port = port
                e.conn.send_connect_to_match(play)
            if port is None:
                log.error("match %d could not be placed on a match server", match_id)
                mm.forget_match(match_id)

        if mm.placer is None:
            placed(mm.port)
        else:
            sids = [str(e.conn.session_id or 0) for e in entries]
            mm.placer(match_id, {k: mm.tickets[k] for k in sids if k in mm.tickets}, placed)

    def on_match_event(self, kind: str, session_id: int, match_id: int = 0, result: dict | None = None,
                       **_) -> None:
        """Called by the in-process match server (MatchCore.on_event) and by the match pool.
        ``result`` (Match.player_result) comes with session_ended after the final whistle and
        with match_finished: it is paid out once per (session, match)."""
        ticket = _TICKETS.get(str(session_id))
        account = ticket.get("account") if ticket else self.sessions.get(session_id)
        if result is not None and account and kind in ("session_ended", "match_finished"):
            self.award_match(account, session_id, match_id, result)
        play = self.status.get(account) if account else None
        if play is None or play.state != IN_MATCH or play.session_id not in (0, session_id):
            return
        if match_id and match_id != play.match_id:
            return                              # an earlier match of the same session
        if kind == "session_connected":
            if not play.reached_match:
                log.debug("match server: %r (session %d) reached match %d", account, session_id,
                          play.match_id)
            play.reached_match = True
        elif kind in ("session_ended", "match_finished"):
            log.info("match server: %r (session %d) left match %d (%s); back to lobby menu",
                     account, session_id, play.match_id, kind)
            self.status[account] = PlayStatus()
            # A client that leaves a match while its lobby TCP stays up only sends its deferred
            # discard (op 39) after a reconnect (network_client::close_current_match), so it
            # keeps showing in_match and Play stays dead: push the menu state to it now.
            for conn in list(self.live_conns):
                if conn.account_name == account and not conn.closed:
                    conn.status_reply(Q_CLIENT_STATE, conn.client_state_body())
                    log.debug("%s: pushed lobby-menu state to %r after the match", conn.peer, account)
                    if account in self.dirty:       # the result was paid before the client left
                        conn.push_refresh()

    # --- progression --------------------------------------------------------------------
    def award_match(self, account: str, session_id: int, match_id: int, result: dict) -> progression.Reward | None:
        """Pay a match result: experience (levels grant skill points), money and faction
        reputation (which unlocks shop items), then tell the client (docs section 14)."""
        key = (session_id, match_id)
        if key in self._awarded:
            return None
        self._awarded[key] = True
        while len(self._awarded) > AWARDED_MAX:
            self._awarded.popitem(last=False)
        acc = self.store.doc["accounts"].get(account)
        if acc is None:
            return None
        self.store.account(account)                      # migrates an account of an older version
        reward = self.prog.reward(result)
        if not reward:
            log.info("match %d: %r earns nothing (%s)", match_id, account, reward.reason)
            return reward
        before_rep = {int(f): v for f, v in acc["reputation"].items()}
        level_before = self.prog.level_for(acc["experience"])
        acc["experience"] += reward.experience
        acc["money"] += reward.money
        level = self.prog.level_for(acc["experience"])
        gained = (level - level_before) * self.prog.skill_points_per_level
        acc["skill_points"] += gained
        for f, points in reward.reputation.items():
            top = max(self.gd.faction_levels.get(f, ()) or (0,))
            acc["reputation"][str(f)] = min(acc["reputation"].get(str(f), 0) + points, top, 0xFFFF)
        stats = acc["stats"]
        stats["matches"] += 1
        stats["wins"] += bool(result.get("won"))
        stats["kills"] += int(result.get("kills", 0))
        stats["deaths"] += int(result.get("deaths", 0))
        self.store.save()
        log.info("match %d: %r %s: +%d exp (level %d), +%d money, reputation %s, +%d skill points",
                 match_id, account, reward.reason, reward.experience, level, reward.money,
                 reward.reputation, gained)
        self.dirty.add(account)
        self._announce(account, acc, match_id, reward, before_rep, level_before, level, gained)
        for conn in list(self.live_conns):
            play = self.status.get(account)
            if conn.account_name == account and not conn.closed and (play is None or play.state == SURF_LOBBY_MENU):
                conn.push_refresh()
        return reward

    def _announce(self, account: str, acc: dict, match_id: int, reward: progression.Reward,
                  before_rep: dict[int, int], level_before: int, level: int, gained: int) -> None:
        """The stats line the client parses (lobby_menu::on_stats_message_arrived: `Player [ nick ]`
        and `#e:[experience]`) plus readable chat lines about reputation, levels and unlocks."""
        if self.notify is None:
            return
        lines = []
        rep = ", ".join(f"{FACTION_NAMES.get(f, f)} +{v} ({acc['reputation'].get(str(f), 0)})"
                        for f, v in sorted(reward.reputation.items()) if f in (1, 2))
        if rep:
            lines.append(f"Reputation: {rep}")
        if level > level_before:
            lines.append(f"Level {level} reached (+{gained} skill points)")
        for dict_id, trader in self.newly_unlocked(before_rep, {int(f): v for f, v in acc["reputation"].items()}):
            lines.append(f"Unlocked at {FACTION_NAMES.get(trader, trader)}: {self.gd.item_label(dict_id)}")
        stats = (f"Player [ {acc['nickname']} ] match {match_id}: {reward.reason}, "
                 f"+{reward.experience} exp, +{reward.money} money #e:[{reward.experience}]")
        try:
            self.notify(account, stats, lines)
        except Exception:  # noqa: BLE001 - chat is optional
            log.exception("match result notification for %r failed", account)

    def newly_unlocked(self, before: dict[int, int], after: dict[int, int]) -> list[tuple[int, int]]:
        out = []
        for trader, rows in sorted(self.gd.prices.items()):
            for dict_id, _cost, lvl in rows:
                if lvl > 0 and not self.gd.is_unlocked(trader, lvl, before.get(trader, 0))                         and self.gd.is_unlocked(trader, lvl, after.get(trader, 0)):
                    out.append((dict_id, trader))
        return out

    # --- read-only accessors (chat.py) -------------------------------------------------
    def account_summary(self, account: str) -> tuple[int, str] | None:
        """(account_id, nickname) of an existing account; never creates one."""
        acc = self.store.doc["accounts"].get(account)
        return (acc["account_id"], acc["nickname"]) if acc else None

    def account_directory(self) -> list[tuple[str, int, str]]:
        return [(name, acc["account_id"], acc["nickname"]) for name, acc in self.store.doc["accounts"].items()]

    def match_assignment(self, account: str) -> tuple[int, int, str | None] | None:
        """(match_id, team 0/1, profile name in that match) while the account is in a match."""
        play = self.status.get(account)
        if play is None or play.state != IN_MATCH:
            return None
        ticket = _TICKETS.get(str(play.session_id))
        profile = ticket.get("profile_name") if ticket and ticket.get("match_id") == play.match_id else None
        return play.match_id, play.team, profile

    def forget_match(self, match_id: int) -> None:
        """The match server dropped a match (MatchCore.on_match_removed / match pool)."""
        self.mm.forget_match(match_id)

    def flush(self) -> None:
        self.store.flush()
        self.mm.writer.flush()

    def summary(self) -> dict:
        return {"lobby": sum(1 for c in self.live_conns if c.account_name),
                "queued": len(self.mm.queue),
                "in_match": sum(1 for p in self.status.values() if p.state == IN_MATCH)}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = LobbyConnection(self, writer)
        self.live_conns.add(conn)
        runtime.tcp_keepalive(writer)
        log.debug("%s: lobby connected", conn.peer)
        try:
            while True:
                n = (await reader.readexactly(1))[0]
                # a client may idle in the menu for hours, but once a frame has started
                # the rest must follow promptly (half-open or trickling peers are dropped)
                if n == 0:
                    n = struct.unpack("<H", await asyncio.wait_for(reader.readexactly(2),
                                                                   FRAME_TIMEOUT_S))[0]
                payload = await asyncio.wait_for(reader.readexactly(n), FRAME_TIMEOUT_S)
                if payload:
                    try:
                        conn.dispatch(payload)
                    except (struct.error, IndexError) as e:
                        log.warning("%s: malformed op %d (%s): %r", conn.peer, payload[0], payload.hex(), e)
                    await writer.drain()      # a client that does not read stalls only itself
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, OSError) as e:
            log.debug("%s: lobby disconnected (%r)", conn.peer, e)
        finally:
            conn.closed = True
            self.live_conns.discard(conn)
            self.mm.queue = [e for e in self.mm.queue if e.conn is not conn]
            writer.close()
            self._forget_idle_status(conn.account_name)

    def _forget_idle_status(self, account: str | None) -> None:
        """Bounded: no PlayStatus is kept for an account that is offline in the menu."""
        if not account:
            return
        play = self.status.get(account)
        if play is not None and play.state == SURF_LOBBY_MENU and not play.message \
                and not any(c.account_name == account for c in self.live_conns):
            del self.status[account]


class LobbyConnection:
    def __init__(self, server: LobbyServer, writer: asyncio.StreamWriter):
        self.srv = server
        self.gd = server.gd
        self.store = server.store
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.session_id: int | None = None
        self.account_name: str | None = None
        self.closed = False

    # --- plumbing -----------------------------------------------------------------------
    @property
    def acc(self) -> dict:
        return self.store.account(self.account_name)

    @property
    def play(self) -> PlayStatus:
        return self.srv.status.setdefault(self.account_name, PlayStatus())

    def send(self, payload: bytes) -> None:
        if not self.closed:
            self.writer.write(frame(payload))

    def status_reply(self, kind: int, body: bytes) -> None:
        self.send(bytes([CLIENT_STATUS, kind]) + body)

    def permit(self, op: int, extra: bytes = b"") -> None:
        self.send(bytes([OPERATION_PERMITTED, op]) + extra)

    def deny(self, op: int, why: str, faction_id: int = 0) -> None:
        # the client reads u8 op, u8 faction_id, then the description (retail on_lobby_packet_received)
        log.debug("%s: op %d denied: %s", self.peer, op, why)
        self.send(bytes([OPERATION_DENIED, op, faction_id]) + wstr(why, 254))

    def dispatch(self, p: bytes) -> None:
        op = p[0]
        if op == LOBBY_SIGN_IN_INFO:
            return self.on_sign_in(p)
        if op == PING_SERVER:
            return self.send(bytes([PING_SERVER_ANSWER]) + p[1:5])
        if self.account_name is None:
            log.warning("%s: op %d before sign-in ignored", self.peer, op)
            return
        handler = {
            QUERY_CLIENT_STATUS: self.on_query,
            SET_STATUS_READY_FOR_MATCH: self.on_ready_for_match,
            INVENTORY_ACTION: self.on_inventory_action,
            SHOP_ACTION: self.on_shop_action,
            SKILLS_TREE_ACTION: self.on_skills_action,
            DISCARD_PLAYING_ORDER: self.on_discard_order,
        }.get(op)
        if handler is None:
            log.info("%s: lobby op %d ignored (%s)", self.peer, op, p.hex())
            return
        handler(p)

    # --- 38 -----------------------------------------------------------------------------
    def on_sign_in(self, p: bytes) -> None:
        sid = struct.unpack_from("<I", p, 1)[0]
        self.session_id = sid
        self.account_name = self.srv.sessions.get(sid) or self.srv.fallback_account
        acc = self.acc
        play = self.play
        if play.state == IN_MATCH:
            # The client drops the lobby TCP when it reaches the match server and then
            # reconnects while still playing (or loading). Answering state 0 here would
            # make lobby_menu::on_client_status_received switch_to_lobby() mid-match, so
            # it keeps state 3 with the op 51 values until the match server reports the
            # session gone (on_match_event), the client discards the order (op 39), or
            # the match was never reached (match_timeout, client_state_body).
            log.info("%s: %r signs in again while in match %d (order %d, team %d); "
                     "keeping state in_match", self.peer, self.account_name, play.match_id,
                     play.order_id, play.team)
        elif play.state != SURF_LOBBY_MENU:   # a queue entry died with the old connection
            log.debug("%s: %r returns from state %d", self.peer, self.account_name, play.state)
            self.srv.status[self.account_name] = PlayStatus()
        log.info("%s: lobby sign in session_id=%d account=%r (account_id %d)",
                 self.peer, sid, self.account_name, acc["account_id"])
        self.send(bytes([CONNECTION_SUCCESSFUL]))

    # --- 33 -----------------------------------------------------------------------------
    def on_query(self, p: bytes) -> None:
        if len(p) == 3 and p[1] == Q_PRICE_ITEMS:          # query_prices
            kind, arg = Q_PRICE_ITEMS, p[2]
        elif len(p) == 6 and p[1] == Q_PROFILE_CONTENTS:   # query_profile_contents
            kind, arg = Q_PROFILE_CONTENTS, struct.unpack_from("<I", p, 2)[0]
        else:                                              # query_client_status
            kind, arg = struct.unpack_from("<I", p, 1)[0], None
        body = self.status_body(kind, arg)
        log.debug("%s: status query type=%d%s %s", self.peer, kind, "" if arg is None else f"({arg})",
                  "answered" if body is not None else "ignored")
        if body is not None:
            self.status_reply(kind, body)
        if kind == Q_CLIENT_STATE and self.account_name in self.srv.dirty and self.play.state == SURF_LOBBY_MENU:
            self.srv.dirty.discard(self.account_name)
            self.push_refresh()               # a match result arrived while the client was away

    def push_refresh(self) -> None:
        """Unsolicited answers for the data a match result changes. The client re-reads money
        and experience only after it sees the stats chat line while connected, reputation
        and the per-trader price lists (items unlock) never, so the server sends them."""
        for kind, arg in ((Q_ACCOUNT_MONEY, None), (Q_PLAYER_SKILLS, None), (Q_PLAYER_REPUTATIONS, None),
                          *((Q_PRICE_ITEMS, t) for t in SHOP_TRADERS)):
            self.status_reply(kind, self.status_body(kind, arg))

    def status_body(self, kind: int, arg: int | None) -> bytes | None:
        acc = self.acc
        if kind == Q_CLIENT_STATE:
            return self.client_state_body()
        if kind == Q_ENUMERATE_PROFILES:
            profiles = acc["profiles"][:MAX_PROFILES]
            return bytes([len(profiles)]) + b"".join(
                struct.pack("<I", pr["profile_id"]) + wstr(pr["name"], NAME_MAX) for pr in profiles)
        if kind == Q_PROFILE_CONTENTS:
            pr = self.find_profile(arg)
            return None if pr is None else self.profile_struct(pr)
        if kind == Q_ENUMERATE_INVENTORY:
            items = acc["storage"]
            return struct.pack("<I", len(items)) + b"".join(self.item_struct(it) for it in items)
        if kind == Q_SLOT_RESTRICTIONS:
            rows = self.gd.slot_restrictions()
            return struct.pack("<I", len(rows)) + b"".join(struct.pack("<BB", s, c) for s, c in rows)
        if kind == Q_ITEMS_COMPATIBILITY:
            rows = self.gd.compatibilities()
            return struct.pack("<I", len(rows)) + b"".join(struct.pack("<HH", a, b) for a, b in rows)
        if kind == Q_PRICE_ITEMS:
            faction = arg if arg is not None and arg < 16 else 0
            rows = self.price_rows(faction)
            return bytes([faction]) + struct.pack("<H", len(rows)) + b"".join(
                struct.pack("<HHBx", d, min(c, 0xFFFF), lvl) for d, c, lvl in rows)
        if kind == Q_ACCOUNT_MONEY:
            return struct.pack("<IIB", acc["money"], acc["premium_money"], min(acc["skill_points"], 255)) \
                + wstr(acc["nickname"], NAME_MAX)
        if kind == Q_PLAYER_SKILLS:
            total = acc["experience"]
            prev, nxt = self.srv.prog.bounds(total)
            skills = sorted((int(k), v) for k, v in acc["skills"].items() if v)
            perks = acc["perks"]
            return struct.pack("<III", total, nxt, prev) \
                + bytes([len(skills)]) + b"".join(struct.pack("<BB", s, v) for s, v in skills) \
                + bytes([len(perks)]) + bytes(perks)
        if kind == Q_PLAYER_SKILLS_TREE:
            return self.gd.skills_tree_blob if self.srv.serve_skills_tree else None
        if kind == Q_SERVICE_PRICES:
            return struct.pack("<III", *self.srv.service_prices)
        if kind == Q_PLAYER_REPUTATIONS:
            reps = sorted((int(f), self.reported_reputation(int(f), v)) for f, v in acc["reputation"].items())
            return bytes([len(reps)]) + b"".join(struct.pack("<BxH", f, min(v, 0xFFFF)) for f, v in reps)
        return None

    def price_rows(self, trader: int) -> list[tuple[int, int, int]]:
        """(dict_id, cost, reputation_level) of one trader for THIS account. An item the account
        has earned goes out as level 0 (the shop's lock test is `unlocked[trader] > level` and
        the client never unlocks more than level 0, see reported_reputation); one it has not
        keeps its real level and shows with the lock icon."""
        points = self.acc["reputation"].get(str(trader), 0)
        return [(d, c, 0 if self.gd.is_unlocked(trader, lvl, points) else lvl)
                for d, c, lvl in self.gd.prices.get(trader, [])]

    def reported_reputation(self, faction: int, points: int) -> int:
        """Reputation as sent in query 11. The retail client forwards (level, points, <unset>)
        to the shop as setup_player_progress(faction, unlocked_level, progress), so a faction at
        level k >= 1 would set `unlocked[k] = points` and open EVERY level of trader k at once.
        Points are therefore reported below the faction's second level; the real amount stays
        on the server and decides what the price lists and shop actions allow."""
        values = self.gd.faction_levels.get(faction, ())
        return min(points, values[1] - 1) if len(values) > 1 else points

    def client_state_body(self) -> bytes:
        play = self.play
        if play.state == IN_MATCH and not play.reached_match \
                and time.monotonic() - play.since > self.srv.match_timeout:
            # long after op 51 and the match server never reported this session (or there
            # is no in-process match server to report it): the client never reached the
            # match. Unlock Play.
            log.info("%s: match %d not reached after %.0fs, back to lobby menu",
                     self.peer, play.match_id, self.srv.match_timeout)
            play = self.srv.status[self.account_name] = PlayStatus(message="match server unreachable")
        body = bytes([play.state])
        if play.state in (IN_MATCH_MAKING_ORDER, IN_MATCH_MAKING, IN_MATCH):
            body += struct.pack("<IIB", play.order_id, play.match_id, play.team)
        if play.message:
            body += wstr(play.message, STATUS_MSG_MAX)
        return body

    def push_client_state(self) -> None:
        self.status_reply(Q_CLIENT_STATE, self.client_state_body())

    # --- structs ------------------------------------------------------------------------
    def item_struct(self, it: dict) -> bytes:
        cond = it["cond"]
        stack = it["dict_id"] in self.gd.items and self.gd.items[it["dict_id"]].is_stack
        return _SLOT.pack(cond, cond if stack else 0, it["id"], it["dict_id"])

    def slot_amount(self, it: dict, slot: int) -> int:
        # inventory::setup_from_profile loads min(condition_or_stack, amount_in_inventory)
        # for ammo and stackable quick-slot items; weapons/armour use condition only.
        stack = self.gd.items[it["dict_id"]].is_stack if it["dict_id"] in self.gd.items else False
        return it["cond"] if (stack or slot in ld.AMMO_SLOTS) else 0

    def slot_struct(self, it: dict | None, slot: int) -> bytes:
        if not it:
            return _SLOT.pack(0, 0, 0, 0)
        return _SLOT.pack(it["cond"], self.slot_amount(it, slot), it["id"], it["dict_id"])

    def profile_struct(self, pr: dict) -> bytes:
        acc = self.acc
        name = pr["name"].encode("cp1251", errors="replace")[:NAME_MAX]
        out = bytearray(_PROFILE_HEAD.pack(acc["account_id"], pr["profile_id"], name))
        boosters = self.boosters()
        for i in range(11):   # boosters[i] carries booster id i+1
            bid = i + 1
            out += _BOOSTER.pack(bid, boosters[bid]) if bid in boosters else _BOOSTER.pack(0, 0.0)
        for slot in range(ld.MAX_SLOTS):
            out += self.slot_struct(pr["slots"].get(str(slot)), slot)
        out += _PROFILE_TAIL.pack(TEAM_UNDEFINED, 0)
        assert len(out) == PROFILE_STRUCT_SIZE
        return bytes(out)

    def boosters(self) -> dict[int, float]:
        return self.gd.boosters_for({int(k): v for k, v in self.acc["skills"].items()})

    def find_profile(self, profile_id: int | None) -> dict | None:
        return next((pr for pr in self.acc["profiles"] if pr["profile_id"] == profile_id), None)

    # --- 32 / 39: play ------------------------------------------------------------------
    def on_ready_for_match(self, p: bytes) -> None:
        profile_id = struct.unpack_from("<I", p, 1)[0]
        pr = self.find_profile(profile_id)
        play = self.play
        if pr is None:
            return self.deny(SET_STATUS_READY_FOR_MATCH, "unknown profile")
        if play.state != SURF_LOBBY_MENU:
            return self.deny(SET_STATUS_READY_FOR_MATCH, "already queued")
        if not pr["slots"].get(str(ld.WEAPON1)) and not pr["slots"].get(str(ld.WEAPON2)):
            return self.deny(SET_STATUS_READY_FOR_MATCH, "equip a weapon first")
        mm = self.srv.mm
        order_id = next(mm.orders)
        self.srv.status[self.account_name] = PlayStatus(
            IN_MATCH_MAKING, order_id, 0xFFFFFFFF, TEAM_UNDEFINED, time.monotonic(), "searching",
            profile_id, self.session_id or 0)
        log.debug("%s: %r ready with profile %d -> order %d, queue %d/%d", self.peer,
                  self.account_name, profile_id, order_id, len(mm.queue) + 1, mm.match_size)
        # lobby_menu: permitted 32 -> poll status in 1 s; status in_match_making -> show the
        # match-making window and keep polling every second.
        self.permit(SET_STATUS_READY_FOR_MATCH)
        self.push_client_state()
        self.srv.enqueue(self, order_id, profile_id)

    def send_connect_to_match(self, play: PlayStatus) -> None:
        mm = self.srv.mm
        port = play.match_port or mm.port
        payload = bytes([CONNECT_TO_MATCH_SERVER]) + wstr(mm.host, HOST_MAX) \
            + struct.pack("<HIB", port, play.match_id, play.team)
        log.debug("%s: connect_to_match_server %s:%d match %d team %d (%s)",
                  self.peer, mm.host, port, play.match_id, play.team, payload.hex())
        self.send(payload)

    def issue_ticket(self, pr: dict, play: PlayStatus, roster: list[int] | None = None,
                     save: bool = True) -> None:
        acc = self.acc
        loadout = []
        for key, it in sorted(pr["slots"].items(), key=lambda kv: int(kv[0])):
            slot = int(key)
            loadout.append({"slot": slot, "dict_id": it["dict_id"], "id": it["id"],
                            "condition_or_stack": min(it["cond"], 0xFFFF),   # u16 on the match wire
                            "amount": self.slot_amount(it, slot)})
        roster = roster if roster is not None else [self.session_id or 0]
        ticket = {
            "account": self.account_name, "profile_name": pr["name"],
            "team": play.team + 1, "team_id": play.team, "match_id": play.match_id,
            "roster": roster, "match_size": len(roster),
            "loadout": loadout, "issued_at": int(time.time()),
            "account_id": acc["account_id"], "profile_id": pr["profile_id"], "order_id": play.order_id,
            "boosters": {str(k): v for k, v in sorted(self.boosters().items())},
            "player_profile_hex": self.profile_struct(pr).hex(),
        }
        if save:
            self.srv.mm.issue_ticket(self.session_id, ticket)
        else:
            self.srv.mm.tickets[str(self.session_id)] = ticket

    def on_discard_order(self, p: bytes) -> None:
        order_id = struct.unpack_from("<I", p, 1)[0]
        play = self.play
        log.debug("%s: discard_playing_order %d (state %d, order %d)", self.peer, order_id, play.state, play.order_id)
        if play.state != SURF_LOBBY_MENU:
            self.srv.status[self.account_name] = PlayStatus()
            self.push_client_state()

    # --- 35: inventory ------------------------------------------------------------------
    def on_inventory_action(self, p: bytes) -> None:
        sub, n = p[1], p[2]
        if sub != 0:
            return self.deny(INVENTORY_ACTION, f"unknown inventory action {sub}")
        moves = [struct.unpack_from("<IIIIIH", p, 3 + 22 * i) for i in range(n)]
        snapshot = json.dumps(self.acc)
        try:
            if self.play.state != SURF_LOBBY_MENU:
                raise Denied("cannot change equipment while queued")
            for move in moves:
                self.relocate(*move)
            for profile_id in {m[0] for m in moves}:
                self.eject_incompatible_ammo(self.find_profile(profile_id))
        except Denied as e:
            self.store.doc["accounts"][self.account_name] = json.loads(snapshot)
            self.deny(INVENTORY_ACTION, str(e))
        else:
            self.store.save()
            self.permit(INVENTORY_ACTION)   # -> client re-queries the selected profile
        # the client never re-queries storage after a move; push it (it then re-enumerates
        # profiles and their contents, the same path as the initial load)
        self.status_reply(Q_ENUMERATE_INVENTORY, self.status_body(Q_ENUMERATE_INVENTORY, None))

    def relocate(self, profile_id: int, item_id: int, dict_id: int, src: int, dst: int, amount: int) -> None:
        log.debug("%s: relocate item %d (dict %d) profile %d: %d -> %d x%d",
                  self.peer, item_id, dict_id, profile_id, src, dst, amount)
        acc = self.acc
        pr = self.find_profile(profile_id)
        if pr is None:
            raise Denied("unknown profile")
        valid = set(range(ld.MAX_SLOTS)) | {ld.STORAGE_SLOT}
        if src not in valid or dst not in valid:
            raise Denied("invalid slot")
        if src == dst:
            return
        slots, storage = pr["slots"], acc["storage"]
        if src == ld.STORAGE_SLOT:
            item = next((it for it in storage if it["id"] == item_id), None)
        else:
            item = slots.get(str(src))
            if item and item["id"] != item_id:
                item = None
        if item is None:
            raise Denied("item not found")
        if not self.gd.slot_accepts(dst, item["dict_id"]):
            raise Denied("item does not fit that slot")
        if dst in ld.AMMO_SLOTS:
            weapon = slots.get(str(ld.AMMO_SLOTS[dst]))
            if weapon and not self.gd.compatible(weapon["dict_id"], item["dict_id"]):
                raise Denied("ammo does not fit the weapon")
        stack = self.gd.items[item["dict_id"]].is_stack
        moving = item
        if stack and 0 < amount < item["cond"]:     # split the stack
            item["cond"] -= amount
            moving = self.store.new_item(item["dict_id"], amount)
        else:                                       # take the whole item
            if src == ld.STORAGE_SLOT:
                storage.remove(item)
            else:
                del slots[str(src)]
        if dst == ld.STORAGE_SLOT:
            self.to_storage(moving)
            return
        occupant = slots.get(str(dst))
        if occupant and stack and occupant["dict_id"] == moving["dict_id"]:
            occupant["cond"] += moving["cond"]
            return
        slots[str(dst)] = moving
        if occupant:
            if src != ld.STORAGE_SLOT and self.gd.slot_accepts(src, occupant["dict_id"]) \
                    and str(src) not in slots:
                slots[str(src)] = occupant          # swap
            else:
                self.to_storage(occupant)

    def eject_incompatible_ammo(self, pr: dict | None) -> None:
        """A new weapon may not fit the ammo its slots still hold (the client checks that only
        when ammo is moved): the unfitting ammo goes back to storage so no ticket carries it."""
        if pr is None:
            return
        slots = pr["slots"]
        for ammo_slot, weapon_slot in ld.AMMO_SLOTS.items():
            ammo, weapon = slots.get(str(ammo_slot)), slots.get(str(weapon_slot))
            if ammo and weapon and not self.gd.compatible(weapon["dict_id"], ammo["dict_id"]):
                del slots[str(ammo_slot)]
                self.to_storage(ammo)

    def to_storage(self, item: dict) -> None:
        storage = self.acc["storage"]
        if self.gd.items[item["dict_id"]].is_stack:
            same = next((it for it in storage if it["dict_id"] == item["dict_id"]), None)
            if same:
                same["cond"] += item["cond"]
                return
        storage.append(item)

    # --- 36: shop -----------------------------------------------------------------------
    def on_shop_action(self, p: bytes) -> None:
        sub = p[1]
        if sub != 0:
            return self.deny(SHOP_ACTION, f"unknown shop action {sub}")
        dict_id, count, faction, premium = struct.unpack_from("<HIBB", p, 2)
        log.debug("%s: buy dict %d x%d faction %d premium %d", self.peer, dict_id, count, faction, premium)
        item = self.gd.items.get(dict_id)
        offer = self.find_offer(dict_id, faction) if item is not None else None
        if offer is None or isinstance(offer, str):
            return self.deny(SHOP_ACTION, offer or "this trader does not sell that item")
        price = offer[1]
        if not 1 <= count <= (10000 if item.is_stack else 20):
            return self.deny(SHOP_ACTION, "invalid amount")
        acc = self.acc
        purse = "premium_money" if premium else "money"
        total = price * count
        if acc[purse] < total:
            return self.deny(SHOP_ACTION, "not enough money")
        acc[purse] -= total
        bought = []
        if item.is_stack:
            same = next((it for it in acc["storage"] if it["dict_id"] == dict_id), None)
            if same is None:
                same = self.store.new_item(dict_id, 0)
                acc["storage"].append(same)
            same["cond"] += count
            bought.append((same["id"], count))      # client adds this to the stack with that id
        else:
            for _ in range(count):
                it = self.store.new_item(dict_id, 100)
                acc["storage"].append(it)
                bought.append((it["id"], 100))
        self.store.save()
        # network_client::process_shop_action: [u8 0][u16 dict][u32 id][u32 condition_or_stack]
        # -> push into the inventory list, refresh it, re-query money. One message per item.
        for iid, cond in bought:
            self.permit(SHOP_ACTION, struct.pack("<BHII", 0, dict_id, iid, cond))

    def find_offer(self, dict_id: int, faction: int) -> tuple[int, int, int] | str | None:
        """The (trader, cost, level) this account may buy the item at: the asked trader first,
        then the cheapest other one. A string is the denial text when every trader selling it
        wants more reputation than the account has; None when nobody sells it."""
        offers = self.gd.offers(dict_id)
        offers.sort(key=lambda o: o[0] != faction)           # stable: the asked trader first
        locked = None
        for trader, cost, lvl in offers:
            have = self.acc["reputation"].get(str(trader), 0)
            if self.gd.is_unlocked(trader, lvl, have):
                return trader, cost, lvl
            if locked is None:
                locked = (trader, lvl, have)
        if locked is None:
            return None
        trader, lvl, have = locked
        return (f"{self.gd.item_label(dict_id)} is locked: {FACTION_NAMES.get(trader, trader)} "
                f"reputation {self.gd.reputation_threshold(trader, lvl)} needed, you have {have}")

    # --- 37: skills ---------------------------------------------------------------------
    def on_skills_action(self, p: bytes) -> None:
        sub = p[1]
        acc = self.acc
        if sub == 1:   # reroll_player_skills
            cost = self.srv.service_prices[0]
            if acc["money"] < cost:
                return self.deny(SKILLS_TREE_ACTION, "not enough money to reset skills")
            acc["money"] -= cost
            acc["skills"], acc["perks"] = {}, []
            self.store.save()
            return self.permit(SKILLS_TREE_ACTION, b"\x01")   # client re-queries money + skills
        if sub != 0:
            return self.deny(SKILLS_TREE_ACTION, f"unknown skills action {sub}")
        n = p[2]
        skills = {}
        for i in range(n):
            sid, pts = p[3 + 2 * i], p[4 + 2 * i]
            skills[sid] = skills.get(sid, 0) + pts
        off = 3 + 2 * n
        m = p[off]
        perks = list(p[off + 1: off + 1 + m])
        if len(perks) != m:
            return self.deny(SKILLS_TREE_ACTION, "truncated request")
        for sid, pts in skills.items():
            if not self.gd.skill_levels(sid) or pts > self.gd.skill_levels(sid):
                return self.deny(SKILLS_TREE_ACTION, f"invalid points for skill {sid}")
        if sum(skills.values()) > acc["skill_points"]:
            return self.deny(SKILLS_TREE_ACTION, "not enough skill points")
        for perk in perks:
            where = self.gd.perk_level(perk)
            if where is None or skills.get(where[0], 0) < where[1]:
                return self.deny(SKILLS_TREE_ACTION, f"perk {perk} is locked")
        acc["skills"] = {str(k): v for k, v in skills.items() if v}
        acc["perks"] = sorted(set(perks))
        self.store.save()
        self.permit(SKILLS_TREE_ACTION, b"\x00")   # client re-queries skills
