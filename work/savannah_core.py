# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_core - the bottom layer of local work sessions (the verbs are in
# savannah_work.py beside this file, which is the only entry point; this is
# a flat sibling module imported by plain name, not a package).
#
# Holds what every other work module stands on: UserError (exit 1 with a
# one-line message), the constants of the storage layout, the paths under
# $SAVANNAH_WORK_DIR (read from the environment at call time, never kept),
# atomic writes, records (load/save/list/new_record/normalize_record,
# claim_record_dir, session_here), notes, the status file, the small text
# helpers (first_line, clip, title_text, coerce_int, display_path) and the
# output helpers every verb shares (emit, print_table, usage_error).
#
# Imports: the standard library only. Nothing else of ours, and never
# savannah_work. A module-level value lives here once: the other
# modules read a constant as savannah_core.NAME and call
# savannah_core.atomic_write(...) at call time, so a test that patches
# either one here is seen everywhere.

import json
import os
import re
import shlex
import shutil
import sys
import time

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RESERVED_RE = re.compile(r"t-[0-9]+")  # mesh task ids
WORKTREES_SUBDIR = ".savannah-worktrees"
ARCHIVE_SUBDIR = "archive"   # notes graveyard under the work dir; never a session name
SNAPSHOTS_SUBDIR = "snapshots"   # saved layouts (work snapshot) under the work dir; never a session name
STATUS_FILE = "status.json"        # the tab's own Claude's poll report, beside record.json
STATUS_TEXT_MAX = 200
TITLE_MAX = 120                    # chars of a session's free-text title (first line, stripped)
PRIORITIES = ("now", "active", "later")   # the board's sections, top first; rank orders rows within one
DEFAULT_PRIORITY = "active"        # where a new session lands, and how a record that names none reads


# --------------------------------------------------------------------------
# paths and records


def env_path(var, *default):
    """$var when it is set and not empty, else ~/.claude/<default...>."""
    return os.environ.get(var) or os.path.join(os.path.expanduser("~"), ".claude", *default)


def work_dir():
    return env_path("SAVANNAH_WORK_DIR", "savannah", "work")


def record_dir(name):
    return os.path.join(work_dir(), name)


def record_path(name):
    return os.path.join(record_dir(name), "record.json")


def notes_path(name):
    return os.path.join(record_dir(name), "notes.md")


def status_path(name):
    return os.path.join(record_dir(name), STATUS_FILE)


def atomic_write_text(path, text, encoding=None):
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding=encoding) as f:
        f.write(text)
    os.replace(tmp, path)  # atomic on POSIX; a reader never sees a half file


def atomic_write(path, obj):
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")


def oserr(e):
    """What an OSError says for a one-line message: its strerror, else itself."""
    return e.strerror or e


def emit(a, obj, line):
    """A verb's one-value result: obj as JSON under --json, else the line."""
    print(json.dumps(obj) if a.json else line)


def print_table(rows):
    """Rows of strings (the header first) as columns two spaces apart: every
    column but the last padded to its widest cell, the last left as it is."""
    n = len(rows[0]) - 1
    widths = [max(len(r[i]) for r in rows) for i in range(n)]
    for r in rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(n)) + "  " + r[n])


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


def first_line(text, limit=None):
    """The first non-blank line of text, stripped, cut to limit chars when
    one is given; "" for blank text or a non-string."""
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line if limit is None else line[:limit]
    return ""


def clip(s, n):
    """s when it fits in n chars, else cut to n with a closing "~"."""
    return s if len(s) <= n else s[: n - 1] + "~"


def title_text(value):
    """A session's free-text title as stored and shown: its first non-blank
    line, stripped, cut to TITLE_MAX; "" for blank text or a non-string
    (a title is for your eye, so the name rule does not apply to it).
    Not tab_title, which strips iTerm2's " (process)" off a tab title."""
    return first_line(value, TITLE_MAX)


def normalize_record(rec, name):
    """Fill missing fields and force the name from the directory, never from
    the file (finding 1). `adopted` (a tab the owner opened, taken in by
    `adopt`) is always a real boolean; records written before it existed
    read False. `priority` is "active" unless the file says "now" or "later", and
    `rank` is an int: the file's when it parses, else the creation time, so
    a record from before the board had sections keeps sorting by age.
    `title` is always a str (title_text), "" for a record from before it."""
    for key in RECORD_FIELDS:
        rec.setdefault(key, "")
    rec["adopted"] = bool(rec.get("adopted"))
    rec["priority"] = rec.get("priority") if rec.get("priority") in PRIORITIES else DEFAULT_PRIORITY
    rec["rank"] = coerce_int(rec.get("rank"), coerce_int(rec.get("created"), 0))
    rec["title"] = title_text(rec.get("title"))
    rec["name"] = name
    return rec


def new_record(name, repo, worktree, branch, terminal, tab_id="", claude_pane="", shell_pane="",
               prompt="", adopted=False, title=""):
    """A fresh record as new and adopt write it: created, updated and rank
    stamped now (a new session ranks by its creation time), the default
    priority, the title through title_text."""
    now = int(time.time())
    return {
        "name": name,
        "repo": repo,
        "worktree": worktree,
        "branch": branch,
        "created": now,
        "updated": now,
        "terminal": terminal,
        "tab_id": tab_id,
        "claude_pane": claude_pane,
        "shell_pane": shell_pane,
        "prompt": prompt,
        "adopted": adopted,
        "priority": DEFAULT_PRIORITY,
        "rank": now,
        "title": title_text(title),
    }


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


def iterm_uuid(iterm_session_id):
    s = iterm_session_id or ""
    return s.split(":", 1)[1] if ":" in s else s


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
    return first_line(" ".join(parts), STATUS_TEXT_MAX)


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
                raise UserError(f"cannot reclaim stale {rdir}: {oserr(e)}")
        except OSError as e:
            raise UserError(f"cannot create {rdir}: {oserr(e)}")
    raise UserError(f"work session already exists: {name}")


def claude_argv():
    return shlex.split(os.environ.get("SAVANNAH_CLAUDE_BIN", "") or "claude")


def display_path(p):
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p
