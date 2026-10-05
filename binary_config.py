"""Reader/writer for vostok binary_config blobs (core/sources/configs_binary_config*.cpp).

A blob is an array of 24-byte binary_config_value records followed by string data:
    u64 data     value for bool/int/float, else offset of the value / first child
    u64 id       offset of the zero-terminated key (0 for none)
    u32 id_crc   boost::crc_32 of the key; named-table children are sorted by it
    u16 type     0 bool, 1 int, 2 float, 3 table_named, 4 table_indexed,
                 5 string, 6 float2, 7 float3, 8 float4, 9 int2
    u16 count    children for tables, byte size of the value otherwise
Offsets are relative to the blob start (binary_config::load adds the base address).
The root record sits at offset 0. Stdlib only.
"""

from __future__ import annotations

import struct
import zlib

T_BOOL, T_INT, T_FLOAT, T_NAMED, T_INDEXED, T_STRING, T_FLOAT2, T_FLOAT3, T_FLOAT4, T_INT2 = range(10)
_REC = struct.Struct("<QQIHH")


def crc(key: str) -> int:
    # boost::crc_32_type == zlib crc32
    return zlib.crc32(key.encode("latin-1")) & 0xFFFFFFFF


def _cstr(buf: bytes, off: int) -> str:
    end = buf.index(b"\0", off)
    return buf[off:end].decode("latin-1")


def load(buf: bytes):
    """Decode a blob into Python values: dict (named table), list (indexed), scalars, tuples."""
    return _read(buf, 0)


def _read(buf: bytes, off: int):
    data, _id, _crc, typ, count = _REC.unpack_from(buf, off)
    if typ == T_BOOL:
        return bool(data & 0xFFFFFFFF)
    if typ == T_INT:
        v = data & 0xFFFFFFFF
        return v - (1 << 32) if v & 0x80000000 else v
    if typ == T_FLOAT:
        return struct.unpack("<f", struct.pack("<I", data & 0xFFFFFFFF))[0]
    if typ == T_STRING:
        return _cstr(buf, data)
    if typ in (T_FLOAT2, T_FLOAT3, T_FLOAT4):
        n = typ - T_FLOAT2 + 2
        return struct.unpack_from(f"<{n}f", buf, data)
    if typ == T_INT2:
        return struct.unpack_from("<2i", buf, data)
    if typ == T_NAMED:
        out = {}
        for i in range(count):
            child = data + i * _REC.size
            cid = _REC.unpack_from(buf, child)[1]
            out[_cstr(buf, cid)] = _read(buf, child)
        return out
    if typ == T_INDEXED:
        return [_read(buf, data + i * _REC.size) for i in range(count)]
    raise ValueError(f"unknown binary_config type {typ} at {off:#x}")


class Float(float):
    """Marks a value to be stored as t_float (plain Python ints are stored as t_integer)."""


def dump(value) -> bytes:
    """Encode Python values back into a blob that binary_config::load accepts.

    dict -> table_named (children sorted by key crc), list -> table_indexed,
    bool -> t_boolean, int -> t_integer, float -> t_float, str -> t_string,
    tuple of 2/3/4 floats -> float2/3/4.
    """
    records: list[list] = []          # [data, id, crc, type, count] (offsets patched later)
    heap = bytearray()                # strings and vector payloads, appended after records
    heap_fix: list[tuple[int, str]] = []   # (record index, field) whose value is a heap offset

    def heap_put(raw: bytes) -> int:
        while len(heap) % 4:
            heap.append(0)
        off = len(heap)
        heap.extend(raw)
        return off

    def key_off(key: str | None) -> int | None:
        return None if key is None else heap_put(key.encode("latin-1") + b"\0")

    def emit_children(items: list[tuple[str | None, object]]) -> int:
        first = len(records)
        for _ in items:
            records.append([0, 0, 0, 0, 0])
        for i, (k, v) in enumerate(items):
            fill(first + i, k, v)
        return first

    def fill(idx: int, key: str | None, v) -> None:
        rec = records[idx]
        ko = key_off(key)
        if ko is not None:
            rec[1] = ko
            heap_fix.append((idx, "id"))
            rec[2] = crc(key)
        if isinstance(v, bool):
            rec[0], rec[3], rec[4] = int(v), T_BOOL, 4
        elif isinstance(v, int):
            rec[0], rec[3], rec[4] = v & 0xFFFFFFFF, T_INT, 4
        elif isinstance(v, float):
            rec[0], rec[3], rec[4] = struct.unpack("<I", struct.pack("<f", v))[0], T_FLOAT, 4
        elif isinstance(v, str):
            raw = v.encode("latin-1") + b"\0"
            rec[0], rec[3], rec[4] = heap_put(raw), T_STRING, len(raw)
            heap_fix.append((idx, "data"))
        elif isinstance(v, tuple):
            raw = struct.pack(f"<{len(v)}f", *v)
            rec[0], rec[3], rec[4] = heap_put(raw), T_FLOAT2 + len(v) - 2, len(raw)
            heap_fix.append((idx, "data"))
        elif isinstance(v, dict):
            items = sorted(v.items(), key=lambda kv: crc(kv[0]))
            rec[3], rec[4] = T_NAMED, len(items)
            rec[0] = emit_children(items) if items else len(records)
            heap_fix.append((idx, "rec"))
        elif isinstance(v, list):
            rec[3], rec[4] = T_INDEXED, len(v)
            rec[0] = emit_children([(None, x) for x in v]) if v else len(records)
            heap_fix.append((idx, "rec"))
        else:
            raise TypeError(f"cannot store {type(v).__name__} in a binary_config")

    records.append([0, 0, 0, 0, 0])
    fill(0, None, value)
    heap_base = len(records) * _REC.size
    for idx, field in heap_fix:
        rec = records[idx]
        if field == "id":
            rec[1] += heap_base
        elif field == "data":
            rec[0] += heap_base
        else:  # child record index -> byte offset
            rec[0] *= _REC.size
    return b"".join(_REC.pack(*r) for r in records) + bytes(heap)
