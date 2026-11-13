// order.go: the sections of the board, dragging rows between them, and
// naming (Ctrl-R).
//
// Named sessions (rows with a work record) sit in NOW, ACTIVE or LATER by
// the record's priority, in rank order within a section (NOW is what the
// owner is pushing on, now.go). That order is the
// owner's, kept in the records (`work move`), and the board never reorders
// it by state: a session that needs you keeps its glyph, its colour and its
// place in the "N need you" count, not a new row. Unnamed tabs (hook
// sessions with no record) close the LATER section in attention order, the
// way the whole board used to be sorted; Ctrl-R names one, and from then on
// it can be dragged. An unnamed tab shows its iTerm2 tab title in the WORK
// column, in parentheses, so it can be told apart before it has a name
// (titles come from `it2 session list --json` on a slow tick).
//
// A drag (Shift-Up/Down; Ctrl-Up/Down and Option-Up/Down too) swaps a row with its neighbour
// in the same section, or carries it over a boundary: down from the bottom
// of one section lands at the top of the next, up from the top at the
// bottom of the one above. Ranks are then renumbered 10, 20, 30 per section
// and only the records whose (priority, rank) changed are written, through
// `savannah work move`, after the board has already drawn the new order.
//
// Stated limits: there is no mouse drag, the keys are the drag; a move is
// one step per keypress; two boards open at once each write what they last
// saw (the records are the truth, re-read every second).

package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

const (
	secNow    = iota // what the owner is pushing on (now.go)
	secActive        // what they are on, in the background
	secLater
	secHistory // closed (offline) work sessions, at the bottom (history.go)
)

// priorityName is the record value a section is written as (`work move
// --now|--active|--later`); HISTORY has none.
func priorityName(sec int) string {
	switch sec {
	case secNow:
		return "now"
	case secLater:
		return "later"
	}
	return "active"
}

// priorityOf is a record's priority as the board reads it: an unknown or
// missing one is active.
func priorityOf(r workRecord) string {
	switch r.Priority {
	case "now", "later":
		return r.Priority
	}
	return "active"
}

const (
	rankStep     = 10
	titleRefresh = 5 * time.Second // how often it2 is asked for tab titles
)

func sectionName(sec int) string {
	switch sec {
	case secNow:
		return "NOW"
	case secActive:
		return "ACTIVE"
	case secHistory:
		return "HISTORY"
	}
	return "LATER"
}

// sectionOf is a session's section: HISTORY for a closed (offline) work
// session, else its record's priority (an unknown or missing priority is
// active), LATER for an unnamed tab.
func sectionOf(s session, recs map[string]workRecord) int {
	if s.State == "offline" {
		return secHistory
	}
	if s.Work == "" {
		return secLater
	}
	if r, ok := recs[s.Work]; ok {
		switch priorityOf(r) {
		case "now":
			return secNow
		case "later":
			return secLater
		}
	}
	return secActive
}

// rankOf is a named session's position key: the record's rank, else its
// creation time, so records written before ranks existed keep their
// creation order.
func rankOf(s session, recs map[string]workRecord) int64 {
	r, ok := recs[s.Work]
	if !ok {
		return 0
	}
	if r.Rank != 0 {
		return r.Rank
	}
	return r.Created
}

func recordMap(work []workRecord) map[string]workRecord {
	out := make(map[string]workRecord, len(work))
	for _, r := range work {
		out[r.Name] = r
	}
	return out
}

// orderSessions arranges sessions for the board: NOW, ACTIVE, LATER, named
// rows by (rank, name) inside each, unnamed tabs after LATER's named rows in
// the order given (the attention sort from joinWork). Stable, so equal keys
// keep their incoming order.
func orderSessions(sessions []session, work []workRecord) []session {
	recs := recordMap(work)
	out := append([]session{}, sessions...)
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		sa, sb := sectionOf(a, recs), sectionOf(b, recs)
		if sa != sb {
			return sa < sb
		}
		if sa == secHistory {
			return a.Updated > b.Updated // closed sessions: newest first
		}
		if (a.Work == "") != (b.Work == "") {
			return a.Work != "" // named before unnamed
		}
		if a.Work == "" {
			return false // unnamed tabs keep the attention order
		}
		ra, rb := rankOf(a, recs), rankOf(b, recs)
		if ra != rb {
			return ra < rb
		}
		return a.Work < b.Work
	})
	return out
}

// sectionLines is the header line above a section's rows, plus a hint when
// the section is empty (where a drag would land); HISTORY's is its own
// (history.go: nothing when no session is closed).
func (m model) sectionLines(sec, n int) []string {
	if sec == secHistory {
		return historyLines(n, m.history || m.filter != "")
	}
	style := headerStyle
	if sec == secNow {
		style = nowStyle
	}
	out := []string{style.Render(fmt.Sprintf("%s (%d)", sectionName(sec), n))}
	if n == 0 {
		var hint string
		switch sec {
		case secNow:
			hint = "  (empty: Ctrl-P puts the session you are pushing on here)"
		case secActive:
			hint = "  (empty: Shift-Up on a named session below moves it here)"
		default:
			hint = "  (empty: Shift-Down on a session above moves it here)"
		}
		out = append(out, helpStyle.Render(hint))
	}
	return out
}

// ---- dragging ---------------------------------------------------------------

// moveChange is one record's new place: the argv of one `work move`.
type moveChange struct {
	name     string
	priority string // "now" | "active" | "later"
	rank     int64
}

// planDrag moves the named session one step (delta -1 up, +1 down) through
// the ordered names of the sections (NOW, ACTIVE, LATER, in that order).
// ok is false when the row cannot move: unknown, at the top of the first
// section going up, at the bottom of the last going down. The input is
// never modified.
func planDrag(secs [][]string, name string, delta int) (out [][]string, ok bool) {
	out = make([][]string, len(secs))
	for i, names := range secs {
		out[i] = append([]string{}, names...)
	}
	for si, names := range out {
		i := indexOf(names, name)
		if i < 0 {
			continue
		}
		switch {
		case delta < 0 && i > 0:
			names[i], names[i-1] = names[i-1], names[i]
		case delta > 0 && i < len(names)-1:
			names[i], names[i+1] = names[i+1], names[i]
		case delta > 0 && si < len(out)-1: // bottom of a section: into the top of the next
			out[si] = append(names[:i], names[i+1:]...)
			out[si+1] = append([]string{name}, out[si+1]...)
		case delta < 0 && si > 0: // top of a section: onto the bottom of the one above
			out[si] = append(names[:i], names[i+1:]...)
			out[si-1] = append(out[si-1], name)
		default:
			return out, false
		}
		return out, true
	}
	return out, false
}

func indexOf(names []string, name string) int {
	for i, n := range names {
		if n == name {
			return i
		}
	}
	return -1
}

// renumber assigns ranks 10, 20, 30 ... to an ordered section and returns
// the records whose priority or rank differs from what they hold now.
func renumber(names []string, priority string, recs map[string]workRecord) []moveChange {
	var out []moveChange
	for i, n := range names {
		want := int64((i + 1) * rankStep)
		r, ok := recs[n]
		cur := int64(0)
		if ok {
			cur = r.Rank
			if cur == 0 {
				cur = r.Created
			}
		}
		if !ok || priorityOf(r) != priority || cur != want {
			out = append(out, moveChange{name: n, priority: priority, rank: want})
		}
	}
	return out
}

// sectionNames lists the named sessions of NOW, ACTIVE and LATER (indexed
// by section) in rank order, from every session the board knows, shown or
// not: the order the records hold, whatever the grouping or filter hides.
func (m model) sectionNames() [][]string {
	out := make([][]string, secHistory)
	recs := recordMap(m.work)
	for _, s := range orderSessions(m.sessions, m.work) {
		if s.Work == "" {
			continue
		}
		if sec := sectionOf(s, recs); sec < secHistory {
			out[sec] = append(out[sec], s.Work)
		}
	}
	return out
}

// applyMoves is the local half of a move: the board's copy of the records
// takes the new places (so the next frame shows them), the rows are
// rebuilt and the cursor follows the named row.
func (m *model) applyMoves(changes []moveChange, name string) {
	for i := range m.work {
		for _, c := range changes {
			if m.work[i].Name == c.name {
				m.work[i].Priority, m.work[i].Rank = c.priority, c.rank
			}
		}
	}
	m.rebuildRows()
	for i, row := range m.rows {
		if row.kind == rowSession && row.s.Work == name {
			m.cursor = i
		}
	}
}

// renumberAll is every record whose place changed across the sections.
func renumberAll(secs [][]string, recs map[string]workRecord) []moveChange {
	var changes []moveChange
	for sec, names := range secs {
		changes = append(changes, renumber(names, priorityName(sec), recs)...)
	}
	return changes
}

// dragRow is the Shift-Up/Down key: plans the move, applies it to the
// board's copy of the records at once (so the next frame shows it), keeps
// the cursor on the moved row and writes the changed records through the
// CLI. An unnamed tab cannot move: it has no record to hold a rank.
func (m model) dragRow(delta int) (tea.Model, tea.Cmd) {
	r, ok := m.currentRow()
	if !ok || r.kind != rowSession {
		return m, nil
	}
	if r.s.Work == "" {
		m.status = warnStyle.Render("name this tab first (Ctrl-R); then it can be moved")
		return m, nil
	}
	if r.s.State == "offline" {
		m.status = warnStyle.Render("a closed session sits in HISTORY and has no place in the order; Enter reopens it")
		return m, nil
	}
	if m.group {
		// The drag edits the rank order, and the grouped view does not show
		// that order in full: a move here would reorder rows it hides.
		m.status = warnStyle.Render("rows are grouped by repository; Ctrl-G back to your order to move one")
		return m, nil
	}
	if m.filter != "" {
		m.status = warnStyle.Render("rows are filtered; Esc clears the filter to move one")
		return m, nil
	}
	name := r.s.Work
	secs, can := planDrag(m.sectionNames(), name, delta)
	if !can {
		where := "top"
		if delta > 0 {
			where = "bottom"
		}
		m.status = helpStyle.Render(name + " is already at the " + where)
		return m, nil
	}
	changes := renumberAll(secs, recordMap(m.work))
	// Apply locally first: the board redraws in the new order now, and the
	// next tick's read of the records (written below) agrees.
	m.applyMoves(changes, name)
	m.status = ""
	if len(changes) == 0 {
		return m, nil
	}
	slog.Info("action", "do", "move", "name", name, "delta", delta, "writes", len(changes))
	return m, m.workMoveCmd(changes)
}

// moveArgs is the argv of one `work move`.
func moveArgs(c moveChange) []string {
	return workArgs("move", c.name, "--"+c.priority, "--rank="+strconv.FormatInt(c.rank, 10))
}

type workMovedMsg struct{ err string }

// workMoveCmd writes every changed record in order; the first refusal stops
// the rest and lands on the status line. Either way the records are re-read
// right after, so the board shows what was actually written.
func (m model) workMoveCmd(changes []moveChange) tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		for _, c := range changes {
			if _, err := run(bin, moveArgs(c)...); err != nil {
				return workMovedMsg{err: c.name + ": " + cliError(err)}
			}
		}
		return workMovedMsg{}
	}
}

// ---- naming (Ctrl-R) ----------------------------------------------------------

type workTitledMsg struct{ name, title, err string }

// titleArgs is the argv that sets a work session's title, or clears it when
// the text is empty. The name is untouched: `savannah work rename OLD NEW`
// is the CLI's own verb for that.
func titleArgs(name, text string) []string {
	if text == "" {
		return workArgs("title", name, "--clear")
	}
	return workArgs("title", name, "--", text)
}

func (m model) workTitleCmd(name, text string) tea.Cmd {
	bin, args := m.bin, titleArgs(name, text)
	return func() tea.Msg {
		if _, err := run(bin, args...); err != nil {
			return workTitledMsg{name: name, title: text, err: cliError(err)}
		}
		return workTitledMsg{name: name, title: text}
	}
}

// startNaming is Ctrl-R: a plain tab gets a name (adopt, with no note after
// it; words become a title and a derived name), a work session gets its
// title edited. The prompt line takes the text; Enter commits, Esc cancels.
func (m model) startNaming(r row, hasRow bool) (tea.Model, tea.Cmd) {
	if !hasRow || r.kind != rowSession {
		m.status = warnStyle.Render("Ctrl-R names a session; select one")
		return m, nil
	}
	m.noteFor = ""
	if r.s.Work == "" {
		m.adoptFor, m.adoptProject, m.adoptNote = r.s.ID, r.s.Project, false
		m.titleFor = ""
		m.input.SetValue(nameSuggestion(r.s.Project, titleOf(r.s, m.titles), m.takenNames()))
	} else {
		m.adoptFor, m.adoptProject = "", ""
		m.titleFor = r.s.Work
		m.input.SetValue(r.s.WorkTitle)
	}
	m.input.CursorEnd()
	m.status = ""
	return m, nil
}

// ---- tab titles ---------------------------------------------------------------

type titlesMsg struct {
	titles map[string]string // iTerm2 pane id -> cleaned tab title
	err    string
}
type titleTick time.Time

func titleTickCmd() tea.Cmd {
	return tea.Tick(titleRefresh, func(t time.Time) tea.Msg { return titleTick(t) })
}

// titlesCmd asks iTerm2 for every pane's title (`it2 session list --json`).
// Without it2 (another terminal, a test) the result is an error the board
// ignores: titles are a nicety, never required.
func titlesCmd() tea.Cmd {
	argv := it2Argv()
	return func() tea.Msg {
		args := append(append([]string{}, argv[1:]...), "session", "list", "--json")
		out, err := poll(argv[0], args...)
		if err != nil {
			return titlesMsg{err: cliError(err)}
		}
		titles, perr := parseTitles(out)
		if perr != nil {
			return titlesMsg{err: perr.Error()}
		}
		return titlesMsg{titles: titles}
	}
}

// parseTitles maps pane id -> cleaned title from it2's session list. The
// `title` field is the readable one; `name` carries escaped slashes.
func parseTitles(b []byte) (map[string]string, error) {
	var rows []struct {
		ID    string `json:"id"`
		Name  string `json:"name"`
		Title string `json:"title"`
	}
	if err := json.Unmarshal(b, &rows); err != nil {
		return nil, err
	}
	out := make(map[string]string, len(rows))
	for _, r := range rows {
		t := r.Title
		if t == "" {
			t = strings.ReplaceAll(r.Name, `\/`, "/")
		}
		if t = cleanTitle(t); t != "" && r.ID != "" {
			out[r.ID] = t
		}
	}
	return out, nil
}

// titleOf is the tab title for a session's pane, "" when unknown.
func titleOf(s session, titles map[string]string) string {
	if s.ITerm == "" {
		return ""
	}
	return titles[itermUUID(s.ITerm)]
}

// cleanTitle drops the process name iTerm2 appends to a tab title, so the
// owner's own words remain: "demo (claude)" is "demo", "~/ng (-zsh)" is "~/ng".
func cleanTitle(t string) string {
	t = strings.TrimSpace(t)
	if i := strings.LastIndex(t, " ("); i >= 0 && strings.HasSuffix(t, ")") {
		t = t[:i]
	} else if strings.HasPrefix(t, "(") && strings.HasSuffix(t, ")") {
		t = "" // only the process name: no title at all
	}
	return strings.TrimSpace(t)
}

// updateOrder folds the results of moves, renames and title reads into the
// model.
func (m model) updateOrder(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case workMovedMsg:
		if msg.err != "" {
			m.status = warnStyle.Render("move " + clip(msg.err))
		}
		return m, readSessionsCmd()
	case workTitledMsg:
		if msg.err != "" {
			// The text stays in the box so a refused one is a one-edit fix.
			slog.Warn("title failed", "name", msg.name, "err", msg.err)
			m.status = warnStyle.Render("title " + msg.name + ": " + clip(msg.err))
			return m, nil
		}
		m.titleFor = ""
		m.input.SetValue("")
		if msg.title == "" {
			slog.Info("title cleared", "name", msg.name)
			m.status = okStyle.Render("cleared the title of " + msg.name)
		} else {
			slog.Info("titled", "name", msg.name, "title", msg.title)
			m.status = okStyle.Render("titled " + msg.name + ": " + msg.title)
		}
		return m, readSessionsCmd()
	case titleTick:
		return m, tea.Batch(titlesCmd(), titleTickCmd())
	case titlesMsg:
		if msg.err == "" {
			m.titles = msg.titles
		}
		return m, nil
	}
	return m, nil
}
