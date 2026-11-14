// history.go: closed sessions out of the way (the HISTORY section, Ctrl-E).
//
// A work session whose tab is gone (iTerm2 restarted, the tab closed by
// hand, `work rm` never run) keeps its record and its notes, and Enter on
// it reopens the tab with its transcript, so it is worth keeping on the
// board. It is not worth a line in ACTIVE or LATER: a faint row among the
// live ones is clutter, and twelve of them are a screen. So every offline
// row sits in a third section at the bottom, HISTORY, closed by default:
// one header line with the count and what the section is. Ctrl-E opens it
// (the rows, newest first, with the same keys: Enter reopens, Ctrl-X
// removes, Ctrl-T notes, Ctrl-O output) and closes it again; the choice is
// remembered in the board file ("history": "open"). A closed session has
// no place in the rank order, so Shift-Up/Down is refused on it; reopening
// it puts it back in its section.
//
// What HISTORY is not: the notes `work rm` archived. Those are files under
// the work dir's archive/, not rows.

package main

import "fmt"

// toggleHistory is Ctrl-E: HISTORY opens or closes, the cursor stays on
// the row it was on when that row is still shown, and the choice is
// written to the board file.
func (m model) toggleHistory() model {
	m.history = !m.history
	m.rebuildKeepingCursor()
	n := m.countClosed()
	var status string
	switch {
	case n == 0:
		status = "no closed session (a work session whose tab is gone would sit in HISTORY)"
	case m.history:
		status = fmt.Sprintf("HISTORY open: %d closed session%s, newest first (Enter reopens one, Ctrl-X removes it; Ctrl-E hides them)", n, plural(n))
	default:
		status = fmt.Sprintf("HISTORY closed: %d closed session%s kept with their notes (Ctrl-E shows them)", n, plural(n))
	}
	m.act("history", status, "open", m.history, "closed", n)
	m.remember(func(p *boardPrefs) {
		p.History = ""
		if m.history {
			p.History = "open"
		}
	})
	return m
}

// countClosed is the number of offline work rows, shown or not.
func (m model) countClosed() int {
	n := 0
	for _, s := range m.sessions {
		if s.closed() {
			n++
		}
	}
	return n
}

// historyLines is the HISTORY header and its hint: nothing when no session
// is closed, else the count and, closed, how to open it, or open, what the
// keys do on a row.
func historyLines(n int, open bool) []string {
	if n == 0 {
		return nil
	}
	out := []string{headerStyle.Render(fmt.Sprintf("HISTORY (%d)", n))}
	if open {
		out = append(out, helpStyle.Render("  (closed, newest first: Enter reopens one, Ctrl-X removes it; Ctrl-E hides them)"))
	} else {
		out = append(out, helpStyle.Render("  (closed sessions kept with their notes; Ctrl-E shows them)"))
	}
	return out
}
