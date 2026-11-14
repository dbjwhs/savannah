// boardkeys.go: the board's keys and its prompt line.
//
// The prompt line has one job at a time (promptState): by default it is
// the highlighted row's (text for a work session's Claude, a prompt for a
// mesh worker, an empty Enter to open either), and a key can lend it to the
// filter (Ctrl-K), to a name for a plain tab (Ctrl-R, Ctrl-T), to a work
// session's title (Ctrl-R) or to a note (Ctrl-T). Esc gives it back, Enter
// commits what it holds (submitPrompt).
//
// updateBoardKeys is a flat switch: one key, one handler, each a value
// method that returns the model it changed and the command to run.

package main

import (
	"log/slog"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
)

// promptKind is what the board's prompt line is for. The kinds exclude each
// other: one prompt line, one job at a time.
type promptKind int

const (
	promptRow   promptKind = iota // the highlighted row's own: say, send, or an empty Enter
	promptFind                    // the filter (find.go; Ctrl-K)
	promptAdopt                   // a name for a plain tab (Ctrl-R, or Ctrl-T with the note to follow)
	promptTitle                   // the title of a work session (Ctrl-R)
	promptNote                    // a note for a work session (Ctrl-T)
)

// promptState is the prompt line's job and who it is for.
type promptState struct {
	kind     promptKind
	target   string // adopt: the plain tab's hook session id; title, note: the work session's name
	project  string // adopt: that tab's directory, for the label
	thenNote bool   // adopt: Ctrl-T named it, so the note follows; Ctrl-R: just the name
}

// setPrompt gives the prompt line a job, with the text the box starts from.
func (m *model) setPrompt(p promptState, text string) {
	m.prompt = p
	m.input.SetValue(text)
	m.input.CursorEnd()
}

// clearPrompt hands the prompt line back to the highlighted row, empty.
func (m *model) clearPrompt() {
	m.prompt = promptState{}
	m.input.SetValue("")
}

// finding reports whether the prompt line is the filter.
func (m model) finding() bool { return m.prompt.kind == promptFind }

// updateBoardKeys is the board's key map. Work rows (a live tab tagged with
// its work session, or an offline record) get the work verbs; a plain hook
// row keeps jump and dismiss; a mesh worker keeps send, view and rm.
func (m model) updateBoardKeys(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	// A pending Ctrl-X confirm survives exactly one keypress: the next Ctrl-X
	// on the same row. Anything else (a cursor move, a typed character)
	// withdraws it, so a removal is never confirmed by accident.
	pending := m.pendingRm
	m.pendingRm = ""
	switch msg.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc:
		return m.pressEsc()
	case tea.KeyEnter:
		return m.submitPrompt()
	case tea.KeyTab:
		return m.pressTab()
	case tea.KeyCtrlA:
		return m.pressAsk()
	case tea.KeyCtrlB:
		return m.pressOpen()
	case tea.KeyCtrlE:
		return m.toggleHistory(), nil
	case tea.KeyCtrlF:
		return m.toggleFade(), nil
	case tea.KeyCtrlG:
		return m.toggleGroup(), nil
	case tea.KeyCtrlK:
		return m.toggleFind(), nil
	case tea.KeyCtrlL:
		return m.enterLog()
	case tea.KeyCtrlN:
		return m.enterNew()
	case tea.KeyCtrlO:
		return m.pressOutput()
	case tea.KeyCtrlP:
		return m.pressNow()
	case tea.KeyCtrlR:
		return m.startNaming()
	case tea.KeyCtrlS:
		return m.enterSnapshot()
	case tea.KeyCtrlT:
		return m.pressNote()
	case tea.KeyCtrlX:
		return m.pressRm(pending)
	case tea.KeyUp:
		return m.pressArrow(-1, msg.Alt)
	case tea.KeyDown:
		return m.pressArrow(1, msg.Alt)
	// The drag has three spellings, since a terminal or the OS may take one:
	// Shift-arrows (the one the help names), Ctrl-arrows (macOS Mission
	// Control takes these by default) and Option-arrows (pressArrow).
	case tea.KeyShiftUp, tea.KeyCtrlUp:
		return m.dragRow(-1)
	case tea.KeyShiftDown, tea.KeyCtrlDown:
		return m.dragRow(1)
	}
	return m.typeInPrompt(msg)
}

// typeInPrompt is every other key: it goes to the prompt line's text box,
// and while that is the filter the rows follow what is typed.
func (m model) typeInPrompt(msg tea.KeyMsg) (model, tea.Cmd) {
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	if m.finding() {
		m.applyFilter(m.input.Value())
	}
	return m, cmd
}

// stepCursor moves the highlight d rows (an arrow, the wheel) and points
// the detail panel at the row it lands on.
func (m model) stepCursor(d int) (model, tea.Cmd) {
	m.moveCursor(d)
	cmd := m.refreshPanel()
	return m, cmd
}

// pressArrow is Up or Down: the highlight moves; with Option held it is a
// third spelling of the drag (see Shift-Up).
func (m model) pressArrow(d int, alt bool) (tea.Model, tea.Cmd) {
	if alt {
		return m.dragRow(d)
	}
	return m.stepCursor(d)
}

// pressEsc is Esc: the filter goes first (being typed or in force), then
// whatever else the prompt line was lent to; with the prompt line the
// row's own, Esc quits.
func (m model) pressEsc() (model, tea.Cmd) {
	if m.finding() || m.filter != "" {
		return m.clearFind(), nil
	}
	switch m.prompt.kind {
	case promptAdopt:
		m.status = "naming cancelled"
	case promptTitle:
		m.status = "title cancelled"
	case promptNote:
		m.status = "note cancelled"
	default:
		return m, tea.Quit
	}
	m.clearPrompt()
	return m, nil
}

// pressTab is the raw jump: a session's tab is focused through it2, a mesh
// worker's live view opens.
func (m model) pressTab() (model, tea.Cmd) {
	r, ok := m.currentRow()
	switch {
	case !ok:
		return m, nil
	case r.kind == rowSession:
		return m, jumpSessionCmd(r.s)
	}
	return m.enterView(r.node, r.t.ID)
}

// pressOutput is Ctrl-O: the output view of the highlighted work session.
func (m model) pressOutput() (model, tea.Cmd) {
	r, ok := m.currentRow()
	if !ok || !r.isWork() {
		m.status = warnStyle.Render("Ctrl-O shows a work session's last replies; select one")
		return m, nil
	}
	return m.enterOutput(r.s.Work)
}

// pressNote is Ctrl-T: the prompt line becomes a note for the highlighted
// work session. A plain tab has nowhere to keep a note until it is a work
// session: it is named first (adopt), and the note follows (updateWork).
func (m model) pressNote() (model, tea.Cmd) {
	r, ok := m.currentRow()
	switch {
	case ok && r.isWork():
		m.setPrompt(promptState{kind: promptNote, target: r.s.Work}, "")
	case ok && r.kind == rowSession:
		m.setPrompt(promptState{kind: promptAdopt, target: r.s.ID, project: r.s.Project, thenNote: true},
			nameSuggestion(r.s.Project, titleOf(r.s, m.titles), m.takenNames()))
	default:
		m.status = warnStyle.Render("Ctrl-T notes a session; select one")
		return m, nil
	}
	m.status = ""
	return m, nil
}

// pressRm is Ctrl-X: a work session is removed on the second press
// (pending is the name the press before this one armed), a plain tab's
// stale session file is dismissed, a finished mesh worker is forgotten.
func (m model) pressRm(pending string) (model, tea.Cmd) {
	r, ok := m.currentRow()
	switch {
	case !ok:
		return m, nil
	case r.isWork() && pending == r.s.Work:
		m.act("rm", "removing "+r.s.Work+"...", "name", r.s.Work)
		return m, m.workVerbCmd(verbRm, r.s.Work, "")
	case r.isWork():
		m.pendingRm = r.s.Work
		m.status = warnStyle.Render("Ctrl-X again to remove " + r.s.Work +
			" (closes its tab, removes a clean worktree, keeps the branch)")
		return m, nil
	case r.kind == rowSession:
		m.act("dismiss", "dismissing session "+r.s.ID+"...", "id", r.s.ID)
		return m, dismissSessionCmd(r.s)
	case r.t.State == "running":
		m.status = warnStyle.Render(r.node + "/" + r.t.ID + " is running (cancel first)")
		return m, nil
	}
	m.act("task rm", "removing "+r.node+"/"+r.t.ID+"...", "node", r.node, "task", r.t.ID)
	return m, m.taskCmd("rm", r.node, r.t.ID)
}

// submitPrompt is Enter: what the prompt line holds goes where its job
// says.
func (m model) submitPrompt() (model, tea.Cmd) {
	text := strings.TrimSpace(m.input.Value())
	switch m.prompt.kind {
	case promptFind:
		return m.acceptFind(), nil
	case promptAdopt:
		return m.submitAdopt(text)
	case promptTitle:
		return m.submitTitle(text)
	case promptNote:
		return m.submitNote(text)
	}
	return m.submitRow(text)
}

// submitAdopt names the plain tab: a name as typed, or any words, and then
// the name is derived and the words become the title (board.go nameFor).
// The prompt stays as it is until the CLI answers, so a refused name is a
// one-edit fix (updateWork).
func (m model) submitAdopt(text string) (model, tea.Cmd) {
	name, title, nerr := nameFor(text, m.takenNames())
	if nerr != "" {
		m.status = warnStyle.Render(nerr)
		return m, nil
	}
	m.act("adopt", "naming it "+name+"...", "name", name, "title", title, "session", m.prompt.target)
	return m, m.workAdoptCmd(name, m.prompt.target, title)
}

// submitTitle sets the work session's title, or clears it on an empty
// Enter. Like a name, the text stays in the box until the CLI answers.
func (m model) submitTitle(text string) (model, tea.Cmd) {
	name := m.prompt.target
	rec, _ := m.recordOf(name)
	switch {
	case text == rec.Title && text == "":
		m.status = warnStyle.Render(name + " has no title yet; type one (the name stays " + name + ")")
	case text == rec.Title:
		m.clearPrompt()
		m.status = "title unchanged"
	case text == "":
		m.act("title clear", "clearing the title of "+name+"...", "name", name)
		return m, m.workVerbCmd(verbTitle, name, "")
	default:
		m.act("title", "titling "+name+"...", "name", name, "title", text)
		return m, m.workVerbCmd(verbTitle, name, text)
	}
	return m, nil
}

// submitNote appends the note to the work session's notes.
func (m model) submitNote(text string) (model, tea.Cmd) {
	name := m.prompt.target
	if text == "" {
		m.status = warnStyle.Render("nothing to note for " + name + " (Esc cancels)")
		return m, nil
	}
	m.clearPrompt()
	m.act("note", "noting "+name+"...", "name", name, "chars", len(text))
	return m, m.workVerbCmd(verbNote, name, text)
}

// submitRow is Enter with the prompt line the row's own: text goes to a
// work session's Claude or to a mesh worker; an empty Enter, and any Enter
// on a plain tab, activates the row (activateRow).
func (m model) submitRow(text string) (model, tea.Cmd) {
	r, ok := m.currentRow()
	switch {
	case !ok:
		return m, nil
	case r.isWork() && text != "":
		name := r.s.Work
		m.input.SetValue("")
		m.act("say", "saying to "+labelOf(r.s)+"...", "name", name, "chars", len(text))
		slog.Debug("say text", "name", name, "head", head([]byte(text), 200))
		return m, m.workVerbCmd(verbSay, name, text)
	case r.kind == rowSession && text != "":
		// A plain tab is a person's session: jump to it, never type.
		m.status = warnStyle.Render(
			"cannot send to a plain tab (not a work session); jumping instead")
		return m.activateRow(r)
	case text == "":
		return m.activateRow(r)
	}
	m.input.SetValue("")
	m.act("task send", "sending to "+r.node+"/"+r.t.ID+"...", "node", r.node, "task", r.t.ID, "chars", len(text))
	return m, m.taskCmd("send", r.node, r.t.ID, text)
}
