# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_sessions - the live Claude Code sessions as the hook records
# them, and the join with work records (savannah_work.py is the entry
# point; this is a flat sibling module).
#
# A hook file ($SAVANNAH_SESSIONS_DIR/<id>.json, written by
# hooks/session-status.py) BELONGS to a work session when
# realpath(hook.project) == realpath(record.worktree). The join happens at
# read time, here: load_hook_sessions, normalize_hook, hook_pane, hooks_in,
# join_session, claude_alive, hook_session_by_id, unclaimed_sessions.
# Nobody writes into the other side's file.
#
# Imports: the standard library, savannah_log and savannah_core.

import json
import os

from savannah_log import log_debug
from savannah_core import env_path, iterm_uuid


def sessions_dir():
    return env_path("SAVANNAH_SESSIONS_DIR", "savannah", "sessions")


def load_hook_sessions():
    d = sessions_dir()
    out = []
    skipped = 0
    try:
        names = os.listdir(d)
    except OSError:
        log_debug("hook sessions", dir=d, loaded=0, skipped=0, missing=True)
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn)) as f:
                s = json.load(f)
        except (OSError, ValueError):
            skipped += 1
            continue
        if isinstance(s, dict):
            out.append(normalize_hook(s))
        else:
            skipped += 1
    log_debug("hook sessions", dir=d, loaded=len(out), skipped=skipped)
    return out


def normalize_hook(h):
    """A hook file read the way today's hook writes it, like the board's
    normalizeSession: an older hook stamped the idle nudge (Notification
    idle_prompt) as waiting, and nothing rewrites that file until the tab's
    next event, so such a file reads as idle with no message. A real
    permission prompt or question (any other notify_type) stays waiting."""
    if h.get("state") == "waiting" and h.get("notify_type") == "idle_prompt":
        h["state"] = "idle"
        h["message"] = ""
    return h


def hook_pane(h):
    """The iTerm2 pane id a hook session recorded, "" when it has none (or
    the field is not a string)."""
    iid = h.get("iterm_session_id")
    return iterm_uuid(iid if isinstance(iid, str) else "")


def hooks_in(worktree, hooks):
    """The hook sessions running in this directory: realpath(project) is
    realpath(worktree), the join rule. [] for no worktree (realpath("") is
    the cwd, which would join by accident); a hook with no usable project
    never matches. The mirror image, the records on a directory, is
    session_here in savannah_core and work_session_for in hooks/session-status.py
    (a standalone copy on purpose: the hook imports nothing of ours)."""
    if not worktree:
        return []
    target = os.path.realpath(worktree)
    return [h for h in hooks
            if isinstance(h.get("project"), str) and h["project"]
            and os.path.realpath(h["project"]) == target]


def claude_alive(rec, live):
    """The record's Claude pane is one of the live pane ids."""
    return bool(rec.get("claude_pane")) and rec["claude_pane"] in live


def join_session(rec, hooks):
    worktree = rec.get("worktree") or ""
    if not worktree:
        return None
    matches = hooks_in(worktree, hooks)
    name = rec.get("name", "")
    if not matches:
        log_debug("join", name=name, worktree=worktree, matches=0)
        return None
    for h in matches:
        if rec.get("claude_pane") and hook_pane(h) == rec["claude_pane"]:
            log_debug("join", name=name, session=h.get("session_id") or "", state=h.get("state") or "",
                      by="pane", matches=len(matches))
            return h
    h = max(matches, key=lambda h: h.get("updated") or 0)
    log_debug("join", name=name, session=h.get("session_id") or "", state=h.get("state") or "",
              by="newest", matches=len(matches))
    return h


def hook_session_by_id(session_id):
    for h in load_hook_sessions():
        if h.get("session_id") == session_id:
            return h
    return None


def unclaimed_sessions(hooks, records):
    """Hook sessions no record claims (the board's plain tabs), in the order
    loaded; a session without a usable project is left out."""
    claimed = set()
    for rec in records:
        h = join_session(rec, hooks)
        if h is not None:
            claimed.add(id(h))
    return [h for h in hooks
            if id(h) not in claimed and isinstance(h.get("project"), str) and h.get("project")]
