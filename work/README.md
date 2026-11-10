# work: local interactive work sessions (Phase 5d)

`work/savannah_work.py` turns a manual habit into a named, file-owned object:
one iTerm2 tab per work item, split into an upper pane running INTERACTIVE
Claude Code in a git worktree and a lower shell pane cd'd into the same
worktree, plus a plain text notes file so you can remember what you were doing
in that session.

This is local tooling in the same spirit as `hooks/`: no savannahd, no song, no
mesh worker, no tokens spent by the module itself. The Claude in the upper pane
is your ordinary interactive `claude`, not a `task_*` worker. iTerm2 is driven
through its own command-line tool, `it2`. The board for all of these sessions
is a bare `savannah` (the dash in work mode, see `dash/README.md`); this
module is what the board runs for every key.

## Usage

```bash
work() { python3 work/savannah_work.py "$@"; }   # a function, not W="..." (zsh does not word-split $W)
work new fix-parser --prompt "look at src/json.cpp" --note "flaky on nested arrays" --title "Fix The Parser"
work ls                      # NAME  TITLE  BRANCH  STATE  STATUS  LAST PROMPT  LAST NOTE
work note fix-parser "tried X, did not help"
echo "multi
line" | work note fix-parser  # stdin when no text args
work notes fix-parser
work say fix-parser "run the tests again"   # types into the Claude pane
work say fix-parser -- --continue           # text starting with "-" goes after "--"
work ask --all [-q TEXT] [--timeout S] [--no-wait] [--json]   # one question into every idle or done tab, the replies collected
work ask fix-parser review-2353 -q "where are you?"            # or only these
work sh fix-parser git status               # types into the shell pane
work jump fix-parser [--shell]              # focus the pane
work output fix-parser [--last N] [--json]  # what that Claude said: last N turns (prompt + reply)
work open fix-parser [--shell] [--json]     # focus the tab; make it again (claude --continue) when it is gone
work adopt old-tab --session ID [--note T] [--title T] [--json]  # a tab you opened yourself becomes a named session (no new tab)
work autoname [SESSION_ID...] [--dry-run] [--no-claude] [--json]  # name every such tab from what it is doing (the board runs this)
work move fix-parser --later [--rank N] [--json]   # which board section (--active/--later) and the order inside it
work rename fix-parser parser-fix [--json]         # a new record name; worktree, branch and tab keep the old one
work status fix-parser -- "waiting for ctest (run 2 of 3)"   # report one poll (that tab's Claude runs this)
work status --here waiting for CI                  # the same from inside the session's own shell
work status fix-parser [--json]                    # read it; --clear removes it
work title fix-parser Fix The Parser, Take Two     # the free-text title the board shows (the name stays the id)
work title fix-parser [--json]                     # read it; --clear removes it
work log [--level normal|debug|verbose] [--lines N] [--follow] [--path] [--json]   # savannah's log, see Logging
work rm fix-parser [--keep-worktree] [--force]
work repos [DIR] [--json]                   # the folder of repos the board offers for new sessions (DIR is remembered)
work snapshot [--note T] [--json]           # save the layout (every work session and open Claude Code tab) before a reboot
work snapshots [--json]                     # the saved snapshots, newest first
work restore [STAMP|latest] [--dry-run] [--no-plain] [--json]   # put the tabs back, each claude resuming its conversation
work hub [DIR] [--no-launch] [--json]       # optional: make DIR the hub, a Claude session that opens tabs for you
```

The built C++ CLI fronts all of this, so with `build/savannah` on your PATH
there is no shell function to define: `savannah work ls`, `savannah work new
...`. A bare `savannah` is the board (the dash in work mode: Ctrl-N opens a
new session through `work new`, Enter opens or types into one, Ctrl-O reads
its output, Ctrl-T takes a note (on a plain tab it first adopts it, see
below), Ctrl-X twice removes it); `savannah mesh` is the mesh board. The dash binary is `dash/savannah-dash`, built with `cmake
--build build --target dash` (or `cd dash && go build -o savannah-dash .`);
`$SAVANNAH_DASH_BIN` points the CLI elsewhere.

`new` flags: `--repo DIR` (default cwd; when a worktree is made, any directory
inside the repository is resolved to the MAIN worktree root, so a session can
never nest inside another session's worktree; with `--no-worktree` the
directory is used exactly as given), `--branch B` (check out an existing LOCAL branch instead
of creating `work/<name>`; `B` must already be in `refs/heads`, there is no
DWIM from remotes, and it cannot be combined with `--no-worktree`),
`--no-worktree` (run in the repo itself), `--prompt TEXT`, `--note TEXT`,
`--title TEXT` (the free-text title the board shows, see `title` below),
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

`open` is the way back after an iTerm2 restart, or a tab closed by hand: when
the recorded Claude pane is still there it focuses it (`--shell` the shell
pane) and prints `focused`; otherwise it makes the tab again in the same
worktree, closes a leftover shell pane first so one session never owns two
tabs, and starts `claude --continue` when a transcript for that worktree
exists (the hook's recorded one, else the newest under Claude Code's projects
dir), a bare named `claude` when none does; it prints `reopened` and records
the new ids. `--json` gives `{name, action, tab_id, claude_pane, shell_pane}`.
A session whose worktree directory is gone cannot be reopened (exit 1; `rm`
it).

### Adopting a tab you opened yourself

`adopt NAME --session ID` takes a Claude Code tab that `new` did not make
(one you opened by hand, which the board lists as a plain row) into a named
work session without touching the tab. `ID` is that tab's Claude session id:
the name of its hook file under the sessions dir, what `ls --json` reports
as `claude_session_id`, what the board knows for every row. The hook file is
the whole input: its `project` becomes the record's `worktree` as it is (no
worktree is made, `repo` is the main worktree root of the repository around
it when there is one, else the same directory, `branch` is whatever is
checked out there, informational only), its `iterm_session_id` becomes
`claude_pane`, the join key, so the board shows the name on that very tab
even when another tab runs in the same directory. `tab_id` is looked up in
`it2 session list` and stays empty when it2 cannot see the pane;
`shell_pane` is empty, there is none; `prompt` is empty; `adopted` is
`true`. `--note` records a first note, `--json` prints the record. A tab
that already belongs to a work session is refused by its pane id. From then
on `note`, `say`, `output`, `open`, `jump` and `rm` address it like any other
session. `rm` of an adopted session archives the notes, closes its pane
(best effort) and deletes the record, and NEVER runs `git worktree remove`
or `prune`: the directory is yours. `open` on an adopted session whose pane
is gone makes a tab and a split in that directory (`claude --continue` when
a transcript exists), after which it has a shell pane like a session made
by `new`. The board does all of this from Ctrl-T on a plain row (name it,
then the note).

### Naming the tabs you opened yourself, from what they are doing

`autoname` is `adopt` for every plain tab at once, with the name found for
you. The board runs it when it comes up (and again for each new plain tab
once something has been typed into it), so the tabs you opened by hand
arrive on the board named; `savannah work autoname` runs the same by hand.
For every hook session no record claims (or only the `SESSION_ID`s given)
it reads the tab's own transcript (the hook's `transcript_path`, else
Claude Code's per-project directory), takes the human prompts (slash
commands and interruption notices dropped) and the latest reply, and asks a
headless Claude for a two-to-five word title over an excerpt: the
directory, the first three prompts, the latest prompt when there are more,
the head of the latest reply, each squeezed to 400 characters. The call is
`claude -p --safe-mode --no-session-persistence --strict-mcp-config --tools
"" --max-turns 1 --output-format json --model haiku`, run from the temp
directory with `SAVANNAH_HOOK_IGNORE=1` in its environment: no hooks (so
the call is never a row on the board), no CLAUDE.md, no MCP servers, no
tools, nothing persisted, a small model (`$SAVANNAH_NAME_MODEL` picks
another; `default` means the CLI's own), about three seconds and a
fraction of a cent per tab, four tabs at a time. Nothing is typed into the
live tab: a question typed there would be a prompt, or the answer to a
permission prompt. The title is cleaned (first line, quotes and a trailing
period dropped, 120 chars), the name is derived from the tab's checkout
when it is one made beside its repository the `<repo>-<ID>` way (a tab in
`~/ng/delphix/zfs-DLPX-98816`, a git worktree or a clone next to `zfs`,
is `zfs-dlpx-98816`; a second tab there `zfs-dlpx-98816-2`) and else
from the title the way the board derives one from words (`Fix The Parser`
is `fix-the-parser`; `-2`, `-3` when taken), and the tab is adopted with
that title. When the call fails
(no `claude`, no network, a timeout of 90 s, a non-zero exit) the first
prompt's first line is the title instead, cut to 60 characters, and the
result says so (`how: prompt`, `claude_error`); `--no-claude` does that
on purpose (no tokens). A tab nothing has been typed into yet is skipped
(`no prompt yet`) and named on a later run. `--dry-run` prints what would
be named and adopts nothing. Exit 0 whenever the verb ran; the outcome per
tab is in the output: `--json` is `{dry_run, named: [{session_id,
project, name, title, how, claude_error?}], skipped: [{session_id, why}],
failed: [{session_id, error}]}`. Ctrl-R on the board edits a title you do
not like, `rename` changes the name. The hook honours `SAVANNAH_HOOK_IGNORE`
regardless of how Claude Code treats hooks in safe mode (belt and braces).

`new` also copies into a fresh worktree whatever `<repo>/.worktreeinclude`
names: one path or glob per line, relative to the repository root (a leading
`/` is ignored), `#` comments and blank lines skipped, `**` for any depth.
It is meant for the gitignored files a worktree needs to run (`.env`,
`conf/*.local`, a `secrets/` directory); anything that already exists in the
worktree, including every tracked file, is left alone, and `.git` and
`.savannah-worktrees` are never copied. `new` reports what it copied on
stderr (`new: copied 3 from .worktreeinclude: ...`). Keep the listed files
gitignored, or the copies make the worktree dirty and `rm` refuses it until
`--force`. With `--no-worktree` nothing is copied.

`move NAME --active|--later [--rank N]` says where the row sits on the board:
`priority` is `active` (the top section, what you are on now) or `later` (the
bottom one), `rank` orders rows inside a section, lower first. A new session
is `active` with its creation time as rank, so untouched rows keep arriving in
order; a record written before the fields existed reads the same way. At
least one option is required (exit 2 otherwise), `--active` and `--later`
exclude each other, a negative rank is fine (`--rank=-1` puts a row first).
Only the record changes: the tab, the worktree and the branch are not
touched. `--json` gives `{name, priority, rank}`. The board's Shift-Up and
Shift-Down call this.

`rename OLD NEW` gives the record a new name: `record.json`, `notes.md` and
`status.json` move to `<NEW>/` and the record is rewritten under the new
name, so notes, status, priority and the join to the live tab all carry over.
The worktree directory (`.savannah-worktrees/OLD`), the branch (`work/OLD`)
and the tab's own Claude session name (`claude -n OLD`) keep the old name on
purpose: an interactive Claude is running in that directory, and `git
worktree move` under a live process would pull the floor from under it; the
branch is yours and renaming branches is git's job. NEW must be free (the
name is claimed first, exactly like `new`); a refused rename leaves both
sessions untouched, and a failure halfway moves the files back. `--json`
gives `{name, old, worktree, branch}`.

`status` is how the Claude in a tab reports a long wait to the board: what it
is polling for you (a test run, CI, a deploy, a slow build). `status NAME
TEXT...` writes `<NAME>/status.json` as `{"text", "updated", "count"}`: the
first non-blank line of the joined words, cut to 200 chars; the epoch of
this report; and the count, one more than the previous file said (1 when
there was none), so "poll 30" is visible without parsing anything. Every
report rewrites the file whole; `--clear` removes it and the count starts
over; with neither text nor `--clear` it prints the report (`NAME: TEXT (N
polls, last Ns ago)`, `NAME: (no status)` when none). `--here` names the
session this shell runs in instead of NAME: the record whose `claude_pane`
is this iTerm2 session (`$ITERM_SESSION_ID`, which the Bash that Claude Code
runs inherits), else the one record whose `worktree` is the cwd; several
records on this directory with no pane match is an error that names them
(`several work sessions share this directory: a, b (name one)`), none is
`no work session for <cwd>`, both exit 1. With `--here` every positional is
text (`status --here waiting for CI` needs no quotes); without it the first
positional is the name. Text starting with `-` goes after `--`. `--json`
prints `{name, text, updated, count}` after a report, `{name, status}` (the
object or `null`) on a read or a `--clear`. `ls --json` adds `priority`,
`rank` and `status` (object or `null`) to every row, and the human table
shows a STATUS column (`TEXT (N)`, clipped to 30 columns, `-` when none).

`title NAME TEXT...` gives a session a free-text title: capitals, spaces,
punctuation, anything; the first non-blank line of the text, stripped and
cut to 120 chars (the name rule exists because the name is a path component
and a branch; a title is for your eye, so the only limit is a width that
still fits a board). It is what the board shows in its WORK column, what
`ls` shows in a TITLE column right after NAME (clipped to 24, `-` when
none), and `ls --json` carries as `title` (`""` when none). The name stays
the identifier underneath: the record directory, the worktree, the branch
`work/<name>`, the tab's `claude -n <name>` and every verb take the name,
never the title, so a title changes at any time and moves nothing. `title
NAME` prints it (`NAME: TEXT`, or `NAME: (no title)`), `title NAME --clear`
removes it (`NAME: title cleared`), a set prints `NAME: title set: TEXT`;
`--json` gives `{name, title}` in every form. Text starting with `-` goes
after `--`; as with `status` and `note`, words after `--json` need `--`
first, or put `--json` after the text. Whitespace-only text is a usage
error (exit 2), an unknown name exit 1. `new --title TEXT` and `adopt
--title TEXT` set it at birth; the record always carries the field (`""`
when unset; a record from before titles, or one whose field is not a
string, reads `""`), and `rename` keeps it.

### Asking every tab what it is doing

`ask` types one question into every tab that is safe to interrupt and
collects the replies. `savannah work ask --all` asks every live Claude
Code tab, work sessions and plain tabs alike; `savannah work ask NAME...`
asks only those. The default question is "In three lines, for a board of
all my sessions: what are you working on, where are you with it, and
what comes next?"; `-q TEXT` asks something else. A tab is asked only
when its hook file says `idle` or `done`: a `working` tab is not
interrupted, a `waiting` tab has a permission prompt or a question up
that typed text would answer, a tab with no recorded pane cannot be typed
into, and the session running the command (a Claude asking from its own
shell) is never asked itself; each skipped tab is listed with the reason.
The question goes in through `it2 session send` like `say`, and then the
verb waits (`--timeout`, default 120 s) for each asked tab's hook file to
show the question as its last prompt and the state `done`, reads the
reply from the tab's transcript (the turn whose prompt is the question),
and keeps each work session's reply twice: appended to its `notes.md`
as a note (the reply, then `(asked: <the question>)` on the last line,
so the NOTE column shows the answer's first line) and on its record as
`summary` with `summary_at` (`ls --json` carries both, the board's panel
shows it; a plain tab has no record, so its reply is only printed). A
tab that has not answered in time is reported as `timeout` with no
reply; `savannah work output` shows it when it comes. `--no-wait` types
the question and returns at once. Exit 0 whatever was asked or skipped;
`--json` is `{question, timeout, waited, asked: [{session_id, name,
project, how: answered|timeout|sent, reply}], skipped: [{..., why}],
failed: [{..., error}]}`. This costs one turn of each asked tab's own
context, so the board asks twice (Ctrl-A, Ctrl-A) before running it.

This is savannah's answer to "ask all my Claudes what they are doing".
Claude Code has cross-session messaging of its own (`ListAgents` and
`SendMessage` from inside a session, `/list-agents`), but its socket
protocol is undocumented, only a Claude can send, and a sender cannot
tell whether a receiver is mid-work or at a permission prompt; the hook
files tell savannah exactly that.

### Polling for you

A Claude that is waiting on something slow should say so where you can see
it. The SessionStart hook (`hooks/session-status.py`) finds the work session
whose worktree is the new session's cwd and hands that Claude, as context,
the exact command for its session (`<cmd> work status NAME -- "waiting for
ctest (run 2 of 3)"`, where `<cmd>` is `build/savannah` when built, else
`python3 work/savannah_work.py`), plus what the board shows and how to clear
it. The board shows a session as polling while that report exists and the
hook still sees the turn open (state `working`); once the turn ends the text
stays as the last thing reported, with its count and age, until the next
report or `--clear`. Nothing makes a Claude report: the context is an offer,
and `status` is as honest as the Claude that wrote it.

`repos [DIR]` lists the git repositories directly under DIR (a `.git` entry;
dot-directories skipped), the list the board's new-session dialog offers.
`repos DIR` remembers DIR in `$SAVANNAH_HUB_FILE` (the same file `hub` uses);
`repos` alone uses the remembered folder, else the cwd. `--json` gives
`{dir, picked ("given"|"remembered"|"cwd"), repos, scratch}`, `scratch` being
`DIR/.savannah-scratch` when it exists (where tasks that belong to no repo
run), else `""`.

The names `archive` (the notes graveyard under the work dir) and `snapshots`
(the saved layouts, see below) are reserved, along with mesh task ids
(`t-<digits>`).

`output` is the read side of `say`. It parses the transcript Claude Code
itself writes for that tab (the `transcript_path` the hook recorded, else the
newest `.jsonl` under `$CLAUDE_CONFIG_DIR/projects/<worktree with every
non-alphanumeric character replaced by "-">/`, default `~/.claude/projects`)
and prints the last N turns: a header with the local time and the first line
of your prompt, then the assistant's text blocks joined in order. Thinking,
tool calls, tool results and subagent (sidechain) lines are dropped. A turn
whose reply has not arrived yet prints `(no reply yet)`. `--json` gives
`{name, transcript_path, source ("hook" or "projects"), turns_total,
turns:[{prompt, text, asked, answered}]}`. Exit 1 until that Claude has
started (there is no transcript to read).

### Snapshot and restore

A reboot, or iTerm2 relaunching, takes every tab with it. `work snapshot`
saves what is open so `work restore` can put it back afterwards, with each
Claude reloading the conversation it was in.

`snapshot [--note T]` writes `<work dir>/snapshots/<YYYYMMDD-HHMMSS>.json`
and copies it to `snapshots/latest.json` (a second snapshot in the same
second gets a `-2`, `-3` suffix). It holds `stamp`, `taken` (epoch),
`taken_local`, `note`, `it2` (whether iTerm2 answered) and two lists.
`sessions` is every work record, in iTerm2's tab order (`it2 session list`
order of the Claude pane; records whose pane is gone come last, by name):
`name`, `title`, `worktree`, `repo`, `branch`, `adopted`, `priority`,
`rank`, `tab_id`, `claude_pane`, `shell_pane`, `tab_title` (the it2 title
without iTerm2's trailing " (process)"), `pane_alive` (the Claude pane is
in the it2 list; `null` without it2), `live` (a Claude Code session is
joined to it, by the usual join rule) and, from that session, `session_id`,
`transcript_path`, `state` and `last_prompt` (empty strings when offline).
`plain` is every other Claude Code tab: a hook session no record claims,
with `project`, `session_id`, `transcript_path`, `state`, `last_prompt`,
`pane` (its iTerm2 pane id), `tab_id`, `title` and `pane_alive`. Without
it2 the snapshot is still written (the records and sessions are all on
disk), only without tab order, titles or liveness, and the line says so.
The human line is `snapshot <path>: N work sessions (K live), M plain
tabs`; `--json` gives `{path, stamp, work, live, plain, it2}`. `snapshots`
lists them newest first (`STAMP  TAKEN  WORK  LIVE  PLAIN  NOTE`; `--json`
is `[{stamp, path, taken, taken_local, work, live, plain, note}]`;
`latest.json` is not a row of its own).

`restore [STAMP|latest]` (default `latest`) walks the snapshot's work
sessions in order. A record whose Claude pane is still there is skipped
(`alive`), as is one whose record is gone (`no record`) or whose worktree
is gone (`worktree gone`). The rest get a new tab and split in the
worktree, like `open`, with the upper pane running `claude -n <name>
--resume <session_id>` when the snapshot recorded a session id and its
transcript is still on disk (the recorded `transcript_path`, else
`$CLAUDE_CONFIG_DIR/projects/<encoded worktree>/<session_id>.jsonl`), else
`claude -n <name> --continue` when the worktree has any transcript, else a
fresh `claude -n <name>`; the record's `tab_id` and pane ids are updated.
Then, unless `--no-plain`, every plain tab comes back as a single pane
(`tab new`, no split) named after its old title, running `cd <project> &&
claude --resume <session_id>` (`--continue` or a fresh `claude` by the
same rule, never `-n`), unless its hook file still names a live pane
(`alive`), its pane was already gone when the snapshot was taken (`not
open when the snapshot was taken`) or the directory is gone (`project
gone`). Nothing is ever closed. An it2 failure on one item is recorded
(`failed NAME: <error>`, the half-made tab closed again) and the rest
continues; the exit code is 1 when anything failed, else 0. `--dry-run`
prints the same plan (`would restore NAME (resume <id>)`) and touches
nothing. The human lines are `restored NAME (resume <id>, tab T)`,
`restored NAME (continue, tab T)`, `restored NAME (fresh, tab T)`, `plain
~/dir (resume <id>, tab T)`, `skipped NAME: <why>`, `failed NAME:
<error>`, then one summary line. `--json` gives `{stamp, path, dry_run,
restored: [{name, how ("resume"|"continue"|"fresh"), tab_id}], plain:
[{project, how, tab_id}], skipped: [{name|project, why}], failed:
[{name|project, error}]}`. With no snapshot at all, exit 1: `no snapshot
(work snapshot makes one)`.

What a restore cannot do: a turn that was running when the machine went
down is lost (Claude's state is its transcript, and `--resume` reloads the
conversation up to the last thing written to it, into a fresh process). It
never closes anything, so if iTerm2 brought its own windows back first,
restore skips the tabs whose Claude is already alive and remakes only the
gone ones; a plain tab whose hook file is stale (its pane died with
iTerm2) is remade until the resumed Claude's hook rewrites that file.
Tabs come back in the window the command runs from, in snapshot order, not
in their original windows; plain tabs come back as single panes whatever
they were; the title set on a plain tab lasts only until its shell
rewrites it (`-n` is not used for plain tabs, since the tab had no name
of ours). `--resume <id>` is Claude Code's own flag and the transcript
naming (`<session_id>.jsonl` under the encoded project dir) is observed,
not promised. Nothing is restored for a tab that was not running Claude
Code (a plain shell tab leaves no hook file).

## The hub (optional)

An optional layer over the same verbs, not the front door (the board is):
one coordinating Claude Code session in the directory that holds all your
repos, opening a tab per task through the `work_*` MCP tools and reading the
results back, for the days you want to hand out work rather than open the
tabs yourself.

```bash
savannah hub ~/ng/dbjwhs        # once: writes .mcp.json, CLAUDE.md, .savannah-scratch/, remembers the dir, runs claude there
savannah hub                    # after that: opens the remembered hub, from any directory
savannah hub --no-launch        # refresh the files without launching
```

`hub` remembers DIR in `$SAVANNAH_HUB_FILE` (default
`~/.claude/savannah/hub.json`, the same memory `work repos` uses for the
board's list of repos); `hub` with no DIR uses the remembered directory
(falling back to the cwd when nothing is remembered or the remembered
directory is gone). `savannah hub DIR` re-points it. A bare `savannah` is
the board, never the hub. Put `build/savannah` on your PATH (a symlink in
`~/.local/bin` is enough).

The hub's rule is strict on purpose: every request becomes a tab, even
"compute pi to 20 digits". Tasks that belong to no repository go to
`DIR/.savannah-scratch/` (`work_new(..., repo=".savannah-scratch",
no_worktree=true)`), a directory `hub` creates with its own short CLAUDE.md
so the tab does the task and stops. Without that area the hub would either
do small tasks itself (which is what happened in the first live test) or
open the tab in the hub directory, where the tab's Claude would read the
hub CLAUDE.md and become another hub.

`hub` also merges the hub's permissions into `DIR/.claude/settings.json`:
`enableAllProjectMcpServers: true` (so the `peer` server in `.mcp.json` is
approved without a prompt) and `permissions.allow` entries for the nine
`mcp__peer__work_*` tools plus `Bash(sleep *)` (the polling loop). Nothing
else in that file is touched, and a malformed file is refused. Without this
the first live run stopped at a permission prompt on `work_new`, which a
headless hub cannot answer and an interactive one would ask on every new
tool. Mesh tools (`task_*`, `ask_peer`) are NOT pre-allowed.

`hub` registers the peer shim in `DIR/.mcp.json` under the `peer` key with
ABSOLUTE paths (the repo's own `.mcp.json` uses relative ones, which is why a
Claude started anywhere else never saw the `work_*` tools). Other servers in
an existing `.mcp.json` are kept; a `peer` entry that differs is replaced and
reported as `updated`; a malformed file is refused untouched. `DIR/CLAUDE.md`
and `DIR/.savannah-scratch/CLAUDE.md` are created from `work/hub_CLAUDE.md`
and `work/scratch_CLAUDE.md` only when missing, never overwritten (the
output says `kept` and names the template so you can merge by hand). Then,
unless `--no-launch`, it `chdir`s to DIR and execs `claude`
(`$SAVANNAH_CLAUDE_BIN` overrides the command; the exit code is claude's).
The first launch asks you to approve the project MCP server; that is Claude
Code's own prompt, answer once.

The hub session then has `work_new(name, repo="<subdir>", prompt=...)` to
open a tab, `work_list` for every tab's state, `work_say`/`work_shell` to
steer, `work_open` to bring a tab back, `work_output` to read what a tab's
Claude said, `work_note`/`work_notes` for the record, `work_rm` to finish. The template CLAUDE.md spells out that
loop (open, poll `work_list` with `sleep 5` between polls until `done` or
`waiting`, `work_output`, relay, `work_rm` a scratch tab) and the rules (no
editing repos from the hub, one tab per work item, `say` is a request and
`output` the result, `waiting` and `offline` rows go to the owner).

Exit codes: 0 success, 1 user error (unknown name, git refused, pane gone),
2 bad usage.

## Logging

Every piece of savannah writes one log: `$SAVANNAH_LOG_DIR/savannah.log`,
default `~/.claude/savannah/log/savannah.log`. This module stamps its lines
`src=work`, the hook `src=hook`, the shim `src=shim`, the board `src=dash`
(Go's `log/slog`, the same text form, `dash/logging.go`), so one file tells
the whole story of a key press: the board's action, the `work` subprocess
it ran, the it2 calls that subprocess made. One line per record, slog's
text form (`work/savannah_log.py` writes it, the three Python pieces share
that module):

```
time=2026-11-05T22:28:08.151-08:00 level=INFO msg=verb src=work verb=say args="[say demo -- run the tests]" cwd=/Users/me/ng/x
time=2026-11-05T22:28:08.290-08:00 level=DEBUG msg=it2 src=work args="[session send --session 8F2A -- run the tests]" rc=0 ms=138
time=2026-11-05T22:28:08.291-08:00 level=INFO msg=typed src=work name=demo pane=claude session=8F2A chars=13
```

`time` is local RFC 3339 with milliseconds, `level` one of VERBOSE, DEBUG,
INFO, WARN, ERROR, `msg` what happened, `src` who wrote it, then key=value
pairs: a value is bare when it is one plain word and JSON-quoted otherwise,
a list is `[a b c]` (quoted once it has a space).

The file is always verbose: every level is written, always, there is no
switch. Levels are for reading. `work log` prints the last 200 lines of the
current file at `--level normal` (INFO, WARN and ERROR); `--level debug`
adds DEBUG; `--level verbose` adds VERBOSE, everything. A line with no
`level=` stamp shows at every level. `--lines N` for more or fewer,
`--follow` keeps printing new lines (a poll every half second; a rotated
file is read again from its start) until Ctrl-C, which exits 0 quietly;
`--path` prints only the path; `--json` gives `{"path", "level", "lines":
[...]}` (not with `--follow`). The board's Ctrl-L view is the same reader.
What lands at which level:

- INFO: a state change. A record written (`new`, `adopt`, with the tab and
  pane ids), removed (`rm`, with what was kept), renamed, moved; a note
  taken; text typed into a pane (`say`, `sh`: which pane, how many chars);
  a pane focused; a status reported or cleared; a title set or cleared; a
  hub set up. From the shim, every tool call (tool name, argument keys, the
  first line of its prompt or text cut to 120). From the hook, a work
  session's Claude being handed its context at SessionStart (name, command).
- WARN: a refused verb (the message), a rolled-back `new`, a pane that is
  gone, a tool error in the shim, a hook that could not write its file.
  ERROR: a verb that crashed (then the traceback, as before).
- DEBUG: every lookup. The hook sessions loaded, the join of each record to
  its live session (which hook file, by pane or newest), where a transcript
  was found, the `repos` list, `ls` counts, a status or title read, every
  it2 and git call (arguments, exit code, milliseconds), a verb finishing
  (rc, ms), a tool call finishing, and from the hook every event (name,
  session id, state, cwd, iTerm2 session id, the file written).
- VERBOSE: payloads. What it2 printed (head), git's stderr on failure, the
  text typed (head), transcript parse counts, the prompt heads of `output`,
  and the `log` verb's own lines, so reading the log never shows up in the
  log you are reading.

Rotation: at 10 MiB the file becomes `savannah.log.1` (older ones shift to
`.2` and `.3`, the fourth is dropped) and a fresh `savannah.log` starts.
Every writer rotates by the same rule, so whichever one crosses the line
does it. Logging never raises and never blocks: a directory that cannot be
made or written means no log and nothing else (the verb runs, the hook and
the shim stay silent, nothing extra on stdout or stderr).

What does NOT work: `work log` reads the current file only, never the
rotated `.1` to `.3` (open those yourself); two writers rotating in the
same instant can lose a few lines (each renames on its own clock); the C++
CLI (`savannah` itself, before it execs the board or this module) and the
mesh daemon do not write here; the log is local to this machine, nothing on
the mesh carries it; and `--follow` is a poll, not a watch, so a line can
show up to half a second late.

## Storage

`$SAVANNAH_WORK_DIR`, default `~/.claude/savannah/work/`:

```
<name>/record.json                 written atomically (tmp + os.replace)
<name>/notes.md                    append-only: "## YYYY-MM-DD HH:MM local\n<text>\n\n"
<name>/status.json                 {"text", "updated", "count"}: the tab's own poll report (status)
archive/<name>-<created-epoch>.md  notes land here on rm (-2, -3, ... appended if taken)
snapshots/<YYYYMMDD-HHMMSS>.json   a layout saved by snapshot (-2, -3, ... when one second holds two)
snapshots/latest.json              a copy of the newest snapshot, what restore takes by default
```

Names match `^[a-z0-9][a-z0-9._-]{0,63}$` as a whole (no trailing newline),
may not contain `..`, end in `.` or `.lock` (git ref rules), may not look
like a mesh task id (`t-<digits>`) and may not be `archive` or `snapshots`.
The name on the command line is the only
thing that names a directory: `record.json`'s own `name` field is replaced by
the directory name on load and is never used to build a path. `record.json`
fields, all always present (strings empty when unknown): `name`, `repo`,
`worktree`, `branch`, `created`, `updated`, `terminal` (`"iterm"`), `tab_id`,
`claude_pane`, `shell_pane`, `prompt`, the boolean `adopted` (`true` for
a session made by `adopt`; a record written before the field existed reads
`false`), `priority` (`"active"` unless the file says `"later"`) and the
integer `rank` (the file's when it parses, else `created`). `ls` skips
directories whose name is invalid and records whose `worktree` is empty.
`status.json` sits beside the record and is optional: absent means no
report; a damaged one reads as no report.

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
newest note), `notes_path`, `transcript_path` and `pane_alive` (whether the
recorded Claude pane is in `it2 session list`: one it2 call per `ls`, `null`
when it2 is unavailable) and `status` (the poll report, or `null`) to each
record; `priority` and `rank` come from the record itself.

The hook has to be installed for any of this to show up: `python3
hooks/install.py`. Without it every session reads `offline` and STATE, LAST
PROMPT and the dash's WORK join stay empty.

## Terminal seam

`Terminal` has six methods: `create_split_tab(name, upper_cmd, lower_cmd)`,
`create_tab(name, cmd)` (one pane, no split: what `restore` makes for a
plain tab; `tab new`, `session run`, `session set-name`, the same rollback),
`write(pane_id, text)`, `focus(pane_id)`, `close_tab(tab_id, panes)` and
`live_ids()`. `ITerm` is the only implementation and shells out to iTerm2's
own command-line tool, `it2` (`$SAVANNAH_IT2` overrides the command and is
split like a shell line; default `it2` on PATH, else the copy inside
iTerm.app at `/Applications/iTerm.app/Contents/Resources/utilities/it2`).
The calls, in order for `new`: `session list --json` (which window to use),
`tab new --window W` (or `window new` when iTerm2 has no window: that
window's own tab becomes the work tab, no second empty one), `session list
--json` again (the new tab's session), `session run --session S -- " cd
<worktree> && claude -n <name> <prompt>"` (types the line and presses
Enter), `session set-name --session S -- <name>`, `session split --session
S` (the shell pane below), `session run` with ` cd <worktree>` into it.
`say` and `sh` are two `session send` each (the text, then a carriage return: the Enter key; the newline `session run` appends is a line break to Claude Code's input box, not Enter, found live 11/3/2026), `jump` is `session focus` plus
`app activate`, `rm` and `open` close a tab with `session close --session P
--force` per pane, `ls --json` and `open` read `session list --json` once.
User text is passed as arguments after `--`, never through a shell or an
AppleScript string, so quoting is it2's problem and not ours. If a step
fails after the tab exists, the half-made tab is closed before the error is
reported. "not found" from it2 means the pane is gone (exit 1 with that
message); any other failure is reported with it2's own stderr. Tests
substitute `tools/fake_it2.py`.

Two things the live run taught (11/3/2026). The line for a new pane is
typed before its shell is up and waits in the tty queue; a shell that reads
a key while starting eats the first character (oh-my-zsh's "Would you like
to update? [Y/n]" turned `cd` into `d`), so both lines start with a space
that nothing misses (zsh's `hist_ignore_space` then keeps them out of
history). And the tab's name is the terminal title: the shell's auto-title
rewrites it at every prompt, so `set-name` only holds while a command runs,
which is why it comes after the `run`; in practice `claude -n <name>` names
the tab for as long as Claude is up, and that is the name you see. `it2 tab
new --command` (a pane that runs Claude directly, no shell) was considered
and not used: it would run without your shell's environment and PATH, and
the pane would close when Claude exits, taking the place you restart it
from.

## Tests

`test/test_work.py` runs under ctest as `test_work`, against a temp work dir,
a temp sessions dir with hand-written hook files, a throwaway git repo
(isolated from your git config: `HOME`, `GIT_CONFIG_GLOBAL=/dev/null`,
`GIT_CONFIG_NOSYSTEM`, explicit author/committer), and the fake it2
(`tools/fake_it2.py`, which answers like it2, keeps a tiny world of windows
and panes, and logs every call as one JSON argv line, so the tests assert
the exact arguments). No real iTerm2, no real claude. When CMake hands it
the built CLI, the `hub`, `work`, `mesh` and bare entry points are driven
too, the dash being a fake script that records its argv. Only a git that is
absent from `PATH` skips the worktree cases (with a message, exit 0); a git
that is present but cannot init or commit is a failure.

`test/test_restore.py` (ctest `test_restore`) covers `snapshot`, `snapshots`
and `restore` the same way: six `--no-worktree` records (so no git is
needed) in the fake's world, hand-written hook files and tiny transcript
files under a temp `CLAUDE_CONFIG_DIR`, a snapshot, the dry-run plan, then
a "reboot" (every fake pane closed, the hook files removed) and the restore,
asserting the exact it2 calls (`--resume <id>`, `--continue`, fresh, the
single-pane plain tab), the skips, a failing `session split` (recorded,
exit 1, the rest restored), `--no-plain`, restore by stamp, the reserved
name and the log lines.

## What does NOT work (yet, or on purpose)

- `ask` knows a tab's state only as the hook recorded it: a Claude at a
  question of its own (AskUserQuestion, no Notification fires) reads as
  `done` and is asked anyway, and the question lands in its input box
  after whatever you had half-typed there. A compacted session answers
  from what it still remembers. The reply is matched by prompt text, so a
  tab asked the same question twice reports its latest answer.
- iTerm2 on macOS only, 3.5 or newer, with its Python API switched on
  (iTerm2 > Settings > General > Magic > Enable Python API): `it2` is
  iTerm2's own tool and talks to that API. With the API off, `it2` hangs;
  after 60 s the verb exits 1 and the message says which switch to flip.
  `terminal` is always `"iterm"`; a `tmux` value is reserved and any other
  value makes the terminal verbs exit 1.
- `it2` costs 200 to 500 ms per call and `new` makes seven of them, so a new
  session takes about two seconds to appear; `ls --json` adds one call for
  `pane_alive`. What `it2` does when iTerm2 is not running at all is
  untested (it may launch it, or hang until the timeout).
- `it2 tab close` reports success and closes nothing (iTerm2 3.7.6), so
  `rm` and `open` close a tab pane by pane with `session close --force`;
  `tab_id` is recorded for the board and nothing acts on it. `it2 tab new`
  refuses to guess a window from a script ("No current window"), so the
  module uses the window of the session it runs in (`ITERM_SESSION_ID`),
  else the first window it2 lists, else makes one.
- STATE, LAST PROMPT and the dash join need `hooks/session-status.py`
  installed (`python3 hooks/install.py`). Without it every session is
  `offline` forever; `new`, `note`, `say`, `ask`, `sh`, `jump`, `open` and `rm`
  still work. iTerm2's own Claude Code integration (its `cc-status` hook,
  Session Status toolbelt) can be installed alongside; the two hooks do not
  interfere, and ours is the one that records the transcript path and
  project the join needs.
- A fresh worktree is a new folder to Claude Code, so the first `claude` in
  it shows the "do you trust the files in this folder" prompt. Until you
  answer it no hook fires and `ls` says `offline` even though the tab is
  open.
- `open` on a gone tab runs `claude --continue`, which resumes the most
  recent conversation Claude Code has for that directory: usually the one
  the tab had, but that is Claude Code's choice, not ours. With no
  transcript it starts a fresh named `claude` and does not replay the
  original prompt (the notes are the record of what the task was).
- `snapshot`/`restore` is a layout, not a process checkpoint: a running
  turn is lost, `--resume` reloads the transcript into a fresh Claude, tabs
  come back in one window in snapshot order, plain tabs as single panes,
  and nothing is ever closed (see "Snapshot and restore" above for the
  full list). You run `snapshot` yourself before the reboot; nothing takes
  one automatically.
- `output` reads a file Claude Code owns. The subset it relies on (`type`
  `user`/`assistant`, `message.content` text blocks, `isSidechain`,
  `timestamp`) is pinned by `test/test_work.py`'s hand-written transcript,
  not by any contract Claude Code publishes; if the layout drifts, `output`
  degrades to `(no turns yet)`. The projects-dir fallback also guesses the
  directory name from the worktree path; the hook's recorded path is the
  reliable route, so keep the hook installed. The reply is only as final as
  the tab's state: read `work ls` (or `work_list`) and wait for `done` or
  `waiting` before trusting `output`.
- `.worktreeinclude` is not gitignore syntax: no negation, no trailing-slash
  rules, a bare name matches at the repository root only (`**/name` for any
  depth), symlinks are copied as links, and whatever already exists in the
  worktree is skipped silently. Nothing re-copies after `new`.
- `hub` is optional and has no `tmux` form; it only knows how to launch
  `claude` with no extra arguments (`--no-launch`, then run it yourself for
  anything else). It never edits an existing `CLAUDE.md`, so a hub set up
  before the template changed keeps its old instructions until you merge by
  hand. The hub's behaviour is a CLAUDE.md, not code: the model decides to
  open a tab. One hub, one machine: `hub.json` remembers a single directory,
  which is also the folder of repos the board offers.
- `savannah` with no arguments execs the dash binary (`$SAVANNAH_DASH_BIN`,
  else `dash/savannah-dash` as baked in at build time, else `savannah-dash`
  on PATH) and exits 1 with a build hint when none exists; the dash needs a
  terminal. The C++ binary finds the work module through the path baked in
  at build time (`SAVANNAH_WORK_PY`), so a moved checkout needs that
  variable set.
- Nothing watches the panes. `ls --json` says whether the Claude pane still
  exists (`pane_alive`) and `open` brings a gone tab back, but nothing
  happens on its own: a tab closed by hand stays an `offline` row until you
  `open` or `rm` it. `say`/`sh`/`jump` exit 1 when the pane is gone.
- `say` and `sh` type blind: they press Enter and never read a reply. Whether
  Claude was actually ready for input is not checked. They do refuse empty
  text (exit 2), which is the only guard.
- ` cd <worktree> && claude -n <name> <prompt>` is typed into a fresh shell
  and waits in the tty queue until the shell reads it. A startup that reads
  one key (oh-my-zsh's update prompt) eats the leading space and nothing
  else; a startup that reads a whole line would eat the command, and the
  pane would just sit at a prompt (type `claude` yourself, or `rm` and
  `new` again). The tab name comes from the terminal title (`claude -n`),
  which the shell's auto-title takes back once Claude exits; `set-name` is
  best effort.
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
- An adopted session has no shell pane: `sh` exits 1 (`shell pane of <name>
  no longer exists`) until `open` remakes the tab after its Claude pane is
  gone. Its `tab_id` is best effort (empty when it2 could not see the pane at
  adopt time) and nothing acts on it. `adopt` reads the hook file, so it
  needs `hooks/install.py` run and the tab's Claude started; a tab whose
  Claude is sitting at a trust or MCP prompt has no hook file yet.
- Two tabs in one directory stay apart only through their pane ids: each
  adopted record names its own pane, so the join picks the right tab while
  both are open. After an iTerm2 restart the recorded panes are gone, the
  join falls back to the newest hook session in that directory, and two
  such records can bind the wrong tab until you `open` one (which gives it
  a fresh pane of its own).
- No Linux or Windows support of any kind; the module imports fine there but
  every terminal verb fails at `it2`.
- `status` is self-reported by that tab's Claude. Nothing verifies the text
  or the count, nothing expires a report on its own, and a Claude that does
  not run the command shows nothing. The board only stops calling a report
  live when the hook sees the turn end; the text itself stays until the
  next report or `--clear`. One file per session, rewritten whole: two
  reporters on one session overwrite each other.
- `status --here` needs `ITERM_SESSION_ID` to name a recorded Claude pane,
  or a cwd that is exactly one work session's worktree. In a directory with
  several sessions (`--no-worktree` sessions on one repo) and no pane match
  it exits 1 and names them; outside every session it exits 1. A Bash that
  Claude Code runs has the pane id; a plain terminal in that directory does
  not, and gets the cwd rule.
- `rename` moves the record only. The worktree directory, the branch
  `work/<old>` and Claude's own session name (`claude -n <old>`, what the
  tab title shows) keep the old name, so after a rename the board says one
  name and the tab title another until the tab is remade (`rm` and `new`,
  or `open` once the pane is gone).
- `priority` and `rank` mean nothing to the CLI beyond storing them: `ls`
  does not sort by them, only the board does. A rank is any integer, two
  rows may share one (the board breaks the tie), and nothing renumbers.
- The SessionStart context is an offer, not a mechanism: the Claude in the
  tab decides whether to run `work status`, and Claude Code adds the context
  only when the hook is installed and the session starts inside a work
  session's worktree (a tab adopted after it started never saw it).
- `autoname` names from an excerpt, not the whole conversation: the first
  three prompts, the latest one and the head of the latest reply. A tab
  whose topic changed halfway may be named for where it started. The
  title is the model's: two to five words were asked for, six have been
  seen, Title Case is not guaranteed; the name derived from it can be
  long (`session-naming-via-claude-headless-prompt`). It needs the `claude`
  CLI logged in (`$SAVANNAH_CLAUDE_BIN` points elsewhere) and spends a few
  hundred tokens per tab; without it the first prompt's first line is the
  title. The board submits each tab once per run, so a tab it skipped for
  having no prompt is named only when the board next starts or when
  `autoname` is run by hand.
- `say` and `sh` press Enter by sending a carriage return after the text,
  which is what Claude Code's input box and a shell both take as Enter (the
  newline `it2 session run` appends is a line break to Claude Code, not
  Enter: found live 11/3/2026). Nothing checks that the box was empty first,
  so text you left half-typed in it goes along with what `say` adds.
