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
- **Mesh plumbing, validated on one machine** (Phase 3, LAN acceptance
  pending): with a shared key, `savannahd --tcp PORT --mdns` authenticates
  every client via HMAC-SHA256 and advertises the node as
  `_agent-song._tcp`. `savannah ls` browses the mesh, and
  `savannah ask <name> --key FILE` resolves a node by name (or `--addr
  HOST:PORT` directly) and streams the answer back. Wrong or missing keys
  are rejected before any dispatcher runs.
- **Deterministic tests, zero tokens**: everything above is covered by
  seven test suites driven by `fake-claude`, a scenario-driven stand-in
  that speaks the exact stream-json subset savannahd parses. `ctest` runs
  the whole mesh locally in a few seconds; the mDNS self-discovery suite
  is opt-in (`SAVANNAH_MDNS=1`) because it advertises on your real
  network.

## What does not work yet

Stated on purpose; this list shrinks phase by phase (see `DESIGN.md`).

- The two-machine acceptance run (both nodes in one `savannah ls`, ask
  across the LAN) has not happened yet; all Phase 3 plumbing is so far
  validated on a single machine. `--tcp` alone binds loopback; `--mdns`
  binds all interfaces and refuses to start without an HMAC key.
- No MCP shim, so no `ask_peer` from a real Claude Code session yet
  (Phase 4: the payoff).
- The real `claude` CLI is never exercised in CI. fake-claude pins the
  stream-json subset; run `SAVANNAH_LIVE=1 ctest --test-dir build -R
  test_live_schema` manually to check the live CLI against it (one tiny
  prompt, burns a few tokens; skipped otherwise).
- v1 is stateless: every ask is a fresh `claude -p`, no sessions.
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

TEXT chunks stream to stdout verbatim; tool events and the result trailer
(cost, duration, turns, is_error) go to stderr. Exit code 0 iff the ask
succeeded, so it pipes cleanly.

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
trailer reports what the ask cost.

## Mesh mode (two machines)

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
authenticate. Traffic is HMAC-authenticated, not encrypted (TLS-PSK is a
later phase); treat the mesh as LAN-trust.

## Layout

See the table in `CLAUDE.md`. Short version: `idl/` is the contract,
`src/` is the daemon and its hand-written TOML/JSON/stream-json plumbing
(zero dependencies beyond libsong and POSIX, on purpose), `cli/` is the
human remote control, `tools/fake_claude.cpp` is the test agent, `test/`
is the definition of done.

## License

MIT. See `LICENSE`.
