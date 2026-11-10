# Savannah — Design Document

**Claude-to-Claude agent mesh, built on [song](https://github.com/dbjwhs/song).**
v0.4 (as-built) · October 27, 2026 · Dennis Jones + Claude
Status: **Phases 1 through 4 complete; Phase 5 patterns 5a through 5d
landed** (task mesh, session handoff, local-session attention, local work
sessions). Two-machine acceptance ran 7/29/2026 (both platforms in one
`savannah ls`, bidirectional HMAC asks); the peer MCP shim landed the same
day and completed the payoff loop live: MCP tool call on the mac, answer
streamed back from the linux node. 12 ctest suites plus the dash's Go tests;
live-CLI and mDNS suites opt-in.

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
register via mDNS (`_agent-song._tcp`), stream responses, carry HMAC auth,
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

## 6. Sessions: `ask` is stateless, sessions live elsewhere

Every `ask` is a fresh `claude -p`. Clean, testable, no context-lifetime
headaches, and it matches the best first use case (clean-context review).
That was the whole v1 story and it still holds for `ask`. Persistent sessions
arrived as separate things, never through `ask`: Phase 5a tasks are headless
`claude --resume` workers owned by savannahd with session ids in the IDL
(`TaskInfo.session_id`), and Phase 5d work sessions are the owner's own
interactive Claude Code tabs, file-owned and joined to the hook's session
files at read time. Cooperative cancellation and context lifetime remain the
hard parts and remain open.

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
3. ✅ **Two machines** — acceptance ran 7/29/2026 (M4 mac + dbj-devone,
   Ubuntu 26.04 GCC 15): both nodes in one `savannah ls`, asks green in
   both directions with HMAC, keyless clients dropped. Bring-up findings
   upstreamed to song same day; macOS gotcha recorded in README (the
   application firewall silently eats inbound for unapproved binaries).
4. ✅ **THE PAYOFF: MCP shim** — landed 7/29/2026. `shim/peer.py`, MCP
   stdio server, stdlib-only Python, wraps the savannah CLI (discovery,
   HMAC, streaming stay on the tested C++ paths). Live loop verified:
   MCP `ask_peer("dbj-devone", ...)` from the mac answered by the linux
   node. `.mcp.json` registers it for Claude Code sessions in this repo.
5. **Patterns**, landed so far:
   5a ✅ Task Mesh (persistent, resumable headless workers: task engine in
   savannahd, `task_*` CLI and shim tools, the dash); 5b ✅ session handoff
   (9/17/2026, `DECISION_NEEDED` convention plus the dash as decision inbox,
   zero server changes); 5c ✅ local-session attention (9/17/2026, Claude
   Code hook plus the dash's LOCAL SESSIONS section and unified "need you"
   count); 5d ✅ local work sessions (10/27/2026, `work/`: one iTerm2 tab
   per work item with interactive Claude in a worktree over a shell,
   plain-text notes, `work_*` shim tools, dash WORK column; iTerm2 only);
   5e ✅ hub (10/29/2026, `savannah hub DIR`: a coordinating Claude session
   over the `work_*` tools; optional since 5f); 5f ✅ direction reset
   (11/3/2026: bare `savannah` is the board, the dash in work mode;
   `savannah mesh` the mesh board; the Terminal seam on iTerm2's `it2`;
   `work open`, `work repos`, `.worktreeinclude`).
   Remaining: reviewer pair (author node + clean-context reviewer), tag
   fan-out ("all idle nodes tagged cpp"), file transfer via song's datacopy
   pattern, a tmux Terminal behind the work/ seam, and live-status push via
   song classes/properties in place of today's polling.

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
- **7/28/2026 (Phase 3)** — Mesh plumbing landed. The mDNS self-test found
  song finding 10: DNS-SD requires two-label service types, so the
  `_agent._song._tcp` format from the original design could never register
  (dns_sd BadParam); fixed upstream in song, mesh type is now
  `_agent-song._tcp`. Decision: savannahd carries its own accept loop
  (thread per client, capped at 32) because run_tcp_multi has no
  SecureTransport hook (finding 11). Policy: `--tcp` alone binds loopback;
  `--mdns` binds all interfaces and refuses to start without
  mesh.hmac_key_file, so an advertised node always authenticates. HMAC is
  authentication only, not encryption; TLS-PSK stays a later phase.
- **7/29/2026 (Phase 3 acceptance)** — Two-machine run green: mac + Ubuntu
  26.04 in one `savannah ls`, bidirectional HMAC asks, keyless rejected.
  Bring-up found and upstreamed two more song fixes: runtime_test missing
  <algorithm> under GCC 15, and the Avahi implementation's includes nested
  inside namespace song (the Linux mDNS path had never compiled anywhere;
  no prior build host had Avahi headers). savannah now mirrors song's
  optional avahi-client link dependency. Field note: macOS ALF completes
  the TCP handshake in-kernel but never delivers connections to unapproved
  binaries; looks like a client-side read timeout while loopback works.
  Allow savannahd via socketfilterfw, and re-allow after rebuilds.
- **7/29/2026 (Phase 4)** — Decision: the peer shim wraps the savannah CLI
  via subprocess instead of speaking song natively. song's Python runtime
  is unary-only (no streaming, no SecureTransport, no discovery), and
  reimplementing those in Python would fork the wire logic; the CLI hop
  keeps one implementation of everything hard. The MCP layer itself is
  hand-written stdlib (newline-delimited JSON-RPC 2.0), keeping the
  zero-dependency rule intact. v2: generated Python bindings once song's
  Python runtime learns streaming and HMAC. Payoff loop verified live.
- **7/30/2026 (flagship demo)** — Both nodes now run the real claude CLI
  (tags carry real-claude; tools Bash/Read/Glob/Grep). The roadmap's
  Phase 4 demo executed literally: the mac asked dbj-devone to run its
  own test suite; the remote agent ran ctest, reported 9/9, and explained
  the two opt-in skips, in 15.5 s for USD 0.15. Streaming fix (findings
  14/15) proved out the same day: real first-token latency of 5 to 10 s
  is routine and now survives.
- **7/30/2026** — Deployment posture: Linux nodes run savannahd as a user
  systemd service with lingering (survives reboots, restarts on crash, no
  login needed). Ephemeral ports on every start are fine on purpose: mDNS
  re-advertises, nothing pins. Unit recorded in README. macOS analog
  (launchd) not written yet.
- **8/25/2026 (dash view mode)** — The dash grew the tmux-like piece: a
  full-screen live view per worker (empty Enter/Tab on a board row), with
  Left/Right flipping between workers across the whole mesh and the prompt
  line still live at the bottom. Decision: it rides the CLI's `task tail`
  seam in a respawn loop rather than holding a connection open. tail
  replays then follows one turn and exits, so every rerun is a fresh full
  replay; the screen swaps to a new run's buffer only once it has caught
  up with the last completed one (the transcript is append-only, so the
  newer replay is a prefix-superset), which makes the loop self-healing
  after disconnects and flicker-free by construction. The dash stays a
  thin client with zero wire knowledge; push (song property fan-out or a
  `watch --json` event source) can replace the loop under the same
  rendering. First Go tests landed in dash/ with it (wrap/trim/switch
  helpers plus the tail run state machine driven by synthetic messages).
- **9/17/2026 (session handoff, design B)** — Reviewed the "Session Handoff
  via Savannah" brief (CI job needs a human decision on another machine).
  Decision: design B, decision delegation, and the insight that shrank it:
  the task engine already IS the pause/resume primitive. A worker that needs
  a human ends its turn with a final line
  `DECISION_NEEDED {"question":...,"options":[...],"context":...}` and goes
  idle; a pending decision is a task whose state is idle/incomplete and
  whose last output line carries the marker; the answer is an ordinary
  task_send; the next turn's output clears the flag. Zero server changes,
  no new wire surface, no session migration (design A stays shelved). Not a
  blocking mesh call on purpose: chunk timeouts are sized to agents, not
  hours (finding 14), and a held stream would pin single-flight. The dash
  is the inbox (header count, highlighted rows, view banner, answer inline);
  test_task_flight pins the contract that a marker-bearing final line
  reaches last_line byte for byte. Full write-up: docs/handoff.md, including
  security posture (HMAC authenticates, does not encrypt; answers execute
  with the worker's tool permissions) and stated limitations (convention,
  not enforcement; 1 s poll, no push yet). Field note from bring-up: macOS
  Local Network privacy (TCC) can block third-party binaries from LAN
  unicast with EHOSTUNREACH while platform ssh/nc/ping work; the ssh tunnel
  plus --addr 127.0.0.1:PORT is the proven workaround and doubles as the
  locked-down-network recipe.
- **9/17/2026 (task_rm)** — Field gap found minutes after the handoff
  landed: a cancelled or failed task sat on every board forever (the auth
  expiry relic on dbj-devone), and the table had no remove operation at
  all. Added `task_rm(id) -> bool` through the whole stack: IDL method,
  supervisor `remove()` (refuses running tasks, joins the worker, prunes
  any worktree, erases), CLI `task rm`, shim `task_rm` tool, dash Ctrl-X
  on the board (a printable key would collide with the prompt input). The
  table switched from unique_ptr to shared_ptr on purpose: tail() streams
  a replay for seconds without holding the supervisor lock, so remove()
  drops the table entry while a live tail keeps the Task alive through
  its own reference. cancel() now clears the branch after pruning so a
  later rm does not prune twice. rm of an idle worktree task discards
  that worktree, same contract as cancel: harvest results before
  forgetting a worker.
- **9/17/2026 (local-session attention, 5c)** — Answered "my iTerm tab hell
  is unmanageable past 5-6 sessions." Key distinction: savannah's workers are
  headless sessions savannahd owns; the user's tabs are interactive sessions
  it cannot see. So the fix is a bridge, not the mesh. A Claude Code hook
  (`hooks/session-status.py`, stdlib) fires on SessionStart/UserPromptSubmit/
  Notification/Stop/SessionEnd and upserts a per-session status file under
  ~/.claude/savannah/sessions; the dash reads them into a LOCAL SESSIONS
  section above the mesh workers, floats the ones needing you (a permission
  prompt or idle nudge, from the Notification event) to the top, and jumps to
  the iTerm2/tmux tab on Enter. One unified header counter sums sessions
  waiting + pending worker decisions: one glance, total. Pure local files, no
  daemon/network/tokens, so `--no-mesh` is a standalone tab monitor for a
  locked-down work box. Confirmed the Claude Code hook schema via the
  claude-code-guide agent (Notification carries notification_message and
  notification_type permission_prompt|idle_prompt; env like ITERM_SESSION_ID
  inherits into the hook). Also answered the recurring "does this use an API?"
  question: no. Mesh is song's binary wire protocol over TCP (HMAC, not
  HTTP/REST); each node's brain is the claude CLI subprocess, which is the only
  thing that talks to Anthropic's API (HTTPS), holding the auth. savannah never
  holds an Anthropic key. install.py merges the hook into settings.json
  (idempotent, backs up, --uninstall); test_hook.py pins the state machine.
- **10/27/2026 (local work sessions, 5d)**: The problem was the owner's own
  habit, not the mesh. Every work item is an iTerm2 tab split in two:
  interactive Claude Code in a git worktree on top, a shell cd'd into the
  same worktree below. Past a handful of tabs nobody remembers what each
  session was doing, and there was no way for a hub session to talk to the
  others except the human typing. Decisions: (1) the brain stays the
  interactive Claude Code in the upper pane, NOT a mesh task; a `task_*`
  worker is headless and savannahd-owned, and the owner wants to sit in
  these sessions. (2) A work session is a local, file-owned object in the
  same spirit as hooks/: `~/.claude/savannah/work/<name>/record.json` plus
  `notes.md`, zero savannahd or song involvement, zero tokens spent by the
  tool. (3) iTerm2 first, driven over AppleScript (`create tab with default
  profile`, `split horizontally with same profile`, `write text`, `select`,
  `close`), behind a four-method Terminal seam (create_split_tab, write,
  focus, close_tab) so tmux can plug in later without touching the verbs;
  the osascript binary is `$SAVANNAH_OSASCRIPT` so tests run a fake. (4)
  Typing into the pane (`write text`) is the only input channel into an
  interactive Claude Code session; hooks can add context to a session but
  cannot speak to it, so `say` is how the hub talks to a session, and it is
  one-way. (5) The join between a work session and its live Claude Code
  session is computed at read time, `realpath(hook.project) ==
  realpath(record.worktree)`, preferring the hook whose iTerm id matches the
  recorded Claude pane, else the newest; `work ls` and the dash both do it,
  and neither side writes into the other's file. (6) Notes are plain text
  appended with a local timestamp heading, for the human, in the owner's
  words; on `rm` they move to `archive/<name>-<created>.md` rather than
  vanishing, and the branch is never deleted. Shim gained `work_*` tools
  whose descriptions insist these are the owner's local tabs, not workers;
  the hook gained `last_prompt` and `transcript_path` so a tab can say what
  it was doing; the dash gained a WORK column and `offline` rows (last note
  in WHAT, Enter still focuses the recorded pane). Build-time findings:
  iTerm2 tabs have no AppleScript id, so `tab_id` is informational
  (`w<window>t<index>`) and `rm` closes by pane ids; `rm` runs `git worktree
  remove` before closing the tab so a refused dirty worktree leaves
  everything in place; `new` rolls back its worktree and branch if the
  terminal step fails. Stated limitations: macOS + iTerm2 only; `say` and
  `sh` type blind (Enter pressed, no reply read, readiness unchecked);
  nothing watches the panes, a hand-closed tab is `offline` until `rm`;
  `.savannah-worktrees/` sits untracked inside the repo; hook files are
  trusted; the real AppleScript is compile-checked against iTerm2's
  dictionary, not executed in CI. Deliberately NOT done: owning the Claude
  brain as a mesh task (would make these headless and savannahd-owned, the
  opposite of the point), and hook-side writes into work records (the hook
  stays a dumb best-effort recorder; one writer per file). Tests: test_work
  (temp work dir, temp sessions dir, throwaway git repo, fake osascript),
  test_shim extended through the MCP frames, test_hook for the two new
  fields, Go tests for the join and the offline rows.
  Live bring-up on the mac the same day found two things the fake could not:
  `index of t` on the tab reference returned by `create tab` is not gettable
  (iTerm2 raised -1728), so `tab_id` is now derived from the tab count at
  creation, and every id is read with an explicit `get`; and a script that
  failed after the tab existed left an orphaned tab running `claude`, so the
  create script now closes its own tab on any error before rethrowing, and
  the python rollback (worktree and branch) was confirmed live. Also noted:
  a fresh worktree is a new folder to Claude Code, so the first launch sits
  at the folder-trust prompt and the hook join reads `offline` until it is
  answered. End-to-end smoke (new, sh, jump, say, rm) is green against the
  real iTerm2; it stays out of CI on purpose.
- **10/29/2026 (the hub, 5e)**: Live testing of 5d showed the tool was one
  step short of the owner's actual picture: not "a tab per work item that I
  drive from a shell" but "one Claude session in the directory that holds
  ALL my repos, which opens the tabs and reads them back while I watch the
  board". Two gaps. (1) The peer shim was registered only in savannah's own
  `.mcp.json`, with relative paths, so a Claude started in `~/ng/dbjwhs` had
  no `work_*` tools at all. `work hub DIR` fixes the wiring: it merges a
  `peer` entry with absolute paths into `DIR/.mcp.json` (other servers kept,
  malformed file refused), drops a starter `CLAUDE.md` (template
  `work/hub_CLAUDE.md`, never overwrites) that tells the hub Claude its job is
  to delegate to tabs and not edit repos itself, then execs `claude` in DIR.
  (2) `say` had no reply channel: the hub could type into a tab but only the
  human could read the answer. `work output <name> [--last N]` (shim
  `work_output`) parses the transcript Claude Code itself writes for that tab
  (the hook's recorded `transcript_path`, else the newest `.jsonl` under
  `$CLAUDE_CONFIG_DIR/projects/<encoded worktree>/`) and returns the last N
  turns as prompt + assistant text, dropping thinking, tool calls, tool
  results and sidechain lines. Decisions: read Claude Code's own file rather
  than scrape the pane or wrap the interactive session in a pty, because the
  file is complete, UTF-8 and already there; pin the subset we read with a
  hand-written transcript in test_work rather than pretend it is a contract;
  keep the hook path primary and the projects-dir guess a fallback; the
  hub stays a Claude Code session with files on disk, no new daemon, nothing
  in savannahd or song. Rejected: a `work hub` that is its own TUI or REPL
  (the dash already is the board; the hub's brain is Claude), and extra
  `claude` arguments through `hub` (use `--no-launch`). Live on the mac the
  same day: `hub ..` wrote both files into `~/ng/dbjwhs`, and `output`
  read two real turns back from a tab opened with `new` and driven with
  `say`. Tests: test_work (transcript fixture, projects fallback,
  `CLAUDE_CONFIG_DIR`, hub create/merge/keep/refuse/exec via a fake claude),
  test_shim (`work_output`, `last` validation).
  Same evening, second live test: the owner started `claude` in the hub and
  asked for pi to 20 digits; the hub computed it itself with Bash. Two
  more fixes. The template was too polite ("every real task") and only knew
  repo tasks; it now says EVERY request becomes a tab, and `hub` creates
  `DIR/.savannah-scratch/` (own short CLAUDE.md) for tasks that belong to no
  repo, because a tab opened in the hub dir would read the hub CLAUDE.md
  and become a hub. And "open claude" was the wrong verb for the owner:
  `savannah hub DIR` now remembers DIR in `~/.claude/savannah/hub.json`, and
  the C++ `savannah` binary with no arguments execs that hub (`savannah hub`
  / `savannah work ...` forward to the python module, path baked in at
  build as `SAVANNAH_WORK_PY`), so daily use is the one word `savannah`.
  Rejected: making bare `savannah` hub the cwd (it would scatter
  `.mcp.json`/`CLAUDE.md` into whatever directory you happened to be in).
  test_work now also drives the built CLI (bare, `hub`, `work`, missing
  module) when CMake hands it the binary.
  Third live test, headless (`claude -p "calculate pi to the 20th digit"`
  in the hub): the hub now called `work_list` and `work_new` for a scratch
  tab, exactly as the template says, and was stopped by permission prompts
  on its own MCP tools. So `hub` also merges `enableAllProjectMcpServers`
  and allow rules for the nine `work_*` tools plus `Bash(sleep *)` into
  `DIR/.claude/settings.json` (everything else kept, malformed refused,
  mesh tools deliberately not pre-allowed). Writing Claude Code settings is
  the owner's call, so that step was left for them to run.
- **11/3/2026 (direction reset: the board is the front door, 5f)**: Live
  use of 5e showed the two previous steps had drifted from the owner's
  picture. They did not want a Claude session as the thing they open; they
  wanted a standalone uber terminal: open savannah, see every work item,
  make a new one (a named tab, Claude on top, the worktree shell below),
  jump to it, steer it, close it, eight or more at a time across repos. The
  hub put a model into a loop that should be deterministic (first live run:
  the hub did the task itself; third: it stopped at its own permission
  prompts), and the AppleScript seam re-implemented plumbing iTerm2 now
  ships. Decisions: (1) bare `savannah` execs the dash in work mode, the
  board (`savannah mesh` is the old mesh board); the C++ CLI stays the one
  binary on PATH and the dash runs `savannah work ...` for every key, so
  the board and the CLI cannot disagree. (2) The Terminal seam moves to
  iTerm2's own `it2` CLI (iTerm2 3.7.6, the copy inside iTerm.app;
  `$SAVANNAH_IT2`): `tab new --window`, `session set-name/run/split/focus/
  close`, `app activate`, `session list --json`; AppleScript and
  `tools/fake_osascript.py` are gone, `tools/fake_it2.py` replaces them and
  the tests assert exact argv. (3) The hub and the `work_*` MCP tools stay
  as an optional layer for a Claude that wants to open tabs; the strict
  "every request is a tab" template and `.savannah-scratch/` stay as they
  were, parked, no longer the front door; `hub.json` doubles as the memory
  of the folder of repos the board offers (`work repos DIR`). (4)
  savannah's own worktree layout stays (`<repo>/.savannah-worktrees/<name>`,
  branch `work/<name>`), not Claude Code's `-w`, so a work item is
  savannah's object with a record, notes and an archive; Claude Code's
  `-n <name>` names the session so the tab title and `/resume` agree. (5)
  Borrowed from the field (agent-deck, ccmanager, claude-squad): restore
  after restart (`work open`: focus the tab, or make it again and `claude
  --continue` when the tab is gone), `.worktreeinclude` (the gitignored
  files a fresh worktree needs), and archive-before-delete, which 5d
  already had. Live it2 findings on the owner's Mac, pinned by the fake:
  `tab new` from a script needs an explicit `--window` ("No current
  window"), so the module uses the window of the session it runs in
  (`ITERM_SESSION_ID`), else the first window listed, else `window new`
  whose own tab becomes the work tab; `tab close --force` prints "Tab
  closed" and closes nothing, so tabs are closed pane by pane with `session
  close --force`; `session run --session S -- TEXT` takes the text as an
  argument, so no quoting layer of ours is involved; every call costs 200
  to 500 ms; `session get-status` is iTerm2's own Claude Code status hook
  (installed alongside ours, both kept: ours carries the transcript path
  and project the join needs). The first end-to-end run then showed the
  typed line landing in the tty queue before the shell was up: oh-my-zsh's
  "Would you like to update? [Y/n]" read one key and turned `cd` into `d`
  (an omz alias, so the rest ran in the wrong directory). Fix: both lines
  start with a space that nothing misses (and that `hist_ignore_space`
  keeps out of history); `tab new --command` (a pane that runs Claude
  directly) was considered and rejected because it runs without the
  owner's shell environment and the pane closes when Claude exits. Also:
  the tab name is the terminal title, which the shell's auto-title rewrites
  at every prompt, so `set-name` moved after the run and is best effort;
  `claude -n <name>` is what names the tab while Claude is up. Rejected: Claude Code's `--tmux` and `-w` as
  the substrate (would give up the named-tab habit and savannah's own
  records), a tmux-first rewrite (the owner lives in iTerm2 tabs; the seam
  still reserves tmux), and reading pane text through it2 as the reply
  channel (the transcript file is complete and already there). Tests:
  test_work rewritten against the fake it2 (argv-level assertions, the
  no-window path, a mid-create failure closes the tab, `open`
  focus/reopen/resume/leftover-pane, `repos`, `.worktreeinclude`,
  `pane_alive`, bare `savannah` execs a fake dash and `savannah mesh` adds
  `--mesh`), test_shim (`work_open`), Go tests for the work board (dialog,
  keys, argv per command).

### 11/3/2026, later (the board fills the screen; a note on every row)

The owner's first look at the board on a 2000 px window: the table sat in
the top-left corner with the rest of the window empty, and a plain tab (one
opened by hand, no work record) had nowhere to keep a note. Decisions:

1. **The layout follows the terminal.** The sessions table plans its
   columns from the width: under 120 columns the old compact plan; from 120
   on a TAB column, a NOTE column (the last note's first line) and PROJECT
   widened to the full `~/...` path when every path fits in 40 percent of
   the room, WORK grown to the longest name. The rest of the height is a
   detail panel for the highlighted row: its notes (every entry of
   notes.md, oldest first, the newest kept when they overflow) beside the
   end of its Claude's last reply, with the last prompt and the tab, branch
   and state above. The reply is `work output --last 1`, fetched when the
   highlighted row changes and when that row's hook file is updated (every
   prompt and every finished turn), never on a timer: one transcript parse
   per turn, not one per tick. The prompt box grows with the width too.
   Under 90 columns the panel stacks, under about 20 lines it is left out.
   (`dash/board.go`: sessionColumns, panelLines, refreshPanel.)
2. **Every row can carry a note; a plain tab is named first.** Ctrl-T on a
   plain tab turns the prompt line into "name this tab" (prefilled from the
   directory), Enter runs the new `work adopt <name> --session=<hook id>`,
   and the note prompt follows. Adopt writes an ordinary record pointing at
   the tab's directory and Claude pane with `adopted: true` and opens
   nothing; the pane match in the join rule binds exactly that tab, so two
   tabs in one directory stay apart. say, output, open, jump and notes work
   unchanged. `rm` of an adopted record closes the pane and archives the
   notes but never touches the directory (no git worktree remove, whatever
   the flags). Rejected: notes keyed by the Claude session id or by
   directory outside the work records (a second notes mechanism, and a
   directory key merges two tabs in one repo); `rm` leaving an adopted tab
   open (its row would come straight back as a plain tab).
3. Tests: `dash/board_test.go` (column plans never exceed the width, wide
   and compact headers, the panel for work, plain and offline rows, exact
   panel height, the Ctrl-T naming flow, suggestName, notes parsing,
   wrapping); `test/test_work.py` adopt section (record, join, note, say,
   sh, refusals, rm keeps the directory, open reopens and keeps adopted);
   `test/test_shim.py` work_adopt. Not verified live: adopting one of the
   owner's real tabs (rm would close it); the layout itself was checked
   live through a pty capture of the dash at 200 columns.
4. Live finding, same evening: `say` through `it2 session run` typed the
   text into the demo tab's Claude but never submitted it. `run` appends a
   newline (LF), which Claude Code's input box takes as a line break; the
   Enter key sends a carriage return. `ITerm.write` (say and sh) is now
   two `session send` calls, the text then `\r`, and a shell accepts the
   CR just the same. The smoke test the day before used `echo` in place of
   claude, which is why a shell newline looked fine; the fake cannot see
   this either, so it is pinned by the argv checks and this note.

### 11/3/2026, night (sections, drag, names, polling)

Third ask of the evening: "name each session", "session priorities
draggable: a top section of active high priority and a bottom section that
is less priority", and "know if a session is polling something for me and
communicate the status of the polling back to savannah".

1. **Two sections, the owner's order, kept in the records.** Each record
   carries `priority` ("active" or "later") and `rank`; the board shows
   ACTIVE then LATER, named rows by (rank, name) inside each, and the
   attention sort no longer moves a named row: the glyph, the colour and
   the "N need you" count carry it, because a row that jumps while you
   reach for it is worse than a glance at the header. Unnamed tabs close
   LATER in attention order, with their iTerm2 tab title in parentheses in
   the WORK column (`it2 session list --json` every 5 s; the `title` field
   is clean, `name` escapes slashes). Shift-Up/Down (Ctrl-Up/Down too)
   drags one step; the dash renumbers the sections 10, 20, 30 and runs
   `work move` only for the records whose place changed, after redrawing
   (optimistic; the 1 s re-read puts back whatever the CLI refused). `new`
   and `adopt` write `priority: active, rank: <created>`, so a new session
   lands at the bottom of ACTIVE. Rejected: the order in a dash-side file
   (two sources of truth, and `work ls` could not show it); a third
   "unnamed" section (two were asked for; the parenthesised title tells an
   unnamed row apart); mouse drag (Bubble Tea has it, the keys are enough
   and work over ssh). (`dash/order.go`; `work move`.)
2. **Ctrl-R names.** On a plain tab it is `work adopt` without the note
   that Ctrl-T adds, the name prefilled from the tab title (what Claude
   Code or the owner called the tab) before the directory; on a work
   session it is the new `work rename OLD NEW`, which claims NEW, moves
   record, notes and status and removes OLD. The worktree path and the
   branch keep the old name: renaming a checked-out worktree and branch
   under a running Claude is a bigger operation than a label change, and
   the label was the ask. The join is by path and the hook knows nothing of
   names, so a renamed session stays joined.
3. **Polling is self-reported.** `work status NAME -- "text"` writes
   `status.json` `{text, updated, count}` (count +1 per report; `--clear`
   removes it; `--here` finds the record from `$ITERM_SESSION_ID`, else
   the cwd). The SessionStart hook hands a work session's Claude that exact
   command as `additionalContext`, phrased as statements, not instructions
   (Claude Code treats imperative hook context with suspicion). The board
   shows state `polling` with the text, count and age, a panel line and a
   header count, only while the report is LIVE: the session is working and
   the report is newer than the hook's `updated`, which the hook stamps at
   the prompt and touches only at Stop, Notification and SessionStart; a
   report from an earlier turn stays in the panel as "last status, not
   live" until cleared. Rejected: inferring polling from tool-call patterns
   (the hook sees no tool calls, and a guess would be overclaiming);
   clearing the report from the Stop hook (join logic on a write path, and
   the liveness rule needs no write). Honest limits: an unreported wait
   looks like "working"; a permission prompt mid-turn hides the live marker
   until the turn resumes. (`dash/polling.go`; `work status`;
   `hooks/session-status.py`.)
4. Tests: `dash/order_test.go` (sections, planDrag, renumber writes only
   changes, the drag keys through the model with cursor follow and boundary
   crossing, Ctrl-R on plain and work rows, rename refusals, section
   headers and the empty hints, titles, suggestName from the title, polling
   live, stale and done, readStatus and readWork); `test/test_work.py`
   move, rename and status sections (incl. `--here`); `test/test_hook.py`
   the SessionStart context; `test/test_shim.py` work_move, work_rename,
   work_status. ctest 12/12 (2 skipped by design), `go test` green. Live:
   the board in tab 11 restarted on the new binary; the demo tab's Claude,
   asked through `work say` to run `work status demo` before a 45 s sleep,
   showed on the real board as `polling: waiting for sleep (poll 1 of 1)
   (1 poll, 14s ago)` with `1 polling` in the header while `work ls`
   showed the same STATUS; the hook run by hand against the demo worktree
   printed the context naming `build/savannah work status demo`; the real
   unnamed tabs showed their titles. Python side by a fork; its one
   deviation from the brief: `status --here` takes every positional as text
   (argparse cannot tell a name from text once the name is optional).

### 11/5/2026 (titles, snapshot and restore, one log)

Fourth ask: the name rule refused "Main Claude Code Session", so "a
separate free-text title (capitals, spaces) that the board shows while the
slug stays the identifier underneath"; "save all session state if requested
due to a pending reboot and iTerm relaunch, and when things are back get
everything set up the way it was with the Claude sessions opened again with
their state loaded"; and "Savannah needs logging: the raw log always
verbose, a key in the stamp saying normal, debug or verbose, and the
interface shows only what the user wanted; is this solved in the Go world
already?"

1. **Title beside the name, never instead of it.** The record gains
   `title` (free text, first line, 120 chars); `work title NAME -- TEXT`
   and `--clear` set it, `new` and `adopt` take `--title`, `ls` shows a
   TITLE column. The board's WORK column shows the title when there is one
   (28 columns, cut with `~`), the panel leads with it and keeps the name
   beside it, prompts say "say to <title>". The name stays what every verb,
   the worktree, the branch and `claude -n` take, so nothing that works
   today changes meaning. Where a name is typed (Ctrl-R, Ctrl-T, Ctrl-N) the
   board now accepts words: a valid name is used as typed, anything else is
   slugged (`Fix the parser` to `fix-the-parser`, `-2` when taken) and the
   words become the title; the dialog's hint shows the derived name before
   Enter. Ctrl-R on a work session edits the title; the slug rename stays a
   CLI verb (`work rename`), since a label edit is what the key is for and
   two meanings on one prompt line is a guessing game. Rejected: relaxing
   the name rule itself (names are directory names, branch names and tab
   names; spaces and capitals in all three is a tax forever); showing the
   title in the iTerm2 tab (the tab is named by `claude -n`, and `/resume`
   relies on it).
2. **Snapshot and restore are two verbs over the records plus the hook.**
   `work snapshot [--note]` writes `<work dir>/snapshots/<stamp>.json`
   (and `latest.json`): every record with its tab, panes, worktree and,
   from the hook file joined at that moment, Claude Code's session id and
   transcript path, plus every plain tab (directory, session id, pane,
   title). `work restore [STAMP|latest] [--dry-run] [--no-plain]` walks
   it: a record whose pane is gone gets its tab remade in its worktree with
   `claude -n <name> --resume <session id>` (else `--continue`, else
   fresh), a plain tab a single pane with `--resume`, a live one is skipped,
   nothing is ever closed. Claude Code's own state is its transcript, so
   `--resume` is the whole "state loaded back in"; a turn that was running
   at the crash is gone and the doc says so. The board's Ctrl-S screen is a
   thin client over the three verbs (`snapshots --json` lists; `s`, `d`,
   `r`), with a one-time hint on the board after a start when named rows
   are offline and a snapshot is under two days old. `snapshots` is a
   reserved name on both sides. Rejected: restoring from the records alone
   with no snapshot (the records do not know which tabs were open, and an
   offline record is the normal state of a parked task, not a crash);
   driving iTerm2's own window restoration (opaque, and it restores shells,
   not Claude conversations); a daemon that snapshots on a timer (nothing
   runs when the board is closed; the owner asked for "if requested").
3. **One log, always verbose, filtered on read.** Yes, this is solved:
   Go's `log/slog` (stdlib) writes leveled `key=value` text records to any
   writer and lets a handler rename levels, Python's `logging` does the same
   with a `RotatingFileHandler`, and a line with `level=` in it is what a
   reader filters on. So: one file, `$SAVANNAH_LOG_DIR/savannah.log`
   (default `~/.claude/savannah/log/`), every piece appends in slog's text
   form with `src=dash|work|hook|shim`, a VERBOSE level below DEBUG (`-8`
   in slog, `5` in Python; Python's WARNING is renamed WARN so the stamps
   agree), 10 MiB rotation with three kept on both sides, and the Go writer
   reopens when the file under its path was swapped. Readers: `work log
   [--level normal|debug|verbose] [--lines N] [--follow]` and the board's
   Ctrl-L view (`n`/`d`/`v`, following the end). What goes where: INFO is
   what you did and what came of it (every board action, every CLI verb and
   its refusal), DEBUG is every subprocess with argv, exit and timing plus
   every change of the row set, VERBOSE is payloads and the periodic polls
   (titles every 5 s, mesh task lists, the output view) so debug stays
   readable. No new dependency on either side. Rejected: one file per
   piece (the point is seeing the board's `say` next to the CLI's `it2`
   call next to the hook's event, in order); JSON lines (grep-able text was
   the ask, and slog's text form already quotes what needs quoting); a
   `--verbose` flag that changes what is written (the raw log is always
   complete, by the owner's rule).
4. Tests: `dash/logging_test.go` (level names, the filter, the tail read
   with a torn first line, rotation at the limit, the writer reopening
   after a swap, `openLog` output and `run`/`poll` levels, the Ctrl-L view
   through the model), `dash/snapshot_test.go` (argv, the screen through
   its keys and messages, the one-time hint), titles in `dash/board_test.go`
   and `dash/order_test.go` (nameFor, nameSuggestion, the WORK cell and
   panel, Ctrl-R editing a title, words in the dialog, `snapshots`
   reserved); `test/test_work.py` title, log and (`test/test_restore.py`)
   snapshot/restore sections; `test/test_hook.py` and `test/test_shim.py`
   for their logging and the new tools.

### 11/9/2026 (the board grouped by repository)

Fifth ask: "one of the columns savannah shows is the project, I would
like that organized a little better so I can see all ZFS Claude sessions".

1. **Group inside the sections, never instead of them.** Ctrl-G (and
   `savannah --group`, `--no-group`) gathers each section's rows by
   repository, repositories in the order their first row appears, the
   owner's rank order kept inside each group, a dim `  zfs (4)` header per
   group (with the full path on a wide board), and the PROJECT cell as the
   path inside the repository (`.`, `dash`, the worktree's name). The
   repository is read from the directory, never from a git subprocess:
   `<repo>/.savannah-worktrees/<name>` is that repo, else the nearest
   `.git` walking up, a `.git` file (`git worktree add`) followed to its
   main repository, a directory under no repository its own group; cached
   per path for the run. The choice is remembered in `$SAVANNAH_BOARD_FILE`
   (`~/.claude/savannah/board.json`), the board's first and only
   preference file (a UI preference, not session state, so the "dash owns
   no state" rule stands). Dragging is refused while grouped: a drag edits
   the rank order, and writing ranks in the grouped order would silently
   rewrite the owner's own order. Rejected: a third section per repo (the
   sections are the owner's priority, not the filesystem's); sorting by
   PROJECT (loses the rank order); a filter typed on the prompt line (the
   ask was to see them organised, not one at a time); `git rev-parse` per
   row (a subprocess per row per second, for an answer a stat gives).
2. Tests: `dash/group_test.go` (the root finder on temp directories with a
   `.git` dir, a `.git` file and none, the labels, the grouped order and
   its headers in the view, Ctrl-G with the cursor following and the file
   written or refused, the drag refusal, the flags, the help line that
   drops its last hint when narrow); `test/test_work.py` checks that
   `savannah --group` reaches the dash and `--help` does not.

### 11/9/2026, later (the tabs you opened by hand, named for you)

Sixth ask: "when savannah comes up, figure out a good name for each
Claude session by asking them; later I can change them if needed".

1. **Ask the transcript, not the tab.** Typing a question into a live tab
   would be a prompt to that session (or the answer to a permission
   prompt waiting there), would spend its context and its tokens, and
   would interrupt whatever it is doing. The tab's transcript is what the
   tab knows, so `work autoname` reads it: the human prompts (slash
   commands and interruption notices dropped) and the latest reply, and
   asks a headless Claude for a two-to-five word title over an excerpt
   (directory, first three prompts, the latest prompt, the head of the
   latest reply). The call is `claude -p --safe-mode
   --no-session-persistence --strict-mcp-config --tools "" --max-turns 1
   --output-format json --model haiku`, from the temp directory, with
   `SAVANNAH_HOOK_IGNORE=1` so the session hook writes nothing: safe mode
   turns hooks off (verified live: no session file), the env var is the
   guarantee that does not depend on that. Three seconds and about two
   hundredths of a cent per tab in the live check, four tabs at a time.
   The name is derived from the title the way the board derives one from
   words, `-2` when taken, and the tab is adopted with the title
   (`adopt_session`, the shared half of `adopt`). The fallback is the first
   prompt's first line, and the result says which (`how`, `claude_error`).
2. **The board asks once per tab, when it has something to go on.** On
   every sessions read the dash hands every plain row with a prompt that
   it has not submitted this run to one `work autoname --json <ids>`,
   reports the result on the status line, and re-reads. A tab with no
   prompt yet is submitted once it has one; a refused tab is not retried
   until the next start (no retry storm on a broken CLI). Off with
   `--no-autoname` or `"autoname": "off"` in the board file (hand-written,
   nothing on the board sets it; the board file's writers load before they
   save so Ctrl-G never wipes it). Rejected: naming at the first prompt
   from the hook (one prompt is a thin excerpt, and the hook stays a
   recorder); a name typed back into the tab's title (iTerm2's auto-title
   beats set-name, found 11/5); asking the live session (above); a
   confirmation dialog per name (the ask was names on arrival, with
   Ctrl-R and `rename` as the correction).
3. Tests: `test/test_work.py` autoname section (a fake `claude` that records
   its argv and environment: safe mode, no tools, haiku, the excerpt as the
   prompt, the hook told to stand down; prompts and excerpt from a
   transcript and from the hook alone; title cleaning and slugs; dry run;
   unique names for one title; the id filter; already named and unknown
   ids skipped; the fallback on a failing or missing claude; `--no-claude`);
   `test/test_hook.py` (`SAVANNAH_HOOK_IGNORE` writes nothing);
   `dash/autoname_test.go` (which tabs are pending and once only, the
   sessions tick starting a run, the status lines, the flags, the board
   file keeping both keys).

### 11/10/2026 (idle is green, working is yellow)

Seventh ask: "all the sessions savannah is monitoring, when idle, are
yellow; that looks odd. I want them green, and if they are working they
should be yellow."

1. **The cause was the hook, not the palette.** Claude Code sends a
   Notification of type `idle_prompt` once a tab has sat unused for a
   minute, and the hook mapped every Notification to `waiting`: so every
   idle tab turned into one that "needs you" after sixty seconds, yellow,
   counted in the header. The nudge now refreshes the timestamp and leaves
   the state alone (its type is recorded, no message); a session with no
   file yet is `idle`. The board reads files an older hook stamped
   `waiting` for the nudge as `idle` (`normalizeSession`), so nothing has
   to be cleaned up by hand.
2. **A traffic light.** With the nudge out of the way the colours follow
   the states the way the owner asked: red for `waiting` (a permission
   prompt or a question; the "N need you" count and a pending mesh
   decision are red too), yellow for `working` (a polling row is a working
   one), green for `idle` and `done`, faint for `offline`. One function,
   `rowStyle`, so the rule lives in one place. Status-line warnings stay
   yellow: they are refusals and errors, not rows.
3. **Tests.** `test/test_hook.py` (the nudge keeps `done`, records its
   type and no message, refreshes the timestamp; first-seen is `idle`);
   `dash/sessions_test.go` (`normalizeSession` turns the legacy file into
   idle and keeps a permission prompt waiting; `TestRowStyle` pins the
   colour per state).

### 11/10/2026, later (install.sh)

Eighth ask: "on my other system, what do I do, pull and then how do I
build; can we create an install script to make this easier".

1. **One script, every step idempotent.** `install.sh` is the README's
   build section as a program: tool check, song sibling (clone when
   missing, build when `libsong.a`/`songc` are missing or after `--pull`),
   cmake configure and build, the dash target when go exists, the hook
   installer, a symlink into `~/.local/bin`. Running it again after a pull
   is the upgrade path; `--pull` does the pull too, fast-forward only, so
   a diverged checkout stops rather than merges.
2. **Dry run as the test seam.** `--dry-run` prints each command as
   `+ cmd` and runs none; `test/test_install.py` drives that against temp
   song directories (built, missing) and checks the commands and their
   order. The real steps are cmake, git and the hook installer, each
   tested elsewhere; what the script adds is the sequencing, and that is
   what the test pins.
3. **Stated limits.** It installs none of the tools (brew or apt do), does
   not configure the mesh, and cannot restart open Claude Code tabs. Bash,
   not Python, because it is the thing that runs before anything else is
   known to work; bash 3.2 (the Mac's) is enough.

### 11/10/2026, later still (the owner's checkouts as a tree)

Ninth ask, with a screenshot of thirty tabs: the owner's repository work
happens in checkouts named `<repo>-<TICKET>` beside the repository
(`~/ng/delphix/zfs-DLPX-98816` next to `~/ng/delphix/zfs`, always git
worktrees, always that pattern), and the board should show zfs /
zfs-DLPX-12345 / zfs-DLPX-6789, and name the tabs that way.

1. **The checkout is a first-class fact.** `repoInfo` gained `checkout`,
   the directory holding the row's `.git`. A git worktree already grouped
   under its main repository (the `.git` file); a clone named `<X>-<ID>`
   beside a repository `<X>` now does too (`siblingMain`, the longest
   sibling so `dlpx-qa-gate-DP-2702` is not `dlpx`'s), and a dash in a
   name alone proves nothing (`dp2817-watch` stays its own group). Both
   rules read the filesystem, never git.
2. **The tree is sorting plus the cell, not more header lines.** Inside a
   group the main checkout leads and the other checkouts follow by name,
   rank order kept inside each; the PROJECT cell is the checkout's name
   (`.` for the main one) plus the path inside it. Sub-headers per
   checkout were considered and dropped: with thirty rows and a dozen
   checkouts they would cost a screen, and the sorted cell reads as the
   tree the owner drew. The rank order now holds only inside one checkout
   while grouped, stated in the README.
3. **Names from the checkout.** `work autoname` names a tab after its
   checkout when `checkout_name` finds one (`zfs-dlpx-98816`, `-2` for a
   second tab there), else from the title as before. The title stays the
   model's, so WORK still says what the tab is doing while the name says
   where. Names are lowercase by the existing rule; the ticket keeps its
   case in the path and the title.
4. **Tests.** `dash/group_test.go` (`findRepoRoot` with a worktree, a
   sibling clone, the longest sibling, a lone dash; `inRepoLabel` on a
   real layout; `TestGroupOrdersByCheckout` for the order and the view);
   `test/test_work.py` (`checkout_name` on every shape; a dry run naming
   two worktree tabs `zfs-dlpx-98816` and `-2`, a clone `zfs-cp-11610`, the
   main checkout by its title).
