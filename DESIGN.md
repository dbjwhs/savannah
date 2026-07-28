# Savannah — Design Document

**Claude-to-Claude agent mesh, built on [song](https://github.com/dbjwhs/song).**
v0.3 (as-built) · July 28, 2026 · Dennis Jones + Claude
Status: **Phases 1 and 2 complete** (solo node over pipes,
giant-chunk/UTF-8/drip/stderr-noise/busy/cancel hardening, loopback TCP
multi-client, `SAVANNAH_LIVE=1` drift check green against the live CLI
7/28/2026; 6 test suites, live one skipped unless opted in).

> One-liner: Claude Code instances become discoverable peers on your network.
> Any agent can ask any other agent for help, mid-task, on its own judgment.
> song provides the substrate (typed IDL, binary wire protocol, process
> supervision, mDNS discovery, streaming, HMAC/TLS); MCP provides the standard
> face so stock Claude Code needs zero forking.

This document is the source of truth for architecture and rationale.
`CLAUDE.md` holds working conventions and the pinned wire/stream contracts.
The decision log at the bottom is append-only.

---

## 1. Why this exists (and why it is not redundant)

Surveyed July 2026. The space is active, which validates demand; nobody
occupies this design point:

| Prior art | Shape | Why Savannah differs |
|---|---|---|
| claude-peers-mcp (louislva, jamditis fork) | Central SQLite broker; delivers by typing into tmux panes; Tailscale gossip cross-machine | Chat between live sessions. Savannah is typed RPC to supervised headless workers |
| WebSocket-broker homebrews | ~80-line relay, experimental channels flag | No auth, no persistence, research-preview API |
| Google A2A | HTTP/JSON-RPC agent cards, enterprise/cloud, 150+ orgs | Heavyweight, web-native, cross-org. Savannah is two boxes on a LAN with zero config |
| anthropics/claude-code#28300 | Open feature request for cross-machine agents | The product does not do it; people want it |

Savannah's five differentiators: **mDNS zero-config discovery** (no broker, no
relay, no tunnel), **typed IDL contract + binary streaming** (not JSON chat),
**supervised headless workers** (`claude -p` under crash containment, not
someone's live terminal), **no central anything**, and **a zero-dependency
substrate we own end to end**.

The meta-reason: Savannah is song's showcase workload. The thesis is that the
missing piece around agents was always plumbing (discovery, supervision, typed
contracts, streaming, security) and song already is that plumbing. Building
Savannah stress-tests the thesis and feeds a wishlist back into song
(seven findings in Phase 1 alone; see CLAUDE.md).

## 2. Architecture: two layers

**Layer 1 — song mesh (substrate).** Each machine runs `savannahd`, a C++20
daemon exposing that machine's agent as a song service (`AgentNode`). Nodes
register via mDNS (`_agent._song._tcp`), stream responses, carry HMAC auth,
and inherit song's process supervision: agents are flaky long-running
processes, and crash containment + restart is exactly the right tool.

**Layer 2 — MCP shim (face).** A small MCP stdio server, `peer` (Python
first: song's generated Python client + MCP SDK), registered in each Claude
Code config. Tools: `list_peers`, `ask_peer(name, prompt)`,
`peer_status(name)`. Its implementation is a song client. The win: the model
itself decides when to delegate ("have the linux box run the tests," "get a
clean-context review") as part of its reasoning loop, not an orchestration
script.

Key insight: LLM latency (seconds) dwarfs wire latency (microseconds).
song's value here is not speed. It is discovery, supervision, typed
contracts, streaming, and security.

```
Claude Code (A)                          Claude Code (B)
     |  MCP: ask_peer("linux-box", ...)       ^
     v                                        | spawns claude -p per ask
  peer shim (song client)                savannahd (song service)
     |                                        ^
     +---- mDNS discovery / HMAC / stream ----+
                    (song wire)
```

## 3. The wire contract (as built)

`idl/agent.song`, compiled by songc into `generated/savannah.hpp`:

- `AgentChunk { i32 kind; string payload; }` streamed from `ask()`.
  kind: 0 TEXT (assistant text delta, verbatim), 1 TOOL_EVENT (one-line
  summary, "Bash: git status", truncated at 120 chars), 2 RESULT (compact
  JSON trailer: cost_usd, duration_ms, num_turns, is_error).
- Every ask ends with exactly one RESULT chunk. If the agent dies first,
  savannahd synthesizes `{"is_error":true,"reason":...,"exit_code":...}`.
  Cost visibility ships in v1 for free because the real CLI's result event
  carries usage.
- `AgentInfo` (name, model, workspace, tags, protocol_ver) from `info()`.
- `AskOptions` (timeout_ms, fresh_context, system_hint).
- Unary methods (`info`, `cancel`, `status`) live on service id 1; streaming
  `ask` lives on service id 2 (`src/wire_ids.hpp`) because song dispatches
  stream-vs-unary per service id, per its own backup example.
- The stream-json subset savannahd parses is pinned in CLAUDE.md; fake-claude
  implements the identical subset, and a `SAVANNAH_LIVE=1` test (Phase 2)
  guards against real-CLI drift.

## 4. Components

| Component | Language | Job | Status |
|---|---|---|---|
| `savannahd` | C++20 | song service; forks headless agent; stream-json → typed chunks; single-flight; timeout/cancel/kill | ✅ Phase 1 |
| `savannah` CLI | C++20 | human remote control + test harness: ask/info/status | ✅ Phase 1 (local pipes) |
| `fake-claude` | C++20 | deterministic stream-json stand-in, 9 scenarios incl. hang/die/garbage/giant/utf8-split/stderr-flood | ✅ Phase 1+2 |
| config (TOML subset) | C++20 | hand-written; tables, strings, ints, bools, string arrays; nothing more on purpose | ✅ Phase 1 |
| JSON (subset) | C++20 | hand-written; full value model, \uXXXX + surrogates; exists to read stream-json | ✅ Phase 1 |
| `peer` MCP shim | Python | list_peers / ask_peer / peer_status over song | Phase 4 |

## 5. Security and trust model

- LAN threat model: HMAC-SHA256 shared key across the mesh (song
  SecureTransport). TLS-PSK for anything beyond the LAN.
- Each node uses its own Anthropic auth; no key sharing. Every ask_peer burns
  real tokens on the remote node; the RESULT trailer reports what it cost.
- `allowed_tools` / `workspace` are enforced by delegation: savannahd passes
  the CLI's own flags and supervises the process. savannahd is not a sandbox,
  and the docs say so.

## 6. Sessions: v1 is deliberately stateless

Every `ask` is a fresh `claude -p`. Clean, testable, no context-lifetime
headaches, and it matches the best first use case (clean-context review).
v2 adds persistent sessions (`--resume` / Agent SDK) with session ids in the
IDL, only after v1 proves the loop. Session semantics, cooperative
cancellation, and context lifetime are the hard parts; they are sequenced
late on purpose.

## 7. Roadmap (tests define done)

1. ✅ **Solo node** — CLI → savannahd → fake-claude over local pipes.
   Done 7/27/2026: 4 suites green, -Werror clean, GCC 13.
2. ✅ **Streaming hardening** — done 7/28/2026: giant chunks (512 KiB split
   around song's 1 MB string cap), mid-UTF8 splits at pipe and chunk layer,
   slow drip past the poll tick, stderr floods past pipe capacity,
   single-flight busy, cancel-while-busy; `savannahd --tcp` loopback
   multi-client landed as the test vehicle (and Phase 3 transport embryo).
   `SAVANNAH_LIVE=1` drift check runs the real CLI through the savannahd
   parse path; green against claude CLI on 7/28/2026, skipped in CI.
3. **Two machines** — mDNS + HMAC, `savannah ls` shows both. Real-LAN only
   (M4 + linux box); never in CI.
4. **THE PAYOFF: MCP shim** — Claude Code on A calls `ask_peer("linux-box",
   ...)` mid-conversation. Demo: A asks B to run B's test suite and
   summarize failures.
5. **Patterns** — reviewer pair (author node + clean-context reviewer),
   tag fan-out ("all idle nodes tagged cpp"), file transfer via song's
   datacopy pattern.

## 8. Known limitations (stated, on purpose)

- NodeBusy arrives as an error RESULT chunk, not a wire-thrown error
  (stream-dispatcher error semantics in song not yet investigated).
- Single-flight per node; N-flight is a v2 option via run_tcp_multi.
- No cross-node file transfer in v1 (prompt out, text back).
- Cancel is SIGKILL, not cooperative.
- Cost rollup across the mesh: deferred (per-ask trailer exists today).

## 9. Decision log (append-only)

- **7/21/2026** — Project named Savannah (after the artist). Architecture:
  song substrate under MCP face. v1 stateless, single-flight. Shim Python
  first. Phase plan with fake-agent CI (cql offline-mode pattern).
- **7/27/2026** — Phase 1 built and green in one session (cloud Linux,
  GCC 13). Chunks are typed structs (AgentChunk), not prefixed strings;
  `kind` is i32 on the wire until songc emits enum codecs. Streaming split
  onto its own service id (song dispatch rule). Doc comments in IDL are
  single-line until songc handles multi-line. Seven song findings recorded
  in CLAUDE.md; `patches/song-cstdint.patch` required to build song on
  Linux GCC 13. Repo DESIGN.md (this file) becomes source of truth; the
  copy in Claude Memory is a pointer.
- **7/28/2026** — Phase 2 core landed (macOS this time; suite now 5/5).
  The giant-chunk test found silent data loss: song's `encode_string`
  refuses >1 MB, and a throwing stream dispatcher sends `stream_end`
  before the error reply, so clients saw a clean empty stream (findings
  8 and 9). Decision: savannahd splits TEXT payloads at 512 KiB
  (`kMaxChunkPayload`); clients already concatenate TEXT, so the contract
  holds and split points may legally land mid-UTF8. Non-TEXT chunks stay
  whole ("exactly one RESULT chunk" is load-bearing). Exception paths
  hardened: busy gate cleared via RAII, agent child reaped when the sink
  throws. Decision: `savannahd --tcp PORT` (run_tcp_multi, loopback only
  until HMAC) added ahead of Phase 3 because pipe mode dispatches
  sequentially, making NodeBusy and cancel-while-busy unreachable and
  untestable over pipes.
