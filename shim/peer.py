#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# peer - the savannah MCP shim. Gives stock Claude Code three tools:
#
#   list_peers()                    -> nodes on the mesh (mDNS browse)
#   ask_peer(name, prompt, ...)     -> one stateless ask, streamed remotely
#   peer_status(name)               -> "idle" or "busy"
#
# Implementation: a Model Context Protocol stdio server in pure stdlib
# Python that shells out to the savannah CLI, which carries the tested
# C++ paths for discovery, HMAC, and streaming. Generated song Python
# bindings are the v2 path once song's Python runtime learns streaming
# and SecureTransport (today it is unary-only).
#
# Config (environment):
#   SAVANNAH_BIN             savannah CLI (default: "savannah" on PATH)
#   SAVANNAH_KEY             HMAC key file passed as --key (optional)
#   SAVANNAH_ASK_TIMEOUT_MS  per-ask agent timeout (default 300000)
#
# Wire: newline-delimited JSON-RPC 2.0 on stdin/stdout per the MCP stdio
# transport. Logs go to stderr; stdout carries protocol frames only.

import json
import os
import shlex
import subprocess
import sys

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "peer", "version": "0.1.0"}

ASK_TIMEOUT_MS = int(os.environ.get("SAVANNAH_ASK_TIMEOUT_MS", "300000"))
# Margin over the agent timeout so the CLI itself is what times out.
SUBPROCESS_MARGIN_S = 30

TOOLS = [
    {
        "name": "list_peers",
        "description": (
            "List Claude agent nodes discoverable on the local network "
            "mesh. Returns name, host, and port per node. Nodes run their "
            "own Claude with their own auth; asking one burns its tokens."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "ask_peer",
        "description": (
            "Ask a peer agent node one stateless question and stream back "
            "its answer. Every ask is a fresh context on the remote agent "
            "(no session memory). The result trailer reports cost, "
            "duration, and turns. Delegation guidance: good for work that "
            "benefits from the peer's machine (its files, its platform, "
            "its toolchain) or from a clean context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Peer node name as shown by list_peers",
                },
                "prompt": {
                    "type": "string",
                    "description": "The question or task for the peer",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": (
                        "Per-ask agent timeout in milliseconds "
                        "(default from SAVANNAH_ASK_TIMEOUT_MS)"
                    ),
                },
            },
            "required": ["name", "prompt"],
        },
    },
    {
        "name": "peer_status",
        "description": (
            "Report whether a peer node is idle or busy (v1 nodes are "
            "single-flight: a busy node rejects new asks immediately)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Peer node name as shown by list_peers",
                }
            },
            "required": ["name"],
        },
    },
]


def savannah_argv():
    return shlex.split(os.environ.get("SAVANNAH_BIN", "savannah"))


def key_args():
    key = os.environ.get("SAVANNAH_KEY", "")
    return ["--key", key] if key else []


def run_cli(args, timeout_s):
    argv = savannah_argv() + args
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"savannah CLI not found ({argv[0]!r}); set SAVANNAH_BIN"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"savannah CLI timed out after {timeout_s}s: {' '.join(args)}"
        )


def parse_result_trailer(stderr_text):
    for line in stderr_text.splitlines():
        if line.startswith("[result] "):
            try:
                return json.loads(line[len("[result] "):])
            except json.JSONDecodeError:
                return {"raw": line}
    return None


def tool_list_peers(_args):
    proc = run_cli(["ls"], timeout_s=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "savannah ls failed")
    peers = []
    for line in proc.stdout.splitlines():
        parts = line.strip().rsplit(" ", 1)
        if len(parts) != 2 or ":" not in parts[1]:
            continue
        host, _, port = parts[1].rpartition(":")
        peers.append({"name": parts[0], "host": host, "port": int(port)})
    if not peers:
        note = proc.stderr.strip()
        return [text_block(note or "no peers on the mesh")], False
    return [text_block(json.dumps(peers, indent=2))], False


def tool_ask_peer(args):
    name = args["name"]
    prompt = args["prompt"]
    timeout_ms = int(args.get("timeout_ms", ASK_TIMEOUT_MS))
    cli = ["ask", name, prompt, "--timeout-ms", str(timeout_ms)] + key_args()
    proc = run_cli(cli, timeout_s=timeout_ms / 1000 + SUBPROCESS_MARGIN_S)
    trailer = parse_result_trailer(proc.stderr)

    blocks = []
    if proc.stdout:
        blocks.append(text_block(proc.stdout))
    if trailer is not None:
        blocks.append(text_block(
            "result: " + json.dumps(trailer, separators=(",", ":"))))
    is_error = proc.returncode != 0
    if is_error and not blocks:
        blocks.append(
            text_block(proc.stderr.strip() or f"ask {name} failed"))
    return blocks, is_error


def tool_peer_status(args):
    proc = run_cli(["status", args["name"]] + key_args(), timeout_s=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "status failed")
    return [text_block(proc.stdout.strip())], False


HANDLERS = {
    "list_peers": tool_list_peers,
    "ask_peer": tool_ask_peer,
    "peer_status": tool_peer_status,
}


def text_block(text):
    return {"type": "text", "text": text}


def reply(msg_id, result):
    emit({"jsonrpc": "2.0", "id": msg_id, "result": result})


def reply_error(msg_id, code, message):
    emit({"jsonrpc": "2.0", "id": msg_id,
          "error": {"code": code, "message": message}})


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def handle(msg):
    method = msg.get("method", "")
    msg_id = msg.get("id")
    is_notification = msg_id is None

    if method == "initialize":
        client_ver = msg.get("params", {}).get("protocolVersion",
                                               PROTOCOL_VERSION)
        reply(msg_id, {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params", {})
        handler = HANDLERS.get(params.get("name", ""))
        if handler is None:
            reply_error(msg_id, -32602,
                        f"unknown tool: {params.get('name')!r}")
            return
        try:
            blocks, is_error = handler(params.get("arguments", {}))
            reply(msg_id, {"content": blocks, "isError": is_error})
        except Exception as e:  # tool failure -> tool-level error result
            reply(msg_id, {"content": [text_block(str(e))],
                           "isError": True})
    elif not is_notification:
        reply_error(msg_id, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"peer: dropping non-JSON line: {line[:80]}",
                  file=sys.stderr)
            continue
        handle(msg)


if __name__ == "__main__":
    main()
