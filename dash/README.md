# savannah-dash

An interactive terminal dashboard for the task mesh, built with
[Bubble Tea](https://github.com/charmbracelet/bubbletea).

It is a thin client over the `savannah` CLI, not a second implementation of
song's wire protocol. In mesh mode it browses the mesh with `savannah ls` on a
3-second tick to **discover every node dynamically** (systems appear and drop
as they join and leave the network), polls each node's `task ls <node> --json`
once a second, and renders all workers on one board with a NODE column. On
Enter it runs `savannah task send <node> <id> <text>` for the highlighted
worker. This mirrors how the Python MCP shim shells out to the CLI; a React app
could sit on the same JSON seam.

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

- **up / down** move the highlighted row (across all nodes)
- **type** a prompt into the field at the bottom
- **Enter** sends it to the highlighted worker (`task send`), clears the field
- **Ctrl-C / Esc** quit

## Scope

Poll-based over the CLI's request/response JSON. A push-based upgrade (a
streaming `watch --json` event source per node) can replace the polling
underneath the same rendering; the multi-node discovery already makes it a
whole-mesh view.
