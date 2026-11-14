#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_work - local "work sessions" (Phase 5d).
#
# A work session models the owner's manual habit: one iTerm2 tab per work
# item, split into an upper pane running INTERACTIVE Claude Code in a git
# worktree and a lower shell pane cd'd into the same worktree, plus a plain
# text notes file so the human can remember what they were doing there.
#
# This is a LOCAL, FILE-OWNED object, like hooks/. No savannahd, no song, no
# mesh worker, no tokens burned by this module itself. Storage:
#   $SAVANNAH_WORK_DIR (default ~/.claude/savannah/work)
#     <name>/record.json                 written atomically (tmp + os.replace)
#     <name>/notes.md                    append-only human text
#     <name>/status.json                 what that tab's Claude says it is polling
#                                        for the owner (text, count, updated)
#     archive/<name>-<created-epoch>.md  notes copied here on rm (-2, -3, ...
#                                        appended when that name is taken)
#     snapshots/<YYYYMMDD-HHMMSS>.json   the layout saved by snapshot (-2, -3,
#                                        ... when one second holds two)
#     snapshots/latest.json              a copy of the newest one (restore's default)
#
# Join rule: a Claude Code hook file ($SAVANNAH_SESSIONS_DIR/<id>.json, written
# by hooks/session-status.py) BELONGS to a work session when
# realpath(hook.project) == realpath(record.worktree). The join happens at read
# time (ls, the dash); nobody writes into the other side's file.
#
# Verbs:
#   new <name> [--repo DIR] [--branch B] [--no-worktree] [--prompt T] [--note T]
#   ls [--json]
#   note <name> <text...>     (or text on stdin)
#   notes <name>
#   say <name> <text...>      type into the Claude pane
#   sh <name> <cmd...>        type into the shell pane
#   jump <name> [--shell]     focus the Claude pane (or shell pane)
#   open <name> [--shell]     focus the tab, or open it again when it is gone
#                             (new tab + split, claude --continue): the way
#                             back after an iTerm2 restart
#   repos [DIR]               the git repos in your folder of repos (DIR
#                             remembers it, in the same file hub uses); the
#                             board's new-task dialog reads this
#   output <name> [--last N]  the last N turns (prompt + reply) of that tab's Claude
#   rm <name> [--keep-worktree] [--force]
#   move <name> [--now|--active|--later] [--rank N]
#                             where the row sits on the board: NOW (what you
#                             are pushing on), ACTIVE or LATER, and its order
#   rename <old> <new>        a new record name; the worktree, the branch and
#                             the tab keep the old one (a Claude runs there)
#   status [<name>|--here] [text...] [--clear]
#                             what that tab's Claude is polling for the owner:
#                             text reports one more poll (counted), nothing
#                             reads it, --clear removes it; --here is the
#                             session this shell runs in
#   snapshot [--note T]       save the layout before a reboot: every work
#                             session (tab order, Claude session id) and every
#                             other open Claude Code tab
#   snapshots                 the saved snapshots, newest first
#   restore [STAMP|latest] [--dry-run] [--no-plain]
#                             put the tabs back: remake each work session whose
#                             tab is gone (claude -n <name> --resume <id>, else
#                             --continue, else fresh) and reopen the plain tabs
#                             (single pane, claude --resume <id>); never closes
#                             anything; one failed item = exit 1, the rest goes on
#   hub [DIR] [--no-launch]   OPTIONAL: make DIR a hub (.mcp.json with the
#                             peer shim, a starter CLAUDE.md, a
#                             .savannah-scratch/ area), remember it in
#                             $SAVANNAH_HUB_FILE, launch claude there: a
#                             coordinating Claude that opens tabs through the
#                             work_* tools. The front door is the dash
#                             (bare `savannah`), not this.
#
# Exit codes: 0 success, 1 user error (unknown name, git refused, pane gone),
# 2 bad usage. --json on ls/new/open/repos/note/output/hub/move/rename/status
# emits one JSON value on stdout.
#
# Reply channel: `say` types into the pane and gets nothing back. `output`
# is the read side: it parses the transcript Claude Code itself writes
# (the path the hook recorded, else the newest .jsonl under
# $CLAUDE_CONFIG_DIR/projects/<encoded worktree>/) and prints the human
# prompts and assistant text of the last N turns. The transcript is Claude
# Code's file and its layout is not a contract we own; the subset read here
# (type user/assistant, message.content text blocks, isSidechain) is pinned
# by test_work's hand-written transcript.
#
# Trust: the NAME on the command line is the only thing that names a record
# directory. A record.json's own "name" field is never used to build a path
# (a record that says "../x" must not be able to point rm at the work dir).
#
# Terminal seam: the Terminal class (savannah_terminal.py) is the only thing
# that knows about iTerm2. ITerm shells out to iTerm2's own `it2` command
# ($SAVANNAH_IT2, else `it2` on PATH, else the copy inside iTerm2.app) so
# tests substitute tools/fake_it2.py; the AppleScript route it replaced had
# no tab ids and a 60 s Automation dialog (5d). A tmux implementation plugs
# in later behind the same methods. Stdlib only.
#
# Modules: this file is the entry point (the path the CLI, the shim, the
# hook and the tests name) and holds the core verbs, the parser and main.
# The rest sits beside it as flat modules imported by plain name (not a
# package, no relative imports), each layer importing only the ones before
# it and none of them this file:
#   savannah_log         the one log
#   savannah_core        UserError, constants, paths, atomic writes, records,
#                        notes, status, text and output helpers
#   savannah_terminal    the Terminal seam and ITerm (it2)
#   savannah_git         run_git, the repo root, a session's worktree
#   savannah_sessions    hook sessions and the join with records
#   savannah_transcript  finding and parsing Claude Code transcripts
#   savannah_tabs        claude_command, reclaim_pane, remake_tab, adopt_session
#   savannah_hub         repos, hub
#   savannah_snapshot    snapshot, snapshots, restore
#   savannah_autoname    autoname
#   savannah_ask         ask
# Names the tests reach on this module are imported here by name (the
# re-export block below). A test that patches a function patches it on the
# module that owns it (savannah_core.atomic_write,
# savannah_terminal.subprocess.run): a patch on this module's copy of the
# name would not reach the code that moved.

import argparse
import json
import os
import shlex
import shutil
import sys
import time

# --------------------------------------------------------------------------
# the sibling modules: this file's own directory (symlinks resolved) goes on
# sys.path before any of them is imported, so they are found when this file
# is run from another cwd, through a symlink, or loaded by path from
# elsewhere (the tests do this).

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# logging: savannah's one log (work/savannah_log.py beside this file), the
# file every piece of savannah writes, src=work for this process. A few
# lines per verb: INFO for a state change, DEBUG for lookups and every it2
# or git call, VERBOSE for payloads; `work log` reads them back.

import savannah_log  # noqa: E402
from savannah_log import (VERBOSE, log_dir, log_path, log_verbose, log_debug,  # noqa: E402,F401
                          log_info, log_warn, log_error)

savannah_log.configure("work")

import savannah_core  # noqa: E402
from savannah_core import (append_note, archive_target, claim_record_dir, clip, emit, first_line,  # noqa: E402
                           is_direct_child, is_inside, last_note_line, list_records, load_record,
                           new_record, normalize_record, note_stamp, notes_path, oserr, print_table,
                           read_notes, read_status, record_dir, record_path, save_record, session_here,
                           status_path, status_text, title_text, usage_error, UserError, valid_name,
                           work_dir)
import savannah_terminal  # noqa: E402,F401
from savannah_terminal import make_terminal  # noqa: E402
import savannah_git  # noqa: E402
from savannah_git import (add_worktree, copy_worktree_includes, remove_worktree, run_git, Worktree)  # noqa: E402
from savannah_sessions import (claude_alive, hook_session_by_id, join_session, load_hook_sessions,  # noqa: E402
                               sessions_dir)
from savannah_transcript import find_transcript, local_stamp, transcript_turns  # noqa: E402
from savannah_tabs import adopt_session, claude_command, reclaim_pane, remake_tab  # noqa: E402
from savannah_hub import cmd_hub, cmd_repos  # noqa: E402
from savannah_snapshot import cmd_restore, cmd_snapshot, cmd_snapshots  # noqa: E402
from savannah_autoname import cmd_autoname  # noqa: E402
import savannah_ask  # noqa: E402
from savannah_ask import cmd_ask  # noqa: E402

# Re-exports: names this file does not use itself but the tests (and
# test_shim's HUB_ALLOW check) reach as attributes of this module.
from savannah_core import ARCHIVE_SUBDIR, NAME_HELP, TITLE_MAX  # noqa: E402,F401
from savannah_terminal import it2_argv, ITerm  # noqa: E402,F401
from savannah_git import git_fail_message  # noqa: E402,F401
from savannah_sessions import normalize_hook  # noqa: E402,F401
from savannah_transcript import encode_project_dir  # noqa: E402,F401
from savannah_hub import HUB_ALLOW  # noqa: E402,F401
from savannah_snapshot import SNAPSHOT_STAMP_RE, tab_title  # noqa: E402,F401
from savannah_autoname import (checkout_name, clean_title, session_excerpt, session_prompts, slug_name,  # noqa: E402,F401
                               unique_name)
from savannah_ask import ASK_DEFAULT, ask_skip_reason, first_prompt_line  # noqa: E402,F401

# --------------------------------------------------------------------------
# verbs


OUTPUT_PROMPT_CLIP = 120           # chars of the prompt shown on an output header


def ls_entry(rec, hooks):
    entry = dict(rec)
    h = join_session(rec, hooks)
    entry["claude_session_id"] = (h or {}).get("session_id", "") or ""
    entry["state"] = (h or {}).get("state", "") or ("offline" if h is None else "")
    entry["message"] = (h or {}).get("message", "") or ""
    entry["last_prompt"] = (h or {}).get("last_prompt", "") or ""
    entry["transcript_path"] = (h or {}).get("transcript_path", "") or ""
    entry["notes_path"] = notes_path(rec["name"])
    entry["last_note"] = last_note_line(read_notes(rec["name"]))
    entry["status"] = read_status(rec["name"])
    return entry


def rollback_new(name, rdir, term, ids, wt):
    """Undo a failed `new`. Nothing of ours should survive it, whatever
    failed: the tab if it already exists (a record write failing after the
    tab is up would otherwise orphan it), the fresh worktree (nothing in it
    yet), the branch we just made, and last, always, the claimed record
    dir. A reattached or --branch branch is left alone."""
    log_warn("new rolled back", name=name, tab=bool(ids), worktree=wt.made, branch=wt.made_branch)
    try:
        if ids:
            try:
                term.close_tab(ids.get("tab_id", ""),
                               (ids.get("claude_pane", ""), ids.get("shell_pane", "")))
            except Exception:
                pass
        if wt.made:
            run_git(["-C", wt.repo, "worktree", "remove", "--force", wt.worktree])
        if wt.made_branch:
            run_git(["-C", wt.repo, "branch", "-D", wt.branch])
    finally:
        shutil.rmtree(rdir, ignore_errors=True)


def cmd_new(a):
    name = a.name
    if not valid_name(name):
        raise UserError(f"invalid name {name!r}: {savannah_core.NAME_HELP}")
    if a.branch and a.no_worktree:
        return usage_error("new", "--branch needs a worktree to check it out in; drop --no-worktree")
    if a.branch is not None and not a.branch.strip():
        return usage_error("new", "--branch needs a branch name")

    repo = os.path.abspath(a.repo or os.getcwd())
    if not os.path.isdir(repo):
        raise UserError(f"repo is not a directory: {repo}")

    rdir = claim_record_dir(name)
    wt = Worktree(repo, repo, "", False, False, False)  # --no-worktree: the repo itself
    copied = []
    term = make_terminal("")
    ids = None
    try:
        if not a.no_worktree:
            wt = add_worktree(repo, name, a.branch)
        prompt = a.prompt or ""
        if wt.made:
            copied = copy_worktree_includes(wt.repo, wt.worktree)
        ids = term.create_split_tab(name, claude_command(wt.worktree, name, prompt),
                                    f"cd {shlex.quote(wt.worktree)}")

        rec = new_record(name, wt.repo, wt.worktree, wt.branch, term.kind, tab_id=ids.get("tab_id", ""),
                         claude_pane=ids.get("claude_pane", ""), shell_pane=ids.get("shell_pane", ""),
                         prompt=prompt, title=getattr(a, "title", ""))
        savannah_core.atomic_write(record_path(name), rec)
        log_info("new", name=name, repo=wt.repo, worktree=wt.worktree, branch=wt.branch, tab=rec["tab_id"],
                 claude_pane=rec["claude_pane"], shell_pane=rec["shell_pane"], reattached=wt.reattached,
                 copied=len(copied), title=rec["title"])
        if a.note:
            append_note(name, a.note)
    except BaseException:
        rollback_new(name, rdir, term, ids, wt)
        raise

    if wt.reattached:
        print(f"new: reattached existing branch {wt.branch}", file=sys.stderr)
    if copied:
        print(f"new: copied {len(copied)} from {savannah_git.WORKTREE_INCLUDE}: {', '.join(copied)}", file=sys.stderr)
    where = f"{wt.worktree} on {wt.branch}" if wt.branch else wt.worktree
    tag = " (reattached)" if wt.reattached else ""
    emit(a, rec, f"{name}: {where}{tag} (tab {rec['tab_id']}, claude {rec['claude_pane']})")
    return 0


def cmd_ls(a):
    hooks = load_hook_sessions()
    entries = [ls_entry(r, hooks) for r in list_records()]
    # Whether each tab still exists is one it2 call for the whole list; when
    # it2 is unavailable (no iTerm2 here) the field is null, never an error.
    try:
        live = make_terminal("").live_ids()
    except UserError:
        live = None
    for e in entries:
        e["pane_alive"] = None if live is None else claude_alive(e, live)
    log_debug("ls", records=len(entries), hooks=len(hooks),
              joined=sum(1 for e in entries if e["claude_session_id"]),
              live=("null" if live is None else len(live)))
    if a.json:
        print(json.dumps(entries))
        return 0
    if not entries:
        print("(no work sessions)")
        return 0

    def cell(s, n):
        return clip(first_line(s), n)

    def status_cell(st):
        if not st:
            return "-"
        count = f" ({st['count']})"
        return cell(st["text"], 30 - len(count)) + count

    rows = [("NAME", "TITLE", "BRANCH", "STATE", "STATUS", "LAST PROMPT", "LAST NOTE")]
    for e in entries:
        rows.append((e["name"], cell(e["title"], 24) or "-", e["branch"] or "-", e["state"],
                     status_cell(e["status"]), cell(e["last_prompt"], 40), cell(e["last_note"], 40)))
    print_table(rows)
    return 0


def cmd_note(a):
    rec = load_record(a.name)
    text = " ".join(a.text) if a.text else sys.stdin.read()
    if not text.strip():
        return usage_error("note", "no text given (pass words or pipe text on stdin)")
    now = time.time()
    path = append_note(a.name, text, now)
    save_record(rec)
    log_info("note", name=a.name, chars=len(text.rstrip("\n")), path=path)
    emit(a, {"name": a.name, "notes_path": path, "time": note_stamp(now), "text": text.rstrip("\n")},
         f"noted in {path}")
    return 0


def cmd_notes(a):
    load_record(a.name)
    text = read_notes(a.name)
    log_debug("notes read", name=a.name, chars=len(text))
    sys.stdout.write(text)
    return 0


def type_into(rec, pane_key, text, label):
    term = make_terminal(rec.get("terminal", ""))
    pane = rec.get(pane_key, "")
    if not pane or not term.write(pane, text):
        # The Claude pane may have moved: the tab closed and claude started
        # again by hand in the worktree. One look, then one more try.
        if pane_key == "claude_pane" and reclaim_pane(rec, term.live_ids()) \
                and term.write(rec["claude_pane"], text):
            pane = rec["claude_pane"]
        else:
            log_warn("pane gone", name=rec["name"], pane=label, session=pane)
            raise UserError(f"{label} pane of {rec['name']} no longer exists")
    log_info("typed", name=rec["name"], pane=label, session=pane, chars=len(text))
    log_verbose("typed text", name=rec["name"], pane=label, text=text[:300])
    return 0


def cmd_say(a):
    text = " ".join(a.text)
    if not text.strip():
        return usage_error("say", "no text given (a bare Enter is never typed into a live pane)")
    rec = load_record(a.name)
    return type_into(rec, "claude_pane", text, "claude")


def cmd_sh(a):
    text = " ".join(a.cmd)
    if not text.strip():
        return usage_error("sh", "no command given (a bare Enter is never typed into a live pane)")
    rec = load_record(a.name)
    return type_into(rec, "shell_pane", text, "shell")


def cmd_jump(a):
    rec = load_record(a.name)
    term = make_terminal(rec.get("terminal", ""))
    key = "shell_pane" if a.shell else "claude_pane"
    pane = rec.get(key, "")
    if not pane or not term.focus(pane):
        # claude started again by hand in the worktree: one look, one more try
        if not a.shell and reclaim_pane(rec, term.live_ids()) and term.focus(rec["claude_pane"]):
            log_info("focused", name=rec["name"], pane=key, session=rec["claude_pane"])
            return 0
        log_warn("pane gone", name=rec["name"], pane=key, session=pane)
        raise UserError(f"{key.replace('_', ' ')} of {rec['name']} not found")
    log_info("focused", name=rec["name"], pane=key, session=pane)
    return 0


def cmd_open(a):
    """Focus the tab if it is still there, else open it again: a fresh tab and
    split in the same worktree, claude resuming the worktree's last
    conversation when one exists. The way back after an iTerm2 restart."""
    rec = load_record(a.name)
    term = make_terminal(rec.get("terminal", ""))
    live = term.live_ids()
    alive = claude_alive(rec, live)
    if not alive:
        alive = reclaim_pane(rec, live)  # claude started again by hand in the worktree
    if alive:
        pane = rec["claude_pane"]
        if a.shell and rec.get("shell_pane") in live:
            pane = rec["shell_pane"]
        if not term.focus(pane):
            raise UserError(f"pane of {a.name} vanished while focusing it")
        action = "focused"
    else:
        how = remake_tab(rec, term, live)
        action = "reopened (claude --continue)" if how == "continue" else "reopened"
    emit(a, {"name": a.name, "action": action.split(" ")[0], "tab_id": rec.get("tab_id", ""),
             "claude_pane": rec.get("claude_pane", ""), "shell_pane": rec.get("shell_pane", "")},
         f"{a.name}: {action} (tab {rec.get('tab_id', '')}, claude {rec.get('claude_pane', '')})")
    return 0


def cmd_adopt(a):
    """Take a Claude Code tab you opened yourself into a named work session:
    no new tab, no worktree, just the record that gives it notes, say,
    output, open, jump and rm (adopt_session)."""
    if not valid_name(a.name):
        raise UserError(f"invalid name {a.name!r}: {savannah_core.NAME_HELP}")
    h = hook_session_by_id(a.session)
    if h is None:
        raise UserError(f"no live Claude session {a.session} (no hook file under {sessions_dir()}; "
                        "hooks/install.py not run, or the tab is closed)")
    rec = adopt_session(a.name, h, a.session, title=getattr(a, "title", ""), note=a.note)
    emit(a, rec,
         f"{a.name}: adopted {rec['worktree']} (tab {rec['tab_id'] or '-'}, claude {rec['claude_pane'] or '-'})")
    return 0


def cmd_move(a):
    """Where the row sits on the board: the NOW section (what the owner is
    pushing on, the top of the board), ACTIVE (what they are on, in the
    background) or LATER, and the rank that orders rows inside a section
    (lower first; a new session's rank is its creation time, so untouched
    rows keep arriving in order). Nothing lands in NOW by itself: only this
    verb and the board's Ctrl-P put a row there. Storage only: the tab, the
    worktree and the branch are not touched."""
    if not (any(getattr(a, p) for p in savannah_core.PRIORITIES) or a.rank is not None):
        return usage_error("move", "give --now, --active, --later or --rank N (or several)")
    rec = load_record(a.name)
    for p in savannah_core.PRIORITIES:   # the flags are --now, --active, --later; at most one is set
        if getattr(a, p):
            rec["priority"] = p
            break
    if a.rank is not None:
        rec["rank"] = a.rank
    save_record(rec)
    log_info("moved", name=rec["name"], priority=rec["priority"], rank=rec["rank"])
    emit(a, {"name": rec["name"], "priority": rec["priority"], "rank": rec["rank"]},
         f"moved {rec['name']}: {rec['priority']}, rank {rec['rank']}")
    return 0


RENAME_FILES = ("record.json", "notes.md", savannah_core.STATUS_FILE)


def cmd_rename(a):
    """A new name for the record: record.json, notes.md and status.json move
    to <new>/ and the record is rewritten with the new name. The worktree
    directory, the branch (work/<old>) and the tab's own Claude session name
    (claude -n <old>) keep the old name: a running Claude sits in that
    directory, and moving it would pull the floor from under it."""
    old, new = a.old, a.new
    if not valid_name(old):
        raise UserError(f"invalid work session name: {old!r}")
    if not valid_name(new):
        raise UserError(f"invalid name {new!r}: {savannah_core.NAME_HELP}")
    if old == new:
        raise UserError(f"new name {new!r} is the current name")
    rec = load_record(old)
    old_dir = record_dir(old)
    if os.path.islink(old_dir):
        raise UserError(f"refusing to rename {old_dir}: it is a symlink")
    new_dir = claim_record_dir(new)
    moved = []
    try:
        for fname in RENAME_FILES:
            src = os.path.join(old_dir, fname)
            if os.path.lexists(src):
                os.replace(src, os.path.join(new_dir, fname))
                moved.append(fname)
        save_record(normalize_record(rec, new))
        shutil.rmtree(old_dir)
        log_info("renamed", old=old, new=new, moved=moved)
    except BaseException:
        # Best effort back to the start: every moved file returns to <old>/
        # and the claim on <new>/ goes, unless a file could not be moved
        # back, in which case <new>/ keeps what it holds (the owner's notes
        # are never deleted to tidy up a failure).
        restored = True
        try:
            os.makedirs(old_dir, exist_ok=True)
        except OSError:
            restored = False
        for fname in moved:
            try:
                os.replace(os.path.join(new_dir, fname), os.path.join(old_dir, fname))
            except OSError:
                restored = False
        if restored:
            shutil.rmtree(new_dir, ignore_errors=True)
        raise
    emit(a, {"name": new, "old": old, "worktree": rec["worktree"], "branch": rec["branch"]},
         f"renamed {old} to {new} (worktree and branch keep their names)")
    return 0


def cmd_status(a):
    """The poll report of a session: what that tab's Claude is waiting on for
    the owner (a test run, CI, a deploy), written by that Claude itself with
    the command the SessionStart hook hands it, and shown on the board with a
    count and an age. One file, <name>/status.json, rewritten whole on every
    report; --clear removes it; with neither text nor --clear it is printed.
    With --here every positional is text (the session is the one this shell
    runs in), so `status --here waiting for ctest` needs no quotes."""
    if a.here and a.name is not None:
        a.text = [a.name] + a.text
        a.name = None
    if a.name is None and not a.here:
        return usage_error("status", "name a session, or pass --here for the one this shell runs in")
    if a.clear and a.text:
        return usage_error("status", "--clear takes no text")
    text = status_text(a.text) if a.text else ""
    if a.text and not text:
        return usage_error("status", "status text is empty")
    rec = session_here() if a.here else load_record(a.name)
    name = rec["name"]
    path = status_path(name)
    if a.clear:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        log_info("status cleared", name=name)
        emit(a, {"name": name, "status": None}, f"{name}: status cleared")
        return 0
    if text:
        prev = read_status(name)
        st = {"text": text, "updated": int(time.time()), "count": (prev["count"] if prev else 0) + 1}
        savannah_core.atomic_write(path, st)
        log_info("status reported", name=name, count=st["count"], text=text)
        emit(a, dict(st, name=name), f"{name}: poll {st['count']}: {text}")
        return 0
    st = read_status(name)
    log_debug("status read", name=name, found=st is not None)
    if st is None:
        line = f"{name}: (no status)"
    else:
        age = max(0, int(time.time()) - st["updated"])
        line = f"{name}: {st['text']} ({st['count']} polls, last {age}s ago)"
    emit(a, {"name": name, "status": st}, line)
    return 0


def cmd_title(a):
    """The free-text title the board shows in its WORK column: capitals,
    spaces, anything, first line only, TITLE_MAX chars. The name stays the
    identifier underneath (the directory, the branch, the tab, every verb),
    so a title changes at any time and moves nothing. Text sets it, --clear
    removes it, neither prints it."""
    if a.clear and a.text:
        return usage_error("title", "--clear takes no text")
    text = title_text(" ".join(a.text)) if a.text else ""
    if a.text and not text:
        return usage_error("title", "title text is empty")
    rec = load_record(a.name)
    name = rec["name"]
    if a.clear or text:
        rec["title"] = text
        save_record(rec)
        log_info("title cleared" if a.clear else "title set", name=name, title=text)
        emit(a, {"name": name, "title": text},
             f"{name}: title cleared" if a.clear else f"{name}: title set: {text}")
        return 0
    log_debug("title read", name=name, title=rec["title"])
    emit(a, {"name": name, "title": rec["title"]},
         f"{name}: {rec['title']}" if rec["title"] else f"{name}: (no title)")
    return 0


LOG_LINES_DEFAULT = 200
LOG_FOLLOW_INTERVAL = 0.5


def read_log_tail(path, level, count):
    """(lines, size): the last `count` lines of the current log file that a
    reader at `level` shows (savannah_log.shows), oldest first, and how many
    bytes of the file they came from (where --follow picks up). No file yet:
    ([], 0)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return [], 0
    text = data.decode("utf-8", errors="replace")
    kept = [ln for ln in text.splitlines() if savannah_log.shows(ln, level)]
    return kept[-count:], len(data)


def follow_log(path, level, pos):
    """Print each new line of the current file as it lands (a poll every
    half second), from byte `pos`; a file that is replaced or shrinks
    (rotation) is read again from its start. Ctrl-C ends it, exit 0."""
    ident = None
    buf = b""
    try:
        while True:
            try:
                st = os.stat(path)
            except OSError:
                time.sleep(LOG_FOLLOW_INTERVAL)
                continue
            now_ident = (st.st_dev, st.st_ino)
            if ident is not None and (now_ident != ident or st.st_size < pos):
                pos, buf = 0, b""
            ident = now_ident
            if st.st_size > pos:
                try:
                    with open(path, "rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                except OSError:
                    chunk = b""
                buf += chunk
                lines = buf.split(b"\n")
                buf = lines.pop()
                for raw in lines:
                    ln = raw.decode("utf-8", errors="replace")
                    if savannah_log.shows(ln, level):
                        print(ln)
                sys.stdout.flush()
            time.sleep(LOG_FOLLOW_INTERVAL)
    except KeyboardInterrupt:
        return 0


def cmd_log(a):
    """Read savannah's log back: the last --lines lines of the current file
    (<log dir>/savannah.log) that pass --level, oldest first. The file holds
    every level; normal shows INFO, WARN and ERROR, debug adds DEBUG, verbose
    adds VERBOSE (the board's Ctrl-L view reads the same way), and a line
    with no level stamp shows at every level. --follow keeps printing new
    lines until Ctrl-C (exit 0); --path prints only the file's path; --json
    prints one object. Reading the log logs nothing above VERBOSE (main)."""
    path = log_path()
    if a.path:
        print(path)
        return 0
    if a.lines < 1:
        return usage_error("log", "--lines wants a positive count")
    if a.follow and a.json:
        return usage_error("log", "--follow prints lines as they land; drop --json")
    lines, size = read_log_tail(path, a.level, a.lines)
    if a.json:
        print(json.dumps({"path": path, "level": a.level, "lines": lines}))
        return 0
    if not os.path.exists(path):
        print(f"(no log yet at {path})")
    for ln in lines:
        print(ln)
    if not a.follow:
        return 0
    sys.stdout.flush()
    return follow_log(path, a.level, size)


def cmd_output(a):
    if a.last < 1:
        return usage_error("output", "--last wants a positive count")
    rec = load_record(a.name)
    path, source = find_transcript(rec, load_hook_sessions())
    if not path:
        raise UserError(f"no transcript for {a.name} yet (claude not started in "
                        f"{rec.get('worktree') or 'its worktree'}, or hooks/install.py not run)")
    turns = transcript_turns(path)
    shown = turns[-a.last:] if turns else []
    log_verbose("output turns", name=a.name, source=source, total=len(turns), shown=len(shown),
                prompts=" | ".join(first_line(t["prompt"], 60) for t in shown))
    if a.json:
        print(json.dumps({"name": a.name, "transcript_path": path, "source": source,
                          "turns_total": len(turns), "turns": shown}))
        return 0
    if not shown:
        print("(no turns yet)")
        return 0
    for i, t in enumerate(shown):
        if i:
            print()
        head = clip(first_line(t["prompt"]), OUTPUT_PROMPT_CLIP)
        when = local_stamp(t["answered"] or t["asked"])
        print(f"## {when}  you: {head}" if head else f"## {when}")
        print(t["text"] if t["text"] else "(no reply yet)")
    return 0


def stale_record_reason(name):
    """Why a record dir holds no usable record, or "" when it does. Read by
    hand, not through load_record: that folds all three cases into "no such
    work session", and rm tells the owner which one it was."""
    if not os.path.exists(record_path(name)):
        return "no record.json"      # new died between mkdir and record write
    try:
        with open(record_path(name)) as f:
            if not isinstance(json.load(f), dict):
                return "record.json is not an object"
    except (OSError, ValueError):
        return "unreadable record.json"
    return ""


def remove_stale(name, rdir, why):
    """Forget a stale or corrupt record dir. Any worktree it had is left for
    `git worktree list` since the record cannot say where it is."""
    try:
        shutil.rmtree(rdir)
    except OSError as e:
        raise UserError(f"cannot remove stale {rdir}: {oserr(e)}")
    log_info("removed stale", name=name, why=why)
    print(f"removed {name} (stale: {why}; check `git worktree list` for leftovers)")


def refuse_nested(name, worktree):
    """rm never removes a worktree another work session lives inside."""
    nested = [r["name"] for r in list_records()
              if r["name"] != name and r.get("worktree") and is_inside(r["worktree"], worktree)]
    if nested:
        raise UserError(f"refusing: work session(s) {', '.join(sorted(nested))} live inside "
                        f"{worktree}; rm them first or pass --keep-worktree")


def archive_notes(name, rec):
    """Copy the session's notes into the archive. -> the copy's path, "" when
    there are no notes. A copy, so nothing is lost if a later step of rm
    refuses: withdraw_archive takes it back and everything stays in place."""
    np = notes_path(name)
    archived = ""
    if os.path.exists(np):
        try:
            adir = os.path.join(work_dir(), savannah_core.ARCHIVE_SUBDIR)
            os.makedirs(adir, exist_ok=True)
            created = rec.get("created") or int(time.time())
            archived = archive_target(adir, name, created)
            shutil.copy2(np, archived)
        except OSError as e:
            raise UserError(f"cannot archive notes to {archived or adir}: {oserr(e)}")
    return archived


def withdraw_archive(archived):
    """Take back archive_notes' copy (best effort); "" = there was none."""
    if archived:
        try:
            os.remove(archived)
        except OSError:
            pass


def cmd_rm(a):
    name = a.name
    if not valid_name(name):
        raise UserError(f"invalid work session name: {name!r}")
    rdir = record_dir(name)
    # Only ever delete a directory that really sits directly under the work
    # dir; a symlink planted there could otherwise point rm anywhere.
    if os.path.lexists(rdir) and (os.path.islink(rdir) or not is_direct_child(rdir, work_dir())):
        raise UserError(f"refusing to remove {rdir}: not a directory directly under {work_dir()}")
    if os.path.isdir(rdir):
        why = stale_record_reason(name)
        if why:
            remove_stale(name, rdir, why)
            return 0

    rec = load_record(name)
    worktree = rec.get("worktree", "")
    repo = rec.get("repo", "")
    adopted = bool(rec.get("adopted"))
    has_worktree = bool(worktree) and os.path.realpath(worktree) != os.path.realpath(repo or "")
    # An adopted session runs in a directory the owner chose; git never
    # removes or prunes it, whatever --force or --keep-worktree say.
    removing_worktree = has_worktree and not a.keep_worktree and not adopted

    if removing_worktree:
        refuse_nested(name, worktree)

    # Archive first (a copy, so nothing is lost if a later step refuses):
    # if git says no, the copy is withdrawn and everything stays in place.
    archived = archive_notes(name, rec)

    # Worktree next: a dirty worktree without --force must leave EVERYTHING
    # in place, including the tab, so nothing is closed before git agrees.
    if removing_worktree:
        try:
            remove_worktree(repo, worktree, a.force)
        except BaseException:
            withdraw_archive(archived)
            raise

    closed = False
    try:
        term = make_terminal(rec.get("terminal", ""))
        closed = term.close_tab(rec.get("tab_id", ""), (rec.get("claude_pane", ""), rec.get("shell_pane", "")))
    except UserError:
        pass  # best effort; the tab may be long gone, or iTerm2 not running

    try:
        shutil.rmtree(rdir)
    except OSError as e:
        raise UserError(f"cannot remove {rdir}: {oserr(e)}"
                        + (f" (notes already archived to {archived})" if archived else ""))
    log_info("removed", name=name, worktree=worktree, worktree_removed=removing_worktree, adopted=adopted,
             tab_closed=closed, archived=archived)
    tail = ""
    if adopted:
        tail = " (adopted: directory kept)"
    elif has_worktree and a.keep_worktree:
        tail = " (worktree kept)"
    print(f"removed {name}{tail}")
    return 0


# --------------------------------------------------------------------------


def build_parser():
    """The whole command line as one flat table: a verb() per verb, its
    arguments in the order --help shows them."""
    ap = argparse.ArgumentParser(
        prog="savannah_work",
        description="Local interactive Claude Code work sessions: one iTerm2 tab "
                    "(Claude above, shell below) per work item, plus notes. "
                    "Text that begins with '-' goes after '--': say NAME -- --continue")
    sub = ap.add_subparsers(dest="verb", metavar="<verb>")
    sub.required = True

    def arg(*names, **kw):
        return names, kw

    def flag(name, help=None):
        return arg(name, action="store_true", help=help)

    def oneof(*args):
        return None, args   # a mutually exclusive group of arg()s

    def verb(name, fn, help, *args, json=True):
        # One verb: its parser, its arguments in order, --json last (so it
        # closes the usage line), and the function that runs it.
        p = sub.add_parser(name, help=help)
        for names, kw in args:
            if names is None:
                g = p.add_mutually_exclusive_group()
                for gnames, gkw in kw:
                    g.add_argument(*gnames, **gkw)
            else:
                p.add_argument(*names, **kw)
        if json:
            p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)

    name = arg("name")
    note = arg("--note", help="first note to record")
    dash_hint = "(use -- before text starting with -)"
    title_cut = f"(first line, {savannah_core.TITLE_MAX} chars"

    verb("new", cmd_new, "create a work session (worktree, tab, record)",
         name,
         arg("--repo", help="git repo to work in (default: cwd); resolved to the main worktree root"),
         arg("--branch", help="check out this existing local branch (default: work/<name>, "
                              "created, or reattached if rm left it behind)"),
         flag("--no-worktree", "run in the repo itself"),
         arg("--prompt", help="initial prompt passed to claude"),
         note,
         arg("--title", help=f"free-text title the board shows {title_cut}; "
                             "the name stays the identifier)"))
    verb("ls", cmd_ls, "list work sessions joined with live Claude sessions")
    verb("note", cmd_note, "append a note (text args or stdin)", name, arg("text", nargs="*"))
    verb("notes", cmd_notes, "print the notes file", name, json=False)
    verb("say", cmd_say, f"type text into the Claude pane {dash_hint}",
         name, arg("text", nargs="+"), json=False)
    verb("ask", cmd_ask,
         "type one question into every tab that is safe to interrupt (idle or "
         "done, never working or waiting), wait for the replies, keep each work "
         "session's as its summary",
         arg("name", nargs="*", help="work sessions to ask (or --all)"),
         flag("--all", "every live Claude Code tab, plain tabs too"),
         arg("-q", "--question", default="", metavar="TEXT",
             help="the question (default: what are you working on, where are you, what is next)"),
         arg("--timeout", type=float, default=savannah_ask.ASK_TIMEOUT, metavar="SECONDS",
             help=f"how long to wait for the replies (default {savannah_ask.ASK_TIMEOUT})"),
         flag("--no-wait", "type the question and return at once"))
    verb("sh", cmd_sh, f"type a command into the shell pane {dash_hint}",
         name, arg("cmd", nargs="+"), json=False)
    verb("jump", cmd_jump, "focus the Claude pane (or --shell)", name, flag("--shell"), json=False)
    verb("open", cmd_open,
         "focus the tab, or open it again (new tab + split, claude --continue) when it is gone",
         name, flag("--shell", "focus the shell pane instead"))
    verb("adopt", cmd_adopt,
         "take a Claude Code tab you opened yourself into a named work "
         "session (no new tab, no worktree; its hook file names the "
         "directory and pane)",
         name,
         arg("--session", required=True, metavar="ID",
             help="that tab's Claude session id (the hook file's name, the dash's row, "
                  "work ls --json claude_session_id)"),
         note,
         arg("--title", help=f"free-text title the board shows {title_cut})"))
    verb("autoname", cmd_autoname,
         "name every Claude Code tab that is not a work session yet from "
         "what it is doing: a headless claude titles an excerpt of its "
         "transcript, the name is derived, the tab is adopted (the board "
         "runs this when it comes up)",
         arg("session", nargs="*", metavar="SESSION_ID", help="only these tabs (default: every unnamed one)"),
         flag("--dry-run", "print the names, adopt nothing"),
         flag("--no-claude", "no headless call: the first prompt's first line is the title"))
    verb("move", cmd_move,
         "where the row sits on the board: --now (the top section, what you are "
         "pushing on), --active or --later, and/or --rank N (order inside a section)",
         name,
         oneof(flag("--now", "the NOW section at the top: what you are pushing on"),
               flag("--active", "the ACTIVE section: what you are on, in the background"),
               flag("--later", "the LATER section")),
         arg("--rank", type=int, metavar="N",
             help="order inside the section, lower first (a new session ranks by its creation time)"))
    verb("rename", cmd_rename,
         "a new name for the record (notes and status travel; the worktree, "
         "the branch and the tab keep the old name)",
         arg("old"), arg("new"))
    verb("status", cmd_status,
         "what that tab's Claude is polling for you: text reports one more "
         "poll, no text reads it, --clear removes it",
         arg("name", nargs="?", help="the session (omit with --here)"),
         arg("text", nargs="*", help="the report, e.g. \"waiting for ctest (run 2 of 3)\" "
                                     "(first line, 200 chars)"),
         flag("--here", "the session this shell runs in (the record whose Claude pane is $ITERM_SESSION_ID, "
                        "else the one record whose worktree is the cwd); every positional is then text"),
         flag("--clear", "remove the report"))
    verb("title", cmd_title,
         "the free-text title the board shows in its WORK column (capitals, "
         "spaces, anything; the name stays the identifier): text sets it, "
         "--clear removes it, neither prints it",
         name,
         arg("text", nargs="*", help=f"the title {title_cut}); text starting with - goes after --"),
         flag("--clear", "remove the title"))
    verb("log", cmd_log,
         "read savannah's log (one file, every piece of savannah writes it): "
         "the last N lines at a level, --follow for new ones",
         arg("--level", choices=savannah_log.READ_LEVELS, default="normal",
             help="normal = INFO and above (default), debug adds DEBUG, verbose adds everything"),
         arg("--lines", type=int, default=LOG_LINES_DEFAULT, metavar="N",
             help=f"how many lines (default {LOG_LINES_DEFAULT})"),
         flag("--follow", "keep printing new lines until Ctrl-C"),
         flag("--path", "print only the log file's path"))
    verb("repos", cmd_repos, "list the git repos in your folder of repos (DIR remembers it)",
         arg("dir", nargs="?"))
    verb("snapshot", cmd_snapshot,
         "save the layout (every work session and open Claude Code tab, "
         "in tab order, with the Claude session ids) under "
         "<work dir>/snapshots, for restore after a reboot",
         arg("--note", default="", help="why, e.g. \"before reboot\""))
    verb("snapshots", cmd_snapshots, "list the saved snapshots, newest first")
    verb("restore", cmd_restore,
         "put a snapshot's tabs back: remake every work session whose tab "
         "is gone (claude --resume its conversation) and reopen the plain "
         "tabs; never closes anything",
         arg("which", nargs="?", default="latest", metavar="STAMP",
             help="a stamp from `snapshots` (default: latest)"),
         flag("--dry-run", "print the plan, touch nothing"),
         flag("--no-plain", "work sessions only, leave the plain tabs"))
    verb("output", cmd_output, "print the last N turns (your prompt, Claude's reply) of that tab",
         name, arg("--last", type=int, default=1, metavar="N"))
    verb("rm", cmd_rm,
         "close tab, remove worktree (never an adopted session's directory), archive notes",
         name, flag("--keep-worktree"), flag("--force", "discard a dirty worktree"), json=False)
    verb("hub", cmd_hub,
         "register the peer shim in DIR/.mcp.json, add a starter "
         "CLAUDE.md, launch claude there (the coordinating session)",
         arg("dir", nargs="?", help="hub directory: your folder of repos "
                                    "(default: the remembered hub, else cwd)"),
         flag("--no-launch", "write the files, do not start claude"))
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)  # argparse exits 2 on bad usage
    t0 = time.time()
    # `log` reads the file: its own lines stay at VERBOSE, so the default
    # view is not a list of the times you looked at it.
    reading = a.verb == "log"
    (log_verbose if reading else log_info)("verb", verb=a.verb,
                                           args=list(sys.argv[1:] if argv is None else argv), cwd=os.getcwd())
    try:
        rc = a.fn(a)
    except UserError as e:
        print(f"{a.verb}: {e}", file=sys.stderr)
        log_warn("verb refused", verb=a.verb, error=str(e), ms=int((time.time() - t0) * 1000))
        return 1
    except Exception as e:  # noqa: BLE001 - logged, then raised as before
        log_error("verb crashed", verb=a.verb, error=repr(e), ms=int((time.time() - t0) * 1000))
        raise
    (log_verbose if reading else log_debug)("verb done", verb=a.verb, rc=rc, ms=int((time.time() - t0) * 1000))
    return rc


if __name__ == "__main__":
    sys.exit(main())
