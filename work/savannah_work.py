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
#     archive/<name>-<created-epoch>.md  notes copied here on rm (-2, -3, ...
#                                        appended when that name is taken)
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
#   rm <name> [--keep-worktree] [--force]
#
# Exit codes: 0 success, 1 user error (unknown name, git refused, pane gone),
# 2 bad usage. --json on ls/new/note emits one JSON value on stdout.
#
# Trust: the NAME on the command line is the only thing that names a record
# directory. A record.json's own "name" field is never used to build a path
# (a record that says "../x" must not be able to point rm at the work dir).
#
# Terminal seam: the Terminal class below is the only thing that knows about
# iTerm2. ITerm shells out to $SAVANNAH_OSASCRIPT (default "osascript") so
# tests substitute tools/fake_osascript.py. A tmux implementation plugs in
# later behind the same four methods. Stdlib only.

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RESERVED_RE = re.compile(r"t-[0-9]+")  # mesh task ids
WORKTREES_SUBDIR = ".savannah-worktrees"
ARCHIVE_SUBDIR = "archive"   # notes graveyard under the work dir; never a session name
LAST_PROMPT_MAX = 200
ITERM_RUNNING_GUARD = 'if application "iTerm2" is not running then return false'


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
    if RESERVED_RE.fullmatch(name) or name == ARCHIVE_SUBDIR:
        return False
    if ".." in name or name.endswith(".") or name.endswith(".lock"):
        return False
    return True


NAME_HELP = "want ^[a-z0-9][a-z0-9._-]{0,63}$ (no '..', no trailing '.' or '.lock'), not t-<digits>"


class UserError(Exception):
    """A problem the human caused or can fix: exit 1 with a one-line message."""


RECORD_FIELDS = (
    "name", "repo", "worktree", "branch", "created", "updated", "terminal",
    "tab_id", "claude_pane", "shell_pane", "prompt",
)


def normalize_record(rec, name):
    """Fill missing fields and force the name from the directory, never from
    the file (finding 1)."""
    for key in RECORD_FIELDS:
        rec.setdefault(key, "")
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
        if entry == ARCHIVE_SUBDIR or not valid_name(entry):
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


def applescript_quote(s):
    """%q-style AppleScript string literal: escape backslash and double quote.

    Newlines cannot sit inside a literal safely, so they become linefeed
    concatenations; the result is a complete AppleScript expression.
    """
    parts = []
    for i, piece in enumerate(str(s).split("\n")):
        esc = piece.replace("\\", "\\\\").replace('"', '\\"')
        parts.append('"' + esc + '"')
    return " & linefeed & ".join(parts)


class Terminal:
    """The seam. One implementation today (ITerm); tmux is reserved."""

    kind = ""

    def create_split_tab(self, name, upper_cmd, lower_cmd):
        """-> {"tab_id", "claude_pane", "shell_pane"}; raises UserError."""
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


class ITerm(Terminal):
    kind = "iterm"

    def osascript_argv(self):
        return shlex.split(os.environ.get("SAVANNAH_OSASCRIPT", "osascript"))

    def run(self, script):
        base = self.osascript_argv()
        if not base or not base[0]:
            raise UserError("SAVANNAH_OSASCRIPT is empty")
        argv = base + ["-e", script]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        except FileNotFoundError:
            raise UserError(f"osascript not found ({argv[0]!r}); set SAVANNAH_OSASCRIPT")
        except subprocess.TimeoutExpired:
            raise UserError("osascript timed out: iTerm2 is not responding, or a macOS "
                            "Automation permission dialog is waiting for you (System "
                            "Settings > Privacy & Security > Automation)")
        except OSError as e:
            raise UserError(f"cannot run osascript ({argv[0]!r}): {e.strerror or e}")
        if p.returncode != 0:
            msg = p.stderr.strip().splitlines()
            raise UserError("osascript failed: " + (msg[-1] if msg else f"rc {p.returncode}"))
        return p.stdout

    @staticmethod
    def truthy(out):
        return out.strip().splitlines()[:1] == ["true"]

    def create_split_tab(self, name, upper_cmd, lower_cmd):
        # Return values are plain lines so parsing is trivial: tab id, then the
        # upper (Claude) session id, then the lower (shell) session id. iTerm2
        # tabs have no id property (and "index of tab" is not gettable live),
        # so the tab id is "w<window id>t<tab position at creation>", taken as
        # the tab count right after the tab is added. Informational only.
        # This is the ONE script allowed to launch iTerm2. When it has to create
        # the window, the window's own first tab is used (no stray empty tab).
        # Ids are read with explicit "get" (bare property references on a
        # returned tab fail to coerce; found live, 10/27/2026), and any
        # failure after the tab exists closes it so no orphan survives.
        script = f"""tell application "iTerm2"
  set madeWindow to false
  try
    set w to current window
  on error
    set w to missing value
  end try
  if w is missing value then
    set w to (create window with default profile)
    set madeWindow to true
  end if
  if madeWindow then
    set t to current tab of w
  else
    tell w
      set t to (create tab with default profile)
    end tell
  end if
  try
    set cs to current session of t
    set tabId to "w" & ((get id of w) as text) & "t" & ((count of tabs of w) as text)
    set csId to (get id of cs) as text
    tell cs
      set name to {applescript_quote(name)}
      write text {applescript_quote(upper_cmd)}
    end tell
    set ss to (split horizontally with same profile cs)
    set ssId to (get id of ss) as text
    tell ss
      write text {applescript_quote(lower_cmd)}
    end tell
  on error msg number num
    try
      close t
    end try
    error msg number num
  end try
  return tabId & linefeed & csId & linefeed & ssId
end tell"""
        lines = [ln.strip() for ln in self.run(script).splitlines() if ln.strip()]
        if len(lines) < 3:
            raise UserError("osascript returned an unexpected reply while creating the tab")
        return {"tab_id": lines[0], "claude_pane": lines[1], "shell_pane": lines[2]}

    def write(self, pane_id, text):
        # The guard keeps a plain "tell" from launching iTerm2 when it is not
        # running: a closed iTerm2 means the pane is gone, not "start iTerm2".
        script = f"""{ITERM_RUNNING_GUARD}
set found to false
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if id of s is {applescript_quote(pane_id)} then
          tell s to write text {applescript_quote(text)}
          set found to true
        end if
      end repeat
    end repeat
  end repeat
end tell
return found"""
        return self.truthy(self.run(script))

    def focus(self, pane_id):
        # Mirrors dash/sessions.go jumpSessionCmd: select window, tab, session;
        # activate iTerm only when found so a miss does not yank it forward.
        script = f"""{ITERM_RUNNING_GUARD}
set found to false
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if id of s is {applescript_quote(pane_id)} then
          select w
          tell t to select
          select s
          set found to true
        end if
      end repeat
    end repeat
  end repeat
  if found then activate
end tell
return found"""
        return self.truthy(self.run(script))

    def close_tab(self, tab_id, panes=()):
        # Tab indices shift as tabs close, so the tab is located by the pane
        # ids it was created with, never by the index baked into tab_id.
        ids = [p for p in panes if p]
        if not ids:
            return False
        cond = " or ".join(f"id of s is {applescript_quote(p)}" for p in ids)
        script = f"""{ITERM_RUNNING_GUARD}
set target to missing value
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if {cond} then set target to t
      end repeat
    end repeat
  end repeat
  if target is missing value then return false
  close target
end tell
return true"""
        return self.truthy(self.run(script))


def make_terminal(kind):
    if kind in ("iterm", ""):
        return ITerm()
    raise UserError(f"terminal {kind!r} is not implemented (only iterm today)")


# --------------------------------------------------------------------------
# git


def run_git(args):
    try:
        return subprocess.run(["git"] + args, capture_output=True, text=True)
    except FileNotFoundError:
        raise UserError("git not found on PATH")
    except OSError as e:
        raise UserError(f"cannot run git: {e.strerror or e}")


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
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn)) as f:
                s = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(s, dict):
            out.append(s)
    return out


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
    if not matches:
        return None
    for h in matches:
        if rec.get("claude_pane") and iterm_uuid(h.get("iterm_session_id")) == rec["claude_pane"]:
            return h
    return max(matches, key=lambda h: h.get("updated") or 0)


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
    return entry


# --------------------------------------------------------------------------
# verbs


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

        prompt = a.prompt or ""
        upper = f"cd {shlex.quote(worktree)} && claude"
        if prompt:
            upper += " " + shlex.quote(prompt)
        lower = f"cd {shlex.quote(worktree)}"

        ids = term.create_split_tab(name, upper, lower)

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
        }
        atomic_write(record_path(name), rec)
        if a.note:
            append_note(name, a.note)
    except BaseException:
        # Nothing of ours should survive a failed new, whatever failed: the
        # claimed record dir, the fresh worktree (nothing in it yet), the
        # branch we just made, and the tab if it already exists (a record
        # write failing after the tab is up would otherwise orphan it). A
        # reattached or --branch branch is left alone.
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
    if a.json:
        print(json.dumps(entries))
        return 0
    if not entries:
        print("(no work sessions)")
        return 0

    def clip(s, n):
        s = (s or "").splitlines()[0] if (s or "").strip() else ""
        return s if len(s) <= n else s[: n - 1] + "~"

    rows = [("NAME", "BRANCH", "STATE", "LAST PROMPT", "LAST NOTE")]
    for e in entries:
        rows.append((e["name"], e["branch"] or "-", e["state"],
                     clip(e["last_prompt"], 40), clip(e["last_note"], 40)))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(4)) + "  " + r[4])
    return 0


def cmd_note(a):
    rec = load_record(a.name)
    text = " ".join(a.text) if a.text else sys.stdin.read()
    if not text.strip():
        return usage_error("note", "no text given (pass words or pipe text on stdin)")
    now = time.time()
    path = append_note(a.name, text, now)
    save_record(rec)
    if a.json:
        print(json.dumps({"name": a.name, "notes_path": path,
                          "time": note_stamp(now), "text": text.rstrip("\n")}))
    else:
        print(f"noted in {path}")
    return 0


def cmd_notes(a):
    load_record(a.name)
    sys.stdout.write(read_notes(a.name))
    return 0


def type_into(rec, pane_key, text, label):
    term = make_terminal(rec.get("terminal", ""))
    pane = rec.get(pane_key, "")
    if not pane or not term.write(pane, text):
        raise UserError(f"{label} pane of {rec['name']} no longer exists")
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
        raise UserError(f"{key.replace('_', ' ')} of {rec['name']} not found")
    return 0


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
            print(f"removed {name} (stale: {why}; check `git worktree list` for leftovers)")
            return 0

    rec = load_record(name)
    worktree = rec.get("worktree", "")
    repo = rec.get("repo", "")
    has_worktree = bool(worktree) and os.path.realpath(worktree) != os.path.realpath(repo or "")
    removing_worktree = has_worktree and not a.keep_worktree

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

    try:
        term = make_terminal(rec.get("terminal", ""))
        term.close_tab(rec.get("tab_id", ""), (rec.get("claude_pane", ""), rec.get("shell_pane", "")))
    except UserError:
        pass  # best effort; the tab may be long gone, or iTerm2 not running

    try:
        shutil.rmtree(rdir)
    except OSError as e:
        raise UserError(f"cannot remove {rdir}: {e.strerror or e}"
                        + (f" (notes already archived to {archived})" if archived else ""))
    print(f"removed {name}" + (" (worktree kept)" if has_worktree and a.keep_worktree else ""))
    return 0


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

    p = sub.add_parser("sh", help="type a command into the shell pane (use -- before text starting with -)")
    p.add_argument("name")
    p.add_argument("cmd", nargs="+")
    p.set_defaults(fn=cmd_sh)

    p = sub.add_parser("jump", help="focus the Claude pane (or --shell)")
    p.add_argument("name")
    p.add_argument("--shell", action="store_true")
    p.set_defaults(fn=cmd_jump)

    p = sub.add_parser("rm", help="close tab, remove worktree, archive notes")
    p.add_argument("name")
    p.add_argument("--keep-worktree", action="store_true")
    p.add_argument("--force", action="store_true", help="discard a dirty worktree")
    p.set_defaults(fn=cmd_rm)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)  # argparse exits 2 on bad usage
    try:
        return a.fn(a)
    except UserError as e:
        print(f"{a.verb}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
