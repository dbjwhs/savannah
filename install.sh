#!/usr/bin/env bash
# install.sh: build savannah on this machine and put it on your PATH, in one
# go. Safe to run again after every pull: each step is idempotent.
#
#   ./install.sh              # build the CLI and the board, install the hook,
#                             # link ~/.local/bin/savannah
#   ./install.sh --pull       # git pull savannah and song first (fast-forward only)
#   ./install.sh --test       # run ctest after building
#   ./install.sh --dry-run    # print every command, run none
#
# What it does, in order:
#   1. checks the tools it needs (git, cmake, a C++ compiler, python3, go) and
#      warns about the optional ones (claude on PATH for naming tabs, iTerm2
#      for the board's tabs);
#   2. clones song as a sibling if it is missing and builds libsong.a + songc
#      when they are missing (or after --pull);
#   3. configures and builds savannah (Debug unless the build dir already has
#      a type), then the board (dash/savannah-dash) when go is available;
#   4. merges the session-status hook into ~/.claude/settings.json
#      (hooks/install.py: idempotent, backs the file up);
#   5. links build/savannah into --bin DIR (default ~/.local/bin) and tells
#      you if that directory is not on your PATH.
#
# What it does not do: install git, cmake, a compiler, Go, Python or iTerm2
# (brew or apt do that), configure the mesh (savannahd, keys, mDNS), or
# restart Claude Code tabs that are already open (the hook runs fresh on
# every event, but a new hook PATH needs a new tab).
#
# Flags: --pull, --test, --dry-run, --bin DIR, --song DIR (default ../song,
# or $SONG_ROOT), --no-hook, --no-link, --release, -h/--help.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
song="${SONG_ROOT:-$here/../song}"
bin="${HOME}/.local/bin"
pull=0 test=0 dry=0 hook=1 link=1 build_type=""

usage() { sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --pull) pull=1 ;;
        --test) test=1 ;;
        --dry-run) dry=1 ;;
        --bin) bin="$2"; shift ;;
        --song) song="$2"; shift ;;
        --no-hook) hook=0 ;;
        --no-link) link=0 ;;
        --release) build_type=Release ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown flag $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\n== %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
run()  {
    printf '+ %s\n' "$*"
    [ "$dry" = 1 ] || "$@"
}
have() { command -v "$1" >/dev/null 2>&1; }

# ---- 1. tools ---------------------------------------------------------------
say "checking tools"
missing=0
for t in git cmake python3; do
    if have "$t"; then echo "  $t: $(command -v "$t")"; else echo "  $t: MISSING"; missing=1; fi
done
if have c++ || have clang++ || have g++; then
    echo "  c++: $(command -v c++ || command -v clang++ || command -v g++)"
else
    echo "  c++: MISSING (a C++20 compiler)"; missing=1
fi
if [ "$missing" = 1 ]; then
    echo "install the missing tools first (brew install git cmake python3 go; or apt)." >&2
    exit 1
fi
go_ok=0
if have go; then echo "  go: $(command -v go)"; go_ok=1
else warn "go not found: the CLI builds, the board (bare savannah) will not"; fi
if have claude; then echo "  claude: $(command -v claude)"
else warn "claude not on PATH: the board names your tabs from their first prompt instead of asking Claude"; fi
if [ "$(uname -s)" = Darwin ]; then
    if have it2 || [ -x /Applications/iTerm.app/Contents/Resources/utilities/it2 ]; then
        echo "  it2: $(command -v it2 || echo /Applications/iTerm.app/Contents/Resources/utilities/it2)"
    else
        warn "iTerm2's it2 not found: work sessions and the board's tabs need iTerm2 3.5+"
    fi
else
    warn "not macOS: the mesh works here, the board's work sessions need iTerm2"
fi
jobs="$( (sysctl -n hw.ncpu 2>/dev/null || nproc 2>/dev/null || echo 4) )"

# ---- 2. pull ---------------------------------------------------------------
if [ "$pull" = 1 ]; then
    say "pulling savannah"
    run git -C "$here" pull --ff-only
fi

# ---- 3. song ---------------------------------------------------------------
if [ -d "$song" ]; then song="$(cd "$song" && pwd)"; fi
say "song at $song"
if [ ! -d "$song" ]; then
    run git clone https://github.com/dbjwhs/song "$song"
elif [ "$pull" = 1 ]; then
    run git -C "$song" pull --ff-only
fi
if [ "$pull" = 1 ] || [ ! -f "$song/build/libsong.a" ] || [ ! -x "$song/build/songc" ]; then
    run cmake -S "$song" -B "$song/build"
    run cmake --build "$song/build" --target song songc -j "$jobs"
else
    echo "  libsong.a and songc already built"
fi

# ---- 4. savannah -----------------------------------------------------------
say "building savannah"
cfg=(-B "$here/build" -S "$here" -DSONG_ROOT="$song")
if [ -n "$build_type" ]; then
    cfg+=(-DCMAKE_BUILD_TYPE="$build_type")
elif [ ! -f "$here/build/CMakeCache.txt" ]; then
    cfg+=(-DCMAKE_BUILD_TYPE=Debug)
fi
run cmake "${cfg[@]}"
run cmake --build "$here/build" -j "$jobs"
if [ "$go_ok" = 1 ]; then
    run cmake --build "$here/build" --target dash
fi

# ---- 5. hook ---------------------------------------------------------------
if [ "$hook" = 1 ]; then
    say "installing the session hook into ~/.claude/settings.json"
    run python3 "$here/hooks/install.py"
fi

# ---- 6. link ---------------------------------------------------------------
if [ "$link" = 1 ]; then
    say "linking $bin/savannah"
    run mkdir -p "$bin"
    run ln -sfn "$here/build/savannah" "$bin/savannah"
    case ":$PATH:" in
        *":$bin:"*) ;;
        *) warn "$bin is not on your PATH; add it (export PATH=\"$bin:\$PATH\") or run $here/build/savannah" ;;
    esac
fi

# ---- 7. tests ---------------------------------------------------------------
if [ "$test" = 1 ]; then
    say "running the tests"
    run ctest --test-dir "$here/build" --output-on-failure
fi

say "done"
echo "  start the board:      savannah"
echo "  name your tabs later: savannah work autoname --dry-run"
if [ "$hook" = 1 ]; then
    echo "  tabs already open keep running the hook from its path at install time;"
    echo "  if that path changed, open a new tab to pick it up."
fi
