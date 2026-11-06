#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# fake_it2 - a stand-in for iTerm2's `it2` command, for work-session tests.
#
# work/savannah_work.py drives iTerm2 through $SAVANNAH_IT2; tests point that
# at this script so no real iTerm2 (or macOS) is involved. It speaks the
# subset of it2 1.0.0 the module uses, with the output lines pinned live on
# 11/3/2026 against iTerm2 3.7.6, and keeps a tiny world in
# $FAKE_IT2_LOG.state (JSON: counter, windows, sessions) so `session list`
# shows what `tab new` / `split` / `window new` created. Every call is
# appended to $FAKE_IT2_LOG as one JSON array (the argv after "it2").
#
#   window list --json            -> the windows ([] with FAKE_IT2_NO_WINDOW=1
#                                    until `window new` makes one)
#   window new                    -> "Created new window: W<n>" + a session in it
#   tab new --window W            -> "Created new tab: TAB-<n>", session PANE-C-<n>
#   session list --json           -> [{id, tab_id, window_id, name}] minus the
#                                    ids in $FAKE_IT2_MISSING
#   session split --session ID    -> "Created new pane: PANE-S-<n>", n being the
#                                    tab's number, so the nth tab is always
#                                    TAB-n / PANE-C-n / PANE-S-n
#   session set-name/run/focus/close, app activate
#                                 -> exit 0, like it2; "not found" exit 3 when
#                                    the id is in $FAKE_IT2_MISSING (comma list)
#   session get-status --session  -> the all-null JSON a plain shell pane has
#   tab close ...                 -> "Tab closed" exit 0 WITHOUT closing anything,
#                                    exactly the it2 1.0.0 bug; tests assert
#                                    the module never calls it
# $FAKE_IT2_FAIL="session split" (any argv prefix, space separated) makes that
# call exit 1 with "Error: boom", to exercise rollback after the tab exists.
# Stdlib only.

import json
import os
import sys


def state_path():
    log = os.environ.get("FAKE_IT2_LOG", "")
    return (log + ".state") if log else ""


def load_state():
    p = state_path()
    if p:
        try:
            with open(p) as f:
                st = json.load(f)
            if isinstance(st, dict):
                return st
        except (OSError, ValueError):
            pass
    windows = [] if os.environ.get("FAKE_IT2_NO_WINDOW") else ["W1"]
    return {"n": 0, "windows": windows, "sessions": {}}


def save_state(st):
    p = state_path()
    if p:
        with open(p, "w") as f:
            json.dump(st, f)


def missing():
    return {m for m in os.environ.get("FAKE_IT2_MISSING", "").split(",") if m}


def gone(sid):
    return sid in missing()


def opt(argv, name):
    """Value of --name (or -s for --session), or ""."""
    for i, a in enumerate(argv):
        if a == name or (name == "--session" and a == "-s"):
            return argv[i + 1] if i + 1 < len(argv) else ""
    return ""


def positional(argv):
    """The last positional: after "--" if present, else the last arg that is
    not an option or an option's value."""
    if "--" in argv:
        rest = argv[argv.index("--") + 1:]
        return " ".join(rest)
    skip = False
    out = []
    for a in argv:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            skip = a in ("--session", "-s", "--window", "-w", "--lines", "-n", "--profile", "-p")
            continue
        out.append(a)
    return out[-1] if out else ""


def main():
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_IT2_LOG", "")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(argv) + "\n")
    fail = os.environ.get("FAKE_IT2_FAIL", "")
    if fail and argv[:len(fail.split())] == fail.split():
        print("Error: boom", file=sys.stderr)
        return 1
    st = load_state()
    sub = " ".join(argv[:2])

    if sub == "window list":
        print(json.dumps([{"id": w, "tabs": 1} for w in st["windows"]]))
        return 0
    if sub == "window new":
        st["n"] += 1
        n = st["n"]
        w = f"W{n}"
        st["windows"].append(w)
        st["sessions"][f"PANE-C-{n}"] = {"tab_id": f"TAB-{n}", "window_id": w, "name": "login"}
        save_state(st)
        print(f"Created new window: {w}")
        return 0
    if sub == "tab new":
        w = opt(argv, "--window")
        if not w:
            print("Error: No current window", file=sys.stderr)
            return 3
        st["n"] += 1
        n = st["n"]
        st["sessions"][f"PANE-C-{n}"] = {"tab_id": f"TAB-{n}", "window_id": w, "name": "login"}
        save_state(st)
        print(f"Created new tab: TAB-{n}")
        return 0
    if sub == "tab close":
        print("Tab closed")  # the real it2 1.0.0 says this and closes nothing
        return 0
    if sub == "session list":
        out = [{"id": sid, "tab_id": s["tab_id"], "window_id": s["window_id"],
                "name": s.get("name", ""), "title": s.get("name", ""), "tty": "/dev/ttys9"}
               for sid, s in st["sessions"].items() if not gone(sid)]
        print(json.dumps(out))
        return 0
    if sub == "session split":
        sid = opt(argv, "--session")
        if gone(sid):
            print("Error: Session not found", file=sys.stderr)
            return 3
        # The split pane takes its tab's number: tab TAB-n holds PANE-C-n and
        # PANE-S-n, so a test can predict every id from the creation order.
        src = st["sessions"].get(sid)
        if src is None:
            st["n"] += 1
            src = {"tab_id": f"TAB-{st['n']}", "window_id": "W1"}
        n = src["tab_id"].rsplit("-", 1)[-1]
        new_id = f"PANE-S-{n}"
        while new_id in st["sessions"]:
            new_id += "b"  # a second split of the same tab (never done today)
        st["sessions"][new_id] = {"tab_id": src["tab_id"], "window_id": src["window_id"], "name": ""}
        save_state(st)
        print(f"Created new pane: {new_id}")
        return 0
    if sub == "session set-name":
        sid = opt(argv, "--session")
        if gone(sid):
            print(f"Error: Set name failed: Object with identifier \"{sid}\" not found", file=sys.stderr)
            return 1
        name = positional(argv)
        if sid in st["sessions"]:
            st["sessions"][sid]["name"] = name
            save_state(st)
        print(f"Session name set to: {name}")
        return 0
    if sub == "session run" or sub == "session send":
        if gone(opt(argv, "--session")):
            print("Error: Session not found", file=sys.stderr)
            return 3
        return 0
    if sub == "session focus":
        sid = positional(argv)
        if gone(sid):
            print(f"Error: Session '{sid}' not found", file=sys.stderr)
            return 3
        print(f"Focused session: {sid}")
        return 0
    if sub == "session close":
        sid = opt(argv, "--session")
        if gone(sid):
            print("Error: Session not found", file=sys.stderr)
            return 3
        st["sessions"].pop(sid, None)
        save_state(st)
        print("Session closed")
        return 0
    if sub == "session get-status":
        print(json.dumps({"status": None, "turn_open": None, "detail": None,
                          "text_color": None, "background_tasks": 0, "dot_color": None}))
        return 0
    if sub == "app activate":
        print("iTerm2 activated")
        return 0
    print(f"Error: fake it2 does not know {sub!r}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main())
