package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// sectionedBoard is boardWith plus a named session parked in LATER and a
// waiting unnamed tab: the shape the sections are about.
func sectionedBoard(t *testing.T) model {
	t.Helper()
	m := boardWith(t, true)
	m.hooks = append(m.hooks, session{ID: "s3", Project: "/elsewhere", State: "waiting",
		Updated: nowUnix() - 100, ITerm: "w0t0p0:UUID-W"})
	m.work = append(m.work, workRecord{Name: "slow", Worktree: "/r/.savannah-worktrees/slow",
		ClaudePane: "S", Priority: "later", Rank: 10})
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	return m
}

// rowNames lists the session rows in board order: work names, "#<id>" for
// unnamed tabs.
func rowNames(m model) []string {
	var out []string
	for _, r := range m.rows {
		if r.kind != rowSession {
			continue
		}
		if r.s.Work != "" {
			out = append(out, r.s.Work)
		} else {
			out = append(out, "#"+r.s.ID)
		}
	}
	return out
}

func setRank(m *model, name string, priority string, rank int64) {
	for i := range m.work {
		if m.work[i].Name == name {
			m.work[i].Priority, m.work[i].Rank = priority, rank
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
}

func TestOrderSessionsSections(t *testing.T) {
	m := sectionedBoard(t)
	// ACTIVE holds the named rows with no priority (rank 0, so creation order,
	// then name); LATER holds slow and then the unnamed tabs in attention
	// order (s3 waits, s2 idles), although the hook sort put s3 first overall.
	want := []string{"live", "parked", "slow", "#s3", "#s2"}
	if got := rowNames(m); !reflect.DeepEqual(got, want) {
		t.Fatalf("order = %v, want %v", got, want)
	}
	// Rank decides inside a section, and state never does: live is the one
	// working, parked is offline, yet parked's lower rank puts it first.
	setRank(&m, "live", "active", 20)
	setRank(&m, "parked", "active", 10)
	want = []string{"parked", "live", "slow", "#s3", "#s2"}
	if got := rowNames(m); !reflect.DeepEqual(got, want) {
		t.Fatalf("ranked order = %v, want %v", got, want)
	}
	// Attention still counts and colours, it just does not reorder.
	if n := countSessionAttention(m.sessions); n != 1 {
		t.Errorf("attention = %d, want 1 (s3)", n)
	}
}

func TestPlanDrag(t *testing.T) {
	active, later := []string{"a", "b", "c"}, []string{"d", "e"}
	cases := []struct {
		name  string
		delta int
		wantA []string
		wantL []string
		ok    bool
	}{
		{"b", -1, []string{"b", "a", "c"}, []string{"d", "e"}, true},
		{"b", 1, []string{"a", "c", "b"}, []string{"d", "e"}, true},
		{"c", 1, []string{"a", "b"}, []string{"c", "d", "e"}, true},  // over the boundary, down
		{"d", -1, []string{"a", "b", "c", "d"}, []string{"e"}, true}, // over the boundary, up
		{"e", -1, []string{"a", "b", "c"}, []string{"e", "d"}, true},
		{"a", -1, nil, nil, false}, // top of ACTIVE
		{"e", 1, nil, nil, false},  // bottom of LATER
		{"z", 1, nil, nil, false},  // unknown
	}
	for _, c := range cases {
		gotA, gotL, ok := planDrag(active, later, c.name, c.delta)
		if ok != c.ok {
			t.Errorf("planDrag(%s, %d) ok = %v, want %v", c.name, c.delta, ok, c.ok)
			continue
		}
		if ok && (!reflect.DeepEqual(gotA, c.wantA) || !reflect.DeepEqual(gotL, c.wantL)) {
			t.Errorf("planDrag(%s, %d) = %v / %v, want %v / %v", c.name, c.delta, gotA, gotL, c.wantA, c.wantL)
		}
	}
	// The inputs are never modified.
	if !reflect.DeepEqual(active, []string{"a", "b", "c"}) || !reflect.DeepEqual(later, []string{"d", "e"}) {
		t.Errorf("planDrag modified its inputs: %v %v", active, later)
	}
}

func TestRenumberWritesOnlyChanges(t *testing.T) {
	recs := map[string]workRecord{
		"a": {Name: "a", Priority: "active", Rank: 10},
		"b": {Name: "b", Priority: "active", Rank: 20},
		"c": {Name: "c", Created: 5}, // no priority, no rank: active by creation
		"d": {Name: "d", Priority: "active", Rank: 10},
	}
	got := renumber([]string{"a", "c", "b"}, "active", recs)
	want := []moveChange{{"c", "active", 20}, {"b", "active", 30}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("renumber active = %+v, want %+v", got, want)
	}
	// Same rank, other section: the priority alone makes it a change.
	got = renumber([]string{"d"}, "later", recs)
	want = []moveChange{{"d", "later", 10}}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("renumber later = %+v, want %+v", got, want)
	}
	if args := moveArgs(want[0]); !reflect.DeepEqual(args, []string{"work", "move", "d", "--later", "--rank=10"}) {
		t.Errorf("moveArgs = %v", args)
	}
}

func TestDragRowMovesAndWrites(t *testing.T) {
	m := sectionedBoard(t) // live, parked | slow, #s3, #s2; cursor on live
	m, cmd := press(m, tea.KeyShiftDown)
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"parked", "live", "slow", "#s3", "#s2"}) {
		t.Fatalf("after Shift-Down: %v", got)
	}
	if m.cursor != 1 || cmd == nil {
		t.Fatalf("cursor = %d (want 1, on live), cmd nil = %v", m.cursor, cmd == nil)
	}
	rec, _ := m.recordOf("parked")
	if rec.Priority != "active" || rec.Rank != 10 {
		t.Errorf("parked not renumbered locally: %+v", rec)
	}
	// Down again from the bottom of ACTIVE: into the top of LATER.
	m, cmd = press(m, tea.KeyShiftDown)
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"parked", "live", "slow", "#s3", "#s2"}) {
		t.Fatalf("after crossing down: %v", got)
	}
	rec, _ = m.recordOf("live")
	if rec.Priority != "later" || rec.Rank != 10 || m.cursor != 1 || cmd == nil {
		t.Fatalf("live should now be later/10 under the cursor: %+v cursor=%d", rec, m.cursor)
	}
	if out := m.View(); !strings.Contains(out, "ACTIVE (1)") || !strings.Contains(out, "LATER (4)") {
		t.Errorf("section counts not updated:\n%s", out)
	}
	// Up from the top of LATER: back onto the bottom of ACTIVE (Ctrl-Up works too).
	m, _ = press(m, tea.KeyCtrlUp)
	rec, _ = m.recordOf("live")
	if rec.Priority != "active" || rec.Rank != 20 {
		t.Fatalf("live should be active/20 again: %+v", rec)
	}
	// Option-Down (KeyDown with Alt) is the third spelling: back into LATER.
	next, _ := m.Update(tea.KeyMsg{Type: tea.KeyDown, Alt: true})
	m = next.(model)
	rec, _ = m.recordOf("live")
	if rec.Priority != "later" || rec.Rank != 10 {
		t.Fatalf("Option-Down should drag like Shift-Down: %+v", rec)
	}
	next, _ = m.Update(tea.KeyMsg{Type: tea.KeyUp, Alt: true})
	m = next.(model)
	rec, _ = m.recordOf("live")
	if rec.Priority != "active" || rec.Rank != 20 {
		t.Fatalf("live should be active/20 again: %+v", rec)
	}
	// Top of ACTIVE: nothing moves, the status says so.
	m.cursor = 0
	m, cmd = press(m, tea.KeyShiftUp)
	if cmd != nil || !strings.Contains(m.status, "already at the top") {
		t.Errorf("drag past the top: cmd=%v status=%q", cmd != nil, m.status)
	}
	// An unnamed tab has no record to rank: it must be named first.
	m.cursor = 3
	m, cmd = press(m, tea.KeyShiftUp)
	if cmd != nil || !strings.Contains(m.status, "name this tab first") {
		t.Errorf("drag on a plain tab: cmd=%v status=%q", cmd != nil, m.status)
	}
	// A failed move is reported and the records re-read.
	next, cmd = m.Update(workMovedMsg{err: "live: no such work session"})
	m = next.(model)
	if cmd == nil || !strings.Contains(m.status, "no such work session") {
		t.Errorf("move error: cmd=%v status=%q", cmd != nil, m.status)
	}
}

func TestCtrlRNamesPlainTabWithoutNote(t *testing.T) {
	m := boardWith(t, true) // live, parked, #s2
	m.titles = map[string]string{"UUID-P": "Local testing"}
	m.cursor = 2
	m, cmd := press(m, tea.KeyCtrlR)
	if cmd != nil || m.adoptFor != "s2" || m.adoptNote || m.titleFor != "" {
		t.Fatalf("Ctrl-R on a plain tab: adoptFor=%q adoptNote=%v titleFor=%q", m.adoptFor, m.adoptNote, m.titleFor)
	}
	// The tab's own words are offered: Enter keeps them as the title and
	// derives the name.
	if got := m.input.Value(); got != "Local testing" {
		t.Errorf("suggested = %q, want the tab title as typed", got)
	}
	if out := m.View(); !strings.Contains(out, "name this tab (other)") || strings.Contains(out, "asks for the note") {
		t.Errorf("prompt line should name without a note:\n%s", out)
	}
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "naming it local-testing") {
		t.Fatalf("Enter should run adopt: cmd=%v status=%q", cmd != nil, m.status)
	}
	next, cmd := m.Update(workAdoptMsg{name: "local-testing", title: "Local testing"})
	m = next.(model)
	if m.noteFor != "" || m.adoptFor != "" || !strings.Contains(m.status, "named it local-testing (Local testing)") ||
		strings.Contains(m.status, "now its note") || cmd == nil {
		t.Errorf("after adopt: noteFor=%q adoptFor=%q status=%q", m.noteFor, m.adoptFor, m.status)
	}
	// Ctrl-T still takes the note after naming.
	m.cursor = 2
	m, _ = press(m, tea.KeyCtrlT)
	if m.adoptFor != "s2" || !m.adoptNote {
		t.Errorf("Ctrl-T should name with a note to follow: adoptFor=%q adoptNote=%v", m.adoptFor, m.adoptNote)
	}
	next, _ = m.Update(workAdoptMsg{name: "x"})
	m = next.(model)
	if m.noteFor != "x" {
		t.Errorf("Ctrl-T adopt should move on to the note, noteFor=%q", m.noteFor)
	}
}

func TestCtrlREditsTitle(t *testing.T) {
	m := boardWith(t, true) // cursor on live, which has no title yet
	m, _ = press(m, tea.KeyCtrlR)
	if m.titleFor != "live" || m.input.Value() != "" || m.adoptFor != "" {
		t.Fatalf("Ctrl-R on a work row: titleFor=%q input=%q", m.titleFor, m.input.Value())
	}
	if out := m.View(); !strings.Contains(out, "title for live") {
		t.Errorf("prompt line should say title for live:\n%s", out)
	}
	m, cmd := press(m, tea.KeyEnter) // empty, and nothing to clear
	if cmd != nil || m.titleFor != "live" || !strings.Contains(m.status, "no title yet") {
		t.Errorf("empty title on an untitled session: cmd=%v status=%q", cmd != nil, m.status)
	}
	m.input.SetValue("Live One")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "titling live") {
		t.Fatalf("Enter should run work title: cmd=%v status=%q", cmd != nil, m.status)
	}
	next, cmd := m.Update(workTitledMsg{name: "live", title: "Live One", err: "title: no such session"})
	m = next.(model)
	if cmd != nil || m.titleFor != "live" || m.input.Value() != "Live One" || !strings.Contains(m.status, "title live: title: no such session") {
		t.Errorf("refused title should keep the box: titleFor=%q input=%q status=%q", m.titleFor, m.input.Value(), m.status)
	}
	next, cmd = m.Update(workTitledMsg{name: "live", title: "Live One"})
	m = next.(model)
	if cmd == nil || m.titleFor != "" || m.input.Value() != "" || !strings.Contains(m.status, "titled live: Live One") {
		t.Errorf("after title: titleFor=%q input=%q status=%q", m.titleFor, m.input.Value(), m.status)
	}
	// With a title set, Ctrl-R offers it; the same text is a no-op, empty clears.
	for i := range m.work {
		if m.work[i].Name == "live" {
			m.work[i].Title = "Live One"
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	cursorTo(t, &m, workRow("live"))
	m, _ = press(m, tea.KeyCtrlR)
	if m.input.Value() != "Live One" {
		t.Errorf("Ctrl-R should offer the current title, got %q", m.input.Value())
	}
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || m.titleFor != "" || m.status != "title unchanged" {
		t.Errorf("unchanged title: cmd=%v titleFor=%q status=%q", cmd != nil, m.titleFor, m.status)
	}
	m, _ = press(m, tea.KeyCtrlR)
	m.input.SetValue("")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "clearing the title of live") {
		t.Errorf("empty Enter should clear: cmd=%v status=%q", cmd != nil, m.status)
	}
	next, _ = m.Update(workTitledMsg{name: "live", title: ""})
	m = next.(model)
	if !strings.Contains(m.status, "cleared the title of live") {
		t.Errorf("after clear: status=%q", m.status)
	}
	// Esc cancels a title in progress.
	m, _ = press(m, tea.KeyCtrlR)
	m, cmd = press(m, tea.KeyEsc)
	if cmd != nil || m.titleFor != "" || m.status != "title cancelled" {
		t.Errorf("Esc: cmd=%v titleFor=%q status=%q", cmd != nil, m.titleFor, m.status)
	}
	if args := titleArgs("live", "Live One"); !reflect.DeepEqual(args, []string{"work", "title", "live", "--", "Live One"}) {
		t.Errorf("titleArgs = %v", args)
	}
	if args := titleArgs("live", ""); !reflect.DeepEqual(args, []string{"work", "title", "live", "--clear"}) {
		t.Errorf("titleArgs clear = %v", args)
	}
	// Ctrl-R with no session row selected only hints.
	m.rows = nil
	m, cmd = press(m, tea.KeyCtrlR)
	if cmd != nil || !strings.Contains(m.status, "select one") {
		t.Errorf("Ctrl-R on nothing: status=%q", m.status)
	}
}

func TestTitleInWorkColumn(t *testing.T) {
	m := boardWith(t, true)
	m.width = 160
	for i := range m.work {
		if m.work[i].Name == "live" {
			m.work[i].Title = "Main Claude Code Session"
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	cursorTo(t, &m, workRow("live"))
	out := m.View()
	for _, want := range []string{"Main Claude Code Session", "say to Main Claude Code Session"} {
		if !strings.Contains(out, want) {
			t.Errorf("view lacks %q:\n%s", want, out)
		}
	}
	if c := sessionColumns(160, m.sessions, "", nil, false); c.work != len("Main Claude Code Session") {
		t.Errorf("WORK column should fit the title, got %d", c.work)
	}
	// The panel leads with the title and keeps the name beside it.
	lines := plainLines(out)
	found := false
	for _, ln := range lines {
		if strings.HasPrefix(ln, "Main Claude Code Session") && strings.Contains(ln, "live") {
			found = true
		}
	}
	if !found {
		t.Errorf("panel head should be the title then the name:\n%s", out)
	}
	// An offline record carries its title too, and a long one is cut with ~.
	for i := range m.work {
		if m.work[i].Name == "parked" {
			m.work[i].Title = strings.Repeat("Parked ", 6)
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	cut := strings.Repeat("Parked ", 6)[:maxWorkCol-1] + "~"
	if out := m.View(); !strings.Contains(out, cut) {
		t.Errorf("long title should be cut to %q:\n%s", cut, out)
	}
	if got := workCell(session{Work: "x", WorkTitle: "Ex"}, nil); got != "Ex" {
		t.Errorf("titled cell = %q, want Ex", got)
	}
}

func TestValidWorkNameReservesSnapshots(t *testing.T) {
	for _, bad := range []string{"snapshots", "archive", "t-12"} {
		if validWorkName(bad) {
			t.Errorf("%q should be refused", bad)
		}
	}
	if !validWorkName("snapshot") || !validWorkName("snapshots-2") {
		t.Error("snapshot and snapshots-2 are ordinary names")
	}
}

func TestSectionHeadersInView(t *testing.T) {
	m := boardWith(t, true)
	out := m.View()
	for _, want := range []string{"ACTIVE (2)", "LATER (1)", "Shift-up/down move", "^R name"} {
		if !strings.Contains(out, want) {
			t.Errorf("view lacks %q:\n%s", want, out)
		}
	}
	lines := plainLines(out)
	iA, iL, iCol := -1, -1, -1
	for i, ln := range lines {
		switch {
		case strings.HasPrefix(ln, "ACTIVE ("):
			iA = i
		case strings.HasPrefix(ln, "LATER ("):
			iL = i
		case strings.Contains(ln, "STATE") && strings.Contains(ln, "WHAT"):
			iCol = i
		}
	}
	if !(iCol < iA && iA < iL) {
		t.Errorf("want column header, then ACTIVE, then LATER; got %d %d %d", iCol, iA, iL)
	}
	// Empty sections say where a drag lands.
	solo := boardWith(t, false)
	if out := solo.View(); !strings.Contains(out, "LATER (0)") || !strings.Contains(out, "(empty: Shift-Down") {
		t.Errorf("empty LATER should carry its hint:\n%s", out)
	}
	setRank(&solo, "live", "later", 10)
	setRank(&solo, "parked", "later", 20)
	if out := solo.View(); !strings.Contains(out, "ACTIVE (0)") || !strings.Contains(out, "(empty: Shift-Up") {
		t.Errorf("empty ACTIVE should carry its hint:\n%s", out)
	}
	// Mesh help names the view, still within one line.
	m.mesh = true
	for _, ln := range plainLines(m.View()) {
		if len([]rune(ln)) > m.width {
			t.Errorf("line wider than the terminal (%d): %q", len([]rune(ln)), ln)
		}
	}
}

func TestUnnamedTabShowsTitle(t *testing.T) {
	m := boardWith(t, true)
	m.width = 160
	m.titles = map[string]string{"UUID-P": "Local testing"}
	if out := m.View(); !strings.Contains(out, "(Local testing)") {
		t.Errorf("WORK cell should show the tab title in parentheses:\n%s", out)
	}
	if c := sessionColumns(160, m.sessions, "", m.titles, false); c.work != len("(Local testing)") {
		t.Errorf("WORK column should fit the title, got %d", c.work)
	}
	if got := workCell(session{Work: "x"}, m.titles); got != "x" {
		t.Errorf("named cell = %q", got)
	}
	// A title longer than the WORK column is cut with its shape kept.
	long := "Savannah iTerm task manager, the second one"
	m.titles["UUID-P"] = long
	want := string([]rune("(" + long + ")")[:maxWorkCol-2]) + "~)"
	if out := m.View(); !strings.Contains(out, want) {
		t.Errorf("long title should be cut to %q:\n%s", want, out)
	}
}

func TestParseTitles(t *testing.T) {
	src := `[{"id":"A","tab_id":"11","name":".\\/build\\/savannah (savannah-dash)","title":"./build/savannah (savannah-dash)"},
	{"id":"B","tab_id":"12","name":"demo (claude)","title":"demo (claude)"},
	{"id":"C","name":"~\\/ng\\/dbjwhs (-zsh)","title":""},
	{"id":"D","name":"","title":""}]`
	got, err := parseTitles([]byte(src))
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]string{"A": "./build/savannah", "B": "demo", "C": "~/ng/dbjwhs"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("parseTitles = %v, want %v", got, want)
	}
	if _, err := parseTitles([]byte("not json")); err == nil {
		t.Error("bad json should be an error")
	}
	for in, want := range map[string]string{
		"demo (claude)": "demo", "a (b) (c)": "a (b)", "plain": "plain", " (claude)": "", "x)": "x)",
	} {
		if got := cleanTitle(in); got != want {
			t.Errorf("cleanTitle(%q) = %q, want %q", in, got, want)
		}
	}
	s := session{ITerm: "w0t0p0:B"}
	if titleOf(s, got) != "demo" || titleOf(session{}, got) != "" {
		t.Error("titleOf should key on the pane uuid")
	}
}

func TestSuggestNamePrefersTitle(t *testing.T) {
	cases := []struct{ project, title, want string }{
		{"/other", "Local testing", "local-testing"},
		{"/other", "Savannah iTerm task manager", "savannah-iterm-task-manager"},
		{"/other", "~/ng/dbjwhs", "dbjwhs"},
		{"/other", "", "other"},
		{"/other", "!!!", "other"},
		{"/other", "a   b", "a-b"},
	}
	for _, c := range cases {
		if got := suggestName(c.project, c.title, nil); got != c.want {
			t.Errorf("suggestName(%q, %q) = %q, want %q", c.project, c.title, got, c.want)
		}
	}
}

func TestPollingRow(t *testing.T) {
	m := boardWith(t, true)
	m.width, m.height = 160, 40
	now := nowUnix()
	for i := range m.hooks {
		if m.hooks[i].ID == "s1" {
			m.hooks[i].Updated = now - 30 // the prompt went in 30 s ago
		}
	}
	report := workStatus{Text: "waiting for ctest (run 2 of 3)", Updated: now - 5, Count: 7, have: true}
	for i := range m.work {
		if m.work[i].Name == "live" {
			m.work[i].status = report
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	out := m.View()
	for _, want := range []string{"1 polling", "polling: waiting for ctest (run 2 of 3) (7 polls,", pollGlyph,
		"polling  waiting for ctest (run 2 of 3)  (7 polls, 5s ago)"} {
		if !strings.Contains(out, want) {
			t.Errorf("polling view lacks %q:\n%s", want, out)
		}
	}
	if m.countPolling() != 1 {
		t.Errorf("countPolling = %d", m.countPolling())
	}
	// A report older than the turn's prompt is from an earlier turn: not live,
	// but the panel still shows it as the last status.
	report.Updated = now - 60
	m.work[0].status, m.work[1].status = workStatus{}, workStatus{}
	for i := range m.work {
		if m.work[i].Name == "live" {
			m.work[i].status = report
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	out = m.View()
	if strings.Contains(out, "1 polling") || strings.Contains(out, "polling:") {
		t.Errorf("stale report shown as live:\n%s", out)
	}
	if !strings.Contains(out, "last status  waiting for ctest (run 2 of 3)  (7 polls, 1m ago, not live)") {
		t.Errorf("panel should show the stale report as not live:\n%s", out)
	}
	// A finished turn is never polling, however fresh the report.
	report.Updated = now
	for i := range m.hooks {
		if m.hooks[i].ID == "s1" {
			m.hooks[i].State = "done"
		}
	}
	for i := range m.work {
		if m.work[i].Name == "live" {
			m.work[i].status = report
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	if out := m.View(); strings.Contains(out, "polling") {
		t.Errorf("done turn shown as polling:\n%s", out)
	}
	if pollsWord(1) != "1 poll" || pollsWord(0) != "0 polls" {
		t.Error("pollsWord")
	}
}

func TestReadStatus(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "status.json")
	if st := readStatus(p); st.have {
		t.Error("missing file should be no status")
	}
	os.WriteFile(p, []byte(`{"text": "waiting for CI", "updated": 1762200000.5, "count": 3}`), 0o644)
	st := readStatus(p)
	if !st.have || st.Text != "waiting for CI" || st.Updated != 1762200000 || st.Count != 3 {
		t.Errorf("readStatus = %+v", st)
	}
	os.WriteFile(p, []byte(`{"text": "", "updated": 1762200000, "count": 3}`), 0o644)
	if st := readStatus(p); st.have {
		t.Error("a cleared status (no text) should be no status")
	}
	os.WriteFile(p, []byte(`garbage`), 0o644)
	if st := readStatus(p); st.have {
		t.Error("garbage should be no status")
	}
	// readWork attaches it to the record.
	wd := t.TempDir()
	os.MkdirAll(filepath.Join(wd, "job"), 0o755)
	os.WriteFile(filepath.Join(wd, "job", "record.json"), []byte(`{"name":"job","worktree":"/w","priority":"later","rank":30}`), 0o644)
	os.WriteFile(filepath.Join(wd, "job", "status.json"), []byte(`{"text":"poll 4","updated":10,"count":4}`), 0o644)
	recs, err := readWork(wd)
	if err != nil || len(recs) != 1 {
		t.Fatalf("readWork: %v %d", err, len(recs))
	}
	r := recs[0]
	if r.Priority != "later" || r.Rank != 30 || !r.status.have || r.status.Count != 4 {
		t.Errorf("record = %+v status=%+v", r, r.status)
	}
}
