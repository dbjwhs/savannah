package main

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// TestHistoryClosedByDefault: a closed session is not a row until Ctrl-E;
// the board says how many there are and where; the header counts it apart.
func TestHistoryClosedByDefault(t *testing.T) {
	file := filepath.Join(t.TempDir(), "board.json")
	t.Setenv("SAVANNAH_BOARD_FILE", file)
	m := boardWith(t, true)
	m.history = false
	m.rebuildRows()
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"live", "#s2"}) {
		t.Fatalf("rows with HISTORY closed = %v, want the live rows only", got)
	}
	out := m.View()
	for _, want := range []string{"2 sessions   1 closed", "HISTORY (1)", "Ctrl-E shows them"} {
		if !strings.Contains(out, want) {
			t.Errorf("closed HISTORY should show %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "offline    parked") {
		t.Errorf("the closed session's row should not be on the board:\n%s", out)
	}
	if named, live, plain := m.openNow(); named != 2 || live != 1 || plain != 1 {
		t.Errorf("openNow should count the closed session: %d %d %d", named, live, plain)
	}
	// Ctrl-E opens it: the row is last, the status and the file say open.
	m, cmd := press(m, tea.KeyCtrlE)
	if cmd != nil || !m.history || !strings.Contains(m.status, "HISTORY open: 1 closed session") {
		t.Fatalf("Ctrl-E: cmd=%v history=%v status=%q", cmd != nil, m.history, m.status)
	}
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"live", "#s2", "parked"}) {
		t.Fatalf("rows with HISTORY open = %v", got)
	}
	if out := m.View(); !strings.Contains(out, "offline    parked") || !strings.Contains(out, "Ctrl-E hides them") {
		t.Errorf("open HISTORY should list the row and say how to hide it:\n%s", out)
	}
	if p := loadBoardPrefs(file); p.History != "open" {
		t.Errorf("board file after Ctrl-E: %+v", p)
	}
	// The cursor on the closed row: Ctrl-E again clamps it onto a live row,
	// and the file forgets.
	cursorTo(t, &m, workRow("parked"))
	m, _ = press(m, tea.KeyCtrlE)
	if m.history || m.cursor >= len(m.rows) || m.rows[m.cursor].s.State == "offline" {
		t.Errorf("after closing: history=%v cursor=%d", m.history, m.cursor)
	}
	if p := loadBoardPrefs(file); p.History != "" {
		t.Errorf("board file after closing: %+v", p)
	}
	// The file is read on start.
	if m2 := newModel(opts{history: true}, "savannah"); !m2.history {
		t.Error("newModel should take the remembered choice")
	}
	// Nothing closed: no section, and Ctrl-E says so.
	solo := boardWith(t, false)
	solo.work = solo.work[:1]
	solo.sessions = joinWork(solo.hooks, solo.work)
	solo.rebuildRows()
	if out := solo.View(); strings.Contains(out, "HISTORY") {
		t.Errorf("no closed session, no HISTORY line:\n%s", out)
	}
	if solo, _ = press(solo, tea.KeyCtrlE); !strings.Contains(solo.status, "no closed session") {
		t.Errorf("Ctrl-E with nothing closed: %q", solo.status)
	}
}

// TestHistoryOrderAndDrag: closed sessions come newest first whatever
// their rank, and a drag on one is refused.
func TestHistoryOrderAndDrag(t *testing.T) {
	m := boardWith(t, true)
	m.work = append(m.work,
		workRecord{Name: "older", Worktree: "/r/.savannah-worktrees/older", ClaudePane: "O", Rank: 5, Updated: nowUnix() - 7200},
		workRecord{Name: "newer", Worktree: "/r/.savannah-worktrees/newer", ClaudePane: "N", Priority: "later", Rank: 50, Updated: nowUnix() - 60})
	for i := range m.work {
		if m.work[i].Name == "parked" {
			m.work[i].Updated = nowUnix() - 3600
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"live", "#s2", "newer", "parked", "older"}) {
		t.Fatalf("order = %v, want the live rows then HISTORY newest first", got)
	}
	cursorTo(t, &m, workRow("parked"))
	next, cmd := m.dragRow(-1)
	m = next.(model)
	if cmd != nil || !strings.Contains(m.status, "HISTORY") {
		t.Errorf("drag on a closed session should be refused: cmd=%v status=%q", cmd != nil, m.status)
	}
	secs := m.sectionNames()
	if !reflect.DeepEqual(secs, [][]string{nil, {"live"}, nil}) {
		t.Errorf("closed sessions must stay out of the drag lists: %v", secs)
	}
}
