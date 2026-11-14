#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# peer - the savannah MCP shim. Gives stock Claude Code these tools:
#
#   list_peers()                    -> nodes on the mesh (mDNS browse)
#   ask_peer(name, prompt, ...)     -> one stateless ask, streamed remotely
#   peer_status(name)               -> "idle" or "busy"
#   task_new(name, title, ...)      -> create a persistent task worker
#   task_list(name)                 -> workers on a node
#   task_status(name, id)           -> snapshot one worker
#   task_send(name, id, prompt)     -> drive one more turn
#   task_output(name, id)           -> tail a worker (replay + follow)
#   task_cancel(name, id)           -> kill a worker, prune its worktree
#   task_rm(name, id)               -> forget a finished worker (cancel first if running)
#   work_new(name, ...)             -> open a LOCAL interactive work tab (Phase 5d)
#   work_list()                     -> local work sessions joined with live hook state
#   work_note(name, text)           -> append a plain-text note for the human
#   work_notes(name)                -> read those notes
#   work_say(name, text)            -> type into that tab's interactive Claude pane
#   work_output(name, last?)        -> the last N turns (prompt + reply) of that tab
#   work_ask(question?, names?, all?, timeout?) -> one question typed into every tab that is
#                                      idle or done, the replies collected (one turn per tab)
#   work_shell(name, cmd)           -> type into that tab's shell pane
#   work_jump(name, shell?)         -> focus the tab
#   work_open(name, shell?)         -> focus the tab, or reopen it when it is gone
#   work_adopt(name, session_id, note?, title?) -> take a tab the human opened into a named session
#   work_move(name, priority?, rank?) -> the board section (now/active/later) and order of a session
#   work_rename(old, new)           -> a new record name (worktree, branch and tab keep theirs)
#   work_status(name, text?, clear?) -> read, report or clear what that tab's Claude is polling
#   work_title(name, text?, clear?)  -> read, set or clear the free-text title the board shows
#   work_rm(name, ...)              -> close tab, remove worktree, archive notes
#
# ask_peer is one-shot; the task_* tools drive persistent, resumable worker
# sessions on a peer node (Phase 5a), so a master session can orchestrate many.
# The work_* tools are different in kind: they manage the OWNER's own
# interactive Claude Code tabs in iTerm2 on THIS machine (work/savannah_work.py,
# a file-owned local object; no savannahd, no mesh, no tokens spent by the
# tool itself). Typing into a pane is how the hub talks to an interactive
# session; work_output is the read side (it parses the transcript Claude Code
# writes), work_list the state (working/idle/done/waiting/offline).
#
# Implementation: a Model Context Protocol stdio server in pure stdlib
# Python that shells out to the savannah CLI, which carries the tested
# C++ paths for discovery, HMAC, and streaming. Generated song Python
# bindings are the v2 path: song's Python runtime has streaming and HMAC
# (song main d4387e8, 87e37cb) but no mDNS discovery yet.
#
# Config (environment):
#   SAVANNAH_BIN             savannah CLI (default: "savannah" on PATH)
#   SAVANNAH_KEY             HMAC key file passed as --key (optional)
#   SAVANNAH_ASK_TIMEOUT_MS  per-ask agent timeout (default 300000)
#   SAVANNAH_WORK_BIN        work-session CLI argv (default: the interpreter
#                            running this shim, sys.executable, on
#                            <real dir of peer.py>/../work/savannah_work.py);
#                            that module honors SAVANNAH_WORK_DIR,
#                            SAVANNAH_SESSIONS_DIR and SAVANNAH_IT2
#
# Wire: newline-delimited JSON-RPC 2.0 on stdin/stdout per the MCP stdio
# transport. Logs go to stderr; stdout carries protocol frames only.
#
# The shim also writes savannah's one log (work/savannah_log.py beside this
# checkout's work/, src=shim; $SAVANNAH_LOG_DIR, default
# ~/.claude/savannah/log/savannah.log): an INFO line per tool call (tool,
# argument keys, the prompt's head), DEBUG when it ends, WARN when it fails;
# `savannah work log` reads them. Optional: a shim copied out of the
# checkout, or an unwritable log directory, logs nothing and says nothing.

import json
import os
import shlex
import subprocess
import sys
import time

try:
    sys.path.insert(0, os.path.normpath(os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "..", "work")))
    import savannah_log
    savannah_log.configure("shim")
    log_debug, log_info, log_warn = (savannah_log.log_debug, savannah_log.log_info,
                                     savannah_log.log_warn)
except Exception:  # noqa: BLE001 - no log is fine, a dead shim is not
    def log_debug(_msg, **_kv):
        pass

    def log_info(_msg, **_kv):
        pass

    def log_warn(_msg, **_kv):
        pass

LOG_PROMPT_CLIP = 120   # chars of a prompt/text argument kept in the tool's INFO line

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "peer", "version": "0.1.0"}

ASK_TIMEOUT_MS = int(os.environ.get("SAVANNAH_ASK_TIMEOUT_MS", "300000"))
# Margin over the agent timeout so the CLI itself is what times out.
SUBPROCESS_MARGIN_S = 30
WORK_PRIORITIES = ("now", "active", "later")   # the board's sections (work/savannah_work.py PRIORITIES)


def savannah_argv():
    return shlex.split(os.environ.get("SAVANNAH_BIN", "savannah"))


def key_args():
    key = os.environ.get("SAVANNAH_KEY", "")
    return ["--key", key] if key else []


def run_cli(args, timeout_s):
    argv = savannah_argv() + args
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"savannah CLI not found ({argv[0]!r}); set SAVANNAH_BIN"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"savannah CLI timed out after {timeout_s}s: {' '.join(args)}"
        )


def parse_result_trailer(stderr_text):
    # Return the LAST result trailer: a task_output that auto-continued past
    # the max-turns cap emits one trailer per invocation, and the final one is
    # the outcome that matters (an early "error_max_turns" is not the verdict).
    result = None
    for line in stderr_text.splitlines():
        if line.startswith("[result] "):
            try:
                result = json.loads(line[len("[result] "):])
            except json.JSONDecodeError:
                result = {"raw": line}
    return result


def text_block(text):
    return {"type": "text", "text": text}


def ok_text(proc, empty="ok"):
    """A successful tool result: the command's stdout, stripped, or `empty`
    when it printed nothing."""
    return [text_block(proc.stdout.strip() or empty)], False


def cli_ok(args, fail, timeout_s=30):
    """run_cli, with a non-zero exit raised as the tool error (the CLI's
    stderr, else `fail`). -> the finished process."""
    proc = run_cli(args, timeout_s=timeout_s)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or fail)
    return proc


def stream_result(proc, fail):
    """What a streamed command (ask, task tail) returns: its stdout, then the
    result trailer from stderr; a failure that printed neither carries the
    CLI's stderr, else `fail`."""
    trailer = parse_result_trailer(proc.stderr)
    blocks = []
    if proc.stdout:
        blocks.append(text_block(proc.stdout))
    if trailer is not None:
        blocks.append(text_block(
            "result: " + json.dumps(trailer, separators=(",", ":"))))
    is_error = proc.returncode != 0
    if is_error and not blocks:
        blocks.append(text_block(proc.stderr.strip() or fail))
    return blocks, is_error


def int_arg(args, key, message, minimum=None):
    """An optional integer argument: None when absent, else the int (never a
    bool, never below `minimum`), or a tool error saying `message`."""
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or (minimum is not None and value < minimum):
        raise RuntimeError(message)
    return value


def tool_list_peers(_args):
    proc = cli_ok(["ls"], "savannah ls failed")
    peers = []
    for line in proc.stdout.splitlines():
        parts = line.strip().rsplit(" ", 1)
        if len(parts) != 2 or ":" not in parts[1]:
            continue
        host, _, port = parts[1].rpartition(":")
        peers.append({"name": parts[0], "host": host, "port": int(port)})
    if not peers:
        note = proc.stderr.strip()
        return [text_block(note or "no peers on the mesh")], False
    return [text_block(json.dumps(peers, indent=2))], False


def tool_ask_peer(args):
    name = args["name"]
    prompt = args["prompt"]
    timeout_ms = int(args.get("timeout_ms", ASK_TIMEOUT_MS))
    cli = ["ask", name, prompt, "--timeout-ms", str(timeout_ms)] + key_args()
    proc = run_cli(cli, timeout_s=timeout_ms / 1000 + SUBPROCESS_MARGIN_S)
    return stream_result(proc, f"ask {name} failed")


def tool_peer_status(args):
    return ok_text(cli_ok(["status", args["name"]] + key_args(), "status failed"), "")


def tool_task_new(args):
    cli = ["task", "new", args["name"], "--title", args["title"]]
    if args.get("prompt"):
        cli += ["--prompt", args["prompt"]]
    if args.get("worktree"):
        cli += ["--worktree"]
    if args.get("workdir"):
        cli += ["--workdir", args["workdir"]]
    if args.get("timeout_ms"):
        cli += ["--timeout-ms", str(int(args["timeout_ms"]))]
    return ok_text(cli_ok(cli + key_args(), "task new failed"), "")


def tool_task_list(args):
    proc = cli_ok(["task", "ls", args["name"]] + key_args(), "task ls failed")
    return ok_text(proc, proc.stderr.strip() or "(no tasks)")


def tool_task_status(args):
    return ok_text(cli_ok(["task", "status", args["name"], args["id"]] + key_args(),
                          "task status failed"), "")


def task_verb(verb, *extra):
    """The handler for `task <verb> <node> <id> [args[key] for key in extra]`:
    the CLI's own message is the result and its exit code the error flag
    (a refused send or an unknown id is an answer, not an exception)."""
    def handler(args):
        cli = ["task", verb, args["name"], args["id"]] + [args[key] for key in extra]
        proc = run_cli(cli + key_args(), timeout_s=30)
        msg = proc.stdout.strip() or proc.stderr.strip()
        return [text_block(msg or "")], proc.returncode != 0
    return handler


tool_task_send = task_verb("send", "prompt")
tool_task_cancel = task_verb("cancel")
tool_task_rm = task_verb("rm")


def tool_task_output(args):
    timeout_ms = int(args.get("timeout_ms", ASK_TIMEOUT_MS))
    cli = ["task", "tail", args["name"], args["id"],
           "--timeout-ms", str(timeout_ms)] + key_args()
    proc = run_cli(cli, timeout_s=timeout_ms / 1000 + SUBPROCESS_MARGIN_S)
    return stream_result(proc, f"tail {args['id']} failed")


# ---- local work sessions (Phase 5d) ----
#
# These shell out to work/savannah_work.py, not to the savannah CLI. Exit 0 is
# success, 1 a user error (unknown name, git refused, pane gone), 2 bad usage;
# anything non-zero surfaces as a tool error carrying the module's one-line
# stderr message.


def work_argv():
    override = os.environ.get("SAVANNAH_WORK_BIN", "")
    if override:
        return shlex.split(override)
    # Same interpreter that runs the shim (not whatever "python3" is on PATH)
    # and the real location of this file (a symlinked shim still finds its
    # sibling work/ module).
    here = os.path.dirname(os.path.realpath(__file__))
    return [sys.executable,
            os.path.normpath(os.path.join(here, "..", "work", "savannah_work.py"))]


def run_work(args, stdin_text=None, timeout_s=120):
    argv = work_argv() + args
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            input=stdin_text)
    except FileNotFoundError:
        raise RuntimeError(
            f"work-session CLI not found ({argv[0]!r}); set SAVANNAH_WORK_BIN")
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"work-session CLI timed out after {timeout_s}s: {' '.join(args)}")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip()
                           or f"work {' '.join(args[:1])} failed")
    return proc


def work_name(args):
    name = args.get("name", "")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError("work session name is required")
    return name.strip()


def work_text(args, key, label):
    """A non-empty string argument, or a tool error.

    Never str()'d: a null, number or object is a caller bug, and typing its
    repr into a live pane (or pressing bare Enter for an empty string) is
    not what anyone meant.
    """
    text = args.get(key)
    if not isinstance(text, str):
        raise RuntimeError(f"{label} must be a string")
    if not text.strip():
        raise RuntimeError(f"{label} is empty")
    return text


def work_opt(args, key, flag):
    """[--flag=VALUE] as ONE token, so a value starting with '-' is a value."""
    value = args.get(key)
    if value is None or value == "":
        return []
    if not isinstance(value, str):
        raise RuntimeError(f"{key} must be a string")
    return [f"{flag}={value}"]


def tool_work_new(args):
    cli = ["new", work_name(args), "--json"]
    cli += work_opt(args, "repo", "--repo")
    cli += work_opt(args, "branch", "--branch")
    if args.get("no_worktree"):
        cli += ["--no-worktree"]
    cli += work_opt(args, "prompt", "--prompt")
    cli += work_opt(args, "note", "--note")
    cli += work_opt(args, "title", "--title")
    return ok_text(run_work(cli), "")


def tool_work_list(_args):
    return ok_text(run_work(["ls", "--json"]), "[]")


def tool_work_note(args):
    text = work_text(args, "text", "note text")
    # Text travels on stdin so multi-line notes survive intact.
    return ok_text(run_work(["note", work_name(args)], stdin_text=text))


def tool_work_notes(args):
    proc = run_work(["notes", work_name(args)])
    return [text_block(proc.stdout if proc.stdout.strip()
                       else "(no notes yet)")], False


def tool_work_say(args):
    # "--" ends option parsing so text starting with "-" is typed, not parsed
    # ("-h" used to come back as argparse help, reported as success).
    text = work_text(args, "text", "say text")
    return ok_text(run_work(["say", work_name(args), "--", text]))


def tool_work_ask(args):
    names = args.get("names") or []
    if not isinstance(names, list) or not all(isinstance(n, str) and n.strip() for n in names):
        raise RuntimeError("names must be a list of work session names")
    cli = ["ask"] + [n.strip() for n in names]
    if args.get("all", not names) is True or not names:
        cli.append("--all")
    question = args.get("question")
    if question is not None:
        if not isinstance(question, str) or not question.strip():
            raise RuntimeError("question must be a non-empty string")
        cli += ["-q", question]
    timeout = int_arg(args, "timeout", "timeout must be a positive integer (seconds)", minimum=1)
    if timeout is not None:
        cli += ["--timeout", str(timeout)]
    return ok_text(run_work(cli, timeout_s=(timeout or 120) + 30), "nothing to ask")


def tool_work_output(args):
    cli = ["output", work_name(args)]
    last = int_arg(args, "last", "last must be a positive integer", minimum=1)
    if last is not None:
        cli += ["--last", str(last)]
    proc = run_work(cli)
    return [text_block(proc.stdout.rstrip("\n") or "(no turns yet)")], False


def tool_work_shell(args):
    cmd = work_text(args, "cmd", "shell command")
    return ok_text(run_work(["sh", work_name(args), "--", cmd]))


def tool_work_jump(args):
    cli = ["jump", work_name(args)]
    if args.get("shell"):
        cli += ["--shell"]
    return ok_text(run_work(cli))


def tool_work_open(args):
    cli = ["open", work_name(args), "--json"]
    if args.get("shell"):
        cli += ["--shell"]
    return ok_text(run_work(cli))


def tool_work_adopt(args):
    session_id = work_text(args, "session_id", "session id").strip()
    # `=` form: a session id is never read as an option, whatever it looks like.
    cli = ["adopt", work_name(args), f"--session={session_id}", "--json"]
    cli += work_opt(args, "note", "--note")
    cli += work_opt(args, "title", "--title")
    return ok_text(run_work(cli), "")


def tool_work_move(args):
    cli = ["move", work_name(args), "--json"]
    priority = args.get("priority")
    if priority is None and args.get("rank") is None:
        raise RuntimeError("give priority ('now', 'active' or 'later'), rank, or both")
    if priority is not None:
        if priority not in WORK_PRIORITIES:
            raise RuntimeError("priority must be 'now', 'active' or 'later'")
        cli.append(f"--{priority}")
    rank = int_arg(args, "rank", "rank must be an integer")
    if rank is not None:
        cli.append(f"--rank={rank}")  # one token: a negative rank is a value
    return ok_text(run_work(cli), "")


def tool_work_rename(args):
    old = work_text(args, "old", "old name").strip()
    new = work_text(args, "new", "new name").strip()
    return ok_text(run_work(["rename", old, new, "--json"]), "")


def work_text_verb(verb, label):
    """The handler for a verb that reads, sets or clears one line of text on
    a session (status, title): `<verb> NAME --json`, plus `--clear`, or
    `-- TEXT` when text is given."""
    def handler(args):
        cli = [verb, work_name(args), "--json"]
        text = args.get("text")
        if args.get("clear"):
            if text not in (None, ""):
                raise RuntimeError("clear takes no text")
            cli.append("--clear")
        elif text is not None:
            cli += ["--", work_text(args, "text", label)]
        return ok_text(run_work(cli), "")
    return handler


tool_work_status = work_text_verb("status", "status text")
tool_work_title = work_text_verb("title", "title text")


def tool_work_rm(args):
    cli = ["rm", work_name(args)]
    if args.get("keep_worktree"):
        cli += ["--keep-worktree"]
    if args.get("force"):
        cli += ["--force"]
    return ok_text(run_work(cli))


# ---- the tool table ----
#
# One registration per tool: tool() appends the tools/list entry to TOOLS and
# files the handler under the same name in HANDLERS, so a tool cannot be
# listed without a handler or handled without a listing. The order here is
# the order tools/list reports.

TOOLS = []
HANDLERS = {}

# Schema properties that many tools share.
NODE_NAME = {"type": "string", "description": "Peer node name as shown by list_peers"}
TASK_ID = {"type": "string", "description": "Task id from task_new or task_list"}
WORK_NAME = {"type": "string", "description": "Work session name from work_list"}


def prop(kind, description, **extra):
    """One schema property: {"type", "description"} plus any extra keys."""
    return dict({"type": kind, "description": description}, **extra)


def tool(name, description, props, required, handler):
    """Register one tool: its tools/list entry and its handler. `required`
    is left out of the schema when empty."""
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = list(required)
    TOOLS.append({"name": name, "description": description, "inputSchema": schema})
    HANDLERS[name] = handler


tool(
    "list_peers",
    (
        "List Claude agent nodes discoverable on the local network "
        "mesh. Returns name, host, and port per node. Nodes run their "
        "own Claude with their own auth; asking one burns its tokens."
    ),
    {},
    [],
    tool_list_peers,
)
tool(
    "ask_peer",
    (
        "Ask a peer agent node one stateless question and stream back "
        "its answer. Every ask is a fresh context on the remote agent "
        "(no session memory). The result trailer reports cost, "
        "duration, and turns. Delegation guidance: good for work that "
        "benefits from the peer's machine (its files, its platform, "
        "its toolchain) or from a clean context."
    ),
    {
        "name": NODE_NAME,
        "prompt": prop("string", "The question or task for the peer"),
        "timeout_ms": prop("integer", "Per-ask agent timeout in milliseconds (default from "
                                      "SAVANNAH_ASK_TIMEOUT_MS)"),
    },
    ["name", "prompt"],
    tool_ask_peer,
)
tool(
    "peer_status",
    (
        "Report whether a peer node is idle or busy (v1 nodes are "
        "single-flight: a busy node rejects new asks immediately)."
    ),
    {
        "name": NODE_NAME,
    },
    ["name"],
    tool_peer_status,
)
tool(
    "task_new",
    (
        "Create a persistent, resumable task worker on a peer node: a "
        "long-lived Claude session you drive turn by turn, unlike ask_peer "
        "which is one-shot. If prompt is given it runs as turn 1. Use "
        "worktree for coding tasks so the worker gets an isolated git "
        "worktree. Returns the new task's id and state. Drive it with "
        "task_send, watch it with task_output, end it with task_cancel."
    ),
    {
        "name": NODE_NAME,
        "title": prop("string", "Human label for the task"),
        "prompt": prop("string", "First turn; omit to create idle and drive later with task_send"),
        "worktree": prop("boolean", "Carve a git worktree for isolation (coding tasks)"),
        "workdir": prop("string", "Base directory the worker runs in"),
        "timeout_ms": prop("integer", "Per-turn agent timeout in milliseconds"),
    },
    ["name", "title"],
    tool_task_new,
)
tool(
    "task_list",
    (
        "List the task workers on a peer node with their id, state, turn "
        "count, and last line."
    ),
    {
        "name": NODE_NAME,
    },
    ["name"],
    tool_task_list,
)
tool(
    "task_status",
    "Snapshot one task worker on a peer node by id.",
    {
        "name": NODE_NAME,
        "id": TASK_ID,
    },
    ["name", "id"],
    tool_task_status,
)
tool(
    "task_send",
    (
        "Drive one more turn on a task worker: send it a follow-up prompt. "
        "The turn runs asynchronously; use task_output to watch it. Returns "
        "whether the turn was accepted (a busy or finished task refuses)."
    ),
    {
        "name": NODE_NAME,
        "id": TASK_ID,
        "prompt": prop("string", "The follow-up prompt for this turn"),
    },
    ["name", "id", "prompt"],
    tool_task_send,
)
tool(
    "task_output",
    (
        "Tail a task worker: replays its transcript so far, then follows "
        "the live turn to its result trailer. Bounded by the current turn, "
        "so it returns once the turn completes."
    ),
    {
        "name": NODE_NAME,
        "id": TASK_ID,
        "timeout_ms": prop("integer", "Max wait for the current turn in milliseconds"),
    },
    ["name", "id"],
    tool_task_output,
)
tool(
    "task_cancel",
    (
        "Cancel a task worker: kill any running turn and prune its "
        "worktree. Returns whether the task existed."
    ),
    {
        "name": NODE_NAME,
        "id": TASK_ID,
    },
    ["name", "id"],
    tool_task_cancel,
)
tool(
    "task_rm",
    (
        "Remove a finished task (idle, incomplete, failed, or cancelled) "
        "from the node's table, pruning any worktree. A running task "
        "must be cancelled first. Returns whether it was removed."
    ),
    {
        "name": NODE_NAME,
        "id": TASK_ID,
    },
    ["name", "id"],
    tool_task_rm,
)
# ---- local work sessions (Phase 5d): the owner's own interactive tabs ----
tool(
    "work_new",
    (
        "Open a LOCAL work session on THIS machine: a new iTerm2 tab in "
        "the current window with interactive Claude Code in the upper "
        "pane and a shell in the lower pane, both cd'd into a fresh git "
        "worktree (<repo>/.savannah-worktrees/<name> on branch "
        "work/<name>, or an existing branch via branch, or the repo "
        "itself with no_worktree). This is the owner's interactive tab, "
        "not a mesh worker: nothing runs headless, nothing is sent to a "
        "peer node, and the tool itself spends no tokens. Returns the "
        "record JSON (name, repo, worktree, branch, tab_id, claude_pane, "
        "shell_pane, prompt, created, updated, terminal). Name rule: "
        "^[a-z0-9][a-z0-9._-]{0,63}$ and not t-<digits>. macOS + iTerm2 "
        "only today."
    ),
    {
        "name": prop("string", "Work session name (also the directory and tab name)"),
        "repo": prop("string", "Git repo to work in (default: the shim's current directory)"),
        "branch": prop("string", "Check out this EXISTING branch instead of creating work/<name>"),
        "no_worktree": prop("boolean", "Run in the repo itself, no worktree, no branch"),
        "prompt": prop("string", "Initial prompt handed to the interactive claude command"),
        "note": prop("string", "First plain-text note for the human"),
        "title": prop("string", "Free-text title the board shows (first line, 120 chars); the name "
                                "stays the identifier"),
    },
    ["name"],
    tool_work_new,
)
tool(
    "work_list",
    (
        "List the owner's LOCAL work sessions on this machine (not mesh "
        "workers), each joined at read time with its live Claude Code "
        "hook session when one is running in that worktree. Returns a "
        "JSON array of records plus claude_session_id, state (offline "
        "when no live session), message, last_prompt, last_note, "
        "notes_path, transcript_path."
    ),
    {},
    [],
    tool_work_list,
)
tool(
    "work_note",
    (
        "Append a timestamped plain-text note to a local work session's "
        "notes.md. Notes are for the human, to remember what that tab "
        "was doing; multi-line text is fine."
    ),
    {
        "name": WORK_NAME,
        "text": prop("string", "The note text"),
    },
    ["name", "text"],
    tool_work_note,
)
tool(
    "work_notes",
    (
        "Return the plain-text notes of a local work session (the whole "
        "notes.md, newest note last)."
    ),
    {
        "name": WORK_NAME,
    },
    ["name"],
    tool_work_notes,
)
tool(
    "work_say",
    (
        "Type text (plus Enter) into the interactive Claude Code pane of "
        "a local work session's iTerm2 tab, as if the owner had typed it. "
        "This is how you talk to that interactive session; it returns "
        "nothing. Poll work_list for its state, then read the reply with "
        "work_output. Fails if the pane no longer exists."
    ),
    {
        "name": WORK_NAME,
        "text": prop("string", "What to type into the Claude pane"),
    },
    ["name", "text"],
    tool_work_say,
)
tool(
    "work_output",
    (
        "Read what the interactive Claude in a local work session's tab "
        "has said: the last N turns, each the human prompt and the "
        "assistant's reply text (tool calls and thinking left out). This "
        "is the reply channel for work_say: send with work_say, poll "
        "work_list until state is done or waiting, then read here. "
        "Parses the transcript Claude Code itself writes (the hook's "
        "recorded path, else the newest transcript for that worktree). "
        "Fails until that Claude has started."
    ),
    {
        "name": WORK_NAME,
        "last": prop("integer", "How many recent turns (default 1)", minimum=1),
    },
    ["name"],
    tool_work_output,
)
tool(
    "work_ask",
    (
        "Ask every local Claude Code tab that is safe to interrupt one "
        "question and collect the replies: the text is typed into each "
        "tab that is idle or done (a working tab is not interrupted, a "
        "waiting tab has a permission prompt up, the asking session is "
        "skipped), the call waits for each tab to stop, reads its reply "
        "from its transcript, and keeps a work session's reply as its "
        "summary. Costs one turn of each asked tab's context. Default "
        "question: what are you working on, where are you with it, what "
        "comes next. Returns the replies, the skipped tabs with reasons, "
        "and the timeouts (their replies arrive in work_output later)."
    ),
    {
        "question": prop("string", "What to ask (default: a three-line status question)"),
        "names": {"type": "array", "items": {"type": "string"},
                  "description": "Work session names to ask; omit with all=true"},
        "all": prop("boolean", "Every live tab, plain tabs too (default true when names is empty)"),
        "timeout": prop("integer", "Seconds to wait for the replies (default 120)", minimum=1),
    },
    [],
    tool_work_ask,
)
tool(
    "work_shell",
    (
        "Type a command (plus Enter) into the lower shell pane of a local "
        "work session's iTerm2 tab, cd'd into its worktree. Output stays "
        "in the tab; nothing is captured back. Fails if the pane no "
        "longer exists."
    ),
    {
        "name": WORK_NAME,
        "cmd": prop("string", "Shell command line to type"),
    },
    ["name", "cmd"],
    tool_work_shell,
)
tool(
    "work_jump",
    (
        "Bring a local work session's iTerm2 tab forward and focus its "
        "Claude pane (or the shell pane with shell=true). Fails if the "
        "pane no longer exists."
    ),
    {
        "name": WORK_NAME,
        "shell": prop("boolean", "Focus the shell pane instead of the Claude pane"),
    },
    ["name"],
    tool_work_jump,
)
tool(
    "work_open",
    (
        "Bring a local work session's tab forward, or, when its tab is "
        "gone (iTerm2 restarted, tab closed by hand), open it again: a "
        "new tab with Claude above and a shell below in the same "
        "worktree, Claude resuming the worktree's last conversation when "
        "one exists. Returns JSON {name, action: focused|reopened, "
        "tab_id, claude_pane, shell_pane}."
    ),
    {
        "name": WORK_NAME,
        "shell": prop("boolean", "When focusing, focus the shell pane instead of the Claude pane"),
    },
    ["name"],
    tool_work_open,
)
tool(
    "work_adopt",
    (
        "Take an interactive Claude Code tab the human opened themselves "
        "(one work_list does not show; known by its Claude session id, "
        "the name of its hook file under ~/.claude/savannah/sessions/) "
        "into a named local work session. No new tab, no worktree: the "
        "directory that tab runs in becomes the session's worktree as "
        "it is. From then on work_note, work_say, work_output, "
        "work_open, work_jump and work_rm address it; work_rm forgets "
        "the record and never touches the directory. It has no shell "
        "pane (work_shell fails) until work_open remakes the tab after "
        "its pane is gone. Returns the record JSON (adopted: true)."
    ),
    {
        "name": prop("string", "New work session name: ^[a-z0-9][a-z0-9._-]{0,63}$"),
        "session_id": prop("string", "That tab's Claude Code session id (its hook file's session_id)"),
        "note": prop("string", "First note to record"),
        "title": prop("string", "Free-text title the board shows (first line, 120 chars)"),
    },
    ["name", "session_id"],
    tool_work_adopt,
)
tool(
    "work_move",
    (
        "Where a local work session sits on the savannah board: priority "
        "'now' (the top section, what the human is pushing on), 'active' "
        "(what they are on in the background) or 'later' (the bottom "
        "section), and/or its rank, the order inside the section (lower "
        "first; a new session ranks by its creation time). Nothing lands "
        "in 'now' by itself: only this tool and the board's Ctrl-P put a "
        "session there. "
        "At least one of priority or rank is required. Storage only: the "
        "tab, the worktree and the branch are not touched. Returns "
        "{name, priority, rank}."
    ),
    {
        "name": WORK_NAME,
        "priority": {"type": "string", "enum": list(WORK_PRIORITIES), "description": "The board section"},
        "rank": prop("integer", "Order inside the section, lower first (may be negative)"),
    },
    ["name"],
    tool_work_move,
)
tool(
    "work_rename",
    (
        "Rename a local work session: its record, notes and status move "
        "to the new name. The worktree directory, the git branch "
        "(work/<old>) and the tab's own Claude session name keep the old "
        "name, because a running Claude sits in that directory. Returns "
        "{name, old, worktree, branch}."
    ),
    {
        "old": prop("string", "Current name"),
        "new": prop("string", "New name: ^[a-z0-9][a-z0-9._-]{0,63}$"),
    },
    ["old", "new"],
    tool_work_rename,
)
tool(
    "work_status",
    (
        "What a local work session's own Claude says it is polling for "
        "the human (a test run, CI, a deploy): with text, record one more "
        "poll with that text (first line, 200 chars; the count goes up by "
        "one each time); with clear, remove the report; with neither, "
        "read it (status null when there is none). The board shows the "
        "text, the count and how long ago. Self-reported: nothing "
        "verifies it. Returns JSON."
    ),
    {
        "name": WORK_NAME,
        "text": prop("string", "What is being waited on, e.g. 'waiting for ctest (run 2 of 3)'"),
        "clear": prop("boolean", "Remove the report instead"),
    },
    ["name"],
    tool_work_status,
)
tool(
    "work_title",
    (
        "The free-text title of a local work session (capitals, spaces, "
        "anything; first line, 120 chars): what the board shows in its "
        "WORK column while the name stays the identifier underneath. "
        "With text, set it; with clear, remove it; with neither, read "
        "it. Returns JSON {name, title} (title empty when there is none)."
    ),
    {
        "name": WORK_NAME,
        "text": prop("string", "The title to set"),
        "clear": prop("boolean", "Remove the title instead"),
    },
    ["name"],
    tool_work_title,
)
tool(
    "work_rm",
    (
        "Remove a local work session: archive notes.md under "
        "<work dir>/archive/, git worktree remove its worktree (refused "
        "when dirty unless force; keep_worktree skips this; a refusal "
        "leaves everything in place, tab included), then close its iTerm2 "
        "tab (best effort) and delete the record. The git branch is left "
        "alone: it is the human's work. An adopted session's directory "
        "is never touched."
    ),
    {
        "name": WORK_NAME,
        "keep_worktree": prop("boolean", "Leave the worktree directory in place"),
        "force": prop("boolean", "Discard a dirty worktree"),
    },
    ["name"],
    tool_work_rm,
)


def reply(msg_id, result):
    emit({"jsonrpc": "2.0", "id": msg_id, "result": result})


def reply_error(msg_id, code, message):
    emit({"jsonrpc": "2.0", "id": msg_id,
          "error": {"code": code, "message": message}})


def emit(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def tool_prompt_head(args):
    """The head of whatever text a tool call carries (prompt, text or cmd),
    for the log line; "" when it carries none."""
    for key in ("prompt", "text", "cmd"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            head = v.strip().splitlines()[0]
            return head if len(head) <= LOG_PROMPT_CLIP else head[:LOG_PROMPT_CLIP - 1] + "~"
    return ""


def handle(msg):
    method = msg.get("method", "")
    msg_id = msg.get("id")
    is_notification = msg_id is None

    if method == "initialize":
        client_ver = msg.get("params", {}).get("protocolVersion",
                                               PROTOCOL_VERSION)
        reply(msg_id, {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        reply(msg_id, {})
    elif method == "tools/list":
        reply(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params", {})
        tool = params.get("name", "")
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            args = {}
        handler = HANDLERS.get(tool)
        if handler is None:
            log_warn("unknown tool", tool=str(tool))
            reply_error(msg_id, -32602,
                        f"unknown tool: {params.get('name')!r}")
            return
        log_info("tool", tool=tool, args=sorted(str(k) for k in args),
                 prompt=tool_prompt_head(args))
        t0 = time.time()
        try:
            blocks, is_error = handler(args)
            reply(msg_id, {"content": blocks, "isError": is_error})
            ms = int((time.time() - t0) * 1000)
            if is_error:
                log_warn("tool error", tool=tool, ms=ms,
                         error=(blocks[0].get("text", "") if blocks else "")[:200])
            else:
                log_debug("tool done", tool=tool, ms=ms)
        except Exception as e:  # tool failure -> tool-level error result
            log_warn("tool error", tool=tool, ms=int((time.time() - t0) * 1000),
                     error=str(e)[:200])
            reply(msg_id, {"content": [text_block(str(e))],
                           "isError": True})
    elif not is_notification:
        reply_error(msg_id, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"peer: dropping non-JSON line: {line[:80]}",
                  file=sys.stderr)
            continue
        handle(msg)


if __name__ == "__main__":
    main()
