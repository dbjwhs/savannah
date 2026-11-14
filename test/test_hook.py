#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Hook test: drives hooks/session-status.py the way Claude Code does (one
# event JSON per invocation on stdin) against a temp SAVANNAH_SESSIONS_DIR,
# and asserts the per-session status files it writes. Deterministic, no
# network, stdlib only. Usage:
#   test_hook.py <path/to/session-status.py>

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import check, finish  # noqa: E402


class HookHarness:
    """The hook script and the temp directories it reads and writes: the
    sessions dir (its status files), a work dir (the records SessionStart
    looks up) and savannah's log dir.

    The sections below run in order against these directories and are not
    independent: the log section reads what the earlier ones made the hook
    log."""

    def __init__(self, script):
        self.script = script
        self.workdir = tempfile.mkdtemp(prefix="savannah-hook-test-")
        self.env = dict(os.environ)
        self.env["SAVANNAH_SESSIONS_DIR"] = self.workdir
        # The work records the SessionStart context reads: a temp dir, so the
        # user's own sessions never match and nothing prints unless a test says so.
        self.work_root = tempfile.mkdtemp(prefix="savannah-hook-work-")
        self.env["SAVANNAH_WORK_DIR"] = self.work_root
        # savannah's one log: a temp dir, never the user's
        self.log_root = tempfile.mkdtemp(prefix="savannah-hook-log-")
        self.env["SAVANNAH_LOG_DIR"] = self.log_root
        self.wt = None    # a work session's worktree (test_session_start_context)

    def fire(self, event, extra_env=None):
        """Run the hook on one event, as Claude Code does; it must exit 0."""
        e = dict(self.env)
        if extra_env:
            e.update(extra_env)
        p = subprocess.run([sys.executable, self.script], input=json.dumps(event),
                           text=True, capture_output=True, env=e)
        check(p.returncode == 0, f"{event.get('hook_event_name')} exits 0")
        return p

    def load(self, sid):
        with open(os.path.join(self.workdir, sid + ".json")) as f:
            return json.load(f)

    def exists(self, sid):
        return os.path.exists(os.path.join(self.workdir, sid + ".json"))

    def put_record(self, name, rec):
        os.makedirs(os.path.join(self.work_root, name), exist_ok=True)
        with open(os.path.join(self.work_root, name, "record.json"), "w") as f:
            json.dump(rec, f)


def context_of(p):
    """The additionalContext a SessionStart printed, None when stdout is not
    exactly that one JSON object."""
    try:
        out = json.loads(p.stdout)
    except ValueError:
        return None
    if not isinstance(out, dict) or set(out) != {"hookSpecificOutput"}:
        return None
    hso = out["hookSpecificOutput"]
    if hso.get("hookEventName") != "SessionStart":
        return None
    return hso.get("additionalContext")


def test_session_states(h):
    """The status file through a session's life, SessionStart to SessionEnd."""
    # SessionStart -> idle, with terminal identity from the environment.
    h.fire({"session_id": "s1", "hook_event_name": "SessionStart",
            "cwd": "/home/u/proj", "permission_mode": "default",
            "transcript_path": "/home/u/.claude/projects/p/s1.jsonl"},
           {"ITERM_SESSION_ID": "w0t1p0:UUID-1", "TERM_PROGRAM": "iTerm.app"})
    r = h.load("s1")
    check(r["state"] == "idle", "SessionStart -> idle")
    check(r["project"] == "/home/u/proj", "cwd captured as project")
    check(r["iterm_session_id"] == "w0t1p0:UUID-1", "iterm id captured")
    check(r["mode"] == "default", "permission mode captured")
    check(isinstance(r["updated"], int) and r["updated"] > 0, "updated stamped")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1.jsonl",
          "transcript_path captured")
    check(r["last_prompt"] == "", "last_prompt empty before any prompt")

    # SAVANNAH_HOOK_IGNORE: savannah's own headless claude calls set it, and
    # the hook then writes nothing (no ghost tab on the board).
    h.fire({"session_id": "ghost", "hook_event_name": "SessionStart", "cwd": "/tmp/x"},
           {"SAVANNAH_HOOK_IGNORE": "1"})
    check(not h.exists("ghost"), "SAVANNAH_HOOK_IGNORE: no session file is written")

    # UserPromptSubmit -> working; last_prompt from the alternate
    # "user_prompt" field; transcript_path survives an event that omits it.
    h.fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
            "cwd": "/home/u/proj", "user_prompt": "do the thing"})
    r = h.load("s1")
    check(r["state"] == "working", "UserPromptSubmit -> working")
    check(r["last_prompt"] == "do the thing", "last_prompt from user_prompt")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1.jsonl",
          "transcript_path preserved when an event omits it")

    # Notification (permission) -> waiting, carries message + type.
    h.fire({"session_id": "s1", "hook_event_name": "Notification",
            "cwd": "/home/u/proj", "notification_type": "permission_prompt",
            "notification_message": "needs Bash"})
    r = h.load("s1")
    check(r["state"] == "waiting", "Notification -> waiting")
    check(r["message"] == "needs Bash", "notification_message captured")
    check(r["notify_type"] == "permission_prompt", "notification_type captured")
    check(r["last_prompt"] == "do the thing", "last_prompt preserved through Notification")

    # The documented "prompt" field wins; multi-line prompts keep the first
    # non-blank line only, cut to 200 chars.
    long_line = "x" * 300
    h.fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
            "cwd": "/home/u/proj",
            "prompt": "\n  " + long_line + "\nsecond line\n",
            "user_prompt": "ignored when prompt is present"})
    r = h.load("s1")
    check(r["last_prompt"] == "x" * 200, "last_prompt first line, 200 chars")
    check(len(r["last_prompt"]) == 200, "last_prompt length capped")
    check("second" not in r["last_prompt"], "last_prompt drops later lines")

    # A fresh transcript_path on a later event replaces the old one.
    h.fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
            "cwd": "/home/u/proj", "prompt": "fix the parser",
            "transcript_path": "/home/u/.claude/projects/p/s1b.jsonl"})
    r = h.load("s1")
    check(r["last_prompt"] == "fix the parser", "last_prompt from prompt")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1b.jsonl",
          "transcript_path updated when present")

    # An empty UserPromptSubmit does not wipe the previous prompt.
    h.fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
            "cwd": "/home/u/proj", "prompt": "   \n"})
    check(h.load("s1")["last_prompt"] == "fix the parser",
          "blank prompt keeps previous last_prompt")

    # The legacy 'message' field is still accepted if notification_message is absent.
    h.fire({"session_id": "s2", "hook_event_name": "Notification",
            "cwd": "/home/u/other", "message": "legacy text"})
    check(h.load("s2")["message"] == "legacy text", "legacy message field accepted")

    # Stop -> done, and clears the pending message/type.
    h.fire({"session_id": "s1", "hook_event_name": "Stop",
            "cwd": "/home/u/proj", "stop_reason": "end_turn"})
    r = h.load("s1")
    check(r["state"] == "done", "Stop -> done")
    check(r["message"] == "" and r["notify_type"] == "", "Stop clears message")
    check(r["last_prompt"] == "fix the parser", "last_prompt preserved through Stop")

    # The idle nudge (Claude Code's idle_prompt notification, a minute after
    # the last turn) is not a session that needs you: the state stays done,
    # no message is recorded, the timestamp is refreshed.
    before = r["updated"]
    h.fire({"session_id": "s1", "hook_event_name": "Notification", "cwd": "/home/u/proj",
            "notification_type": "idle_prompt", "notification_message": "Claude is waiting for your input"})
    r = h.load("s1")
    check(r["state"] == "done", f"idle_prompt keeps the state ({r['state']})")
    check(r["message"] == "" and r["notify_type"] == "idle_prompt", "idle_prompt records its type and no message")
    check(r["updated"] >= before, "idle_prompt refreshes the timestamp")
    h.fire({"session_id": "s9", "hook_event_name": "Notification", "cwd": "/home/u/proj",
            "notification_type": "idle_prompt"})
    check(h.load("s9")["state"] == "idle", "idle_prompt on a session with no file yet is idle")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1b.jsonl",
          "transcript_path preserved through Stop")
    # A nudge on top of a real permission prompt leaves the prompt's
    # message and type in place (it still needs you).
    h.fire({"session_id": "s8", "hook_event_name": "UserPromptSubmit", "cwd": "/home/u/proj", "prompt": "go"})
    h.fire({"session_id": "s8", "hook_event_name": "Notification", "cwd": "/home/u/proj",
            "notification_type": "permission_prompt", "notification_message": "use Bash"})
    h.fire({"session_id": "s8", "hook_event_name": "Notification", "cwd": "/home/u/proj",
            "notification_type": "idle_prompt", "notification_message": "Claude is waiting for your input"})
    r = h.load("s8")
    check(r["state"] == "waiting" and r["notify_type"] == "permission_prompt" and r["message"] == "use Bash",
          f"a nudge over a permission prompt keeps it waiting with its message ({r['state']}, {r['notify_type']!r})")
    # A file an older hook stamped waiting for the nudge heals on the next
    # nudge: idle, no message.
    with open(os.path.join(h.workdir, "s7.json"), "w") as f:
        json.dump({"session_id": "s7", "project": "/home/u/proj", "state": "waiting",
                   "notify_type": "idle_prompt", "message": "Claude is waiting for your input"}, f)
    h.fire({"session_id": "s7", "hook_event_name": "Notification", "cwd": "/home/u/proj",
            "notification_type": "idle_prompt", "notification_message": "Claude is waiting for your input"})
    r = h.load("s7")
    check(r["state"] == "idle" and r["message"] == "", f"an old waiting-for-the-nudge file heals to idle ({r['state']})")

    # A session that never saw a transcript_path or prompt still has both
    # fields, as empty strings, so consumers never need to guard the key.
    r = h.load("s2")
    check(r.get("transcript_path") == "", "transcript_path defaults to empty")
    check(r.get("last_prompt") == "", "last_prompt defaults to empty")

    # SessionEnd removes the file; a second SessionEnd is harmless.
    h.fire({"session_id": "s1", "hook_event_name": "SessionEnd",
            "session_end_reason": "logout"})
    check(not h.exists("s1"), "SessionEnd removes the file")
    h.fire({"session_id": "s1", "hook_event_name": "SessionEnd"})
    check(not h.exists("s1"), "second SessionEnd is harmless")


def test_unusable_input(h):
    """Events and records the hook cannot use: nothing written, never a traceback."""
    # No session_id: do no harm, write nothing.
    before = set(os.listdir(h.workdir))
    h.fire({"hook_event_name": "Notification", "notification_message": "x"})
    check(set(os.listdir(h.workdir)) == before, "missing session_id writes nothing")

    # Empty stdin: exit clean, write nothing.
    p = subprocess.run([sys.executable, h.script], input="", text=True,
                       capture_output=True, env=h.env)
    check(p.returncode == 0, "empty stdin exits 0")

    # Valid JSON that is not an object (array, string, number) has no
    # session_id to key on: exit 0, write nothing, never raise.
    for raw in ("[1,2]", "\"just a string\"", "42", "null"):
        before = set(os.listdir(h.workdir))
        p = subprocess.run([sys.executable, h.script], input=raw, text=True,
                           capture_output=True, env=h.env)
        check(p.returncode == 0, f"non-object stdin {raw!r} exits 0")
        check(p.stderr == "", f"non-object stdin {raw!r} prints no traceback")
        check(set(os.listdir(h.workdir)) == before,
              f"non-object stdin {raw!r} writes nothing")

    # A non-string session_id is treated as missing, not used as a filename.
    before = set(os.listdir(h.workdir))
    h.fire({"session_id": 7, "hook_event_name": "UserPromptSubmit", "prompt": "x"})
    check(set(os.listdir(h.workdir)) == before, "non-string session_id writes nothing")

    # A pre-existing record that parses as JSON but is not an object (a
    # damaged or foreign file) is rebuilt as an object, not a crash.
    with open(os.path.join(h.workdir, "s3.json"), "w") as f:
        f.write("\"junk\"")
    p = h.fire({"session_id": "s3", "hook_event_name": "UserPromptSubmit",
                "cwd": "/home/u/three", "prompt": "rebuild me"})
    check(p.stderr == "", "junk record: no traceback")
    r = h.load("s3")
    check(isinstance(r, dict), "junk record rebuilt as an object")
    if not isinstance(r, dict):
        r = {}  # keep reporting clean FAILs below instead of a traceback
    check(r.get("session_id") == "s3", "junk record: session_id set")
    check(r.get("state") == "working", "junk record: UserPromptSubmit -> working")
    check(r.get("last_prompt") == "rebuild me", "junk record: last_prompt set")
    check(r.get("project") == "/home/u/three", "junk record: project set")

    # Same for an array-shaped record.
    with open(os.path.join(h.workdir, "s4.json"), "w") as f:
        f.write("[1, 2, 3]")
    h.fire({"session_id": "s4", "hook_event_name": "Stop", "cwd": "/home/u/four"})
    r = h.load("s4")
    check(isinstance(r, dict) and r.get("state") == "done",
          "array record rebuilt as an object with new state")

    # Unreadable record file (a directory where the file should be): the
    # hook still exits 0 rather than leaking an exception.
    os.makedirs(os.path.join(h.workdir, "s5.json"))
    p = h.fire({"session_id": "s5", "hook_event_name": "Stop", "cwd": "/home/u/five"})
    check(p.stderr == "", "directory in place of record: no traceback")


def test_session_start_context(h):
    """SessionStart tells a work session's Claude how to report a poll."""
    # The hook finds the work record (work/) whose worktree is the event's cwd
    # and prints one JSON object with additionalContext; on any other event,
    # or with no matching record, stdout stays empty. The records are read as
    # files (the module is not imported), archive/ and junk are skipped.
    h.wt = tempfile.mkdtemp(prefix="savannah-hook-wt-")

    h.put_record("fix-parser", {"name": "fix-parser", "worktree": h.wt, "claude_pane": "PANE-A", "created": 10})
    h.put_record("elsewhere", {"name": "elsewhere", "worktree": "/home/u/elsewhere", "claude_pane": "", "created": 11})
    os.makedirs(os.path.join(h.work_root, "archive"))
    with open(os.path.join(h.work_root, "archive", "record.json"), "w") as f:
        json.dump({"name": "archive", "worktree": h.wt, "created": 999}, f)
    os.makedirs(os.path.join(h.work_root, "broken"))
    with open(os.path.join(h.work_root, "broken", "record.json"), "w") as f:
        f.write("[1, 2")
    os.makedirs(os.path.join(h.work_root, "alist"))
    with open(os.path.join(h.work_root, "alist", "record.json"), "w") as f:
        f.write("[1, 2]")
    os.makedirs(os.path.join(h.work_root, "empty"))
    p = h.fire({"session_id": "ws1", "hook_event_name": "SessionStart", "cwd": h.wt, "permission_mode": "default"},
               {"ITERM_SESSION_ID": "w0t1p0:PANE-A"})
    check(p.stderr == "", f"SessionStart in a work session: no stderr ({p.stderr!r})")
    ctx = context_of(p)
    check(isinstance(ctx, str) and p.stdout.count("\n") == 1,
          f"SessionStart prints exactly one JSON object with additionalContext ({p.stdout!r})")
    ctx = ctx or ""
    check(ctx.startswith('This directory is savannah work session "fix-parser", shown on the savannah board. '),
          f"additionalContext names the session ({ctx})")
    check(' work status fix-parser -- "waiting for ctest (run 2 of 3)" . Each run counts as one poll; ' in ctx
          and ctx.endswith(" work status fix-parser --clear removes it."),
          f"additionalContext carries the status command and the clear ({ctx})")
    check(" with: /" in ctx and ("/build/savannah work status" in ctx or "/work/savannah_work.py work status" in ctx),
          f"the command is the built CLI or the module under python3, by absolute path ({ctx})")
    check("\u2014" not in ctx, "no em dash in the context")
    r = h.load("ws1")
    check(r["state"] == "idle" and r["project"] == h.wt, "the status file is still written on SessionStart")
    # another directory: nothing printed, file still written
    p = h.fire({"session_id": "ws2", "hook_event_name": "SessionStart", "cwd": "/home/u/proj"},
               {"ITERM_SESSION_ID": "w0t1p0:PANE-A"})
    check(p.stdout == "" and p.stderr == "", f"SessionStart outside every work session prints nothing ({p.stdout!r})")
    check(h.exists("ws2"), "the status file is written when nothing prints")
    # other events in that directory print nothing
    for ev in ("UserPromptSubmit", "Notification", "Stop", "PostToolUse"):
        p = h.fire({"session_id": "ws1", "hook_event_name": ev, "cwd": h.wt, "prompt": "x",
                    "notification_message": "m"}, {"ITERM_SESSION_ID": "w0t1p0:PANE-A"})
        check(p.stdout == "" and p.stderr == "", f"{ev} prints nothing ({p.stdout!r})")
    # two records on one worktree: the pane decides, else the newest
    h.put_record("twin", {"name": "twin", "worktree": h.wt, "claude_pane": "PANE-B", "created": 99})
    p = h.fire({"session_id": "ws3", "hook_event_name": "SessionStart", "cwd": h.wt},
               {"ITERM_SESSION_ID": "w0t2p0:PANE-B"})
    check('work session "twin"' in (context_of(p) or ""), f"two records on one directory: the pane match wins ({p.stdout!r})")
    p = h.fire({"session_id": "ws4", "hook_event_name": "SessionStart", "cwd": h.wt},
               {"ITERM_SESSION_ID": "w0t2p0:PANE-A"})
    check('work session "fix-parser"' in (context_of(p) or ""), "the other pane picks the other record")
    p = h.fire({"session_id": "ws5", "hook_event_name": "SessionStart", "cwd": h.wt},
               {"ITERM_SESSION_ID": "w0t2p0:NEITHER"})
    check('work session "twin"' in (context_of(p) or ""), "no pane match: the newest record wins")
    p = h.fire({"session_id": "ws6", "hook_event_name": "SessionStart", "cwd": h.wt}, {"ITERM_SESSION_ID": ""})
    check('work session "twin"' in (context_of(p) or ""), "no ITERM_SESSION_ID at all: still the newest record")
    # the cwd spelled through a symlink still matches (realpath on both sides)
    link = os.path.join(tempfile.mkdtemp(prefix="savannah-hook-link-"), "wt")
    os.symlink(h.wt, link)
    p = h.fire({"session_id": "ws7", "hook_event_name": "SessionStart", "cwd": link},
               {"ITERM_SESSION_ID": "w0t2p0:PANE-A"})
    check('work session "fix-parser"' in (context_of(p) or ""), "a symlinked cwd matches by real path")
    # a work dir that does not exist: nothing printed, nothing broken
    p = h.fire({"session_id": "ws8", "hook_event_name": "SessionStart", "cwd": h.wt},
               {"SAVANNAH_WORK_DIR": os.path.join(h.work_root, "missing")})
    check(p.stdout == "" and p.stderr == "" and h.exists("ws8"), "a missing work dir: nothing printed, file written")
    # a cwd that is not a string never breaks the hook
    p = h.fire({"session_id": "ws9", "hook_event_name": "SessionStart", "cwd": 42})
    check(p.stderr == "" and h.exists("ws9"), "a non-string cwd: exit 0, file written")


def test_log(h):
    """The hook writes savannah's log (src=hook) and never a word anywhere else."""
    log_file = os.path.join(h.log_root, "savannah.log")
    check(os.path.isfile(log_file), f"events are logged to {log_file} (SAVANNAH_LOG_DIR)")
    try:
        with open(log_file, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        lines = []
    check(lines and all(" src=hook " in ln for ln in lines), "every line is stamped src=hook")
    check(all(ln.startswith("time=") and " level=" in ln for ln in lines), "every line carries a time and a level")
    check(any(" level=DEBUG msg=event src=hook event=UserPromptSubmit session=" in ln
              and " state=working " in ln and " file=" in ln and " written=True" in ln for ln in lines),
          "each event is a DEBUG line: event, session, state, cwd, file")
    check(any(" level=DEBUG msg=event src=hook event=SessionEnd session=" in ln and " removed=True" in ln
              for ln in lines), "SessionEnd is a DEBUG line with removed=True")
    check(any(' level=INFO msg="work context" src=hook session=' in ln and " name=fix-parser cmd=" in ln
              for ln in lines), "the SessionStart context is INFO with the work session's name and the command")
    check(not any(" level=INFO " in ln and 'msg="work context"' not in ln for ln in lines),
          "nothing but the context is INFO")
    # a log directory that cannot be made: exit 0, file written, nothing printed
    blocker = os.path.join(h.log_root, "blocker")
    open(blocker, "w").close()
    unwritable = {"SAVANNAH_LOG_DIR": os.path.join(blocker, "log")}
    p = h.fire({"session_id": "lg1", "hook_event_name": "Stop", "cwd": "/home/u/proj"}, unwritable)
    check(p.stdout == "" and p.stderr == "" and h.exists("lg1") and h.load("lg1")["state"] == "done",
          f"an unwritable SAVANNAH_LOG_DIR: exit 0, file written, nothing printed ({p.stderr!r})")
    p = h.fire({"session_id": "lg2", "hook_event_name": "SessionStart", "cwd": h.wt},
               dict(unwritable, ITERM_SESSION_ID="w0t2p0:PANE-A"))
    check(p.stderr == "" and 'work session "fix-parser"' in (context_of(p) or ""),
          "the context still prints when the log cannot be written")



# The sections, in the order they must run (see HookHarness).
SECTIONS = (
    test_session_states,
    test_unusable_input,
    test_session_start_context,
    test_log,
)


def main():
    h = HookHarness(sys.argv[1])
    for section in SECTIONS:
        section(h)
    return finish("test_hook")


if __name__ == "__main__":
    sys.exit(main())
