# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_iterm_api - the one iTerm2 API request `it2` has no command for
# (savannah_work.py is the entry point; this is a flat sibling module).
#
# `it2 tab move` takes a tab out into its own new window, and nothing in it2
# puts a tab INTO a window that already exists. iTerm2's own API can
# (ReorderTabsRequest: "these tab ids, in this order, in this window"; a tab
# listed from another window is moved, and a window left with no tab closes
# by itself). The official client for that API is the `iterm2` Python
# package, which needs protobuf and websockets; this module speaks just
# enough of the same protocol with the standard library, so `attach` adds no
# dependency:
#
#   a Unix socket      ~/Library/Application Support/iTerm2/private/socket
#                      ($SAVANNAH_ITERM_SOCKET overrides it; tests point it at
#                      tools/fake_iterm_api.py)
#   a websocket        the HTTP upgrade by hand, subprotocol api.iterm2.com,
#                      the cookie and key from `it2 auth cookie` as headers,
#                      one masked binary frame out, one frame back
#   one protobuf       ClientOriginatedMessage{id=1, reorder_tabs_request=125
#   message            {assignments=3 [{window_id=1, tab_ids=2...}]}}, and the
#                      reply ServerOriginatedMessage{error=2 |
#                      reorder_tabs_response=125 {status=4}}
#
# The field numbers are iTerm2's published api.proto, pinned live on
# 11/14/2026 against iTerm2 3.7.7 (the same request the iterm2 package 2.30
# sends for Window.async_set_tabs). Nothing warns us if they change, so
# every way this can go wrong is a UserError that says what happened, never
# a traceback and never a silent success: the caller also checks afterwards
# that the tab is where it was sent.
#
# Imports: the standard library, savannah_log and savannah_core.

import base64
import os
import socket
import struct

from savannah_log import log_debug
from savannah_core import UserError

SOCKET_DEFAULT = "~/Library/Application Support/iTerm2/private/socket"
SUBPROTOCOL = "api.iterm2.com"
TIMEOUT = 10                       # seconds for the connect and for each read
MAX_FRAME = 1 << 20                # a reply is a few bytes; anything huge is not one

# ClientOriginatedMessage / ServerOriginatedMessage
F_ID = 1
F_ERROR = 2
F_REORDER_TABS = 125
# ReorderTabsRequest, its Assignment, ReorderTabsResponse
F_ASSIGNMENTS = 3
F_WINDOW_ID = 1
F_TAB_IDS = 2
F_STATUS = 4
STATUS = {0: "OK", 1: "INVALID_ASSIGNMENT", 2: "INVALID_WINDOW_ID", 3: "INVALID_TAB_ID"}

OP_BINARY = 0x2
OP_CLOSE = 0x8


def socket_path():
    return os.environ.get("SAVANNAH_ITERM_SOCKET", "") or os.path.expanduser(SOCKET_DEFAULT)


# ---- protobuf, just enough ---------------------------------------------------

def _varint(n):
    out = bytearray()
    while True:
        low, n = n & 0x7F, n >> 7
        out.append(low | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _len_field(num, payload):
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


def encode_reorder_tabs(window_id, tab_ids, msg_id=1):
    """The ClientOriginatedMessage that puts tab_ids, in order, at the front
    of window_id. Tabs the window already has and the list leaves out stay
    after the listed ones."""
    assignment = _len_field(F_WINDOW_ID, window_id.encode())
    for tab in tab_ids:
        assignment += _len_field(F_TAB_IDS, str(tab).encode())
    request = _len_field(F_ASSIGNMENTS, assignment)
    return _varint(F_ID << 3 | 0) + _varint(msg_id) + _len_field(F_REORDER_TABS, request)


def decode_fields(buf):
    """One message level -> {field number: [values]}; a varint is an int,
    anything length-delimited or fixed is bytes. ValueError on a buffer that
    is not a protobuf message."""
    out = {}
    pos = 0

    def varint():
        nonlocal pos
        shift = value = 0
        while True:
            if pos >= len(buf) or shift > 63:
                raise ValueError("truncated varint")
            byte = buf[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    def take(n):
        nonlocal pos
        if n < 0 or pos + n > len(buf):
            raise ValueError("truncated field")
        chunk = bytes(buf[pos:pos + n])
        pos += n
        return chunk

    while pos < len(buf):
        key = varint()
        num, wire = key >> 3, key & 7
        if wire == 0:
            value = varint()
        elif wire == 2:
            value = take(varint())
        elif wire == 1:
            value = take(8)
        elif wire == 5:
            value = take(4)
        else:
            raise ValueError(f"wire type {wire}")
        out.setdefault(num, []).append(value)
    return out


def decode_reorder_reply(data):
    """-> "" when iTerm2 said OK, else what it said instead."""
    try:
        reply = decode_fields(data)
        if F_ERROR in reply and isinstance(reply[F_ERROR][0], bytes):
            return reply[F_ERROR][0].decode("utf-8", "replace") or "an error with no text"
        if F_REORDER_TABS not in reply or not isinstance(reply[F_REORDER_TABS][0], bytes):
            return f"a reply that is not a tab-move reply (fields {sorted(reply)})"
        status = decode_fields(reply[F_REORDER_TABS][0]).get(F_STATUS, [0])[0]
    except ValueError as e:
        return f"a reply that does not parse ({e})"
    if status == 0:
        return ""
    return STATUS.get(status, f"status {status}")


# ---- websocket, just enough --------------------------------------------------

def _read_exact(sock, pending, n):
    """n bytes, from what is already read (pending) and then the socket.
    -> (the bytes, what is left of pending)."""
    while len(pending) < n:
        chunk = sock.recv(65536)
        if not chunk:
            raise UserError("iTerm2 closed the API connection before answering")
        pending += chunk
    return pending[:n], pending[n:]


def _handshake(sock, cookie, key):
    nonce = base64.b64encode(os.urandom(16)).decode()
    lines = [
        "GET / HTTP/1.1",
        "Host: localhost",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {nonce}",
        "Sec-WebSocket-Version: 13",
        f"Sec-WebSocket-Protocol: {SUBPROTOCOL}",
        "origin: ws://localhost/",
        "x-iterm2-library-version: python 2.30",
        "x-iterm2-disable-auth-ui: true",
        "x-iterm2-advisory-name: savannah",
    ]
    if cookie:
        lines.append(f"x-iterm2-cookie: {cookie}")
    if key:
        lines.append(f"x-iterm2-key: {key}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    head = b""
    while b"\r\n\r\n" not in head:
        if len(head) > 65536:
            raise UserError("iTerm2's API answered the connection with something that is not HTTP")
        chunk = sock.recv(4096)
        if not chunk:
            break
        head += chunk
    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    parts = status.split()
    if len(parts) < 2 or parts[1] != "101":
        raise UserError(f"iTerm2's API refused the connection ({status or 'no answer'}); is its "
                        "Python API on (iTerm2 > Settings > General > Magic > Enable Python API)?")
    return head.split(b"\r\n\r\n", 1)[1]


def _send_frame(sock, payload):
    # A client frame is always masked (RFC 6455 5.3).
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | OP_BINARY, 0x80 | n])
    elif n < 65536:
        head = bytes([0x80 | OP_BINARY, 0x80 | 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | OP_BINARY, 0x80 | 127]) + struct.pack(">Q", n)
    sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _recv_frame(sock, pending):
    """One unfragmented frame -> its payload. iTerm2 answers a request with
    a single binary frame; a close frame or anything else is a UserError."""
    head, pending = _read_exact(sock, pending, 2)
    opcode, n = head[0] & 0x0F, head[1] & 0x7F
    if n == 126:
        raw, pending = _read_exact(sock, pending, 2)
        n = struct.unpack(">H", raw)[0]
    elif n == 127:
        raw, pending = _read_exact(sock, pending, 8)
        n = struct.unpack(">Q", raw)[0]
    if n > MAX_FRAME:
        raise UserError(f"iTerm2's API sent a {n}-byte frame where a short reply was expected")
    mask = b""
    if head[1] & 0x80:
        mask, pending = _read_exact(sock, pending, 4)
    payload, pending = _read_exact(sock, pending, n)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    if opcode == OP_CLOSE:
        raise UserError("iTerm2 closed the API connection before answering")
    if opcode != OP_BINARY or not head[0] & 0x80:
        raise UserError(f"iTerm2's API sent a frame that is not a reply (opcode {opcode})")
    return payload


# ---- the request ---------------------------------------------------------------

def reorder_tabs(window_id, tab_ids, cookie="", key=""):
    """Make tab_ids, in that order, the first tabs of window_id; a tab that
    sits in another window is moved there. Raises UserError with the reason
    when iTerm2 cannot be reached, refuses, or answers anything but OK."""
    path = socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        try:
            sock.connect(path)
        except OSError as e:
            raise UserError(f"cannot reach iTerm2's API socket ({path}): {e.strerror or e}; is "
                            "iTerm2 running with its Python API on (Settings > General > Magic)?")
        try:
            pending = _handshake(sock, cookie, key)
            _send_frame(sock, encode_reorder_tabs(window_id, tab_ids))
            reply = _recv_frame(sock, pending)
        except socket.timeout:
            raise UserError("iTerm2's API did not answer in time")
        except (BrokenPipeError, ConnectionResetError):
            # It hung up while the request was still being written.
            raise UserError("iTerm2 closed the API connection before answering")
        except OSError as e:
            raise UserError(f"talking to iTerm2's API failed: {e.strerror or e}")
    finally:
        sock.close()
    problem = decode_reorder_reply(reply)
    log_debug("iterm api reorder_tabs", window=window_id, tabs=list(tab_ids), reply=problem or "OK")
    if problem:
        raise UserError(f"iTerm2 would not move the tab: {problem}")
