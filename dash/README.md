# savannah-dash

The work board behind bare `savannah`: an interactive terminal app, built
with [Bubble Tea](https://github.com/charmbracelet/bubbletea), for keeping
eight or more programming tasks across repos manageable from one screen.
Each task is a **work session** (see `../work/`): one iTerm2 tab, named
after the task, with interactive Claude in a git worktree above and a shell
in the same worktree below. The board lists every work session and every
other open Claude Code tab on this machine (via the session hook, see
`../hooks/`) in two sections you order yourself, **ACTIVE** and **LATER**,
flags the ones blocked on a permission prompt or your input (and counts
them in the header), shows which ones are polling something for you, and
gives you one key for each thing you used to do by hand:

- **Ctrl-N** opens a new task: a dialog asks for a name, a repo (from your
  folder of repos, or a path, or `scratch`), an optional first prompt, and
  whether to use a fresh worktree; Enter creates the tab.
- **Enter** on a work session focuses its tab, or reopens a closed one with
  `claude --continue`; with text typed on the prompt line it types that
  into the tab's Claude instead.
- **Ctrl-O** shows what that Claude said last (the last five turns of its
  transcript), refreshed while you read.
- **Ctrl-T** appends a note to the session's `notes.md`. On a plain tab (one
  you opened by hand, no work record) it first asks for a name, which makes
  that tab a work session without opening anything, then takes the note.
- **Ctrl-R** names a session. A plain tab gets a name (prefilled from its
  iTerm2 tab title; the tab becomes a work session, nothing opens). A work
  session gets a **title**: free text, capitals and spaces welcome, shown
  in the WORK column and the prompts instead of the name (`work title`);
  the name stays the identifier the CLI takes, and `savannah work rename`
  changes that one. Wherever a name is asked for (Ctrl-R, Ctrl-T, Ctrl-N)
  you may type words instead: "Fix the parser" becomes the title and
  `fix-the-parser` the name.
- **Shift-Up / Shift-Down** (Option-Up / Option-Down and Ctrl-Up / Ctrl-Down
  do the same; macOS takes Ctrl-arrows for Mission Control by default) move
  the highlighted session one row: within its section, or over the line
  between ACTIVE and LATER at the ends. The order lives in the records (`work move`), so it
  survives a restart and `work ls` sees the same.
- **Ctrl-X**, pressed twice, removes a finished session: closes the tab,
  removes a clean worktree, keeps the branch.
- **Ctrl-S** is the snapshot screen: save every session's state before a
  reboot (`work snapshot`), and after iTerm2 is back bring the missing tabs
  back with their Claude conversations resumed (`work restore`); a dry run
  shows what that would do.
- **Ctrl-L** reads savannah's log (`~/.claude/savannah/log/savannah.log`,
  shared by the board, the CLI, the hook and the shim) at the level you
  choose: normal, debug or verbose. The file always holds everything.

The dash owns no state and never touches a record, a worktree or a tab
itself. Every action is one `savannah work <verb>` subprocess (the same CLI
you can type, and the one the MCP shim's `work_*` tools call), so the board
and the command line can never disagree about what a session is; a refusal
from the CLI (a dirty worktree, a pane that is gone) lands on the status
line as the CLI printed it. Jumping to a tab goes through iTerm2's bundled
`it2` CLI (`session focus`, then `app activate` only after a hit, so a
stale pane never yanks iTerm2 forward).

LOCAL SESSIONS carries a **WORK** column. On the same 1-second tick as the
hook files the dash reads every `record.json` under `$SAVANNAH_WORK_DIR`
(default `~/.claude/savannah/work`) and joins each one to the hook session
whose `project` is that record's worktree (realpath compare, falling back
to a cleaned lexical compare when a path no longer exists). The join
happens at read time; the dash never writes into a work record or a hook
file. A work session whose tab is gone still shows as an **offline** row,
with the first line of its last note in the WHAT cell so you can remember
where you left off; Enter on it reopens the tab. The worktree path
`<repo>/.savannah-worktrees/<name>` renders as `<repo>/<name>` in PROJECT.

## Sections, order and polling

Rows sit in **ACTIVE** and **LATER**. A named session is in the section its
record's `priority` says (active unless moved) and in `rank` order inside
it (records from before ranks existed sort by creation time); unnamed tabs
close the LATER section in attention order (waiting first, then working,
done, idle). Attention never reorders a named row: a session that needs
you keeps its place and gets the glyph, the colour and the "N need you"
count instead, because a row that jumps while you reach for it is worse
than a glance at the header. Shift-Up/Down renumbers the section it leaves
and the one it lands in as 10, 20, 30, ... and runs `work move` only for
the records whose place changed; the board redraws in the new order first
and re-reads the records on its next tick, so a refusal from the CLI puts
the old order back. An unnamed tab shows its iTerm2 tab title in
parentheses in the WORK column (from `it2 session list --json`, every five
seconds) and cannot be moved until Ctrl-R names it: there is no record to
hold its rank. Both empty sections say where a move would land.

A session's Claude can tell the board what it is waiting on. `savannah work
status <name> -- "waiting for ctest (run 2 of 3)"` from the tab (the
SessionStart hook tells that Claude the command, see `../hooks/`) writes
the record's `status.json`; each call counts as one poll. While the report
is **live**, that row's state reads **polling** with its own glyph, the
WHAT cell says `polling: <text> (N polls, <age> ago)`, the panel carries a
line with the same, and the header counts the rows so marked ("N
polling"). Live means the session is working and the report is newer than
the prompt it is working on: the hook stamps the session's `updated` at
the prompt and touches it again only when the turn ends or needs you, so a
report left from an earlier turn is never shown as live; the panel still
shows it as "last status ... not live" until `work status <name> --clear`.

## The layout

The board uses the whole terminal. Under 120 columns the table is the
compact one (STATE, WORK, PROJECT, AGE, WHAT). From 120 columns on it adds
a TAB column (the iTerm2 tab number from the record), a NOTE column (the
first line of the last note) and widens PROJECT to the full `~/...` path
when every path fits in 40 percent of the room; WORK grows to the longest
name. The rest of the height is a **detail panel** for the highlighted
row: its name, path, branch, tab and state on one line (plus "later" when
it sits in LATER and "polling" while a report is live), the last prompt
its Claude was given under that, its progress report when there is one,
then its notes (every entry, oldest first,
the newest kept when they do not fit) beside the end of its Claude's last
reply (`work output --last 1`; a "(N more lines above)" marker says when
it was cut, and Ctrl-O shows it all). Under 90 columns the two halves
stack; under about 20 lines the panel is left out. The reply is fetched
when the highlighted row changes and whenever that row's hook file is
updated (the hook fires on every prompt and every finished turn), never on
a timer, so a long transcript is parsed once per turn, not once per
second. A plain tab's panel shows its last prompt and the hint to name it;
a mesh worker's shows its last line.

## The mesh (`--mesh`)

`savannah-dash --mesh` adds the **MESH WORKERS** section: the headless
savannah tasks on the nodes of the mesh. It is a thin client over the
`savannah` CLI, not a second implementation of song's wire protocol. It
browses the mesh with `savannah ls` on a 3-second tick to **discover every
node dynamically** (systems appear and drop as they join and leave the
network), polls each node's `task ls <node> --json` once a second, and
renders all workers under the sessions with a NODE column. On Enter it runs
`savannah task send <node> <id> <text>` for the highlighted worker. One
cursor moves over both sections, and the header shows a single "N need you"
count over sessions waiting plus pending worker decisions. A leading node
name pins one node instead of discovering (with `--addr` for a node not
advertised on mDNS) and implies `--mesh`.

Enter with an empty prompt (or Tab) on a worker opens a **tmux-like
full-screen view** of it: the transcript replayed with turn boundaries, the
live turn streaming as it happens, and the prompt line still active at the
bottom. Left/Right flips between workers like tmux windows; Esc returns to
the board. The view rides the CLI's `task tail` seam in a respawn loop
(tail replays, follows the live turn, exits; every rerun is a fresh full
replay), and the screen only swaps to a new run's buffer once it has caught
up with the last completed one, so redraws do not flicker.

The dash is also the inbox for the session-handoff pattern
(`docs/handoff.md`). A worker that ends its turn with a final
`DECISION_NEEDED {"question":...,"options":[...]}` line shows up highlighted
on the board with the parsed question in the LAST LINE column, the header
counts decisions waiting, and the full-screen view adds a banner with the
question and options above the prompt line. Type the answer and press Enter;
the answer is an ordinary `task send`, the worker resumes, and the flag
clears itself when the next turn's output replaces the marker.

## Build and run

```bash
cd dash
go build -o savannah-dash .

# The work board (no savannahd, no network): what bare `savannah` runs.
SAVANNAH_BIN=/path/to/savannah ./savannah-dash

# Plus the mesh: discover and show every node. Run from a directory where a
# relative --key path resolves (e.g. the build dir with mesh.key).
./savannah-dash --mesh --key mesh.key

# Pin a single node instead (use --addr for a node not advertised on mDNS):
./savannah-dash dbj-mac --addr 127.0.0.1:8790 --key mesh.key
```

The savannah CLI must be reachable (on PATH or via `$SAVANNAH_BIN`).
Jumping to a tab needs iTerm2's `it2`: on PATH, else the copy inside
iTerm.app (`/Applications/iTerm.app/Contents/Resources/utilities/it2`);
`$SAVANNAH_IT2` overrides both and may carry flags. `--no-mesh` and
`--sessions` are still accepted and mean nothing: the work board is the
default now.

## Keys

Board:

- **up / down** move the highlighted row (across every section)
- **Shift-Up / Shift-Down** (also **Option-Up / Option-Down** and
  **Ctrl-Up / Ctrl-Down**) move a named session one row within ACTIVE or LATER, or over the line between them
  from the bottom of ACTIVE or the top of LATER (`work move`); an unnamed
  tab must be named first
- On a **work session** row: **Enter** with an empty prompt line focuses
  its tab, or reopens it when the row is offline (`work open`); **type +
  Enter** types that text into its Claude (`work say`); **Ctrl-O** opens the
  output view (`work output`); **Ctrl-T** turns the prompt line into a note,
  then **Enter** saves it (`work note`) and **Esc** cancels it; **Ctrl-X**
  asks, and a second **Ctrl-X** removes the session (`work rm`); any other
  key in between withdraws the question; **Ctrl-R** turns the prompt line
  into the session's title (prefilled with the current one), **Enter**
  runs `work title` (an empty Enter clears it), **Esc** cancels
- On a plain **session** row (an open tab with no work record): **Enter /
  Tab** jumps to that terminal tab; **Ctrl-R** names it (the prompt line
  is prefilled with its iTerm2 tab title when that reads as words, else a
  name from its directory; **Enter** runs `work adopt`, which makes it a
  work session without opening anything, with the words as its title and
  a name derived from them; **Esc** cancels); **Ctrl-T** does the same and
  then asks for the note; **Ctrl-X** dismisses a stale entry
- **Tab** on any session row is the raw jump (`it2 session focus`), even on
  a work session: no reopen, no CLI
- On a **worker** row (`--mesh`): **type** a prompt then **Enter** sends it
  (`task send`); **empty Enter / Tab** opens the full-screen view; **Ctrl-X**
  removes it if finished (`task rm`; a running worker is refused, cancel
  first)
- **Ctrl-N** opens the new-task dialog from anywhere on the board
- **Ctrl-S** opens the snapshot screen, **Ctrl-L** the log view
- **Ctrl-C** quits; **Esc** quits too, unless a note, a name or a title
  is being typed

New-task dialog (Ctrl-N):

- **Name**: the work session's name (`^[a-z0-9][a-z0-9._-]{0,63}$`, never
  `t-<digits>`, `archive` or `snapshots`), or any words: the hint under
  the field shows the name they become (`Fix the parser` is
  `fix-the-parser`, `-2` when that is taken) and the words are saved as
  the title; checked in the dialog before anything runs
- **Repo**: a repo from your folder of repos (`savannah work repos DIR`
  remembers the folder; the dialog lists it through `work repos --json` and
  names the folder in the hint, or says it is using the cwd), typed in
  full or as a unique prefix; `scratch` for a task that belongs to no repo
  (no git, so no worktree); or a path (`~` expands)
- **Prompt**: what that Claude should do first (optional, one line)
- **Worktree**: `[x]` makes a fresh worktree on branch `work/<name>` (the
  default); **Space** turns it off to run in the repo itself
- **Tab / Shift-Tab** (or down / up) move between fields; **Enter** runs
  `work new` and closes the dialog once the tab is up; **Esc** cancels. A
  refusal from the CLI stays under the fields with what you typed, so a
  typo is a one-field fix.

Snapshot screen (Ctrl-S):

- shows what is open now (work sessions live and offline, plain tabs) and
  the latest snapshot (its stamp, when, how many sessions, its note), with
  older stamps after it
- **s** saves a snapshot now (`work snapshot --json`): every work session
  with its tab, worktree and Claude conversation id, plus every plain
  Claude Code tab; then reboot
- **r** restores the latest (`work restore --json`): every session whose
  tab is gone gets its tab back in its worktree with `claude --resume
  <conversation>` (so the Claude in it picks up where it left off); plain
  tabs come back as single panes; what is still open is left alone; the
  result lines land on the screen when the CLI is done (about two seconds
  per tab)
- **d** is the dry run (`work restore --dry-run --json`): the same lines,
  nothing touched
- **Esc / Tab** back to the board
- The board itself, right after a start, points at a snapshot taken in the
  last two days when named sessions are offline: one status line, once.

Log view (Ctrl-L):

- the tail of `savannah.log` (the last 512 KiB), one record per line, at
  the chosen level: **n** normal (INFO and above: what you did and what
  came of it, refusals, failures), **d** debug (adds every subprocess with
  its argv, exit status and timing, and every change of the row set), **v**
  verbose (adds payloads: output heads, the row list); **Left / Right**
  cycle; the header says the level and how many of the tail's lines it
  shows
- refreshed every two seconds while open, following the end unless you
  scroll up; **Up / Down / PgUp / PgDn / Home** scroll, **End** follows
  again
- **Esc / Tab** back to the board; the level you chose is kept for the
  next Ctrl-L
- The same reader in the shell: `savannah work log [--level normal|debug|
  verbose] [--lines N] [--follow]`.

Output view (Ctrl-O):

- the last five turns (your prompts and the assistant's text) of that
  session's transcript, refetched every two seconds
- **Up / Down / PgUp / PgDn / Home** scroll (scrolling up pauses follow;
  **End** jumps to the bottom and resumes following, as does scrolling back
  down to it); **Esc / Tab** back to the board; **Ctrl-C** quits

Full-screen worker view (`--mesh`):

- **Left / Right** switch to the previous/next worker (wraps around, all nodes)
- **Up / Down / PgUp / PgDn** scroll the transcript (scrolling up pauses
  follow; **End** jumps back to the bottom and resumes following, as does
  scrolling back to the bottom)
- **type + Enter** sends a prompt to the viewed worker; its turn streams in
- **Esc / Tab** back to the board, **Ctrl-C** quit

## Scope

Poll-based over the CLI's request/response JSON; the full-screen view re-runs
`task tail` (a fresh replay each time) rather than holding one connection
open. A push-based upgrade (a streaming `watch --json` event source per node,
or song property fan-out) can replace both underneath the same rendering; the
multi-node discovery already makes it a whole-mesh view. Wrapping counts
runes, so double-width glyphs can wrap a column early; a task that vanishes
mid-view keeps showing its "no such task" replay until you leave the view.

Work sessions, the join rules: exactly one live session is tagged per work
record and one record per session. Pane matches are settled first across every
record (a session running in a record's recorded Claude pane always belongs to
that record), then the remaining records each take the most recently updated
still-unclaimed hook session in their worktree, in name order. Two records on
one worktree (two `work new --no-worktree` in the same repo) therefore never
share or steal a tab, and when every tab is closed each gets its own offline
row; an offline row is never a join candidate. A directory under the work dir
is only read when its name passes the CLI's rule (`^[a-z0-9][a-z0-9._-]{0,63}$`,
not `t-<digits>`, not `..`, no trailing `.`, no `.lock` suffix), so the dash
and `work ls` list the same records (both also skip a record with no
worktree, and both take the name from the directory, never from the file).
When the sessions directory or the work directory cannot be read, the dash
keeps the last good list for that side, re-joins it against whatever the
other side read, and prints the error under LOCAL SESSIONS (even with zero
rows) until a read succeeds; live rows never flip to offline because of a
read error.

The exact command behind each key, so the board and the shell agree:

| Key | Command |
|---|---|
| Enter, empty | `savannah work open <name> --json` |
| Enter, text | `savannah work say <name> -- <text>` |
| Ctrl-T, Enter | `savannah work note <name> -- <text>` |
| Ctrl-T on a plain tab, Enter | `savannah work adopt <name> --session=<hook session id> [--title=<words>] --json`, then the note |
| Ctrl-R on a plain tab, Enter | `savannah work adopt <name> --session=<hook session id> [--title=<words>] --json` (no note) |
| Ctrl-R on a work session, Enter | `savannah work title <name> -- <words>`, or `savannah work title <name> --clear` on an empty Enter |
| Shift-Up / Shift-Down | `savannah work move <name> --active\|--later --rank=<N>`, once per record whose place changed |
| (STATE, WHAT, panel, header) | `<work dir>/<name>/status.json`, written by `savannah work status <name> -- <text>` from inside the tab |
| (WORK column of an unnamed tab) | `it2 session list --json` every 5 s, for the tab titles |
| Ctrl-O | `savannah work output <name> --last 5` (every 2 s) |
| (detail panel) | `savannah work output <name> --last 1` (on selection, and on each hook update of that row) |
| Ctrl-X, Ctrl-X | `savannah work rm <name>` |
| Ctrl-N | `savannah work repos --json`, then `savannah work new <name> --repo=<path> [--title=<words>] [--prompt=<text>] [--no-worktree] --json` |
| Ctrl-S | `savannah work snapshots --json` on entry; **s** `savannah work snapshot --json`; **d** `savannah work restore --dry-run --json`; **r** `savannah work restore --json` |
| Ctrl-L | none: the dash reads `$SAVANNAH_LOG_DIR/savannah.log` itself (`savannah work log` is the shell's reader) |

Values travel in the `=` form or after `--`, so text that begins with `-`
is never read as an option.

## Logging

The board appends to savannah's one log, `$SAVANNAH_LOG_DIR/savannah.log`
(default `~/.claude/savannah/log/`), the same file the work CLI, the hook
and the MCP shim write, one record per line in Go's `log/slog` text form:

```
time=2026-11-05T22:28:08.151-08:00 level=INFO msg=action src=dash do=say name=demo chars=41
time=2026-11-05T22:28:08.203-08:00 level=DEBUG msg=command src=dash cmd=savannah args="[work say demo -- ...]" ms=51 bytes=0
```

The file is always verbose: every record is written whatever its level,
and the `level=` stamp is what a reader filters on (Ctrl-L here, `savannah
work log` in the shell). `src=` says which piece wrote the line (`dash`,
`work`, `hook`, `shim`). Levels, from the board: VERBOSE for payloads (the
head of a subprocess's output, the row list, periodic polls such as the
tab titles every five seconds and the output view's refetch), DEBUG for
every other subprocess (argv, exit status, milliseconds) and for a change
of the row set, INFO for what you did (`msg=action do=say|open|note|rm|
adopt|title|move|new|jump|dismiss|snapshot|restore`) and what came of it,
WARN for a refused or failed command (with the CLI's stderr), ERROR when
the board itself broke. Rotation at 10 MiB with three old files kept
(`savannah.log.1` to `.3`), the scheme Python's `RotatingFileHandler` uses
on the CLI side, so either writer may rotate and this one reopens when the
file under its path has been swapped. Nothing here is stdlib-external:
`log/slog` is Go's own.

## What does not work

- The prompt line is one line: a multi-line prompt goes through the tab
  itself.
- The output view is the CLI's rendering of the transcript's last five
  turns, polled every two seconds; it is not a live stream, and a turn in
  progress shows only once Claude Code has written it to the transcript.
- A repo typed as a unique prefix of a listed name is taken as that repo, so
  a relative path that happens to prefix a repo name must be written as
  `./name`.
- Jumping needs iTerm2 and `it2`. tmux work sessions are reserved, not
  implemented. An offline row whose recorded pane was closed reports "tab
  not found" on Tab; Enter reopens it instead.
- A second Claude tab you open by hand in a worktree that already has its
  record's tab shows no WORK name (one tag per record).
- The panel shows the END of the last reply (where things are now) and the
  newest notes; the rest is one Ctrl-O away. Its text is wrapped by rune
  count, so double-width glyphs can wrap a line early.
- The reply column refreshes only when the hook updates that row: a turn in
  progress shows nothing new until Claude Code has finished it.
- Under 120 columns there is no NOTE or TAB column, under 90 the panel
  stacks its two halves, under about 20 lines there is no panel at all.
- A plain tab has no notes and no reply column until Ctrl-R or Ctrl-T names
  it, and naming needs the tab's hook file: a tab still sitting at a trust
  or MCP prompt has none yet.
- Moving a row is one step per Shift-Up/Down; there is no mouse drag and no
  multi-select. An unnamed tab cannot move (nothing holds its rank): name
  it first. Two boards open at once each write the order they last saw;
  the records are the truth, both re-read them every second, the last move
  wins.
- "polling" is self-reported: the board shows what the tab's Claude wrote
  with `work status`, nothing else, so a wait that was not reported looks
  like plain "working". A report is live only while that session is working
  and the report is newer than its prompt; a permission prompt mid-turn
  flips the row to waiting and hides the live marker until the turn
  resumes; a report from an earlier turn stays in the panel as "not live"
  until `work status <name> --clear`.
- Tab titles come from `it2` every five seconds and only for iTerm2 panes;
  a tab with no title shows an empty WORK cell. The title is whatever
  Claude Code or the shell set, so it changes as the tab's process changes.
- A work session's own title is the board's and the CLI's (`work ls` shows
  it); the iTerm2 tab keeps the name, since `claude -n <name>` is what
  names the tab. The WORK column grows to 28 columns for titles and cuts
  longer ones with `~`; the panel head shows the whole title.
- The board does not rename a session's name (the slug): Ctrl-R on a work
  session edits the title. `savannah work rename OLD NEW` changes the name;
  the worktree path and the branch keep the old one (the CLI's rule), so
  PROJECT still shows the old directory.
- A snapshot is the layout and the conversation ids, not the terminals'
  contents: a turn that was running when the machine went down is lost,
  and the restored Claude resumes from its last finished turn. Restore
  never closes anything, so tabs iTerm2 restored on its own sit next to
  the ones it remakes (close the stale ones yourself); plain tabs come
  back as single panes (no shell below); the screen has no note field
  (`savannah work snapshot --note "..."` does); every tab costs about two
  seconds of `it2`.
- The log view reads the current file only (`savannah.log.1` to `.3` are a
  `less` away), the last 512 KiB of it, and a line without a `level=`
  stamp passes every filter. The board never blocks on the log: a log
  directory it cannot write means no log, not no board. Two processes
  rotating in the same instant can lose a few lines.
- Ctrl-X on a named (adopted) tab closes its pane like any other session
  and keeps the directory: it is the CLI's `work rm`, which never runs git
  on a directory it did not create.
- Removing a session is `work rm` without flags: a worktree with uncommitted
  changes is refused with the CLI's hint, and the dash has no force. Run
  `savannah work rm <name> --force` (or `--keep-worktree`) yourself.
- A `work new` that fails after the tab was created (a bad first prompt, a
  worktree the CLI could not make) is reported from the CLI's stderr; the
  dash does not clean up on its behalf.
