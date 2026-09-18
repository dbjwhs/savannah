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
          "cwd": "/home/u/proj", "permission_mode": "default"},
         {"ITERM_SESSION_ID": "w0t1p0:UUID-1", "TERM_PROGRAM": "iTerm.app"})
    r = load("s1")
    check(r["state"] == "idle", "SessionStart -> idle")
    check(r["project"] == "/home/u/proj", "cwd captured as project")
    check(r["iterm_session_id"] == "w0t1p0:UUID-1", "iterm id captured")
    check(r["mode"] == "default", "permission mode captured")
    check(isinstance(r["updated"], int) and r["updated"] > 0, "updated stamped")

    # UserPromptSubmit -> working.
    fire({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
          "cwd": "/home/u/proj", "user_prompt": "do the thing"})
    check(load("s1")["state"] == "working", "UserPromptSubmit -> working")

    # Notification (permission) -> waiting, carries message + type.
    fire({"session_id": "s1", "hook_event_name": "Notification",
          "cwd": "/home/u/proj", "notification_type": "permission_prompt",
          "notification_message": "needs Bash"})
    r = load("s1")
    check(r["state"] == "waiting", "Notification -> waiting")
    check(r["message"] == "needs Bash", "notification_message captured")
    check(r["notify_type"] == "permission_prompt", "notification_type captured")

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

    if failures == 0:
        print("test_hook: all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
