// boardview.go: the board screen drawn. From the top: the header line, the
// LOCAL SESSIONS table (board.go) and with --mesh the MESH WORKERS table
// (mesh.go), the detail panel in whatever height is left (board.go), then
// the prompt line, the status line and the key help.

package main

import (
	"fmt"
	"strings"
	"time"
)

// viewBoard is the board: header, the tables, the detail panel sized to the
// room left (board.go), the prompt line, status and help. It is built as
// lines so the panel can take exactly what the terminal has left; the whole
// thing is never taller than the terminal.
func (m model) viewBoard() string {
	width := m.boardWidth()
	top, _ := m.topLines(width, m.headerLine())
	bottom := m.promptLines(width)
	panelH := 0
	if m.height > 0 {
		panelH = m.height - len(top) - len(bottom)
	}
	lines := top
	lines = append(lines, m.panelLines(width, panelH, time.Now())...)
	lines = append(lines, bottom...)
	return strings.Join(lines, "\n")
}

// headerLine is the board's first line: the scope, what is on the board,
// the clock and the one number that matters.
func (m model) headerLine() string {
	nSess, nTask := m.sectionCounts()
	scope := "work"
	if m.mesh.on {
		scope = "mesh"
		if m.mesh.pin != "" {
			scope = m.mesh.pin
		}
	}
	counts := fmt.Sprintf("%d session%s", nSess, plural(nSess))
	if closed := m.countClosed(); closed > 0 {
		counts += fmt.Sprintf("   %d closed", closed)
	}
	if m.filter != "" {
		counts += fmt.Sprintf("   matching %q", m.filter)
	}
	if n := m.countNow(); n > 0 {
		counts += fmt.Sprintf("   %d now", n)
	}
	if m.group {
		counts += "   by repository"
	}
	if p := m.countPolling(); p > 0 {
		counts += fmt.Sprintf("   %d polling", p)
	}
	if m.mesh.on {
		counts += fmt.Sprintf("   %d worker%s", nTask, plural(nTask))
	}
	// One attention number over both worlds: sessions needing you + pending
	// mesh decisions. This is the whole point: one glance, total.
	attention := countSessionAttention(m.sessions) + countDecisions(m.rows)
	deco := ""
	if attention > 0 {
		deco = "   " + needStyle.Render(fmt.Sprintf("%d need you", attention))
	}
	return fmt.Sprintf("%s   %s   %s%s", headerStyle.Render("savannah  "+scope), counts, clock(), deco)
}

// topLines is the top of the board: the header line given, a blank, the
// LOCAL SESSIONS table and, with --mesh, the MESH WORKERS table, each
// followed by a blank; and, line for line, what the mouse would hit there
// (mouse.go). It is what viewBoard draws and what a click is read against.
func (m model) topLines(width int, header string) ([]string, []lineHit) {
	var top hitLines
	top.add(noHit, header)
	top.add(noHit, "")
	top.extend(m.localLines(width))
	top.extend(m.meshLines(width))
	return top.lines, top.hits
}

// localLines is the LOCAL SESSIONS section (open Claude Code tabs, via the
// hook, and the work sessions): its header, the table, the read errors and
// a closing blank. The section shows when there are rows OR a read error to
// report: an error with zero rows must still be visible, not hidden by the
// guard.
func (m model) localLines(width int) hitLines {
	var out hitLines
	nSess, _ := m.sectionCounts()
	if nSess == 0 && m.sessErr == "" && m.workErr == "" {
		return out
	}
	out.add(noHit, headerStyle.Render("LOCAL SESSIONS"))
	if nSess > 0 {
		out.extend(m.sessionLines(sessionColumns(width, m.sessions, m.home, m.titles, m.group), nowUnix()))
	}
	if m.sessErr != "" {
		out.add(noHit, warnStyle.Render("  sessions: "+m.sessErr+" (showing last good list)"))
	}
	if m.workErr != "" {
		out.add(noHit, warnStyle.Render("  work: "+m.workErr+" (showing last good list)"))
	}
	out.add(noHit, "")
	return out
}

// promptLines is the bottom of the board: a blank line, the prompt line
// (whose label says what Enter will do for the highlighted row), a blank
// line, the status line (a CLI error can be long) and the key help.
func (m model) promptLines(width int) []string {
	return []string{"", m.promptLine(width), "", m.status, m.helpLine(width)}
}

// promptLine is the prompt line for the job it has (boardkeys.go): the
// filter, a name, a title or a note being typed, else the highlighted
// row's own (rowPrompt).
func (m model) promptLine(width int) string {
	room := max(width-70, 8)
	switch m.prompt.kind {
	case promptFind:
		return headerStyle.Render("find") + ":  " +
			m.inputView("words to match: name, title, ticket, path, branch, prompt, summary, note (Enter keeps the filter, Esc clears it)")
	case promptAdopt:
		ph := "a name, or any words (the name is derived); Enter makes it a work session, Esc cancels"
		if m.prompt.thenNote {
			ph = "a name, or any words; Enter makes it a work session and asks for the note, Esc cancels"
		}
		return headerStyle.Render(trunc("name this tab ("+projectLabel(m.prompt.project)+")", room)) + ":  " +
			m.inputView(ph)
	case promptTitle:
		return headerStyle.Render(trunc("title for "+m.prompt.target, room)) + ":  " +
			m.inputView("the words the board shows for it (Enter saves, empty Enter clears, Esc cancels); the name stays")
	case promptNote:
		return headerStyle.Render(trunc("note for "+m.prompt.target, room)) + ":  " +
			m.inputView("what you decided or learned (Enter saves, Esc cancels)")
	}
	return m.rowPrompt(width, room)
}

// rowPrompt is the prompt line when it is the highlighted row's: what
// Enter will do there.
func (m model) rowPrompt(width, room int) string {
	r, ok := m.currentRow()
	switch {
	case !ok:
		hint := "no rows yet: Ctrl-N opens a work session (a tab, a worktree, a first prompt)"
		if m.mesh.on {
			hint = "no rows yet: Ctrl-N opens a work session; workers appear as nodes are found"
		}
		return helpStyle.Render(hint)
	case r.isWork():
		placeholder := "text for its Claude; empty Enter opens the tab"
		if r.s.closed() {
			placeholder = "empty Enter reopens the tab (claude --continue); text needs a live tab"
		}
		return headerStyle.Render(trunc("say to "+labelOf(r.s), room)) + ":  " + m.inputView(placeholder)
	case r.kind == rowSession:
		hint := "a plain tab (no work session): Enter jumps to it, Ctrl-R names it"
		return headerStyle.Render(fitLabel(projectLabel(r.s.Project), width, hint)) +
			"   " + helpStyle.Render(hint)
	}
	return "send to " + headerStyle.Render(r.node+"/"+r.t.ID) +
		":  " + m.inputView("prompt for this worker; empty Enter opens the live view")
}

// helpLine is the key help. One line, never wrapped (a wrapped help line
// would push the layout down): hints are dropped from the end until the
// line fits, so a narrow terminal loses "^C quit" (Esc quits too) before
// anything else.
func (m model) helpLine(width int) string {
	parts := []string{"Shift-up/down move", "Enter say", "^P now", "^K find", "^N new", "^R name", "^G group", "^F fade", "^B open", "^O out",
		"^A ask", "^T note", "^S snap", "^L log", "^X rm", "^C quit"}
	if m.mesh.on {
		parts[1] = "Enter say/send"
		parts = append(parts[:2], append([]string{"Tab view"}, parts[2:]...)...)
	}
	if m.group {
		parts[indexOf(parts, "^G group")] = "^G ungroup"
	}
	return helpStyle.Render(fitHelp(parts, width))
}

// inputView renders the prompt line's text box with a placeholder that says
// what Enter will do for the highlighted row.
func (m model) inputView(placeholder string) string {
	m.input.Placeholder = placeholder
	return m.input.View()
}

// labelOf is what the board calls a work session in prompts: its title when
// it has one, else its name.
func labelOf(s session) string {
	if s.WorkTitle != "" {
		return s.WorkTitle
	}
	return s.Work
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
