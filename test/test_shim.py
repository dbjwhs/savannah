#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Shim test: speaks genuine MCP frames (newline-delimited JSON-RPC 2.0
# over stdio) to shim/peer.py, which shells out to fake_savannah.py.
# Deterministic, zero tokens, no network. Usage:
#   test_shim.py <path/to/peer.py> <path/to/fake_savannah.py>

import json
import subprocess
import sys

failures = 0


def check(cond, label):
    global failures
    if not cond:
        print(f"FAIL: {label}")
        failures += 1


class Shim:
    def __init__(self, peer, fake):
        self.proc = subprocess.Popen(
            [sys.executable, peer],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env={"SAVANNAH_BIN": f"{sys.executable} {fake}",
                 "SAVANNAH_KEY": "", "PATH": "/usr/bin:/bin"},
        )
        self.next_id = 0

    def request(self, method, params=None):
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        return json.loads(line)

    def notify(self, method):
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name, arguments):
        return self.request("tools/call",
                            {"name": name, "arguments": arguments})

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def main():
    peer, fake = sys.argv[1], sys.argv[2]
    shim = Shim(peer, fake)

    # ---- MCP lifecycle ----
    r = shim.request("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    check(r["result"]["serverInfo"]["name"] == "peer", "serverInfo name")
    check(r["result"]["protocolVersion"] == "2025-06-18", "protocol echo")
    shim.notify("notifications/initialized")

    r = shim.request("tools/list")
    names = sorted(t["name"] for t in r["result"]["tools"])
    check(names == ["ask_peer", "list_peers", "peer_status", "task_cancel",
                    "task_list", "task_new", "task_output", "task_send",
                    "task_status"], "tool names")
    for t in r["result"]["tools"]:
        check("inputSchema" in t and "description" in t,
              f"schema for {t['name']}")

    # ---- list_peers ----
    r = shim.call_tool("list_peers", {})
    check(r["result"]["isError"] is False, "ls not error")
    peers = json.loads(r["result"]["content"][0]["text"])
    check({p["name"] for p in peers} == {"dbj-mac", "dbj-devone"},
          "ls peer names")
    check(peers[1]["port"] == 39321, "ls port parsed")

    # ---- ask_peer: happy path ----
    r = shim.call_tool("ask_peer",
                       {"name": "dbj-devone", "prompt": "echo me"})
    res = r["result"]
    check(res["isError"] is False, "ask not error")
    check(res["content"][0]["text"] == "echo me", "ask echoes text")
    check('"is_error":false' in res["content"][1]["text"],
          "ask has result trailer")

    # ---- ask_peer: busy node surfaces as tool error ----
    r = shim.call_tool("ask_peer", {"name": "busy-node", "prompt": "x"})
    check(r["result"]["isError"] is True, "busy is error")
    check("busy" in r["result"]["content"][0]["text"], "busy reason")

    # ---- ask_peer: transport-level death ----
    r = shim.call_tool("ask_peer", {"name": "dead-node", "prompt": "x"})
    check(r["result"]["isError"] is True, "dead is error")
    check("died" in r["result"]["content"][0]["text"], "dead reason")

    # ---- peer_status ----
    r = shim.call_tool("peer_status", {"name": "dbj-devone"})
    check(r["result"]["content"][0]["text"] == "idle", "status idle")
    r = shim.call_tool("peer_status", {"name": "busy-node"})
    check(r["result"]["content"][0]["text"] == "busy", "status busy")

    # ---- task_new ----
    r = shim.call_tool("task_new",
                       {"name": "dbj-devone", "title": "demo", "prompt": "hi"})
    check(r["result"]["isError"] is False, "task_new not error")
    check("t-0001" in r["result"]["content"][0]["text"], "task_new returns id")

    # ---- task_list ----
    r = shim.call_tool("task_list", {"name": "dbj-devone"})
    check("t-0001" in r["result"]["content"][0]["text"], "task_list shows task")

    # ---- task_status ----
    r = shim.call_tool("task_status", {"name": "dbj-devone", "id": "t-0001"})
    check("idle" in r["result"]["content"][0]["text"], "task_status idle")

    # ---- task_send: accepted, and refused for a missing task ----
    r = shim.call_tool("task_send",
                       {"name": "dbj-devone", "id": "t-0001", "prompt": "more"})
    check(r["result"]["isError"] is False, "task_send ok")
    check("sent" in r["result"]["content"][0]["text"], "task_send sent")
    r = shim.call_tool("task_send",
                       {"name": "dbj-devone", "id": "t-404", "prompt": "x"})
    check(r["result"]["isError"] is True, "task_send missing is error")

    # ---- task_output: replays transcript + shows the FINAL trailer ----
    # The fake tail emits two invocations (an interim error_max_turns, then a
    # success); the shim must surface the last one, not the first.
    r = shim.call_tool("task_output", {"name": "dbj-devone", "id": "t-0001"})
    check(r["result"]["isError"] is False, "task_output ok")
    check("hello" in r["result"]["content"][0]["text"], "task_output replays")
    trailer = r["result"]["content"][1]["text"]
    check('"is_error":false' in trailer, "task_output shows final trailer")
    check("error_max_turns" not in trailer, "task_output not the interim one")

    # ---- task_cancel: ok, and error for a missing task ----
    r = shim.call_tool("task_cancel", {"name": "dbj-devone", "id": "t-0001"})
    check("cancelled" in r["result"]["content"][0]["text"], "task_cancel ok")
    r = shim.call_tool("task_cancel", {"name": "dbj-devone", "id": "t-404"})
    check(r["result"]["isError"] is True, "task_cancel missing is error")

    # ---- protocol errors ----
    r = shim.call_tool("no_such_tool", {})
    check("error" in r and r["error"]["code"] == -32602, "unknown tool")
    r = shim.request("no/such/method")
    check("error" in r and r["error"]["code"] == -32601, "unknown method")
    r = shim.request("ping")
    check(r["result"] == {}, "ping")

    shim.close()
    if failures == 0:
        print("test_shim: all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
