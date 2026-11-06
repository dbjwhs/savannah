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
# runs anywhere Claude Code does; the one import of our own, the shared log
# module beside this checkout's work/, is optional (see below).
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
# hook's environment so the dash can jump you to the right tab. Two more
# fields help you remember what a tab was doing: transcript_path (where Claude
# Code keeps the conversation) and last_prompt (the first line of what you
# last typed, capped at 200 chars). Both are kept across later events that do
# not carry them. The hook does no joining of its own: work sessions (work/)
# match a hook file to their worktree at read time by comparing "project".
# One thing besides the file: on SessionStart, when the cwd is a work
# session's worktree, the hook prints one JSON object with additionalContext
# (Claude Code adds it to that session's context) that tells the Claude in
# the tab which session it is and the `work status` command that reports a
# poll (a test run, CI, a deploy) to the board. Nothing on any other event,
# nothing when no record matches, nothing else ever on stdout.
# Everything is best-effort: a missing field or env var is simply omitted,
# never fatal, and the hook always exits 0 so it can never block or break a
# Claude session.
# The hook also writes savannah's one log (work/savannah_log.py, src=hook):
# a DEBUG line per event and an INFO line when a work session's Claude is
# handed its context; `work log --level debug` reads them. Best effort
# too: a hook copied out of the checkout, or a log directory that cannot be
# written, logs nothing, and nothing ever reaches stdout or stderr for it.

import json
import os
import sys
import time

try:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "work"))
    import savannah_log
    savannah_log.configure("hook")
    log_debug, log_info, log_warn = savannah_log.log_debug, savannah_log.log_info, savannah_log.log_warn
except Exception:  # noqa: BLE001 - no log is fine, a failed hook is not
    def log_debug(_msg, **_kv):
        pass

    def log_info(_msg, **_kv):
        pass

    def log_warn(_msg, **_kv):
        pass


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


def first_line(text, limit):
    """First non-blank line of text, stripped, cut to limit characters."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def atomic_write(path, obj):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)  # atomic on POSIX; a reader never sees a half file


# What a work session's Claude is told at SessionStart. Statements, not
# instructions: Claude Code treats imperative hook context with suspicion.
WORK_CONTEXT = (
    'This directory is savannah work session "{name}", shown on the savannah '
    'board. Progress on a long wait or a poll for the owner (tests, CI, a '
    'deploy, a slow build) can be reported to that board with: {cmd} work '
    'status {name} -- "waiting for ctest (run 2 of 3)" . Each run counts as '
    'one poll; the board shows the text, the poll count and how long ago it '
    'was reported. The report stops showing as live when the turn ends, and '
    '{cmd} work status {name} --clear removes it.'
)


def work_dir():
    d = os.environ.get("SAVANNAH_WORK_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), ".claude", "savannah", "work")


def savannah_command():
    """How to run savannah from this checkout (the hook lives in <repo>/hooks/):
    the built CLI when it exists, else the work module under python3."""
    repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    cli = os.path.join(repo, "build", "savannah")
    if os.path.isfile(cli):
        return cli
    return "python3 " + os.path.join(repo, "work", "savannah_work.py")


def work_session_for(cwd, iterm_session_id):
    """Name of the work session (work/) whose worktree is cwd, or "". Reads
    the record files directly (the work module is not imported; this hook
    stays one standalone file), skipping archive/ and anything unreadable.
    Several records on one directory: the one whose claude_pane is this
    iTerm2 session (the uuid after the colon in ITERM_SESSION_ID) wins, else
    the newest."""
    if not isinstance(cwd, str) or not cwd:
        return ""
    root = work_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return ""
    here = os.path.realpath(cwd)
    sid = iterm_session_id or ""
    pane = sid.split(":", 1)[1] if ":" in sid else sid
    matches = []
    for name in names:
        if name == "archive":
            continue
        try:
            with open(os.path.join(root, name, "record.json")) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        worktree = rec.get("worktree") if isinstance(rec, dict) else None
        if not isinstance(worktree, str) or not worktree:
            continue
        if os.path.realpath(worktree) != here:
            continue
        if pane and rec.get("claude_pane") == pane:
            return name
        try:
            created = int(rec.get("created") or 0)
        except (TypeError, ValueError, OverflowError):
            created = 0
        matches.append((created, name))
    if not matches:
        return ""
    return max(matches)[1]


def run():
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        event = {}
    # Valid JSON that is not an object (an array, a string, a number) carries
    # no fields we can read; treat it like an empty event.
    if not isinstance(event, dict):
        event = {}

    sid = event.get("session_id") or ""
    if not isinstance(sid, str):
        sid = ""
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
        log_debug("event", event=name, session=sid, cwd=event.get("cwd") or "", file=path, removed=True)
        return 0

    # Start from any existing record so a state-less event (a heartbeat)
    # preserves the message and identity already captured.
    record = {}
    try:
        with open(path) as f:
            record = json.load(f)
    except (OSError, ValueError):
        record = {}
    # A damaged record that parses but is not an object is rebuilt from
    # scratch rather than crashing on the first key assignment.
    if not isinstance(record, dict):
        record = {}

    record["session_id"] = sid
    record["project"] = event.get("cwd") or os.getcwd()
    record["updated"] = int(time.time())
    record["pid"] = os.getppid()  # the Claude Code process, not this hook
    record["mode"] = event.get("permission_mode", record.get("mode", ""))
    record.update(terminal_identity())

    # Where Claude Code keeps this conversation's transcript. Most events
    # carry it; when one does not, the previous value stands.
    tp = event.get("transcript_path")
    if isinstance(tp, str) and tp:
        record["transcript_path"] = tp
    record.setdefault("transcript_path", "")

    # What you last asked for, so a tab still tells you what it was doing.
    # "prompt" is the documented field, "user_prompt" an alternate name. First
    # line only, capped at 200 chars, and kept through every later event.
    if name == "UserPromptSubmit":
        prompt = event.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            prompt = event.get("user_prompt")
        if isinstance(prompt, str) and prompt.strip():
            record["last_prompt"] = first_line(prompt, 200)
    record.setdefault("last_prompt", "")

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

    written = True
    try:
        atomic_write(path, record)
    except OSError as e:
        written = False
        log_warn("session file not written", session=sid, file=path, error=e.strerror or str(e))
    log_debug("event", event=name or "", session=sid, state=record.get("state", ""), cwd=record["project"],
              iterm=record.get("iterm_session_id", ""), file=path, written=written)

    # A work session's Claude learns which session it is and how to report
    # a poll to the board. The only thing this hook ever prints to stdout,
    # and only on SessionStart: Claude Code adds additionalContext to the
    # session (stdout that is not JSON would have to be plain text).
    if name == "SessionStart":
        work_name = work_session_for(record["project"], os.environ.get("ITERM_SESSION_ID", ""))
        if work_name:
            context = WORK_CONTEXT.format(name=work_name, cmd=savannah_command())
            log_info("work context", session=sid, name=work_name, cmd=savannah_command())
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": context}}))
    return 0


def main():
    # Best effort, always. A hook that exits nonzero can block a Claude
    # session, so whatever goes wrong in run() (an unexpected payload shape,
    # a filesystem surprise) is swallowed and the session carries on.
    try:
        return run()
    except Exception:  # noqa: BLE001 - the whole point is to never fail
        return 0


if __name__ == "__main__":
    sys.exit(main())
