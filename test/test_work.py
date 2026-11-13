#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Work-session test: drives work/savannah_work.py the way a human (or the
# shim) does, against a temp SAVANNAH_WORK_DIR, a temp SAVANNAH_SESSIONS_DIR
# with hand-written hook files, a throwaway git repo, and the fake it2
# (tools/fake_it2.py, standing in for iTerm2's own command) via SAVANNAH_IT2.
# No real iTerm2, no real claude, no network, stdlib only. git is isolated from the user's config
# (HOME=tmp, GIT_CONFIG_GLOBAL=/dev/null, GIT_CONFIG_NOSYSTEM=1, identity via
# GIT_AUTHOR_*/GIT_COMMITTER_*). Worktree cases are skipped (with a message,
# still exit 0) ONLY when git is not on PATH; a git that is present but fails
# to init/commit is a test FAILURE. Usage:
#   test_work.py <path/to/savannah_work.py> <path/to/fake_it2.py> [savannah CLI]
# The optional third argument is the built C++ CLI; when given, its `hub`,
# `work`, `mesh` and bare-invocation entry points are exercised too (they
# exec the module, or the dash; SAVANNAH_WORK_PY / SAVANNAH_DASH_BIN point
# them at the module under test and a fake dash).

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
import time

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
    log = os.path.join(tmp, "it2.log")
    os.makedirs(sessions)

    env = dict(os.environ)
    env["SAVANNAH_WORK_DIR"] = workroot
    env["SAVANNAH_SESSIONS_DIR"] = sessions
    env["SAVANNAH_IT2"] = f"{shlex.quote(sys.executable)} {shlex.quote(fake)}"
    env["SAVANNAH_HUB_FILE"] = os.path.join(tmp, "hub.json")
    logdir = os.path.join(tmp, "log")          # savannah's one log, never the user's
    env["SAVANNAH_LOG_DIR"] = logdir
    os.environ["SAVANNAH_LOG_DIR"] = logdir    # the in-process module checks below log too
    env.pop("SAVANNAH_WORK_PY", None)
    env.pop("SAVANNAH_DASH_BIN", None)
    env.pop("SAVANNAH_CLAUDE_BIN", None)
    env.pop("SAVANNAH_BIN", None)
    env["FAKE_IT2_LOG"] = log
    for k in ("FAKE_IT2_MISSING", "FAKE_IT2_FAIL", "FAKE_IT2_NO_WINDOW"):
        env.pop(k, None)
    # The module names the window of the session it runs in; the fake never
    # lists this one, so it falls through to the fake's own window.
    env["ITERM_SESSION_ID"] = "w9t9p9:NOT-IN-FAKE"
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

    def calls():
        """Every it2 argv the fake saw since clear_log, in order."""
        return [json.loads(ln) for ln in read_log().splitlines() if ln.strip()]

    def called(prefix, last=None):
        """Index of the first call starting with prefix (and ending with last
        when given), or -1."""
        for i, c in enumerate(calls()):
            if c[:len(prefix)] == list(prefix) and (last is None or c[-1] == last):
                return i
        return -1

    def clear_log(keep_state=False):
        # The state file is the fake's world (its counter and live panes):
        # clearing it restarts ids at TAB-1 / PANE-C-1 / PANE-S-1. keep_state
        # drops only the call log, so the next checks see one command's calls
        # while the panes made so far stay alive.
        for p in ((log,) if keep_state else (log, log + ".state")):
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

    # timeout message names both causes; patched subprocess so nothing hangs
    real_run = sw.subprocess.run

    def fake_timeout(*a, **k):
        raise sw.subprocess.TimeoutExpired(cmd=a[0], timeout=60)
    sw.subprocess.run = fake_timeout
    try:
        sw.ITerm().write("P", "x")
        check(False, "timeout raises UserError")
    except sw.UserError as e:
        check("Python API" in str(e) and "iTerm2" in str(e),
              f"timeout message names iTerm2 and its Python API switch ({e})")
    finally:
        sw.subprocess.run = real_run
    os.environ["SAVANNAH_IT2"] = "/nonexistent/it2 --flag"
    check(sw.it2_argv() == ["/nonexistent/it2", "--flag"], "it2_argv splits SAVANNAH_IT2 like a shell line")
    os.environ["SAVANNAH_IT2"] = "   "
    try:
        sw.it2_argv()
        check(False, "empty SAVANNAH_IT2 raises")
    except sw.UserError:
        pass
    os.environ.pop("SAVANNAH_IT2", None)
    check(sw.it2_argv()[0].endswith("it2"), "it2_argv without the override names an it2")
    # The claude launch line: cd, claude named after the session, prompt quoted.
    check(sw.claude_command("/w t", "nm", 'say "hi"') == "cd '/w t' && claude -n nm 'say \"hi\"'",
          "claude_command quotes the worktree and the prompt")
    check(sw.claude_command("/w", "nm", "", resume=True) == "cd /w && claude -n nm --continue",
          "claude_command resumes with --continue and no prompt")
    os.environ["SAVANNAH_CLAUDE_BIN"] = "echo claude"
    check(sw.claude_command("/w", "nm", "") == "cd /w && echo claude -n nm",
          "SAVANNAH_CLAUDE_BIN replaces the claude command as a shell fragment")
    os.environ.pop("SAVANNAH_CLAUDE_BIN", None)

    # ---- bad usage -> 2 ----------------------------------------------------
    check(run([]).returncode == 2, "no verb exits 2")
    check(run(["bogus"]).returncode == 2, "unknown verb exits 2")
    check(run(["new"]).returncode == 2, "new without name exits 2")
    check(run(["say", "x"]).returncode == 2, "say without text exits 2")
    check(run(["rename", "x"]).returncode == 2, "rename without a new name exits 2")
    check(run(["status"]).returncode == 2, "status without a name or --here exits 2")
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
                 ["sh", "nope", "x"], ["jump", "nope"], ["rm", "nope"],
                 ["move", "nope", "--later"], ["rename", "nope", "nope2"], ["status", "nope"],
                 ["status", "nope", "x"], ["status", "nope", "--clear"]):
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
    check(rec.get("tab_id") == "TAB-1", "tab id from fake it2")
    check(rec.get("claude_pane") == "PANE-C-1" and rec.get("shell_pane") == "PANE-S-1",
          "pane ids from fake it2")
    check(rec.get("prompt") == 'say hi "there"', "prompt recorded")
    check(set(rec) == {"name", "repo", "worktree", "branch", "created", "updated", "terminal",
                       "tab_id", "claude_pane", "shell_pane", "prompt", "adopted", "priority", "rank",
                       "title"},
          "record has exactly the contract fields")
    check(rec.get("priority") == "active" and rec.get("rank") == rec.get("created"),
          "a new session is active, ranked by its creation time")
    check(rec.get("adopted") is False, "a session made by new is not adopted")
    check(isinstance(rec.get("created"), int) and rec.get("created") > 0, "created is epoch int")
    check(record("flat") == rec, "record.json on disk matches --json output")
    with open(os.path.join(workroot, "flat", "notes.md")) as f:
        check(f.read().endswith(" local\nfirst note\n\n"), "--note appended to notes.md")

    # The exact it2 calls: a tab in a named window (it2 has no "current
    # window" from a script, found live 11/3/2026), the session named after
    # the work session, cd + claude in the upper pane, a split, cd below.
    rq = os.path.abspath(repo)
    i_tab = called(["tab", "new", "--window", "W1"])
    i_name = called(["session", "set-name", "--session", "PANE-C-1", "--"], last="flat")
    # Typed lines start with a space: a shell that reads a key while starting
    # (oh-my-zsh's update prompt, live 11/3/2026) eats the first character.
    i_up = called(["session", "run", "--session", "PANE-C-1", "--"],
                  last=f" cd {rq} && claude -n flat 'say hi \"there\"'")
    i_split = called(["session", "split", "--session", "PANE-C-1"])
    i_down = called(["session", "run", "--session", "PANE-S-1", "--"], last=f" cd {rq}")
    check(i_tab >= 0, f"tab created in the fake's window ({calls()})")
    check(i_name >= 0, "session named after the work session (after --, so a dashed name is a name)")
    check(i_up >= 0, f"upper pane gets ' cd <worktree> && claude -n <name> <shlex-quoted prompt>' ({calls()})")
    check(i_split >= 0, "upper pane split (horizontal divider: shell below)")
    check(i_down >= 0, "lower pane gets ' cd <worktree>'")
    check(-1 < i_tab < i_up < i_name < i_split < i_down,
          "calls happen in that order (the name after the run, or the shell's first prompt resets it)")
    check(called(["tab", "close"]) < 0, "new never calls `it2 tab close`")
    check(calls()[0] == ["session", "list", "--json"], "new asks for the session list first (to pick the window)")

    # new without --prompt: bare claude, still named.
    clear_log()
    run_json(["new", "bare", "--no-worktree", "--repo", repo, "--json"])
    check(called(["session", "run", "--session", "PANE-C-1", "--"], last=f" cd {rq} && claude -n bare") >= 0,
          "no prompt: cd && claude -n <name>")

    # No window at all (iTerm2 just started, or every window closed): the new
    # window's own tab is the work tab, no stray empty tab.
    clear_log()
    rec = run_json(["new", "nowin", "--no-worktree", "--repo", repo, "--json"], {"FAKE_IT2_NO_WINDOW": "1"})
    check(called(["window", "new"]) >= 0 and called(["tab", "new"]) < 0,
          "no window: `window new`, never `tab new`")
    check(rec.get("tab_id") == "TAB-1" and rec.get("claude_pane") == "PANE-C-1"
          and rec.get("shell_pane") == "PANE-S-1", f"no window: the window's own tab is the work tab ({rec})")
    check(run(["rm", "nowin"]).returncode == 0, "rm nowin")

    # A failure after the tab exists closes that tab before the error is reported.
    clear_log()
    p = run(["new", "halfway", "--no-worktree", "--repo", repo], {"FAKE_IT2_FAIL": "session split"})
    check(p.returncode == 1 and "split" in p.stderr and "boom" in p.stderr,
          f"it2 failing mid-create: exit 1 naming the step ({p.stderr.strip()})")
    check(called(["session", "close", "--session", "PANE-C-1", "--force"]) >= 0,
          "it2 failing mid-create: the half-made tab is closed")
    check(not os.path.exists(os.path.join(workroot, "halfway")), "it2 failing mid-create: no record")

    # duplicate refused
    p = run(["new", "flat", "--no-worktree", "--repo", repo])
    check(p.returncode == 1 and "exists" in p.stderr, "duplicate name refused with exit 1")

    # human line
    p = run(["new", "human", "--no-worktree", "--repo", repo])
    check(p.returncode == 0 and p.stdout.startswith("human: "), "new prints one human line")
    check(p.stdout.count("\n") == 1, "exactly one line")

    # terminal failure: nothing created
    p = run(["new", "nofail", "--no-worktree", "--repo", repo],
            {"SAVANNAH_IT2": "/nonexistent/it2"})
    check(p.returncode == 1 and not os.path.exists(os.path.join(workroot, "nofail")),
          "it2 missing: exit 1, no record, no claimed dir")
    check("it2" in p.stderr and "SAVANNAH_IT2" in p.stderr, f"it2 missing: the message says what to set ({p.stderr.strip()})")

    # non-ENOENT OSError (EACCES: a fake with the x bit cleared): still exit 1, nothing left
    noexec = os.path.join(tmp, "noexec-it2")
    shutil.copy(fake, noexec)
    os.chmod(noexec, stat.S_IRUSR | stat.S_IWUSR)
    p = run(["new", "noexec", "--no-worktree", "--repo", repo], {"SAVANNAH_IT2": noexec})
    check(p.returncode == 1 and p.stderr.count("\n") == 1 and "it2" in p.stderr,
          f"it2 not executable: exit 1, one stderr line ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(workroot, "noexec")), "it2 not executable: no record dir")

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
        check(called(["session", "run", "--session", "PANE-C-1", "--"], last=f" cd {wt} && claude -n feat") >= 0,
              "upper pane cds into the worktree")
        check(called(["session", "run", "--session", "PANE-S-1", "--"], last=f" cd {wt}") >= 0,
              "lower pane cds into the worktree")
        check(rec.get("tab_id") == "TAB-1" and rec.get("claude_pane") == "PANE-C-1",
              "fake counter restarted after clear")

        # .worktreeinclude: the gitignored files a worktree needs to run are
        # copied in (paths and globs, comments skipped, existing files kept).
        with open(os.path.join(repo, ".gitignore"), "w") as f:
            f.write(".env\nconf/*.local\nsecrets/\n")
        with open(os.path.join(repo, ".worktreeinclude"), "w") as f:
            f.write("# what a worktree needs\n\n.env\nconf/*.local\nsecrets\nnot-there.txt\n/README\n")
        with open(os.path.join(repo, ".env"), "w") as f:
            f.write("KEY=1\n")
        os.makedirs(os.path.join(repo, "conf"))
        for n in ("a.local", "b.local", "c.other"):
            with open(os.path.join(repo, "conf", n), "w") as f:
                f.write(n + "\n")
        os.makedirs(os.path.join(repo, "secrets", "deep"))
        with open(os.path.join(repo, "secrets", "deep", "k"), "w") as f:
            f.write("k\n")
        git(repo, "add", ".gitignore", ".worktreeinclude")
        git(repo, "commit", "-q", "-m", "include list")
        p = run(["new", "inc", "--repo", repo, "--json"])
        check(p.returncode == 0, f"new with .worktreeinclude exits 0 ({p.stderr.strip()})")
        wti = wt_of("inc")
        for rel in (".env", "conf/a.local", "conf/b.local", "secrets/deep/k"):
            check(os.path.isfile(os.path.join(wti, rel)), f".worktreeinclude copied {rel}")
        check(not os.path.exists(os.path.join(wti, "conf", "c.other")), ".worktreeinclude copies only what it names")
        with open(os.path.join(wti, "README")) as f:
            check(f.read() == "hi\n", "a tracked file the list names is left as git checked it out")
        check("copied 4" in p.stderr and ".env" in p.stderr and "not-there" not in p.stderr,
              f"new reports what .worktreeinclude copied ({p.stderr.strip()})")
        check(run(["rm", "inc"]).returncode == 0, "rm after .worktreeinclude: ignored copies keep the worktree clean")
        for rel in (".gitignore", ".worktreeinclude"):
            git(repo, "rm", "-q", rel)
        git(repo, "commit", "-q", "-m", "drop include list")
        shutil.rmtree(os.path.join(repo, "conf"))
        shutil.rmtree(os.path.join(repo, "secrets"))
        os.remove(os.path.join(repo, ".env"))

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
        p = run(["new", "rollback", "--repo", repo], {"SAVANNAH_IT2": "/nonexistent/it2"})
        check(p.returncode == 1, "terminal failure: exit 1")
        check(not os.path.isdir(wt_of("rollback")), "terminal failure: worktree rolled back")
        check(git(repo, "rev-parse", "--verify", "refs/heads/work/rollback").returncode != 0,
              "terminal failure: branch rolled back")
        check(not os.path.exists(os.path.join(workroot, "rollback")), "terminal failure: claimed dir removed")
        # same with a non-ENOENT OSError
        p = run(["new", "rollback2", "--repo", repo], {"SAVANNAH_IT2": noexec})
        check(p.returncode == 1 and not os.path.isdir(wt_of("rollback2"))
              and git(repo, "rev-parse", "--verify", "refs/heads/work/rollback2").returncode != 0
              and not os.path.exists(os.path.join(workroot, "rollback2")),
              "non-ENOENT it2 failure: worktree, branch and record dir rolled back")
        # a failure after the tab exists: tab closed, worktree and branch rolled back
        clear_log()
        p = run(["new", "rollback4", "--repo", repo], {"FAKE_IT2_FAIL": "session run"})
        check(p.returncode == 1 and not os.path.isdir(wt_of("rollback4"))
              and git(repo, "rev-parse", "--verify", "refs/heads/work/rollback4").returncode != 0
              and not os.path.exists(os.path.join(workroot, "rollback4")),
              "it2 failing after the tab exists: worktree, branch and record dir rolled back")
        check(called(["session", "close", "--session", "PANE-C-1", "--force"]) >= 0,
              "it2 failing after the tab exists: the tab is closed")
        # rollback with --branch leaves the pre-existing branch alone
        p = run(["new", "rollback3", "--repo", repo, "--branch", "topic"], {"SAVANNAH_IT2": noexec})
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

    # A session whose tab the fake still has (the resets above dropped the
    # older tabs from the fake's world, as closing them in iTerm2 would).
    alive_rec = run_json(["new", "alive", "--no-worktree", "--repo", repo, "--json"])
    before_ls = len(calls())
    entries = run_json(["ls", "--json"])
    check(isinstance(entries, list), "ls --json is an array")
    by = {e["name"]: e for e in entries}
    extra = {"claude_session_id", "state", "message", "last_prompt", "last_note", "notes_path",
             "transcript_path", "pane_alive"}
    for e in entries:
        check(extra <= set(e), f"ls entry {e['name']} carries join fields")
    # pane_alive: one it2 session list for the whole ls; null when it2 is unavailable
    alive = {e["name"]: e["pane_alive"] for e in entries}
    check(alive.get("alive") is True, f"ls pane_alive is true for a tab the fake still has ({alive})")
    check(all(isinstance(v, bool) for v in alive.values()), f"ls pane_alive is a bool on every row when it2 answers ({alive})")
    check(calls()[before_ls:] == [["session", "list", "--json"]],
          f"ls asks it2 for the session list exactly once ({calls()[before_ls:]})")
    e2 = {e["name"]: e for e in run_json(["ls", "--json"], {"FAKE_IT2_MISSING": alive_rec["claude_pane"]})}
    check(e2["alive"]["pane_alive"] is False, "ls pane_alive is false once the Claude pane is gone")
    e3 = {e["name"]: e for e in run_json(["ls", "--json"], {"SAVANNAH_IT2": "/nonexistent/it2"})}
    check(e3[joined_name]["pane_alive"] is None and e3[joined_name]["state"] == "waiting",
          "ls without it2 still joins, pane_alive null")
    check(run(["ls"], {"SAVANNAH_IT2": "/nonexistent/it2"}).returncode == 0, "human ls without it2 exits 0")
    check(run(["rm", "alive"]).returncode == 0, "rm alive")
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
    check(calls() == [["session", "send", "--session", "PANE-C-1", "--", "please run the tests"],
                      ["session", "send", "--session", "PANE-C-1", "--", "\r"]],
          f"say is `session send` of the text then of a carriage return (Enter), text after -- ({calls()})")
    clear_log()
    p = run(["sh", "flat", "git", "status"])
    check(p.returncode == 0 and calls() == [["session", "send", "--session", "PANE-S-1", "--", "git status"],
                                            ["session", "send", "--session", "PANE-S-1", "--", "\r"]],
          f"sh is `session send` of the command then of a carriage return into the shell pane ({calls()})")
    clear_log()
    p = run(["jump", "flat"])
    check(p.returncode == 0 and calls() == [["session", "focus", "PANE-C-1"], ["app", "activate"]],
          f"jump focuses the claude pane then brings iTerm2 forward ({calls()})")
    clear_log()
    p = run(["jump", "flat", "--shell"])
    check(p.returncode == 0 and calls()[0] == ["session", "focus", "PANE-S-1"], "jump --shell focuses the shell pane")

    # empty text never reaches a pane; leading-dash text goes after --
    for args in (["say", "flat", ""], ["say", "flat", " ", "  "], ["sh", "flat", ""], ["sh", "flat", "\t"]):
        clear_log()
        p = run(args)
        check(p.returncode == 2 and p.stderr.count("\n") == 1 and p.stdout == "",
              f"{args[0]} with blank text exits 2 with one stderr line ({p.stderr.strip()!r})")
        check(read_log() == "", f"{args[0]} with blank text never runs it2")
    clear_log()
    p = run(["say", "flat", "--", "--continue"])
    check(p.returncode == 0 and calls()[-2][-2:] == ["--", "--continue"] and calls()[-1][-1] == "\r",
          "say x -- --continue types the text, then Enter")
    clear_log()
    p = run(["sh", "flat", "--", "-la"])
    check(p.returncode == 0 and calls()[-2][-2:] == ["--", "-la"] and calls()[-1][-1] == "\r",
          "sh x -- -la types the text, then Enter")
    check(run(["say", "flat", "--continue"]).returncode == 2, "say with a bare leading-dash word is a usage error")

    missing = {"FAKE_IT2_MISSING": "PANE-C-1,PANE-S-1"}
    p = run(["say", "flat", "x"], missing)
    check(p.returncode == 1 and "no longer exists" in p.stderr, f"say to a missing pane exits 1 ({p.stderr.strip()})")
    check(run(["sh", "flat", "x"], missing).returncode == 1, "sh to a missing pane exits 1")
    check(run(["jump", "flat"], missing).returncode == 1, "jump to a missing pane exits 1")
    check(run(["jump", "flat", "--shell"], missing).returncode == 1, "jump --shell to a missing pane exits 1")
    check(run(["say", "flat", "x"], {"FAKE_IT2_MISSING": "PANE-S-1"}).returncode == 0,
          "say still works when only the shell pane is gone")
    # it2 broken (not a missing pane): say says so, exit 1
    p = run(["say", "flat", "x"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check(p.returncode == 1 and "it2" in p.stderr, "say without it2 exits 1 naming it2")

    # ---- open: focus the tab, or open it again when it is gone ---------------
    # The fake's world was reset above, so flat's recorded panes are gone (as
    # after an iTerm2 restart): open makes the tab again, ids restart at 1.
    clear_log()
    before_rec = record("flat")
    j = run_json(["open", "flat", "--json"])
    check(j.get("action") == "reopened" and j.get("name") == "flat", f"open on a gone tab reopens it ({j})")
    after_rec = record("flat")
    check(after_rec["tab_id"] == "TAB-1" and after_rec["claude_pane"] == "PANE-C-1" == j.get("claude_pane")
          and after_rec["shell_pane"] == "PANE-S-1" == j.get("shell_pane"),
          f"reopen records the new tab and panes ({after_rec})")
    check(called(["session", "run", "--session", "PANE-C-1", "--"], last=f" cd {rq} && claude -n flat") >= 0,
          "reopen with no transcript starts claude fresh (named, no prompt replay)")
    check(called(["session", "set-name", "--session", "PANE-C-1", "--"], last="flat") >= 0,
          "reopen names the new session")
    check(called(["tab", "close"]) < 0, "reopen never calls `it2 tab close`")
    check(after_rec["updated"] >= before_rec["updated"], "reopen bumps updated")
    # Now the tab is live: open only focuses it.
    clear_log(keep_state=True)
    p = run(["open", "flat"])
    check(p.returncode == 0 and p.stdout.startswith("flat: focused"), f"open on a live tab focuses it ({p.stdout.strip()})")
    check(calls() == [["session", "list", "--json"], ["session", "focus", "PANE-C-1"], ["app", "activate"]],
          f"open on a live tab: list, focus, activate ({calls()})")
    clear_log(keep_state=True)
    p = run(["open", "flat", "--shell"])
    check(p.returncode == 0 and called(["session", "focus", "PANE-S-1"]) >= 0, "open --shell focuses the shell pane")
    check(record("flat")["tab_id"] == "TAB-1", "open on a live tab leaves the record alone")
    # Both panes gone while the fake keeps counting: a new tab with new ids.
    clear_log(keep_state=True)
    j = run_json(["open", "flat", "--json"], {"FAKE_IT2_MISSING": "PANE-C-1,PANE-S-1"})
    rec_now = record("flat")
    check(j.get("action") == "reopened" and rec_now["tab_id"] == "TAB-2"
          and rec_now["claude_pane"] == j.get("claude_pane") == "PANE-C-2"
          and rec_now["shell_pane"] == j.get("shell_pane") == "PANE-S-2",
          f"open on a gone tab reopens it with new ids ({j})")
    # claude pane gone, shell pane still there: the leftover pane is closed first
    clear_log(keep_state=True)
    j = run_json(["open", "flat", "--json"], {"FAKE_IT2_MISSING": rec_now["claude_pane"]})
    check(j.get("action") == "reopened"
          and called(["session", "close", "--session", rec_now["shell_pane"], "--force"]) >= 0,
          "reopen closes a leftover shell pane so one task never has two tabs")
    rec_now = record("flat")
    # with a transcript for the worktree, claude resumes it
    with open(os.path.join(sessions, "flat-old.json"), "w") as f:
        json.dump({"session_id": "flat-old", "project": rq, "transcript_path": os.path.join(tmp, "flat.jsonl")}, f)
    with open(os.path.join(tmp, "flat.jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": "earlier"}}) + "\n")
    clear_log(keep_state=True)
    j = run_json(["open", "flat", "--json"], {"FAKE_IT2_MISSING": f"{rec_now['claude_pane']},{rec_now['shell_pane']}"})
    rec_now = record("flat")
    check(called(["session", "run", "--session", rec_now["claude_pane"], "--"],
                 last=f" cd {rq} && claude -n flat --continue") >= 0,
          f"reopen with a transcript resumes it with --continue ({calls()})")
    os.remove(os.path.join(sessions, "flat-old.json"))
    check(run(["open", "nosuch"]).returncode == 1, "open of an unknown name exits 1")
    p = run(["open", "flat"], {"SAVANNAH_IT2": "/nonexistent/it2"})
    check(p.returncode == 1 and "it2" in p.stderr, "open without it2 exits 1")
    gone_dir = os.path.join(tmp, "gone-wt")
    os.makedirs(gone_dir)
    run_json(["new", "gonewt", "--no-worktree", "--repo", gone_dir, "--json"])
    shutil.rmtree(gone_dir)
    p = run(["open", "gonewt"], {"FAKE_IT2_MISSING": "PANE-C-9,PANE-S-9," + ",".join(
        v for k, v in record("gonewt").items() if k in ("claude_pane", "shell_pane"))})
    check(p.returncode == 1 and "gone" in p.stderr, f"open with a vanished worktree and tab exits 1 ({p.stderr.strip()})")
    check(run(["rm", "gonewt"]).returncode == 0, "rm gonewt")

    # ---- adopt: a tab you opened yourself becomes a named work session -------
    # The hook file is the only input: it names the directory (the session's
    # worktree, as is) and the iTerm2 pane (the join key). No tab is made.
    hook_adopt = os.path.join(sessions, "adopt-1.json")
    with open(hook_adopt, "w") as f:
        json.dump({"session_id": "adopt-1", "project": rq, "state": "idle",
                   "iterm_session_id": "w0t0p0:ZZ-PANE", "updated": int(time.time()),
                   "last_prompt": "what was I doing", "transcript_path": ""}, f)
    clear_log(keep_state=True)
    j = run_json(["adopt", "mine", "--session=adopt-1", "--json"])
    head_branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() if have_git else ""
    check(j.get("adopted") is True and j.get("name") == "mine", f"adopt records adopted: true ({j})")
    check(j.get("worktree") == rq and j.get("repo") == rq,
          f"adopt: worktree and repo are the tab's own directory ({j})")
    check(j.get("branch") == head_branch,
          f"adopt records the checked-out branch ({j.get('branch')!r} vs {head_branch!r})")
    check(j.get("claude_pane") == "ZZ-PANE" and j.get("shell_pane") == "" and j.get("tab_id") == "",
          f"adopt: pane from the hook, no shell pane, no tab id when it2 does not list the pane ({j})")
    check(j.get("prompt") == "" and j.get("terminal") == "iterm", f"adopt: empty prompt, iterm terminal ({j})")
    check(os.path.exists(os.path.join(workroot, "mine", "record.json")), "adopt writes the record")
    check(called(["tab", "new"]) < 0 and called(["session", "split"]) < 0 and called(["session", "run"]) < 0,
          f"adopt opens nothing in iTerm2 ({calls()})")
    check(record("mine")["adopted"] is True, "adopted is stored as a boolean")
    entries = {e["name"]: e for e in run_json(["ls", "--json"])}
    mine = entries.get("mine", {})
    check(mine.get("state") == "idle" and mine.get("claude_session_id") == "adopt-1" and mine.get("adopted") is True,
          f"ls joins the adopted session to its hook file ({mine})")
    check(mine.get("last_prompt") == "what was I doing", "ls carries the adopted tab's last prompt")
    check(entries.get("flat", {}).get("adopted") is False, "a session made by new reads adopted: false")
    check(run(["note", "mine", "--", "first"]).returncode == 0, "note on an adopted session")
    check("first" in run(["notes", "mine"]).stdout, "notes of an adopted session")
    p = run(["say", "mine", "--", "hi"], {"FAKE_IT2_MISSING": "ZZ-PANE"})
    check(p.returncode == 1 and "no longer exists" in p.stderr,
          f"say to a gone adopted pane exits 1 ({p.stderr.strip()})")
    clear_log(keep_state=True)
    check(run(["say", "mine", "--", "hi"]).returncode == 0
          and called(["session", "send", "--session", "ZZ-PANE", "--"], last="hi") >= 0
          and called(["session", "send", "--session", "ZZ-PANE", "--"], last="\r") >= 0,
          f"say types into the adopted tab's pane ({calls()})")
    p = run(["sh", "mine", "--", "ls"])
    check(p.returncode == 1 and "shell pane" in p.stderr,
          f"sh on an adopted session (no shell pane) exits 1 ({p.stderr.strip()})")
    p = run(["adopt", "again", "--session=adopt-1"])
    check(p.returncode == 1 and "already work session mine" in p.stderr,
          f"adopting the same tab twice is refused ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(workroot, "again")), "a refused adopt leaves no record dir")
    p = run(["adopt", "Bad Name", "--session=adopt-1"])
    check(p.returncode == 1 and "invalid name" in p.stderr, "adopt validates the name")
    p = run(["adopt", "ghost", "--session=nope"])
    check(p.returncode == 1 and "no live Claude session nope" in p.stderr,
          f"adopt of an unknown session exits 1 ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(workroot, "ghost")), "a failed adopt leaves no record dir")
    check(run(["adopt", "noflag"]).returncode == 2, "adopt without --session exits 2")
    # rm: the directory is the owner's, so git never removes or prunes it;
    # the record goes, the notes are archived, the pane is closed best effort.
    wl_before = git(repo, "worktree", "list").stdout if have_git else ""
    clear_log(keep_state=True)
    p = run(["rm", "mine", "--force"])
    check(p.returncode == 0 and "adopted: directory kept" in p.stdout,
          f"rm of an adopted session keeps the directory, even with --force ({p.stdout.strip()} / {p.stderr.strip()})")
    check(os.path.isdir(rq) and os.path.exists(os.path.join(rq, "README")),
          "rm of an adopted session leaves the directory and its files")
    if have_git:
        check(git(repo, "worktree", "list").stdout == wl_before and rq in wl_before.split("\n")[0],
              "rm of an adopted session changes nothing in git worktree list")
    check(called(["session", "close", "--session", "ZZ-PANE", "--force"]) >= 0,
          f"rm closes the adopted pane best effort ({calls()})")
    check(called(["session", "close", "--session", "", "--force"]) < 0, "rm never closes an empty pane id")
    check(not os.path.exists(os.path.join(workroot, "mine")), "rm of an adopted session deletes the record")
    adir = os.path.join(workroot, "archive")
    check(os.path.isdir(adir) and any(f.startswith("mine-") for f in os.listdir(adir)),
          "rm archives an adopted session's notes")
    # open: the adopted pane is not in it2's list any more, so open makes a
    # tab and a split in that directory and the record stays adopted.
    run_json(["adopt", "back", "--session=adopt-1", "--json"])
    clear_log(keep_state=True)
    j = run_json(["open", "back", "--json"])
    rec_back = record("back")
    check(j.get("action") == "reopened" and rec_back["adopted"] is True and rec_back["worktree"] == rq,
          f"open on an adopted session whose pane is gone reopens it and keeps adopted ({j} / {rec_back})")
    check(rec_back["claude_pane"] == j.get("claude_pane") and rec_back["shell_pane"] == j.get("shell_pane") != "",
          f"a reopened adopted session has both panes ({rec_back})")
    check(called(["session", "run", "--session", rec_back["claude_pane"], "--"],
                 last=f" cd {rq} && claude -n back") >= 0,
          f"reopen of an adopted session starts a named claude in its directory, no --continue without a transcript ({calls()})")
    clear_log(keep_state=True)
    p = run(["open", "back"])
    check(p.returncode == 0 and p.stdout.startswith("back: focused"), "open on the reopened adopted session focuses it")
    check(run(["rm", "back"]).returncode == 0, "rm back")
    os.remove(hook_adopt)

    # ---- autoname: names for the tabs you opened yourself, from their transcripts ----
    # A fake claude stands in for the headless call: it records its argv and
    # environment and answers with a title; FAKE_CLAUDE_FAIL makes it exit 1
    # (the first prompt's first line is the title then). Transcripts are
    # small jsonl files in the shape Claude Code writes.
    fake_claude = os.path.join(tmp, "fake-claude.py")
    claude_log = os.path.join(tmp, "claude-calls.jsonl")
    with open(fake_claude, "w") as f:
        f.write("#!" + sys.executable + "\nimport json, os, sys\n"
                "with open(os.environ['FAKE_CLAUDE_LOG'], 'a') as f:\n"
                "    f.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
                "        'hook_ignore': os.environ.get('SAVANNAH_HOOK_IGNORE', '')}) + '\\n')\n"
                "if os.environ.get('FAKE_CLAUDE_FAIL'):\n"
                "    sys.stderr.write('boom\\n'); sys.exit(1)\n"
                "prompt = sys.argv[-1]\n"
                "title = 'Parser Rewrite' if 'parser' in prompt else 'ZFS Send Resume Review'\n"
                "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,\n"
                "    'result': '\"' + title + '.\"\\n', 'total_cost_usd': 0.0002}))\n")
    os.chmod(fake_claude, 0o755)
    claude_env = {"SAVANNAH_CLAUDE_BIN": fake_claude, "FAKE_CLAUDE_LOG": claude_log}

    def claude_calls():
        try:
            with open(claude_log) as f:
                return [json.loads(ln) for ln in f if ln.strip()]
        except OSError:
            return []

    def write_transcript(path, turns):
        with open(path, "w") as f:
            for prompt, reply in turns:
                f.write(json.dumps({"type": "user", "timestamp": "2026-11-09T10:00:00.000Z",
                                    "message": {"role": "user", "content": prompt}}) + "\n")
                if reply:
                    f.write(json.dumps({"type": "assistant", "timestamp": "2026-11-09T10:00:05.000Z",
                                        "message": {"role": "assistant",
                                                    "content": [{"type": "text", "text": reply}]}}) + "\n")

    an_dir = os.path.join(tmp, "autoname-dirs")
    zfs_dir, parser_dir, fresh_dir = (os.path.join(an_dir, n) for n in ("zfs", "parser", "fresh"))
    for d in (zfs_dir, parser_dir, fresh_dir):
        os.makedirs(d)
    t_zfs, t_parser = os.path.join(tmp, "an-zfs.jsonl"), os.path.join(tmp, "an-parser.jsonl")
    write_transcript(t_zfs, [("<command-name>/clear</command-name>", ""),
                             ("review PR 2353 for the zfs send resume fix", "Looking at the PR now."),
                             ("is CI green yet?", "CI is still running.")])
    write_transcript(t_parser, [("rewrite the parser\nit is slow", "On it.")])
    hooks_an = {
        "an-zfs": {"project": zfs_dir, "transcript_path": t_zfs, "last_prompt": "is CI green yet?",
                   "iterm_session_id": "w0t0p0:AN-ZFS"},
        "an-zfs2": {"project": zfs_dir, "transcript_path": t_zfs, "last_prompt": "is CI green yet?",
                    "iterm_session_id": "w0t0p0:AN-ZFS2"},
        "an-parser": {"project": parser_dir, "transcript_path": "", "last_prompt": "rewrite the parser",
                      "iterm_session_id": "w0t0p0:AN-PARSER"},
        "an-fresh": {"project": fresh_dir, "transcript_path": "", "last_prompt": "",
                     "iterm_session_id": "w0t0p0:AN-FRESH"},
    }
    for sid, h in hooks_an.items():
        h.update({"session_id": sid, "state": "idle", "updated": int(time.time())})
        with open(os.path.join(sessions, sid + ".json"), "w") as f:
            json.dump(h, f)
    # The in-process helpers first: prompts, excerpt, title cleaning, slugs.
    prompts, reply = sw.session_prompts(hooks_an["an-zfs"])
    check(prompts == ["review PR 2353 for the zfs send resume fix", "is CI green yet?"] and reply == "CI is still running.",
          f"session_prompts reads the human prompts (commands dropped) and the latest reply ({prompts}, {reply!r})")
    prompts, reply = sw.session_prompts(hooks_an["an-parser"])
    check(prompts == ["rewrite the parser"] and reply == "",
          f"session_prompts falls back to the hook's last_prompt without a transcript ({prompts})")
    check(sw.session_prompts(hooks_an["an-fresh"]) == ([], ""), "session_prompts: nothing typed yet is ([], '')")
    ex = sw.session_excerpt(zfs_dir, ["a", "b", "c", "d"], "x" * 500)
    check(ex.startswith("Directory: ") and "Prompt 3: c" in ex and "Latest prompt (4 so far): d" in ex
          and "Prompt 4" not in ex and ex.endswith("...") and len(ex) < 600,
          f"session_excerpt: three first prompts, the latest, the reply head squeezed ({ex!r})")
    for raw, want in (('"Fix The Parser."\n', "Fix The Parser"), ("Title: ZFS Review!", "ZFS Review"),
                      ("\n\n  `Two   Words`  ", "Two Words"), ("", ""), (None, ""), ("...", "")):
        check(sw.clean_title(raw) == want, f"clean_title({raw!r}) = {sw.clean_title(raw)!r}, want {want!r}")
    for raw, want in (("Fix The Parser", "fix-the-parser"), ("ZFS Send Resume Review", "zfs-send-resume-review"),
                      ("/a/b/My Repo", "my-repo"), ("!!!", ""), ("t-12", ""), ("a" * 70, "a" * 60),
                      ("v1.2_x-y", "v1.2_x-y"), ("..a", "a")):
        check(sw.slug_name(raw) == want, f"slug_name({raw!r}) = {sw.slug_name(raw)!r}, want {want!r}")
    check(sw.unique_name("a", {"a", "a-2"}) == "a-3" and sw.unique_name("b", {"a"}) == "b", "unique_name")
    # --dry-run: names and titles from the fake claude, nothing adopted, the
    # fresh tab skipped, two tabs with one title get distinct names.
    clear_log(keep_state=True)
    def mine(entries):
        return [e for e in entries if e.get("session_id") in hooks_an]

    j = run_json(["autoname", "--dry-run", "--json"], claude_env)
    by_sid = {e["session_id"]: e for e in mine(j.get("named", []))}
    check(j.get("dry_run") is True and set(by_sid) == {"an-zfs", "an-zfs2", "an-parser"},
          f"autoname --dry-run names every unnamed tab with a prompt ({j})")
    check(by_sid.get("an-zfs", {}).get("title") == "ZFS Send Resume Review"
          and by_sid.get("an-zfs", {}).get("how") == "claude",
          f"autoname: the title is claude's answer, cleaned ({by_sid.get('an-zfs')})")
    names_an = sorted(e["name"] for e in by_sid.values())
    check(names_an == ["parser-rewrite", "zfs-send-resume-review", "zfs-send-resume-review-2"],
          f"autoname derives names from the titles and keeps them unique ({names_an})")
    check([(e.get("session_id"), e.get("why")) for e in mine(j.get("skipped", []))] == [("an-fresh", "no prompt yet")],
          f"autoname skips a tab nothing was typed into ({j.get('skipped')})")
    check(not any(os.path.exists(os.path.join(workroot, n)) for n in names_an), "autoname --dry-run adopts nothing")
    ccalls = claude_calls()
    check(len(ccalls) == 3 + len(j.get("named", [])) - len(by_sid),
          f"autoname asks claude once per tab ({len(ccalls)} calls for {len(j.get('named', []))} tabs)")
    # The calls run on a thread pool, so find the zfs tabs' calls by content
    # (two tabs share that transcript, so two calls carry it).
    zfs_calls = [c for c in ccalls if "review PR 2353" in c["argv"][-1]]
    check(len(zfs_calls) == 2, f"autoname asks claude once per zfs tab, two tabs ({len(zfs_calls)} calls)")
    if zfs_calls:
        c0 = zfs_calls[0]
        check("--safe-mode" in c0["argv"] and "--no-session-persistence" in c0["argv"] and "-p" in c0["argv"]
              and "--strict-mcp-config" in c0["argv"] and c0["argv"][c0["argv"].index("--tools") + 1] == ""
              and c0["argv"][c0["argv"].index("--model") + 1] == "haiku" and "--output-format" in c0["argv"],
              f"autoname runs claude -p in safe mode, no tools, haiku, json ({c0['argv']})")
        check(c0["hook_ignore"] == "1", "autoname tells the session hook to stand down")
        check(os.path.realpath(c0["cwd"]) != os.path.realpath(tmp) and "Directory:" in c0["argv"][-1]
              and "Prompt 1: review PR 2353" in c0["argv"][-1] and "/clear" not in c0["argv"][-1],
              f"autoname runs claude away from the project with the excerpt as the prompt ({c0['cwd']}, {c0['argv'][-1]!r})")
    # A tab in one of the owner's checkouts beside its repository
    # (zfs-DLPX-98816 next to zfs: a git worktree, or a clone named that
    # way) is named after the checkout, not the title; the main checkout
    # and a plain directory keep the title's name.
    co_main = os.path.join(an_dir, "co", "zfs")
    co_wt, co_clone = os.path.join(an_dir, "co", "zfs-DLPX-98816"), os.path.join(an_dir, "co", "zfs-CP-11610")
    co_lone, co_sav = os.path.join(an_dir, "co", "dp2817-watch"), os.path.join(co_main, ".savannah-worktrees", "ed")
    for d in (os.path.join(co_main, ".git"), os.path.join(co_wt, "module"), os.path.join(co_clone, ".git"),
              os.path.join(co_lone, ".git"), co_sav):
        os.makedirs(d)
    with open(os.path.join(co_wt, ".git"), "w") as f:
        f.write(f"gitdir: {co_main}/.git/worktrees/zfs-DLPX-98816\n")
    with open(os.path.join(co_sav, ".git"), "w") as f:
        f.write(f"gitdir: {co_main}/.git/worktrees/ed\n")
    for d, want in ((co_wt, "zfs-DLPX-98816"), (os.path.join(co_wt, "module"), "zfs-DLPX-98816"),
                    (co_clone, "zfs-CP-11610"), (co_main, ""), (co_lone, ""), (co_sav, ""), (fresh_dir, ""), ("", "")):
        check(sw.checkout_name(d) == want, f"checkout_name({d!r}) = {sw.checkout_name(d)!r}, want {want!r}")
    for sid, d in (("an-wt1", co_wt), ("an-wt2", os.path.join(co_wt, "module")), ("an-cl", co_clone)):
        hooks_an[sid] = {"project": d, "transcript_path": "", "last_prompt": "look at the zfs send resume fix",
                         "iterm_session_id": "w0t0p0:" + sid.upper(), "session_id": sid, "state": "idle",
                         "updated": int(time.time())}
        with open(os.path.join(sessions, sid + ".json"), "w") as f:
            json.dump(hooks_an[sid], f)
    j = run_json(["autoname", "an-wt1", "an-wt2", "an-cl", "an-zfs", "--dry-run", "--json"], claude_env)
    by_sid = {e["session_id"]: e for e in j.get("named", [])}
    check(by_sid.get("an-wt1", {}).get("name") == "zfs-dlpx-98816" and by_sid.get("an-wt2", {}).get("name") == "zfs-dlpx-98816-2"
          and by_sid.get("an-cl", {}).get("name") == "zfs-cp-11610" and by_sid.get("an-zfs", {}).get("name") == "zfs-send-resume-review",
          f"autoname names a tab after its checkout, -2 for a second tab there, the title elsewhere ({by_sid})")
    check(by_sid.get("an-wt1", {}).get("title") == "ZFS Send Resume Review" and by_sid.get("an-wt1", {}).get("how") == "claude",
          f"the title still comes from claude ({by_sid.get('an-wt1')})")

    # The real run: records with adopted true and the title; the hook ids
    # given on the command line limit it; a named or unknown id is skipped.
    clear_log(keep_state=True)
    j = run_json(["autoname", "an-zfs", "an-parser", "nope", "--json"], claude_env)
    check(sorted(e["session_id"] for e in j.get("named", [])) == ["an-parser", "an-zfs"]
          and {e["session_id"]: e["why"] for e in j.get("skipped", [])} == {"nope": "no live Claude session"},
          f"autoname SESSION_ID... names only those, an unknown id is skipped ({j})")
    rec_zfs = record("zfs-send-resume-review")
    check(rec_zfs.get("adopted") is True and rec_zfs.get("title") == "ZFS Send Resume Review"
          and rec_zfs.get("worktree") == zfs_dir and rec_zfs.get("claude_pane") == "AN-ZFS",
          f"autoname adopts the tab with the title ({rec_zfs})")
    check(record("parser-rewrite").get("claude_pane") == "AN-PARSER", "autoname adopts the second tab too")
    entries = {e["name"]: e for e in run_json(["ls", "--json"])}
    check(entries.get("zfs-send-resume-review", {}).get("claude_session_id") == "an-zfs",
          "ls joins the auto-named session to its hook file")
    j = run_json(["autoname", "an-zfs", "--json"], claude_env)
    check(j.get("named") == [] and j.get("skipped") == [{"session_id": "an-zfs", "why": "already a work session"}],
          f"autoname of a tab that is a work session already is skipped ({j})")
    # A second plain tab in the zfs directory still gets named: the pane is
    # the join key, so the first record does not claim it.
    j = run_json(["autoname", "an-zfs2", "an-fresh", "--json"], claude_env)
    check([e["session_id"] for e in j.get("named", [])] == ["an-zfs2"]
          and j["named"][0]["name"] == "zfs-send-resume-review-2",
          f"autoname names the remaining tab with a -2 name ({j})")
    check([e["session_id"] for e in j.get("skipped", [])] == ["an-fresh"], f"the fresh tab is still skipped ({j})")
    # Fallback: claude fails -> the first prompt's first line is the title,
    # and the result says why; --no-claude never calls it.
    hooks_an["an-fresh"]["last_prompt"] = "look at the flaky test\nin ctest"
    with open(os.path.join(sessions, "an-fresh.json"), "w") as f:
        json.dump(hooks_an["an-fresh"], f)
    j = run_json(["autoname", "an-fresh", "--dry-run", "--json"], dict(claude_env, FAKE_CLAUDE_FAIL="1"))
    e = (j.get("named") or [{}])[0]
    check(e.get("title") == "look at the flaky test" and e.get("how") == "prompt" and "exited 1" in e.get("claude_error", "")
          and e.get("name") == "look-at-the-flaky-test",
          f"autoname falls back to the first prompt when claude fails, and says so ({e})")
    n_calls = len(claude_calls())
    j = run_json(["autoname", "an-fresh", "--dry-run", "--no-claude", "--json"], claude_env)
    e = (j.get("named") or [{}])[0]
    check(e.get("how") == "prompt" and "claude_error" not in e and len(claude_calls()) == n_calls,
          f"autoname --no-claude never runs claude ({e})")
    p = run(["autoname", "an-fresh", "--dry-run", "--no-claude"], claude_env)
    check(p.returncode == 0 and p.stdout.startswith('would name an-fresh: look-at-the-flaky-test  "look at the flaky test"  (prompt, '),
          f"autoname's human output ({p.stdout.strip()!r})")
    p = run(["autoname", "an-fresh", "--dry-run"], {"SAVANNAH_CLAUDE_BIN": os.path.join(tmp, "no-claude")})
    check(p.returncode == 0 and "(prompt," in p.stdout,
          f"autoname without a claude binary still names from the prompt ({p.stdout.strip()!r} / {p.stderr.strip()!r})")
    with open(os.path.join(logdir, "savannah.log")) as f:
        sav_log = f.read()
    check(any("autoname fallback" in ln and "level=WARN" in ln for ln in sav_log.splitlines()),
          "the fallback is a WARN line in savannah's log")
    for n in ("zfs-send-resume-review", "zfs-send-resume-review-2", "parser-rewrite"):
        check(run(["rm", n]).returncode == 0, f"rm {n}")
    for sid in hooks_an:
        os.remove(os.path.join(sessions, sid + ".json"))
    empty_sessions = os.path.join(tmp, "no-sessions")
    os.makedirs(empty_sessions)
    p = run(["autoname"], dict(claude_env, SAVANNAH_SESSIONS_DIR=empty_sessions))
    check(p.returncode == 0 and "nothing to name" in p.stdout, f"autoname with no plain tabs says so ({p.stdout!r})")

    # ---- ask: one question into every tab that is safe to interrupt ----------
    # Hook files are the input: which tabs are live, their state, their pane.
    # Two work sessions (one done, one working), a plain tab (idle), a
    # waiting tab, and the asking session itself (this test's pid is the
    # CLI's ancestor, as a Claude session's would be).
    ask_dir = os.path.join(tmp, "ask-dirs")
    ask_hooks = {}
    for sid, state, extra in (("ask-a", "done", {}), ("ask-b", "working", {}), ("ask-p", "idle", {}),
                              ("ask-w", "waiting", {"message": "use Bash", "notify_type": "permission_prompt"}),
                              ("ask-o", "waiting", {"message": "Claude is waiting for your input", "notify_type": "idle_prompt"}),
                              ("ask-me", "idle", {"pid": os.getpid()})):
        d = os.path.join(ask_dir, sid)
        os.makedirs(d)
        h = {"session_id": sid, "project": d, "state": state, "iterm_session_id": "w0t0p0:" + sid.upper(),
             "updated": int(time.time()), "last_prompt": "earlier", "transcript_path": os.path.join(tmp, sid + ".jsonl")}
        h.update(extra)
        ask_hooks[sid] = h

    def put_ask_hook(sid):
        with open(os.path.join(sessions, sid + ".json"), "w") as f:
            json.dump(ask_hooks[sid], f)

    for sid in ask_hooks:
        put_ask_hook(sid)
    run_json(["adopt", "ask-a", "--session=ask-a", "--json"])
    run_json(["adopt", "ask-b", "--session=ask-b", "--json"])
    check(sw.first_prompt_line("\n  What are you doing?\nreally") == "What are you doing?", "first_prompt_line")
    check(sw.ask_skip_reason({"state": "working"}, set()) == "working (not interrupted)"
          and sw.ask_skip_reason({"state": "waiting"}, set()).startswith("waiting for you")
          and sw.ask_skip_reason({"state": "idle", "pid": 7, "iterm_session_id": "w:X"}, {7}) == "the session asking"
          and sw.ask_skip_reason({"state": "done", "iterm_session_id": ""}, set()) == "no iTerm2 pane recorded"
          and sw.ask_skip_reason({"state": "done", "iterm_session_id": "w0t0p0:X"}, set()) == "",
          "ask_skip_reason: working, waiting, the asker, no pane; else asked")
    check(sw.normalize_hook({"state": "waiting", "notify_type": "idle_prompt", "message": "x"}) == {"state": "idle", "notify_type": "idle_prompt", "message": ""}
          and sw.normalize_hook({"state": "waiting", "notify_type": "permission_prompt", "message": "x"})["state"] == "waiting",
          "normalize_hook: an old hook's waiting-for-the-nudge file is idle, a permission prompt stays waiting")
    check(run(["ask"]).returncode == 2, "ask with neither names nor --all is a usage error")
    check(run(["ask", "--all", "--timeout", "0"]).returncode == 2, "ask --timeout 0 is a usage error")

    # The run: the question goes to ask-a and ask-p only; the replies land
    # (hook files flipped to done with our prompt, transcripts written) while
    # the CLI waits; each work session's reply becomes its summary.
    question = "What are you doing?"
    clear_log(keep_state=True)
    e = dict(env)
    proc = subprocess.Popen([sys.executable, module, "ask", "--all", "-q", question, "--timeout", "8", "--json"],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e, cwd=tmp)
    time.sleep(1.5)
    for sid, reply in (("ask-a", "Reviewing the parser.\nNext: the tests."), ("ask-p", "Reading the wifi logs."),
                       ("ask-o", "Idle since the nudge.")):
        write_transcript(ask_hooks[sid]["transcript_path"], [("earlier", "ok"), (question, reply)])
        ask_hooks[sid].update({"state": "done", "last_prompt": question, "updated": int(time.time())})
        put_ask_hook(sid)
    out, err = proc.communicate(timeout=30)
    check(proc.returncode == 0, f"ask --all exits 0 ({err.strip()[:200]})")
    try:
        j = json.loads(out)
    except ValueError:
        j = {}
        check(False, f"ask --all emits JSON ({out!r})")
    asked = {a_["session_id"]: a_ for a_ in j.get("asked", [])}
    skipped = {a_["session_id"]: a_["why"] for a_ in j.get("skipped", [])}
    check(set(asked) == {"ask-a", "ask-p", "ask-o"}, f"ask --all asks the done and idle tabs only ({sorted(asked)})")
    check(asked.get("ask-o", {}).get("how") == "answered",
          f"a file an older hook stamped waiting for the idle nudge is an idle tab, asked ({asked.get('ask-o')})")
    check(asked.get("ask-a", {}).get("how") == "answered" and asked["ask-a"]["reply"] == "Reviewing the parser.\nNext: the tests."
          and asked["ask-a"]["name"] == "ask-a",
          f"the work session's reply is read from its transcript ({asked.get('ask-a')})")
    check(asked.get("ask-p", {}).get("how") == "answered" and asked["ask-p"]["reply"] == "Reading the wifi logs."
          and asked["ask-p"]["name"] == "", f"a plain tab is asked too, by its hook file ({asked.get('ask-p')})")
    check(skipped.get("ask-b") == "working (not interrupted)" and (skipped.get("ask-w") or "").startswith("waiting for you")
          and skipped.get("ask-me") == "the session asking", f"working, waiting and the asker are skipped ({skipped})")
    check(j.get("question") == question and j.get("waited", 0) < 8, f"the result carries the question and the wait ({j.get('waited')})")
    sends = [c for c in calls() if c[:2] == ["session", "send"]]
    panes = [c[c.index("--session") + 1] for c in sends]
    check(sorted(set(panes)) == ["ASK-A", "ASK-O", "ASK-P"] and len(sends) == 6
          and all(c[-1] in (question, "\r") for c in sends),
          f"the question (then Enter) is typed into exactly the asked panes ({sends})")
    rec_a = record("ask-a")
    check(rec_a.get("summary") == "Reviewing the parser.\nNext: the tests." and isinstance(rec_a.get("summary_at"), int),
          f"the reply is kept as the work session's summary ({rec_a.get('summary')!r})")
    check("summary" not in record("ask-b"), "a skipped session gets no summary")
    def notes_of(n):
        try:
            with open(os.path.join(workroot, n, "notes.md")) as f:
                return f.read()
        except OSError:
            return ""

    notes_a = notes_of("ask-a")
    check(notes_a.endswith("Reviewing the parser.\nNext: the tests.\n(asked: What are you doing?)\n\n")
          and notes_a.count("## ") == 1,
          f"the reply is appended to the session's notes with the question after it ({notes_a!r})")
    check(notes_of("ask-b") == "", "a skipped session gets no note")
    entries = {e_["name"]: e_ for e_ in run_json(["ls", "--json"])}
    check(entries.get("ask-a", {}).get("summary") == rec_a["summary"], "ls --json carries the summary")
    check(entries.get("ask-a", {}).get("last_note") == "Reviewing the parser.", "the NOTE column shows the reply's first line")

    # A tab that does not answer in time: how timeout, no reply, the old
    # summary kept; --no-wait returns at once with how sent; the default
    # question; a named session with no live tab is skipped as offline.
    j = run_json(["ask", "ask-a", "-q", "Still there?", "--timeout", "1", "--json"])
    e1 = (j.get("asked") or [{}])[0]
    check(e1.get("how") == "timeout" and e1.get("reply") == "" and record("ask-a")["summary"] == rec_a["summary"]
          and notes_of("ask-a") == notes_a,
          f"no answer in time is a timeout with the old summary and notes kept ({e1})")
    j = run_json(["ask", "ask-a", "--no-wait", "--json"])
    e1 = (j.get("asked") or [{}])[0]
    check(e1.get("how") == "sent" and j.get("question") == sw.ASK_DEFAULT, f"--no-wait sends and returns; the default question ({j})")
    os.remove(os.path.join(sessions, "ask-b.json"))
    j = run_json(["ask", "ask-b", "--json"])
    check(j.get("asked") == [] and (j.get("skipped") or [{}])[0].get("why", "").startswith("no live Claude session"),
          f"a named session with no live tab is skipped as offline ({j})")
    p = run(["ask", "ask-a", "ask-b", "-q", "Still there?", "--timeout", "1"])
    check(p.returncode == 0 and p.stdout.startswith("## ask-a  (") and "(no reply within 1" in p.stdout
          and "skipped ask-b: no live Claude session" in p.stdout, f"ask's human output ({p.stdout!r})")
    for n in ("ask-a", "ask-b"):
        check(run(["rm", n]).returncode == 0, f"rm {n}")
    for sid in ask_hooks:
        try:
            os.remove(os.path.join(sessions, sid + ".json"))
        except OSError:
            pass

    # ---- move: priority (the board's two sections) and rank -------------------
    # A record from before the fields existed reads active, ranked by its
    # creation time; new sessions get the same, so untouched rows keep
    # arriving in order. move changes the record and nothing else.
    pri = run_json(["new", "pri", "--no-worktree", "--repo", repo, "--json"])
    check(pri.get("priority") == "active" and pri.get("rank") == pri.get("created"),
          f"new records priority active and rank == created ({pri})")
    write_record("oldrec", {"name": "oldrec", "worktree": other, "repo": other, "created": 1234})
    write_record("oddrec", {"name": "oddrec", "worktree": other, "repo": other, "created": 77,
                            "priority": "urgent", "rank": "soon"})
    write_record("laterrec", {"name": "laterrec", "worktree": other, "repo": other, "created": 5,
                              "priority": "later", "rank": "12"})
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check(by["oldrec"]["priority"] == "active" and by["oldrec"]["rank"] == 1234,
          f"a record without the fields reads active, rank == created ({by['oldrec']})")
    check(by["oddrec"]["priority"] == "active" and by["oddrec"]["rank"] == 77,
          f"an unknown priority reads active, an unparsable rank falls back to created ({by['oddrec']})")
    write_record("nowrec", {"name": "nowrec", "worktree": other, "repo": other, "created": 6,
                            "priority": "now", "rank": 3})
    by = {r["name"]: r for r in run_json(["ls", "--json"])}
    check(by["nowrec"]["priority"] == "now" and by["nowrec"]["rank"] == 3, f"priority now is kept ({by['nowrec']})")
    check(by["laterrec"]["priority"] == "later" and by["laterrec"]["rank"] == 12,
          f"later and a numeric-string rank are read as stored ({by['laterrec']})")
    clear_log(keep_state=True)
    j = run_json(["move", "pri", "--later", "--json"])
    check(j == {"name": "pri", "priority": "later", "rank": pri["created"]},
          f"move --later: json names the new section and keeps the rank ({j})")
    check(record("pri")["priority"] == "later", "move --later is stored")
    j = run_json(["move", "pri", "--rank", "-5", "--json"])
    check(j.get("rank") == -5 and j.get("priority") == "later" and record("pri")["rank"] == -5,
          f"move --rank takes a negative rank and keeps the section ({j})")
    j = run_json(["move", "pri", "--active", "--rank=10", "--json"])
    check(j == {"name": "pri", "priority": "active", "rank": 10}, f"move --active --rank together ({j})")
    p = run(["move", "pri", "--later"])
    check(p.returncode == 0 and p.stdout == "moved pri: later, rank 10\n", f"move human line ({p.stdout!r})")
    j = run_json(["move", "pri", "--now", "--json"])
    check(j == {"name": "pri", "priority": "now", "rank": 10} and record("pri")["priority"] == "now",
          f"move --now: the top section, rank kept ({j})")
    check(run(["move", "pri", "--now", "--active"]).returncode == 2, "move --now --active exits 2")
    p = run(["move", "pri", "--later"])
    check(p.returncode == 0, "back to later")
    check(run(["move", "pri"]).returncode == 2, "move without an option exits 2")
    check(run(["move", "pri", "--active", "--later"]).returncode == 2, "move --active --later exits 2")
    check(run(["move", "pri", "--rank", "soon"]).returncode == 2, "move --rank wants an integer")
    check(record("pri")["priority"] == "later" and record("pri")["rank"] == 10,
          "refused moves change nothing")
    check(read_log() == "", "move never talks to it2")
    rec_pri = record("pri")
    check(set(rec_pri) >= {"priority", "rank", "updated"} and rec_pri["worktree"] == pri["worktree"],
          "move rewrites the record whole, with the fields")
    for n in ("oldrec", "oddrec", "laterrec"):
        shutil.rmtree(os.path.join(workroot, n))

    # ---- rename: the record moves, the worktree and branch stay ----------------
    rn = run_json(["new", "rn-old", "--no-worktree", "--repo", repo, "--json"])
    run(["note", "rn-old", "--", "note travels"])
    run_json(["status", "rn-old", "--json", "--", "poll travels"])
    clear_log(keep_state=True)
    j = run_json(["rename", "rn-old", "rn-new", "--json"])
    check(j == {"name": "rn-new", "old": "rn-old", "worktree": rn["worktree"], "branch": rn["branch"]},
          f"rename json ({j})")
    check(read_log() == "", "rename never talks to it2 (the ls below does, once, for pane_alive)")
    check(not os.path.exists(os.path.join(workroot, "rn-old")), "rename: the old record dir is gone")
    for fn in ("record.json", "notes.md", "status.json"):
        check(os.path.exists(os.path.join(workroot, "rn-new", fn)), f"rename: {fn} travels")
    rec_new = record("rn-new")
    check(rec_new["name"] == "rn-new" and rec_new["worktree"] == rn["worktree"]
          and rec_new["claude_pane"] == rn["claude_pane"] and rec_new["created"] == rn["created"]
          and rec_new["rank"] == rn["rank"],
          f"rename rewrites the record with the new name, everything else as it was ({rec_new})")
    check("note travels" in run(["notes", "rn-new"]).stdout, "rename: notes readable under the new name")
    j = run_json(["status", "rn-new", "--json"])
    check((j.get("status") or {}).get("text") == "poll travels", f"rename: status readable under the new name ({j})")
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check("rn-new" in by and "rn-old" not in by and by["rn-new"]["worktree"] == rn["worktree"]
          and by["rn-new"]["branch"] == rn["branch"], "ls shows the new name with the same worktree and branch")
    p = run(["rename", "rn-new", "flat"])
    check(p.returncode == 1 and "exists" in p.stderr, f"rename onto an existing name is refused ({p.stderr.strip()})")
    check(record("rn-new") == rec_new and record("flat")["name"] == "flat"
          and os.path.exists(os.path.join(workroot, "rn-new", "notes.md"))
          and os.path.exists(os.path.join(workroot, "flat", "record.json")),
          "a refused rename leaves both sessions untouched")
    p = run(["rename", "rn-new", "Bad Name"])
    check(p.returncode == 1 and "invalid name" in p.stderr, "rename validates the new name")
    p = run(["rename", "bad name", "rn-x"])
    check(p.returncode == 1 and "invalid" in p.stderr and not os.path.exists(os.path.join(workroot, "rn-x")),
          "rename validates the old name and claims nothing")
    check(run(["rename", "rn-new", "rn-new"]).returncode == 1, "rename to the same name exits 1")
    check(record("rn-new") == rec_new, "refused renames change nothing")
    p = run(["rename", "rn-new", "rn-final"])
    check(p.returncode == 0 and p.stdout == "renamed rn-new to rn-final (worktree and branch keep their names)\n",
          f"rename human line ({p.stdout!r})")
    clear_log(keep_state=True)
    p = run(["rm", "rn-final"])
    check(p.returncode == 0 and not os.path.exists(os.path.join(workroot, "rn-final")),
          f"rm of a renamed session ({p.stderr.strip()})")
    adir = os.path.join(workroot, "archive")
    archived = [f for f in os.listdir(adir) if f.startswith("rn-final-")] if os.path.isdir(adir) else []
    check(archived, "rm of a renamed session archives its notes under the new name")
    if archived:
        with open(os.path.join(adir, archived[0])) as f:
            check("note travels" in f.read(), "the archived notes are the ones that travelled")
    check(called(["session", "close", "--session", rn["claude_pane"], "--force"]) >= 0,
          f"rm of a renamed session closes the original panes ({calls()})")

    # ---- status: what a tab's Claude is polling for you ------------------------
    st_dir = os.path.join(tmp, "herewt")
    os.makedirs(st_dir)
    here1 = run_json(["new", "here1", "--no-worktree", "--repo", st_dir, "--json"])
    p = run(["status", "here1"])
    check(p.returncode == 0 and p.stdout == "here1: (no status)\n", f"status of a fresh session ({p.stdout!r})")
    j = run_json(["status", "here1", "--json"])
    check(j == {"name": "here1", "status": None}, f"status --json of a fresh session ({j})")
    t0 = int(time.time())
    j = run_json(["status", "here1", "--json", "--", "waiting for ctest (run 1 of 3)"])
    check(j.get("name") == "here1" and j.get("text") == "waiting for ctest (run 1 of 3)" and j.get("count") == 1
          and isinstance(j.get("updated"), int) and j["updated"] >= t0 and set(j) == {"name", "text", "count", "updated"},
          f"status TEXT writes text, count 1 and an epoch ({j})")
    with open(os.path.join(workroot, "here1", "status.json")) as f:
        on_disk = json.load(f)
    check(on_disk == {"text": "waiting for ctest (run 1 of 3)", "updated": j["updated"], "count": 1},
          f"status.json shape ({on_disk})")
    p = run(["status", "here1", "waiting", "for", "ctest", "(run 2 of 3)"])
    check(p.returncode == 0 and p.stdout == "here1: poll 2: waiting for ctest (run 2 of 3)\n",
          f"a second report counts 2 and joins the words ({p.stdout!r})")
    j = run_json(["status", "here1", "--json"])
    check(j.get("name") == "here1" and (j.get("status") or {}).get("text") == "waiting for ctest (run 2 of 3)"
          and j["status"]["count"] == 2 and j["status"]["updated"] >= t0, f"status --json reads it back ({j})")
    p = run(["status", "here1"])
    check(p.returncode == 0 and re.fullmatch(r"here1: waiting for ctest \(run 2 of 3\) \(2 polls, last \d+s ago\)\n", p.stdout),
          f"status human read line ({p.stdout!r})")
    j = run_json(["status", "here1", "--json", "--", "first line\nsecond line"])
    check(j.get("text") == "first line" and j.get("count") == 3, f"multi-line text keeps its first line ({j})")
    j = run_json(["status", "here1", "--json", "--", "x" * 300])
    check(j.get("text") == "x" * 200 and j.get("count") == 4, "text is capped at 200 chars")
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check(by["here1"]["status"] == {"text": "x" * 200, "count": 4, "updated": j["updated"]}
          and by["here1"]["priority"] == "active" and by["here1"]["rank"] == here1["created"],
          f"ls --json carries status, priority and rank ({by['here1']})")
    check(by["flat"]["status"] is None, "ls --json: status is null without a report")
    p = run(["ls"])
    head = p.stdout.splitlines()[0]
    check(head.split()[:5] == ["NAME", "TITLE", "BRANCH", "STATE", "STATUS"], f"ls human has a STATUS column after STATE ({head})")
    idx = head.index("STATUS") if "STATUS" in head else 0
    row = [ln for ln in p.stdout.splitlines() if ln.startswith("here1 ")]
    check(row and row[0][idx:idx + 30] == "x" * 25 + "~ (4)", f"ls human clips the text to 30 with the count ({row})")
    row = [ln for ln in p.stdout.splitlines() if ln.startswith("flat ")]
    check(row and row[0][idx:].startswith("- "), f"ls human shows - without a report ({row})")
    # a damaged status.json reads as no status, never as an error
    with open(os.path.join(workroot, "here1", "status.json"), "w") as f:
        f.write("{not json")
    check(run_json(["status", "here1", "--json"]) == {"name": "here1", "status": None}, "a damaged status.json reads as none")
    check(run(["ls"]).returncode == 0, "ls survives a damaged status.json")
    j = run_json(["status", "here1", "--json", "--", "after damage"])
    check(j.get("count") == 1, "the count starts over after a damaged file")
    p = run(["status", "here1", "--clear"])
    check(p.returncode == 0 and p.stdout == "here1: status cleared\n"
          and not os.path.exists(os.path.join(workroot, "here1", "status.json")),
          f"status --clear removes the file ({p.stdout!r})")
    j = run_json(["status", "here1", "--clear", "--json"])
    check(j == {"name": "here1", "status": None}, f"--clear twice is fine and json says null ({j})")
    check(run(["status", "here1"]).stdout == "here1: (no status)\n", "after --clear: (no status)")
    j = run_json(["status", "here1", "--json", "--", "again"])
    check(j.get("count") == 1, "the count starts over after --clear")
    # usage
    check(run(["status", "here1", "--clear", "--", "text"]).returncode == 2, "status --clear with text exits 2")
    check(run(["status", "here1", "--", "   "]).returncode == 2, "blank status text exits 2")
    check(run_json(["status", "here1", "--json"]).get("status", {}).get("count") == 1, "refused reports change nothing")
    # --here: the Claude pane first (ITERM_SESSION_ID is what Claude Code's Bash inherits)
    pane_env = {"ITERM_SESSION_ID": f"w1t1p1:{here1['claude_pane']}"}
    j = run_json(["status", "--here", "--json", "--", "from the pane"], pane_env, cwd=tmp)
    check(j.get("name") == "here1" and j.get("text") == "from the pane" and j.get("count") == 2,
          f"--here finds the session by its Claude pane, whatever the cwd ({j})")
    j = run_json(["status", "--here", "--json"], pane_env, cwd=tmp)
    check(j.get("name") == "here1" and (j.get("status") or {}).get("text") == "from the pane", f"--here reads too ({j})")
    j = run_json(["status", "--here", "some", "words", "--json"], pane_env, cwd=tmp)
    check(j.get("text") == "some words" and j.get("count") == 3, f"with --here every positional is text ({j})")
    # --here: the cwd when no pane matches (the test env's id matches no record)
    j = run_json(["status", "--here", "--json", "--", "from the cwd"], cwd=st_dir)
    check(j.get("name") == "here1" and j.get("count") == 4, f"--here falls back to the one session in the cwd ({j})")
    j = run_json(["status", "--here", "--json", "--", "no id at all"], {"ITERM_SESSION_ID": ""}, cwd=st_dir)
    check(j.get("name") == "here1" and j.get("count") == 5, f"--here with an empty ITERM_SESSION_ID uses the cwd ({j})")
    p = run(["status", "--here", "--", "x"], cwd=rq)
    check(p.returncode == 1 and "several work sessions share this directory" in p.stderr
          and "flat" in p.stderr and "bare" in p.stderr and "(name one)" in p.stderr,
          f"--here in a directory with several sessions names them ({p.stderr.strip()})")
    nowhere = os.path.join(tmp, "nowhere")
    os.makedirs(nowhere)
    p = run(["status", "--here"], cwd=nowhere)
    check(p.returncode == 1 and "no work session for" in p.stderr and "nowhere" in p.stderr,
          f"--here outside every session exits 1 ({p.stderr.strip()})")
    check(run(["status", "--here", "--clear"], pane_env, cwd=tmp).returncode == 0
          and run_json(["status", "here1", "--json"]) == {"name": "here1", "status": None},
          "--here --clear clears the pane's session")
    check(run(["rm", "here1"]).returncode == 0, "rm here1")

    # ---- repos: the folder of repos the board offers ---------------------------
    folder = os.path.join(tmp, "folder")
    for n in ("zeta", "alpha", ".hidden", "plain"):
        os.makedirs(os.path.join(folder, n))
    for n in ("zeta", "alpha", ".hidden"):
        os.makedirs(os.path.join(folder, n, ".git"))
    with open(os.path.join(folder, "wtfile", ), "w") as f:
        f.write("x")  # a file, not a dir
    os.makedirs(os.path.join(folder, "linked"))
    with open(os.path.join(folder, "linked", ".git"), "w") as f:
        f.write("gitdir: elsewhere\n")  # a worktree or submodule: still a repo
    j = run_json(["repos", folder, "--json"])
    check(j.get("repos") == ["alpha", "linked", "zeta"] and j.get("dir") == folder and j.get("picked") == "given"
          and j.get("scratch") == "", f"repos DIR lists git dirs sorted, skips dotdirs and plain dirs ({j})")
    with open(os.path.join(tmp, "hub.json")) as f:
        check(json.load(f).get("dir") == folder, "repos DIR remembers the folder in hub.json")
    os.makedirs(os.path.join(folder, ".savannah-scratch"))
    j = run_json(["repos", "--json"], cwd=tmp)
    check(j.get("dir") == folder and j.get("picked") == "remembered"
          and j.get("scratch") == os.path.join(folder, ".savannah-scratch"),
          f"repos without DIR uses the remembered folder and reports scratch ({j})")
    p = run(["repos"], cwd=tmp)
    check(p.returncode == 0 and p.stdout.splitlines()[0].startswith(folder) and "  alpha" in p.stdout
          and "scratch" in p.stdout, f"repos human output ({p.stdout!r})")
    check(run(["repos", os.path.join(tmp, "nope")]).returncode == 1, "repos on a missing dir exits 1")
    os.remove(os.path.join(tmp, "hub.json"))
    j = run_json(["repos", "--json"], cwd=folder)
    check(os.path.realpath(j.get("dir", "")) == os.path.realpath(folder) and j.get("picked") == "cwd",
          "repos with nothing remembered uses the cwd")

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
        check(called(["session", "close", "--session", "PANE-C-1", "--force"]) >= 0
              and called(["session", "close", "--session", "PANE-S-1", "--force"]) >= 0,
              f"rm closes the tab pane by pane ({calls()})")
        check(called(["tab", "close"]) < 0, "rm never calls `it2 tab close` (it closes nothing)")
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
        p = run(["new", "feat", "--repo", repo], {"SAVANNAH_IT2": noexec})
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
    p = run(["rm", "human"], {"FAKE_IT2_MISSING": f"{hrec['claude_pane']},{hrec['shell_pane']}"})
    check(p.returncode == 0 and not os.path.exists(os.path.join(workroot, "human")),
          "rm with a vanished tab still succeeds")
    check(called(["session", "close", "--session", hrec["claude_pane"], "--force"]) >= 0
          and called(["session", "close", "--session", hrec["shell_pane"], "--force"]) >= 0,
          "rm with a vanished tab still tried to close its own panes")
    adir = os.path.join(workroot, "archive")
    check(not os.path.isdir(adir) or not any(f.startswith("human-") for f in os.listdir(adir)),
          "no notes, nothing archived")
    check(run(["rm", "human"]).returncode == 1, "rm twice: unknown name exits 1")
    check(run(["rm", "bare"], {"SAVANNAH_IT2": "/nonexistent/it2"}).returncode == 0,
          "rm succeeds even when it2 is unavailable")
    check(run(["rm", "lonely"], {"SAVANNAH_IT2": noexec}).returncode == 0,
          "rm succeeds even when it2 is not executable")

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
    # (in-process: make the record write fail; the fake it2 logs the close).
    import types
    clear_log()
    real_write = sw.atomic_write

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    # In-process, so the module reads os.environ: point it at the temp dirs
    # and the fake it2 for the duration (never the real iTerm2).
    saved_env = {k: os.environ.get(k) for k in
                 ("SAVANNAH_WORK_DIR", "SAVANNAH_SESSIONS_DIR", "SAVANNAH_IT2", "FAKE_IT2_LOG", "ITERM_SESSION_ID",
                  "SAVANNAH_LOG_DIR")}
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
    check(any(c[:3] == ["session", "close", "--session"] and c[3].startswith("PANE-C-") for c in calls()),
          f"rollback after a failed record write closes the tab it just created ({calls()})")
    check(not os.path.exists(os.path.join(workroot, "nospace")), "rollback removed the claimed dir")

    # ---- output: the read side of say --------------------------------------
    chat_dir = os.path.join(tmp, "chatrepo")
    os.makedirs(chat_dir, exist_ok=True)
    run_json(["new", "chat", "--no-worktree", "--repo", chat_dir, "--json"])
    p = run(["output", "chat"])
    check(p.returncode == 1 and "no transcript" in p.stderr,
          f"output with no transcript exits 1 with a reason ({p.stderr.strip()})")
    check(run(["output", "chat", "--last", "0"]).returncode == 2, "output --last 0 exits 2")
    check(run(["output", "nosuch"]).returncode == 1, "output of unknown name exits 1")

    def line(obj):
        return json.dumps(obj) + "\n"
    transcript = os.path.join(tmp, "chat.jsonl")
    with open(transcript, "w") as f:
        f.write(line({"type": "mode", "mode": "normal"}))                       # no message: ignored
        f.write(line({"type": "user", "timestamp": "2026-10-30T01:00:00.000Z",
                      "message": {"role": "user", "content": "first question"}}))
        f.write(line({"type": "assistant", "timestamp": "2026-10-30T01:00:05.000Z",
                      "message": {"role": "assistant", "content": [
                          {"type": "thinking", "thinking": "secret"},
                          {"type": "text", "text": "Looking now."},
                          {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}))
        f.write(line({"type": "user", "timestamp": "2026-10-30T01:00:06.000Z",
                      "message": {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": "x", "content": "a b c"}]}}))
        f.write(line({"type": "assistant", "isSidechain": True,
                      "message": {"role": "assistant", "content": [{"type": "text", "text": "SUBAGENT"}]}}))
        f.write("{not json\n")
        f.write(line({"type": "assistant", "timestamp": "2026-10-30T01:00:09.000Z",
                      "message": {"role": "assistant", "content": [{"type": "text", "text": "First answer."}]}}))
        f.write(line({"type": "user", "timestamp": "2026-10-30T01:01:00.000Z",
                      "message": {"role": "user", "content": [{"type": "text", "text": "second\nquestion"}]}}))
        f.write(line({"type": "assistant", "timestamp": "2026-10-30T01:01:04.000Z",
                      "message": {"role": "assistant", "content": [{"type": "text", "text": "Second answer."}]}}))
        f.write(line({"type": "user", "timestamp": "2026-10-30T01:02:00.000Z",
                      "message": {"role": "user", "content": "third, unanswered"}}))
    with open(os.path.join(sessions, "chat.json"), "w") as f:
        json.dump({"session_id": "chat-s", "project": chat_dir, "state": "working", "updated": 1,
                   "transcript_path": transcript}, f)
    p = run(["output", "chat"])
    check(p.returncode == 0, f"output exits 0 ({p.stderr.strip()})")
    check("you: third, unanswered" in p.stdout and "(no reply yet)" in p.stdout,
          f"output default shows the last turn, unanswered ({p.stdout!r})")
    check("First answer" not in p.stdout and "Second answer" not in p.stdout, "output default is one turn")
    p = run(["output", "chat", "--last", "3"])
    check(p.stdout.count("## ") == 3, f"output --last 3 prints three headers ({p.stdout!r})")
    check("SUBAGENT" not in p.stdout and "secret" not in p.stdout and "a b c" not in p.stdout,
          "output drops sidechain lines, thinking blocks and tool results")
    check("Looking now.\n\nFirst answer." in p.stdout, "output joins a turn's assistant texts in order")
    check("you: second" in p.stdout and "second\nquestion" not in p.stdout,
          "output header shows the prompt's first line only")
    check(" local  you:" in p.stdout, "output header carries a local timestamp")
    out = run_json(["output", "chat", "--last", "99", "--json"])
    check(out.get("source") == "hook" and out.get("transcript_path") == transcript,
          "output --json names the hook transcript")
    check(out.get("turns_total") == 3 and len(out.get("turns", [])) == 3, "output --json --last 99 caps at total")
    t = out["turns"]
    check(t[0]["prompt"] == "first question" and t[0]["text"] == "Looking now.\n\nFirst answer."
          and t[0]["asked"].startswith("2026-10-30T01:00:00") and t[0]["answered"].startswith("2026-10-30T01:00:09"),
          f"output --json turn 1 fields ({t[0]})")
    check(t[1]["prompt"] == "second\nquestion" and t[1]["text"] == "Second answer.", "output --json turn 2")
    check(t[2]["text"] == "" and t[2]["answered"] == "", "output --json unanswered turn has empty text")
    out = run_json(["output", "chat", "--json"])
    check(len(out["turns"]) == 1 and out["turns"][0]["prompt"] == "third, unanswered", "output --json default last 1")

    # fallback: no hook file, Claude Code's own projects dir (HOME is tmp here)
    os.remove(os.path.join(sessions, "chat.json"))
    p = run(["output", "chat"])
    check(p.returncode == 1, "output without hook and without projects dir exits 1")
    sw2 = load_module(module)
    check(sw2.encode_project_dir("/a/b.c_d x") == "-a-b-c-d-x", "encode_project_dir replaces every non-alphanumeric")
    pdir = os.path.join(tmp, ".claude", "projects", sw2.encode_project_dir(os.path.realpath(chat_dir)))
    os.makedirs(pdir)
    with open(os.path.join(pdir, "old.jsonl"), "w") as f:
        f.write(line({"type": "user", "message": {"role": "user", "content": "OLD"}}))
    os.utime(os.path.join(pdir, "old.jsonl"), (1000, 1000))
    shutil.copy(transcript, os.path.join(pdir, "new.jsonl"))
    out = run_json(["output", "chat", "--json"])
    check(out.get("source") == "projects" and out["transcript_path"].endswith("new.jsonl"),
          f"output falls back to the newest projects transcript ({out.get('transcript_path')})")
    cfg = os.path.join(tmp, "altcfg")
    os.makedirs(os.path.join(cfg, "projects", sw2.encode_project_dir(os.path.realpath(chat_dir))))
    with open(os.path.join(cfg, "projects", sw2.encode_project_dir(os.path.realpath(chat_dir)), "x.jsonl"), "w") as f:
        f.write(line({"type": "user", "message": {"role": "user", "content": "ALT"}}))
    out = run_json(["output", "chat", "--json"], extra_env={"CLAUDE_CONFIG_DIR": cfg})
    check(out["turns"][0]["prompt"] == "ALT", "output honors CLAUDE_CONFIG_DIR")
    with open(os.path.join(sessions, "chat.json"), "w") as f:
        json.dump({"session_id": "chat-s", "project": chat_dir, "transcript_path": os.path.join(tmp, "gone.jsonl")}, f)
    out = run_json(["output", "chat", "--json"])
    check(out.get("source") == "projects", "a hook transcript_path that no longer exists falls back to projects")
    os.remove(os.path.join(sessions, "chat.json"))
    check(sw2.local_stamp("") == "" and sw2.local_stamp("junk") == "junk"
          and sw2.local_stamp("2026-10-30T01:00:00.000Z").endswith(" local"), "local_stamp edge cases")

    # ---- hub: .mcp.json + CLAUDE.md in a directory of repos ----------------
    hub = os.path.join(tmp, "hub")
    os.makedirs(hub)
    check(run(["hub", os.path.join(tmp, "nope"), "--no-launch"]).returncode == 1, "hub on a missing dir exits 1")
    check(not os.path.exists(os.path.join(tmp, "hub.json")), "hub on a missing dir remembers nothing")
    h = run_json(["hub", hub, "--no-launch", "--json"])
    check(h.get("mcp_status") == "created" and h.get("claude_md_status") == "created" and h.get("launch") is False,
          f"hub first run creates both files ({h})")
    check(h.get("picked") == "given" and h.get("scratch_status") == "created"
          and h.get("hub_file") == os.path.join(tmp, "hub.json"), f"hub first run: scratch + remembered ({h})")
    settings_path = os.path.join(hub, ".claude", "settings.json")
    check(h.get("settings_status") == "created" and h.get("settings") == settings_path,
          f"hub first run creates .claude/settings.json ({h})")
    with open(settings_path) as f:
        st = json.load(f)
    allow = st.get("permissions", {}).get("allow", [])
    check(st.get("enableAllProjectMcpServers") is True, "hub settings approve project MCP servers")
    check("mcp__peer__work_new" in allow and "mcp__peer__work_output" in allow and "Bash(sleep *)" in allow,
          f"hub settings pre-allow the work_* tools and sleep ({allow})")
    check(not any(r.startswith("mcp__peer__task") or r.startswith("mcp__peer__ask") for r in allow),
          "hub settings do not pre-allow mesh tools")
    scratch = os.path.join(hub, ".savannah-scratch")
    check(os.path.isdir(scratch) and os.path.isfile(os.path.join(scratch, "CLAUDE.md")),
          "hub creates .savannah-scratch/CLAUDE.md")
    with open(os.path.join(scratch, "CLAUDE.md")) as f:
        check("scratch" in f.read().lower(), "scratch CLAUDE.md is the scratch template")
    with open(os.path.join(tmp, "hub.json")) as f:
        check(json.load(f).get("dir") == hub, "hub.json remembers the hub dir")
    with open(os.path.join(hub, ".mcp.json")) as f:
        mcp = json.load(f)
    peer = mcp.get("mcpServers", {}).get("peer", {})
    shim_path = os.path.normpath(os.path.join(os.path.dirname(module), "..", "shim", "peer.py"))
    check(peer.get("command") == "python3" and peer.get("args") == [os.path.realpath(shim_path)],
          f"hub .mcp.json points at the shim by absolute path ({peer})")
    check(all(os.path.isabs(v) for v in peer.get("env", {}).values()), "hub .mcp.json env paths are absolute")
    with open(os.path.join(hub, "CLAUDE.md")) as f:
        md = f.read()
    for word in ("work_new", "work_list", "work_output", "work_say", "work_rm", "HUB"):
        check(word in md, f"hub CLAUDE.md mentions {word}")
    check("—" not in md, "hub CLAUDE.md has no em dashes")
    h = run_json(["hub", hub, "--no-launch", "--json"])
    check(h.get("mcp_status") == "unchanged" and h.get("claude_md_status") == "kept"
          and h.get("scratch_status") == "kept" and h.get("settings_status") == "unchanged",
          f"hub second run changes nothing ({h})")
    with open(settings_path, "w") as f:
        json.dump({"model": "x", "permissions": {"allow": ["Bash(git status)"], "deny": ["WebFetch"]}}, f)
    h = run_json(["hub", hub, "--no-launch", "--json"])
    check(h.get("settings_status") == "updated", "hub merges into an existing settings.json")
    with open(settings_path) as f:
        st = json.load(f)
    check(st.get("model") == "x" and st["permissions"].get("deny") == ["WebFetch"]
          and st["permissions"]["allow"][0] == "Bash(git status)" and "mcp__peer__work_say" in st["permissions"]["allow"]
          and st.get("enableAllProjectMcpServers") is True, f"hub settings merge keeps everything else ({st})")
    with open(settings_path, "w") as f:
        f.write("[1, 2")
    p = run(["hub", hub, "--no-launch"])
    check(p.returncode == 1 and "malformed" in p.stderr, "hub refuses a malformed settings.json")
    os.remove(settings_path)
    for word in ("EVERY request", ".savannah-scratch", "sleep 5", "work_output", "waiting", "offline"):
        check(word in md, f"hub CLAUDE.md covers {word!r}")
    with open(os.path.join(hub, "CLAUDE.md"), "w") as f:
        f.write("mine\n")
    with open(os.path.join(hub, ".mcp.json"), "w") as f:
        json.dump({"mcpServers": {"other": {"command": "x"}, "peer": {"command": "stale"}}, "keep": 1}, f)
    p = run(["hub", hub, "--no-launch"])
    check(p.returncode == 0 and "updated" in p.stdout and "kept" in p.stdout,
          f"hub merges into an existing .mcp.json and keeps CLAUDE.md ({p.stdout!r})")
    with open(os.path.join(hub, ".mcp.json")) as f:
        mcp = json.load(f)
    check(mcp.get("keep") == 1 and mcp["mcpServers"].get("other") == {"command": "x"}
          and mcp["mcpServers"]["peer"] == peer, "hub keeps other servers and keys, replaces a stale peer")
    with open(os.path.join(hub, "CLAUDE.md")) as f:
        check(f.read() == "mine\n", "hub never overwrites an existing CLAUDE.md")
    with open(os.path.join(hub, ".mcp.json"), "w") as f:
        f.write("{broken")
    p = run(["hub", hub, "--no-launch"])
    check(p.returncode == 1 and "malformed" in p.stderr, "hub refuses a malformed .mcp.json")
    with open(os.path.join(hub, ".mcp.json")) as f:
        check(f.read() == "{broken", "hub leaves the malformed file alone")
    os.remove(os.path.join(hub, ".mcp.json"))
    # no DIR: the remembered hub wins over the cwd
    elsewhere = os.path.join(tmp, "elsewhere")
    os.makedirs(elsewhere)
    h = run_json(["hub", "--no-launch", "--json"], cwd=elsewhere)
    check(h.get("dir") == hub and h.get("picked") == "remembered" and h.get("mcp_status") == "created",
          f"hub without DIR opens the remembered hub ({h})")
    check(not os.path.exists(os.path.join(elsewhere, ".mcp.json")), "remembered hub: cwd untouched")
    # no DIR and nothing remembered (or a stale memory): the cwd
    with open(os.path.join(tmp, "hub.json"), "w") as f:
        json.dump({"dir": os.path.join(tmp, "vanished")}, f)
    h = run_json(["hub", "--no-launch", "--json"], cwd=elsewhere)
    check(os.path.realpath(h.get("dir", "")) == os.path.realpath(elsewhere) and h.get("picked") == "cwd",
          f"hub without DIR and a stale memory falls back to the cwd ({h})")
    with open(os.path.join(tmp, "hub.json")) as f:
        check(os.path.realpath(json.load(f).get("dir")) == os.path.realpath(elsewhere), "and remembers the cwd")
    os.remove(os.path.join(tmp, "hub.json"))
    h = run_json(["hub", "--no-launch", "--json"], cwd=hub)
    check(os.path.realpath(h.get("dir", "")) == os.path.realpath(hub) and h.get("picked") == "cwd",
          "hub without DIR and no memory uses the cwd")
    # `hub DIR` re-points the memory
    run_json(["hub", hub, "--no-launch", "--json"])
    with open(os.path.join(tmp, "hub.json")) as f:
        check(json.load(f).get("dir") == hub, "hub DIR re-points hub.json")
    # launch: exec of $SAVANNAH_CLAUDE_BIN in the hub dir (a fake that records its cwd)
    fake_claude = os.path.join(tmp, "fake_claude.py")
    with open(fake_claude, "w") as f:
        f.write("import os, sys\nopen(sys.argv[1], 'w').write(os.getcwd())\nsys.exit(7)\n")
    marker = os.path.join(tmp, "launched.txt")
    p = run(["hub", hub], extra_env={"SAVANNAH_CLAUDE_BIN": f"{shlex.quote(sys.executable)} {shlex.quote(fake_claude)} {shlex.quote(marker)}"})
    check(p.returncode == 7 and "launching" in p.stdout, f"hub execs claude and its exit code is the result (rc {p.returncode})")
    with open(marker) as f:
        check(os.path.realpath(f.read()) == os.path.realpath(hub), "hub launches claude with cwd = hub dir")
    p = run(["hub", hub], extra_env={"SAVANNAH_CLAUDE_BIN": os.path.join(tmp, "no-such-claude")})
    check(p.returncode == 1 and "cannot launch" in p.stderr, "hub reports a missing claude binary")

    # ---- the C++ CLI entry points: savannah [hub|work|<nothing>] -----------
    cli = os.path.abspath(sys.argv[3]) if len(sys.argv) > 3 else ""
    if cli and os.path.isfile(cli) and os.access(cli, os.X_OK):
        def run_cli(args, extra_env=None, cwd=None):
            e = dict(env)
            e["SAVANNAH_WORK_PY"] = module
            if extra_env:
                e.update(extra_env)
            return subprocess.run([cli] + args, text=True, capture_output=True, env=e, cwd=cwd or tmp)
        p = run_cli(["work", "ls", "--json"])
        check(p.returncode == 0 and isinstance(json.loads(p.stdout or "x"), list) if p.returncode == 0 else False,
              f"savannah work ls --json runs the module (rc {p.returncode}: {p.stderr.strip()})")
        p = run_cli(["work"])
        check(p.returncode == 2, "savannah work with no verb is the module's usage error (2)")
        p = run_cli(["hub", hub, "--no-launch", "--json"])
        check(p.returncode == 0 and json.loads(p.stdout).get("dir") == hub,
              f"savannah hub DIR --no-launch forwards to the module ({p.stderr.strip()})")
        # Bare savannah is the board: it execs the dash (a fake here that
        # records its argv, cwd and SAVANNAH_BIN), `mesh` adds --mesh.
        fake_dash = os.path.join(tmp, "fake-dash.py")
        with open(fake_dash, "w") as f:
            f.write("#!" + sys.executable + "\nimport json, os, sys\n"
                    "open(os.environ['DASH_MARKER'], 'w').write(json.dumps("
                    "{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'bin': os.environ.get('SAVANNAH_BIN', '')}))\n"
                    "sys.exit(9)\n")
        os.chmod(fake_dash, 0o755)
        dash_marker = os.path.join(tmp, "dash.json")
        p = run_cli([], extra_env={"SAVANNAH_DASH_BIN": fake_dash, "DASH_MARKER": dash_marker}, cwd=elsewhere)
        check(p.returncode == 9 and os.path.exists(dash_marker), f"bare savannah execs the dash (rc {p.returncode}: {p.stderr.strip()})")
        if os.path.exists(dash_marker):
            with open(dash_marker) as f:
                d = json.load(f)
            check(d["argv"] == [] and os.path.realpath(d["cwd"]) == os.path.realpath(elsewhere),
                  f"bare savannah: dash in work mode, in the cwd ({d})")
            check(os.path.realpath(d["bin"]) == os.path.realpath(cli),
                  f"bare savannah tells the dash where the CLI is via SAVANNAH_BIN ({d['bin']})")
            os.remove(dash_marker)
        p = run_cli(["mesh", "--key", "k"], extra_env={"SAVANNAH_DASH_BIN": fake_dash, "DASH_MARKER": dash_marker,
                                                        "SAVANNAH_BIN": "/already/set"})
        with open(dash_marker) as f:
            d = json.load(f)
        check(p.returncode == 9 and d["argv"] == ["--mesh", "--key", "k"] and d["bin"] == "/already/set",
              f"savannah mesh execs the dash with --mesh and keeps a SAVANNAH_BIN already set ({d})")
        # Flags before any verb are the board's: `savannah --group` is the
        # dash with --group (the mesh verbs all start with a word).
        p = run_cli(["--group"], extra_env={"SAVANNAH_DASH_BIN": fake_dash, "DASH_MARKER": dash_marker})
        with open(dash_marker) as f:
            d = json.load(f)
        check(p.returncode == 9 and d["argv"] == ["--group"],
              f"savannah --group execs the dash with its flags ({d})")
        p = run_cli(["--help"], extra_env={"SAVANNAH_DASH_BIN": fake_dash, "DASH_MARKER": dash_marker})
        check(p.returncode == 2 and "usage: savannah" in p.stderr, "savannah --help is the CLI's usage, not the dash")
        p = run_cli([], extra_env={"SAVANNAH_DASH_BIN": os.path.join(tmp, "no-dash"), "PATH": tmp})
        check(p.returncode == 1 and "go build" in p.stderr and "SAVANNAH_DASH_BIN" in p.stderr,
              f"bare savannah without a dash says how to build it (rc {p.returncode}: {p.stderr.strip()})")
        p = run_cli(["work", "ls"], extra_env={"SAVANNAH_WORK_PY": os.path.join(tmp, "no-module.py")})
        check(p.returncode == 1 and "work module not found" in p.stderr, "savannah names a missing work module")
    else:
        print("test_work: savannah CLI not given or not built, CLI entry points skipped")

    # ---- title: a free-text title beside the name (the name stays the id) -------
    tt_dir = os.path.join(tmp, "titlewt")
    os.makedirs(tt_dir)
    clear_log(keep_state=True)
    j = run_json(["new", "tt1", "--no-worktree", "--repo", tt_dir, "--json", "--title", "  Fix The Parser  "])
    check(j.get("title") == "Fix The Parser", f"new --title stores the stripped title ({j.get('title')!r})")
    check(record("tt1").get("title") == "Fix The Parser", "the title is in record.json")
    j = run_json(["new", "tt2", "--no-worktree", "--repo", tt_dir, "--json"])
    check(j.get("title") == "" and "title" in record("tt2"), 'new without --title writes title: ""')
    p = run(["title", "tt1"])
    check(p.returncode == 0 and p.stdout == "tt1: Fix The Parser\n", f"title NAME prints it ({p.stdout!r})")
    check(run_json(["title", "tt1", "--json"]) == {"name": "tt1", "title": "Fix The Parser"}, "title --json")
    p = run(["title", "tt2"])
    check(p.returncode == 0 and p.stdout == "tt2: (no title)\n", f"title of a session without one ({p.stdout!r})")
    check(run_json(["title", "tt2", "--json"]) == {"name": "tt2", "title": ""}, "title --json without one: empty string")
    p = run(["title", "tt1", "Parser", "Fix,", "Take", "Two"])
    check(p.returncode == 0 and p.stdout == "tt1: title set: Parser Fix, Take Two\n",
          f"title TEXT... joins the words ({p.stdout!r})")
    check(record("tt1")["title"] == "Parser Fix, Take Two", "the new title is stored")
    j = run_json(["title", "tt1", "--json", "--", "-leading dash"])
    check(j == {"name": "tt1", "title": "-leading dash"}, f"text starting with - travels after -- ({j})")
    # (--json after the text, or -- before it: argparse stops filling a
    # nargs="*" positional at the first option, like status and note)
    check(run_json(["title", "tt1", "x" * 150, "--json"])["title"] == "x" * 120, "a title is cut to 120 chars")
    check(run_json(["title", "tt1", "first line\nsecond line", "--json"])["title"] == "first line",
          "only the first line of a title is kept")
    check(run_json(["title", "tt1", "\n  \n  Third try  \nmore", "--json"])["title"] == "Third try",
          "the first NON-BLANK line, stripped")
    p = run(["title", "tt1", "   "])
    check(p.returncode == 2 and "empty" in p.stderr,
          f"whitespace-only title is a usage error (rc {p.returncode}: {p.stderr.strip()})")
    check(run(["title", "tt1", "--clear", "x"]).returncode == 2, "title --clear with text is a usage error")
    p = run(["title", "tt1", "--clear"])
    check(p.returncode == 0 and p.stdout == "tt1: title cleared\n" and record("tt1")["title"] == "",
          f"title --clear ({p.stdout!r})")
    check(run_json(["title", "tt1", "--clear", "--json"]) == {"name": "tt1", "title": ""}, "title --clear --json")
    p = run(["title", "nope", "x"])
    check(p.returncode == 1 and "no such work session" in p.stderr, "title of an unknown name exits 1")
    check(run(["title", "nope"]).returncode == 1, "title read of an unknown name exits 1")
    # ls: a TITLE column right after NAME (clipped to 24, "-" when none); title in every --json row
    long_title = "A Rather Long Title That Will Not Fit In The Column"
    run_json(["title", "tt1", long_title, "--json"])
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check(by["tt1"]["title"] == long_title and by["tt2"]["title"] == "", "ls --json rows carry title")
    p = run(["ls"])
    head = p.stdout.splitlines()[0]
    check(head.split()[:3] == ["NAME", "TITLE", "BRANCH"], f"ls human has a TITLE column after NAME ({head})")
    row1 = [ln for ln in p.stdout.splitlines() if ln.startswith("tt1 ")][0]
    row2 = [ln for ln in p.stdout.splitlines() if ln.startswith("tt2 ")][0]
    check(long_title[:23] + "~" in row1, f"ls clips the title to 24 columns ({row1})")
    check(row2.split()[1] == "-", f"ls shows - for no title ({row2})")
    # the normalizer: a record from before titles, or with a non-string one, reads ""
    rp = os.path.join(workroot, "tt2", "record.json")
    with open(rp) as f:
        raw = json.load(f)
    del raw["title"]
    with open(rp, "w") as f:
        json.dump(raw, f)
    check(run_json(["title", "tt2", "--json"])["title"] == "", "a record without the field reads an empty title")
    raw["title"] = 42
    with open(rp, "w") as f:
        json.dump(raw, f)
    check(run_json(["title", "tt2", "--json"])["title"] == "", "a non-string title reads empty")
    raw["title"] = "  Kept\nnot this"
    with open(rp, "w") as f:
        json.dump(raw, f)
    by = {e["name"]: e for e in run_json(["ls", "--json"])}
    check(by["tt2"]["title"] == "Kept", f"ls normalizes a stored title: first line, stripped ({by['tt2']['title']!r})")
    # rename keeps it; adopt --title sets it, adopt without one writes ""
    run_json(["title", "tt1", "--json", "--", "Survives Rename"])
    check(run(["rename", "tt1", "tt1b"]).returncode == 0
          and run_json(["title", "tt1b", "--json"])["title"] == "Survives Rename", "rename keeps the title")
    for sid, pane in (("adopt-title", "w0t0p0:TT-PANE"), ("adopt-notitle", "")):
        with open(os.path.join(sessions, sid + ".json"), "w") as f:
            json.dump({"session_id": sid, "project": tt_dir, "state": "idle",
                       "iterm_session_id": pane, "updated": int(time.time())}, f)
    j = run_json(["adopt", "tt3", "--session=adopt-title", "--json", "--title=Adopted Tab"])
    check(j.get("title") == "Adopted Tab" and j.get("adopted") is True, f"adopt --title ({j})")
    j = run_json(["adopt", "tt4", "--session=adopt-notitle", "--json"])
    check(j.get("title") == "" and "title" in j, 'adopt without --title writes title: ""')
    check(sw.title_text(None) == "" and sw.title_text("  a  ") == "a" and sw.title_text("\n\n") == ""
          and sw.title_text("y" * 200) == "y" * 120 and sw.TITLE_MAX == 120, "title_text: str, first non-blank line, 120")

    # ---- log: savannah's one log; every verb writes it, `log` reads it ----------
    log_file = os.path.join(logdir, "savannah.log")
    check(os.path.isfile(log_file), f"verbs write {log_file} (SAVANNAH_LOG_DIR)")

    def log_lines():
        try:
            with open(log_file, encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()
        except OSError:
            return []

    stamp_re = re.compile(r"^time=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d "
                          r"level=(VERBOSE|DEBUG|INFO|WARN|ERROR) msg=")
    lines = log_lines()
    bad = [ln for ln in lines if not stamp_re.match(ln)]
    check(lines and not bad, f"every line opens with an RFC 3339 ms stamp and a level ({bad[:2]})")
    check(all(" src=work" in ln for ln in lines), "every line of the work module is stamped src=work")
    check(any(" level=INFO msg=verb src=work verb=ls args=" in ln and " cwd=" in ln for ln in lines),
          "a verb writes an INFO line: msg=verb, verb=, args=, cwd=")
    check(any(" level=DEBUG msg=it2 src=work args=" in ln and " rc=" in ln and " ms=" in ln for ln in lines),
          "an it2 call writes a DEBUG line with its rc and ms")
    check(any(' level=VERBOSE msg="it2 output" src=work' in ln for ln in lines), "what it2 printed lands at VERBOSE")
    check(any(' level=WARN msg="verb refused" src=work verb=title error="no such work session: nope"' in ln
              for ln in lines), "a refused verb writes a WARN line with the message")
    check(any(' level=INFO msg="title set" src=work name=tt1 title=' in ln for ln in lines),
          "a state change (title set) is INFO")
    check(any(" level=DEBUG msg=join src=work name=" in ln for ln in lines), "the join of a record is DEBUG")
    check(any(" level=DEBUG msg=verb" in ln or ' level=DEBUG msg="verb done" src=work verb=' in ln for ln in lines),
          "a finished verb is DEBUG")
    if have_git:
        check(any(" level=DEBUG msg=git src=work args=" in ln and " rc=" in ln for ln in lines), "every git call is DEBUG")
    sl = sw.savannah_log
    check(sl.log_quote("plain") == "plain" and sl.log_quote(["a", "b"]) == '"[a b]"' and sl.log_quote(["one"]) == '"[one]"'
          and sl.log_quote("has space") == '"has space"' and sl.log_quote("") == '""' and sl.log_quote(7) == "7"
          and sl.log_quote("/a/b.c:1") == "/a/b.c:1",
          "log_quote: a plain word bare, anything else JSON-quoted, a list as [a b]")
    check(sl.line_level("time=x level=DEBUG msg=y") == "DEBUG" and sl.line_level("no stamp here") == ""
          and sl.line_level('time=x level=INFO msg="level=VERBOSE"') == "INFO", "line_level reads the stamp")
    check(sl.shows("level=DEBUG", "normal") is False and sl.shows("level=DEBUG", "debug") is True
          and sl.shows("level=VERBOSE", "debug") is False and sl.shows("level=VERBOSE", "verbose") is True
          and sl.shows("no stamp", "normal") is True and sl.shows("level=INFO", "normal") is True
          and sl.shows("level=WARN", "normal") is True,
          "shows: normal hides DEBUG and VERBOSE, debug hides VERBOSE, verbose nothing, no stamp always shows")
    check(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d", sl.log_stamp(1700000000.5)) is not None,
          "log_stamp is RFC 3339 local time with milliseconds")
    # the reader
    p = run(["log"])
    shown = p.stdout.splitlines()
    noisy = [ln for ln in shown if " level=DEBUG " in ln or " level=VERBOSE " in ln]
    check(p.returncode == 0 and shown and not noisy and len(shown) <= 200,
          f"log (normal) shows INFO, WARN and ERROR only, 200 lines at most ({noisy[:1]})")
    p = run(["log", "--level", "debug"])
    shown = p.stdout.splitlines()
    check(any(" level=DEBUG " in ln for ln in shown) and not any(" level=VERBOSE " in ln for ln in shown),
          "log --level debug adds DEBUG and hides VERBOSE")
    p = run(["log", "--level", "verbose"])
    check(any(" level=VERBOSE " in ln for ln in p.stdout.splitlines()), "log --level verbose shows everything")
    expect = [ln for ln in log_lines() if " level=DEBUG " not in ln and " level=VERBOSE " not in ln][-3:]
    p = run(["log", "--lines", "3"])
    check(p.stdout.splitlines() == expect, f"log --lines 3 is the last 3 passing lines, oldest first ({p.stdout!r})")
    p = run(["log", "--path"])
    check(p.stdout == log_file + "\n", f"log --path prints only the path ({p.stdout!r})")
    j = run_json(["log", "--json", "--lines", "2", "--level", "debug"])
    check(set(j) == {"path", "level", "lines"} and j["path"] == log_file and j["level"] == "debug"
          and len(j["lines"]) == 2 and all(isinstance(ln, str) and ln.startswith("time=") for ln in j["lines"]),
          f"log --json is {{path, level, lines}} ({j})")
    for bad_args in (["log", "--lines", "0"], ["log", "--level", "loud"], ["log", "--follow", "--json"]):
        check(run(bad_args).returncode == 2, f"{' '.join(bad_args)} is a usage error")
    own = [ln for ln in log_lines() if " verb=log " in ln]
    check(own and all(" level=VERBOSE " in ln for ln in own), f"reading the log logs only at VERBOSE ({own[:1]})")
    p = run(["log", "--path"], {"SAVANNAH_LOG_DIR": os.path.join(tmp, "elsewhere")})
    check(p.stdout == os.path.join(tmp, "elsewhere", "savannah.log") + "\n", "SAVANNAH_LOG_DIR moves the file")
    # a log directory that cannot be made: the verb works, nothing extra is printed
    p = run(["title", "tt2"], {"SAVANNAH_LOG_DIR": os.path.join(log, "under-a-file")})
    check(p.returncode == 0 and p.stderr == "" and p.stdout == "tt2: Kept\n",
          f"an unwritable SAVANNAH_LOG_DIR: the verb runs, nothing extra printed ({p.stderr.strip()!r})")
    # --follow: a line written after the start shows up; Ctrl-C (SIGINT) ends it with exit 0
    import select
    import signal
    fp = subprocess.Popen([sys.executable, module, "log", "--follow", "--lines", "1"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, env=env, cwd=tmp,
                          preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
    time.sleep(0.7)
    run(["title", "tt2", "--json"])   # one INFO verb line, after the follow started
    got = b""
    deadline = time.time() + 8
    while time.time() < deadline and b"verb=title args=\"[title tt2 --json]\"" not in got:
        ready, _, _ = select.select([fp.stdout], [], [], 0.5)
        if ready:
            got += os.read(fp.stdout.fileno(), 65536)
    fp.send_signal(signal.SIGINT)
    try:
        fp.wait(timeout=10)
    except subprocess.TimeoutExpired:
        fp.kill()
        fp.wait()
    rest, err = fp.communicate()
    got += rest or b""
    check(b'verb=title args="[title tt2 --json]"' in got, f"log --follow prints a line written after it started ({got[-300:]!r})")
    check(fp.returncode == 0 and not err, f"Ctrl-C ends log --follow with exit 0 and no noise (rc {fp.returncode}, {err!r})")

    shutil.rmtree(tmp, ignore_errors=True)
    if failures == 0:
        print("test_work: all passed" + ("" if have_git else " (worktree cases skipped)"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
