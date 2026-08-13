#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# fake_savannah - a stand-in for the savannah CLI, for shim tests.
# Mirrors the CLI's output contract exactly (stdout = TEXT verbatim,
# stderr = [tool]/[result] lines, exit 0 iff is_error:false) with
# deterministic canned scenarios keyed off the node name:
#
#   any name       echo the prompt, clean result trailer
#   busy-node      busy error trailer, exit 1
#   dead-node      no trailer at all, exit 1 (transport-level failure)

import sys


def main():
    args = sys.argv[1:]
    if not args:
        return 2
    cmd = args[0]

    if cmd == "ls":
        print("dbj-mac 127.0.0.1:59959")
        print("dbj-devone 10.0.0.208:39321")
        return 0

    if cmd == "status":
        print("busy" if args[1] == "busy-node" else "idle")
        return 0

    if cmd == "ask":
        node, prompt = args[1], args[2]
        if node == "busy-node":
            print('\n[result] {"is_error":true,"reason":"busy",'
                  '"exit_code":0}', file=sys.stderr)
            return 1
        if node == "dead-node":
            print("savannah: Service died or timed out", file=sys.stderr)
            return 1
        sys.stdout.write(prompt)
        print("[tool] Bash: git status", file=sys.stderr)
        print('\n[result] {"cost_usd":0.00042,"duration_ms":1234,'
              '"is_error":false,"num_turns":1}', file=sys.stderr)
        return 0

    # task <sub> <node> [id] [prompt] [flags]. Canned, deterministic; id
    # "t-404" is the not-found task so send/cancel can exercise the error path.
    if cmd == "task":
        sub, node = args[1], args[2]
        if sub == "new":
            title = args[args.index("--title") + 1] if "--title" in args else ""
            print(f't-0001  running  turns=0  "{title}"  /tmp/wt')
            return 0
        if sub == "ls":
            print('t-0001  idle  turns=1  "demo"  /tmp/wt')
            print("    turn 1: hello")
            return 0
        tid = args[3] if len(args) > 3 else ""
        if sub == "status":
            print(f'{tid}  idle  turns=1  "demo"  /tmp/wt')
            return 0
        if sub == "send":
            if tid == "t-404":
                print("not sent (task busy or gone)", file=sys.stderr)
                return 1
            print("sent", file=sys.stderr)
            return 0
        if sub == "tail":
            # Two invocations: the first hit the max-turns cap (error_max_turns,
            # an interim trailer), the auto-continue finished (success). The
            # shim must surface the LAST trailer, not the first.
            sys.stdout.write("turn 1: hello\n")
            print('\n[result] {"is_error":true,"num_turns":3,'
                  '"subtype":"error_max_turns"}', file=sys.stderr)
            sys.stdout.write("\n----- turn 2 -----\ndone\n")
            print('\n[result] {"cost_usd":0.00042,"duration_ms":1234,'
                  '"is_error":false,"num_turns":1,"subtype":"success"}',
                  file=sys.stderr)
            return 0
        if sub == "cancel":
            if tid == "t-404":
                print("no such task", file=sys.stderr)
                return 1
            print("cancelled", file=sys.stderr)
            return 0
        return 2

    return 2


if __name__ == "__main__":
    sys.exit(main())
