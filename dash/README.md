# savannah-dash

An interactive terminal dashboard for the task mesh, built with
[Bubble Tea](https://github.com/charmbracelet/bubbletea).

It is a thin client over the `savannah` CLI's JSON seam, not a second
implementation of song's wire protocol: it polls `savannah task ls <node>
--json` once a second to render a live table, and on Enter runs `savannah task
send <node> <id> <text>` for the highlighted task. This mirrors how the Python
MCP shim shells out to the CLI. A Go TUI, a React app, or a shell script could
all sit on the same seam.

## Build and run

```bash
cd dash
go build -o savannah-dash .

# The savannah CLI must be reachable (on PATH or via $SAVANNAH_BIN). Run from a
# directory where a relative --key path resolves (e.g. the build dir with
# mesh.key), or pass an absolute key path.
SAVANNAH_BIN=/path/to/savannah ./savannah-dash <node> --key mesh.key
```

## Keys

- **up / down** move the highlighted row
- **type** a prompt into the field at the bottom
- **Enter** sends it to the highlighted task (`task send`), clears the field
- **Ctrl-C / Esc** quit

## Scope

This is the poll-based MVP. It reuses the CLI's request/response JSON. A
push-based upgrade (a streaming `watch --json` event source on the node) can
replace the 1-second poll underneath the same rendering, and the same seam
extends to multi-node roll-up. See the savannah roadmap.
