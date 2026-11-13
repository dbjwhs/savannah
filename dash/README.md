# savannah-dash

The work board behind bare `savannah`: an interactive terminal app, built
with [Bubble Tea](https://github.com/charmbracelet/bubbletea), for keeping
eight or more programming tasks across repos manageable from one screen.
Each task is a **work session** (see `../work/`): one iTerm2 tab, named
after the task, with interactive Claude in a git worktree above and a shell
in the same worktree below. The board lists every work session and every
other open Claude Code tab on this machine (via the session hook, see
`../hooks/`) in three sections you order yourself, **NOW**, **ACTIVE** and **LATER**,
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
- **Ctrl-A, Ctrl-A** asks every idle or done tab what it is doing (`work
  ask --all`): the rows turn yellow and back to green as each answers,
  then the replies open as one screen, and each work session keeps its
  reply as a note (NOTES in the panel, the NOTE column) and as a summary
  line the panel shows. Working and waiting tabs are never
  interrupted. One turn of each asked tab's context, hence the two
  presses.
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
- **Ctrl-P** puts the highlighted session in **NOW**, the section above
  ACTIVE for the few things you are pushing on today, at its bottom; on a
  NOW row it sends the session back to the top of ACTIVE. One key, a
  toggle, and it works while grouped or filtered. Nothing lands in NOW by
  itself (not a new session, not a named tab): only Ctrl-P and `work move
  --now`. An empty NOW is one dim hint line; the header counts "N now";
  there is no cap, a long NOW is yours to prune. The state is the record's
  priority, so each machine's board keeps its own NOW.
- **Shift-Up / Shift-Down** (Option-Up / Option-Down and Ctrl-Up / Ctrl-Down
  do the same; macOS takes Ctrl-arrows for Mission Control by default) move
  the highlighted session one row: within its section, or over the line
  between NOW, ACTIVE and LATER at the ends. The order lives in the records (`work move`), so it
  survives a restart and `work ls` sees the same.
- The tabs you opened by hand get **named for you** when the board comes
  up: each plain tab that has a prompt is handed to `savannah work
  autoname`, which asks a headless Claude (safe mode, no tools, haiku,
  about three seconds and a fraction of a cent per tab) for a title over
  an excerpt of the tab's own transcript, derives the name and adopts the
  tab. The status line says what was named; Ctrl-R changes a title you do
  not like. `--no-autoname` turns it off for one run, `"autoname": "off"`
  in the board file for good.
- **Ctrl-G** groups the rows by repository: inside NOW, ACTIVE and LATER every
  repository's sessions come together under a dim header naming it (and
  counting them), so every ZFS tab is one glance. Inside the group the
  main checkout leads and the other checkouts follow by name
  (`zfs-DLPX-97306`, `zfs-DLPX-98816`: a `git worktree add` or a clone
  named `<repo>-<ID>` beside the repository), the PROJECT column naming
  the checkout and where in it the tab sits. The choice is remembered
  (`~/.claude/savannah/board.json`); `savannah --group` and `--no-group`
  set it for one run.
- **Ctrl-F** sets the fade: a row you have not touched for three hours
  (the default) is drawn faint, in its state colour, exactly where it is,
  so on a board of thirty green rows the ones with activity today stand
  out. Nothing moves and nothing hides; a row that needs you or is
  working never fades. Ctrl-F cycles 1h, 3h, 8h, 24h and off, remembered
  in the board file; `savannah --fade 90m` (or `--fade off`) sets it for
  one run.
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
file. A work session whose tab is gone is a closed session: its record
and notes are kept, and it sits in the **HISTORY** section at the bottom
(closed by default, Ctrl-E opens it) as an **offline** row with the first
line of its last note in the WHAT cell so you can remember where you
left off; Enter on it reopens the tab. The worktree path
`<repo>/.savannah-worktrees/<name>` renders as `<repo>/<name>` in PROJECT.

## Sections, order and polling

Rows sit in **NOW** (what you are pushing on, Ctrl-P), **ACTIVE** and
**LATER**, with **HISTORY** below them for the
closed sessions (a work record whose tab is gone): one header line with
the count, closed by default so the live rows have the screen; **Ctrl-E**
opens it (the rows newest first, Enter reopens one, Ctrl-X removes it,
Ctrl-T and Ctrl-O work as on any work row) and closes it again, and the
choice is remembered in the board file (`"history": "open"`). The header
counts closed sessions apart ("12 sessions   3 closed"). A closed session
has no place in the rank order, so a drag on it is refused; reopening it
puts it back in its section. A named session is in the section its
record's `priority` says (`now`, `later`, else active) and in `rank` order inside
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

**Ctrl-G** gathers each section's rows by repository. The sections and
the rank order inside them stay; what changes is that one repository's
rows sit together, repositories in the order their first row appears (so
the row you ranked first still decides which repository leads), each
under a dim header `  zfs (4)` (plus the full path on a wide board). A
row's repository is read from its directory, never from a git
subprocess: `<repo>/.savannah-worktrees/<name>` is that repo, otherwise
the nearest ancestor holding `.git` is the row's checkout, and that
checkout's repository is where a `.git` file from `git worktree add`
points, or the sibling `<X>` when the checkout is named `<X>-<ID>` beside
a repository `<X>` (`zfs-DLPX-98816` next to `zfs`, worktree or clone;
the longest such sibling, so `dlpx-qa-gate-DP-2702` goes under
`dlpx-qa-gate`), else the checkout itself; a directory under no
repository is its own group, named by its path. Inside a group the main
checkout's rows come first and the other checkouts follow by name, the
rank order kept inside each, so a repository reads as a tree:

```
  zfs (6)   ~/ng/delphix/zfs
 ·  idle   Resuming ZFS Handoff Session   .                 38   19h
 ·  idle   Pool Close Dump Cache Fix      zfs-DLPX-97306    34   19h
 ·  idle   Resume DLPX-97306 PR #2352     zfs-DLPX-97306    40   19h
 ·  idle   ZFS DLPX-98816 handoff         zfs-DLPX-98816    41   19h
 ·  idle   ZFS DLPX-98816 Handoff Resu~   zfs-DLPX-98816    35   19h
 ·  idle   ZettaCache index dump versi~   zfs-CP-11610      39   19h
```

The PROJECT column shows the checkout (`.` for the main one, the session
name for a savannah worktree, `zfs-DLPX-98816` for one beside the
repository) and the path inside it when the tab sits in a subdirectory
(`dash`, `zfs-DLPX-98816/module`), since the header already names the
repository. Dragging is refused while grouped (the drag edits the rank
order, which the grouped view does not show in full): Ctrl-G back first.
The header says "by repository", the help line "^G ungroup". The choice
is written to `$SAVANNAH_BOARD_FILE` (default
`~/.claude/savannah/board.json`, `{"group": "repo"}`) so the next board
starts the same way; `--group` and `--no-group` on the command line
(`savannah --group`) win for one run.

**Naming the tabs you opened by hand.** On every sessions read the board
looks for plain rows (a hook session no record claims) that have a prompt
(the hook's `last_prompt`) and have not been submitted this run, and hands
them all to one `savannah work autoname --json <ids>` (see `../work/`: a
headless Claude titles an excerpt of each tab's transcript, the name is
derived from the title, the tab is adopted with it; the first prompt's
first line is the title when the call fails). One run at a time; the
status line reads "naming 3 tabs from what they are doing..." then "named
3 tabs: ZFS Send Resume Review (zfs-send-resume-review), ..." (with "; N
from the first prompt (claude unavailable)" when the fallback was used,
and the first failure when one failed), and the names show on the next
read. A tab is submitted once per board run: a tab with no prompt yet is
submitted once it has one, a tab the CLI refused is not retried. Nothing
is ever typed into a tab for this. Off with `--no-autoname` for one run
(`savannah --no-autoname`) or `"autoname": "off"` in the board file
(written by hand; nothing on the board sets it).

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

Rows are coloured like a traffic light: red for a session that needs you
(a permission prompt or a question), yellow for one still working (a
polling row too), green for one idle or done (ready for you, nothing
pending), faint for an offline work session. Claude Code's idle nudge, a
notification sent once a tab has sat unused for a minute, is not "needs
you": the hook records it as a heartbeat, so an idle tab stays green.

On top of the colour, age: a row whose last activity is older than the
fade (3 hours unless Ctrl-F or `--fade` says otherwise) is drawn faint in
that same colour, where it is. Activity is the hook file's `updated`
stamp, which every event refreshes; the idle nudge lands about a minute
after a turn ends, so a row fades about three hours after its last turn.
An offline row goes by its record's stamp. A row that needs you or is
working is never faded. The AGE column still gives the exact age; the
fade only decides where your eye lands first.

A TICKET column sits after WORK whenever any row has one: the ids the
row's work hangs off, the ticket (`DLPX-98816`, `DP-2702`, `TOOL-31974`)
and the pull request (`PR-940`), each from the first of these that names
one: the checkout's directory name (`zfs-DLPX-98816` beside the
repository, or a savannah worktree's name), the work session's name and
branch, its title, then the last prompt and the tab's `work ask`
summary. A ticket is `KEY-NUMBER` (two to five letters or digits, a
dash, two to seven digits); a pull request is `PR` with a number in any
spelling, or a GitHub pull request URL in the summary or the notes,
which comes first because it says which repository. In names the key
may be any case, in free text only an upper-case key counts. The detail
panel's head carries them too. No row with an id, no column.

Both are links. A ticket links to the tracker's page, by default
`https://perforce.atlassian.net/browse/{ticket}` (the author's Jira).
Another tracker is one line, once per machine:

```bash
savannah --ticket-url 'https://yourcompany.atlassian.net/browse/{ticket}'
```

`{ticket}` stands for the key (a pattern ending in `/` gets the key
appended); the choice is written to the board file (`"ticket_url"`),
`--ticket-url off` turns ticket links off (remembered too), and
`--ticket-url default` goes back to the built-in pattern.
A pull request links to the URL the tab gave, else to the checkout's
`origin` remote on GitHub plus `/pull/N` (`git remote get-url origin`,
once per checkout per run; another host, or a prompt naming a PR in a
plain tab under no repository, gives no link). The link is a terminal
hyperlink (OSC 8): in iTerm2, Cmd-click it. **Ctrl-B** opens the row's
links with the system opener (`open`, or `xdg-open`) for when a click
is not to hand; the status line says what opened, or why nothing could.

The board uses the whole terminal. Under 120 columns the table is the
compact one (STATE, WORK, PROJECT, AGE, WHAT). From 120 columns on it adds
a TAB column (the iTerm2 tab number from the record), a NOTE column (the
first line of the last note) and widens PROJECT to the full `~/...` path
when every path fits in 40 percent of the room (grouped, PROJECT stays
the path inside the repository and the group header carries the full
path instead); WORK grows to the longest name. The rest of the height is a **detail panel** for the highlighted
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
  **Ctrl-Up / Ctrl-Down**) move a named session one row within NOW, ACTIVE or LATER, or over the line between
  two of them from the bottom of one or the top of the next (`work move`); an unnamed
  tab must be named first
- **Ctrl-P** moves a named session into NOW (at the bottom) or, from NOW,
  back to the top of ACTIVE (`work move --now` / `--active`, the sections
  renumbered like a drag); refused on an unnamed tab or a closed session
- On a **work session** row: **Enter** with an empty prompt line focuses
  its tab, or reopens it when the row is offline (`work open`); **type +
  Enter** types that text into its Claude (`work say`); **Ctrl-O** opens the
  output view (`work output`); **Ctrl-A** says how many tabs it would ask
  and a second **Ctrl-A** asks them all what they are doing (`work ask
  --all`, every idle or done tab, plain ones too; the replies open as a
  screen when the CLI returns, up to two minutes; once there are replies
  Ctrl-A shows them again and `a` on that screen asks again); **Ctrl-T**
  turns the prompt line into a note,
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
- **Ctrl-G** groups the rows by repository inside each section, and back
  (remembered in the board file; a drag is refused while grouped)
- **Ctrl-F** cycles the fade (1h, 3h, 8h, 24h, off): rows untouched for
  longer are drawn faint (remembered in the board file)
- **Ctrl-E** opens and closes HISTORY, the closed sessions at the bottom
  (remembered in the board file)
- **Ctrl-K** finds: the prompt line becomes a filter and the board shows
  only the rows that match as you type. Every word typed must appear,
  case aside, in the row's name, title, ticket or pull request, project
  path, branch, last prompt, `work ask` summary, last note's first line
  or (a plain tab) its tab title; a mesh worker matches on node, id,
  title and worktree; a closed session is searched whether HISTORY is
  open or not. **Enter** keeps the filter and gives the prompt line back
  (the header says `matching "zfs"`), **Esc** clears it, **Ctrl-K** edits
  it again; a drag is refused while filtered
- **Ctrl-B** opens the row's ticket and pull request in the browser
  (the TICKET column; Cmd-click does the same in iTerm2)
- **Ctrl-S** opens the snapshot screen, **Ctrl-L** the log view
- **Ctrl-C** quits; **Esc** quits too, unless a filter is in force (it
  clears that first) or a note, a name or a title
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
| Shift-Up / Shift-Down, Ctrl-P | `savannah work move <name> --now\|--active\|--later --rank=<N>`, once per record whose place changed |
| (STATE, WHAT, panel, header) | `<work dir>/<name>/status.json`, written by `savannah work status <name> -- <text>` from inside the tab |
| (WORK column of an unnamed tab) | `it2 session list --json` every 5 s, for the tab titles |
| Ctrl-O | `savannah work output <name> --last 5` (every 2 s) |
| Ctrl-A, Ctrl-A (or `a` on the replies screen) | `savannah work ask --all --json` (waits up to 120 s for the replies) |
| (detail panel) | `savannah work output <name> --last 1` (on selection, and on each hook update of that row) |
| Ctrl-X, Ctrl-X | `savannah work rm <name>` |
| Ctrl-N | `savannah work repos --json`, then `savannah work new <name> --repo=<path> [--title=<words>] [--prompt=<text>] [--no-worktree] --json` |
| Ctrl-S | `savannah work snapshots --json` on entry; **s** `savannah work snapshot --json`; **d** `savannah work restore --dry-run --json`; **r** `savannah work restore --json` |
| Ctrl-L | none: the dash reads `$SAVANNAH_LOG_DIR/savannah.log` itself (`savannah work log` is the shell's reader) |
| Ctrl-G | none: the rows regroup in the dash; the choice is written to `$SAVANNAH_BOARD_FILE` (`~/.claude/savannah/board.json`) |
| Ctrl-F | none: the fade changes in the dash; `"fade": "8h"` (or `"off"`) is written to the board file |
| Ctrl-B | `open <url>` (macOS) or `xdg-open <url>` per link on the row; the PR link may first cost one `git -C <checkout> remote get-url origin`, cached for the run |
| Ctrl-E | none: HISTORY opens or closes in the dash; `"history": "open"` is written to the board file |
| Ctrl-K | none: the filter runs over what the board already holds |
| (start, and each new plain tab with a prompt) | `savannah work autoname --json <hook session ids>`, one run at a time |

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
- Ctrl-A asks only the tabs whose hook state is idle or done. A Claude at
  a question of its own (no Notification fires for AskUserQuestion) reads
  as done and is asked anyway; the board cannot see inside the tab. The
  screen shows what the CLI collected within its timeout; a slower reply
  is one Ctrl-O away, and the panel's summary line keeps the last reply
  only for work sessions (a plain tab has no record to keep it in).
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
- Naming the tabs you opened by hand costs tokens (a few hundred per tab,
  on haiku) and needs the `claude` CLI logged in; without it the first
  prompt's first line is the name. The title is the model's reading of an
  excerpt (first prompts, latest prompt and reply): a tab that changed
  topic may be named for where it started, and six-word titles happen.
  Each tab is tried once per board run; a tab with no prompt yet is tried
  once it has one, a refused one not again until the board restarts. The
  board file key `"autoname": "off"` is hand-written; no key sets it.
- Grouping (Ctrl-G) is by directory, not by project: a second clone of a
  repository groups with it only when it is a git worktree or is named
  `<repo>-<ID>` beside it, so two clones named otherwise are two groups,
  and a directory under no `.git` is a group of its own named by its
  path. Inside a group the checkouts sort by name, so the rank order holds
  only inside one checkout while grouped. The repository of a
  directory is looked up once per board run and cached, so a `git init`
  under a running board shows after a restart. Dragging is refused while
  grouped; the help line's "^C quit" is the first hint dropped when the
  terminal is narrower than the help (141 columns for the work board).
- The filter (Ctrl-K) searches what the board holds for a row, not the
  whole of its notes (only the highlighted row's notes are read) and not
  the transcripts; a session is found by its notes' last line, not by a
  decision noted a week ago. The filter is not remembered between runs.
- Links are OSC 8 hyperlinks: iTerm2 shows them, tmux strips them
  unless `set -ga terminal-features ",*:hyperlinks"` (Ctrl-B works
  regardless). A ticket links to the author's Jira unless `--ticket-url`
  says otherwise, so a key from another tracker opens a wrong page
  there until it is set; a
  pull request links nowhere when its checkout's origin is not on
  GitHub and no URL for it was seen in the tab's summary or notes.
- A ticket is pattern-matched, never looked up: a slug such as `x86-64`
  or `iso-8601` in a checkout or session name reads as one, and a ticket
  only ever mentioned in lower case in a prompt is not found. Two show at
  most, from one source: a checkout named for one ticket hides a second
  ticket in the title.
- The fade goes by the hook file's `updated` stamp, so a tab whose Claude
  has been running one long turn for four hours is not faded (it is
  working), while a tab you read but did not type into fades on schedule:
  reading is not activity the hook can see.
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
