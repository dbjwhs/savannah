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

**See it as pictures:** [How savannah works](https://dbjwhs.github.io/savannah/how-savannah-works.html)
is a one-page visual tour (the board on one machine, the mesh over song, and
what running across machines still needs). The page is
`how-savannah-works.html` in this repository, served by GitHub Pages.

`DESIGN.md` is the source of truth for architecture and rationale.
`CLAUDE.md` holds the working conventions and the pinned wire contracts.

## What works today

- **Solo node** (Phase 1): `savannah ask local "..."` spawns `savannahd`
  over local pipes, which forks the agent, maps its stream-json output to
  typed chunks (TEXT, TOOL_EVENT, RESULT), and streams them back. Timeout,
  cancel, crash synthesis, and cost reporting per ask.
- **Streaming hardening** (Phase 2, core cases): 4 MiB text blocks survive
  intact (split into 512 KiB chunks, a contract dating from song's old 1 MB
  string cap), UTF-8
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
- **Local work sessions and the board** (Phases 5d and 5f): `savannah` with
  no arguments is a board of your own iTerm2 Claude Code tabs, one named tab
  per work item with a worktree shell below, driven through iTerm2's own
  `it2` tool; new, open, say, output, note and rm are keys. No tokens, no
  daemon; see "Work sessions (local)" below.
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

- The shim wraps the CLI rather than speaking song natively. song's
  Python runtime has had streaming and HMAC since 11/13/2026 but still has
  no mDNS discovery, so generated Python bindings remain the v2 path.
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
(or pass `-DSONG_ROOT=/path/to/song`). The board needs Go; the work
sessions need iTerm2 3.5+ on a Mac.

The short way, on a fresh machine or after every pull:

```bash
./install.sh            # song sibling, CLI, board, hook, ~/.local/bin/savannah
./install.sh --pull     # git pull savannah and song first, then the same
./install.sh --test     # ... and run ctest
./install.sh --dry-run  # print every step, run nothing
```

It clones song beside this checkout when it is missing, builds it when
`libsong.a` or `songc` is missing (or after `--pull`), configures and
builds savannah, builds `dash/savannah-dash` when `go` is on PATH, merges
the session hook into `~/.claude/settings.json` (`hooks/install.py`,
idempotent, backs the file up) and links `build/savannah` into
`~/.local/bin` (`--bin DIR` for elsewhere; it tells you if that directory
is not on your PATH). Every step is safe to repeat. It does not install
git, cmake, a compiler, Go, Python or iTerm2, does not configure the mesh
(`savannahd`, keys, mDNS), and does not restart Claude Code tabs that are
already open. `--no-hook`, `--no-link`, `--release`, `--song DIR`.

By hand:

```bash
git clone https://github.com/dbjwhs/song ../song
cmake -S ../song -B ../song/build && cmake --build ../song/build --target song songc
cmake -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j
cmake --build build --target dash
python3 hooks/install.py
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

Not part of the mesh. This is the uber terminal: one board for every task
you have open.

- **A work session** is one named iTerm2 tab per work item: your ordinary
  interactive `claude` in a git worktree in the upper pane, a shell cd'd
  into the same worktree below, and a plain-text notes file.
- **The board** (bare `savannah`) lists them all with the state of each
  Claude.
- **No savannahd, no song, no tokens spent by the tool.** The brain in the
  upper pane is your own interactive Claude Code, not a headless worker.
- `work/savannah_work.py` (stdlib Python, Phase 5d) owns the records and
  drives iTerm2 through iTerm2's own `it2` command; the dash in work mode
  is the board (Phase 5f).

Details and the storage layout are in `work/README.md` and `dash/README.md`.

### Commands

`build/savannah` on your PATH is assumed.

| Command | What it does |
|---------|--------------|
| `savannah` | The board: every work session and its state. Ctrl-N makes a new one. |
| `savannah work repos ~/ng/dbjwhs` | Once: the folder of repos the new-session dialog offers. |
| `savannah work new fix-parser --repo ~/ng/dbjwhs/savannah --prompt "look at src/json.cpp"` | Worktree, named tab and record. |
| `savannah work note fix-parser "flaky on nested arrays"` | Append a note. |
| `savannah work say fix-parser "run ctest -R test_json and summarize"` | Type into the Claude pane. |
| `savannah work output fix-parser --last 2` | What that Claude said: the last two turns, prompt and reply. |
| `savannah work recap` | Back after a few days: where each session in NOW stands, read from disk (Ctrl-Q on the board). `--section now,active` picks sections; `--ask` asks them for fresh summaries first. |
| `savannah work ask --section now -q "rebase on main when you are idle"` | One line typed into every session of a section that is safe to interrupt, the replies collected (`--all`: every tab; Ctrl-A on the board). |
| `savannah work open fix-parser` | Focus the tab, or make it again (`claude --continue`) after an iTerm2 restart. |
| `savannah work adopt old-tab --session <claude session id>` | A tab you opened by hand becomes a work session (the board does this from Ctrl-R or Ctrl-T). |
| `savannah work move fix-parser --later` | The board's LATER section (Shift-Down on the board); `--active` and `--rank N` order it. |
| `savannah work rename fix-parser parser` | A new name; the worktree and branch keep theirs. |
| `savannah work title parser -- "Parser: nested arrays"` | Free text the board shows instead of the name (Ctrl-R on the board); `--clear` drops it. |
| `savannah work status fix-parser -- "waiting for ctest (run 2 of 3)"` | From inside the tab: the board shows that row as polling. |
| `savannah work ls` | NAME, TITLE, BRANCH, STATE, STATUS, LAST PROMPT, LAST NOTE. |
| `savannah work snapshot --note "before reboot"` | Every session's tab, worktree and Claude conversation id to a file (Ctrl-S on the board). |
| `savannah work restore` | After the relaunch: the missing tabs back, each Claude resumed (`--dry-run` shows the plan). |
| `savannah work recover` | After a reboot or a crash nobody planned for: every Claude that went down is started again on its own conversation, no snapshot needed (`--dry-run` shows the plan; Ctrl-S then v on the board). |
| `savannah work log --level debug` | Savannah's log at normal, debug or verbose (Ctrl-L on the board); `--follow` tails it. |
| `savannah mesh` | The mesh board (workers on every node), what a bare `savannah` used to be. |

The rest of the set: `notes` (print the notes), `sh` (type into the shell
pane), `jump` (focus a pane) and `rm` (close the tab, remove the worktree,
archive the notes; the branch stays, it is your work).

### What `new` sets up

- **A worktree**: `<repo>/.savannah-worktrees/<name>` on branch
  `work/<name>`. `--branch B` checks out an existing local branch instead,
  and `--no-worktree` runs in the repo itself.
- **A split tab** in your current iTerm2 window, with the session named
  after the work item (`claude -n <name>`, so the tab title and `/resume`
  agree).
- **A record**: `~/.claude/savannah/work/<name>/record.json`.

Worth knowing:

- `--repo` may point anywhere inside the repository; it is resolved to the
  main worktree root.
- A `.worktreeinclude` file in the repository lists the gitignored files a
  fresh worktree needs (`.env`, local config); `new` copies them in.
- `new` of a name whose `work/<name>` branch survived an earlier `rm`
  reattaches to that branch and says so.
- `open` on a tab that is gone makes it again, resuming that worktree's
  conversation with `claude --continue` when one exists.

### The board

`savannah` (the dash in work mode) shows each work session with the state
of its Claude: idle, working, waiting, done or offline. A row is joined to
its live Claude Code session through the hook files (`hooks/`) by worktree
path at read time.

| Key | What it does |
|-----|--------------|
| Enter | Open or focus the row's tab. With text typed, send that text to its Claude pane. |
| Ctrl-N | The new-session dialog: name, repo from the remembered folder, prompt, worktree or not, its own window or not. Mode `review` opens a code review instead (see below). |
| Ctrl-O | Show what that Claude said. |
| Ctrl-Q | Recap the row's section on one screen: each session's summary, last note, your last prompt and the end of its last reply. Free: nothing is typed and no Claude is called. On that screen `1` `2` `3` show NOW, ACTIVE or both, and `a` asks those sessions for fresh summaries. |
| Ctrl-A twice | Ask every idle or done tab what it is doing, and keep each reply as that session's summary. |
| Ctrl-T | Take a note. |
| Ctrl-R | Give it a title: free text, capitals and spaces, shown in place of the name. The name stays the identifier; `work rename` changes that. |
| Ctrl-X twice | Remove it. |
| Ctrl-D | Move its tab into the board's window, or out into a window of its own (`work attach`, `work detach`). |
| Ctrl-Y | Mark an existing session as a review. |
| Shift-Up, Shift-Down | Move a session one row, or over the line between sections at the ends. |
| Ctrl-S | The snapshot and recover screen. |
| Ctrl-L | Read savannah's log. |

**Names.** Wherever a name is asked for you may type words: "Fix the
parser" becomes the title and `fix-the-parser` the name.

**Sections.** Rows sit in sections you order yourself: NOW, ACTIVE and
LATER. The order is kept in the records (`work move`), so the CLI and a
restarted board see the same.

**Reviews.** Mode `review` in the dialog opens a code review of a pull
request or a branch, checked out and handed to `/code-review`. Reviews sit
in their own REVIEWS section.

**Plain tabs.** Tabs you opened by hand are on the board too, at the bottom
of LATER with their iTerm2 tab title in parentheses. Ctrl-R on one names it
(`work adopt`: the tab becomes a work session, nothing new opens); Ctrl-T
names it and then takes a note.

**Polling.** A session's Claude can report what it is waiting on (`work
status <name> -- "waiting for ctest (run 2 of 3)"`; the SessionStart hook
tells it the command). While that turn is open the board shows the row as
polling, with the text, the poll count and its age, and counts such rows in
the header.

**Layout.** The board uses the whole terminal. From 120 columns the table
adds TAB and NOTE columns and shows full paths; the rest of the height is a
panel for the highlighted row, with its notes beside the end of its
Claude's last reply.

**Snapshot and restore.** Before a reboot, Ctrl-S saves every session's
tab, worktree and Claude conversation id (`work snapshot`). Once iTerm2 is
back it remakes the tabs that are gone, with each Claude resumed on its
conversation (`work restore`; a dry run first if you like).

**Recover.** A forced update, a crash or a dead battery does not wait for a
snapshot. Savannah's hook already records, for every open Claude Code tab,
which conversation it holds, where, in which iTerm2 pane and under which
process, and it removes that record only when the session ends properly. So
after the machine comes back, `savannah work recover` (or Ctrl-S then v;
the board says so on its status line when it starts and finds Claudes
gone) starts each one again with `claude --resume` on its own conversation:
in its own pane when iTerm2 brought the tab back, else in a pane sitting
idle in its directory, else in a new tab. `--dry-run` (c on the screen)
shows the plan and touches nothing. What it does not do: it never types
into a pane where something other than a shell runs, never touches a
session whose Claude runs, leaves a session last active over 7 days ago
alone unless you name it (`--within` changes that), and cannot bring back a
turn that was running when the machine went down. Whether iTerm2 keeps a
pane's id across a real reboot has not been confirmed here; when it does
not, the idle pane in the session's directory is used, and when there is
none you get a new tab beside the one iTerm2 restored.

Every key is a `savannah work ...` subprocess, so the board and the CLI
never disagree. The dash is a Go program: `cmake --build build --target
dash` (or `cd dash && go build -o savannah-dash .`) puts it where the CLI
looks.

### The log

Everything local logs to one file, `~/.claude/savannah/log/savannah.log`
(`$SAVANNAH_LOG_DIR`): the board, the work CLI, the hook and the shim.

- **Format**: one `key=value` line per record, with a `level=` stamp
  (VERBOSE, DEBUG, INFO, WARN, ERROR) and a `src=` (dash, work, hook, shim).
- **Reading it**: the file is always verbose and the readers filter:
  `savannah work log --level normal|debug|verbose` (`--follow` tails) and
  the board's Ctrl-L view.
- **Levels**: normal is what you did and what came of it, debug adds every
  subprocess and every it2 call with its timing, verbose adds payloads.
- **Rotation**: at 10 MiB, three old files kept.
- **No dependency added**: Go's log/slog on one side, Python's logging on
  the other.

### From a Claude: MCP tools and the hub

The same verbs are MCP tools in the shim, so a Claude Code session can open
a tab for you, type a line into it, and read the reply back:

`work_new`, `work_adopt`, `work_list`, `work_note`, `work_notes`,
`work_say`, `work_ask`, `work_recap`, `work_output`, `work_shell`,
`work_jump`, `work_open`,
`work_move`, `work_rename`, `work_title`, `work_status`, `work_rm`.

`savannah hub DIR` sets such a coordinating session up:

- the shim registered in `DIR/.mcp.json` with absolute paths;
- a starter `CLAUDE.md` from `work/hub_CLAUDE.md` that tells it to delegate
  every task to a tab;
- `DIR/.savannah-scratch/` for tasks that belong to no repo.

The hub is optional and no longer the front door; the board is.

### From a script

Your own scripts can open sessions too, review sessions included. There is
no server to talk to: the interface is the `work new` command, made safe to
call unattended.

```bash
savannah work new pr-2353 --repo ~/ng/zfs --mode review --target 2353 \
    --if-absent --background --json
```

- `--if-absent` makes the call repeatable: exit 0 with the session already
  there, matched by name or, for a review, by the same pull request or
  branch in the same repo.
- `--background` leaves the tab you are on selected.
- `--json` prints one JSON value whether it worked or not: the record, or
  `{"error", "code"}` on exit 1.

"Opening sessions from a script" in `work/README.md` has the details and a
`gh pr list` loop to start from.

### What does not work

**Platform**

- iTerm2 on macOS only, 3.5 or newer with its Python API enabled. `it2` is
  iTerm2's own tool; the Terminal seam exists, a tmux implementation does
  not.
- `it2` costs 200 to 500 ms per call, so a new session takes about two
  seconds to appear.
- STATE, LAST PROMPT and the join need the hook installed (`python3
  hooks/install.py`); without it every session reads offline.

**Talking to a tab**

- Typing into a pane is the only input channel into an interactive Claude
  Code session, and it is blind: `say` presses Enter and never checks that
  Claude was ready (it does refuse empty text, exit 2).
- `output` is the read side, but it reads the transcript file Claude Code
  writes, not the pane. The subset of that file it relies on is pinned by a
  test fixture, not by any published contract, and a reply is only final
  once `ls` reports the tab done or waiting.
- Nothing watches the panes: `ls` reports whether the Claude pane still
  exists and `open` brings a gone tab back, but a tab closed by hand stays
  an offline row until you do.

**Worktrees and branches**

- `rm` refuses a dirty worktree without `--force`, and `--force` discards
  uncommitted changes there.
- `rm` refuses when another work session's worktree lies inside the one it
  is about to remove (`--keep-worktree` skips both the removal and that
  check).
- `--branch B` must already exist locally (no DWIM from remotes), and
  cannot be combined with `--no-worktree`.
- `.savannah-worktrees/` lives inside your repo and shows up as untracked
  unless you exclude it.

**Snapshots**

- A snapshot is the layout and the conversation ids, not the terminals'
  contents: a turn that was running when the machine went down is lost.
- `restore` never closes anything (tabs iTerm2 restored on its own sit next
  to the remade ones), and plain tabs come back as single panes.

**Log and tests**

- The log is best effort: a directory that cannot be written means no log,
  not no board, and two writers rotating in the same instant can lose a few
  lines.
- Tests drive a fake `it2` and a throwaway git repo; the real `it2` is
  exercised by hand on a Mac, not in CI.

## Layout

See the table in `CLAUDE.md`. Short version: `idl/` is the contract,
`src/` is the daemon and its hand-written TOML/JSON/stream-json plumbing
(zero dependencies beyond libsong and POSIX, on purpose), `cli/` is the
human remote control, `tools/fake_claude.cpp` is the test agent, `test/`
is the definition of done.

## License

MIT. See `LICENSE`.
