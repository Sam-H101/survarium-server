"""Faithful Python port of network_core::udp_match_connection.

Sources (vostok/sources/vostok/network_core/):
  sources/udp_match_connection.cpp      send_queued_packets, send_packets_list,
                                        fill_packet_header, update_acknowledgements,
                                        process_low_level_message, (instant_)disconnect
  udp_match_connection_inline.h         construct_packet, call_predicate,
                                        process_incoming_packet
  udp_match_connection.h                states, low-level ids, channel
  udp_match_packet.h                    6-byte header reserve, 250-byte message buffer
  move_to_list_predicate.h              resend after max_packet_wait_time_in_ms
  udp_match_client_session.h            server-side parameters (120000/500/33)

The client and the original server both used this one class, so the same port is used
here for the server sessions and for the mock client in the tests.

Differences from the C++ (none visible on the wire):
  * Sends are synchronous: handle_send() runs right after the datagram is handed to the
    socket, so there is no m_outgoing_packets list.
  * Statistics are reduced to a few counters.
  * A malformed datagram is dropped instead of tripping an ASSERT.
"""

from __future__ import annotations

import logging
import struct
from typing import Callable, List, Optional

from . import seqnum

log = logging.getLogger("match.transport")

# udp_match_connection::state
CONNECTED = 0
INITIATING_DISCONNECTION = 1
CONFIRMING_DISCONNECTION = 2
DISCONNECTED = 3

# low_level_message_type_enum (udp_match_connection.h:128-133)
LL_INITIATE_DISCONNECTION = 0
LL_CONFIRM_DISCONNECTION = 1
LL_CONTINUOUS_FLOW = 2

# disconnect_event_types_enum (names only; values are internal to each side)
DISCONNECTED_BY_TIMEOUT = "timeout"
DISCONNECTED_BY_INITIATOR = "initiator"
DISCONNECTED_BY_CONNECTION_LOST = "connection_lost"

HEADER_SIZE = 6                 # udp_match_packet.h:65 (m_buffer.elems + 6)
DATAGRAM_SIZE = 256             # boost::array<u8,256>
MESSAGE_CAPACITY = DATAGRAM_SIZE - HEADER_SIZE   # allocated_size() == 250
CHANNELS_COUNT = 1

DEFAULT_DISCONNECTION_TIMEOUT_MS = 120000
DEFAULT_MAX_PACKET_WAIT_TIME_MS = 500
DEFAULT_MAX_IDLE_TIME_MS = 33

_HDR = struct.Struct("<HHH")


class MessageTooLarge(ValueError):
    pass


class UdpMatchPacket:
    """One message (or one low-level control record) awaiting send / ack.

    ``data`` is base_packet's buffer(): ``[u8 type][u16 order_id][payload]`` for a game
    message, ``[u8 1][u8 ll_type]`` for a low-level packet.  ``multi`` mirrors
    buffer_to_send()[0] (udp_match_packets_count_enum): 1 for low-level packets.
    """

    __slots__ = ("data", "multi", "message_type", "is_reliable", "is_ordered",
                 "channel_id", "sequence_id", "order_id", "last_send_time_in_ms",
                 "send_count")

    def __init__(self) -> None:
        self.data = bytearray()
        self.multi = 0
        self.message_type = 0
        self.is_reliable = False
        self.is_ordered = False
        self.channel_id = 0x3F
        self.sequence_id = 0xFFFF
        self.order_id = 0xFFFF
        self.last_send_time_in_ms = 0xFFFFFFFF
        self.send_count = 0

    def append(self, raw: bytes) -> "UdpMatchPacket":
        if len(self.data) + len(raw) > MESSAGE_CAPACITY:
            # udp_match_packet::reallocate is UNREACHABLE_CODE: no growth, no fragmentation
            raise MessageTooLarge(
                f"message 0x{self.message_type:02x} would be {len(self.data) + len(raw)} "
                f"bytes (max {MESSAGE_CAPACITY})")
        self.data += raw
        return self


def message_type_info(message_type: int):
    """network_packets_orderer::get_*_message_info: every match message, both
    directions, is ordered_reliable(0) (game_core/network_messages.h:19-33)."""
    return True, True, 0     # reliable, ordered, channel


class _Channel:
    __slots__ = ("packets", "received_order_id", "sent_order_id")

    def __init__(self) -> None:
        self.packets = {}               # order_id -> (message_type, payload bytes)
        self.received_order_id = 0xFFFF
        self.sent_order_id = 0

    def reset(self) -> None:
        self.packets.clear()
        self.received_order_id = 0xFFFF
        self.sent_order_id = 0


class UdpMatchConnection:
    def __init__(
        self,
        send_datagram: Callable[[bytes], None],
        disconnection_timeout_in_ms: int = DEFAULT_DISCONNECTION_TIMEOUT_MS,
        max_packet_wait_time_in_ms: int = DEFAULT_MAX_PACKET_WAIT_TIME_MS,
        max_idle_time_in_ms: int = DEFAULT_MAX_IDLE_TIME_MS,
        logging_id: str = "connection",
    ) -> None:
        self._send_datagram = send_datagram
        self.logging_id = logging_id
        self.on_disconnect: Optional[Callable[[str], None]] = None

        self.m_last_receive_time_in_ms = 0
        self.m_disconnection_timeout_in_ms = disconnection_timeout_in_ms
        self.m_last_send_time_in_ms = 0
        self.m_last_send_attempt_time_in_ms = 0
        self.m_max_packet_wait_time_in_ms = max_packet_wait_time_in_ms
        self.m_max_idle_time_in_ms = max_idle_time_in_ms
        self.m_disconnection_receive_time_in_ms = 0
        self.m_state = DISCONNECTED
        self.m_remote_acknowledgement_bits = 0
        self.m_received_local_acknowledgement_bits = 0
        self.m_local_sequence_id = 0xFFFF
        self.m_remote_sequence_id = 0xFFFF
        self.m_received_local_sequence_id = 0xFFFF
        self.m_disconnection_local_sequence_id = 0xFFFF

        self.m_packets_to_send: List[UdpMatchPacket] = []
        self.m_unacknowledged_packets: List[UdpMatchPacket] = []
        self.m_channels = [_Channel() for _ in range(CHANNELS_COUNT)]

        self.stats = {
            "sent_datagrams": 0, "sent_messages": 0, "resent_messages": 0,
            "sent_low_level": 0, "received_datagrams": 0, "received_duplicates": 0,
            "received_low_level": 0, "delivered_messages": 0, "dropped_malformed": 0,
        }

    # ------------------------------------------------------------------ state
    def is_connected(self) -> bool:
        return self.m_state == CONNECTED

    def is_disconnected(self) -> bool:
        return self.m_state == DISCONNECTED

    def is_disconnecting(self) -> bool:
        return self.m_state in (INITIATING_DISCONNECTION, CONFIRMING_DISCONNECTION)

    def are_there_any_queued_packets(self) -> bool:
        return bool(self.m_packets_to_send)

    def unacknowledged_packets_count(self) -> int:
        return len(self.m_unacknowledged_packets)

    def last_receive_time_in_ms(self) -> int:
        return self.m_last_receive_time_in_ms

    # ---------------------------------------------------------------- packets
    @staticmethod
    def new_packet(message_type: int) -> UdpMatchPacket:
        """construct_packet (udp_match_connection_inline.h:9-25)."""
        packet = UdpMatchPacket()
        packet.message_type = message_type & 0xFF
        packet.data.append(message_type & 0xFF)
        reliable, ordered, channel = message_type_info(message_type)
        packet.channel_id = channel
        packet.is_reliable = reliable
        packet.is_ordered = ordered
        if ordered:
            packet.data += b"\xff\xff"        # order id placeholder, set by enqueue_impl
        return packet

    def _new_low_level_packet(self, message_type: int) -> UdpMatchPacket:
        """new_low_level_packet (udp_match_connection.cpp:252-283): unreliable,
        buffer_to_send()[0] = multi, body [01][type]."""
        packet = UdpMatchPacket()
        packet.multi = 1
        packet.data += bytes((1, message_type & 0xFF))
        self.stats["sent_low_level"] += 1
        return packet

    def connect(self, packet: Optional[UdpMatchPacket] = None) -> None:
        assert self.m_state == DISCONNECTED
        self.m_state = CONNECTED
        if packet is not None:
            self._enqueue_impl(packet)

    def _enqueue_impl(self, packet: UdpMatchPacket) -> None:
        if packet.is_ordered:
            channel = self.m_channels[packet.channel_id]
            packet.order_id = channel.sent_order_id
            struct.pack_into("<H", packet.data, 1, channel.sent_order_id)
            channel.sent_order_id = seqnum.inc(channel.sent_order_id)
        self.m_packets_to_send.append(packet)

    def enqueue(self, packet: UdpMatchPacket) -> bool:
        if self.m_state == CONNECTED:
            self._enqueue_impl(packet)
            return True
        return False

    # ------------------------------------------------------------------- send
    def _header(self, sequence_id: int, multi: int) -> bytes:
        """fill_packet_header (udp_match_connection.cpp:133-145)."""
        bits = ((self.m_remote_acknowledgement_bits << 1) | (1 if multi else 0)) & 0xFFFF
        return _HDR.pack(sequence_id, self.m_remote_sequence_id, bits)

    def _handle_send(self, packet: UdpMatchPacket) -> None:
        """handle_send (udp_match_connection.cpp:76-110) for a single-packet datagram."""
        if not packet.is_reliable:
            return
        if self.m_state != CONNECTED and not packet.multi:
            return
        self.m_unacknowledged_packets.append(packet)

    def _send_packets_list(self, packets: List[UdpMatchPacket]) -> None:
        """send_packets_list (udp_match_connection.cpp:147-211)."""
        self.stats["sent_messages"] += len(packets)
        for p in packets:
            if p.send_count > 1:
                self.stats["resent_messages"] += 1
        head = packets[0]
        if len(packets) == 1:
            datagram = self._header(head.sequence_id, head.multi) + bytes(head.data)
            self._transmit(datagram)
            self._handle_send(head)
            return

        body = bytearray()
        for p in packets:
            assert len(p.data) < 256
            body.append(len(p.data))
            body += p.data
        datagram = self._header(head.sequence_id, 1) + bytes(body)
        self._transmit(datagram)
        for p in packets:
            if p.is_reliable:
                self.m_unacknowledged_packets.append(p)

    def _transmit(self, datagram: bytes) -> None:
        assert len(datagram) <= DATAGRAM_SIZE, len(datagram)
        self.stats["sent_datagrams"] += 1
        self._send_datagram(datagram)

    def send_queued_packets(self, current_time_in_ms: int) -> None:
        """send_queued_packets (udp_match_connection.cpp:285-396)."""
        self.m_last_send_attempt_time_in_ms = current_time_in_ms

        state = self.m_state
        if state == CONNECTED:
            if (self.m_last_receive_time_in_ms and
                    self.m_last_receive_time_in_ms + self.m_disconnection_timeout_in_ms
                    <= current_time_in_ms):
                self.instant_disconnect(DISCONNECTED_BY_TIMEOUT)
                return
        elif state == INITIATING_DISCONNECTION:
            self.m_packets_to_send.append(self._new_low_level_packet(LL_INITIATE_DISCONNECTION))
        elif state == CONFIRMING_DISCONNECTION:
            if (self.m_disconnection_receive_time_in_ms + self.m_max_packet_wait_time_in_ms
                    <= current_time_in_ms):
                self.instant_disconnect(DISCONNECTED_BY_INITIATOR)
                return
            self.m_packets_to_send.append(self._new_low_level_packet(LL_CONFIRM_DISCONNECTION))
        elif state == DISCONNECTED:
            return

        # move_to_list_predicate: resend everything unacked for max_packet_wait_time
        keep = []
        for p in self.m_unacknowledged_packets:
            if current_time_in_ms < p.last_send_time_in_ms + self.m_max_packet_wait_time_in_ms:
                keep.append(p)
            else:
                self.m_packets_to_send.append(p)
        self.m_unacknowledged_packets = keep

        if not self.m_packets_to_send:
            if current_time_in_ms < self.m_last_send_time_in_ms + self.m_max_idle_time_in_ms:
                return
            self.m_packets_to_send.append(self._new_low_level_packet(LL_CONTINUOUS_FLOW))

        packets = self.m_packets_to_send
        self.m_packets_to_send = []
        packets.sort(key=lambda p: len(p.data))      # packets_predicate: by buffer_size

        while packets:
            if seqnum.le(seqnum.inc(self.m_local_sequence_id), self.m_received_local_sequence_id):
                # send window exhausted (32768 unacked sequences): park reliable packets
                for p in packets:
                    if p.is_reliable:
                        self.m_packets_to_send.append(p)
                break

            packet = packets.pop()
            chain = [packet]
            packet.last_send_time_in_ms = current_time_in_ms
            self.m_local_sequence_id = seqnum.inc(self.m_local_sequence_id)
            packet.sequence_id = self.m_local_sequence_id
            packet.send_count += 1

            size_left = MESSAGE_CAPACITY - len(packet.data) - 1
            if size_left > 1 and not packet.multi:
                for other in reversed(packets):
                    if size_left > len(other.data) and not other.multi:
                        size_left -= len(other.data) + 1
                        chain.append(other)
                        other.last_send_time_in_ms = current_time_in_ms
                        other.sequence_id = packet.sequence_id
                        other.send_count += 1
                if len(chain) > 1:
                    taken = set(map(id, chain))
                    packets = [p for p in packets if id(p) not in taken]

            self.m_last_send_time_in_ms = current_time_in_ms
            self._send_packets_list(chain)

    # ------------------------------------------------------------- recovery
    def adopt_peer_state(self, old: "UdpMatchConnection", first_order_id: int) -> None:
        """Server-side recovery, not in the C++: continue a peer whose connection object
        was NOT reset across a socket change.

        The retail client re-enters udp_match_client::connect while its old connection is
        still `connected` (Play pressed again mid-match): the ASSERT in
        udp_match_connection::connect is compiled out, so the new 0x40 keeps the old
        sequence and order numbering. A fresh server connection would buffer that 0x40
        forever (it waits for order 0). Continue the old session's numbering instead:
        our sequence/ack state and sent order ids from ``old`` (None if unknown), and
        deliver the peer's next message at ``first_order_id``."""
        if old is not None:
            self.m_local_sequence_id = old.m_local_sequence_id
            self.m_received_local_sequence_id = old.m_received_local_sequence_id
            self.m_received_local_acknowledgement_bits = old.m_received_local_acknowledgement_bits
            self.m_remote_sequence_id = old.m_remote_sequence_id
            self.m_remote_acknowledgement_bits = old.m_remote_acknowledgement_bits
            for mine, theirs in zip(self.m_channels, old.m_channels):
                mine.sent_order_id = theirs.sent_order_id
            # the peer still waits for every order id the old endpoint had in flight:
            # resend those (with new sequence numbers) or its channel stalls on the gap
            self.m_packets_to_send = [p for p in old.m_unacknowledged_packets + old.m_packets_to_send
                                      if p.is_reliable and not p.multi] + self.m_packets_to_send
        for channel in self.m_channels:
            channel.packets.clear()
            channel.received_order_id = seqnum.dec(first_order_id)

    # ---------------------------------------------------------------- receive
    def _update_acknowledgements(self, remote_sequence_id: int, local_sequence_id: int,
                                 local_acknowledgement_bits: int) -> None:
        """update_acknowledgements (udp_match_connection.cpp:480-533)."""
        remote_difference = (remote_sequence_id - self.m_remote_sequence_id) & 0xFFFF
        self.m_remote_acknowledgement_bits = (
            (self.m_remote_acknowledgement_bits >> remote_difference)
            if remote_difference < 16 else 0) | 0x8000
        self.m_remote_sequence_id = remote_sequence_id

        if seqnum.lt(self.m_local_sequence_id, local_sequence_id):
            return                          # acks something we never sent

        if seqnum.le(local_sequence_id, self.m_received_local_sequence_id):
            difference = (self.m_received_local_sequence_id - local_sequence_id) & 0xFFFF
            if difference:
                if difference <= 15:
                    self.m_received_local_acknowledgement_bits |= 1 << (15 - difference)
                self._remove_acknowledged(local_sequence_id)
            return

        local_difference = (local_sequence_id - self.m_received_local_sequence_id) & 0xFFFF
        last_bits = ((self.m_received_local_acknowledgement_bits >> local_difference)
                     if local_difference < 16 else 0) & 0xFFFF
        if (last_bits & local_acknowledgement_bits) != last_bits:
            return
        self.m_received_local_acknowledgement_bits = local_acknowledgement_bits
        self.m_received_local_sequence_id = local_sequence_id

        bits = (self.m_received_local_acknowledgement_bits ^ last_bits) & 0xFFFF
        sequence_id = local_sequence_id
        acked = set()
        while bits:
            if bits & 0x8000:
                acked.add(sequence_id)
            sequence_id = seqnum.dec(sequence_id)
            bits = (bits << 1) & 0xFFFF
        if acked and self.m_unacknowledged_packets:
            # one pass for the whole bitfield (same result as one removal per sequence)
            self.m_unacknowledged_packets = [
                p for p in self.m_unacknowledged_packets if p.sequence_id not in acked]

    def _remove_acknowledged(self, sequence_id: int) -> None:
        self.m_unacknowledged_packets = [
            p for p in self.m_unacknowledged_packets if p.sequence_id != sequence_id]

    def _process_low_level_message(self, message_type: int, time_in_ms: int) -> None:
        """process_low_level_message (udp_match_connection.cpp:535-565)."""
        self.stats["received_low_level"] += 1
        if message_type == LL_CONTINUOUS_FLOW:
            return
        if message_type == LL_CONFIRM_DISCONNECTION:
            if self.m_state == INITIATING_DISCONNECTION:
                self.instant_disconnect(DISCONNECTED_BY_INITIATOR)
            return
        # initiate_disconnection and every unknown type
        if self.m_state == CONNECTED:
            self.m_state = CONFIRMING_DISCONNECTION
            self.m_disconnection_receive_time_in_ms = time_in_ms

    def _call_predicate(self, record: memoryview, predicate) -> None:
        """call_predicate (udp_match_connection_inline.h:27-70)."""
        if len(record) < 1:
            raise struct.error("empty record")
        message_type = record[0]
        reliable, ordered, channel_id = message_type_info(message_type)
        if not ordered:
            predicate(message_type, bytes(record[1:]))
            return
        if len(record) < 3:
            raise struct.error("record shorter than type+order")
        order_id = record[1] | (record[2] << 8)
        channel = self.m_channels[channel_id]
        if seqnum.le(order_id, channel.received_order_id):
            return
        if order_id in channel.packets:
            return
        channel.packets[order_id] = (message_type, bytes(record[3:]))
        next_order_id = seqnum.inc(channel.received_order_id)
        while next_order_id in channel.packets:
            mtype, payload = channel.packets.pop(next_order_id)
            channel.received_order_id = next_order_id
            next_order_id = seqnum.inc(next_order_id)
            self.stats["delivered_messages"] += 1
            predicate(mtype, payload)
            if self.m_state == DISCONNECTED:
                return

    def process_incoming_packet(self, datagram: bytes, predicate) -> None:
        """process_incoming_packet (udp_match_connection_inline.h:72-136).

        ``predicate(message_type, payload_bytes)`` is called once per in-order message;
        payload excludes the type byte and the order id."""
        if self.m_state == DISCONNECTED:
            return
        self.m_last_receive_time_in_ms = self.m_last_send_attempt_time_in_ms
        self.stats["received_datagrams"] += 1
        try:
            if len(datagram) < HEADER_SIZE:
                raise struct.error("short datagram")
            remote_sequence_id, local_sequence_id, bits = _HDR.unpack_from(datagram, 0)
            local_acknowledgement_bits = ((bits >> 1) | 0x8000) & 0xFFFF

            if self.m_remote_sequence_id == remote_sequence_id:
                self.stats["received_duplicates"] += 1
                return

            if seqnum.lt(self.m_remote_sequence_id, remote_sequence_id):
                self._update_acknowledgements(remote_sequence_id, local_sequence_id,
                                              local_acknowledgement_bits)

            body = memoryview(datagram)[HEADER_SIZE:]
            if not (bits & 1):
                self._call_predicate(body, predicate)
                return

            pos = 0
            i = 0
            while pos < len(body):
                size = body[pos]
                pos += 1
                if pos + size > len(body):
                    raise struct.error("multi record overruns datagram")
                record = body[pos:pos + size]
                pos += size
                if i or pos < len(body):
                    self._call_predicate(record, predicate)
                else:
                    self._process_low_level_message(record[0] if size else 0,
                                                     self.m_last_send_attempt_time_in_ms)
                if self.m_state == DISCONNECTED:
                    return
                i += 1
        except struct.error as exc:
            self.stats["dropped_malformed"] += 1
            log.warning("%s: malformed datagram dropped (%s): %s",
                        self.logging_id, exc, bytes(datagram[:32]).hex())

    # ------------------------------------------------------------ disconnect
    def disconnect(self) -> None:
        """disconnect (udp_match_connection.cpp:639-666): graceful, sends initiate."""
        if self.m_state != CONNECTED:
            return
        self.m_state = INITIATING_DISCONNECTION
        if seqnum.le(seqnum.inc(self.m_local_sequence_id), self.m_received_local_sequence_id):
            self.instant_disconnect(DISCONNECTED_BY_INITIATOR)
            return
        self.m_unacknowledged_packets = []
        self.m_packets_to_send = []
        for channel in self.m_channels:
            channel.packets.clear()
        self.m_disconnection_local_sequence_id = seqnum.inc(self.m_local_sequence_id)

    def instant_disconnect(self, reason: str) -> None:
        """instant_disconnect (udp_match_connection.cpp:605-637)."""
        self.m_state = DISCONNECTED
        self.m_last_send_time_in_ms = 0
        self.m_last_send_attempt_time_in_ms = 0
        self.m_last_receive_time_in_ms = 0
        self.m_disconnection_receive_time_in_ms = 0
        self.m_remote_acknowledgement_bits = 0
        self.m_received_local_acknowledgement_bits = 0
        self.m_local_sequence_id = 0xFFFF
        self.m_remote_sequence_id = 0xFFFF
        self.m_received_local_sequence_id = 0xFFFF
        self.m_disconnection_local_sequence_id = 0xFFFF
        self.m_unacknowledged_packets = []
        self.m_packets_to_send = []
        for channel in self.m_channels:
            channel.reset()
        if self.on_disconnect:
            self.on_disconnect(reason)
