"""Mock of the shipped lobby client: sends the exact bytes lobby_client.cpp builds and
parses answers exactly the way network_client_lobby.cpp / lobby_client.cpp read them,
then reacts the way lobby_menu.cpp does (follow-up queries, status polling).

Every read is bounds-checked like packet_reader::r (ASSERT on overrun), strings are
checked against the client's buffer sizes, and the raw structs are decoded with the
client's MSVC layouts. Anything the real client would choke on raises AssertionError.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import binary_config as bc  # noqa: E402

# ---- client -> server packets (lobby_client.cpp) ------------------------------------------


def tcp_frame(payload: bytes) -> bytes:   # network_core/sources/tcp_packet.cpp
    if len(payload) < 256:
        return bytes([len(payload)]) + payload
    return b"\0" + struct.pack("<H", len(payload)) + payload


def pk_sign_in(sid):                 return bytes([38]) + struct.pack("<I", sid)
def pk_query(t):                     return bytes([33]) + struct.pack("<I", t)
def pk_query_prices(f):              return bytes([33, 6, f & 0xFF])
def pk_query_profile(pid):           return bytes([33, 2]) + struct.pack("<I", pid)
def pk_ready(pid):                   return bytes([32]) + struct.pack("<I", pid)
def pk_discard(order):               return bytes([39]) + struct.pack("<I", order & 0xFFFFFFFF)
def pk_ping(t):                      return bytes([40]) + struct.pack("<I", t)
def pk_reroll():                     return bytes([37, 1])


def pk_move(descrs):
    out = bytes([35, 0, len(descrs) & 0xFF])
    for d in descrs:   # relocate_item_descr::serialize
        out += struct.pack("<IIIIIH", *d)
    return out


def pk_buy(dict_id, count, faction, premium=False):
    return bytes([36, 0]) + struct.pack("<HIBB", dict_id, count, faction, int(premium))


def pk_skills(skills, perks):
    out = bytes([37, 0, len(skills)]) + b"".join(struct.pack("<BB", s, p) for s, p in skills)
    return out + bytes([len(perks)]) + bytes(perks)


# ---- server -> client parsing --------------------------------------------------------------

class Reader:
    """packet_reader: r<T>, r_string with the client's buffer size, eof."""

    def __init__(self, data: bytes):
        self.b, self.p = data, 0

    def r(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        assert self.p + size <= len(self.b), f"read past end ({fmt} at {self.p}/{len(self.b)})"
        v = struct.unpack_from("<" + fmt, self.b, self.p)
        self.p += size
        return v if len(v) > 1 else v[0]

    def raw(self, n: int) -> bytes:
        assert self.p + n <= len(self.b), f"raw read past end ({n} at {self.p}/{len(self.b)})"
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def r_string(self, buffer_size: int) -> str:
        n = self.r("B")
        assert n < 255 and n < buffer_size, f"string of {n} bytes overflows char[{buffer_size}]"
        return self.raw(n).decode("cp1251")

    def eof(self) -> bool:
        return self.p == len(self.b)

    def rest(self) -> bytes:
        return self.raw(len(self.b) - self.p)


def parse_profile(raw: bytes) -> dict:
    assert len(raw) == 0x1B8
    account_id, profile_id, name = struct.unpack_from("<II32s", raw, 0)
    assert b"\0" in name, "profile_name not terminated"
    boosters = [struct.unpack_from("<B3xf", raw, 0x28 + 8 * i) for i in range(11)]
    slots = [struct.unpack_from("<IIIH2x", raw, 0x80 + 16 * i) for i in range(19)]
    team, is_local = struct.unpack_from("<IB", raw, 0x1B0)
    return {"account_id": account_id, "profile_id": profile_id, "name": name.split(b"\0")[0].decode("cp1251"),
            "boosters": boosters, "team": team, "is_local": is_local,
            "slots": {i: {"cond": s[0], "amount": s[1], "id": s[2], "dict_id": s[3]} for i, s in enumerate(slots) if s[2]}}


class ClientModel:
    """The lobby_client data members plus the lobby_menu reactions."""

    def __init__(self, dictionaries: dict | None = None):
        self.dicts = dictionaries        # db_static_dictionaries, for the UI-side lookups
        self.status = 4                  # lobby::unknown
        self.order_id = self.match_id = 0xFFFFFFFF
        self.team = 3
        self.status_message = ""
        self.profiles: list[dict] = []
        self.selected = 0                # lobby_menu::m_selected_profile
        self.inventory: list[dict] = []
        self.prices: dict[int, list[tuple]] = {}
        self.money = self.premium = self.skill_points = 0
        self.nickname = ""
        self.leveling = None
        self.skills: list[tuple[int, int]] = []
        self.perks: list[int] = []
        self.restrictions: list[tuple[int, int]] = []
        self.compat: list[tuple[int, int]] = []
        self.skills_tree: bytes | None = None
        self.service_prices = None
        self.reputations: list[tuple[int, int]] = []
        self.connect_to_match = None
        self.permitted: list[tuple[int, bytes]] = []
        self.denied: list[tuple[int, str]] = []
        self.pings: list[int] = []
        self.log: list[str] = []

    # network_client::on_lobby_packet_received
    def on_packet(self, payload: bytes) -> list[bytes]:
        """Returns the packets the real client sends in reaction (lobby_menu logic)."""
        rd = Reader(payload)
        op = rd.r("B")
        out: list[bytes] = []
        if op == 51:
            host = rd.r_string(64)
            port = rd.r("H")
            self.match_id = rd.r("I")
            self.team = rd.r("B")
            self.status = 3
            self.connect_to_match = (host, port, self.match_id, self.team)
            self.log.append(f"51 {host}:{port} match {self.match_id} team {self.team}")
        elif op == 54:
            kind = rd.r("B")
            out = self.on_status(kind, rd)
        elif op == 52:
            sub = rd.r("B")
            self.log.append(f"52 {sub}")
            if sub == 36:                       # process_shop_action
                if rd.r("B") == 0:
                    dict_id, iid, cond = rd.r("H"), rd.r("I"), rd.r("I")
                    self.check_dict_id(dict_id)
                    for it in self.inventory:
                        if it["id"] == iid:
                            it["cond"] += cond
                            break
                    else:
                        self.inventory.append({"cond": cond, "amount": 0, "id": iid, "dict_id": dict_id})
                    out = [pk_query(7)]
                self.permitted.append((sub, b""))
            elif sub == 37:
                reroll = rd.r("B") == 1
                out = ([pk_query(7)] if reroll else []) + [pk_query(8)]
                self.permitted.append((sub, bytes([reroll])))
            else:                               # lobby_menu::on_operation_permitted_received
                self.permitted.append((sub, b""))
                if sub == 32:
                    out = [("delay", 1.0, pk_query(0))]
                elif sub in (35, 36):
                    out = [pk_query_profile(self.profiles[self.selected]["profile_id"])] if self.profiles else []
        elif op == 53:
            sub = rd.r("B")
            rd.r("B")  # faction_id
            desc = rd.r_string(512)
            self.denied.append((sub, desc))
            self.log.append(f"53 {sub} {desc}")
            if sub in (32, 35, 36, 37):
                out = [("delay", 0.5, pk_query(0))]
        elif op == 55:
            self.pings.append(rd.r("I"))
        else:
            raise AssertionError(f"unknown lobby op {op}")
        if op != 54 or kind not in (0, 9):      # these read optional/trailing data to eof
            assert rd.eof(), f"op {op}: {len(rd.b) - rd.p} unread trailing bytes"
        return out

    def on_status(self, kind: int, rd: Reader) -> list[bytes]:
        out: list[bytes] = []
        if kind == 0:
            self.status = rd.r("B")
            if self.status in (1, 2, 3):
                self.order_id, self.match_id, self.team = rd.r("I"), rd.r("I"), rd.r("B")
            elif self.status == 0:
                self.order_id = self.match_id = 0xFFFFFFFF
                self.team = 3
            self.status_message = "" if rd.eof() else rd.r_string(128)
            assert rd.eof(), "trailing bytes after client_state"
            self.log.append(f"54/0 state {self.status} {self.status_message!r}")
            if self.status == 0 and not self.profiles:
                out = [pk_query(3), pk_query(7), pk_query(8), pk_query(11)]   # query_account_data
            elif self.status == 2:
                out = [("delay", 1.0, pk_query(0))]
            assert self.status in (0, 1, 2, 3), f"unknown client state {self.status}"
        elif kind == 1:
            n = rd.r("B")
            assert n <= 3, f"{n} profiles overflow lobby_client::m_profiles[3]"
            self.profiles = [{"profile_id": rd.r("I"), "name": rd.r_string(32)} for _ in range(n)]
            out = [pk_query_profile(p["profile_id"]) for p in self.profiles]
        elif kind == 2:
            prof = parse_profile(rd.raw(0x1B8))
            for p in self.profiles:
                if p["profile_id"] == prof["profile_id"]:
                    p.update(prof)
                    break
            for s in prof["slots"].values():
                self.check_dict_id(s["dict_id"])
        elif kind == 3:
            n = rd.r("I")
            self.inventory = []
            for _ in range(n):
                cond, amount, iid, dict_id = rd.r("IIIH2x")
                self.check_dict_id(dict_id)
                self.inventory.append({"cond": cond, "amount": amount, "id": iid, "dict_id": dict_id})
            out = [pk_query(1)]
        elif kind == 4:
            n = rd.r("I")
            self.restrictions = [rd.r("BB") for _ in range(n)]
        elif kind == 5:
            n = rd.r("I")
            self.compat = [rd.r("HH") for _ in range(n)]
            for a, b in self.compat:
                self.check_dict_id(a)
                self.check_dict_id(b)
        elif kind == 6:
            faction = rd.r("B")
            assert faction < 16, "m_prices[16] overflow"
            n = rd.r("H")
            self.prices[faction] = [rd.r("HHBx") for _ in range(n)]
            levels = self.faction_levels(faction)       # on_price_items_arrived
            for dict_id, cost, lvl in self.prices[faction]:
                self.check_dict_id(dict_id)
                assert lvl < len(levels), f"price on level {lvl} never shown"
        elif kind == 7:
            self.money, self.premium, self.skill_points = rd.r("I"), rd.r("I"), rd.r("B")
            self.nickname = rd.r_string(32)
        elif kind == 8:
            total, nxt, prev = rd.r("I"), rd.r("I"), rd.r("I")
            self.leveling = (total, nxt, prev)
            self.skills = [rd.r("BB") for _ in range(rd.r("B"))]
            self.perks = list(rd.raw(rd.r("B")))
            assert sum(p for _, p in self.skills) <= self.skill_points, "points_available underflows"
        elif kind == 9:
            self.skills_tree = rd.rest()
            self.check_skills_tree(self.skills_tree)
        elif kind == 10:
            self.service_prices = rd.r("III")
        elif kind == 11:
            self.reputations = [rd.r("BxH") for _ in range(rd.r("B"))]
            for f, _pts in self.reputations:
                self.faction_levels(f)
        else:
            raise AssertionError(f"unknown status type {kind}")
        return out

    # ---- checks of the UI-side lookups the client performs on the data -----------------
    def check_dict_id(self, dict_id: int) -> None:
        if self.dicts is not None:
            assert any(e["dict_id"] == dict_id for e in self.dicts["items_dict"].values()), \
                f"dict_id {dict_id} not in items_dictionary (client crash)"

    def faction_levels(self, faction: int) -> list:
        if self.dicts is None:
            return [0] * 8
        key = f"faction_{faction}"
        assert key in self.dicts["factions_dict"], f"factions_dict has no {key} (R_ASSERT)"
        return self.dicts["factions_dict"][key]["levels"]

    def check_skills_tree(self, blob: bytes) -> None:
        """Replays lobby_menu::fill_skills_tree against the raw blob with the client's
        binary_config_value::operator[] (crc lower_bound + strcmp)."""
        root = RawConfig(blob)
        for branch in range(1, 6):
            sk = root[f"skill_{branch}"]
            skill_id = sk["id"].u32()
            if self.dicts is not None:
                d = self.dicts["skills_dict"][f"skill_{skill_id}"]
                assert {"skill_name", "skill_description", "skill_icon"} <= set(d)
            levels = sk["levels"]
            for i in range(1, levels.size() + 1):
                lvl = levels[f"skill_level_{i}"]
                for b in lvl["boosters"].children():
                    b["value"].f32()
                    bid = b["id"].u32()
                    if self.dicts is not None:
                        assert f"booster_{bid}" in self.dicts["boosters_dict"]
                if lvl.value_exists("perks"):
                    for p in lvl["perks"].children():
                        pid = p["id"].u32()
                        if self.dicts is not None:
                            assert f"perk_{pid}" in self.dicts["perks_dict"]


class RawConfig:
    """binary_config_value semantics over the raw bytes (R_ASSERTs become AssertionError)."""

    def __init__(self, blob: bytes, off: int = 0):
        self.b, self.off = blob, off
        self.data, self.id, self.crc, self.type, self.count = bc._REC.unpack_from(blob, off)

    def children(self) -> list["RawConfig"]:
        assert self.type in (3, 4), "not a table"
        return [RawConfig(self.b, self.data + 24 * i) for i in range(self.count)]

    def size(self) -> int:
        return len(self.children())

    def key(self) -> str:
        return self.b[self.id:self.b.index(b"\0", self.id)].decode("latin-1")

    def _find(self, key: str):
        kids = self.children()
        c = bc.crc(key)
        lo = 0
        while lo < len(kids) and kids[lo].crc < c:     # std::lower_bound on id_crc
            lo += 1
        if lo == len(kids) or kids[lo].crc != c:
            return None
        assert kids[lo].id and kids[lo].key() == key
        return kids[lo]

    def __getitem__(self, key: str) -> "RawConfig":
        r = self._find(key)
        assert r is not None, f"item not found [{key}]"
        return r

    def value_exists(self, key: str) -> bool:
        return self._find(key) is not None

    def u32(self) -> int:
        assert self.type == 1, "cast_number on non-integer"
        return self.data & 0xFFFFFFFF

    def f32(self) -> float:
        assert self.type in (1, 2)
        return struct.unpack("<f", struct.pack("<I", self.data & 0xFFFFFFFF))[0]


class MockLobbyClient:
    """Drives a ClientModel over a real TCP connection."""

    def __init__(self, model: ClientModel):
        self.m = model
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.sent: list[bytes] = []
        self.received: list[bytes] = []
        self._pending: list[asyncio.Task] = []

    async def connect(self, host: str, port: int, session_id: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(host, port)
        await self.send(pk_sign_in(session_id))
        payload = await self.recv_frame()
        assert payload == bytes([48]), f"expected connection_successful, got {payload.hex()}"
        # lobby_menu::query_lobby_info (first time: static info, then state)
        for pk in (pk_query(4), pk_query(5), pk_query(9), pk_query(10)):
            await self.send(pk)
        for f in range(1, 5):
            await self.send(pk_query_prices(f))
        await self.send(pk_query(0))

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)
        self.writer.write(tcp_frame(payload))
        await self.writer.drain()

    async def recv_frame(self, timeout: float = 5.0) -> bytes:
        async def _read():
            n = (await self.reader.readexactly(1))[0]
            if n == 0:
                n = struct.unpack("<H", await self.reader.readexactly(2))[0]
            return await self.reader.readexactly(n)
        return await asyncio.wait_for(_read(), timeout)

    async def pump(self, until=None, timeout: float = 5.0) -> None:
        """Process server packets (and the client's reactions) until `until(model)` holds,
        or until the line has been quiet for 0.3 s when no condition is given."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if until is not None and until(self.m):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                if until is None:
                    return
                raise AssertionError(f"timed out; log tail: {self.m.log[-8:]}")
            try:
                payload = await self.recv_frame(min(remaining, 0.3 if until is None else remaining))
            except asyncio.TimeoutError:
                if until is None and not self._pending_alive():
                    return
                continue
            self.received.append(payload)
            for action in self.m.on_packet(payload):
                if isinstance(action, tuple):           # scheduler-delayed query
                    _, delay, pk = action
                    self._pending.append(asyncio.create_task(self._later(delay, pk)))
                else:
                    await self.send(action)

    def _pending_alive(self) -> bool:
        self._pending = [t for t in self._pending if not t.done()]
        return bool(self._pending)

    async def _later(self, delay: float, pk: bytes) -> None:
        await asyncio.sleep(delay)
        if not self.writer.is_closing():
            await self.send(pk)

    async def close(self) -> None:
        for t in self._pending:
            t.cancel()
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except ConnectionError:
                pass
