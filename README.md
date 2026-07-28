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
- **Deterministic tests, zero tokens**: everything above is covered by five
  test suites driven by `fake-claude`, a scenario-driven stand-in that
  speaks the exact stream-json subset savannahd parses. `ctest` runs the
  whole mesh locally in about two seconds.

## What does not work yet

Stated on purpose; this list shrinks phase by phase (see `DESIGN.md`).

- No mDNS discovery, no HMAC, no second machine (Phase 3). TCP mode binds
  loopback only until auth lands.
- No MCP shim, so no `ask_peer` from a real Claude Code session yet
  (Phase 4: the payoff).
- Not yet exercised against the real `claude` CLI in CI. fake-claude pins
  the stream-json subset; a `SAVANNAH_LIVE=1` drift check is planned.
- Remaining Phase 2 scenarios: slow drip, stderr noise.
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

## Layout

See the table in `CLAUDE.md`. Short version: `idl/` is the contract,
`src/` is the daemon and its hand-written TOML/JSON/stream-json plumbing
(zero dependencies beyond libsong and POSIX, on purpose), `cli/` is the
human remote control, `tools/fake_claude.cpp` is the test agent, `test/`
is the definition of done.

## License

MIT. See `LICENSE`.
