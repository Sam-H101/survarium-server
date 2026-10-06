# Survarium v0.100b match protocol

Reverse-engineered from the client only. This spec is for writing a match server for the original 2013 client (`survarium.exe` v0.100b).

* **Source of truth.** The binary-matched decompilation in `vostok/sources/vostok/`. All paths below are relative to that folder. `file:N` means a line in the current tree.
* **Faithfulness.** Ledger column `cur` in `vostok/config/match_state.tsv`. Every function below that is under ~85% was checked against the retail disassembly (`_wsl/mp_dis.py`). Section 10 lists those checks. Where the decompiled source and the retail binary disagree, the spec follows **retail** and says so.
* **Byte order and encodings.** Everything is little-endian x86. Values are raw `memcpy` copies:
  * `packet_reader::r<T>` reads `sizeof(T)` bytes (`network_core/packet_reader_inline.h:25-31`).
  * `packet::append` writes them (`network_core/packet_inline.h:269-353`).
  * `bool` is 1 byte (0 or 1).
  * `float` is IEEE-754 f32. `float2` / `float3` are 2 / 3 consecutive f32 (8 / 12 bytes).
  * An enum read with `r<enum>` is 4 bytes (MSVC `int`). Most enums on the wire are instead read as `u8` / `u32` and cast; each table says which.
* **Strings** (`str`): `u8 length`, then `length` bytes. There is no NUL and no padding (`packet_reader_inline.h:39-46`, `packet_inline.h:347-353`). The reader asserts `length < 255` and copies into a fixed buffer, so every string has a per-field maximum. Text is Windows-1251 (Russian locale).
* **Uncertain items** are marked **[UNCERTAIN: reason]**.

---

## 1. Transport

The match connection is plain UDP with a custom reliability layer, `network_core::udp_match_connection`. The client uses it through `udp_match_client`. The original server used the **same class** through `udp_match_client_session`, with identical parameters (`network_core/udp_match_client_session.h:31-44`).

The simplest correct server is a port of `network_core/sources/udp_match_connection.cpp` plus `udp_match_connection_inline.h`, about 600 lines. Everything in this section describes that class.

### 1.1 Endpoint and socket rules

| Rule | Detail | Cite |
|---|---|---|
| Server address | The lobby op 51 sends a host string. The client parses it with `boost::asio::ip::address::from_string`, so it **must be a numeric IPv4 literal**. No DNS. Max 63 chars. | `network_core/sources/udp_match_client.cpp:115`, `game/sources/network_client_lobby.cpp:40-43` |
| Port | From op 51 (`u16`). The conventional value is 25103 (`match_udp_port`). | `login_server/constants.h:32-33` |
| Client socket | IPv4 UDP, bound to an ephemeral port. | `udp_match_client.cpp:113-114` |
| Reply source | Datagrams must come from **exactly** the IP:port the client sent to. Any other sender makes the client log "unexpected sender" and instantly disconnect (`disconnected_by_connection_lost`). | `udp_match_client.cpp:75-79`, `:39-42` |
| Max datagram | The client receive buffer is 256 bytes (`boost::array<u8,256>`). A longer datagram is truncated or errors. **Keep every datagram at 256 bytes or less.** | `network_core/udp_match_client.h:125,139` |
| Server sessions | The original server keyed one session per remote endpoint and created it on the first datagram. There is no transport-level hello. | `network_core/udp_match_server.h:163-174` |
| Transport "connected" | Set locally as soon as the client starts sending. The **application** handshake (section 2) is separate. | `udp_match_connection.cpp:398-406` |

### 1.2 Datagram layout

The header is 6 bytes. Each packet object reserves 6 header bytes in front of its payload (`udp_match_packet.h:65`, `header_size()` at `:102-105`). The `packet_header_size = 4` enum at `udp_match_packet.h:51` is unused and wrong.

| Off | Type | Name | Meaning |
|---|---|---|---|
| 0 | u16 | `sequence` | Sender's packet sequence number. Starts at 0, +1 per datagram including keepalives. Wraps at 65536. |
| 2 | u16 | `ack` | Highest sequence number received **from the peer** (`m_remote_sequence_id`). 0xFFFF before anything was received. |
| 4 | u16 | `bits` | `bit0` = **multi flag** (1 = bundle / low-level, 0 = single message). `bits >> 1` = ack bitfield, see 1.3. |
| 6 | ... | body | Either one message (single) or a bundle (multi). |

* Writer: `fill_packet_header`, `network_core/sources/udp_match_connection.cpp:133-145`. Its ledger score is 71%; retail was checked and matches field for field.
* Reader: `network_core/udp_match_connection_inline.h:87-90`.

**Single body** (`bit0 = 0`): the body is exactly one message, laid out as in 1.5 (`udp_match_connection_inline.h:106-115`).

**Multi body** (`bit0 = 1`): the body is a sequence of `[u8 len][len bytes]` records until the end of the datagram (`udp_match_connection_inline.h:117-136`). The bundle writer is `udp_match_connection.cpp:172-198`.

* **A multi body with exactly one record is a LOW-LEVEL control message**, not an application message: the condition `if ( i || !reader.eof() )` at `udp_match_connection_inline.h:125`.
* Therefore **never send a single application message in multi form.** Use multi only for 2 or more messages. Otherwise the client reads your message's first byte as a low-level type, and type 0 or anything above 2 means "initiate disconnection".

### 1.3 Sequence numbers and acknowledgements

* **Comparison.** Serial-number arithmetic with a 0x8000 half window (`network_core/sequence_number_inline.h:64-80`): `a < b` iff `(a < b && a + 0x8000 > b) || (b < a && b + 0x8000 <= a)`.
* **Initial values.** Sent sequence 0xFFFF, so the first datagram is 0. `ack` is 0xFFFF and the bitfield is 0 (`udp_match_connection.cpp:37-43`).
* **Receiving datagram `seq`** (`udp_match_connection_inline.h:87-104`):
  * If `seq == last_received_seq`, it is dropped as a duplicate. Only exact repeats are dropped.
  * If `seq > last_received_seq`, ack state is updated (`update_acknowledgements`, `udp_match_connection.cpp:480-533`). The window shifts with `bits = (diff < 16 ? bits >> diff : 0) | 0x8000; last = seq`.
  * Older datagrams are still **processed**. Ordered delivery dedups them (1.5).
* **Ack bitfield semantics.** The internal 16-bit field has bit 15 = `ack` itself and bit `15-k` = `ack - k`. On the wire it is sent as `(field << 1) | multi`. The receiver rebuilds it as `(wire >> 1) | 0x8000` (`udp_match_connection_inline.h:90`). So the wire carries the status of `ack-1 … ack-15`, and `ack` is implicitly acknowledged.
* **Client-side checks on incoming acks**, which a server must satisfy (`udp_match_connection.cpp:492-523`):
  * `ack` greater than the client's last sent sequence: ignored.
  * An older `ack` acknowledges only that one sequence.
  * A newer `ack` is accepted **only if its bitfield is a superset** of the previously accepted bitfield shifted by the difference. A normal "highest received + the 15 before it" ack, built from what actually arrived, satisfies this.
* **The server must ack client datagrams.** Every client application message is reliable (1.5). Without acks the client resends them every 500 ms forever (1.4). Its 8192-packet pool then fills, and after 32768 unacked sequences its send window stalls (`udp_match_connection.cpp:349-360`).
* **Retransmission unit.** A reliable message is resent **inside a new datagram with a new sequence number**, possibly bundled differently (`udp_match_connection.cpp:321`, `move_to_list_predicate.h:29-36`). It is acknowledged when the datagram that carried it is acknowledged (`sequence_id_predicate`, `udp_match_connection.cpp:441-467`).

### 1.4 Timers

All parameters are passed to the constructor at `udp_match_client.cpp:20-29`, and the server's are the same at `udp_match_client_session.h:31-40`. The flow emulator is compiled out in retail: `network_flow_emulator_options()` returns NULL (`game/sources/match_client.cpp:25-28`).

| Parameter | Value | Effect |
|---|---|---|
| `disconnection_timeout_in_ms` | 120000 | No datagram received for 120 s means `disconnected_by_timeout`, and the client returns to the lobby (`udp_match_connection.cpp:290-295`, `game/sources/network_client.cpp:353-367`). The timer only runs **after the first datagram is received** (`m_last_receive_time_in_ms != 0`). |
| `max_packet_wait_time_in_ms` | 500 | Resend interval for unacked reliable messages. Also the linger time in `confirming_disconnection`. |
| `max_idle_time_in_ms` | 33 | If nothing has been sent for 33 ms, send a low-level `continuous_flow` keepalive (`udp_match_connection.cpp:323-332`). |
| UI "connection lost" | 3000 | If nothing has been received for 3 s, the HUD shows "match server connection lost" and pending inputs are discarded. This is UI only (`game/sources/network_client_processing.cpp:601-605`). |
| Client send cadence | 33 ms | `send_queued_packets` runs when packets are pending or every 33 ms (`network_client_processing.cpp:548,593-618`). |

**Server recommendation:** run the same 33 ms send cadence and keepalive. Send something at least every second so the 3 s HUD warning never fires.

### 1.5 Messages, reliability, ordering, channels

A message is `[u8 type][u16 order_id if ordered][payload]` (`construct_packet`, `udp_match_connection_inline.h:9-25`).

* **Every match message in both directions is reliable + ordered on channel 0.** `network_packets_orderer::get_message_type_info` returns `ordered_reliable(0)` for every id (`game_core/network_messages.h:19-33`; `get_received_message_info` is 100% matched). There are no unreliable or unordered match messages. Only one channel exists (`channels_count = 1`, `udp_match_connection.h:205`).
* **Every message therefore carries a u16 `order_id`** right after the type byte.
  * Per direction it starts at **0** and increments by 1 per message (`enqueue_impl`, `udp_match_connection.cpp:408-417`).
  * The receiver expects `received_order_id + 1`, starting from 0xFFFF + 1 = 0 (`udp_match_connection.h:185-197`).
  * It drops ids `<=` the last delivered id or already buffered, buffers future ids, and delivers in order (`udp_match_connection_inline.h:38-70`).
* **A gap in the server's order_ids stalls the client forever.** Never skip an id, and resend until acked.
* **Size limit.** One message, type + order + payload, must be at most 250 bytes. The packet buffer is 256 minus the 6-byte header (`udp_match_packet.h:97,136`), and the receiver copies the post-order bytes into a 250-byte packet (`udp_match_connection_inline.h:52-55`; growth is `UNREACHABLE_CODE`, `udp_match_packet.h:111`). There is **no fragmentation**.
* **Bundling** as the client does it: sort the pending messages by size, take the biggest, then add smaller ones while `size_left > len` (`udp_match_connection.cpp:346-389`). Every bundled message gets the same datagram sequence number. A bundle is `6 + Σ(1 + len_i) ≤ 256`.
* **Client receive throttle.** The network thread queues each received message as one "response". The game thread executes **at most 10 responses per `process_responses` call**, about once per frame (`network/sources/network_world.cpp:70-79`). Bursts are fine; they are drained over a few frames.

### 1.6 Low-level control messages

A low-level message is sent as a multi datagram whose body is exactly `[0x01][type]`. That makes the datagram 8 bytes (`new_low_level_packet`, `udp_match_connection.cpp:252-283`; retail checked). Low-level packets are unreliable, never bundled, and still consume a sequence number. Types are listed at `udp_match_connection.h:128-133`:

| type | name | Receiver action (`process_low_level_message`, `udp_match_connection.cpp:535-565`) |
|---|---|---|
| 0 | `initiate_disconnection` | If connected, enter `confirming_disconnection`. Then send `confirm` every send tick for 500 ms, then drop the connection (`udp_match_connection.cpp:304-311`). Any type other than 1 or 2 also lands here. |
| 1 | `confirm_disconnection` | If we are `initiating_disconnection`, disconnect now (`disconnected_by_initiator`). |
| 2 | `continuous_flow` | Keepalive. No action beyond the header and ack processing. |

**Disconnect from the client side** (the user leaves): the client sends `initiate` on every send tick until it receives a `confirm` (`udp_match_connection.cpp:297-302,639-666`). The server should reply `confirm` for about 500 ms and then free the session.

**Server kick:** send `initiate`. The client confirms, and the server's disconnect then completes. On the client this fires `disconnected_by_initiator`, then `close_current_match(true)`, which also sends the lobby `discard_playing_order` (`network_client.cpp:362-364`, `network_client_lobby.cpp:206-223`).

---

## 2. PRIORITY: player profile (0x92) and spawn (0x84) payloads

All layouts here were checked against retail. Section 10 has the ledger numbers.

### 2.1 Profile slots

`game_core/profile_slot_enum.h:9-29`. The wire mode per slot comes from `slot_serialize_mode[]` (`game_core/game_net_defines.h:34-55`). The values were **confirmed from retail `.rdata` VA 0x9c2290** = `0,0,0,0,0,0,0,0,2,2,0,2,2,2,2,2,2,2,2`.

| id | slot | mode | | id | slot | mode |
|---|---|---|---|---|---|---|
| 0 | helmet | 0 | | 10 | weapon2 | 0 |
| 1 | mask | 0 | | 11 | ammo1_weapon2 | 2 |
| 2 | torso | 0 | | 12 | ammo2_weapon2 | 2 |
| 3 | back | 0 | | 13..18 | quick_slot1..6 | 2 |
| 4 | pants | 0 | | 19 | `max_slots_count` = `invalid_slot` | - |
| 5 | gloves | 0 | | | | |
| 6 | boots | 0 | | | | |
| 7 | weapon1 | 0 | | | | |
| 8 | ammo1_weapon1 | 2 | | | | |
| 9 | ammo2_weapon1 | 2 | | | | |

Modes (`slot_serialize_mode_enum.h`):
* 0 = condition/stack only.
* 1 = amount only. No slot uses it.
* 2 = both.

### 2.2 0x92 player_profile

Handled by `network_client::process_player_profile` (`game/sources/network_client_processing.cpp:132-139`), which calls `player_profile::deserialize` (`game_core/game_net_defines.h:57-83`). Retail `player_profile::deserialize` (rva 0x98670, 96%) was checked.

| field | type | notes |
|---|---|---|
| team | u8 | `game_team_id`: 0 team_1, 1 team_2, 2 neutral, 3 undefined (`game_core/game_team_id.h`) |
| is_local | u8 | Nonzero means this is the receiving client's own player. Exactly one profile per client should be local. |
| profile_name | str | Buffer is `char[32]` (`game_core/player_profile.h:17`) with no bounds check: **len ≤ 31**. |
| boosters_mask | u16 | Bits 0..10 (`boosters[11]`). Send 0 for none. |
| for each set bit i | u8 booster_id, f32 value | Ids are from `boosters_enum.h` (1..11). |
| repeat until end of message | u8 slot, then `inventory_item_instance` | **Slot must be 0..18.** There is no range check, so a bad value writes out of bounds. |

`inventory_item_instance` wire layout (`game_core/inventory_item_instance.h:28-38`):

| field | type | present if |
|---|---|---|
| dict_id | u16 | always |
| id | u32 | always. **A slot counts as occupied only if `id != 0`** (`inventory_cook.cpp:51,63,75`). |
| condition_or_stack | u16 | mode ≠ 1 |
| amount_in_inventory | u32 | mode ≠ 0 |

A record is therefore 1 + 8 = **9 bytes in mode 0** and 1 + 12 = **13 bytes in mode 2**. The loop ends at end of message (`reader.eof()`), so the slot records must come last.

* **Player ids** are assigned in the order 0x92 messages arrive: 0, 1, 2, … (`player_profiles[received_players_count++]`). Every later message uses them as its id byte.
* **Ordering:** 0x81 must arrive first. It resets `received_players_count` from its initial 0xFF to 0. The client must then receive exactly `players_count` profiles (≤ 20). The last profile starts the map load (`network_client_processing.cpp:137-138`).
* **dict_id** indexes the `items_dict` in `resources/gameplay/db_static_dictionaries` (`items_dictionary_cook.cpp:19,48-76`). An unknown dict_id is undefined behaviour (`items_dictionary.h:16`). Use only ids from `game_data/json/items.json`.
* **Item class per slot** (`inventory_cook.cpp:47-119`, `items_cook.cpp:55-101`):
  * Weapon slots create a weapon.
  * Ammo slots create `weapon_ammunition`.
  * Quick slots create a medkit, oxygen_tank, booby_trap_set or artefact_lifebone, depending on the config `type`.
  * Armour slots 0..6 create no network item; they only affect visuals and stats.
  * Do not put scopes in slots.
* **Amounts at insert** (`inventory.cpp:136-186`):
  * Weapon: `set_amount(condition_or_stack)`, and ammo is loaded on the next activate.
  * Ammo: `amount = min(condition_or_stack, amount_in_inventory)`.

### 2.3 A player MUST have a weapon

**A profile with no weapon in slot 7 or 10 crashes the client at spawn.**
* `player::insert` falls back to `m_empty_hands` (`game/sources/player.cpp:220-229`).
* For network players that pointer is NULL. The empty-hands resource is only requested for demo players (`player_cook.cpp:94-95,151-152`), and `query_players` sets `is_demo_player=false` (`network_client_processing.cpp:107`).
* The next `activate()` therefore dereferences NULL (retail 0x5e4e65).

Both active-slot bytes in 0x84 must also name a slot that holds a weapon. An empty slot yields a NULL item, and the client calls `activate()` on it (`player.cpp:1242-1252`).

### 2.4 0x84 spawn_player / respawn

* Handler: `process_player_respawn` (`network_client_processing.cpp:222-234`; 100%; retail calls vtable slot 25 with the reader).
* It calls `player::deserialize` (`game/sources/player.cpp:1226-1256`, 98.8%).
* **Send 0x84 only after the client's 0x42 join_match.** The handler calls `get_player(id)->...` with no NULL check. Before the map and players have loaded, the player is NULL and the client crashes.

| field | type | notes |
|---|---|---|
| player_id | u8 | |
| position | float3 | World position. Take it from the map's respawn points (section 8). |
| yaw | f32 | Radians about Y. The transform is `create_rotation_y(yaw)`. |
| look_pitch | f32 | Clamped to [-1, 1] by input. |
| is_alive | bool | 1 = spawn alive. 0 = insert as a dead body. |
| current_active_slot | u8 | **Must be the weapon slot `insert` picked:** 7 if weapon1 is filled, else 10. |
| target_active_slot | u8 | Same value. |
| stamina.value | f32 | `default.player` `stamina_params.max_value` = 100 (`game_data/json/raw/gameplay/players/default.player.json`). |
| stamina.last_spending_time_in_ms | u32 | In client game ms. 0 is fine. |
| stamina.last_tick_time_in_ms | u32 | Same. |
| stamina.lower_threshold_was_reached | bool | 0 |
| inventory tail | see 2.5 | |

The stamina layout is in `game_core/sources/player_stamina.cpp:54-60`.

On a player that is already inserted, `remove()` runs first, so the same message also serves as respawn. `insert()` re-reads the inventory from the profile (`player.cpp:195-238`).

### 2.5 Inventory tail

`inventory::deserialize` (`game_core/sources/inventory.cpp:268-280`) walks **slots 7..18 in slot order**. Slots 0..6 are `ignored_slots_for_serialization` (`inventory.cpp:23-31`). For each occupied slot it calls the item's virtual `deserialize`:

| item class | wire | cite |
|---|---|---|
| weapon_ammunition, medkit, oxygen_tank, booby_trap_set | u16 amount | `inventory_item.cpp:34-37`, `medkit.h:88`, `oxygen_tank.h:75`, `weapon_ammunition.h:76` |
| artefact_lifebone | **0 bytes** | Retail 0xd2070 is an empty `ret 4`. The server-side `serialize` would write a u16, so **write nothing here.** |
| weapon (`weapon_core`) | table below | `game_core/sources/weapon_core.cpp:1024-1074`. Ledger 83.6%; the read order was checked in retail 0x594450. |

**weapon_core:**

| field | type | notes |
|---|---|---|
| amount | u16 | The base `inventory_item` field. Use the profile's `condition_or_stack`. **[UNCERTAIN: semantics]** |
| random seed | u32 | Seeds the client's fire PRNG. The server must use the same seed to reproduce dispersion. |
| normal_random seed | s32 | |
| target | u8 | `weapon_targets`: 0 idle, 1 fire, 2 aim, 3 aim_fire, 4 reload, 5 inactive. Send 0. |
| old_actions_mask | u32 | 0 |
| ammo_in_magazine | u16 | e.g. the clip size (`items.json` `clip_size`) |
| bullets_in_queue | u16 | 0 |
| fire_queue_type | u8 | Fire mode. Send 0. |
| ammo_slot | u8 | 8, 9, 11 or 12; 0x13 = none |
| is_round_chambered | u8 | **Only if** the weapon config has a `chamber_a_round` state (rem_700, rem_870, toz_122; not ak_74u). |
| *active block: only for the weapon in `current_active_slot`* | | Only `activate()` gives the fsm a current state (`weapon_core.cpp:792,864`). |
| is_shown | u8 | 1 |
| hand IK active mask | u8 | bit0 = left, bit1 = right (`hand_to_weapon_ik_processor.cpp:160-169`) |
| hand IK left start time | u32 | In client game ms (the server subtracts `client_offset`). 0 is fine. |
| hand IK right start time | u32 | |
| weapon state id | u8 | fsm order: 0 inactive, 1 show, 2 hide, 3 idle, 4 reload, 5 fire, 6 aim, 7 aim_fire, then [8 chamber_a_round], then [9 chamber_a_round_aimed] (`weapon_core.cpp:169-181`). **An out-of-range id crashes the client.** |
| weapon state payload | 0..8 bytes | 0 bytes for inactive, idle, aim and aim_fire. 8 bytes (u32 interval_id + f32 interval_time) for show, hide and fire. 1 byte for reload, the shotgun-reload substate and the chamber states (`weapon_core_base_state.cpp:47-54` and subclasses). **Send 3 (idle), which has 0 bytes.** |
| user-animation state id | u8 | 0 stand, 1 crouch, 2 sprint, 3 jump. The payload is always 0 bytes (`weapon_user_animations_selector.cpp:163-182`). Send 0. |

Weapons that are not active (for example weapon2 when weapon1 is active) end after `is_round_chambered`, or after `ammo_slot` if they have no chamber.

### 2.6 Worked example: AK-74u (dict 13) + 5.45x39 FMJ (dict 7)

The dict ids are from `game_data/json/items.json`; `player_templates[0]` uses 13 and 7 for weapon2. The AK-74u has no chamber state. Generator: `_wsl/mp_examples.py`.

**0x92** body (after `92 <u16 order>`), 31 bytes. Team 0, local, name "Test", no boosters, slot 7 = AK (id 1, 30 rounds), slot 8 = ammo (id 2, clip 30, total 90):
```
00 01 04 54 65 73 74 00 00
07 0D 00 01 00 00 00 1E 00
08 07 00 02 00 00 00 1E 00 5A 00 00 00
```

**0x84** body, 72 bytes. Player 0, alive, at team_1 respawn point 21 (-30.552, 0.505, -80.514), yaw 0, pitch 0, active slot 7:
```
00                                       player id
7F 6A F4 C1 AE 47 01 3F 2B 07 A1 C2      position
00 00 00 00 00 00 00 00                  yaw, pitch
01 07 07                                 alive, current slot, target slot
00 00 C8 42 00 00 00 00 00 00 00 00 00   stamina 100.0, 0, 0, false
1E 00 00 00 00 00 01 00 00 00 00 00 00 00 00 1E 00 00 00 00 08   weapon (21 bytes)
01 00 00 00 00 00 00 00 00 00 03 00                               active block: shown, IK off, idle, stand
1E 00                                                             ammo slot 8, amount 30
```

The **smallest valid profile** is this weapon record plus its ammo record. A profile with no weapon is not valid (2.3). For a Remington 700 (dict 14, chambered), add one `is_round_chambered` byte after `ammo_slot`.

---

## 3. Handshake and session identity

**Session-id chain:**
1. The login reply 0x08 carries `u32 session_id` (`network/sources/login_client_impl_sign_in.cpp:33-53`).
2. The client sends it in lobby sign-in op 38 (`game/sources/lobby_client.cpp:84-91`).
3. The lobby sends **op 51** `connect_to_match_server`: `str host (≤63)`, `u16 port`, `u32 match_id`, `u8 team_id` (`game/sources/network_client_lobby.cpp:38-58`, retail checked).
4. The client calls `match_client::connect(host, port, session_id, …)`.

**`match_id` and `team_id` are never sent to the match server.** `match_id` only feeds the chat channel. The match server must map `session_id` to account, profile, team and match from lobby state; the PoC `lobby.py` already issues tickets keyed by `session_id`.

| step | dir | message | layout | cite |
|---|---|---|---|---|
| 1 | C→S | 0x40 connection_request | `u32 session_id` (0 for `-spectator`) | `game/sources/match_client.cpp:43-54`, retail 0x5c7470 |
| 2 | S→C | **0x80** match_server_connection_successful | empty. It must be the **first** server message (order 0). | `network/sources/match_client_impl.cpp:55-78`, retail checked (`cmp edx,0x80`) |
| 3 | C→S | 0x41 get_startup_info | empty. Sent on success. A non-spectator client also drops its lobby TCP connection here. | `game/sources/network_client.cpp:120-125`, retail 0x7048c3 |

* **Rejection.** If the first message is anything other than 0x80, the client treats it as "connection forbidden": it reports `invalid_session_id`, logs "game: invalid session id", and does nothing else. To reject cleanly, send one non-0x80 message, then a low-level `initiate_disconnection` (1.6).
* **0x80 is handshake-only.** It must never be sent after the handshake (4.3).

---

## 4. Message catalogue

A message is `[u8 type][u16 order_id][payload]` (1.5). The tables show the payload only. Ids are defined in `network/message_types.h:8-58`.

### 4.1 Client → server

The retail call sites of `new_packet` are exactly 0x40, 0x41, 0x42, 0x43, 0x44, 0x45, 0x46, 0x48 and 0x4a. **0x47 and 0x49 are never sent.**

| id | name | payload | when | cite |
|---|---|---|---|---|
| 0x40 | connection_request | u32 session_id | connect | `match_client.cpp:51-52` |
| 0x41 | get_startup_info | - | after 0x80 | `network_client.cpp:125` |
| 0x42 | join_match | - | map and players loaded (sent after 0x48) | `network_client_processing.cpp:86-88` |
| 0x43 | client_player_update | 44 bytes, below | one per local frame, flushed every 33 ms (≤32 queued) | `:468-484,518-527`, `game_core/sources/client_player_update.cpp:16-21` |
| 0x44 | client_player_commit_suicide | - | K key or the `suicide` command, only when a local and current player exist | `:646-650` |
| 0x45 | time_synchronization_request | u32 client_permanent_timer_ms | first local-player tick, then about every 4 s | `:445-451,613-614,631-635` |
| 0x46 | time_synchronization_confirmation | - | on receiving 0x8b | `:457` |
| 0x47 | bullets_info_request | never sent | | |
| 0x48 | team_bases_initialize_info | - | just before 0x42. Answer with 0x93. | `:86` |
| 0x49 | force_finish_match | never sent | | |
| 0x4a | world_synchronization_confirmation | - | after handling 0x9d | `:755` |

**0x43 client_player_update** (44 bytes). The serializers were checked against retail: `player_input::serialize` 0x700e80 and `player_state::serialize` 0x776570.

| off | type | field |
|---|---|---|
| 0 | float2 | angular_velocity (yaw rate, pitch rate) |
| 8 | float2 | angular_acceleration |
| 16 | u32 | actions_mask (bits below) |
| 20 | float3 | position (`transform.c.xyz`) of the client's predicted "target" transform |
| 32 | f32 | yaw (`get_angles(rotation_zxy).y`) |
| 36 | f32 | look_pitch |
| 40 | u32 | time_in_ms, on the client game clock (section 7) |

**actions_mask** (`game/sources/player_input_handler.cpp:217-323`):

| bit | action | bit | action |
|---|---|---|---|
| 0x1 | forward | 0x800 | next ammo type |
| 0x2 | back | 0x1000 | select weapon slot 1 |
| 0x4 | strafe left | 0x2000 | select weapon slot 2 |
| 0x8 | strafe right | 0x4000 / 0x8000 | quick slot 1 down / up |
| 0x10 | jump | 0x10000 / 0x20000 | quick slot 2 down / up |
| 0x20 | fire | 0x40000 / 0x80000 | quick slot 3 down / up |
| 0x40 | reload | 0x100000 / 0x200000 | quick slot 4 down / up |
| 0x80 | aim | 0x400000 / 0x800000 | quick slot 5 down / up |
| 0x100 | crouch | 0x1000000 / 0x2000000 | quick slot 6 down / up |
| 0x200 | sprint | 0x4000000 | use back slot |
| 0x400 | next fire mode | 0x8000000 | hold breath (counts only with aim) |
| | | 0x10000000 | use |
| | | 0x20000000 / 0x40000000 | missile / drop (no client consumer found) |

Sprint is active when `0x200 && 0x1 && !(mask & 0x16E)` (`game_core/player_input_inline.h:8-13`).

### 4.2 Server → client

The dispatch is in `game/sources/network_client_handler.cpp:20-60`. The retail jump table was decoded (`_wsl/mp_jt.py`) and agrees with it.

| id | name | payload | client action and cite |
|---|---|---|---|
| 0x80 | connection_successful | - | Handshake only (section 3). **Sending it later crashes the client.** |
| 0x81 | match_options | u8 map_id (ignored), str map_name (≤31), u8 game_mode, u8 players_count (1..20), u8 victory_items_count, u8 respawn_time, u16 match_time | Stores the options (`game_net_defines.h:143-154`). Must come before 0x92. |
| 0x82 | server_player_input | u32 time_in_ms, then **1 or more** × {u8 player_id, float2 ang_vel, float2 ang_acc, u32 actions_mask, float3 pos, f32 yaw, f32 pitch, u8 weapon_slot_id, u8 ammo_slot_id, u8 weapon_state}. Each entry is 44 bytes; ≤5 fit in one message. | Read with `do…while(!eof)`, so at least one entry is required (`network_client_handler.cpp:27-35`). A dead player's transform is snapped; an alive player goes through `time_warp` (section 6). An unknown id is logged and skipped (`network_client_processing.cpp:492-516`, `server_player_update.cpp`, `weapon_state.cpp:27-32`). Of the weapon fields, only weapon_slot_id is used. |
| 0x83 | kill_player | u8 victim, u8 killer, bool headshot, u32 item_dict_id | Kill feed, then `player::kill` if the victim is inserted and alive (`:182-194`). |
| 0x84 | spawn_player | section 2.4 | Insert or respawn. **Only after 0x42.** |
| 0x85 | team_base_capture_progress | u32 point_id, u32 progress | HUD: `set_base_capture_progress(progress, point_id)` keys `m_base_points` by the FIRST wire field (retail disassembly; the decompiled `:286-291` reads them in the wrong order). No encoder: level_03 has no base points. |
| 0x86 | match_time_changed | u32 ms | HUD (`:293-296`) |
| 0x87 | respawn_time_changed | u32 seconds (0 hides it) | HUD only (`:298-301`) |
| 0x88 | player_kd_stats_changed | u8 id, u32 kills, u32 deaths | HUD (`:368-374`) |
| 0x89 | hit_player | u8 initiator (0xFF = none), u8 victim, str body_part (≤15), str damage_type (≤15, e.g. "injury"), f32 amount, f32 armor_piercing | `apply_hit_directly`; the client computes the damage itself (`game_core/sources/hit_initiator.cpp:41-58`; 70.7%, retail checked). |
| 0x8a | affect_damage_model | u8 id, str body_part (≤15), **u32** hit_affects_type, **u32** affect_event_type | `:205-215`. The enums are 4 bytes (retail checked). Affect: 0 death, 1 bleeding, 2 concussion, 3 hand_damage, 4 leg_damage, 5 critical_poisoning, 6 poisoning, 7 radiation_sickness, 8 blindness. Event: 0 applying, 1 recalling, 2 canceling. |
| 0x8b | sync_response | u32 connected_mask (bit i = player i connected) | The client replies 0x46, marks itself time-synced, and attaches control if status = 4 (`:453-466`, retail 0x5b5a40). |
| 0x8c | match_finished | - | `close_current_match(false)`, which returns to the lobby (`network_client_lobby.cpp:225-228`). |
| 0x8d-0x90 | server_bullet_* | **NEVER SEND.** Their jump-table entries are 0, so the client jumps to address 0. | |
| 0x91 | player_visibility_changed | u8 id, bool visible | `:668-676` |
| 0x92 | player_profile | section 2.2 | |
| 0x93 | team_bases | u32 count, then count × {u32 point_id, u32 owner_team, u32 team_points, u32 capture_progress} | `game/sources/game_world_ui.cpp:131-148`. For level_03 send count 0. |
| 0x94 | initialize_victory_items | s8 team1_pts, s8 team2_pts, u8 n, then n × {u8 holder (0xFF = lying in the world), u8 item_idx, float3 pos}, then u8 containers, then per container {u8 container_id, u8 k, k × u8 item_idx} | `:236-284` (retail checked) |
| 0x95 | victory_item_take_or_put | **Retail layout:** u8 player_id, u8 item_idx, bool is_take, u8 container_id (0xFF = none), then a float3 only if `!is_take && container == 0xFF` (the client ignores it) | The decompiled source at `:384-443` is wrong; this follows retail rva 0x5b53d0. |
| 0x96 | trap_placed | u8 player, u8 slot, u8 trap_index, float3 pos, float3 angles | `:683-693`. The server decides placement (11.8); slot must hold a trap set, index < its stack size; every client does `--m_amount`. |
| 0x97 / 0x98 / 0x99 | trap_removed / fired / disarmed | u8 player, u8 slot, u8 trap_index | `:695-723`. 0x97 only for a trap that is in the world (11.8). |
| 0x9a | game_status_changed | u32 status: 0 inactive, 1 waiting_for_first_player, 2 waiting_for_players, 3 final_countdown, 4 inprocess | Retail 0x5b5b30. Status 4 hides the pregame UI and, if time-synced, attaches the local player. A first status of 1..3 shows the pregame UI and the warm-up camera (`game/sources/game_status.h`). |
| 0x9b | match_wait_time_changed | u32 seconds | Pregame label: "final countdown" if status is 3, otherwise "waiting for players" (`:303-308`). |
| 0x9c | game_world_object_state | u8 player, u8 slot, u8 trap_index, u8 trap_state (0 removed, 1 armed, 2 fired, 3 disarmed), float3 pos, float3 angles | No NULL check on the player. `base_player.cpp:93`, `booby_trap_set_core.cpp:312`, `booby_trap_core.cpp:339` |
| 0x9d | world_synchronization_request | - | The client removes all players and victory items, then replies 0x4a. The server then re-sends 0x84 for everyone, plus 0x94 (`:731-756`). |
| 0x9e | damage_model_state | u8 player, then **one block per body part in config order**: {f32 health, u32 last_hit_time, u8 affects_count, then affects_count × {u8 type, u32 time}} | There is no count prefix. human_hit_params has 15 parts: body, back, head, face, right_leg, left_leg, right_foot, left_foot, right_arm, left_arm, right_hand, left_hand, pain, infection, radiation (`damage_model.cpp:332`, `body_part_parameters.cpp:407-428`). **[UNCERTAIN: the part order is taken from the JSON export of the config]** |

### 4.3 Crash-prone ids and states

The server must avoid all of these:
* **Unbounded dispatch.** The retail dispatch is `sub eax,0x81; jmp [eax*4+table]` with **no bounds check**. The following all jump to garbage:
  * 0x80 after the handshake
  * **0x8d-0x90**
  * any id ≥ 0x9f
* **Before 0x42.** 0x84, 0x9c and 0x9e sent before 0x42 (players not loaded yet) dereference a NULL player.
* **Empty 0x82.** A 0x82 with zero entries reads past the end of the message.
* **Bad byte values:**
  * slot bytes ≥ 19
  * weapon-state ids outside the fsm
  * active-slot bytes that name empty slots
* **Oversize.** Messages over 250 bytes, or datagrams over 256 bytes.

---

## 5. M2 minimal server sequence

The order ids are the server's per-message u16 `order_id`.

| # | dir | message | notes |
|---|---|---|---|
| 1 | C→S | 0x40 (u32 session_id) | Look up the ticket by session_id. Remember the client's endpoint. |
| 2 | S→C | 0x80 (order 0) | |
| 3 | C→S | 0x41 | |
| 4 | S→C | 0x81 (order 1) | Example body: `00 08 6C 65 76 65 6C 5F 30 33 02 01 00 0A 58 02` = map_id 0, "level_03", mode 2, 1 player, 0 victory items, respawn 10 s, match_time 600. |
| 5 | S→C | 0x92 × players_count (orders 2..) | Example in 2.6. Exactly one profile has is_local = 1. |
| - | client | loads `resources/projects/level_03/client_project` and `gameplay/players/default.player` | This can take seconds. Keep acking and sending keepalives. |
| 6 | C→S | 0x48, then 0x42 | |
| 7 | S→C | 0x93 `00 00 00 00` | Optional but cheap; answers 0x48. |
| 8 | S→C | 0x84 for every player | Example in 2.6. The local player is now inserted. |
| 9 | S→C | 0x9a `04 00 00 00` (inprocess) | May also come before 0x84. |
| 10 | C→S | 0x45 (u32) | Sent on the first frame the local player is inserted. |
| 11 | S→C | 0x8b `01 00 00 00` | **Required.** The local player does not tick, move or send input until this arrives. With status 4 it also attaches the camera and input (retail 0x5b5a40). |
| 12 | C→S | 0x46, then a stream of 0x43, plus a 0x45 about every 4 s | Answer every 0x45 with 0x8b. |
| 13 | S→C | (optional) 0x82 echoing the client's own position | Not needed for local movement (section 6). Send something, even just keepalives, at least every 1-3 s. |

**When the local player becomes controllable.** These rules are from retail: `process_game_status` 0x5b5b30, `process_sync_response` 0x5b5a40 and `process_player_respawn` 0x5b5bf0.
* Control requires all three: status = 4, time synced (a 0x8b has been received), and the player alive.
* The attach happens on whichever of 0x84, 0x9a and 0x8b arrives last.

Example first datagrams:

| datagram | bytes | meaning |
|---|---|---|
| Client hello | `00 00 FF FF 00 00 40 00 00 D2 04 00 00` | seq 0, ack 0xFFFF, single, 0x40 order 0, session 1234 |
| Server reply | `00 00 00 00 00 00 80 00 00` | seq 0, ack 0, bits 0, 0x80 order 0 |
| Keepalive | `05 00 09 00 01 00 01 02` | seq 5, ack 9, multi, low-level 2 |
| Bundle of 0x81 + 0x92 | `01 00 01 00 01 00 13 <19-byte 0x81 msg> 22 <34-byte 0x92 msg>` | |

---

## 6. Authority and reconciliation

* **Movement: the client predicts, the server corrects.**
  * The local player moves immediately from local input through its physics controller (`player_tick.cpp:297-441`).
  * Each frame it stores `{time, input, state, weapon slot}` in a 64-entry history (`player.cpp:62,323-331`).
  * It sends 0x43 carrying its predicted transform.
  * M2 works with **no 0x82 at all**. The server can either trust the 0x43 position (client-authoritative) or simulate the player and correct it.
* **Corrections for the local player** come from 0x82 entries (`player::time_warp`, `player_tick.cpp:155-219`; 87.5%, retail checked).
  * The entry is accepted only if `newest_history_time - 1000 ≤ t ≤ newest_history_time` and t is not older than the last correction. A t equal to the newest entry does nothing.
  * The client rewinds to the history entry at t, inserts the server's state, replays its inputs, and keeps its own rotation, adopting only the corrected position.
  * **So the 0x82 time must be in the recipient's own clock.** Use the newest 0x43 `time_in_ms` that the server has processed from that recipient. This means 0x82 is built per recipient.
  * Never let that time decrease, and never send 0 after a non-zero value.
* **Remote players.**
  * 0x82 sets the target transform directly, with the time clamped to the client's current time.
  * The rendered model smooths toward that target (`player::smooth`: about 3 m/s linear, 180°/s angular, both console-tunable) and animates from the received input and actions.
  * Retail seeds one history item when the history is empty. The decompiled `player_tick.cpp:389` has this condition inverted; retail 0x5d6811 shows the correct form.
* **Combat: the server is authoritative.**
  * The client simulates its own shots for the visuals, using PRNG seeds it receives in 0x84.
  * It **never reports shots or hits**: `on_player_hit_received` is empty (retail rva 0x12c50).
  * The server must derive firing from bit 0x20 of 0x43 plus the transform and pitch, trace the bullets itself, and send 0x89, 0x8a, 0x9e, 0x83 and 0x88.

---

## 7. Time model

| item | finding | cite |
|---|---|---|
| Client clock | `game::m_current_time_in_ms = m_timer.get_elapsed_msec()`. Starts at process start and is never reset per match. u32 milliseconds, QPC-based. A console pause freezes it. | `game.cpp:247-248,629-631,923-936` |
| Frame rate | Variable and uncapped. One `network_client::tick` per frame. | `engine/sources/engine_world_logic.cpp:118-139`, `game.cpp:642,658` |
| 0x43 time | The frame's `current_time_in_ms` (game clock). | `player_tick.cpp:393` |
| 0x45 time | `permanent_timer` ms. It has the same origin as the game clock unless the client paused. The client uses it only to compute RTT, and `m_server_latency` is never used. | `network_client_processing.cpp:445-455` |
| Does the server set client time? | **Never.** 0x8b carries only the connected mask. | `:453-466` |
| Periodic sync | Fires when `current_time - m_last_sync_request_time > 4000`. This mixes the two clocks; they are equal unless the client paused, so the cadence is about 4 s. | `:613-614` (retail checked) |
| Input send | Every 33 ms, quantised to multiples of 33. At most 32 updates per flush; the oldest is dropped. | `:548,475-476,607-611` |
| Server tick and snapshots | Not observable from the client. **Recommend a 33 ms (30 Hz) tick** to match the client's send cadence and keepalive. Send 0x82 per recipient each tick, at most 5 players per message. | |
| Interpolation | No snapshot buffer. Remote players lerp toward the latest target (section 6). | `player_tick.cpp:267-295` |

---

## 8. Maps, modes, spawn data

* **map_name to resource path.** `game_world::load` calls `create_request(map_name, client_game_project_class)`, which resolves to `resources/projects/<map_name>/client_project` (`game_world.cpp:426`, `project_cooker_simple.cpp:53`). **The client ignores map_id; send 0.**
* **Maps in the shipped `resources.db`:** `level_03`, `level_03_evn` and `level_03_rain`. Each has `client_project`, `server_project`, `*.lua` and `vegetation`. `lobby_scene` is the menu background, not a match map. **Use 0x81 map_name `"level_03"`.**
* **Modes** (`game_core/game_mode_type.h`): 0 capture_enemy_base, 1 capture_neutral_base, **2 gather_victory_items**, 0xFF invalid.
  * The client uses the mode only for UI (`game_world_ui.cpp:74-88`).
  * `team_base_points` is empty in all three maps, so **only mode 2 has map data. Use mode 2.**
  * With `victory_items_count = n`, the client creates `vp_0..vp_{n-1}` from `gameplay/victory_items/default.lua` (`game_world.cpp:429-435`). M2 can send 0.
* **Respawn points come from the server.** The client loads them but only uses them for debug drawing (`game_world.cpp:556-562`). The server picks the positions and sends them in 0x84, and sends victory-item positions in 0x94.
  * The server should read `projects/level_03/server_project(.lua)` itself; the data is exported in `game_data/json/maps.json`.
  * **Respawn points:** 43. point_id 0-20 belong to team 2 (team=1) and 21-42 to team 1 (team=0). Priority 2 marks front spawns; priority 1 marks rear "safe" spawns.
  * **Victory items:** 26 victory_item_spawners and 2 containers: id 0 for team 2 at (46.747, 0.423, 8.476) and id 1 for team 1 at (-20.754, 0.510, -79.267).

---

## 9. M3 sketch

The sketch as first written; section 11 records what the PoC server implements and where it
deviates (no 0x9e, no local-player 0x82, 0x94 only after attach, continued connections).

* **Other players:**
  * Send each client every profile, with `is_local = 1` only on its own.
  * Send 0x84 for each player after that client's 0x42.
  * Send 0x82 per recipient each tick, at most 5 entries per message (split the rest), with times in that recipient's clock (section 6).
  * Send 0x8b connected masks, and 0x91 for visibility culling.
* **Fire and hits:**
  * Simulate weapon fire from bit 0x20 of 0x43 and the weapon state.
  * Trace against hit boxes, using the weapon PRNG seeds the server sent in 0x84.
  * Send 0x89 to all clients (each computes the damage locally), 0x8a for status affects, and 0x9e to resync health.
* **Death:**
  * Send 0x83, then 0x88 with K/D. The body stays inserted.
  * Count down with 0x87, then send 0x84 with a new position to respawn. This fully reloads the inventory from the profile.
  * Treat 0x44 suicide as a kill.
* **Rounds:**
  * Warm-up: 0x9a 1, 2 and 3 with 0x9b countdowns.
  * Start: 0x9a 4 and 0x86 match time.
  * Victory items: 0x94 for the initial state, 0x95 for each take or put.
  * 0x9d triggers a full world resync and re-spawn; the client replies 0x4a.
* **Match end:**
  * Send 0x8c. The client returns to the lobby with `user_initiate=false`, which does not discard the order.
  * On user quit or connection loss, the client sends lobby op 39 `discard_playing_order` (u32 match_order_id) at its next lobby sign-in (`lobby_client.cpp:153-156`).
  * The lobby must stop reporting that player's status as in a match.

---

## 10. Verification log and uncertainties

These are functions that are below 85% in the ledger, plus places where the spec follows retail rather than the decompiled source. Each was checked against the retail binary.

| Function | Ledger | Result |
|---|---|---|
| `fill_packet_header` | 71% | Header identical to source |
| `new_low_level_packet` | 86% | `[01][type]`, multi flag |
| `match_client_impl::on_packet_received` | 96% | 0x80 check |
| `on_connected_to_match` | parked | Sends 0x41 on connection_successful (0x30) |
| `player_state::serialize` | 31% | Identical to source |
| `player_input::serialize` | 49% | Identical to source |
| `send_player_inputs` | 57% | Identical to source |
| `network_client::tick` | 93% | Constants 33 / 3000 / 4000 / 5000 |
| `process_game_status` | 76% | **Source wrong**: retail attaches `m_local_player` inside the "local && synced" check |
| `process_sync_response` | 86% | **Source wrong**: retail attaches `m_local_player`, not NULL |
| `player::tick` | not given | **Source inverted**: retail serializes history when it is empty |
| `process_victory_item_take_or_put` | 78% | **Source wrong**: correct layout in 4.2 |
| `hit_info::deserialize` | 71% | Checked |
| `process_initialize_victory_items` | 75% | Checked |
| `weapon_core::deserialize` | 84% | Read order checked |
| `setup_from_profile` | 80% | Source uses `item_by_id(current)`; retail uses dict_id |
| `slot_serialize_mode` table | n/a | Read from `.rdata` |
| `on_match_packet_received` jump table | n/a | Fully decoded |

**Open uncertainties:**
1. What these weapon fields mean: `amount`, `weapon_targets` values above 0, `old_actions_mask`, and `weapon_state.ammo_slot_id/state`. The client never reads the last two.
2. The order of body parts in 0x9e, which is taken from the JSON export of the config.
3. The trailing float3 in 0x95. The client reads it and ignores it.
4. How the original server chose between respawn priorities and zones. Only the data survives.
5. `call_item_serialize` (74%) and `call_item_deserialize` (73%) were not checked against retail. Their inlining caller `inventory::deserialize` is 100% matched.
6. The original server's tick rate. The client gives no evidence; 30 Hz is a recommendation.

**Helper scripts** in `F:\Software\survarium\_wsl\`:
* `mp_dis.py`: retail disassembly by name
* `mp_ledger.sh`: ledger lookup
* `mp_jt.py`: decodes the dispatch jump table
* `mp_rd.py`: reads `.rdata`
* `mp_examples.py`: generates the hex examples

---

## 11. Implementation notes (poc-server M3)

What `poc-server/match/` does, and which client code each rule follows. [V] = checked in the
client sources for this milestone; [A] = an assumption or approximation.

### 11.1 Roster, sessions, lobby

* **Fixed roster.** The lobby forms a match before any op 51 is sent and writes every ticket
  with `roster` (all session ids, in order). The first 0x40 of that match builds the whole
  roster, so every client gets the same 0x81 `players_count` and the same 0x92 profiles in
  the same order; player id = roster index. A roster player who has not joined is loaded on
  every client (`query_players`) but never inserted: no 0x84 is sent for it, its bit in the
  0x8b mask is 0 and no 0x82/0x89/0x83 mention it. [V] `network_client_processing.cpp:91-139,222-234`
* **Open match.** Sessions without a roster ticket share one match whose roster grows (M2
  behaviour; an earlier client never learns about a later one). It has no items, timer or end
  unless `open_match_rules`.
* **Lobby re-sign-in during a match.** The client drops the lobby TCP when it reaches the match
  and reconnects to the lobby while loading/playing. The lobby answers status 0 with state 3
  (in_match) and the op 51 order/match/team. State 0 makes `lobby_menu::on_client_status_received`
  call `switch_to_lobby()` mid-match; state 3 does nothing. The player returns to state 0 only
  when the in-process match server reports its session ended (disconnect, timeout, match
  removed), on op 39, or when the match server never saw it within `--match-timeout`. [V]
  `lobby_menu.cpp:185-223`, `lobby_client.cpp:208-234`
* **Same session_id from a new endpoint.** A 0x40 whose session_id is bound to a player on
  another address replaces the stale session (old transport dropped silently, roster slot
  kept, no "session ended" to the lobby) and runs the normal handshake when the new connection
  starts at order 0.
* **Continued connection.** If that 0x40 carries order_id != 0, the client re-ran
  `udp_match_client::connect` while still connected (Play pressed again mid-match; the ASSERT
  in `udp_match_connection::connect` is compiled out), so the old sequence/order numbering
  continues. Its `match_client_impl` is still `handshaked` with the game dispatcher installed
  (only `disconnect`/`on_disconnect` reset it), so **a 0x80 here would hit the unchecked jump
  table**. The server adopts the old numbering (so its datagrams are accepted, unacked reliable
  messages re-sent) and ends that connection with `initiate_disconnection`; the client's
  `on_disconnect` runs `close_current_match(true)` and returns to the lobby. In the observed
  retail run the aborted read on the closed socket disconnects the client anyway. [V]
  `match_client_impl.cpp:53-136`, `udp_match_client.cpp:39-121`, `udp_match_connection.cpp:398-423,605-637`

### 11.2 Rounds (gather_victory_items)

| state | sent | |
|---|---|---|
| waiting | 0x9a 2, 0x9b (seconds of `join_timeout` left, every second) | until the whole roster joined or the timeout |
| countdown | 0x9a 3, 0x9b every second | `countdown_s`; skipped for a 1-player roster |
| in process | 0x9a 4, 0x86 ms left every second (attached clients only) | |
| finished | 0x94 final score, 0x86 0 ("MATCH TIME IS UP"), then after `end_delay` 0x8c | 0x9a is not changed |

* **When 0x94/0x95 may be sent.** `game_world_ui::set_victory_points`/`add_victory_points`
  call `get_current_player()->team()`. The server sends them only to a client that is
  *attached*: it was sent 0x9a 4, it answered a 0x8b with 0x46, and its own 0x84 was sent.
  That mirrors when retail attaches the local player (`process_game_status`,
  `process_sync_response`, `process_player_respawn`). The first time a client is attached it
  gets one 0x94 snapshot; afterwards only 0x95. 0x94 must not be repeated with items: putting an
  item that is already in the world inserts it twice. [V] `game_world_ui.cpp:150-180`,
  `network_client_processing.cpp:236-284`, `game_world.cpp:578-581`
* **Use.** The client sends only action bit 0x10000000; `use_victory_item` is an empty virtual
  on the client. On the rising edge the server stores (carrying, own container within 3 m),
  steals (not carrying, enemy container with items) or picks up (an item within 2 m). [A] radii;
  the client's own detection is a 1 m ray from the head (`player.cpp:44,483-533`).
* **Steal order.** `victory_items_container_core::take_item` pops the LAST item whatever 0x95
  names, so the server steals the top of its container stack. [V] `victory_items_container_core.cpp`
* **Score.** The client derives the 0x95 deltas from `container->team() != team_x`
  (`network_client_processing.cpp:400`); it is unclear whether that sign is right in retail. After
  every container put/take the server therefore also sends a score-only 0x94 (n = 0,
  containers = 0), which just calls `set_victory_points`.
* **Win.** The team whose container holds `victory_items_count` items wins at once; otherwise the
  higher count when the timer ends; equal is a draw. A carrier who dies or disconnects drops the
  item where it was (0x95 put, container 0xFF). [A] the original rules are not in the client.

### 11.3 Remote players and the local player

* 0x82 per recipient, every server tick (30 Hz), at most 5 entries per message, never empty. The
  time is the recipient's newest 0x43 time advanced by the server time since it arrived, and
  never decreasing; remote entries are clamped to the client's current time
  (`player::time_warp`, `player_tick.cpp:163-177`). Only alive, inserted players; never the
  recipient itself. Input bits are relayed, so the remote client animates fire and switches
  weapons through `process_quick_slots_for_proxy_player` (bits 0x1000/0x2000).
* **No echo of the local player.** For the local player `time_warp` rewinds to the entry's time
  and re-simulates every newer history item through the physics controller
  (`replay_history` -> `update_history_item_from_previous` -> `update_action`), with the
  controller's *present* jump state. Echoing the client's own reported state is therefore not a
  no-op: it replays jump frames and moves the target, which `player::smooth` then chases. The
  history is a ring that overwrites its oldest item (`circular_buffer::new_item`), so nothing
  needs trimming. Without server corrections the local player is pure client prediction; the
  remaining jump feel is the client's animation-driven jump (`jump_logic_state_start`
  -> `player::jump` on both the target and current physics controllers, `smooth_linear_speed`
  3 m/s). [V] `player_tick.cpp:142-219,267-393`, `circular_buffer_inline.h`, `player.cpp:807-818`
* Acks: the server sends a datagram (data or keepalive, with acks) every 33 ms tick, so the
  client never resends its reliable 0x43 on a clean network (asserted in `tests/test_m3.py`).
* A player whose session ends is turned into a hidden body on the other clients: 0x84 with
  is_alive = 0, then 0x91 visible = 0; an unsolicited 0x8b updates the connected mask (the client
  just answers 0x46). Rejoining re-sends 0x84 alive, which shows the model again.

### 11.4 Combat

* **Fire** is derived from 0x43 in the shooter's own clock: rising/held bit 0x20, the active
  weapon (bits 0x1000/0x2000 switch, 0.7 s show; section 13.2), `rounds_per_minute`, the fire queue
  (`fire_queue_types`, -1 = automatic; bit 0x400 cycles it; a queue needs the trigger released,
  `weapon_core::reset_fire_queue`), magazine + chamber, reload on bit 0x40 or an empty magazine
  (`reload_time`, `load_magazine`), bit 0x800 swaps the ammo slot. No fire while sprinting
  (`player_input_inline.h`) or outside "in process". [V] `weapon_core.cpp:368-705`; [A] the show
  and chamber timings.
* **Hits**: per pellet, one ray from the eye (1.62 m, 1.05 m crouched) along the direction
  of `ballistics.ShotModel` (11.6), against vertical capsules (1.8 m / 1.2 m crouched, r 0.35)
  at each other player's current and lag-compensated position (rtt/2 + 100 ms, at most
  350 ms). The height and side pick a body part name of `human_hit_params` (head/face,
  body/back, arms, legs, feet). When a capsule is hit, the level collision between the eye
  and that point decides whether a wall stopped the round first (11.6). Each pellet is its
  own 0x89, as each client bullet calls `hit_receiver::hit` (`bullet.cpp:545`). No travel time
  or drop. [A] capsules and height bands
* **Damage**: amount = weapon `bullet_damage` x ammo `k_damage` per pellet, armour piercing =
  `bullet_pierce` x `k_arp`, type "injury" (`bullet.cpp:545`). 0x89 goes to every client that
  knows both players; each runs `damage_model::hit_body_part` itself. The server runs the same
  code (`body_part_parameters::hit_by_type`: armour coefficient, bdb coefficients to "pain",
  thresholds, regeneration after `regeneration_timeout`), including the armour modifiers of
  `player_parameters_modifyer_cook` (per body part and hit type the items' armor/reduce/
  absorption are *summed* and *replace* the base values) and the pain-health / regeneration
  boosters. A player dies when the death affect (0) is applied to any part. Drugs, the
  lifebone, the oxygen tank, damage protectors and the other boosters: 11.8. [V] `damage_model.cpp`, `body_part_parameters.cpp`,
  `hit_type_parameters.cpp`, `player_parameters_cook.cpp`
* **0x8a** is sent for each affect the server model applies/recalls/cancels, except death, to
  the clients *other than the victim's*: the victim's own damage model is `type_apply_directly`
  and computes them itself; remote copies are `type_read_only` (`player_cook.cpp:160`).
* **0x9e is not sent.** The client already computes the same health from 0x89 and resets it on
  every 0x84 (`player::insert_alive` -> `damage_model::reset`). Sending it would also need the
  per-recipient clock for `last_hit_time` and the body-part order, and the retail pair is
  inconsistent: `body_part_parameters::serialize` writes the affects count as u32 while
  `deserialize` reads a u8. The encoder (`messages.encode_damage_model_state`) follows the reader.
* **Death**: 0x83 {victim, killer, headshot = head/face hit, item = killer's weapon dict id; for
  0x44 suicide killer = victim and item 0}; `on_player_killed` dereferences both players and
  `item_by_id`, so both must be roster ids and the dict id valid or 0. 0x88 K/D for victim and
  killer (team kills give no kill). 0x87 counts the respawn down on the victim every second, then
  0x84 at a team respawn point (random front point; [A] the original choice) to everyone and
  0x87 0. 0x84 re-sends full ammo and new PRNG seeds.

### 11.5 Remaining uncertainties added by M3

1. The exact retail meaning of the 0x95 score delta (11.2) and the original win rule.
2. ~~`look_pitch` as an angle~~: resolved in 11.6, it is a look-animation time, not radians.
3. Show/chamber/reload timings, use radii, capsule sizes and body-part height bands.

### 11.6 World collision, dispersion, recoil

**Level collision** (`match/level_collision.py`, cache `match/data/level_03.collision` built by
`match/tools/build_level_collision.py`; loaded once at server start, ~0.05 s).

* Source: `projects/level_03/client_project` `collision_objects` (1,858 entries), which the
  client turns into static Bullet bodies with `cgroup`/`cmask` (`project_cooker_simple.cpp:114-136`,
  `base_project.cpp:42-60`). Bullets trace `ray_test(.., group 16, mask 8)` (`bullet.cpp:402`), so
  only `collision` (8/16) and prop (10/20) bodies count; the 673 `walker_collision` (2/4) bodies
  are movement-only and skipped. [V]
* Each `lib_name` is `models/<m>.model/collision[#[sx][sy][sz]]` (suffix = baked scale,
  `collision_shape_cook.cpp:26-44`): `vertices` (chunk 0x19 float3), `indices` (0x1a u32),
  `face_data` (0x1b maya_sg names, 0x1c u16 per face) mapped to game materials through the
  model's `settings` `game_material_settings`, plus `exported_primitives` (boxes, cylinders,
  spheres; tessellated). Transform = rotation (`create_rotation(float3)`) + position. [V]
* Coverage: 1,183 of 1,185 bullet-visible objects, 587,645 triangles from 342 models: terrain
  (9 `terrain_p_*`, 18k), buildings/level (192k), props (151k), vehicles (130k), flora trunks
  and canopies (81k). Missing: `props/electric/soviet_comp_01` (2 objects, no collision data),
  grass/bush vegetation (`vegetation/*.veg`, render-only in the client too), dynamic objects.
  All 43 respawn points have ground 0.0-0.19 m below (`level_03.collision.json`).
* Rules, as `bullet::check_collision`/`process_ray_query`/`collide_front_face`: back faces are
  skipped (`kF_KeepUnflippedNormal`, `bullet_physics_world.cpp:342`); a front face stops the
  round when the material `resistance` > `bullet_pierce x k_arp`, otherwise the round pierces
  with speed x clamp(pierce/resistance - 1, 0, 1) (glass 0.1, foliage/grass 0, cloth 0.2,
  wood_squeak 0.4, brick 0.5; concrete, metall_thick, stone, ground 1). A grazing hit within
  `k_ricochet x ricochet_angle` ricochets; the server ends the line there. [V]
  [A] the front-face sign: following the z-mirror through Bullet on paper gives the opposite
  sign; the data (every terrain triangle, the barracks floors) shows (v1-v0)x(v2-v0) is the
  outward normal. [A] terrain `l3terr_` is material `grass` (resistance 0), so the client lets
  bullets through hills; the server makes the 9 terrain models solid unless
  `solid_terrain=False` (`python -m match --client-terrain`).
* Use: only when a capsule is hit is the segment eye -> hit point traced (misses cost no
  geometry). 1 m x/z grid, y-range cell skip, Moller-Trumbore: ~0.5 ms per 100 m ray; 24
  rays of 150 m per tick ~10-20 ms. Also: respawn points no living enemy can see are
  preferred (budget 48 rays), and the jump state for dispersion probes the ground.

**Aim frame** (`match/ballistics.py`). The bullet leaves along the k axis of the animated
Head (`weapon_core::update_bones_matrices` -> `set_fire_bullet_transform(character_head_transform)`).
Pitch comes from the additive look clip at time `look_pitch/2 + 0.5`; measured from the clips
(`match/tools/extract_view_animation_angles.py`, `match/data/view_animation_angles.json`):
look_pitch -1/0/+1 = -75.4/0/+70.7 deg standing, -70.5/0/+73.0 crouched, piecewise linear.
The 0x43 `look_pitch`/yaw only ever receive mouse deltas (`player.cpp:300`); recoil is not in
them, so the server adds its own replayed recoil and nothing is applied twice. [V]

**Dispersion** (`weapon_core::get_dispersed_bullet_dir`, `dispersion_calculator.cpp`): angle =
`get_dispersion() x clamp(normal_random.rand_n(1), -1, 1)` degrees around a random axis
(`random32.random_f(2 pi)`), with the weapon's two PRNGs seeded by the 0x84 seeds, so the
server draws the client's own sequence. `get_dispersion = base_dispersion x ammo.dispersion
x (aimed ? aim_multiplier : from_the_hip_multiplier) + (weapon + character) x (1 + booster 1 / 100)`.
Character term (`character_dispersion_calculator.cpp`, `default.player`): idle 0.5, idle aim 0.1,
walk 1, walk aim 0.2, crouch 0.3 / aim 0.05, crouch walk 0.25 / aim 0.1, sprint 5, jump 3,
x injury penalty (broken arms = affect 3 on left/right_arm); rises at once, decays at
`speed_of_aiming`, smoothed at 5/s. Weapon term (`weapon_dispersion_calculator.cpp`): +reload
amount on reload (cap 1), grows at 5/s, decays at `speed_of_aiming`. Retail quirks kept:
`weapon_dispersion_params` zeroes `one_shoot_dispersion_amount` after reading it, and
`growth_speed`/`max_dispersion` are never used, so spread does not grow per shot
(`spread_growth_from_config=True` / `--spread-growth` applies the config value). Examples: AK
idle hip 0.8 deg, aimed 0.4, walk 1.3, sprint 5.3; rem_870 buckshot 2.3. [V];
[A] the decompiled second `create_rotation` has its arguments in a physically impossible
order (90.7% match, the residual is that argument), the cone reading is used; aiming = bit
0x80 while not sprinting/reloading; jump = bit 0x10 or feet > 0.3 m above ground.

**Recoil** (`weapon_recoil_calculator.cpp`, `character_recoil_calculator.cpp`, exact port incl.
its random32 seeded 0 per weapon for the whole match): `fire()` kicks the target by
`(first_)shoot_side_recoil x max(0.25, rand)` at `shoot_recoil_min..max_angle` (0 = up), an
additive counter-kick after `additive_recoil_time` (150-210 deg = down), the coefficients
follow the target over 0.1 s and `side_compensation_speed + |target|` pulls the target back,
but only while the additive timer is pending: afterwards the residual stays until the next
shot (retail). Reload / chamber_a_round reset the targets. View turn from the recoil clips
(`selected_animations`: vertical t = clamp(v) + 0.5, horizontal t = 0.5 - clamp(h)): about
43 deg pitch and 37 deg yaw per unit (+-21.5 / +-18.7 at the clip ends); `recoil_back` does not
move the Head. Breath vibration is zero (`update_breath_vibration` sets multiplier 0). [V];
[A] the client ticks per frame, the server per 0x43 and per shot time; a desync of the
recoil PRNG/timing shifts the server's shot by the difference (typically < 1 deg).

### 11.7 Several match servers (poc-server scaling)

* **Any port.** op 51 carries a `u16` port that the client uses as is: `network_client::
  on_lobby_packet_received` reads it and calls `udp_match_client::connect(host, port, ...)`,
  which builds the endpoint from it (`network_client_lobby.cpp:43-52`,
  `udp_match_client.cpp:115`). The PoC runs each match worker process on its own UDP port
  (`--match-server` port + k) and op 51 names the port of the match's worker. Reconnects
  and the "continued connection" of 11.1 go to the same address, so they reach the same
  worker. [V]
* **Fixed-rate tick.** 30 Hz is held on Windows only with a 1 ms timer period and a
  sub-millisecond loop clock (asyncio's monotonic clock has 15.6 ms steps; a plain
  `sleep(0.033)` gave 47 ms ticks). [V measured]
* **Silent peers.** Every 0x82 is reliable, so a client that stopped answering would
  collect 30+ resent messages per second until the 120 s timeout. The server stops queueing
  0x82 to a peer it has not heard from for 3 s (the client's own HUD already shows
  "connection lost" then, 1.4) and resumes when it is heard again. [A]
* **Unhandshaked endpoints.** A remote endpoint that does not send a valid 0x40 within
  15 s is dropped (it would otherwise get a keep-alive every 33 ms for 120 s), and at most
  256 such endpoints exist at once. [A]

### 11.8 Items: booby traps, drugs, artefacts, oxygen tank, boosters, consumption

The item class comes from the config's `data.type` (`item_types_enum.h`, `items_cook.cpp`):
0 medkit (all three drugs), 1 oxygen tank (back slot), 2 booby trap set, 3 lifebone. The
v0.100b dictionary has no other usable item (no anomaly protectors, no other artefacts).
Code: `match/items.py` (records, geometry) and `Match` in `match/game.py`.

**Booby traps.** In a networked match the client never places, fires or defuses a trap:
`booby_trap_set::action` calls `try_place_trap` only without bandwidth (offline),
`booby_trap::register_tick` (which runs the collision sensor) and `defuse_completed` return
early with bandwidth, and the trap state changes only through 0x96-0x99 / 0x9c
(`game/sources/booby_trap.cpp`, `booby_trap_set.cpp`). [V] The server therefore does all of it:

* *Wire.* The slot byte must name a slot holding a booby trap set (the handler
  `static_cast`s whatever item is there) and the index must be below the set's trap count,
  which is the u8 of the slot's `condition_or_stack` in 0x92 (`inventory_cook.cpp:91-93`,
  `booby_trap_set_core_cook`). 0x96 also does `--m_amount` on every client, so the 0x84 amount
  the server sends next already counts it. 0x97 for a trap that is not in the world calls
  `remove_game_world_object` on it: never sent. [V]
* *Place* on the rising edge of a quick slot's **up** bit (0x8000 << 2k; key release =
  `action(false)`), alive, in process, amount > 0, a free trap index: a ray from the eye along
  the view (look animation pitch, no recoil), `max_deploy_distance` (2 m) long, must hit an
  upward face within `max_slope_angle` (30 deg) whose game material may hold a mine
  (`game.materials` `mine.can_place`, `material_can_place_test`). Standing, the 2 m ray reaches
  the ground up to about 1.17 m ahead. Position = hit point; rotation =
  `create_place_matrix_for_looking_point` (up = surface normal, forward follows the view);
  angles = `get_angles(rotation_zxy)` as `booby_trap_core::serialize` writes them (the client
  rebuilds with `create_rotation(angles)`; both agree on level ground). [A] the decompiled
  `get_visible_place_transform` tests read inverted (they return false on success), the server
  follows their evident intent; the client ray uses the walker collision (0x404/0x202), the
  server the bullet collision cache; no `recover_from_penetrations` nudge; without a level cache
  the ground is the plane through the player's feet.
* *Trigger.* While armed, every alive player whose feet are inside the sensor box (config
  `collision_sensor`: 0.25 x 0.1 x 0.25 half extents at +0.12 m) grown by a 0.15 m foot radius,
  at most 0.3 m below it, takes the `damage_parameters` hits (right_foot 1, left_foot 1, pain
  1.5, injury, armour piercing 1) as 0x89 with the owner as initiator and the trap's dict id in a
  resulting 0x83; then the trap fires: 0x98, and after `fired_life_time` (3 s) 0x97
  (`booby_trap_core::on_enter`, `switch_to_state`, `on_state_timer_finished`). Broken feet put
  leg damage (affect 4) on both legs. [A] the foot model; the client sensor has no team filter,
  the server lets the owner and his team trigger it only with `--friendly-fire`.
* *Defuse with use.* While the use bit is held and the 1 m ray from the eye
  (`s_usable_objects_detection_distance`) meets the trap's usable box, the owner or an enemy
  (`can_defuse`) defuses it after `defuse_time` (5 s) x (1 + engineer_use_time booster / 100) of
  the player's own clock; releasing or looking away starts over (`use_initialize/execute/
  finalize`). From a standing eye 1 m does not reach the ground: crouch and look down. Then
  0x99, and 0x97 after `disarmed_life_time` (3 s). [A] the boxes are axis aligned.
* *Defuse by hit* (`defuse_by_hit`): a round that reaches the trap's hittable box (0.125 x 0.05 x
  0.125) before any player or wall disarms it (`booby_trap_core::hit`); the round stops there [A].
* *Lifetime.* `armed_life_time` 0 = armed until triggered. A player's (re)spawn 0x84 and the
  hidden-body 0x84 of a player who left make every client remove that player's traps
  (`player::remove` -> `inventory::remove` -> `booby_trap_set::remove`); the server forgets them
  silently. A client that joins later gets one 0x9c per active trap after the owner's 0x84
  (`booby_trap_core::deserialize` inserts without touching the amount).

**Drugs** (medkit, bandages, painkiller; `medkit.cpp`). The quick slot's **down** bit runs
`medkit::action(true)`: nothing while this slot's drug is active (`m_active`), else one item
less. The damage protectors register at once and stay until the activity ends
(`set_active`): the painkiller's pain/injury hits become (amount - 0) x 0.2 for 6 s
(1 s delay + 5 s). After `activation_delay` the `remove_affects` are cancelled and the
`influences` heal amount/`activity_time` per second [A: spread per server tick].
`add_stamina_regen` is not simulated (the server has no stamina; 0x84 always sends full
stamina).

**Lifebone** (`artefact_lifebone_core.cpp`): passive for the whole match from the moment the
inventory gets its holder: its protector blocks hand damage (3) and leg damage (4) on
left/right hand and leg, damage itself passes. Its quick-slot key resets those four parts
(full health, affects dropped) without spending anything (`amount` -1 = unlimited; the
config's `cooldown_ms` is never checked by `action`). 0x84 has no bytes for it.

**Oxygen tank** (back slot, `oxygen_tank.cpp`): the back-slot key (0x4000000) toggles it while
time is left (60 s, spent only while on); while on, intoxication on `infection` is
(amount - 10) x 0 and irradiation on `radiation` (amount - 15) x 0.5. The server deals no
anomaly damage (level_03 anomalies are not simulated), so this only matters if such damage is
added.

**Affect events to the other clients.** A read-only (remote) damage model handles only
"applying" and "recalling" in `body_part_parameters::apply_affect_by_force`; "canceling" is
ignored. A medkit's cancelled affects and the lifebone's reset are therefore sent as recalling
(1), which removes them on the other clients. [V]

**Boosters** (`player_parameters_modifyer::apply`, ids from `boosters_enum.h`):

| id | booster | server |
|---|---|---|
| 1, 2 | dispersion, aiming speed | shot model (11.6) |
| 3, 7 | health regeneration, pain health | damage model |
| 9 | anomaly damage | `add_damage_protector` x (1 + v/100) for irradiation, ambustion, intoxication, electric_shock (no such damage on the server yet) |
| 10 | engineer use time | trap defuse time |
| 4 | stamina regeneration | not simulated (no server stamina) |
| 5 | movement speed | not simulated (movement is client-authoritative, section 6) |
| 6 | additional max weight | not simulated (the server has no carried-weight model) |
| 8 | artefact container search time | not simulated (no anomalies / artefact containers on the server) |
| 11 | engineer success chance | loaded by the client, never used by any client code |

**Consumption.** Every round fired is counted against the ammo slot it came from, every drug
used, trap placed and limited lifebone charge against its quick slot (`Player.used`). A new
life gets min(`condition_or_stack`, `amount_in_inventory` - used) per ammo/quick slot, as
`inventory::setup_from_profile` gives the client after `unload_to_profile` returned only the
remainder. [A] a spawn still fills the weapons' magazines without taking the rounds from the
slot (the server's earlier behaviour); those rounds count once fired. The match result carries
`used` (14.4) and the lobby takes it out of the account.


---

## 12. Messaging/chat

Chat does not use the match server. There is no chat message in the match catalogue (section 4). It runs on its own TCP service, `chat_tcp_port = 25102` (`login_server/constants.h`). The client side is `game/sources/messaging_client*.cpp`, `chat_handler.cpp`, the friends UI in `lobby_menu*.cpp`, and the Scaleform movie `flash_movies/chat.swf` (AS3 `survarium.chat.GameChat`). The PoC implementation is `poc-server/chat.py`. Paths are relative to `vostok/sources/vostok/game/sources/` unless they say otherwise. [V] means checked in the client sources or in the chat.swf bytecode; [A] means an assumption.

### 12.1 Discovery and connection

* **Address.** After login, `http_query_server_connection_info(4)` asks the HTTP browser for `...&type=4&...`. The body `host:port` goes to `messaging_client::connect` (`network_client.cpp:315-343`). With no browser address configured, the client falls back to the compiled `188.93.23.27:25102` (`network_client.cpp:307-309`). [V]
* **Disabled chat.** `connect` dials only `if (strcmp(host, "x") != 0 && port != 0)` (`messaging_client.cpp:67`). The answer `x:0` therefore turns chat off silently. An unparsable answer sets `need_resolve` instead and is retried (`network_client.cpp:338-342`). [V]
* **When.** The client queries type=4 only once the lobby connection no longer needs resolving, and at most every 3000 ms while `need_resolve` is set (`network_client_processing.cpp:555,572-579`). Chat failures never sign the player out; only lobby failures do. [V]
* **Reconnect.** Every socket failure ends in `on_error`. That covers connect refused, read error and remote close, because the socket layer reports EOF as `unable_to_read_from_socket` (`network_core/tcp_packet_socket_inline.h:21-36`). `on_error` shows the system line "Lost connection to messaging server. Reconnecting...", disconnects, and sets `need_resolve` (`messaging_client.cpp:103-113`). The next type=4 query reconnects with the **same login session_id**. `on_disconnected` is never wired by the socket layer. A server that closes the connection therefore makes the client reconnect within about 3 s. [V]
* **Framing** is the same as the lobby: `[u8 len][payload]`, or `[0][u16 len][payload]` when the payload is 256 bytes or more. Little-endian. `str` is `[u8 len][bytes]` (`network_core/packet_inline.h:117-128`). The reader asserts `len < 255` and copies the string into a fixed buffer (`packet_reader_inline.h:34-46`). [V]
* **Text encoding.** The UI is wide (`wchar_t`). The client calls `setlocale(LC_ALL, "")` (`client/sources/entry_point.cpp:418`), and every chat string is converted with `wcstombs_s` / `mbstowcs_s` (`messaging_client_process_messagess.cpp:130,138,144,307,311`). The wire therefore carries the sending PC's **ANSI code page** (Windows-1251 on a Russian system), not UTF-16. A character that is not in that code page turns the whole body into `##text conversion error##`. The server should relay bodies as opaque bytes. Clients on different code pages see mojibake for non-ASCII text. [V]

### 12.2 Message ids

| dir | id | name | layout | client code |
|---|---|---|---|---|
| C→S | 0xC3 | sign_in | u32 session_id (login), u8 client_type = 5 (`account_client_type`) | `messaging_client.cpp:81-96` |
| S→C | 0xCB | signed_in | str local_name (< 32) | `messaging_client_sign_in.cpp:12-36` |
| C→S | 0xC5 | channel_subscriptions | raw `u32[9]`, indexed by `message_channel_enum` | `messaging_client_sign_in.cpp:38-53` |
| C→S | 0xC1 | send_text | u32 channel_id, str receiver_name (< 32), u8 channel, str body (≤ 255) | `messaging_client_process_messagess.cpp:107-186` |
| S→C | 0xC9 | text_message | u8 sender_type, u32 sender_account_id, str sender_name (< 32), u8 channel, str body (< 255) | `messaging_client_process_messagess.cpp:290-322` |
| C→S | 0xC4 | friendship | u8 action, then a per-action argument (12.5) | `messaging_client_process_messagess.cpp:188-279` |
| S→C | 0xCC | friendship_answer | u8 action, then a per-action body (12.5) | `messaging_client_process_messagess.cpp:32-73`, `messaging_client.cpp:115-173` |

Any other id after sign-in is logged as "messaging_client received unknown message" and dropped (`:76-78`). During sign-in, any message other than 0xCB is logged and ignored, and the client keeps waiting (`messaging_client_sign_in.cpp:16-20`). [V]

**Enums** (`messaging_enums.h`):

* `message_channel_enum`: 0 server, 1 general, 2 system, 3 clan, 4 private, 5 match, 6 team1, 7 team2, 8 squad.
* `friendship_actions_enum`: 0 add_friend, 1 remove_friend, 2 add_ignorable, 3 remove_ignorable, 4 find_players, 5 query_friend_list, 6 query_ignore_list, 7 update_friends_status.
* `client_type_enum`: 4 message_server, 5 account, among others.

### 12.3 Sign-in and subscriptions

1. TCP connect, then `on_connected` sends `C3 <u32 session_id> 05`. [V]
2. The server answers `CB <str name>`. The name goes to `m_local_name` (`char[32]`). It is the sender name of the client's own local echo and is shown by `root.set_local_player`. The client also shows the system line "Connected to messaging server." [V]
3. The client immediately sends `C5`, then `C4 05` (friend list), then `C4 06` (ignore list) (`messaging_client_sign_in.cpp:33-35`). [V]

The `C5` array is `{0, 0xFFFFFFFF, 0xFFFFFFFF, 0, 0, match_id or 0, 0, 0, 0}` (`messaging_client_sign_in.cpp:43-45`). Slot 5 is the lobby's `match_id` from op 51 or from status 0. `assign_match_channel_order` stores it and the team, then re-sends `C5` (`network_client_lobby.cpp:55,68`, `messaging_client.cpp:41-52`). A value of `-1` is ignored, so **the match channel id is never cleared** after a match. The team is never sent. [V]

### 12.4 Sending and receiving text

**What the UI hands over.** chat.swf calls `send_function(text_input, tab_id)`. `chat_handler::call` ignores the tab id and always passes `player_general_channel` (`chat_handler.cpp:75-82`). The channel therefore comes **only from the input text**. chat.swf keeps the selected tab's key at the start of the field: `setNewAdress`, `onKeyEnter` and `privateMessage` write `"/general "`, `"/all "`, `"/<name> "` and so on. The tab keys come from `chat_handler::set_mode`: lobby tabs are `/general`, private (no key), `/clan`, `/squad`, system (no key); game tabs are `/team`, `/all` (`chat_handler.cpp:228-243`). [V chat.swf `GameChat::sendText`/`setNewAdress`/`privateMessage`]

**`on_message_typed`** (`messaging_client_process_messagess.cpp:107-186`) [V]:

* `"/word rest"` sets receiver = `word` and channel = `parse_receiver_channel(word, in_match)` (`:86-105`). That function compares prefixes in Russian and English:
  * `general` gives 1, `squad` gives 8, `clan` gives 3.
  * In a match only: `team` gives 6, or 7 if `m_game_team_id != 0`; `all` gives 5.
  * Anything else gives **4, a private message to that name**.
* Text without a leading `/` keeps channel 1. For any channel other than 4 the receiver is set to `""`.
* **The client echoes locally.** If connected, it first calls `add_message(channel, text, local_name)` (`:132`); for private it also adds the receiver to the recent list. The server **must not echo** a message back to its sender, or it appears twice. If not connected, the client prints "not connected to messaging server..." and sends nothing.
* `channel_id` by channel:

  | channel | channel_id | sent? |
  |---|---|---|
  | 1 general, 2 system, 4 private | 0 | yes |
  | 3 clan | — | **no** (`return`) |
  | 5 match | `m_match_channel_id_` | yes, unless it is `-1` (never in a match) |
  | 6, 7 team | — | **no** (`return`): team chat is echoed locally and never sent |
  | 8 squad | `0xFFFFFFFF` | yes, but only if `m_match_channel_id_ != -1` |

* Packet: `C1 <u32 channel_id> <str receiver> <u8 channel> <str body>`. The body is `char[256]` truncated, so up to 255 bytes. A frame over 255 bytes uses the u16 form.

**`process_incoming_text_message`** (`:290-322`) [V]:

* It reads `u8 sender_type` and `u32 sender_account_id`. If `sender_type == 5` and the id is on the client's ignore list, it stops there (`accept_message_from`, `:281-288`).
* Otherwise it reads `sender_name` (`char[32]`: **31 bytes max**), `u8 channel` and `body` (`char[256]`, read with buffer size 255: **254 bytes max**, because of the `len < 255` assert).
* Then it dispatches on the channel:
  * **7** goes to `lobby_menu::on_match_message_arrived`. This is the match-making feed (`#+p:[ name ]#t:[team]`, `#-p:[ name ]`, `#q:[n]`), never shown as chat (`lobby_menu_ui.cpp:1422-1477`).
  * **8** goes to `lobby_menu::on_stats_message_arrived`. This is the stats feed: `Player [ name ] #e:[exp]` is shown only to that player and refreshes money and skills; `#pc:[n]` goes to `root.set_games_online` (`lobby_menu_ui.cpp:1479-1531`). The PoC sends both feeds (12.8).
  * **Any other channel** calls `chat_handler::add_message(channel, text, sender)` and `add_to_recent_list(sender)`. The recent list is skipped in game mode (`chat_handler.cpp:218-226`).
* In game mode, a channel-5 line is shown red if `network_client::get_player_team(sender_name)` finds the sender on the other team. Otherwise it is prefixed `[st_to_all]` (`chat_handler.cpp:185-210`). The lookup compares against the match's **profile names** (0x92), not account names. [V]
* **Display filter** (chat.swf `GameChat::got_message`): every line is stored under the main tab and under its own type. It is shown when its type equals the selected tab, or when the main tab is selected outside frame 4, or **in frame 4 (the in-game chat) when its type is 5..9**. In-game players therefore do not see general (1) or private (4) lines until they are back in the lobby. [V bytecode, A: frame 4 = game mode]

### 12.5 Friends, ignore list, player search

These requests go from the lobby UI (`lobby_menu_ui.cpp:245-270`) to `C4 <action> [arg]`:

* `C4 00/01/02/03 <u32 account_id>`: add or remove a friend, add or remove an ignored player.
* `C4 04 <str name>`: find players. The UI sends it only for names of 3 or more characters.
* `C4 05`, `C4 06`, `C4 07`: query the friend list, the ignore list, the friends' status.

| answer | body | client reaction |
|---|---|---|
| `CC 05` | u16 n, n × {u32 id, str name (< 32), u8 online} | `fill_friend_list` (`root.set_friends_list`, status 0 online / 2 offline); schedules `C4 07` in 10 s (`lobby_menu.cpp:294-305`) |
| `CC 06` | u16 n, n × {u32 id, str name} | `fill_ignore_list`; the ids feed the client-side ignore filter |
| `CC 04` | u16 n, n × {u32 id, str name} | `fill_found_players` (`root.fill_players_search`) |
| `CC 07` | u16 n, n × {u32 id, u8 online} | updates the flags only, with **no UI refresh**; an unknown id logs "Friend list out of sync." |
| `CC 00..03` | u8 result | `'4'` (0x34) re-queries the friend list (0, 1) or the ignore list (2, 3); anything else logs "operation denied" |

Every `CC` ends in `lobby_menu::on_friendship_status_recivied(action)`. [V]

**Do not push `CC 05` unsolicited.** Each `CC 05` runs `scheduler::register_for_update(&m_update_friends_status_handler, ...)` (`lobby_menu.cpp:412-421`). That pushes a second record for an identifier that is still registered and overwrites its index (`game_core/scheduler_inline.h:34-45`). The orphaned record later unregisters the wrong slot. Two `CC 05` within 10 s already do this, even from the client's own add-then-requery flow. [V code, A: the effect on the real client was not run]

**When an unsolicited `CC 05` is safe.** The timer handler unregisters itself and then sends `C4 07` (`request_friends_status_from_server_impl`, `lobby_menu.cpp:424-429`); nothing else sends `C4 07`. So between a `C4 07` and the next `CC 05` the timer is idle, and one `CC 05` may be pushed. The online flag is a bool on the wire (`read_friend_list`/`read_friend_status`) and `fill_friend_list` maps it to status 0 (online) or 2 (offline). The UI has labels for *in game* and *away* (`st_friend_state_game`, `st_friend_state_away`), but the client never sets those statuses, so "in a match" cannot be shown. [V]

### 12.6 PoC server behaviour (`chat.py`)

* **Identity.** The session_id maps to the login account through the login server's session table. A session the login server did not issue (or that signed out) is disconnected without an answer: the client has no refusal message, it reconnects about every 3 s with the session of its current login. The lobby answers such a session with op 49 `invalid_session_id` and closes; after four failed lobby connections the client signs out and returns to the login screen (`network_client_processing.cpp:557-565`). `--accept-unknown-sessions` restores the old behaviour (the `--nickname` account) for tools that skip the login. The account id and the nickname come from the lobby store (`LobbyServer.account_summary`), so chat ids equal lobby ids and the `CB` name equals the lobby `account_nickname_`.
* **General (1).** Relayed to every signed-in connection except the sender's. Each copy is `C9 05 <account_id> <nickname> 01 <body>`.
* **Private (4).** The receiver is matched case-insensitively against the nickname or the login name of online accounts and delivered to all of that account's connections. The message is dropped if the receiver ignores the sender. An unknown or offline name gets `C9 04 0 "System" 02 "<name> is not online."`. The client then adds "System" to its recent list; this is a known cosmetic side effect.
* **Match (5).** Recipients are the players the lobby has in the sender's match (`LobbyServer.match_assignment`: state in_match and `match_id`). Without a lobby, recipients are the connections whose `C5` slot 5 equals `channel_id`. The sender name is the sender's **profile name** from the match ticket, so `get_player_team` colours the line correctly. A channel-5 message from someone the lobby has not placed in a match is dropped; the client's match id goes stale after a match.
* **Team.** Squad (8) and, if ever received, team (6/7) go to the players in the same match and the same team. They are delivered as channel **6** to both teams, because 7 and 8 are status feeds on the client. Players reach their team by typing `/squad text` in a match. The game's own **team tab (`/team`) never sends** (12.4).
* **Not routed.** Clan (3), system (2) and server (0) messages from clients. Bodies are capped at 254 bytes and names at 31.
* **Friends.** Implemented and persisted in `state/chat_state.json`, keyed by login account. Online means at least one live chat connection (players in a match keep theirs, so they count as online). Search is a case-insensitive substring match over all lobby accounts (50 results max).
* **Friend status pushes.** When an account comes online (its first chat connection) or goes offline (its last), every signed-in player with it in their friend list gets a fresh `CC 05`, the only answer that redraws the list, under the rule above: per connection the server counts the `CC 05` it sent whose `C4 07` has not come back. At zero it pushes at once; otherwise it marks the list stale and pushes right after answering the next `C4 07`. A connection that has not asked for its list yet gets nothing (its own `C4 05` follows the sign-in).
* **Reconnect.** A new `C3` for a session that already has a connection closes the old one. A dead socket from before the client's reconnect is the common case.
* **Robustness.** Truncated or garbage packets are logged and skipped, and the connection stays up. Packets before sign-in, other than `C3`, are ignored.

### 12.7 Uncertainties

1. The meaning of the non-match `C5` slots (`-1` for general and system) and how the retail server used them. The PoC ignores everything except slot 5.
2. The retail failure codes for `CC 00..03`. Only `'4'` is defined by the client; the PoC sends `'0'` for denied.
3. Whether the retail server echoed to the sender or delivered team chat at all. The client sends neither, so the PoC does neither.
4. The retail wording of the `#q:[...]` value (12.8). The client copies it verbatim into the "waiting for players" field; the PoC sends `n/size`.
5. `root.add_player` argument order: the client passes `(team, {name, icon})` while `match_making.swf` declares `add_player(param1:Object, param2:uint)` and forwards `addPlayer(param1, param2)`, i.e. the object is expected first. If the decompiled order is right, the retail movie throws inside `addPlayer` (`.name` of a number) and the columns stay empty, whatever the server sends. [V source and swf bytecode; A: not run against the real client]

### 12.8 Lobby status feeds on channels 7 and 8 (PoC)

The client never shows channels 7 and 8 as chat (12.4): they carry status lines for the lobby menu. The PoC sends them from the message server (`C9 04 <id 0> "System" <channel> <body>`, so the ignore filter never applies). Wire forms are the ones `lobby_menu_ui.cpp:1414-1532` parses; every value is copied with `wcsncpy_s` into a fixed `wchar_t` buffer, and a value that does not fit terminates the client (invalid parameter), so the limits below are hard.

**Match-making window, channel 7** (`on_match_message_arrived`). Each line may hold one join, one leave and one queue value; the PoC sends one item per line:

| line | effect | limits |
|---|---|---|
| `#+p:[ <name> ]#t:[<team>]` | `root.add_player(team, {name, icon: 0})`: column 0 (team A) or 1 (team B); a name already in that column is ignored | name up to the first `" ]"`, < 32 chars; `#t:[` must be present (a join without it dereferences NULL); team < 8 chars, `_wtoi` |
| `#-p:[ <name> ]` | `root.remove_player(name)`: first match in column A, else B | name < 32 chars |
| `#q:[<text>]` | `root.set_place(text)`, the field labelled "Ожидание игроков:" (waiting for players) | < 16 chars |

* The window exists for the whole lobby session (loaded before the network client is created, `game.cpp:445-470`). `show_match_making(true)` restarts the movie (empty columns, empty place) whenever the window is shown, i.e. on the first `in_match_making` state of an order. The PoC therefore starts an order's feed only when the client **polls** its state while queued (`request_status_from_server(1000)` after that first state): by then the window is up and a line cannot be wiped by the restart, even though chat and lobby are separate TCP connections.
* What a waiting player sees: the queued players it would be matched with (the queue in order, `--match-size` at a time), named by the **profile** they queued with, in the team columns the matchmaker will use (alternating within each match, so the first player of every match is team 0), and `#q:[n/size]`. Names are sanitised for the parser: `[`, `]` and `#` become `_`, at most 31 characters.
* Joins and leaves are sent as diffs when the queue changes (a player queues, discards the order with op 39, disconnects, signs in again, or is matched) and at every poll. When a match forms, every player whose window is up gets the final roster with the real teams and `#q:[n/size]`; the window then switches to the level loading view, which still shows the columns.

**Online counter, channel 8** (`on_stats_message_arrived`): `#pc:[<n>]` with `n` the number of accounts that have a chat connection. It goes to `root.set_games_online(n:uint)` (the status panel's online figure; < 8 chars). The PoC sends it right after `CB` and to every signed-in client when the number changes, coalesced to one broadcast per second. A line that contains `Player [ ` is taken as a match result instead, so the counter is always on a line of its own.

---

## 13. Equipment, weapon switching and weapon upgrades

Paths are relative to `vostok/sources/vostok/`. [V] = checked in the client sources (and, for the Flash UI, in the `inventory.swf` strings).

### 13.1 Which loadout a match uses

The lobby ticket (`lobby.py issue_ticket`) carries every slot of the profile the player pressed Play with (`loadout`: slot, dict_id, id, condition_or_stack, amount) and `match/game_data.py ticket_from_dict` reads it back; the same slots go into every client's 0x92 and, for slots 7..18, into the 0x84 inventory tail (2.5). A real run was checked end to end (profile with TOZ-122 + 7.62 in weapon1, AK-74u + 5.45 in weapon2: first weapon is the TOZ, key 2 shows the AK with 30/90, key 1 the TOZ again). The server only substitutes the AK-74u default loadout (`default_loadout()`) when there is no ticket at all (open match, warning "no lobby ticket") or when nothing usable is left in it.

* **Per-item sanitising.** `GameData.sanitize_loadout` drops only the slots that must not be sent (out-of-range slot, unknown dict_id, an item that cannot occupy a slot such as a scope, id 0) and logs them as "ticket items dropped from the loadout". Before, one such item discarded the whole equipped loadout for the default one. If no weapon remains in slot 7 or 10 the default is used (2.3).
* The accepted loadout is logged on every session bind: `session N -> player i 'name' team t (match m, loadout 7:12 8:51 10:13 11:7 ...)` (slot:dict_id, armour left out).

### 13.2 Weapon switching

* The client changes weapon from `actions_mask` bits 0x1000 (slot 1) and 0x2000 (slot 2) (4.1). The server acts on the rising edge (`Match._weapon_input`): the active slot becomes 7 or 10 if that slot holds a weapon, a running reload is cancelled and the new weapon is ready after the show time (11.4). Fire, magazine, reload and `next ammo type` then use that weapon's own ammo slots: weapon1 -> 8/9, weapon2 -> 11/12 (`ammo_slot_for`), damage and the 0x83 item id are the active weapon's.
* **What a remote client reads.** `player::process_quick_slots_for_proxy_player` (`player.cpp:909-935`, called from `time_warp` for non-local players, `player_tick.cpp:175`) changes a proxy player's weapon **only** from bits 0x1000 / 0x2000 of the relayed input; the weapon slot id and ammo slot id in the 0x82 entry are not read. The keys are down for a few frames, so one relayed 0x82 can miss them, and a client that joins later never sees them (its 0x84 names weapon1, `insert()` picks it). Once a player has switched, the server therefore ORs the select bit of its active slot into every 0x82 entry it relays about that player until the next spawn (`Player.switched`, `Match.send_corrections`). It is harmless when repeated: `inventory::action` returns at once if that slot is already active (`inventory.cpp:73-104`), and the bits are used nowhere else for remote players (`player_input_inline.h` sprint test is 0x16E). The entry's slot id / ammo slot id are the real active ones.
* **Local player.** Nothing is echoed back to it (11.3); the client switches by itself and the server follows the same bits. A respawn resets both sides to weapon1 (`insert()`).

### 13.3 Weapon upgrades / addons: what the retail client supports

**Nothing that a server can drive.** The only addon in the client is a rifle scope, and it is a property of the weapon config, not of the profile:

* `gameplay/weapons/rem_700.options` is the only weapon config with `"addons": {"rifle_scope_dict_id": 69}`. `weapon_cook::on_weapon_config_loaded` (`game/sources/weapon_cook.cpp:68-72,125-131`) resolves dict 69 through the items dictionary to `gameplay/items/scopes/leupold` (type 5 `item_type_rifle_scope`, `fov_factor` 0.25, `hide_weapon_on_aim`, `idle_model`/`aimed_model`), cooks it as `rifle_scope_class` together with the weapon and `weapon::load_weapon` stores it (`weapon.cpp:248-255`). The model is attached at the weapon's `scope_point` locator and swapped for the aimed model when the zoom passes `change_scope_factor` (`weapon.cpp:302-335,627-684`); the aim fov/near-plane factor comes from it (`weapon.cpp:149-150`). All of this is client-side rendering and camera: the server's hit trace, damage and dispersion do not depend on it (no use of `rifle_scope` in `game_core/`), and a scoped aim is the same `from the hip / aimed` dispersion state as any other weapon (11.6).
* The profile carries no addon: `inventory_item_instance` is dict_id, id, condition_or_stack, amount (2.2); the 0x92 / 0x84 formats have no field for one; the weapon record of 0x84 has none either.
* The lobby protocol has no attach/detach/upgrade operation: the client ops are exactly 32 ready_for_match, 33 query, 35 inventory_action (relocate), 36 shop_action (buy), 37 skills, 38 sign-in, 39 discard, 40 ping (`login_server/message_types.h:50-61`; `lobby_client.cpp:397-420` builds 35 and 36). `inventory_action` moves an item between a profile slot and storage; `shop_action` buys `dict_id x count`.
* The inventory UI has no addon slot: the paper doll of `inventory.swf` has `slotWpn1`, `slotWpn1A1`, `slotWpn1A2`, `slotWpn2`, `slotWpn2A1`, `slotWpn2A2` (weapon + two ammo), armour and `slotQck1..6` and nothing else. The slot restriction table the lobby serves (q_profile_slots_restrictions) has no entry for item category 22 ("scope"), and the compatibility table (q_items_compatibility) pairs weapons with ammo only.
* `profile_character::weapon_resources_ready` (`lobby_menu_scene.cpp:223-253`: model, animation and an addon model per weapon, attached at `scope_point`) is an unused prototype: `lobby_menu` owns a `profile_player_character` (`lobby_menu.h:203`), nothing constructs a `profile_character` or binds its callbacks, and the lobby character preview cooks the real `player` from the profile (`query_profile_contents`), so its weapon gets whatever the weapon config has (the rem_700 shows its scope).
* A scope placed into a profile slot is a client crash, not a feature: `inventory_cook` cooks a type-5 item in a quick slot as `rifle_scope_class` and then casts that resource to `inventory_item` (`inventory_cook.cpp:100-108,179-190`; 2.2 "do not put scopes in slots").

**What the PoC server therefore does** (it implements everything the client can use, and refuses the rest rather than inventing messages the client never sends or reads):

* No attach/detach/upgrade operation exists, so none is served. The persisted profile format is unchanged.
* The scope item (dict 69) is not sold (`lobby_data.GameData.equippable`: no slot accepts its category, so it is left out of `q_price_items` and a buy of it is denied) and every relocation of it into a profile slot is denied (`slot_accepts`). The one in the starter stash stays in storage. A scope in a ticket would be dropped by `sanitize_loadout` (13.1) instead of reaching 0x92.
* In a match, the rem_700 carries its scope through its own config; the server needs and sends nothing for it.

### 13.4 Carried weight

* **What the client shows.** `lobby_menu::player_parameters_ready` calls `root.player_profile.updateWeight(total, max)`. `total` is `player_parameters_modifyer_cook`'s `total_items_weight`: over the profile's 19 slots, `count x weight`, where count is `condition_or_stack` for stackable items and 1 otherwise, and weight is the item config's `parameters.weight`, or `parameters.clip_weight / clip_size` (one round) for ammunition (`items_dictionary_cook.cpp:103-111`; items without `parameters` weigh nothing). `max` is `default.player` `player.stamina_params.max_carried_weight` = **30** (`lobby_menu_ui.cpp:487`); the `additional_max_weight` booster is added only to the in-game stamina (`player_parameters_cook.cpp:93`), not to this figure. [V]
* **What the client enforces.** Nothing: `PlayerProfile.updateWeight` paints the figure red when `total > max`, and only the ammunition autofill and the ammo slider are capped by the free weight (`PaperDollSlot.tryFillAmmo`: whole clips in half of `max - total`; `MessageAsk`). An over-weight profile can still be played. [V inventory.swf]
* **Data quirk.** The shipped configs weigh a painkiller 5 kg and the lifebone artefact 5 kg, so a few quick-slot items already reach the limit (the starter profiles weigh 14.2, 10.1 and 19.3 kg).
* **PoC.** `lobby_data` computes the same per-unit weights and reads the maximum from `default.player`. The server-side ammunition autofill (`attach_ammo`) applies the client's own cap. `--weight-limit` (off by default, as the client allows it) denies an inventory action that takes a profile over the maximum, with `53 35 "too heavy: x of 30 kg"`; a move that lightens an already heavy profile is always allowed. Without game data the weights are unknown and nothing is limited.

`tests/test_weapons.py` covers 13.1-13.3; `tests/test_lobby.py` covers 13.4.

---

## 14. Progression: reputation, experience, unlocking weapons

Paths are relative to `vostok/sources/vostok/game/sources/` unless noted. [V] = checked in the client sources or in the decompiled `inventory.swf` (`GangShop.as`, `ShopItem.as`, `MessageAsk.as`), [R] = checked in the retail `survarium.exe` disassembly, [S] = a PoC-server choice (the retail numbers are not in the client).

### 14.1 What the client shows

* **Traders.** `lobby_menu::query_lobby_info` asks for the price lists of traders 1..4 (`lobby_client::query_prices`, `33 6 faction`) and `lobby_menu_ui.cpp:1069` again on `shop_ready`, but the shop window lists only two of them: `GangShop.fillSellers` does `param1.length = 2` on the six traders the client sends, so only 1 (Scavengers) and 2 (Black Market) can be browsed [V; confirmed on screen]. A weapon sold only by trader 3 (Renaissance) or 4 could never be bought through the UI. Every weapon is therefore offered by trader 1 or 2 (14.3). The shop tabs are all / armour / weapons / ammo / consumables / the two sellers.
* **Price list** (query 6, `on_price_items_arrived`): `u8 faction, u16 count, count x {u16 item_dict_id, u16 cost, u8 reputation_level, u8 pad}`. For each level `0 .. levels_count-1` of `factions_dict.faction_<id>.levels` the client calls `root.setup_shop_data(trader, items of that level, level, level name, level value)`; an item whose `reputation_level` is not below `levels_count` is never shown. Stackable ammo is priced **per round**: the tile shows `price x clip_size`, the buy dialog counts rounds in steps of `clip_size` and sends `buy_ok_clicked(dict_id, rounds, trader)` -> `shop_action {dict_id, count = rounds, faction, premium}`.
* **Locked or available.** `GangShop.setupShopData`: `canBuy = _unlockedLevel[seller] > item.level`, with `_unlockedLevel` starting as `[1,1,1,1,1,1]`. `ShopItem.updateText` shows the item normally (buy button, drag) if `canBuy` and the player can afford at least one unit (`genericMoney / price >= 1`); otherwise a **lock icon** covers the tile, the buy button is disabled and the tile cannot be dragged [V; confirmed on screen]. The level names and values sent with `setup_shop_data` are stored (`_reputations`) but never displayed. Nothing else in the UI shows reputation.
* **The reputation call is wrong in retail.** `lobby_menu::on_player_reputations_arrived` (query 11: `u8 count, count x {u8 faction_id, u8 pad, u16 reputation_points}`) computes the player's level `k` in each faction (the highest `i` with `points >= levels[i].value`, else 0) and calls `root.setup_player_progress(k, points, <unset>)` [R: the third `flash_value` is left default-constructed, the first two are the level and the points]. The flash function is `setPlayerProgress(faction, unlockedLevel, progress)`, so it executes `_unlockedLevel[k] = points`: **the reported reputation only ever sets `_unlockedLevel[level]`, and with `points` in the hundreds that opens every level of trader `k` at once.** The faction id never arrives. A faction at level 0 only touches index 0, which no trader uses.
* **Experience / skill points** (query 8: `u32 total_experience, u32 next_level_experience, u32 prev_level_experience, u8 n, n x {u8 skill, u8 points}, u8 m, m x u8 perk`; query 7: `u32 money, u32 premium, u8 total_skill_points, str nickname`). `fill_character_data` shows `experience_current = total - prev`, `experience_next_level = next - prev` (a bar; 0 = full), `points_unlocked = total_skill_points`, `points_available = total_skill_points - spent` and `experience_delta = m_match_stats.last_match_exp_delta`. There is no level number in the UI.
* **Match results.** The client has no message for them. The only trace is a chat line on the squad channel (8) that `messaging_client::process_incoming_text_message` hands to `lobby_menu::on_stats_message_arrived`: if it contains `Player [ <own nickname> ]` and `#e:[<n>]`, `last_match_exp_delta = n` is stored and, while connected to the lobby, money (7) and skills (8) are re-queried. The line is also printed in the chat (twice, a retail quirk). The client never re-reads reputation (11) or prices (6) by itself after the first load.

### 14.2 What the PoC server does

* **Gating is server-side and per account.** `lobby.LobbyConnection.price_rows`: an item the account has earned goes out with `reputation_level 0`, one it has not keeps its real level, so it shows with the lock icon (the client unlocks nothing but level 0, see below). `shop_action` re-checks: `LobbyConnection.find_offer` picks the asked trader's offer first, then the cheapest other one, and denies a locked item with `53 36 <faction> "<item> is locked: <Trader> reputation <n> needed, you have <m>"`. An unknown item, or one nobody sells, is "this trader does not sell that item".
* **Reputation is reported capped.** Query 11 sends `min(points, levels[1].value - 1)` per faction (`reported_reputation`), i.e. always client level 0, so the quirk in 14.1 cannot open a whole trader. The real points live in the account (`reputation`, u16 on the wire).
* **Unlocks reach the client without a restart.** After a result is paid the server pushes unsolicited answers: 7 (money, skill points), 8 (experience bar), 11 and the four price lists 6 (`LobbyConnection.push_refresh`). They go to every live lobby connection of the account that is in the menu, and are also sent once after the next `q_client_state` answered in the menu state (`LobbyServer.dirty`; the client sends one every time the lobby scene activates or reconnects) for a client that was away. A pushed 6 re-runs `on_price_items_arrived`, which rebuilds the shop lists.
* **Chat.** `ChatServer.notify_match_result` sends the squad-channel line `Player [ <nick> ] match <id>: <won|draw|completed|left early>, +<exp> exp, +<money> money #e:[<exp>]` and system lines (`Reputation: Scavengers +93 (393), ...`, `Level 3 reached (+1 skill points)`, `Unlocked at Black Market: uzi`).
* **Equipping.** `inventory_action` is unchanged (slot categories, ammo/weapon compatibility). New: when a weapon lands in a weapon slot, ammo still in its ammo slots that does not fit it goes back to storage (`eject_incompatible_ammo`, after the whole batch), because the client checks compatibility only when ammo is moved. The match ticket (`issue_ticket`) is the profile as it is, so a bought weapon is in 0x92/0x84 exactly like a starter (13.1).

### 14.3 Rules [S]

All numbers are invented. They live in `data/progression.json` (rewards, level table; `--progression FILE`, `--reward-scale N` multiplies every reward), `lobby_data.WEAPON_OFFERS` / `AMMO_PRICES` (the shop; `tools/gen_shop_prices.py` writes them into `data/shop_prices.json`) and the retail `factions_dict` (reputation thresholds).

**Levels.** Level 1 starts at 0 xp; level n needs `500 + 200 (n-2)` xp more than level n-1 (level 2 at 500, 3 at 1200, 4 at 2100, ... `100 (n-1)(n+3)`), cap 30. Each level reached grants 1 skill point on top of `--start-skill-points` (10).

**Match reward** (one per roster player and match, paid once; `progression.Progression.reward`): `base + per_kill x kills + per_item x victory items stored + win bonus (or draw bonus)`, then x`--reward-scale`.

| | base | per kill | per item stored | win | draw |
|---|---|---|---|---|---|
| experience | 120 | 25 | 40 | 150 | 60 |
| money | 250 | 60 | 120 | 400 | 150 |
| reputation, 1 Scavengers | 45 | 6 | 10 | 20 | |
| reputation, 2 Black Market | 25 | 12 | 15 | 15 | |
| reputation, 3 Renaissance | 10 | 8 | 0 | 30 | |
| reputation, 4 Border | 10 | 3 | 20 | 10 | |
| reputation, 5 Scientists | 5 | 0 | 25 | 5 | |
| reputation, 6 Mercenaries | 10 | 10 | 0 | 0 | |

Less than 60 s in the round pays nothing. A player who left before the end, or in a match that never finished, gets 50% of the sum and no win/draw bonus. A match with 3 kills, one item stored and a win pays 385 xp, 950 money, +93 Scavengers and +91 Black Market reputation. Reputation is capped at the faction's top level value. A new account starts with each faction's `levels[0].value` reputation and `--start-money` 10000 (was 50000).

**Shop** (`WEAPON_OFFERS`; level n needs the trader's reputation to reach `levels[n].value`; Scavengers 100/250/400/650/1000, Black Market 200/500/800):

| trader | level (reputation) | weapons (price) |
|---|---|---|
| 1 Scavengers | 0 | Fort-17 600, TT-33 800, TOZ-34 1500 |
| 1 Scavengers | 1 (250) | TOZ-66 1800 |
| 1 Scavengers | 2 (400) | Remington 870 2800 |
| 2 Black Market | 0 | AK-74u 2500, TOZ-122 3000 |
| 2 Black Market | 1 (500) | Uzi 3500, Magnum 3000 |
| 2 Black Market | 2 (800) | Vityaz 4500, Remington 700 5000 |

The starter loadouts (TOZ-122 + AK-74u, TOZ-34, Rem 700) stay as they were; new accounts no longer get a Vityaz, Uzi and TOZ-34 in the stash (accounts that already have them keep them). Armour, consumables and artefacts keep the levels of `data/shop_prices.json`. Ammunition is on both traders at level 0, priced per round (5.45 10, 9x18 8, 9x19 10 / HP 14, 7.62x25 10, 7.62x51 16 / AP 24, .357 15, 12 mm 12 / slug 16 / buck 14), so a clip costs about 100-500.

At about 90 Scavengers reputation per match (more with kills and wins) level 1 takes ~3 matches and level 2 ~5; the Black Market (~75 per match) takes ~7 matches for level 1 and ~11 for level 2.

### 14.4 Match server -> lobby

`MatchCore.on_event(kind, session_id=, match_id=, result=)`: `result` (`Match.player_result`) comes with `match_finished` (a lobby match was removed; one per roster player) and with `session_ended` once the match is finished (the player left after the final whistle), and is paid once per (session, match) by `LobbyServer.award_match`. Fields: `team, finished, won, draw, present_at_end` (connected when the match finished)`, kills, deaths, items_stored, play_s` (connected seconds while the round ran), `used` (a list of `{slot, id, dict_id, count}`: rounds fired and items used per profile slot, at most what the slot held; `LobbyServer.apply_usage` takes them off that item's stack, clears an emptied slot, finds an item moved to storage by id or its stack by dict id, and does so even when the result pays nothing; the next menu refresh re-sends storage and profiles). Over worker processes the event is `("event", kind, session_id, match_id, result)`. A worker that dies reports `match_finished` without a result and pays nothing.

### 14.5 Weapons in the match server

`match/game_data.py` builds a `WeaponInfo` from the config (`gameplay/weapons/<w>.options`) of every item the dictionary marks as a weapon. All 11 load (dict ids 12-19, 55, 56, 64): `rounds_per_minute`, `reload_time`, `magazine_capacity`, `fire_queue_types` (first entry -1 = automatic: AK-74u, Vityaz, Uzi; the fire-mode key 0x400 cycles the rest), chamber states, dispersion and recoil. `tests/test_progression.py` fires every one in a simulated match: automatic weapons keep firing while the trigger is held, the others fire once per press.
