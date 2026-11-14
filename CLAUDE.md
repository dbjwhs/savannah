# CLAUDE.md — savannah

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

Claude-to-Claude agent mesh on the [song](https://github.com/dbjwhs/song) substrate.
Read `idl/agent.song` first; it is the contract everything else serves.

## What this is

Every machine runs `savannahd`, a C++20 daemon exposing that machine's headless
Claude (`claude -p --output-format stream-json`) as a song service (`AgentNode`),
discoverable over mDNS, streamed, HMAC-authenticated, process-supervised.
The MCP shim (`shim/peer.py`, stdlib-only Python, wraps the savannah CLI)
gives stock Claude Code `list_peers`, `ask_peer`, `peer_status`, and the
`task_*` tools that drive persistent worker sessions on peer nodes (Phase 5a).
The model decides when to delegate. No forking Claude Code. Alongside the
mesh, two pieces of purely local tooling (no daemon, no song, no tokens):
`hooks/` records the state of your own open Claude Code tabs, and `work/`
turns the owner's tab+split habit into named work sessions (interactive
Claude in a git worktree, a shell below, plain-text notes; Phase 5d),
driven through iTerm2's own `it2` command. A bare `savannah` is the board
for them (the dash in work mode; `savannah mesh` is the mesh board), and the
hub (5e) is an optional layer, not the front door (direction reset 5f,
11/3/2026).

Design doc of record: `DESIGN.md` in this repo (architecture, rationale,
roadmap, append-only decision log). One-liners:
- **`ask` is stateless.** Every `ask` = fresh agent invocation. Persistent,
  resumable sessions exist too, but only as Phase 5a tasks (`task_*`), never
  through `ask`.
- **Single-flight per node in v1.** `NodeBusy` otherwise.
- **song's value here is plumbing, not speed.** Discovery, supervision, typed
  contracts, streaming, security. LLM latency dwarfs wire latency.

## House rules (non-negotiable)

- C++20. Never below C++17 idioms. Concepts/`std::endian`/ranges welcome.
- `-Wall -Wextra -Werror` clean or it is not done.
- Tests are the definition of done. Unit + integration for every component.
  Integration tests run against `fake-claude`, never the real CLI (deterministic,
  zero tokens). The live-schema check (`test_live_schema`) runs the real CLI
  behind `SAVANNAH_LIVE=1` only; without it, ctest reports it Skipped.
- Zero new dependencies. libsong + POSIX + platform crypto. The TOML-subset and
  JSON-subset parsers are hand-written in this repo on purpose.
- Honest limitations: every README/doc states what does NOT work. No overclaiming.
- No em dashes in docs or user-facing strings (author's rule).

## Build

```bash
./install.sh            # all of the below plus the hook and ~/.local/bin/savannah; --pull, --test, --dry-run
# or by hand:
# song checked out AND BUILT as a sibling (or pass -DSONG_ROOT=/path/to/song).
# CMake hard-fails unless ../song/build has libsong.a and songc.
git clone https://github.com/dbjwhs/song ../song
cmake -S ../song -B ../song/build && cmake --build ../song/build --target song songc
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j
cmake --build build --target dash   # the board (Go, not in ALL): bare savannah execs dash/savannah-dash
ctest --test-dir build --output-on-failure
ctest --test-dir build -R test_json --output-on-failure   # single test
```

`songc` (built from SONG_ROOT) code-generates `generated/savannah.hpp` from
`idl/agent.song` at build time. Never edit generated code; edit the IDL.

## Layout

| Path | What |
|---|---|
| `install.sh` | One-shot build on a machine: tool check (git, cmake, c++, python3; go, claude and it2 warned about), song cloned beside the checkout when missing and built when `libsong.a`/`songc` are missing (or `--pull`), savannah configured (Debug unless the cache says otherwise, `--release`) and built, the dash target when go exists, `hooks/install.py`, `build/savannah` linked into `--bin DIR` (default `~/.local/bin`, warns when off PATH). `--pull` (ff-only, both repos), `--test` (ctest), `--dry-run` (prints `+ cmd` lines, runs nothing), `--no-hook`, `--no-link`, `--song DIR` (`$SONG_ROOT`). Tested by `test/test_install.py` (dry-run only). |
| `idl/agent.song` | The AgentNode contract. Compiles via songc. |
| `src/config.{hpp,cpp}` | Hand-written TOML-subset parser + `NodeConfig`. |
| `src/json.{hpp,cpp}` | Hand-written minimal JSON parser (stream-json needs). |
| `src/stream_json.{hpp,cpp}` | claude stream-json events → `AgentChunk` mapping. |
| `src/agent_process.{hpp,cpp}` | Spawn/supervise the headless agent, line pump, timeout, kill. `UniqueFd`, `Child` (kill and reap in the destructor) and `spawn` are the process helpers (`run_git` in the supervisor uses them too); `base_agent_argv` is the argv every invocation starts with; `failure_reason` names why one failed. Unit-tested by `test/test_agent_process.cpp`. |
| `src/task_supervisor.{hpp,cpp}` | Phase 5a task engine: persistent, resumable worker sessions (`claude --resume` per turn), each bound to a workdir or git worktree. Authoritative task table lives here (NOT in song objects; finding 19). Per-worker single-flight on a background thread; auto-continues on `error_max_turns` up to a budget (state `incomplete`, not `failed`, when the budget runs out). |
| `src/savannahd.cpp` | The node daemon: song runtime, dispatchers, single-flight. |
| `cli/savannah_main.cpp` | Human remote control. Bare `savannah` = exec the dash in work mode, the board (flags before any verb, `savannah --group`, go to the dash; `$SAVANNAH_DASH_BIN`, else the path baked in as `SAVANNAH_DASH_BIN` = `dash/savannah-dash`, else `savannah-dash` on PATH; exit 1 with a build hint when missing; sets `SAVANNAH_BIN` to its own path so the dash runs `savannah work ...`); `savannah mesh ...` = the dash with `--mesh`; `savannah hub ...` and `savannah work ...` exec `python3` on `work/savannah_work.py` (path baked in as `SAVANNAH_WORK_PY`, env overrides) before any song code. Mesh verbs: `savannah ls`, `savannah <ask\|info\|status> <node>`, and `savannah task <new\|ls\|status\|send\|tail\|cancel\|rm> <node>` (local pipes, or mesh via mDNS/--addr with --key). Task state needs a persistent daemon, so `task` targets a TCP/mesh node, not pipe-spawned `local`. |
| `tools/fake_claude.cpp` | Fake agent speaking genuine stream-json. Scenario-driven. |
| `shim/peer.py` | MCP stdio server (stdlib-only) wrapping the CLI: list_peers/ask_peer/peer_status + task_new/list/status/send/output/cancel/rm, plus the `work_*` tools (new/adopt/list/note/notes/say/ask/output/shell/jump/open/move/rename/title/status/rm; `work_new`/`work_adopt` take `title`; `work_ask(question?, names?, all?, timeout?)` = `work ask`) that drive `work/savannah_work.py` via `$SAVANNAH_WORK_BIN` (default: the shim's own interpreter, `sys.executable`, running `<realpath of peer dir>/../work/savannah_work.py`). The work tools manage the OWNER's local interactive iTerm2 tabs on this machine, not mesh workers; `work_say` types into the pane and returns nothing, `work_output` is the read side (last N turns parsed from the transcript Claude Code writes). Module exit 1/2 surface as MCP tool errors. Logs to savannah's one log as `src=shim` (`work/savannah_log.py`): INFO `tool` per call (tool, sorted argument keys, a 120-char head of prompt/text/cmd), DEBUG `tool done` with ms, WARN `tool error`/`unknown tool`. |
| `hooks/` | `session-status.py`: a Claude Code hook that records each interactive session's live state (idle/working/waiting/done) to `~/.claude/savannah/sessions/<id>.json`, so the dash can show every open tab and flag the ones needing you (the `idle_prompt` notification, Claude Code's nudge after a minute unused, is a heartbeat: timestamp refreshed, state kept, never `waiting`; a file an older hook stamped `waiting` for it is rewritten `idle` on the next nudge, and a nudge over a real permission prompt keeps that prompt's message and type). `work/savannah_work.py` reads hook files through `normalize_hook` (waiting + `idle_prompt` = idle), the same rule as the board's `normalizeSession`, so `work ask` and the board agree on which tabs are idle. `SAVANNAH_HOOK_IGNORE` set = do nothing at all (savannah's own `claude -p` calls set it). Also records `transcript_path` (kept across events that omit it) and `last_prompt` (UserPromptSubmit `prompt`, or `user_prompt`; first non-blank line, 200 chars, kept until the next prompt) so a tab can tell you what it was doing. On SessionStart only, when the cwd is a work record's worktree (pane match on `$ITERM_SESSION_ID` preferred, else newest), it prints `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}` telling that Claude the exact `work status <name> -- "..."` command for progress reports (statements, not instructions); otherwise no join logic in the hook. `install.py` merges it into settings.json (idempotent, backs up, `--uninstall`). Pure local files, no daemon/network/tokens. Logs as `src=hook` (DEBUG per event with session, state, cwd and file; INFO `work context`; WARN when its file cannot be written); an unimportable logger or an unwritable log dir is a no-op, never a byte on stdout. Tested by `test/test_hook.py`. |
| `work/` | `savannah_work.py` (Phase 5d, stdlib CLI; the entry point, the one path everything names, holding the core verbs, `build_parser`, `main` and the re-exports the tests reach, with the rest beside it as flat modules imported by plain name, not a package, each importing only earlier ones and never the entry file: `savannah_core.py` = `UserError`, constants, paths, atomic writes, records, notes, status, text and output helpers; `savannah_terminal.py` = the `Terminal` seam and `ITerm`; `savannah_git.py` = `run_git`, the repo root, worktrees, `.worktreeinclude`; `savannah_sessions.py` = hook sessions and the join; `savannah_transcript.py` = transcript finding and parsing; `savannah_tabs.py` = `claude_command`, `reclaim_pane`, `remake_tab`, `adopt_session`; `savannah_hub.py` = `repos` and `hub`; `savannah_snapshot.py` = `snapshot`/`snapshots`/`restore`; `savannah_autoname.py`; `savannah_ask.py`; a constant or `atomic_write` is read as `savannah_core.NAME` at call time, and a test patches a function on the module that owns it): local work sessions, one iTerm2 tab per work item, upper pane interactive `claude -n <name>` in a git worktree (`<repo>/.savannah-worktrees/<name>`, branch `work/<name>`), lower pane a shell in the same worktree, plus append-only `notes.md`. Verbs `new/adopt/ls/note/notes/say/ask/sh/jump/open/output/rm/move/rename/title/status/repos/snapshot/snapshots/restore/log/hub`; exit 0/1/2; `--json` on ls/new/adopt/note/open/output/move/rename/title/status/repos/snapshot/snapshots/restore/log/hub. Records carry `title` (free text, first non-blank line, 120 chars, `title_text`; `new`/`adopt --title TEXT`, `rename` keeps it): `title NAME [TEXT...] [--clear] [--json]` sets, clears or prints it; `ls` shows a TITLE column after NAME and `ls --json` carries `title`; the name stays the identifier everywhere (worktree, branch, `claude -n`). `snapshot [--note TEXT] [--json]` writes `<work dir>/snapshots/<YYYYMMDD-HHMMSS>.json` (+ `latest.json`; `-2` on a same-second clash): every record with tab, panes, worktree and, from the hook join at that moment, Claude Code's `session_id` and `transcript_path`, plus every plain tab (project, pane, title, session id); `snapshots [--json]` lists them newest first (`{stamp,path,taken,taken_local,work,live,plain,note}`); `restore [STAMP|latest|FILE] [--dry-run] [--no-plain] [--json]` remakes each tab whose pane is gone in its worktree with `claude -n NAME --resume <session id>` (`how=resume`; else `continue` when the directory has a transcript, else `fresh`), plain tabs as single panes (`Terminal.create_tab`), skips `alive`/`no record`/`worktree gone`/`not open when the snapshot was taken`, never closes anything, exit 1 when anything failed, JSON `{stamp,path,dry_run,restored:[{name,how,tab_id}],plain:[{project,how,tab_id}],skipped:[{name|project,why}],failed:[{name|project,error}]}`; `remake_tab(rec, term, live, how, resume_id)` is the shared half of `open` and `restore`; `snapshots` is a reserved name. `log [--level normal|debug|verbose] [--lines N] [--follow] [--path] [--json]` reads savannah's one log (current file only; normal = INFO and above, debug adds DEBUG, verbose adds VERBOSE; `--follow` polls every 0.5 s and reopens a replaced file). Logging: `work/savannah_log.py` (stdlib `logging`, `RotatingFileHandler` 10 MiB keep 3, `VERBOSE = 5`, WARNING renamed WARN, `configure(src)`, `log_verbose/debug/info/warn/error(msg, **kv)`, line form `time=<RFC3339 ms> level=<L> msg=<m> src=<s> k=v`, byte-compatible with the dash's slog text handler; reader half `line_level`/`shows`) appends to `$SAVANNAH_LOG_DIR/savannah.log` (default `~/.claude/savannah/log/`) as `src=work`: INFO every verb and what it did, WARN every refusal (exit 1) and rollback, DEBUG every it2 and git call (args, rc, ms), the hook join, verb done; VERBOSE output heads and typed text heads; the `log` verb's own lines at VERBOSE; a logger that cannot open its file is a NullHandler, never a traceback. Records carry `priority` ("now"/"active"/"later"; anything else reads active) and `rank` (new/adopt write active + rank = created; nothing writes now but `move` and the board's Ctrl-P): `move NAME [--now|--active|--later] [--rank N]` is the board's section and order. `rename OLD NEW` claims NEW, moves record.json/notes.md/status.json and removes OLD; the worktree and branch keep their names. `status NAME [TEXT...] [--clear]` (or `--here`: the record whose claude_pane is `$ITERM_SESSION_ID`, else the one whose worktree is the cwd) is the tab's own progress report, `status.json` `{text, updated, count}` (count +1 per report); `ls` shows a STATUS column and `ls --json` carries `priority`, `rank`, `status`. `autoname [SESSION_ID...] [--dry-run] [--no-claude] [--json]` = `adopt` for every hook session no record claims, the title from one `claude -p --safe-mode --no-session-persistence --strict-mcp-config --tools "" --max-turns 1 --output-format json --model haiku` (`$SAVANNAH_NAME_MODEL`, `$SAVANNAH_CLAUDE_BIN`) per tab over an excerpt of its transcript (first three prompts, latest prompt, latest reply head; run from the temp dir with `SAVANNAH_HOOK_IGNORE=1`), four at a time, the name from the tab's checkout when it is a `<repo>-<ID>` checkout beside its repository (`checkout_name`: a git worktree, or a clone named that way; `zfs-dlpx-98816`, `-2` for a second tab there) else derived from the title like the board's nameFor (`slug_name`, `-2` when taken), the first prompt's first line when the call fails (`how: prompt`, `claude_error`), a tab with no prompt skipped (`no prompt yet`); exit 0, JSON `{dry_run, named, skipped, failed}`. `ask [NAME...] [--all] [-q TEXT] [--timeout S] [--no-wait] [--json]` = one question (default: what are you working on, where are you, what is next, and the URL of any pull request for it) typed into every tab whose hook state is idle or done (never working or waiting, never the asking session, `ask_skip_reason`), then a wait for each asked tab's hook file to show the question as `last_prompt` and state `done`, the reply read from its transcript (the turn whose prompt is the question), each work session's reply appended to its notes (the question after it) and kept in its record as `summary`/`summary_at` (`ls --json` carries them); `how` answered/timeout/sent; exit 0, JSON `{question, timeout, waited, asked, skipped, failed}`. `adopt <name> --session ID` = a Claude Code tab you opened yourself (its hook session id) becomes a work session with no new tab: a record with `adopted: true` pointing at its directory and Claude pane (refused only when a record claims the pane AND joins the session, `join_session`, the board's rule; a record claiming the pane without joining it, the pane now running a Claude in another directory, is a stale claim: its `claude_pane`/`tab_id` are cleared, WARN `pane released`, and the adopt goes through); `rm` of it closes the pane and archives the notes but never runs git on that directory. `output <name> [--last N]` = the last N turns (human prompt + assistant text) parsed from the tab's Claude Code transcript (hook `transcript_path`, else newest `.jsonl` under `$CLAUDE_CONFIG_DIR/projects/<encoded worktree>/`): the reply channel for `say`. `open <name> [--shell]` = focus the tab when its Claude pane is still there, else (`reclaim_pane`, shared with `say`, `jump` and `restore`) re-point the record at the pane of a live hook session in its worktree that no other record claims (claude started again by hand; `tab_id` and a dead `shell_pane` cleared, INFO `pane reclaimed`) and focus that, else make it again in the same worktree (leftover shell pane closed first, `claude --continue` when a transcript exists): the way back after an iTerm2 restart. `repos [DIR]` = the git repos directly under DIR, the list the board's new-session dialog offers; DIR is remembered in `$SAVANNAH_HUB_FILE`. `new` copies what `<repo>/.worktreeinclude` names (paths/globs relative to the root, `#` comments, not gitignore syntax) into a fresh worktree, for the gitignored files it needs. `ls --json` adds `pane_alive` (one `it2 session list` per ls, null without it2). `hub [DIR]` = OPTIONAL, not the front door: the owner's folder of repos becomes a coordinating Claude session over the `work_*` tools (merges the peer shim into `DIR/.mcp.json`, creates `DIR/CLAUDE.md` from `work/hub_CLAUDE.md` and `DIR/.savannah-scratch/CLAUDE.md` from `work/scratch_CLAUDE.md` only if missing, merges `enableAllProjectMcpServers` + allow rules for the `work_*` tools and `Bash(sleep *)` into `DIR/.claude/settings.json`, remembers DIR, execs `claude` there; `$SAVANNAH_CLAUDE_BIN`, `--no-launch`). Storage `$SAVANNAH_WORK_DIR` (default `~/.claude/savannah/work/<name>/{record.json,notes.md}`, notes to `archive/` on rm). Names `^[a-z0-9][a-z0-9._-]{0,63}$`, never `t-<digits>`. Join to a live Claude Code session happens at READ time: `realpath(hook.project) == realpath(record.worktree)`, prefer the hook whose iTerm id == `claude_pane`, else newest. `Terminal` seam (create_split_tab/write/focus/close_tab/live_ids); `ITerm` is the only implementation, over iTerm2's own `it2` CLI (`$SAVANNAH_IT2`, default `it2` on PATH else the copy inside iTerm.app): `session list --json`, `tab new --window W` (or `window new`), `session set-name/run/split/focus/close --force`, `session send` (say/sh: the text, then a carriage return, since the newline `run` appends is a line break to Claude Code's input box, not Enter), `app activate`; never `tab close` (it closes nothing); text goes after `--` as arguments, no quoting layer of ours; a failure after the tab exists closes it. tmux reserved. Nothing in savannahd or song. `work/README.md` lists what does not work. Tested by `test/test_work.py` and `test/test_restore.py` (both set `SAVANNAH_LOG_DIR` to a temp dir). |
| `dash/` | `savannah-dash`: interactive Go/Bubble Tea board, the front door (bare `savannah` execs it). Work mode by default: LOCAL SESSIONS (open Claude Code tabs from the hook, joined to work records under `$SAVANNAH_WORK_DIR` by the work/ rule on the same 1 s tick; a work session with no live session shows as an `offline` row). Rows sit in three sections, NOW, ACTIVE and LATER, by the records' `priority`/`rank` (`dash/order.go`: `secNow/secActive/secLater`, `priorityName`/`priorityOf`, named rows never reorder by state, unnamed tabs close LATER in attention order with their it2 tab title in parentheses, from `it2 session list --json` every 5 s; `dash/now.go`: Ctrl-P `pressNow` toggles the highlighted named session into the bottom of NOW or back onto the top of ACTIVE through `work move --now|--active`, works grouped or filtered since `sectionNames` reads the rank order from every session, not the rows; nothing else writes `now`; an empty NOW is a header and a hint, the header counts "N now", `nowStyle` cyan), and closed sessions (offline work rows) in HISTORY below them (`dash/history.go`: `secHistory`, newest first, closed by default so its rows are not on the board, one header line with the count; Ctrl-E opens and closes it, `"history": "open"` remembered in the board file; the header counts "N closed" apart; a drag on a closed session is refused, `sectionNames` leaves it out of the move lists); Shift-Up/Down (or Ctrl-Up/Down) drags one step across NOW/ACTIVE/LATER (`planDrag` over the three lists), renumbers the sections 10/20/30 (`renumberAll`, `applyMoves`) and runs `work move` only for changed records (optimistic redraw, the 1 s re-read is the truth); a drag or Ctrl-P on an unnamed tab first names it from its tab title (`adoptForMove`: `nameFor` over the it2 title, else `suggestName` from the directory, the record and the session tagged locally, `nameTried` set; `tea.Sequence` of the `work adopt` and the moves), a drag landing it at the bottom of LATER's named rows, Ctrl-P in NOW; On start and for each new plain tab with a prompt, `dash/autoname.go` runs `work autoname --json <hook ids>` once at a time (a headless Claude titles the tab's transcript excerpt, the tab is adopted with the title; `--no-autoname`, or `"autoname": "off"` in the board file); the status line reports the names. The mouse (`dash/mouse.go`, `tea.WithMouseCellMotion` unless `--no-mouse` or `"mouse": "off"` in the board file): `topLines` builds the board's top half with a `lineHit` per line (row index or section header), `hitAt(y)` reads a click against the same lines, a press selects the row, a second press on it within 400 ms is `activateRow` (what an empty Enter does: `work open`, jump, worker view), the wheel moves the cursor, a press on the HISTORY header is Ctrl-E; other screens ignore the mouse; text selection is Option-drag with reporting on. Ctrl-K finds (`dash/find.go`: the prompt line is the filter, every word must appear, case aside, in `sessionHaystack`: name, title, ticket, `~` path, last prompt, state, and with a record its branch, summary, last note line and title, a plain tab's it2 title; a mesh worker's node, id, title, worktree, state; `rebuildRows` keeps the matching rows only and looks into closed sessions whether HISTORY is open or not; Enter keeps the filter and the header says `matching "x"`, Esc clears it before anything else, Ctrl-K edits it; a drag is refused while filtered; not remembered). Ctrl-A twice asks every idle or done tab what it is doing (`dash/ask.go`: `work ask --all --json` in the background, the rows show who has answered, the replies open as a screen, modeAsk, Esc back, `a` asks again; the panel shows a record's `summary`). Ctrl-G groups each section's rows by repository (`dash/group.go`: `<repo>/.savannah-worktrees/<name>` is that repo, else the nearest `.git` walking up is the row's checkout, a `.git` file followed to its main repo, a checkout named `<X>-<ID>` beside a repo `<X>` grouped under it (`siblingMain`, longest sibling), cached per path for the run; dim `  zfs (4)` header per group, inside it the main checkout first then the other checkouts by name (`checkoutLabel`), PROJECT as the checkout name plus the path inside it (`.` for the main checkout), drag refused while grouped; remembered in `$SAVANNAH_BOARD_FILE` = `~/.claude/savannah/board.json` (`dash/prefs.go`: `boardPrefs`, `remember`; the width and cut helpers live in `dash/text.go`; `dash/main.go` keeps the model, `Update` and `main`, with one `screens` table per mode for keys, view, resize and mouse; `dash/boardkeys.go` holds the board keys as one-line cases over `press*`/`submit*` handlers and the prompt line as one `promptState` with a kind; `dash/boardview.go` draws the board, `dash/mesh.go` is the mesh half, `dash/opts.go` the flags), `--group`/`--no-group` win for one run); Ctrl-R names a plain tab (`work adopt`, no note; words typed where a name is asked for, there, on Ctrl-T and in the Ctrl-N dialog, become the record's `title` and a derived name, `dash/board.go` nameFor) or edits a work session's title (`work title NAME -- TEXT`, empty = `--clear`; the slug rename stays `work rename` on the CLI). The WORK column shows the title when set (28 columns, cut with `~`), the panel leads with it and keeps the name beside it. A row whose Claude reported progress with `work status` shows state `polling` with the text, poll count and age, the panel carries the line and the header counts them; live = the session is working and status.json is newer than the hook's `updated` (`dash/polling.go`). Rows are coloured by state (`rowStyle`, `dash/sessions.go`): red needs you, yellow working, green idle or done, faint offline; a file an older hook stamped `waiting` for the idle nudge is read as `idle` (`normalizeSession`). Ctrl-F sets the fade (`dash/fade.go`): a row whose hook `updated` stamp (an offline row's record stamp) is older than the fade, 3 h by default, is drawn faint in its state colour where it is (`rowStyleAt`; never a waiting or working row); Ctrl-F cycles 1h, 3h, 8h, 24h, off and writes `"fade"` to the board file, `--fade DUR|off` wins for one run (`parseFade`: a Go duration, a bare number of hours, or off). A TICKET column after WORK whenever a row has one (`dash/ticket.go`: two slots, the ticket and the pull request, `ticketFor` filling each from the first source naming one of the checkout's directory name via `checkoutLabel`, the record's name and branch, the title, the last prompt, the summary; a GitHub PR URL in the summary or notes first, `prURLIn`; `ticketsIn` matches `KEY-NUMBER`, a 2 to 5 character key starting with a letter and 2 to 7 digits, any case in a slug and upper case only in free text, plus `PR #n`/`PR n`/`pr-n` as `PR-n`; `session.Ticket/TicketID/TicketURL/PR/PRURL` filled by `fillTickets` in joinWork; the panel head carries them). Each id is an OSC 8 hyperlink (`osc8`, `linkify` after padding): the ticket through the board file's `ticket_url` pattern (`{ticket}`; `defaultTicketURL` = the author's Jira when the file says nothing, `resolveTicketURL`; set and remembered by `--ticket-url URL`, `off` = none, `default` = back to the built-in; `ticketURLPattern`), the PR through its URL else the checkout's GitHub origin (`repoWeb`, `git remote get-url origin` once per checkout, `githubWeb`) plus `/pull/N`; Ctrl-B runs the system opener on the row's links (`pressOpen`, `openerArgv`), the status line saying what opened or why nothing could. The table plans its columns from the width (from 120 columns: TAB and NOTE columns, PROJECT as the full `~/` path) and the rest of the height is a detail panel for the highlighted row: its notes beside the end of its last reply (`work output --last 1`, fetched on selection and on each hook update of that row, never on a timer); `dash/board.go`. Keys on a work row: Enter = `work open` (empty input) or `work say` (typed text), Ctrl-O = output view (`work output --last 5`, refetched every 2 s), Ctrl-T = note (`work note`; on a plain tab it first asks for a name and runs `work adopt <name> --session=<hook id>`, then takes the note), Ctrl-X twice = `work rm` (refusals shown verbatim), Tab = raw it2 jump; Ctrl-N = new-session dialog (Name/Repo/Prompt/Worktree; repos from `work repos --json`, a `.savannah-scratch` entry when the folder has one) running `work new ... --json` (`--title=<words>` when the Name field held words). Ctrl-S = snapshot screen (`dash/snapshot.go`: `work snapshots --json` on entry, `s` = `work snapshot --json`, `d` = `work restore --dry-run --json`, `r` = `work restore --json`, results as lines; a one-time status hint after a start when named rows are offline and a snapshot is under 48 h old). Ctrl-L = log view (`dash/logview.go`: the tail of `$SAVANNAH_LOG_DIR/savannah.log` read by the dash itself, filtered by the `level=` stamp, `n`/`d`/`v` or Left/Right = normal (INFO+), debug (DEBUG+), verbose (all), refreshed every 2 s, following the end). Logging (`dash/logging.go`): `openLog()` in `main` installs slog's default as a text handler at VERBOSE (`-8`) over `logWriter` (append, 10 MiB rotation keep 3, reopen when the file was swapped), `src=dash` on every record; every subprocess goes through `run` (DEBUG argv/ms/bytes, WARN with stderr on failure, VERBOSE output head) or `poll` (the periodic reads: titles, mesh task lists, the output view; VERBOSE on success); INFO `msg=action do=<verb>` for each key and INFO/WARN for each result; DEBUG `sessions changed` only when the row set or states change. Every action is a `savannah work ...` subprocess (`$SAVANNAH_BIN`, else `savannah` on PATH); jump is `it2 session focus <id>` then `it2 app activate` (`$SAVANNAH_IT2`, else `it2`, else the bundled copy). `--mesh` (what `savannah mesh` passes) adds MESH WORKERS: savannah tasks on every node `savannah ls` finds (`--addr` pins one), a thin client over `task ls --json` and `task send`; Enter/Tab on a worker row opens a full-screen live view riding `task tail` (Left/Right switch, Esc back); the decision inbox for `DECISION_NEEDED` rows (`docs/handoff.md`). Header shows one "N need you". Separate Go module (`cd dash && go build -o savannah-dash .`, `go test ./...`, or `cmake --build build --target dash`), not in CMake's default target. |
| `docs/handoff.md` | Session handoff (human-in-the-loop over the mesh): an unattended worker ends its turn with a final `DECISION_NEEDED {json}` line; pending decision = idle/incomplete task with the marker as its last output line; answer = ordinary `task_send`. Convention + dash inbox + contract test, zero server changes. |
| `tools/fake_savannah.py` | Fake savannah CLI for shim tests (canned ls/ask/status). |
| `tools/fake_it2.py` | Fake `it2` for work-session tests: logs every call as one JSON argv line to `$FAKE_IT2_LOG`, keeps its world (counter, windows, panes) in `$FAKE_IT2_LOG.state`; tab n = `TAB-n` / `PANE-C-n` / `PANE-S-n`; ids in `$FAKE_IT2_MISSING` (comma list) are absent (`not found`, rc 3; set-name rc 1); `$FAKE_IT2_FAIL="session split"` fails the call with that argv prefix (rc 1); `FAKE_IT2_NO_WINDOW=1` = no window (the `window new` path); `tab close` prints success and closes nothing, like the real one. No real iTerm2 is ever driven in tests. |
| `test/` | Unit tests per component + end-to-end echo integration. Shared scaffolding, one file per language: `test/test_support.hpp` (`savannah::test`: `CHECK`/`CHECK_THROWS_AS`/`finish`, a move-only `Daemon` spawner, `connect_tcp`, the streaming `ask`), `test/_support.py` (`check`/`finish`, `load_module`, the `Harness` over a temp work dir and the fake it2; each Python suite is a list of `test_<section>(h)` functions run in order over shared state), `dash/support_test.go` (`testBoard`, `send`, `press`, `click`). Python suites in ctest: `test_shim` (MCP frames against `fake_savannah.py` plus the real work module with a temp repo and the fake it2), `test_hook`, `test_install` (`install.sh --dry-run` against temp song dirs: the steps and their order, nothing built or written), `test_work` (temp work dir, temp sessions dir, throwaway git repo, fake it2, and the built CLI with a fake dash when CMake hands it the binary; skips the worktree cases with a message, still exit 0, when git is missing), `test_restore` (snapshot/restore against the fake it2 with `--no-worktree` records, so no git needed). |

fake-claude scenarios (`--scenario NAME`, plus `--chunks N`, `--delay-ms D`,
`--giant-bytes N`): `echo`, `chunks`, `tool_storm`, `giant_chunk`,
`die_before_result`, `hang`, `bad_json`, `utf8_split`, `stderr_noise`.
CMake writes
per-scenario savannahd configs (`cfg_<name>.toml`) into the build dir at
configure time; `test_integration_echo` runs against them, which is why it
needs `WORKING_DIRECTORY` = build dir.

Pipe mode (`runtime.run()`) dispatches one message at a time, so single-flight
NodeBusy and cancel-while-busy can only fire over concurrent connections:
`savannahd --tcp PORT` serves TCP via `run_tcp_multi` (thread per client).
Port 0 = OS picks; prints `SAVANNAHD_TCP_PORT=N` on stdout. With
`mesh.hmac_key_file` every client is HMAC-wrapped via
`runtime.set_transport_wrapper` (finding 11, fixed). `--mdns` additionally
advertises as `_agent-song._tcp` and binds all interfaces, so it hard-requires
the key. `test_tcp_flight`, `test_secure`, and `test_task_flight` run in CI
(`test_task_flight` self-skips, exit 77, if git is unavailable, since it
exercises worktree isolation); `test_mdns` is opt-in via `SAVANNAH_MDNS=1`
(advertises on the real network).

## Key patterns (learned from song itself)

- **Streaming is generated from the IDL** (hand-wiring deleted, commit
  08e3d95). songc honors `stream` methods (song main 316ccf4, finding 6,
  wishlist #1 done) and emits a client `call_streaming` proxy, a
  `StreamWriter&` service interface, and `dispatch_AgentNode_stream` on the
  generated `kService_AgentNode_Stream`. savannahd registers the generated
  dispatchers; the CLI uses the incremental `call_streaming` overload
  (per-chunk handler, finding 15). `src/wire_ids.hpp` now holds only the mDNS
  service type name.
- **Properties are class-level in songc**, not service-level. `status()` is a
  polled method in v1. v2: promote the agent to a song class with a `status`
  property to get push via subscription fan-out. **song wishlist #2.**
- **Streaming methods need their own service id.** runtime.cpp checks stream
  dispatchers first and they shadow the unary dispatcher for that id, so
  streaming methods (`ask`, `task_output`) live on the generated
  `kService_AgentNode_Stream`, exactly like backup.song's split. songc now
  emits the split itself (wishlist #3 resolved by finding 6's codegen).
- Register every method with `runtime.register_method(...)` for capability
  exchange; streaming methods get `wire::MethodFlags::streaming` and
  `runtime.set_capability(wire::Capability::streaming)`.

## song findings from Phase 1 (upstream these)

Found while building against song @ HEAD on Linux GCC 13:

1. `include/song/logging.hpp` needs `#include <cstdint>` (uint32_t; breaks
   GCC 13 Linux build). Fixed upstream 7/28/2026 (song main 53fc6f1);
   `patches/` retired.
2. `logging.hpp` is not in the `song.hpp` umbrella header; consumers must
   include it separately. Fixed upstream 7/28/2026 (b51a93f).
3. song is not add_subdirectory-friendly (test/ assumes top-level context).
   savannah links the prebuilt `libsong.a` instead (still fine). Fixed
   upstream 7/28/2026 (4e989ed): subdirs gate on top-level context.
4. Multi-line `///` doc comments in IDL lose the `///` on continuation lines
   in generated code (syntax errors). Fixed upstream 8/12/2026 (song main
   686af7d): codegen's `emit_doc` prefixes every line. Multi-line docs OK now.
5. Enums referenced in structs generate calls to `encode_<Enum>`/`decode_<Enum>`
   that are never emitted. Fixed upstream 8/12/2026 (song main 686af7d):
   codegen emits scalar + array enum codecs (enum travels as its underlying
   integer). `AgentChunk.kind` can drop its `i32`-on-the-wire workaround and use
   the real enum whenever we choose to; the workaround is no longer forced.
6. `is_stream` was parsed but ignored by codegen (proxies came out unary), so
   streaming was hand-wired via `call_streaming` + generated method ids. Fixed
   upstream 8/12/2026 (song main 316ccf4): codegen emits streaming proxies -- a
   client `call_streaming` proxy on `kService_<Name>_Stream`, a `StreamWriter&`
   service interface, and a `dispatch_<Name>_stream`. savannah adopted the
   generated form and deleted its hand-wiring (commit 08e3d95). Closes song
   wishlist #1.
7. Streaming dispatchers shadow unary dispatchers per service id (see above).
8. `encode_string` throws over `kMaxStringSize` (1 MB, buffer.hpp). savannahd
   splits big TEXT payloads into 512 KiB chunks (`kMaxChunkPayload`).
9. When a stream dispatcher throws, the StreamWriter destructor sends
   `stream_end` during unwind, then the catch sends the error reply. Streaming
   clients stop at `stream_end` and never read the error: the failure looks
   like a clean empty stream. Fixed upstream 7/28/2026 (cfb6af6):
   `StreamWriter::abort()` + error-as-terminator in both dispatch paths.
   Clients now see a thrown ServiceError carrying the dispatcher's message.
10. `make_service_type`'s old `_type._song._tcp` form was three DNS labels;
    dns_sd rejects it (kDNSServiceErr_BadParam, RFC 6763 wants `_app._proto`),
    so mDNS registration had never actually worked. Fixed upstream 7/28/2026
    (song main 4dcc542): the format is now `_type-song._tcp`; savannah's mesh
    type is `_agent-song._tcp`. Keep type names to 10 chars (RFC 6335).
11. `run_tcp_multi` has no security hook, so a service cannot serve
    SecureTransport clients through it. Fixed upstream 7/28/2026 (6714339):
    `runtime.set_transport_wrapper(...)` wraps every accepted client;
    savannahd uses it and its hand-rolled accept loop is gone.
12. `runtime_test.cpp` used `std::find` without `<algorithm>`; GCC 15
    libstdc++ dropped the transitive include. Fixed upstream 7/29/2026
    (5746063).
13. The Avahi discovery implementation did its includes inside
    `namespace song` (song::std) and had never compiled anywhere: no build
    host had Avahi headers until dbj-devone. Fixed upstream 7/29/2026
    (6e8ba10). savannah's CMake now mirrors song's optional avahi-client
    link dependency.
14. `call_streaming` hardcoded a 5000 ms per-chunk receive timeout, killing
    any ask whose first token took over five seconds. Invisible with
    fake-claude (instant); fatal with the real CLI on every nontrivial
    prompt. Found by the dbj-devone Claude session using the mesh. Fixed
    upstream 7/30/2026 (6bc974f): chunk_timeout_ms is a parameter. Size it
    to the agent (ask timeout + margin), never the wire.
15. `call_streaming` buffered every chunk until stream_end, so consumers
    saw nothing until completion: streaming without the streaming. Fixed
    upstream in the same commit: the incremental overload takes a per-chunk
    handler; the eager StreamReader form is now a collector built on it.
    The CLI and integration tests use the incremental form.

Found while exploring song's object/class system during Phase 5 planning
(the substrate the Task Mesh's live-status channel will use):

16. Object method dispatch mis-dispatched: `call_object()` stamped the frame
    `MsgType::call`, so the server decoded an object-method frame as a plain
    service-call header and `Object::dispatch()` was never reached. Fixed
    upstream 8/12/2026 (song main f9b7a27): new `MsgType::object_call` (0x11)
    + decode-and-dispatch in both server paths, with an e2e round-trip test.
17. Property push works over TCP only; the pipe path (`run()`) dropped
    `prop_subscribe` silently (fan-out needs a Transport target; a raw stdout
    fd has none). Made loud upstream 8/12/2026 (song main c5497a3):
    `handle_message_fd` logs a warning naming the cause and pointing at
    `--tcp`. Push over pipes would need an fd-subscriber model; savannah uses
    TCP for push by design.
18. Calls and subscriptions on one `ServiceConnection` were mutually
    destructive: an unsolicited `prop_notify` arriving mid-call tripped the
    sequence-id check and killed the call. Fixed upstream 8/12/2026 (song
    main c5497a3): a shared `recv_reply` demuxes interleaved notifications to
    their callbacks and keeps waiting for the real reply. A conductor no
    longer needs a dedicated notification connection.
19. song objects are creator-scoped, not durably shared. The registry is
    shared across TCP clients (a known id is callable from any connection),
    but an object is refcounted to its creating connection and deleted when
    that connection drops even if another client holds the id, so there is no
    reliable handoff or reconnect-and-reacquire; object ids also carry no
    ownership check. Left as a STATED SHORTCOMING, not fixed (decision A,
    2026-08-12, revisit later): the Task Mesh keeps authoritative state in
    savannahd's own long-lived table and treats song objects as ephemeral
    live-status views, so it routes around this by design (see the Phase 5
    plan's substrate pivot). The real fix, when we return to it, is
    refcount-scoped lifetime + per-object ownership.
20. Codegen never emitted `register_factory`, and generated `prop_set` never
    called `notify_property` -- both had to be hand-written. Fixed upstream
    8/12/2026 (song main 7e29245): generated setters now notify, and a
    `register_<Name><Impl>(ServiceRuntime&)` template decodes constructor
    args and builds the concrete Impl.

## stream-json contract (pinned subset)

`fake-claude` and `src/stream_json.cpp` both implement exactly this subset; if
the real CLI drifts, `SAVANNAH_LIVE=1 ctest -R test_live_schema` catches it
(fails live while fake-claude suites stay green):

- `{"type":"system","subtype":"init",...}` → ignored
- `{"type":"assistant","message":{"content":[{"type":"text","text":...} |
  {"type":"tool_use","name":...,"input":{...}}]}}` → TEXT chunk per text block,
  TOOL_EVENT chunk per tool_use ("name: first-string-arg, truncated")
- A text block over 512 KiB arrives as multiple TEXT chunks (song's 1 MB
  string cap; finding 8). Clients concatenate TEXT payloads; split points can
  land mid-UTF8 and concatenation restores the byte sequence.
- `{"type":"user",...}` (tool results) → ignored in v1
- `{"type":"result","subtype":...,"total_cost_usd":...,"duration_ms":...,
  "num_turns":...,"is_error":...}` → RESULT chunk, compact JSON trailer. The
  trailer carries `subtype` when present: `"success"` is a clean finish,
  `"error_max_turns"` means the agent hit its per-invocation `--max-turns` cap
  (continuable, not a real failure), other subtypes are genuine errors. The
  task supervisor auto-continues a turn on `error_max_turns` (see below); a
  bare `ask` just forwards the trailer.
- Unknown `type` → ignored (forward compatible)

Every `ask` response MUST end with exactly one RESULT chunk. A stream that ends
without one means the agent died: savannahd synthesizes
`RESULT {"is_error":true,"reason":"died before result"}`.

## Known v1 limitations (stated, on purpose)

- `NodeBusy` is delivered as an error RESULT chunk, not a wire-level thrown
  error (stream-dispatcher error semantics in song need investigation first).
- `allowed_tools`/`workspace` enforcement is delegation to the claude CLI's own
  flags plus process supervision. savannahd is not a sandbox.
- No cross-node file transfer (prompt + text back only). datacopy pattern later.
- Cancel kills the child process; no cooperative cancellation.

## Phases (tests define done)

1. **Solo node**: CLI → savannahd → fake-claude over local pipes. ✅
2. **Streaming hardening**: pathological fake scenarios (giant chunks, mid-UTF8 splits, hang, die-early) all handled. ✅
3. **Two machines**: mDNS + HMAC, `savannah ls` shows both. Runs on real LAN only (not in CI). ✅
4. **MCP shim**: `ask_peer` from stock Claude Code. The payoff demo. ✅
5. **Patterns**: 5a Task Mesh (persistent workers: task engine + `task_*` CLI/shim tools + dash) landed; 5b session handoff (`docs/handoff.md`, `DECISION_NEEDED` convention + dash decision inbox) landed; 5c local-session attention (`hooks/`, Claude Code hook + dash LOCAL SESSIONS section: your open tabs on the board, unified "need you" counter, jump-to-tab) landed; 5d local work sessions (`work/`: iTerm2 tab + split per work item, interactive Claude in a worktree, plain-text notes, `work_*` shim tools, dash WORK column with offline rows; iTerm2 only, tmux seam reserved) landed; 5e hub (`savannah hub DIR`: the folder of repos as a coordinating Claude session over the `work_*` tools, `work output`/`work_output` as the reply channel) landed, then demoted to an optional layer; 5f direction reset (11/3/2026: bare `savannah` = the board, the dash in work mode with a new-session dialog; `savannah mesh` = the mesh board; the Terminal seam on iTerm2's `it2` CLI, AppleScript gone; `work open` restores a tab after an iTerm2 restart, `work repos` lists the folder of repos, `.worktreeinclude` seeds a fresh worktree) landed; 5g the board as the task manager (11/3-4/2026: ACTIVE/LATER sections ordered by the records with Shift-Up/Down drag via `work move`, Ctrl-R naming via `work adopt`/`work rename`, self-reported polling via `work status` + the SessionStart hook context) landed; next: reviewer pair, tag fan-out, maybe datacopy file transfer, a tmux Terminal for work/. Live-status push via song classes/properties is the planned upgrade over today's polling.
