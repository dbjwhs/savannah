# work: local interactive work sessions (Phase 5d)

`work/savannah_work.py` turns a manual habit into a named, file-owned object:
one iTerm2 tab per work item, split into an upper pane running INTERACTIVE
Claude Code in a git worktree and a lower shell pane cd'd into the same
worktree, plus a plain text notes file so you can remember what you were doing
in that session.

This is local tooling in the same spirit as `hooks/`: no savannahd, no song, no
mesh worker, no tokens spent by the module itself. The Claude in the upper pane
is your ordinary interactive `claude`, not a `task_*` worker.

## Usage

```bash
work() { python3 work/savannah_work.py "$@"; }   # a function, not W="..." (zsh does not word-split $W)
work new fix-parser --prompt "look at src/json.cpp" --note "flaky on nested arrays"
work ls                      # NAME  BRANCH  STATE  LAST PROMPT  LAST NOTE
work note fix-parser "tried X, did not help"
echo "multi
line" | work note fix-parser  # stdin when no text args
work notes fix-parser
work say fix-parser "run the tests again"   # types into the Claude pane
work say fix-parser -- --continue           # text starting with "-" goes after "--"
work sh fix-parser git status               # types into the shell pane
work jump fix-parser [--shell]              # focus the pane
work rm fix-parser [--keep-worktree] [--force]
```

`new` flags: `--repo DIR` (default cwd; when a worktree is made, any directory
inside the repository is resolved to the MAIN worktree root, so a session can
never nest inside another session's worktree; with `--no-worktree` the
directory is used exactly as given), `--branch B` (check out an existing LOCAL branch instead
of creating `work/<name>`; `B` must already be in `refs/heads`, there is no
DWIM from remotes, and it cannot be combined with `--no-worktree`),
`--no-worktree` (run in the repo itself), `--prompt TEXT`, `--note TEXT`,
`--json`. A value that starts with `-` needs the `=` form (`--prompt=--continue`),
argparse otherwise reads it as a flag. `ls`, `new` and `note` take `--json` and then print one JSON value
on stdout (for `note`, put `--json` before the name: `note --json <name>
<text>`).

Text for `say`, `sh` and `note` that begins with `-` must come after `--`
(argparse would otherwise read it as an option): `say x -- --continue`.
`say`/`sh` refuse empty or whitespace-only text with exit 2; a bare Enter is
never typed into a live pane.

`rm` keeps the branch `work/<name>` (that is your work). A later `new <name>`
notices the surviving branch and reattaches a fresh worktree to it instead of
failing, and says so (`(reattached)` on the human line, a `new: reattached
existing branch work/<name>` line on stderr with `--json`). After
`rm --keep-worktree` the worktree path is still registered with git, so a later
`new <name>` fails with git's "already exists" until you `git worktree remove`
it yourself; that is the point of keeping it.

The name `archive` is reserved (it is the notes graveyard under the work dir),
along with mesh task ids (`t-<digits>`).

Exit codes: 0 success, 1 user error (unknown name, git refused, pane gone),
2 bad usage.

## Storage

`$SAVANNAH_WORK_DIR`, default `~/.claude/savannah/work/`:

```
<name>/record.json                 written atomically (tmp + os.replace)
<name>/notes.md                    append-only: "## YYYY-MM-DD HH:MM local\n<text>\n\n"
archive/<name>-<created-epoch>.md  notes land here on rm (-2, -3, ... appended if taken)
```

Names match `^[a-z0-9][a-z0-9._-]{0,63}$` as a whole (no trailing newline),
may not contain `..`, end in `.` or `.lock` (git ref rules), and may not look
like a mesh task id (`t-<digits>`). The name on the command line is the only
thing that names a directory: `record.json`'s own `name` field is replaced by
the directory name on load and is never used to build a path. `record.json`
fields, all always present (strings empty when unknown): `name`, `repo`,
`worktree`, `branch`, `created`, `updated`, `terminal` (`"iterm"`), `tab_id`,
`claude_pane`, `shell_pane`, `prompt`. `ls` skips directories whose name is
invalid and records whose `worktree` is empty.

`new` claims `<name>/` with a bare `mkdir` before any git or terminal work, so
two concurrent `new <name>` cannot both proceed; a directory with no
`record.json` is a stale half-claim that `new` reclaims and `rm` deletes
(reported as stale, exit 0). Any failure after the claim removes the claimed
directory along with the fresh worktree and branch.

Worktrees are created at `<repo>/.savannah-worktrees/<name>` on branch
`work/<name>` (or the `--branch` you name). `rm` removes the worktree but
never the branch.

## Join rule

A work session finds its live Claude Code session through the hook files that
`hooks/session-status.py` writes (`$SAVANNAH_SESSIONS_DIR`, default
`~/.claude/savannah/sessions/`): a hook file belongs to a work session when
`realpath(hook.project) == realpath(record.worktree)`. If several match, the
one whose `iterm_session_id` (after the colon) equals `claude_pane` wins, else
the most recently updated. A record with an empty `worktree` never joins. The
join happens at read time (`ls`, the dash); nobody writes into the other
side's file. `ls --json` adds `claude_session_id`, `state` (`"offline"` when
no live session), `message`, `last_prompt`, `last_note` (first line of the
newest note), `notes_path`, `transcript_path` to each record.

The hook has to be installed for any of this to show up: `python3
hooks/install.py`. Without it every session reads `offline` and STATE, LAST
PROMPT and the dash's WORK join stay empty.

## Terminal seam

`Terminal` has four methods: `create_split_tab(name, upper_cmd, lower_cmd)`,
`write(pane_id, text)`, `focus(pane_id)`, `close_tab(tab_id, panes)`. `ITerm`
is the only implementation and shells out to `$SAVANNAH_OSASCRIPT` (default
`osascript`). Every user string is quoted into the AppleScript with
backslash and double-quote escaping; newlines become `linefeed`
concatenations. The scripts return plain lines (`return a & linefeed & b`,
`return found`) so parsing is trivial. The create script is the only one
allowed to launch iTerm2; when it has to create the window it uses that
window's own first tab rather than adding a second, empty one. `write`,
`focus` and `close` begin with `if application "iTerm2" is not running then
return false`, so `say`/`sh`/`jump`/`rm` on a closed iTerm2 report the pane
as gone instead of starting the app. The create script was compile-checked
against iTerm2's dictionary with `osacompile`; `create tab with default
profile`, `split horizontally with same profile`, `write text`, `select` and
`close` are the commands used. Tests substitute `tools/fake_osascript.py`.

## Tests

`test/test_work.py` runs under ctest as `test_work`, against a temp work dir,
a temp sessions dir with hand-written hook files, a throwaway git repo
(isolated from your git config: `HOME`, `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM`, explicit author/committer), and the fake osascript. No
real iTerm2, no real claude. Only a git that is absent from `PATH` skips the
worktree cases (with a message, exit 0); a git that is present but cannot
init or commit is a failure.

## What does NOT work (yet, or on purpose)

- iTerm2 on macOS only. `terminal` is always `"iterm"`; a `tmux` value is
  reserved and any other value makes the terminal verbs exit 1.
- STATE, LAST PROMPT and the dash join need `hooks/session-status.py`
  installed (`python3 hooks/install.py`). Without it every session is
  `offline` forever; `new`, `note`, `say`, `sh`, `jump` and `rm` still work.
- iTerm2 tabs have no id in its AppleScript dictionary (and a tab's `index`
  is not even gettable live), so `tab_id` is `w<window id>t<tab position at
  creation>`, taken from the tab count right after the tab is added. Positions
  shift when tabs close, so `rm` locates the tab by its two pane ids instead;
  `tab_id` is informational.
- A fresh worktree is a new folder to Claude Code, so the first `claude` in it
  shows the "do you trust the files in this folder" prompt. Until you answer
  it no hook fires and `ls` says `offline` even though the tab is open.
- Nothing watches the panes. If you close the tab by hand the record stays
  until you `rm` it; `ls` just shows `offline`. `say`/`sh`/`jump` report exit 1
  when the pane is gone, nothing more.
- `say` and `sh` type blind: they press Enter and never read a reply. Whether
  Claude was actually ready for input is not checked. They do refuse empty
  text (exit 2), which is the only guard.
- `cd <worktree> && claude <prompt>` is typed into a fresh shell; if your shell
  startup is slow or prints a prompt late, the text may land before the prompt.
  iTerm2 queues typed input, so in practice it works, but it is not guaranteed.
- The first `osascript` call may block on macOS's Automation permission
  dialog. If nothing happens for 60 s the verb exits 1 and the message says
  to look at System Settings > Privacy & Security > Automation.
- `.savannah-worktrees/` lives inside your repo and shows up in `git status`
  as an untracked directory unless you ignore it (`.git/info/exclude` is a
  good place).
- `rm` without `--force` refuses a dirty worktree and leaves everything in
  place, tab included. `--force` discards uncommitted changes in the worktree.
  `rm` also refuses (exit 1, naming them, `--force` does not override) when
  another work session's worktree lies inside the one being removed, for
  example a `--no-worktree` session started with `--repo` pointing into it; rm
  those first or pass `--keep-worktree`.
- `--branch B` requires a local branch. A branch that only exists on a remote
  is refused (`branch B does not exist locally`); `git branch B origin/B`
  first if that is what you meant.
- If any step of `new` fails after the worktree exists, the fresh worktree,
  the `work/<name>` branch it just made, and the tab if it was already created
  are rolled back so nothing half-made survives; a pre-existing `--branch` or
  reattached `work/<name>` branch is left alone.
- A session directory whose `record.json` is missing, unreadable or not a JSON
  object is skipped by `ls` and treated as stale by `rm <name>`, which deletes
  the directory and tells you to check `git worktree list` for leftovers, since
  the record cannot say where its worktree was.
- The nested-worktree refusal only runs when `rm` is actually removing the
  worktree; `--keep-worktree` skips it along with the removal.
- Hook files are trusted as-is. A hook that lies about `project` joins the
  wrong work session; this is your own machine, so that is accepted.
- No Linux or Windows support of any kind; the module imports fine there but
  every terminal verb fails at `osascript`.
