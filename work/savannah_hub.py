# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_hub - the folder of repos: the `repos` and `hub` verbs
# (savannah_work.py is the entry point; this is a flat sibling module).
#
# A hub is a directory of repos where a coordinating Claude runs (OPTIONAL;
# the front door is the board). This module remembers that directory
# ($SAVANNAH_HUB_FILE), lists its repos (repos_in, cmd_repos) and writes
# the hub's files (cmd_hub): the peer shim merged into DIR/.mcp.json, the
# work_* tools pre-allowed in DIR/.claude/settings.json (HUB_ALLOW, which
# test_shim compares with what the shim serves), and the CLAUDE.md
# templates that sit beside this file (hub_CLAUDE.md, scratch_CLAUDE.md).
#
# Imports: the standard library, savannah_log and savannah_core.

import json
import os
import sys
import time

from savannah_log import log_debug, log_info
import savannah_core
from savannah_core import claude_argv, env_path, oserr, UserError

HUB_TEMPLATE = "hub_CLAUDE.md"     # sibling of this module; copied into a new hub
SCRATCH_TEMPLATE = "scratch_CLAUDE.md"
SCRATCH_SUBDIR = ".savannah-scratch"   # tabs for tasks that belong to no repo
HUB_SERVER = "peer"                # the mcpServers key the hub registers
# Pre-approved in DIR/.claude/settings.json so the hub never stops at a
# permission prompt for its own tools (headless runs cannot answer one).
# Every work_* tool the shim serves (shim/peer.py; test_shim compares the two).
HUB_WORK_TOOLS = ("new", "adopt", "list", "note", "notes", "say", "ask", "output", "shell", "jump", "open",
                  "move", "rename", "title", "status", "rm")
HUB_ALLOW = [f"mcp__{HUB_SERVER}__work_{v}" for v in HUB_WORK_TOOLS] + ["Bash(sleep *)"]


def savannah_root():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def hub_server_entry():
    """The mcpServers entry for the peer shim, with absolute paths so it works
    from any directory (the repo's own .mcp.json uses relative ones)."""
    root = savannah_root()
    entry = {"command": "python3",
             "args": [os.path.join(root, "shim", "peer.py")]}
    env = {}
    for key, rel in (("SAVANNAH_BIN", ("build", "savannah")),
                     ("SAVANNAH_KEY", ("build", "mesh.key"))):
        path = os.path.join(root, *rel)
        if os.path.isfile(path):
            env[key] = path
    if env:
        entry["env"] = env
    return entry


def template_path(name):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), name)


def template_text(name):
    path = template_path(name)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise UserError(f"template missing: {path} ({oserr(e)})")


def hub_file():
    return env_path("SAVANNAH_HUB_FILE", "savannah", "hub.json")


def recorded_hub():
    """The remembered hub directory, or "" (no file, unreadable, not a dir)."""
    try:
        with open(hub_file(), encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return ""
    d = rec.get("dir") if isinstance(rec, dict) else None
    return d if isinstance(d, str) and os.path.isdir(d) else ""


def remember_hub(d):
    path = hub_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    savannah_core.atomic_write(path, {"dir": d, "updated": int(time.time())})
    return path


def pick_hub_dir(arg):
    """(dir, picked) for `repos` and `hub`: the directory given ("given"),
    else the remembered hub ("remembered"), else the cwd ("cwd"). Raises
    UserError when it is not a directory."""
    if arg:
        d, picked = os.path.abspath(arg), "given"
    else:
        d = recorded_hub()
        picked = "remembered" if d else "cwd"
        d = d or os.getcwd()
    if not os.path.isdir(d):
        raise UserError(f"not a directory: {d}")
    return d, picked


def load_json_object(path):
    """(object, existed) of a JSON file about to be merged into and written
    back: ({}, False) when there is no file; a file that is not JSON, not
    readable or not an object is refused (UserError), never overwritten."""
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except ValueError as e:
        raise UserError(f"refusing to rewrite malformed {path}: {e}")
    except OSError as e:
        raise UserError(f"cannot read {path}: {oserr(e)}")
    if not isinstance(cfg, dict):
        raise UserError(f"refusing to rewrite {path}: top level is not an object")
    return cfg, True


def write_hub_settings(path):
    """Merge the hub's permissions into DIR/.claude/settings.json: allow the
    work_* tools and `sleep`, and auto-approve the project's .mcp.json
    servers. Everything else in the file is kept. 'created', 'updated' or
    'unchanged'."""
    cfg, existed = load_json_object(path)
    status = "updated" if existed else "created"
    changed = False
    if cfg.get("enableAllProjectMcpServers") is not True:
        cfg["enableAllProjectMcpServers"] = True
        changed = True
    perms = cfg.get("permissions")
    if not isinstance(perms, dict):
        perms = {}
        cfg["permissions"] = perms
        changed = True
    allow = perms.get("allow")
    if not isinstance(allow, list):
        allow = []
        perms["allow"] = allow
        changed = True
    for rule in HUB_ALLOW:
        if rule not in allow:
            allow.append(rule)
            changed = True
    if not changed:
        return "unchanged"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    savannah_core.atomic_write(path, cfg)
    return status


def write_if_missing(path, text):
    """'created' or 'kept'; never overwrites."""
    if os.path.exists(path):
        return "kept"
    savannah_core.atomic_write_text(path, text, encoding="utf-8")
    return "created"


def write_hub_mcp(path):
    """Merge the peer server into DIR/.mcp.json. Other servers are kept.
    Returns 'created', 'updated' or 'unchanged'."""
    cfg, existed = load_json_object(path)
    status = "updated" if existed else "created"
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        cfg["mcpServers"] = servers
    entry = hub_server_entry()
    if servers.get(HUB_SERVER) == entry:
        return "unchanged"
    servers[HUB_SERVER] = entry
    savannah_core.atomic_write(path, cfg)
    return status


def repos_in(d):
    """Subdirectories of d that are git repositories (a .git dir or file),
    sorted, dotfiles skipped."""
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        raise UserError(f"cannot list {d}: {oserr(e)}")
    return [n for n in names if not n.startswith(".")
            and os.path.exists(os.path.join(d, n, ".git"))]


def cmd_repos(a):
    """The folder of repos the board offers for new tasks: `repos DIR`
    remembers DIR (the same memory `hub` uses), `repos` alone lists the
    remembered folder, else the cwd."""
    d, picked = pick_hub_dir(a.dir)
    if picked == "given":
        remember_hub(d)
    repos = repos_in(d)
    scratch = os.path.join(d, SCRATCH_SUBDIR)
    scratch = scratch if os.path.isdir(scratch) else ""
    log_debug("repos", dir=d, picked=picked, count=len(repos), scratch=bool(scratch))
    if a.json:
        print(json.dumps({"dir": d, "picked": picked, "repos": repos, "scratch": scratch}))
    else:
        print(f"{d}" + (f" ({picked})" if picked != "given" else ""))
        for r in repos:
            print(f"  {r}")
        if scratch:
            print(f"  scratch (no worktree): {scratch}")
        if not repos:
            print("  (no git repositories here)")
    return 0


def cmd_hub(a):
    d, picked = pick_hub_dir(a.dir)
    mcp_path = os.path.join(d, ".mcp.json")
    mcp_status = write_hub_mcp(mcp_path)
    settings_path = os.path.join(d, ".claude", "settings.json")
    settings_status = write_hub_settings(settings_path)
    claude_md = os.path.join(d, "CLAUDE.md")
    md_status = write_if_missing(claude_md, template_text(HUB_TEMPLATE))
    scratch = os.path.join(d, SCRATCH_SUBDIR)
    try:
        os.makedirs(scratch, exist_ok=True)
    except OSError as e:
        raise UserError(f"cannot create {scratch}: {oserr(e)}")
    scratch_status = write_if_missing(os.path.join(scratch, "CLAUDE.md"),
                                      template_text(SCRATCH_TEMPLATE))
    remembered = remember_hub(d)
    launch = not a.no_launch
    argv = claude_argv()
    if a.json:
        print(json.dumps({"dir": d, "picked": picked, "mcp_json": mcp_path,
                          "mcp_status": mcp_status, "settings": settings_path,
                          "settings_status": settings_status, "claude_md": claude_md,
                          "claude_md_status": md_status, "scratch": scratch,
                          "scratch_status": scratch_status, "hub_file": remembered,
                          "launch": launch, "claude": argv}))
    else:
        print(f"hub {d}" + (f" ({picked})" if picked != "given" else ""))
        print(f"  {mcp_path}: {mcp_status} ({HUB_SERVER} shim)")
        print(f"  {settings_path}: {settings_status} (work_* tools and sleep pre-allowed, "
              f"project MCP servers approved)")
        print(f"  {claude_md}: {md_status}" + (
            "" if md_status == "created" else f" (not touched; see {template_path(HUB_TEMPLATE)})"))
        print(f"  {scratch}/: {scratch_status} (tabs for tasks outside any repo)")
        print(f"  {remembered}: remembers this hub, so a bare `savannah` opens it")
        if launch:
            print(f"  launching {' '.join(argv)} in {d}")
    log_info("hub", dir=d, picked=picked, mcp=mcp_status, settings=settings_status, claude_md=md_status,
             scratch=scratch_status, launch=launch, claude=argv)
    sys.stdout.flush()
    if not launch:
        return 0
    try:
        os.chdir(d)
        os.execvp(argv[0], argv)
    except OSError as e:
        raise UserError(f"cannot launch {argv[0]}: {oserr(e)} (set SAVANNAH_CLAUDE_BIN)")
    return 0  # not reached: execvp replaces the process
