# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_snapshot - the `snapshot`, `snapshots` and `restore` verbs
# (savannah_work.py is the entry point; this is a flat sibling module).
#
# The whole layout saved before a reboot or an iTerm2 relaunch (every work
# session and every other open Claude Code tab, in tab order, with the
# Claude session ids), and put back afterwards: tabs remade, each Claude
# reloading its own conversation with --resume. Files:
# <work dir>/snapshots/<YYYYMMDD-HHMMSS>.json plus latest.json.
#
# Imports: the standard library, savannah_log, savannah_core,
# savannah_terminal, savannah_sessions, savannah_transcript and
# savannah_tabs.

import json
import os
import re
import time

from savannah_log import log_debug, log_info, log_warn
import savannah_core
from savannah_core import (coerce_int, display_path, emit, first_line, list_records, load_record,
                           print_table, UserError, valid_name, work_dir)
from savannah_terminal import make_terminal
from savannah_sessions import claude_alive, hook_pane, hook_session_by_id, join_session, load_hook_sessions
from savannah_transcript import find_transcript, transcript_for
from savannah_tabs import claude_command, reclaim_pane, remake_tab

SNAPSHOT_LATEST = "latest.json"  # a copy of the newest snapshot, what restore takes by default
SNAPSHOT_STAMP_RE = re.compile(r"\d{8}-\d{6}(-\d+)?")   # 20261105-223012, -2 when one second holds two


def snapshots_dir():
    return os.path.join(work_dir(), savannah_core.SNAPSHOTS_SUBDIR)


def snapshot_stamp(now=None):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


def snapshot_file(stamp):
    return os.path.join(snapshots_dir(), stamp + ".json")


def tab_title(title):
    """An it2 tab title without iTerm2's trailing " (process)", exactly as
    the dash's cleanTitle: "demo (claude)" -> "demo", "~/x (-zsh)" -> "~/x",
    "(claude)" alone -> "". (title_text is the record's own free-text
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


def tab_layout(listed):
    """What `it2 session list` says about each pane, keyed by pane id:
    (order, tabs, titles) = its place in iTerm2's tab order, its tab id, its
    tab title without the process (tab_title). A pane listed twice counts
    once, where it first appears."""
    order, tabs, titles = {}, {}, {}
    for i, s in enumerate(listed):
        sid = s.get("id") if isinstance(s, dict) else None
        if isinstance(sid, str) and sid and sid not in order:
            order[sid] = i
            tabs[sid] = s.get("tab_id", "") if isinstance(s.get("tab_id"), str) else ""
            titles[sid] = tab_title(s.get("title", "") if isinstance(s.get("title"), str) else "")
    return order, tabs, titles


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
    order, tabs, titles = tab_layout(listed)

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
                 "priority": rec.get("priority", savannah_core.DEFAULT_PRIORITY), "rank": rec.get("rank", 0),
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
        pane = hook_pane(h)
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
    savannah_core.atomic_write(path, snap)
    savannah_core.atomic_write(os.path.join(d, SNAPSHOT_LATEST), snap)
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


def snapshot_str(entry, key):
    v = entry.get(key)
    return v if isinstance(v, str) else ""


def cmd_snapshot(a):
    snap = take_snapshot(a.note)
    path = write_snapshot(snap)
    live = sum(1 for e in snap["sessions"] if e["live"])
    log_info("snapshot written", path=path, work=len(snap["sessions"]), live=live,
             plain=len(snap["plain"]), it2=snap["it2"])
    tail = "" if snap["it2"] else " (it2 unavailable: no tab order, titles or pane liveness)"
    emit(a, {"path": path, "stamp": snap["stamp"], "work": len(snap["sessions"]),
             "live": live, "plain": len(snap["plain"]), "it2": snap["it2"]},
         f"snapshot {path}: {len(snap['sessions'])} work sessions ({live} live), "
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
        table.append((r["stamp"], r["taken_local"] or "-", str(r["work"]), str(r["live"]),
                      str(r["plain"]), first_line(r["note"])))
    print_table(table)
    return 0


class RestoreRun:
    """One restore as it goes: what the two halves (restore_work,
    restore_plain) need to look at (the terminal, the panes alive now, the
    hook sessions, whether this is a dry run) and what they have done so
    far: restored and plain (the JSON lists of remade tabs), skipped,
    failed, and lines, the same story for a person in the order it
    happened."""

    def __init__(self, term, live, hooks, dry_run):
        self.term, self.live, self.hooks, self.dry_run = term, live, hooks, dry_run
        self.restored, self.plain, self.skipped, self.failed, self.lines = [], [], [], [], []

    def skip(self, key, value, why):
        # JSON carries the full name or project path; the human line shows ~/.
        self.skipped.append({key: value, "why": why})
        self.lines.append(f"skipped {display_path(value)}: {why}")
        log_debug("restore skip", **{key: value, "why": why})

    def fail(self, key, value, err):
        self.failed.append({key: value, "error": str(err)})
        self.lines.append(f"failed {display_path(value)}: {err}")
        log_warn("restore failed", **{key: value, "error": str(err)})


def restore_how(entry, rec, hooks):
    """What a remade tab's Claude starts with, for a snapshot entry and the
    record whose worktree it runs in (a plain tab passes a bare
    {"worktree": project} and no hooks): "resume" when the snapshot's own
    conversation is on disk, else "continue" when the directory has any
    transcript, else "fresh". -> (how, howtext); howtext is how for a
    person, with the session id on a resume."""
    session_id = snapshot_str(entry, "session_id")
    if transcript_for(session_id, snapshot_str(entry, "transcript_path"), rec["worktree"]):
        return "resume", f"resume {session_id}"
    if find_transcript(rec, hooks)[0]:
        return "continue", "continue"
    return "fresh", "fresh"


def restore_work(run, entries):
    """The work sessions of a snapshot, in its order: a record whose Claude
    pane is still there (or was started again by hand, reclaim_pane) is
    left alone; one whose tab is gone gets a fresh tab and split
    (remake_tab)."""
    for entry in entries:
        name = entry["name"]
        if not valid_name(name):
            run.skip("name", name, "bad name")
            continue
        try:
            rec = load_record(name)
        except UserError:
            run.skip("name", name, "no record")
            continue
        if claude_alive(rec, run.live) or reclaim_pane(rec, run.live, dry=run.dry_run):
            run.skip("name", name, "alive")
            continue
        worktree = rec.get("worktree") or ""
        if not worktree or not os.path.isdir(worktree):
            run.skip("name", name, "worktree gone")
            continue
        how, howtext = restore_how(entry, rec, run.hooks)
        if run.dry_run:
            run.restored.append({"name": name, "how": how, "tab_id": ""})
            run.lines.append(f"would restore {name} ({howtext})")
            continue
        try:
            remake_tab(rec, run.term, run.live, how=how, resume_id=snapshot_str(entry, "session_id"))
        except UserError as e:
            run.fail("name", name, e)
            continue
        run.restored.append({"name": name, "how": how, "tab_id": rec.get("tab_id", "")})
        run.lines.append(f"restored {name} ({howtext}, tab {rec.get('tab_id', '')})")


def restore_plain(run, entries):
    """The other Claude Code tabs a snapshot saw: each comes back as a
    single pane in its directory, unless it was already closed when the
    snapshot was taken, is open now, or its directory is gone."""
    for entry in entries:
        project = entry["project"]
        label = display_path(project)
        session_id = snapshot_str(entry, "session_id")
        if entry.get("pane_alive") is False:
            run.skip("project", project, "not open when the snapshot was taken")
            continue
        h = hook_session_by_id(session_id) if session_id else None
        if h is not None and hook_pane(h) in run.live:
            run.skip("project", project, "alive")
            continue
        if not os.path.isdir(project):
            run.skip("project", project, "project gone")
            continue
        how, howtext = restore_how(entry, {"worktree": project}, [])
        if run.dry_run:
            run.plain.append({"project": project, "how": how, "tab_id": ""})
            run.lines.append(f"would open plain {label} ({howtext})")
            continue
        title = snapshot_str(entry, "title") or os.path.basename(project.rstrip(os.sep)) or project
        cmd = claude_command(project, "", "", resume=(how == "continue"),
                             resume_id=(session_id if how == "resume" else ""))
        try:
            ids = run.term.create_tab(title, cmd)
        except UserError as e:
            run.fail("project", project, e)
            continue
        run.plain.append({"project": project, "how": how, "tab_id": ids["tab_id"]})
        run.lines.append(f"plain {label} ({howtext}, tab {ids['tab_id']})")
        log_info("plain tab remade", project=project, how=how, tab=ids["tab_id"])


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
    run = RestoreRun(term, live, load_hook_sessions(), a.dry_run)
    restore_work(run, snap["sessions"])
    if not a.no_plain:
        restore_plain(run, snap["plain"])

    counts = (f"{len(run.restored)} work sessions, {len(run.plain)} plain tabs, "
              f"{len(run.skipped)} skipped, {len(run.failed)} failed")
    log_info("restore", stamp=stamp, dry_run=bool(a.dry_run), restored=len(run.restored),
             plain=len(run.plain), skipped=len(run.skipped), failed=len(run.failed))
    if a.json:
        print(json.dumps({"stamp": stamp, "path": path, "dry_run": bool(a.dry_run), "restored": run.restored,
                          "plain": run.plain, "skipped": run.skipped, "failed": run.failed}))
    else:
        for ln in run.lines:
            print(ln)
        if a.dry_run:
            print(f"plan from snapshot {stamp}: {counts} (nothing touched)")
        else:
            print(f"restored from snapshot {stamp}: {counts}")
    return 1 if run.failed else 0
