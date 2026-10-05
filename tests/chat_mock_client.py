"""Mock of the shipped messaging client (game/sources/messaging_client*.cpp): sends the exact
bytes messaging_client builds and parses the server's packets exactly the way
sign_in_on_packet_received / on_packet_received / process_incoming_text_message and the
read_* helpers do, then reacts like the client (subscriptions + list queries after sign-in,
re-query after a '4' result). Text goes through the same wide -> ANSI conversion
(wcstombs_s with the system code page; cp1251 here).

Anything the real client would choke on (read past the end, a string overflowing its
buffer, an unknown message id) is recorded in `faults`.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from mock_client import Reader, tcp_frame  # noqa: E402

CODEPAGE = "cp1251"
# messaging::message_channel_enum
GENERAL, SYSTEM, CLAN, PRIVATE, MATCH, TEAM1, TEAM2, SQUAD = 1, 2, 3, 4, 5, 6, 7, 8
# messaging::friendship_actions_enum
ADD_FRIEND, REMOVE_FRIEND, ADD_IGNORABLE, REMOVE_IGNORABLE = 0, 1, 2, 3
FIND_PLAYERS, QUERY_FRIEND_LIST, QUERY_IGNORE_LIST, UPDATE_FRIENDS_STATUS = 4, 5, 6, 7
NO_MATCH = 0xFFFFFFFF


def mb(text: str) -> bytes:
    """wcstombs_s(..., _TRUNCATE) into char[256]/char[32] under the system code page."""
    return text.encode(CODEPAGE)


def pk_sign_in(sid: int) -> bytes:                        # messaging_client.cpp:84-96
    return bytes([0xC3]) + struct.pack("<I", sid) + bytes([5])


def pk_subscriptions(match_channel_id: int) -> bytes:     # messaging_client_sign_in.cpp:39-53
    subs = [0, NO_MATCH, NO_MATCH, 0, 0, match_channel_id if match_channel_id != NO_MATCH else 0, 0, 0, 0]
    return bytes([0xC5]) + struct.pack("<9I", *subs)


def pk_text(channel_id: int, receiver: bytes, channel: int, body: bytes) -> bytes:   # :176-182
    return (bytes([0xC1]) + struct.pack("<I", channel_id) + bytes([len(receiver)]) + receiver
            + bytes([channel]) + bytes([len(body)]) + body)


def pk_friendship(action: int, arg: int | bytes | None = None) -> bytes:              # :188-279
    out = bytes([0xC4, action])
    if isinstance(arg, int):
        out += struct.pack("<I", arg)
    elif isinstance(arg, bytes):
        out += bytes([len(arg)]) + arg
    return out


@dataclass
class ChatLine:
    channel: int
    sender: str
    text: str
    sender_id: int = 0
    sender_type: int = 0


@dataclass
class ChatModel:
    local_name: str = "local"
    connected: bool = False
    match_channel_id: int = NO_MATCH
    team: int = 0
    in_match: bool = False                    # chat_handler::m_game_ui_mode
    lines: list[ChatLine] = field(default_factory=list)       # chat_handler::add_message
    received: list[ChatLine] = field(default_factory=list)    # 0xC9 that reached the chat UI
    match_making: list[str] = field(default_factory=list)     # channel 7 -> on_match_message_arrived
    stats: list[str] = field(default_factory=list)           # channel 8 -> on_stats_message_arrived
    friends: list[tuple[int, str, bool]] = field(default_factory=list)
    ignores: list[tuple[int, str]] = field(default_factory=list)
    found: list[tuple[int, str]] = field(default_factory=list)
    friendship_events: list[int] = field(default_factory=list)
    results: list[tuple[int, int]] = field(default_factory=list)
    faults: list[str] = field(default_factory=list)


class MockChatClient:
    def __init__(self):
        self.m = ChatModel()
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.sent: list[bytes] = []
        self.frames: list[bytes] = []

    # --- transport ----------------------------------------------------------------------
    async def connect(self, host: str, port: int, sid: int, expect_name: str | None = None) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)
        await self.send(pk_sign_in(sid))                 # on_connected
        await self.pump(lambda m: m.connected)
        if expect_name is not None:
            assert self.m.local_name == expect_name, self.m.local_name
        await self.pump()                                # friend + ignore list answers

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)
        self.writer.write(tcp_frame(payload))
        await self.writer.drain()

    async def recv_frame(self, timeout: float) -> bytes:
        async def _read():
            n = (await self.reader.readexactly(1))[0]
            if n == 0:
                n = struct.unpack("<H", await self.reader.readexactly(2))[0]
            return await self.reader.readexactly(n)
        return await asyncio.wait_for(_read(), timeout)

    async def pump(self, until=None, timeout: float = 5.0, quiet: float = 0.25) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if until is not None and until(self.m):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                if until is None:
                    return
                raise AssertionError(f"timed out; lines={self.m.lines[-5:]} faults={self.m.faults}")
            try:
                payload = await self.recv_frame(min(remaining, quiet) if until is None else remaining)
            except asyncio.TimeoutError:
                if until is None:
                    return
                continue
            self.frames.append(payload)
            for pk in self.on_packet(payload):
                await self.send(pk)

    async def close(self) -> None:
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except ConnectionError:
                pass

    async def closed_by_server(self, timeout: float = 3.0) -> bool:
        try:
            data = await asyncio.wait_for(self.reader.read(1), timeout)
        except (asyncio.TimeoutError, ConnectionError):
            return False
        return data == b""

    # --- server -> client ---------------------------------------------------------------
    def on_packet(self, payload: bytes) -> list[bytes]:
        rd = Reader(payload)
        try:
            op = rd.r("B")
            if not self.m.connected:                     # sign_in_on_packet_received
                if op != 0xCB:
                    self.m.faults.append(f"unknown message during sign-in: {op}")
                    return []
                self.m.local_name = rd.r_string(32)
                self.m.connected = True
                out = [pk_subscriptions(self.m.match_channel_id),
                       pk_friendship(QUERY_FRIEND_LIST), pk_friendship(QUERY_IGNORE_LIST)]
            elif op == 0xC9:
                out = self.on_text(rd)
            elif op == 0xCC:
                out = self.on_friendship(rd)
            else:
                self.m.faults.append(f"messaging_client received unknown message:{op}")
                return []
            if not rd.eof():
                self.m.faults.append(f"op {op:#x}: {len(rd.b) - rd.p} trailing bytes")
            return out
        except AssertionError as e:
            self.m.faults.append(f"{payload[:1].hex()}: {e}")
            return []

    def on_text(self, rd: Reader) -> list[bytes]:      # process_incoming_text_message
        sender_type = rd.r("B")
        sender_id = rd.r("I")
        if sender_type == 5 and any(aid == sender_id for aid, _ in self.m.ignores):
            rd.p = len(rd.b)                             # accept_message_from: dropped unread
            return []
        name = rd.r_string(32)
        channel = rd.r("B")
        n = rd.r("B")
        assert n < 255, f"body of {n} bytes"              # r_string(char[256]) -> buffer 255
        text = rd.raw(n).decode(CODEPAGE)
        line = ChatLine(channel, name, text, sender_id, sender_type)
        if channel == TEAM2:
            self.m.match_making.append(text)
        elif channel == SQUAD:
            self.m.stats.append(text)
        else:
            self.m.lines.append(line)
            self.m.received.append(line)
        return []

    def on_friendship(self, rd: Reader) -> list[bytes]:
        action = rd.r("B")
        out = []
        if action == QUERY_FRIEND_LIST:
            self.m.friends = [(rd.r("I"), rd.r_string(32), bool(rd.r("B"))) for _ in range(rd.r("H"))]
        elif action == UPDATE_FRIENDS_STATUS:
            for _ in range(rd.r("H")):
                aid, online = rd.r("I"), bool(rd.r("B"))
                for i, (fid, nm, _) in enumerate(self.m.friends):
                    if fid == aid:
                        self.m.friends[i] = (fid, nm, online)
                        break
                else:
                    self.m.faults.append("Friend list out of sync.")
        elif action == QUERY_IGNORE_LIST:
            self.m.ignores = [(rd.r("I"), rd.r_string(32)) for _ in range(rd.r("H"))]
        elif action == FIND_PLAYERS:
            self.m.found = [(rd.r("I"), rd.r_string(32)) for _ in range(rd.r("H"))]
        else:
            result = rd.r("B")
            self.m.results.append((action, result))
            if result == ord("4"):
                out.append(pk_friendship(QUERY_FRIEND_LIST if action in (ADD_FRIEND, REMOVE_FRIEND)
                                         else QUERY_IGNORE_LIST))
        self.m.friendship_events.append(action)        # lobby_menu::on_friendship_status_recivied
        return out

    # --- client actions -----------------------------------------------------------------
    def parse_receiver_channel(self, name: str) -> int:   # messaging_client_process_messagess.cpp:86
        if name.startswith(("общий", "general")):
            return GENERAL
        if name.startswith(("отряд", "squad")):
            return SQUAD
        if name.startswith(("клан", "clan")):
            return CLAN
        if self.m.in_match:
            if name.startswith(("своим", "team")):
                return TEAM2 if self.m.team else TEAM1
            if name.startswith(("всем", "all")):
                return MATCH
        return PRIVATE

    async def type_message(self, text: str, channel: int = GENERAL) -> bool:
        """messaging_client::on_message_typed; chat.swf hands over the input field text, which
        carries the selected channel's key ("/general hi", "/all gg", "/Name psst")."""
        receiver = ""
        space = text.find(" ")
        if text.startswith("/") and space >= 0:
            receiver = text[1:space][:31]
            channel = self.parse_receiver_channel(receiver)
            text = text[space + 1:]
        if channel != PRIVATE:
            receiver = ""
        if not self.m.connected:
            self.m.lines.append(ChatLine(SYSTEM, "System", "not connected to messaging server..."))
            return False
        self.m.lines.append(ChatLine(channel, self.m.local_name, text))   # local echo
        channel_id = 0
        if channel == CLAN or channel in (TEAM1, TEAM2):
            return False
        if channel == MATCH:
            channel_id = self.m.match_channel_id
            if channel_id == NO_MATCH:
                return False
        elif channel == SQUAD:
            channel_id = NO_MATCH
            if self.m.match_channel_id == NO_MATCH:
                return False
        await self.send(pk_text(channel_id, mb(receiver)[:31], channel, mb(text)[:255]))
        return True

    async def assign_match_channel_order(self, match_id: int, team: int) -> None:
        if self.m.match_channel_id == match_id or match_id == NO_MATCH:
            return
        self.m.match_channel_id, self.m.team = match_id, team
        if self.m.connected:
            await self.send(pk_subscriptions(match_id))
