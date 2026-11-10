// group.go: the board grouped by repository (Ctrl-G, --group).
//
// With many tabs across many repos the question is often "show me every
// ZFS session". Grouping keeps the two sections and the owner's rank order
// inside them, but gathers the rows of one repository together under a dim
// header line naming it (and counting them), repositories in the order
// their first row appears; the PROJECT cell then shows where inside the
// repository the tab sits ("." for its root, "dash" for a subdirectory,
// the session name for a savannah worktree), since the header already says
// which repository.
//
// A row's repository comes from its directory, never from a git
// subprocess: `<repo>/.savannah-worktrees/<name>` is that repo; otherwise
// the nearest ancestor holding `.git`, and when `.git` is a file (a `git
// worktree add` checkout) the main repository it points at. A directory
// outside any repository groups under its own path. Lookups are cached for
// the life of the process: a directory does not change its repository
// while the board runs (a `git init` under a running board shows after a
// restart).
//
// The choice is remembered in $SAVANNAH_BOARD_FILE (default
// ~/.claude/savannah/board.json) so the next board starts the same way;
// --group and --no-group on the command line win for one run. Dragging is
// refused while grouped: the drag edits the rank order, which the grouped
// view does not show in full (Ctrl-G back first). Stated limit: the
// grouping is by directory, so two checkouts of one project in two
// directories are two groups.

package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

const worktreesDir = ".savannah-worktrees"

// repoCache remembers repoRoot's answers for the life of the process.
var repoCache = struct {
	sync.Mutex
	m map[string]repoInfo
}{m: map[string]repoInfo{}}

type repoInfo struct {
	root string
	repo bool // root is a git repository (else the directory itself)
}

// repoRoot is the repository a directory belongs to, or (cleaned, false)
// when it is under none. Cached.
func repoRoot(path string) (root string, isRepo bool) {
	path = filepath.Clean(path)
	repoCache.Lock()
	defer repoCache.Unlock()
	if r, ok := repoCache.m[path]; ok {
		return r.root, r.repo
	}
	r := findRepoRoot(path)
	repoCache.m[path] = r
	return r.root, r.repo
}

// findRepoRoot is repoRoot without the cache: the worktree path rule, then
// the walk up to a `.git`.
func findRepoRoot(path string) repoInfo {
	if path == "" || path == "." {
		return repoInfo{root: path}
	}
	if i := strings.Index(path, "/"+worktreesDir+"/"); i > 0 {
		return repoInfo{root: path[:i], repo: true}
	}
	for dir := path; ; {
		fi, err := os.Stat(filepath.Join(dir, ".git"))
		if err == nil {
			if !fi.IsDir() {
				if main := worktreeMain(filepath.Join(dir, ".git")); main != "" {
					return repoInfo{root: main, repo: true}
				}
			}
			return repoInfo{root: dir, repo: true}
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return repoInfo{root: path}
		}
		dir = parent
	}
}

// worktreeMain reads a `.git` file ("gitdir: <repo>/.git/worktrees/<name>")
// and returns <repo>, or "" when the file is not that shape.
func worktreeMain(gitFile string) string {
	b, err := os.ReadFile(gitFile)
	if err != nil {
		return ""
	}
	line := strings.TrimSpace(strings.SplitN(string(b), "\n", 2)[0])
	if !strings.HasPrefix(line, "gitdir:") {
		return ""
	}
	gitdir := strings.TrimSpace(strings.TrimPrefix(line, "gitdir:"))
	if i := strings.Index(gitdir, "/.git/worktrees/"); i > 0 {
		return gitdir[:i]
	}
	return ""
}

// groupLabel names a group: the repository's base name, or the directory
// itself (with ~) when it is no repository.
func groupLabel(root string, isRepo bool, home string) string {
	if isRepo {
		return filepath.Base(root)
	}
	return tilde(root, home)
}

// inRepoLabel is the PROJECT cell of a grouped row: the path inside its
// repository ("." at the root), a savannah worktree by its name, and the
// whole path (with ~) when the directory is not under the root (a `git
// worktree add` checkout elsewhere).
func inRepoLabel(path, root, home string) string {
	path = filepath.Clean(path)
	rel, err := filepath.Rel(root, path)
	if err != nil || rel == ".." || strings.HasPrefix(rel, "../") {
		return tilde(path, home)
	}
	if strings.HasPrefix(rel, worktreesDir+"/") {
		return strings.TrimPrefix(rel, worktreesDir+"/")
	}
	return rel
}

// groupKey is what rows are gathered by: the repository root (or the
// directory itself).
func groupKey(s session) string {
	root, _ := repoRoot(s.Project)
	return root
}

// groupSessions takes sessions in board order (orderSessions) and gathers
// each section's rows by repository, repositories in the order their first
// row appears, rows inside a repository in their incoming order. Stable, so
// the owner's rank order survives inside every group.
func groupSessions(ordered []session, work []workRecord) []session {
	recs := recordMap(work)
	type key struct {
		sec  int
		root string
	}
	first := map[key]int{}
	for i, s := range ordered {
		k := key{sectionOf(s, recs), groupKey(s)}
		if _, ok := first[k]; !ok {
			first[k] = i
		}
	}
	out := append([]session{}, ordered...)
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		sa, sb := sectionOf(a, recs), sectionOf(b, recs)
		if sa != sb {
			return sa < sb
		}
		return first[key{sa, groupKey(a)}] < first[key{sb, groupKey(b)}]
	})
	return out
}

// groupHeader is the dim line above a repository's rows: its label and row
// count, plus the full path when the board is wide and the label is not
// already the path.
func groupHeader(root string, isRepo bool, n int, home string, wide bool) string {
	label := groupLabel(root, isRepo, home)
	line := colStyle.Render(fmt.Sprintf("  %s (%d)", label, n))
	if wide {
		if full := tilde(root, home); full != label {
			line += helpStyle.Render("   " + full)
		} else if !isRepo {
			line += helpStyle.Render("   no repository")
		}
	}
	return line
}

// ---- the remembered choice ------------------------------------------------------

// boardPrefs is $SAVANNAH_BOARD_FILE: what the board remembers between
// runs: the grouping (written by Ctrl-G) and whether the tabs you open by
// hand are named for you ("off" is written by hand, nothing on the board
// sets it). A writer loads the file first so one key never wipes another.
type boardPrefs struct {
	Group    string `json:"group"`              // "repo" | ""
	Autoname string `json:"autoname,omitempty"` // "off" | "" (autoname.go)
}

func boardFile() string {
	if f := os.Getenv("SAVANNAH_BOARD_FILE"); f != "" {
		return f
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".claude", "savannah", "board.json")
}

// loadBoardPrefs reads the file; a missing or unreadable file is the zero
// value (nothing remembered), never an error the board needs to show.
func loadBoardPrefs(path string) boardPrefs {
	var p boardPrefs
	if path == "" {
		return p
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return p
	}
	_ = json.Unmarshal(b, &p)
	return p
}

// saveBoardPrefs writes the file (creating its directory). The error is
// the caller's to show: the toggle still applies to this run.
func saveBoardPrefs(path string, p boardPrefs) error {
	if path == "" {
		return fmt.Errorf("no home directory for the board file")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	b, err := json.Marshal(p)
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(b, '\n'), 0o644)
}

// groupPref is the prefs value for a grouping flag.
func groupPref(group bool) string {
	if group {
		return "repo"
	}
	return ""
}
