package main

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// askBoard: an idle work session, a done plain tab, a working one, a
// waiting one, an offline record.
func askBoard(t *testing.T) model {
	t.Helper()
	m := newModel(opts{autoname: -1}, "savannah")
	m.width, m.height = 100, 30
	m.hooks = []session{
		{ID: "s1", Project: "/r/.savannah-worktrees/live", State: "idle", Updated: nowUnix(), ITerm: "w0t0p0:L", LastPrompt: "fix it"},
		{ID: "p1", Project: "/zfs", State: "done", Updated: nowUnix(), ITerm: "w0t0p0:P1", LastPrompt: "review"},
		{ID: "p2", Project: "/net", State: "working", Updated: nowUnix(), ITerm: "w0t0p0:P2", LastPrompt: "why"},
		{ID: "p3", Project: "/x", State: "waiting", Updated: nowUnix(), ITerm: "w0t0p0:P3", LastPrompt: "do"},
	}
	m.work = []workRecord{
		{Name: "live", Worktree: "/r/.savannah-worktrees/live", ClaudePane: "L", Summary: "Fixing the parser.\nTests next.", SummaryAt: nowUnix() - 120},
		{Name: "parked", Worktree: "/r/.savannah-worktrees/parked", ClaudePane: "P"},
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	return m
}

func TestCtrlAArmsThenAsks(t *testing.T) {
	m := askBoard(t)
	if n := m.askable(); n != 2 {
		t.Fatalf("askable = %d, want the idle and done tabs", n)
	}
	m, cmd := press(m, tea.KeyCtrlA)
	if cmd != nil || !m.ask.armed || !strings.Contains(m.status, "Ctrl-A again to ask 2 tabs") {
		t.Fatalf("first Ctrl-A should arm: cmd=%v armed=%v status=%q", cmd != nil, m.ask.armed, m.status)
	}
	m, cmd = press(m, tea.KeyCtrlA)
	if cmd == nil || m.ask.armed || !m.ask.busy || m.mode != modeBoard || !strings.Contains(m.status, "asking every idle tab") {
		t.Fatalf("second Ctrl-A should ask and stay on the board: cmd=%v busy=%v mode=%d status=%q", cmd != nil, m.ask.busy, m.mode, m.status)
	}
	// While busy a third press only says so.
	m, cmd = press(m, tea.KeyCtrlA)
	if cmd != nil || !strings.Contains(m.status, "still asking") {
		t.Errorf("Ctrl-A while busy: cmd=%v status=%q", cmd != nil, m.status)
	}
	// The result opens the replies screen; its lines carry each reply, the
	// skipped tabs with their reasons, and the timeouts.
	var o askOut
	o.Question = "What are you doing?"
	o.Waited = 7
	o.Asked = append(o.Asked, struct {
		Session string `json:"session_id"`
		Name    string `json:"name"`
		Project string `json:"project"`
		How     string `json:"how"`
		Reply   string `json:"reply"`
	}{"s1", "live", "/r/.savannah-worktrees/live", "answered", "Fixing the parser.\nTests next."}, struct {
		Session string `json:"session_id"`
		Name    string `json:"name"`
		Project string `json:"project"`
		How     string `json:"how"`
		Reply   string `json:"reply"`
	}{"p1", "", "/zfs", "timeout", ""})
	o.Skipped = append(o.Skipped, struct {
		Session string `json:"session_id"`
		Name    string `json:"name"`
		Project string `json:"project"`
		Why     string `json:"why"`
	}{"p2", "", "/net", "working (not interrupted)"})
	next, cmd := m.Update(askMsg{out: o})
	m = next.(model)
	if m.ask.busy || cmd == nil || m.mode != modeAsk || !m.ask.have {
		t.Fatalf("the result should open the screen and re-read sessions: busy=%v cmd=%v mode=%d", m.ask.busy, cmd != nil, m.mode)
	}
	view := m.View()
	for _, want := range []string{"savannah  replies", "asked: What are you doing?", "live", "Fixing the parser.", "Tests next.",
		"/zfs", "no reply within the timeout", "skipped /net: working (not interrupted)", "a ask again"} {
		if !strings.Contains(view, want) {
			t.Errorf("replies screen lacks %q:\n%s", want, view)
		}
	}
	if !strings.Contains(m.status, "1 tab answered in 7 s, 1 did not in time, 1 skipped") {
		t.Errorf("status = %q", m.status)
	}
	// Esc back; Ctrl-A on the board now shows the last replies again; `a`
	// on the screen asks again.
	m, _ = press(m, tea.KeyEsc)
	if m.mode != modeBoard {
		t.Fatal("Esc should return to the board")
	}
	m, cmd = press(m, tea.KeyCtrlA)
	if cmd != nil || m.mode != modeAsk {
		t.Fatalf("Ctrl-A with replies should show them: cmd=%v mode=%d", cmd != nil, m.mode)
	}
	next, cmd = m.Update(tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune("a")})
	m = next.(model)
	if cmd == nil || !m.ask.busy || m.mode != modeBoard {
		t.Errorf("`a` on the screen should ask again: cmd=%v busy=%v mode=%d", cmd != nil, m.ask.busy, m.mode)
	}
	// An error is a status line and nothing opens.
	next, _ = m.Update(askMsg{err: "work ask: boom"})
	m = next.(model)
	if m.ask.busy || m.mode != modeBoard || !strings.Contains(m.status, "ask: work ask: boom") {
		t.Errorf("error: busy=%v mode=%d status=%q", m.ask.busy, m.mode, m.status)
	}
	// Nothing to ask: no arming.
	for i := range m.hooks {
		m.hooks[i].State = "working"
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	m.ask.have = false
	m, cmd = press(m, tea.KeyCtrlA)
	if cmd != nil || m.ask.armed || !strings.Contains(m.status, "no tab to ask") {
		t.Errorf("nothing askable: armed=%v status=%q", m.ask.armed, m.status)
	}
}

func TestPanelShowsSummary(t *testing.T) {
	m := askBoard(t)
	m.height = 40
	cursorTo(t, &m, workRow("live"))
	view := m.View()
	if !strings.Contains(view, "summary (2m ago)  Fixing the parser. Tests next.") {
		t.Errorf("panel should carry the summary on one line:\n%s", view)
	}
	if got := askArgs(); strings.Join(got, " ") != "work ask --all --json" {
		t.Errorf("askArgs = %v", got)
	}
}
