"""Messaging (chat) server for the shipped Survarium v0.100b client.

Client side: game/sources/messaging_client*.cpp, chat_handler.cpp, lobby_menu_ui.cpp
(friends UI) and flash_movies/chat.swf. Full spec: docs/match_protocol.md section 12.

Port: chat_tcp_port = 25102 (login_server/constants.h). The client learns host:port from
the HTTP server browser (`type=4`) and dials only if the host is not "x" and the port is
not 0 (messaging_client.cpp:59). Framing is the lobby's: [u8 len][payload], or
[0][u16 len][payload] when len >= 256; little-endian; str = [u8 len][bytes] (no NUL).
Text is the client's ANSI code page (setlocale(LC_ALL, "") + wcstombs_s), relayed as
opaque bytes.

C->S
  0xC3 sign_in        u32 session_id, u8 client_type (5 = account)          -> 0xCB
  0xC5 subscriptions  9 x u32, one per message_channel_enum (raw array)
  0xC1 send_text      u32 channel_id, str receiver (<32), u8 channel, str body (<=255)
  0xC4 friendship     u8 action [, u32 account_id | str name]               -> 0xCC
S->C
  0xCB signed_in      str local_name (<32)
  0xC9 text_message   u8 sender_type, u32 sender_account_id, str sender_name (<32),
                      u8 channel, str body (<255)
  0xCC friendship     u8 action, then per action (see FRIENDSHIP_* below)

The client shows its own message locally before sending (messaging_client_process_
messagess.cpp:132), so the server never echoes a message back to its sender.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import persist
import runtime

log = logging.getLogger("poc.chat")

FRAME_TIMEOUT_S = 30.0               # the rest of a frame must follow its length byte
MAX_PENDING_BYTES = 256 * 1024       # a reader this far behind is dropped (the client
                                     # reconnects within ~3 s, spec 12.1)

CHAT_TCP_PORT = 25102          # network_ports_enum::chat_tcp_port

# C->S
SEND_TEXT = 0xC1
SIGN_IN = 0xC3
FRIENDSHIP = 0xC4
SUBSCRIPTIONS = 0xC5
# S->C
TEXT_MESSAGE = 0xC9
SIGNED_IN = 0xCB
FRIENDSHIP_ANSWER = 0xCC

# messaging::message_channel_enum
CH_SERVER, CH_GENERAL, CH_SYSTEM, CH_CLAN, CH_PRIVATE = 0, 1, 2, 3, 4
CH_MATCH, CH_TEAM1, CH_TEAM2, CH_SQUAD, MAX_CHANNEL = 5, 6, 7, 8, 9

# messaging::friendship_actions_enum
ADD_FRIEND, REMOVE_FRIEND, ADD_IGNORABLE, REMOVE_IGNORABLE = 0, 1, 2, 3
FIND_PLAYERS, QUERY_FRIEND_LIST, QUERY_IGNORE_LIST, UPDATE_FRIENDS_STATUS = 4, 5, 6, 7
RESULT_OK = ord("4")           # on_packet_received: result == '4' -> re-query the list
RESULT_DENIED = ord("0")

# messaging::client_type_enum (sender_type of 0xC9; only 5 is subject to the ignore list)
MESSAGE_SERVER_CLIENT_TYPE = 4
ACCOUNT_CLIENT_TYPE = 5

NAME_MAX = 31                  # char[32] / fixed_string<32> buffers
BODY_MAX = 254                 # char body[256], r_string buffer 255, asserts len < 255
FIND_MAX = 50
NO_MATCH = 0xFFFFFFFF
LOCAL_ID_BASE = 0x40000000     # ids for accounts the lobby does not know (no lobby attached)
ONLINE_COUNT_DELAY_S = 1.0     # #pc broadcasts are coalesced to one per this interval


def frame(payload: bytes) -> bytes:
    if len(payload) < 256:
        return bytes([len(payload)]) + payload
    return b"\0" + struct.pack("<H", len(payload)) + payload


def pstr(b: bytes, limit: int) -> bytes:
    b = b[:limit]
    return bytes([len(b)]) + b


class Malformed(Exception):
    pass


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.b, self.p = data, pos

    def r(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        if self.p + size > len(self.b):
            raise Malformed(f"read {fmt} past end at {self.p}/{len(self.b)}")
        v = struct.unpack_from("<" + fmt, self.b, self.p)
        self.p += size
        return v if len(v) > 1 else v[0]

    def str(self) -> bytes:
        n = self.r("B")
        if self.p + n > len(self.b):
            raise Malformed(f"string of {n} bytes past end at {self.p}/{len(self.b)}")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v


class Roster(Protocol):
    """Read-only view of the lobby (lobby.LobbyServer implements it)."""

    def account_summary(self, account: str) -> tuple[int, str] | None: ...      # (account_id, nickname)
    def account_directory(self) -> list[tuple[str, int, str]]: ...             # (account, id, nickname)
    def match_assignment(self, account: str) -> tuple[int, int, str | None] | None: ...  # (match, team, profile)


@dataclass(eq=False)
class ChatConnection:
    writer: asyncio.StreamWriter
    peer: object
    session_id: int | None = None
    account: str | None = None          # login account name (key of the lobby's state)
    subscriptions: list[int] = field(default_factory=lambda: [0] * MAX_CHANNEL)
    closed: bool = False
    online_shown: int | None = None     # the last #pc value this client got

    def send(self, payload: bytes) -> None:
        if self.closed or self.writer.is_closing():
            return
        transport = self.writer.transport
        if transport.get_write_buffer_size() > MAX_PENDING_BYTES:
            # Broadcasts (general chat to everyone) go to every connection without
            # waiting; a peer that stopped reading must not grow its buffer forever.
            log.warning("%s: chat reader %d KiB behind; dropping the connection", self.peer,
                        transport.get_write_buffer_size() // 1024)
            self.closed = True
            transport.abort()
            return
        self.writer.write(frame(payload))


class FriendStore:
    """friends / ignore lists keyed by login account name; state/chat_state.json."""

    def __init__(self, path: Path | None):
        self.path = path
        self.doc: dict = {"friends": {}, "ignores": {}}
        persist.flush_pending(path)          # a restart in this process sees the newest state
        if path and path.is_file():
            try:
                doc = persist.load_json(path)
                self.doc = {"friends": dict(doc.get("friends", {})), "ignores": dict(doc.get("ignores", {}))}
            except (OSError, ValueError, AttributeError) as e:
                log.warning("cannot read %s (%r); starting with empty friend lists", path, e)
        # coalesced, written off the event loop (small file: keep it readable)
        self.writer = persist.JsonWriter(path, lambda: self.doc, 0.2, indent=1)

    def list(self, kind: str, account: str) -> list[str]:
        return list(self.doc[kind].get(account, []))

    def add(self, kind: str, account: str, other: str) -> bool:
        lst = self.doc[kind].setdefault(account, [])
        if other in lst:
            return False
        lst.append(other)
        self.save()
        return True

    def remove(self, kind: str, account: str, other: str) -> bool:
        lst = self.doc[kind].get(account, [])
        if other not in lst:
            return False
        lst.remove(other)
        self.save()
        return True

    def save(self) -> None:
        self.writer.save()

    def flush(self) -> None:
        self.writer.flush()


class ChatServer:
    def __init__(self, sessions: dict[int, str] | None = None, roster: Roster | None = None,
                 fallback_account: str | None = None, state_path: Path | None = None,
                 codepage: str = "cp1251"):
        self.sessions = sessions if sessions is not None else {}
        self.roster = roster
        # None: a session the login server did not issue is disconnected without an answer;
        # a name: such sessions chat as that account (--accept-unknown-sessions)
        self.fallback_account = fallback_account
        self.codepage = codepage
        self.friends = FriendStore(state_path)
        self.conns: list[ChatConnection] = []
        self._local_ids: dict[str, int] = {}
        self._count_timer: asyncio.TimerHandle | None = None

    # --- identity -----------------------------------------------------------------------
    def account_id(self, account: str) -> int:
        summary = self.roster.account_summary(account) if self.roster else None
        if summary:
            return summary[0]
        return self._local_ids.setdefault(account, LOCAL_ID_BASE + len(self._local_ids) + 1)

    def nickname(self, account: str) -> str:
        summary = self.roster.account_summary(account) if self.roster else None
        if summary:
            return summary[1]
        out = "".join(ch for ch in account if ch.isprintable()).strip() or "Stalker"
        while len(out.encode(self.codepage, errors="replace")) > NAME_MAX:
            out = out[:-1]
        return out

    def wire_name(self, name: str) -> bytes:
        return name.encode(self.codepage, errors="replace")[:NAME_MAX]

    def directory(self) -> dict[str, tuple[int, str]]:
        """Every account chat can name: lobby accounts, online ones, friend-list entries."""
        out: dict[str, tuple[int, str]] = {}
        if self.roster:
            for account, aid, nick in self.roster.account_directory():
                out[account] = (aid, nick)
        names = {c.account for c in self.conns if c.account} | set(self._local_ids)
        for kind in ("friends", "ignores"):
            for owner, lst in self.friends.doc[kind].items():
                names.add(owner)
                names.update(lst)
        for account in names:
            if account not in out:
                out[account] = (self.account_id(account), self.nickname(account))
        return out

    def account_by_id(self, account_id: int) -> str | None:
        return next((a for a, (aid, _) in self.directory().items() if aid == account_id), None)

    def online(self, account: str) -> list[ChatConnection]:
        return [c for c in self.conns if c.account == account and not c.closed]

    def match_of(self, account: str) -> tuple[int, int, str | None] | None:
        return self.roster.match_assignment(account) if self.roster else None

    # --- transport ----------------------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = ChatConnection(writer, writer.get_extra_info("peername"))
        self.conns.append(conn)
        runtime.tcp_keepalive(writer)
        log.debug("%s: chat connected", conn.peer)
        try:
            while not conn.closed:
                n = (await reader.readexactly(1))[0]
                if n == 0:
                    n = struct.unpack("<H", await asyncio.wait_for(reader.readexactly(2),
                                                                   FRAME_TIMEOUT_S))[0]
                payload = await asyncio.wait_for(reader.readexactly(n), FRAME_TIMEOUT_S)
                if not payload:
                    continue
                try:
                    self.dispatch(conn, payload)
                except (Malformed, struct.error, IndexError, ValueError, UnicodeError) as e:
                    log.warning("%s: malformed chat op 0x%02x (%s): %r", conn.peer, payload[0],
                                payload[:64].hex(), e)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, OSError) as e:
            log.debug("%s: chat disconnected (%r)", conn.peer, e)
        finally:
            conn.closed = True
            if conn in self.conns:
                self.conns.remove(conn)
            writer.close()
            if conn.account:
                log.debug("%s: %r left chat (%d online)", conn.peer, conn.account, len(self.signed_in()))
                self.online_changed()

    def drop_session(self, session_id: int) -> None:
        """The login server signed this session out: close its chat connections."""
        for c in [c for c in self.conns if c.session_id == session_id and not c.closed]:
            log.info("%s: session %d signed out; closing the chat connection", c.peer, session_id)
            c.closed = True
            c.writer.close()

    def signed_in(self) -> list[ChatConnection]:
        return [c for c in self.conns if c.account and not c.closed]

    def dispatch(self, conn: ChatConnection, p: bytes) -> None:
        op = p[0]
        if op == SIGN_IN:
            return self.on_sign_in(conn, Reader(p, 1))
        if conn.account is None:
            log.warning("%s: chat op 0x%02x before sign-in ignored", conn.peer, op)
            return
        if op == SUBSCRIPTIONS:
            return self.on_subscriptions(conn, Reader(p, 1))
        if op == SEND_TEXT:
            return self.on_send_text(conn, Reader(p, 1))
        if op == FRIENDSHIP:
            return self.on_friendship(conn, Reader(p, 1))
        log.info("%s: chat op 0x%02x ignored (%s)", conn.peer, op, p[:64].hex())

    # --- 0xC3 / 0xC5 --------------------------------------------------------------------
    def on_sign_in(self, conn: ChatConnection, rd: Reader) -> None:
        sid = rd.r("I")
        client_type = rd.r("B") if rd.p < len(rd.b) else ACCOUNT_CLIENT_TYPE
        account = self.sessions.get(sid) or self.fallback_account
        if account is None:
            # the client has no refusal message (only 0xCB ends its sign-in): close; it
            # reconnects with the session of its next login
            log.warning("%s: chat sign in with unknown session_id=%d; closed", conn.peer, sid)
            conn.closed = True
            conn.writer.close()
            return
        # A reconnect after a dead socket: drop older connections of the same session.
        for old in [c for c in self.conns if c is not conn and c.session_id == sid and c.account]:
            log.info("%s: session %d re-signed in from %s; dropping the old chat connection",
                     old.peer, sid, conn.peer)
            old.closed = True
            self.conns.remove(old)
            old.writer.close()
        conn.session_id = sid
        conn.account = account
        name = self.nickname(conn.account)
        log.info("%s: chat sign in session_id=%d type=%d account=%r as %r (%d online)", conn.peer,
                 sid, client_type, conn.account, name, len(self.signed_in()))
        conn.send(bytes([SIGNED_IN]) + pstr(self.wire_name(name), NAME_MAX))
        self.send_online_count(conn, self.online_count())
        self.online_changed()

    def on_subscriptions(self, conn: ChatConnection, rd: Reader) -> None:
        conn.subscriptions = list(rd.r(f"{MAX_CHANNEL}I"))
        log.debug("%s: %r subscriptions %s", conn.peer, conn.account, conn.subscriptions)

    # --- 0xC1 ---------------------------------------------------------------------------
    def on_send_text(self, conn: ChatConnection, rd: Reader) -> None:
        channel_id = rd.r("I")
        receiver = rd.str()
        channel = rd.r("B")
        body = rd.str()[:BODY_MAX]
        if not body.strip():
            return
        sender = conn.account
        text = body.decode(self.codepage, errors="replace")
        if channel == CH_GENERAL:
            targets = [c for c in self.signed_in() if c is not conn]
            self.relay(conn, targets, CH_GENERAL, self.nickname(sender), body)
            log.debug("chat general %r: %s (%d recipients)", sender, text, len(targets))
        elif channel == CH_PRIVATE:
            self.on_private(conn, receiver, body, text)
        elif channel == CH_MATCH:
            self.on_match_text(conn, channel_id, body, text)
        elif channel in (CH_TEAM1, CH_TEAM2, CH_SQUAD):
            self.on_team_text(conn, channel, body, text)
        else:
            log.info("chat %r: channel %d not routed (%s)", sender, channel, text)

    def on_private(self, conn: ChatConnection, receiver: bytes, body: bytes, text: str) -> None:
        want = receiver.decode(self.codepage, errors="replace").strip().casefold()
        found = None
        for account in {c.account for c in self.signed_in()}:
            if want in (account.casefold(), self.nickname(account).casefold()):
                found = account
                break
        if found is None:
            log.info("chat private %r -> %r: not online", conn.account, want)
            self.system(conn, f"{receiver.decode(self.codepage, errors='replace')} is not online.")
            return
        if conn.account in self.friends.list("ignores", found):
            log.info("chat private %r -> %r: ignored by the receiver", conn.account, found)
            return
        targets = [c for c in self.online(found) if c is not conn]
        self.relay(conn, targets, CH_PRIVATE, self.nickname(conn.account), body)
        log.debug("chat private %r -> %r: %s", conn.account, found, text)

    def on_match_text(self, conn: ChatConnection, channel_id: int, body: bytes, text: str) -> None:
        mine = self.match_of(conn.account)
        if mine is not None:
            match_id = mine[0]
            targets = [c for c in self.signed_in() if c is not conn
                       and (self.match_of(c.account) or (None,))[0] == match_id]
        elif self.roster is None and channel_id not in (0, NO_MATCH):
            # no lobby to ask: trust the subscriptions (0xC5 slot 5 = match_id)
            match_id = channel_id
            targets = [c for c in self.signed_in() if c is not conn
                       and c.subscriptions[CH_MATCH] == channel_id]
        else:
            log.info("chat match %r: not in a match (channel id %d); dropped", conn.account, channel_id)
            return
        self.relay(conn, targets, CH_MATCH, self.match_name(conn.account, mine), body)
        log.info("chat match %d %r: %s (%d recipients)", match_id, conn.account, text, len(targets))

    def on_team_text(self, conn: ChatConnection, channel: int, body: bytes, text: str) -> None:
        mine = self.match_of(conn.account)
        if mine is None:
            log.info("chat team %r: not in a match (channel %d); dropped", conn.account, channel)
            return
        match_id, team, _ = mine
        targets = [c for c in self.signed_in() if c is not conn
                   and (self.match_of(c.account) or (None, None))[:2] == (match_id, team)]
        # Delivered as team1 (6) to both teams: an incoming 7 is the client's match-making
        # status feed and 8 its stats feed (on_match_message_arrived / on_stats_message_arrived),
        # never shown as chat. In the game view the chat shows every type 5..9.
        self.relay(conn, targets, CH_TEAM1, self.match_name(conn.account, mine), body)
        log.debug("chat team %d/%d %r (channel %d): %s (%d recipients)", match_id, team,
                  conn.account, channel, text, len(targets))

    def match_name(self, account: str, mine) -> str:
        """In the game view chat_handler::add_message colours channel 5 by looking the
        sender name up in the match's profile names (network_client::get_player_team)."""
        if mine is not None and mine[2]:
            return mine[2]
        return self.nickname(account)

    def relay(self, sender: ChatConnection, targets: list[ChatConnection], channel: int,
              sender_name: str, body: bytes) -> None:
        payload = (bytes([TEXT_MESSAGE, ACCOUNT_CLIENT_TYPE]) + struct.pack("<I", self.account_id(sender.account))
                   + pstr(self.wire_name(sender_name), NAME_MAX) + bytes([channel]) + pstr(body, BODY_MAX))
        for c in targets:
            c.send(payload)

    def system(self, conn: ChatConnection, text: str) -> None:
        body = text.encode(self.codepage, errors="replace")
        conn.send(bytes([TEXT_MESSAGE, MESSAGE_SERVER_CLIENT_TYPE]) + struct.pack("<I", 0)
                  + pstr(b"System", NAME_MAX) + bytes([CH_SYSTEM]) + pstr(body, BODY_MAX))

    def server_line(self, channel: int, text: str) -> bytes:
        return (bytes([TEXT_MESSAGE, MESSAGE_SERVER_CLIENT_TYPE]) + struct.pack("<I", 0)
                + pstr(b"System", NAME_MAX) + bytes([channel])
                + pstr(text.encode(self.codepage, errors="replace"), BODY_MAX))

    def send_feed(self, account: str, channel: int, text: str) -> None:
        """A status line for the lobby menu (lobby.LobbyServer.feed): channel 7 feeds the
        match-making window, 8 the stats parser; neither is shown as chat."""
        payload = self.server_line(channel, text)
        for c in self.online(account):
            c.send(payload)

    # --- online counter (#pc) -----------------------------------------------------------
    def online_count(self) -> int:
        return len({c.account for c in self.signed_in()})

    def send_online_count(self, conn: ChatConnection, count: int) -> None:
        """`#pc:[n]` on the stats channel: lobby_menu::on_stats_message_arrived passes n to
        root.set_games_online (the status panel's online figure; wchar_t[8], so <= 7 digits)."""
        conn.online_shown = count
        conn.send(self.server_line(CH_SQUAD, f"#pc:[{min(count, 9999999)}]"))

    def online_changed(self) -> None:
        """Coalesced: one broadcast at most every ONLINE_COUNT_DELAY_S."""
        if self._count_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self.broadcast_online_count()
        self._count_timer = loop.call_later(ONLINE_COUNT_DELAY_S, self.broadcast_online_count)

    def broadcast_online_count(self) -> None:
        self._count_timer = None
        count = self.online_count()
        for c in self.signed_in():
            if c.online_shown != count:
                self.send_online_count(c, count)

    def notify_match_result(self, account: str, stats: str, lines: list[str]) -> None:
        """A finished match for one account (lobby.LobbyServer.award_match). `stats` goes out on
        the squad channel, which the client feeds to lobby_menu::on_stats_message_arrived: it
        reads the experience from `#e:[n]`, remembers it as the match's experience delta and
        re-queries money and skills; the other lines are plain system messages."""
        for c in self.online(account):
            c.send(self.server_line(CH_SQUAD, stats))
            for line in lines:
                self.system(c, line)

    # --- 0xC4 ---------------------------------------------------------------------------
    def on_friendship(self, conn: ChatConnection, rd: Reader) -> None:
        action = rd.r("B")
        me = conn.account
        if action == QUERY_FRIEND_LIST:
            rows = self.rows(self.friends.list("friends", me))
            body = b"".join(struct.pack("<I", aid) + pstr(self.wire_name(nick), NAME_MAX)
                            + bytes([bool(self.online(acc))]) for acc, aid, nick in rows)
        elif action == UPDATE_FRIENDS_STATUS:
            rows = self.rows(self.friends.list("friends", me))
            body = b"".join(struct.pack("<IB", aid, bool(self.online(acc))) for acc, aid, _ in rows)
        elif action == QUERY_IGNORE_LIST:
            rows = self.rows(self.friends.list("ignores", me))
            body = b"".join(struct.pack("<I", aid) + pstr(self.wire_name(nick), NAME_MAX)
                            for acc, aid, nick in rows)
        elif action == FIND_PLAYERS:
            want = rd.str().decode(self.codepage, errors="replace").strip().casefold()
            rows = sorted(((acc, aid, nick) for acc, (aid, nick) in self.directory().items()
                           if acc != me and want and want in nick.casefold()),
                          key=lambda r: r[2].casefold())[:FIND_MAX]
            body = b"".join(struct.pack("<I", aid) + pstr(self.wire_name(nick), NAME_MAX)
                            for acc, aid, nick in rows)
            log.info("chat %r find %r -> %d", me, want, len(rows))
        elif action in (ADD_FRIEND, REMOVE_FRIEND, ADD_IGNORABLE, REMOVE_IGNORABLE):
            other = self.account_by_id(rd.r("I"))
            kind = "friends" if action in (ADD_FRIEND, REMOVE_FRIEND) else "ignores"
            ok = other is not None and other != me and (
                self.friends.add(kind, me, other) if action in (ADD_FRIEND, ADD_IGNORABLE)
                else self.friends.remove(kind, me, other))
            log.info("chat %r friendship action %d on %r -> %s", me, action, other, "ok" if ok else "denied")
            conn.send(bytes([FRIENDSHIP_ANSWER, action, RESULT_OK if ok else RESULT_DENIED]))
            return
        else:
            log.info("%s: unknown friendship action %d ignored", conn.peer, action)
            return
        conn.send(bytes([FRIENDSHIP_ANSWER, action]) + struct.pack("<H", len(rows)) + body)

    def rows(self, accounts: list[str]) -> list[tuple[str, int, str]]:
        return [(a, self.account_id(a), self.nickname(a)) for a in accounts]
