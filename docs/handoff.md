# Session handoff: a human in the loop over the mesh

An unattended worker (a CI job, an overnight task) reaches a point where it
needs a human decision. This document defines how it asks, how a person on
another machine answers, and how the worker resumes. It is the first cut of
the "session handoff" design brief (decision log, 9/17/2026); the brief's
design (B), decision delegation, built on primitives savannah already had.

## The idea

There is no new wire surface, no new daemon state, and no session migration.
The task engine already models a paused session: a task between turns is a
session file plus a row in savannahd's table, with no live process, no held
connection, and no pinned CI executor. So:

- A worker that needs a human ENDS ITS TURN, with a marker as the last line
  of its final message.
- A pending decision IS a task whose state is `idle` (or `incomplete`) and
  whose last output line carries the marker. The decision queue is the task
  table; the marker rides `last_line` through `task ls --json` untouched.
- Answering IS `task_send`. The next turn resumes the same session with
  `--resume`, and its output replaces `last_line`, which clears the flag.
  Nothing to garbage-collect.

Why a turn boundary and not a blocking mesh call: song streaming budgets a
per-chunk timeout sized to an agent (minutes, not hours; finding 14), and a
node holding a stream open is single-flight busy for everyone else. A human
decision can take hours. Turn boundaries cost nothing while they wait.

## The marker

The worker's final message must end with one line of this form:

```
DECISION_NEEDED {"question":"promote 4812 to staging?","options":["promote","hold"],"context":"3 flaky tests quarantined"}
```

- `DECISION_NEEDED`, a space, then one JSON object on the same line.
- `question` (string) is required in spirit; `options` (array of strings)
  and `context` (string) are optional.
- Only the LAST non-empty line of the final message counts. A marker
  followed by more prose is not a pending decision.
- Malformed JSON after the marker still flags the task (the raw text stands
  in for the question). A worker that fumbles the JSON still gets a human's
  eyes, which is the point.

The canonical instruction block to paste into an unattended worker's prompt:

> If you reach a point where you need a human decision you cannot make
> yourself, end your reply with a single final line of exactly this form:
> `DECISION_NEEDED {"question":"...","options":["..."],"context":"..."}`
> and stop. Do not invent an answer. When a later message answers, act on
> it and continue.

## The human side

`savannah-dash` is the inbox. The board header counts pending decisions,
pending rows highlight with the parsed question in the LAST LINE column, and
the full-screen view shows a banner with the question and options above the
prompt line. Type the answer, press Enter, done. The CLI works too:

```bash
savannah task ls   <node> --key mesh.key            # marker visible per task
savannah task send <node> <id> "promote" --key mesh.key
savannah task tail <node> <id> --key mesh.key       # watch the resumed turn
```

A conductor Claude sees the same thing through the shim's `task_list` and
answers with `task_send`. Nothing about the pattern requires a person on the
answering end, only something with better judgment or authority than the
blocked worker.

## The CI side

Run the pipeline step as a task on the build machine's own savannahd (local
`--tcp` binds loopback only) instead of invoking `claude -p` directly:

```bash
savannah task new ci-node --addr 127.0.0.1:8790 --title "release gate" \
    --prompt "<the job, plus the instruction block above>"
```

Then poll until the state leaves `running` and branch on the marker.
Illustrative (untested) wrapper shape, with the exit-code convention
0 = done, 3 = decision pending, 1 = failed:

```bash
state_json() { savannah task ls ci-node --addr 127.0.0.1:8790 --json; }
until state_json | grep -qv '"state":"running"'; do sleep 10; done
state_json | grep -q 'DECISION_NEEDED' && exit 3
state_json | grep -q '"state":"idle"' && exit 0
exit 1
```

The Jenkins job can exit at code 3 and be re-triggered after the answer, or
keep polling if executor time is cheap. savannahd holds the paused session
either way; it outlives the job's process.

## Crossing networks

mDNS is LAN-scoped. When the CI box and the laptop cannot see each other's
multicast (different VLANs, a VPC, a locked-down host), two options, both
proven:

- `--addr host:port` on every CLI/dash command pins the target explicitly
  and skips discovery.
- An ssh tunnel carries the hop where only ssh is allowed:
  `ssh -f -N -L 8799:127.0.0.1:<port> ci-box`, then `--addr 127.0.0.1:8799`.
  This is also the workaround when macOS Local Network privacy blocks
  third-party binaries from LAN unicast (platform ssh is exempt).

## Security posture

- The mesh key (HMAC-SHA256) authenticates machines and protects message
  integrity. It does NOT encrypt. Decision payloads cross in cleartext, so
  scope them: the question and the minimum context, never raw file contents
  or environment data. For anything beyond a trusted LAN, put the hop inside
  a VPN or the ssh tunnel above.
- The answer executes on the CI box with the worker's tool permissions
  (`acceptEdits`). The key proves "holder of the mesh key", not which human.
  Whoever can reach the inbox holds that authority; treat answers as an
  instruction channel and keep the key tight.

## Stated limitations

- The marker is a convention with the model, not an enforced protocol. A
  worker that forgets to emit it just sits idle, unflagged; the human finds
  it by tailing, as before. Test coverage pins the plumbing (a marker line
  survives to `last_line` verbatim; see test_task_flight), not the model's
  compliance.
- One pending decision per worker at a time, by construction (one last
  line).
- The dash polls once a second; there is no push and no notification besides
  the board itself. Push lands with the song property fan-out upgrade.
- No per-human identity on answers, and no audit trail beyond the task
  transcript.
- `savannah ls` still needs mDNS; without it, every command needs `--addr`.
  A static peer registry is the known next step.
