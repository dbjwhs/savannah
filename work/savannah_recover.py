# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_recover - the `recover` verb: after a reboot, a crash or a forced
# update took every Claude down, start each one again where it was
# (savannah_work.py is the entry point; this is a flat sibling module).
#
# No snapshot is needed. The hook writes a status file per Claude Code
# session on every event and removes it when the session ends, so a file
# whose Claude process is gone is a session that never got to end: it was
# open when the machine went down. The file names the conversation
# (session_id), the directory, the iTerm2 pane and the process id. For each
# such session, `recover` types `claude --resume <id>` (with `-n <name>` for
# a work session)
#
#   1. into its own pane, when iTerm2 brought the tab back under the same
#      id and nothing runs there but a shell;
#   2. else into a pane that sits idle in the session's directory (iTerm2
#      reopened the tab, but under a new id), and a work record is pointed
#      at it;
#   3. else into a new tab (remake_tab for a work session, a single pane for
#      a plain tab).
#
# What it never does: touch a session whose Claude runs (the pid is alive
# and older than the file's last event, so a pid the system handed to
# another process after the reboot does not count), type into a pane where
# something other than a shell runs, close anything, or recover a session
# twice (the status file is stamped `recovered`/`recovered_pane`, and the
# stamp holds while that pane lives and no newer event has been written).
# A session whose pane is gone and that was last active longer ago than
# --within (7 days) is a leftover of an older crash, not of this one, and is
# listed, not recovered, unless it is named. A pane iTerm2 brought back
# says the tab was open, however long it had sat idle.
#
# Imports: the standard library, savannah_log, savannah_core,
# savannah_terminal, savannah_sessions, savannah_tabs, savannah_snapshot and
# savannah_scan.

import calendar
import json
import os
import re
import subprocess
import time

from savannah_log import log_debug, log_info, log_warn
import savannah_core
from savannah_core import coerce_int, display_path, iterm_uuid, list_records, save_record, UserError
from savannah_terminal import make_terminal
from savannah_sessions import hook_pane, join_session, load_hook_sessions, sessions_dir
from savannah_tabs import claude_command, remake_tab
from savannah_snapshot import restore_how
import savannah_scan

WITHIN_DEFAULT = "7d"
WITHIN_UNITS = {"m": 60, "h": 3600, "d": 86400, "": 3600}
SHELLS = {"login", "zsh", "bash", "sh", "fish", "tcsh", "csh", "ksh", "dash", "nu"}
START_SLACK = 5   # seconds a process may seem to start after the event it fired (clock grain)


def parse_within(text):
    """--within as seconds: 90m, 72h, 7d, a bare number of hours; "all" is
    no limit (0). Raises UserError on anything else."""
    t = (text or "").strip().lower()
    if t == "all":
        return 0
    m = re.fullmatch(r"(\d+)\s*([mhd]?)", t)
    if not m or int(m.group(1)) == 0:
        raise UserError(f"--within {text!r}: a duration like 90m, 72h or 7d, or all")
    return int(m.group(1)) * WITHIN_UNITS[m.group(2)]


def age_text(seconds):
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def start_epoch(lstart):
    """ps's lstart in UTC ("Sat Nov 14 06:27:00 2026") as epoch seconds,
    None when it does not read that way."""
    try:
        return calendar.timegm(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
    except (TypeError, ValueError):
        return None


def claude_state(h, info=None):
    """Whether the Claude Code process a status file names still runs:
    "running", "gone", or "unknown" when the file names no process. A
    process that started after the file's last event is another one that
    was handed the same pid (after a reboot the numbers start over), so the
    session's own Claude is gone."""
    pid = h.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return "unknown"
    got = (info or savannah_scan.process_info)(pid)
    if got is None:
        return "gone"
    started = start_epoch(got[1])
    updated = coerce_int(h.get("updated"), 0)
    if started is not None and updated > 0 and started > updated + START_SLACK:
        return "gone"
    return "running"


def tty_table():
    """{"/dev/ttys003": [(pid, command name, foreground), ...]} for every
    process with a controlling terminal, in ps order; {} when ps cannot be
    run. foreground = the process is in its terminal's foreground process
    group (the "+" in ps's state): what the keyboard reaches."""
    try:
        p = subprocess.run(["ps", "-axo", "tty=,pid=,stat=,comm="], capture_output=True, text=True,
                           env=dict(os.environ, LC_ALL="C"))
    except OSError:
        return {}
    out = {}
    if p.returncode != 0:
        return out
    for line in p.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or parts[0].startswith("?") or not parts[1].isdigit():
            continue
        out.setdefault("/dev/" + parts[0], []).append((int(parts[1]), parts[3].strip(), "+" in parts[2]))
    return out


def pane_states(panes, table):
    """What runs in each pane, by its tty: {pane id: {"busy": the name of the
    first foreground process that is not a shell ("" when the keyboard
    reaches a shell), "shell": the pid of its innermost shell (0 when
    none)}}. A background process is not what the pane runs: a prompt
    theme's helper (gitstatusd) or a job sent to the background leaves the
    shell at its prompt. A pane whose tty has no process in the table (or no
    tty) is left out: nothing is known of it."""
    out = {}
    for s in panes:
        pane, tty = s.get("id"), s.get("tty")
        if not isinstance(pane, str) or not isinstance(tty, str) or not table.get(tty):
            continue
        busy, shell = "", 0
        for pid, comm, foreground in table[tty]:
            base = os.path.basename(comm).lstrip("-")
            if base not in SHELLS:
                if foreground:
                    busy = busy or base
            elif base != "login":
                shell = pid
        out[pane] = {"busy": busy, "shell": shell}
    return out


def shell_cwd(pid):
    """The working directory of a process ("" when it cannot be read): the
    directory iTerm2 reopened a tab in is its shell's."""
    try:
        p = subprocess.run(["lsof", "-a", "-d", "cwd", "-Fn", "-p", str(pid)], capture_output=True, text=True)
    except OSError:
        return ""
    for line in p.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return line[1:]
    return ""


def candidates(hooks, records):
    """Every status file with a session id and a directory as (record or
    None, hook): a work session's joined one under its record, the rest
    plain, newest event first (when two files name one pane, the newer is
    the conversation that was in it)."""
    out, claimed = [], set()
    for rec in records:
        h = join_session(rec, hooks)
        if h is not None and id(h) not in claimed:
            claimed.add(id(h))
            out.append((rec, h))
    out += [(None, h) for h in hooks if id(h) not in claimed]
    out = [(rec, h) for rec, h in out
           if isinstance(h.get("session_id"), str) and h["session_id"]
           and isinstance(h.get("project"), str) and h["project"]]
    out.sort(key=lambda t: coerce_int(t[1].get("updated"), 0), reverse=True)
    return out


def select(items, wanted):
    """The candidates the command line names (a work session's name, a
    session id or the start of one); all of them when it names none. Raises
    UserError for a word that names nothing."""
    if not wanted:
        return items, set()
    picked, named = [], set()
    for w in wanted:
        hit = [(rec, h) for rec, h in items
               if (rec is not None and rec.get("name") == w) or h["session_id"] == w]
        if not hit and len(w) >= 6:
            hit = [(rec, h) for rec, h in items if h["session_id"].startswith(w)]
        if len(hit) != 1:
            raise UserError(f"{'several sessions match' if hit else 'no recorded session'} {w} "
                            "(work recover --dry-run lists them)")
        if id(hit[0][1]) not in named:
            named.add(id(hit[0][1]))
            picked.append(hit[0])
    return picked, named


class Recovery:
    """One recover as it goes: the terminal as it is now (the panes, what
    runs in each, which a running Claude or a record holds), and what has
    been done so far: recovered, skipped, failed (the JSON lists) and lines,
    the same story for a person."""

    def __init__(self, term, panes, hooks, records, dry_run, within, now):
        self.term, self.hooks, self.dry_run, self.within, self.now = term, hooks, dry_run, within, now
        self.order = [s["id"] for s in panes if isinstance(s.get("id"), str) and s["id"]]
        self.live = set(self.order)
        self.tabs = {s["id"]: s.get("tab_id") if isinstance(s.get("tab_id"), str) else ""
                     for s in panes if isinstance(s.get("id"), str)}
        self.states = pane_states(panes, tty_table())
        self.held = {hook_pane(h) for h in hooks if claude_state(h) == "running"} - {""}
        self.claims = {}   # pane -> the record that names it (its Claude pane or its shell pane)
        for r in records:
            for key in ("claude_pane", "shell_pane"):
                if r.get(key):
                    self.claims[r[key]] = r.get("name", "")
        self.here = iterm_uuid(os.environ.get("ITERM_SESSION_ID", ""))
        # Panes this run has typed into (or would), and the ones an earlier
        # run did: an older file on such a pane is not that pane's session.
        self.taken = {h["recovered_pane"] for h in hooks if recovered_already(h, self.live)}
        self.cwds = {}
        self.recovered, self.skipped, self.failed, self.lines = [], [], [], []

    def ident(self, rec, h):
        return {"name": rec["name"] if rec else "", "session_id": h["session_id"], "project": h["project"]}

    def label(self, rec, h):
        return rec["name"] if rec else f"{display_path(h['project'])} ({h['session_id'][:8]})"

    def skip(self, rec, h, why, quiet=False):
        self.skipped.append(dict(self.ident(rec, h), why=why))
        if not quiet:
            self.lines.append(f"skipped {self.label(rec, h)}: {why}")
        log_debug("recover skip", session=h["session_id"], name=rec["name"] if rec else "", why=why)

    def fail(self, rec, h, err):
        self.failed.append(dict(self.ident(rec, h), error=str(err)))
        self.lines.append(f"failed {self.label(rec, h)}: {err}")
        log_warn("recover failed", session=h["session_id"], name=rec["name"] if rec else "", error=str(err))

    def cwd_of(self, pane):
        if pane not in self.cwds:
            shell = self.states.get(pane, {}).get("shell", 0)
            cwd = shell_cwd(shell) if shell else ""
            self.cwds[pane] = os.path.realpath(cwd) if cwd else ""
        return self.cwds[pane]

    def idle_pane_in(self, project, name, tab=""):
        """A pane that sits at a shell in project and is nobody's: not
        typed into by this run, not a running Claude's, not another record's,
        not the one this command runs in. tab narrows it to that tab."""
        target = os.path.realpath(project)
        for pane in self.order:
            if pane in self.taken or pane in self.held or pane == self.here:
                continue
            if self.claims.get(pane, name) != name or (tab and self.tabs.get(pane) != tab):
                continue
            state = self.states.get(pane)
            if state is None or state["busy"] or not state["shell"]:
                continue
            if self.cwd_of(pane) == target:
                return pane
        return ""

    def own_pane(self, rec, h):
        """(pane, why not): the session's own pane when iTerm2 still has it
        and it can be typed into; ("", reason) when it is there but must be
        left alone; ("", "") when it is gone."""
        for pane in dict.fromkeys(p for p in (hook_pane(h), (rec or {}).get("claude_pane", "")) if p):
            if pane not in self.live:
                continue
            if pane in self.held:
                return "", "its pane now runs another Claude session"
            if pane in self.taken:
                return "", "a newer session of its pane is the one recovered"
            if pane == self.here:
                return "", "its pane is the one this command runs in"
            busy = self.states.get(pane, {}).get("busy", "")
            if busy:
                return "", f"its pane is busy ({busy} runs there)"
            return pane, ""
        return "", ""


def recovered_already(h, live):
    """The stamp an earlier recover left still stands: its pane is there and
    the hook has written no event since."""
    pane = h.get("recovered_pane")
    return (isinstance(pane, str) and pane in live
            and coerce_int(h.get("recovered"), 0) >= coerce_int(h.get("updated"), 0) > 0)


def stamp_recovered(session_id, pane, now):
    """Mark the status file so a second recover leaves the session alone
    while its Claude starts (its first event rewrites the file with the new
    process). Best effort: a file that cannot be written is logged."""
    path = os.path.join(sessions_dir(), session_id + ".json")
    try:
        with open(path) as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            return
        doc["recovered"], doc["recovered_pane"] = max(int(now), coerce_int(doc.get("updated"), 0)), pane
        savannah_core.atomic_write(path, doc)
    except (OSError, ValueError) as e:
        log_warn("recover: status file not stamped", session=session_id, error=str(e))


def repoint(run, rec, pane):
    """A work record whose Claude was started in another pane than the one
    it names: the record follows. A shell pane that is gone is replaced by
    an idle pane of the same tab in the worktree, else dropped."""
    if rec.get("claude_pane") == pane:
        return
    was = rec.get("claude_pane", "")
    rec["claude_pane"], rec["tab_id"] = pane, run.tabs.get(pane, "")
    if rec.get("shell_pane") not in run.live:
        rec["shell_pane"] = run.idle_pane_in(rec["worktree"], rec["name"], tab=rec["tab_id"]) if rec["tab_id"] else ""
        if rec["shell_pane"]:
            run.taken.add(rec["shell_pane"])
    save_record(rec)
    log_info("record re-pointed", name=rec["name"], was=was, claude_pane=pane, shell_pane=rec["shell_pane"])


def recover_one(run, rec, h, named):
    """One recorded session: left alone with a reason, or its Claude started
    again (own pane, an idle pane in its directory, or a new tab)."""
    sid, project = h["session_id"], h["project"]
    state = claude_state(h)
    if state == "running":
        return run.skip(rec, h, "running", quiet=True)
    if state == "unknown":
        return run.skip(rec, h, "its status file names no process, so whether its Claude runs is not known")
    if recovered_already(h, run.live):
        return run.skip(rec, h, f"already recovered (pane {h['recovered_pane']})")
    worktree = (rec or {}).get("worktree") or project
    if not os.path.isdir(worktree):
        return run.skip(rec, h, "directory gone")
    entry = {"session_id": sid, "transcript_path": h.get("transcript_path")}
    how, howtext = restore_how(entry, rec or {"worktree": project}, run.hooks if rec else [])
    if rec is None and how != "resume":
        # --continue would load whatever conversation is the directory's
        # latest, which is another tab's as likely as this one's.
        return run.skip(rec, h, "its conversation is not on disk")

    pane, why_not = run.own_pane(rec, h)
    if why_not:
        return run.skip(rec, h, why_not)
    where = "its pane"
    age = run.now - coerce_int(h.get("updated"), 0)
    if not pane and run.within and not named and age > run.within:
        # Its pane is gone, so nothing says it was open when the machine
        # went down: the leftover of an older crash. A pane iTerm2 brought
        # back is that evidence, whatever the age.
        return run.skip(rec, h, f"its pane is gone and it was last active {age_text(age)} ago, older than "
                                f"--within {age_text(run.within)} (name it to recover it anyway)")
    if not pane:
        pane = run.idle_pane_in(worktree, rec["name"] if rec else "")
        where = "the pane open in its directory" if pane else "a new tab"
    if pane:
        run.taken.add(pane)
    done = dict(run.ident(rec, h), how=how, where="pane" if pane else "tab", pane=pane,
                tab_id=run.tabs.get(pane, ""))
    if run.dry_run:
        run.recovered.append(done)
        run.lines.append(f"would recover {run.label(rec, h)} in {where} ({howtext})")
        return None
    name = rec["name"] if rec else ""
    try:
        if pane:
            cmd = claude_command(worktree, name, "", resume=(how == "continue"),
                                 resume_id=(sid if how == "resume" else ""))
            if not run.term.write(pane, cmd):
                raise UserError(f"pane {pane} went away")
            if rec:
                repoint(run, rec, pane)
        elif rec:
            remake_tab(rec, run.term, run.live, how=how, resume_id=sid)
            done.update(pane=rec["claude_pane"], tab_id=rec["tab_id"])
        else:
            cmd = claude_command(project, "", "", resume=(how == "continue"),
                                 resume_id=(sid if how == "resume" else ""))
            ids = run.term.create_tab(os.path.basename(project.rstrip(os.sep)) or project, cmd)
            done.update(pane=ids["pane"], tab_id=ids["tab_id"])
    except UserError as e:
        return run.fail(rec, h, e)
    stamp_recovered(sid, done["pane"], run.now)
    run.recovered.append(done)
    run.lines.append(f"recovered {run.label(rec, h)} in {where} ({howtext})")
    log_info("recovered", session=sid, name=name, project=project, how=how, where=done["where"],
             pane=done["pane"], tab=done["tab_id"])
    return None


def cmd_recover(a):
    """Start again every recorded Claude Code session whose Claude is gone
    (the module header has the rules). One failing session is recorded
    (exit 1 at the end) and the rest continues."""
    within = parse_within(a.within)
    hooks = load_hook_sessions()
    records = list_records()
    items, named = select(candidates(hooks, records), a.sessions)
    term = make_terminal("")
    try:
        panes = [s for s in term.sessions() if isinstance(s, dict)]
    except UserError:
        if not a.dry_run:
            raise
        panes = []   # the plan can still be shown: every session would get a new tab
    run = Recovery(term, panes, hooks, records, a.dry_run, within, int(time.time()))
    for rec, h in items:
        recover_one(run, rec, h, id(h) in named)

    running = sum(1 for s in run.skipped if s["why"] == "running")
    in_panes = sum(1 for r in run.recovered if r["where"] == "pane")
    counts = (f"{len(run.recovered)} recovered ({in_panes} in a pane that was open, "
              f"{len(run.recovered) - in_panes} in a new tab), {running} running and left alone, "
              f"{len(run.skipped) - running} skipped, {len(run.failed)} failed")
    log_info("recover", dry_run=bool(a.dry_run), recovered=len(run.recovered), running=running,
             skipped=len(run.skipped) - running, failed=len(run.failed), within=within)
    if a.json:
        print(json.dumps({"dry_run": bool(a.dry_run), "within": within, "recovered": run.recovered,
                          "skipped": run.skipped, "failed": run.failed}))
    else:
        for ln in run.lines:
            print(ln)
        print(f"{'recover plan' if a.dry_run else 'recover'}: {counts}"
              + (" (nothing touched)" if a.dry_run else ""))
    return 1 if run.failed else 0
