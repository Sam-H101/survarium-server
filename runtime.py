"""Process-level plumbing for running many clients at once (stdlib only).

* Windows timer resolution: asyncio sleeps are rounded up to the 15.6 ms system tick, which
  turns the 33 ms match tick into ~47 ms (21 Hz). ``high_resolution_timers()`` asks for 1 ms.
* UDP sockets: on Windows an ICMP "port unreachable" (sent when a client's socket is gone)
  surfaces as WSAECONNRESET on the next recvfrom, and the Proactor datagram transport of
  Python 3.12 then stops reading for good: the match server goes deaf for everyone after
  the first client vanishes. ``udp_socket()`` turns that report off (SIO_UDP_CONNRESET).
* TCP keep-alive (half-open lobby/chat connections are found within ~2 minutes).
* Log summarisation: ``RateLimitFilter`` caps repeated per-event lines.
* Measurements: event-loop lag, percentiles, CPU and resident memory.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import socket
import sys
import time
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("poc.runtime")

SIO_UDP_CONNRESET = 0x9800000C          # _WSAIOW(IOC_VENDOR, 12)


# ------------------------------------------------------------------------- timers
_timer_period = 0


def high_resolution_timers(ms: int = 1) -> None:
    """timeBeginPeriod(ms) on Windows (released at process exit); a no-op elsewhere."""
    global _timer_period
    if sys.platform != "win32" or _timer_period:
        return
    try:
        if ctypes.windll.winmm.timeBeginPeriod(ms) == 0:
            _timer_period = ms
    except (AttributeError, OSError):
        pass


def precise_loop_clock(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Give an asyncio loop a sub-millisecond clock on Windows.

    asyncio schedules timers with time.monotonic(), which on Windows is GetTickCount64
    (15.6 ms steps), and treats every timer due within that resolution as ready: a fixed
    33 ms schedule then runs ticks 0 ms or 65 ms apart. perf_counter is monotonic too and
    precise; with high_resolution_timers() the loop's waits are then accurate to ~1 ms."""
    if sys.platform != "win32":
        return
    loop = loop or asyncio.get_running_loop()
    if getattr(loop, "_precise_clock", False):
        return
    # continue the loop's time base (timers already scheduled keep their meaning)
    offset = time.monotonic() - time.perf_counter()
    pc = time.perf_counter
    loop.time = lambda: pc() + offset                  # instance attribute: used by call_at etc.
    loop._clock_resolution = 0.001                     # BaseEventLoop's "ready" window
    loop._precise_clock = True


# ------------------------------------------------------------------------- sockets
def udp_socket(host: str, port: int, buffer_bytes: int = 1 << 20) -> socket.socket:
    """A bound, non-blocking IPv4 UDP socket for asyncio's create_datagram_endpoint(sock=)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if sys.platform == "win32":
            _disable_udp_connreset(sock)
        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF):
            try:
                sock.setsockopt(socket.SOL_SOCKET, opt, buffer_bytes)
            except OSError:
                pass
        sock.bind((host, port))
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def _disable_udp_connreset(sock: socket.socket) -> None:
    try:
        ws2 = ctypes.windll.ws2_32
        flag = ctypes.c_ulong(0)
        returned = ctypes.c_ulong(0)
        ws2.WSAIoctl.argtypes = [ctypes.c_size_t, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                 ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                 ctypes.c_void_p, ctypes.c_void_p]
        rc = ws2.WSAIoctl(sock.fileno(), SIO_UDP_CONNRESET, ctypes.byref(flag), ctypes.sizeof(flag),
                          None, 0, ctypes.byref(returned), None, None)
        if rc != 0:
            log.warning("SIO_UDP_CONNRESET failed (%d); a vanished client may stall UDP reads",
                        ws2.WSAGetLastError())
    except (AttributeError, OSError) as exc:
        log.warning("cannot disable UDP connection-reset reports: %s", exc)


def tcp_keepalive(writer: asyncio.StreamWriter, idle_s: int = 60, interval_s: int = 10) -> None:
    """Turn on TCP keep-alive so a peer that vanished without FIN/RST (power off, cable
    pulled, NAT timeout) is detected and its connection closed."""
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle_s * 1000, interval_s * 1000))
        else:
            for name, value in (("TCP_KEEPIDLE", idle_s), ("TCP_KEEPINTVL", interval_s),
                                ("TCP_KEEPCNT", 6)):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except (OSError, ValueError, AttributeError):
        pass


# ------------------------------------------------------------------------- logging
class RateLimitFilter(logging.Filter):
    """Per call site (logger + format string): at most ``burst`` records, refilled at
    ``rate`` per second; the next record that passes notes how many were dropped.
    WARNING and above use their own, larger budget; ERROR and above always pass."""

    def __init__(self, burst: int = 20, rate: float = 0.5, warn_burst: int = 50,
                 warn_rate: float = 2.0, max_keys: int = 4096) -> None:
        super().__init__()
        self.burst, self.rate = burst, rate
        self.warn_burst, self.warn_rate = warn_burst, warn_rate
        self.max_keys = max_keys
        self.buckets: Dict[Tuple[str, str], List[float]] = {}   # key -> [tokens, last, dropped]
        self.suppressed_total = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR or getattr(record, "forwarded", False):
            return True                    # forwarded: already limited where it came from
        burst, rate = (self.warn_burst, self.warn_rate) if record.levelno >= logging.WARNING \
            else (self.burst, self.rate)
        key = (record.name, str(record.msg))
        now = time.monotonic()
        b = self.buckets.get(key)
        if b is None:
            if len(self.buckets) >= self.max_keys:
                self.buckets.clear()
            b = self.buckets[key] = [float(burst), now, 0]
        b[0] = min(float(burst), b[0] + (now - b[1]) * rate)
        b[1] = now
        if b[0] < 1.0:
            b[2] += 1
            self.suppressed_total += 1
            return False
        b[0] -= 1.0
        if b[2]:
            record.msg = f"{record.msg} [{int(b[2])} similar lines suppressed]"
            b[2] = 0
        return True


def setup_logging(level: int = logging.INFO, rate_limit: bool = True,
                  fmt: str = "%(asctime)s %(levelname)s %(message)s") -> Optional[RateLimitFilter]:
    logging.basicConfig(level=level, format=fmt)
    if not rate_limit:
        return None
    flt = RateLimitFilter()
    for h in logging.getLogger().handlers:
        h.addFilter(flt)
    return flt


# ------------------------------------------------------------------------- measuring
def pct(values: Iterable[float], q: float) -> Optional[float]:
    v = sorted(values)
    if not v:
        return None
    return v[min(len(v) - 1, max(0, int(math.ceil(q / 100.0 * len(v))) - 1))]


class Samples:
    """Recent samples (bounded) for percentiles, plus a list drained by the stats file."""

    def __init__(self, keep: int = 2048) -> None:
        self.recent: Deque[float] = deque(maxlen=keep)
        self.pending: List[float] = []
        self.max_pending = 20000

    def add(self, v: float) -> None:
        self.recent.append(v)
        if len(self.pending) < self.max_pending:
            self.pending.append(v)

    def drain(self) -> List[float]:
        out, self.pending = self.pending, []
        return out

    def p(self, q: float) -> Optional[float]:
        return pct(self.recent, q)


class LoopLagMonitor:
    """Wakes every ``period`` seconds and records how late it woke (ms): the time other
    work held the event loop."""

    def __init__(self, period: float = 0.05) -> None:
        self.period = period
        self.samples = Samples()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> "LoopLagMonitor":
        self._task = asyncio.get_running_loop().create_task(self._run())
        return self

    async def _run(self) -> None:
        pc = time.perf_counter
        while True:
            t0 = pc()
            await asyncio.sleep(self.period)
            self.samples.add(max(0.0, (pc() - t0 - self.period) * 1000.0))

    def stop(self) -> None:
        if self._task:
            self._task.cancel()


def cpu_seconds() -> float:
    return time.process_time()


def rss_mb() -> float:
    """Working set (Windows) or max RSS (POSIX) of this process, in MB."""
    if sys.platform == "win32":
        try:
            class PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
                    (n, ctypes.c_size_t) for n in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / 1048576.0
        except (AttributeError, OSError):
            pass
        return 0.0
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except ImportError:
        return 0.0


class TokenBuckets:
    """Per-key token buckets (login attempts per IP)."""

    def __init__(self, rate: float, burst: float, max_keys: int = 65536) -> None:
        self.rate, self.burst, self.max_keys = rate, burst, max_keys
        self.buckets: Dict[object, Tuple[float, float]] = {}

    def allow(self, key, cost: float = 1.0) -> bool:
        if self.rate <= 0:
            return True
        now = time.monotonic()
        tokens, last = self.buckets.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        ok = tokens >= cost
        if ok:
            tokens -= cost
        if key not in self.buckets and len(self.buckets) >= self.max_keys:
            # drop full (idle) buckets first; they carry no state
            for k in [k for k, (t, l) in self.buckets.items()
                      if min(self.burst, t + (now - l) * self.rate) >= self.burst]:
                del self.buckets[k]
            if len(self.buckets) >= self.max_keys:
                self.buckets.clear()
        self.buckets[key] = (tokens, now)
        return ok
