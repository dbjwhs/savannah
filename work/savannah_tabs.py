# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_tabs - a record and its tab, the steps more than one verb shares
# (savannah_work.py is the entry point; this is a flat sibling module).
#
# claude_command (the line typed into a new Claude pane), reclaim_pane (a
# record pointed at the pane where its Claude was started again by hand),
# remake_tab (the shared half of open and restore) and adopt_session (the
# shared half of adopt and autoname, with release_pane_claims and
# pane_tab_id). They live below the verbs so savannah_snapshot and
# savannah_autoname can use them without importing the entry module.
#
# Imports: the standard library, savannah_log, savannah_core,
# savannah_terminal, savannah_git, savannah_sessions and
# savannah_transcript.

import os
import shlex
import shutil

from savannah_log import log_info, log_warn
import savannah_core
from savannah_core import (append_note, claim_record_dir, list_records, load_record, new_record,
                           record_path, save_record, UserError, valid_name)
from savannah_terminal import make_terminal
from savannah_git import current_branch, resolve_repo_root
from savannah_sessions import claude_alive, hook_pane, hooks_in, join_session, load_hook_sessions
from savannah_transcript import find_transcript


def claude_command(worktree, name, prompt, resume=False, resume_id=""):
    """The line typed into the upper pane: cd into the worktree, start claude
    named after the work session (`-n` shows in the prompt box, the /resume
    picker and the terminal title; no name, no `-n`: a plain tab). resume_id
    reloads that exact conversation (`--resume <id>`, what restore uses);
    resume reloads the directory's latest one (`--continue`). $SAVANNAH_CLAUDE_BIN
    replaces `claude` (a shell fragment, so it may carry its own flags)."""
    cmd = f"cd {shlex.quote(worktree)} && {os.environ.get('SAVANNAH_CLAUDE_BIN') or 'claude'}"
    if name:
        cmd += f" -n {shlex.quote(name)}"
    if resume_id:
        cmd += f" --resume {shlex.quote(resume_id)}"
    elif resume:
        cmd += " --continue"
    if prompt:
        cmd += " " + shlex.quote(prompt)
    return cmd


def reclaim_pane(rec, live, dry=False):
    """A record whose Claude pane is gone, while a live Claude Code session
    runs in its worktree in another pane (the owner closed the tab and
    started claude there again by hand): the record is pointed at that pane,
    so open focuses it, say types into it and restore leaves it alone,
    instead of a second tab being made for a session that is already open.
    The hook file is the evidence (it is removed on SessionEnd) and its pane
    must be in live; a pane another record claims is never taken. The old
    tab id is dropped and so is a shell pane that is gone (the tab made by
    hand has none). -> True when the record was re-pointed (and saved);
    dry = only say whether it would be."""
    if claude_alive(rec, live):
        return False
    worktree = rec.get("worktree") or ""
    if not worktree:
        return False
    taken = {r.get("claude_pane") for r in list_records() if r.get("name") != rec.get("name")}
    best, best_pane = None, ""
    for h in hooks_in(worktree, load_hook_sessions()):
        pane = hook_pane(h)
        if not pane or pane not in live or pane in taken:
            continue
        if best is None or (h.get("updated") or 0) > (best.get("updated") or 0):
            best, best_pane = h, pane
    if best is None:
        return False
    if dry:
        return True
    log_info("pane reclaimed", name=rec["name"], was=rec.get("claude_pane", ""), claude_pane=best_pane,
             session=best.get("session_id") or "", why="a live Claude session runs in the worktree")
    rec["claude_pane"], rec["tab_id"] = best_pane, ""
    if rec.get("shell_pane") and rec["shell_pane"] not in live:
        rec["shell_pane"] = ""
    save_record(rec)
    return True


def remake_tab(rec, term, live, how="", resume_id=""):
    """Open a work session's tab again (a fresh tab and split in its
    worktree) and point the record at it. how says what the Claude pane
    starts with: "resume" (that exact conversation, resume_id), "continue"
    (the worktree's latest), "fresh", or "" to decide between the last two
    by whether the worktree has a transcript. -> the how used. Raises
    UserError when the worktree is gone or it2 fails (nothing is saved then)."""
    name = rec["name"]
    worktree = rec.get("worktree") or ""
    if not worktree or not os.path.isdir(worktree):
        raise UserError(f"worktree of {name} is gone ({worktree or 'unknown'}); rm it")
    leftover = [p for p in (rec.get("shell_pane"),) if p and p in live]
    if leftover:
        term.close_tab(rec.get("tab_id", ""), leftover)  # never two tabs for one task
    if not how:
        how = "continue" if find_transcript(rec, load_hook_sessions())[0] else "fresh"
    cmd = claude_command(worktree, name, "", resume=(how == "continue"),
                         resume_id=(resume_id if how == "resume" else ""))
    ids = term.create_split_tab(name, cmd, f"cd {shlex.quote(worktree)}")
    rec.update(tab_id=ids["tab_id"], claude_pane=ids["claude_pane"], shell_pane=ids["shell_pane"])
    save_record(rec)
    log_info("tab remade", name=name, how=how, tab=ids["tab_id"])
    return how


def release_pane_claims(pane, h, name):
    """Before name adopts hook session h in pane: a record that claims the
    pane AND joins the session is that tab already (UserError). One that
    claims it without joining (the pane now runs a Claude in another
    directory, so that record's own tab is gone) has a stale claim: its
    claude_pane and tab_id are cleared, so `say` and `rm` on the old record
    never reach the new tab, and `open` remakes its own."""
    for other in list_records():
        if other.get("claude_pane") != pane:
            continue
        if join_session(other, [h]) is not None:
            raise UserError(f"that tab is already work session {other['name']}")
        stale = load_record(other["name"])
        stale["claude_pane"], stale["tab_id"] = "", ""
        save_record(stale)
        log_warn("pane released", name=other["name"], claude_pane=pane, to=name,
                 why="the pane runs a Claude in another directory")


def pane_tab_id(term, pane):
    """The iTerm2 tab a pane sits in, "" when it2 cannot say. Best effort:
    the tab is for the board's eye only; nothing acts on it."""
    try:
        for s in term.sessions():
            if s.get("id") == pane:
                return str(s.get("tab_id") or "")
    except UserError:
        pass
    return ""


def adopt_session(name, h, session_id, title="", note=""):
    """The shared half of `adopt` and `autoname`: write the record that
    takes hook session h (its id is session_id) into work session name. The
    hook file is the source of truth: it names the directory the tab runs in
    (which becomes the session's worktree as it is) and its iTerm2 pane (the
    join key). Refuses (UserError, no record left behind) an invalid or taken
    name, a session whose directory is not here, or a pane some record
    claims AND joins (the same rule as the board: the record's worktree is
    the hook's directory); a stale claim on the pane is released
    (release_pane_claims)."""
    if not valid_name(name):
        raise UserError(f"invalid name {name!r}: {savannah_core.NAME_HELP}")
    rdir = claim_record_dir(name)
    try:
        project = h.get("project") if isinstance(h.get("project"), str) else ""
        if not project or not os.path.isdir(project):
            raise UserError(f"session {session_id} runs in {project or 'an unknown directory'}, "
                            "which is not a directory here")
        pane = hook_pane(h)
        if pane:
            release_pane_claims(pane, h, name)
        try:
            repo = resolve_repo_root(project)
        except UserError:
            repo = project  # no git here; the directory is the whole story
        term = make_terminal("")
        tab_id = pane_tab_id(term, pane) if pane else ""
        rec = new_record(name, repo, project, current_branch(project), term.kind, tab_id=tab_id,
                         claude_pane=pane, adopted=True, title=title)
        savannah_core.atomic_write(record_path(name), rec)
        log_info("adopted", name=name, session=session_id, worktree=project, repo=repo,
                 claude_pane=pane, tab=tab_id, title=rec["title"])
        if note:
            append_note(name, note)
    except BaseException:
        shutil.rmtree(rdir, ignore_errors=True)
        raise
    return rec
