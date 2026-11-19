"""A fake DS1000Z on 127.0.0.1 so the tools can be tested without a scope.

FakeScope is a small SCPI server on a thread. Like the real scope it serves one
client at a time (the next connection waits in the listen backlog until the
current one closes), answers queries with one '\\n'-terminated line or an IEEE
488.2 block, and stays silent after a command without '?'. Every command it
receives is logged in order, so a test can check what a tool sent.
"""

import socket
import threading

import pytest

import rigol_mcp

IDN = "RIGOL TECHNOLOGIES,DS1104Z Plus,FAKE0001,00.04.05.SP2"


def block(data: bytes) -> bytes:
    """IEEE 488.2 definite-length block plus the scope's trailing newline."""
    return b"#9" + f"{len(data):09d}".encode() + data + b"\n"


class FakeScope:
    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.log: list[str] = []
        self.mute = False                # accept, but never answer (a hung scope)
        self.no_reply: set[str] = set()  # queries that get no answer (a typo, say)
        self.status = ["RUN"]            # :TRIG:STAT? replies in turn; the last repeats
        self.sweep = "NORM"
        self.replies: dict[str, str] = {}
        self.meas: dict[tuple[str, str], str] = {}   # (ITEM, CHANn) -> reply
        self.waves: dict[str, bytes] = {}            # CHANn -> BYTE samples
        self.preambles: dict[str, str] = {}          # CHANn -> preamble line
        self.png = b"\x89PNG\r\n\x1a\nfake-screen"
        self.source = "CHAN1"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.srv.close()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                self._handle(conn)  # one client at a time, like the scope

    def _handle(self, conn):
        conn.settimeout(0.1)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                cmd = line.decode("ascii").strip()
                self.log.append(cmd)
                if self.mute or cmd in self.no_reply:
                    continue
                reply = self.answer(cmd)
                if reply is not None:
                    try:
                        conn.sendall(reply)
                    except OSError:
                        return

    @staticmethod
    def _src(token: str) -> str:
        return token.strip().upper().replace("CHANNEL", "CHAN")

    def answer(self, cmd: str) -> bytes | None:
        u = cmd.upper()
        if u == "*IDN?":
            return (IDN + "\n").encode()
        if u.startswith(":TRIGGER:STATUS?") or u.startswith(":TRIG:STAT?"):
            st = self.status.pop(0) if len(self.status) > 1 else self.status[0]
            return (st + "\n").encode()
        if u.startswith(":TRIGGER:SWEEP?"):
            return (self.sweep + "\n").encode()
        if u.startswith(":MEASURE:ITEM?") or u.startswith(":MEAS:ITEM?"):
            item, src = u.split("?", 1)[1].strip().split(",")
            return (self.meas.get((item, self._src(src)), "9.9E37") + "\n").encode()
        if u.startswith(":WAVEFORM:SOURCE "):
            self.source = self._src(u.split(" ", 1)[1])
            return None
        if u.startswith(":WAVEFORM:PREAMBLE?"):
            return (self.preambles[self.source] + "\n").encode()
        if u.startswith(":WAVEFORM:DATA?"):
            return block(self.waves[self.source])
        if u.startswith(":DISPLAY:DATA?"):
            return block(self.png)
        if "?" in u:
            return (self.replies.get(u, "1") + "\n").encode()
        return None  # a setting: the scope says nothing


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """A FakeScope with rigol_mcp pointed at it and its files under tmp_path."""
    scope = FakeScope()
    monkeypatch.setattr(rigol_mcp, "RIGOL_HOST", "127.0.0.1")
    monkeypatch.setattr(rigol_mcp, "RIGOL_PORT", scope.port)
    monkeypatch.setattr(rigol_mcp, "RIGOL_TIMEOUT", 2.0)
    monkeypatch.setattr(rigol_mcp, "RIGOL_CONNECT_TIMEOUT", 1.0)
    monkeypatch.setattr(rigol_mcp, "OUT_DIR", tmp_path)
    yield scope
    scope.close()
