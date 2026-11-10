#!/usr/bin/env python3
"""install.sh in --dry-run: the steps it would run, in order, for a built
song, a missing song, --pull, --no-hook/--no-link, --release, and a bad
flag. Nothing is built, cloned or written (except the temp dirs here).

usage: test_install.py INSTALL_SH
"""
import os
import shutil
import subprocess
import sys
import tempfile

FAILS = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run(script, *args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    p = subprocess.run(["bash", script, "--dry-run", *args], capture_output=True, text=True, env=e)
    return p.returncode, p.stdout, p.stderr


def commands(out):
    """The '+ ...' lines: what a real run would execute."""
    return [l[2:] for l in out.splitlines() if l.startswith("+ ")]


def main():
    script = os.path.abspath(sys.argv[1])
    here = os.path.dirname(script)
    tmp = tempfile.mkdtemp(prefix="savannah-install-test-")
    try:
        # A song that is already built: no clone, no song build.
        built = os.path.join(tmp, "song")
        os.makedirs(os.path.join(built, "build"))
        open(os.path.join(built, "build", "libsong.a"), "w").close()
        songc = os.path.join(built, "build", "songc")
        open(songc, "w").close()
        os.chmod(songc, 0o755)
        bin_dir = os.path.join(tmp, "bin")

        rc, out, err = run(script, "--song", built, "--bin", bin_dir)
        cmds = commands(out)
        check(rc == 0, f"dry run exits 0 ({rc}, {err.strip()[:200]})")
        check(not any("clone" in c or "--target song" in c for c in cmds),
              f"a built song is neither cloned nor rebuilt ({cmds})")
        check(any(c.startswith("cmake -B") and f"-DSONG_ROOT={built}" in c for c in cmds),
              f"savannah is configured against --song ({cmds})")
        check(any(c.startswith("cmake --build") and "-j" in c and "--target" not in c for c in cmds),
              "savannah is built")
        check(any(c.endswith("hooks/install.py") for c in cmds), "the hook is installed")
        check(any(c == f"ln -sfn {here}/build/savannah {bin_dir}/savannah" for c in cmds),
              f"build/savannah is linked into --bin ({cmds})")
        check(f"{bin_dir} is not on your PATH" in err, "a --bin off the PATH is pointed out")
        check(not os.path.exists(bin_dir), "dry run creates nothing")
        order = [i for i, c in enumerate(cmds) if c.startswith("cmake -B")] + \
                [i for i, c in enumerate(cmds) if c.endswith("hooks/install.py")] + \
                [i for i, c in enumerate(cmds) if c.startswith("ln -sfn")]
        check(order == sorted(order), f"configure, then hook, then link ({cmds})")
        has_go = shutil.which("go") is not None
        check(any("--target dash" in c for c in cmds) == has_go,
              f"the board is built exactly when go is on PATH (go={has_go})")

        # A song that is not there: cloned, then built; --pull pulls both.
        missing = os.path.join(tmp, "nosong")
        rc, out, err = run(script, "--song", missing, "--bin", bin_dir, "--pull")
        cmds = commands(out)
        check(rc == 0, "dry run with a missing song exits 0")
        check(any(c == f"git clone https://github.com/dbjwhs/song {missing}" for c in cmds), f"a missing song is cloned ({cmds})")
        check(any(c == f"cmake --build {missing}/build --target song songc" or
                  c.startswith(f"cmake --build {missing}/build --target song songc -j") for c in cmds),
              f"song is built after the clone ({cmds})")
        check(any(c == f"git -C {here} pull --ff-only" for c in cmds), "--pull pulls savannah")
        check(not any(c == f"git -C {missing} pull --ff-only" for c in cmds), "--pull does not pull a song it just cloned")
        check(not os.path.exists(missing), "dry run clones nothing")

        # A built song with --pull is pulled and rebuilt.
        rc, out, err = run(script, "--song", built, "--bin", bin_dir, "--pull")
        cmds = commands(out)
        check(any(c == f"git -C {built} pull --ff-only" for c in cmds), "--pull pulls song")
        check(any("--target song songc" in c for c in cmds), "--pull rebuilds song")

        # --no-hook, --no-link, --release, --test.
        rc, out, err = run(script, "--song", built, "--bin", bin_dir, "--no-hook", "--no-link", "--release", "--test")
        cmds = commands(out)
        check(rc == 0 and not any("install.py" in c or c.startswith("ln ") for c in cmds),
              f"--no-hook --no-link skip the hook and the link ({cmds})")
        check(any("-DCMAKE_BUILD_TYPE=Release" in c for c in cmds), "--release configures Release")
        check(any(c.startswith("ctest --test-dir") for c in cmds), "--test runs ctest")

        # A bad flag: usage, exit 2. --help: usage, exit 0.
        rc, out, err = run(script, "--bogus")
        check(rc == 2 and "unknown flag --bogus" in err, f"a bad flag exits 2 with a message ({rc}, {err[:80]!r})")
        p = subprocess.run(["bash", script, "--help"], capture_output=True, text=True)
        check(p.returncode == 0 and "./install.sh --pull" in p.stdout, "--help prints the usage")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILS:
        print(f"\n{len(FAILS)} check(s) failed")
        return 1
    print("\nall install.sh checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
