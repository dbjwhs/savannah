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
#   move <name> [--active|--later] [--rank N]
#                             where the row sits on the board: the ACTIVE
#                             section or the LATER one, and its order there
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
# Terminal seam: the Terminal class below is the only thing that knows about
# iTerm2. ITerm shells out to iTerm2's own `it2` command ($SAVANNAH_IT2, else
# `it2` on PATH, else the copy inside iTerm2.app) so tests substitute
# tools/fake_it2.py; the AppleScript route it replaced had no tab ids and
# a 60 s Automation dialog (5d). A tmux implementation plugs in later behind
# the same methods. Stdlib only.

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RESERVED_RE = re.compile(r"t-[0-9]+")  # mesh task ids
WORKTREES_SUBDIR = ".savannah-worktrees"
ARCHIVE_SUBDIR = "archive"   # notes graveyard under the work dir; never a session name
SNAPSHOTS_SUBDIR = "snapshots"   # saved layouts (work snapshot) under the work dir; never a session name
SNAPSHOT_LATEST = "latest.json"  # a copy of the newest snapshot, what restore takes by default
LAST_PROMPT_MAX = 200
STATUS_FILE = "status.json"        # the tab's own Claude's poll report, beside record.json
STATUS_TEXT_MAX = 200
TITLE_MAX = 120                    # chars of a session's free-text title (first line, stripped)
PRIORITIES = ("active", "later")   # the board's two sections; rank orders rows within one
OUTPUT_PROMPT_CLIP = 120           # chars of the prompt shown on an output header
HUB_TEMPLATE = "hub_CLAUDE.md"     # sibling of this module; copied into a new hub
SCRATCH_TEMPLATE = "scratch_CLAUDE.md"
SCRATCH_SUBDIR = ".savannah-scratch"   # tabs for tasks that belong to no repo
HUB_SERVER = "peer"                # the mcpServers key the hub registers
# Pre-approved in DIR/.claude/settings.json so the hub never stops at a
# permission prompt for its own tools (headless runs cannot answer one).
HUB_ALLOW = [f"mcp__{HUB_SERVER}__work_{v}" for v in
             ("new", "list", "note", "notes", "say", "output", "shell", "jump", "rm")] + ["Bash(sleep *)"]
IT2_DEFAULT = "/Applications/iTerm.app/Contents/Resources/utilities/it2"  # inside iTerm2 3.5+
WORKTREE_INCLUDE = ".worktreeinclude"   # paths/globs to copy into every new worktree


# --------------------------------------------------------------------------
# logging: savannah's one log (work/savannah_log.py beside this file), the
# file every piece of savannah writes, src=work for this process. A few
# lines per verb: INFO for a state change, DEBUG for lookups and every it2
# or git call, VERBOSE for payloads; `work log` reads them back.

try:
    import savannah_log
except ImportError:  # loaded as a module from elsewhere (the tests do this)
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    import savannah_log
from savannah_log import (VERBOSE, log_dir, log_path, log_verbose, log_debug,  # noqa: E402,F401
                          log_info, log_warn, log_error)

savannah_log.configure("work")


# --------------------------------------------------------------------------
# paths and records


def work_dir():
    d = os.environ.get("SAVANNAH_WORK_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), ".claude", "savannah", "work")


def sessions_dir():
    d = os.environ.get("SAVANNAH_SESSIONS_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), ".claude", "savannah", "sessions")


def record_dir(name):
    return os.path.join(work_dir(), name)


def record_path(name):
    return os.path.join(record_dir(name), "record.json")


def notes_path(name):
    return os.path.join(record_dir(name), "notes.md")


def status_path(name):
    return os.path.join(record_dir(name), STATUS_FILE)


def atomic_write(path, obj):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)  # atomic on POSIX; a reader never sees a half file


def valid_name(name):
    """A name is a single safe path component: fullmatch (no trailing
    newline), no "..", no trailing "." and no ".lock" suffix (git refs)."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        return False
    if RESERVED_RE.fullmatch(name) or name in (ARCHIVE_SUBDIR, SNAPSHOTS_SUBDIR):
        return False
    if ".." in name or name.endswith(".") or name.endswith(".lock"):
        return False
    return True


NAME_HELP = ("want ^[a-z0-9][a-z0-9._-]{0,63}$ (no '..', no trailing '.' or '.lock'), not t-<digits>, "
             "archive or snapshots")


class UserError(Exception):
    """A problem the human caused or can fix: exit 1 with a one-line message."""


RECORD_FIELDS = (
    "name", "repo", "worktree", "branch", "created", "updated", "terminal",
    "tab_id", "claude_pane", "shell_pane", "prompt", "title",
)


def coerce_int(value, default):
    """int(value) for a number or a numeric string, else default (a JSON
    true, null, word or Infinity is not a rank)."""
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def title_text(value):
    """A session's free-text title as stored and shown: its first non-blank
    line, stripped, cut to TITLE_MAX; "" for blank text or a non-string
    (a title is for your eye, so the name rule does not apply to it).
    Not tab_title, which strips iTerm2's " (process)" off a tab title."""
    if not isinstance(value, str):
        return ""
    for line in value.splitlines():
        line = line.strip()
        if line:
            return line[:TITLE_MAX]
    return ""


def normalize_record(rec, name):
    """Fill missing fields and force the name from the directory, never from
    the file (finding 1). `adopted` (a tab the owner opened, taken in by
    `adopt`) is always a real boolean; records written before it existed
    read False. `priority` is "active" unless the file says "later", and
    `rank` is an int: the file's when it parses, else the creation time, so
    a record from before the board had sections keeps sorting by age.
    `title` is always a str (title_text), "" for a record from before it."""
    for key in RECORD_FIELDS:
        rec.setdefault(key, "")
    rec["adopted"] = bool(rec.get("adopted"))
    rec["priority"] = "later" if rec.get("priority") == "later" else "active"
    rec["rank"] = coerce_int(rec.get("rank"), coerce_int(rec.get("created"), 0))
    rec["title"] = title_text(rec.get("title"))
    rec["name"] = name
    return rec


def load_record(name):
    if not valid_name(name):
        raise UserError(f"invalid work session name: {name!r}")
    try:
        with open(record_path(name)) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        raise UserError(f"no such work session: {name}")
    if not isinstance(rec, dict):
        raise UserError(f"no such work session: {name}")
    return normalize_record(rec, name)


def save_record(rec):
    rec["updated"] = int(time.time())
    atomic_write(record_path(rec["name"]), rec)


def list_records():
    root = work_dir()
    out = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    for entry in entries:
        if entry in (ARCHIVE_SUBDIR, SNAPSHOTS_SUBDIR) or not valid_name(entry):
            continue
        try:
            with open(os.path.join(root, entry, "record.json")) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or not rec.get("worktree"):
            continue  # a record without a worktree cannot be joined or removed sanely
        out.append(normalize_record(rec, entry))
    return out


def is_direct_child(path, parent):
    """realpath(path) is exactly one level below realpath(parent)."""
    rp = os.path.realpath(path)
    return os.path.dirname(rp) == os.path.realpath(parent) and rp != os.path.realpath(parent)


def is_inside(path, root):
    """realpath(path) is realpath(root) or lies below it: deleting root would
    take path with it."""
    rp = os.path.realpath(path)
    rr = os.path.realpath(root)
    return rp == rr or rp.startswith(rr.rstrip(os.sep) + os.sep)


# --------------------------------------------------------------------------
# notes


def note_stamp(now=None):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(now))


def append_note(name, text, now=None):
    text = text.rstrip("\n")
    path = notes_path(name)
    with open(path, "a") as f:
        f.write(f"## {note_stamp(now)} local\n{text}\n\n")
    return path


def read_notes(name):
    try:
        with open(notes_path(name)) as f:
            return f.read()
    except OSError:
        return ""


NOTE_HEADER_RE = re.compile(r"^## \d{4}-\d{2}-\d{2} \d{2}:\d{2} local$")


def last_note_line(text):
    """First non-empty line of the most recent note body, or ""."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if NOTE_HEADER_RE.match(line):
            start = i
    if start is None:
        return ""
    for line in lines[start + 1:]:
        if line.strip():
            return line.strip()
    return ""


def archive_target(adir, name, created):
    """archive/<name>-<created>.md, or -2, -3, ... when that file exists.
    Never overwrites (finding 9)."""
    base = os.path.join(adir, f"{name}-{created}")
    cand = base + ".md"
    n = 2
    while os.path.exists(cand):
        cand = f"{base}-{n}.md"
        n += 1
    return cand


# --------------------------------------------------------------------------
# terminal seam


def it2_argv():
    """argv prefix for iTerm2's own `it2` command: $SAVANNAH_IT2 (split like a
    shell line), else `it2` on PATH, else the copy inside iTerm2.app."""
    override = os.environ.get("SAVANNAH_IT2", "")
    if override:
        argv = shlex.split(override)
        if not argv or not argv[0]:
            raise UserError("SAVANNAH_IT2 is empty")
        return argv
    return [shutil.which("it2") or IT2_DEFAULT]


class Terminal:
    """The seam. One implementation today (ITerm); tmux is reserved."""

    kind = ""

    def create_split_tab(self, name, upper_cmd, lower_cmd):
        """-> {"tab_id", "claude_pane", "shell_pane"}; raises UserError."""
        raise NotImplementedError

    def create_tab(self, name, cmd):
        """A single-pane tab running cmd (a plain tab put back by restore).
        -> {"tab_id", "pane"}; raises UserError."""
        raise NotImplementedError

    def write(self, pane_id, text):
        """Type text (plus Enter) into a pane. -> bool found."""
        raise NotImplementedError

    def focus(self, pane_id):
        """Bring a pane's window/tab/pane forward. -> bool found."""
        raise NotImplementedError

    def close_tab(self, tab_id, panes=()):
        """Close the tab. -> bool found. panes: the tab's known pane ids."""
        raise NotImplementedError

    def live_ids(self):
        """The set of pane ids the terminal currently has; raises UserError."""
        raise NotImplementedError


class ITerm(Terminal):
    """iTerm2 driven through its own `it2` command (ships inside iTerm2 3.5+
    and needs the Python API switched on: iTerm2 > Settings > General >
    Magic). Each verb is one or more it2 calls, about a quarter second each.
    The output lines parsed here are the contract, pinned live on 11/3/2026
    against iTerm2 3.7.6 / it2 1.0.0 and mirrored by tools/fake_it2.py."""

    kind = "iterm"

    def run(self, args, timeout=60):
        argv = it2_argv() + list(args)
        t0 = time.time()
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            log_warn("it2 not found", it2=argv[0])
            raise UserError(f"it2 not found ({argv[0]!r}); it ships with iTerm2 3.5+ "
                            "(set SAVANNAH_IT2 to its path)")
        except subprocess.TimeoutExpired:
            log_warn("it2 timed out", args=list(args), timeout=timeout)
            raise UserError("it2 timed out: iTerm2 is not responding, or its Python API is "
                            "off (iTerm2 > Settings > General > Magic > Enable Python API)")
        except OSError as e:
            log_warn("it2 failed to start", it2=argv[0], error=e.strerror or str(e))
            raise UserError(f"cannot run it2 ({argv[0]!r}): {e.strerror or e}")
        ms = int((time.time() - t0) * 1000)
        # DEBUG for every call (what, how it ended, how long), VERBOSE for what
        # it printed: `session list` JSON is big, so only its head.
        log_debug("it2", args=list(args), rc=p.returncode, ms=ms)
        if p.stdout or p.stderr:
            log_verbose("it2 output", args=list(args)[:2], stdout=p.stdout[:300], stderr=p.stderr[:200])
        return p

    @staticmethod
    def _err(p):
        lines = [ln for ln in p.stderr.strip().splitlines() if ln.strip()]
        return lines[-1] if lines else f"rc {p.returncode}"

    @staticmethod
    def _not_found(p):
        # "Error: Session not found" (rc 3), "Error: Session 'X' not found"
        # (rc 3), "Set name failed: Object with identifier ... not found" (rc 1).
        return p.returncode != 0 and "not found" in p.stderr.lower()

    def ok(self, args, what):
        """Run and insist on exit 0; anything else is a UserError."""
        p = self.run(args)
        if p.returncode != 0:
            raise UserError(f"it2 {what} failed: {self._err(p)}")
        return p

    @staticmethod
    def _json(p, what):
        try:
            data = json.loads(p.stdout or "[]")
        except ValueError:
            raise UserError(f"it2 {what} printed something that is not JSON")
        return [d for d in data if isinstance(d, dict) and d.get("id")] if isinstance(data, list) else []

    def sessions(self):
        return self._json(self.ok(["session", "list", "--json"], "session list"), "session list")

    def live_ids(self):
        return {s["id"] for s in self.sessions()}

    def pick_window(self, sessions):
        """The window the new tab goes in. it2 has no "current window" when
        called from a script, so it is named explicitly: the window this
        process runs in (ITERM_SESSION_ID) when it is still there, else the
        first listed session's, else the first window, else "" (none)."""
        mine = iterm_uuid(os.environ.get("ITERM_SESSION_ID", ""))
        for s in sessions:
            if mine and s.get("id") == mine and s.get("window_id"):
                return s["window_id"]
        for s in sessions:
            if s.get("window_id"):
                return s["window_id"]
        for w in self._json(self.ok(["window", "list", "--json"], "window list"), "window list"):
            return w["id"]
        return ""

    @staticmethod
    def _created(p, what):
        """'Created new tab: 6' / 'Created new pane: <uuid>' / 'Created new window: <id>'."""
        m = re.search(r"Created new \w+: (\S+)", p.stdout)
        if not m:
            raise UserError(f"it2 {what} printed no id ({p.stdout.strip()!r})")
        return m.group(1)

    def _new_tab(self):
        """A fresh tab in the window pick_window names. -> (tab id, the id of
        the pane it opened with)."""
        window = self.pick_window(self.sessions())
        if window:
            tab = self._created(self.ok(["tab", "new", "--window", window], "tab new"), "tab new")
            fresh = [s for s in self.sessions()
                     if s.get("tab_id") == tab and s.get("window_id") == window]
        else:
            # No window at all: the new window's own tab is the work tab, so no
            # stray empty tab is left behind.
            window = self._created(self.ok(["window", "new"], "window new"), "window new")
            fresh = [s for s in self.sessions() if s.get("window_id") == window]
            tab = fresh[0].get("tab_id", "") if fresh else ""
        if not fresh:
            raise UserError("it2 created the tab but session list does not show it")
        return tab, fresh[0]["id"]

    def create_tab(self, name, cmd):
        # One pane, no split: the shape of a tab the owner opened by hand,
        # which is what restore puts back. The leading space and the
        # best-effort set-name are explained in create_split_tab.
        tab, cs = self._new_tab()
        try:
            self.ok(["session", "run", "--session", cs, "--", " " + cmd], "run")
            self.ok(["session", "set-name", "--session", cs, "--", name], "set-name")
        except BaseException:
            try:
                self.close_tab(tab, (cs,))
            except Exception:
                pass
            raise
        return {"tab_id": tab, "pane": cs}

    def create_split_tab(self, name, upper_cmd, lower_cmd):
        tab, cs = self._new_tab()
        ss = ""
        try:
            # The line is typed before the shell is up and waits in the tty
            # queue. A shell that reads a key while starting eats the first
            # character (oh-my-zsh's update prompt turned `cd` into `d`, live
            # 11/3/2026), so the line starts with a space nothing will miss;
            # zsh's hist_ignore_space keeps it out of history as a bonus.
            self.ok(["session", "run", "--session", cs, "--", " " + upper_cmd], "run")
            # After the run: the shell's first prompt resets the title (auto
            # title), so a name set before it is gone at once. Best effort
            # anyway; `claude -n <name>` names the tab while Claude runs.
            self.ok(["session", "set-name", "--session", cs, "--", name], "set-name")
            ss = self._created(self.ok(["session", "split", "--session", cs], "split"), "split")
            self.ok(["session", "run", "--session", ss, "--", " " + lower_cmd], "run")
        except BaseException:
            # Any failure after the tab exists closes it so no orphan survives.
            try:
                self.close_tab(tab, (cs, ss))
            except Exception:
                pass
            raise
        return {"tab_id": tab, "claude_pane": cs, "shell_pane": ss}

    def write(self, pane_id, text):
        # Two sends, never one `run`: `run` ends the text with a newline (LF),
        # which Claude Code's input box takes as a line break, not as Enter
        # (found live 11/3/2026: the text sat unsent in the box). A carriage
        # return is what the Enter key sends, and a shell accepts it too.
        # "--" so text that starts with "-" is text, never an it2 option.
        for payload in (text, "\r"):
            p = self.run(["session", "send", "--session", pane_id, "--", payload])
            if p.returncode == 0:
                continue
            if self._not_found(p):
                return False
            raise UserError(f"it2 send failed: {self._err(p)}")
        return True

    def focus(self, pane_id):
        p = self.run(["session", "focus", pane_id])
        if p.returncode != 0:
            if self._not_found(p):
                return False
            raise UserError(f"it2 focus failed: {self._err(p)}")
        self.run(["app", "activate"])  # best effort: bring iTerm2 forward too
        return True

    def close_tab(self, tab_id, panes=()):
        # `it2 tab close` answers "Tab closed" without closing anything (found
        # live 11/3/2026, it2 1.0.0), so the tab goes pane by pane; closing
        # the last pane closes the tab (and an otherwise empty window).
        found = False
        for pane in panes:
            if not pane:
                continue
            p = self.run(["session", "close", "--session", pane, "--force"])
            if p.returncode == 0:
                found = True
            elif not self._not_found(p):
                raise UserError(f"it2 close failed: {self._err(p)}")
        return found


def make_terminal(kind):
    if kind in ("iterm", ""):
        return ITerm()
    raise UserError(f"terminal {kind!r} is not implemented (only iterm today)")


# --------------------------------------------------------------------------
# git


def run_git(args):
    t0 = time.time()
    try:
        p = subprocess.run(["git"] + args, capture_output=True, text=True)
    except FileNotFoundError:
        log_warn("git not found")
        raise UserError("git not found on PATH")
    except OSError as e:
        log_warn("git failed to start", error=e.strerror or str(e))
        raise UserError(f"cannot run git: {e.strerror or e}")
    log_debug("git", args=list(args), rc=p.returncode, ms=int((time.time() - t0) * 1000))
    if p.returncode != 0 and p.stderr:
        log_verbose("git stderr", args=list(args)[:3], stderr=p.stderr[:300])
    return p


def git_fail_message(p, what):
    """Prefer git's own fatal:/error: line over trailing hint: chatter."""
    lines = [ln for ln in p.stderr.strip().splitlines() if ln.strip()]
    pick = ""
    for ln in lines:
        if ln.startswith("fatal:") or ln.startswith("error:"):
            pick = ln
    if not pick:
        for ln in lines:
            if not ln.startswith("hint:"):
                pick = ln
    if not pick:
        pick = lines[-1] if lines else f"rc {p.returncode}"
    return f"{what} failed: {pick}"


def git_branch_exists(repo, branch):
    p = run_git(["-C", repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"])
    return p.returncode == 0


def resolve_repo_root(d):
    """The MAIN worktree root of the repository containing d (finding 12).

    git-common-dir is the shared .git of the main worktree even when d is a
    linked worktree or a subdirectory, so a work session can never nest inside
    another session's worktree. Falls back to --show-toplevel when the common
    dir is not a plain ".git" (separate git dir, bare repo).
    """
    p = run_git(["-C", d, "rev-parse", "--git-common-dir"])
    if p.returncode != 0:
        raise UserError(f"not a git repository: {d}")
    common = p.stdout.strip()
    if common and not os.path.isabs(common):
        common = os.path.join(d, common)
    common = os.path.realpath(common)
    if os.path.basename(common) == ".git":
        root = os.path.dirname(common)
    else:
        p = run_git(["-C", d, "rev-parse", "--show-toplevel"])
        if p.returncode != 0 or not p.stdout.strip():
            raise UserError(f"cannot find the worktree root of {d}")
        root = os.path.realpath(p.stdout.strip())
    # Keep the caller's spelling when it already names the root (realpath may
    # only differ by a symlinked prefix such as /var -> /private/var).
    if os.path.realpath(d) == root:
        return d
    return root


# --------------------------------------------------------------------------
# join with hook sessions


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


def iterm_uuid(iterm_session_id):
    s = iterm_session_id or ""
    return s.split(":", 1)[1] if ":" in s else s


def join_session(rec, hooks):
    worktree = rec.get("worktree") or ""
    if not worktree:
        return None  # realpath("") is the cwd, which would join by accident
    target = os.path.realpath(worktree)
    matches = [h for h in hooks
               if h.get("project") and os.path.realpath(h["project"]) == target]
    name = rec.get("name", "")
    if not matches:
        log_debug("join", name=name, worktree=worktree, matches=0)
        return None
    for h in matches:
        if rec.get("claude_pane") and iterm_uuid(h.get("iterm_session_id")) == rec["claude_pane"]:
            log_debug("join", name=name, session=h.get("session_id") or "", state=h.get("state") or "",
                      by="pane", matches=len(matches))
            return h
    h = max(matches, key=lambda h: h.get("updated") or 0)
    log_debug("join", name=name, session=h.get("session_id") or "", state=h.get("state") or "",
              by="newest", matches=len(matches))
    return h


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


# --------------------------------------------------------------------------
# status: what the tab's own Claude reports it is polling for the owner


def read_status(name):
    """The session's self-reported poll status, or None when there is none
    (or the file is unreadable): {"text", "updated", "count"}, each field
    normalized so a reader never meets a missing or oddly typed one."""
    try:
        with open(status_path(name)) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(st, dict):
        return None
    text = st.get("text")
    return {"text": text if isinstance(text, str) else "",
            "updated": coerce_int(st.get("updated"), 0),
            "count": coerce_int(st.get("count"), 0)}


def status_text(parts):
    """First non-blank line of the joined text args, cut to STATUS_TEXT_MAX."""
    for line in " ".join(parts).splitlines():
        line = line.strip()
        if line:
            return line[:STATUS_TEXT_MAX]
    return ""


def session_here():
    """The work session this process runs inside: first the record whose
    Claude pane is this iTerm2 session ($ITERM_SESSION_ID, which the Bash
    that Claude Code runs inherits), else the one record whose worktree is
    the cwd. Several records on this directory with no pane match is
    ambiguous, none is an error; both messages name what to do."""
    recs = list_records()
    pane = iterm_uuid(os.environ.get("ITERM_SESSION_ID", ""))
    if pane:
        for r in recs:
            if r.get("claude_pane") == pane:
                return r
    cwd = os.getcwd()
    here = os.path.realpath(cwd)
    matches = [r for r in recs if os.path.realpath(r["worktree"]) == here]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise UserError("several work sessions share this directory: "
                        + ", ".join(sorted(r["name"] for r in matches)) + " (name one)")
    raise UserError(f"no work session for {cwd}")


# --------------------------------------------------------------------------
# transcripts (the read side of say)


def claude_config_dir():
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    if d:
        return d
    return os.path.join(os.path.expanduser("~"), ".claude")


def encode_project_dir(path):
    """Claude Code names ~/.claude/projects/<dir> after the cwd with every
    non-alphanumeric character replaced by '-' (so '/a/b.c' -> '-a-b-c').
    Observed, not promised: the hook's transcript_path is the primary source
    and this is only the fallback for a tab whose hook file is gone."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def find_transcript(rec, hooks):
    """(path, source) of the transcript for a work session, or ("", "").
    source is "hook" (the live hook file named it) or "projects" (newest
    .jsonl in Claude Code's own per-project directory for the worktree)."""
    name = rec.get("name", "")
    h = join_session(rec, hooks)
    p = (h or {}).get("transcript_path") or ""
    if isinstance(p, str) and p and os.path.isfile(p):
        log_debug("transcript", name=name, source="hook", path=p)
        return p, "hook"
    worktree = rec.get("worktree") or ""
    if not worktree:
        log_debug("transcript", name=name, source="", why="no worktree")
        return "", ""
    d = os.path.join(claude_config_dir(), "projects",
                     encode_project_dir(os.path.realpath(worktree)))
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        log_debug("transcript", name=name, source="", why="no projects dir", dir=d)
        return "", ""
    if not names:
        log_debug("transcript", name=name, source="", why="no .jsonl", dir=d)
        return "", ""
    newest = max(names, key=lambda n: os.path.getmtime(os.path.join(d, n)))
    log_debug("transcript", name=name, source="projects", path=os.path.join(d, newest), candidates=len(names))
    return os.path.join(d, newest), "projects"


def _text_blocks(content):
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                out.append(t)
    return out


def _human_prompt(content):
    """The text of a human turn, or None for a tool_result carrier."""
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    parts = _text_blocks(content)
    return "\n".join(parts) if parts else None


def transcript_turns(path):
    """Turns of a Claude Code transcript: [{prompt, text, asked, answered}].
    A turn starts at each human prompt (type user whose content is not a
    tool_result) and collects the text blocks of every assistant message
    until the next prompt; thinking and tool_use blocks are dropped,
    sidechain (subagent) lines are skipped, unparsable lines are ignored."""
    turns = []
    cur = None
    lines = 0
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError as e:
        log_warn("transcript unreadable", path=path, error=e.strerror or str(e))
        raise UserError(f"cannot read transcript {path}: {e.strerror or e}")
    with f:
        for line in f:
            lines += 1
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("isSidechain"):
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            kind = d.get("type")
            ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) else ""
            if kind == "user":
                prompt = _human_prompt(msg.get("content"))
                if prompt is None:
                    continue
                cur = {"prompt": prompt, "text": "", "asked": ts, "answered": ""}
                turns.append(cur)
            elif kind == "assistant":
                parts = _text_blocks(msg.get("content"))
                if not parts:
                    continue
                if cur is None:
                    cur = {"prompt": "", "text": "", "asked": "", "answered": ""}
                    turns.append(cur)
                joined = "\n\n".join(parts)
                cur["text"] = (cur["text"] + "\n\n" + joined) if cur["text"] else joined
                cur["answered"] = ts or cur["answered"]
    log_verbose("transcript parsed", path=path, lines=lines, turns=len(turns))
    return turns


def local_stamp(iso):
    """'2026-10-30T01:33:10.326Z' -> '2026-10-29 18:33 local'; '' stays ''."""
    if not iso:
        return ""
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M local")
    except ValueError:
        return iso


# --------------------------------------------------------------------------
# hub (a directory of repos where a coordinating Claude runs)


def savannah_root():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def hub_server_entry():
    """The mcpServers entry for the peer shim, with absolute paths so it works
    from any directory (the repo's own .mcp.json uses relative ones)."""
    root = savannah_root()
    entry = {"command": "python3",
             "args": [os.path.join(root, "shim", "peer.py")]}
    env = {}
    for key, rel in (("SAVANNAH_BIN", ("build", "savannah")),
                     ("SAVANNAH_KEY", ("build", "mesh.key"))):
        path = os.path.join(root, *rel)
        if os.path.isfile(path):
            env[key] = path
    if env:
        entry["env"] = env
    return entry


def template_path(name):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), name)


def template_text(name):
    path = template_path(name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise UserError(f"template missing: {path} ({e.strerror or e})")


def hub_file():
    f = os.environ.get("SAVANNAH_HUB_FILE")
    if f:
        return f
    return os.path.join(os.path.expanduser("~"), ".claude", "savannah", "hub.json")


def recorded_hub():
    """The remembered hub directory, or "" (no file, unreadable, not a dir)."""
    try:
        with open(hub_file(), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return ""
    d = rec.get("dir") if isinstance(rec, dict) else None
    return d if isinstance(d, str) and os.path.isdir(d) else ""


def remember_hub(d):
    path = hub_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, {"dir": d, "updated": int(time.time())})
    return path


def write_hub_settings(path):
    """Merge the hub's permissions into DIR/.claude/settings.json: allow the
    work_* tools and `sleep`, and auto-approve the project's .mcp.json
    servers. Everything else in the file is kept. 'created', 'updated' or
    'unchanged'."""
    cfg = {}
    status = "created"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except ValueError as e:
            raise UserError(f"refusing to rewrite malformed {path}: {e}")
        except OSError as e:
            raise UserError(f"cannot read {path}: {e.strerror or e}")
        if not isinstance(cfg, dict):
            raise UserError(f"refusing to rewrite {path}: top level is not an object")
        status = "updated"
    changed = False
    if cfg.get("enableAllProjectMcpServers") is not True:
        cfg["enableAllProjectMcpServers"] = True
        changed = True
    perms = cfg.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        cfg["permissions"] = perms
        changed = True
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
        perms["allow"] = allow
        changed = True
    for rule in HUB_ALLOW:
        if rule not in allow:
            allow.append(rule)
            changed = True
    if not changed:
        return "unchanged"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write(path, cfg)
    return status


def write_if_missing(path, text):
    """'created' or 'kept'; never overwrites."""
    if os.path.exists(path):
        return "kept"
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return "created"


def write_hub_mcp(path):
    """Merge the peer server into DIR/.mcp.json. Other servers are kept.
    Returns 'created', 'updated' or 'unchanged'."""
    cfg = {}
    status = "created"
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except ValueError as e:
            raise UserError(f"refusing to rewrite malformed {path}: {e}")
        except OSError as e:
            raise UserError(f"cannot read {path}: {e.strerror or e}")
        if not isinstance(cfg, dict):
            raise UserError(f"refusing to rewrite {path}: top level is not an object")
        status = "updated"
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        cfg["mcpServers"] = servers
    entry = hub_server_entry()
    if servers.get(HUB_SERVER) == entry:
        return "unchanged"
    servers[HUB_SERVER] = entry
    atomic_write(path, cfg)
    return status


# --------------------------------------------------------------------------
# verbs



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


def copy_worktree_includes(repo, worktree):
    """Copy what <repo>/.worktreeinclude names into a fresh worktree: one path
    or glob per line relative to the repo root (`.env`, `config/*.local`,
    `**/.secrets`), `#` comments and blank lines ignored. Meant for the
    gitignored files a worktree needs to run. Not gitignore syntax: no
    negation, no trailing-slash rules. Returns the relative paths copied;
    an existing destination is left alone."""
    try:
        with open(os.path.join(repo, WORKTREE_INCLUDE), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    root = os.path.realpath(repo)
    copied = []
    for raw in lines:
        pat = raw.strip()
        if not pat or pat.startswith("#"):
            continue
        for src in sorted(glob.glob(os.path.join(root, pat.lstrip("/")), recursive=True)):
            rel = os.path.relpath(os.path.realpath(src) if not os.path.islink(src) else src, root)
            top = rel.split(os.sep)[0]
            if rel.startswith("..") or top in (".git", WORKTREES_SUBDIR):
                continue
            dst = os.path.join(worktree, rel)
            if os.path.lexists(dst):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst, follow_symlinks=False)
            except OSError as e:
                raise UserError(f"{WORKTREE_INCLUDE}: cannot copy {rel}: {e.strerror or e}")
            copied.append(rel)
    return copied


def repos_in(d):
    """Subdirectories of d that are git repositories (a .git dir or file),
    sorted, dotfiles skipped."""
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        raise UserError(f"cannot list {d}: {e.strerror or e}")
    return [n for n in names if not n.startswith(".")
            and os.path.exists(os.path.join(d, n, ".git"))]


def usage_error(verb, msg):
    print(f"{verb}: {msg}", file=sys.stderr)
    return 2


def claim_record_dir(name):
    """os.mkdir(record_dir) claims the name before any git or terminal work,
    so two concurrent `new <name>` cannot both proceed (finding 10). A
    directory with no record.json is a stale half-claim: reclaim it."""
    rdir = record_dir(name)
    for _attempt in (1, 2):
        try:
            os.makedirs(os.path.dirname(rdir), exist_ok=True)
            os.mkdir(rdir)
            return rdir
        except FileExistsError:
            if os.path.exists(record_path(name)):
                raise UserError(f"work session already exists: {name}")
            try:
                shutil.rmtree(rdir)
            except OSError as e:
                raise UserError(f"cannot reclaim stale {rdir}: {e.strerror or e}")
        except OSError as e:
            raise UserError(f"cannot create {rdir}: {e.strerror or e}")
    raise UserError(f"work session already exists: {name}")


def cmd_new(a):
    name = a.name
    if not valid_name(name):
        raise UserError(f"invalid name {name!r}: {NAME_HELP}")
    if a.branch and a.no_worktree:
        return usage_error("new", "--branch needs a worktree to check it out in; drop --no-worktree")
    if a.branch is not None and not a.branch.strip():
        return usage_error("new", "--branch needs a branch name")

    repo = os.path.abspath(a.repo or os.getcwd())
    if not os.path.isdir(repo):
        raise UserError(f"repo is not a directory: {repo}")

    rdir = claim_record_dir(name)
    worktree = repo
    branch = ""
    made_worktree = False
    made_branch = False
    reattached = False
    copied = []
    term = ITerm()
    ids = None
    try:
        if not a.no_worktree:
            repo = resolve_repo_root(repo)
            base = os.path.join(repo, WORKTREES_SUBDIR)
            worktree = os.path.join(base, name)
            try:
                os.makedirs(base, exist_ok=True)
            except OSError as e:
                raise UserError(f"cannot create {base}: {e.strerror}")
            if a.branch:
                branch = a.branch
                if not git_branch_exists(repo, branch):
                    raise UserError(f"branch {branch} does not exist locally")
                p = run_git(["-C", repo, "worktree", "add", worktree, branch])
            else:
                branch = f"work/{name}"
                if git_branch_exists(repo, branch):
                    # rm keeps the branch (it is your work); a new session of the
                    # same name picks it up where it was left.
                    reattached = True
                    p = run_git(["-C", repo, "worktree", "add", worktree, branch])
                else:
                    p = run_git(["-C", repo, "worktree", "add", "-b", branch, worktree])
                    made_branch = p.returncode == 0
            if p.returncode != 0:
                raise UserError(git_fail_message(p, "git worktree add"))
            made_worktree = True
            log_debug("worktree added", name=name, worktree=worktree, branch=branch,
                      reattached=reattached, new_branch=made_branch)

        prompt = a.prompt or ""
        if made_worktree:
            copied = copy_worktree_includes(repo, worktree)
        ids = term.create_split_tab(name, claude_command(worktree, name, prompt),
                                    f"cd {shlex.quote(worktree)}")

        now = int(time.time())
        rec = {
            "name": name,
            "repo": repo,
            "worktree": worktree,
            "branch": branch,
            "created": now,
            "updated": now,
            "terminal": term.kind,
            "tab_id": ids.get("tab_id", ""),
            "claude_pane": ids.get("claude_pane", ""),
            "shell_pane": ids.get("shell_pane", ""),
            "prompt": prompt,
            "adopted": False,
            "priority": "active",
            "rank": now,
            "title": title_text(getattr(a, "title", "")),
        }
        atomic_write(record_path(name), rec)
        log_info("new", name=name, repo=repo, worktree=worktree, branch=branch, tab=rec["tab_id"],
                 claude_pane=rec["claude_pane"], shell_pane=rec["shell_pane"], reattached=reattached,
                 copied=len(copied), title=rec["title"])
        if a.note:
            append_note(name, a.note)
    except BaseException:
        # Nothing of ours should survive a failed new, whatever failed: the
        # claimed record dir, the fresh worktree (nothing in it yet), the
        # branch we just made, and the tab if it already exists (a record
        # write failing after the tab is up would otherwise orphan it). A
        # reattached or --branch branch is left alone.
        log_warn("new rolled back", name=name, tab=bool(ids), worktree=made_worktree, branch=made_branch)
        try:
            if ids:
                try:
                    term.close_tab(ids.get("tab_id", ""),
                                   (ids.get("claude_pane", ""), ids.get("shell_pane", "")))
                except Exception:
                    pass
            if made_worktree:
                run_git(["-C", repo, "worktree", "remove", "--force", worktree])
            if made_branch:
                run_git(["-C", repo, "branch", "-D", branch])
        finally:
            shutil.rmtree(rdir, ignore_errors=True)
        raise

    if reattached:
        print(f"new: reattached existing branch {branch}", file=sys.stderr)
    if copied:
        print(f"new: copied {len(copied)} from {WORKTREE_INCLUDE}: {', '.join(copied)}", file=sys.stderr)
    if a.json:
        print(json.dumps(rec))
    else:
        where = f"{worktree} on {branch}" if branch else worktree
        tag = " (reattached)" if reattached else ""
        print(f"{name}: {where}{tag} (tab {rec['tab_id']}, claude {rec['claude_pane']})")
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
        e["pane_alive"] = None if live is None else bool(e.get("claude_pane") and e["claude_pane"] in live)
    log_debug("ls", records=len(entries), hooks=len(hooks),
              joined=sum(1 for e in entries if e["claude_session_id"]),
              live=("null" if live is None else len(live)))
    if a.json:
        print(json.dumps(entries))
        return 0
    if not entries:
        print("(no work sessions)")
        return 0

    def clip(s, n):
        s = (s or "").splitlines()[0] if (s or "").strip() else ""
        return s if len(s) <= n else s[: n - 1] + "~"

    def status_cell(st):
        if not st:
            return "-"
        count = f" ({st['count']})"
        return clip(st["text"], 30 - len(count)) + count

    rows = [("NAME", "TITLE", "BRANCH", "STATE", "STATUS", "LAST PROMPT", "LAST NOTE")]
    for e in entries:
        rows.append((e["name"], clip(e["title"], 24) or "-", e["branch"] or "-", e["state"],
                     status_cell(e["status"]), clip(e["last_prompt"], 40), clip(e["last_note"], 40)))
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(6)) + "  " + r[6])
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
    if a.json:
        print(json.dumps({"name": a.name, "notes_path": path,
                          "time": note_stamp(now), "text": text.rstrip("\n")}))
    else:
        print(f"noted in {path}")
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
        log_warn("pane gone", name=rec["name"], pane=key, session=pane)
        raise UserError(f"{key.replace('_', ' ')} of {rec['name']} not found")
    log_info("focused", name=rec["name"], pane=key, session=pane)
    return 0


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


def cmd_open(a):
    """Focus the tab if it is still there, else open it again: a fresh tab and
    split in the same worktree, claude resuming the worktree's last
    conversation when one exists. The way back after an iTerm2 restart."""
    rec = load_record(a.name)
    term = make_terminal(rec.get("terminal", ""))
    live = term.live_ids()
    claude_alive = bool(rec.get("claude_pane")) and rec["claude_pane"] in live
    if claude_alive:
        pane = rec["claude_pane"]
        if a.shell and rec.get("shell_pane") in live:
            pane = rec["shell_pane"]
        if not term.focus(pane):
            raise UserError(f"pane of {a.name} vanished while focusing it")
        action = "focused"
    else:
        how = remake_tab(rec, term, live)
        action = "reopened (claude --continue)" if how == "continue" else "reopened"
    if a.json:
        print(json.dumps({"name": a.name, "action": action.split(" ")[0], "tab_id": rec.get("tab_id", ""),
                          "claude_pane": rec.get("claude_pane", ""), "shell_pane": rec.get("shell_pane", "")}))
    else:
        print(f"{a.name}: {action} (tab {rec.get('tab_id', '')}, claude {rec.get('claude_pane', '')})")
    return 0


def hook_session_by_id(session_id):
    for h in load_hook_sessions():
        if h.get("session_id") == session_id:
            return h
    return None


def current_branch(d):
    """`git branch --show-current` in d, "" on any failure (informational
    only: an adopted session never checks anything out)."""
    try:
        p = run_git(["-C", d, "branch", "--show-current"])
    except UserError:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def adopt_session(name, h, session_id, title="", note=""):
    """The shared half of `adopt` and `autoname`: write the record that
    takes hook session h (its id is session_id) into work session name. The
    hook file is the source of truth: it names the directory the tab runs in
    (which becomes the session's worktree as it is) and its iTerm2 pane (the
    join key). Refuses (UserError, no record left behind) an invalid or taken
    name, a session whose directory is not here, or a pane some record
    already claims."""
    if not valid_name(name):
        raise UserError(f"invalid name {name!r}: {NAME_HELP}")
    rdir = claim_record_dir(name)
    try:
        project = h.get("project") if isinstance(h.get("project"), str) else ""
        if not project or not os.path.isdir(project):
            raise UserError(f"session {session_id} runs in {project or 'an unknown directory'}, "
                            "which is not a directory here")
        pane = iterm_uuid(h.get("iterm_session_id") if isinstance(h.get("iterm_session_id"), str) else "")
        if pane:
            for other in list_records():
                if other.get("claude_pane") == pane:
                    raise UserError(f"that tab is already work session {other['name']}")
        try:
            repo = resolve_repo_root(project)
        except UserError:
            repo = project  # no git here; the directory is the whole story
        tab_id = ""
        if pane:
            # Best effort: the tab is for the board's eye only; nothing acts on it.
            try:
                for s in ITerm().sessions():
                    if s.get("id") == pane:
                        tab_id = str(s.get("tab_id") or "")
                        break
            except UserError:
                pass
        now = int(time.time())
        rec = {
            "name": name,
            "repo": repo,
            "worktree": project,
            "branch": current_branch(project),
            "created": now,
            "updated": now,
            "terminal": ITerm.kind,
            "tab_id": tab_id,
            "claude_pane": pane,
            "shell_pane": "",
            "prompt": "",
            "adopted": True,
            "priority": "active",
            "rank": now,
            "title": title_text(title),
        }
        atomic_write(record_path(name), rec)
        log_info("adopted", name=name, session=session_id, worktree=project, repo=repo,
                 claude_pane=pane, tab=tab_id, title=rec["title"])
        if note:
            append_note(name, note)
    except BaseException:
        shutil.rmtree(rdir, ignore_errors=True)
        raise
    return rec


def cmd_adopt(a):
    """Take a Claude Code tab you opened yourself into a named work session:
    no new tab, no worktree, just the record that gives it notes, say,
    output, open, jump and rm (adopt_session)."""
    if not valid_name(a.name):
        raise UserError(f"invalid name {a.name!r}: {NAME_HELP}")
    h = hook_session_by_id(a.session)
    if h is None:
        raise UserError(f"no live Claude session {a.session} (no hook file under {sessions_dir()}; "
                        "hooks/install.py not run, or the tab is closed)")
    rec = adopt_session(a.name, h, a.session, title=getattr(a, "title", ""), note=a.note)
    if a.json:
        print(json.dumps(rec))
    else:
        print(f"{a.name}: adopted {rec['worktree']} (tab {rec['tab_id'] or '-'}, claude {rec['claude_pane'] or '-'})")
    return 0


# --------------------------------------------------------------------------
# autoname: a name for every tab you opened yourself, from what it is doing
#
# The board asks for this when it comes up (and for each new plain tab once
# it has a prompt): every hook session no record claims gets a title and a
# name, then is adopted like a Ctrl-R would. The title comes from a headless
# Claude reading an excerpt of the tab's own transcript (its first prompts,
# the latest prompt, the head of the latest reply), never by typing into the
# live session: a question typed into a tab would land as a prompt (or as
# the answer to a permission prompt), spend that session's context and
# tokens, and interrupt whatever it is doing. The excerpt is what the tab
# knows; a fresh small model names it in a few seconds for a fraction of a
# cent. When the call fails (no claude, no network, a timeout) the first
# prompt's first line is the title instead, and the result says so.

NAME_SYSTEM_PROMPT = (
    "You name programming work sessions for a board that lists many of them. You are given "
    "excerpts of one Claude Code conversation: its directory, its first prompts, its latest "
    "prompt and the start of the latest reply. Answer with ONE line: a title of two to five "
    "words in Title Case naming the task being worked on (the what, not the who or the how). "
    "No quotes, no trailing punctuation, nothing else.")
NAME_TIMEOUT = 90      # seconds per headless call
NAME_EXCERPT = 400     # chars of each quoted prompt or reply
NAME_WORKERS = 4       # headless calls at once
NAME_FALLBACK_MAX = 60  # chars of a title made from the first prompt


def claude_argv():
    return shlex.split(os.environ.get("SAVANNAH_CLAUDE_BIN", "") or "claude")


def name_model():
    """The model the naming call asks for: $SAVANNAH_NAME_MODEL, default the
    'haiku' alias (small, quick, cheap); 'default' or empty = the CLI's own."""
    m = os.environ.get("SAVANNAH_NAME_MODEL")
    if m is None:
        return "haiku"
    return "" if m.strip().lower() == "default" else m.strip()


def squeeze(text, limit):
    """text with its whitespace collapsed to single spaces, cut to limit."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit].rstrip() + "..."


def is_human_prompt(text):
    """A prompt a person typed, not a slash command's wrapper, a system
    marker or an interruption notice."""
    t = (text or "").strip()
    return bool(t) and not t.startswith("<") and not t.startswith("[Request interrupted")


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


def session_prompts(h):
    """(prompts, last_reply) of a hook session: the human prompts of its
    transcript in order (commands and interruptions dropped) and the text
    of the latest reply, or the hook's last_prompt alone when there is no
    transcript on disk. ([], "") when nothing has been typed yet."""
    sid = h.get("session_id", "") if isinstance(h.get("session_id"), str) else ""
    tp = h.get("transcript_path", "") if isinstance(h.get("transcript_path"), str) else ""
    project = h.get("project", "")
    path = transcript_for(sid, tp, project)
    prompts, reply = [], ""
    if path:
        try:
            turns = transcript_turns(path)
        except UserError:
            turns = []
        for t in turns:
            if is_human_prompt(t["prompt"]):
                prompts.append(t["prompt"])
                reply = t["text"]
    last = h.get("last_prompt", "")
    if not prompts and isinstance(last, str) and is_human_prompt(last):
        prompts = [last]
    return prompts, reply


def session_excerpt(project, prompts, reply):
    """The few lines the naming call reads: the directory, up to three first
    prompts, the latest prompt when there are more, the head of the latest
    reply. Each quote is squeezed to NAME_EXCERPT chars."""
    lines = [f"Directory: {display_path(project)}"]
    for i, p in enumerate(prompts[:3], 1):
        lines.append(f"Prompt {i}: {squeeze(p, NAME_EXCERPT)}")
    if len(prompts) > 3:
        lines.append(f"Latest prompt ({len(prompts)} so far): {squeeze(prompts[-1], NAME_EXCERPT)}")
    if reply:
        lines.append(f"Latest reply begins: {squeeze(reply, NAME_EXCERPT)}")
    return "\n".join(lines)


def clean_title(text):
    """Claude's answer as a title: the first non-blank line, a leading
    'Title:' and surrounding quotes or backticks dropped, trailing
    punctuation dropped, whitespace squeezed, cut to TITLE_MAX. '' when
    nothing remains."""
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        t = line.strip()
        if not t:
            continue
        t = re.sub(r"^(title|name)\s*:\s*", "", t, flags=re.IGNORECASE)
        t = t.strip().strip('"\'`*').strip()
        t = t.rstrip(".!?:;,").strip()
        return title_text(" ".join(t.split()))
    return ""


def ask_title(excerpt):
    """One headless Claude call (claude -p) for a title, or UserError (the
    caller falls back). --safe-mode: no hooks (so no ghost row on the
    board), no CLAUDE.md, no MCP servers; no tools, one turn, nothing
    persisted, run from the temp dir so no project is involved. The hook is
    told to stand down as well (SAVANNAH_HOOK_IGNORE), belt and braces."""
    argv = claude_argv() + ["-p", "--safe-mode", "--no-session-persistence", "--strict-mcp-config",
                            "--tools", "", "--max-turns", "1", "--output-format", "json",
                            "--system-prompt", NAME_SYSTEM_PROMPT]
    model = name_model()
    if model:
        argv += ["--model", model]
    argv.append(excerpt)
    env = dict(os.environ)
    env["SAVANNAH_HOOK_IGNORE"] = "1"
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=NAME_TIMEOUT, env=env,
                           cwd=tempfile.gettempdir())
    except OSError as e:
        raise UserError(f"cannot run {argv[0]}: {e.strerror or e} (set SAVANNAH_CLAUDE_BIN)")
    except subprocess.TimeoutExpired:
        raise UserError(f"{argv[0]} took over {NAME_TIMEOUT}s")
    ms = int((time.time() - t0) * 1000)
    log_debug("claude", argv=argv[:-1], rc=p.returncode, ms=ms, out=len(p.stdout))
    if p.returncode != 0:
        raise UserError(f"{argv[0]} exited {p.returncode}: {squeeze(p.stderr, 160) or 'no message'}")
    try:
        d = json.loads(p.stdout)
    except ValueError:
        raise UserError(f"{argv[0]} printed no JSON result")
    if not isinstance(d, dict) or d.get("is_error"):
        why = d.get("result") if isinstance(d, dict) else ""
        raise UserError(f"{argv[0]} reported an error: {squeeze(str(why or ''), 160) or d.get('subtype', '?')}")
    title = clean_title(d.get("result"))
    if not title:
        raise UserError(f"{argv[0]} gave no title")
    log_debug("named by claude", title=title, ms=ms, cost=d.get("total_cost_usd"), model=model or "default")
    return title


def slug_name(src):
    """Free text (or a path, whose last component counts) as a valid work
    session name, or "": lowered, letters and digits kept, '.', '_' and '-'
    kept after the start, any other run one '-', trailing '.-_' dropped,
    60 chars. The same rule as the board's nameFor (dash/board.go)."""
    base = (src or "").strip().rstrip("/").lower()
    base = base.rsplit("/", 1)[-1]
    out = []
    last = ""
    for ch in base:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch)
        elif out and ch in "._-":
            out.append(ch)
        elif out and last != "-":
            ch = "-"
            out.append(ch)
        else:
            continue
        last = ch
    name = "".join(out).rstrip(".-_")
    if len(name) > 60:
        name = name[:60].rstrip(".-_")
    return name if valid_name(name) else ""


def checkout_name(project):
    """The checkout a directory sits in, when it is one the owner made
    beside its repository the `<repo>-<ID>` way (zfs-DLPX-98816 next to
    zfs): the checkout directory's base name. That is a git worktree (its
    `.git` is a file) or a clone named `<X>-<ID>` with a repository `<X>`
    beside it (the longest such X). '' for the main checkout, a savannah
    worktree, or a directory under no `.git`: the title names those."""
    d = os.path.realpath(project) if project else ""
    while d and not os.path.exists(os.path.join(d, ".git")):
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent
    if not d:
        return ""
    parent, base = os.path.dirname(d), os.path.basename(d)
    if os.path.basename(parent) == WORKTREES_SUBDIR:
        return ""
    if os.path.isfile(os.path.join(d, ".git")):
        return base
    i = base.rfind("-")
    while i > 0:
        if os.path.exists(os.path.join(parent, base[:i], ".git")):
            return base
        i = base.rfind("-", 0, i)
    return ""


def unique_name(name, taken):
    """name, or name-2, name-3, ... when it is taken."""
    if name not in taken:
        return name
    i = 2
    while f"{name}-{i}" in taken:
        i += 1
    return f"{name}-{i}"


def cmd_autoname(a):
    """Name every Claude Code tab no record claims (or the given session
    ids) from what it is doing: a title from a headless Claude over the
    tab's transcript excerpt (the first prompt's first line when that
    fails, or with --no-claude), a name derived from the checkout when the
    tab sits in a `<repo>-<ID>` checkout beside its repository (zfs-DLPX-98816,
    checkout_name) and else from the title, then adopt.
    A tab nothing has been typed into yet is skipped (nothing to name it
    by) and named on a later run. Exit 0 whenever the verb ran: each tab's
    outcome is in the output (named / skipped / failed)."""
    hooks = load_hook_sessions()
    records = list_records()
    plain = unclaimed_sessions(hooks, records)
    wanted = list(dict.fromkeys(a.session or []))
    named, skipped, failed = [], [], []
    if wanted:
        by_id = {h.get("session_id"): h for h in plain}
        chosen = []
        for sid in wanted:
            if sid in by_id:
                chosen.append(by_id[sid])
            elif hook_session_by_id(sid) is not None:
                skipped.append({"session_id": sid, "why": "already a work session"})
            else:
                skipped.append({"session_id": sid, "why": "no live Claude session"})
        plain = chosen
    jobs = []
    for h in plain:
        sid = h.get("session_id", "") if isinstance(h.get("session_id"), str) else ""
        project = h["project"]
        prompts, reply = session_prompts(h)
        if not prompts:
            skipped.append({"session_id": sid, "project": project, "why": "no prompt yet"})
            continue
        jobs.append({"session_id": sid, "project": project, "h": h, "prompts": prompts,
                     "excerpt": session_excerpt(project, prompts, reply)})
    log_info("autoname", candidates=len(plain), asking=len(jobs), skipped=len(skipped),
             claude=not a.no_claude, dry_run=a.dry_run)

    def title_for(job):
        if a.no_claude:
            return squeeze(job["prompts"][0].splitlines()[0] if job["prompts"][0].strip() else "",
                           NAME_FALLBACK_MAX), "prompt", ""
        try:
            return ask_title(job["excerpt"]), "claude", ""
        except UserError as e:
            log_warn("autoname fallback", session=job["session_id"], error=str(e))
            first = job["prompts"][0].strip().splitlines()[0]
            return squeeze(first, NAME_FALLBACK_MAX), "prompt", str(e)

    if jobs:
        with ThreadPoolExecutor(max_workers=min(NAME_WORKERS, len(jobs))) as pool:
            results = list(pool.map(title_for, jobs))
    else:
        results = []
    taken = {r["name"] for r in records}
    for job, (title, how, err) in zip(jobs, results):
        name = unique_name(slug_name(checkout_name(job["project"])) or slug_name(title)
                           or slug_name(job["project"]) or "tab", taken)
        taken.add(name)
        entry = {"session_id": job["session_id"], "project": job["project"], "name": name,
                 "title": title, "how": how}
        if err:
            entry["claude_error"] = err
        if a.dry_run:
            named.append(entry)
            continue
        try:
            adopt_session(name, job["h"], job["session_id"], title=title)
        except UserError as e:
            failed.append({"session_id": job["session_id"], "project": job["project"], "error": str(e)})
            continue
        named.append(entry)
    out = {"dry_run": bool(a.dry_run), "named": named, "skipped": skipped, "failed": failed}
    log_info("autoname done", named=len(named), skipped=len(skipped), failed=len(failed), dry_run=a.dry_run)
    if a.json:
        print(json.dumps(out))
        return 0
    verb = "would name" if a.dry_run else "named"
    for e in named:
        print(f"{verb} {e['session_id']}: {e['name']}  \"{e['title']}\"  ({e['how']}, {display_path(e['project'])})")
    for e in skipped:
        print(f"skipped {e['session_id']}: {e['why']}")
    for e in failed:
        print(f"failed {e['session_id']}: {e['error']}")
    if not (named or skipped or failed):
        print("nothing to name: every live Claude Code tab is a work session already")
    return 0


def cmd_move(a):
    """Where the row sits on the board: the ACTIVE section (what the owner is
    on now) or LATER, and the rank that orders rows inside a section (lower
    first; a new session's rank is its creation time, so untouched rows keep
    arriving in order). Storage only: the tab, the worktree and the branch
    are not touched."""
    if not (a.active or a.later or a.rank is not None):
        return usage_error("move", "give --active, --later or --rank N (or several)")
    rec = load_record(a.name)
    if a.active:
        rec["priority"] = "active"
    elif a.later:
        rec["priority"] = "later"
    if a.rank is not None:
        rec["rank"] = a.rank
    save_record(rec)
    log_info("moved", name=rec["name"], priority=rec["priority"], rank=rec["rank"])
    if a.json:
        print(json.dumps({"name": rec["name"], "priority": rec["priority"], "rank": rec["rank"]}))
    else:
        print(f"moved {rec['name']}: {rec['priority']}, rank {rec['rank']}")
    return 0


RENAME_FILES = ("record.json", "notes.md", STATUS_FILE)


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
        raise UserError(f"invalid name {new!r}: {NAME_HELP}")
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
    if a.json:
        print(json.dumps({"name": new, "old": old, "worktree": rec["worktree"], "branch": rec["branch"]}))
    else:
        print(f"renamed {old} to {new} (worktree and branch keep their names)")
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
        if a.json:
            print(json.dumps({"name": name, "status": None}))
        else:
            print(f"{name}: status cleared")
        return 0
    if text:
        prev = read_status(name)
        st = {"text": text, "updated": int(time.time()), "count": (prev["count"] if prev else 0) + 1}
        atomic_write(path, st)
        log_info("status reported", name=name, count=st["count"], text=text)
        if a.json:
            print(json.dumps(dict(st, name=name)))
        else:
            print(f"{name}: poll {st['count']}: {text}")
        return 0
    st = read_status(name)
    log_debug("status read", name=name, found=st is not None)
    if a.json:
        print(json.dumps({"name": name, "status": st}))
    elif st is None:
        print(f"{name}: (no status)")
    else:
        age = max(0, int(time.time()) - st["updated"])
        print(f"{name}: {st['text']} ({st['count']} polls, last {age}s ago)")
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
        if a.json:
            print(json.dumps({"name": name, "title": text}))
        else:
            print(f"{name}: title cleared" if a.clear else f"{name}: title set: {text}")
        return 0
    log_debug("title read", name=name, title=rec["title"])
    if a.json:
        print(json.dumps({"name": name, "title": rec["title"]}))
    else:
        print(f"{name}: {rec['title']}" if rec["title"] else f"{name}: (no title)")
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


def cmd_repos(a):
    """The folder of repos the board offers for new tasks: `repos DIR`
    remembers DIR (the same memory `hub` uses), `repos` alone lists the
    remembered folder, else the cwd."""
    if a.dir:
        d = os.path.abspath(a.dir)
        if not os.path.isdir(d):
            raise UserError(f"not a directory: {d}")
        remember_hub(d)
        picked = "given"
    else:
        d = recorded_hub()
        picked = "remembered" if d else "cwd"
        d = d or os.getcwd()
    repos = repos_in(d)
    scratch = os.path.join(d, SCRATCH_SUBDIR)
    scratch = scratch if os.path.isdir(scratch) else ""
    log_debug("repos", dir=d, picked=picked, count=len(repos), scratch=bool(scratch))
    if a.json:
        print(json.dumps({"dir": d, "picked": picked, "repos": repos, "scratch": scratch}))
    else:
        print(f"{d}" + (f" ({picked})" if picked != "given" else ""))
        for r in repos:
            print(f"  {r}")
        if scratch:
            print(f"  scratch (no worktree): {scratch}")
        if not repos:
            print("  (no git repositories here)")
    return 0


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
                prompts=" | ".join((t["prompt"].strip().splitlines() or [""])[0][:60] for t in shown))
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
        head = (t["prompt"].strip().splitlines() or [""])[0]
        if len(head) > OUTPUT_PROMPT_CLIP:
            head = head[: OUTPUT_PROMPT_CLIP - 1] + "~"
        when = local_stamp(t["answered"] or t["asked"])
        print(f"## {when}  you: {head}" if head else f"## {when}")
        print(t["text"] if t["text"] else "(no reply yet)")
    return 0


# --------------------------------------------------------------------------
# ask: one question typed into every tab that is safe to interrupt, the
# replies collected

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
    if not iterm_uuid(h.get("iterm_session_id")):
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
        pane = (rec or {}).get("claude_pane") or iterm_uuid(h.get("iterm_session_id"))
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
    200 chars (hooks/session-status.py first_line)."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


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
    term = make_terminal("")
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
    log_info("ask", asked=len(asked), skipped=len(skipped), failed=len(failed), wait=not a.no_wait,
             timeout=a.timeout)
    log_verbose("ask question", text=question[:300])
    waited = 0.0
    if asked and not a.no_wait:
        deadline = time.time() + a.timeout
        pending = {t["session_id"]: t for t in asked}
        while pending and time.time() < deadline:
            time.sleep(ASK_POLL)
            for h in load_hook_sessions():
                sid = h.get("session_id")
                if sid in pending and ask_answered(h, question):
                    pending[sid]["h"] = h
                    pending[sid]["answered"] = time.time()
                    del pending[sid]
        waited = round(time.time() - (asked[0]["sent"]), 1)
    out_asked = []
    for t in asked:
        answered = "answered" in t
        reply = ask_reply(t["h"], question) if (answered or a.no_wait) else ""
        if answered and not reply:
            # Stopped, but the transcript has no text for it yet (or none at
            # all): read it once more after a moment, then report as is.
            time.sleep(ASK_POLL)
            reply = ask_reply(t["h"], question)
        how = "answered" if answered else ("sent" if a.no_wait else "timeout")
        entry = {"session_id": t["session_id"], "name": t["name"], "project": t["project"],
                 "how": how, "reply": reply}
        out_asked.append(entry)
        if t["rec"] is not None and reply:
            # The reply is the session's own account: it goes into its notes
            # (the question after it, so the NOTE column shows the answer's
            # first line) and onto the record as the latest summary.
            rec = t["rec"]
            rec["summary"] = reply
            rec["summary_at"] = int(time.time())
            save_record(rec)
            append_note(rec["name"], reply + "\n(asked: " + first_prompt_line(question) + ")")
            log_info("reply noted", name=rec["name"], chars=len(reply))
    log_info("ask done", asked=len(asked), answered=sum(1 for e in out_asked if e["how"] == "answered"),
             waited=waited)
    if a.json:
        print(json.dumps({"question": question, "timeout": a.timeout, "waited": waited, "asked": out_asked,
                          "skipped": skipped, "failed": failed}))
        return 0
    if not out_asked and not skipped and not failed:
        print("nothing to ask: no live Claude Code tab")
        return 0
    for i, e in enumerate(out_asked):
        if i:
            print()
        who = e["name"] or e["session_id"]
        print(f"## {who}  ({display_path(e['project'])})  {e['how']}")
        if e["reply"]:
            print(e["reply"])
        elif e["how"] == "sent":
            print("(asked; savannah work output shows the reply)")
        else:
            print(f"(no reply within {a.timeout} s; savannah work output shows it when it comes)")
    for e in skipped:
        print(f"skipped {e['name'] or e['session_id'] or display_path(e['project'])}: {e['why']}")
    for e in failed:
        print(f"failed {e['name'] or e['session_id']}: {e['error']}")
    return 0


def cmd_hub(a):
    if a.dir:
        d = os.path.abspath(a.dir)
        picked = "given"
    else:
        d = recorded_hub()
        picked = "remembered" if d else "cwd"
        d = d or os.getcwd()
    if not os.path.isdir(d):
        raise UserError(f"not a directory: {d}")
    mcp_path = os.path.join(d, ".mcp.json")
    mcp_status = write_hub_mcp(mcp_path)
    settings_path = os.path.join(d, ".claude", "settings.json")
    settings_status = write_hub_settings(settings_path)
    claude_md = os.path.join(d, "CLAUDE.md")
    md_status = write_if_missing(claude_md, template_text(HUB_TEMPLATE))
    scratch = os.path.join(d, SCRATCH_SUBDIR)
    try:
        os.makedirs(scratch, exist_ok=True)
    except OSError as e:
        raise UserError(f"cannot create {scratch}: {e.strerror or e}")
    scratch_status = write_if_missing(os.path.join(scratch, "CLAUDE.md"),
                                      template_text(SCRATCH_TEMPLATE))
    remembered = remember_hub(d)
    launch = not a.no_launch
    argv = shlex.split(os.environ.get("SAVANNAH_CLAUDE_BIN", "") or "claude")
    if a.json:
        print(json.dumps({"dir": d, "picked": picked, "mcp_json": mcp_path,
                          "mcp_status": mcp_status, "settings": settings_path,
                          "settings_status": settings_status, "claude_md": claude_md,
                          "claude_md_status": md_status, "scratch": scratch,
                          "scratch_status": scratch_status, "hub_file": remembered,
                          "launch": launch, "claude": argv}))
    else:
        print(f"hub {d}" + (f" ({picked})" if picked != "given" else ""))
        print(f"  {mcp_path}: {mcp_status} ({HUB_SERVER} shim)")
        print(f"  {settings_path}: {settings_status} (work_* tools and sleep pre-allowed, "
              f"project MCP servers approved)")
        print(f"  {claude_md}: {md_status}" + (
            "" if md_status == "created" else f" (not touched; see {template_path(HUB_TEMPLATE)})"))
        print(f"  {scratch}/: {scratch_status} (tabs for tasks outside any repo)")
        print(f"  {remembered}: remembers this hub, so a bare `savannah` opens it")
        if launch:
            print(f"  launching {' '.join(argv)} in {d}")
    log_info("hub", dir=d, picked=picked, mcp=mcp_status, settings=settings_status, claude_md=md_status,
             scratch=scratch_status, launch=launch, claude=argv)
    sys.stdout.flush()
    if not launch:
        return 0
    try:
        os.chdir(d)
        os.execvp(argv[0], argv)
    except OSError as e:
        raise UserError(f"cannot launch {argv[0]}: {e.strerror or e} (set SAVANNAH_CLAUDE_BIN)")
    return 0  # not reached: execvp replaces the process


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
        why = ""
        if not os.path.exists(record_path(name)):
            why = "no record.json"      # new died between mkdir and record write
        else:
            try:
                with open(record_path(name)) as f:
                    if not isinstance(json.load(f), dict):
                        why = "record.json is not an object"
            except (OSError, ValueError):
                why = "unreadable record.json"
        if why:
            # Stale or corrupt: forget it. Any worktree it had is left for
            # `git worktree list` since the record cannot say where it is.
            try:
                shutil.rmtree(rdir)
            except OSError as e:
                raise UserError(f"cannot remove stale {rdir}: {e.strerror or e}")
            log_info("removed stale", name=name, why=why)
            print(f"removed {name} (stale: {why}; check `git worktree list` for leftovers)")
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
        nested = [r["name"] for r in list_records()
                  if r["name"] != name and r.get("worktree") and is_inside(r["worktree"], worktree)]
        if nested:
            raise UserError(f"refusing: work session(s) {', '.join(sorted(nested))} live inside "
                            f"{worktree}; rm them first or pass --keep-worktree")

    # Archive first (a copy, so nothing is lost if a later step refuses):
    # if git says no, the copy is withdrawn and everything stays in place.
    np = notes_path(name)
    archived = ""
    if os.path.exists(np):
        try:
            adir = os.path.join(work_dir(), ARCHIVE_SUBDIR)
            os.makedirs(adir, exist_ok=True)
            created = rec.get("created") or int(time.time())
            archived = archive_target(adir, name, created)
            shutil.copy2(np, archived)
        except OSError as e:
            raise UserError(f"cannot archive notes to {archived or adir}: {e.strerror or e}")

    def withdraw_archive():
        if archived:
            try:
                os.remove(archived)
            except OSError:
                pass

    # Worktree next: a dirty worktree without --force must leave EVERYTHING
    # in place, including the tab, so nothing is closed before git agrees.
    if removing_worktree:
        try:
            if os.path.isdir(worktree):
                args = ["-C", repo, "worktree", "remove"]
                if a.force:
                    args.append("--force")
                p = run_git(args + [worktree])
                if p.returncode != 0:
                    hint = "" if a.force else " (dirty? pass --force to discard, or --keep-worktree)"
                    raise UserError(git_fail_message(p, "git worktree remove") + hint)
            else:
                run_git(["-C", repo, "worktree", "prune"])  # dir already gone by hand
        except BaseException:
            withdraw_archive()
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
        raise UserError(f"cannot remove {rdir}: {e.strerror or e}"
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
# snapshot and restore: the whole layout saved before a reboot or an iTerm2
# relaunch (every work session and every other open Claude Code tab, in tab
# order, with the Claude session ids), and put back afterwards: tabs remade,
# each Claude reloading its own conversation with --resume


SNAPSHOT_STAMP_RE = re.compile(r"\d{8}-\d{6}(-\d+)?")   # 20261105-223012, -2 when one second holds two


def snapshots_dir():
    return os.path.join(work_dir(), SNAPSHOTS_SUBDIR)


def snapshot_stamp(now=None):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


def snapshot_file(stamp):
    return os.path.join(snapshots_dir(), stamp + ".json")


def tab_title(title):
    """An it2 tab title without iTerm2's trailing " (process)", exactly as
    the dash's cleanTitle: "demo (claude)" -> "demo", "~/x (-zsh)" -> "~/x",
    "(claude)" alone -> "". (title_text above is the record's own free-text
    title; this is what iTerm2 shows on the tab.)"""
    t = (title or "").strip()
    i = t.rfind(" (")
    if i >= 0 and t.endswith(")"):
        t = t[:i]
    elif t.startswith("(") and t.endswith(")"):
        t = ""
    return t.strip()


def hook_fields(h):
    """The fields of a hook session a snapshot keeps (what --resume and the
    restore plan need), every value a string."""
    out = {}
    for key in ("session_id", "transcript_path", "state", "last_prompt"):
        v = (h or {}).get(key, "")
        out[key] = v if isinstance(v, str) else ""
    return out


def take_snapshot(note, now=None):
    """The snapshot dict: every work record (in iTerm2 tab order, the ones
    whose Claude pane is gone last, then by name) with its joined Claude
    Code session, and every other Claude Code tab (a hook session no record
    claims). it2 may be unavailable (no iTerm2 here): the snapshot still
    holds the records and sessions, only without tab order, titles or pane
    liveness (pane_alive null), and says so (it2 false)."""
    now = int(now if now is not None else time.time())
    hooks = load_hook_sessions()
    records = list_records()
    it2_ok = True
    try:
        listed = make_terminal("").sessions()
    except UserError as e:
        listed = []
        it2_ok = False
        log_warn("snapshot without it2", error=str(e))
    order, tabs, titles = {}, {}, {}
    for i, s in enumerate(listed):
        sid = s.get("id") if isinstance(s, dict) else None
        if isinstance(sid, str) and sid and sid not in order:
            order[sid] = i
            tabs[sid] = s.get("tab_id", "") if isinstance(s.get("tab_id"), str) else ""
            titles[sid] = tab_title(s.get("title", "") if isinstance(s.get("title"), str) else "")

    def position(pane, fallback):
        return (0, order[pane], "") if pane in order else (1, 0, fallback)

    sessions, claimed = [], set()
    for rec in records:
        h = join_session(rec, hooks)
        if h is not None:
            claimed.add(id(h))
        pane = rec.get("claude_pane") or ""
        entry = {"name": rec["name"], "title": rec.get("title", "") or "",
                 "worktree": rec.get("worktree", ""), "repo": rec.get("repo", ""),
                 "branch": rec.get("branch", ""), "adopted": bool(rec.get("adopted")),
                 "priority": rec.get("priority", "active"), "rank": rec.get("rank", 0),
                 "tab_id": rec.get("tab_id", ""), "claude_pane": pane,
                 "shell_pane": rec.get("shell_pane", ""), "tab_title": titles.get(pane, ""),
                 "pane_alive": (pane in order) if it2_ok else None, "live": h is not None}
        entry.update(hook_fields(h))
        sessions.append((position(pane, rec["name"]), entry))
    plain = []
    for h in hooks:
        project = h.get("project", "")
        if id(h) in claimed or not isinstance(project, str) or not project:
            continue
        iid = h.get("iterm_session_id")
        pane = iterm_uuid(iid if isinstance(iid, str) else "")
        entry = {"project": project, "pane": pane, "tab_id": tabs.get(pane, ""),
                 "title": titles.get(pane, ""), "pane_alive": (pane in order) if it2_ok else None}
        entry.update(hook_fields(h))
        plain.append((position(pane, project), entry))
    sessions.sort(key=lambda t: t[0])
    plain.sort(key=lambda t: t[0])
    return {"stamp": snapshot_stamp(now), "taken": now,
            "taken_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)) + " local",
            "note": note or "", "it2": it2_ok,
            "sessions": [e for _, e in sessions], "plain": [e for _, e in plain]}


def write_snapshot(snap):
    """<snapshots dir>/<stamp>.json plus the latest.json copy; a second
    snapshot in the same second gets a -2, -3 suffix. -> the stamp's path."""
    d = snapshots_dir()
    os.makedirs(d, exist_ok=True)
    stamp, n = snap["stamp"], 1
    while os.path.exists(snapshot_file(stamp)):
        n += 1
        stamp = f"{snap['stamp']}-{n}"
    snap["stamp"] = stamp
    path = snapshot_file(stamp)
    atomic_write(path, snap)
    atomic_write(os.path.join(d, SNAPSHOT_LATEST), snap)
    return path


def read_snapshot(path):
    """The snapshot at path with its lists sane (every session entry has a
    string name, every plain entry a string project), or None."""
    try:
        with open(path) as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict):
        return None
    for key, field in (("sessions", "name"), ("plain", "project")):
        items = snap.get(key)
        if not isinstance(items, list):
            items = []
        snap[key] = [e for e in items if isinstance(e, dict) and isinstance(e.get(field), str) and e[field]]
    return snap


def snapshot_row(path, stamp, snap):
    return {"stamp": stamp, "path": path, "taken": coerce_int(snap.get("taken"), 0),
            "taken_local": snap.get("taken_local") if isinstance(snap.get("taken_local"), str) else "",
            "work": len(snap["sessions"]), "live": sum(1 for e in snap["sessions"] if e.get("live")),
            "plain": len(snap["plain"]), "note": snap.get("note") if isinstance(snap.get("note"), str) else ""}


def list_snapshots():
    """Every snapshot file as a row, newest first; the latest.json copy is
    not a row of its own."""
    d = snapshots_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return []
    out = []
    for fn in names:
        if not fn.endswith(".json") or not SNAPSHOT_STAMP_RE.fullmatch(fn[:-5]):
            continue
        snap = read_snapshot(os.path.join(d, fn))
        if snap is not None:
            out.append(snapshot_row(os.path.join(d, fn), fn[:-5], snap))
    out.sort(key=lambda r: r["stamp"], reverse=True)
    return out


def pick_snapshot(which):
    """(path, snapshot) for what `restore` names: latest.json for "" or
    "latest", a stamp's file, or an existing path. Raises UserError when
    there is none."""
    d = snapshots_dir()
    if not which or which == "latest":
        path = os.path.join(d, SNAPSHOT_LATEST)
    elif SNAPSHOT_STAMP_RE.fullmatch(which):
        path = snapshot_file(which)
    elif os.path.isfile(which):
        path = which
    else:
        raise UserError(f"no snapshot {which} (work snapshots lists them)")
    snap = read_snapshot(path)
    if snap is None:
        if which and which != "latest":
            raise UserError(f"no snapshot {which} (work snapshots lists them)")
        raise UserError("no snapshot (work snapshot makes one)")
    if not isinstance(snap.get("stamp"), str) or not snap["stamp"]:
        base = os.path.basename(path)
        snap["stamp"] = base[:-5] if base.endswith(".json") else base
    return path, snap


def transcript_for(session_id, transcript_path, project):
    """The transcript `claude --resume <session_id>` would reload, or "" when
    none is on disk: the path the snapshot recorded, else Claude Code's own
    <projects dir>/<encoded project>/<session_id>.jsonl (observed naming,
    like encode_project_dir)."""
    if not session_id:
        return ""
    if transcript_path and os.path.isfile(transcript_path):
        return transcript_path
    if project:
        p = os.path.join(claude_config_dir(), "projects",
                         encode_project_dir(os.path.realpath(project)), session_id + ".jsonl")
        if os.path.isfile(p):
            return p
    return ""


def display_path(p):
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def snapshot_str(entry, key):
    v = entry.get(key)
    return v if isinstance(v, str) else ""


def cmd_snapshot(a):
    snap = take_snapshot(a.note)
    path = write_snapshot(snap)
    live = sum(1 for e in snap["sessions"] if e["live"])
    log_info("snapshot written", path=path, work=len(snap["sessions"]), live=live,
             plain=len(snap["plain"]), it2=snap["it2"])
    if a.json:
        print(json.dumps({"path": path, "stamp": snap["stamp"], "work": len(snap["sessions"]),
                          "live": live, "plain": len(snap["plain"]), "it2": snap["it2"]}))
        return 0
    tail = "" if snap["it2"] else " (it2 unavailable: no tab order, titles or pane liveness)"
    print(f"snapshot {path}: {len(snap['sessions'])} work sessions ({live} live), "
          f"{len(snap['plain'])} plain tabs{tail}")
    return 0


def cmd_snapshots(a):
    rows = list_snapshots()
    if a.json:
        print(json.dumps(rows))
        return 0
    if not rows:
        print("(no snapshots; work snapshot makes one)")
        return 0
    table = [("STAMP", "TAKEN", "WORK", "LIVE", "PLAIN", "NOTE")]
    for r in rows:
        note = r["note"].splitlines()[0] if r["note"].strip() else ""
        table.append((r["stamp"], r["taken_local"] or "-", str(r["work"]), str(r["live"]),
                      str(r["plain"]), note))
    widths = [max(len(row[i]) for row in table) for i in range(5)]
    for row in table:
        print("  ".join(row[i].ljust(widths[i]) for i in range(5)) + "  " + row[5])
    return 0


def cmd_restore(a):
    """Put a snapshot's tabs back. Work sessions first, in snapshot order:
    a record whose Claude pane is still there is left alone; one whose tab
    is gone gets a fresh tab and split (remake_tab) with claude reloading
    the snapshot's conversation (--resume <id>) when that transcript is on
    disk, else the worktree's latest (--continue), else a fresh claude.
    Then, unless --no-plain, every other Claude Code tab the snapshot saw
    comes back as a single pane (claude --resume <id> in its directory).
    Nothing is ever closed. One failing item is recorded (exit 1 at the
    end) and the rest continues."""
    path, snap = pick_snapshot(a.which)
    stamp = snap["stamp"]
    term = make_terminal("")
    try:
        live = term.live_ids()
    except UserError:
        if not a.dry_run:
            raise
        live = set()  # the plan can still be shown: nothing is known to be open
    hooks = load_hook_sessions()
    restored, plain, skipped, failed, lines = [], [], [], [], []

    def skip(key, value, why):
        # JSON carries the full name or project path; the human line shows ~/.
        skipped.append({key: value, "why": why})
        lines.append(f"skipped {display_path(value)}: {why}")
        log_debug("restore skip", **{key: value, "why": why})

    def fail(key, value, err):
        failed.append({key: value, "error": str(err)})
        lines.append(f"failed {display_path(value)}: {err}")
        log_warn("restore failed", **{key: value, "error": str(err)})

    for entry in snap["sessions"]:
        name = entry["name"]
        if not valid_name(name):
            skip("name", name, "bad name")
            continue
        try:
            rec = load_record(name)
        except UserError:
            skip("name", name, "no record")
            continue
        if rec.get("claude_pane") and rec["claude_pane"] in live:
            skip("name", name, "alive")
            continue
        worktree = rec.get("worktree") or ""
        if not worktree or not os.path.isdir(worktree):
            skip("name", name, "worktree gone")
            continue
        session_id = snapshot_str(entry, "session_id")
        if transcript_for(session_id, snapshot_str(entry, "transcript_path"), worktree):
            how = "resume"
        elif find_transcript(rec, hooks)[0]:
            how = "continue"
        else:
            how = "fresh"
        howtext = f"resume {session_id}" if how == "resume" else how
        if a.dry_run:
            restored.append({"name": name, "how": how, "tab_id": ""})
            lines.append(f"would restore {name} ({howtext})")
            continue
        try:
            remake_tab(rec, term, live, how=how, resume_id=session_id)
        except UserError as e:
            fail("name", name, e)
            continue
        restored.append({"name": name, "how": how, "tab_id": rec.get("tab_id", "")})
        lines.append(f"restored {name} ({howtext}, tab {rec.get('tab_id', '')})")

    if not a.no_plain:
        for entry in snap["plain"]:
            project = entry["project"]
            label = display_path(project)
            session_id = snapshot_str(entry, "session_id")
            if entry.get("pane_alive") is False:
                skip("project", project, "not open when the snapshot was taken")
                continue
            h = hook_session_by_id(session_id) if session_id else None
            if h is not None:
                iid = h.get("iterm_session_id")
                if iterm_uuid(iid if isinstance(iid, str) else "") in live:
                    skip("project", project, "alive")
                    continue
            if not os.path.isdir(project):
                skip("project", project, "project gone")
                continue
            if transcript_for(session_id, snapshot_str(entry, "transcript_path"), project):
                how = "resume"
            elif find_transcript({"worktree": project}, [])[0]:
                how = "continue"
            else:
                how = "fresh"
            howtext = f"resume {session_id}" if how == "resume" else how
            if a.dry_run:
                plain.append({"project": project, "how": how, "tab_id": ""})
                lines.append(f"would open plain {label} ({howtext})")
                continue
            title = snapshot_str(entry, "title") or os.path.basename(project.rstrip(os.sep)) or project
            cmd = claude_command(project, "", "", resume=(how == "continue"),
                                 resume_id=(session_id if how == "resume" else ""))
            try:
                ids = term.create_tab(title, cmd)
            except UserError as e:
                fail("project", project, e)
                continue
            plain.append({"project": project, "how": how, "tab_id": ids["tab_id"]})
            lines.append(f"plain {label} ({howtext}, tab {ids['tab_id']})")
            log_info("plain tab remade", project=project, how=how, tab=ids["tab_id"])

    counts = (f"{len(restored)} work sessions, {len(plain)} plain tabs, "
              f"{len(skipped)} skipped, {len(failed)} failed")
    log_info("restore", stamp=stamp, dry_run=bool(a.dry_run), restored=len(restored),
             plain=len(plain), skipped=len(skipped), failed=len(failed))
    if a.json:
        print(json.dumps({"stamp": stamp, "path": path, "dry_run": bool(a.dry_run), "restored": restored,
                          "plain": plain, "skipped": skipped, "failed": failed}))
    else:
        for ln in lines:
            print(ln)
        if a.dry_run:
            print(f"plan from snapshot {stamp}: {counts} (nothing touched)")
        else:
            print(f"restored from snapshot {stamp}: {counts}")
    return 1 if failed else 0


# --------------------------------------------------------------------------


def build_parser():
    ap = argparse.ArgumentParser(
        prog="savannah_work",
        description="Local interactive Claude Code work sessions: one iTerm2 tab "
                    "(Claude above, shell below) per work item, plus notes. "
                    "Text that begins with '-' goes after '--': say NAME -- --continue")
    sub = ap.add_subparsers(dest="verb", metavar="<verb>")
    sub.required = True

    p = sub.add_parser("new", help="create a work session (worktree, tab, record)")
    p.add_argument("name")
    p.add_argument("--repo", help="git repo to work in (default: cwd); resolved to the main worktree root")
    p.add_argument("--branch", help="check out this existing local branch (default: work/<name>, "
                                    "created, or reattached if rm left it behind)")
    p.add_argument("--no-worktree", action="store_true", help="run in the repo itself")
    p.add_argument("--prompt", help="initial prompt passed to claude")
    p.add_argument("--note", help="first note to record")
    p.add_argument("--title", help=f"free-text title the board shows (first line, {TITLE_MAX} chars; "
                                   "the name stays the identifier)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("ls", help="list work sessions joined with live Claude sessions")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("note", help="append a note (text args or stdin)")
    p.add_argument("name")
    p.add_argument("text", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("notes", help="print the notes file")
    p.add_argument("name")
    p.set_defaults(fn=cmd_notes)

    p = sub.add_parser("say", help="type text into the Claude pane (use -- before text starting with -)")
    p.add_argument("name")
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_say)

    p = sub.add_parser("ask", help="type one question into every tab that is safe to interrupt (idle or "
                                   "done, never working or waiting), wait for the replies, keep each work "
                                   "session's as its summary")
    p.add_argument("name", nargs="*", help="work sessions to ask (or --all)")
    p.add_argument("--all", action="store_true", help="every live Claude Code tab, plain tabs too")
    p.add_argument("-q", "--question", default="", metavar="TEXT",
                   help="the question (default: what are you working on, where are you, what is next)")
    p.add_argument("--timeout", type=float, default=ASK_TIMEOUT, metavar="SECONDS",
                   help=f"how long to wait for the replies (default {ASK_TIMEOUT})")
    p.add_argument("--no-wait", action="store_true", help="type the question and return at once")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("sh", help="type a command into the shell pane (use -- before text starting with -)")
    p.add_argument("name")
    p.add_argument("cmd", nargs="+")
    p.set_defaults(fn=cmd_sh)

    p = sub.add_parser("jump", help="focus the Claude pane (or --shell)")
    p.add_argument("name")
    p.add_argument("--shell", action="store_true")
    p.set_defaults(fn=cmd_jump)

    p = sub.add_parser("open", help="focus the tab, or open it again (new tab + split, claude --continue) "
                                    "when it is gone")
    p.add_argument("name")
    p.add_argument("--shell", action="store_true", help="focus the shell pane instead")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("adopt", help="take a Claude Code tab you opened yourself into a named work "
                                     "session (no new tab, no worktree; its hook file names the "
                                     "directory and pane)")
    p.add_argument("name")
    p.add_argument("--session", required=True, metavar="ID",
                   help="that tab's Claude session id (the hook file's name, the dash's row, "
                        "work ls --json claude_session_id)")
    p.add_argument("--note", help="first note to record")
    p.add_argument("--title", help=f"free-text title the board shows (first line, {TITLE_MAX} chars)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_adopt)

    p = sub.add_parser("autoname", help="name every Claude Code tab that is not a work session yet from "
                                        "what it is doing: a headless claude titles an excerpt of its "
                                        "transcript, the name is derived, the tab is adopted (the board "
                                        "runs this when it comes up)")
    p.add_argument("session", nargs="*", metavar="SESSION_ID", help="only these tabs (default: every unnamed one)")
    p.add_argument("--dry-run", action="store_true", help="print the names, adopt nothing")
    p.add_argument("--no-claude", action="store_true",
                   help="no headless call: the first prompt's first line is the title")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_autoname)

    p = sub.add_parser("move", help="where the row sits on the board: --active (the top section) or "
                                    "--later (the bottom one), and/or --rank N (order inside a section)")
    p.add_argument("name")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--active", action="store_true", help="the ACTIVE section: what you are on now")
    g.add_argument("--later", action="store_true", help="the LATER section")
    p.add_argument("--rank", type=int, metavar="N",
                   help="order inside the section, lower first (a new session ranks by its creation time)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_move)

    p = sub.add_parser("rename", help="a new name for the record (notes and status travel; the worktree, "
                                      "the branch and the tab keep the old name)")
    p.add_argument("old")
    p.add_argument("new")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_rename)

    p = sub.add_parser("status", help="what that tab's Claude is polling for you: text reports one more "
                                      "poll, no text reads it, --clear removes it")
    p.add_argument("name", nargs="?", help="the session (omit with --here)")
    p.add_argument("text", nargs="*", help="the report, e.g. \"waiting for ctest (run 2 of 3)\" "
                                           "(first line, 200 chars)")
    p.add_argument("--here", action="store_true",
                   help="the session this shell runs in (the record whose Claude pane is $ITERM_SESSION_ID, "
                        "else the one record whose worktree is the cwd); every positional is then text")
    p.add_argument("--clear", action="store_true", help="remove the report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("title", help="the free-text title the board shows in its WORK column (capitals, "
                                     "spaces, anything; the name stays the identifier): text sets it, "
                                     "--clear removes it, neither prints it")
    p.add_argument("name")
    p.add_argument("text", nargs="*", help=f"the title (first line, {TITLE_MAX} chars); "
                                           "text starting with - goes after --")
    p.add_argument("--clear", action="store_true", help="remove the title")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_title)

    p = sub.add_parser("log", help="read savannah's log (one file, every piece of savannah writes it): "
                                   "the last N lines at a level, --follow for new ones")
    p.add_argument("--level", choices=savannah_log.READ_LEVELS, default="normal",
                   help="normal = INFO and above (default), debug adds DEBUG, verbose adds everything")
    p.add_argument("--lines", type=int, default=LOG_LINES_DEFAULT, metavar="N",
                   help=f"how many lines (default {LOG_LINES_DEFAULT})")
    p.add_argument("--follow", action="store_true", help="keep printing new lines until Ctrl-C")
    p.add_argument("--path", action="store_true", help="print only the log file's path")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("repos", help="list the git repos in your folder of repos (DIR remembers it)")
    p.add_argument("dir", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_repos)

    p = sub.add_parser("snapshot", help="save the layout (every work session and open Claude Code tab, "
                                        "in tab order, with the Claude session ids) under "
                                        "<work dir>/snapshots, for restore after a reboot")
    p.add_argument("--note", default="", help="why, e.g. \"before reboot\"")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("snapshots", help="list the saved snapshots, newest first")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_snapshots)

    p = sub.add_parser("restore", help="put a snapshot's tabs back: remake every work session whose tab "
                                       "is gone (claude --resume its conversation) and reopen the plain "
                                       "tabs; never closes anything")
    p.add_argument("which", nargs="?", default="latest", metavar="STAMP",
                   help="a stamp from `snapshots` (default: latest)")
    p.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    p.add_argument("--no-plain", action="store_true", help="work sessions only, leave the plain tabs")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("output", help="print the last N turns (your prompt, Claude's reply) of that tab")
    p.add_argument("name")
    p.add_argument("--last", type=int, default=1, metavar="N")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_output)

    p = sub.add_parser("rm", help="close tab, remove worktree (never an adopted session's directory), "
                                  "archive notes")
    p.add_argument("name")
    p.add_argument("--keep-worktree", action="store_true")
    p.add_argument("--force", action="store_true", help="discard a dirty worktree")
    p.set_defaults(fn=cmd_rm)

    p = sub.add_parser("hub", help="register the peer shim in DIR/.mcp.json, add a starter "
                                   "CLAUDE.md, launch claude there (the coordinating session)")
    p.add_argument("dir", nargs="?", help="hub directory: your folder of repos "
                                          "(default: the remembered hub, else cwd)")
    p.add_argument("--no-launch", action="store_true", help="write the files, do not start claude")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_hub)
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
