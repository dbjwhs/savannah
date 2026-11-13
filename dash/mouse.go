// mouse.go: the rows clicked (and the wheel).
//
// Thirty rows and a trackpad: a click on a row should select it, a double
// click should do what Enter does with an empty prompt line (open the work
// session's tab, jump to a plain tab, open a worker's view), and the
// wheel should move the highlight. That is all the mouse does; every verb
// stays on a key. A click on the HISTORY header toggles the section, as
// Ctrl-E does. Nothing else on the screen (the panel, the prompt line,
// the help) answers a click.
//
// The cost, stated: with mouse reporting on, the terminal hands clicks to
// the board, so selecting text to copy is Option-drag in iTerm2 (its own
// rule for apps that take the mouse). Cmd-click on a ticket or pull
// request link is iTerm2's, and should still open it; if a terminal
// swallows it, Ctrl-B opens the row's links from the keyboard. `--no-mouse`
// turns reporting off for one run, `"mouse": "off"` in the board file for
// good. A click lands on whatever the last frame drew; the board rebuilds
// the same lines to read it, so a row that moved between the frame and
// the click is read where the click's frame had it, give or take one
// tick.

package main

import (
	"log/slog"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// doubleClick is how close two presses on one row must be to count as one
// double click.
const doubleClick = 400 * time.Millisecond

// lineHit is what one line of the board's top half is to the mouse: a row
// (its index in m.rows), a section header (its section), or nothing.
type lineHit struct {
	row     int // index in m.rows, -1 for none
	section int // secNow..secHistory for a section's header line, -1 for none
}

var noHit = lineHit{row: -1, section: -1}

// clickState is the last press, for telling a double click.
type clickState struct {
	row int
	at  time.Time
}

// hitAt is what the line y of the board holds, from the same lines the
// view draws.
func (m model) hitAt(y int) lineHit {
	width := m.width
	if width <= 0 {
		width = 100
	}
	_, hits := m.topLines(width, "")
	if y < 0 || y >= len(hits) {
		return noHit
	}
	return hits[y]
}

// updateMouse is the board's mouse map: a press on a row selects it (a
// second press on the same row within doubleClick activates it), a press
// on the HISTORY header toggles the section, the wheel moves the
// highlight. Other screens and the dialogs ignore the mouse.
func (m model) updateMouse(msg tea.MouseMsg) (tea.Model, tea.Cmd) {
	if m.mode != modeBoard {
		return m, nil
	}
	switch {
	case msg.Button == tea.MouseButtonWheelUp && msg.Action == tea.MouseActionPress:
		if m.cursor > 0 {
			m.cursor--
		}
		return m, m.refreshPanel()
	case msg.Button == tea.MouseButtonWheelDown && msg.Action == tea.MouseActionPress:
		if m.cursor < len(m.rows)-1 {
			m.cursor++
		}
		return m, m.refreshPanel()
	case msg.Button != tea.MouseButtonLeft || msg.Action != tea.MouseActionPress:
		return m, nil
	}
	hit := m.hitAt(msg.Y)
	switch {
	case hit.row >= 0 && hit.row < len(m.rows):
		twice := m.click.row == hit.row && time.Since(m.click.at) < doubleClick
		m.click = clickState{row: hit.row, at: time.Now()}
		if twice {
			m.click = clickState{row: -1}
			m.cursor = hit.row
			slog.Info("action", "do", "double click", "row", hit.row)
			return m.activateRow(m.rows[hit.row])
		}
		m.pendingRm = ""
		m.cursor = hit.row
		return m, m.refreshPanel()
	case hit.section == secHistory:
		m.click = clickState{row: -1}
		return m.toggleHistory(), nil
	}
	m.click = clickState{row: -1}
	return m, nil
}

// activateRow is Enter with an empty prompt line, and a double click: a
// work session's tab opens (or is remade when the row is offline), a plain
// tab is jumped to, a worker's full-screen view opens.
func (m model) activateRow(r row) (tea.Model, tea.Cmd) {
	switch {
	case isWorkRowOf(r):
		name := r.s.Work
		slog.Info("action", "do", "open", "name", name, "state", r.s.State)
		m.status = "opening " + name + "..."
		return m, m.workOpenCmd(name)
	case r.kind == rowSession:
		slog.Info("action", "do", "jump", "id", r.s.ID)
		return m, jumpSessionCmd(r.s)
	}
	return m, m.enterView(r.node, r.t.ID)
}
