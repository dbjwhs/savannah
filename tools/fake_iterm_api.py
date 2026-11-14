# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# fake_iterm_api - a stand-in for iTerm2's API socket, for the attach tests.
#
# work/savannah_iterm_api.py moves a tab into an existing window by speaking
# iTerm2's API directly (a websocket on a Unix socket carrying one protobuf
# request); tests point $SAVANNAH_ITERM_SOCKET at this server so no real
# iTerm2 is involved. It is the other half of tools/fake_it2.py and shares
# its world: an accepted move rewrites $FAKE_IT2_LOG.state, so the next
# `it2 session list` shows the tab in its new window, in the order asked.
#
# The parsing here is written on its own, not imported from the module
# under test, so a mistake in the module's encoding is not mirrored away.
#
#   with FakeItermApi(state_path) as api:
#       env["SAVANNAH_ITERM_SOCKET"] = api.path
#       ... run `attach` ...
#       api.requests  -> [{"headers": {...}, "id": 1, "window": "W1",
#                          "tabs": ["TAB-1", "TAB-2"]}]
#
# mode (set before the call) says how the next connections are answered:
#   "ok"            apply the move, reply status OK
#   "status:<n>"    do not move, reply ReorderTabsResponse status n
#   "error:<text>"  do not move, reply ServerOriginatedMessage.error
#   "refuse"        answer the upgrade with HTTP 401
#   "close"         upgrade, then a websocket close frame instead of a reply
#   "hang"          upgrade, read the request, never answer
# Stdlib only.

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import tempfile
import threading

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_varint(buf, pos):
    shift = value = 0
    while True:
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _fields(buf):
    """[(field number, wire type, value)] of one message level."""
    out, pos = [], 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 2:
            n, pos = _read_varint(buf, pos)
            value, pos = buf[pos:pos + n], pos + n
        else:
            raise ValueError(f"wire type {wire}")
        out.append((num, wire, value))
    return out


def _varint(n):
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _len_field(num, payload):
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


class FakeItermApi:
    def __init__(self, state_path):
        self.state_path = state_path
        self.mode = "ok"
        self.requests = []
        # AF_UNIX paths are short (104 bytes on macOS): a directory straight
        # under /tmp, not under the long per-user temp dir.
        self._dir = tempfile.mkdtemp(prefix="fia-", dir="/tmp")
        self.path = os.path.join(self._dir, "s")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(4)
        self._held = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self._server.close()
        except OSError:
            pass
        for c in self._held:
            try:
                c.close()
            except OSError:
                pass
        shutil.rmtree(self._dir, ignore_errors=True)

    # -- the server --

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            try:
                self._one(conn)
            except (OSError, ValueError, IndexError):
                pass
            finally:
                if conn not in self._held:
                    conn.close()

    @staticmethod
    def _recv_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _one(self, conn):
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        mode = self.mode
        if mode == "refuse":
            self.requests.append({"headers": headers})
            conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            return
        accept = base64.b64encode(
            hashlib.sha1((headers.get("sec-websocket-key", "") + WS_GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      "Connection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n"
                      "Sec-WebSocket-Protocol: api.iterm2.com\r\n\r\n").encode())
        if mode == "close":
            self.requests.append({"headers": headers})
            conn.sendall(bytes([0x88, 0x00]))
            return

        b0, b1 = self._recv_exact(conn, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._recv_exact(conn, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._recv_exact(conn, 8))[0]
        masked = bool(b1 & 0x80)
        mask = self._recv_exact(conn, 4) if masked else b""
        payload = self._recv_exact(conn, n)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

        req = {"headers": headers, "masked": masked, "opcode": b0 & 0x0F, "id": None,
               "window": "", "tabs": []}
        for num, _, value in _fields(payload):
            if num == 1:
                req["id"] = value
            elif num == 125:
                for anum, _, assignment in _fields(value):
                    if anum != 3:
                        continue
                    for fnum, _, fvalue in _fields(assignment):
                        if fnum == 1:
                            req["window"] = fvalue.decode()
                        elif fnum == 2:
                            req["tabs"].append(fvalue.decode())
        self.requests.append(req)

        if mode == "hang":
            self._held.append(conn)
            return
        ident = _varint(1 << 3 | 0) + _varint(req["id"] or 0)
        if mode.startswith("error:"):
            body = ident + _len_field(2, mode.split(":", 1)[1].encode())
        elif mode.startswith("status:"):
            status = int(mode.split(":", 1)[1])
            body = ident + _len_field(125, _varint(4 << 3 | 0) + _varint(status))
        else:
            self._apply(req["window"], req["tabs"])
            body = ident + _len_field(125, b"")   # status OK is the default, so absent
        frame = bytes([0x82, len(body)]) if len(body) < 126 else \
            bytes([0x82, 126]) + struct.pack(">H", len(body))
        conn.sendall(frame + body)

    def _apply(self, window, tabs):
        """What iTerm2 does: the listed tabs become the first tabs of window,
        in that order; the window's other tabs stay after them; a window
        left with no tab is gone."""
        with open(self.state_path) as f:
            st = json.load(f)
        for s in st["sessions"].values():
            if s["tab_id"] in tabs:
                s["window_id"] = window

        def rank(item):
            tab = item[1]["tab_id"]
            return tabs.index(tab) if tab in tabs else len(tabs)

        inside = [(sid, s) for sid, s in st["sessions"].items() if s["window_id"] == window]
        outside = [(sid, s) for sid, s in st["sessions"].items() if s["window_id"] != window]
        inside.sort(key=rank)   # stable: unlisted tabs keep their order, after the listed
        st["sessions"] = dict(outside + inside)
        used = {s["window_id"] for s in st["sessions"].values()}
        st["windows"] = [w for w in st["windows"] if w in used]
        with open(self.state_path, "w") as f:
            json.dump(st, f)
