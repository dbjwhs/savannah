package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// TestFindRepoRoot: the worktree path rule, a `.git` directory, a `.git`
// file pointing at a main repository, and a directory under no repository.
func TestFindRepoRoot(t *testing.T) {
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "zfs")
	if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	sub := filepath.Join(repo, "module", "zfs")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	wt := filepath.Join(tmp, "zfs-ed")
	if err := os.MkdirAll(wt, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(wt, ".git"), []byte("gitdir: "+repo+"/.git/worktrees/zfs-ed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	odd := filepath.Join(tmp, "odd")
	if err := os.MkdirAll(odd, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(odd, ".git"), []byte("not a gitdir line\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	plain := filepath.Join(tmp, "plain", "dir")
	if err := os.MkdirAll(plain, 0o755); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		in   string
		root string
		repo bool
	}{
		{repo, repo, true},
		{sub, repo, true},
		{repo + "/", repo, true},
		{wt, repo, true},                         // a `git worktree add` checkout groups with its repository
		{odd, odd, true},                         // a `.git` file of another shape: the directory itself
		{plain, plain, false},                    // no repository above it
		{"/r/.savannah-worktrees/x", "/r", true}, // the path rule needs no filesystem
		{"/r/.savannah-worktrees/x/sub", "/r", true},
		{"", "", false},
	}
	for _, c := range cases {
		got := findRepoRoot(filepath.Clean(c.in))
		if c.in == "" {
			got = findRepoRoot("")
		}
		if got.root != c.root || got.repo != c.repo {
			t.Errorf("findRepoRoot(%q) = (%q, %v), want (%q, %v)", c.in, got.root, got.repo, c.root, c.repo)
		}
	}
	// The cache answers the same and survives the directory going away.
	if r, ok := repoRoot(sub); r != repo || !ok {
		t.Errorf("repoRoot(%q) = (%q, %v)", sub, r, ok)
	}
	os.RemoveAll(repo)
	if r, ok := repoRoot(sub); r != repo || !ok {
		t.Errorf("repoRoot after removal = (%q, %v), want the cached answer", r, ok)
	}
}

func TestInRepoLabelAndGroupLabel(t *testing.T) {
	home := "/Users/x"
	cases := []struct{ path, root, want string }{
		{"/Users/x/ng/zfs", "/Users/x/ng/zfs", "."},
		{"/Users/x/ng/zfs/module/zfs", "/Users/x/ng/zfs", "module/zfs"},
		{"/Users/x/ng/zfs/.savannah-worktrees/zfs-ed", "/Users/x/ng/zfs", "zfs-ed"},
		{"/Users/x/ng/zfs/.savannah-worktrees/zfs-ed/cmd", "/Users/x/ng/zfs", "zfs-ed/cmd"},
		{"/Users/x/ng/zfs-ed", "/Users/x/ng/zfs", "~/ng/zfs-ed"}, // a checkout elsewhere keeps its path
		{"/Users/x", "/Users/x", "."},
	}
	for _, c := range cases {
		if got := inRepoLabel(c.path, c.root, home); got != c.want {
			t.Errorf("inRepoLabel(%q, %q) = %q, want %q", c.path, c.root, got, c.want)
		}
	}
	if got := groupLabel("/Users/x/ng/zfs", true, home); got != "zfs" {
		t.Errorf("groupLabel repo = %q, want zfs", got)
	}
	if got := groupLabel("/Users/x/ng/notes", false, home); got != "~/ng/notes" {
		t.Errorf("groupLabel non-repo = %q, want ~/ng/notes", got)
	}
}

// groupedBoard: two repositories interleaved in ACTIVE by rank, one named
// row in LATER, and two plain tabs (one in repo a, one under no repo).
func groupedBoard(t *testing.T) model {
	t.Helper()
	m := newModel(opts{}, "savannah")
	m.width, m.height = 100, 40
	m.hooks = []session{
		{ID: "s1", Project: "/a/.savannah-worktrees/a1", State: "working", Updated: nowUnix(), ITerm: "w0t0p0:A1"},
		{ID: "s2", Project: "/b/.savannah-worktrees/b1", State: "idle", Updated: nowUnix(), ITerm: "w0t0p0:B1"},
		{ID: "s3", Project: "/a/.savannah-worktrees/a2", State: "idle", Updated: nowUnix(), ITerm: "w0t0p0:A2"},
		{ID: "s4", Project: "/a/.savannah-worktrees/plain-a", State: "waiting", Updated: nowUnix() - 50, ITerm: "w0t0p0:PA"},
		{ID: "s5", Project: "/nowhere/else", State: "idle", Updated: nowUnix() - 10, ITerm: "w0t0p0:PN"},
	}
	m.work = []workRecord{
		{Name: "a1", Worktree: "/a/.savannah-worktrees/a1", ClaudePane: "A1", Priority: "active", Rank: 10},
		{Name: "b1", Worktree: "/b/.savannah-worktrees/b1", ClaudePane: "B1", Priority: "active", Rank: 20},
		{Name: "a2", Worktree: "/a/.savannah-worktrees/a2", ClaudePane: "A2", Priority: "active", Rank: 30},
		{Name: "b2", Worktree: "/b/.savannah-worktrees/b2", ClaudePane: "B2", Priority: "later", Rank: 10},
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	return m
}

func TestGroupSessions(t *testing.T) {
	m := groupedBoard(t)
	// Ungrouped: the rank order, then LATER's named row, then the plain tabs
	// in attention order (s4 waits).
	want := []string{"a1", "b1", "a2", "b2", "#s4", "#s5"}
	if got := rowNames(m); !reflect.DeepEqual(got, want) {
		t.Fatalf("plain order = %v, want %v", got, want)
	}
	// Grouped: inside ACTIVE repo a's rows come together (a1 first, so a
	// leads), rank order kept inside the group; LATER keeps b2 first (named
	// before unnamed, b's group) and the plain tabs each in their group.
	m.group = true
	m.rebuildRows()
	want = []string{"a1", "a2", "b1", "b2", "#s4", "#s5"}
	if got := rowNames(m); !reflect.DeepEqual(got, want) {
		t.Fatalf("grouped order = %v, want %v", got, want)
	}
	// A rank change still decides which group leads: b1 to the top.
	setRank(&m, "b1", "active", 5)
	want = []string{"b1", "a1", "a2", "b2", "#s4", "#s5"}
	if got := rowNames(m); !reflect.DeepEqual(got, want) {
		t.Fatalf("grouped order after rank change = %v, want %v", got, want)
	}
}

func TestGroupHeadersAndCellsInView(t *testing.T) {
	m := groupedBoard(t)
	m.group = true
	m.rebuildRows()
	out := m.View()
	for _, want := range []string{"ACTIVE (3)", "  a (2)", "  b (1)", "LATER (3)", "  b (1)", "  /nowhere/else (1)",
		"by repository", "^G ungroup"} {
		if !strings.Contains(out, want) {
			t.Errorf("grouped view lacks %q:\n%s", want, out)
		}
	}
	// The PROJECT cell is the path inside the repository, not repo/name.
	lines := plainLines(out)
	var a1 string
	for _, ln := range lines {
		if strings.Contains(ln, " a1 ") {
			a1 = ln
		}
	}
	if a1 == "" || strings.Contains(a1, "a/a1") {
		t.Errorf("a1's row should show the in-repo label, got %q", a1)
	}
	// Group headers sit between the section header and the first row of
	// each group: ACTIVE, a, a1, a2, b, b1, LATER, b, b2, ...
	var seq []string
	for _, ln := range lines {
		switch {
		case strings.HasPrefix(ln, "ACTIVE ("), strings.HasPrefix(ln, "LATER ("):
			seq = append(seq, strings.Fields(ln)[0])
		case strings.HasPrefix(ln, "  a ("), strings.HasPrefix(ln, "  b ("), strings.HasPrefix(ln, "  /nowhere"):
			seq = append(seq, "hdr:"+strings.Fields(ln)[0])
		case strings.Contains(ln, " a1 "), strings.Contains(ln, " a2 "), strings.Contains(ln, " b1 "), strings.Contains(ln, " b2 "):
			seq = append(seq, strings.TrimSpace(strings.Fields(ln)[2]))
		}
	}
	want := []string{"ACTIVE", "hdr:a", "a1", "a2", "hdr:b", "b1", "LATER", "hdr:b", "b2", "hdr:a", "hdr:/nowhere/else"}
	if !reflect.DeepEqual(seq, want) {
		t.Errorf("sequence = %v, want %v\n%s", seq, want, out)
	}
	// Wide: the header carries the full path beside the label, and PROJECT
	// never widens to the full path (the header has it).
	m.width = 160
	out = m.View()
	if !strings.Contains(out, "  a (2)   /a") {
		t.Errorf("wide grouped header should carry the path:\n%s", out)
	}
	if c := sessionColumns(160, m.sessions, m.home, nil, true); c.paths {
		t.Errorf("grouped columns must not switch PROJECT to full paths")
	}
	// Ungrouped there are no group headers and the help says "group".
	m.group = false
	m.rebuildRows()
	out = m.View()
	if strings.Contains(out, "  a (2)") || strings.Contains(out, "by repository") || !strings.Contains(out, "^G group") {
		t.Errorf("plain view should carry no group headers:\n%s", out)
	}
}

func TestCtrlGTogglesAndRemembers(t *testing.T) {
	file := filepath.Join(t.TempDir(), "deep", "board.json")
	t.Setenv("SAVANNAH_BOARD_FILE", file)
	m := groupedBoard(t)
	cursorTo(t, &m, workRow("b1"))
	m, _ = press(m, tea.KeyCtrlG)
	if !m.group {
		t.Fatal("Ctrl-G should group")
	}
	if r, ok := m.currentRow(); !ok || r.s.Work != "b1" {
		t.Errorf("cursor should follow b1, is on %+v", r.s.Work)
	}
	if !strings.Contains(m.status, "grouped by repository") {
		t.Errorf("status = %q", m.status)
	}
	if p := loadBoardPrefs(file); p.Group != "repo" {
		t.Errorf("board file after Ctrl-G = %+v, want group repo", p)
	}
	m, _ = press(m, tea.KeyCtrlG)
	if m.group {
		t.Fatal("second Ctrl-G should ungroup")
	}
	if r, ok := m.currentRow(); !ok || r.s.Work != "b1" {
		t.Errorf("cursor should still follow b1, is on %+v", r.s.Work)
	}
	if p := loadBoardPrefs(file); p.Group != "" {
		t.Errorf("board file after the second Ctrl-G = %+v, want no group", p)
	}
	// A missing file is nothing remembered; an unwritable one is a status
	// line, and the toggle still holds for this run.
	if p := loadBoardPrefs(filepath.Join(t.TempDir(), "none.json")); p != (boardPrefs{}) {
		t.Errorf("missing board file = %+v, want zero", p)
	}
	t.Setenv("SAVANNAH_BOARD_FILE", filepath.Join(file, "not-a-dir", "board.json"))
	m, _ = press(m, tea.KeyCtrlG)
	if !m.group || !strings.Contains(m.status, "not remembered") {
		t.Errorf("unwritable board file: group=%v status=%q", m.group, m.status)
	}
}

func TestDragRefusedWhileGrouped(t *testing.T) {
	t.Setenv("SAVANNAH_BOARD_FILE", filepath.Join(t.TempDir(), "board.json"))
	m := groupedBoard(t)
	m.group = true
	m.rebuildRows()
	cursorTo(t, &m, workRow("a2"))
	next, cmd := m.dragRow(-1)
	m = next.(model)
	if cmd != nil || !strings.Contains(m.status, "Ctrl-G back") {
		t.Errorf("drag while grouped should be refused with a hint; cmd=%v status=%q", cmd != nil, m.status)
	}
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"a1", "a2", "b1", "b2", "#s4", "#s5"}) {
		t.Errorf("drag while grouped changed the order: %v", got)
	}
}

func TestParseArgsGroup(t *testing.T) {
	cases := []struct {
		args []string
		want int
	}{
		{nil, 0},
		{[]string{"--group"}, 1},
		{[]string{"--no-group"}, -1},
		{[]string{"--mesh", "--group"}, 1},
		{[]string{"--group", "--no-group"}, -1}, // the last wins
	}
	for _, c := range cases {
		o, err := parseArgs(c.args)
		if err != nil || o.group != c.want {
			t.Errorf("parseArgs(%v) = group %d err %v, want %d", c.args, o.group, err, c.want)
		}
	}
}

func TestFitHelp(t *testing.T) {
	parts := []string{"Shift-up/down move", "Enter say", "^N new", "^C quit"}
	if got := fitHelp(parts, 200); got != "Shift-up/down move  Enter say  ^N new  ^C quit" {
		t.Errorf("wide = %q", got)
	}
	if got := fitHelp(parts, 40); got != "Shift-up/down move  Enter say  ^N new" {
		t.Errorf("40 columns = %q, want the last hint dropped", got)
	}
	if got := fitHelp(parts, 10); got != "Shift-up/d" {
		t.Errorf("10 columns = %q, want the first hint cut", got)
	}
	// The work board's help line is 106 columns whole, so at 100 it loses
	// "^C quit" (Esc quits too) and nothing else; from 106 on it is whole.
	m := boardWith(t, true)
	for _, ln := range plainLines(m.View()) {
		if strings.HasPrefix(ln, "Shift-up/down move") && !strings.HasSuffix(ln, "^X rm") {
			t.Errorf("work help at 100 columns should end with ^X rm: %q", ln)
		}
	}
	m.width = 110
	for _, ln := range plainLines(m.View()) {
		if strings.HasPrefix(ln, "Shift-up/down move") && !strings.HasSuffix(ln, "^C quit") {
			t.Errorf("work help at 110 columns should be whole: %q", ln)
		}
	}
}
