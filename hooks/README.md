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
| `waiting` | Notification     | NEEDS YOU: a permission prompt or idle nudge   |
| `done`    | Stop             | finished responding, ready for your read       |

The file is deleted on SessionEnd. It also captures the project directory, the
permission mode, and the terminal identity (`ITERM_SESSION_ID` or
`TMUX_PANE`), so the dashboard can jump you to the right tab. Everything is
best-effort and always exits 0, so it can never block or slow a session.

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
