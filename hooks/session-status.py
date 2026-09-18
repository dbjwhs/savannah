#!/usr/bin/env python3
# session-status.py - a Claude Code hook that records each interactive
# session's live state to a file, so savannah-dash can show every open tab
# on one board and flag the ones that need you.
#
# Wire it into ~/.claude/settings.json (see hooks/settings.snippet.json):
# one entry per event, all pointing here. Claude Code passes the event as
# JSON on stdin; we upsert
#   $SAVANNAH_SESSIONS_DIR/<session_id>.json   (default ~/.claude/savannah/sessions)
# and delete it on SessionEnd. Stdlib only, no third-party imports, so it
# runs anywhere Claude Code does.
#
# The state we record, and what fires it:
#   idle     SessionStart          fresh session, nothing pending
#   working  UserPromptSubmit      you asked; Claude is on it
#   waiting  Notification          NEEDS YOU: a permission prompt or an
#                                  idle-too-long nudge (this is the attention
#                                  signal the dash counts and highlights)
#   done     Stop                  finished responding; ready for your read
#   (file removed on SessionEnd)
#
# Terminal identity (ITERM_SESSION_ID, TMUX_PANE, ...) is captured from the
# hook's environment so the dash can jump you to the right tab. Everything is
# best-effort: a missing field or env var is simply omitted, never fatal, and
# the hook always exits 0 so it can never block or break a Claude session.

import json
import os
import sys
import time


# Event -> state. Events not listed here (tool hooks, etc.) refresh the
# timestamp without changing state, so an optional PostToolUse heartbeat can
# keep a busy session's age fresh without overriding a pending "waiting".
EVENT_STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "Notification": "waiting",
    "Stop": "done",
}


def sessions_dir():
    d = os.environ.get("SAVANNAH_SESSIONS_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), ".claude", "savannah", "sessions")


def terminal_identity():
    """Whatever we can learn about which terminal/tab this session is in."""
    ident = {}
    for key, field in (
        ("ITERM_SESSION_ID", "iterm_session_id"),
        ("TERM_PROGRAM", "term"),
        ("TMUX_PANE", "tmux_pane"),
        ("WEZTERM_PANE", "wezterm_pane"),
        ("KITTY_WINDOW_ID", "kitty_window_id"),
    ):
        v = os.environ.get(key)
        if v:
            ident[field] = v
    return ident


def atomic_write(path, obj):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)  # atomic on POSIX; a reader never sees a half file


def main():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        event = {}

    sid = event.get("session_id") or ""
    if not sid:
        return 0  # nothing we can key on; do no harm

    name = event.get("hook_event_name", "")
    directory = sessions_dir()
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return 0

    path = os.path.join(directory, sid + ".json")

    if name == "SessionEnd":
        try:
            os.remove(path)
        except OSError:
            pass
        return 0

    # Start from any existing record so a state-less event (a heartbeat)
    # preserves the message and identity already captured.
    record = {}
    try:
        with open(path) as f:
            record = json.load(f)
    except (OSError, ValueError):
        record = {}

    record["session_id"] = sid
    record["project"] = event.get("cwd") or os.getcwd()
    record["updated"] = int(time.time())
    record["pid"] = os.getppid()  # the Claude Code process, not this hook
    record["mode"] = event.get("permission_mode", record.get("mode", ""))
    record.update(terminal_identity())

    if name in EVENT_STATE:
        record["state"] = EVENT_STATE[name]
    record.setdefault("state", "working")

    # A notification carries why it needs you (notification_message is the
    # documented field; message is an older/alternate name we also accept).
    # notification_type distinguishes a permission prompt from an idle nudge,
    # so the dash can say which. Any non-notification event clears both.
    if name == "Notification":
        record["message"] = (event.get("notification_message")
                             or event.get("message") or "needs attention")
        record["notify_type"] = event.get("notification_type", "")
    else:
        record["message"] = ""
        record["notify_type"] = ""

    try:
        atomic_write(path, record)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
