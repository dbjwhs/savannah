# Session hooks: your open tabs on the dashboard

The problem: once you have more than a handful of interactive Claude Code
sessions open in terminal tabs, you cannot tell at a glance which ones are
working, which finished, and which are blocked waiting for your permission or
input. This makes every tab visible on one board and flags the ones that need
you.

It is separate from the mesh. These are your own interactive Claude Code
sessions (the ones you type into), not the headless workers savannahd runs.
No daemon, no network, no tokens: a hook writes a small status file per
session, and `savannah-dash` reads them.

## What it captures

`session-status.py` runs on Claude Code lifecycle events and upserts
`~/.claude/savannah/sessions/<session_id>.json` (override the directory with
`$SAVANNAH_SESSIONS_DIR`). The state it records:

| State     | Fires on         | Meaning                                        |
|-----------|------------------|------------------------------------------------|
| `idle`    | SessionStart     | fresh session, nothing pending                 |
| `working` | UserPromptSubmit | you asked, Claude is on it                     |
| `waiting` | Notification     | NEEDS YOU: a permission prompt or a question   |
| `done`    | Stop             | finished responding, ready for your read       |

One Notification is not a `waiting`: the idle nudge (`notification_type`
`idle_prompt`, which Claude Code sends once a tab has sat unused for a
minute) refreshes the timestamp and leaves the state alone, recording
`notify_type` and no message. A tab that is merely idle does not need you,
so the board shows it green, not yellow. A file an older hook stamped
`waiting` for the nudge (state `waiting`, `notify_type` `idle_prompt`) is
read as `idle` by both the board and `savannah work` (so `work ask` asks
that tab), and the hook rewrites it as `idle` on the tab's next nudge. A
nudge that lands on a real permission prompt leaves that prompt's state,
type and message alone.

The file is deleted on SessionEnd. It also captures the project directory, the
permission mode, and the terminal identity (`ITERM_SESSION_ID` or
`TMUX_PANE`), so the dashboard can jump you to the right tab. Everything is
best-effort and always exits 0, so it can never block or slow a session.

Two fields exist so you can remember what a tab was doing:

| Field             | Set on           | What it holds                                   |
|-------------------|------------------|-------------------------------------------------|
| `transcript_path` | every event      | where Claude Code keeps this conversation's transcript; the last known value stays when an event omits it |
| `last_prompt`     | UserPromptSubmit | the first non-blank line of what you last typed, cut to 200 chars; kept through Notification, Stop and heartbeats until your next prompt |

Both are always present, as empty strings until known. The hook reads
`prompt` from the event (and accepts `user_prompt` as an alternate name).

`SAVANNAH_HOOK_IGNORE` set in the environment makes the hook do nothing at
all (no file written, no log line, exit 0). savannah's own headless
`claude -p` calls (`work autoname`) set it so they are never a row on the
board; `--safe-mode` already keeps hooks from running, this is the
guarantee that does not depend on that.

The hook does no joining of its own. Work sessions (`work/`, one iTerm tab
per work item) find their live Claude Code session at read time: a hook file
belongs to a work session when its `project` is the same real path as the
work session's `worktree`. `work ls` and the dash perform that match; nothing
writes into the other side's file.

## What a work session's Claude is told

On SessionStart only, the hook looks for the work session whose `worktree`
is the new session's cwd (it reads every `<work dir>/<name>/record.json`
itself, `$SAVANNAH_WORK_DIR` or `~/.claude/savannah/work`, skipping
`archive/` and anything unreadable; with several on one directory, the one
whose `claude_pane` is this tab's `ITERM_SESSION_ID` uuid wins, else the
newest). When one matches, the hook prints one JSON object on stdout,
`{"hookSpecificOutput": {"hookEventName": "SessionStart",
"additionalContext": TEXT}}`, which Claude Code adds to that session's
context. TEXT is a statement of fact, not an instruction: it names the work
session, gives the exact command that reports a poll to the board
(`<repo>/build/savannah work status <name> -- "waiting for ctest (run 2 of
3)"`, or `python3 <repo>/work/savannah_work.py ...` when the CLI is not
built), says that each run counts as one poll and that the board shows the
text, the count and the age, and names the `--clear` form. Nothing is
printed on any other event, nothing when no record matches, and the status
file is written either way. See `work/README.md` ("Polling for you") for
what the board does with the report.

The hook also writes savannah's one log, `$SAVANNAH_LOG_DIR/savannah.log`
(default `~/.claude/savannah/log/`; the format and the reader, `savannah
work log`, are in `work/README.md` under "Logging"), through
`work/savannah_log.py`, found beside this checkout's `hooks/` directory:
one DEBUG line per event (`msg=event`, with the event name, session id,
state, cwd, iTerm2 session id and the file written; `removed=true` on
SessionEnd), an INFO line (`msg="work context"`, the name and the command)
when a SessionStart hands a work session's Claude its context, a WARN line
when the session file could not be written, all stamped `src=hook`. Best
effort like the rest: a hook copied out of the checkout cannot import the
module and logs nothing, an unwritable log directory logs nothing, and in
neither case does anything reach stdout or stderr, since those are Claude
Code's. `savannah work log --level debug` is the reader.

## Install

```bash
python3 hooks/install.py            # merge into ~/.claude/settings.json (all projects)
python3 hooks/install.py --project  # or this repo's .claude/settings.json
python3 hooks/install.py --print    # preview the merged JSON, write nothing
python3 hooks/install.py --uninstall
```

It backs up settings.json first, preserves every other setting and hook you
have, and is idempotent. After installing, restart your Claude Code sessions
(or run `/hooks`) so they pick up the new hooks. To wire it by hand instead,
see `settings.snippet.json`.

## See it

```bash
cd dash && go build -o savannah-dash .
./savannah-dash --no-mesh    # sessions only, no savannahd needed
```

Sessions appear under LOCAL SESSIONS, the ones needing you float to the top and
count in the header, and Enter jumps to that terminal tab (iTerm2 or tmux).
In full mesh mode (`./savannah-dash --key mesh.key`) the same board also shows
the savannah workers below, and the header's "N need you" counts sessions and
pending worker decisions together.

## Limitations (stated)

- The terminal jump is iTerm2 and tmux only; other terminals show the row but
  cannot focus the tab.
- `Notification` fires roughly six seconds after a permission prompt and about
  sixty after Claude goes idle, so "waiting" can lag a beat behind the screen.
- A session that crashes without firing SessionEnd leaves a stale file; press
  Ctrl-X on its row to dismiss it (a live session rewrites its file anyway).
- Cloud and web Claude Code sessions do not run user-level hooks and will not
  appear.
- The dashboard polls the directory once a second; there is no push.
- `last_prompt` is the first line of your prompt only, capped at 200 chars;
  it is a reminder, not a record. The full conversation is in
  `transcript_path`, which the hook records but does not read or parse.
- `transcript_path` is whatever Claude Code sends; if a release stops sending
  it, the field stays empty (or stale from an earlier event) rather than
  failing.
- The SessionStart context reaches only a Claude started inside a work
  session's worktree with this hook installed; a tab adopted later (`work
  adopt`) never saw it, and whether that Claude runs `work status` is its
  own call. The command is spelled with this checkout's absolute path, so a
  moved checkout needs the hook reinstalled.
