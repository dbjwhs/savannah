#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Recover test: drives `work recover` in work/savannah_work.py against a temp
# SAVANNAH_WORK_DIR, a temp SAVANNAH_SESSIONS_DIR with hand-written hook
# files, a temp CLAUDE_CONFIG_DIR (transcripts), a temp SAVANNAH_LOG_DIR and
# the fake it2 (tools/fake_it2.py) via SAVANNAH_IT2. The "reboot" is what a
# real one leaves behind: the hook files stay (no SessionEnd fired), the
# processes they name are gone, and the panes are either still listed (iTerm2
# brought the tabs back) or not. A dead process is a child that has exited, a
# running one a sleeper. The fake's panes all sit on a tty no process has, so
# what runs in a pane is unknown to the command-line runs; the busy-pane and
# reopened-under-a-new-id cases run in process with the process table and the
# shell directories handed in. No real iTerm2, no real claude, no network, no
# git, stdlib only. Usage:
#   test_recover.py <path/to/savannah_work.py> <path/to/fake_it2.py>

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import Harness, check, finish, load_module  # noqa: E402

IN_PROCESS_ENV = ("SAVANNAH_WORK_DIR", "SAVANNAH_SESSIONS_DIR", "SAVANNAH_IT2", "FAKE_IT2_LOG",
                  "ITERM_SESSION_ID", "SAVANNAH_LOG_DIR", "CLAUDE_CONFIG_DIR")


class RecoverHarness(Harness):
    """The harness plus Claude Code's directory and transcripts, a dead pid,
    sleepers, and the hook-file helpers. The sections run in order against
    one world; what a later one reads from an earlier one is named here."""

    def __init__(self, module, fake):
        super().__init__(module, fake, prefix="savannah-recover-test-")
        self.claude_dir = os.path.join(self.tmp, "claude")
        self.transcripts = os.path.join(self.tmp, "tr")
        for d in (self.claude_dir, self.transcripts):
            os.makedirs(d)
        self.env["CLAUDE_CONFIG_DIR"] = self.claude_dir
        self.sw = load_module(self.module)
        self.rc = self.sw.savannah_recover
        self.sleepers = []
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        self.dead = done.pid          # a process id nothing runs under any more
        self.now = int(time.time())
        self.dirs = {}                # label -> directory (test_world)
        self.ids = {}                 # work session name -> its `new --json` (test_world)
        self.plain_pane = ""          # the pane of the plain tab iTerm2 brought back (test_world)
        self.idle_pane = ""           # the pane of the tab that had sat idle for weeks (test_world)

    def sleeper(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.sleepers.append(p)
        return p.pid

    def close(self):
        for p in self.sleepers:
            p.kill()
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def dir(self, label):
        d = os.path.join(self.tmp, "d-" + label)
        os.makedirs(d, exist_ok=True)
        self.dirs[label] = d
        return d

    def shown(self, label):
        """A directory as the command prints it: its HOME is the temp dir."""
        return "~" + self.dirs[label][len(self.tmp):]

    def transcript(self, session_id):
        path = os.path.join(self.transcripts, session_id + ".jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        return path

    def hook(self, session_id, project, pane, pid=None, transcript=True, age=60, **more):
        """A status file as the hook left it: last event `age` seconds ago,
        written by process pid (the dead one unless told otherwise)."""
        rec = {"session_id": session_id, "project": project, "state": "done", "updated": self.now - age,
               "pid": self.dead if pid is None else pid, "last_prompt": f"prompt of {session_id}"}
        if pane:
            rec["iterm_session_id"] = f"w0t1p0:{pane}"
        if transcript:
            rec["transcript_path"] = self.transcript(session_id)
        rec.update(more)
        rec = {k: v for k, v in rec.items() if v is not None}
        with open(os.path.join(self.sessions, session_id + ".json"), "w") as f:
            json.dump(rec, f)

    def status(self, session_id):
        with open(os.path.join(self.sessions, session_id + ".json")) as f:
            return json.load(f)

    def sent(self):
        """(pane, text) of every text typed into a pane since clear_log,
        without the carriage returns."""
        return [(c[3], c[-1]) for c in self.calls() if c[:3] == ["session", "send", "--session"] and c[-1] != "\r"]

    def in_process(self, argv, table=None, cwds=None):
        """Run the verb in this process with the process table and the shell
        directories replaced: (rc, stdout)."""
        saved = {k: os.environ.get(k) for k in IN_PROCESS_ENV}
        real_table, real_cwd = self.rc.tty_table, self.rc.shell_cwd
        os.environ.update({k: self.env[k] for k in IN_PROCESS_ENV})
        self.rc.tty_table = lambda: table or {}
        self.rc.shell_cwd = lambda pid: (cwds or {}).get(pid, "")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = self.sw.main(argv)
        finally:
            self.rc.tty_table, self.rc.shell_cwd = real_table, real_cwd
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return rc, out.getvalue()


def test_units(h):
    """The pure halves: durations, whether a Claude runs, what a pane runs."""
    rc = h.rc
    check(rc.parse_within("7d") == 7 * 86400 and rc.parse_within("72h") == 72 * 3600
          and rc.parse_within("90m") == 5400 and rc.parse_within("3") == 3 * 3600 and rc.parse_within("all") == 0,
          "parse_within reads minutes, hours, days, bare hours and all")
    for bad in ("", "0", "soon", "-3h", "7w"):
        try:
            rc.parse_within(bad)
            check(False, f"parse_within refuses {bad!r}")
        except h.sw.UserError:
            pass
    check(rc.age_text(26 * 86400 + 5) == "26d" and rc.age_text(7300) == "2h" and rc.age_text(59) == "59s",
          "age_text keeps the largest unit")
    check(rc.start_epoch("Sat Nov 14 06:27:00 2026") == 1794637620 and rc.start_epoch("Thu Nov 5 06:27:00 2026")
          is not None and rc.start_epoch("yesterday") is None and rc.start_epoch(None) is None,
          "start_epoch reads ps's lstart as UTC")
    started = "Sat Nov 14 06:27:00 2026"
    table = {7: ("/dev/ttys001", started)}
    check(rc.claude_state({"pid": 7, "updated": 1794637620 + 600}, info=table.get) == "running",
          "a process older than the file's last event is the session's Claude")
    check(rc.claude_state({"pid": 7, "updated": 1794637620 - 600}, info=table.get) == "gone",
          "a process that started after the last event is another one on a reused pid")
    check(rc.claude_state({"pid": 8, "updated": 5}, info=table.get) == "gone", "no such process: gone")
    check(rc.claude_state({"pid": 7}, info=table.get) == "running", "no timestamp to compare: a live pid counts")
    for bad in (None, 0, 1, True, "7"):
        check(rc.claude_state({"pid": bad}, info=table.get) == "unknown", f"pid {bad!r} names no process")
    check(rc.claude_state({"pid": os.getpid(), "updated": int(time.time()) + 10}) == "running"
          and rc.claude_state({"pid": h.dead, "updated": int(time.time())}) == "gone",
          "claude_state against the real process table")
    panes = [{"id": "P1", "tty": "/dev/ttys001"}, {"id": "P2", "tty": "/dev/ttys002"},
             {"id": "P3", "tty": "/dev/ttys003"}, {"id": "P4"}, {"id": "P5", "tty": "/dev/ttys005"}]
    states = rc.pane_states(panes, {
        "/dev/ttys001": [(10, "login", False), (11, "-zsh", True), (12, "gitstatusd-darwin-arm64", False)],
        "/dev/ttys002": [(20, "login", False), (21, "-zsh", False), (22, "claude", True)],
        "/dev/ttys003": [(30, "/bin/bash", False), (31, "/usr/bin/vim", True), (32, "/bin/zsh", False)],
    })
    check(states == {"P1": {"busy": "", "shell": 11}, "P2": {"busy": "claude", "shell": 21},
                     "P3": {"busy": "vim", "shell": 32}},
          f"pane_states: a shell in the foreground is idle whatever runs behind it (a prompt helper), "
          f"the innermost shell is kept, an unknown tty is left out ({states})")
    table = rc.tty_table()
    check(isinstance(table, dict) and all(k.startswith("/dev/") for k in table)
          and all(len(e) == 3 and isinstance(e[2], bool) for v in table.values() for e in v), "tty_table reads ps")
    check(rc.shell_cwd(2 ** 22 + 4321) == "", "shell_cwd of no process is empty")


def test_nothing_recorded(h):
    """No status file at all."""
    p = h.run(["recover"])
    check(p.returncode == 0 and "0 recovered" in p.stdout, f"nothing to recover is not an error ({p.stdout!r})")
    got = h.run_json(["recover", "--json"])
    check(got == {"dry_run": False, "within": 7 * 86400, "recovered": [], "skipped": [], "failed": []},
          f"the empty JSON shape ({got})")
    p = h.run(["recover", "--within", "soon"])
    check(p.returncode == 1 and "--within" in p.stderr, f"a bad --within is refused ({p.stderr!r})")


def test_world(h):
    """The world a reboot left behind."""
    # Work sessions (all --no-worktree, so plain directories are repos):
    #   alpha  iTerm2 brought its tab back (same pane ids)     -> typed into its pane
    #   beta   its tab is gone                                 -> a new tab and split
    #   gamma  closed long before the crash: no status file    -> not looked at
    # Plain tabs:
    #   s-plain    its pane is back                            -> typed into its pane
    #   s-gone     its pane is gone                            -> a new single-pane tab
    #   s-running  its Claude runs (a sleeper)                 -> left alone
    #   s-old      pane gone, last active 30 days ago          -> skipped (--within)
    #   s-idle     last active 40 days ago, but its pane is back -> typed into its pane
    #   s-nopid    its file names no process                   -> skipped
    #   s-notr     no conversation on disk                     -> skipped
    #   s-nodir    its directory is gone                       -> skipped
    #   s-stale    an older dead file on s-plain's pane        -> skipped (the newer one wins)
    #   s-under    a dead file on the pane s-running holds     -> skipped (another Claude runs there)
    for n in ("alpha", "beta", "gamma"):
        h.ids[n] = h.run_json(["new", n, "--no-worktree", "--repo", h.dir(n), "--json"])
    plain = h.run_json(["new", "plainish", "--no-worktree", "--repo", h.dir("plain"), "--json"])
    held = h.run_json(["new", "heldish", "--no-worktree", "--repo", h.dir("running"), "--json"])
    idle = h.run_json(["new", "idleish", "--no-worktree", "--repo", h.dir("idle"), "--json"])
    for n in ("plainish", "heldish", "idleish"):
        shutil.rmtree(os.path.join(h.workroot, n))   # the tabs stay in the fake's world, recordless
    h.plain_pane, h.idle_pane = plain["claude_pane"], idle["claude_pane"]
    for pane in (plain["shell_pane"], held["shell_pane"], idle["shell_pane"], h.ids["beta"]["claude_pane"],
                 h.ids["beta"]["shell_pane"], h.ids["gamma"]["claude_pane"], h.ids["gamma"]["shell_pane"]):
        h.it2("session", "close", "--session", pane, "--force")
    h.hook("s-alpha", h.dirs["alpha"], h.ids["alpha"]["claude_pane"])
    h.hook("s-beta", h.dirs["beta"], h.ids["beta"]["claude_pane"], age=120)
    h.hook("s-plain", h.dirs["plain"], h.plain_pane, age=30)
    h.hook("s-gone", h.dir("gone"), "PANE-NOT-LISTED", age=200)
    h.hook("s-running", h.dirs["running"], held["claude_pane"], pid=h.sleeper(), age=-30)
    h.hook("s-old", h.dir("old"), "PANE-NOT-LISTED-2", age=30 * 86400)
    h.hook("s-idle", h.dirs["idle"], h.idle_pane, age=40 * 86400)
    h.hook("s-nopid", h.dir("nopid"), "PANE-NOT-LISTED-3", pid=0)
    h.hook("s-notr", h.dir("notr"), "PANE-NOT-LISTED-4", transcript=False)
    h.hook("s-nodir", os.path.join(h.tmp, "d-never-made"), "PANE-NOT-LISTED-5")
    h.hook("s-stale", h.dir("stale"), h.plain_pane, age=5000)
    h.hook("s-under", h.dir("under"), held["claude_pane"], age=400)
    h.hook("s-noproj", "", "PANE-X")
    with open(os.path.join(h.sessions, "junk.json"), "w") as f:
        f.write("not json")


def expected_skips(h):
    return {"s-running": "running", "s-old": "older than --within 7d", "s-nopid": "names no process",
            "s-notr": "not on disk", "s-nodir": "directory gone", "s-stale": "newer session",
            "s-under": "another Claude session"}


def check_outcome(h, got, label):
    done = {r["session_id"]: r for r in got["recovered"]}
    check(sorted(done) == ["s-alpha", "s-beta", "s-gone", "s-idle", "s-plain"],
          f"{label}: the five dead sessions ({sorted(done)})")
    check(done["s-idle"]["where"] == "pane" and done["s-idle"]["pane"] == h.idle_pane,
          f"{label}: a tab that sat idle for weeks is recovered when its pane is back ({done.get('s-idle')})")
    check(done["s-alpha"]["name"] == "alpha" and done["s-alpha"]["where"] == "pane"
          and done["s-alpha"]["pane"] == h.ids["alpha"]["claude_pane"] and done["s-alpha"]["how"] == "resume",
          f"{label}: alpha in its own pane ({done.get('s-alpha')})")
    check(done["s-plain"]["name"] == "" and done["s-plain"]["where"] == "pane"
          and done["s-plain"]["pane"] == h.plain_pane and done["s-plain"]["project"] == h.dirs["plain"],
          f"{label}: the plain tab in its own pane ({done.get('s-plain')})")
    check(done["s-beta"]["name"] == "beta" and done["s-beta"]["where"] == "tab"
          and done["s-gone"]["where"] == "tab" and done["s-gone"]["how"] == "resume",
          f"{label}: the two whose pane is gone get a tab ({done.get('s-beta')}, {done.get('s-gone')})")
    skipped = {s["session_id"]: s["why"] for s in got["skipped"]}
    want = expected_skips(h)
    check(sorted(skipped) == sorted(want), f"{label}: who is skipped ({sorted(skipped)})")
    for sid, part in want.items():
        check(part in skipped.get(sid, ""), f"{label}: {sid} skipped because {part!r} ({skipped.get(sid)!r})")
    check(got["failed"] == [], f"{label}: nothing failed ({got['failed']})")


def test_dry_run(h):
    """The plan: everything said, nothing touched."""
    before = {sid: h.status(sid) for sid in ("s-alpha", "s-beta", "s-plain", "s-gone")}
    h.clear_log(keep_state=True)
    got = h.run_json(["recover", "--dry-run", "--json"])
    check(got["dry_run"] is True and got["within"] == 7 * 86400, f"dry-run JSON head ({got})")
    check_outcome(h, got, "dry-run")
    check(all(c[:2] == ["session", "list"] for c in h.calls()), f"a dry run only lists panes ({h.calls()})")
    check(before == {sid: h.status(sid) for sid in before}, "a dry run stamps no status file")
    check(h.record("beta")["claude_pane"] == h.ids["beta"]["claude_pane"], "a dry run changes no record")
    p = h.run(["recover", "--dry-run"])
    check(p.returncode == 0 and "would recover alpha in its pane (resume s-alpha)" in p.stdout
          and "would recover beta in a new tab (resume s-beta)" in p.stdout
          and f"would recover {h.shown('gone')} (s-gone) in a new tab" in p.stdout,
          f"the plan as lines ({p.stdout!r})")
    check("skipped " + h.shown("old") in p.stdout and "s-running" not in p.stdout
          and "recover plan: 5 recovered (3 in a pane that was open, 2 in a new tab), 1 running and left alone, "
              "6 skipped, 0 failed (nothing touched)" in p.stdout,
          f"skips have a line each, a running session only counts ({p.stdout!r})")


def test_recover(h):
    """The real thing."""
    h.clear_log(keep_state=True)
    got = h.run_json(["recover", "--json"])
    check(got["dry_run"] is False, "not a dry run")
    check_outcome(h, got, "recover")
    sent = h.sent()
    alpha, plain = h.ids["alpha"]["claude_pane"], h.plain_pane
    check((alpha, f"cd {h.dirs['alpha']} && claude -n alpha --resume s-alpha") in sent,
          f"alpha's Claude is typed into its own pane, named and resumed ({sent})")
    check((plain, f"cd {h.dirs['plain']} && claude --resume s-plain") in sent,
          f"the plain tab's Claude is typed into its pane with no name ({sent})")
    check((h.idle_pane, f"cd {h.dirs['idle']} && claude --resume s-idle") in sent and len(sent) == 3,
          f"nothing else is typed anywhere ({sent})")
    for pane in (alpha, plain):
        check(h.called(["session", "send", "--session", pane, "--"], "\r") >= 0, f"Enter follows the text in {pane}")
    runs = [c[-1] for c in h.calls() if c[:2] == ["session", "run"]]
    check(f" cd {h.dirs['beta']} && claude -n beta --resume s-beta" in runs
          and f" cd {h.dirs['gone']} && claude --resume s-gone" in runs,
          f"the sessions whose pane is gone start in new tabs ({runs})")
    check(not any(c[:2] in (["session", "close"], ["tab", "close"]) for c in h.calls()),
          f"recover closes nothing ({h.calls()})")
    done = {r["session_id"]: r for r in got["recovered"]}
    beta = h.record("beta")
    check(beta["claude_pane"] == done["s-beta"]["pane"] != h.ids["beta"]["claude_pane"]
          and beta["tab_id"] == done["s-beta"]["tab_id"] and beta["shell_pane"],
          f"beta's record names its new tab and split ({beta})")
    check(h.record("alpha")["claude_pane"] == alpha and h.record("alpha")["shell_pane"] == h.ids["alpha"]["shell_pane"],
          "alpha's record is as it was")
    check(h.record("gamma")["claude_pane"] == h.ids["gamma"]["claude_pane"],
          "a session that was closed before the crash is not touched")
    for sid in ("s-alpha", "s-beta", "s-plain", "s-gone"):
        st = h.status(sid)
        check(st.get("recovered_pane") == done[sid]["pane"] and st.get("recovered", 0) >= st["updated"]
              and st["session_id"] == sid and st["pid"] == h.dead,
              f"{sid}'s status file is stamped and otherwise kept ({st})")
    check("recovered" not in h.status("s-old") and "recovered" not in h.status("s-running"),
          "a skipped session's file is not stamped")
    log = open(os.path.join(h.logdir, "savannah.log")).read()
    check("msg=recovered" in log and "session=s-alpha" in log and "msg=recover " in log,
          "the log says what was recovered")


def test_twice(h):
    """A second recover, before any Claude has written an event."""
    h.clear_log(keep_state=True)
    got = h.run_json(["recover", "--json"])
    skipped = {s["session_id"]: s["why"] for s in got["skipped"]}
    check(got["recovered"] == [] and all("already recovered" in skipped.get(sid, "")
                                         for sid in ("s-alpha", "s-beta", "s-plain", "s-gone", "s-idle")),
          f"nothing is recovered twice ({got})")
    check("newer session" in skipped.get("s-stale", ""),
          f"an older file on a pane recovered earlier is still not that pane's session ({skipped.get('s-stale')!r})")
    check(h.sent() == [] and not any(c[0] == "tab" for c in h.calls()), f"and nothing is typed or opened ({h.calls()})")
    # The resumed Claude went down again after writing an event: the stamp
    # is older than the file, so the session is recoverable once more.
    st = h.status("s-alpha")
    st["updated"] = st["recovered"] + 5
    with open(os.path.join(h.sessions, "s-alpha.json"), "w") as f:
        json.dump(st, f)
    got = h.run_json(["recover", "--dry-run", "--json", "alpha"])
    check([r["session_id"] for r in got["recovered"]] == ["s-alpha"] and got["skipped"] == [],
          f"a stamp older than the last event no longer holds, and a name selects one session ({got})")


def test_named(h):
    """Naming sessions: only those, whatever their age."""
    got = h.run_json(["recover", "--dry-run", "--json", "s-old"])
    check([r["session_id"] for r in got["recovered"]] == ["s-old"] and got["recovered"][0]["where"] == "tab"
          and got["skipped"] == [], f"a named session is recovered whatever its age ({got})")
    got = h.run_json(["recover", "--dry-run", "--json", "--within", "all"])
    check("s-old" in [r["session_id"] for r in got["recovered"]], "--within all lifts the age limit")
    h.hook("s-recent", h.dir("recent"), "PANE-NOT-LISTED-8", age=300)
    got = h.run_json(["recover", "--dry-run", "--json", "--within", "1m"])
    check("s-recent" in {s["session_id"] for s in got["skipped"] if "older than --within 1m" in s["why"]},
          f"--within narrows it ({got['skipped']})")
    got = h.run_json(["recover", "--dry-run", "--json"])
    check("s-recent" in [r["session_id"] for r in got["recovered"]], "which the default would have recovered")
    os.remove(os.path.join(h.sessions, "s-recent.json"))
    got = h.run_json(["recover", "--dry-run", "--json", "s-runn"])
    check(got["recovered"] == [] and [s["why"] for s in got["skipped"]] == ["running"],
          f"the start of a session id selects it; a running one is still left alone ({got})")
    p = h.run(["recover", "--dry-run", "nosuch-session"])
    check(p.returncode == 1 and "no recorded session nosuch-session" in p.stderr, f"an unknown name ({p.stderr!r})")
    p = h.run(["recover", "--dry-run", "s-"])
    check(p.returncode == 1, "a word too short to be the start of an id names nothing")


def test_busy_and_own_pane(h):
    """What runs in a pane decides whether it is typed into (in process)."""
    d = h.dir("busy")
    tab = h.run_json(["new", "busyish", "--no-worktree", "--repo", d, "--json"])
    shutil.rmtree(os.path.join(h.workroot, "busyish"))
    pane, lower = tab["claude_pane"], tab["shell_pane"]
    h.hook("s-busy", d, pane)
    # The fake puts every pane on one tty; give these two their own.
    ttys = {pane: "/dev/ttys101", lower: "/dev/ttys102"}
    real_sessions = h.sw.ITerm.sessions
    h.sw.ITerm.sessions = lambda self: [dict(s, tty=ttys.get(s["id"], s["tty"])) for s in real_sessions(self)]
    try:
        h.clear_log(keep_state=True)
        rc, out = h.in_process(["recover", "--json", "s-busy"],
                               table={"/dev/ttys101": [(50, "login", False), (51, "-zsh", False), (52, "vim", True)]})
        got = json.loads(out)
        check(rc == 0 and got["recovered"] == [] and "its pane is busy (vim runs there)" in got["skipped"][0]["why"],
              f"a pane where something other than a shell runs is never typed into ({got})")
        check(h.sent() == [], "nothing was typed")
        rc, out = h.in_process(["recover", "--dry-run", "--json", "s-busy"],
                               table={"/dev/ttys101": [(50, "login", False), (51, "-zsh", True), (53, "gitstatusd", False)]})
        got = json.loads(out)
        check(got["recovered"] and got["recovered"][0]["pane"] == pane and got["recovered"][0]["where"] == "pane",
              f"the same pane at a shell prompt is the place ({got})")
        os.environ["ITERM_SESSION_ID"], saved = f"w0t0p0:{pane}", os.environ.get("ITERM_SESSION_ID")
        h.env["ITERM_SESSION_ID"], saved_env = f"w0t0p0:{pane}", h.env["ITERM_SESSION_ID"]
        try:
            rc, out = h.in_process(["recover", "--dry-run", "--json", "s-busy"])
        finally:
            h.env["ITERM_SESSION_ID"] = saved_env
            if saved is None:
                os.environ.pop("ITERM_SESSION_ID", None)
            else:
                os.environ["ITERM_SESSION_ID"] = saved
        got = json.loads(out)
        check(got["recovered"] == [] and "the one this command runs in" in got["skipped"][0]["why"],
              f"the pane the command itself runs in is never typed into ({got})")
    finally:
        h.sw.ITerm.sessions = real_sessions
    os.remove(os.path.join(h.sessions, "s-busy.json"))


def test_reopened_under_new_ids(h):
    """iTerm2 reopened the tabs in their directories, but under new pane ids:
    the idle shell in the session's directory is the place (in process)."""
    d = h.dir("moved")
    was = h.run_json(["new", "moved", "--no-worktree", "--repo", d, "--json"])
    for pane in (was["claude_pane"], was["shell_pane"]):
        h.it2("session", "close", "--session", pane, "--force")           # the old ids are gone
    again = h.run_json(["new", "reopened", "--no-worktree", "--repo", h.dir("scratch"), "--json"])
    shutil.rmtree(os.path.join(h.workroot, "reopened"))                    # iTerm2's own two panes, recordless
    upper, lower = again["claude_pane"], again["shell_pane"]
    other = h.run_json(["new", "elsewhere", "--no-worktree", "--repo", h.dir("elsewhere"), "--json"])
    h.hook("s-moved", d, was["claude_pane"])
    h.hook("s-moved-plain", h.dir("roam"), "PANE-NOT-LISTED-9", age=90)   # elsewhere's shell sits in roam
    ttys = {upper: "/dev/ttys201", lower: "/dev/ttys202", other["shell_pane"]: "/dev/ttys203"}
    table = {"/dev/ttys201": [(60, "login", False), (61, "-zsh", True)],
             "/dev/ttys202": [(70, "login", False), (71, "-zsh", True)],
             "/dev/ttys203": [(80, "login", False), (81, "-zsh", True)]}
    cwds = {61: d, 71: d, 81: h.dirs["roam"]}
    real_sessions = h.sw.ITerm.sessions
    h.sw.ITerm.sessions = lambda self: [dict(s, tty=ttys.get(s["id"], s["tty"])) for s in real_sessions(self)]
    try:
        h.clear_log(keep_state=True)
        rc, out = h.in_process(["recover", "--json", "moved", "s-moved-plain"], table=table, cwds=cwds)
    finally:
        h.sw.ITerm.sessions = real_sessions
    got = json.loads(out)
    done = {r["session_id"]: r for r in got["recovered"]}
    check(rc == 0 and done.get("s-moved", {}).get("pane") == upper and done["s-moved"]["where"] == "pane",
          f"the first idle shell in the worktree takes the session ({got})")
    check((upper, f"cd {d} && claude -n moved --resume s-moved") in h.sent(), f"and gets the command ({h.sent()})")
    rec = h.record("moved")
    check(rec["claude_pane"] == upper and rec["shell_pane"] == lower and rec["tab_id"] == again["tab_id"],
          f"the record follows: the upper pane is its Claude, the tab's other idle shell its shell ({rec})")
    check(done.get("s-moved-plain", {}).get("where") == "tab",
          f"a shell pane a work record names is not a plain tab's place: it gets a tab ({done.get('s-moved-plain')})")
    check(not any(p == other["shell_pane"] for p, _ in h.sent()), "nothing is typed into another session's shell")


def test_failing_it2_call(h):
    """One session failing is recorded; the exit code says so."""
    d = h.dir("fail")
    h.hook("s-fail", d, "PANE-NOT-LISTED-7")
    p = h.run(["recover", "--json", "s-fail"], extra_env={"FAKE_IT2_FAIL": "session run"})
    got = json.loads(p.stdout)
    check(p.returncode == 1 and got["recovered"] == [] and got["failed"][0]["session_id"] == "s-fail"
          and got["failed"][0]["error"], f"a failing it2 call is a failed entry and exit 1 ({got})")
    check("recovered" not in h.status("s-fail"), "a failed session is not stamped, so the next recover tries again")


SECTIONS = (
    test_units,
    test_nothing_recorded,
    test_world,
    test_dry_run,
    test_recover,
    test_twice,
    test_named,
    test_busy_and_own_pane,
    test_reopened_under_new_ids,
    test_failing_it2_call,
)


def main():
    h = RecoverHarness(sys.argv[1], sys.argv[2])
    try:
        for section in SECTIONS:
            section(h)
    finally:
        h.close()
    return finish("test_recover", "ok", count=True)


if __name__ == "__main__":
    sys.exit(main())
