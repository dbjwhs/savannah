#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Shim test: speaks genuine MCP frames (newline-delimited JSON-RPC 2.0
# over stdio) to shim/peer.py, which shells out to fake_savannah.py.
# Deterministic, zero tokens, no network. Usage:
#   test_shim.py <path/to/peer.py> <path/to/fake_savannah.py>
#
# The work_* tools (Phase 5d) are driven through the REAL work module
# (<peer dir>/../work/savannah_work.py) against a temp SAVANNAH_WORK_DIR, a
# temp SAVANNAH_SESSIONS_DIR, tools/fake_it2.py (next to the fake
# savannah) in place of iTerm2, and a throwaway git repo. No real iTerm2, no
# real claude. Without git on PATH the worktree checks are skipped and the
# --no-worktree path is still exercised.

import json
import os
import shutil
import subprocess
import sys
import tempfile

failures = 0


def check(cond, label):
    global failures
    if not cond:
        print(f"FAIL: {label}")
        failures += 1


class Shim:
    def __init__(self, peer, fake, extra_env=None):
        env = {"SAVANNAH_BIN": f"{sys.executable} {fake}",
               "SAVANNAH_KEY": "", "PATH": "/usr/bin:/bin"}
        env.update(extra_env or {})
        self.proc = subprocess.Popen(
            [sys.executable, peer],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
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


def git_ok(repo):
    """Make a throwaway repo with one commit; False if git is unusable."""
    if shutil.which("git", path="/usr/bin:/bin") is None:
        return False
    base = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"]
    try:
        for args in (["init", "-q"],
                     ["commit", "-q", "--allow-empty", "-m", "init"]):
            if subprocess.run(base + args, capture_output=True,
                              env=GIT_ENV).returncode != 0:
                return False
    except OSError:
        return False
    return True


GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"}


def text_of(r):
    return r["result"]["content"][0]["text"]


def test_work_tools(peer, fake, tmp):
    """Drive the real work module through the shim, end to end."""
    work_py = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(peer)), "..", "work", "savannah_work.py"))
    fake_it2 = os.path.join(os.path.dirname(os.path.abspath(fake)),
                            "fake_it2.py")
    check(os.path.exists(work_py), f"work module present at {work_py}")
    check(os.path.exists(fake_it2), f"fake it2 present at {fake_it2}")

    work_dir = os.path.join(tmp, "work")
    sessions_dir = os.path.join(tmp, "sessions")
    repo = os.path.join(tmp, "repo")
    log = os.path.join(tmp, "it2.log")
    for d in (work_dir, sessions_dir, repo):
        os.makedirs(d)
    have_git = git_ok(repo)
    if not have_git:
        print("test_shim: git unavailable, worktree checks skipped")

    env = {
        "HOME": tmp,  # keep the user's gitconfig out of the temp repo
        "GIT_CONFIG_NOSYSTEM": "1",
        "SAVANNAH_WORK_BIN": f"{sys.executable} {work_py}",
        "SAVANNAH_WORK_DIR": work_dir,
        "SAVANNAH_SESSIONS_DIR": sessions_dir,
        "SAVANNAH_IT2": f"{sys.executable} {fake_it2}",
        "SAVANNAH_LOG_DIR": os.path.join(tmp, "log"),   # savannah's one log, never the user's
        "FAKE_IT2_LOG": log,
        "ITERM_SESSION_ID": "w9t9p9:NOT-IN-FAKE",
    }
    shim = Shim(peer, fake, env)
    shim.request("initialize", {"protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}})
    shim.notify("notifications/initialized")

    def it2_calls():
        """Every it2 argv the fake saw, in order (one JSON array per line)."""
        try:
            with open(log) as f:
                return [json.loads(ln) for ln in f if ln.strip()]
        except OSError:
            return []

    def runs_into(calls, pane):
        """The texts typed into a pane (`session run --session PANE -- TEXT`)."""
        return [c[-1] for c in calls if c[:4] == ["session", "run", "--session", pane] and len(c) == 6]

    def sent_into(calls, pane):
        """The texts say/sh typed into a pane: `session send --session PANE -- TEXT`,
        each followed by a `session send` of a carriage return (the Enter key;
        a text without one is reported as such so the check fails loudly)."""
        sends = [c[-1] for c in calls if c[:4] == ["session", "send", "--session", pane] and len(c) == 6]
        texts, i = [], 0
        while i < len(sends):
            if i + 1 < len(sends) and sends[i + 1] == "\r":
                texts.append(sends[i])
                i += 2
            else:
                texts.append(sends[i] + " (no Enter)")
                i += 1
        return texts

    # ---- work_new: worktree path when git works, else --no-worktree ----
    new_args = {"name": "shim-demo", "repo": repo, "prompt": "hello there",
                "note": "first note"}
    if not have_git:
        new_args["no_worktree"] = True
    r = shim.call_tool("work_new", new_args)
    check(r["result"]["isError"] is False, f"work_new ok: {text_of(r)}")
    rec = json.loads(text_of(r))
    check(rec["name"] == "shim-demo", "work_new record name")
    check(rec["repo"] == repo, "work_new record repo")
    check(rec["tab_id"] == "TAB-1", "work_new tab id from the fake it2")
    check(rec["claude_pane"] == "PANE-C-1", "work_new claude pane id")
    check(rec["shell_pane"] == "PANE-S-1", "work_new shell pane id")
    check(rec["terminal"] == "iterm", "work_new terminal kind")
    check(rec["prompt"] == "hello there", "work_new prompt recorded")
    check(isinstance(rec["created"], int) and rec["created"] > 0,
          "work_new created epoch")
    if have_git:
        wt = os.path.join(repo, ".savannah-worktrees", "shim-demo")
        check(rec["worktree"] == wt, "work_new worktree path")
        check(rec["branch"] == "work/shim-demo", "work_new branch")
        check(os.path.isdir(os.path.join(wt, ".git")) or
              os.path.isfile(os.path.join(wt, ".git")), "worktree exists")
    else:
        check(rec["worktree"] == repo, "work_new no-worktree cwd is repo")
        check(rec["branch"] == "", "work_new no-worktree branch empty")
    check(os.path.isfile(os.path.join(work_dir, "shim-demo", "record.json")),
          "record.json written")
    first = it2_calls()
    check(any(c[:2] == ["session", "split"] for c in first), "create splits the tab")
    up = runs_into(first, "PANE-C-1")
    check(up == [f" cd {rec['worktree']} && claude -n shim-demo 'hello there'"],
          f"upper pane: ' cd worktree && claude -n <name> <shlex-quoted prompt>' ({up})")
    check(runs_into(first, "PANE-S-1") == [f" cd {rec['worktree']}"], "lower pane cds into the worktree")

    # a second, flat session: the --no-worktree path is always covered
    r = shim.call_tool("work_new", {"name": "shim-flat", "repo": repo,
                                    "no_worktree": True})
    check(r["result"]["isError"] is False, f"work_new flat ok: {text_of(r)}")
    flat = json.loads(text_of(r))
    check(flat["worktree"] == repo and flat["branch"] == "",
          "flat session runs in the repo itself")
    check(flat["claude_pane"] == "PANE-C-2", "second tab gets fresh pane ids")

    # values starting with "-" are values, not options (--prompt=VALUE form)
    r = shim.call_tool("work_new", {"name": "shim-dash", "repo": repo,
                                    "no_worktree": True,
                                    "prompt": "-h is not help here",
                                    "note": "--note starting with dashes"})
    check(r["result"]["isError"] is False, f"work_new dash prompt ok: {text_of(r)}")
    dash = json.loads(text_of(r)) if not r["result"]["isError"] else {}
    check(dash.get("prompt") == "-h is not help here",
          "work_new dash-prefixed prompt reaches the record")
    r = shim.call_tool("work_notes", {"name": "shim-dash"})
    check("--note starting with dashes" in text_of(r),
          "work_new dash-prefixed note reaches notes")
    r = shim.call_tool("work_rm", {"name": "shim-dash", "keep_worktree": True})
    check(r["result"]["isError"] is False, "work_new dash session removed")

    # duplicate and reserved names surface as tool errors
    r = shim.call_tool("work_new", {"name": "shim-demo", "repo": repo,
                                    "no_worktree": True})
    check(r["result"]["isError"] is True, "work_new duplicate is error")
    check("already exists" in text_of(r), "work_new duplicate reason")
    r = shim.call_tool("work_new", {"name": "t-0001", "repo": repo,
                                    "no_worktree": True})
    check(r["result"]["isError"] is True, "work_new reserved name is error")

    # ---- work_list: offline (no hook session yet), then joined ----
    r = shim.call_tool("work_list", {})
    check(r["result"]["isError"] is False, "work_list ok")
    entries = {e["name"]: e for e in json.loads(text_of(r))}
    check(set(entries) == {"shim-demo", "shim-flat"}, "work_list names")
    demo = entries["shim-demo"]
    check(demo["state"] == "offline", "work_list offline without hook")
    check(demo["claude_session_id"] == "", "work_list no claude session id")
    check(demo["last_note"] == "first note", "work_list last_note from --note")
    check(demo["notes_path"].endswith(os.path.join("shim-demo", "notes.md")),
          "work_list notes_path")

    hook = {
        "session_id": "sess-abc",
        "project": rec["worktree"],
        "state": "waiting",
        "message": "needs an answer",
        "last_prompt": "fix the flaky test",
        "transcript_path": os.path.join(tmp, "t.jsonl"),
        "iterm_session_id": "w0t1p0:PANE-C-1",
        "updated": 1234567890,
    }
    with open(os.path.join(sessions_dir, "sess-abc.json"), "w") as f:
        json.dump(hook, f)
    r = shim.call_tool("work_list", {})
    demo = {e["name"]: e for e in json.loads(text_of(r))}["shim-demo"]
    check(demo["state"] == "waiting", "work_list joins hook state")
    check(demo["claude_session_id"] == "sess-abc", "work_list joins session id")
    check(demo["last_prompt"] == "fix the flaky test", "work_list last_prompt")
    check(demo["message"] == "needs an answer", "work_list message")
    check(demo["transcript_path"] == hook["transcript_path"],
          "work_list transcript_path")

    # ---- work_output: the reply channel, read from the hook's transcript ----
    r = shim.call_tool("work_output", {"name": "shim-demo"})
    check(r["result"]["isError"] is True and "no transcript" in text_of(r),
          f"work_output before the transcript exists is an error ({text_of(r)})")
    with open(hook["transcript_path"], "w") as f:
        for obj in (
            {"type": "user", "timestamp": "2026-10-30T01:00:00Z",
             "message": {"role": "user", "content": "fix the flaky test"}},
            {"type": "assistant", "timestamp": "2026-10-30T01:00:03Z",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Bash", "input": {"command": "ctest"}},
                 {"type": "text", "text": "Fixed it: the sleep was too short."}]}},
            {"type": "user", "timestamp": "2026-10-30T01:01:00Z",
             "message": {"role": "user", "content": "now the docs"}},
            {"type": "assistant", "timestamp": "2026-10-30T01:01:03Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "Docs updated."}]}},
        ):
            f.write(json.dumps(obj) + "\n")
    r = shim.call_tool("work_output", {"name": "shim-demo"})
    check(r["result"]["isError"] is False, f"work_output ok: {text_of(r)}")
    check("Docs updated." in text_of(r) and "Fixed it" not in text_of(r),
          "work_output default returns only the last turn")
    r = shim.call_tool("work_output", {"name": "shim-demo", "last": 2})
    check("Fixed it: the sleep was too short." in text_of(r) and "you: fix the flaky test" in text_of(r)
          and "ctest" not in text_of(r), f"work_output last=2 has both turns, no tool calls ({text_of(r)!r})")
    for bad in (0, -1, "2", 1.5, True):
        r = shim.call_tool("work_output", {"name": "shim-demo", "last": bad})
        check(r["result"]["isError"] is True, f"work_output last={bad!r} is an error")
    # ---- work_ask: the question goes to the tabs that are safe to interrupt ----
    # shim-demo's hook says idle: it is asked (the fake it2 logs the send);
    # with --timeout 1 and no answer the result is a timeout, exit 0.
    r = shim.call_tool("work_ask", {"names": ["shim-demo"], "question": "What are you doing?", "timeout": 1})
    check(r["result"]["isError"] is False and "skipped shim-demo: waiting for you" in text_of(r),
          f"work_ask skips a waiting tab and says so ({text_of(r)!r})")
    hook["state"] = "done"
    with open(os.path.join(sessions_dir, "sess-abc.json"), "w") as f:
        json.dump(hook, f)
    r = shim.call_tool("work_ask", {"names": ["shim-demo"], "question": "What are you doing?", "timeout": 1})
    check(r["result"]["isError"] is False and "## shim-demo" in text_of(r) and "no reply within 1" in text_of(r),
          f"work_ask asks a done tab, waits, reports the timeout ({text_of(r)!r})")
    for bad in ({"names": "shim-demo"}, {"question": ""}, {"timeout": 0}, {"timeout": "5"}):
        r = shim.call_tool("work_ask", bad)
        check(r["result"]["isError"] is True, f"work_ask {bad} is an error")
    r = shim.call_tool("work_output", {"name": "shim-flat"})
    check(r["result"]["isError"] is True, "work_output of a session with no transcript is an error")

    # ---- work_note / work_notes (multi-line survives) ----
    r = shim.call_tool("work_note", {"name": "shim-demo",
                                     "text": "second note\nwith a 2nd line"})
    check(r["result"]["isError"] is False, f"work_note ok: {text_of(r)}")
    r = shim.call_tool("work_notes", {"name": "shim-demo"})
    check(r["result"]["isError"] is False, "work_notes ok")
    notes = text_of(r)
    check("first note" in notes and "second note" in notes
          and "with a 2nd line" in notes, "work_notes has both notes")
    check(notes.count(" local\n") == 2, "work_notes has two stamped headers")
    r = shim.call_tool("work_note", {"name": "shim-demo", "text": "   "})
    check(r["result"]["isError"] is True, "work_note empty text is error")
    r = shim.call_tool("work_note", {"name": "shim-demo", "text": None})
    check(r["result"]["isError"] is True, "work_note null text is error")
    r = shim.call_tool("work_note", {"name": "shim-demo", "text": 42})
    check(r["result"]["isError"] is True, "work_note non-string text is error")
    r = shim.call_tool("work_notes", {"name": "shim-demo"})
    # Compare note bodies, not the whole text: the stamped headers carry the
    # local time, and "42" was in them at 11:42 (a real failure, 11/3/2026).
    bodies = [ln for ln in text_of(r).splitlines() if not ln.startswith("## ")]
    check("None" not in bodies and "42" not in bodies,
          f"work_note never str()'s a rejected value into the notes ({bodies})")
    r = shim.call_tool("work_notes", {"name": "shim-flat"})
    check(text_of(r) == "(no notes yet)", "work_notes empty placeholder")

    # ---- work_say / work_shell / work_jump type into the right panes ----
    before = len(it2_calls())
    r = shim.call_tool("work_say", {"name": "shim-demo", "text": "carry on"})
    check(r["result"]["isError"] is False, "work_say ok")
    check(sent_into(it2_calls()[before:], "PANE-C-1") == ["carry on"],
          "work_say types into the claude pane")

    before = len(it2_calls())
    r = shim.call_tool("work_shell", {"name": "shim-demo", "cmd": "git status"})
    check(r["result"]["isError"] is False, "work_shell ok")
    check(sent_into(it2_calls()[before:], "PANE-S-1") == ["git status"],
          "work_shell types into the shell pane")

    # text starting with "-" is typed verbatim, never parsed as an option
    # ("-h" used to return argparse help as a successful result)
    before = len(it2_calls())
    r = shim.call_tool("work_say", {"name": "shim-demo", "text": "-h"})
    check(r["result"]["isError"] is False, f"work_say -h ok: {text_of(r)}")
    check("usage:" not in text_of(r), "work_say -h is not argparse help")
    check(sent_into(it2_calls()[before:], "PANE-C-1") == ["-h"],
          "work_say types dash-prefixed text into the claude pane")
    before = len(it2_calls())
    r = shim.call_tool("work_say", {"name": "shim-demo",
                                    "text": "--force is the flag"})
    check(r["result"]["isError"] is False, "work_say --prefixed ok")
    check(sent_into(it2_calls()[before:], "PANE-C-1") == ["--force is the flag"],
          "work_say types --prefixed text verbatim")
    before = len(it2_calls())
    r = shim.call_tool("work_shell", {"name": "shim-demo", "cmd": "-la"})
    check(r["result"]["isError"] is False, f"work_shell -la ok: {text_of(r)}")
    check(sent_into(it2_calls()[before:], "PANE-S-1") == ["-la"],
          "work_shell types dash-prefixed cmd into the shell pane")

    # ---- work_open: focus a live tab (the reopen path is test_work's) ----
    before = len(it2_calls())
    r = shim.call_tool("work_open", {"name": "shim-demo"})
    check(r["result"]["isError"] is False, f"work_open ok: {text_of(r)}")
    opened = json.loads(text_of(r)) if not r["result"]["isError"] else {}
    check(opened.get("action") == "focused" and opened.get("claude_pane") == "PANE-C-1",
          f"work_open on a live tab reports focused ({opened})")
    check(["session", "focus", "PANE-C-1"] in it2_calls()[before:], "work_open focuses the claude pane")
    r = shim.call_tool("work_open", {"name": "shim-demo", "shell": True})
    check(r["result"]["isError"] is False and ["session", "focus", "PANE-S-1"] in it2_calls()[before:],
          "work_open shell=true focuses the shell pane")
    r = shim.call_tool("work_open", {"name": "no-such"})
    check(r["result"]["isError"] is True, "work_open of an unknown name is an error")

    # ---- work_adopt: a tab the human opened becomes a named session ----
    # (the module's own checks are test_work's; this is the tool's plumbing)
    adopt_hook = os.path.join(sessions_dir, "adopt-s.json")
    with open(adopt_hook, "w") as f:
        json.dump({"session_id": "adopt-s", "project": repo, "state": "idle",
                   "iterm_session_id": "w0t0p0:SHIM-PANE", "updated": 1}, f)
    before = len(it2_calls())
    r = shim.call_tool("work_adopt", {"name": "shim-adopt", "session_id": "adopt-s",
                                      "note": "taken in"})
    check(r["result"]["isError"] is False, f"work_adopt ok: {text_of(r)}")
    adopted = json.loads(text_of(r)) if not r["result"]["isError"] else {}
    check(adopted.get("adopted") is True and adopted.get("worktree") == repo
          and adopted.get("claude_pane") == "SHIM-PANE" and adopted.get("shell_pane") == "",
          f"work_adopt returns the adopted record ({adopted})")
    check(not any(c[:2] in (["tab", "new"], ["session", "split"]) for c in it2_calls()[before:]),
          "work_adopt opens no tab")
    r = shim.call_tool("work_notes", {"name": "shim-adopt"})
    check("taken in" in text_of(r), "work_adopt note recorded")
    r = shim.call_tool("work_adopt", {"name": "shim-adopt2"})
    check(r["result"]["isError"] is True and "session id" in text_of(r),
          f"work_adopt without session_id is a tool error ({text_of(r)})")
    r = shim.call_tool("work_rm", {"name": "shim-adopt"})
    check(r["result"]["isError"] is False and "directory kept" in text_of(r),
          f"work_rm of an adopted session keeps its directory ({text_of(r)})")
    check(os.path.isdir(repo), "work_rm left the adopted directory alone")
    os.remove(adopt_hook)

    # ---- work_status / work_move / work_rename: the board's new fields ----
    # (the module's own checks are test_work's; this is the tool's plumbing)
    r = shim.call_tool("work_status", {"name": "shim-demo", "text": "waiting for ctest (run 1 of 3)"})
    check(r["result"]["isError"] is False, f"work_status report ok: {text_of(r)}")
    st = json.loads(text_of(r)) if not r["result"]["isError"] else {}
    check(st.get("name") == "shim-demo" and st.get("count") == 1
          and st.get("text") == "waiting for ctest (run 1 of 3)" and isinstance(st.get("updated"), int),
          f"work_status returns the report with its count ({st})")
    r = shim.call_tool("work_status", {"name": "shim-demo"})
    st = json.loads(text_of(r)) if not r["result"]["isError"] else {}
    check((st.get("status") or {}).get("count") == 1, f"work_status without text reads it ({st})")
    r = shim.call_tool("work_status", {"name": "shim-demo", "text": "x", "clear": True})
    check(r["result"]["isError"] is True, "work_status with text and clear is a tool error")
    r = shim.call_tool("work_status", {"name": "shim-demo", "text": "   "})
    check(r["result"]["isError"] is True, "work_status with blank text is a tool error")
    r = shim.call_tool("work_status", {"name": "shim-demo", "text": 7})
    check(r["result"]["isError"] is True, "work_status with a non-string text is a tool error")
    r = shim.call_tool("work_list", {})
    row = {e["name"]: e for e in json.loads(text_of(r))}["shim-demo"]
    check((row.get("status") or {}).get("count") == 1 and row.get("priority") == "active"
          and row.get("rank") == row.get("created"), f"work_list carries status, priority, rank ({row})")
    r = shim.call_tool("work_status", {"name": "shim-demo", "clear": True})
    check(r["result"]["isError"] is False and json.loads(text_of(r)) == {"name": "shim-demo", "status": None},
          f"work_status clear ({text_of(r)})")
    r = shim.call_tool("work_move", {"name": "shim-demo", "priority": "later", "rank": -2})
    check(r["result"]["isError"] is False
          and json.loads(text_of(r)) == {"name": "shim-demo", "priority": "later", "rank": -2},
          f"work_move ({text_of(r)})")
    r = shim.call_tool("work_move", {"name": "shim-demo", "priority": "active"})
    check(r["result"]["isError"] is False and json.loads(text_of(r)) == {"name": "shim-demo", "priority": "active", "rank": -2},
          f"work_move priority alone keeps the rank ({text_of(r)})")
    r = shim.call_tool("work_move", {"name": "shim-demo", "priority": "now"})
    check(r["result"]["isError"] is False and json.loads(text_of(r)) == {"name": "shim-demo", "priority": "now", "rank": -2},
          f"work_move priority now ({text_of(r)})")
    r = shim.call_tool("work_move", {"name": "shim-demo", "priority": "active"})
    r = shim.call_tool("work_move", {"name": "shim-demo"})
    check(r["result"]["isError"] is True, "work_move without priority or rank is a tool error")
    r = shim.call_tool("work_move", {"name": "shim-demo", "priority": "urgent"})
    check(r["result"]["isError"] is True, "work_move with an unknown priority is a tool error")
    r = shim.call_tool("work_move", {"name": "shim-demo", "rank": "3"})
    check(r["result"]["isError"] is True, "work_move with a string rank is a tool error")
    r = shim.call_tool("work_rename", {"old": "shim-demo", "new": "shim-renamed"})
    check(r["result"]["isError"] is False and json.loads(text_of(r)).get("name") == "shim-renamed"
          and json.loads(text_of(r)).get("old") == "shim-demo", f"work_rename ({text_of(r)})")
    r = shim.call_tool("work_notes", {"name": "shim-renamed"})
    check("first note" in text_of(r), "the notes travelled with the rename")
    r = shim.call_tool("work_rename", {"old": "shim-renamed", "new": "shim-demo"})
    check(r["result"]["isError"] is False, "renamed back")
    r = shim.call_tool("work_rename", {"old": "shim-demo"})
    check(r["result"]["isError"] is True, "work_rename without new is a tool error")
    r = shim.call_tool("work_rename", {"old": "shim-demo", "new": "shim-flat"})
    check(r["result"]["isError"] is True and "exists" in text_of(r), "work_rename onto an existing name is a tool error")

    # ---- work_title: the free-text title beside the name ----
    r = shim.call_tool("work_title", {"name": "shim-demo", "text": "  Shim Demo Title  "})
    check(r["result"]["isError"] is False
          and json.loads(text_of(r)) == {"name": "shim-demo", "title": "Shim Demo Title"},
          f"work_title sets the stripped title ({text_of(r)})")
    r = shim.call_tool("work_title", {"name": "shim-demo"})
    check(json.loads(text_of(r)) == {"name": "shim-demo", "title": "Shim Demo Title"}, "work_title without text reads it")
    r = shim.call_tool("work_list", {})
    row = {e["name"]: e for e in json.loads(text_of(r))}["shim-demo"]
    check(row.get("title") == "Shim Demo Title", f"work_list carries the title ({row.get('title')!r})")
    r = shim.call_tool("work_title", {"name": "shim-demo", "text": "-starts with a dash"})
    check(json.loads(text_of(r)).get("title") == "-starts with a dash", "work_title text starting with - is text")
    r = shim.call_tool("work_title", {"name": "shim-demo", "text": "x", "clear": True})
    check(r["result"]["isError"] is True, "work_title with text and clear is a tool error")
    r = shim.call_tool("work_title", {"name": "shim-demo", "text": "   "})
    check(r["result"]["isError"] is True, "work_title with blank text is a tool error")
    r = shim.call_tool("work_title", {"name": "shim-demo", "text": 7})
    check(r["result"]["isError"] is True, "work_title with a non-string text is a tool error")
    r = shim.call_tool("work_title", {"name": "shim-demo", "clear": True})
    check(r["result"]["isError"] is False and json.loads(text_of(r)) == {"name": "shim-demo", "title": ""},
          f"work_title clear ({text_of(r)})")
    r = shim.call_tool("work_title", {"name": "nope"})
    check(r["result"]["isError"] is True and "no such work session" in text_of(r),
          "work_title of an unknown name is a tool error")
    r = shim.call_tool("work_new", {"name": "shim-titled", "repo": repo, "no_worktree": True,
                                    "title": "Titled At Birth"})
    check(r["result"]["isError"] is False and json.loads(text_of(r)).get("title") == "Titled At Birth",
          f"work_new title ({text_of(r)})")
    r = shim.call_tool("work_rm", {"name": "shim-titled"})
    check(r["result"]["isError"] is False, "work_new titled session removed")
    with open(os.path.join(sessions_dir, "adopt-t.json"), "w") as f:
        json.dump({"session_id": "adopt-t", "project": repo, "state": "idle", "iterm_session_id": ""}, f)
    r = shim.call_tool("work_adopt", {"name": "shim-adopt-t", "session_id": "adopt-t", "title": "Adopted With Title"})
    check(r["result"]["isError"] is False and json.loads(text_of(r)).get("title") == "Adopted With Title",
          f"work_adopt title ({text_of(r)})")
    r = shim.call_tool("work_rm", {"name": "shim-adopt-t"})
    check(r["result"]["isError"] is False, "adopted titled session removed")

    # ---- the shim writes savannah's log: src=shim, an INFO line per tool call ----
    try:
        with open(os.path.join(tmp, "log", "savannah.log"), encoding="utf-8", errors="replace") as f:
            slog = f.read().splitlines()
    except OSError:
        slog = []
    check(slog, "tool calls write savannah.log under SAVANNAH_LOG_DIR")
    check(any(' level=INFO msg=tool src=shim tool=work_title args="[clear name]"' in ln for ln in slog),
          f"a tool call is INFO with the tool and its argument keys ({[ln for ln in slog if 'tool=work_title' in ln][:1]})")
    check(any(" level=INFO msg=tool src=shim tool=work_say args=" in ln and ' prompt="carry on"' in ln for ln in slog),
          "the text a tool carries is in its INFO line, as prompt=")
    check(any(' level=DEBUG msg="tool done" src=shim tool=work_list ms=' in ln for ln in slog),
          "a finished tool call is DEBUG with its ms")
    check(any(' level=WARN msg="tool error" src=shim tool=work_title ' in ln and " error=" in ln for ln in slog),
          "a tool error is WARN with the message")
    check(any(" src=work " in ln for ln in slog), "the work module's own lines land in the same file (src=work)")

    # empty, whitespace, null or non-string text never presses Enter in a pane
    for tool, key in (("work_say", "text"), ("work_shell", "cmd")):
        for bad in ("", "   ", "\n", None, 7, ["x"], {"t": "x"}):
            before = len(it2_calls())
            r = shim.call_tool(tool, {"name": "shim-demo", key: bad})
            check(r["result"]["isError"] is True,
                  f"{tool} rejects {bad!r}: {text_of(r)}")
            check(it2_calls()[before:] == [],
                  f"{tool} with {bad!r} never reaches it2")
        before = len(it2_calls())
        r = shim.call_tool(tool, {"name": "shim-demo"})
        check(r["result"]["isError"] is True, f"{tool} missing {key} is error")
        check(it2_calls()[before:] == [], f"{tool} missing {key} no it2")

    before = len(it2_calls())
    r = shim.call_tool("work_jump", {"name": "shim-demo"})
    check(r["result"]["isError"] is False, "work_jump ok")
    check(it2_calls()[before:] == [["session", "focus", "PANE-C-1"], ["app", "activate"]],
          f"work_jump focuses the claude pane and brings iTerm2 forward ({it2_calls()[before:]})")
    before = len(it2_calls())
    r = shim.call_tool("work_jump", {"name": "shim-demo", "shell": True})
    check(r["result"]["isError"] is False, "work_jump shell ok")
    check(["session", "focus", "PANE-S-1"] in it2_calls()[before:], "work_jump --shell focuses shell")

    # ---- unknown name: a tool error, never a crash ----
    for tool, args in (("work_notes", {"name": "nope"}),
                       ("work_say", {"name": "nope", "text": "x"}),
                       ("work_shell", {"name": "nope", "cmd": "x"}),
                       ("work_jump", {"name": "nope"}),
                       ("work_note", {"name": "nope", "text": "x"}),
                       ("work_rm", {"name": "nope"})):
        r = shim.call_tool(tool, args)
        check(r["result"]["isError"] is True, f"{tool} unknown name is error")
        check("no such work session" in text_of(r), f"{tool} unknown reason")
    r = shim.request("ping")
    check(r["result"] == {}, "shim alive after unknown-name errors")

    # ---- a pane that vanished: say fails, rm still works (best effort) ----
    gone = Shim(peer, fake, dict(env, FAKE_IT2_MISSING="PANE-C-2"))
    r = gone.call_tool("work_say", {"name": "shim-flat", "text": "hi"})
    check(r["result"]["isError"] is True, "work_say missing pane is error")
    check("no longer exists" in text_of(r), "work_say missing pane reason")
    r = gone.call_tool("work_jump", {"name": "shim-flat"})
    check(r["result"]["isError"] is True, "work_jump missing pane is error")
    gone.close()

    # ---- work_rm: dirty worktree refused, then forced; notes archived ----
    if have_git:
        with open(os.path.join(rec["worktree"], "scratch.txt"), "w") as f:
            f.write("dirty\n")
        r = shim.call_tool("work_rm", {"name": "shim-demo"})
        check(r["result"]["isError"] is True, "work_rm dirty worktree refused")
        check("--force" in text_of(r), "work_rm dirty hint mentions --force")
        check(os.path.isfile(os.path.join(work_dir, "shim-demo", "record.json")),
              "work_rm refused leaves the record")
        check(os.path.isdir(rec["worktree"]), "work_rm refused leaves worktree")
        r = shim.call_tool("work_rm", {"name": "shim-demo", "force": True})
        check(r["result"]["isError"] is False, f"work_rm force ok: {text_of(r)}")
        check(not os.path.exists(rec["worktree"]), "work_rm removed worktree")
        branches = subprocess.run(
            ["git", "-C", repo, "branch", "--list", "work/shim-demo"],
            capture_output=True, text=True, env=GIT_ENV).stdout
        check("work/shim-demo" in branches, "work_rm leaves the branch alone")
    else:
        r = shim.call_tool("work_rm", {"name": "shim-demo"})
        check(r["result"]["isError"] is False, f"work_rm ok: {text_of(r)}")
    check("removed shim-demo" in text_of(r), "work_rm reports removal")
    check(not os.path.exists(os.path.join(work_dir, "shim-demo")),
          "work_rm deleted the record dir")
    archived = [fn for fn in os.listdir(os.path.join(work_dir, "archive"))
                if fn.startswith("shim-demo-") and fn.endswith(".md")]
    check(len(archived) == 1, "work_rm archived notes")
    if archived:
        with open(os.path.join(work_dir, "archive", archived[0])) as f:
            check("second note" in f.read(), "archived notes keep content")
    check(["session", "close", "--session", "PANE-C-1", "--force"] in it2_calls()
          and ["session", "close", "--session", "PANE-S-1", "--force"] in it2_calls(),
          "work_rm closes the tab pane by pane")

    r = shim.call_tool("work_rm", {"name": "shim-flat", "keep_worktree": True})
    check(r["result"]["isError"] is False, "work_rm flat ok")
    r = shim.call_tool("work_list", {})
    check(json.loads(text_of(r)) == [], "work_list empty after rm")

    shim.close()


def test_work_argv(peer):
    """Default work argv: this interpreter + the real work module path."""
    peer_dir = os.path.dirname(os.path.realpath(peer))
    code = (
        "import json, os, sys; sys.path.insert(0, sys.argv[1]); "
        "import peer; os.environ.pop('SAVANNAH_WORK_BIN', None); "
        "d = peer.work_argv(); os.environ['SAVANNAH_WORK_BIN'] = 'a b \\'c d\\''; "
        "print(json.dumps([d, peer.work_argv()]))")
    proc = subprocess.run([sys.executable, "-c", code, peer_dir],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"})
    check(proc.returncode == 0, f"work_argv probe runs: {proc.stderr}")
    if proc.returncode != 0:
        return
    default, override = json.loads(proc.stdout)
    check(default[0] == sys.executable,
          f"work_argv uses sys.executable, not 'python3': {default[0]}")
    expected = os.path.normpath(os.path.join(
        peer_dir, "..", "work", "savannah_work.py"))
    check(default[1] == expected, f"work_argv resolves realpath: {default[1]}")
    check(override == ["a", "b", "c d"], "SAVANNAH_WORK_BIN override is shlex-split")


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
                    "task_list", "task_new", "task_output", "task_rm",
                    "task_send", "task_status", "work_adopt", "work_ask", "work_jump",
                    "work_list", "work_move", "work_new", "work_note", "work_notes",
                    "work_open", "work_output", "work_rename", "work_rm", "work_say",
                    "work_shell", "work_status", "work_title"], "tool names")
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

    # ---- task_rm: ok, and error for a missing/running task ----
    r = shim.call_tool("task_rm", {"name": "dbj-devone", "id": "t-0001"})
    check("removed" in r["result"]["content"][0]["text"], "task_rm ok")
    r = shim.call_tool("task_rm", {"name": "dbj-devone", "id": "t-404"})
    check(r["result"]["isError"] is True, "task_rm missing is error")

    # ---- protocol errors ----
    r = shim.call_tool("no_such_tool", {})
    check("error" in r and r["error"]["code"] == -32602, "unknown tool")
    r = shim.request("no/such/method")
    check("error" in r and r["error"]["code"] == -32601, "unknown method")
    r = shim.request("ping")
    check(r["result"] == {}, "ping")

    shim.close()

    # ---- work_* tools against the real work module ----
    test_work_argv(peer)
    with tempfile.TemporaryDirectory(prefix="savannah-shim-") as tmp:
        test_work_tools(peer, fake, tmp)

    if failures == 0:
        print("test_shim: all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
