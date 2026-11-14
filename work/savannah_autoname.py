# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_autoname - the `autoname` verb: a name for every tab you opened
# yourself, from what it is doing (savannah_work.py is the entry point;
# this is a flat sibling module).
#
# The board asks for this when it comes up (and for each new plain tab once
# it has a prompt): every hook session no record claims gets a title and a
# name, then is adopted like a Ctrl-R would. The title comes from a headless
# Claude reading an excerpt of the tab's own transcript (its first prompts,
# the latest prompt, the head of the latest reply), never by typing into the
# live session: a question typed into a tab would land as a prompt (or as
# the answer to a permission prompt), spend that session's context and
# tokens, and interrupt whatever it is doing. The excerpt is what the tab
# knows; a fresh small model names it in a few seconds for a fraction of a
# cent. When the call fails (no claude, no network, a timeout) the first
# prompt's first line is the title instead, and the result says so.
#
# Imports: the standard library, savannah_log, savannah_core,
# savannah_sessions, savannah_transcript and savannah_tabs.

import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

from savannah_log import log_debug, log_info, log_warn
import savannah_core
from savannah_core import (claude_argv, display_path, first_line, list_records, oserr, title_text,
                           UserError, valid_name)
from savannah_sessions import hook_session_by_id, load_hook_sessions, unclaimed_sessions
from savannah_transcript import transcript_for, transcript_turns
from savannah_tabs import adopt_session

NAME_SYSTEM_PROMPT = (
    "You name programming work sessions for a board that lists many of them. You are given "
    "excerpts of one Claude Code conversation: its directory, its first prompts, its latest "
    "prompt and the start of the latest reply. Answer with ONE line: a title of two to five "
    "words in Title Case naming the task being worked on (the what, not the who or the how). "
    "No quotes, no trailing punctuation, nothing else.")
NAME_TIMEOUT = 90      # seconds per headless call
NAME_EXCERPT = 400     # chars of each quoted prompt or reply
NAME_WORKERS = 4       # headless calls at once
NAME_FALLBACK_MAX = 60  # chars of a title made from the first prompt


def name_model():
    """The model the naming call asks for: $SAVANNAH_NAME_MODEL, default the
    'haiku' alias (small, quick, cheap); 'default' or empty = the CLI's own."""
    m = os.environ.get("SAVANNAH_NAME_MODEL")
    if m is None:
        return "haiku"
    return "" if m.strip().lower() == "default" else m.strip()


def squeeze(text, limit):
    """text with its whitespace collapsed to single spaces, cut to limit."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[:limit].rstrip() + "..."


def is_human_prompt(text):
    """A prompt a person typed, not a slash command's wrapper, a system
    marker or an interruption notice."""
    t = (text or "").strip()
    return bool(t) and not t.startswith("<") and not t.startswith("[Request interrupted")


def session_prompts(h):
    """(prompts, last_reply) of a hook session: the human prompts of its
    transcript in order (commands and interruptions dropped) and the text
    of the latest reply, or the hook's last_prompt alone when there is no
    transcript on disk. ([], "") when nothing has been typed yet."""
    sid = h.get("session_id", "") if isinstance(h.get("session_id"), str) else ""
    tp = h.get("transcript_path", "") if isinstance(h.get("transcript_path"), str) else ""
    project = h.get("project", "")
    path = transcript_for(sid, tp, project)
    prompts, reply = [], ""
    if path:
        try:
            turns = transcript_turns(path)
        except UserError:
            turns = []
        for t in turns:
            if is_human_prompt(t["prompt"]):
                prompts.append(t["prompt"])
                reply = t["text"]
    last = h.get("last_prompt", "")
    if not prompts and isinstance(last, str) and is_human_prompt(last):
        prompts = [last]
    return prompts, reply


def session_excerpt(project, prompts, reply):
    """The few lines the naming call reads: the directory, up to three first
    prompts, the latest prompt when there are more, the head of the latest
    reply. Each quote is squeezed to NAME_EXCERPT chars."""
    lines = [f"Directory: {display_path(project)}"]
    for i, p in enumerate(prompts[:3], 1):
        lines.append(f"Prompt {i}: {squeeze(p, NAME_EXCERPT)}")
    if len(prompts) > 3:
        lines.append(f"Latest prompt ({len(prompts)} so far): {squeeze(prompts[-1], NAME_EXCERPT)}")
    if reply:
        lines.append(f"Latest reply begins: {squeeze(reply, NAME_EXCERPT)}")
    return "\n".join(lines)


def clean_title(text):
    """Claude's answer as a title: the first non-blank line, a leading
    'Title:' and surrounding quotes or backticks dropped, trailing
    punctuation dropped, whitespace squeezed, cut to TITLE_MAX. '' when
    nothing remains."""
    t = first_line(text)
    if not t:
        return ""
    t = re.sub(r"^(title|name)\s*:\s*", "", t, flags=re.IGNORECASE)
    t = t.strip().strip('"\'`*').strip()
    t = t.rstrip(".!?:;,").strip()
    return title_text(" ".join(t.split()))


def ask_title(excerpt):
    """One headless Claude call (claude -p) for a title, or UserError (the
    caller falls back). --safe-mode: no hooks (so no ghost row on the
    board), no CLAUDE.md, no MCP servers; no tools, one turn, nothing
    persisted, run from the temp dir so no project is involved. The hook is
    told to stand down as well (SAVANNAH_HOOK_IGNORE), belt and braces."""
    argv = claude_argv() + ["-p", "--safe-mode", "--no-session-persistence", "--strict-mcp-config",
                            "--tools", "", "--max-turns", "1", "--output-format", "json",
                            "--system-prompt", NAME_SYSTEM_PROMPT]
    model = name_model()
    if model:
        argv += ["--model", model]
    argv.append(excerpt)
    env = dict(os.environ)
    env["SAVANNAH_HOOK_IGNORE"] = "1"
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=NAME_TIMEOUT, env=env,
                           cwd=tempfile.gettempdir())
    except OSError as e:
        raise UserError(f"cannot run {argv[0]}: {oserr(e)} (set SAVANNAH_CLAUDE_BIN)")
    except subprocess.TimeoutExpired:
        raise UserError(f"{argv[0]} took over {NAME_TIMEOUT}s")
    ms = int((time.time() - t0) * 1000)
    log_debug("claude", argv=argv[:-1], rc=p.returncode, ms=ms, out=len(p.stdout))
    if p.returncode != 0:
        raise UserError(f"{argv[0]} exited {p.returncode}: {squeeze(p.stderr, 160) or 'no message'}")
    try:
        d = json.loads(p.stdout)
    except ValueError:
        raise UserError(f"{argv[0]} printed no JSON result")
    if not isinstance(d, dict):
        raise UserError(f"{argv[0]} printed JSON that is not a result object")
    if d.get("is_error"):
        raise UserError(f"{argv[0]} reported an error: "
                        f"{squeeze(str(d.get('result') or ''), 160) or d.get('subtype', '?')}")
    title = clean_title(d.get("result"))
    if not title:
        raise UserError(f"{argv[0]} gave no title")
    log_debug("named by claude", title=title, ms=ms, cost=d.get("total_cost_usd"), model=model or "default")
    return title


def slug_name(src):
    """Free text (or a path, whose last component counts) as a valid work
    session name, or "": lowered, letters and digits kept, '.', '_' and '-'
    kept after the start, any other run one '-', trailing '.-_' dropped,
    60 chars. The same rule as the board's nameFor (dash/board.go)."""
    base = (src or "").strip().rstrip("/").lower()
    base = base.rsplit("/", 1)[-1]
    out = []
    last = ""
    for ch in base:
        if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
            out.append(ch)
        elif out and ch in "._-":
            out.append(ch)
        elif out and last != "-":
            ch = "-"
            out.append(ch)
        else:
            continue
        last = ch
    name = "".join(out).rstrip(".-_")
    if len(name) > 60:
        name = name[:60].rstrip(".-_")
    return name if valid_name(name) else ""


def checkout_name(project):
    """The checkout a directory sits in, when it is one the owner made
    beside its repository the `<repo>-<ID>` way (zfs-DLPX-98816 next to
    zfs): the checkout directory's base name. That is a git worktree (its
    `.git` is a file) or a clone named `<X>-<ID>` with a repository `<X>`
    beside it (the longest such X). '' for the main checkout, a savannah
    worktree, or a directory under no `.git`: the title names those."""
    d = os.path.realpath(project) if project else ""
    while d and not os.path.exists(os.path.join(d, ".git")):
        parent = os.path.dirname(d)
        if parent == d:
            return ""
        d = parent
    if not d:
        return ""
    parent, base = os.path.dirname(d), os.path.basename(d)
    if os.path.basename(parent) == savannah_core.WORKTREES_SUBDIR:
        return ""
    if os.path.isfile(os.path.join(d, ".git")):
        return base
    i = base.rfind("-")
    while i > 0:
        if os.path.exists(os.path.join(parent, base[:i], ".git")):
            return base
        i = base.rfind("-", 0, i)
    return ""


def unique_name(name, taken):
    """name, or name-2, name-3, ... when it is taken."""
    if name not in taken:
        return name
    i = 2
    while f"{name}-{i}" in taken:
        i += 1
    return f"{name}-{i}"


def autoname_pick(wanted, plain, skipped):
    """The tabs to name: every unclaimed one (plain), or of those only the
    session ids asked for. An id that is not among them is added to skipped
    with why (a work session already, or no live session at all)."""
    if not wanted:
        return plain
    by_id = {h.get("session_id"): h for h in plain}
    chosen = []
    for sid in wanted:
        if sid in by_id:
            chosen.append(by_id[sid])
        elif hook_session_by_id(sid) is not None:
            skipped.append({"session_id": sid, "why": "already a work session"})
        else:
            skipped.append({"session_id": sid, "why": "no live Claude session"})
    return chosen


def autoname_jobs(plain, skipped):
    """One naming job per tab that has something to name it by: its prompts
    and the excerpt the headless call reads. A tab nothing has been typed
    into yet is added to skipped (it is named on a later run)."""
    jobs = []
    for h in plain:
        sid = h.get("session_id", "") if isinstance(h.get("session_id"), str) else ""
        project = h["project"]
        prompts, reply = session_prompts(h)
        if not prompts:
            skipped.append({"session_id": sid, "project": project, "why": "no prompt yet"})
            continue
        jobs.append({"session_id": sid, "project": project, "h": h, "prompts": prompts,
                     "excerpt": session_excerpt(project, prompts, reply)})
    return jobs


def title_for(job, no_claude):
    """(title, how, error) for one naming job: Claude's title (how
    "claude"), or the first prompt's first line (how "prompt") with
    no_claude or when the call fails; error then says why it failed. Runs
    on a pool thread, one per tab."""
    if no_claude:
        return squeeze(first_line(job["prompts"][0]), NAME_FALLBACK_MAX), "prompt", ""
    try:
        return ask_title(job["excerpt"]), "claude", ""
    except UserError as e:
        log_warn("autoname fallback", session=job["session_id"], error=str(e))
        return squeeze(first_line(job["prompts"][0]), NAME_FALLBACK_MAX), "prompt", str(e)


def autoname_adopt(jobs, results, taken, dry_run):
    """Give each titled job its name (the checkout's when the tab sits in a
    `<repo>-<ID>` checkout, else from the title, never one in taken) and
    adopt the tab under it; dry_run only says what would be. -> (named,
    failed)."""
    named, failed = [], []
    for job, (title, how, err) in zip(jobs, results):
        name = unique_name(slug_name(checkout_name(job["project"])) or slug_name(title)
                           or slug_name(job["project"]) or "tab", taken)
        taken.add(name)
        entry = {"session_id": job["session_id"], "project": job["project"], "name": name,
                 "title": title, "how": how}
        if err:
            entry["claude_error"] = err
        if dry_run:
            named.append(entry)
            continue
        try:
            adopt_session(name, job["h"], job["session_id"], title=title)
        except UserError as e:
            failed.append({"session_id": job["session_id"], "project": job["project"], "error": str(e)})
            continue
        named.append(entry)
    return named, failed


def print_autoname(named, skipped, failed, dry_run):
    """autoname's result for a person: one line per tab."""
    verb = "would name" if dry_run else "named"
    for e in named:
        print(f"{verb} {e['session_id']}: {e['name']}  \"{e['title']}\"  ({e['how']}, {display_path(e['project'])})")
    for e in skipped:
        print(f"skipped {e['session_id']}: {e['why']}")
    for e in failed:
        print(f"failed {e['session_id']}: {e['error']}")
    if not (named or skipped or failed):
        print("nothing to name: every live Claude Code tab is a work session already")


def cmd_autoname(a):
    """Name every Claude Code tab no record claims (or the given session
    ids) from what it is doing: a title from a headless Claude over the
    tab's transcript excerpt (the first prompt's first line when that
    fails, or with --no-claude), a name derived from the checkout when the
    tab sits in a `<repo>-<ID>` checkout beside its repository (zfs-DLPX-98816,
    checkout_name) and else from the title, then adopt.
    A tab nothing has been typed into yet is skipped (nothing to name it
    by) and named on a later run. Exit 0 whenever the verb ran: each tab's
    outcome is in the output (named / skipped / failed)."""
    hooks = load_hook_sessions()
    records = list_records()
    skipped = []
    plain = autoname_pick(list(dict.fromkeys(a.session or [])), unclaimed_sessions(hooks, records), skipped)
    jobs = autoname_jobs(plain, skipped)
    log_info("autoname", candidates=len(plain), asking=len(jobs), skipped=len(skipped),
             claude=not a.no_claude, dry_run=a.dry_run)
    if jobs:
        with ThreadPoolExecutor(max_workers=min(NAME_WORKERS, len(jobs))) as pool:
            results = list(pool.map(lambda job: title_for(job, a.no_claude), jobs))
    else:
        results = []
    named, failed = autoname_adopt(jobs, results, {r["name"] for r in records}, a.dry_run)
    log_info("autoname done", named=len(named), skipped=len(skipped), failed=len(failed), dry_run=a.dry_run)
    if a.json:
        print(json.dumps({"dry_run": bool(a.dry_run), "named": named, "skipped": skipped, "failed": failed}))
    else:
        print_autoname(named, skipped, failed, a.dry_run)
    return 0
