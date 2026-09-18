#!/usr/bin/env python3
# install.py - merge the session-status hook into a Claude Code settings.json,
# so savannah-dash can see this machine's open Claude Code tabs.
#
#   python3 hooks/install.py            # user-level ~/.claude/settings.json
#   python3 hooks/install.py --project  # this repo's .claude/settings.json
#   python3 hooks/install.py --print    # print the merged result, write nothing
#
# Safe and idempotent: it backs up the existing file first, preserves every
# other setting and hook you already have, and adds our command only where it
# is not already present. Run it again after moving the repo to refresh paths.
# To undo, restore the printed .bak file (or run with --uninstall).

import json
import os
import sys
import time

EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "SessionEnd"]
# Events that do not support a matcher field (see Claude Code hooks reference).
NO_MATCHER = {"UserPromptSubmit", "Stop"}


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hook_command():
    return "python3 %s" % os.path.join(repo_root(), "hooks", "session-status.py")


def settings_path(project):
    if project:
        return os.path.join(repo_root(), ".claude", "settings.json")
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except ValueError:
        print("refusing to touch %s: it is not valid JSON" % path, file=sys.stderr)
        sys.exit(1)


def group_has_command(group, cmd):
    for h in group.get("hooks", []):
        if h.get("type") == "command" and h.get("command") == cmd:
            return True
    return False


def merge(settings, cmd, uninstall=False):
    """Add (or remove) our command in each event. Returns count changed."""
    hooks = settings.setdefault("hooks", {})
    changed = 0
    for event in EVENTS:
        groups = hooks.get(event, [])
        if uninstall:
            new_groups = []
            for g in groups:
                g["hooks"] = [h for h in g.get("hooks", [])
                              if h.get("command") != cmd]
                if g.get("hooks"):
                    new_groups.append(g)
                else:
                    changed += 1
            if new_groups:
                hooks[event] = new_groups
            else:
                hooks.pop(event, None)
            continue
        if any(group_has_command(g, cmd) for g in groups):
            continue  # already installed for this event
        group = {"hooks": [{"type": "command", "command": cmd}]}
        if event not in NO_MATCHER:
            group["matcher"] = ""
        groups.append(group)
        hooks[event] = groups
        changed += 1
    if not hooks:
        settings.pop("hooks", None)
    return changed


def main():
    project = "--project" in sys.argv
    do_print = "--print" in sys.argv
    uninstall = "--uninstall" in sys.argv

    path = settings_path(project)
    cmd = hook_command()
    settings = load(path)
    changed = merge(settings, cmd, uninstall=uninstall)
    rendered = json.dumps(settings, indent=2)

    if do_print:
        print(rendered)
        return 0

    if changed == 0:
        print("no change: %s already %s" %
              (path, "clean" if uninstall else "has the hook"))
        return 0

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = "%s.bak.%d" % (path, int(time.time()))
        with open(path) as src, open(backup, "w") as dst:
            dst.write(src.read())
        print("backed up %s -> %s" % (path, backup))
    with open(path, "w") as f:
        f.write(rendered + "\n")

    verb = "removed from" if uninstall else "installed into"
    print("hook %s %s (%d event%s)" %
          (verb, path, changed, "" if changed == 1 else "s"))
    if not uninstall:
        print("restart your Claude Code sessions (or run /hooks) to activate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
