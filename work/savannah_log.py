#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_log - savannah's one log, shared by the work CLI (src=work), the
# Claude Code hook (src=hook) and the MCP shim (src=shim); the board writes
# the same file from Go (src=dash, dash/logging.go). Stdlib only (logging
# and its rotating file handler), nothing on the network, and never a byte
# on stdout or stderr: the hook's and the shim's streams belong to Claude
# Code, and a verb's output is its contract.
#
# Every record goes to <log dir>/savannah.log ($SAVANNAH_LOG_DIR, default
# ~/.claude/savannah/log), one line each, in the text form Go's log/slog
# writes, so the four writers read as one stream:
#   time=2026-11-05T09:12:33.123-08:00 level=INFO msg=verb src=work verb=say name=demo
# The file is always VERBOSE (every level is written); the level stamp is
# what a reader filters on (`work log --level normal|debug|verbose`, Ctrl-L
# on the board): normal = INFO and above, debug = DEBUG and above, verbose =
# everything. Levels, lowest first: VERBOSE (payloads: what it2 printed,
# transcript counts, output heads), DEBUG (lookups and joins, every it2 and
# git call with its exit code and milliseconds, every hook event), INFO (a
# state change: a record written or removed, text typed into a pane, a
# status or title set, a tool call), WARN (a refused verb, a failed command,
# a tool error), ERROR (a crash). Rotation at 10 MiB with three old files
# kept (savannah.log.1 .. .3), the scheme the board's own writer uses, so
# either side may rotate; two writers rotating in the same instant can lose
# a few lines (stated in work/README.md). Logging never raises and never
# blocks a caller: an unwritable directory means no log, not no work.
#
# Use: `import savannah_log` (this directory on sys.path), then once
# `savannah_log.configure("hook")` to set the src= stamp of every line this
# process writes ("work" until then), then savannah_log.log_info("msg",
# key=value, ...) and friends. shows()/line_level() are the reader's half.

import json
import logging
import logging.handlers
import os
import re
import time

VERBOSE = 5                       # below DEBUG: payloads and output heads
logging.addLevelName(VERBOSE, "VERBOSE")
logging.addLevelName(logging.WARNING, "WARN")   # slog's spelling
LOG_FILE = "savannah.log"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_KEEP = 3
SRC = "work"                      # the src= stamp of this process's lines
_logger = None

# The reader's levels: what each one hides. A line with no level= stamp, or
# one nobody writes, shows at every level (hiding what we cannot classify
# would hide the surprise someone is looking for).
READ_LEVELS = ("normal", "debug", "verbose")
HIDDEN = {
    "normal": frozenset(("DEBUG", "VERBOSE", "TRACE")),
    "debug": frozenset(("VERBOSE", "TRACE")),
    "verbose": frozenset(),
}
LEVEL_RE = re.compile(r"(?:^| )level=([A-Za-z]+)(?= |$)")


def configure(src):
    """The src= stamp of every line this process writes from now on: "work",
    "hook" or "shim" (the board stamps "dash" from Go)."""
    global SRC
    SRC = str(src or "").strip() or "work"


def log_dir():
    return os.environ.get("SAVANNAH_LOG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "savannah", "log")


def log_path():
    return os.path.join(log_dir(), LOG_FILE)


def log_quote(value):
    """slog's text form: a list as [a b c]; a value bare when it is one plain
    word, quoted (JSON escapes) otherwise."""
    if isinstance(value, (list, tuple)):
        value = "[" + " ".join(str(v) for v in value) + "]"
    s = str(value)
    if s and all(c.isalnum() or c in "-_./:@+" for c in s):
        return s
    return json.dumps(s, ensure_ascii=False)


def log_stamp(created):
    """RFC 3339 local time with milliseconds, what slog writes."""
    lt = time.localtime(created)
    tz = time.strftime("%z", lt)
    return (time.strftime("%Y-%m-%dT%H:%M:%S", lt) + f".{int((created % 1) * 1000):03d}"
            + tz[:3] + ":" + tz[3:])


class LogFormatter(logging.Formatter):
    def format(self, record):
        parts = [f"time={log_stamp(record.created)}", f"level={record.levelname}",
                 f"msg={log_quote(record.getMessage())}", f"src={SRC}"]
        for k, v in (getattr(record, "kv", None) or {}).items():
            parts.append(f"{k}={log_quote(v)}")
        return " ".join(parts)


class _FileHandler(logging.handlers.RotatingFileHandler):
    def handleError(self, record):
        # logging's default prints a traceback on stderr when a write fails;
        # stderr is never ours to print on, so a failed line is a lost line.
        pass


def logger():
    global _logger
    if _logger is None:
        lg = logging.getLogger("savannah")
        lg.setLevel(VERBOSE)
        lg.propagate = False
        try:
            os.makedirs(log_dir(), exist_ok=True)
            h = _FileHandler(log_path(), maxBytes=LOG_MAX_BYTES, backupCount=LOG_KEEP,
                             encoding="utf-8")
            h.setFormatter(LogFormatter())
            lg.addHandler(h)
        except OSError:
            lg.addHandler(logging.NullHandler())
        _logger = lg
    return _logger


def log(level, msg, **kv):
    try:
        logger().log(level, msg, extra={"kv": kv})
    except Exception:  # noqa: BLE001 - a log line is never worth a failed caller
        pass


def log_verbose(msg, **kv):
    log(VERBOSE, msg, **kv)


def log_debug(msg, **kv):
    log(logging.DEBUG, msg, **kv)


def log_info(msg, **kv):
    log(logging.INFO, msg, **kv)


def log_warn(msg, **kv):
    log(logging.WARNING, msg, **kv)


def log_error(msg, **kv):
    log(logging.ERROR, msg, **kv)


# --------------------------------------------------------------------------
# the reader's half


def line_level(line):
    """The level= stamp of one log line ("INFO"), or "" when it has none."""
    m = LEVEL_RE.search(line)
    return m.group(1) if m else ""


def shows(line, level):
    """Whether a reader at `level` (normal, debug, verbose) shows this line."""
    return line_level(line) not in HIDDEN[level]
