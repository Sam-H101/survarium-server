#!/usr/bin/env python3
"""Minimal proof-of-concept server for the original Survarium v0.100b client.

Goal: let the shipped survarium.exe sign in and get past the login screen.
The first sign-in of an account name creates the account with that password. Protocol
taken from the binary-matched decompilation (vostok repo):

  sources/vostok/network/sources/login_client_impl_sign_in.cpp   (shipped login client)
  sources/vostok/network/sources/login_client_impl_sign_out.cpp
  sources/vostok/login_server/message_types.h, constants.h       (enums, ports)
  sources/vostok/login_server/sources/client_session_sign_*.cpp  (2012 GSC login server)

Sign in (one TCP connection, TLS is started mid-stream):
  C->S plain : 0x01 | u8 len | account_name | char version[8] ("0.100b")
  S->C plain : 0x0B valid_user_name, or a refusal the client reports and closes on:
               0x14 sign_in_invalid_version, 0x0A invalid_user_name_or_password (empty
               name), 0x0C sign_in_attempt_interval_violated (too many wrong passwords)
  -- TLS handshake, client verifies us against resources/ssl/survarium_login_server.crt --
  C->S tls   : u8 len | password
  S->C tls   : 0x08 | u8 len | browser_address | u8 len | initial_query | u32le session_id
               (servers_connection_info; read with a single 70-byte read_some), or
               0x0A (wrong password) / 0x13 sign_in_user_already_signed_in (the account's
               session still pings) as the first byte of that read
  C->S udp   : u32 session_id every second to <login host>:25100 (no reply expected)

Sign out (network_client::disconnect, a new TCP connection; no answer is read):
  C->S plain : 0x02 | u32le session_id
  -- TLS handshake --
  C->S tls   : u8 len | password          -> the session is dropped if the password is the
                                             account's; the server then closes

After login the client asks the "server browser" over HTTP (always port 80) where the
lobby and chat live (network_client.cpp, network_core/sources/http_client.cpp):
  GET <initial_query>&type=2&local_ip=..&login_ip=.. HTTP/1.0  ->  body "host:port" (lobby)
  GET <initial_query>&type=4&...                              ->  body "host:port" (chat, see chat.py;
                                                                  "x:0" with --no-chat)

Lobby TCP (game/sources/lobby_client.cpp): see lobby.py - profiles, inventory, shop,
skills and Play (op 32 -> matchmaking -> op 51 connect_to_match_server, default
127.0.0.1:25103). Per-account state persists in state/lobby_state.json.

Scaling (README "Scaling"): login, browser, lobby and chat share the main event loop;
matches run in --match-workers processes with one UDP port each (op 51 names the port of
the match's worker), so match simulation never stalls the TCP services.

Run:  python survarium_poc_server.py --ssl-dir <game>/resources/ssl
Then: survarium.exe -no_splash_screen -client=127.0.0.1:25100
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import hmac
import itertools
import json
import logging
import os
import ssl
import struct
import time
from pathlib import Path

import chat
import lobby
import lobby_data
import progression
import runtime

# login_client_message_types_enum / login_server_message_types_enum
SIGN_UP = 0x00
SIGN_IN = 0x01
SIGN_OUT = 0x02
SERVERS_CONNECTION_INFO = 0x08
INVALID_USER_NAME_OR_PASSWORD = 0x0A
VALID_USER_NAME = 0x0B
SIGN_IN_ATTEMPT_INTERVAL_VIOLATED = 0x0C
SIGN_IN_USER_ALREADY_SIGNED_IN = 0x13
SIGN_IN_INVALID_VERSION = 0x14

CLIENT_VERSIONS = ("0.100b",)   # login_client_impl::sign_in_on_connected: char version[8]
SESSION_ALIVE_S = 10.0          # a session pinged this recently is signed in (pings: 1/s)
FAILED_SIGN_INS_MAX = 5         # wrong passwords in a row before the account is held ...
FAILED_SIGN_IN_HOLD_S = 30.0    # ... for this long (0x0C)
PBKDF2_ITERATIONS = 20000

LOGIN_UDP_PORT = 25100  # network_ports_enum::login_udp_port (compiled into the client)

SIGN_IN_ANSWER_MAX = 70  # client reads the answer with one 70-byte read_some

LOGIN_TIMEOUT_S = 30.0       # the whole login exchange (the client needs milliseconds)
TLS_HANDSHAKE_TIMEOUT_S = 15.0
HTTP_TIMEOUT_S = 10.0
LISTEN_BACKLOG = 1024        # bursts of simultaneous connects (asyncio's default is 100)

log = logging.getLogger("poc")
_session_ids = itertools.count(1)


class SessionTable(collections.OrderedDict):
    """session_id -> account, bounded: the oldest sessions beyond ``limit`` are forgotten
    (each login creates one; a lobby/chat sign-in refreshes it)."""

    def __init__(self, limit: int = 20000):
        super().__init__()
        self.limit = limit

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.limit:
            self.popitem(last=False)

    def get(self, key, default=None):
        value = super().get(key, default)
        if key in self:
            self.move_to_end(key)
        return value


def hash_password(password: bytes, salt: bytes | None = None,
                  iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password, salt, iterations)
    return {"scheme": "pbkdf2_sha256", "iterations": iterations, "salt": salt.hex(), "hash": digest.hex()}


def check_password(password: bytes, record: dict) -> bool:
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password, bytes.fromhex(record["salt"]),
                                     int(record["iterations"]))
        return hmac.compare_digest(digest.hex(), record["hash"])
    except (KeyError, ValueError, TypeError):
        log.warning("unreadable password record %r", record)
        return False


def make_tls_context(ssl_dir: Path) -> ssl.SSLContext:
    """TLS 1.0 server context: the client is OpenSSL 1.0.0g (no TLS 1.1/1.2)."""
    crt = _pick(ssl_dir, "survarium_login_server", (".crt", ".pem"))
    key = _pick(ssl_dir, "survarium_login_server", (".key", ".pem"))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    ctx.load_cert_chain(crt, key)
    log.info("TLS cert %s, key %s", crt.name, key.name)
    return ctx


def _pick(ssl_dir: Path, stem: str, exts: tuple[str, ...]) -> Path:
    for ext in exts:
        p = ssl_dir / f"{stem}{ext}"
        if p.exists():
            return p
    raise SystemExit(f"missing {stem}{{{','.join(exts)}}} in {ssl_dir} - files: "
                     f"{sorted(x.name for x in ssl_dir.iterdir())}")


async def read_exact(reader: asyncio.StreamReader, n: int) -> bytes:
    return await reader.readexactly(n)


class MemoryTls:
    """Server-side TLS over an existing StreamReader/Writer, fed from whatever the reader
    has buffered (asyncio's start_tls loses bytes that arrived before it was called)."""

    def __init__(self, ctx: ssl.SSLContext, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        self.incoming, self.outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(self.incoming, self.outgoing, server_side=True)
        self.plain = bytearray()

    async def _flush(self) -> None:
        data = self.outgoing.read()
        if data:
            self.writer.write(data)
            await self.writer.drain()

    async def _feed(self) -> None:
        data = await self.reader.read(4096)
        if not data:
            raise ConnectionError("closed during TLS")
        self.incoming.write(data)

    async def handshake(self) -> None:
        while True:
            try:
                self.obj.do_handshake()
                break
            except ssl.SSLWantReadError:
                await self._flush()
                await self._feed()
        await self._flush()

    async def read_exact(self, n: int) -> bytes:
        while len(self.plain) < n:
            try:
                self.plain += self.obj.read(4096)
            except ssl.SSLWantReadError:
                await self._flush()
                await self._feed()
            except ssl.SSLZeroReturnError:
                raise ConnectionError("TLS closed") from None
        out, self.plain = bytes(self.plain[:n]), self.plain[n:]
        return out


class LoginServer:
    """``accounts`` (lobby.Store) holds the password hashes; None accepts any password.
    ``is_alive(session_id)`` tells whether a session still pings (PingSink.alive); None
    never refuses a sign-in as already signed in. Each ``on_sign_out`` callback gets the
    session a sign-out dropped (the lobby and chat close what it still has open)."""

    def __init__(self, tls: ssl.SSLContext, browser_address: str, initial_query: str,
                 sessions: dict[int, str] | None = None, rate: float = 0.0, burst: float = 40,
                 max_handshakes: int = 64, accounts=None, versions: tuple[str, ...] | None = CLIENT_VERSIONS,
                 is_alive=None):
        self.tls = tls
        self.sessions = sessions if sessions is not None else {}  # session_id -> account (for the lobby)
        self.accounts = accounts
        self.versions = versions                    # None: any version string
        self.is_alive = is_alive
        self.on_sign_out: list = []
        self.failures: collections.OrderedDict[str, tuple[int, float]] = collections.OrderedDict()
        self.browser_address = browser_address.encode()
        self.initial_query = initial_query.encode()
        if 1 + 1 + len(self.browser_address) + 1 + len(self.initial_query) + 4 > SIGN_IN_ANSWER_MAX:
            raise SystemExit("browser address + initial query exceed the client's 70-byte answer buffer")
        self.limiter = runtime.TokenBuckets(rate, burst)     # login attempts per IP
        self.handshakes = asyncio.Semaphore(max_handshakes)  # concurrent TLS logins
        self.logins = 0
        self.refused = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if peer and not self.limiter.allow(peer[0]):
            self.refused += 1
            log.warning("%s: too many login attempts from this address; refused", peer)
            writer.close()
            return
        try:
            await asyncio.wait_for(self._handle(reader, writer, peer), LOGIN_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("%s: login did not complete within %.0f s; closed", peer, LOGIN_TIMEOUT_S)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError) as e:
            log.warning("%s: connection ended: %r", peer, e)
        finally:
            writer.close()

    async def _handle(self, reader, writer, peer) -> None:
        msg = (await read_exact(reader, 1))[0]
        if msg == SIGN_IN:
            async with self.handshakes:
                await self.sign_in(reader, writer, peer)
        elif msg == SIGN_OUT:
            await self.sign_out(reader, writer, peer)
        else:
            log.warning("%s: unsupported login message 0x%02x", peer, msg)

    async def refuse(self, writer, peer, code: int, why: str) -> None:
        log.info("%s: sign in refused (0x%02x): %s", peer, code, why)
        self.refused += 1
        writer.write(bytes([code]))
        await writer.drain()

    def held(self, account: str) -> bool:
        count, until = self.failures.get(account, (0, 0.0))
        return count >= FAILED_SIGN_INS_MAX and time.monotonic() < until

    def failed(self, account: str) -> None:
        count, until = self.failures.pop(account, (0, 0.0))
        if count >= FAILED_SIGN_INS_MAX and time.monotonic() >= until:
            count = 0                                  # the hold is over: a fresh series
        self.failures[account] = (count + 1, time.monotonic() + FAILED_SIGN_IN_HOLD_S)
        while len(self.failures) > 10000:
            self.failures.popitem(last=False)

    async def verify_password(self, account: str, password: bytes) -> bool:
        """The first sign-in of an account (or of one saved before passwords existed) sets its
        password; later ones must match it."""
        if self.accounts is None:
            return True
        record = self.accounts.password_record(account)
        if record is not None:
            return await asyncio.to_thread(check_password, password, record)
        record = await asyncio.to_thread(hash_password, password)
        if self.accounts.password_record(account) is not None:      # set meanwhile
            return await self.verify_password(account, password)
        self.accounts.set_password(account, record)
        log.info("account %r: password set by its first sign-in", account)
        return True

    def sessions_of(self, account: str) -> list[int]:
        return [sid for sid, acc in list(self.sessions.items()) if acc == account]

    async def sign_in(self, reader, writer, peer) -> None:
        name_len = (await read_exact(reader, 1))[0]
        account = (await read_exact(reader, name_len)).decode(errors="replace")
        version = (await read_exact(reader, 8)).split(b"\0", 1)[0].decode(errors="replace")
        log.info("%s: sign in account=%r version=%r", peer, account, version)
        if self.versions is not None and version not in self.versions:
            return await self.refuse(writer, peer, SIGN_IN_INVALID_VERSION,
                                     f"client version {version!r}, expected {', '.join(self.versions)}")
        if not account.strip():
            return await self.refuse(writer, peer, INVALID_USER_NAME_OR_PASSWORD, "empty account name")
        if self.held(account):
            return await self.refuse(writer, peer, SIGN_IN_ATTEMPT_INTERVAL_VIOLATED,
                                     f"{account!r}: too many wrong passwords, held for {FAILED_SIGN_IN_HOLD_S:.0f} s")

        writer.write(bytes([VALID_USER_NAME]))
        await writer.drain()
        await writer.start_tls(self.tls, ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT_S)
        log.debug("%s: TLS up (%s)", peer, writer.get_extra_info("ssl_object").version())

        pw_len = (await read_exact(reader, 1))[0]
        password = await read_exact(reader, pw_len)
        if not await self.verify_password(account, password):
            self.failed(account)
            return await self.refuse(writer, peer, INVALID_USER_NAME_OR_PASSWORD, f"{account!r}: wrong password")
        self.failures.pop(account, None)
        old = self.sessions_of(account)
        if self.is_alive is not None and any(self.is_alive(sid) for sid in old):
            return await self.refuse(writer, peer, SIGN_IN_USER_ALREADY_SIGNED_IN,
                                     f"{account!r} is signed in (session {old}) and still pings")
        for sid in old:                  # client_session::add_online_user: one session per account
            self.sessions.pop(sid, None)

        session_id = next(_session_ids)
        self.sessions[session_id] = account
        answer = (bytes([SERVERS_CONNECTION_INFO, len(self.browser_address)]) + self.browser_address
                  + bytes([len(self.initial_query)]) + self.initial_query
                  + struct.pack("<I", session_id))
        writer.write(answer)
        await writer.drain()
        self.logins += 1
        log.debug("%s: signed in, session_id=%d, browser=%r query=%r",
                  peer, session_id, self.browser_address, self.initial_query)

    async def sign_out(self, reader, writer, peer) -> None:
        """login_client_impl_sign_out.cpp: [02][u32 session], a TLS handshake, then the password
        over TLS; the client reads nothing and closes. As client_session::process_sign_out, an
        unknown session is closed before the handshake and the session is removed only when
        the password is the account's (remove_online_user_with_password)."""
        session_id = struct.unpack("<I", await read_exact(reader, 4))[0]
        account = self.sessions.get(session_id)
        if account is None:
            log.info("%s: sign out of unknown session %d", peer, session_id)
            return
        # The client starts the handshake right after its 5 bytes, without waiting for us, so
        # its ClientHello may already sit in the StreamReader's buffer (start_tls would never
        # see it): run TLS over the stream through memory BIOs instead.
        tls = MemoryTls(self.tls, reader, writer)
        await asyncio.wait_for(tls.handshake(), TLS_HANDSHAKE_TIMEOUT_S)
        pw_len = (await tls.read_exact(1))[0]
        password = await tls.read_exact(pw_len)
        record = self.accounts.password_record(account) if self.accounts is not None else None
        if record is not None and not await asyncio.to_thread(check_password, password, record):
            log.warning("%s: sign out of session %d (%r) with a wrong password; ignored", peer, session_id, account)
            return
        self.sessions.pop(session_id, None)
        log.info("%s: %r signed out (session %d)", peer, account, session_id)
        for callback in self.on_sign_out:
            try:
                callback(session_id)
            except Exception:  # noqa: BLE001 - one service must not keep the others open
                log.exception("sign-out hook failed for session %d", session_id)


class BrowserServer:
    """HTTP server browser: tells the client where the lobby (type=2) and chat (type=4) are.
    The client skips chat when the answer's host is "x" or its port 0 (messaging_client.cpp:59)."""

    def __init__(self, lobby: str, chat: str = "x:0"):
        self.lobby = lobby
        self.chat = chat

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HTTP_TIMEOUT_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError,
                asyncio.TimeoutError, OSError):
            writer.close()
            return
        line = request.split(b"\r\n", 1)[0].decode(errors="replace")
        body = self.chat if "type=4" in line else self.lobby
        log.debug("%s: http %s -> %s", peer, line, body)
        # One write: the client reads the status line, drains headers already buffered,
        # then collects the body until EOF.
        try:
            writer.write(f"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\n\r\n{body}".encode())
            await asyncio.wait_for(writer.drain(), HTTP_TIMEOUT_S)
        except (ConnectionError, asyncio.TimeoutError, OSError):
            pass
        writer.close()


class PingSink(asyncio.DatagramProtocol):
    """Swallows the client's keep-alive pings so Windows never reports the port unreachable."""

    SEEN_MAX = 20000

    def __init__(self):
        self.last: collections.OrderedDict[int, float] = collections.OrderedDict()  # session -> time
        self.pings = 0

    def datagram_received(self, data: bytes, addr) -> None:
        self.pings += 1
        if len(data) == 4:
            sid = struct.unpack("<I", data)[0]
            if sid not in self.last:
                log.debug("ping from %s session_id=%d (further pings not logged)", addr, sid)
            self.last[sid] = time.monotonic()
            self.last.move_to_end(sid)
            while len(self.last) > self.SEEN_MAX:
                self.last.popitem(last=False)

    def alive(self, session_id: int) -> bool:
        """The client pings every second while signed in (login_client_impl::ping)."""
        seen = self.last.get(session_id)
        return seen is not None and time.monotonic() - seen < SESSION_ALIVE_S

    def error_received(self, exc: Exception) -> None:
        pass


class ConnectionGuard:
    """Caps the simultaneous TCP connections of all services (file descriptors / memory);
    a connection over the cap is closed at once."""

    def __init__(self, limit: int):
        self.limit = limit
        self.open = 0
        self.refused = 0

    def wrap(self, handler):
        async def guarded(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            if self.limit and self.open >= self.limit:
                self.refused += 1
                log.warning("connection limit %d reached; refusing %s", self.limit,
                            writer.get_extra_info("peername"))
                writer.close()
                return
            self.open += 1
            try:
                await handler(reader, writer)
            finally:
                self.open -= 1
        return guarded


HERE = Path(__file__).resolve().parent


def host_port(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected host:port, got {text!r}")
    return host, int(port)


def worker_count(text: str) -> int:
    if text == "auto":
        return min(4, max(1, (os.cpu_count() or 2) // 2))
    if not text.isdigit():
        raise argparse.ArgumentTypeError("expected a number or 'auto'")
    return int(text)


def build_lobby(args, sessions: dict[int, str]) -> lobby.LobbyServer:
    gd = lobby_data.load(args.game_data)
    state_dir = None if args.no_persist else args.state_dir
    store = lobby.Store(state_dir / "lobby_state.json" if state_dir else None, gd, {
        "money": args.start_money, "premium_money": args.start_premium, "skill_points": args.start_skill_points})
    mm_host, mm_port = args.match_server
    matchmaker = lobby.Matchmaker(mm_host, mm_port, args.match_id, args.matchmaking_delay,
                                  state_dir / "match_tickets.json" if state_dir else None,
                                  match_size=args.match_size, min_players=args.min_players)
    rules = progression.load(args.progression if args.progression.is_file() else None, args.reward_scale)
    return lobby.LobbyServer(gd, store, matchmaker, sessions,
                             fallback_account=args.nickname if args.accept_unknown_sessions else None,
                             match_timeout=args.match_timeout, serve_skills_tree=not args.no_skills_tree,
                             rules=rules)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssl-dir", type=Path, required=True, help="game's resources/ssl directory")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--public-host", default="127.0.0.1", help="address the client is told to dial")
    ap.add_argument("--login-port", type=int, default=25100)
    ap.add_argument("--udp-port", type=int, default=LOGIN_UDP_PORT,
                    help="keep-alive sink; the client always pings 25100 (change only for tests)")
    ap.add_argument("--lobby-port", type=int, default=25101)
    ap.add_argument("--http-port", type=int, default=80, help="the client always uses 80")
    ap.add_argument("--initial-query", default="/sb?v=1", help="client appends &type=..")
    ap.add_argument("--accept-unknown-sessions", action="store_true",
                    help="dev/tools: a lobby or chat sign-in with a session the login server did not "
                         "issue uses the --nickname account (default: op 49 invalid_session_id)")
    ap.add_argument("--nickname", default="Stalker", help="account for --accept-unknown-sessions")
    ap.add_argument("--client-version", default=",".join(CLIENT_VERSIONS),
                    help="comma-separated client version strings the login accepts ('any' = all); "
                         "others get 0x14 sign_in_invalid_version")
    ap.add_argument("--no-passwords", action="store_true",
                    help="accept any password (the old behaviour); passwords are still not stored")
    lob = ap.add_argument_group("lobby")
    lob.add_argument("--match-server", type=host_port, default=None,
                     help="host:port sent in connect_to_match_server (default <public-host>:25103, "
                          "match_udp_port); the host must be a numeric IPv4 address. With match "
                          "workers, worker k listens on port+k and op 51 names the match's worker")
    lob.add_argument("--match-id", type=int, default=1, help="id of the first match formed")
    lob.add_argument("--match-size", type=int, default=2,
                     help="players per match: a match starts as soon as this many are queued")
    lob.add_argument("--min-players", type=int, default=1,
                     help="after the fill timeout a match starts with at least this many queued")
    lob.add_argument("--matchmaking-delay", "--fill-timeout", dest="matchmaking_delay", type=float,
                     default=20.0, help="fill timeout: seconds the first queued player waits for "
                                        "--match-size players before starting with fewer")
    lob.add_argument("--match-timeout", type=float, default=60.0,
                     help="seconds after op 51 before a player the match server never saw is put back in the menu")
    lob.add_argument("--game-data", type=Path, default=HERE.parent / "game_data" / "extracted",
                     help="unpacked resources (gameplay/db_static_dictionaries); built-in table if missing")
    lob.add_argument("--state-dir", type=Path, default=HERE / "state")
    lob.add_argument("--no-persist", action="store_true", help="keep lobby state in memory only")
    lob.add_argument("--start-money", type=int, default=10000,
                     help="money of a new account (the retail starter table: 10000)")
    lob.add_argument("--start-premium", type=int, default=100)
    lob.add_argument("--start-skill-points", type=int, default=10)
    lob.add_argument("--no-skills-tree", action="store_true", help="leave query type 9 unanswered")
    lob.add_argument("--progression", type=Path, default=HERE / "data" / "progression.json",
                     help="level table and match rewards (experience, money, reputation); "
                          "built-in defaults if missing")
    lob.add_argument("--reward-scale", type=float, default=1.0,
                     help="multiplies every match reward (10 = progress ten times as fast)")
    ch = ap.add_argument_group("chat")
    ch.add_argument("--chat-port", type=int, default=chat.CHAT_TCP_PORT, help="chat TCP listen port")
    ch.add_argument("--chat-address", type=host_port, default=None,
                    help="host:port the browser's type=4 answer names (default <public-host>:<chat-port>)")
    ch.add_argument("--no-chat", action="store_true",
                    help="don't run the chat server; the browser answers type=4 with x:0")
    ap.add_argument("--no-match-server", action="store_true",
                    help="don't run the in-process match server (run `python -m match` separately)")
    mt = ap.add_argument_group("match rules")
    mt.add_argument("--match-time", type=int, default=600, help="seconds per match (0x81 match_time)")
    mt.add_argument("--respawn-time", type=int, default=10, help="seconds dead before respawn")
    mt.add_argument("--victory-items", type=int, default=3,
                    help="victory items on the map; a team that stores them all wins")
    mt.add_argument("--join-timeout", type=float, default=60.0,
                    help="seconds a match waits for its whole roster before starting anyway")
    mt.add_argument("--friendly-fire", action="store_true")
    sc = ap.add_argument_group("scaling and robustness (README: Scaling)")
    sc.add_argument("--match-workers", type=worker_count, default="auto",
                    help="match processes (each its own UDP port, port+k); 0 = matches run in "
                         "the main process; default auto = min(4, CPUs/2)")
    sc.add_argument("--login-rate", type=float, default=5.0,
                    help="login attempts per second per IP address (0 = unlimited)")
    sc.add_argument("--login-burst", type=float, default=40.0,
                    help="login attempts an IP address may make at once before --login-rate applies")
    sc.add_argument("--max-logins", type=int, default=64, help="TLS logins handled at the same time")
    sc.add_argument("--max-connections", type=int, default=4096,
                    help="simultaneous TCP connections over all services (0 = unlimited)")
    sc.add_argument("--stats-interval", type=float, default=30.0,
                    help="seconds between the summary lines in the log (0 = none)")
    sc.add_argument("--stats-file", type=Path, default=None,
                    help="append one JSON line of measurements per --stats-file-interval (tools/loadtest.py)")
    sc.add_argument("--stats-file-interval", type=float, default=1.0)
    sc.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                    help="DEBUG logs every query, chat line and spawn")
    sc.add_argument("-v", "--verbose", dest="log_level", action="store_const", const="DEBUG")
    sc.add_argument("--no-log-rate-limit", action="store_true",
                    help="do not cap repeated log lines (default: ~20 per call site, then 1 per 2 s)")
    args = ap.parse_args(argv)
    if args.match_server is None:
        args.match_server = (args.public_host, 25103)
    if args.chat_address is None:
        args.chat_address = (args.public_host, args.chat_port)
    return args


class StatsReporter:
    """Periodic measurements: a summary line in the log every --stats-interval seconds and,
    with --stats-file, one JSON line per --stats-file-interval."""

    def __init__(self, args, login: LoginServer, lobby_server: lobby.LobbyServer,
                 chat_server, ping: PingSink, guard: ConnectionGuard, match_server=None,
                 pool=None, log_filter=None):
        self.args = args
        self.login, self.lobby, self.chat = login, lobby_server, chat_server
        self.ping, self.guard = ping, guard
        self.match_server, self.pool = match_server, pool
        self.log_filter = log_filter
        self.lag = runtime.LoopLagMonitor()
        self.tick = runtime.Samples(8192)
        self.wlag = runtime.Samples(4096)
        self.cycle = runtime.Samples(4096)
        self.cpu_by_worker: dict[int, float] = {}
        self.rss_by_worker: dict[int, float] = {}
        self.match_info = {"matches": 0, "sessions": 0, "players": 0}
        self.cpu0, self.t0 = runtime.cpu_seconds(), time.monotonic()

    def start(self) -> None:
        self.lag.start()
        loop = asyncio.get_running_loop()
        loop.create_task(self._file_loop())
        if self.args.stats_interval > 0:
            loop.create_task(self._log_loop())

    def players(self) -> dict:
        out = {"sessions": len(self.login.sessions), "tcp": self.guard.open}
        out.update(self.lobby.summary())
        if self.chat is not None:
            out["chat"] = len(self.chat.signed_in())
        out.update(matches=self.match_info.get("matches", 0),
                   match_sessions=self.match_info.get("sessions", 0),
                   players=self.match_info.get("players", 0))
        return out

    def collect(self) -> dict:
        """Drain every source once (main loop lag, in-process match server or workers)."""
        line = {"t": time.time(), "loop_lag_ms": self.lag.samples.drain(), "tick_ms": {},
                "cycle_ms": [], "tick_interval_ms": [], "worker_lag_ms": [], "dgram_ms": 0.0}
        if self.match_server is not None:
            d = self.match_server.drain_stats()
            line["tick_ms"], line["cycle_ms"] = d["tick_ms"], d["cycle_ms"]
            line["tick_interval_ms"], line["dgram_ms"] = d["tick_interval_ms"], d["dgram_ms"]
            self.match_info = {k: d[k] for k in ("matches", "sessions", "players")}
        if self.pool is not None:
            batch, self.pool.stats_pending = self.pool.stats_pending, []
            for d in batch:
                for k, v in d.get("tick_ms", {}).items():
                    line["tick_ms"].setdefault(k, []).extend(v)
                line["cycle_ms"] += d.get("cycle_ms", [])
                line["tick_interval_ms"] += d.get("tick_interval_ms", [])
                line["worker_lag_ms"] += d.get("loop_lag_ms", [])
                line["dgram_ms"] += d.get("dgram_ms", 0.0)
                self.cpu_by_worker[d["index"]] = d.get("cpu_s", 0.0)
                self.rss_by_worker[d["index"]] = d.get("rss_mb", 0.0)
            self.match_info = self.pool.summary()
        for vs in line["tick_ms"].values():
            for v in vs:
                self.tick.add(v)
        for v in line["cycle_ms"]:
            self.cycle.add(v)
        for v in line["worker_lag_ms"]:
            self.wlag.add(v)
        line["cpu_s"] = runtime.cpu_seconds() + sum(self.cpu_by_worker.values())
        line["rss_mb"] = runtime.rss_mb() + sum(self.rss_by_worker.values())
        line["processes"] = 1 + len(self.rss_by_worker)
        line["players"] = self.players()
        return line

    async def _file_loop(self) -> None:
        interval = self.args.stats_file_interval if self.args.stats_file else 1.0
        f = open(self.args.stats_file, "a", encoding="utf-8") if self.args.stats_file else None
        try:
            while True:
                await asyncio.sleep(interval)
                line = self.collect()
                if f is not None:
                    f.write(json.dumps(line) + "\n")
                    f.flush()
        finally:
            if f is not None:
                f.close()

    async def _log_loop(self) -> None:
        while True:
            await asyncio.sleep(self.args.stats_interval)
            p = self.players()
            now, cpu = time.monotonic(), runtime.cpu_seconds() + sum(self.cpu_by_worker.values())
            cpu_pct = 100.0 * (cpu - self.cpu0) / max(1e-6, now - self.t0)
            self.cpu0, self.t0 = cpu, now

            def ms(v):
                return "-" if v is None else f"{v:.1f}"
            lag = list(self.lag.samples.recent)
            log.info("stats: %d sessions, lobby %d, chat %s, queued %d, in match %d; %d matches, "
                     "%d match players; tick p50/p99 %s/%s ms, cycle p99 %s ms; loop lag p99 %s "
                     "ms%s; cpu %.0f%%, rss %.0f MB; logins %d (refused %d), log lines dropped %d",
                     p["sessions"], p["lobby"], p.get("chat", "off"), p["queued"], p["in_match"],
                     p["matches"], p["players"], ms(self.tick.p(50)), ms(self.tick.p(99)),
                     ms(self.cycle.p(99)), ms(runtime.pct(lag, 99)),
                     f", worker lag p99 {ms(self.wlag.p(99))} ms" if self.pool else "",
                     cpu_pct, runtime.rss_mb() + sum(self.rss_by_worker.values()),
                     self.login.logins, self.login.refused,
                     self.log_filter.suppressed_total if self.log_filter else 0)


async def main(argv=None) -> None:
    args = parse_args(argv)
    log_filter = runtime.setup_logging(getattr(logging, args.log_level), not args.no_log_rate_limit)
    runtime.high_resolution_timers()
    runtime.precise_loop_clock()
    sessions = SessionTable()
    ping = PingSink()
    lobby_server = build_lobby(args, sessions)
    versions = None if args.client_version == "any" else tuple(v.strip() for v in args.client_version.split(","))
    login = LoginServer(make_tls_context(args.ssl_dir), args.public_host, args.initial_query, sessions,
                        rate=args.login_rate, burst=args.login_burst, max_handshakes=args.max_logins,
                        accounts=None if args.no_passwords else lobby_server.store, versions=versions,
                        is_alive=ping.alive)
    login.on_sign_out.append(lobby_server.drop_session)
    chat_server = None
    if not args.no_chat:
        state_dir = None if args.no_persist else args.state_dir
        chat_server = chat.ChatServer(sessions, lobby_server,
                                      fallback_account=args.nickname if args.accept_unknown_sessions else None,
                                      state_path=state_dir / "chat_state.json" if state_dir else None)
        login.on_sign_out.append(chat_server.drop_session)
        lobby_server.notify = chat_server.notify_match_result
        lobby_server.feed = chat_server.send_feed
    browser = BrowserServer(f"{args.public_host}:{args.lobby_port}",
                            "x:0" if args.no_chat else "%s:%d" % args.chat_address)

    loop = asyncio.get_running_loop()
    ping_transport, _ = await loop.create_datagram_endpoint(
        lambda: ping, sock=runtime.udp_socket(args.host, args.udp_port))
    guard = ConnectionGuard(args.max_connections)

    async def listen(handler, port, **kw):
        return await asyncio.start_server(guard.wrap(handler), args.host, port,
                                          backlog=LISTEN_BACKLOG, **kw)

    match_server = pool = None
    if not args.no_match_server:
        from match.match_state import MatchConfig
        cfg = MatchConfig(match_time=args.match_time, respawn_time=args.respawn_time,
                          victory_items_count=args.victory_items, join_timeout_s=args.join_timeout,
                          friendly_fire=args.friendly_fire)
        # the in-process / worker match server has the tickets in memory: the file is
        # informational and written off the event loop
        lobby_server.mm.sync_tickets = False
        if args.match_workers > 0:
            from match.pool import MatchPool
            pool = MatchPool(args.match_workers, args.host, args.match_server[1], cfg,
                             on_event=lobby_server.on_match_event,
                             log_level=getattr(logging, args.log_level),
                             rate_limit_logs=not args.no_log_rate_limit)
            pool.on_match_removed = lobby_server.forget_match
            lobby_server.mm.placer = pool.place
        else:
            from match.server import start_match_server
            match_server = await start_match_server(loop, args.host, args.match_server[1], cfg,
                                                    ticket_lookup=lobby.get_match_ticket,
                                                    on_event=lobby_server.on_match_event)
            match_server.core.on_match_removed = lambda m: lobby_server.forget_match(m.match_id)
    if pool is not None:
        # workers first: the services open once every match process is up (~2 s), so a
        # stopped server never leaves a half-started worker behind
        await pool.start()

    servers = [
        await listen(login.handle, args.login_port),
        await listen(browser.handle, args.http_port, limit=8192),
        await listen(lobby_server.handle, args.lobby_port),
    ]
    if chat_server:
        servers.append(await listen(chat_server.handle, args.chat_port))
    stats = StatsReporter(args, login, lobby_server, chat_server, ping, guard, match_server, pool,
                          log_filter)
    stats.start()
    log.info("login tcp :%d, ping udp :%d, http :%d, lobby tcp :%d, chat %s, match server %s:%d%s - "
             "launch with -client=%s:%d", args.login_port, args.udp_port, args.http_port, args.lobby_port,
             "off" if args.no_chat else "tcp :%d (advertised %s:%d)" % (args.chat_port, *args.chat_address),
             *args.match_server,
             f" ({args.match_workers} worker processes, udp {', '.join(map(str, pool.ports))})" if pool
             else " (in-process)" if match_server else " (external)",
             args.public_host, args.login_port)
    try:
        await asyncio.gather(*(s.serve_forever() for s in servers))
    finally:
        if match_server:
            match_server.close()
        if pool:
            pool.close()
        ping_transport.close()
        lobby_server.flush()
        if chat_server:
            chat_server.friends.flush()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
