#!/usr/bin/env python3
# MIT License
# Copyright (c) 2026 Dennis B Jones
#
# Review-mode test: drives `work new --mode review [--target T]` and `work
# mode` in work/savannah_work.py against a temp SAVANNAH_WORK_DIR, the fake
# it2 (tools/fake_it2.py) via SAVANNAH_IT2 and two throwaway git repos: a
# bare "origin" that holds a pull request ref (refs/pull/7/head, as GitHub
# publishes one) and a branch only it has, and a clone of it. No real
# iTerm2, no claude, no network, stdlib only; git is isolated from the
# user's config. Without git on PATH the test says so and exits 0. Usage:
#   test_review.py <path/to/savannah_work.py> <path/to/fake_it2.py>

import os
import shlex
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _support import Harness, check, finish, load_module  # noqa: E402


class ReviewHarness(Harness):
    """The harness plus origin and its clone. The sections run in order
    over one world. pr and remote_only are the commits a review of the pull
    request and of origin's own branch must check out."""

    def __init__(self, module, fake):
        super().__init__(module, fake, prefix="savannah-review-test-")
        self.isolate_git()
        self.origin = os.path.join(self.tmp, "origin.git")
        self.repo = os.path.realpath(os.path.join(self.tmp, "repo"))
        self.pr = self.remote_only = self.local = ""

    def must(self, repo, *args):
        r = self.git(repo, *args)
        check(r.returncode == 0, f"git {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout.strip()

    def commit(self, repo, text):
        with open(os.path.join(repo, "file"), "a") as f:
            f.write(text + "\n")
        self.must(repo, "add", "file")
        self.must(repo, "commit", "-q", "-m", text)
        return self.must(repo, "rev-parse", "HEAD")

    def build(self):
        seed = os.path.join(self.tmp, "seed")
        os.makedirs(seed)
        self.must(seed, "init", "-q")
        self.must(seed, "symbolic-ref", "HEAD", "refs/heads/main")
        self.commit(seed, "init")
        r = subprocess.run(["git", "clone", "-q", "--bare", seed, self.origin], capture_output=True, text=True,
                           env=self.env)
        check(r.returncode == 0, f"git clone --bare: {r.stderr.strip()}")
        r = subprocess.run(["git", "clone", "-q", self.origin, self.repo], capture_output=True, text=True,
                           env=self.env)
        check(r.returncode == 0, f"git clone: {r.stderr.strip()}")
        # The pull request and origin's own branch arrive after the clone,
        # so the clone has seen neither.
        self.must(seed, "checkout", "-q", "-b", "pr-work")
        self.pr = self.commit(seed, "the pull request")
        self.must(seed, "push", "-q", self.origin, "pr-work:refs/pull/7/head")
        self.must(seed, "checkout", "-q", "-b", "theirs", "main")
        self.remote_only = self.commit(seed, "their branch")
        self.must(seed, "push", "-q", self.origin, "theirs:refs/heads/theirs")
        self.must(self.repo, "checkout", "-q", "-b", "mine")
        self.local = self.commit(self.repo, "my branch")
        self.must(self.repo, "checkout", "-q", "main")

    def wt_of(self, name):
        return os.path.join(self.repo, ".savannah-worktrees", name)

    def head(self, d):
        return self.must(d, "rev-parse", "HEAD")

    def typed(self, pane):
        """The line typed into that pane by `session run`, "" when none."""
        for c in self.calls():
            if c[:5] == ["session", "run", "--session", pane, "--"]:
                return c[-1]
        return ""

    def refused(self, args, needle, label, rc=1):
        p = self.run(args)
        check(p.returncode == rc and needle in p.stderr,
              f"{label}: exit {rc} naming {needle!r} (got {p.returncode}, {p.stderr.strip()!r})")


def test_units(h):
    sw = load_module(h.module)
    g = sw.savannah_git
    for text, want, prompt in (
            ("", ("", ""), "/code-review"),
            ("  7 ", ("pr", "7"), "/code-review 7"),
            ("#2353", ("pr", "2353"), "/code-review 2353"),
            ("https://github.com/o/r/pull/77", ("pr", "77"), "/code-review 77"),
            ("https://github.com/o/r/pull/77/files#diff", ("pr", "77"), "/code-review 77"),
            ("feature/x", ("branch", "feature/x"), "/code-review feature/x"),
            ("pull/9", ("branch", "pull/9"), "/code-review pull/9"),
            ("v2", ("branch", "v2"), "/code-review v2")):
        check(g.review_target(text) == want, f"review_target({text!r}) = {g.review_target(text)}, want {want}")
        check(g.review_prompt(text) == prompt, f"review_prompt({text!r}) = {g.review_prompt(text)!r}")
    core = sw.savannah_core
    old = core.normalize_record({"name": "x", "kind": "nonsense", "target": 5}, "x")
    check(old["kind"] == "coding" and old["target"] == "", f"an odd kind reads coding, an odd target empty ({old})")
    check(core.normalize_record({}, "y")["kind"] == "coding", "a record from before kinds is coding")


def test_review_pr(h):
    """A pull request: fetched from origin, checked out detached, reviewed."""
    j = h.run_json(["new", "rev-pr", "--repo", h.repo, "--mode", "review", "--target", "#7", "--json"])
    wt = h.wt_of("rev-pr")
    check(j.get("kind") == "review" and j.get("target") == "#7", f"the record is a review of what was typed ({j})")
    check(j.get("worktree") == wt and j.get("branch") == "" and j.get("prompt") == "/code-review 7",
          f"worktree, no branch, the /code-review prompt ({j})")
    check(os.path.isdir(wt) and h.head(wt) == h.pr, "the worktree is at the pull request's commit")
    check(h.must(wt, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD", "the worktree is detached")
    check(h.git(h.repo, "rev-parse", "--verify", "-q", "refs/heads/work/rev-pr").returncode != 0,
          "a review makes no work/<name> branch")
    line = h.typed("PANE-C-1")
    check(line == f" cd {shlex.quote(wt)} && claude -n rev-pr {shlex.quote('/code-review 7')}",
          f"claude starts on /code-review 7 in the worktree ({line!r})")
    j = h.run_json(["new", "rev-url", "--repo", h.repo, "--mode", "review",
                    "--target", "https://github.com/o/r/pull/7/files", "--json"])
    check(h.head(h.wt_of("rev-url")) == h.pr and j.get("prompt") == "/code-review 7",
          f"a pull request URL is the same pull request ({j})")
    check(j.get("target") == "https://github.com/o/r/pull/7/files", "the target is kept as typed")


def test_review_branch(h):
    j = h.run_json(["new", "rev-mine", "--repo", h.repo, "--mode", "review", "--target", "mine", "--json"])
    check(h.head(h.wt_of("rev-mine")) == h.local and j.get("prompt") == "/code-review mine",
          f"a local branch is reviewed at its tip ({j})")
    check(h.must(h.repo, "rev-parse", "--abbrev-ref", "HEAD") == "main", "the repo's own checkout is not touched")
    # Detached, so the branch stays free: a coding session can still take it.
    j = h.run_json(["new", "rev-theirs", "--repo", h.repo, "--mode", "review", "--target", "theirs", "--json"])
    check(h.head(h.wt_of("rev-theirs")) == h.remote_only,
          "a branch only origin has is fetched and reviewed at its tip")
    check(j.get("branch") == "", "and no local branch is recorded for it")


def test_review_in_place(h):
    """No target, or --no-worktree: the review runs in the repo itself."""
    h.clear_log(keep_state=True)
    j = h.run_json(["new", "rev-here", "--repo", h.repo, "--mode", "review", "--json"])
    check(j.get("worktree") == h.repo and j.get("prompt") == "/code-review" and j.get("target") == "",
          f"no target: the current changes, in the repo ({j})")
    check(not os.path.exists(h.wt_of("rev-here")), "no worktree is made for it")
    j = h.run_json(["new", "rev-flat", "--repo", h.repo, "--mode", "review", "--target", "7", "--no-worktree",
                    "--json"])
    check(j.get("worktree") == h.repo and j.get("prompt") == "/code-review 7" and j.get("kind") == "review",
          f"--no-worktree: the target is reviewed from the repo as it is ({j})")
    j = h.run_json(["new", "rev-own", "--repo", h.repo, "--mode", "review", "--target", "mine",
                    "--prompt", "read src/ and tell me what worries you", "--json"])
    check(j.get("prompt") == "read src/ and tell me what worries you" and h.head(h.wt_of("rev-own")) == h.local,
          f"--prompt replaces /code-review, the target is still checked out ({j})")


def test_refusals(h):
    h.refused(["new", "rev-none", "--repo", h.repo, "--mode", "review", "--target", "no-such-branch"],
              "cannot find branch no-such-branch", "a branch nobody has")
    h.refused(["new", "rev-404", "--repo", h.repo, "--mode", "review", "--target", "404"],
              "cannot fetch pull request 404", "a pull request origin does not have")
    for name in ("rev-none", "rev-404"):
        check(not os.path.exists(os.path.join(h.workroot, name)) and not os.path.exists(h.wt_of(name)),
              f"{name}: a refused review leaves no record and no worktree")
    h.refused(["new", "x1", "--repo", h.repo, "--target", "7"], "--mode review", "--target without review", rc=2)
    h.refused(["new", "x2", "--repo", h.repo, "--mode", "review", "--target", "7", "--branch", "mine"],
              "not a --branch", "a review with --branch", rc=2)
    h.refused(["new", "x3", "--repo", h.repo, "--mode", "audit"], "invalid choice", "an unknown mode", rc=2)
    # A coding session is what it always was.
    j = h.run_json(["new", "code", "--repo", h.repo, "--json"])
    check(j.get("kind") == "coding" and j.get("target") == "" and j.get("branch") == "work/code",
          f"the default mode is coding, on its own branch ({j})")


def test_mode_verb(h):
    check(h.run_json(["mode", "code", "--json"]) == {"name": "code", "kind": "coding"}, "mode reads the kind")
    check(h.run_json(["mode", "code", "review", "--json"]) == {"name": "code", "kind": "review"}, "mode sets it")
    rec = h.record("code")
    check(rec["kind"] == "review" and rec["branch"] == "work/code" and rec["priority"] == "active",
          f"only the kind changes ({rec})")
    p = h.run(["mode", "rev-pr"])
    check(p.returncode == 0 and p.stdout.strip() == "rev-pr: review", f"the plain form prints it ({p.stdout!r})")
    h.run_json(["mode", "code", "coding", "--json"])
    check(h.record("code")["kind"] == "coding", "and back")
    h.refused(["mode", "code", "audit"], "invalid choice", "an unknown kind", rc=2)
    h.refused(["mode", "nobody"], "no such work session", "an unknown session")
    kinds = {e["name"]: e["kind"] for e in h.run_json(["ls", "--json"])}
    check(kinds.get("rev-pr") == "review" and kinds.get("code") == "coding", f"ls --json carries the kind ({kinds})")
    # A record written before kinds existed reads as coding, everywhere.
    rec = h.record("code")
    del rec["kind"], rec["target"]
    h.write_record("code", rec)
    check(h.run_json(["mode", "code", "--json"]).get("kind") == "coding", "a record with no kind is coding")


def test_rm(h):
    wt = h.wt_of("rev-pr")
    p = h.run(["rm", "rev-pr"])
    check(p.returncode == 0 and not os.path.exists(wt), f"rm removes a review's worktree ({p.stderr.strip()})")
    check(not os.path.exists(os.path.join(h.workroot, "rev-pr", "record.json")), "and its record")


SECTIONS = (
    test_units,
    test_review_pr,
    test_review_branch,
    test_review_in_place,
    test_refusals,
    test_mode_verb,
    test_rm,
)


def main():
    if len(sys.argv) != 3:
        print("usage: test_review.py <savannah_work.py> <fake_it2.py>")
        return 2
    if shutil.which("git") is None:
        print("test_review: git not found, skipped")
        return 0
    h = ReviewHarness(sys.argv[1], sys.argv[2])
    h.build()
    for section in SECTIONS:
        section(h)
    return finish("test_review")


if __name__ == "__main__":
    sys.exit(main())
