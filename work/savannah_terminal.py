# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_terminal - the terminal seam of local work sessions
# (savannah_work.py is the entry point; this is a flat sibling module).
#
# The Terminal class is the only thing that knows about iTerm2. ITerm
# shells out to iTerm2's own `it2` command ($SAVANNAH_IT2, else `it2` on
# PATH, else the copy inside iTerm2.app) so tests substitute
# tools/fake_it2.py. A tmux implementation plugs in later behind the same
# methods. make_terminal(kind) is how every caller gets one.
#
# Imports: the standard library, savannah_log and savannah_core. Every it2
# call goes through ITerm.run and this module's subprocess.run, which is
# what a test patches (savannah_terminal.subprocess.run).

import json
import os
import re
import shlex
import shutil
import subprocess
import time

from savannah_log import log_debug, log_verbose, log_warn
from savannah_core import iterm_uuid, oserr, UserError

IT2_DEFAULT = "/Applications/iTerm.app/Contents/Resources/utilities/it2"  # inside iTerm2 3.5+


def it2_argv():
    """argv prefix for iTerm2's own `it2` command: $SAVANNAH_IT2 (split like a
    shell line), else `it2` on PATH, else the copy inside iTerm2.app."""
    override = os.environ.get("SAVANNAH_IT2", "")
    if override:
        argv = shlex.split(override)
        if not argv or not argv[0]:
            raise UserError("SAVANNAH_IT2 is empty")
        return argv
    return [shutil.which("it2") or IT2_DEFAULT]


class Terminal:
    """The seam. One implementation today (ITerm); tmux is reserved."""

    kind = ""

    def create_split_tab(self, name, upper_cmd, lower_cmd):
        """-> {"tab_id", "claude_pane", "shell_pane"}; raises UserError."""
        raise NotImplementedError

    def create_tab(self, name, cmd):
        """A single-pane tab running cmd (a plain tab put back by restore).
        -> {"tab_id", "pane"}; raises UserError."""
        raise NotImplementedError

    def write(self, pane_id, text):
        """Type text (plus Enter) into a pane. -> bool found."""
        raise NotImplementedError

    def focus(self, pane_id):
        """Bring a pane's window/tab/pane forward. -> bool found."""
        raise NotImplementedError

    def close_tab(self, tab_id, panes=()):
        """Close the tab. -> bool found. panes: the tab's known pane ids."""
        raise NotImplementedError

    def sessions(self):
        """The terminal's panes, one dict each with at least "id" (plus
        "tab_id", "window_id" and "title" where it knows them), in tab
        order; raises UserError."""
        raise NotImplementedError

    def live_ids(self):
        """The set of pane ids the terminal currently has; raises UserError."""
        raise NotImplementedError


class ITerm(Terminal):
    """iTerm2 driven through its own `it2` command (ships inside iTerm2 3.5+
    and needs the Python API switched on: iTerm2 > Settings > General >
    Magic). Each verb is one or more it2 calls, about a quarter second each.
    The output lines parsed here are the contract, pinned live on 11/3/2026
    against iTerm2 3.7.6 / it2 1.0.0 and mirrored by tools/fake_it2.py."""

    kind = "iterm"

    def run(self, args, timeout=60):
        argv = it2_argv() + list(args)
        t0 = time.time()
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            log_warn("it2 not found", it2=argv[0])
            raise UserError(f"it2 not found ({argv[0]!r}); it ships with iTerm2 3.5+ "
                            "(set SAVANNAH_IT2 to its path)")
        except subprocess.TimeoutExpired:
            log_warn("it2 timed out", args=list(args), timeout=timeout)
            raise UserError("it2 timed out: iTerm2 is not responding, or its Python API is "
                            "off (iTerm2 > Settings > General > Magic > Enable Python API)")
        except OSError as e:
            log_warn("it2 failed to start", it2=argv[0], error=str(oserr(e)))
            raise UserError(f"cannot run it2 ({argv[0]!r}): {oserr(e)}")
        ms = int((time.time() - t0) * 1000)
        # DEBUG for every call (what, how it ended, how long), VERBOSE for what
        # it printed: `session list` JSON is big, so only its head.
        log_debug("it2", args=list(args), rc=p.returncode, ms=ms)
        if p.stdout or p.stderr:
            log_verbose("it2 output", args=list(args)[:2], stdout=p.stdout[:300], stderr=p.stderr[:200])
        return p

    @staticmethod
    def _err(p):
        lines = [ln for ln in p.stderr.strip().splitlines() if ln.strip()]
        return lines[-1] if lines else f"rc {p.returncode}"

    @staticmethod
    def _not_found(p):
        # "Error: Session not found" (rc 3), "Error: Session 'X' not found"
        # (rc 3), "Set name failed: Object with identifier ... not found" (rc 1).
        return p.returncode != 0 and "not found" in p.stderr.lower()

    def ok(self, args, what):
        """Run and insist on exit 0; anything else is a UserError."""
        p = self.run(args)
        if p.returncode != 0:
            raise UserError(f"it2 {what} failed: {self._err(p)}")
        return p

    @staticmethod
    def _json(p, what):
        try:
            data = json.loads(p.stdout or "[]")
        except ValueError:
            raise UserError(f"it2 {what} printed something that is not JSON")
        return [d for d in data if isinstance(d, dict) and d.get("id")] if isinstance(data, list) else []

    def sessions(self):
        return self._json(self.ok(["session", "list", "--json"], "session list"), "session list")

    def live_ids(self):
        return {s["id"] for s in self.sessions()}

    def pick_window(self, sessions):
        """The window the new tab goes in. it2 has no "current window" when
        called from a script, so it is named explicitly: the window this
        process runs in (ITERM_SESSION_ID) when it is still there, else the
        first listed session's, else the first window, else "" (none)."""
        mine = iterm_uuid(os.environ.get("ITERM_SESSION_ID", ""))
        for s in sessions:
            if mine and s.get("id") == mine and s.get("window_id"):
                return s["window_id"]
        for s in sessions:
            if s.get("window_id"):
                return s["window_id"]
        for w in self._json(self.ok(["window", "list", "--json"], "window list"), "window list"):
            return w["id"]
        return ""

    @staticmethod
    def _created(p, what):
        """'Created new tab: 6' / 'Created new pane: <uuid>' / 'Created new window: <id>'."""
        m = re.search(r"Created new \w+: (\S+)", p.stdout)
        if not m:
            raise UserError(f"it2 {what} printed no id ({p.stdout.strip()!r})")
        return m.group(1)

    def _new_tab(self):
        """A fresh tab in the window pick_window names. -> (tab id, the id of
        the pane it opened with)."""
        window = self.pick_window(self.sessions())
        if window:
            tab = self._created(self.ok(["tab", "new", "--window", window], "tab new"), "tab new")
            fresh = [s for s in self.sessions()
                     if s.get("tab_id") == tab and s.get("window_id") == window]
        else:
            # No window at all: the new window's own tab is the work tab, so no
            # stray empty tab is left behind.
            window = self._created(self.ok(["window", "new"], "window new"), "window new")
            fresh = [s for s in self.sessions() if s.get("window_id") == window]
            tab = fresh[0].get("tab_id", "") if fresh else ""
        if not fresh:
            raise UserError("it2 created the tab but session list does not show it")
        return tab, fresh[0]["id"]

    def create_tab(self, name, cmd):
        # One pane, no split: the shape of a tab the owner opened by hand,
        # which is what restore puts back. The leading space and the
        # best-effort set-name are explained in create_split_tab.
        tab, cs = self._new_tab()
        try:
            self.ok(["session", "run", "--session", cs, "--", " " + cmd], "run")
            self.ok(["session", "set-name", "--session", cs, "--", name], "set-name")
        except BaseException:
            try:
                self.close_tab(tab, (cs,))
            except Exception:
                pass
            raise
        return {"tab_id": tab, "pane": cs}

    def create_split_tab(self, name, upper_cmd, lower_cmd):
        tab, cs = self._new_tab()
        ss = ""
        try:
            # The line is typed before the shell is up and waits in the tty
            # queue. A shell that reads a key while starting eats the first
            # character (oh-my-zsh's update prompt turned `cd` into `d`, live
            # 11/3/2026), so the line starts with a space nothing will miss;
            # zsh's hist_ignore_space keeps it out of history as a bonus.
            self.ok(["session", "run", "--session", cs, "--", " " + upper_cmd], "run")
            # After the run: the shell's first prompt resets the title (auto
            # title), so a name set before it is gone at once. Best effort
            # anyway; `claude -n <name>` names the tab while Claude runs.
            self.ok(["session", "set-name", "--session", cs, "--", name], "set-name")
            ss = self._created(self.ok(["session", "split", "--session", cs], "split"), "split")
            self.ok(["session", "run", "--session", ss, "--", " " + lower_cmd], "run")
        except BaseException:
            # Any failure after the tab exists closes it so no orphan survives.
            try:
                self.close_tab(tab, (cs, ss))
            except Exception:
                pass
            raise
        return {"tab_id": tab, "claude_pane": cs, "shell_pane": ss}

    def write(self, pane_id, text):
        # Two sends, never one `run`: `run` ends the text with a newline (LF),
        # which Claude Code's input box takes as a line break, not as Enter
        # (found live 11/3/2026: the text sat unsent in the box). A carriage
        # return is what the Enter key sends, and a shell accepts it too.
        # "--" so text that starts with "-" is text, never an it2 option.
        for payload in (text, "\r"):
            p = self.run(["session", "send", "--session", pane_id, "--", payload])
            if p.returncode == 0:
                continue
            if self._not_found(p):
                return False
            raise UserError(f"it2 send failed: {self._err(p)}")
        return True

    def focus(self, pane_id):
        p = self.run(["session", "focus", pane_id])
        if p.returncode != 0:
            if self._not_found(p):
                return False
            raise UserError(f"it2 focus failed: {self._err(p)}")
        self.run(["app", "activate"])  # best effort: bring iTerm2 forward too
        return True

    def close_tab(self, tab_id, panes=()):
        # `it2 tab close` answers "Tab closed" without closing anything (found
        # live 11/3/2026, it2 1.0.0), so the tab goes pane by pane; closing
        # the last pane closes the tab (and an otherwise empty window).
        found = False
        for pane in panes:
            if not pane:
                continue
            p = self.run(["session", "close", "--session", pane, "--force"])
            if p.returncode == 0:
                found = True
            elif not self._not_found(p):
                raise UserError(f"it2 close failed: {self._err(p)}")
        return found


def make_terminal(kind):
    if kind in ("iterm", ""):
        return ITerm()
    raise UserError(f"terminal {kind!r} is not implemented (only iterm today)")
