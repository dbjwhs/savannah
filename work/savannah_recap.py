# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_recap - the `recap` verb: where each session of a board section
# stands, on one screen (savannah_work.py is the entry point; this is a
# flat sibling module).
#
# Back after days away, the NOW list is a column of names. recap prints,
# for every work session of the sections asked for, what is already on
# disk: its title and branch, when it was last active, the summary its own
# Claude last gave (`work ask`), the last note, your last prompt and the
# end of Claude's last reply. Nothing is typed into any tab and no Claude
# is called, so it works on a session whose tab is closed and costs
# nothing.
#
# --ask first types the `work ask` question into the sessions that are safe
# to interrupt (savannah_ask: idle or done, never working or waiting),
# waits for the replies, then prints the recap with the fresh summaries.
# That spends one turn of each asked session.
#
# The last prompt and reply come from the transcript Claude Code writes
# (savannah_transcript). A turn that is an answer to `work ask` is skipped
# when the summary already shows it, so the recap shows the last piece of
# real work and not the question about it twice.
#
# Imports: the standard library, savannah_log, savannah_core,
# savannah_sessions, savannah_transcript and savannah_ask.

import datetime
import json
import time

from savannah_log import log_debug, log_info
from savannah_core import (clip, first_line, last_note_line, list_records, load_record, parse_sections,
                           read_notes, section_of, usage_error, UserError)
from savannah_sessions import join_session, load_hook_sessions
from savannah_transcript import find_transcript, transcript_turns
import savannah_ask

RECAP_SECTIONS = "now"             # the sections recapped when none is named
RECAP_LINES = 12                   # lines of Claude's last reply shown
RECAP_PROMPT_CLIP = 200            # chars of your last prompt shown
RECAP_LINE_CLIP = 400              # chars of one reply line kept


def iso_epoch(iso):
    """'2026-10-30T01:33:10.326Z' -> epoch seconds; 0 for '' or junk."""
    if not iso:
        return 0
    try:
        return int(datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


def human_age(seconds):
    """A span as one unit: 45s, 12m, 3h, 4d."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def tail_lines(text, count):
    """The last count non-blank-edged lines of text, each cut to
    RECAP_LINE_CLIP. -> (the lines joined, how many lines were left out)."""
    lines = (text or "").strip().splitlines()
    cut = max(0, len(lines) - count)
    return "\n".join(clip(line.rstrip(), RECAP_LINE_CLIP) for line in lines[cut:]), cut


def last_work_turn(turns, summary):
    """The newest turn that is not the answer the summary already shows
    (a `work ask` reply), else the newest turn, else None."""
    summary = (summary or "").strip()
    for t in reversed(turns):
        if not summary or (t.get("text") or "").strip() != summary:
            return t
    return turns[-1] if turns else None


def recap_entry(rec, hooks, lines):
    """One session's recap from what is on disk."""
    h = join_session(rec, hooks)
    name = rec["name"]
    summary = rec.get("summary") or ""
    entry = {
        "name": name,
        "title": rec.get("title") or "",
        "section": section_of(rec, h is not None),
        "kind": rec.get("kind") or "",
        "branch": rec.get("branch") or "",
        "worktree": rec.get("worktree") or "",
        "state": (h or {}).get("state", "") or ("offline" if h is None else ""),
        "last_active": 0,
        "summary": summary,
        "summary_at": rec.get("summary_at") or 0,
        "last_note": last_note_line(read_notes(name)),
        "last_prompt": "",
        "reply": "",
        "reply_cut": 0,
        "turns_total": 0,
    }
    turns = []
    path, source = find_transcript(rec, hooks)
    if path:
        try:
            turns = transcript_turns(path)
        except UserError:
            turns = []
    entry["turns_total"] = len(turns)
    if turns:
        entry["last_active"] = iso_epoch(turns[-1].get("answered") or turns[-1].get("asked") or "")
    t = last_work_turn(turns, summary)
    if t is not None:
        entry["last_prompt"] = clip(first_line(t.get("prompt") or ""), RECAP_PROMPT_CLIP)
        entry["reply"], entry["reply_cut"] = tail_lines(t.get("text") or "", lines)
    if not entry["last_active"]:
        entry["last_active"] = rec.get("updated") or rec.get("created") or 0
    log_debug("recap entry", name=name, section=entry["section"], turns=len(turns), source=source)
    return entry


def recap_entries(names, sections, hooks, records, lines):
    """The named sessions, then every session of those sections, each once:
    named ones in the order given, the rest in the board's order."""
    out, seen = [], set()
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(recap_entry(load_record(name), hooks, lines))
    rows = []
    for rec in records:
        if rec["name"] in seen:
            continue
        sec = section_of(rec, join_session(rec, hooks) is not None)
        if sec in sections:
            rows.append((sections.index(sec), rec.get("rank") or 0, rec["name"], rec))
    for _, _, _, rec in sorted(rows, key=lambda r: r[:3]):
        out.append(recap_entry(rec, hooks, lines))
    return out


def print_recap(entries, sections, ask, now=None):
    """recap for a person: a block per session under its section's header."""
    now = time.time() if now is None else now
    if not entries:
        where = ", ".join(s.upper() for s in sections) or "the sessions named"
        print(f"(no work sessions in {where})")
    current = None
    for e in entries:
        if e["section"] != current:
            if current is not None:
                print()
            current = e["section"]
            count = sum(1 for x in entries if x["section"] == current)
            print(f"{current.upper()} ({count})")
        print()
        head = [f"## {e['name']}"]
        if e["title"]:
            head.append(e["title"])
        if e["branch"]:
            head.append(e["branch"])
        head.append(e["state"] or "idle")
        if e["last_active"]:
            head.append(f"last active {human_age(now - e['last_active'])} ago")
        print("   ".join(head))
        if e["summary"]:
            age = f" ({human_age(now - e['summary_at'])} ago)" if e["summary_at"] else ""
            print(f"summary{age}:")
            for line in e["summary"].strip().splitlines():
                print("  " + line)
        if e["last_note"]:
            print(f"note: {e['last_note']}")
        if e["last_prompt"]:
            print(f"you: {e['last_prompt']}")
        if e["reply"]:
            more = f" (the last {len(e['reply'].splitlines())} lines; {e['reply_cut']} above)" \
                if e["reply_cut"] else ""
            print(f"claude{more}:")
            for line in e["reply"].splitlines():
                print("  " + line)
        elif not e["turns_total"]:
            print("(no transcript found: nothing was asked there yet, or its transcript is gone)")
    if ask is not None:
        print()
        answered = sum(1 for x in ask["asked"] if x["how"] == "answered")
        print(f"asked {len(ask['asked'])}, {answered} answered in {ask['waited']:.0f} s")
        for x in ask["asked"]:
            if x["how"] != "answered":
                print(f"no reply yet from {x['name'] or x['session_id']} ({x['how']})")
        for x in ask["skipped"]:
            print(f"skipped {x['name'] or x['session_id']}: {x['why']}")
        for x in ask["failed"]:
            print(f"failed {x['name'] or x['session_id']}: {x['error']}")


def cmd_recap(a):
    """Where each session stands: the named work sessions and/or those of
    --section (default: now), from the records, notes and transcripts on
    disk. --ask first asks the ones that are safe to interrupt and waits,
    so the summaries are fresh. Exit 0 whatever was found; 1 for an
    unknown name or section."""
    if a.lines < 1:
        return usage_error("recap", "--lines wants a positive count")
    if a.timeout < 1:
        return usage_error("recap", "--timeout wants a positive number of seconds")
    sections = parse_sections(a.section) if a.section else ([] if a.name else parse_sections(RECAP_SECTIONS))
    hooks = load_hook_sessions()
    records = list_records()
    for name in a.name:
        load_record(name)              # an unknown name fails before anything is typed
    ask = None
    if a.ask:
        names = list(dict.fromkeys(a.name))
        names += [n for n in savannah_ask.section_names(sections, hooks, records) if n not in names]
        question = (a.question or "").strip() or savannah_ask.ASK_DEFAULT
        ask = savannah_ask.run_ask(names, False, question, a.timeout, False, hooks, records)
        hooks = load_hook_sessions()   # the asked tabs have moved on
        records = list_records()       # and their summaries are new
    entries = recap_entries(a.name, sections, hooks, records, a.lines)
    log_info("recap", sections=",".join(sections), named=len(a.name), sessions=len(entries), asked=a.ask)
    if a.json:
        out = {"sections": sections, "sessions": entries}
        if ask is not None:
            out["ask"] = ask
        print(json.dumps(out))
    else:
        print_recap(entries, sections, ask)
    return 0
