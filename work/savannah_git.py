# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# savannah_git - the git side of local work sessions (savannah_work.py is
# the entry point; this is a flat sibling module).
#
# run_git (every git call, logged), git's own failure line picked out of
# its chatter, the main worktree root of a directory, and the worktree of a
# session: add_worktree (<repo>/.savannah-worktrees/<name> on work/<name>),
# remove_worktree, and copy_worktree_includes for the gitignored files
# <repo>/.worktreeinclude names.
#
# Imports: the standard library, savannah_log and savannah_core.

import collections
import glob
import os
import shutil
import subprocess
import time

from savannah_log import log_debug, log_verbose, log_warn
import savannah_core
from savannah_core import oserr, UserError

WORKTREE_INCLUDE = ".worktreeinclude"   # paths/globs to copy into every new worktree


def run_git(args):
    t0 = time.time()
    try:
        p = subprocess.run(["git"] + args, capture_output=True, text=True)
    except FileNotFoundError:
        log_warn("git not found")
        raise UserError("git not found on PATH")
    except OSError as e:
        log_warn("git failed to start", error=str(oserr(e)))
        raise UserError(f"cannot run git: {oserr(e)}")
    log_debug("git", args=list(args), rc=p.returncode, ms=int((time.time() - t0) * 1000))
    if p.returncode != 0 and p.stderr:
        log_verbose("git stderr", args=list(args)[:3], stderr=p.stderr[:300])
    return p


def git_fail_message(p, what):
    """Prefer git's own fatal:/error: line over trailing hint: chatter."""
    lines = [ln for ln in p.stderr.strip().splitlines() if ln.strip()]
    pick = ""
    for ln in lines:
        if ln.startswith("fatal:") or ln.startswith("error:"):
            pick = ln
    if not pick:
        for ln in lines:
            if not ln.startswith("hint:"):
                pick = ln
    if not pick:
        pick = lines[-1] if lines else f"rc {p.returncode}"
    return f"{what} failed: {pick}"


def git_branch_exists(repo, branch):
    p = run_git(["-C", repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"])
    return p.returncode == 0


def resolve_repo_root(d):
    """The MAIN worktree root of the repository containing d (finding 12).

    git-common-dir is the shared .git of the main worktree even when d is a
    linked worktree or a subdirectory, so a work session can never nest inside
    another session's worktree. Falls back to --show-toplevel when the common
    dir is not a plain ".git" (separate git dir, bare repo).
    """
    p = run_git(["-C", d, "rev-parse", "--git-common-dir"])
    if p.returncode != 0:
        raise UserError(f"not a git repository: {d}")
    common = p.stdout.strip()
    if common and not os.path.isabs(common):
        common = os.path.join(d, common)
    common = os.path.realpath(common)
    if os.path.basename(common) == ".git":
        root = os.path.dirname(common)
    else:
        p = run_git(["-C", d, "rev-parse", "--show-toplevel"])
        if p.returncode != 0 or not p.stdout.strip():
            raise UserError(f"cannot find the worktree root of {d}")
        root = os.path.realpath(p.stdout.strip())
    # Keep the caller's spelling when it already names the root (realpath may
    # only differ by a symlinked prefix such as /var -> /private/var).
    if os.path.realpath(d) == root:
        return d
    return root


def copy_worktree_includes(repo, worktree):
    """Copy what <repo>/.worktreeinclude names into a fresh worktree: one path
    or glob per line relative to the repo root (`.env`, `config/*.local`,
    `**/.secrets`), `#` comments and blank lines ignored. Meant for the
    gitignored files a worktree needs to run. Not gitignore syntax: no
    negation, no trailing-slash rules. Returns the relative paths copied;
    an existing destination is left alone."""
    try:
        with open(os.path.join(repo, WORKTREE_INCLUDE), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    root = os.path.realpath(repo)
    copied = []
    for raw in lines:
        pat = raw.strip()
        if not pat or pat.startswith("#"):
            continue
        for src in sorted(glob.glob(os.path.join(root, pat.lstrip("/")), recursive=True)):
            rel = os.path.relpath(os.path.realpath(src) if not os.path.islink(src) else src, root)
            top = rel.split(os.sep)[0]
            if rel.startswith("..") or top in (".git", savannah_core.WORKTREES_SUBDIR):
                continue
            dst = os.path.join(worktree, rel)
            if os.path.lexists(dst):
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, dst, symlinks=True)
                else:
                    shutil.copy2(src, dst, follow_symlinks=False)
            except OSError as e:
                raise UserError(f"{WORKTREE_INCLUDE}: cannot copy {rel}: {oserr(e)}")
            copied.append(rel)
    return copied


Worktree = collections.namedtuple("Worktree", "repo worktree branch made made_branch reattached")
Worktree.__doc__ = """Where a new session runs and what `new` made for it, so a rollback
knows what is its own to undo: made = the git worktree was added,
made_branch = its branch was created here (not reattached, not --branch)."""


def add_worktree(repo, name, branch):
    """The git worktree of a new session: <main root>/.savannah-worktrees/<name>
    on the given existing local branch, else on work/<name> (created, or
    reattached when an earlier rm left it: rm keeps the branch, it is your
    work, and a new session of the same name picks it up where it was left).
    -> Worktree. Raises UserError with nothing left behind: a failed
    `git worktree add` makes neither the worktree nor the branch."""
    repo = resolve_repo_root(repo)
    base = os.path.join(repo, savannah_core.WORKTREES_SUBDIR)
    worktree = os.path.join(base, name)
    try:
        os.makedirs(base, exist_ok=True)
    except OSError as e:
        raise UserError(f"cannot create {base}: {oserr(e)}")
    made_branch = False
    reattached = False
    if branch:
        if not git_branch_exists(repo, branch):
            raise UserError(f"branch {branch} does not exist locally")
        p = run_git(["-C", repo, "worktree", "add", worktree, branch])
    else:
        branch = f"work/{name}"
        if git_branch_exists(repo, branch):
            reattached = True
            p = run_git(["-C", repo, "worktree", "add", worktree, branch])
        else:
            p = run_git(["-C", repo, "worktree", "add", "-b", branch, worktree])
            made_branch = p.returncode == 0
    if p.returncode != 0:
        raise UserError(git_fail_message(p, "git worktree add"))
    log_debug("worktree added", name=name, worktree=worktree, branch=branch,
              reattached=reattached, new_branch=made_branch)
    return Worktree(repo, worktree, branch, True, made_branch, reattached)


def current_branch(d):
    """`git branch --show-current` in d, "" on any failure (informational
    only: an adopted session never checks anything out)."""
    try:
        p = run_git(["-C", d, "branch", "--show-current"])
    except UserError:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def remove_worktree(repo, worktree, force):
    """`git worktree remove` for rm, or a prune when the directory is already
    gone by hand. A dirty worktree without force is a UserError that says
    how to go on."""
    if os.path.isdir(worktree):
        args = ["-C", repo, "worktree", "remove"]
        if force:
            args.append("--force")
        p = run_git(args + [worktree])
        if p.returncode != 0:
            hint = "" if force else " (dirty? pass --force to discard, or --keep-worktree)"
            raise UserError(git_fail_message(p, "git worktree remove") + hint)
    else:
        run_git(["-C", repo, "worktree", "prune"])  # dir already gone by hand
