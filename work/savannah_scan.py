# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_scan - the `scan` verb: find the running Claude Code tabs the
# board does not know and put them on it (savannah_work.py is the entry
# point; this is a flat sibling module).
#
# The board knows a tab through the status file savannah's hook writes on
# each Claude Code event (savannah_sessions). A tab with no file (the file
# was lost, or the tab started before the hook was installed) and no event
# since is on no row. Claude Code keeps a registry of its own, one file per
# running process:
#
#   $CLAUDE_CONFIG_DIR/sessions/<pid>.json   {pid, sessionId, cwd, kind,
#                                             status (idle | busy), updatedAt
#                                             (ms), procStart, name, ...}
#
# `scan` reads it, keeps the interactive sessions whose process is really
# running (the pid is alive and started when the file says), drops the ones
# that already have a status file, finds each one's iTerm2 pane by its tty
# (`ps` for the process, `it2 session list` for the panes) and writes the
# status file the hook would have written, marked "scanned". From then on
# the tab is a row like any other, and its next hook event takes the file
# over.
#
# It runs when the owner asks (this verb, the board's Ctrl-U), never on a
# timer: the registry is Claude Code's own file, not a documented interface
# (read live from Claude Code 2.1.325 on 11/15/2026), so the hook stays the
# source of truth and this is the repair. A registry that no longer reads
# this way finds nothing and says so; it never invents a session.
#
# Imports: the standard library, savannah_log, savannah_core,
# savannah_terminal, savannah_sessions and savannah_transcript.

import glob
import json
import os
import subprocess

from savannah_log import log_debug, log_info, log_warn
from savannah_core import display_path, env_path, print_table, UserError
from savannah_terminal import make_terminal
from savannah_sessions import hook_pane, load_hook_sessions, sessions_dir
from savannah_transcript import encode_project_dir

REGISTRY_SUBDIR = "sessions"
STATES = {"busy": "working", "idle": "idle"}   # Claude Code's status -> the hook's state


def registry_dir():
    return os.path.join(env_path("CLAUDE_CONFIG_DIR"), REGISTRY_SUBDIR)


def process_info(pid):
    """(tty, start) of a running process: its controlling terminal as
    /dev/<name> ("" when it has none) and its start time as ps prints it
    ("Sat Nov 14 06:27:00 2026" in UTC, the registry's procStart). None when no
    such process runs."""
    env = dict(os.environ, LC_ALL="C", TZ="UTC")   # the registry's procStart is ps's lstart in UTC
    try:
        p = subprocess.run(["ps", "-o", "tty=,lstart=", "-p", str(pid)], capture_output=True, text=True, env=env)
    except OSError:
        return None
    parts = p.stdout.split()
    if p.returncode != 0 or not parts:
        return None
    tty = "" if parts[0].startswith("?") else "/dev/" + parts[0]
    return tty, " ".join(parts[1:])


def read_registry(info=process_info):
    """The interactive Claude Code sessions running now, from Claude Code's
    registry: [{pid, session_id, project, name, state, updated, tty}].
    A file that does not parse, names no session, is not interactive, or
    whose process is gone (or is another process that took the pid: the
    start time differs) is skipped."""
    out = []
    for path in sorted(glob.glob(os.path.join(registry_dir(), "*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("kind", "interactive") != "interactive":
            continue
        pid, sid, cwd = d.get("pid"), d.get("sessionId"), d.get("cwd")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            continue
        if not (isinstance(sid, str) and sid and os.sep not in sid) or not isinstance(cwd, str) or not cwd:
            continue
        got = info(pid)
        if got is None:
            continue
        tty, start = got
        said = d.get("procStart")
        if isinstance(said, str) and said and start and " ".join(said.split()) != start:
            log_debug("scan: pid reused", pid=pid, file=path)
            continue
        updated = d.get("updatedAt")
        out.append({
            "pid": pid, "session_id": sid, "project": cwd,
            "name": d.get("name") if isinstance(d.get("name"), str) else "",
            "state": STATES.get(d.get("status"), "idle"),
            "updated": int(updated / 1000) if isinstance(updated, (int, float)) and not isinstance(updated, bool)
            else 0,
            "tty": tty,
        })
    return out


def unknown_sessions(running, hooks, panes):
    """The running sessions the board has no status file for, each with the
    id of its pane ("" when its tty is no pane of the terminal's). A session
    whose pane another status file already holds is known under that file
    (one tab, one row) and left alone."""
    known = {h.get("session_id") for h in hooks}
    held = {hook_pane(h) for h in hooks} - {""}
    by_tty = {s.get("tty"): s.get("id", "") for s in panes if s.get("tty")}
    out = []
    for r in running:
        if r["session_id"] in known:
            continue
        pane = by_tty.get(r["tty"], "") if r["tty"] else ""
        if pane and pane in held:
            continue
        out.append(dict(r, pane=pane))
    return out


def status_record(found):
    """The status file the hook would have written for that session, as far
    as it can be known from outside: no last prompt, and the transcript only
    when it is where Claude Code keeps it."""
    rec = {
        "session_id": found["session_id"], "project": found["project"], "state": found["state"],
        "updated": found["updated"], "pid": found["pid"], "mode": "", "message": "", "notify_type": "",
        "last_prompt": "", "transcript_path": "", "scanned": True,
    }
    if found["pane"]:
        rec["iterm_session_id"] = "w0t0p0:" + found["pane"]   # the hook's form; the pane is what is read
        rec["term"] = "iTerm.app"
    path = os.path.join(env_path("CLAUDE_CONFIG_DIR"), "projects", encode_project_dir(found["project"]),
                        found["session_id"] + ".jsonl")
    if os.path.isfile(path):
        rec["transcript_path"] = path
    return rec


def write_new(path, rec):
    """Create the status file; False when one appeared meanwhile (the hook
    fired: its file is the better one)."""
    try:
        with open(path, "x") as f:
            f.write(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    except FileExistsError:
        return False
    return True


def cmd_scan(a):
    """Put the running Claude Code tabs the board does not show on it."""
    running = read_registry()
    try:
        panes = make_terminal("").sessions()
    except UserError as e:
        panes = []
        log_warn("scan: no pane list", error=str(e))
    found = unknown_sessions(running, load_hook_sessions(), panes)
    directory = sessions_dir()
    added = []
    for f in found:
        if not a.dry_run:
            try:
                os.makedirs(directory, exist_ok=True)
                if not write_new(os.path.join(directory, f["session_id"] + ".json"), status_record(f)):
                    continue
            except OSError as e:
                raise UserError(f"cannot write {directory}: {e.strerror or e}")
        added.append({"session_id": f["session_id"], "pid": f["pid"], "project": f["project"],
                      "name": f["name"], "state": f["state"], "pane": f["pane"]})
    log_info("scan", running=len(running), found=len(added), dry_run=a.dry_run, registry=registry_dir())
    if a.json:
        print(json.dumps({"running": len(running), "found": added, "dry_run": a.dry_run}))
        return 0
    if not added:
        if running:
            print(f"scan: all {len(running)} running Claude Code session(s) are on the board")
        else:
            print(f"scan: no running Claude Code session found in {display_path(registry_dir())}")
        return 0
    verb = "would add" if a.dry_run else "added"
    print(f"scan: {verb} {len(added)} of {len(running)} running Claude Code session(s)")
    rows = [("NAME", "STATE", "PROJECT", "PANE", "SESSION")]
    for f in added:
        rows.append((f["name"] or "-", f["state"], display_path(f["project"]), f["pane"] or "(no pane found)",
                     f["session_id"]))
    print_table(rows)
    return 0
