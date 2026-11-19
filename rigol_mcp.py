#!/usr/bin/env python3
"""
Rigol DS1054Z (DS1000Z series) MCP server.

Talks to the scope directly over its raw SCPI socket (TCP 5555): no Rigol
UltraSigma, no NI-VISA, no drivers. The scope is an LXI Core instrument, so
plain ASCII SCPI over a TCP socket is all it needs.

Exposes tools to Claude Code:
  - scope_info        : identity + current settings snapshot
  - get_screenshot    : live display as a PNG (Claude can SEE the scope)
  - get_stats         : measurements (Vpp, freq, duty, rise/fall, ...)
  - poll              : timed measurement polling with PC timestamps -> CSV
  - get_waveform      : capture one or more channels (NORM screen or RAW deep
                        memory) -> stats + CSV, optionally with a screenshot
  - plot_waveform     : capture + render a PNG plot (needs matplotlib)
  - run_control       : run / stop / single / force / autoscale
  - set_channel       : per-channel scale/offset/coupling/display/probe
  - set_timebase      : timebase scale + offset
  - set_trigger       : edge source/level/slope/sweep
  - set_acquire       : memory depth
  - scpi              : raw SCPI escape hatch, one command or several in order

Config via environment variables:
  RIGOL_HOST             scope IP                  (default 192.168.178.96)
  RIGOL_PORT             raw SCPI port             (default 5555)
  RIGOL_TIMEOUT          reply timeout             (default 10 seconds)
  RIGOL_CONNECT_TIMEOUT  connect + *IDN? check     (default 3 seconds)
  RIGOL_OUT_DIR          CSV/PNG output dir        (default ./captures next to this file)

Use the scope from ONE client at a time. It works through one request at a
time: a client that stops reading in the middle of a reply blocks every other
client until it reads on or disconnects. Every tool call opens one connection,
checks that the scope answers *IDN? on it, and closes it again (after *OPC?
when it ended on a setting, so the scope cannot drop it), so a wrong address
or a blocked port fails within RIGOL_CONNECT_TIMEOUT seconds with a message
that says which.
"""

import asyncio
import csv
import functools
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP, Image

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
RIGOL_HOST = os.environ.get("RIGOL_HOST", "192.168.178.96")
RIGOL_PORT = int(os.environ.get("RIGOL_PORT", "5555"))
RIGOL_TIMEOUT = float(os.environ.get("RIGOL_TIMEOUT", "10"))
RIGOL_CONNECT_TIMEOUT = float(os.environ.get("RIGOL_CONNECT_TIMEOUT", "3"))
OUT_DIR = Path(os.environ.get("RIGOL_OUT_DIR", Path(__file__).resolve().parent / "captures"))

INVALID = 9.9e37  # Rigol returns 9.9E37 when a measurement has no valid value

# poll: longest run one call may take. Claude Code aborts a stdio MCP call that
# sends no response or progress for 30 minutes (measured: 1800 s), so a poll
# stays well below that; split longer recordings into several calls.
POLL_MAX_SECONDS = 1200
POLL_MIN_INTERVAL_MS = 50
WRITE_SETTLE_S = 0.05  # pause after a setting in a multi-command scpi call
LOCK_WAIT_S = 2.0  # how long a call waits for another call of this server

mcp = FastMCP("Rigol DS1054Z")


# --------------------------------------------------------------------------- #
# matplotlib font-cache warmup (see plot_waveform)
# --------------------------------------------------------------------------- #
# matplotlib builds a font cache the first time it resolves a font. That build
# is normally quick, but on Windows it opens/stats thousands of font files and,
# with antivirus intercepting each one, can stall for minutes-to-tens-of-minutes.
# plot_waveform imports matplotlib lazily, so without care that whole build lands
# *inside* the first tool call, with no feedback to the client -- the tool looks
# hung. Two guards:
#   1) Pin a stable, writable, persistent MPLCONFIGDIR so the cache is built at
#      most once *ever*. Without this, a spawn environment that hands matplotlib
#      a non-persistent cache dir (stripped HOME, wiped TEMP) makes it rebuild on
#      every call -- a permanent stall rather than a one-time warmup.
#   2) Warm the cache in a background thread at startup, off the request path, so
#      the first plot_waveform is instant and the stall (if any) never blocks a
#      tool call or the MCP handshake.
_MPL_CACHE = Path(__file__).resolve().parent / ".mplcache"
try:
    _MPL_CACHE.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))
except OSError:
    pass  # e.g. read-only site-packages install: fall back to matplotlib's default


def _warm_matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot  # noqa: F401  -- pulls in font_manager
        from matplotlib.font_manager import FontProperties, findfont

        findfont(FontProperties())  # force the one-time font-cache build now
    except Exception:
        pass  # matplotlib is optional; plot_waveform reports the real error if used


threading.Thread(target=_warm_matplotlib, name="mpl-warm", daemon=True).start()


# --------------------------------------------------------------------------- #
# Low-level SCPI-over-socket transport
# --------------------------------------------------------------------------- #
class ScopeError(RuntimeError):
    """A scope call failed. The message names the address and what to check."""


# One scope session at a time inside this server. Sync tools run on the MCP
# event loop one after another anyway; the lock matters for poll, which runs in
# a worker thread, so a call that arrives meanwhile fails fast instead of
# opening a second connection to the scope.
_scope_lock = threading.Lock()
_scope_user = ""


def _no_window() -> dict:
    """subprocess kwargs that keep a console window from flashing up on Windows."""
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _win_process_names() -> dict:
    r = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=5, **_no_window()
    )
    names = {}
    for row in csv.reader(r.stdout.decode("ascii", "replace").splitlines()):
        if len(row) >= 2 and row[1].isdigit():
            names[row[1]] = row[0]
    return names


def _win_tcp_connections(ip: str, port: int) -> list[tuple[int, int, str]]:
    """(local port, pid, state) of this PC's live IPv4 connections to ip:port, from
    GetExtendedTcpTable (numeric states, so no localized netstat text to parse)."""
    import ctypes
    from ctypes import wintypes

    class Row(ctypes.Structure):  # MIB_TCPROW_OWNER_PID
        _fields_ = [
            (f, wintypes.DWORD) for f in ("state", "laddr", "lport", "raddr", "rport", "pid")
        ]

    get_table = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
    size = wintypes.DWORD(0)
    for _ in range(3):  # the table can grow between the size query and the read
        buf = ctypes.create_string_buffer(size.value or 4)
        # AF_INET = 2, TCP_TABLE_OWNER_PID_ALL = 5
        if get_table(buf, ctypes.byref(size), False, 2, 5, 0) == 0:
            break
    else:
        raise OSError("GetExtendedTcpTable failed")
    count = wintypes.DWORD.from_buffer(buf).value
    rows = (Row * count).from_buffer(buf, ctypes.sizeof(wintypes.DWORD))
    want = int.from_bytes(socket.inet_aton(ip), "little")
    live = {3: "SYN_SENT", 5: "ESTABLISHED"}  # FIN_WAIT, TIME_WAIT etc. hold nothing
    return [
        (socket.ntohs(r.lport & 0xFFFF), r.pid, live[r.state])
        for r in rows
        if r.raddr == want and socket.ntohs(r.rport & 0xFFFF) == port and r.state in live
    ]


def _local_holders(ip: str, port: int, exclude_port: int | None = None) -> list[str] | None:
    """Best effort: this PC's live TCP connections to ip:port, as
    'pid 123 python.exe (ESTABLISHED)'. None when they cannot be listed."""
    try:
        import psutil  # optional; used when installed
    except ImportError:
        psutil = None
    try:
        out = []
        if psutil is not None:
            for c in psutil.net_connections(kind="tcp"):
                if not c.raddr or c.raddr.ip != ip or c.raddr.port != port or not c.pid:
                    continue
                if c.status not in ("ESTABLISHED", "SYN_SENT") or c.laddr.port == exclude_port:
                    continue
                try:
                    name = psutil.Process(c.pid).name()
                except Exception:
                    name = "?"
                out.append(f"pid {c.pid} {name} ({c.status})")
            return out
        if sys.platform == "win32":
            conns = [c for c in _win_tcp_connections(ip, port) if c[0] != exclude_port]
            names = _win_process_names() if conns else {}
            return [f"pid {pid} {names.get(str(pid), '?')} ({st})" for _, pid, st in conns]
        if shutil.which("ss"):
            r = subprocess.run(["ss", "-tnpH"], capture_output=True, timeout=5)
            for line in r.stdout.decode("ascii", "replace").splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[4] != f"{ip}:{port}":
                    continue
                if parts[0] not in ("ESTAB", "SYN-SENT"):
                    continue
                if exclude_port and parts[3].rsplit(":", 1)[-1] == str(exclude_port):
                    continue
                m = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
                who = f"pid {m.group(2)} {m.group(1)}" if m else "pid ? (another user)"
                out.append(f"{who} ({parts[0]})")
            return out
    except Exception:
        return None
    return None


class Scope:
    """One short-lived TCP/SCPI session. Use as a context manager.

    Entering connects within `connect_timeout` and checks that the scope answers
    *IDN? on this connection, so a wrong address or a busy port fails fast with
    a message saying which, instead of every query waiting out `timeout`."""

    def __init__(
        self, host=None, port=None, timeout=None, connect_timeout=None, user="a tool call"
    ):
        self.host = host or RIGOL_HOST
        self.port = int(port or RIGOL_PORT)
        self.timeout = float(timeout or RIGOL_TIMEOUT)
        self.connect_timeout = float(connect_timeout or RIGOL_CONNECT_TIMEOUT)
        self.user = user
        self.s = None
        self.idn = ""
        self._locked = False
        self._unconfirmed = False  # a command was written and nothing read since

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"

    def __enter__(self):
        global _scope_user
        if not _scope_lock.acquire(timeout=LOCK_WAIT_S):
            raise ScopeError(
                f"this server is still using the scope for {_scope_user}; the scope takes one "
                "client at a time. Wait for that call to end (or cancel it), then retry."
            )
        self._locked = True
        _scope_user = self.user
        try:
            self._connect()
        except BaseException:
            self._close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._unconfirmed and self.s is not None:
                self._confirm(strict=exc_type is None)
        finally:
            self._close()

    def _confirm(self, strict: bool):
        """Wait (*OPC?) until the scope has worked through every command sent.
        The scope drops commands it has not read yet when the connection closes:
        measured on a DS1104Z Plus, a :RUN at the end of a batch of settings was
        lost every time without this, while the scope was still applying them."""
        try:
            if not strict:
                self.s.settimeout(min(self.timeout, 2.0))  # best effort after an error
            self.query("*OPC?")
        except ScopeError:
            if strict:
                raise ScopeError(
                    f"the scope at {self.addr} did not confirm the commands sent (*OPC?) within "
                    f"{self.timeout:g} s; they may not all have been applied."
                ) from None
        except OSError:
            if strict:
                raise

    def _close(self):
        if self.s is not None:
            try:
                self.s.close()
            except Exception:
                pass
            self.s = None
        if self._locked:
            self._locked = False
            _scope_lock.release()

    def _connect(self):
        try:
            self.s = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except TimeoutError:
            raise ScopeError(
                f"no connection to {self.addr} within {self.connect_timeout:g} s: nothing answers "
                f"at that address. Check RIGOL_HOST (this server uses {self.host}; a running MCP "
                "server keeps the value it was started with, so reconnect the MCP client after "
                "changing it) and that the scope is on and on the network."
            ) from None
        except ConnectionRefusedError:
            raise ScopeError(
                f"{self.addr} refused the connection: a device answers at {self.host} but takes "
                f"no SCPI on port {self.port}. Is RIGOL_HOST the scope, and is its LAN SCPI on?"
            ) from None
        except OSError as e:
            raise ScopeError(f"cannot connect to {self.addr}: {e}") from None
        self.s.settimeout(self.connect_timeout)
        try:
            self.write("*IDN?")
            self.idn = self._read_line().decode("ascii", "replace").strip()
            self._unconfirmed = False
        except TimeoutError:
            raise ScopeError(self._busy_message()) from None
        except OSError as e:
            raise ScopeError(
                f"{self.addr} accepted the connection but closed it before answering *IDN? "
                f"({e}). Retry; if it keeps happening, check that RIGOL_HOST is the scope."
            ) from None
        self.s.settimeout(self.timeout)

    def _busy_message(self) -> str:
        try:
            ip, own = self.s.getpeername()[0], self.s.getsockname()[1]
        except OSError:
            ip, own = self.host, None
        msg = (
            f"connected to {self.addr}, but the scope did not answer *IDN? within "
            f"{self.connect_timeout:g} s: port {self.port} busy or scope not answering. The scope "
            "works through one request at a time, one client at a time: a client that stopped "
            "reading in the middle of a reply (a stalled script, another MCP server or Claude "
            "session, UltraSigma) blocks every other client until it reads on or disconnects."
        )
        holders = _local_holders(ip, self.port, exclude_port=own)
        if holders:
            msg += (
                " Live connections to the scope from this PC: " + "; ".join(holders)
                + " (ending that process frees the scope at once)."
            )
        elif holders == []:
            msg += (
                " No other connection to it from this PC, so the holder is another machine, "
                "or the scope itself is stuck (does its front panel respond?)."
            )
        return msg + " Retry once that client is done."

    def write(self, cmd: str):
        self.s.sendall((cmd + "\n").encode("ascii"))
        self._unconfirmed = True

    def _recv_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.s.recv(n - len(buf))
            if not chunk:
                raise OSError("socket closed early")
            buf += chunk
        return bytes(buf)

    def _read_line(self) -> bytes:
        buf = bytearray()
        while not buf.endswith(b"\n"):
            chunk = self.s.recv(4096)
            if not chunk:
                raise OSError("socket closed early")
            buf += chunk
        return bytes(buf)

    def _no_reply(self, cmd: str) -> ScopeError:
        return ScopeError(
            f"no reply to {cmd!r} within {self.timeout:g} s from {self.addr}. The scope answered "
            "*IDN? on this connection, so the link is fine: the command may not produce a reply "
            "(a malformed query, or a setting sent with '?'); read :SYSTem:ERRor? to see what "
            "the scope made of it, or pass a longer timeout for a slow query."
        )

    def query(self, cmd: str) -> str:
        """Text query: send and read one '\\n'-terminated line."""
        self.write(cmd)
        try:
            line = self._read_line()
        except TimeoutError:
            raise self._no_reply(cmd) from None
        self._unconfirmed = False  # the scope answers in order: all before it is done
        return line.decode("ascii", "replace").strip()

    def query_block(self, cmd: str) -> bytes:
        """
        Binary query returning an IEEE 488.2 definite-length block:
            '#'  <1 digit N>  <N length digits>  <data...>  '\\n'
        Returns the raw <data> bytes (header + trailing newline stripped).
        """
        self.write(cmd)
        try:
            if self._recv_exactly(1) != b"#":
                raise OSError("expected IEEE 488.2 block header '#'")
            ndig = int(self._recv_exactly(1).decode("ascii"))
            nlen = int(self._recv_exactly(ndig).decode("ascii"))
            data = self._recv_exactly(nlen)
        except TimeoutError:
            raise self._no_reply(cmd) from None
        self._unconfirmed = False
        # drain the single trailing '\n' the scope appends after the block
        prev = self.s.gettimeout()
        try:
            self.s.settimeout(0.5)
            self.s.recv(1)
        except TimeoutError:
            pass
        finally:
            self.s.settimeout(prev)
        return data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _chan(channel) -> str:
    """Normalize '1' / 'ch1' / 'chan1' / 'CHANnel1' / 'math' -> 'CHANnel1' / 'MATH'."""
    c = str(channel).upper().strip()
    if c == "MATH":
        return "MATH"
    c = c.replace("CHANNEL", "").replace("CHAN", "").replace("CH", "")
    if c in ("1", "2", "3", "4"):
        return "CHANnel" + c
    raise ValueError(f"invalid channel {channel!r}; use 1-4 or MATH")


def _short(src: str) -> str:
    """'CHANnel3' -> 'CHAN3' (file and column names)."""
    return src.upper().replace("CHANNEL", "CHAN")


def _split(value) -> list[str]:
    """'CH3,CH4' / 'CH3 CH4' / ['CH3', 'CH4'] -> ['CH3', 'CH4']."""
    parts = value if isinstance(value, (list, tuple)) else [value]
    out = []
    for p in parts:
        out += re.split(r"[,;\s]+", str(p))
    return [t.strip("[]\"' ") for t in out if t.strip("[]\"' ")]


def _chan_list(value) -> list[str]:
    srcs = []
    for t in _split(value):
        src = _chan(t)
        if src not in srcs:
            srcs.append(src)
    if not srcs:
        raise ValueError("no channel given; use 1-4 or MATH")
    return srcs


def _norm_mode(mode: str) -> str:
    m = str(mode).upper().strip()
    m = {"NORMAL": "NORM", "MAXIMUM": "MAX"}.get(m, m)
    if m not in ("NORM", "RAW", "MAX"):
        raise ValueError(f"invalid mode {mode!r}; use NORM, RAW, or MAX")
    return m


def _out_path(stem: str, suffix: str) -> Path:
    """A file under RIGOL_OUT_DIR. `stem` is a plain name, never a path."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(stem)).strip("._") or "capture"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"{safe}{suffix}"


def _read_timeout(mode: str, timeout_s) -> float:
    if timeout_s:
        return float(timeout_s)
    return max(RIGOL_TIMEOUT, 60) if mode == "RAW" else RIGOL_TIMEOUT


def _single_shot(s: Scope):
    """Fire one :SINGle acquisition and wait (about 10 s at most) for STOP."""
    s.write(":SINGle")
    time.sleep(0.3)
    for _ in range(200):                               # wait for a stable acquisition
        if s.query(":TRIGger:STATus?") == "STOP":
            break
        time.sleep(0.05)


def _read_trace(s: Scope, src: str, mode: str):
    """Read one source's trace on an open session -> (volts, xinc, xorig)."""
    s.write(f":WAVeform:SOURce {src}")
    s.write(f":WAVeform:MODE {mode}")
    s.write(":WAVeform:FORMat BYTE")

    pre = s.query(":WAVeform:PREamble?").split(",")
    xinc, xorig = float(pre[4]), float(pre[5])
    yinc, yorig, yref = float(pre[7]), float(pre[8]), float(pre[9])

    if mode == "RAW":
        mdep = s.query(":ACQuire:MDEPth?")
        try:
            n_pts = int(float(mdep))
        except ValueError:
            n_pts = int(float(pre[2]))             # preamble fallback (AUTO depth)
        raw = bytearray()
        CHUNK = 250000                             # BYTE-mode per-read maximum
        pos = 1
        while pos <= n_pts:
            end = min(n_pts, pos + CHUNK - 1)
            want = end - pos + 1
            for _ in range(10):                    # a 250k chunk occasionally needs a retry
                s.write(f":WAVeform:STARt {pos}")
                s.write(f":WAVeform:STOP {end}")
                data = s.query_block(":WAVeform:DATA?")
                if len(data) == want:
                    raw += data
                    break
                time.sleep(0.05)
            else:
                raise OSError(f"chunk {pos}-{end} never returned {want} bytes")
            pos += CHUNK
            time.sleep(0.01)
    else:
        s.write(":WAVeform:STARt 1")
        s.write(":WAVeform:STOP 1200")
        raw = s.query_block(":WAVeform:DATA?")

    volts = [(b - yorig - yref) * yinc for b in raw]
    return volts, xinc, xorig


def _capture(channel: str, mode: str, single: bool, timeout_s=None):
    """
    Grab a trace and return (src, mode, volts, xinc, xorig).
    NORM  : ~1200 on-screen points, fast, scope keeps running.
    RAW   : full acquisition memory, chunked in <=250k reads; scope is STOPPED.
    """
    src = _chan(channel)
    mode = _norm_mode(mode)
    with Scope(timeout=_read_timeout(mode, timeout_s), user="get_waveform") as s:
        if single:
            _single_shot(s)
        if mode == "RAW":
            s.write(":STOP")                           # internal memory readable only when stopped
        volts, xinc, xorig = _read_trace(s, src, mode)
    return src, mode, volts, xinc, xorig


def _stats(volts):
    n = len(volts)
    if n == 0:
        return {}
    vmin, vmax = min(volts), max(volts)
    vmean = sum(volts) / n
    vrms = (sum(v * v for v in volts) / n) ** 0.5
    return {
        "v_min": vmin,
        "v_max": vmax,
        "v_pp": vmax - vmin,
        "v_mean": vmean,
        "v_rms": vrms,
    }


def _trace_result(src, mode, volts, xinc, xorig, preview_points, csv_path=None) -> dict:
    n = len(volts)
    result = {
        "source": src,
        "mode": mode,
        "samples": n,
        "x_increment_s": xinc,
        "duration_s": xinc * n,
        "sample_rate_Sa_s": (1.0 / xinc) if xinc else None,
        **_stats(volts),
    }
    if n:
        step = max(1, n // max(1, preview_points))
        result["preview_volts"] = [round(volts[i], 5) for i in range(0, n, step)][:preview_points]
    if csv_path is not None and n:
        with open(csv_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time_s", "volts"])
            for i, v in enumerate(volts):
                w.writerow([xorig + i * xinc, v])
        result["csv_path"] = str(csv_path)
    return result


def _fmt_meas(raw: str) -> str:
    """A measurement reply as a CSV cell: '' for the scope's 9.9E37 (no valid value)."""
    try:
        v = float(raw)
    except ValueError:
        return raw.replace(",", ";")
    return "" if abs(v) >= INVALID else format(v, ".7g")


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool()
def scope_info() -> str:
    """Return the oscilloscope identity (*IDN?) plus a snapshot of its current
    settings: timebase, sample rate, memory depth, trigger, and the state of
    each of the four analog channels. Good first call to confirm the link; it
    fails within a few seconds, naming the address, when the scope is not there
    or another client holds its port."""
    with Scope(user="scope_info") as s:
        lines = [
            f"IDN:           {s.idn}",
            f"Host:          {s.addr} (RIGOL_HOST)",
            f"Timebase:      {s.query(':TIMebase:MAIN:SCALe?')} s/div  "
            f"offset {s.query(':TIMebase:MAIN:OFFSet?')} s",
            f"Sample rate:   {s.query(':ACQuire:SRATe?')} Sa/s",
            f"Memory depth:  {s.query(':ACQuire:MDEPth?')} pts",
            f"Trigger:       status={s.query(':TRIGger:STATus?')} "
            f"sweep={s.query(':TRIGger:SWEep?')} "
            f"src={s.query(':TRIGger:EDGe:SOURce?')} "
            f"slope={s.query(':TRIGger:EDGe:SLOPe?')} "
            f"level={s.query(':TRIGger:EDGe:LEVel?')} V",
            "Channels:",
        ]
        for n in (1, 2, 3, 4):
            on = s.query(f":CHANnel{n}:DISPlay?")
            scal = s.query(f":CHANnel{n}:SCALe?")
            off = s.query(f":CHANnel{n}:OFFSet?")
            coup = s.query(f":CHANnel{n}:COUPling?")
            prob = s.query(f":CHANnel{n}:PROBe?")
            state = "ON " if on in ("1", "ON") else "off"
            lines.append(
                f"  CH{n}: {state}  {scal} V/div  offset {off} V  {coup}  probe {prob}x"
            )
    return "\n".join(lines)


@mcp.tool()
def get_screenshot(timeout_s: float | None = None) -> Image:
    """Capture the live oscilloscope display exactly as shown on screen and
    return it as a PNG image. Use this to visually inspect the trace, cursors,
    measurements, and menus: it is the fastest way to 'see' what the scope sees.
    timeout_s: reply timeout for this call (default max(RIGOL_TIMEOUT, 15))."""
    with Scope(timeout=timeout_s or max(RIGOL_TIMEOUT, 15), user="get_screenshot") as s:
        png = s.query_block(":DISPlay:DATA? ON,OFF,PNG")
    return Image(data=png, format="png")


@mcp.tool()
def get_stats(channel: str = "CH1", items: list[str] | None = None) -> dict:
    """Read measurement values for a channel. Values are SI units: volts,
    seconds, Hz; a value of null means the scope reported it as unmeasurable.

    channel: 1-4 or MATH (default CH1).
    items:   optional list of measurement tokens. Default set covers the common
             ones. Valid tokens include: VPP, VMAX, VMIN, VTOP, VBASe, VAMP,
             VAVG, VRMS, OVERshoot, PREShoot, FREQuency, PERiod, PWIDth, NWIDth,
             PDUTy (+duty), NDUTy (-duty), RTIMe, FTIMe. (Note: there is no
             'DUTY' token; use PDUTy/NDUTy.)"""
    src = _chan(channel)
    use = items or [
        "VPP", "VMAX", "VMIN", "VAVG", "VRMS",
        "FREQuency", "PERiod", "PWIDth", "NWIDth", "PDUTy", "RTIMe", "FTIMe",
    ]
    out = {}
    with Scope(user="get_stats") as s:
        for it in use:
            raw = s.query(f":MEASure:ITEM? {it},{src}")
            try:
                val = float(raw)
            except ValueError:
                val = None
            out[it] = None if (val is None or abs(val) >= INVALID) else val
    return {"source": src, "measurements": out}


def _poll_worker(srcs, items, seconds, interval_s, trig, stop_after, path, timeout, cancel, state):
    """Blocking poll loop (runs in a worker thread). Writes the CSV row by row."""
    header = ["time", "t_s"] + (["trig_status"] if trig else [])
    header += [f"{_short(src)}_{it.upper()}" for src in srcs for it in items]
    rows = []
    ended = "time up"
    t_start = time.monotonic()
    try:
        with Scope(timeout=timeout, user=f"a poll (CSV {path.name})") as s, open(
            path, "w", newline=""
        ) as f:
            w = csv.writer(f)
            w.writerow(header)
            f.flush()
            t_start = time.monotonic()
            next_t = t_start
            stop_at = None
            while True:
                now = time.monotonic()
                if cancel.is_set():
                    ended = "cancelled (the client gave up or cancelled the call)"
                    break
                if stop_at is not None and now >= stop_at:
                    ended = f"the scope reported STOP, then {stop_after:g} s"
                    break
                if now - t_start >= seconds:
                    break
                row = [datetime.now().strftime("%H:%M:%S.%f")[:-3], f"{now - t_start:.3f}"]
                if trig:
                    st = s.query(":TRIGger:STATus?")
                    row.append(st)
                    if stop_after is not None and stop_at is None and st == "STOP":
                        stop_at = time.monotonic() + stop_after
                for src in srcs:
                    for it in items:
                        row.append(_fmt_meas(s.query(f":MEASure:ITEM? {it},{src}")))
                w.writerow(row)
                f.flush()
                rows.append(row)
                state["rows"] = len(rows)
                next_t += interval_s
                wait = next_t - time.monotonic()
                if wait > 0:
                    cancel.wait(wait)
                else:
                    next_t = time.monotonic()  # the scope was slower than the interval
    except ScopeError as e:
        raise ScopeError(f"{e} (poll stopped after {len(rows)} rows; CSV so far: {path})") from None
    return header, rows, ended, time.monotonic() - t_start


def _poll_report(path, header, rows, ended, duration, interval_ms, inline_rows) -> str:
    lines = [
        f"poll: {len(rows)} rows in {duration:.1f} s (interval {interval_ms} ms), ended: {ended}",
        f"csv: {path}",
    ]
    first = 3 if header[2:3] == ["trig_status"] else 2
    if first == 3:
        changes = []
        for r in rows:
            if not changes or changes[-1][0] != r[2]:
                changes.append((r[2], r[0]))
        shown = ", ".join(f"{st} from {t}" for st, t in changes[:12])
        more = f" (+{len(changes) - 12} more changes)" if len(changes) > 12 else ""
        lines.append(f"trig_status: {shown}{more}")
    for i in range(first, len(header)):
        vals = []
        bad = 0
        for r in rows:
            try:
                vals.append(float(r[i]))
            except ValueError:
                bad += 1
        if vals:
            lines.append(
                f"{header[i]}: min {min(vals):.6g}  max {max(vals):.6g}  "
                f"mean {sum(vals) / len(vals):.6g}  ({bad} invalid)"
            )
        elif rows:
            lines.append(
                f"{header[i]}: no valid reading in {len(rows)} rows. The scope answers 9.9E37 "
                "for a channel that is off, a value it cannot measure, or a channel with no "
                "acquired data: in NORM sweep with no trigger (status WAIT) nothing is acquired, "
                "so use AUTO sweep to read levels."
            )
    lines.append("")
    body = [",".join(header)] + [",".join(r) for r in rows]
    if len(rows) <= inline_rows:
        lines += body
    else:
        lines += body[:6] + [f"... {len(rows) - 10} rows omitted, see the CSV ..."] + body[-5:]
    return "\n".join(lines)


@mcp.tool()
async def poll(
    channels: str | list[str] = "CH1",
    items: str | list[str] = "VAVG",
    seconds: float = 10.0,
    interval_ms: int = 500,
    trigger_status: bool = False,
    stop_after_stop_s: float | None = None,
    name: str | None = None,
    inline_rows: int = 60,
    timeout_s: float | None = None,
    ctx: Context | None = None,
) -> str:
    """Poll measurements over time on ONE connection: every interval_ms, for
    `seconds`, read each item of each channel (:MEASure:ITEM?) and log it with
    the PC's timestamp. Use it for timed checks (what a level does while you
    switch something) instead of calling get_stats in a loop. It only reads:
    it never sends :RUN, :STOP, :SINGle or any setting.

    channels  : one or more of 1-4 / MATH, e.g. "CH3,CH4" or ["CH3", "CH4"].
    items     : measurement tokens (see get_stats), default VAVG; one column per
                channel and item, e.g. CHAN3_VAVG.
    seconds   : how long to poll, at most 1200 s per call.
    interval_ms: time between row starts (>= 50 ms; a row of many queries can
                take longer, and then the rows simply follow each other).
    trigger_status: add a trig_status column (:TRIGger:STATus?: RUN, WAIT, TD,
                AUTO or STOP).
    stop_after_stop_s: for a single shot you armed yourself: end this many
                seconds after the scope first reports STOP (the single shot is
                complete). Implies trigger_status.
    name      : CSV file name (no folder); default poll_<date>_<time>.
    inline_rows: return every row up to this many, else a summary plus the
                first and last rows.
    timeout_s : reply timeout per query (default RIGOL_TIMEOUT).

    The CSV (columns time = PC local time at the start of the row, t_s =
    seconds since the first row, then the readings; empty cell = the scope had
    no valid value, 9.9E37) is written to RIGOL_OUT_DIR row by row, so it is
    complete up to the last row even if the call is abandoned. Long calls and
    MCP clients: Claude Code aborts a call that sends no response or progress
    for 30 minutes (measured: 1800 s). This tool reports progress every 5 s
    and stops early when the client cancels the call."""
    srcs = _chan_list(channels)
    its = _split(items) or ["VAVG"]
    if not 0 < seconds <= POLL_MAX_SECONDS:
        raise ValueError(
            f"seconds must be between 0 and {POLL_MAX_SECONDS}: MCP clients abort long calls "
            "(Claude Code after 30 minutes without a reply). Split a longer recording into "
            "several calls."
        )
    if interval_ms < POLL_MIN_INTERVAL_MS:
        raise ValueError(f"interval_ms must be at least {POLL_MIN_INTERVAL_MS}")
    if stop_after_stop_s is not None and stop_after_stop_s < 0:
        raise ValueError("stop_after_stop_s must be 0 or more")
    trig = trigger_status or stop_after_stop_s is not None
    path = _out_path(name or f"poll_{datetime.now():%Y%m%d_%H%M%S}", ".csv")

    cancel = threading.Event()
    state = {"rows": 0}
    loop = asyncio.get_running_loop()
    work = functools.partial(
        _poll_worker, srcs, its, float(seconds), interval_ms / 1000.0, trig,
        stop_after_stop_s, path, timeout_s, cancel, state,
    )
    fut = loop.run_in_executor(None, work)
    t0 = time.monotonic()
    try:
        while True:
            done, _ = await asyncio.wait({fut}, timeout=5)
            if done:
                break
            if ctx is not None:
                try:
                    await ctx.report_progress(
                        min(time.monotonic() - t0, seconds), seconds, f"{state['rows']} rows"
                    )
                except Exception:
                    pass
    except asyncio.CancelledError:
        cancel.set()  # the worker ends its CSV within one interval and frees the scope
        raise
    header, rows, ended, duration = fut.result()
    return _poll_report(path, header, rows, ended, duration, interval_ms, inline_rows)


@mcp.tool()
def get_waveform(
    channel: str | list[str] = "CH1",
    mode: str = "NORM",
    single: bool = False,
    save_csv: bool = True,
    preview_points: int = 200,
    screenshot: bool = False,
    name: str | None = None,
    timeout_s: float | None = None,
) -> dict:
    """Capture waveform traces and return summary statistics, a decimated
    preview array, sample timing, and (optionally) a CSV of each full trace.

    channel: one source (1-4 or MATH), or several, e.g. "CH1,CH4" or
             ["CH1", "CH4"]. Several sources (or screenshot=true) are read on
             ONE connection from the SAME acquisition: the scope is stopped
             once for the read, the screen is saved if asked, and then it is
             resumed (:RUN, or :SINGle if a single shot was armed) when it was
             running before. A scope that was already stopped stays stopped.
             The result then has one entry per source under "channels".
    mode   : NORM = ~1200 on-screen points, fast, scope keeps running.
             RAW  = full acquisition memory (up to millions of points); this
                    STOPS the scope (and leaves it stopped) and reads internal
                    memory in 250k chunks.
             MAX  = screen memory while running, internal memory while stopped.
    single : if true, fire one :SINGle acquisition and wait for it before
             reading (recommended for a clean RAW capture of a transient); the
             scope stays stopped afterwards.
    save_csv      : write each full time_s,volts trace to RIGOL_OUT_DIR.
    preview_points: how many decimated samples to inline per source.
    screenshot    : also save the screen as PNG beside the CSVs (path returned).
    name          : file name stem (no folder): <name>-CHAN1.csv ... <name>.png.
    timeout_s     : reply timeout for this call (default RIGOL_TIMEOUT, 60 s for RAW)."""
    srcs = _chan_list(channel)
    mode = _norm_mode(mode)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if len(srcs) == 1 and not screenshot:
        src, mode, volts, xinc, xorig = _capture(srcs[0], mode, single, timeout_s)
        stem = f"{name}-{_short(src)}" if name else f"{src}_{mode}_{stamp}"
        path = _out_path(stem, ".csv") if save_csv else None
        return _trace_result(src, mode, volts, xinc, xorig, preview_points, path)

    base = name or f"wave_{mode}_{stamp}"
    traces = []
    png = None
    resumed = None
    stopped_here = False
    timeout = _read_timeout(mode, timeout_s)
    with Scope(timeout=max(timeout, 15) if screenshot else timeout, user="get_waveform") as s:
        sweep = ""
        if single:
            _single_shot(s)
        elif s.query(":TRIGger:STATus?") != "STOP":
            sweep = s.query(":TRIGger:SWEep?")
            s.write(":STOP")                           # one acquisition for every source
            stopped_here = True
            time.sleep(0.3)
        try:
            for src in srcs:
                traces.append((src, *_read_trace(s, src, mode)))
            if screenshot:
                png = s.query_block(":DISPlay:DATA? ON,OFF,PNG")
        finally:
            if stopped_here and mode != "RAW":
                resumed = ":SINGle" if sweep.upper().startswith("SING") else ":RUN"
                s.write(resumed)

    result = {
        "mode": mode,
        "stopped_for_read": stopped_here or single or mode == "RAW",
        "resumed_with": resumed,
        "channels": [
            _trace_result(
                src, mode, volts, xinc, xorig, preview_points,
                _out_path(f"{base}-{_short(src)}", ".csv") if save_csv else None,
            )
            for src, volts, xinc, xorig in traces
        ],
    }
    if png is not None:
        shot = _out_path(base, ".png")
        shot.write_bytes(png)
        result["screenshot_path"] = str(shot)
    return result


@mcp.tool()
def plot_waveform(channel: str = "CH1", mode: str = "NORM", single: bool = False) -> Image:
    """Capture a waveform and return a rendered PNG plot of it (voltage vs time).
    Requires matplotlib (pip install matplotlib). For very deep RAW captures the
    plot is decimated to ~4000 points for legibility."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise RuntimeError(
            "matplotlib is required for plot_waveform: pip install matplotlib"
        ) from e

    src, mode, volts, xinc, xorig = _capture(channel, mode, single)
    n = len(volts)
    if n == 0:
        raise RuntimeError("no samples captured")

    step = max(1, n // 4000)
    idx = range(0, n, step)
    xs = [(xorig + i * xinc) * 1e3 for i in idx]  # ms
    ys = [volts[i] for i in idx]

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(xs, ys, linewidth=0.8)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("volts")
    ax.set_title(f"{src}  {mode}  {n} samples")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return Image(data=buf.getvalue(), format="png")


@mcp.tool()
def run_control(action: str) -> str:
    """Control acquisition. action is one of:
    run, stop, single, force (force a trigger), auto/autoscale (autoset)."""
    cmd = {
        "run": ":RUN",
        "stop": ":STOP",
        "single": ":SINGle",
        "force": ":TFORce",
        "auto": ":AUToscale",
        "autoscale": ":AUToscale",
    }.get(action.lower().strip())
    if not cmd:
        raise ValueError("action must be run, stop, single, force, or autoscale")
    with Scope(user="run_control") as s:
        s.write(cmd)
        time.sleep(0.1)
        status = s.query(":TRIGger:STATus?")
    return f"sent {cmd}; trigger status now {status}"


@mcp.tool()
def set_channel(
    channel: str,
    scale: float | None = None,
    offset: float | None = None,
    coupling: str | None = None,
    display: bool | None = None,
    probe: float | None = None,
) -> str:
    """Configure an analog channel (1-4). Only the arguments you pass are applied.
    scale    : V/div.
    offset   : V.
    coupling : AC, DC, or GND.
    display  : true/false to show/hide the channel.
    probe    : attenuation factor (e.g. 1, 10, 100)."""
    src = _chan(channel)
    if src == "MATH":
        raise ValueError("set_channel is for analog channels 1-4, not MATH")
    n = src[-1]
    applied = []
    with Scope(user="set_channel") as s:
        if scale is not None:
            s.write(f":CHANnel{n}:SCALe {scale}")
            applied.append(f"scale={scale}V/div")
        if offset is not None:
            s.write(f":CHANnel{n}:OFFSet {offset}")
            applied.append(f"offset={offset}V")
        if coupling is not None:
            cp = coupling.upper()
            if cp not in ("AC", "DC", "GND"):
                raise ValueError("coupling must be AC, DC, or GND")
            s.write(f":CHANnel{n}:COUPling {cp}")
            applied.append(f"coupling={cp}")
        if display is not None:
            s.write(f":CHANnel{n}:DISPlay {'ON' if display else 'OFF'}")
            applied.append(f"display={'ON' if display else 'OFF'}")
        if probe is not None:
            s.write(f":CHANnel{n}:PROBe {probe}")
            applied.append(f"probe={probe}x")
    return f"CH{n}: " + (", ".join(applied) if applied else "no changes requested")


@mcp.tool()
def set_timebase(scale: float | None = None, offset: float | None = None) -> str:
    """Set the main timebase. scale = seconds/div (5e-9 .. 50). offset = seconds."""
    applied = []
    with Scope(user="set_timebase") as s:
        if scale is not None:
            s.write(f":TIMebase:MAIN:SCALe {scale}")
            applied.append(f"scale={scale}s/div")
        if offset is not None:
            s.write(f":TIMebase:MAIN:OFFSet {offset}")
            applied.append(f"offset={offset}s")
    return "timebase: " + (", ".join(applied) if applied else "no changes requested")


@mcp.tool()
def set_trigger(
    source: str | None = None,
    level: float | None = None,
    slope: str | None = None,
    sweep: str | None = None,
) -> str:
    """Configure the edge trigger.
    source : channel 1-4 (or AC).
    level  : trigger level in volts.
    slope  : POS, NEG, or RFAL (rise+fall).
    sweep  : AUTO, NORM, or SING."""
    applied = []
    with Scope(user="set_trigger") as s:
        if source is not None:
            src = "AC" if str(source).upper() == "AC" else _chan(source)
            s.write(f":TRIGger:EDGe:SOURce {src}")
            applied.append(f"source={src}")
        if slope is not None:
            sl = slope.upper()
            if sl not in ("POS", "NEG", "RFAL", "POSITIVE", "NEGATIVE"):
                raise ValueError("slope must be POS, NEG, or RFAL")
            sl = {"POSITIVE": "POS", "NEGATIVE": "NEG"}.get(sl, sl)
            s.write(f":TRIGger:EDGe:SLOPe {sl}")
            applied.append(f"slope={sl}")
        if level is not None:
            s.write(f":TRIGger:EDGe:LEVel {level}")
            applied.append(f"level={level}V")
        if sweep is not None:
            sw = sweep.upper()
            if sw not in ("AUTO", "NORM", "NORMAL", "SING", "SINGLE"):
                raise ValueError("sweep must be AUTO, NORM, or SING")
            sw = {"NORMAL": "NORM", "SINGLE": "SING"}.get(sw, sw)
            s.write(f":TRIGger:SWEep {sw}")
            applied.append(f"sweep={sw}")
    return "trigger: " + (", ".join(applied) if applied else "no changes requested")


@mcp.tool()
def set_acquire(mem_depth: str) -> str:
    """Set acquisition memory depth. Pass AUTO or a point count valid for the
    active channel count, e.g. 1 ch: 12000/120000/1200000/12000000/24000000;
    2 ch: 6000/60000/600000/6000000/12000000; 3-4 ch: 3000/30000/300000/3000000/6000000."""
    with Scope(user="set_acquire") as s:
        s.write(f":ACQuire:MDEPth {mem_depth}")
        time.sleep(0.1)
        actual = s.query(":ACQuire:MDEPth?")
        srate = s.query(":ACQuire:SRATe?")
    return f"memory depth set; now {actual} pts at {srate} Sa/s"


@mcp.tool()
def scpi(command: str | list[str], timeout_s: float | None = None) -> str:
    """Raw SCPI escape hatch for anything the other tools do not cover (see the
    DS1000Z programming guide).

    command: one SCPI string, or a LIST of them that run in order on ONE
             connection. A command containing '?' is a query (it may carry
             arguments after the '?', e.g. ':MEAS:ITEM? VPP,CHAN1') and its
             reply is returned; any other command is written. One string
             returns the reply, or 'OK (write sent)'. A list returns one line
             per command: the command, then its reply or OK.
    timeout_s: reply timeout per query for this call (default RIGOL_TIMEOUT).

    A query the scope does not answer (a typo, a setting sent with '?') fails
    after timeout_s naming the command; in a list, the replies before it are
    in the error."""
    single = isinstance(command, str)
    cmds = [c.strip() for c in ([command] if single else command) if c and c.strip()]
    if not cmds:
        raise ValueError("no SCPI command given")
    done = []
    with Scope(timeout=timeout_s, user="scpi") as s:
        for i, cmd in enumerate(cmds, 1):
            try:
                if "?" in cmd:                 # '?' marks a query; it may be followed by args
                    reply = s.query(cmd)
                else:
                    s.write(cmd)
                    reply = "OK"
                    if not single and i < len(cmds):
                        time.sleep(WRITE_SETTLE_S)  # let the scope apply a setting
            except ScopeError as e:
                if single:
                    raise
                before = "\n".join(f"  {c}  {r}" for c, r in done) or "  (none)"
                raise ScopeError(
                    f"command {i} of {len(cmds)} failed: {e}\nreplies before it:\n{before}"
                ) from None
            done.append((cmd, reply))
    if single:
        return done[0][1] if "?" in cmds[0] else "OK (write sent)"
    width = max(len(c) for c in cmds)
    return "\n".join(f"{c:<{width}}  {r}" for c, r in done)


def main():
    """Console-script entry point; runs the MCP server on stdio transport."""
    mcp.run()  # stdio transport (default): how Claude Code launches it


if __name__ == "__main__":
    main()
