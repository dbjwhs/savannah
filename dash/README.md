# savannah-dash

An interactive terminal dashboard for the task mesh, built with
[Bubble Tea](https://github.com/charmbracelet/bubbletea).

The board has two halves. **LOCAL SESSIONS** are your own open Claude Code
tabs (via the session hook, see `../hooks/`): every interactive session on
this machine, with the ones blocked on a permission prompt or your input
floated to the top and Enter to jump to that terminal tab. **MESH WORKERS**
are the headless savannah tasks below. One cursor moves over both, and the
header shows a single "N need you" count over sessions waiting plus pending
worker decisions. Run `savannah-dash --no-mesh` for the sessions half alone
(no savannahd, no network) as a standalone tab monitor.

It is a thin client over the `savannah` CLI, not a second implementation of
song's wire protocol. In mesh mode it browses the mesh with `savannah ls` on a
3-second tick to **discover every node dynamically** (systems appear and drop
as they join and leave the network), polls each node's `task ls <node> --json`
once a second, and renders all workers on one board with a NODE column. On
Enter it runs `savannah task send <node> <id> <text>` for the highlighted
worker. This mirrors how the Python MCP shim shells out to the CLI; a React app
could sit on the same JSON seam.

Enter with an empty prompt (or Tab) opens a **tmux-like full-screen view** of
the highlighted worker: the transcript replayed with turn boundaries, the live
turn streaming as it happens, and the prompt line still active at the bottom.
Left/Right flips between workers like tmux windows; Esc returns to the board.
The view rides the CLI's `task tail` seam in a respawn loop (tail replays,
follows the live turn, exits; every rerun is a fresh full replay), and the
screen only swaps to a new run's buffer once it has caught up with the last
completed one, so redraws do not flicker.

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

# Mesh mode: discover and show every node on the mesh. Run from a directory
# where a relative --key path resolves (e.g. the build dir with mesh.key).
SAVANNAH_BIN=/path/to/savannah ./savannah-dash --key mesh.key

# Pin a single node instead (use --addr for a node not advertised on mDNS):
./savannah-dash dbj-mac --addr 127.0.0.1:8790 --key mesh.key
```

The savannah CLI must be reachable (on PATH or via `$SAVANNAH_BIN`).

## Keys

Board:

- **up / down** move the highlighted row (across both sections)
- On a **SESSION** row: **Enter / Tab** jumps to that terminal tab;
  **Ctrl-X** dismisses a stale entry
- On a **WORKER** row: **type** a prompt then **Enter** sends it (`task
  send`); **empty Enter / Tab** opens the full-screen view; **Ctrl-X**
  removes it if finished (`task rm`; a running worker is refused, cancel first)
- **Ctrl-C / Esc** quit

Full-screen view:

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
