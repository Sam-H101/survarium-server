"""Serial-number arithmetic of network_core::sequence_number<u16>.

Port of network_core/sequence_number_inline.h:64-80 (operator<, operator<=) and the
free operator- (signed wrapped distance).
"""

MASK = 0xFFFF


def lt(a: int, b: int) -> bool:
    """a < b in the 16-bit serial space (0x8000 half window)."""
    return (a < b and a + 0x8000 > b) or (b < a and b + 0x8000 <= a)


def le(a: int, b: int) -> bool:
    return (a <= b and a + 0x8000 > b) or (b < a and b + 0x8000 <= a)


def diff(left: int, right: int) -> int:
    """operator-(left, right): s16 distance when right <= left, else negated mirror."""
    if le(right, left):
        d = (left - right) & MASK
        return d - 0x10000 if d >= 0x8000 else d
    return -diff(right, left)


def inc(a: int) -> int:
    return (a + 1) & MASK


def dec(a: int) -> int:
    return (a - 1) & MASK
