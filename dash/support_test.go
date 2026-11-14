package main

import (
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// Shared scaffolding for the tests: the board fixture every file builds
// its own board through, and the helpers that drive a model (send, press,
// typeText, click, wheel) or read one (cursorTo, rowNames, lineOf).

// bare is the opts of a board with nothing switched on: no autoname, no
// fade, HISTORY closed. With sized(100, 0) it stands in for the hand-written
// `model{width: 100, tasks: ..., errs: ...}` the older tests started from.
var bare = opts{autoname: -1, fade: "off"}

// testBoard is a board as the dash would have it after one read: newModel
// with the given opts, 100x30 unless sized says otherwise, the setups
// applied in order, the hooks joined to the work records (unless a setup
// handed it the joined sessions already, withSessions), the rows built.
func testBoard(t *testing.T, o opts, setup ...func(*model)) model {
	t.Helper()
	m := newModel(o, "savannah")
	m.width, m.height = 100, 30
	for _, f := range setup {
		f(&m)
	}
	if m.sessions == nil {
		m.sessions = joinWork(m.hooks, m.work)
	}
	m.rebuildRows()
	return m
}

// sized sets the terminal size (0 is a size the board has not been told).
func sized(width, height int) func(*model) {
	return func(m *model) { m.width, m.height = width, height }
}

// withHooks sets the hook sessions: the open Claude Code tabs.
func withHooks(hooks []session) func(*model) {
	return func(m *model) { m.hooks = hooks }
}

// withWork sets the work records.
func withWork(work []workRecord) func(*model) {
	return func(m *model) { m.work = work }
}

// withSessions sets the sessions as given, already joined: testBoard does
// not run joinWork over them.
func withSessions(sessions []session) func(*model) {
	return func(m *model) { m.sessions = sessions }
}

// send hands the model one message and returns what Update made of it.
func send(m model, msg tea.Msg) (model, tea.Cmd) {
	next, cmd := m.Update(msg)
	return next.(model), cmd
}

func press(m model, k tea.KeyType) (model, tea.Cmd) {
	return send(m, tea.KeyMsg{Type: k})
}

// typeText sends s one key at a time, the way the terminal would.
func typeText(m model, s string) model {
	for _, r := range s {
		k := tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune{r}}
		if r == ' ' {
			k = tea.KeyMsg{Type: tea.KeySpace, Runes: []rune{r}}
		}
		m, _ = send(m, k)
	}
	return m
}

func click(m model, y int) (model, tea.Cmd) {
	return send(m, tea.MouseMsg{X: 3, Y: y, Button: tea.MouseButtonLeft, Action: tea.MouseActionPress})
}

func wheel(m model, b tea.MouseButton) model {
	m, _ = send(m, tea.MouseMsg{Button: b, Action: tea.MouseActionPress})
	return m
}

func workRow(name string) func(row) bool {
	return func(r row) bool { return r.kind == rowSession && r.s.Work == name }
}

func hookRow(id string) func(row) bool {
	return func(r row) bool { return r.kind == rowSession && r.s.Work == "" && r.s.ID == id }
}

// indexOfRow is the index of the first row the predicate accepts, -1 when none.
func indexOfRow(m model, match func(row) bool) int {
	for i, r := range m.rows {
		if match(r) {
			return i
		}
	}
	return -1
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

// lineOf is the first line of the view holding the text, -1 when none.
func lineOf(m model, text string) int {
	for i, ln := range strings.Split(m.View(), "\n") {
		if strings.Contains(ln, text) {
			return i
		}
	}
	return -1
}

// plainLines are the view's lines that carry no escape code.
func plainLines(view string) []string {
	var out []string
	for _, ln := range strings.Split(view, "\n") {
		if !strings.Contains(ln, "\x1b") {
			out = append(out, ln)
		}
	}
	return out
}

// promptFor is who the prompt line is for when it is of that kind (the
// plain tab being named, the work session being titled or noted), "" when
// the prompt line has another job.
func (m model) promptFor(kind promptKind) string {
	if m.prompt.kind != kind {
		return ""
	}
	return m.prompt.target
}
