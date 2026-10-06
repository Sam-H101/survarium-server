# Survarium v0.100b PoC server

Local server for the original 2013 client. The first sign-in of an account name creates
the account with that password (see Accounts below). The lobby then serves characters, inventory and equipment, the shop, skills, and Play
(matchmaking, then `connect_to_match_server`). Chat (lobby, match, squad/team and private
messages, friends and ignore lists) runs in the same process. The protocol comes from the
binary-matched `vostok` decompilation. Login, browser and keep-alive are documented in
the `survarium_poc_server.py` docstring; the lobby ops are in the `lobby.py` docstring.

## Run

Double-click `play.bat` (needs Python 3.11+ on PATH), or by hand:

    python survarium_poc_server.py --ssl-dir ..\game\resources\ssl
    cd ..\game\binaries\win32
    survarium.exe -no_splash_screen -client=127.0.0.1:25100

Ports: TCP 25100 (login, TLS 1.0), UDP 25100 (keep-alive), TCP 80 (server browser),
TCP 25101 (lobby), TCP 25102 (chat), UDP 25103-25106 (match). Play sends the client to
`--match-server` (default `127.0.0.1:25103`; the host must be a numeric IPv4 address). The
match server (`match/`, protocol in `docs/match_protocol.md`) runs in `--match-workers`
processes started by the server, one UDP port each from 25103 up (see Scaling); pass
`--match-workers 0` to run it inside the main process, or `--no-match-server` to run it
separately with `python -m match`. Allow Python through Windows Firewall if prompted.

Match (M3): Play queues you; the lobby forms a match with a fixed roster (teams
alternate) and sends everyone to the match server, which loads `level_03` in mode
`gather_victory_items`:

- Everybody sees everybody: same roster and profiles on every client, remote players
  streamed with 0x82 at 30 Hz, late joiners and reconnects handled, a player who leaves
  is hidden.
- Combat is server-authoritative: fire is derived from the input bits and the weapon's
  rate of fire, magazine and reload; hits are traced on the server and applied with the
  client's own damage formula (armour included); kill feed, K/D, respawn after
  `--respawn-time` at a team spawn point. Friendly fire is off unless `--friendly-fire`.
- Shots follow the client: the view pitch comes from the look animation, each weapon's
  recoil is replayed (it turns the camera, not the reported view), and every pellet gets
  the weapon's dispersion drawn from the same PRNG seeds the client got in 0x84. Walls,
  terrain and props of `level_03` stop bullets; glass, foliage and other thin materials
  let them through as in the client. Respawns prefer points no living enemy can see.
  Details: `docs/match_protocol.md` section 11.6.
- Rounds: waiting for players (up to `--join-timeout`), a final countdown, then the
  match timer. Walk to a victory item and press *use* to pick it up, *use* at your
  team's container to store it, *use* at the enemy container to steal its last item; a
  carrier who dies drops the item. The team that stores all `--victory-items` wins,
  otherwise the higher score when the timer ends. The client then returns to the lobby
  and can Play again.

Equipment: the loadout of the profile you press Play with (both weapons and their ammo, armour,
quick slots, artefacts) is what the match sends; keys 1/2 switch weapon, and the server keeps
other clients in step (`docs/match_protocol.md` section 13). One unusable item is dropped from a
ticket instead of replacing the whole loadout with the AK-74u default. The retail client has no
weapon upgrade/attachment protocol: the only addon is the rem_700's scope, baked into its weapon
config, so the lobby neither sells nor equips scopes (section 13.3). Weight: a weapon the
server fills with ammunition gets only what fits the 30 kg the inventory shows, like the
client's own autofill; heavier loadouts are allowed unless `--weight-limit` (section 13.4).

Progression (`docs/match_protocol.md` section 14): a finished match pays every player experience,
money and faction reputation (`data/progression.json`, `--reward-scale`); levels grant skill points
and reputation unlocks weapons in the shop. The shop lists two traders, Scavengers and Black Market,
and sells every weapon: the locked ones show the lock icon, a buy is denied until the reputation is
there, and after a match the server pushes the new money, experience, reputation and unlocked price
lists to the open client. Ladder: Fort-17, TT-33, TOZ-34 free; TOZ-66 at Scavengers 250, Remington 870
at 400; AK-74u, TOZ-122 free; Uzi and Magnum at Black Market 500, Vityaz and Remington 700 at 800 (about 3 to
11 matches). A match is worth roughly 200-400 experience, 250-950 money and 40-90 reputation per trader.

Items (`docs/match_protocol.md` section 11.8): booby traps are placed when the quick-slot key
is released (look at the ground within about 1.2 m), fire on an enemy who steps on them
(broken legs), and are disarmed by an enemy or their owner holding *use* for 5 s while
crouched and looking at them, or by shooting them; drugs (medkit, bandages, painkiller with
its damage protection), the lifebone artefact (no broken limbs; its key restores hands and
legs) and the oxygen tank (back-slot key) work as in the client's item code. What a player
fires and uses is gone for the rest of the match and is taken out of the account afterwards.

Known limitations: players are simple capsules (not hit boxes) and bullets fly straight
(no drop or travel time, a ricochet ends the shot); movement is trusted, not simulated;
scopes are not simulated; the server has no stamina, carried weight, movement-speed or
anomaly model, so boosters 4, 5, 6 and 8 and the drugs' stamina regeneration change nothing
on the server, and the oxygen tank and anomaly booster protect only against damage the
server never deals. Trap placement and triggering use the bullet collision and a foot
radius instead of the client's walker collision and physics sensor; drug healing is spread
per server tick; a spawn fills the magazines without taking those rounds from the stack.

Level collision: the server loads `match/data/level_03.collision` (37 MB, built from the
game data, coverage in `match/data/level_03.collision.json`). It is extracted from the game's
own files, so it is not in the repository: build it from your own install with

    python match/tools/build_level_collision.py

(reads `..\game_data\extracted`, about 15 s). Without the file the server logs a warning
and bullets ignore walls. `python -m match` options `--no-world-collision`,
`--no-dispersion`, `--no-recoil`, `--spread-growth` (apply the configs'
`one_shoot_dispersion_amount`, which the retail client zeroes) and `--client-terrain`
(terrain lets bullets through like the client's `grass` material) switch the parts off;
in-process they are `MatchConfig` fields. `match/tools/extract_view_animation_angles.py`
regenerates `match/data/view_animation_angles.json` (camera angles of the look and recoil
animations).

Chat (`chat.py`, spec in `docs/match_protocol.md` section 12): the server browser
answers the client's `type=4` query with `<public-host>:25102`, and the client connects
once the lobby is up (it retries every 3 s after a failure, without affecting the lobby).

- Lobby chat: the *general* tab reaches every signed-in player.
- Private: type `/Name text` (the name is the other player's account name).
- In a match: the *all* tab (`/all`) reaches everyone in your match, shown with your
  character name. The game's own *team* tab never sends anything (the client returns
  before sending, `messaging_client_process_messagess.cpp:165-167`); type `/squad text`
  to reach only your team. In the game view the client shows only match/team lines;
  lobby and private lines appear once you are back in the lobby.
- Friends, ignore list and player search (lobby menu) work and are saved in
  `state/chat_state.json`. When a friend signs in or out, your list is redrawn with the
  new online flag (at once, or within the client's 10 s friends timer, spec 12.6). The
  client only knows online/offline: a friend in a match shows as online.
- Lobby status lines (spec 12.8), carried by chat and never shown as chat: while you
  wait in the match-making window it lists the players you would be matched with in
  their team columns and `n/size` waiting (channel 7, `#+p`/`#-p`/`#q`); the status
  panel shows how many players are online (channel 8, `#pc`). Without chat
  (`--no-chat`) neither is sent.
- Text is relayed in the sender's ANSI code page (Windows-1251 on a Russian system).

| Option | Default | Meaning |
|---|---|---|
| `--chat-port` | `25102` | Chat TCP listen port. |
| `--chat-address` | `<public-host>:<chat-port>` | `host:port` the browser hands out for chat. |
| `--no-chat` | | No chat server; the browser answers `x:0` and the client never dials. |

Lobby and match options (see `--help`):

| Option | Default | Meaning |
|---|---|---|
| `--match-server`, `--match-id` | `<public-host>:25103`, `1` | Where op 51 sends the player; id of the first match. |
| `--match-size` | `2` | A match starts as soon as this many players are queued. |
| `--min-players`, `--matchmaking-delay` (`--fill-timeout`) | `1`, `20` | After the first queued player has waited this many seconds, start with everyone queued if at least `--min-players`. Use `--match-size 1` to play alone without waiting. |
| `--match-timeout` | `60` | A player the match server never saw this long after op 51 is put back in the menu. While the match server reports the player connected, a lobby (re)sign-in answers state 3 (in match). |
| `--match-time`, `--respawn-time`, `--victory-items` | `600`, `10`, `3` | Match rules (seconds, seconds, items to win). |
| `--join-timeout`, `--friendly-fire` | `60` | Wait for the whole roster at most this long; allow team damage. |
| `--state-dir` | `state/` | Holds `lobby_state.json` (accounts), `match_tickets.json` and `chat_state.json` (friends). |
| `--no-persist` | | Keep state in memory only. |
| `--game-data` | `..\game_data\extracted` | Read `gameplay/db_static_dictionaries`. If it is missing, a built-in snapshot is used. |
| `--start-money`, `--start-premium`, `--start-skill-points` | `10000`, `100`, `10` | Starting values for a new account. |
| `--progression`, `--reward-scale` | `data/progression.json`, `1.0` | Level table and match rewards; `--reward-scale 10` pays ten times as much (test the unlocks quickly). |
| `--no-skills-tree` | | Leave status type 9 unanswered. |
| `--weight-limit` | | Deny equipment moves that take a character over the inventory's 30 kg maximum (the client only shows the total in red; spec 13.4). |

## Two players

**Two clients on one PC** are blocked by the client itself: `survarium.exe` creates the
named mutex `survarium_already_running` and quits if it exists
(`survarium_pc_application_win.cpp:267-279`). Either run the second client in a VM or a
sandbox with its own object namespace (e.g. Sandboxie-Plus), or close the mutex handle of
the first instance before starting the second (Sysinternals:
`handle64 -a -p survarium.exe survarium_already_running`, then
`handle64 -c <handle> -p <pid> -y`). Both clients use the same server and must sign in
with different account names.

**LAN:** on the server PC (say `192.168.1.10`), allow Python through the firewall and run

    python survarium_poc_server.py --ssl-dir ..\game\resources\ssl --public-host 192.168.1.10

`--public-host` is what the clients are told for the server browser, the lobby and the
match server (op 51 needs a numeric IPv4 address). On every PC start

    survarium.exe -no_splash_screen -client=192.168.1.10:25100

Ports: TCP 25100, 25101, 25102, 80 and UDP 25100, 25103-25106 (one per match worker,
`--match-workers`). Both players press Play; with the
default `--match-size 2` the match starts when the second one queues (or after
`--fill-timeout` seconds with whoever is queued).

## Accounts and sessions

- **Passwords:** the first sign-in of a login name creates the account with that password
  (stored as a salted PBKDF2 hash in `lobby_state.json`); later sign-ins with another
  password get "invalid user name or password". An account saved by an older version
  without a password takes the first one used. Five wrong passwords in a row hold the
  account for 30 s (the client shows "sign in attempt interval violated").
  `--no-passwords` accepts any password.
- **Client version:** the login accepts the version string `0.100b` the client sends;
  anything else gets "invalid version" (`--client-version a,b` or `any` to change).
- **One session per account:** signing in while the account's previous session still
  sends its keep-alive (within 10 s) is refused with "user already signed in"; otherwise
  the new sign-in replaces the old session. Quitting the game signs out (the client sends
  the session and, over TLS, the password), which drops the session at once.
- **Unknown sessions:** the lobby answers a session the login server did not issue with
  `invalid_session_id` and closes (after four tries the client returns to the login
  screen); chat closes such connections. Sessions live in memory, so after a server
  restart every client signs in again. `--accept-unknown-sessions` maps unknown sessions
  to the `--nickname` account instead (tools that skip the login).

## Lobby data

- **Accounts:** an account is created on its first sign-in, keyed by the login
  name. Each account gets 3 characters (the client holds at most 3). Their equipment is
  `player_templates[0..2]`, the real starter loadouts recovered from `survarium.exe`.
  Each account also gets a storage stash.
- **Static tables:** `data/*.json` are copies of `..\game_data\json\` from the data
  workstream:
  - `lobby_static_tables.json`: slot rules and weapon–ammo pairs.
  - `skills_tree.json` / `.bin`: the tree served as status 9.
  - `shop_prices.json`: prices and service prices. These are invented balance, marked as such. The
    weapon rows and the ammunition prices are generated from `lobby_data.WEAPON_OFFERS` /
    `AMMO_PRICES` with `python tools/gen_shop_prices.py` (`--check` for CI).
  - `progression.json`: level table and match rewards (`progression.py`).

  If a file is missing, the lobby falls back to the tables in `lobby_data.py`.
- **Match tickets:** `state/match_tickets.json` is keyed by the login session_id, which
  the client also sends to the match server in 0x40. With `--no-match-server` (a separate
  `python -m match` reads the file) it is written atomically before op 51 is sent;
  otherwise the match server gets the tickets in memory (`lobby.get_match_ticket`, or the
  match worker it is placed on) and the file is written within 0.5 s, off the event loop.
  Tickets of a match are dropped when the match server removes it (others after 6 h).
- **Saving:** `lobby_state.json` (now one compact line) and `chat_state.json` are written
  atomically at most once a second (0.2 s for chat) on a background thread, and at
  shutdown; a burst of 100 new accounts is one write. A hard kill loses at most that last
  second.

## Scaling

**Architecture.** One main process runs a single asyncio loop with the login (TLS 1.0),
the HTTP browser, the UDP keep-alive sink, the lobby with matchmaking, chat, the state
files and the stats. Matches run in `--match-workers` processes (default `auto` =
min(4, CPUs/2); `match/pool.py`, `match/worker.py`). Each worker has its own GIL, event
loop, 30 Hz tick and UDP socket, on `--match-server` port + k (25103, 25104, ...; an
ephemeral port if that one is taken, except for the first). When the lobby forms a match it
places it on the worker with the fewest placed players. The tickets go to that worker
first, and op 51 goes out with that worker's port once the worker has confirmed them (the
client dials whatever port op 51 names, spec 11.7). The workers send session events and
log lines back to the main process. A worker that dies is restarted on its port, and its
players are returned to the menu. Workers exit when the main process is gone. One slow
match can therefore only slow the other matches of its own worker:
`tests/test_concurrency.py` runs 80 ms ticks in a worker while lobby pings stay at a few
ms. `--match-workers 0` keeps the old single-process server, and `--no-match-server` with
`python -m match` still works.

What else changed for load:

- **Windows UDP bug:** after a client closed its socket, the next ICMP "port unreachable"
  made Python 3.12's Proactor UDP transport stop reading for good, and the match server
  went deaf for everyone. Every UDP socket now turns that off (`SIO_UDP_CONNRESET`) and
  has 1 MB buffers.
- **Real 30 Hz:** asyncio on Windows sleeps on a 15.6 ms grid, so the old tick ran every
  ~47 ms (21 Hz). The server now uses a fixed-rate schedule, a 1 ms timer period and a
  precise loop clock (`runtime.py`).
- **Cheaper ticks:** each 0x82 entry is encoded once per tick instead of once per
  recipient, 0x43 is decoded with one `struct` call, and the rewound pose for lag
  compensation is cached per tick. Acks are removed in one pass. The respawn
  line-of-sight search is capped at 6 ms. The level cache is memory-mapped, so the
  workers share its 37 MB.
- **Bounded match state:** a peer not heard from for 3 s gets no new 0x82 (each one is
  reliable and would be resent until the 120 s timeout). An endpoint without a valid
  0x40 is dropped after 15 s, and at most 256 such endpoints exist at once. The
  per-session message logs are capped.
- **TCP robustness:** the listen backlog is 1024, TCP keep-alive finds half-open peers,
  and a lobby/chat frame must complete within 30 s of its length byte. The whole login
  must finish in 30 s (the TLS handshake in 15 s), with at most 64 TLS logins at once and
  `--login-rate`/`--login-burst` attempts per IP. HTTP requests time out after 10 s. A
  chat reader more than 256 KB behind is disconnected (the client reconnects in ~3 s).
  The lobby waits for each client's writes to drain, so a non-reading client only stalls
  itself. `--max-connections` caps all TCP connections.
- **Bounded memory:** tickets are dropped with their match (or after 6 h), login sessions
  are capped at 20 000, menu states of offline accounts are dropped, and the ping-sink
  set is bounded.
- **Logs:** per-query, per-chat-line and per-spawn lines are at DEBUG (`-v` /
  `--log-level DEBUG`). Every other call site is rate-limited (about 20 lines, then 1
  per 2 s, with a "[N similar lines suppressed]" note). Every `--stats-interval` (30 s)
  there is one line: `stats: sessions, lobby, chat, queued, in match; matches, match
  players; tick p50/p99, cycle p99, loop lag p99, worker lag p99; cpu, rss; logins
  (refused)`.

**Limits.** The protocol allows 1..20 players per match (0x81 `players_count`), so
`--match-size` is capped at 20. Measured on a 4-core i7-7700K, with the load generator on
the same machine (table below): 200 players in 20-player matches held the 30 Hz tick
(interval p50 33.0 ms, p99 37 ms), with a worst per-match tick p99 of 2.5 ms and lobby
ping p99 of 15 ms. The whole server used 1.3 cores and 233 MB. Each extra worker costs
~30 MB.

**Load test.** `tools/loadtest.py` starts the server on spare ports and drives N
simulated players with the test mocks. The players are spread over several processes.
Each one does a TLS login, the browser, the lobby, chat, Play, the match (walk at the
nearest enemy and fire bursts with full ballistics), leave, and Play again; meanwhile it
chats and pings the lobby. Example runs:

    python tools/loadtest.py --players 10 50 100                 (from poc-server/)
    python tools/loadtest.py --players 200 --match-size 20 --json out.json
    python tools/loadtest.py --players 50 --server-dir <older copy> --probe-udp-fix

The client side measures login, lobby load, lobby ping, chat delay, Play to op 51, time
to control, the gap between 0x82 ticks, leave and back-to-menu. The server side
(`--stats-file`) gives the per-match tick time, the tick cycle and interval, the loop lag
of the main loop and of the workers, and CPU and RSS of all processes. An older server
without `--stats-file` is measured by wrapping its classes (`--probe`). The script
returns 1 on any client fault or error. Slow test entry point: `set SURV_LOAD_TESTS=1`,
then `python -m unittest tests.test_load -v` (100 players with 10 per match, and 5 x 20).

Before: the original server, with the UDP fix patched in (`--probe-udp-fix`); without it,
every second round failed. After: this version. Both used match size 10, 2 rounds of
20 s, burst logins.

| players | | login p50 | lobby load p99 | ping p99 | chat p99 | 0x82 gap p50/p99 | tick interval p50/p99 | worst match tick p99 | main loop lag p99 | CPU | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 10 | before | 57 ms | 72 ms | 64 ms | 86 ms | 42/96 ms | 37/85 ms | 11.1 ms | 46 ms | 9% | 0 |
| 10 | after | 28 ms | 28 ms | 3 ms | 8 ms | 33/37 ms | 33/42 ms | 0.7 ms | 11 ms | 3% | 0 |
| 50 | before | 210 ms | 407 ms | 40 ms | 87 ms | 48/108 ms | 47/94 ms | 6.2 ms | 49 ms | 33% | 0 |
| 50 | after | 75 ms | 85 ms | 5 ms | 22 ms | 33/37 ms | 33/35 ms | 0.7 ms | 4 ms | 11% | 0 |
| 100 | before | 244 ms | 902 ms | 262 ms | 281 ms | 74/2358 ms | 32/266 ms | 2.1 ms (cycle p99 247 ms) | 245 ms | 81% | 200 |
| 100 | after | 84 ms | 43 ms | 7 ms | 14 ms | 33/45 ms | 33/37 ms | 0.8 ms | 6 ms | 47% | 0 |
| 200, 20/match | after | 130 ms | 57 ms | 15 ms | 39 ms | 33/76 ms | 33/37 ms | 2.5 ms | 12 ms | 129% | 0 |

**Remaining limits.**

- Each worker is one Python thread. A worker holds about 50 players of 20-player
  matches at under 10% of its 33 ms budget. Heavy fire with many wall traces (~0.5 ms
  each) is the most expensive part.
- The lobby, chat and TLS logins share one loop. At 200 players in a burst, login p99 is
  ~300 ms. A chat line to everyone costs O(players), and the cost grows with chat volume.
- The lobby state is one JSON document. It is serialized whole (with the C encoder) at most
  once a second, which takes a few ms per 1000 accounts.
- Windows is the target: there is no uvloop, and there is one UDP port per worker.

## Tests

From `poc-server/`:

    python -m unittest discover -s tests -v

- `tests/mock_client.py` sends the exact bytes from `lobby_client.cpp`. It parses
  replies the way `network_client_lobby.cpp` does, with bounds and buffer checks. It
  also reacts like `lobby_menu.cpp`: follow-up queries and status polls.
- `tests/test_lobby.py` covers entry, shop, inventory, skills, Play and leave-queue,
  persistence, and the built-in fallback data.
- `tests/test_e2e.py` runs the real server process on spare ports. It goes through TLS
  1.0 sign-in, UDP keep-alive, the HTTP browser, the lobby and chat sign-in, and ends at
  Play / op 51 plus a match-channel message, then plays into the match over UDP. A
  `--no-chat` run checks the `x:0` answer.
- `tests/chat_mock_client.py` sends the exact bytes of `messaging_client` (including the
  `/key text` parsing and local echo of `on_message_typed`) and parses replies the way
  `process_incoming_text_message` and the `read_*` helpers do; channel 7 and 8 lines go
  through the `lobby_menu_ui.cpp` parsers, whose buffer overflows are faults. `tests/test_chat.py`
  covers sign-in, lobby broadcast, private messages, match and team isolation (also
  after real matchmaking), friends and ignore lists, reconnect and malformed packets,
  the match-making window feed and the online counter.
- `tests/test_login.py`: passwords (first sign-in, wrong password, legacy accounts,
  restart, the hold after repeated failures), the version check, already signed in,
  sign-out with the client's exact bytes, and the lobby/chat answer to unknown sessions.
- `tests/match_mock_client.py` emulates the client's match handlers and records as a
  fault everything that would crash the original client (spec 4.3 and the M3 rules in
  `docs/match_protocol.md` section 11). `tests/test_match.py` covers transport, codecs and
  the M2 flow; `tests/test_m3.py` covers 2-7 player matches in virtual time (roster,
  remote sync, fire/hit/death/respawn, items, a whole match, loss/reordering) and a real
  lobby -> match -> lobby round trip; `tests/test_reconnect.py` replays the first
  real-client run (lobby reconnect mid-match, second connect with the same session).
  These protocol tests shoot along the exact view without world collision
  (`m3_config`).
- `tests/test_weapons.py`: loadout sanitising, the full equipped loadout in 0x92/0x84 (simulated
  and through the real lobby ticket over UDP), weapon switching (active slot, weapon-2 fire,
  reload and ammo, relayed select bits, late joiner), and the scope guards (not sold, not
  equippable, dropped from tickets).
- `tests/test_progression.py`: the progression rules and data (every weapon is sold by a trader the
  client lists, at a reputation level the faction has; the match server has stats for all of them),
  shop gating through the mock client (locked list, denied buy, capped reputation), buying, equipping
  and the ticket of a newly unlocked weapon over the simulated match network, reward accrual (once
  per match, levels and skill points, the stats chat line, unsolicited money / skills / reputation /
  price answers, an offline client), a whole queued match paying out and unlocking, and every weapon
  firing at its own rate in a match (automatic ones stay automatic).
- `tests/test_ballistics.py`: the level cache (coverage, ground under every respawn
  point, a wall that blocks, an open line, thin materials), rays per tick under the 33 ms
  budget, the client PRNGs, spread statistics per weapon and stance against the
  configs, recoil growth/recovery, and duels over the simulated network with the wall,
  the open line and full ballistics.
- `tests/test_concurrency.py`: 30 concurrent logins through the real server, 5 matches x 4
  shooting players in-process and on two worker processes (tick p99 under budget, no
  client faults, lobby events relayed), a match with 80 ms ticks that must not slow lobby
  pings, the login rate limit, trickling and non-reading TCP peers, the UDP endpoint cap,
  bounded tickets/sessions/state, coalesced saves, and the fast 0x82/0x43 codecs against
  the original encoders.
- `tests/test_load.py`: the long load runs of `tools/loadtest.py` (100 players), skipped
  unless `SURV_LOAD_TESTS=1` (see Scaling).

## Notes

- `game/resources/ssl/*.crt` were re-signed with their original keys because the 2013
  certs expired in 2014. Originals: `*.crt.orig`.
- Russian UI strings are Windows-1251. On a non-Russian system locale labels render
  blank; use Locale Emulator (ru-RU) or set "Language for non-Unicode programs" to Russian.
- The client has no op to create, rename or delete a character, or to add a profile.
  `service_prices` carries such costs, but nothing sends a request for them.
- Chat can be switched off with `--no-chat` (browser answer `x:0`).
- Client log: `%USERPROFILE%\Documents\survarium\survarium_<user>.log`.
