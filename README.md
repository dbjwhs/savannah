# savannah

Claude-to-Claude agent mesh for your LAN. Every machine runs `savannahd`, a
small C++20 daemon that exposes that machine's headless Claude as a typed,
streamed, process-supervised service on the [song](https://github.com/dbjwhs/song)
substrate. An MCP shim (planned, Phase 4) gives stock Claude Code an
`ask_peer` tool, so one agent can delegate to another mid-task on its own
judgment. No forked Claude Code, no broker, no cloud relay.

```
Claude Code (A)                          Claude Code (B)
     |  MCP: ask_peer("linux-box", ...)       ^
     v                                        | spawns claude -p per ask
  peer shim (song client)                savannahd (song service)
     |                                        ^
     +---- mDNS discovery / HMAC / stream ----+
                    (song wire)
```

`DESIGN.md` is the source of truth for architecture and rationale.
`CLAUDE.md` holds the working conventions and the pinned wire contracts.

## What works today

- **Solo node** (Phase 1): `savannah ask local "..."` spawns `savannahd`
  over local pipes, which forks the agent, maps its stream-json output to
  typed chunks (TEXT, TOOL_EVENT, RESULT), and streams them back. Timeout,
  cancel, crash synthesis, and cost reporting per ask.
- **Streaming hardening** (Phase 2, core cases): 4 MiB text blocks survive
  intact (split into 512 KiB chunks around song's 1 MB string cap), UTF-8
  split mid-codepoint at both the pipe layer and the chunk layer reassembles
  byte-identically, garbage JSON lines are skipped, an agent that dies or
  hangs produces an honest error trailer.
- **Concurrent clients over loopback TCP**: `savannahd --tcp PORT` serves
  multiple clients (thread per client). Single-flight is enforced: a second
  ask while one is in flight gets an immediate `busy` error trailer;
  `cancel` kills the in-flight agent and frees the node.
- **`ask_peer` from stock Claude Code** (Phase 4): `shim/peer.py` is an
  MCP stdio server in pure stdlib Python giving any MCP client the mesh
  tools `list_peers`, `ask_peer(name, prompt)`, `peer_status(name)`, the
  `task_*` tools (Phase 5a) and the local `work_*` tools (Phase 5d).
  It shells out to the `savannah` CLI, so discovery, HMAC, and streaming
  ride the tested C++ paths. This repo's `.mcp.json` registers it
  automatically for Claude Code sessions started here (build first, and
  point `SAVANNAH_KEY` at your mesh key); elsewhere:
  `claude mcp add peer -e SAVANNAH_BIN=/path/build/savannah -e
  SAVANNAH_KEY=/path/mesh.key -- python3 /path/shim/peer.py`
- **A real two-machine mesh** (Phase 3, acceptance run on a live LAN:
  macOS arm64 + Ubuntu 26.04 x86_64): with a shared key,
  `savannahd --tcp PORT --mdns` authenticates every client via HMAC-SHA256
  and advertises the node as `_agent-song._tcp`. `savannah ls` shows every
  node on the network, and `savannah ask <name> --key FILE` resolves a
  node by name (or `--addr HOST:PORT` directly) and streams the answer
  back, in either direction. Wrong or missing keys are rejected before
  any dispatcher runs.
- **Deterministic tests, zero tokens**: everything above is covered by
  seven test suites driven by `fake-claude`, a scenario-driven stand-in
  that speaks the exact stream-json subset savannahd parses. `ctest` runs
  the whole mesh locally in a few seconds; the mDNS self-discovery suite
  is opt-in (`SAVANNAH_MDNS=1`) because it advertises on your real
  network.

## What does not work yet

Stated on purpose; this list shrinks phase by phase (see `DESIGN.md`).

- The shim wraps the CLI rather than speaking song natively: song's
  Python runtime is unary-only today (no streaming, no HMAC), so
  generated Python bindings are the v2 path.
- The real `claude` CLI is never exercised in CI. fake-claude pins the
  stream-json subset; run `SAVANNAH_LIVE=1 ctest --test-dir build -R
  test_live_schema` manually to check the live CLI against it (one tiny
  prompt, burns a few tokens; skipped otherwise).
- `ask` is stateless: every ask is a fresh `claude -p`. Persistent sessions
  exist in two forms only, and neither flows through `ask`: mesh tasks
  (Phase 5a, `savannah task ...` / `task_*` tools, headless workers that
  savannahd owns and resumes per turn) and local work sessions (Phase 5d,
  `work/`, your own interactive Claude tabs in iTerm2; see below). Nothing
  migrates a session between machines.
- Single-flight per node. Cancel is SIGKILL, not cooperative.
- No cross-node file transfer: prompt out, text back.
- savannahd delegates tool/workspace restrictions to the claude CLI's own
  flags and supervises the process. It is not a sandbox.

## Build

Requires CMake 3.16+, a C++20 compiler, and song built as a sibling
(or pass `-DSONG_ROOT=/path/to/song`).

```bash
git clone https://github.com/dbjwhs/song ../song
cmake -S ../song -B ../song/build && cmake --build ../song/build --target song songc
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j
ctest --test-dir build --output-on-failure
```

`songc` generates the wire code from `idl/agent.song` at build time.

## Try it (zero tokens)

The build dir contains ready-made configs that point at `fake-claude`:

```bash
cd build
./savannah ask local "hello savannah" --config cfg_echo.toml
./savannah info local --config cfg_echo.toml
```

TEXT chunks stream to stdout verbatim AS THEY ARRIVE (live progress, not
buffered-until-done); tool events and the result trailer (cost, duration,
turns, is_error) go to stderr. Exit code 0 iff the ask succeeded, so it
pipes cleanly.

## Point it at the real thing

Write a config and set `cmd` to the real CLI:

```toml
[node]
name = "linux-box"          # required
workspace = "/home/me/work" # required
tags = ["cpp", "linux"]     # optional

[agent]
cmd = "claude"              # default
allowed_tools = ["Bash", "Read", "Edit"]
max_turns = 25              # default
timeout_ms = 300000         # default, per ask

[mesh]
max_concurrent = 1          # v1: must be 1 (single-flight)
```

```bash
./savannah ask local "summarize the failing tests" --config node.toml
```

Each node uses its own Anthropic auth and burns its own tokens; the RESULT
trailer reports what the ask cost. Read cost_usd for what it is: a local
estimate at standard API list rates, computed by the claude CLI. Under a
subscription login (Pro/Max) nothing is billed per ask; usage draws down
the plan's shared allowance instead, and every node logged into the same
account drains one pool. Under an API key it approximates the real charge.

## Mesh mode (two machines)

What it looks like once two nodes are up (a Mac and an Ubuntu box on one
LAN, July 2026):

```
$ ./savannah ls
dbj-mac 127.0.0.1:59959
dbj-devone 10.0.0.208:36749

$ ./savannah ask dbj-devone "playground warmup check" --key mesh.key
playground warmup check
[result] {"cost_usd":0.00042,"duration_ms":1234,"is_error":false,"num_turns":1}
```

Asks work in both directions; a node with no key, or the wrong key, is
dropped before any dispatcher runs.

Generate one shared key, give it to every node, and start each daemon
advertised:

```bash
head -c 48 /dev/urandom | base64 > mesh.key   # once; scp to each machine
[mesh]
hmac_key_file = "mesh.key"                    # in each node's config
./savannahd --config node.toml --tcp 0 --mdns
```

Then from anywhere on the LAN:

```bash
./savannah ls                                  # browse the mesh
./savannah ask linux-box "run ctest and summarize" --key mesh.key
./savannah ask linux-box "..." --addr 192.168.1.20:7000 --key mesh.key
```

`--mdns` refuses to start without a key: advertised nodes always
authenticate. `--tcp` alone binds loopback; `--mdns` binds all interfaces.
Traffic is HMAC-authenticated, not encrypted (TLS-PSK is a later phase);
treat the mesh as LAN-trust.

macOS gotcha: the application firewall silently swallows inbound
connections to unapproved binaries. The TCP handshake completes in the
kernel, savannahd never sees the connection, and remote clients report a
read timeout while loopback works fine. Allow it once (and again after
rebuilds, since the binary is unsigned):

```bash
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add $PWD/build/savannahd
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp $PWD/build/savannahd
```

Linux needs `libavahi-client-dev` (and a running avahi-daemon) at build
time for mDNS; without it song builds with discovery disabled.

### Run a node as a service (Linux)

A mesh node should survive reboots and crashes without anyone logged in.
A user systemd unit does it; no root beyond one lingering grant:

```ini
# ~/.config/systemd/user/savannahd.service
[Unit]
Description=savannah agent node (AgentNode on the song mesh)

[Service]
WorkingDirectory=%h/ng/dbjwhs/savannah/build
Environment=SAVANNAHD_CONFIG=node.toml
ExecStart=%h/ng/dbjwhs/savannah/build/savannahd --tcp 0 --mdns
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now savannahd
loginctl enable-linger $USER      # start at boot, no login required

systemctl --user status savannahd     # inspect
journalctl --user -u savannahd -f     # logs (port marker, client drops)
```

The port is OS-assigned on every start; peers find the node through mDNS,
so nothing needs pinning. macOS nodes have no equivalent yet (a launchd
plist would be the analog); the daemon there is started by hand.

## Work sessions (local)

Not part of the mesh. `work/savannah_work.py` (stdlib Python, Phase 5d)
names the habit of one iTerm2 tab per work item: an upper pane running your
ordinary interactive `claude` in a git worktree, a lower shell pane cd'd
into the same worktree, and a plain-text notes file so you can remember what
that session was doing. No savannahd, no song, no tokens spent by the tool;
the brain in the upper pane is your own interactive Claude Code, not a
headless worker.

```bash
work() { python3 work/savannah_work.py "$@"; }   # a shell function; W="python3 ..." breaks under zsh
work new fix-parser --prompt "look at src/json.cpp"   # worktree + tab + record
work note fix-parser "flaky on nested arrays, suspect the depth check"
work say fix-parser "run ctest -R test_json and summarize"   # types into the Claude pane
work say fix-parser -- --continue      # text that starts with "-" goes after "--"
work ls          # NAME  BRANCH  STATE  LAST PROMPT  LAST NOTE
```

`new` creates `<repo>/.savannah-worktrees/<name>` on branch `work/<name>`
(or `--branch B` for an existing local branch, `--no-worktree` for the repo
itself), opens the split tab in the current iTerm2 window, and writes
`~/.claude/savannah/work/<name>/record.json`. `--repo` may point anywhere
inside the repository; it is resolved to the main worktree root. `notes`,
`sh` (type into the shell pane), `jump` (focus a pane) and `rm` (close the
tab, remove the worktree, archive the notes; the branch stays, it is your
work) complete the set. `new` of a name whose `work/<name>` branch survived
an earlier `rm` reattaches to that branch and says so. The same verbs are MCP
tools in the shim (`work_new`, `work_list`, `work_note`, `work_notes`,
`work_say`, `work_shell`, `work_jump`, `work_rm`), so a hub Claude Code
session can open a tab for you, leave a note, or type a line into another
session. The dash shows each work session in a WORK column on LOCAL
SESSIONS, joined to its live Claude Code session through the hook files
(`hooks/`) by worktree path at read time, and keeps an `offline` row with the
last note when the tab is gone. Details and the storage layout are in
`work/README.md`.

What does not work: iTerm2 on macOS only (the Terminal seam exists, a tmux
implementation does not). STATE, LAST PROMPT and the dash join need the hook
installed (`python3 hooks/install.py`); without it every session reads
`offline`. Typing into a pane is the only input channel into an interactive
Claude Code session, and it is blind: `say` presses Enter and never reads a
reply or checks that Claude was ready (it does refuse empty text, exit 2).
Nothing watches the panes, so a tab closed by hand leaves an `offline` record
until `rm`. `rm` refuses a dirty worktree without `--force`, `--force`
discards uncommitted changes there, and `rm` refuses when another work
session's worktree lies inside the one it is about to remove (`--keep-worktree`
skips both the removal and that check). `--branch B` must
already exist locally (no DWIM from remotes), and cannot be combined with
`--no-worktree`. `.savannah-worktrees/` lives inside your repo and shows up
as untracked unless you exclude it. Tests drive a fake `osascript` and a
throwaway git repo; the real AppleScript is compile-checked against iTerm2's
dictionary, not run in CI.

## Layout

See the table in `CLAUDE.md`. Short version: `idl/` is the contract,
`src/` is the daemon and its hand-written TOML/JSON/stream-json plumbing
(zero dependencies beyond libsong and POSIX, on purpose), `cli/` is the
human remote control, `tools/fake_claude.cpp` is the test agent, `test/`
is the definition of done.

## License

MIT. See `LICENSE`.
