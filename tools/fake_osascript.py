#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# fake_osascript - a stand-in for macOS osascript, for work-session tests.
#
# work/savannah_work.py drives iTerm2 through $SAVANNAH_OSASCRIPT; tests point
# that at this script so no real iTerm2 (or macOS) is involved. It accepts the
# script the same way osascript does (one or more -e arguments, or stdin when
# none), appends every script it receives to $FAKE_OSASCRIPT_LOG, and answers
# deterministically:
#   create-split-tab script (mentions "split horizontally"):
#       three lines TAB-<n> / PANE-C-<n> / PANE-S-<n>, n persisted in
#       $FAKE_OSASCRIPT_LOG.counter so successive tabs get distinct ids
#   anything else (focus / write / close):
#       "false" if the script mentions any id in $FAKE_OSASCRIPT_MISSING
#       (comma separated), else "true"
# Stdlib only. Exit 0 always, like a well-behaved osascript.

import os
import sys


def main():
    argv = sys.argv[1:]
    pieces = []
    i = 0
    while i < len(argv):
        if argv[i] == "-e" and i + 1 < len(argv):
            pieces.append(argv[i + 1])
            i += 2
        else:
            i += 1
    script = "\n".join(pieces) if pieces else sys.stdin.read()

    log = os.environ.get("FAKE_OSASCRIPT_LOG", "")
    if log:
        with open(log, "a") as f:
            f.write("---- osascript ----\n" + script + "\n")

    if "split horizontally" in script:
        n = 1
        counter = (log + ".counter") if log else ""
        if counter:
            try:
                with open(counter) as f:
                    n = int(f.read().strip() or "0") + 1
            except (OSError, ValueError):
                n = 1
            with open(counter, "w") as f:
                f.write(str(n))
        print(f"TAB-{n}\nPANE-C-{n}\nPANE-S-{n}")
        return 0

    missing = [m for m in os.environ.get("FAKE_OSASCRIPT_MISSING", "").split(",") if m]
    print("false" if any(m in script for m in missing) else "true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
