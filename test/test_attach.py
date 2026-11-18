#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Attach/detach test: drives `work new --detach`, `work attach` and
# `work detach` in work/savannah_work.py against a temp SAVANNAH_WORK_DIR, a
# temp SAVANNAH_SESSIONS_DIR with hand-written hook files, the fake it2
# (tools/fake_it2.py) via SAVANNAH_IT2 and the fake iTerm2 API socket
# (tools/fake_iterm_api.py) via SAVANNAH_ITERM_SOCKET. The two fakes share
# one world, so a move either of them makes shows in the next `it2 session
# list`. No real iTerm2, no claude, no network, no git (every record is
# --no-worktree, so a plain directory is a repo), stdlib only. Usage:
#   test_attach.py <path/to/savannah_work.py> <path/to/fake_it2.py>

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import Harness, check, finish, load_module  # noqa: E402


class AttachHarness(Harness):
    """The harness plus the fake API server. The sections run in order over
    one world: three sessions are made once and then moved about."""

    def __init__(self, module, fake):
        super().__init__(module, fake, prefix="savannah-attach-test-")
        sys.path.insert(0, os.path.dirname(self.fake))
        import fake_iterm_api
        self.sw = load_module(self.module)
        self.api_mod = self.sw.savannah_terminal.savannah_iterm_api
        self.repo = os.path.join(self.tmp, "repo")
        self.elsewhere = os.path.join(self.tmp, "elsewhere")   # where the plain tabs run
        for d in (self.repo, self.elsewhere):
            os.makedirs(d)
        # The command runs in the group window: the first tab's Claude pane,
        # once it exists (before that the fake's only window is the group).
        self.env["ITERM_SESSION_ID"] = "w0t0p0:PANE-C-1"
        self.state = self.it2_log + ".state"
        self.api = fake_iterm_api.FakeItermApi(self.state)
        self.env["SAVANNAH_ITERM_SOCKET"] = self.api.path

    def world(self):
        """pane id -> (tab id, window id), as the fake it2 lists them."""
        return {s["id"]: (s["tab_id"], s["window_id"]) for s in json.loads(self.it2("session", "list", "--json"))}

    def window_tabs(self, window):
        tabs = []
        for s in json.loads(self.it2("session", "list", "--json")):
            if s["window_id"] == window and s["tab_id"] not in tabs:
                tabs.append(s["tab_id"])
        return tabs

    def hook(self, session_id, pane, project=None):
        rec = {"session_id": session_id, "project": project or self.elsewhere, "state": "idle", "updated": 100,
               "last_prompt": "hi"}
        if pane is not None:
            rec["iterm_session_id"] = f"w0t0p0:{pane}"
        with open(os.path.join(self.sessions, session_id + ".json"), "w") as f:
            json.dump(rec, f)

    def refused(self, args, needle, label, extra_env=None, rc=1):
        p = self.run(args, extra_env)
        check(p.returncode == rc and needle in p.stderr,
              f"{label}: exit {rc} naming {needle!r} (got {p.returncode}, {p.stderr.strip()!r})")
        return p


def test_wire_units(h):
    """The hand-written protocol, byte for byte."""
    api = h.api_mod
    # ClientOriginatedMessage{id=1, reorder_tabs_request{assignments[{window_id="W", tab_ids=["7","8"]}]}}
    want = bytes([0x08, 0x01,                    # field 1 varint: id 1
                  0xEA, 0x07, 0x0B,              # field 125 (tag 1002 = EA 07), 11 bytes
                  0x1A, 0x09,                    # field 3: assignment, 9 bytes
                  0x0A, 0x01, ord("W"),          # field 1: window id
                  0x12, 0x01, ord("7"),          # field 2: tab id
                  0x12, 0x01, ord("8")])
    got = api.encode_reorder_tabs("W", ["7", "8"])
    check(got == want, f"reorder request encodes as iTerm2's api.proto has it ({got.hex()} vs {want.hex()})")
    check(api.encode_reorder_tabs("W", [7, 8]) == want, "tab ids given as numbers encode as their text")
    check(api.decode_reorder_reply(bytes([0x08, 0x01, 0xEA, 0x07, 0x00])) == "", "an empty response is OK")
    check(api.decode_reorder_reply(bytes([0x08, 0x01, 0xEA, 0x07, 0x02, 0x20, 0x00])) == "", "status 0 is OK")
    check(api.decode_reorder_reply(bytes([0x08, 0x01, 0xEA, 0x07, 0x02, 0x20, 0x03])) == "INVALID_TAB_ID",
          "status 3 is INVALID_TAB_ID")
    check(api.decode_reorder_reply(bytes([0xEA, 0x07, 0x02, 0x20, 0x09])) == "status 9",
          "an unknown status is reported by number")
    check(api.decode_reorder_reply(bytes([0x08, 0x01, 0x12, 0x03]) + b"bad") == "bad",
          "a server error is reported with its text")
    check("not a tab-move reply" in api.decode_reorder_reply(bytes([0x08, 0x01])),
          "a reply with no tab-move response is refused")
    check("does not parse" in api.decode_reorder_reply(bytes([0xEA, 0x07, 0x7F])),
          "a truncated reply is refused, not a traceback")
    check("does not parse" in api.decode_reorder_reply(bytes([0x0B])), "an unknown wire type is refused")
    long_tabs = [str(n) for n in range(200)]
    req = api.encode_reorder_tabs("pty-" + "A" * 36, long_tabs)
    check(len(req) > 126, "a long request crosses the 126-byte frame boundary (the extended length path)")


def test_new_detach(h):
    """new --detach opens the session in a window of its own."""
    for name in ("alpha", "beta"):
        j = h.run_json(["new", name, "--repo", h.repo, "--no-worktree", "--json"])
        check(j.get("name") == name, f"new {name} ({j})")
    check(h.window_tabs("W1") == ["TAB-1", "TAB-2"], f"plain new: tabs in the group window ({h.world()})")
    h.clear_log(keep_state=True)
    j = h.run_json(["new", "gamma", "--repo", h.repo, "--no-worktree", "--detach", "--json"])
    check(j.get("tab_id") == "TAB-3" and j.get("claude_pane") == "PANE-C-3" and j.get("shell_pane") == "PANE-S-3",
          f"new --detach records its tab and both panes ({j})")
    check(h.called(["window", "new"]) >= 0 and h.called(["tab", "new"]) < 0,
          f"new --detach makes a window, not a tab ({h.calls()})")
    w = h.world()
    check(w["PANE-C-3"][1] == "W3" and w["PANE-S-3"][1] == "W3", f"both panes are in the new window ({w})")
    check(h.window_tabs("W1") == ["TAB-1", "TAB-2"], "the group window is untouched")
    p = h.run(["new", "--help"])
    check("--detach" in p.stdout, "new --help lists --detach")


def test_attach(h):
    """attach moves a tab into the group window through iTerm2's API."""
    before = h.record("gamma")
    h.clear_log(keep_state=True)
    del h.api.requests[:]
    j = h.run_json(["attach", "gamma", "--json"])
    check(j == {"name": "gamma", "action": "attached", "tab_id": "TAB-3", "window_id": "W1"},
          f"attach reports the tab and its new window ({j})")
    check(len(h.api.requests) == 1, f"one API request ({h.api.requests})")
    req = h.api.requests[0] if h.api.requests else {}
    check(req.get("window") == "W1" and req.get("tabs") == ["TAB-1", "TAB-2", "TAB-3"],
          f"the request lists the window's own tabs first, then the newcomer ({req})")
    hd = req.get("headers", {})
    check(hd.get("x-iterm2-cookie") == "fake-cookie" and hd.get("x-iterm2-key") == "fake-key",
          f"the cookie and key from it2 auth cookie travel as headers ({hd})")
    check(hd.get("sec-websocket-protocol") == "api.iterm2.com" and hd.get("x-iterm2-disable-auth-ui") == "true",
          f"iTerm2's subprotocol and the no-prompt header are sent ({hd})")
    check(req.get("masked") is True and req.get("opcode") == 2 and req.get("id") == 1,
          f"one masked binary frame, message id 1 ({req})")
    check(h.called(["auth", "cookie", "--single-use"]) >= 0, "the cookie is single-use")
    w = h.world()
    check(w["PANE-C-3"] == ("TAB-3", "W1") and w["PANE-S-3"] == ("TAB-3", "W1"),
          f"both panes moved, the tab id kept ({w})")
    check(h.window_tabs("W1") == ["TAB-1", "TAB-2", "TAB-3"], "it is the last tab of the group window")
    check(h.record("gamma") == before, "attach changes nothing in the record")
    p = h.run(["attach", "gamma"])
    check(p.returncode == 1 and "already in the group window" in p.stderr and len(h.api.requests) == 1,
          f"attaching a tab that is already there is refused before any request ({p.stderr.strip()})")
    # The group window is the one the command runs in: from beta's shell pane
    # it is still W1, and from a pane iTerm2 does not list it is the first
    # listed session's.
    for pane in ("PANE-S-2", "NOT-IN-FAKE"):
        p = h.run(["attach", "gamma"], {"ITERM_SESSION_ID": f"w0t0p0:{pane}"})
        check(p.returncode == 1 and "already in the group window" in p.stderr,
              f"the group window is found from {pane} ({p.stderr.strip()})")


def test_detach(h):
    """detach takes a tab out into a window of its own, with it2."""
    before = h.record("gamma")
    h.clear_log(keep_state=True)
    del h.api.requests[:]
    j = h.run_json(["detach", "gamma", "--json"])
    check(j == {"name": "gamma", "action": "detached", "tab_id": "TAB-3", "window_id": "WD-3"},
          f"detach reports the tab and its new window ({j})")
    check(h.called(["tab", "move", "TAB-3"]) >= 0, f"detach is it2 tab move ({h.calls()})")
    check(not h.api.requests, "detach makes no API request")
    w = h.world()
    check(w["PANE-C-3"][1] == "WD-3" and w["PANE-S-3"][1] == "WD-3", f"both panes are in the new window ({w})")
    check(h.window_tabs("W1") == ["TAB-1", "TAB-2"], "the group window keeps the others")
    check(h.record("gamma") == before, "detach changes nothing in the record")
    h.clear_log(keep_state=True)
    h.refused(["detach", "gamma"], "already alone in its own window", "detaching a tab that is alone")
    check(h.called(["tab", "move"]) < 0, "nothing is moved then")
    p = h.run(["detach", "gamma"])
    check(p.stdout == "", "a refused detach prints nothing on stdout")


def test_toggle(h):
    """attach --toggle is the board's one key."""
    j = h.run_json(["attach", "gamma", "--toggle", "--json"])
    check(j.get("action") == "attached" and j.get("window_id") == "W1", f"toggle attaches a tab that is outside ({j})")
    j = h.run_json(["attach", "gamma", "--toggle", "--json"])
    check(j.get("action") == "detached" and j.get("window_id") == "WD-3", f"toggle detaches a tab in the group ({j})")
    # The last tab of the group window cannot be detached: it is alone there.
    j = h.run_json(["attach", "gamma", "--window", "W1", "--json"])
    check(j.get("window_id") == "W1", "--window names the target")
    p = h.run(["attach", "gamma", "--toggle"])
    check(p.returncode == 0 and "detached" in p.stdout and "window WD-3" in p.stdout,
          f"the plain line says what happened ({p.stdout.strip()})")
    # --window to somewhere that is not the group: beta joins gamma's window.
    j = h.run_json(["attach", "beta", "--window", "WD-3", "--json"])
    check(j.get("window_id") == "WD-3" and h.window_tabs("WD-3") == ["TAB-3", "TAB-2"],
          f"--window attaches to another window, last ({j}, {h.world()})")
    h.refused(["attach", "beta", "--window", "NOPE"], "no window NOPE", "an unknown --window")
    j = h.run_json(["attach", "beta", "--json"])
    check(h.window_tabs("W1") == ["TAB-1", "TAB-2"], f"beta is back in the group ({j})")


def test_plain_tab(h):
    """A tab the owner opened by hand, named by its hook session id."""
    h.it2("window", "new")   # W4 / TAB-4 / PANE-C-4: a tab opened by hand, elsewhere
    h.hook("plain-1", "PANE-C-4")
    del h.api.requests[:]
    j = h.run_json(["attach", "--session=plain-1", "--json"])
    check(j == {"session": "plain-1", "action": "attached", "tab_id": "TAB-4", "window_id": "W1"},
          f"a plain tab attaches by session id ({j})")
    check(h.api.requests and h.api.requests[0]["tabs"] == ["TAB-1", "TAB-2", "TAB-4"],
          f"the request appends it ({h.api.requests})")
    check(not os.path.exists(os.path.join(h.workroot, "plain-1")), "attaching a plain tab makes no record")
    j = h.run_json(["detach", "--session=plain-1", "--json"])
    check(j.get("session") == "plain-1" and j.get("action") == "detached" and j.get("window_id") == "WD-4",
          f"a plain tab detaches by session id ({j})")
    h.refused(["attach", "--session=nope"], "no live Claude session nope", "an unknown session id")
    h.hook("plain-2", None)
    h.refused(["attach", "--session=plain-2"], "recorded no iTerm2 pane", "a session with no pane")
    h.hook("plain-3", "NOT-A-PANE")
    h.refused(["attach", "--session=plain-3"], "not in iTerm2 any more", "a session whose pane is gone")
    for sid in ("plain-2", "plain-3"):
        os.remove(os.path.join(h.sessions, sid + ".json"))


def test_usage(h):
    """Naming the tab: exactly one of NAME and --session."""
    for verb in ("attach", "detach"):
        h.refused([verb], "name one tab", f"{verb} with no tab", rc=2)
        h.refused([verb, "gamma", "--session=plain-1"], "name one tab", f"{verb} with both", rc=2)
        h.refused([verb, "nosuch"], "nosuch", f"{verb} of an unknown name")
    h.refused(["attach", "alpha"], "has no open tab", "a work session whose tab is gone",
              {"FAKE_IT2_MISSING": "PANE-C-1,PANE-S-1"})
    p = h.run(["--help"])
    check("attach" in p.stdout and "detach" in p.stdout, "--help lists both verbs")


def test_api_failures(h):
    """Every way the API can fail is a refusal with a reason, and nothing moves."""
    check(h.world()["PANE-C-3"][1] == "WD-3", "gamma starts detached")

    def still_out(label):
        check(h.world()["PANE-C-3"][1] == "WD-3", f"{label}: the tab did not move")

    h.api.mode = "status:3"
    h.refused(["attach", "gamma"], "INVALID_TAB_ID", "iTerm2 answering INVALID_TAB_ID")
    still_out("bad status")
    h.api.mode = "error:no such thing"
    h.refused(["attach", "gamma"], "no such thing", "iTerm2 answering an error")
    still_out("server error")
    h.api.mode = "refuse"
    p = h.refused(["attach", "gamma"], "refused the connection", "iTerm2 refusing the upgrade")
    check("401" in p.stderr and "Enable Python API" in p.stderr, "the refusal carries the status and the setting to check")
    still_out("refused upgrade")
    h.api.mode = "close"
    h.refused(["attach", "gamma"], "closed the API connection before answering", "iTerm2 closing the socket")
    still_out("closed")
    h.api.mode = "ok"
    h.refused(["attach", "gamma"], "cannot reach iTerm2's API socket", "no socket at all",
              {"SAVANNAH_ITERM_SOCKET": os.path.join(h.tmp, "no-such-socket")})
    still_out("no socket")
    h.refused(["attach", "gamma"], "auth cookie", "it2 failing to hand out a cookie", {"FAKE_IT2_FAIL": "auth cookie"})
    still_out("no cookie")
    p = h.run(["attach", "gamma", "--json"], {"SAVANNAH_ITERM_SOCKET": os.path.join(h.tmp, "no-such-socket")})
    try:
        err = json.loads(p.stdout)
    except ValueError:
        err = {}
    check(p.returncode == 1 and err.get("code") == "error" and "cannot reach" in err.get("error", "")
          and "Traceback" not in p.stderr, f"a failed attach --json prints the error as JSON, no traceback ({p.stdout!r})")

    # A server that takes the request and never answers: in process, with the
    # timeout cut down, so the suite does not wait ten seconds.
    h.api.mode = "hang"
    old_timeout, old_env = h.api_mod.TIMEOUT, os.environ.get("SAVANNAH_ITERM_SOCKET")
    h.api_mod.TIMEOUT = 0.3
    os.environ["SAVANNAH_ITERM_SOCKET"] = h.api.path
    try:
        try:
            h.api_mod.reorder_tabs("W1", ["TAB-1", "TAB-3"], "c", "k")
            check(False, "a silent server raises")
        except h.sw.UserError as e:
            check("did not answer in time" in str(e), f"a silent server is a timeout, said so ({e})")
    finally:
        h.api_mod.TIMEOUT = old_timeout
        if old_env is None:
            del os.environ["SAVANNAH_ITERM_SOCKET"]
        else:
            os.environ["SAVANNAH_ITERM_SOCKET"] = old_env
    h.api.mode = "ok"
    still_out("timeout")

    # iTerm2 says OK and the tab is not there: said, not believed.
    def lying_apply(window, tabs):
        return None
    real_apply = h.api._apply
    h.api._apply = lying_apply
    h.refused(["attach", "gamma"], "accepted the move but the tab is not in the group window", "an OK that moved nothing")
    h.api._apply = real_apply
    j = h.run_json(["attach", "gamma", "--json"])
    check(j.get("action") == "attached", f"and with the API healthy again the attach goes through ({j})")


def test_log(h):
    with open(os.path.join(h.logdir, "savannah.log")) as f:
        text = f.read()
    check("tab attached" in text and "tab detached" in text, "both moves are logged at INFO")
    check("fake-cookie" not in text and "fake-key" not in text, "the cookie and key are never logged")


SECTIONS = (
    test_wire_units,
    test_new_detach,
    test_attach,
    test_detach,
    test_toggle,
    test_plain_tab,
    test_usage,
    test_api_failures,
    test_log,
)


def main():
    if len(sys.argv) != 3:
        print("usage: test_attach.py <savannah_work.py> <fake_it2.py>")
        return 2
    h = AttachHarness(sys.argv[1], sys.argv[2])
    try:
        for section in SECTIONS:
            section(h)
    finally:
        h.api.close()
    return finish("test_attach")


if __name__ == "__main__":
    sys.exit(main())
