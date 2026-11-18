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
# One thing it2 cannot do, putting a tab into a window that already exists,
# goes to iTerm2's API directly (savannah_iterm_api.py, stdlib too).
#
# Imports: the standard library, savannah_log, savannah_core and
# savannah_iterm_api. Every it2
# call goes through ITerm.run and this module's subprocess.run, which is
# what a test patches (savannah_terminal.subprocess.run).

import json
import os
import re
import shlex
import shutil
import subprocess
import time

from savannah_log import log_debug, log_info, log_verbose, log_warn
from savannah_core import iterm_uuid, oserr, UserError
import savannah_iterm_api

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

    def create_split_tab(self, name, upper_cmd, lower_cmd, detached=False, background=False):
        """-> {"tab_id", "claude_pane", "shell_pane"}; raises UserError.
        detached: in a window of its own instead of a tab in the group.
        background: the tab (and pane) the window showed before stays the
        one it shows, so a script making tabs never takes the keyboard."""
        raise NotImplementedError

    def tab_of(self, pane_id):
        """Where a pane sits. -> {"tab_id", "window_id", "alone"} (alone:
        its tab is the only one in its window), or None when the terminal
        does not list the pane; raises UserError."""
        raise NotImplementedError

    def group_window(self):
        """The window the tabs are grouped in: the one this process runs
        in (the board's). "" when there is none; raises UserError."""
        raise NotImplementedError

    def attach_tab(self, pane_id, window=""):
        """Move the pane's tab into window (default: group_window()), as
        its last tab. -> {"tab_id", "window_id"}; raises UserError (pane
        gone, already there, the terminal refused)."""
        raise NotImplementedError

    def detach_tab(self, pane_id):
        """Move the pane's tab out into a new window of its own.
        -> {"tab_id", "window_id"}; raises UserError (pane gone, already
        alone in its window, the terminal refused)."""
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

    def run(self, args, timeout=60, secret=False):
        """secret: the output is a credential, so it is never logged."""
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
        if (p.stdout or p.stderr) and not secret:
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

    def ok(self, args, what, secret=False):
        """Run and insist on exit 0; anything else is a UserError."""
        p = self.run(args, secret=secret)
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

    # One line per pane: its id, its tab, the pane that tab has the keyboard
    # in, and the tab its window shows (iTerm2's own variables, pinned live
    # on 11/17/2026 against iTerm2 3.7.7).
    SHOWN_FORMAT = "\\(id)|\\(tab.id)|\\(tab.currentSession.id)|\\(tab.window.currentTab.id)"

    def shown_pane(self, window):
        """The pane the window's selected tab has the keyboard in, "" when
        it cannot be told (never raises: this only serves background)."""
        try:
            p = self.run(["session", "list", "--json", "--format", self.SHOWN_FORMAT])
            if p.returncode != 0:
                return ""
            for s in self._json(p, "session list"):
                parts = str(s.get("formatted") or "").split("|")
                if s.get("window_id") == window and len(parts) == 4 and parts[1] and parts[1] == parts[3]:
                    return parts[2]
        except UserError:
            pass
        return ""

    def reshow(self, pane_id):
        """Select the pane's tab again without bringing iTerm2 forward.
        Best effort: a tab left selected is a nuisance, not a failure."""
        if not pane_id:
            return False
        try:
            return self.run(["session", "focus", pane_id]).returncode == 0
        except UserError:
            return False

    def _new_tab(self, detached=False, background=False):
        """A fresh tab in the window pick_window names, or (detached) in a
        new window of its own. -> (tab id, the id of the pane it opened
        with, the pane to show again afterwards: "" unless background)."""
        window = "" if detached else self.pick_window(self.sessions())
        shown = self.shown_pane(window) if background and window else ""
        if window:
            tab = self._created(self.ok(["tab", "new", "--window", window], "tab new"), "tab new")
            fresh = [s for s in self.sessions()
                     if s.get("tab_id") == tab and s.get("window_id") == window]
        else:
            # No window at all, or a detached session: the new window's own
            # tab is the work tab, so no stray empty tab is left behind.
            window = self._created(self.ok(["window", "new"], "window new"), "window new")
            fresh = [s for s in self.sessions() if s.get("window_id") == window]
            tab = fresh[0].get("tab_id", "") if fresh else ""
        if not fresh:
            raise UserError("it2 created the tab but session list does not show it")
        return tab, fresh[0]["id"], shown

    def create_tab(self, name, cmd):
        # One pane, no split: the shape of a tab the owner opened by hand,
        # which is what restore puts back. The leading space and the
        # best-effort set-name are explained in create_split_tab.
        tab, cs, _shown = self._new_tab()
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

    def create_split_tab(self, name, upper_cmd, lower_cmd, detached=False, background=False):
        tab, cs, shown = self._new_tab(detached, background)
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
            self.reshow(shown)
            raise
        self.reshow(shown)
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

    # -- the tab group: which window a tab sits in --

    @staticmethod
    def _window_tabs(sessions, window):
        """The window's tab ids, in tab order (session list is in tab
        order; a split tab lists once per pane)."""
        tabs = []
        for s in sessions:
            tab = str(s.get("tab_id") or "")
            if s.get("window_id") == window and tab and tab not in tabs:
                tabs.append(tab)
        return tabs

    def _locate(self, sessions, pane_id):
        for s in sessions:
            if s.get("id") == pane_id and s.get("tab_id") and s.get("window_id"):
                tab, window = str(s["tab_id"]), s["window_id"]
                return {"tab_id": tab, "window_id": window,
                        "alone": self._window_tabs(sessions, window) == [tab]}
        return None

    def tab_of(self, pane_id):
        return self._locate(self.sessions(), pane_id)

    def group_window(self):
        return self.pick_window(self.sessions())

    def _cookie(self):
        """A single-use cookie and key for iTerm2's API, from it2 itself
        ("ITERM2_COOKIE=... ITERM2_KEY=..."). No prompt appears in iTerm2
        for it (live 11/14/2026, 3.7.7)."""
        p = self.ok(["auth", "cookie", "--single-use"], "auth cookie", secret=True)
        pairs = dict(tok.split("=", 1) for tok in p.stdout.split() if "=" in tok)
        cookie, key = pairs.get("ITERM2_COOKIE", ""), pairs.get("ITERM2_KEY", "")
        if not cookie or not key:
            raise UserError("it2 auth cookie printed no cookie and key")
        return cookie, key

    def attach_tab(self, pane_id, window=""):
        sessions = self.sessions()
        here = self._locate(sessions, pane_id)
        if here is None:
            raise UserError("that pane is not in iTerm2 any more")
        target = window or self.pick_window(sessions)
        if not target:
            raise UserError("there is no iTerm2 window to attach the tab to")
        if here["window_id"] == target:
            raise UserError("that tab is already in the group window")
        tabs = self._window_tabs(sessions, target)
        if not tabs:
            raise UserError(f"iTerm2 has no window {target}")
        # The window's own tabs first, in their order, then the newcomer: it
        # lands last, where a new tab would.
        cookie, key = self._cookie()
        savannah_iterm_api.reorder_tabs(target, tabs + [here["tab_id"]], cookie, key)
        now = self.tab_of(pane_id)
        if now is None or now["window_id"] != target:
            raise UserError("iTerm2 accepted the move but the tab is not in the group window")
        log_info("tab attached", pane=pane_id, tab=now["tab_id"], window=target, was=here["window_id"])
        return {"tab_id": now["tab_id"], "window_id": target}

    def detach_tab(self, pane_id):
        here = self.tab_of(pane_id)
        if here is None:
            raise UserError("that pane is not in iTerm2 any more")
        if here["alone"]:
            raise UserError("that tab is already alone in its own window")
        self.ok(["tab", "move", here["tab_id"]], "tab move")
        now = self.tab_of(pane_id)
        if now is None or now["window_id"] == here["window_id"]:
            raise UserError("it2 said the tab moved but it is still in the same window")
        log_info("tab detached", pane=pane_id, tab=now["tab_id"], window=now["window_id"],
                 was=here["window_id"])
        return {"tab_id": now["tab_id"], "window_id": now["window_id"]}


def make_terminal(kind):
    if kind in ("iterm", ""):
        return ITerm()
    raise UserError(f"terminal {kind!r} is not implemented (only iterm today)")
