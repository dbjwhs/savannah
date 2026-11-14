# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# What the Python test scripts share: the check() counter and its summary
# line, the loader for the work module, and the Harness that drives
# work/savannah_work.py against a temp work dir, a temp sessions dir and the
# fake it2 (tools/fake_it2.py). Stdlib only. Not a test itself: each
# test_*.py still runs as a plain script and imports this after putting its
# own directory on sys.path:
#   sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile

# ---- check(): the one assertion every script uses --------------------------

_checks_run = 0
_checks_failed = 0
_fail_prefix = "FAIL: "
_ok_prefix = None


def check(cond, label):
    """Count one check; a failing one prints its label and the run goes on."""
    global _checks_run, _checks_failed
    _checks_run += 1
    if not cond:
        print(f"{_fail_prefix}{label}")
        _checks_failed += 1
    elif _ok_prefix is not None:
        print(f"{_ok_prefix}{label}")


def check_format(fail_prefix="FAIL: ", ok_prefix=None):
    """What check() prints: the prefix of a failure, and of a pass when a
    script wants its passes listed too (None = passes are silent)."""
    global _fail_prefix, _ok_prefix
    _fail_prefix, _ok_prefix = fail_prefix, ok_prefix


def failures():
    """How many checks have failed so far."""
    return _checks_failed


def checks_run():
    """How many checks have run so far."""
    return _checks_run


def finish(name, passed="all passed", count=False):
    """Print the script's last line and return its exit code.

    A pass prints "<name>: <passed>" and returns 0. A failure returns 1 and
    prints nothing more (every failed check already printed its own line);
    with count=True it prints "<name>: N failure(s)" and returns N."""
    if not _checks_failed:
        print(f"{name}: {passed}")
        return 0
    if count:
        print(f"{name}: {_checks_failed} failure(s)")
        return _checks_failed
    return 1


# ---- the work module, in process --------------------------------------------

def load_module(path, name="savannah_work_under_test"):
    """Import work/savannah_work.py (or any module) from its path. The caller
    gets the module object. savannah_work.py is the entry point of a set of
    flat sibling modules, which it imports by plain name and re-exports from,
    so a test patches a function on the module that owns it
    (sw.savannah_core.atomic_write, sw.savannah_terminal.subprocess.run) and
    puts it back: a patch on the entry module would not reach the code."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the harness -------------------------------------------------------------

# Variables that would point the module under test, or the fake it2, at
# something of the user's: never inherited from the environment of the run.
SCRUBBED_ENV = ("SAVANNAH_WORK_PY", "SAVANNAH_DASH_BIN", "SAVANNAH_CLAUDE_BIN", "SAVANNAH_BIN",
                "FAKE_IT2_MISSING", "FAKE_IT2_FAIL", "FAKE_IT2_NO_WINDOW")


class Harness:
    """One temp world for the work module: its work dir, its sessions dir (the
    hook files a test writes by hand), savannah's log dir and the fake it2's
    call log, plus the environment that points the module at all of them.

    A test's sections share one Harness, in order: the sessions, the fake's
    panes and the files one section leaves behind are what the next starts
    from. Values a later section reads from an earlier one are attributes of
    the harness (a subclass names them)."""

    def __init__(self, module, fake, prefix):
        self.module = os.path.abspath(module)
        self.fake = os.path.abspath(fake)
        self.tmp = tempfile.mkdtemp(prefix=prefix)
        self.workroot = os.path.join(self.tmp, "work")
        self.sessions = os.path.join(self.tmp, "sessions")
        self.logdir = os.path.join(self.tmp, "log")          # savannah's one log, never the user's
        self.it2_log = os.path.join(self.tmp, "it2.log")
        os.makedirs(self.sessions)

        env = dict(os.environ)
        env["SAVANNAH_WORK_DIR"] = self.workroot
        env["SAVANNAH_SESSIONS_DIR"] = self.sessions
        env["SAVANNAH_LOG_DIR"] = self.logdir
        env["SAVANNAH_IT2"] = f"{shlex.quote(sys.executable)} {shlex.quote(self.fake)}"
        env["SAVANNAH_HUB_FILE"] = os.path.join(self.tmp, "hub.json")
        for k in SCRUBBED_ENV:
            env.pop(k, None)
        env["FAKE_IT2_LOG"] = self.it2_log
        # The module names the window of the session it runs in; the fake never
        # lists this one, so it falls through to the fake's own window.
        env["ITERM_SESSION_ID"] = "w9t9p9:NOT-IN-FAKE"
        env["HOME"] = self.tmp
        self.env = env

    def isolate_git(self):
        """The user's git config must not leak into a throwaway repo (hooks,
        signing, templates, default branch...), and identity is explicit.
        HOME is the temp dir already."""
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            self.env[k] = "t"
        for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            self.env[k] = "t@example.com"
        for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.env.pop(k, None)

    # -- the module as a command --

    def run(self, args, extra_env=None, stdin=None, cwd=None):
        """Run the work module with args; the CompletedProcess (text mode)."""
        e = dict(self.env)
        if extra_env:
            e.update(extra_env)
        return subprocess.run([sys.executable, self.module] + args, input=stdin,
                              text=True, capture_output=True, env=e, cwd=cwd or self.tmp)

    def run_json(self, args, extra_env=None, cwd=None, rc=0):
        """Run it, check the exit code (rc) and return its stdout parsed as
        JSON ({} and a failed check when it is not JSON)."""
        p = self.run(args, extra_env, cwd=cwd)
        check(p.returncode == rc, f"{' '.join(args)} exits {rc} (got {p.returncode}, stderr: {p.stderr.strip()})")
        try:
            return json.loads(p.stdout)
        except ValueError:
            check(False, f"{' '.join(args)} emits JSON (got {p.stdout!r})")
            return {}

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, env=self.env)

    # -- the fake it2 --

    def it2(self, *args):
        """Drive the fake directly: what iTerm2 itself does to its world
        (closing panes when it quits)."""
        p = subprocess.run([sys.executable, self.fake] + list(args), text=True, capture_output=True, env=self.env)
        check(p.returncode == 0, f"fake it2 {' '.join(args)} ({p.stderr.strip()})")
        return p.stdout

    def read_log(self):
        """The fake's call log as text ("" when it saw no call)."""
        try:
            with open(self.it2_log) as f:
                return f.read()
        except OSError:
            return ""

    def calls(self):
        """Every it2 argv the fake saw since clear_log, in order."""
        return [json.loads(ln) for ln in self.read_log().splitlines() if ln.strip()]

    def called(self, prefix, last=None):
        """Index of the first call starting with prefix (and ending with last
        when given), or -1."""
        for i, c in enumerate(self.calls()):
            if c[:len(prefix)] == list(prefix) and (last is None or c[-1] == last):
                return i
        return -1

    def clear_log(self, keep_state=False):
        # The state file is the fake's world (its counter and live panes):
        # clearing it restarts ids at TAB-1 / PANE-C-1 / PANE-S-1. keep_state
        # drops only the call log, so the next checks see one command's calls
        # while the panes made so far stay alive.
        for p in ((self.it2_log,) if keep_state else (self.it2_log, self.it2_log + ".state")):
            try:
                os.remove(p)
            except OSError:
                pass

    # -- records on disk --

    def record(self, name):
        with open(os.path.join(self.workroot, name, "record.json")) as f:
            return json.load(f)

    def write_record(self, name, rec):
        os.makedirs(os.path.join(self.workroot, name), exist_ok=True)
        with open(os.path.join(self.workroot, name, "record.json"), "w") as f:
            json.dump(rec, f)
