# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_ask - the `ask` verb: one question typed into every tab that is
# safe to interrupt, the replies collected (savannah_work.py is the entry
# point; this is a flat sibling module).
#
# Targets are the tabs whose hook state is idle or done (never working or
# waiting, never the asking session); the wait is on each asked tab's hook
# file, the reply is read from its transcript, and a work session's reply
# is kept as its summary and in its notes.
#
# Imports: the standard library, savannah_log, savannah_core,
# savannah_terminal, savannah_sessions and savannah_transcript.

import json
import os
import subprocess
import time

from savannah_log import log_info, log_verbose, log_warn
from savannah_core import (append_note, display_path, first_line, list_records, load_record, save_record,
                           usage_error, UserError)
from savannah_terminal import make_terminal
from savannah_sessions import hook_pane, join_session, load_hook_sessions, unclaimed_sessions
from savannah_transcript import transcript_for, transcript_turns

LAST_PROMPT_MAX = 200
ASK_DEFAULT = ("In three lines, for a board of all my sessions: what are you working on, "
               "where are you with it, and what comes next? If a pull request exists for "
               "this work, include its URL.")
ASK_TIMEOUT = 120
ASK_POLL = 0.5


def self_pids():
    """This process's ancestors (a few levels), so a Claude session running
    `savannah work ask --all` from its own shell never asks itself: the
    question typed into its own pane would be a prompt to it. Each hook
    file carries the Claude Code pid."""
    pids = set()
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        pids.add(pid)
        try:
            out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
            pid = int(out)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            break
    return pids


def ask_skip_reason(h, mine):
    """Why a live tab is not asked now, or "" when it is safe to: a working
    tab is not interrupted, a waiting tab has a permission prompt or a
    question up that typed text would answer, the asking session is not
    asked, a tab with no pane cannot be typed into."""
    state = h.get("state") or ""
    if state == "working":
        return "working (not interrupted)"
    if state == "waiting":
        return "waiting for you (text typed there would answer its prompt)"
    if h.get("pid") in mine:
        return "the session asking"
    if not hook_pane(h):
        return "no iTerm2 pane recorded"
    return ""


def ask_targets(names, all_tabs, hooks, records):
    """The tabs to ask: the named work sessions, or with all_tabs every live
    Claude Code tab (work sessions by their records, plain tabs by their
    hook files). -> (targets, skipped); a target is {session_id, name,
    project, pane, h, rec}."""
    mine = self_pids()
    targets, skipped, seen = [], [], set()

    def consider(h, rec):
        sid = h.get("session_id") or ""
        if sid in seen:
            return
        seen.add(sid)
        entry = {"session_id": sid, "name": (rec or {}).get("name", ""), "project": h.get("project") or ""}
        why = ask_skip_reason(h, mine)
        if why:
            skipped.append(dict(entry, why=why))
            return
        pane = (rec or {}).get("claude_pane") or hook_pane(h)
        targets.append(dict(entry, pane=pane, h=h, rec=rec))

    for name in names:
        rec = load_record(name)
        h = join_session(rec, hooks)
        if h is None:
            skipped.append({"session_id": "", "name": name, "project": rec.get("worktree") or "",
                            "why": "no live Claude session (offline)"})
            continue
        consider(h, rec)
    if all_tabs:
        for rec in records:
            h = join_session(rec, hooks)
            if h is not None:
                consider(h, rec)
        for h in unclaimed_sessions(hooks, records):
            consider(h, None)
    return targets, skipped


def ask_answered(h, question):
    """A hook file shows the question answered: Claude Code recorded our
    question as the tab's last prompt and has stopped since."""
    return (h or {}).get("state") == "done" and (h or {}).get("last_prompt") == first_prompt_line(question)


def first_prompt_line(text):
    """The hook's last_prompt of a typed text: its first non-blank line,
    LAST_PROMPT_MAX chars (hooks/session-status.py first_line)."""
    return first_line(text, LAST_PROMPT_MAX)


def ask_reply(h, question):
    """The tab's reply to the question from its transcript: the text of the
    last turn whose prompt is the question, "" when the transcript or that
    turn is not there yet."""
    path = transcript_for(h.get("session_id") or "", h.get("transcript_path") or "", h.get("project") or "")
    if not path:
        return ""
    try:
        turns = transcript_turns(path)
    except UserError:
        return ""
    head = first_prompt_line(question)
    for t in reversed(turns):
        if first_prompt_line(t.get("prompt") or "") == head:
            return (t.get("text") or "").strip()
    return ""


def ask_send(term, targets, question):
    """Type the question into each target's Claude pane. -> (asked, failed):
    the targets it reached, each stamped with when (sent), and an entry for
    each whose pane is gone."""
    asked, failed = [], []
    for t in targets:
        if term.write(t["pane"], question):
            t["sent"] = time.time()
            asked.append(t)
            log_info("asked", name=t["name"], session=t["session_id"], pane=t["pane"], chars=len(question))
        else:
            log_warn("pane gone", name=t["name"], pane="claude", session=t["pane"])
            failed.append({"session_id": t["session_id"], "name": t["name"], "project": t["project"],
                           "error": "claude pane no longer exists"})
    return asked, failed


def ask_wait(asked, question, timeout):
    """Poll the hook files until every asked tab shows the question answered
    (ask_answered) or timeout seconds have passed. An answered target gets
    its fresh hook (h) and when (answered). -> seconds waited since the
    first question was typed."""
    deadline = time.time() + timeout
    pending = {t["session_id"]: t for t in asked}
    while pending and time.time() < deadline:
        time.sleep(ASK_POLL)
        for h in load_hook_sessions():
            sid = h.get("session_id")
            if sid in pending and ask_answered(h, question):
                pending[sid]["h"] = h
                pending[sid]["answered"] = time.time()
                del pending[sid]
    return round(time.time() - (asked[0]["sent"]), 1)


def keep_reply(rec, reply, question):
    """A work session's reply is its own account: it goes onto the record as
    the latest summary and into its notes (the question after it, so the
    NOTE column shows the answer's first line). The record is read again
    first: the wait was long enough for a move, a title or a status to land
    on it, and that must not be undone."""
    try:
        rec = load_record(rec["name"])
    except UserError:
        pass
    rec["summary"] = reply
    rec["summary_at"] = int(time.time())
    save_record(rec)
    append_note(rec["name"], reply + "\n(asked: " + first_prompt_line(question) + ")")
    log_info("reply noted", name=rec["name"], chars=len(reply))


def ask_collect(asked, question, no_wait):
    """Each asked tab's outcome, in the order asked: how (answered, timeout,
    or sent with no_wait) and the reply read from its transcript; a work
    session's reply is kept (keep_reply). -> the entries the output lists."""
    out = []
    for t in asked:
        answered = "answered" in t
        reply = ask_reply(t["h"], question) if (answered or no_wait) else ""
        if answered and not reply:
            # Stopped, but the transcript has no text for it yet (or none at
            # all): read it once more after a moment, then report as is.
            time.sleep(ASK_POLL)
            reply = ask_reply(t["h"], question)
        how = "answered" if answered else ("sent" if no_wait else "timeout")
        out.append({"session_id": t["session_id"], "name": t["name"], "project": t["project"],
                    "how": how, "reply": reply})
        if t["rec"] is not None and reply:
            keep_reply(t["rec"], reply, question)
    return out


def print_ask(asked, skipped, failed, timeout):
    """ask's result for a person: each reply under a heading, then who was
    skipped and who could not be reached."""
    if not asked and not skipped and not failed:
        print("nothing to ask: no live Claude Code tab")
        return
    for i, e in enumerate(asked):
        if i:
            print()
        who = e["name"] or e["session_id"]
        print(f"## {who}  ({display_path(e['project'])})  {e['how']}")
        if e["reply"]:
            print(e["reply"])
        elif e["how"] == "sent":
            print("(asked; savannah work output shows the reply)")
        else:
            print(f"(no reply within {timeout} s; savannah work output shows it when it comes)")
    for e in skipped:
        print(f"skipped {e['name'] or e['session_id'] or display_path(e['project'])}: {e['why']}")
    for e in failed:
        print(f"failed {e['name'] or e['session_id']}: {e['error']}")


def cmd_ask(a):
    """Type one question into every tab that is safe to interrupt (idle or
    done; never a working or waiting one, never the asking session), wait
    for the replies (each tab's Claude answers in its own context, the
    hook says when it has stopped), keep each work session's reply as a
    note and as its summary, print them all. --no-wait types and returns; --timeout caps
    the wait. Exit 0 whatever was asked or skipped; the output says."""
    question = (a.question or "").strip() or ASK_DEFAULT
    if not a.name and not a.all:
        return usage_error("ask", "name the work sessions to ask, or --all for every live tab")
    if a.timeout < 1:
        return usage_error("ask", "--timeout wants a positive number of seconds")
    hooks = load_hook_sessions()
    records = list_records()
    targets, skipped = ask_targets(a.name, a.all, hooks, records)
    asked, failed = ask_send(make_terminal(""), targets, question)
    log_info("ask", asked=len(asked), skipped=len(skipped), failed=len(failed), wait=not a.no_wait,
             timeout=a.timeout)
    log_verbose("ask question", text=question[:300])
    waited = 0.0
    if asked and not a.no_wait:
        waited = ask_wait(asked, question, a.timeout)
    out_asked = ask_collect(asked, question, a.no_wait)
    log_info("ask done", asked=len(asked), answered=sum(1 for e in out_asked if e["how"] == "answered"),
             waited=waited)
    if a.json:
        print(json.dumps({"question": question, "timeout": a.timeout, "waited": waited, "asked": out_asked,
                          "skipped": skipped, "failed": failed}))
    else:
        print_ask(out_asked, skipped, failed, a.timeout)
    return 0
