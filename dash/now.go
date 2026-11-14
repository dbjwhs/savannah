// now.go: the NOW section (Ctrl-P), what the owner is pushing on.
//
// With thirty tabs, ACTIVE is everything that is live, which is not the
// same as the three things the owner is jamming on this afternoon. NOW is
// a section above ACTIVE for exactly those: the board reads as a timeline,
// NOW, ACTIVE, LATER, HISTORY. A row gets there by one key, Ctrl-P (push,
// promote, priority), a toggle: on an ACTIVE or LATER row it moves the row
// to the bottom of NOW; on a NOW row it sends it back to the top of ACTIVE.
// Shift-Up/Down cross the boundary too, one step at a time (order.go), but
// the toggle is the one key that works from anywhere. An unnamed tab is
// named first, from its tab title, by the same key (adoptForMove), since
// having to name a thing before it can be moved is a chore. Nothing lands in NOW
// by itself: not autoname, not adopt, not new; only Ctrl-P and `work move
// --now`. The state is the record's priority, so the two boards of two
// machines each keep their own NOW, and the board file holds nothing new.
//
// Ctrl-P works while grouped or filtered, since it does not need the
// neighbours on the screen: it reads the rank order from the records and
// appends or prepends. An empty NOW shows its header and a one-line hint,
// like HISTORY's, so the key is found; it is never capped (a NOW of ten
// rows is visible, and pruning it is a habit, not a nag). The fade applies
// in NOW as anywhere: a row said to be pushed on and not touched for three
// hours going faint is the signal, not a bug.

package main

import (
	tea "github.com/charmbracelet/bubbletea"
)

// pressNow is Ctrl-P: the highlighted named session into NOW, or out of
// it onto the top of ACTIVE; the records are renumbered 10, 20, 30 like a
// drag and written through `work move`.
func (m model) pressNow() (model, tea.Cmd) {
	r, ok := m.currentRow()
	if !ok || r.kind != rowSession {
		m.status = warnStyle.Render("Ctrl-P moves a session to NOW; select one")
		return m, nil
	}
	if r.s.closed() {
		m.status = warnStyle.Render("a closed session sits in HISTORY; Enter reopens it, then Ctrl-P")
		return m, nil
	}
	name := r.s.Work
	var adopt tea.Cmd
	if name == "" {
		// An unnamed tab is named from its tab title first (order.go), then
		// pushed like any other.
		name, adopt = m.adoptForMove(r.s, prioActive)
	}
	secs := m.sectionNames()
	var into int
	if indexOf(secs[secNow], name) >= 0 {
		secs[secNow] = without(secs[secNow], name)
		secs[secActive] = append([]string{name}, secs[secActive]...)
		into = secActive
	} else {
		secs[secActive] = without(secs[secActive], name)
		secs[secLater] = without(secs[secLater], name)
		secs[secNow] = append(secs[secNow], name)
		into = secNow
	}
	changes := renumberAll(secs, recordMap(m.work))
	m.applyMoves(changes, name)
	if into == secNow {
		m.status = okStyle.Render(name + " is in NOW (Ctrl-P again sends it back to ACTIVE)")
	} else {
		m.status = name + " is back at the top of ACTIVE"
	}
	action("now", "name", name, "section", sectionName(into), "writes", len(changes))
	if adopt != nil {
		m.status = "naming it " + name + " and moving it to NOW..."
		return m, tea.Sequence(adopt, m.workMoveCmd(changes))
	}
	if len(changes) == 0 {
		return m, nil
	}
	return m, m.workMoveCmd(changes)
}

// countNow is the number of sessions in NOW, shown or not.
func (m model) countNow() int {
	return len(m.sectionNames()[secNow])
}

// without is names less one.
func without(names []string, name string) []string {
	out := names[:0:0]
	for _, n := range names {
		if n != name {
			out = append(out, n)
		}
	}
	return out
}
