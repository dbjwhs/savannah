# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_transcript - the transcripts Claude Code writes, read back (the
# read side of say; savannah_work.py is the entry point; this is a flat
# sibling module).
#
# Finding one (find_transcript: the path the hook recorded, else the newest
# .jsonl under $CLAUDE_CONFIG_DIR/projects/<encoded worktree>/;
# transcript_for: the file `claude --resume <id>` would reload) and parsing
# it into turns (transcript_turns). The transcript is Claude Code's file
# and its layout is not a contract we own; the subset read here (type
# user/assistant, message.content text blocks, isSidechain) is pinned by
# test_work's hand-written transcript.
#
# Imports: the standard library, savannah_log, savannah_core and
# savannah_sessions.

import json
import os
import re

from savannah_log import log_debug, log_verbose, log_warn
from savannah_core import env_path, oserr, UserError
from savannah_sessions import join_session


def claude_config_dir():
    return env_path("CLAUDE_CONFIG_DIR")


def encode_project_dir(path):
    """Claude Code names ~/.claude/projects/<dir> after the cwd with every
    non-alphanumeric character replaced by '-' (so '/a/b.c' -> '-a-b-c').
    Observed, not promised: the hook's transcript_path is the primary source
    and this is only the fallback for a tab whose hook file is gone."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def find_transcript(rec, hooks):
    """(path, source) of the transcript for a work session, or ("", "").
    source is "hook" (the live hook file named it) or "projects" (newest
    .jsonl in Claude Code's own per-project directory for the worktree)."""
    name = rec.get("name", "")
    h = join_session(rec, hooks)
    p = (h or {}).get("transcript_path") or ""
    if isinstance(p, str) and p and os.path.isfile(p):
        log_debug("transcript", name=name, source="hook", path=p)
        return p, "hook"
    worktree = rec.get("worktree") or ""
    if not worktree:
        log_debug("transcript", name=name, source="", why="no worktree")
        return "", ""
    d = os.path.join(claude_config_dir(), "projects",
                     encode_project_dir(os.path.realpath(worktree)))
    try:
        names = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    except OSError:
        log_debug("transcript", name=name, source="", why="no projects dir", dir=d)
        return "", ""
    if not names:
        log_debug("transcript", name=name, source="", why="no .jsonl", dir=d)
        return "", ""
    newest = max(names, key=lambda n: os.path.getmtime(os.path.join(d, n)))
    log_debug("transcript", name=name, source="projects", path=os.path.join(d, newest), candidates=len(names))
    return os.path.join(d, newest), "projects"


def _text_blocks(content):
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    out = []
    for b in content:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                out.append(t)
    return out


def _human_prompt(content):
    """The text of a human turn, or None for a tool_result carrier."""
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return None
    parts = _text_blocks(content)
    return "\n".join(parts) if parts else None


def transcript_turns(path):
    """Turns of a Claude Code transcript: [{prompt, text, asked, answered}].
    A turn starts at each human prompt (type user whose content is not a
    tool_result) and collects the text blocks of every assistant message
    until the next prompt; thinking and tool_use blocks are dropped,
    sidechain (subagent) lines are skipped, unparsable lines are ignored."""
    turns = []
    cur = None
    lines = 0
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except OSError as e:
        log_warn("transcript unreadable", path=path, error=str(oserr(e)))
        raise UserError(f"cannot read transcript {path}: {oserr(e)}")
    with f:
        for line in f:
            lines += 1
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict) or d.get("isSidechain"):
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            kind = d.get("type")
            ts = d.get("timestamp") if isinstance(d.get("timestamp"), str) else ""
            if kind == "user":
                prompt = _human_prompt(msg.get("content"))
                if prompt is None:
                    continue
                cur = {"prompt": prompt, "text": "", "asked": ts, "answered": ""}
                turns.append(cur)
            elif kind == "assistant":
                parts = _text_blocks(msg.get("content"))
                if not parts:
                    continue
                if cur is None:
                    cur = {"prompt": "", "text": "", "asked": "", "answered": ""}
                    turns.append(cur)
                joined = "\n\n".join(parts)
                cur["text"] = (cur["text"] + "\n\n" + joined) if cur["text"] else joined
                cur["answered"] = ts or cur["answered"]
    log_verbose("transcript parsed", path=path, lines=lines, turns=len(turns))
    return turns


def local_stamp(iso):
    """'2026-10-30T01:33:10.326Z' -> '2026-10-29 18:33 local'; '' stays ''."""
    if not iso:
        return ""
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M local")
    except ValueError:
        return iso


def transcript_for(session_id, transcript_path, project):
    """The transcript `claude --resume <session_id>` would reload, or "" when
    none is on disk: the path the snapshot recorded, else Claude Code's own
    <projects dir>/<encoded project>/<session_id>.jsonl (observed naming,
    like encode_project_dir)."""
    if not session_id:
        return ""
    if transcript_path and os.path.isfile(transcript_path):
        return transcript_path
    if project:
        p = os.path.join(claude_config_dir(), "projects",
                         encode_project_dir(os.path.realpath(project)), session_id + ".jsonl")
        if os.path.isfile(p):
            return p
    return ""
