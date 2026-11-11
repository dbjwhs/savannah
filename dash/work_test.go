package main

import (
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// boardWith returns a work-mode board of 100x30 with one live work session
// ("live": a hook session in its worktree, tagged by the join through its
// Claude pane), one offline record ("parked") and, when plain is set, one
// plain hook session ("s2") that belongs to no work record.
func boardWith(t *testing.T, plain bool) model {
	t.Helper()
	m := newModel(opts{}, "savannah")
	m.width, m.height = 100, 30
	hooks := []session{
		{ID: "s1", Project: "/r/.savannah-worktrees/live", State: "working",
			Updated: nowUnix(), ITerm: "w0t0p0:UUID-L"},
	}
	if plain {
		hooks = append(hooks, session{ID: "s2", Project: "/other", State: "idle",
			Updated: nowUnix(), ITerm: "w0t0p0:UUID-P"})
	}
	work := []workRecord{
		{Name: "live", Worktree: "/r/.savannah-worktrees/live", ClaudePane: "UUID-L"},
		{Name: "parked", Worktree: "/r/.savannah-worktrees/parked", ClaudePane: "P"},
	}
	m.hooks, m.work = hooks, work
	m.sessions = joinWork(hooks, work)
	m.rebuildRows()
	return m
}

func workRow(name string) func(row) bool {
	return func(r row) bool { return r.kind == rowSession && r.s.Work == name }
}

func hookRow(id string) func(row) bool {
	return func(r row) bool { return r.kind == rowSession && r.s.Work == "" && r.s.ID == id }
}

// cursorTo moves the cursor to the first row the predicate accepts (row
// order is the session sort's business, not these tests').
func cursorTo(t *testing.T, m *model, match func(row) bool) {
	t.Helper()
	for i, r := range m.rows {
		if match(r) {
			m.cursor = i
			return
		}
	}
	t.Fatalf("no matching row among %+v", m.rows)
}

func press(m model, k tea.KeyType) (model, tea.Cmd) {
	next, cmd := m.Update(tea.KeyMsg{Type: k})
	return next.(model), cmd
}

// typeText sends s one key at a time, the way the terminal would.
func typeText(m model, s string) model {
	for _, r := range s {
		k := tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}}
		if r == ' ' {
			k = tea.KeyMsg{Type: tea.KeySpace, Runes: []rune{r}}
		}
		next, _ := m.Update(k)
		m = next.(model)
	}
	return m
}

func TestParseArgs(t *testing.T) {
	cases := []struct {
		args []string
		want opts
		bad  bool
	}{
		{nil, opts{}, false},
		{[]string{"--mesh"}, opts{mesh: true}, false},
		{[]string{"--mesh", "--key", "k"}, opts{mesh: true, key: "k"}, false},
		{[]string{"dbj-mac", "--addr", "127.0.0.1:1", "--key", "k"},
			opts{pin: "dbj-mac", mesh: true, addr: "127.0.0.1:1", key: "k"}, false},
		{[]string{"--no-mesh"}, opts{}, false},
		{[]string{"--sessions"}, opts{}, false},
		{[]string{"-h"}, opts{help: true}, false},
		{[]string{"--help"}, opts{help: true}, false},
		{[]string{"--bogus"}, opts{}, true},
		{[]string{"--key"}, opts{}, true},
		{[]string{"--mesh", "--addr"}, opts{}, true},
	}
	for _, c := range cases {
		got, err := parseArgs(c.args)
		if (err != nil) != c.bad {
			t.Errorf("parseArgs(%v): err = %v, want error %v", c.args, err, c.bad)
			continue
		}
		if !c.bad && got != c.want {
			t.Errorf("parseArgs(%v) = %+v, want %+v", c.args, got, c.want)
		}
	}
}

func TestNewModel(t *testing.T) {
	m := newModel(opts{}, "savannah")
	if m.mesh || m.bin != "savannah" || !m.input.Focused() || m.mode != modeBoard {
		t.Errorf("default model: mesh=%v bin=%q focused=%v mode=%d", m.mesh, m.bin, m.input.Focused(), m.mode)
	}
	p := newModel(opts{pin: "dbj-mac", mesh: true, addr: "h:1"}, "savannah")
	if len(p.nodes) != 1 || p.nodes[0] != "dbj-mac" || !p.mesh {
		t.Errorf("pinned model: nodes=%v mesh=%v", p.nodes, p.mesh)
	}
}

func TestIt2Argv(t *testing.T) {
	t.Setenv("SAVANNAH_IT2", "/x/it2 --flag")
	if got := it2Argv(); !reflect.DeepEqual(got, []string{"/x/it2", "--flag"}) {
		t.Errorf("it2Argv with SAVANNAH_IT2 = %v", got)
	}
	t.Setenv("SAVANNAH_IT2", "")
	if got := it2Argv(); len(got) != 1 || !strings.HasSuffix(got[0], "it2") {
		t.Errorf("it2Argv default = %v, want one element ending in it2", got)
	}
}

func TestWorkNewArgs(t *testing.T) {
	repos := reposInfo{Dir: "/hub", Repos: []string{"savannah", "song", "sonar"},
		Scratch: "/hub/.savannah-scratch"}
	home := "/home/me"
	cases := []struct {
		label, repo, prompt string
		worktree            bool
		repos               reposInfo
		want                []string
	}{
		{"listed name", "savannah", "", true, repos,
			[]string{"work", "new", "a", "--repo=/hub/savannah", "--json"}},
		{"unique prefix", "sav", "", true, repos,
			[]string{"work", "new", "a", "--repo=/hub/savannah", "--json"}},
		{"ambiguous prefix is a path", "so", "", true, repos,
			[]string{"work", "new", "a", "--repo=so", "--json"}},
		{"scratch forces no worktree", "scratch", "", true, repos,
			[]string{"work", "new", "a", "--repo=/hub/.savannah-scratch", "--no-worktree", "--json"}},
		{"tilde expands", "~/code/x", "", true, repos,
			[]string{"work", "new", "a", "--repo=/home/me/code/x", "--json"}},
		{"worktree off", "/abs/x", "", false, repos,
			[]string{"work", "new", "a", "--repo=/abs/x", "--no-worktree", "--json"}},
		{"dash-leading prompt", "song", "--continue", true, repos,
			[]string{"work", "new", "a", "--repo=/hub/song", "--prompt=--continue", "--json"}},
		{"no repos dir: typed text", "song", "fix it", true, reposInfo{},
			[]string{"work", "new", "a", "--repo=song", "--prompt=fix it", "--json"}},
	}
	for _, c := range cases {
		got := workNewArgs("a", "", c.repo, c.prompt, c.worktree, c.repos, home)
		if !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s: workNewArgs = %q, want %q", c.label, got, c.want)
		}
	}
	got := workNewArgs("fix-the-parser", "Fix the parser", "song", "go", true, repos, home)
	want := []string{"work", "new", "fix-the-parser", "--repo=/hub/song", "--title=Fix the parser", "--prompt=go", "--json"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("titled: workNewArgs = %q, want %q", got, want)
	}
}

func TestCtrlNDialogWordsBecomeTitle(t *testing.T) {
	m := boardWith(t, false)
	m, _ = press(m, tea.KeyCtrlN)
	m = typeText(m, "Fix the parser")
	if out := m.View(); !strings.Contains(out, "name fix-the-parser, titled as typed") {
		t.Errorf("the Name hint should show the derived name:\n%s", out)
	}
	m, _ = press(m, tea.KeyTab)
	m = typeText(m, "/tmp/repo")
	m, cmd := press(m, tea.KeyEnter)
	if cmd == nil || !m.nd.busy || m.nd.err != "" {
		t.Fatalf("words in Name: cmd=%v busy=%v err=%q", cmd != nil, m.nd.busy, m.nd.err)
	}
	if nameHint("???") == "" || nameHint("fix-parser") != "" || nameHint("") != "" {
		t.Errorf("nameHint: %q %q %q", nameHint("???"), nameHint("fix-parser"), nameHint(""))
	}
}

func TestRepoMatches(t *testing.T) {
	repos := reposInfo{Dir: "/hub", Repos: []string{"savannah", "song"}, Scratch: "/hub/.savannah-scratch"}
	if got := repoMatches("", repos, 8); !reflect.DeepEqual(got, []string{"savannah", "song", "scratch"}) {
		t.Errorf("empty prefix = %v", got)
	}
	if got := repoMatches("s", repos, 2); !reflect.DeepEqual(got, []string{"savannah", "song"}) {
		t.Errorf("capped = %v", got)
	}
	if got := repoMatches("zz", repos, 8); len(got) != 0 {
		t.Errorf("no match = %v", got)
	}
}

func TestBoardHeaderByMode(t *testing.T) {
	m := boardWith(t, false)
	out := m.View()
	for _, want := range []string{"savannah  work", "2 sessions", "LOCAL SESSIONS", "^N new", "^F fade", "^B open", "^O out"} {
		if !strings.Contains(out, want) {
			t.Errorf("work board missing %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "MESH WORKERS") || strings.Contains(out, "worker") {
		t.Errorf("work board must not show the mesh section:\n%s", out)
	}
	mm := newModel(opts{mesh: true}, "savannah")
	mm.width = 100
	out = mm.View()
	for _, want := range []string{"savannah  mesh", "0 workers", "MESH WORKERS", "discovering nodes"} {
		if !strings.Contains(out, want) {
			t.Errorf("mesh board missing %q:\n%s", want, out)
		}
	}
	pinned := newModel(opts{pin: "dbj-mac", mesh: true}, "savannah")
	pinned.width = 100
	if out := pinned.View(); !strings.Contains(out, "savannah  dbj-mac") || !strings.Contains(out, "no workers yet") {
		t.Errorf("pinned board header:\n%s", out)
	}
}

func TestEnterOnWorkRow(t *testing.T) {
	m := boardWith(t, true)
	cursorTo(t, &m, workRow("live"))
	m, cmd := press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "opening live") {
		t.Errorf("empty Enter: cmd=%v status=%q", cmd != nil, m.status)
	}
	m = typeText(m, "run the tests")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "saying to live") {
		t.Errorf("Enter with text: cmd=%v status=%q", cmd != nil, m.status)
	}
	if m.input.Value() != "" {
		t.Errorf("the prompt line should be cleared after say: %q", m.input.Value())
	}
	// Tab is the raw jump, even on a work row.
	if _, cmd := press(m, tea.KeyTab); cmd == nil {
		t.Error("Tab on a work row should produce a jump command")
	}
	// The offline record reopens through the same open command.
	cursorTo(t, &m, workRow("parked"))
	if m2, cmd := press(m, tea.KeyEnter); cmd == nil || !strings.Contains(m2.status, "opening parked") {
		t.Errorf("Enter on offline row: cmd=%v status=%q", cmd != nil, m2.status)
	}
	// A plain tab jumps; typed text is not sent to it.
	cursorTo(t, &m, hookRow("s2"))
	m = typeText(m, "hello")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "plain tab") {
		t.Errorf("Enter with text on a plain tab: cmd=%v status=%q", cmd != nil, m.status)
	}
}

func TestCtrlXTwoPress(t *testing.T) {
	m := boardWith(t, true)
	cursorTo(t, &m, workRow("parked"))
	m, cmd := press(m, tea.KeyCtrlX)
	if cmd != nil {
		t.Error("the first Ctrl-X must not run anything")
	}
	if m.pendingRm != "parked" || !strings.Contains(m.status, "parked") || !strings.Contains(m.status, "again") {
		t.Errorf("first press: pending=%q status=%q", m.pendingRm, m.status)
	}
	if m2, cmd := press(m, tea.KeyCtrlX); cmd == nil || m2.pendingRm != "" {
		t.Errorf("second press: cmd=%v pending=%q", cmd != nil, m2.pendingRm)
	}
	// Any other key withdraws the question: a cursor move...
	if m2, _ := press(m, tea.KeyDown); m2.pendingRm != "" {
		t.Error("a cursor move must withdraw the pending rm")
	}
	if m2, _ := press(m, tea.KeyUp); m2.pendingRm != "" {
		t.Error("a cursor move must withdraw the pending rm")
	}
	// ...or a typed character, after which Ctrl-X asks afresh.
	m = typeText(m, "x")
	if m.pendingRm != "" {
		t.Error("typing must withdraw the pending rm")
	}
	if _, cmd := press(m, tea.KeyCtrlX); cmd != nil {
		t.Error("Ctrl-X after a withdrawal must ask again, not remove")
	}
	// The live work row asks the same way; a plain hook row dismisses at once.
	cursorTo(t, &m, workRow("live"))
	if m2, cmd := press(m, tea.KeyCtrlX); cmd != nil || m2.pendingRm != "live" {
		t.Errorf("live work row: cmd=%v pending=%q", cmd != nil, m2.pendingRm)
	}
	cursorTo(t, &m, hookRow("s2"))
	if m2, cmd := press(m, tea.KeyCtrlX); cmd == nil || !strings.Contains(m2.status, "dismissing") {
		t.Errorf("plain hook row: cmd=%v status=%q", cmd != nil, m2.status)
	}
}

func TestCtrlNDialog(t *testing.T) {
	m := boardWith(t, false)
	m, cmd := press(m, tea.KeyCtrlN)
	if m.mode != modeNew || cmd == nil {
		t.Fatalf("Ctrl-N: mode=%d cmd=%v", m.mode, cmd != nil)
	}
	if m.nd.focus != fieldName || !m.nd.worktree || !m.nd.name.Focused() {
		t.Errorf("fresh dialog: focus=%d worktree=%v nameFocused=%v", m.nd.focus, m.nd.worktree, m.nd.name.Focused())
	}
	if back, cmd := press(m, tea.KeyEsc); back.mode != modeBoard || cmd != nil {
		t.Errorf("Esc should leave the dialog: mode=%d cmd=%v", back.mode, cmd != nil)
	}
	// Tab walks the fields and wraps; Shift-Tab walks back.
	for _, want := range []int{fieldRepo, fieldPrompt, fieldWorktree, fieldName} {
		m, _ = press(m, tea.KeyTab)
		if m.nd.focus != want {
			t.Fatalf("Tab: focus=%d want %d", m.nd.focus, want)
		}
	}
	m, _ = press(m, tea.KeyShiftTab)
	if m.nd.focus != fieldWorktree {
		t.Fatalf("Shift-Tab: focus=%d want %d", m.nd.focus, fieldWorktree)
	}
	m, _ = press(m, tea.KeySpace)
	if m.nd.worktree {
		t.Error("Space on Worktree should toggle it off")
	}
	m, _ = press(m, tea.KeySpace)
	if !m.nd.worktree {
		t.Error("Space again should toggle it back on")
	}
	m, _ = press(m, tea.KeyUp)
	m, _ = press(m, tea.KeyUp)
	m, _ = press(m, tea.KeyUp)
	if m.nd.focus != fieldName {
		t.Fatalf("Up x3: focus=%d want %d", m.nd.focus, fieldName)
	}
	// Validation happens in the dialog, before anything runs.
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || m.nd.err != "name is required" {
		t.Errorf("empty name: cmd=%v err=%q", cmd != nil, m.nd.err)
	}
	m = typeText(m, "???")
	if m.nd.err != "" {
		t.Errorf("typing should clear the error: %q", m.nd.err)
	}
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || !strings.Contains(m.nd.err, "no name can be made from ???") {
		t.Errorf("unusable name: cmd=%v err=%q", cmd != nil, m.nd.err)
	}
	m.nd.name.SetValue("live") // taken
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || !strings.Contains(m.nd.err, "exists") {
		t.Errorf("taken name: cmd=%v err=%q", cmd != nil, m.nd.err)
	}
	m.nd.name.SetValue("good-name")
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || !strings.Contains(m.nd.err, "repo is required") || m.nd.focus != fieldRepo {
		t.Errorf("no repo: cmd=%v err=%q focus=%d", cmd != nil, m.nd.err, m.nd.focus)
	}
	m = typeText(m, "/tmp/repo")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !m.nd.busy || m.nd.err != "" {
		t.Fatalf("valid dialog: cmd=%v busy=%v err=%q", cmd != nil, m.nd.busy, m.nd.err)
	}
	if !strings.Contains(m.View(), "opening good-name") {
		t.Errorf("busy dialog should say so:\n%s", m.View())
	}
	if _, cmd := press(m, tea.KeyEnter); cmd != nil {
		t.Error("Enter while busy must not run work new twice")
	}
	// A refusal keeps the dialog, with what was typed, and the CLI's words.
	next, _ := m.Update(workNewMsg{name: "good-name", err: "fatal: not a git repository\nmore"})
	m = next.(model)
	if m.mode != modeNew || m.nd.busy || m.nd.err != "fatal: not a git repository" ||
		m.nd.repo.Value() != "/tmp/repo" {
		t.Errorf("after refusal: mode=%d busy=%v err=%q repo=%q", m.mode, m.nd.busy, m.nd.err, m.nd.repo.Value())
	}
	// Success leaves the dialog and refreshes the board.
	next, cmd = m.Update(workNewMsg{name: "good-name"})
	m = next.(model)
	if m.mode != modeBoard || cmd == nil || !strings.Contains(m.status, "opened good-name") {
		t.Errorf("after success: mode=%d cmd=%v status=%q", m.mode, cmd != nil, m.status)
	}
}

func TestViewNewRenders(t *testing.T) {
	m := boardWith(t, false)
	m, _ = press(m, tea.KeyCtrlN)
	out := m.View()
	for _, want := range []string{"new work session", "Name", "Repo", "Prompt", "Worktree", "[x]",
		"no folder of repos remembered", "Enter opens the tab"} {
		if !strings.Contains(out, want) {
			t.Errorf("dialog missing %q:\n%s", want, out)
		}
	}
	next, _ := m.Update(reposMsg{info: reposInfo{Dir: "/hub", Repos: []string{"savannah", "song"},
		Scratch: "/hub/.savannah-scratch"}})
	m = next.(model)
	if out := m.View(); !strings.Contains(out, "/hub: savannah  song  scratch") {
		t.Errorf("repos hint missing:\n%s", out)
	}
	m, _ = press(m, tea.KeyTab) // Repo
	m = typeText(m, "so")
	if out := m.View(); !strings.Contains(out, "/hub: song") || strings.Contains(out, "savannah  song") {
		t.Errorf("prefix hint wrong:\n%s", out)
	}
	m = typeText(m, "zz")
	if out := m.View(); !strings.Contains(out, "nothing in /hub matches") {
		t.Errorf("no-match hint missing:\n%s", out)
	}
	next, _ = m.Update(reposMsg{info: reposInfo{Dir: "/cwd", Picked: "cwd", Repos: []string{"one"}}})
	m = next.(model)
	if out := m.View(); !strings.Contains(out, "/cwd (the cwd; savannah work repos DIR remembers a folder)") {
		t.Errorf("cwd hint missing:\n%s", out)
	}
	next, _ = m.Update(reposMsg{info: reposInfo{Dir: "/hub", Repos: []string{"savannah", "song"},
		Scratch: "/hub/.savannah-scratch"}})
	m = next.(model)
	m, _ = press(m, tea.KeyTab)
	m, _ = press(m, tea.KeyTab) // Worktree
	m, _ = press(m, tea.KeySpace)
	if out := m.View(); !strings.Contains(out, "[ ]") {
		t.Errorf("worktree off should render [ ]:\n%s", out)
	}
	next, _ = m.Update(reposMsg{err: "work repos: exit status 2"})
	m = next.(model)
	if out := m.View(); !strings.Contains(out, "repos: work repos: exit status 2") {
		t.Errorf("repos error missing:\n%s", out)
	}
}

func TestOutputMode(t *testing.T) {
	m := boardWith(t, true)
	cursorTo(t, &m, hookRow("s2"))
	m, cmd := press(m, tea.KeyCtrlO)
	if cmd != nil || m.mode != modeBoard || !strings.Contains(m.status, "Ctrl-O") {
		t.Errorf("Ctrl-O on a plain tab: cmd=%v mode=%d status=%q", cmd != nil, m.mode, m.status)
	}
	cursorTo(t, &m, workRow("live"))
	m, cmd = press(m, tea.KeyCtrlO)
	if m.mode != modeOutput || cmd == nil || m.out.name != "live" || !m.out.follow {
		t.Fatalf("Ctrl-O on a work row: mode=%d cmd=%v out=%+v", m.mode, cmd != nil, m.out)
	}
	seq := m.out.seq
	if out := m.View(); !strings.Contains(out, "output live") || !strings.Contains(out, "fetching") {
		t.Errorf("fresh output view:\n%s", out)
	}
	next, _ := m.Update(workOutputMsg{seq: seq, name: "live", text: "you: hi\nclaude: hello there\n"})
	m = next.(model)
	if out := m.View(); !strings.Contains(out, "hello there") {
		t.Errorf("fetched text should render:\n%s", out)
	}
	next, _ = m.Update(workOutputMsg{seq: seq - 1, name: "live", text: "STALE"})
	m = next.(model)
	if strings.Contains(m.View(), "STALE") {
		t.Error("a stale fetch must be dropped")
	}
	if _, cmd := m.Update(outputTick{seq: seq}); cmd == nil {
		t.Error("a current tick should refetch")
	}
	if _, cmd := m.Update(outputTick{seq: seq - 1}); cmd != nil {
		t.Error("a stale tick must be dropped")
	}
	m, _ = press(m, tea.KeyUp)
	if m.out.follow {
		t.Error("scrolling up should pause follow")
	}
	m, _ = press(m, tea.KeyEnd)
	if !m.out.follow {
		t.Error("End should resume follow")
	}
	m, cmd = press(m, tea.KeyEsc)
	if m.mode != modeBoard || cmd != nil {
		t.Errorf("Esc should return to the board: mode=%d cmd=%v", m.mode, cmd != nil)
	}
	if _, cmd := m.Update(outputTick{seq: seq}); cmd != nil {
		t.Error("after leaving, the old view's tick must be dropped")
	}
	if next, _ := m.Update(workOutputMsg{seq: seq, name: "live", text: "LATE"}); next.(model).out.text == "LATE" {
		t.Error("after leaving, a late fetch must be dropped")
	}
}

func TestNoteMode(t *testing.T) {
	m := boardWith(t, true)
	cursorTo(t, &m, hookRow("s2"))
	// On a plain tab Ctrl-T names it first (TestCtrlTOnPlainRowNamesItFirst);
	// no note prompt yet.
	m, cmd := press(m, tea.KeyCtrlT)
	if cmd != nil || m.noteFor != "" || m.adoptFor != "s2" {
		t.Errorf("Ctrl-T on a plain tab: cmd=%v noteFor=%q adoptFor=%q", cmd != nil, m.noteFor, m.adoptFor)
	}
	cursorTo(t, &m, workRow("live"))
	m, _ = press(m, tea.KeyCtrlT)
	if m.noteFor != "live" || m.adoptFor != "" || !strings.Contains(m.View(), "note for live") {
		t.Errorf("Ctrl-T on a work row: noteFor=%q adoptFor=%q\n%s", m.noteFor, m.adoptFor, m.View())
	}
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || m.noteFor != "live" {
		t.Errorf("empty note: cmd=%v noteFor=%q", cmd != nil, m.noteFor)
	}
	m = typeText(m, "decided: keep the branch")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || m.noteFor != "" || m.input.Value() != "" || !strings.Contains(m.status, "noting live") {
		t.Errorf("note Enter: cmd=%v noteFor=%q input=%q status=%q", cmd != nil, m.noteFor, m.input.Value(), m.status)
	}
	// Esc cancels a half-typed note without quitting the dash.
	m, _ = press(m, tea.KeyCtrlT)
	m = typeText(m, "half")
	m, cmd = press(m, tea.KeyEsc)
	if cmd != nil || m.noteFor != "" || m.input.Value() != "" || m.mode != modeBoard {
		t.Errorf("Esc in note mode: cmd=%v noteFor=%q input=%q", cmd != nil, m.noteFor, m.input.Value())
	}
	// With no note pending, Esc quits as before.
	if _, cmd := press(m, tea.KeyEsc); cmd == nil {
		t.Error("Esc on the plain board should quit")
	}
}

func TestWorkResultStatuses(t *testing.T) {
	m := boardWith(t, false)
	cases := []struct {
		msg  tea.Msg
		want string
		cmd  bool
	}{
		{workOpenMsg{name: "live", action: "focused"}, "focused live", false},
		{workOpenMsg{name: "parked", action: "reopened"}, "reopened parked", true},
		{workOpenMsg{name: "live", err: "no such work session: live\nx"}, "open live: no such work session: live", false},
		{workSaidMsg{name: "live"}, "said to live", false},
		{workSaidMsg{name: "live", err: "pane gone"}, "say to live: pane gone", false},
		{workNotedMsg{name: "live"}, "noted live", false},
		{workRmMsg{name: "parked"}, "removed parked", true},
		{workRmMsg{name: "parked", err: "worktree has uncommitted changes"}, "rm parked: worktree has uncommitted changes", false},
		{workNewMsg{name: "x", err: "boom"}, "new x: boom", false},
	}
	for _, c := range cases {
		next, cmd := m.Update(c.msg)
		got := next.(model).status
		if !strings.Contains(got, c.want) || (cmd != nil) != c.cmd {
			t.Errorf("%+v: status=%q (want %q) cmd=%v (want %v)", c.msg, got, c.want, cmd != nil, c.cmd)
		}
	}
}

func TestTildeShortensHome(t *testing.T) {
	cases := []struct{ path, home, want string }{
		{"/Users/x/ng", "/Users/x", "~/ng"},
		{"/Users/x", "/Users/x", "~"},
		{"/Users/xy/ng", "/Users/x", "/Users/xy/ng"},
		{"/opt/repos", "", "/opt/repos"},
	}
	for _, c := range cases {
		if got := tilde(c.path, c.home); got != c.want {
			t.Errorf("tilde(%q, %q) = %q, want %q", c.path, c.home, got, c.want)
		}
	}
}
