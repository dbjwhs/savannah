#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Work-session test: drives work/savannah_work.py the way a human (or the
# shim) does, against a temp SAVANNAH_WORK_DIR, a temp SAVANNAH_SESSIONS_DIR
# with hand-written hook files, a throwaway git repo, and the fake osascript
# (tools/fake_osascript.py) via SAVANNAH_OSASCRIPT. No real iTerm2, no real
# claude, no network, stdlib only. git is isolated from the user's config
# (HOME=tmp, GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1, identity via
# GIT_AUTHOR_*/GIT_COMMITTER_*). Worktree cases are skipped (with a message,
# still exit 0) ONLY when git is not on PATH; a git that is present but fails
# to init/commit is a test FAILURE. Usage:
#   test_work.py <path/to/savannah_work.py> <path/to/fake_osascript.py>

import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
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
    tmp = tempfile.mkdtemp(prefix="savannah-work-test-")
    workroot = os.path.join(tmp, "work")
    sessions = os.path.join(tmp, "sessions")
    log = os.path.join(tmp, "osascript.log")
    os.makedirs(sessions)

    env = dict(os.environ)
    env["SAVANNAH_WORK_DIR"] = workroot
    env["SAVANNAH_SESSIONS_DIR"] = sessions
    env["SAVANNAH_OSASCRIPT"] = f"{shlex.quote(sys.executable)} {shlex.quote(fake)}"
    env["FAKE_OSASCRIPT_LOG"] = log
    env.pop("FAKE_OSASCRIPT_MISSING", None)
    # git isolation: the user's config must not leak into the throwaway repo
    # (hooks, signing, templates, default branch...), and identity is explicit.
    env["HOME"] = tmp
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        env[k] = "t"
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        env[k] = "t@example.com"
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(k, None)

    def run(args, extra_env=None, stdin=None, cwd=None):
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        return subprocess.run([sys.executable, module] + args, input=stdin,
                              text=True, capture_output=True, env=e, cwd=cwd or tmp)

    def run_json(args, extra_env=None, cwd=None):
        p = run(args, extra_env, cwd=cwd)
        check(p.returncode == 0, f"{' '.join(args)} exits 0 (stderr: {p.stderr.strip()})")
        try:
            return json.loads(p.stdout)
        except ValueError:
            check(False, f"{' '.join(args)} emits JSON (got {p.stdout!r})")
            return {}

    def read_log():
        try:
            with open(log) as f:
                return f.read()
        except OSError:
            return ""

    def clear_log():
        for p in (log, log + ".counter"):
            try:
                os.remove(p)
            except OSError:
                pass

    def record(name):
        with open(os.path.join(workroot, name, "record.json")) as f:
            return json.load(f)

    def write_record(name, rec):
        os.makedirs(os.path.join(workroot, name), exist_ok=True)
        with open(os.path.join(workroot, name, "record.json"), "w") as f:
            json.dump(rec, f)

    def git(repo, *args):
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, env=env)

    def wt_of(name):
        return os.path.join(os.path.abspath(repo), ".savannah-worktrees", name)

    have_git = shutil.which("git") is not None
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    if have_git:
        ok = True
        for args in (("init", "-q"), ("symbolic-ref", "HEAD", "refs/heads/main")):
            r = git(repo, *args)
            if r.returncode != 0:
                check(False, f"git {' '.join(args)} in the throwaway repo: {r.stderr.strip()}")
                ok = False
        with open(os.path.join(repo, "README"), "w") as f:
            f.write("hi\n")
        r = git(repo, "add", "README")
        if r.returncode != 0:
            check(False, f"git add in the throwaway repo: {r.stderr.strip()}")
            ok = False
        r = git(repo, "commit", "-q", "-m", "init")
        if r.returncode != 0:
            check(False, f"git commit in the throwaway repo (git present, so this is a failure): {r.stderr.strip()}")
            ok = False
        if not ok:
            have_git = False  # the worktree cases cannot run, but the failure above is recorded
    else:
        print("test_work: git not found, skipping worktree cases")

    # ---- unit-level checks on the module itself -------------------------------
    sw = load_module(module)
    check(not sw.valid_name("archive"), "valid_name: 'archive' is reserved for the notes graveyard")
    check(sw.ARCHIVE_SUBDIR == "archive", "archive subdir constant")
    for bad in ("a..b", "a.", "a.lock", "ok\n", "..", "x..", "t-1", "", "A"):
        check(not sw.valid_name(bad), f"valid_name rejects {bad!r}")
    for good in ("a.b", "a-lock", "alock", "t-1x", "0", "a" * 64, "x.lockx"):
        check(sw.valid_name(good), f"valid_name accepts {good!r}")

    class P:
        def __init__(self, stderr, rc=128):
            self.stderr, self.returncode = stderr, rc
    check(sw.git_fail_message(P("Preparing worktree\nfatal: boom\nhint: try this\nhint: or that"), "x")
          == "x failed: fatal: boom", "git_fail_message prefers the fatal: line over trailing hints")
    check(sw.git_fail_message(P("error: first\nerror: second\nhint: h"), "x") == "x failed: error: second",
          "git_fail_message takes the LAST fatal:/error: line")
    check(sw.git_fail_message(P("something odd\nhint: h"), "x") == "x failed: something odd",
          "git_fail_message falls back to the last non-hint line")
    check(sw.git_fail_message(P("hint: only"), "x") == "x failed: hint: only",
          "git_fail_message uses the last line when every line is a hint")
    check(sw.git_fail_message(P("", 3), "x") == "x failed: rc 3", "git_fail_message with empty stderr")
    check(sw.join_session({"worktree": "", "claude_pane": ""},
                          [{"project": os.getcwd(), "session_id": "s"}]) is None,
          "join_session: empty worktree never joins (realpath('') would be cwd)")

    # timeout message mentions both causes; patched subprocess so nothing hangs
    real_run = sw.subprocess.run

    def fake_timeout(*a, **k):
        raise sw.subprocess.TimeoutExpired(cmd=a[0], timeout=60)
    sw.subprocess.run = fake_timeout
    try:
        sw.ITerm().write("P", "x")
        check(False, "timeout raises UserError")
    except sw.UserError as e:
        check("Automation" in str(e) and "Privacy & Security" in str(e) and "iTerm2" in str(e),
              f"timeout message names iTerm2 and the Automation permission dialog ({e})")
    finally:
        sw.subprocess.run = real_run

    # ---- bad usage -> 2 ----------------------------------------------------
    check(run([]).returncode == 2, "no verb exits 2")
    check(run(["bogus"]).returncode == 2, "unknown verb exits 2")
    check(run(["new"]).returncode == 2, "new without name exits 2")
    check(run(["say", "x"]).returncode == 2, "say without text exits 2")
    p = run(["new", "nb", "--no-worktree", "--branch", "main", "--repo", repo])
    check(p.returncode == 2 and "--branch" in p.stderr and "--no-worktree" in p.stderr,
          f"--branch with --no-worktree exits 2 with a clear message ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(workroot, "nb")), "--branch + --no-worktree creates nothing")

    # ---- name validation ---------------------------------------------------
    for bad in ("Caps", "-lead", "has space", "t-12", "a" * 65, "", "dots..ok?",
                "a..b", "a.", "a.lock", "x\n"):
        p = run(["new", bad, "--no-worktree", "--repo", repo])
        check(p.returncode in (1, 2) and not os.path.exists(os.path.join(workroot, bad or "_")),
              f"name {bad!r} rejected")
    check(run(["new", "t-12x", "--no-worktree", "--repo", repo, "--json"]).returncode == 0,
          "t-12x (not a pure task id) accepted")
    check(run(["new", "ok.name_1-x", "--no-worktree", "--repo", repo]).returncode == 0,
          "dotted/underscored/dashed name accepted")

    # ---- unknown name -> 1 on every verb -----------------------------------
    for verb in (["notes", "nope"], ["note", "nope", "x"], ["say", "nope", "x"],
                 ["sh", "nope", "x"], ["jump", "nope"], ["rm", "nope"]):
        p = run(verb)
        check(p.returncode == 1 and "nope" in p.stderr and p.stderr.count("\n") == 1,
              f"{verb[0]} unknown name exits 1 with one stderr line")

    # ---- new --no-worktree ---------------------------------------------------
    clear_log()
    rec = run_json(["new", "flat", "--no-worktree", "--repo", repo, "--json",
                    "--prompt", "say hi \"there\"", "--note", "first note"])
    check(rec.get("worktree") == os.path.abspath(repo), "no-worktree: worktree == repo")
    check(rec.get("branch") == "", "no-worktree: branch empty")
    check(rec.get("terminal") == "iterm", "terminal is iterm")
    check(rec.get("tab_id") == "TAB-1", "tab id from fake osascript")
    check(rec.get("claude_pane") == "PANE-C-1" and rec.get("shell_pane") == "PANE-S-1",
          "pane ids from fake osascript")
    check(rec.get("prompt") == 'say hi "there"', "prompt recorded")
    check(set(rec) == {"name", "repo", "worktree", "branch", "created", "updated", "terminal",
                       "tab_id", "claude_pane", "shell_pane", "prompt"}, "record has exactly the contract fields")
    check(isinstance(rec.get("created"), int) and rec.get("created") > 0, "created is epoch int")
    check(record("flat") == rec, "record.json on disk matches --json output")
    with open(os.path.join(workroot, "flat", "notes.md")) as f:
        check(f.read().endswith(" local\nfirst note\n\n"), "--note appended to notes.md")

    # The exact AppleScript: cd into the worktree in both panes, prompt shlex-quoted.
    sent = read_log()
    rq = os.path.abspath(repo)
    check(f'write text "cd {rq} && claude ' + "'say hi \\\"there\\\"'\"" in sent,
          "upper pane gets cd <worktree> && claude <shlex-quoted prompt>, AppleScript-escaped")
    check(f'write text "cd {rq}"' in sent, "lower pane gets cd <worktree>")
    check('set name to "flat"' in sent, "session named after the work session")
    check("create tab with default profile" in sent and "split horizontally with same profile" in sent,
          "tab created then split horizontally")
    # No-window path: the freshly created window's own tab is used, no stray tab.
    check("set madeWindow to true" in sent and "set t to current tab of w" in sent,
          "create script uses the new window's current tab when it had to create the window")
    check(re.search(r"if madeWindow then\s+set t to current tab of w\s+else\s+tell w\s+"
                    r"set t to \(create tab with default profile\)", sent) is not None,
          "create tab only on the else branch (window already existed)")
    guard = 'if application "iTerm2" is not running then return false'
    check(guard not in sent, "create script may launch iTerm2 (no running guard)")
    # Found live 10/27/2026: bare property references on the returned tab do
    # not coerce, and a failure after the tab exists must not orphan it.
    check("(get id of w)" in sent and "(get id of cs)" in sent and "(get id of ss)" in sent,
          "create script reads every id with an explicit get")
    check("(count of tabs of w)" in sent and "index of t" not in sent,
          "tab position comes from the tab count, never a tab index")
    check(re.search(r"on error msg number num\s+try\s+close t\s+end try\s+error msg number num", sent) is not None,
          "create script closes its own tab on any error before rethrowing")

    # new without --prompt: bare claude.
    clear_log()
    run_json(["new", "bare", "--no-worktree", "--repo", repo, "--json"])
    check(f'write text "cd {rq} && claude"\n' in read_log(), "no prompt: bare claude")

    # duplicate refused
    p = run(["new", "flat", "--no-worktree", "--repo", repo])
    check(p.returncode == 1 and "exists" in p.stderr, "duplicate name refused with exit 1")

    # human line
    p = run(["new", "human", "--no-worktree", "--repo", repo])
    check(p.returncode == 0 and p.stdout.startswith("human: "), "new prints one human line")
    check(p.stdout.count("\n") == 1, "exactly one line")

    # terminal failure: nothing created
    p = run(["new", "nofail", "--no-worktree", "--repo", repo],
            {"SAVANNAH_OSASCRIPT": "/nonexistent/osascript"})
    check(p.returncode == 1 and not os.path.exists(os.path.join(workroot, "nofail")),
          "osascript missing: exit 1, no record, no claimed dir")

    # non-ENOENT OSError (EACCES: a fake with the x bit cleared): still exit 1, nothing left
    noexec = os.path.join(tmp, "noexec-osascript")
    shutil.copy(fake, noexec)
    os.chmod(noexec, stat.S_IRUSR | stat.S_IWUSR)
    p = run(["new", "noexec", "--no-worktree", "--repo", repo], {"SAVANNAH_OSASCRIPT": noexec})
    check(p.returncode == 1 and p.stderr.count("\n") == 1 and "osascript" in p.stderr,
          f"osascript not executable: exit 1, one stderr line ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(workroot, "noexec")), "osascript not executable: no record dir")

    # stale claim (dir without record.json) is reclaimed by new, reported by rm
    os.makedirs(os.path.join(workroot, "stale"))
    rec = run_json(["new", "stale", "--no-worktree", "--repo", repo, "--json"])
    check(rec.get("name") == "stale" and os.path.exists(os.path.join(workroot, "stale", "record.json")),
          "new reclaims a record-less directory")
    os.makedirs(os.path.join(workroot, "stale2"))
    p = run(["rm", "stale2"])
    check(p.returncode == 0 and "stale" in p.stdout and not os.path.exists(os.path.join(workroot, "stale2")),
          f"rm of a record-less directory deletes it and says stale ({p.stdout.strip()})")
    check(run(["rm", "stale"]).returncode == 0, "rm of the reclaimed session")

    # ---- worktree cases ------------------------------------------------------
    if have_git:
        clear_log()
        rec = run_json(["new", "feat", "--repo", repo, "--json"])
        wt = wt_of("feat")
        check(rec.get("worktree") == wt, "worktree path under <repo>/.savannah-worktrees/<name>")
        check(rec.get("repo") == os.path.abspath(repo), "repo keeps the caller's spelling when it is the root")
        check(rec.get("branch") == "work/feat", "branch work/<name>")
        check(os.path.isdir(wt) and os.path.exists(os.path.join(wt, "README")), "worktree dir exists")
        r = git(repo, "rev-parse", "--verify", "refs/heads/work/feat")
        check(r.returncode == 0, "branch work/feat exists")
        r = git(wt, "rev-parse", "--abbrev-ref", "HEAD")
        check(r.stdout.strip() == "work/feat", "worktree is on work/feat")
        check(f'write text "cd {wt} && claude"' in read_log(), "upper pane cds into the worktree")
        check(f'write text "cd {wt}"' in read_log(), "lower pane cds into the worktree")
        check(rec.get("tab_id") == "TAB-1" and rec.get("claude_pane") == "PANE-C-1",
              "fake counter restarted after clear")

        # --branch existing
        git(repo, "branch", "topic")
        rec = run_json(["new", "ontopic", "--repo", repo, "--branch", "topic", "--json"])
        wt2 = wt_of("ontopic")
        check(rec.get("branch") == "topic", "--branch recorded")
        r = git(wt2, "rev-parse", "--abbrev-ref", "HEAD")
        check(r.stdout.strip() == "topic", "worktree checked out existing branch")
        check(git(repo, "rev-parse", "--verify", "refs/heads/work/ontopic").returncode != 0,
              "--branch does not create work/<name>")

        # --branch nonexistent locally: refused before git touches anything
        p = run(["new", "ghost", "--repo", repo, "--branch", "no-such-branch"])
        check(p.returncode == 1, "missing branch: exit 1")
        check("no-such-branch" in p.stderr and "does not exist locally" in p.stderr,
              f"missing branch: names the branch ({p.stderr.strip()})")
        check(not os.path.exists(os.path.join(workroot, "ghost")), "missing branch: no record")
        check(not os.path.exists(wt_of("ghost")), "missing branch: no worktree dir")
        # a remote-tracking ref alone is not "local": no DWIM
        git(repo, "update-ref", "refs/remotes/origin/remote-only", "HEAD")
        p = run(["new", "ghost2", "--repo", repo, "--branch", "remote-only"])
        check(p.returncode == 1 and "does not exist locally" in p.stderr,
              "a remote-only branch is not local: refused, no DWIM")
        check(not os.path.exists(wt_of("ghost2")) and git(repo, "rev-parse", "--verify", "-q",
                                                            "refs/heads/remote-only").returncode != 0,
              "remote-only refusal created nothing")

        # terminal failure after worktree add: worktree and branch rolled back
        p = run(["new", "rollback", "--repo", repo], {"SAVANNAH_OSASCRIPT": "/nonexistent/osascript"})
        check(p.returncode == 1, "terminal failure: exit 1")
        check(not os.path.isdir(wt_of("rollback")), "terminal failure: worktree rolled back")
        check(git(repo, "rev-parse", "--verify", "refs/heads/work/rollback").returncode != 0,
              "terminal failure: branch rolled back")
        check(not os.path.exists(os.path.join(workroot, "rollback")), "terminal failure: claimed dir removed")
        # same with a non-ENOENT OSError
        p = run(["new", "rollback2", "--repo", repo], {"SAVANNAH_OSASCRIPT": noexec})
        check(p.returncode == 1 and not os.path.isdir(wt_of("rollback2"))
              and git(repo, "rev-parse", "--verify", "refs/heads/work/rollback2").returncode != 0
              and not os.path.exists(os.path.join(workroot, "rollback2")),
              "non-ENOENT osascript failure: worktree, branch and record dir rolled back")
        # rollback with --branch leaves the pre-existing branch alone
        p = run(["new", "rollback3", "--repo", repo, "--branch", "topic"], {"SAVANNAH_OSASCRIPT": noexec})
        check(p.returncode == 1 and not os.path.isdir(wt_of("rollback3"))
              and git(repo, "rev-parse", "--verify", "refs/heads/topic").returncode == 0,
              "rollback never deletes a --branch branch")

        # --repo default is cwd
        p = run(["new", "cwdrepo", "--json"], cwd=repo)
        check(p.returncode == 0, f"new from inside the repo exits 0 ({p.stderr.strip()})")
        rec = json.loads(p.stdout) if p.returncode == 0 else {}
        check(os.path.realpath(rec.get("repo", "")) == os.path.realpath(repo), "repo defaults to cwd")
        check(run(["rm", "cwdrepo"]).returncode == 0, "rm of cwd-default session")

        # --repo (or cwd) in a subdirectory resolves to the main worktree root
        sub = os.path.join(repo, "sub", "deeper")
        os.makedirs(sub)
        rec = run_json(["new", "fromsub", "--repo", sub, "--json"])
        check(os.path.realpath(rec.get("repo", "")) == os.path.realpath(repo),
              f"--repo subdirectory resolves to the repo root ({rec.get('repo')})")
        check(os.path.realpath(rec.get("worktree", "")) == os.path.realpath(wt_of("fromsub")),
              "worktree lands under the ROOT's .savannah-worktrees")
        check(not os.path.exists(os.path.join(sub, ".savannah-worktrees")), "no .savannah-worktrees in the subdir")
        rec = run_json(["new", "fromcwdsub", "--json"], cwd=sub)
        check(os.path.realpath(rec.get("repo", "")) == os.path.realpath(repo), "cwd subdirectory resolves too")
        check(run(["rm", "fromsub"]).returncode == 0 and run(["rm", "fromcwdsub"]).returncode == 0,
              "rm of subdir-created sessions")
        # --repo pointing INTO another session's worktree resolves to the main root (no nesting)
        rec = run_json(["new", "notnested", "--repo", wt, "--json"])
        check(os.path.realpath(rec.get("repo", "")) == os.path.realpath(repo),
              "--repo <another session's worktree> resolves to the main root")
        check(os.path.realpath(rec.get("worktree", "")) == os.path.realpath(wt_of("notnested")),
              "worktree is a sibling, not nested inside the other session's worktree")
        check(run(["rm", "notnested"]).returncode == 0, "rm notnested")
        # not a git repo at all
        plain = os.path.join(tmp, "plain")
        os.makedirs(plain)
        p = run(["new", "plainrepo", "--repo", plain])
        check(p.returncode == 1 and "not a git repository" in p.stderr
              and not os.path.exists(os.path.join(workroot, "plainrepo")),
              f"--repo that is not a git repo: exit 1, nothing left ({p.stderr.strip()})")

    # ---- ls join ------------------------------------------------------------
    target = wt_of("feat") if have_git else os.path.abspath(repo)
    joined_name = "feat" if have_git else "flat"
    # The joining session: project == worktree, iterm id matches claude_pane (after the colon).
    with open(os.path.join(sessions, "s1.json"), "w") as f:
        json.dump({"session_id": "s1", "project": target, "state": "waiting",
                   "message": "needs Bash", "updated": 100, "last_prompt": "fix the thing",
                   "transcript_path": "/t/s1.jsonl", "iterm_session_id": "w0t1p0:PANE-C-1"}, f)
    # A newer session in the same dir but a different pane: loses to the pane match.
    with open(os.path.join(sessions, "s2.json"), "w") as f:
        json.dump({"session_id": "s2", "project": target, "state": "done", "updated": 999,
                   "iterm_session_id": "w0t2p0:OTHER"}, f)
    # Unrelated session.
    with open(os.path.join(sessions, "s3.json"), "w") as f:
        json.dump({"session_id": "s3", "project": "/elsewhere", "state": "idle", "updated": 5}, f)
    # Garbage file is ignored.
    with open(os.path.join(sessions, "junk.json"), "w") as f:
        f.write("{not json")

    entries = run_json(["ls", "--json"])
    check(isinstance(entries, list), "ls --json is an array")
    by = {e["name"]: e for e in entries}
    extra = {"claude_session_id", "state", "message", "last_prompt", "last_note", "notes_path", "transcript_path"}
    for e in entries:
        check(extra <= set(e), f"ls entry {e['name']} carries join fields")
    e = by.get(joined_name, {})
    check(e.get("claude_session_id") == "s1", "pane match wins over newer session")
    check(e.get("state") == "waiting", "state comes through")
    check(e.get("message") == "needs Bash", "message comes through")
    check(e.get("last_prompt") == "fix the thing", "last_prompt comes through")
    check(e.get("transcript_path") == "/t/s1.jsonl", "transcript_path comes through")
    check(e.get("notes_path") == os.path.join(workroot, joined_name, "notes.md"), "notes_path")
    other = os.path.join(tmp, "other")
    os.makedirs(other, exist_ok=True)
    run_json(["new", "lonely", "--no-worktree", "--repo", other, "--json"])
    entries = run_json(["ls", "--json"])
    by = {e["name"]: e for e in entries}
    off = by.get("lonely", {})
    check(off.get("state") == "offline" and off.get("claude_session_id") == "", "no session: offline")
    check(off.get("last_prompt") == "" and off.get("message") == "", "offline: join fields are empty strings")
    if have_git:
        # 'flat' and 'bare' share project==repo with no hook: both offline too.
        check(by["flat"]["state"] == "offline", "repo-rooted session with no hook is offline")
    # Remove the pane match: most recently updated wins.
    os.remove(os.path.join(sessions, "s1.json"))
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check(by[joined_name]["claude_session_id"] == "s2", "without pane match, newest updated wins")
    check(by[joined_name]["last_prompt"] == "" and by[joined_name]["transcript_path"] == "",
          "missing hook fields come through as empty strings")
    os.remove(os.path.join(sessions, "s2.json"))

    # ls hygiene: name comes from the directory, not the file; junk dirs skipped
    write_record("evil", {"name": "../x", "worktree": other, "repo": other})
    write_record("noworktree", {"name": "noworktree", "worktree": "", "repo": other})
    os.makedirs(os.path.join(workroot, "Bad Name"))
    with open(os.path.join(workroot, "Bad Name", "record.json"), "w") as f:
        json.dump({"name": "Bad Name", "worktree": other}, f)
    with open(os.path.join(sessions, "cwd.json"), "w") as f:
        json.dump({"session_id": "cwd", "project": tmp, "state": "idle", "updated": 7}, f)
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check("evil" in by and "../x" not in by, "ls reports the directory name, not the record's name field")
    check("noworktree" not in by, "ls skips a record with an empty worktree")
    check("Bad Name" not in by, "ls skips a directory whose name is invalid")
    check(all(e["claude_session_id"] != "cwd" for e in by.values()),
          "a hook whose project is the cwd never joins an empty-worktree record")
    os.remove(os.path.join(sessions, "cwd.json"))
    check(run(["notes", "noworktree"]).returncode == 0, "notes still works on an unlisted record")

    # human table
    p = run(["ls"])
    check(p.returncode == 0 and p.stdout.startswith("NAME  "), "ls human header")
    check("LAST PROMPT" in p.stdout.splitlines()[0] and "LAST NOTE" in p.stdout.splitlines()[0],
          "ls human columns")
    check(any(ln.startswith("flat") and "first note" in ln for ln in p.stdout.splitlines()),
          "ls human shows last note")
    check(by["flat"]["last_note"] == "first note", "last_note is the latest note's first line")

    # empty root
    p = run(["ls"], {"SAVANNAH_WORK_DIR": os.path.join(tmp, "empty")})
    check(p.returncode == 0, "ls on empty root exits 0")
    check(run_json(["ls", "--json"], {"SAVANNAH_WORK_DIR": os.path.join(tmp, "empty")}) == [],
          "ls --json on empty root is []")

    # ---- rm safety: the record's name field can never aim rm at the work dir ----
    before = set(os.listdir(workroot))
    p = run(["rm", "evil"])
    check(p.returncode == 0 and "evil" in p.stdout, f"rm of a lying record exits 0 ({p.stderr.strip()})")
    check(os.path.isdir(workroot) and not os.path.exists(os.path.join(workroot, "evil"))
          and set(os.listdir(workroot)) == before - {"evil"},
          "rm of a record with name '../x' removes only its own directory")
    check(run(["rm", "noworktree"]).returncode == 0 and not os.path.exists(os.path.join(workroot, "noworktree")),
          "rm of an empty-worktree record removes just the record")
    shutil.rmtree(os.path.join(workroot, "Bad Name"))
    # a symlink planted in the work dir is refused, and its target untouched
    victim = os.path.join(tmp, "victim")
    os.makedirs(victim)
    with open(os.path.join(victim, "record.json"), "w") as f:
        json.dump({"name": "link", "worktree": other, "repo": other}, f)
    with open(os.path.join(victim, "keep"), "w") as f:
        f.write("x")
    os.symlink(victim, os.path.join(workroot, "link"))
    p = run(["rm", "link"])
    check(p.returncode == 1 and "refusing" in p.stderr, f"rm of a symlinked record dir is refused ({p.stderr.strip()})")
    check(os.path.exists(os.path.join(victim, "keep")), "symlink target untouched")
    os.remove(os.path.join(workroot, "link"))

    # ---- note / notes --------------------------------------------------------
    p = run(["note", "flat", "second", "note here"])
    check(p.returncode == 0, "note exits 0")
    text = run(["notes", "flat"]).stdout
    blocks = re.findall(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) local\n(.*?)\n\n", text, re.S | re.M)
    check(len(blocks) == 2, f"two note blocks ({len(blocks)})")
    check(blocks[1][1] == "second note here", "note text args joined with spaces")
    check(text.endswith("\n\n"), "notes end with a blank line")
    p = run(["note", "flat"], stdin="line one\nline two\n")
    check(p.returncode == 0, "stdin note exits 0")
    text = run(["notes", "flat"]).stdout
    check(text.endswith(" local\nline one\nline two\n\n"), "multi-line stdin note appended verbatim")
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check(by["flat"]["last_note"] == "line one", "last_note is first line of the newest note")
    check(by["flat"]["updated"] >= by["flat"]["created"], "note bumps updated")
    p = run(["note", "flat"], stdin="")
    check(p.returncode == 2, "note with no text exits 2")
    j = run_json(["note", "--json", "flat", "json note"])  # --json before the name
    check(j.get("name") == "flat" and j.get("text") == "json note" and j.get("notes_path"), "note --json shape")
    check(run(["notes", "bare"]).stdout == "", "notes of a session with no notes is empty")

    # ---- say / sh / jump ------------------------------------------------------
    clear_log()
    p = run(["say", "flat", "please", "run", "the", "tests"])
    check(p.returncode == 0, "say exits 0")
    sent = read_log()
    check('if id of s is "PANE-C-1"' in sent and 'write text "please run the tests"' in sent,
          "say writes into the claude pane")
    check(sent.lstrip().startswith("---- osascript ----\n" + guard + "\n"),
          "say script begins with the iTerm2-running guard (never launches iTerm2)")
    clear_log()
    p = run(["sh", "flat", "git", "status"])
    sent = read_log()
    check(p.returncode == 0 and 'if id of s is "PANE-S-1"' in sent
          and 'write text "git status"' in sent, "sh writes into the shell pane")
    check(guard in sent, "sh script carries the running guard")
    clear_log()
    p = run(["jump", "flat"])
    sent = read_log()
    check(p.returncode == 0 and 'if id of s is "PANE-C-1"' in sent and "select s" in sent
          and "if found then activate" in sent, "jump focuses the claude pane like the dash")
    check(sent.lstrip().startswith("---- osascript ----\n" + guard + "\n"), "jump script begins with the running guard")
    clear_log()
    p = run(["jump", "flat", "--shell"])
    check(p.returncode == 0 and 'if id of s is "PANE-S-1"' in read_log(), "jump --shell focuses the shell pane")

    # empty text never reaches a pane; leading-dash text goes after --
    for args in (["say", "flat", ""], ["say", "flat", " ", "  "], ["sh", "flat", ""], ["sh", "flat", "\t"]):
        clear_log()
        p = run(args)
        check(p.returncode == 2 and p.stderr.count("\n") == 1 and p.stdout == "",
              f"{args[0]} with blank text exits 2 with one stderr line ({p.stderr.strip()!r})")
        check(read_log() == "", f"{args[0]} with blank text never runs osascript")
    clear_log()
    p = run(["say", "flat", "--", "--continue"])
    check(p.returncode == 0 and 'write text "--continue"' in read_log(), "say x -- --continue types the text")
    clear_log()
    p = run(["sh", "flat", "--", "-la"])
    check(p.returncode == 0 and 'write text "-la"' in read_log(), "sh x -- -la types the text")
    check(run(["say", "flat", "--continue"]).returncode == 2, "say with a bare leading-dash word is a usage error")

    missing = {"FAKE_OSASCRIPT_MISSING": "PANE-C-1,PANE-S-1"}
    check(run(["say", "flat", "x"], missing).returncode == 1, "say to a missing pane exits 1")
    check(run(["sh", "flat", "x"], missing).returncode == 1, "sh to a missing pane exits 1")
    check(run(["jump", "flat"], missing).returncode == 1, "jump to a missing pane exits 1")
    check(run(["jump", "flat", "--shell"], missing).returncode == 1, "jump --shell to a missing pane exits 1")
    check(run(["say", "flat", "x"], {"FAKE_OSASCRIPT_MISSING": "PANE-S-1"}).returncode == 0,
          "say still works when only the shell pane is gone")

    # ---- rm ----------------------------------------------------------------
    if have_git:
        wt = wt_of("feat")
        with open(os.path.join(wt, "dirty.txt"), "w") as f:
            f.write("uncommitted\n")
        run(["note", "feat", "keep me"])
        created = record("feat")["created"]
        adir = os.path.join(workroot, "archive")
        clear_log()
        p = run(["rm", "feat"])
        check(p.returncode == 1, "dirty worktree refused without --force")
        check("--force" in p.stderr, "dirty refusal hints at --force")
        check(os.path.isdir(wt) and os.path.exists(os.path.join(workroot, "feat", "record.json")),
              "refused rm leaves worktree and record in place")
        check(os.path.exists(os.path.join(workroot, "feat", "notes.md")), "refused rm leaves the notes in place")
        check(not os.path.isdir(adir) or not any(f.startswith("feat-") for f in os.listdir(adir)),
              "refused rm leaves no archive copy behind")
        check(read_log() == "", "refused rm does not touch the tab")
        # archive collision: a file already sits at archive/<name>-<created>.md
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, f"feat-{created}.md"), "w") as f:
            f.write("older archive, do not overwrite\n")
        p = run(["rm", "feat", "--force"])
        check(p.returncode == 0, "rm --force exits 0")
        check(not os.path.exists(wt), "--force removes the dirty worktree")
        check(not os.path.exists(os.path.join(workroot, "feat")), "record dir deleted")
        with open(os.path.join(adir, f"feat-{created}.md")) as f:
            check(f.read() == "older archive, do not overwrite\n", "existing archive file never overwritten")
        archived = os.path.join(adir, f"feat-{created}-2.md")
        check(os.path.exists(archived), "collision: notes archived as archive/<name>-<created>-2.md")
        with open(archived) as f:
            check("keep me" in f.read(), "archived notes keep their text")
        check(git(repo, "rev-parse", "--verify", "refs/heads/work/feat").returncode == 0,
              "rm leaves the branch alone")
        check("close target" in read_log() and '"PANE-C-1"' in read_log(), "rm closes the tab by its panes")
        check(guard in read_log(), "close script carries the running guard")
        check(git(repo, "worktree", "list").stdout.count("\n") >= 1, "worktree list still sane")

        # new after rm of the same name: the branch survived, so new reattaches it
        git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q",
            "--allow-empty", "-m", "on work/feat", "--no-verify")  # main moves on
        p = run(["new", "feat", "--repo", repo])
        check(p.returncode == 0, f"new after rm exits 0 ({p.stderr.strip()})")
        check("reattached" in (p.stdout + p.stderr) and p.stdout.count("\n") == 1,
              f"new after rm says it reattached, one stdout line ({p.stdout.strip()})")
        check(record("feat")["branch"] == "work/feat", "reattached session records work/<name>")
        check(git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "work/feat",
              "reattached worktree is on work/<name>")
        check(git(wt, "rev-parse", "HEAD").stdout.strip() == git(repo, "rev-parse", "work/feat").stdout.strip()
              != git(repo, "rev-parse", "main").stdout.strip(),
              "reattached worktree sits at the branch's own tip, not a fresh branch off HEAD")
        p = run(["new", "feat2", "--repo", repo, "--json"])
        check(p.returncode == 0 and "reattached" not in (p.stdout + p.stderr), "a fresh name is not 'reattached'")
        check(run(["rm", "feat2"]).returncode == 0, "rm feat2")
        # a failure after reattaching must NOT delete the surviving branch
        check(run(["rm", "feat"]).returncode == 0, "rm feat again")
        p = run(["new", "feat", "--repo", repo], {"SAVANNAH_OSASCRIPT": noexec})
        check(p.returncode == 1 and not os.path.isdir(wt)
              and git(repo, "rev-parse", "--verify", "refs/heads/work/feat").returncode == 0,
              "failed reattach rolls back the worktree but keeps the pre-existing branch")
        # a second archive of the same name in the same second gets -3 (the -2 exists)
        run_json(["new", "feat", "--repo", repo, "--json"])
        run(["note", "feat", "third life"])
        c3 = record("feat")["created"]
        for suffix in ("", "-2"):
            path = os.path.join(adir, f"feat-{c3}{suffix}.md")
            if not os.path.exists(path):
                with open(path, "w") as f:
                    f.write("taken\n")
        check(run(["rm", "feat"]).returncode == 0, "rm feat (third)")
        check(os.path.exists(os.path.join(adir, f"feat-{c3}-3.md")), "archive suffix counts past -2")

        # --keep-worktree
        p = run(["rm", "ontopic", "--keep-worktree"])
        check(p.returncode == 0, "rm --keep-worktree exits 0")
        check(os.path.isdir(wt_of("ontopic")), "--keep-worktree leaves the worktree")
        check(not os.path.exists(os.path.join(workroot, "ontopic")), "--keep-worktree still forgets the record")

        # clean worktree removes without --force; worktree dir removed by hand prunes fine
        run_json(["new", "clean", "--repo", repo, "--json"])
        check(run(["rm", "clean"]).returncode == 0, "clean worktree removed without --force")
        run_json(["new", "gone", "--repo", repo, "--json"])
        shutil.rmtree(wt_of("gone"))
        check(run(["rm", "gone"]).returncode == 0, "hand-deleted worktree dir: rm still succeeds")
        check("gone" not in git(repo, "worktree", "list").stdout, "hand-deleted worktree pruned")

        # nested: a --no-worktree session living inside another session's worktree
        run_json(["new", "outer", "--repo", repo, "--json"])
        run_json(["new", "inner", "--no-worktree", "--repo", wt_of("outer"), "--json"])
        p = run(["rm", "outer"])
        check(p.returncode == 1 and "inner" in p.stderr, f"rm refuses a worktree that hosts another session ({p.stderr.strip()})")
        check(os.path.isdir(wt_of("outer")) and os.path.exists(os.path.join(workroot, "outer", "record.json")),
              "nested refusal leaves the outer worktree and record")
        check(run(["rm", "outer", "--force"]).returncode == 1, "--force does not override the nested refusal")
        check(run(["rm", "inner"]).returncode == 0, "rm inner first")
        check(run(["rm", "outer"]).returncode == 0 and not os.path.isdir(wt_of("outer")), "then rm outer")

    # rm of a --no-worktree session never touches git; tab close is best effort
    hrec = record("human")
    clear_log()
    p = run(["rm", "human"], {"FAKE_OSASCRIPT_MISSING": f"{hrec['claude_pane']},{hrec['shell_pane']}"})
    check(p.returncode == 0 and not os.path.exists(os.path.join(workroot, "human")),
          "rm with a vanished tab still succeeds")
    sent = read_log()
    check("close target" in sent and f'"{hrec["claude_pane"]}"' in sent and f'"{hrec["shell_pane"]}"' in sent,
          "rm with a vanished tab still emitted the close script for its own panes")
    adir = os.path.join(workroot, "archive")
    check(not os.path.isdir(adir) or not any(f.startswith("human-") for f in os.listdir(adir)),
          "no notes, nothing archived")
    check(run(["rm", "human"]).returncode == 1, "rm twice: unknown name exits 1")
    check(run(["rm", "bare"], {"SAVANNAH_OSASCRIPT": "/nonexistent/osascript"}).returncode == 0,
          "rm succeeds even when osascript is unavailable")
    check(run(["rm", "lonely"], {"SAVANNAH_OSASCRIPT": noexec}).returncode == 0,
          "rm succeeds even when osascript is not executable")

    # Reserved names: 'archive' is the notes graveyard; new/rm must never
    # treat it as a session (rm archive would otherwise delete every archived note).
    adir = os.path.join(workroot, "archive")
    os.makedirs(adir, exist_ok=True)
    with open(os.path.join(adir, "keep-1.md"), "w") as f:
        f.write("precious\n")
    p = run(["new", "archive", "--no-worktree", "--repo", repo])
    check(p.returncode == 1 and "invalid name" in p.stderr, "new archive: reserved name exits 1")
    p = run(["rm", "archive"])
    check(p.returncode == 1 and os.path.exists(os.path.join(adir, "keep-1.md")),
          "rm archive: refused, archived notes untouched")

    # A corrupt record.json is stale: ls skips it, rm deletes the directory.
    cdir = os.path.join(workroot, "corrupt")
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, "record.json"), "w") as f:
        f.write("{not json")
    check("corrupt" not in [e["name"] for e in run_json(["ls", "--json"])], "ls skips a corrupt record")
    p = run(["rm", "corrupt"])
    check(p.returncode == 0 and "stale" in p.stdout and not os.path.exists(cdir),
          "rm of a corrupt record removes the directory and says stale")
    with open(os.path.join(workroot, "notobj"), "w") as f:
        f.write("x")  # a plain file where a dir should be: left alone
    check(run(["rm", "notobj"]).returncode == 1 and os.path.exists(os.path.join(workroot, "notobj")),
          "rm of a plain file in the work dir is refused")

    # Rollback closes the tab when a step AFTER create_split_tab fails
    # (in-process: make the record write fail; the fake osascript logs close).
    import types
    clear_log()
    real_write = sw.atomic_write

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    # In-process, so the module reads os.environ: point it at the temp dirs
    # and the fake osascript for the duration (never the real iTerm2).
    saved_env = {k: os.environ.get(k) for k in
                 ("SAVANNAH_WORK_DIR", "SAVANNAH_SESSIONS_DIR", "SAVANNAH_OSASCRIPT", "FAKE_OSASCRIPT_LOG")}
    for k in saved_env:
        os.environ[k] = env[k]
    sw.atomic_write = boom
    try:
        sw.cmd_new(types.SimpleNamespace(name="nospace", repo=repo, branch=None,
                                         no_worktree=True, prompt="", note="", json=False))
        check(False, "cmd_new re-raises the record write failure")
    except OSError:
        pass
    finally:
        sw.atomic_write = real_write
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    sent = read_log()
    check("close target" in sent and "PANE-C-" in sent,
          "rollback after a failed record write closes the tab it just created")
    check(not os.path.exists(os.path.join(workroot, "nospace")), "rollback removed the claimed dir")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures == 0:
        print("test_work: all passed" + ("" if have_git else " (worktree cases skipped)"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
