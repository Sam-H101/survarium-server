"""Login server tests: the shipped login client's exact bytes (login_client_impl_sign_in.cpp,
login_client_impl_sign_out.cpp) against survarium_poc_server.LoginServer, with a real
lobby.Store holding the passwords; and the lobby/chat answer to sessions it never issued.

    python -m unittest tests.test_login -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import json
import ssl
import struct
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chat  # noqa: E402
import lobby  # noqa: E402
import lobby_data as ld  # noqa: E402
import survarium_poc_server as sps  # noqa: E402
from chat_mock_client import MockChatClient  # noqa: E402
from mock_client import ClientModel, MockLobbyClient, pk_sign_in, tcp_frame  # noqa: E402

SSL_DIR = ROOT.parent / "game" / "resources" / "ssl"
CRT = SSL_DIR / "survarium_login_server.crt"
VERSION = b"0.100b\0\0"            # char version[8] = { 0 }; strings::copy( version, "0.100b" )


def client_tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(CRT)          # the client pins resources/ssl/survarium_login_server.crt
    ctx.check_hostname = False
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    try:                                    # OpenSSL 1.0.0g: TLS 1.0 (if this OpenSSL still allows it)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ssl.TLSVersion.TLSv1
    except (ValueError, ssl.SSLError):
        pass
    return ctx


async def sign_in(port: int, account: bytes, password: bytes, version: bytes = VERSION):
    """sign_in_on_connected .. on_sign_in_answer_received. Returns (first plain byte, the
    70-byte TLS answer or None when the client stops after the plain byte)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write(bytes([1, len(account)]) + account + version)
        await w.drain()
        first = await asyncio.wait_for(r.read(1), 5)
        if first != bytes([0x0B]):                  # on_user_name_answer_received: report, close
            return first, None
        await w.start_tls(client_tls_context())
        w.write(bytes([len(password)]) + password)
        await w.drain()
        return first, await asyncio.wait_for(r.read(70), 5)
    finally:
        w.close()


async def sign_out(port: int, session_id: int, password: bytes) -> bool:
    """sign_out_on_connected .. on_sign_out_password_written: no answer is read. Returns
    whether the TLS handshake happened (the server closes unknown sessions before it)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        w.write(bytes([2]) + struct.pack("<I", session_id))
        await w.drain()
        try:
            await asyncio.wait_for(w.start_tls(client_tls_context()), 5)
        except (ssl.SSLError, ConnectionError, OSError):
            return False
        w.write(bytes([len(password)]) + password)
        await w.drain()
        await asyncio.wait_for(r.read(), 5)          # until the server closes
        return True
    finally:
        w.close()


def session_of(answer: bytes) -> int:
    assert answer[0] == 0x08, answer.hex()
    n = answer[1]
    m = answer[2 + n]
    return struct.unpack_from("<I", answer, 3 + n + m)[0]


@unittest.skipUnless((SSL_DIR / "survarium_login_server.key").is_file(), "game ssl dir missing")
class TestLogin(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "lobby_state.json"
        self.gd = ld.load(None, None)
        self.alive: set[int] = set()
        await self.start()

    async def start(self):
        self.store = lobby.Store(self.path, self.gd, {"money": 1, "premium_money": 1, "skill_points": 1})
        self.sessions: dict[int, str] = {}
        self.login = sps.LoginServer(sps.make_tls_context(SSL_DIR), "127.0.0.1", "/sb?v=1", self.sessions,
                                     accounts=self.store, is_alive=lambda sid: sid in self.alive)
        self.dropped: list[int] = []
        self.login.on_sign_out.append(self.dropped.append)
        self.server = await asyncio.start_server(self.login.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()
        self.store.flush()

    async def asyncTearDown(self):
        await self.stop()
        self.tmp.cleanup()

    async def test_first_sign_in_sets_the_password(self):
        first, answer = await sign_in(self.port, b"neo", b"red pill")
        self.assertEqual(first, b"\x0b")
        sid = session_of(answer)
        self.assertEqual(self.sessions, {sid: "neo"})
        rec = self.store.password_record("neo")
        self.assertEqual(rec["scheme"], "pbkdf2_sha256")
        self.assertNotIn("red pill", json.dumps(rec))
        self.assertEqual(len(self.store.doc["accounts"]["neo"]["profiles"]), 3)   # the account exists

        _, answer = await sign_in(self.port, b"neo", b"blue pill")      # wrong password, over TLS
        self.assertEqual(answer, b"\x0a")                                # invalid_user_name_or_password
        _, answer = await sign_in(self.port, b"neo", b"red pill")
        sid2 = session_of(answer)
        self.assertNotEqual(sid2, sid)
        self.assertEqual(self.sessions, {sid2: "neo"})                   # one session per account

        await self.stop()                                                # kept across a restart
        await self.start()
        self.assertEqual(json.loads(self.path.read_text())["accounts"]["neo"]["password"], rec)
        _, answer = await sign_in(self.port, b"neo", b"blue pill")
        self.assertEqual(answer, b"\x0a")
        _, answer = await sign_in(self.port, b"neo", b"red pill")
        self.assertEqual(answer[0], 0x08)

    async def test_account_saved_before_passwords_adopts_the_first_one(self):
        self.store.account("veteran")                                    # an old state file entry
        self.assertIsNone(self.store.password_record("veteran"))
        money = self.store.doc["accounts"]["veteran"]["money"]
        _, answer = await sign_in(self.port, b"veteran", b"whatever")
        self.assertEqual(answer[0], 0x08)
        self.assertIsNotNone(self.store.password_record("veteran"))
        self.assertEqual(self.store.doc["accounts"]["veteran"]["money"], money)   # same account
        _, answer = await sign_in(self.port, b"veteran", b"other")
        self.assertEqual(answer, b"\x0a")

    async def test_wrong_version_and_empty_name(self):
        first, answer = await sign_in(self.port, b"neo", b"pw", b"0.99a\0\0\0")
        self.assertEqual((first, answer), (b"\x14", None))               # sign_in_invalid_version
        first, answer = await sign_in(self.port, b"", b"pw")
        self.assertEqual((first, answer), (b"\x0a", None))
        self.assertEqual(self.sessions, {})
        self.assertNotIn("neo", self.store.doc["accounts"])

    async def test_repeated_wrong_passwords_hold_the_account(self):
        await sign_in(self.port, b"neo", b"right")
        for _ in range(sps.FAILED_SIGN_INS_MAX):
            _, answer = await sign_in(self.port, b"neo", b"wrong")
            self.assertEqual(answer, b"\x0a")
        first, answer = await sign_in(self.port, b"neo", b"right")
        self.assertEqual((first, answer), (b"\x0c", None))               # attempt interval violated
        first, answer = await sign_in(self.port, b"trinity", b"pw")      # other accounts unaffected
        self.assertEqual(first, b"\x0b")
        self.login.failures["neo"] = (sps.FAILED_SIGN_INS_MAX, 0.0)      # the hold ran out
        _, answer = await sign_in(self.port, b"neo", b"right")
        self.assertEqual(answer[0], 0x08)
        self.assertNotIn("neo", self.login.failures)

    async def test_already_signed_in_while_the_session_pings(self):
        _, answer = await sign_in(self.port, b"neo", b"pw")
        sid = session_of(answer)
        self.alive.add(sid)                                              # its keep-alive arrives
        _, answer = await sign_in(self.port, b"neo", b"pw")
        self.assertEqual(answer, b"\x13")                                # sign_in_user_already_signed_in
        _, answer = await sign_in(self.port, b"neo", b"wrong")
        self.assertEqual(answer, b"\x0a")                                # the password is checked first
        self.assertEqual(self.sessions, {sid: "neo"})
        self.alive.clear()                                               # the client is gone
        _, answer = await sign_in(self.port, b"neo", b"pw")
        self.assertEqual(self.sessions, {session_of(answer): "neo"})

    async def test_sign_out_drops_the_session(self):
        _, answer = await sign_in(self.port, b"neo", b"pw")
        sid = session_of(answer)
        self.assertTrue(await sign_out(self.port, sid, b"wrong"))
        self.assertIn(sid, self.sessions)                                # remove_online_user_with_password
        self.assertEqual(self.dropped, [])
        self.assertTrue(await sign_out(self.port, sid, b"pw"))
        self.assertEqual(self.sessions, {})
        self.assertEqual(self.dropped, [sid])
        self.assertFalse(await sign_out(self.port, sid, b"pw"))          # unknown: closed, no TLS
        _, answer = await sign_in(self.port, b"neo", b"pw")              # signs in again at once
        self.assertEqual(answer[0], 0x08)

    def test_ping_sink_liveness(self):
        sink = sps.PingSink()
        self.assertFalse(sink.alive(5))
        sink.datagram_received(struct.pack("<I", 5), ("127.0.0.1", 1))
        self.assertTrue(sink.alive(5))
        sink.last[5] -= sps.SESSION_ALIVE_S + 1
        self.assertFalse(sink.alive(5))


class TestUnknownSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gd = ld.load(None, None)
        self.sessions = {7: "tester"}

    async def serve(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addAsyncCleanup(self._close, server)
        return server.sockets[0].getsockname()[1]

    @staticmethod
    async def _close(server):
        server.close()
        await server.wait_closed()

    def make_lobby(self, fallback=None) -> lobby.LobbyServer:
        return lobby.LobbyServer(self.gd, lobby.Store(None, self.gd, {"money": 1, "premium_money": 1,
                                                                      "skill_points": 1}),
                                 lobby.Matchmaker("127.0.0.1", 25103, 1, 1.0, None), self.sessions,
                                 fallback_account=fallback)

    async def test_lobby_answers_invalid_session_id_and_closes(self):
        srv = self.make_lobby()
        port = await self.serve(srv.handle)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(tcp_frame(pk_sign_in(999)))
        w.write(tcp_frame(bytes([33, 0, 0, 0, 0])))                       # never answered
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.read(), 5), bytes([1, 49]))   # then EOF
        w.close()
        self.assertEqual(srv.store.doc["accounts"], {})
        c = MockLobbyClient(ClientModel())                                # an issued session still works
        await c.connect("127.0.0.1", port, 7)
        await c.pump(lambda m: m.nickname == "tester")
        self.assertEqual(srv.sessions.get(7), "tester")
        srv.drop_session(7)                                               # signed out: closed
        await asyncio.wait_for(c.reader.read(), 5)
        self.assertTrue(c.reader.at_eof())
        await c.close()

    async def test_lobby_fallback_account_when_asked(self):
        srv = self.make_lobby(fallback="Stalker")
        port = await self.serve(srv.handle)
        c = MockLobbyClient(ClientModel())
        await c.connect("127.0.0.1", port, 999)
        await c.pump(lambda m: m.nickname == "Stalker")
        await c.close()

    async def test_chat_closes_unknown_sessions(self):
        srv = chat.ChatServer(self.sessions)
        port = await self.serve(srv.handle)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(tcp_frame(bytes([0xC3]) + struct.pack("<I", 999) + bytes([5])))
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.read(), 5), b"")       # no 0xCB, closed
        w.close()
        c = MockChatClient()
        await c.connect("127.0.0.1", port, 7, expect_name="tester")
        srv.drop_session(7)
        self.assertTrue(await c.closed_by_server())
        await c.close()
        fallback = chat.ChatServer(self.sessions, fallback_account="Stalker")
        port = await self.serve(fallback.handle)
        c = MockChatClient()
        await c.connect("127.0.0.1", port, 999, expect_name="Stalker")
        self.assertEqual(c.m.faults, [])
        await c.close()


if __name__ == "__main__":
    unittest.main()
