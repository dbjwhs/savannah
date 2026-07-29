# CLAUDE.md — savannah

Claude-to-Claude agent mesh on the [song](https://github.com/dbjwhs/song) substrate.
Read `idl/agent.song` first; it is the contract everything else serves.

## What this is

Every machine runs `savannahd`, a C++20 daemon exposing that machine's headless
Claude (`claude -p --output-format stream-json`) as a song service (`AgentNode`),
discoverable over mDNS, streamed, HMAC-authenticated, process-supervised.
An MCP shim (`peer`, Python, lives in `shim/` when it lands) gives stock Claude
Code three tools: `list_peers`, `ask_peer`, `peer_status`. The model decides when
to delegate. No forking Claude Code.

Design doc of record: `DESIGN.md` in this repo (architecture, rationale,
roadmap, append-only decision log). One-liners:
- **v1 is stateless.** Every `ask` = fresh agent invocation. Sessions are v2.
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
# song checked out AND BUILT as a sibling (or pass -DSONG_ROOT=/path/to/song).
# CMake hard-fails unless ../song/build has libsong.a and songc.
git clone https://github.com/dbjwhs/song ../song
cmake -S ../song -B ../song/build && cmake --build ../song/build --target song songc
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j
ctest --test-dir build --output-on-failure
ctest --test-dir build -R test_json --output-on-failure   # single test
```

`songc` (built from SONG_ROOT) code-generates `generated/savannah.hpp` from
`idl/agent.song` at build time. Never edit generated code; edit the IDL.

## Layout

| Path | What |
|---|---|
| `idl/agent.song` | The AgentNode contract. Compiles via songc. |
| `src/config.{hpp,cpp}` | Hand-written TOML-subset parser + `NodeConfig`. |
| `src/json.{hpp,cpp}` | Hand-written minimal JSON parser (stream-json needs). |
| `src/stream_json.{hpp,cpp}` | claude stream-json events → `AgentChunk` mapping. |
| `src/agent_process.{hpp,cpp}` | Spawn/supervise the headless agent, line pump, timeout, kill. |
| `src/savannahd.cpp` | The node daemon: song runtime, dispatchers, single-flight. |
| `cli/savannah_main.cpp` | Human remote control: `savannah ls`, `savannah <ask\|info\|status> <node>` (local pipes, or mesh via mDNS/--addr with --key). |
| `tools/fake_claude.cpp` | Fake agent speaking genuine stream-json. Scenario-driven. |
| `test/` | Unit tests per component + end-to-end echo integration. |

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
the key. `test_tcp_flight` and `test_secure` run in CI; `test_mdns` is opt-in
via `SAVANNAH_MDNS=1` (advertises on the real network).

## Key patterns (learned from song itself)

- **Streaming is hand-wired, backup.song-style.** songc parses the `stream`
  modifier but codegen does not yet emit streaming proxies (`is_stream` is unused
  in codegen.cpp). Client side: `conn.call_streaming(kService_AgentNode,
  kMethod_AgentNode_ask, args)` → `StreamReader`; decode each Buffer chunk with
  generated `decode_AgentChunk`. Service side:
  `runtime.register_stream_dispatcher` + `encode_AgentChunk` into
  `StreamWriter::write`. **song wishlist #1: teach codegen about `is_stream`.**
- **Properties are class-level in songc**, not service-level. `status()` is a
  polled method in v1. v2: promote the agent to a song class with a `status`
  property to get push via subscription fan-out. **song wishlist #2.**
- **Streaming methods need their own service id.** runtime.cpp checks stream
  dispatchers first and they shadow the unary dispatcher for that id, so ask()
  lives on `savannah_wire::kService_AgentNode_Stream = 2` (src/wire_ids.hpp),
  exactly like backup.song's split. songc cannot express this. **wishlist #3.**
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
   in generated code (syntax errors). Workaround: single-line docs only.
5. Enums referenced in structs generate calls to `encode_<Enum>`/`decode_<Enum>`
   that are never emitted. Workaround: `AgentChunk.kind` is `i32` on the wire.
6. `is_stream` is parsed but ignored by codegen (proxies come out unary).
   Streaming is hand-wired via `call_streaming` + generated method ids.
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
- `{"type":"result","total_cost_usd":...,"duration_ms":...,"num_turns":...,
  "is_error":...}` → RESULT chunk, compact JSON trailer
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

1. **Solo node** (this tree): CLI → savannahd → fake-claude over local pipes. ✅ when e2e echo test passes.
2. **Streaming hardening**: pathological fake scenarios (giant chunks, mid-UTF8 splits, hang, die-early) all handled.
3. **Two machines**: mDNS + HMAC, `savannah ls` shows both. Runs on real LAN only (not in CI).
4. **MCP shim**: `ask_peer` from stock Claude Code. The payoff demo.
5. **Patterns**: reviewer pair, tag fan-out, maybe datacopy file transfer.
