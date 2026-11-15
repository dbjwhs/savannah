#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Scan test: drives `work scan` in work/savannah_work.py against a temp
# SAVANNAH_SESSIONS_DIR, a temp CLAUDE_CONFIG_DIR holding a hand-written
# Claude Code session registry (sessions/<pid>.json), and the fake it2. The
# "running Claude Code processes" are sleepers this test starts, so their
# pids are alive and theirs to kill. No real iTerm2, no claude, no network,
# no git, stdlib only. Usage:
#   test_scan.py <path/to/savannah_work.py> <path/to/fake_it2.py>

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import Harness, check, finish, load_module  # noqa: E402


class ScanHarness(Harness):
    def __init__(self, module, fake):
        super().__init__(module, fake, prefix="savannah-scan-test-")
        self.config = os.path.join(self.tmp, "claude")
        self.registry = os.path.join(self.config, "sessions")
        os.makedirs(self.registry)
        self.env["CLAUDE_CONFIG_DIR"] = self.config
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.sleepers = []

    def sleeper(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.sleepers.append(p)
        return p.pid

    def register(self, pid, sid, **over):
        d = {"pid": pid, "sessionId": sid, "cwd": self.repo, "kind": "interactive", "status": "idle",
             "updatedAt": 1794776649856, "name": "repo-" + sid}
        d.update(over)
        with open(os.path.join(self.registry, f"{pid}.json"), "w") as f:
            json.dump(d, f)

    def status_file(self, sid):
        path = os.path.join(self.sessions, sid + ".json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def close(self):
        for p in self.sleepers:
            p.kill()
            p.wait()


def test_units(h):
    """The pure halves: which registry entries count, which are unknown."""
    sw = load_module(h.module)
    scan = sw.savannah_scan
    os.environ["CLAUDE_CONFIG_DIR"] = h.config
    try:
        h.register(101, "live", procStart="Sat Nov 14 06:27:00 2026", status="busy")
        h.register(102, "reused", procStart="Sat Nov 14 06:27:00 2026")
        h.register(103, "gone")
        h.register(104, "headless", kind="sdk")
        h.register(105, "", cwd="/x")
        h.register(106, "nocwd", cwd="")
        h.register("107", "strpid")
        with open(os.path.join(h.registry, "junk.json"), "w") as f:
            f.write("not json")
        with open(os.path.join(h.registry, "list.json"), "w") as f:
            f.write("[]")
        table = {101: ("/dev/ttys001", "Sat Nov 14 06:27:00 2026"),
                 102: ("/dev/ttys002", "Sun Nov 15 01:00:00 2026")}
        got = scan.read_registry(info=table.get)
        check([g["session_id"] for g in got] == ["live"],
              f"only the interactive session whose process is the one the file names counts ({got})")
        live = got[0]
        check(live["state"] == "working" and live["updated"] == 1794776649 and live["tty"] == "/dev/ttys001",
              f"busy is working, updatedAt is seconds, the tty travels ({live})")
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]
        for f in os.listdir(h.registry):
            os.remove(os.path.join(h.registry, f))
    running = [dict(live, session_id=s, tty=t) for s, t in
               (("a", "/dev/ttys001"), ("b", "/dev/ttys002"), ("c", ""), ("d", "/dev/ttys004"), ("e", "/dev/ttys009"))]
    hooks = [{"session_id": "a", "iterm_session_id": "w0t0p0:P1"},
             {"session_id": "old-d", "iterm_session_id": "w0t3p0:P4"}]
    panes = [{"id": "P1", "tty": "/dev/ttys001"}, {"id": "P2", "tty": "/dev/ttys002"},
             {"id": "P4", "tty": "/dev/ttys004"}, {"id": "P5"}]
    got = {u["session_id"]: u["pane"] for u in scan.unknown_sessions(running, hooks, panes)}
    check(got == {"b": "P2", "c": "", "e": ""},
          f"known ids and panes another file holds are left alone; a pane is found by tty ({got})")
    rec = scan.status_record(dict(live, pane="P2"))
    check(rec["iterm_session_id"] == "w0t0p0:P2" and rec["scanned"] is True and rec["state"] == "working"
          and rec["pid"] == 101 and rec["last_prompt"] == "" and rec["transcript_path"] == "",
          f"the status file is the hook's shape ({rec})")
    check("iterm_session_id" not in scan.status_record(dict(live, pane="")), "no pane, no pane field")
    check(scan.process_info(os.getpid()) is not None and scan.process_info(2 ** 22 + 12345) is None,
          "process_info knows a running process from none")


def test_scan(h):
    known, lost, dead = h.sleeper(), h.sleeper(), h.sleeper()
    h.register(known, "s-known")
    h.register(lost, "s-lost", status="busy")
    h.register(dead, "s-dead")
    h.sleepers[-1].kill()
    h.sleepers[-1].wait()
    with open(os.path.join(h.sessions, "s-known.json"), "w") as f:
        json.dump({"session_id": "s-known", "project": h.repo, "state": "done", "updated": 5}, f)

    j = h.run_json(["scan", "--dry-run", "--json"])
    check(j.get("running") == 2 and [f["session_id"] for f in j.get("found", [])] == ["s-lost"]
          and j.get("dry_run") is True, f"dry run: two run, one is unknown ({j})")
    check(h.status_file("s-lost") is None, "a dry run writes nothing")

    p = h.run(["scan"])
    check(p.returncode == 0 and "added 1 of 2" in p.stdout and "repo-s-lost" in p.stdout and "s-lost" in p.stdout,
          f"scan says what it added ({p.stdout!r} {p.stderr!r})")
    rec = h.status_file("s-lost")
    check(rec is not None and rec.get("project") == h.repo and rec.get("state") == "working"
          and rec.get("pid") == lost and rec.get("scanned") is True, f"the lost tab has a status file ({rec})")
    check(h.status_file("s-known") == {"session_id": "s-known", "project": h.repo, "state": "done", "updated": 5},
          "a tab that already had its file is not touched")
    check(h.status_file("s-dead") is None, "a registry entry whose process is gone is not a tab")

    p = h.run(["scan"])
    check(p.returncode == 0 and "all 2 running" in p.stdout, f"a second scan has nothing to add ({p.stdout!r})")
    # The row is real for every other verb: adopt takes it by its id.
    j = h.run_json(["adopt", "found-again", "--session", "s-lost", "--json"])
    check(j.get("name") == "found-again" and j.get("worktree") == h.repo, f"a scanned tab can be adopted ({j})")

    for f in os.listdir(h.registry):
        os.remove(os.path.join(h.registry, f))
    p = h.run(["scan"])
    check(p.returncode == 0 and "no running Claude Code session found" in p.stdout,
          f"an empty registry is said, not an error ({p.stdout!r})")
    with open(os.path.join(h.logdir, "savannah.log")) as f:
        check(any("msg=scan " in ln and "found=1" in ln for ln in f), "the scan is logged")


def main():
    if len(sys.argv) != 3:
        print("usage: test_scan.py <savannah_work.py> <fake_it2.py>")
        return 2
    h = ScanHarness(sys.argv[1], sys.argv[2])
    try:
        test_units(h)
        test_scan(h)
    finally:
        h.close()
    return finish("test_scan")


if __name__ == "__main__":
    sys.exit(main())
