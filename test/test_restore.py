#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Snapshot/restore test: drives `work snapshot`, `work snapshots` and
# `work restore` in work/savannah_work.py against a temp SAVANNAH_WORK_DIR,
# a temp SAVANNAH_SESSIONS_DIR with hand-written hook files, a temp
# CLAUDE_CONFIG_DIR (transcripts), a temp SAVANNAH_LOG_DIR and the fake it2
# (tools/fake_it2.py) via SAVANNAH_IT2. The "reboot" is the fake's panes
# being closed and the hook files going away, exactly what iTerm2 quitting
# and Claude Code exiting leave behind. No real iTerm2, no real claude, no
# network, no git (every record is --no-worktree, so a plain directory is a
# repo), stdlib only. Usage:
#   test_restore.py <path/to/savannah_work.py> <path/to/fake_it2.py>

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import Harness, check, finish, load_module  # noqa: E402


class RestoreHarness(Harness):
    """The harness plus Claude Code's own directory (CLAUDE_CONFIG_DIR) and a
    directory of transcripts, with the helpers that write hook files and
    transcripts into them.

    The sections below run in order against this one world and are not
    independent: the sessions are made once, snapshotted, "rebooted" and
    restored. What a later section reads from an earlier one is named here."""

    def __init__(self, module, fake):
        super().__init__(module, fake, prefix="savannah-restore-test-")
        self.claude_dir = os.path.join(self.tmp, "claude")
        self.transcripts = os.path.join(self.tmp, "tr")
        for d in (self.claude_dir, self.transcripts):
            os.makedirs(d)
        self.env["CLAUDE_CONFIG_DIR"] = self.claude_dir
        self.sw = load_module(self.module)   # the work module in process
        self.dirs = None                     # name -> directory of each session (test_world_before_reboot)
        self.stamp1 = None                   # the first snapshot's stamp (test_snapshot)
        self.snap_doc = None                 # the first snapshot file, parsed (test_snapshot)
        self.snap_names = None               # its session names, in order (test_snapshot)
        self.stamp2 = None                   # the second snapshot's stamp (test_snapshots_listing)

    def hook(self, session_id, project, pane, transcript=None, **more):
        """Write a hook file: a live Claude Code session in project, on pane."""
        rec = {"session_id": session_id, "project": project, "state": "idle", "updated": 100,
               "iterm_session_id": f"w0t1p0:{pane}", "last_prompt": f"prompt of {session_id}"}
        if transcript is not None:
            rec["transcript_path"] = transcript
        rec.update(more)
        with open(os.path.join(self.sessions, session_id + ".json"), "w") as f:
            json.dump(rec, f)

    def transcript(self, session_id, where=None):
        """Write a one-line transcript <session_id>.jsonl; its path."""
        path = os.path.join(where or self.transcripts, session_id + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        return path

    def projects_dir(self, project):
        """Where Claude Code itself keeps the transcripts of project."""
        return os.path.join(self.claude_dir, "projects", self.sw.encode_project_dir(os.path.realpath(project)))

    def snapshot_file(self, stamp):
        with open(os.path.join(self.workroot, "snapshots", stamp + ".json")) as f:
            return json.load(f)


def test_module_units(h):
    """Unit-level: the name rule, titles, the claude command."""
    check(h.sw.valid_name("snapshots") is False and h.sw.valid_name("archive") is False,
          "snapshots and archive are reserved names")
    check(h.sw.valid_name("snapshot") is True, "snapshot (singular) is an ordinary name")
    check("snapshots" in h.sw.NAME_HELP, "NAME_HELP names the reserved snapshots")
    check(h.sw.tab_title("demo (claude)") == "demo", "tab_title strips the process")
    check(h.sw.tab_title("~/ng/dbjwhs (-zsh)") == "~/ng/dbjwhs", "tab_title keeps a path")
    check(h.sw.tab_title("(claude)") == "" and h.sw.tab_title(" (claude) ") == "",
          "tab_title of a bare process is empty")
    check(h.sw.tab_title("plain") == "plain" and h.sw.tab_title("") == "", "tab_title leaves the rest")
    check(h.sw.claude_command("/w", "n", "", resume_id="abc") == "cd /w && claude -n n --resume abc",
          "claude_command --resume <id>")
    check(h.sw.claude_command("/w", "n", "", resume=True) == "cd /w && claude -n n --continue",
          "claude_command --continue")
    check(h.sw.claude_command("/w", "n", "", resume=True, resume_id="abc") == "cd /w && claude -n n --resume abc",
          "resume_id wins over resume")
    check(h.sw.claude_command("/w", "", "", resume_id="abc") == "cd /w && claude --resume abc",
          "no name, no -n (a plain tab)")
    check(h.sw.claude_command("/w", "", "") == "cd /w && claude", "a fresh plain claude")


def test_reserved_name(h):
    """The reserved name through the CLI."""
    repo = os.path.join(h.tmp, "repo")
    os.makedirs(repo)
    p = h.run(["new", "snapshots", "--no-worktree", "--repo", repo])
    check(p.returncode == 1 and "snapshots" in p.stderr, f"new snapshots is refused ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(h.workroot, "snapshots", "record.json")),
          "the refusal writes no record")


def test_nothing_yet(h):
    """Nothing to list or restore yet."""
    p = h.run(["snapshots"])
    check(p.returncode == 0 and "no snapshots" in p.stdout, "snapshots with none says so, exit 0")
    check(h.run_json(["snapshots", "--json"]) == [], "snapshots --json with none is []")
    p = h.run(["restore"])
    check(p.returncode == 1 and "no snapshot (work snapshot makes one)" in p.stderr,
          f"restore with no snapshot exits 1 with the hint ({p.stderr.strip()})")
    p = h.run(["restore", "20000101-000000"])
    check(p.returncode == 1 and "no snapshot 20000101-000000" in p.stderr,
          f"restore of an unknown stamp exits 1 ({p.stderr.strip()})")
    p = h.run(["restore", "--dry-run"])
    check(p.returncode == 1, "dry-run without a snapshot still exits 1")


def test_world_before_reboot(h):
    """The world before the reboot."""
    # Six work sessions (all --no-worktree, so plain directories are repos):
    #   alpha  live, its hook names a transcript file        -> resume <id>
    #   beta   live, hook without transcript_path, but Claude
    #          Code's own projects dir holds <id>.jsonl      -> resume <id>
    #   gamma  no hook, a transcript in its projects dir     -> continue
    #   delta  no hook, nothing on disk                      -> fresh
    #   epsilon record removed before the restore            -> skipped
    #   zeta   worktree removed before the restore           -> skipped
    # One plain tab (a Claude Code tab with no record: the session's own
    # record is deleted after the fake made its tab), one dead hook file
    # (its pane is not in the it2 list), one hook without a project.
    h.dirs = {n: os.path.join(h.tmp, "d-" + n) for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta",
                                                          "other", "other2")}
    for d in h.dirs.values():
        os.makedirs(d)
    ids = {}
    for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
        ids[n] = h.run_json(["new", n, "--no-worktree", "--repo", h.dirs[n], "--json"])
    plainish = h.run_json(["new", "plainish", "--no-worktree", "--repo", h.dirs["other"], "--json"])
    shutil.rmtree(os.path.join(h.workroot, "plainish"))   # the tab stays in the fake's world, recordless
    check(ids["alpha"]["claude_pane"] == "PANE-C-1" and plainish["claude_pane"] == "PANE-C-7",
          "the fake handed out the expected pane ids")
    # gamma's and delta's tabs are already gone before the snapshot (offline rows).
    for pane in (ids["gamma"]["claude_pane"], ids["gamma"]["shell_pane"],
                 ids["delta"]["claude_pane"], ids["delta"]["shell_pane"]):
        h.it2("session", "close", "--session", pane, "--force")
    h.hook("s-alpha", h.dirs["alpha"], ids["alpha"]["claude_pane"], h.transcript("s-alpha"), state="working")
    h.hook("s-beta", h.dirs["beta"], ids["beta"]["claude_pane"])
    h.transcript("s-beta", h.projects_dir(h.dirs["beta"]))
    h.transcript("older", h.projects_dir(h.dirs["gamma"]))
    h.hook("s-plain", h.dirs["other"], plainish["claude_pane"], h.transcript("s-plain"))
    h.hook("s-dead", h.dirs["other2"], "PANE-NOT-LISTED", h.transcript("s-dead"))
    h.hook("s-noproj", "", "PANE-X")
    # A session in alpha's directory on another pane: a second Claude there,
    # not the record's (join prefers the pane match), so a plain tab.
    h.hook("s-alpha2", h.dirs["alpha"], "PANE-NOT-LISTED-2")


def test_snapshot(h):
    """snapshot."""
    h.clear_log(keep_state=True)
    snap = h.run_json(["snapshot", "--note", "before reboot", "--json"])
    check(set(snap) == {"path", "stamp", "work", "live", "plain", "it2"}, f"snapshot --json shape ({snap})")
    check(snap.get("work") == 6 and snap.get("live") == 2 and snap.get("plain") == 3 and snap.get("it2") is True,
          f"snapshot counts: 6 work (2 live), 3 plain ({snap})")
    check(h.calls() == [["session", "list", "--json"]], f"snapshot asks it2 only for the session list ({h.calls()})")
    h.stamp1 = snap.get("stamp", "")
    check(h.sw.SNAPSHOT_STAMP_RE.fullmatch(h.stamp1) is not None, f"stamp is YYYYMMDD-HHMMSS ({h.stamp1})")
    check(snap.get("path") == os.path.join(h.workroot, "snapshots", h.stamp1 + ".json"), "path under <work dir>/snapshots")
    h.snap_doc = h.snapshot_file(h.stamp1)
    with open(os.path.join(h.workroot, "snapshots", "latest.json")) as f:
        check(json.load(f) == h.snap_doc, "latest.json is a copy of the newest snapshot")
    check(set(h.snap_doc) == {"stamp", "taken", "taken_local", "note", "it2", "sessions", "plain"},
          f"snapshot file shape ({sorted(h.snap_doc)})")
    check(h.snap_doc["note"] == "before reboot" and isinstance(h.snap_doc["taken"], int) and h.snap_doc["taken"] > 0
          and h.snap_doc["taken_local"].endswith(" local") and h.snap_doc["it2"] is True, "snapshot header fields")
    h.snap_names = [e["name"] for e in h.snap_doc["sessions"]]
    check(h.snap_names == ["alpha", "beta", "epsilon", "zeta", "delta", "gamma"],
          f"sessions in tab order, the ones whose pane is gone last by name ({h.snap_names})")
    by = {e["name"]: e for e in h.snap_doc["sessions"]}
    want = {"name", "title", "worktree", "repo", "branch", "adopted", "priority", "rank", "tab_id",
            "claude_pane", "shell_pane", "tab_title", "pane_alive", "live", "session_id",
            "transcript_path", "state", "last_prompt"}
    for e in h.snap_doc["sessions"]:
        check(set(e) == want, f"session entry {e['name']} has the contract fields ({sorted(set(e) ^ want)})")
    a = by["alpha"]
    check(a["live"] is True and a["pane_alive"] is True and a["session_id"] == "s-alpha"
          and a["transcript_path"] == os.path.join(h.transcripts, "s-alpha.jsonl") and a["state"] == "working"
          and a["last_prompt"] == "prompt of s-alpha" and a["tab_id"] == "TAB-1"
          and a["claude_pane"] == "PANE-C-1" and a["shell_pane"] == "PANE-S-1" and a["worktree"] == h.dirs["alpha"]
          and a["tab_title"] == "alpha" and a["priority"] == "active" and a["adopted"] is False,
          f"alpha: live, with its session ({a})")
    check(by["beta"]["session_id"] == "s-beta" and by["beta"]["transcript_path"] == "", "beta: id without a path")
    g = by["gamma"]
    check(g["live"] is False and g["pane_alive"] is False and g["session_id"] == "" and g["tab_title"] == "",
          f"gamma: offline, no session ({g})")
    plain = h.snap_doc["plain"]
    projects = [e["project"] for e in plain]
    check(projects == [h.dirs["other"], h.dirs["alpha"], h.dirs["other2"]],
          f"plain tabs: the listed pane first, then the rest by project ({projects})")
    pw = {"project", "pane", "tab_id", "title", "pane_alive", "session_id", "transcript_path", "state", "last_prompt"}
    for e in plain:
        check(set(e) == pw, f"plain entry {e['project']} has the contract fields ({sorted(set(e) ^ pw)})")
    o = plain[0]
    check(o["pane"] == "PANE-C-7" and o["tab_id"] == "TAB-7" and o["title"] == "plainish" and o["pane_alive"] is True
          and o["session_id"] == "s-plain" and o["transcript_path"] == os.path.join(h.transcripts, "s-plain.jsonl"),
          f"the plain tab: pane, tab, title, session ({o})")
    check(plain[2]["pane_alive"] is False and plain[2]["tab_id"] == "", "a dead hook: pane not alive, no tab")
    check(not any(e.get("session_id") == "s-noproj" for e in plain), "a hook without a project is not a tab")

    # The snapshots directory is never a work session.
    check("snapshots" not in [e["name"] for e in h.run_json(["ls", "--json"])], "ls skips snapshots/")


def test_snapshots_listing(h):
    """snapshots listing."""
    rows = h.run_json(["snapshots", "--json"])
    check(len(rows) == 1 and set(rows[0]) == {"stamp", "path", "taken", "taken_local", "work", "live", "plain", "note"},
          f"snapshots --json: one row with the contract fields ({rows})")
    check(rows and rows[0]["stamp"] == h.stamp1 and rows[0]["work"] == 6 and rows[0]["live"] == 2
          and rows[0]["plain"] == 3 and rows[0]["note"] == "before reboot", f"snapshots row values ({rows})")
    p = h.run(["snapshots"])
    check(p.returncode == 0 and p.stdout.splitlines()[0].split() == ["STAMP", "TAKEN", "WORK", "LIVE", "PLAIN", "NOTE"]
          and h.stamp1 in p.stdout and "before reboot" in p.stdout, f"human snapshots table ({p.stdout!r})")
    p = h.run(["snapshot"])
    check(p.returncode == 0 and p.stdout.startswith("snapshot ") and "6 work sessions (2 live), 3 plain tabs" in p.stdout,
          f"human snapshot line ({p.stdout!r})")
    rows = h.run_json(["snapshots", "--json"])
    check(len(rows) == 2 and rows[0]["stamp"] > rows[1]["stamp"] == h.stamp1,
          f"two snapshots, newest first, distinct stamps ({[r['stamp'] for r in rows]})")
    check(rows[0]["note"] == "", "a snapshot without --note has an empty note")
    h.stamp2 = rows[0]["stamp"]
    check(h.snapshot_file(h.stamp2)["sessions"] == h.snap_doc["sessions"], "the second snapshot saw the same world")
    with open(os.path.join(h.workroot, "snapshots", "latest.json")) as f:
        check(json.load(f)["stamp"] == h.stamp2, "latest.json follows the newest snapshot")

    # Without it2 the snapshot still writes and says so.
    p = h.run(["snapshot", "--json"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check(p.returncode == 0, f"snapshot without it2 exits 0 ({p.stderr.strip()})")
    noit2 = json.loads(p.stdout) if p.returncode == 0 else {}
    check(noit2.get("it2") is False and noit2.get("work") == 6, f"snapshot without it2: it2 false ({noit2})")
    nd = h.snapshot_file(noit2.get("stamp", h.stamp2))
    check(all(e["pane_alive"] is None for e in nd["sessions"] + nd["plain"]), "without it2 pane_alive is null")
    check([e["name"] for e in nd["sessions"]] == sorted(h.snap_names), "without it2 the order is by name")
    p = h.run(["snapshot"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check("it2 unavailable" in p.stdout, f"human snapshot without it2 says so ({p.stdout!r})")
    # Put latest back to the real one so restore below uses the full snapshot.
    shutil.copy(os.path.join(h.workroot, "snapshots", h.stamp2 + ".json"),
                os.path.join(h.workroot, "snapshots", "latest.json"))


def test_plan_before_reboot(h):
    """The plan before anything is gone."""
    h.clear_log(keep_state=True)
    plan = h.run_json(["restore", "--dry-run", "--json"])
    check(set(plan) == {"stamp", "path", "dry_run", "restored", "plain", "skipped", "failed"},
          f"restore --json shape ({sorted(plan)})")
    check(plan["dry_run"] is True and plan["stamp"] == h.stamp2 and plan["failed"] == [], f"dry-run plan header ({plan})")
    skipped = {s.get("name") or s.get("project"): s["why"] for s in plan["skipped"]}
    check(skipped.get("alpha") == "alive" and skipped.get("beta") == "alive", f"alive tabs are skipped ({skipped})")
    check(skipped.get(h.dirs["other"]) == "alive", f"a live plain tab is skipped ({skipped})")
    check(skipped.get(h.dirs["other2"]) == "not open when the snapshot was taken"
          and skipped.get(h.dirs["alpha"]) == "not open when the snapshot was taken",
          f"a hook whose pane was already gone is not reopened ({skipped})")
    check(skipped.get("epsilon") == "alive" and skipped.get("zeta") == "alive",
          f"epsilon and zeta still have their tabs at this point ({skipped})")
    would = {r["name"]: r for r in plan["restored"]}
    check(set(would) == {"gamma", "delta"}, f"the offline sessions would be restored ({would})")
    check(would["gamma"]["how"] == "continue" and would["delta"]["how"] == "fresh"
          and all(r["tab_id"] == "" for r in plan["restored"]), f"dry-run hows, no tab ids ({would})")
    check(plan["plain"] == [], "no plain tab to open yet")
    check(h.calls() == [["session", "list", "--json"]], f"dry-run touches nothing ({h.calls()})")
    p = h.run(["restore", "--dry-run"])
    check(p.returncode == 0 and "would restore gamma (continue)" in p.stdout and "skipped alpha: alive" in p.stdout
          and "plan from snapshot" in p.stdout and "nothing touched" in p.stdout, f"human dry-run ({p.stdout!r})")
    p = h.run(["restore", "--dry-run", "--json"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check(p.returncode == 0 and json.loads(p.stdout)["dry_run"] is True, "dry-run without it2 still plans")


def test_reboot(h):
    """The reboot."""
    # iTerm2 quit: every pane is gone (the fake's counter keeps counting, so
    # the remade tabs get new ids). Claude Code exited: the hook files are
    # gone, except one stale file whose pane no longer exists.
    for pane in ("PANE-C-1", "PANE-S-1", "PANE-C-2", "PANE-S-2", "PANE-C-5", "PANE-S-5",
                 "PANE-C-6", "PANE-S-6", "PANE-C-7", "PANE-S-7"):
        h.it2("session", "close", "--session", pane, "--force")
    check(json.loads(h.it2("session", "list", "--json")) == [], "every pane is gone")
    for sid in ("s-alpha", "s-beta", "s-dead", "s-noproj", "s-alpha2"):
        os.remove(os.path.join(h.sessions, sid + ".json"))
    shutil.rmtree(os.path.join(h.workroot, "epsilon"))
    shutil.rmtree(h.dirs["zeta"])


def test_restore(h):
    """restore."""
    h.clear_log(keep_state=True)
    res = h.run_json(["restore", "--json"])
    check(res["dry_run"] is False and res["stamp"] == h.stamp2 and res["failed"] == [], f"restore header ({res})")
    got = {r["name"]: r for r in res["restored"]}
    check([r["name"] for r in res["restored"]] == ["alpha", "beta", "delta", "gamma"],
          f"restored in snapshot order ({[r['name'] for r in res['restored']]})")
    check(got["alpha"]["how"] == "resume" and got["beta"]["how"] == "resume" and got["gamma"]["how"] == "continue"
          and got["delta"]["how"] == "fresh", f"hows ({got})")
    check(got["alpha"]["tab_id"] == "TAB-8" and got["beta"]["tab_id"] == "TAB-9" and got["delta"]["tab_id"] == "TAB-10"
          and got["gamma"]["tab_id"] == "TAB-11", f"new tab ids ({got})")
    skipped = {s.get("name") or s.get("project"): s["why"] for s in res["skipped"]}
    check(skipped.get("epsilon") == "no record" and skipped.get("zeta") == "worktree gone", f"skips ({skipped})")
    check(res["plain"] == [{"project": h.dirs["other"], "how": "resume", "tab_id": "TAB-12"}], f"plain tab ({res['plain']})")
    # Records point at the new tabs.
    ra = h.record("alpha")
    check(ra["tab_id"] == "TAB-8" and ra["claude_pane"] == "PANE-C-8" and ra["shell_pane"] == "PANE-S-8",
          f"alpha's record follows its new tab ({ra})")
    check(h.record("gamma")["tab_id"] == "TAB-11", "gamma's record follows its new tab")
    # The exact it2 conversation: each work session gets a tab, cd + claude
    # with the right resume form, a split and the shell below; the plain tab
    # gets one pane, no split, named after its old title.
    c = h.calls()
    da, db, dg, dd, do = (h.dirs[n] for n in ("alpha", "beta", "gamma", "delta", "other"))
    check(h.called(["session", "run", "--session", "PANE-C-8", "--"], last=f" cd {da} && claude -n alpha --resume s-alpha") >= 0,
          f"alpha: claude -n alpha --resume s-alpha ({c})")
    check(h.called(["session", "run", "--session", "PANE-C-9", "--"], last=f" cd {db} && claude -n beta --resume s-beta") >= 0,
          "beta: resumed by the id found in Claude Code's projects dir")
    check(h.called(["session", "run", "--session", "PANE-C-10", "--"], last=f" cd {dd} && claude -n delta") >= 0,
          "delta: a fresh claude")
    check(h.called(["session", "run", "--session", "PANE-C-11", "--"], last=f" cd {dg} && claude -n gamma --continue") >= 0,
          "gamma: claude --continue")
    check(h.called(["session", "split", "--session", "PANE-C-8"]) >= 0
          and h.called(["session", "run", "--session", "PANE-S-8", "--"], last=f" cd {da}") >= 0,
          "alpha: split with the shell below")
    check(h.called(["session", "set-name", "--session", "PANE-C-8", "--"], last="alpha") >= 0, "alpha: tab named")
    check(h.called(["session", "run", "--session", "PANE-C-12", "--"], last=f" cd {do} && claude --resume s-plain") >= 0,
          f"plain: cd + claude --resume <id>, no -n ({c})")
    check(h.called(["session", "set-name", "--session", "PANE-C-12", "--"], last="plainish") >= 0,
          "plain: named after its old title")
    check(h.called(["session", "split", "--session", "PANE-C-12"]) < 0, "plain: a single pane, no split")
    check(h.called(["session", "close"]) < 0 and h.called(["tab", "close"]) < 0, "restore closes nothing")
    check(sum(1 for x in c if x[:2] == ["tab", "new"]) == 5, f"five tabs made ({c})")
    live = {s["id"] for s in json.loads(h.it2("session", "list", "--json"))}
    check({"PANE-C-8", "PANE-S-8", "PANE-C-9", "PANE-C-11", "PANE-C-12"} <= live and "PANE-S-12" not in live,
          f"the fake's world after the restore ({sorted(live)})")

    # Human output of a restore (on a copy of the world: close alpha again).
    h.it2("session", "close", "--session", "PANE-C-8", "--force")
    h.it2("session", "close", "--session", "PANE-S-8", "--force")
    p = h.run(["restore"])
    check(p.returncode == 0 and "restored alpha (resume s-alpha, tab TAB-13)" in p.stdout
          and "skipped beta: alive" in p.stdout and "skipped epsilon: no record" in p.stdout
          and "skipped zeta: worktree gone" in p.stdout and "restored from snapshot" in p.stdout,
          f"human restore lines ({p.stdout!r} {p.stderr!r})")
    # The plain tab came back a second time: its hook file still names the
    # pane that died with iTerm2. Once the resumed Claude's hook writes the
    # new pane, restore leaves it alone.
    check("plain " in p.stdout and "(resume s-plain, tab TAB-14)" in p.stdout, f"plain tab line ({p.stdout!r})")
    h.hook("s-plain", h.dirs["other"], "PANE-C-14", os.path.join(h.transcripts, "s-plain.jsonl"))
    res = h.run_json(["restore", "--json"])
    skipped = {s.get("name") or s.get("project"): s["why"] for s in res["skipped"]}
    check(res["restored"] == [] and res["plain"] == [] and skipped.get(h.dirs["other"]) == "alive"
          and skipped.get("alpha") == "alive", f"everything alive: nothing remade ({res})")


def test_no_plain(h):
    """--no-plain."""
    h.it2("session", "close", "--session", "PANE-C-14", "--force")
    h.clear_log(keep_state=True)
    res = h.run_json(["restore", "--no-plain", "--json"])
    check(res["plain"] == [] and res["restored"] == [] and h.called(["tab", "new"]) < 0
          and not any(s.get("project") for s in res["skipped"]),
          f"--no-plain leaves the plain tabs out entirely ({res} {h.calls()})")


def test_failing_it2_call(h):
    """A failing it2 call is recorded, the rest continues, exit 1."""
    h.it2("session", "close", "--session", "PANE-C-13", "--force")
    h.it2("session", "close", "--session", "PANE-S-13", "--force")
    h.clear_log(keep_state=True)
    p = h.run(["restore", "--json"], {"FAKE_IT2_FAIL": "session split"})
    check(p.returncode == 1, f"a failed item makes restore exit 1 ({p.stdout!r} {p.stderr!r})")
    try:
        res = json.loads(p.stdout)
    except ValueError:
        res = {}
        check(False, f"restore --json still prints JSON on failure ({p.stdout!r})")
    check(len(res.get("failed", [])) == 1 and res["failed"][0].get("name") == "alpha" and "boom" in res["failed"][0]["error"],
          f"alpha's split failed and is recorded ({res.get('failed')})")
    check(res.get("plain") == [{"project": h.dirs["other"], "how": "resume", "tab_id": "TAB-16"}],
          f"the plain tab after the failure is still opened ({res.get('plain')})")
    check(h.called(["session", "close", "--session", "PANE-C-15"]) >= 0, "the half-made tab is closed again")
    check(h.record("alpha")["tab_id"] == "TAB-13", "a failed remake leaves the record as it was")
    p = h.run(["restore"], {"FAKE_IT2_FAIL": "session split"})
    check(p.returncode == 1 and "failed alpha: " in p.stdout and "1 failed" in p.stdout, f"human failure line ({p.stdout!r})")


def test_restore_by_stamp(h):
    """restore by stamp, and a bad stamp."""
    res = h.run_json(["restore", h.stamp1, "--dry-run", "--json"])
    check(res["stamp"] == h.stamp1, "restore STAMP picks that snapshot")
    res = h.run_json(["restore", "latest", "--dry-run", "--json"])
    check(res["stamp"] == h.stamp2, "restore latest is the newest")
    p = h.run(["restore", "nope"])
    check(p.returncode == 1 and "no snapshot nope" in p.stderr, f"restore of a bad stamp exits 1 ({p.stderr.strip()})")


def test_log(h):
    """The log."""
    try:
        with open(os.path.join(h.tmp, "log", "savannah.log")) as f:
            text = f.read()
    except OSError:
        text = ""
    check('msg="snapshot written"' in text and "msg=restore " in text and 'msg="restore failed"' in text,
          "snapshot and restore log what they did")



# The sections, in the order they must run (see RestoreHarness).
SECTIONS = (
    test_module_units,
    test_reserved_name,
    test_nothing_yet,
    test_world_before_reboot,
    test_snapshot,
    test_snapshots_listing,
    test_plan_before_reboot,
    test_reboot,
    test_restore,
    test_no_plain,
    test_failing_it2_call,
    test_restore_by_stamp,
    test_log,
)


def main():
    h = RestoreHarness(sys.argv[1], sys.argv[2])
    for section in SECTIONS:
        section(h)
    shutil.rmtree(h.tmp, ignore_errors=True)
    return finish("test_restore", "ok", count=True)


if __name__ == "__main__":
    sys.exit(main())
