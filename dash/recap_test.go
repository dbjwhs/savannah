package main

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func key(r string) tea.KeyMsg { return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(r)} }

func TestRecapArgs(t *testing.T) {
	if got := strings.Join(recapArgs("now", false), " "); got != "work recap --section now --json" {
		t.Errorf("recapArgs = %q", got)
	}
	if got := strings.Join(recapArgs("now,active", true), " "); got != "work recap --section now,active --json --ask" {
		t.Errorf("recapArgs with ask = %q", got)
	}
}

// TestCtrlQRecapsTheRowsSection: Ctrl-Q reads the recap of the section the
// highlighted row sits in, the result opens the screen, and the screen's
// keys switch sections, read again, ask, and leave.
func TestCtrlQRecapsTheRowsSection(t *testing.T) {
	hooks := []session{
		{ID: "s1", Project: "/r/a", State: "done", Updated: nowUnix(), ITerm: "w0t0p0:A", LastPrompt: "x"},
		{ID: "s2", Project: "/r/b", State: "idle", Updated: nowUnix() - 1, ITerm: "w0t0p0:B", LastPrompt: "y"},
	}
	work := []workRecord{
		{Name: "pushing", Worktree: "/r/a", ClaudePane: "A", Priority: "now", Rank: 10},
		{Name: "around", Worktree: "/r/b", ClaudePane: "B", Priority: "active", Rank: 10},
	}
	m := testBoard(t, opts{autoname: -1}, withHooks(hooks), withWork(work))
	for ndx, r := range m.rows {
		if r.kind == rowSession && r.s.Work == "around" {
			m.cursor = ndx
		}
	}
	if got := m.recapSection(); got != "active" {
		t.Fatalf("recapSection on an ACTIVE row = %q", got)
	}
	for ndx, r := range m.rows {
		if r.kind == rowSession && r.s.Work == "pushing" {
			m.cursor = ndx
		}
	}
	m, cmd := press(m, tea.KeyCtrlQ)
	if cmd == nil || !m.recap.busy || m.recap.sections != "now" || m.mode != modeBoard {
		t.Fatalf("Ctrl-Q should read NOW's recap: cmd=%v busy=%v sections=%q mode=%d", cmd != nil, m.recap.busy, m.recap.sections, m.mode)
	}
	m, cmd = press(m, tea.KeyCtrlQ)
	if cmd != nil || !strings.Contains(m.status, "still reading") {
		t.Errorf("Ctrl-Q while busy: cmd=%v status=%q", cmd != nil, m.status)
	}
	out := recapOut{Sections: []string{"now"}, Sessions: []recapEntry{{
		Name: "pushing", Title: "The parser", Section: "now", Branch: "work/pushing", State: "done",
		LastActive: nowUnix() - 4*86400, Summary: "Fixing the parser.\nTests next.", SummaryAt: nowUnix() - 7200,
		LastNote: "half done", LastPrompt: "fix the lexer", Reply: "Done.\nTests pass.", ReplyCut: 3, Turns: 9,
	}}}
	m, _ = send(m, recapMsg{out: out})
	if m.recap.busy || m.mode != modeRecap {
		t.Fatalf("the result should open the screen: busy=%v mode=%d", m.recap.busy, m.mode)
	}
	text := strings.Join(plainLines(m.View()), "\n")
	for _, want := range []string{"recap of NOW", "NOW (1)", "pushing", "The parser", "work/pushing", "last active 4d ago",
		"summary (2h ago):", "  Tests next.", "note: half done", "you: fix the lexer", "3 lines above", "  Tests pass."} {
		if !strings.Contains(text, want) {
			t.Errorf("the recap screen lacks %q:\n%s", want, text)
		}
	}
	m, cmd = send(m, key("3"))
	if cmd == nil || m.recap.sections != "now,active" || m.recap.asking {
		t.Errorf("3 should read both sections: cmd=%v sections=%q", cmd != nil, m.recap.sections)
	}
	m, cmd = send(m, key("a"))
	if cmd != nil {
		t.Errorf("a key while a read is running should do nothing")
	}
	m, _ = send(m, recapMsg{out: recapOut{Sections: []string{"now", "active"}}})
	if !strings.Contains(strings.Join(plainLines(m.View()), "\n"), "no work sessions in NOW, ACTIVE") {
		t.Errorf("an empty recap should say so:\n%s", m.View())
	}
	m, cmd = send(m, key("a"))
	if cmd == nil || !m.recap.asking || !strings.Contains(m.status, "asking NOW,ACTIVE") {
		t.Errorf("a should ask the sections shown: cmd=%v asking=%v status=%q", cmd != nil, m.recap.asking, m.status)
	}
	var ask askOut
	ask.Waited = 5
	ask.Asked = []askAsked{{Session: "s1", Name: "pushing", How: "answered", Reply: "ok"}, {Session: "s2", Name: "around", How: "timeout"}}
	ask.Skipped = []askSkipped{{Name: "busy", Why: "working (not interrupted)"}}
	m, cmd = send(m, recapMsg{out: recapOut{Sections: []string{"now", "active"}, Ask: &ask}})
	text = strings.Join(plainLines(m.View()), "\n")
	if cmd == nil || m.recap.asking || !strings.Contains(text, "asked 2, 1 answered in 5 s") ||
		!strings.Contains(text, "no reply yet from around (timeout)") || !strings.Contains(text, "skipped busy: working") {
		t.Errorf("an asked recap should say who answered and reread the sessions: cmd=%v\n%s", cmd != nil, text)
	}
	m, _ = send(m, recapMsg{err: "boom"})
	if m.mode != modeRecap || !strings.Contains(m.status, "boom") {
		t.Errorf("a failed recap keeps the screen and says why: mode=%d status=%q", m.mode, m.status)
	}
	m, _ = press(m, tea.KeyEsc)
	if m.mode != modeBoard {
		t.Errorf("Esc should go back to the board: mode=%d", m.mode)
	}
}
