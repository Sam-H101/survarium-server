"""Chat tests: tests/chat_mock_client.py (the shipped messaging_client) against chat.ChatServer
with a real lobby.LobbyServer as its roster.

    python -m unittest tests.test_chat -v        (from poc-server/)
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import chat  # noqa: E402
import lobby  # noqa: E402
import lobby_data as ld  # noqa: E402
from chat_mock_client import (GENERAL, MATCH, PRIVATE, SYSTEM, TEAM1, MockChatClient,  # noqa: E402
                              pk_friendship, pk_sign_in, pk_text)
from mock_client import ClientModel, MockLobbyClient, pk_discard, pk_ready, tcp_frame  # noqa: E402
from test_lobby import DICTS  # noqa: E402

SESSIONS = {1: "alice", 2: "bob", 3: "carol", 4: "dave", 5: "erin"}


class ChatTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.gd = ld.load(None, None)
        self.sessions = dict(SESSIONS)
        store = lobby.Store(self.state / "lobby_state.json", self.gd,
                            {"money": 50000, "premium_money": 100, "skill_points": 10})
        for name in self.sessions.values():     # accounts exist once the lobby saw them
            store.account(name)
        mm = lobby.Matchmaker("127.0.0.1", 25103, 1, 0.2, self.state / "match_tickets.json")
        self.lobby = lobby.LobbyServer(self.gd, store, mm, self.sessions)
        self.lobby_srv = await asyncio.start_server(self.lobby.handle, "127.0.0.1", 0)
        self.lobby_port = self.lobby_srv.sockets[0].getsockname()[1]
        self.chat = chat.ChatServer(self.sessions, self.lobby, state_path=self.state / "chat_state.json")
        self.lobby.feed = self.chat.send_feed
        self.server = await asyncio.start_server(self.chat.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients: list[MockChatClient] = []

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        for s in (self.server, self.lobby_srv):
            s.close()
            await s.wait_closed()
        lobby._TICKETS.clear()
        self.tmp.cleanup()

    async def client(self, sid: int) -> MockChatClient:
        c = MockChatClient()
        await c.connect("127.0.0.1", self.port, sid, expect_name=SESSIONS[sid])
        self.clients.append(c)
        return c

    async def settle(self, *clients: MockChatClient) -> None:
        for c in clients:
            await c.pump()

    def aid(self, sid: int) -> int:
        return self.lobby.account_summary(SESSIONS[sid])[0]

    def put_in_match(self, sid: int, match_id: int, team: int) -> None:
        self.lobby.status[SESSIONS[sid]] = lobby.PlayStatus(
            lobby.IN_MATCH, 1, match_id, team, time.monotonic(), "", 0, sid)

    def assertClean(self, *clients: MockChatClient) -> None:
        for c in clients:
            self.assertEqual(c.m.faults, [])


class TestSignIn(ChatTestBase):
    async def test_sign_in_sequence(self):
        c = await self.client(1)
        self.assertEqual(c.sent[0], bytes([0xC3, 1, 0, 0, 0, 5]))
        # after 0xCB: update_channel_subscriptions, query_for_friend_list, query_for_ignore_list
        self.assertEqual(c.sent[1], bytes([0xC5]) + struct.pack("<9I", 0, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0, 0, 0, 0, 0))
        self.assertEqual(c.sent[2:], [bytes([0xC4, 5]), bytes([0xC4, 6])])
        self.assertEqual(c.frames[0], bytes([0xCB, 5]) + b"alice")
        # the online counter (stats channel 8, from the message server), then the two lists
        self.assertEqual(c.frames[1], bytes([0xC9, 4, 0, 0, 0, 0, 6]) + b"System" + bytes([8, 7]) + b"#pc:[1]")
        self.assertEqual(c.frames[2:], [bytes([0xCC, 5, 0, 0]), bytes([0xCC, 6, 0, 0])])
        self.assertEqual(c.m.games_online, 1)
        self.assertEqual(c.m.received, [])                  # neither is shown as chat
        self.assertEqual(c.m.friendship_events, [5, 6])
        self.assertClean(c)

    async def test_unknown_session_uses_fallback_account(self):
        c = MockChatClient()
        await c.connect("127.0.0.1", self.port, 999, expect_name="Stalker")
        self.clients.append(c)
        self.assertClean(c)


class TestLobbyChat(ChatTestBase):
    async def test_general_broadcast_without_echo(self):
        a, b = await self.client(1), await self.client(2)
        self.assertTrue(await a.type_message("/general hello stalkers"))
        await b.pump(lambda m: m.received)
        self.assertEqual(b.m.received[0].channel, GENERAL)
        self.assertEqual(b.m.received[0].sender, "alice")
        self.assertEqual(b.m.received[0].text, "hello stalkers")
        self.assertEqual(b.m.received[0].sender_id, self.aid(1))
        self.assertEqual(b.m.received[0].sender_type, 5)
        await b.type_message("hi alice")                    # no key: default general channel
        await a.pump(lambda m: m.received)
        await self.settle(a, b)
        self.assertEqual([(x.sender, x.text) for x in a.m.received], [("bob", "hi alice")])
        self.assertEqual([x.text for x in b.m.received], ["hello stalkers"])   # no echo
        self.assertEqual([x.text for x in a.m.lines], ["hello stalkers", "hi alice"])  # local echo + b
        self.assertClean(a, b)

    async def test_cp1251_text_and_long_body(self):
        a, b = await self.client(1), await self.client(2)
        await a.type_message("/general привет, сталкер")
        await b.pump(lambda m: m.received)
        self.assertEqual(b.m.received[0].text, "привет, сталкер")
        await a.type_message("/general " + "x" * 300)     # wcstombs_s truncates to 255
        await b.pump(lambda m: len(m.received) == 2)
        self.assertEqual(len(b.m.received[1].text), 254)
        self.assertClean(a, b)

    async def test_private_messages(self):
        a, b, c = await self.client(1), await self.client(2), await self.client(3)
        await a.type_message("/Bob psst")                   # case-insensitive name
        await b.pump(lambda m: m.received)
        self.assertEqual((b.m.received[0].channel, b.m.received[0].sender, b.m.received[0].text),
                         (PRIVATE, "alice", "psst"))
        await b.type_message("/alice back at you")
        await a.pump(lambda m: m.received)
        self.assertEqual((a.m.received[0].channel, a.m.received[0].text), (PRIVATE, "back at you"))
        await a.type_message("/nobody hello?")
        await a.pump(lambda m: len(m.received) == 2)
        self.assertEqual((a.m.received[1].channel, a.m.received[1].sender, a.m.received[1].sender_type),
                         (SYSTEM, "System", 4))
        self.assertIn("nobody", a.m.received[1].text)
        await self.settle(a, b, c)
        self.assertEqual(c.m.received, [])
        self.assertEqual(len(b.m.received), 1)
        self.assertClean(a, b, c)


class TestMatchChat(ChatTestBase):
    async def test_match_and_team_isolation(self):
        # alice + carol: match 1 team 0; bob: match 1 team 1; dave: match 2 team 0; erin: lobby
        for sid, match_id, team in ((1, 1, 0), (3, 1, 0), (2, 1, 1), (4, 2, 0)):
            self.put_in_match(sid, match_id, team)
        cl = {sid: await self.client(sid) for sid in (1, 2, 3, 4, 5)}
        for sid, match_id, team in ((1, 1, 0), (3, 1, 0), (2, 1, 1), (4, 2, 0)):
            await cl[sid].assign_match_channel_order(match_id, team)
            cl[sid].m.in_match = True
        await self.settle(*cl.values())

        self.assertTrue(await cl[1].type_message("/all gg"))
        await cl[2].pump(lambda m: m.received)
        await cl[3].pump(lambda m: m.received)
        self.assertEqual((cl[2].m.received[0].channel, cl[2].m.received[0].text), (MATCH, "gg"))

        self.assertFalse(await cl[1].type_message("/team rush B"))   # the client never sends team
        self.assertTrue(await cl[1].type_message("/squad rush B"))   # squad -> own team
        await cl[3].pump(lambda m: len(m.received) == 2)
        self.assertEqual((cl[3].m.received[1].channel, cl[3].m.received[1].text), (TEAM1, "rush B"))

        await cl[4].type_message("/all other match")
        await self.settle(*cl.values())
        self.assertEqual([x.text for x in cl[2].m.received], ["gg"])           # not team 0, not match 2
        self.assertEqual([x.text for x in cl[3].m.received], ["gg", "rush B"])
        self.assertEqual(cl[4].m.received, [])
        self.assertEqual(cl[5].m.received, [])
        self.assertEqual(cl[1].m.received, [])
        self.assertEqual([x.text for x in cl[1].m.lines], ["gg", "rush B", "rush B"])  # local echoes
        self.assertClean(*cl.values())

    async def test_match_chat_after_real_matchmaking_uses_profile_names(self):
        lob = []
        for sid in (1, 2):
            lc = MockLobbyClient(ClientModel(DICTS))
            await lc.connect("127.0.0.1", self.lobby_port, sid)
            await lc.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles), timeout=10)
            lob.append(lc)
        a, b = await self.client(1), await self.client(2)
        try:
            for lc in lob:
                await lc.send(pk_ready(lc.m.profiles[1]["profile_id"]))
            for lc in lob:
                await lc.pump(lambda m: m.connect_to_match is not None, timeout=10)
            for c, lc in ((a, lob[0]), (b, lob[1])):
                _, _, match_id, team = lc.m.connect_to_match
                await c.assign_match_channel_order(match_id, team)
                c.m.in_match = True
            await a.type_message("/all good luck")
            await b.pump(lambda m: m.received)
            # the sender name is alice's profile in this match (get_player_team looks it up)
            self.assertEqual(b.m.received[0].sender, lob[0].m.profiles[1]["name"])
            self.assertNotEqual(b.m.received[0].sender, "alice")
            self.assertEqual(b.m.received[0].channel, MATCH)
            self.assertClean(a, b)
        finally:
            for lc in lob:
                await lc.close()

    async def test_match_text_outside_a_match_is_dropped(self):
        a, b = await self.client(1), await self.client(2)
        await a.send(pk_text(7, b"", MATCH, b"stale channel"))
        await self.settle(a, b)
        self.assertEqual(b.m.received, [])

    async def test_without_lobby_match_routes_by_subscription(self):
        srv = chat.ChatServer(self.sessions)                # no roster attached
        s = await asyncio.start_server(srv.handle, "127.0.0.1", 0)
        port = s.sockets[0].getsockname()[1]
        cs = []
        try:
            for sid, match_id in ((1, 9), (2, 9), (3, 0xFFFFFFFF)):
                c = MockChatClient()
                await c.connect("127.0.0.1", port, sid, expect_name=SESSIONS[sid])
                await c.assign_match_channel_order(match_id, 0)
                c.m.in_match = True
                cs.append(c)
            await self.settle(*cs)
            await cs[0].type_message("/all hi")
            await cs[1].pump(lambda m: m.received)
            await self.settle(*cs)
            self.assertEqual(cs[2].m.received, [])
            self.assertClean(*cs)
        finally:
            for c in cs:
                await c.close()
            s.close()
            await s.wait_closed()


class TestLobbyFeeds(ChatTestBase):
    async def lobby_client(self, sid: int) -> MockLobbyClient:
        lc = MockLobbyClient(ClientModel(DICTS))
        await lc.connect("127.0.0.1", self.lobby_port, sid)
        await lc.pump(lambda m: len(m.profiles) == 3 and all("slots" in p for p in m.profiles), timeout=10)
        return lc

    async def queue(self, lc: MockLobbyClient, c: MockChatClient) -> str:
        """Play: the pushed state shows the window (show_match_making restarts the movie);
        the feed starts once the client polls its state a second later."""
        await lc.send(pk_ready(lc.m.profiles[1]["profile_id"]))
        await lc.pump(lambda m: m.status in (2, 3))
        if lc.m.status == 2:
            c.show_match_making()
        return lc.m.profiles[1]["name"]

    async def test_online_counter(self):
        a = await self.client(1)
        self.assertEqual(a.m.games_online, 1)
        b = await self.client(2)
        self.assertEqual(b.m.games_online, 2)
        await a.pump(lambda m: m.games_online == 2)        # coalesced broadcast (<= 1 s)
        b2 = await self.client(2)                            # a second connection: same account
        self.assertEqual(b2.m.games_online, 2)
        await b.close()
        await b2.close()
        self.clients.remove(b)
        self.clients.remove(b2)
        await a.pump(lambda m: m.games_online == 1)
        self.assertEqual(a.m.received, [])
        self.assertClean(a)

    async def test_match_making_window(self):
        mm = self.lobby.mm
        mm.match_size, mm.delay = 3, 60.0                    # nobody is matched by the timer
        lob = {sid: await self.lobby_client(sid) for sid in (1, 2, 3)}
        cl = {sid: await self.client(sid) for sid in (1, 2, 3)}
        try:
            na = await self.queue(lob[1], cl[1])
            await cl[1].pump(lambda m: m.mm_place == "1/3")
            self.assertEqual(cl[1].m.mm_teams, ([na], []))
            self.assertEqual(cl[1].m.match_making[0], f"#+p:[ {na} ]#t:[0]")

            nb = await self.queue(lob[2], cl[2])
            await cl[1].pump(lambda m: m.mm_place == "2/3")
            self.assertEqual(cl[1].m.mm_teams, ([na], [nb]))
            await cl[2].pump(lambda m: m.mm_place == "2/3")   # after bob's first poll
            self.assertEqual(cl[2].m.mm_teams, ([na], [nb]))

            await lob[2].send(pk_discard(lob[2].m.order_id))   # bob leaves the queue
            await lob[2].pump(lambda m: m.status == 0)
            await cl[1].pump(lambda m: m.mm_place == "1/3")
            self.assertEqual(cl[1].m.mm_teams, ([na], []))
            self.assertIn(f"#-p:[ {nb} ]", cl[1].m.match_making)

            await self.queue(lob[2], cl[2])
            await cl[2].pump(lambda m: m.mm_place == "2/3")
            nc = await self.queue(lob[3], cl[3])               # the third player: match formed
            for sid in (1, 2, 3):
                await lob[sid].pump(lambda m: m.connect_to_match is not None)
            teams = {sid: lob[sid].m.connect_to_match[3] for sid in (1, 2, 3)}
            self.assertEqual(teams, {1: 0, 2: 1, 3: 0})       # alternate within the match
            # the waiting windows end with the final roster (shown while the level loads)
            for sid in (1, 2):
                await cl[sid].pump(lambda m: m.mm_place == "3/3")
                self.assertEqual(cl[sid].m.mm_teams, ([na, nc], [nb]))
            self.assertEqual(cl[3].m.match_making, [])          # matched before its first poll
            await self.settle(*cl.values())
            for c in cl.values():
                self.assertEqual(c.m.received, [])
            self.assertClean(*cl.values())
            self.assertEqual(self.lobby.feed_views, {})
        finally:
            for lc in lob.values():
                await lc.close()

    async def test_waiting_player_loses_a_disconnected_one(self):
        mm = self.lobby.mm
        mm.match_size, mm.delay = 4, 60.0
        lob = {sid: await self.lobby_client(sid) for sid in (1, 2)}
        a = await self.client(1)
        try:
            na = await self.queue(lob[1], a)
            nb = await self.queue(lob[2], MockChatClient())
            await a.pump(lambda m: m.mm_teams == ([na], [nb]))
            await lob[2].close()                                # bob's lobby connection dies
            await a.pump(lambda m: m.mm_teams == ([na], []) and m.mm_place == "1/4")
            self.assertClean(a)
        finally:
            for lc in lob.values():
                await lc.close()

    def test_feed_lines_fit_the_client_buffers(self):
        sent = []
        self.lobby.feed = lambda account, channel, text: sent.append((channel, text))
        view = lobby.FeedView(1)
        long_name = "[x]#" + "y" * 40
        self.lobby.send_feed_diff("alice", view, {lobby.feed_name(long_name): 1}, "12/20")
        c = MockChatClient()
        for channel, text in sent:
            self.assertEqual(channel, 7)
            c.on_match_message_arrived(text)
        self.assertEqual(c.m.faults, [])
        self.assertEqual(c.m.mm_teams, ([], ["_x__" + "y" * 27]))
        self.assertEqual(c.m.mm_place, "12/20")
        self.lobby.send_feed_diff("alice", view, {}, "0/20")
        for _, text in sent[2:]:
            c.on_match_message_arrived(text)
        self.assertEqual(c.m.mm_teams, ([], []))


class TestFriends(ChatTestBase):
    async def test_find_add_ignore(self):
        a, b = await self.client(1), await self.client(2)
        await a.send(pk_friendship(4, b"bo"))
        await a.pump(lambda m: m.found)
        self.assertEqual(a.m.found, [(self.aid(2), "bob")])
        await a.send(pk_friendship(0, self.aid(2)))            # add_friend -> '4' -> re-query
        await a.pump(lambda m: m.friends)
        self.assertEqual(a.m.results, [(0, ord("4"))])
        self.assertEqual(a.m.friends, [(self.aid(2), "bob", True)])
        await a.send(pk_friendship(7))                          # update_friends_status
        await a.pump()
        await a.send(pk_friendship(0, 12345))                   # unknown account
        await a.pump(lambda m: len(m.results) == 2)
        self.assertEqual(a.m.results[1], (0, ord("0")))
        # bob ignores alice: her privates are dropped server-side
        await b.send(pk_friendship(2, self.aid(1)))
        await b.pump(lambda m: m.ignores)
        self.assertEqual(b.m.ignores, [(self.aid(1), "alice")])
        await a.type_message("/bob hello?")
        await a.type_message("/general everyone")
        await self.settle(a, b)
        self.assertEqual([x.text for x in b.m.received], [])   # general dropped by the client filter
        self.assertClean(a, b)
        saved = json.loads((self.state / "chat_state.json").read_text())
        self.assertEqual(saved["friends"], {"alice": ["bob"]})
        self.assertEqual(saved["ignores"], {"bob": ["alice"]})
        # offline friend
        await b.close()
        self.clients.remove(b)
        await asyncio.sleep(0.1)
        await a.send(pk_friendship(5))
        await a.pump(lambda m: m.friends and not m.friends[0][2])


class TestRobustness(ChatTestBase):
    async def test_reconnect(self):
        a, b = await self.client(1), await self.client(2)
        await a.close()                                         # on_error -> retry 3 s later
        self.clients.remove(a)
        a2 = await self.client(1)
        await b.type_message("welcome back")
        await a2.pump(lambda m: m.received)
        self.assertEqual(a2.m.received[0].text, "welcome back")
        # a half-dead old socket: the new sign-in of the same session replaces it
        a3 = await self.client(1)
        self.assertTrue(await a2.closed_by_server())
        await b.type_message("again")
        await a3.pump(lambda m: m.received)
        self.assertEqual([x.text for x in a3.m.received], ["again"])
        self.assertClean(b, a3)

    async def test_malformed_packets_do_not_crash(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        junk = [
            bytes([0xC1, 1, 2]),                                # before sign-in
            bytes([0xC3, 1]),                                   # truncated sign-in
            pk_sign_in(1),
            bytes([0xC1, 0, 0, 0, 0, 200, 1]),                  # string past the end
            bytes([0xC5, 1, 2, 3]),                             # short subscriptions
            bytes([0xC4]),                                      # no action
            bytes([0xC4, 0]),                                   # add_friend without id
            bytes([0xC4, 4, 50]),                               # find_players, string past end
            bytes([0xC4, 99]),
            bytes([0x00, 0xFF, 0x13]),
            bytes([0xC1]) + b"\xff" * 400,                      # u16-framed garbage
        ]
        for p in junk:
            w.write(tcp_frame(p))
        w.write(b"\x00\x00\x00")                                # zero-length u16 frame
        await w.drain()
        self.assertEqual(await asyncio.wait_for(r.readexactly(8), 3), bytes([7, 0xCB, 5]) + b"alice")
        w.write(b"\x00\xff\xff" + b"\x01" * 10)                 # truncated frame, then hang up
        await w.drain()
        w.close()
        await asyncio.sleep(0.1)
        a, b = await self.client(1), await self.client(2)       # server still serves
        await a.type_message("still alive")
        await b.pump(lambda m: m.received)
        self.assertEqual(b.m.received[0].text, "still alive")
        self.assertClean(a, b)


if __name__ == "__main__":
    unittest.main()
