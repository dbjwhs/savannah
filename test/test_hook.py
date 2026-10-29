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

failures = 0


def check(cond, label):
    global failures
    if not cond:
        print(f"FAIL: {label}")
        failures += 1


def main():
    script = sys.argv[1]
    workdir = tempfile.mkdtemp(prefix="savannah-hook-test-")
    env = dict(os.environ)
    env["SAVANNAH_SESSIONS_DIR"] = workdir

    def fire(event, extra_env=None):
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        p = subprocess.run([sys.executable, script], input=json.dumps(event),
                           text=True, capture_output=True, env=e)
        check(p.returncode == 0, f"{event.get('hook_event_name')} exits 0")
        return p

    def load(sid):
        with open(os.path.join(workdir, sid + ".json")) as f:
            return json.load(f)

    def exists(sid):
        return os.path.exists(os.path.join(workdir, sid + ".json"))

    # SessionStart -> idle, with terminal identity from the environment.
    fire({"session_id": "s1", "hook_event_name": "SessionStart",
          "cwd": "/home/u/proj", "permission_mode": "default",
          "transcript_path": "/home/u/.claude/projects/p/s1.jsonl"},
         {"ITERM_SESSION_ID": "w0t1p0:UUID-1", "TERM_PROGRAM": "iTerm.app"})
    r = load("s1")
    check(r["state"] == "idle", "SessionStart -> idle")
    check(r["project"] == "/home/u/proj", "cwd captured as project")
    check(r["iterm_session_id"] == "w0t1p0:UUID-1", "iterm id captured")
    check(r["mode"] == "default", "permission mode captured")
    check(isinstance(r["updated"], int) and r["updated"] > 0, "updated stamped")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1.jsonl",
          "transcript_path captured")
    check(r["last_prompt"] == "", "last_prompt empty before any prompt")

    # UserPromptSubmit -> working; last_prompt from the alternate
    # "user_prompt" field; transcript_path survives an event that omits it.
    fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
          "cwd": "/home/u/proj", "user_prompt": "do the thing"})
    r = load("s1")
    check(r["state"] == "working", "UserPromptSubmit -> working")
    check(r["last_prompt"] == "do the thing", "last_prompt from user_prompt")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1.jsonl",
          "transcript_path preserved when an event omits it")

    # Notification (permission) -> waiting, carries message + type.
    fire({"session_id": "s1", "hook_event_name": "Notification",
          "cwd": "/home/u/proj", "notification_type": "permission_prompt",
          "notification_message": "needs Bash"})
    r = load("s1")
    check(r["state"] == "waiting", "Notification -> waiting")
    check(r["message"] == "needs Bash", "notification_message captured")
    check(r["notify_type"] == "permission_prompt", "notification_type captured")
    check(r["last_prompt"] == "do the thing", "last_prompt preserved through Notification")

    # The documented "prompt" field wins; multi-line prompts keep the first
    # non-blank line only, cut to 200 chars.
    long_line = "x" * 300
    fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
          "cwd": "/home/u/proj",
          "prompt": "\n  " + long_line + "\nsecond line\n",
          "user_prompt": "ignored when prompt is present"})
    r = load("s1")
    check(r["last_prompt"] == "x" * 200, "last_prompt first line, 200 chars")
    check(len(r["last_prompt"]) == 200, "last_prompt length capped")
    check("second" not in r["last_prompt"], "last_prompt drops later lines")

    # A fresh transcript_path on a later event replaces the old one.
    fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
          "cwd": "/home/u/proj", "prompt": "fix the parser",
          "transcript_path": "/home/u/.claude/projects/p/s1b.jsonl"})
    r = load("s1")
    check(r["last_prompt"] == "fix the parser", "last_prompt from prompt")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1b.jsonl",
          "transcript_path updated when present")

    # An empty UserPromptSubmit does not wipe the previous prompt.
    fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
          "cwd": "/home/u/proj", "prompt": "   \n"})
    check(load("s1")["last_prompt"] == "fix the parser",
          "blank prompt keeps previous last_prompt")

    # The legacy 'message' field is still accepted if notification_message is absent.
    fire({"session_id": "s2", "hook_event_name": "Notification",
          "cwd": "/home/u/other", "message": "legacy text"})
    check(load("s2")["message"] == "legacy text", "legacy message field accepted")

    # Stop -> done, and clears the pending message/type.
    fire({"session_id": "s1", "hook_event_name": "Stop",
          "cwd": "/home/u/proj", "stop_reason": "end_turn"})
    r = load("s1")
    check(r["state"] == "done", "Stop -> done")
    check(r["message"] == "" and r["notify_type"] == "", "Stop clears message")
    check(r["last_prompt"] == "fix the parser", "last_prompt preserved through Stop")
    check(r["transcript_path"] == "/home/u/.claude/projects/p/s1b.jsonl",
          "transcript_path preserved through Stop")

    # A session that never saw a transcript_path or prompt still has both
    # fields, as empty strings, so consumers never need to guard the key.
    r = load("s2")
    check(r.get("transcript_path") == "", "transcript_path defaults to empty")
    check(r.get("last_prompt") == "", "last_prompt defaults to empty")

    # SessionEnd removes the file; a second SessionEnd is harmless.
    fire({"session_id": "s1", "hook_event_name": "SessionEnd",
          "session_end_reason": "logout"})
    check(not exists("s1"), "SessionEnd removes the file")
    fire({"session_id": "s1", "hook_event_name": "SessionEnd"})
    check(not exists("s1"), "second SessionEnd is harmless")

    # No session_id: do no harm, write nothing.
    before = set(os.listdir(workdir))
    fire({"hook_event_name": "Notification", "notification_message": "x"})
    check(set(os.listdir(workdir)) == before, "missing session_id writes nothing")

    # Empty stdin: exit clean, write nothing.
    p = subprocess.run([sys.executable, script], input="", text=True,
                       capture_output=True, env=env)
    check(p.returncode == 0, "empty stdin exits 0")

    # Valid JSON that is not an object (array, string, number) has no
    # session_id to key on: exit 0, write nothing, never raise.
    for raw in ("[1,2]", "\"just a string\"", "42", "null"):
        before = set(os.listdir(workdir))
        p = subprocess.run([sys.executable, script], input=raw, text=True,
                           capture_output=True, env=env)
        check(p.returncode == 0, f"non-object stdin {raw!r} exits 0")
        check(p.stderr == "", f"non-object stdin {raw!r} prints no traceback")
        check(set(os.listdir(workdir)) == before,
              f"non-object stdin {raw!r} writes nothing")

    # A non-string session_id is treated as missing, not used as a filename.
    before = set(os.listdir(workdir))
    fire({"session_id": 7, "hook_event_name": "UserPromptSubmit", "prompt": "x"})
    check(set(os.listdir(workdir)) == before, "non-string session_id writes nothing")

    # A pre-existing record that parses as JSON but is not an object (a
    # damaged or foreign file) is rebuilt as an object, not a crash.
    with open(os.path.join(workdir, "s3.json"), "w") as f:
        f.write("\"junk\"")
    p = fire({"session_id": "s3", "hook_event_name": "UserPromptSubmit",
              "cwd": "/home/u/three", "prompt": "rebuild me"})
    check(p.stderr == "", "junk record: no traceback")
    r = load("s3")
    check(isinstance(r, dict), "junk record rebuilt as an object")
    if not isinstance(r, dict):
        r = {}  # keep reporting clean FAILs below instead of a traceback
    check(r.get("session_id") == "s3", "junk record: session_id set")
    check(r.get("state") == "working", "junk record: UserPromptSubmit -> working")
    check(r.get("last_prompt") == "rebuild me", "junk record: last_prompt set")
    check(r.get("project") == "/home/u/three", "junk record: project set")

    # Same for an array-shaped record.
    with open(os.path.join(workdir, "s4.json"), "w") as f:
        f.write("[1, 2, 3]")
    fire({"session_id": "s4", "hook_event_name": "Stop", "cwd": "/home/u/four"})
    r = load("s4")
    check(isinstance(r, dict) and r.get("state") == "done",
          "array record rebuilt as an object with new state")

    # Unreadable record file (a directory where the file should be): the
    # hook still exits 0 rather than leaking an exception.
    os.makedirs(os.path.join(workdir, "s5.json"))
    p = fire({"session_id": "s5", "hook_event_name": "Stop", "cwd": "/home/u/five"})
    check(p.stderr == "", "directory in place of record: no traceback")

    if failures == 0:
        print("test_hook: all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
