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

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

failures = 0


def check(cond, label):
    global failures
    if not cond:
        print(f"FAIL: {label}")
        failures += 1


def load_module(path):
    spec = importlib.util.spec_from_file_location("savannah_work_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    module = os.path.abspath(sys.argv[1])
    fake = os.path.abspath(sys.argv[2])
    tmp = tempfile.mkdtemp(prefix="savannah-restore-test-")
    workroot = os.path.join(tmp, "work")
    sessions = os.path.join(tmp, "sessions")
    claude_dir = os.path.join(tmp, "claude")
    transcripts = os.path.join(tmp, "tr")
    log = os.path.join(tmp, "it2.log")
    for d in (sessions, claude_dir, transcripts):
        os.makedirs(d)

    env = dict(os.environ)
    env["SAVANNAH_WORK_DIR"] = workroot
    env["SAVANNAH_SESSIONS_DIR"] = sessions
    env["SAVANNAH_LOG_DIR"] = os.path.join(tmp, "log")
    env["CLAUDE_CONFIG_DIR"] = claude_dir
    env["SAVANNAH_IT2"] = f"{shlex.quote(sys.executable)} {shlex.quote(fake)}"
    env["SAVANNAH_HUB_FILE"] = os.path.join(tmp, "hub.json")
    for k in ("SAVANNAH_WORK_PY", "SAVANNAH_DASH_BIN", "SAVANNAH_CLAUDE_BIN", "SAVANNAH_BIN",
              "FAKE_IT2_MISSING", "FAKE_IT2_FAIL", "FAKE_IT2_NO_WINDOW"):
        env.pop(k, None)
    env["FAKE_IT2_LOG"] = log
    env["ITERM_SESSION_ID"] = "w9t9p9:NOT-IN-FAKE"
    env["HOME"] = tmp

    def run(args, extra_env=None, cwd=None):
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        return subprocess.run([sys.executable, module] + args, text=True, capture_output=True,
                              env=e, cwd=cwd or tmp)

    def run_json(args, extra_env=None, rc=0):
        p = run(args, extra_env)
        check(p.returncode == rc, f"{' '.join(args)} exits {rc} (got {p.returncode}, stderr: {p.stderr.strip()})")
        try:
            return json.loads(p.stdout)
        except ValueError:
            check(False, f"{' '.join(args)} emits JSON (got {p.stdout!r})")
            return {}

    def it2(*args):
        """Drive the fake directly: what iTerm2 itself does to its world
        (closing panes when it quits)."""
        p = subprocess.run([sys.executable, fake] + list(args), text=True, capture_output=True, env=env)
        check(p.returncode == 0, f"fake it2 {' '.join(args)} ({p.stderr.strip()})")
        return p.stdout

    def calls():
        try:
            with open(log) as f:
                return [json.loads(ln) for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return []

    def called(prefix, last=None):
        for i, c in enumerate(calls()):
            if c[:len(prefix)] == list(prefix) and (last is None or c[-1] == last):
                return i
        return -1

    def clear_calls():
        try:
            os.remove(log)  # the .state file (the fake's world) stays
        except OSError:
            pass

    def record(name):
        with open(os.path.join(workroot, name, "record.json")) as f:
            return json.load(f)

    def hook(session_id, project, pane, transcript=None, **more):
        rec = {"session_id": session_id, "project": project, "state": "idle", "updated": 100,
               "iterm_session_id": f"w0t1p0:{pane}", "last_prompt": f"prompt of {session_id}"}
        if transcript is not None:
            rec["transcript_path"] = transcript
        rec.update(more)
        with open(os.path.join(sessions, session_id + ".json"), "w") as f:
            json.dump(rec, f)

    def transcript(session_id, where=None):
        path = os.path.join(where or transcripts, session_id + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
        return path

    def projects_dir(project):
        return os.path.join(claude_dir, "projects", sw.encode_project_dir(os.path.realpath(project)))

    def snapshot_file(stamp):
        with open(os.path.join(workroot, "snapshots", stamp + ".json")) as f:
            return json.load(f)

    sw = load_module(module)

    # ---- unit-level: the name rule, titles, the claude command ----------------
    check(sw.valid_name("snapshots") is False and sw.valid_name("archive") is False,
          "snapshots and archive are reserved names")
    check(sw.valid_name("snapshot") is True, "snapshot (singular) is an ordinary name")
    check("snapshots" in sw.NAME_HELP, "NAME_HELP names the reserved snapshots")
    check(sw.tab_title("demo (claude)") == "demo", "tab_title strips the process")
    check(sw.tab_title("~/ng/dbjwhs (-zsh)") == "~/ng/dbjwhs", "tab_title keeps a path")
    check(sw.tab_title("(claude)") == "" and sw.tab_title(" (claude) ") == "",
          "tab_title of a bare process is empty")
    check(sw.tab_title("plain") == "plain" and sw.tab_title("") == "", "tab_title leaves the rest")
    check(sw.claude_command("/w", "n", "", resume_id="abc") == "cd /w && claude -n n --resume abc",
          "claude_command --resume <id>")
    check(sw.claude_command("/w", "n", "", resume=True) == "cd /w && claude -n n --continue",
          "claude_command --continue")
    check(sw.claude_command("/w", "n", "", resume=True, resume_id="abc") == "cd /w && claude -n n --resume abc",
          "resume_id wins over resume")
    check(sw.claude_command("/w", "", "", resume_id="abc") == "cd /w && claude --resume abc",
          "no name, no -n (a plain tab)")
    check(sw.claude_command("/w", "", "") == "cd /w && claude", "a fresh plain claude")

    # ---- the reserved name through the CLI --------------------------------------
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    p = run(["new", "snapshots", "--no-worktree", "--repo", repo])
    check(p.returncode == 1 and "snapshots" in p.stderr, f"new snapshots is refused ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(workroot, "snapshots", "record.json")),
          "the refusal writes no record")

    # ---- nothing to list or restore yet --------------------------------------------
    p = run(["snapshots"])
    check(p.returncode == 0 and "no snapshots" in p.stdout, "snapshots with none says so, exit 0")
    check(run_json(["snapshots", "--json"]) == [], "snapshots --json with none is []")
    p = run(["restore"])
    check(p.returncode == 1 and "no snapshot (work snapshot makes one)" in p.stderr,
          f"restore with no snapshot exits 1 with the hint ({p.stderr.strip()})")
    p = run(["restore", "20000101-000000"])
    check(p.returncode == 1 and "no snapshot 20000101-000000" in p.stderr,
          f"restore of an unknown stamp exits 1 ({p.stderr.strip()})")
    p = run(["restore", "--dry-run"])
    check(p.returncode == 1, "dry-run without a snapshot still exits 1")

    # ---- the world before the reboot --------------------------------------------
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
    dirs = {n: os.path.join(tmp, "d-" + n) for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta",
                                                      "other", "other2")}
    for d in dirs.values():
        os.makedirs(d)
    ids = {}
    for n in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
        ids[n] = run_json(["new", n, "--no-worktree", "--repo", dirs[n], "--json"])
    plainish = run_json(["new", "plainish", "--no-worktree", "--repo", dirs["other"], "--json"])
    shutil.rmtree(os.path.join(workroot, "plainish"))   # the tab stays in the fake's world, recordless
    check(ids["alpha"]["claude_pane"] == "PANE-C-1" and plainish["claude_pane"] == "PANE-C-7",
          "the fake handed out the expected pane ids")
    # gamma's and delta's tabs are already gone before the snapshot (offline rows).
    for pane in (ids["gamma"]["claude_pane"], ids["gamma"]["shell_pane"],
                 ids["delta"]["claude_pane"], ids["delta"]["shell_pane"]):
        it2("session", "close", "--session", pane, "--force")
    hook("s-alpha", dirs["alpha"], ids["alpha"]["claude_pane"], transcript("s-alpha"), state="working")
    hook("s-beta", dirs["beta"], ids["beta"]["claude_pane"])
    transcript("s-beta", projects_dir(dirs["beta"]))
    transcript("older", projects_dir(dirs["gamma"]))
    hook("s-plain", dirs["other"], plainish["claude_pane"], transcript("s-plain"))
    hook("s-dead", dirs["other2"], "PANE-NOT-LISTED", transcript("s-dead"))
    hook("s-noproj", "", "PANE-X")
    # A session in alpha's directory on another pane: a second Claude there,
    # not the record's (join prefers the pane match), so a plain tab.
    hook("s-alpha2", dirs["alpha"], "PANE-NOT-LISTED-2")

    # ---- snapshot -------------------------------------------------------------------
    clear_calls()
    snap = run_json(["snapshot", "--note", "before reboot", "--json"])
    check(set(snap) == {"path", "stamp", "work", "live", "plain", "it2"}, f"snapshot --json shape ({snap})")
    check(snap.get("work") == 6 and snap.get("live") == 2 and snap.get("plain") == 3 and snap.get("it2") is True,
          f"snapshot counts: 6 work (2 live), 3 plain ({snap})")
    check(calls() == [["session", "list", "--json"]], f"snapshot asks it2 only for the session list ({calls()})")
    stamp1 = snap.get("stamp", "")
    check(sw.SNAPSHOT_STAMP_RE.fullmatch(stamp1) is not None, f"stamp is YYYYMMDD-HHMMSS ({stamp1})")
    check(snap.get("path") == os.path.join(workroot, "snapshots", stamp1 + ".json"), "path under <work dir>/snapshots")
    doc = snapshot_file(stamp1)
    with open(os.path.join(workroot, "snapshots", "latest.json")) as f:
        check(json.load(f) == doc, "latest.json is a copy of the newest snapshot")
    check(set(doc) == {"stamp", "taken", "taken_local", "note", "it2", "sessions", "plain"},
          f"snapshot file shape ({sorted(doc)})")
    check(doc["note"] == "before reboot" and isinstance(doc["taken"], int) and doc["taken"] > 0
          and doc["taken_local"].endswith(" local") and doc["it2"] is True, "snapshot header fields")
    names = [e["name"] for e in doc["sessions"]]
    check(names == ["alpha", "beta", "epsilon", "zeta", "delta", "gamma"],
          f"sessions in tab order, the ones whose pane is gone last by name ({names})")
    by = {e["name"]: e for e in doc["sessions"]}
    want = {"name", "title", "worktree", "repo", "branch", "adopted", "priority", "rank", "tab_id",
            "claude_pane", "shell_pane", "tab_title", "pane_alive", "live", "session_id",
            "transcript_path", "state", "last_prompt"}
    for e in doc["sessions"]:
        check(set(e) == want, f"session entry {e['name']} has the contract fields ({sorted(set(e) ^ want)})")
    a = by["alpha"]
    check(a["live"] is True and a["pane_alive"] is True and a["session_id"] == "s-alpha"
          and a["transcript_path"] == os.path.join(transcripts, "s-alpha.jsonl") and a["state"] == "working"
          and a["last_prompt"] == "prompt of s-alpha" and a["tab_id"] == "TAB-1"
          and a["claude_pane"] == "PANE-C-1" and a["shell_pane"] == "PANE-S-1" and a["worktree"] == dirs["alpha"]
          and a["tab_title"] == "alpha" and a["priority"] == "active" and a["adopted"] is False,
          f"alpha: live, with its session ({a})")
    check(by["beta"]["session_id"] == "s-beta" and by["beta"]["transcript_path"] == "", "beta: id without a path")
    g = by["gamma"]
    check(g["live"] is False and g["pane_alive"] is False and g["session_id"] == "" and g["tab_title"] == "",
          f"gamma: offline, no session ({g})")
    plain = doc["plain"]
    projects = [e["project"] for e in plain]
    check(projects == [dirs["other"], dirs["alpha"], dirs["other2"]],
          f"plain tabs: the listed pane first, then the rest by project ({projects})")
    pw = {"project", "pane", "tab_id", "title", "pane_alive", "session_id", "transcript_path", "state", "last_prompt"}
    for e in plain:
        check(set(e) == pw, f"plain entry {e['project']} has the contract fields ({sorted(set(e) ^ pw)})")
    o = plain[0]
    check(o["pane"] == "PANE-C-7" and o["tab_id"] == "TAB-7" and o["title"] == "plainish" and o["pane_alive"] is True
          and o["session_id"] == "s-plain" and o["transcript_path"] == os.path.join(transcripts, "s-plain.jsonl"),
          f"the plain tab: pane, tab, title, session ({o})")
    check(plain[2]["pane_alive"] is False and plain[2]["tab_id"] == "", "a dead hook: pane not alive, no tab")
    check(not any(e.get("session_id") == "s-noproj" for e in plain), "a hook without a project is not a tab")

    # The snapshots directory is never a work session.
    check("snapshots" not in [e["name"] for e in run_json(["ls", "--json"])], "ls skips snapshots/")

    # ---- snapshots listing ---------------------------------------------------------------
    rows = run_json(["snapshots", "--json"])
    check(len(rows) == 1 and set(rows[0]) == {"stamp", "path", "taken", "taken_local", "work", "live", "plain", "note"},
          f"snapshots --json: one row with the contract fields ({rows})")
    check(rows and rows[0]["stamp"] == stamp1 and rows[0]["work"] == 6 and rows[0]["live"] == 2
          and rows[0]["plain"] == 3 and rows[0]["note"] == "before reboot", f"snapshots row values ({rows})")
    p = run(["snapshots"])
    check(p.returncode == 0 and p.stdout.splitlines()[0].split() == ["STAMP", "TAKEN", "WORK", "LIVE", "PLAIN", "NOTE"]
          and stamp1 in p.stdout and "before reboot" in p.stdout, f"human snapshots table ({p.stdout!r})")
    p = run(["snapshot"])
    check(p.returncode == 0 and p.stdout.startswith("snapshot ") and "6 work sessions (2 live), 3 plain tabs" in p.stdout,
          f"human snapshot line ({p.stdout!r})")
    rows = run_json(["snapshots", "--json"])
    check(len(rows) == 2 and rows[0]["stamp"] > rows[1]["stamp"] == stamp1,
          f"two snapshots, newest first, distinct stamps ({[r['stamp'] for r in rows]})")
    check(rows[0]["note"] == "", "a snapshot without --note has an empty note")
    stamp2 = rows[0]["stamp"]
    check(snapshot_file(stamp2)["sessions"] == doc["sessions"], "the second snapshot saw the same world")
    with open(os.path.join(workroot, "snapshots", "latest.json")) as f:
        check(json.load(f)["stamp"] == stamp2, "latest.json follows the newest snapshot")

    # Without it2 the snapshot still writes and says so.
    p = run(["snapshot", "--json"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check(p.returncode == 0, f"snapshot without it2 exits 0 ({p.stderr.strip()})")
    noit2 = json.loads(p.stdout) if p.returncode == 0 else {}
    check(noit2.get("it2") is False and noit2.get("work") == 6, f"snapshot without it2: it2 false ({noit2})")
    nd = snapshot_file(noit2.get("stamp", stamp2))
    check(all(e["pane_alive"] is None for e in nd["sessions"] + nd["plain"]), "without it2 pane_alive is null")
    check([e["name"] for e in nd["sessions"]] == sorted(names), "without it2 the order is by name")
    p = run(["snapshot"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check("it2 unavailable" in p.stdout, f"human snapshot without it2 says so ({p.stdout!r})")
    # Put latest back to the real one so restore below uses the full snapshot.
    shutil.copy(os.path.join(workroot, "snapshots", stamp2 + ".json"),
                os.path.join(workroot, "snapshots", "latest.json"))

    # ---- the plan before anything is gone -------------------------------------------------
    clear_calls()
    plan = run_json(["restore", "--dry-run", "--json"])
    check(set(plan) == {"stamp", "path", "dry_run", "restored", "plain", "skipped", "failed"},
          f"restore --json shape ({sorted(plan)})")
    check(plan["dry_run"] is True and plan["stamp"] == stamp2 and plan["failed"] == [], f"dry-run plan header ({plan})")
    skipped = {s.get("name") or s.get("project"): s["why"] for s in plan["skipped"]}
    check(skipped.get("alpha") == "alive" and skipped.get("beta") == "alive", f"alive tabs are skipped ({skipped})")
    check(skipped.get(dirs["other"]) == "alive", f"a live plain tab is skipped ({skipped})")
    check(skipped.get(dirs["other2"]) == "not open when the snapshot was taken"
          and skipped.get(dirs["alpha"]) == "not open when the snapshot was taken",
          f"a hook whose pane was already gone is not reopened ({skipped})")
    check(skipped.get("epsilon") == "alive" and skipped.get("zeta") == "alive",
          f"epsilon and zeta still have their tabs at this point ({skipped})")
    would = {r["name"]: r for r in plan["restored"]}
    check(set(would) == {"gamma", "delta"}, f"the offline sessions would be restored ({would})")
    check(would["gamma"]["how"] == "continue" and would["delta"]["how"] == "fresh"
          and all(r["tab_id"] == "" for r in plan["restored"]), f"dry-run hows, no tab ids ({would})")
    check(plan["plain"] == [], "no plain tab to open yet")
    check(calls() == [["session", "list", "--json"]], f"dry-run touches nothing ({calls()})")
    p = run(["restore", "--dry-run"])
    check(p.returncode == 0 and "would restore gamma (continue)" in p.stdout and "skipped alpha: alive" in p.stdout
          and "plan from snapshot" in p.stdout and "nothing touched" in p.stdout, f"human dry-run ({p.stdout!r})")
    p = run(["restore", "--dry-run", "--json"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check(p.returncode == 0 and json.loads(p.stdout)["dry_run"] is True, "dry-run without it2 still plans")

    # ---- the reboot --------------------------------------------------------------------------
    # iTerm2 quit: every pane is gone (the fake's counter keeps counting, so
    # the remade tabs get new ids). Claude Code exited: the hook files are
    # gone, except one stale file whose pane no longer exists.
    for pane in ("PANE-C-1", "PANE-S-1", "PANE-C-2", "PANE-S-2", "PANE-C-5", "PANE-S-5",
                 "PANE-C-6", "PANE-S-6", "PANE-C-7", "PANE-S-7"):
        it2("session", "close", "--session", pane, "--force")
    check(json.loads(it2("session", "list", "--json")) == [], "every pane is gone")
    for sid in ("s-alpha", "s-beta", "s-dead", "s-noproj", "s-alpha2"):
        os.remove(os.path.join(sessions, sid + ".json"))
    shutil.rmtree(os.path.join(workroot, "epsilon"))
    shutil.rmtree(dirs["zeta"])

    # ---- restore ------------------------------------------------------------------------------
    clear_calls()
    res = run_json(["restore", "--json"])
    check(res["dry_run"] is False and res["stamp"] == stamp2 and res["failed"] == [], f"restore header ({res})")
    got = {r["name"]: r for r in res["restored"]}
    check([r["name"] for r in res["restored"]] == ["alpha", "beta", "delta", "gamma"],
          f"restored in snapshot order ({[r['name'] for r in res['restored']]})")
    check(got["alpha"]["how"] == "resume" and got["beta"]["how"] == "resume" and got["gamma"]["how"] == "continue"
          and got["delta"]["how"] == "fresh", f"hows ({got})")
    check(got["alpha"]["tab_id"] == "TAB-8" and got["beta"]["tab_id"] == "TAB-9" and got["delta"]["tab_id"] == "TAB-10"
          and got["gamma"]["tab_id"] == "TAB-11", f"new tab ids ({got})")
    skipped = {s.get("name") or s.get("project"): s["why"] for s in res["skipped"]}
    check(skipped.get("epsilon") == "no record" and skipped.get("zeta") == "worktree gone", f"skips ({skipped})")
    check(res["plain"] == [{"project": dirs["other"], "how": "resume", "tab_id": "TAB-12"}], f"plain tab ({res['plain']})")
    # Records point at the new tabs.
    ra = record("alpha")
    check(ra["tab_id"] == "TAB-8" and ra["claude_pane"] == "PANE-C-8" and ra["shell_pane"] == "PANE-S-8",
          f"alpha's record follows its new tab ({ra})")
    check(record("gamma")["tab_id"] == "TAB-11", "gamma's record follows its new tab")
    # The exact it2 conversation: each work session gets a tab, cd + claude
    # with the right resume form, a split and the shell below; the plain tab
    # gets one pane, no split, named after its old title.
    c = calls()
    da, db, dg, dd, do = (dirs[n] for n in ("alpha", "beta", "gamma", "delta", "other"))
    check(called(["session", "run", "--session", "PANE-C-8", "--"], last=f" cd {da} && claude -n alpha --resume s-alpha") >= 0,
          f"alpha: claude -n alpha --resume s-alpha ({c})")
    check(called(["session", "run", "--session", "PANE-C-9", "--"], last=f" cd {db} && claude -n beta --resume s-beta") >= 0,
          "beta: resumed by the id found in Claude Code's projects dir")
    check(called(["session", "run", "--session", "PANE-C-10", "--"], last=f" cd {dd} && claude -n delta") >= 0,
          "delta: a fresh claude")
    check(called(["session", "run", "--session", "PANE-C-11", "--"], last=f" cd {dg} && claude -n gamma --continue") >= 0,
          "gamma: claude --continue")
    check(called(["session", "split", "--session", "PANE-C-8"]) >= 0
          and called(["session", "run", "--session", "PANE-S-8", "--"], last=f" cd {da}") >= 0,
          "alpha: split with the shell below")
    check(called(["session", "set-name", "--session", "PANE-C-8", "--"], last="alpha") >= 0, "alpha: tab named")
    check(called(["session", "run", "--session", "PANE-C-12", "--"], last=f" cd {do} && claude --resume s-plain") >= 0,
          f"plain: cd + claude --resume <id>, no -n ({c})")
    check(called(["session", "set-name", "--session", "PANE-C-12", "--"], last="plainish") >= 0,
          "plain: named after its old title")
    check(called(["session", "split", "--session", "PANE-C-12"]) < 0, "plain: a single pane, no split")
    check(called(["session", "close"]) < 0 and called(["tab", "close"]) < 0, "restore closes nothing")
    check(sum(1 for x in c if x[:2] == ["tab", "new"]) == 5, f"five tabs made ({c})")
    live = {s["id"] for s in json.loads(it2("session", "list", "--json"))}
    check({"PANE-C-8", "PANE-S-8", "PANE-C-9", "PANE-C-11", "PANE-C-12"} <= live and "PANE-S-12" not in live,
          f"the fake's world after the restore ({sorted(live)})")

    # Human output of a restore (on a copy of the world: close alpha again).
    it2("session", "close", "--session", "PANE-C-8", "--force")
    it2("session", "close", "--session", "PANE-S-8", "--force")
    p = run(["restore"])
    check(p.returncode == 0 and "restored alpha (resume s-alpha, tab TAB-13)" in p.stdout
          and "skipped beta: alive" in p.stdout and "skipped epsilon: no record" in p.stdout
          and "skipped zeta: worktree gone" in p.stdout and "restored from snapshot" in p.stdout,
          f"human restore lines ({p.stdout!r} {p.stderr!r})")
    # The plain tab came back a second time: its hook file still names the
    # pane that died with iTerm2. Once the resumed Claude's hook writes the
    # new pane, restore leaves it alone.
    check("plain " in p.stdout and "(resume s-plain, tab TAB-14)" in p.stdout, f"plain tab line ({p.stdout!r})")
    hook("s-plain", dirs["other"], "PANE-C-14", os.path.join(transcripts, "s-plain.jsonl"))
    res = run_json(["restore", "--json"])
    skipped = {s.get("name") or s.get("project"): s["why"] for s in res["skipped"]}
    check(res["restored"] == [] and res["plain"] == [] and skipped.get(dirs["other"]) == "alive"
          and skipped.get("alpha") == "alive", f"everything alive: nothing remade ({res})")

    # ---- --no-plain ----------------------------------------------------------------------------
    it2("session", "close", "--session", "PANE-C-14", "--force")
    clear_calls()
    res = run_json(["restore", "--no-plain", "--json"])
    check(res["plain"] == [] and res["restored"] == [] and called(["tab", "new"]) < 0
          and not any(s.get("project") for s in res["skipped"]),
          f"--no-plain leaves the plain tabs out entirely ({res} {calls()})")

    # ---- a failing it2 call is recorded, the rest continues, exit 1 ----------------
    it2("session", "close", "--session", "PANE-C-13", "--force")
    it2("session", "close", "--session", "PANE-S-13", "--force")
    clear_calls()
    p = run(["restore", "--json"], {"FAKE_IT2_FAIL": "session split"})
    check(p.returncode == 1, f"a failed item makes restore exit 1 ({p.stdout!r} {p.stderr!r})")
    try:
        res = json.loads(p.stdout)
    except ValueError:
        res = {}
        check(False, f"restore --json still prints JSON on failure ({p.stdout!r})")
    check(len(res.get("failed", [])) == 1 and res["failed"][0].get("name") == "alpha" and "boom" in res["failed"][0]["error"],
          f"alpha's split failed and is recorded ({res.get('failed')})")
    check(res.get("plain") == [{"project": dirs["other"], "how": "resume", "tab_id": "TAB-16"}],
          f"the plain tab after the failure is still opened ({res.get('plain')})")
    check(called(["session", "close", "--session", "PANE-C-15"]) >= 0, "the half-made tab is closed again")
    check(record("alpha")["tab_id"] == "TAB-13", "a failed remake leaves the record as it was")
    p = run(["restore"], {"FAKE_IT2_FAIL": "session split"})
    check(p.returncode == 1 and "failed alpha: " in p.stdout and "1 failed" in p.stdout, f"human failure line ({p.stdout!r})")

    # ---- restore by stamp, and a bad stamp ----------------------------------------------------
    res = run_json(["restore", stamp1, "--dry-run", "--json"])
    check(res["stamp"] == stamp1, "restore STAMP picks that snapshot")
    res = run_json(["restore", "latest", "--dry-run", "--json"])
    check(res["stamp"] == stamp2, "restore latest is the newest")
    p = run(["restore", "nope"])
    check(p.returncode == 1 and "no snapshot nope" in p.stderr, f"restore of a bad stamp exits 1 ({p.stderr.strip()})")

    # ---- the log ----------------------------------------------------------------------------------------
    try:
        with open(os.path.join(tmp, "log", "savannah.log")) as f:
            text = f.read()
    except OSError:
        text = ""
    check('msg="snapshot written"' in text and "msg=restore " in text and 'msg="restore failed"' in text,
          "snapshot and restore log what they did")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures:
        print(f"test_restore: {failures} failure(s)")
    else:
        print("test_restore: ok")
    return failures


if __name__ == "__main__":
    sys.exit(main())
