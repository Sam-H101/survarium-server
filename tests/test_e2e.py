"""End-to-end: start survarium_poc_server.py as a subprocess on spare ports, then replay
the shipped client's whole path: TLS sign-in (login_client_impl_sign_in.cpp), keep-alive
UDP, HTTP server browser (network_client.cpp), lobby (mock_client.py) and Play -> op 51.

    python -m unittest tests.test_e2e -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from chat_mock_client import MATCH, MockChatClient  # noqa: E402
from mock_client import ClientModel, MockLobbyClient, pk_ready  # noqa: E402
from test_lobby import DICTS  # noqa: E402

SSL_DIR = ROOT.parent / "game" / "resources" / "ssl"


def free_port(kind=socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def sign_in(port: int, account: str, password: str, crt: Path) -> tuple[str, str, int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(bytes([1, len(account)]) + account.encode() + b"0.100b\0\0")
    await writer.drain()
    assert await reader.readexactly(1) == bytes([0x0B]), "expected valid_user_name"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(crt)          # the client pins resources/ssl/survarium_login_server.crt
    ctx.check_hostname = False
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    tls_version = "default"
    try:                                    # the client is OpenSSL 1.0.0g: TLS 1.0 only
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1
        tls_version = "TLSv1"
    except (ValueError, ssl.SSLError):
        pass
    try:
        await writer.start_tls(ctx)
    except ssl.SSLError:
        if tls_version != "TLSv1":
            raise
        writer.close()                      # local OpenSSL refuses TLS 1.0: retry with defaults
        return await _sign_in_default(port, account, password, crt)
    return await _finish_sign_in(reader, writer, password, tls_version)


async def _sign_in_default(port, account, password, crt):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(bytes([1, len(account)]) + account.encode() + b"0.100b\0\0")
    await writer.drain()
    assert await reader.readexactly(1) == bytes([0x0B])
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(crt)
    ctx.check_hostname = False
    await writer.start_tls(ctx)
    return await _finish_sign_in(reader, writer, password, "default")


async def _finish_sign_in(reader, writer, password, tls_version):
    writer.write(bytes([len(password)]) + password.encode())
    await writer.drain()
    answer = await reader.read(70)          # one 70-byte read_some
    writer.close()
    assert answer[0] == 0x08, f"expected servers_connection_info, got {answer.hex()}"
    n = answer[1]
    browser = answer[2:2 + n].decode()
    m = answer[2 + n]
    query = answer[3 + n:3 + n + m].decode()
    sid = struct.unpack_from("<I", answer, 3 + n + m)[0]
    return browser, query, sid, tls_version


async def http_get(port: int, path: str) -> str:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200"), head
    return body.decode()


@unittest.skipUnless((SSL_DIR / "survarium_login_server.key").is_file(), "game ssl dir missing")
class TestEndToEnd(unittest.IsolatedAsyncioTestCase):
    chat_enabled = True

    def server_extra_args(self) -> list[str]:
        return ["--no-match-server"]

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ports = {"login": free_port(), "udp": free_port(socket.SOCK_DGRAM),
                      "http": free_port(), "lobby": free_port(), "match": free_port(socket.SOCK_DGRAM),
                      "chat": free_port()}
        self.log = open(Path(self.tmp.name) / "server.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "survarium_poc_server.py"), "--ssl-dir", str(SSL_DIR),
             "--host", "127.0.0.1", "--login-port", str(self.ports["login"]), "--udp-port", str(self.ports["udp"]),
             "--http-port", str(self.ports["http"]), "--lobby-port", str(self.ports["lobby"]),
             "--chat-port", str(self.ports["chat"]),
             "--state-dir", str(Path(self.tmp.name) / "state"), "--matchmaking-delay", "0.3",
             *self.server_extra_args()],
            stdout=self.log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.ports["lobby"]), timeout=0.5):
                    break
            except OSError:
                assert self.proc.poll() is None, self.server_log()
                await asyncio.sleep(0.2)
        else:
            self.fail("server did not start\n" + self.server_log())

    def server_log(self) -> str:
        self.log.flush()
        return (Path(self.tmp.name) / "server.log").read_text()

    async def asyncTearDown(self):
        self.proc.terminate()
        self.proc.wait(10)
        self.log.close()
        self.tmp.cleanup()

    async def test_login_browser_lobby_play(self):
        crt = SSL_DIR / "survarium_login_server.crt"
        browser, query, sid, tls = await sign_in(self.ports["login"], "e2e_user", "secret", crt)
        self.assertEqual(browser, "127.0.0.1")
        print(f"\n  signed in over {tls}: session {sid}, browser {browser}, query {query}")

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:   # keep-alive
            udp.sendto(struct.pack("<I", sid), ("127.0.0.1", self.ports["udp"]))

        lobby_addr = await http_get(self.ports["http"], f"{query}&type=2&local_ip=127.0.0.1&login_ip=127.0.0.1")
        chat_addr = await http_get(self.ports["http"], f"{query}&type=4&local_ip=127.0.0.1&login_ip=127.0.0.1")
        self.assertEqual(lobby_addr, f"127.0.0.1:{self.ports['lobby']}")
        if not self.chat_enabled:
            self.assertEqual(chat_addr, "x:0")
        else:
            self.assertEqual(chat_addr, f"127.0.0.1:{self.ports['chat']}")

        c = MockLobbyClient(ClientModel(DICTS))
        chat = MockChatClient()
        try:
            await c.connect("127.0.0.1", self.ports["lobby"], sid)
            await c.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles)
                         and m.skills_tree is not None and m.reputations, timeout=10)
            self.assertEqual(c.m.nickname, "e2e_user")      # session -> account from the login
            if self.chat_enabled:                           # chat is resolved once the lobby is up
                host, _, port = chat_addr.partition(":")
                await chat.connect(host, int(port), sid, expect_name="e2e_user")
                self.assertEqual(chat.m.friendship_events, [5, 6])
            await c.send(pk_ready(c.m.profiles[0]["profile_id"]))
            await c.pump(lambda m: m.connect_to_match is not None, timeout=10)
            self.assertEqual(c.m.connect_to_match, ("127.0.0.1", 25103, 1, 0))
            if self.chat_enabled:                           # op 51 -> assign_match_channel_order
                await chat.assign_match_channel_order(1, 0)
                chat.m.in_match = True
                self.assertTrue(await chat.type_message("/all gl hf"))
                self.assertEqual(chat.sent[-1][:7], bytes([0xC1, 1, 0, 0, 0, 0, MATCH]))
                await chat.pump()
                self.assertEqual(chat.m.faults, [])
        finally:
            await c.close()
            await chat.close()
        tickets = json.loads((Path(self.tmp.name) / "state" / "match_tickets.json").read_text())
        self.assertEqual(tickets[str(sid)]["account"], "e2e_user")
        self.assertIn("lobby sign in session_id=%d account='e2e_user'" % sid, self.server_log())
        if self.chat_enabled:
            log = self.server_log()
            self.assertIn("chat sign in session_id=%d type=5 account='e2e_user'" % sid, log)
            self.assertIn("chat match 1 'e2e_user': gl hf (0 recipients)", log)


class TestNoChat(TestEndToEnd):
    """--no-chat restores the browser's x:0 (the client then never dials chat)."""
    chat_enabled = False

    def server_extra_args(self) -> list[str]:
        return ["--no-match-server", "--no-chat"]


class TestFullStack(TestEndToEnd):
    """One server process with its in-process match server: login -> lobby Play -> op 51 ->
    UDP match handshake -> spawn as the lobby's character -> controllable."""

    def server_extra_args(self) -> list[str]:
        return ["--match-server", f"127.0.0.1:{self.ports['match']}"]

    async def test_login_browser_lobby_play(self):   # covered by the parent class
        pass

    async def test_play_into_match(self):
        from match.game_data import GameData
        from match_mock_client import MockMatchClient

        crt = SSL_DIR / "survarium_login_server.crt"
        _, query, sid, _ = await sign_in(self.ports["login"], "stack_user", "secret", crt)
        lobby_addr = await http_get(self.ports["http"], f"{query}&type=2&local_ip=127.0.0.1&login_ip=127.0.0.1")
        self.assertEqual(lobby_addr, f"127.0.0.1:{self.ports['lobby']}")

        c = MockLobbyClient(ClientModel(DICTS))
        try:
            await c.connect("127.0.0.1", self.ports["lobby"], sid)
            await c.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles), timeout=10)
            profile_name = c.m.profiles[0]["name"]
            await c.send(pk_ready(c.m.profiles[0]["profile_id"]))
            await c.pump(lambda m: m.connect_to_match is not None, timeout=10)
            host, port, _, _ = c.m.connect_to_match
        finally:
            await c.close()
        self.assertEqual((host, port), ("127.0.0.1", self.ports["match"]))

        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        m = MockMatchClient(lambda d: sock.sendto(d, (host, port)), sid, GameData(), 200)
        t0 = loop.time()
        now = lambda: int((loop.time() - t0) * 1000) + 1000
        m.connect(now())
        try:
            deadline = loop.time() + 20
            while loop.time() < deadline and not (m.controllable and m.inputs_sent >= 30):
                while True:
                    try:
                        data, _ = sock.recvfrom(4096)
                    except (BlockingIOError, ConnectionResetError):
                        break
                    m.datagram_received(data)
                m.tick(now())
                await asyncio.sleep(0.01)
        finally:
            sock.close()
        self.assertEqual(m.faults, [])
        self.assertTrue(m.controllable, f"status={m.game_status} synced={m.time_synced}\n" + self.server_log())
        local = [p for p, is_local in m.profiles if is_local]
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0].name, profile_name)        # the lobby's character, not the fallback
        self.assertTrue({7, 10} & set(local[0].slots))       # spawned with a weapon
        print(f"\n  session {sid} played into match as {profile_name!r}, slots {sorted(local[0].slots)}")


if __name__ == "__main__":
    unittest.main()
