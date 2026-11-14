// board.go: the board screen's layout. The sessions table uses the whole
// width (a NOTE and a TAB column appear once there is room, PROJECT grows
// into the full path), in two sections, ACTIVE and LATER (order.go), and a
// detail panel for the highlighted row uses the rest of the height: its
// notes beside the last reply of its Claude.
//
// The panel's reply comes from `savannah work output <name> --last 1`,
// fetched when the highlighted row changes and whenever that row's hook
// file is updated (the hook fires on every prompt and every finished turn),
// never on a timer. Notes come from notes.md, which the sessions read already
// loads every tick.
//
// Stated limits: wrapping counts runes, so double-width glyphs can wrap a
// column early; the reply column shows the END of the last reply (Ctrl-O
// shows all of it); a plain tab (no work session) has no notes and no reply
// column until Ctrl-T names it.

package main

import (
	"fmt"
	"regexp"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

const (
	wideWidth   = 120 // from here on the table shows NOTE and TAB
	twoColWidth = 90  // from here on the panel has notes beside the reply
	minPanel    = 5   // lines; with less room the panel is left out
	maxWorkCol  = 28  // a title ("Main Claude Code Session") fits; longer is cut with ~
	maxNoteCol  = 80
)

// sessCols is the sessions table's column plan for one width.
type sessCols struct {
	wide                                        bool // NOTE and TAB columns shown
	paths                                       bool // PROJECT shows ~/the/full/path instead of repo/name
	work, ticket, project, tab, age, note, what int  // ticket is 0 when no row has one (the column is left out)
}

// sessionColumns plans the table for a width. Under wideWidth it is the
// compact plan every terminal fits (fixed cells total 60 columns, WHAT gets
// the rest). From wideWidth on, WORK grows to its longest name, PROJECT to
// the longest full path when every path fits in 40 percent of the room (else
// to the longest repo/name label), and the rest is split between NOTE and
// WHAT. Every cell but WHAT is padded by two columns (pad), so the widths
// here sum to at most the width. Grouped (group.go), PROJECT holds the
// path inside the repository, so it is sized to the longest of those and
// never the full path (the group header carries that).
func sessionColumns(width int, rows []session, home string, titles map[string]string, grouped bool) sessCols {
	if width <= 0 {
		width = defaultWidth
	}
	c := sessCols{work: 12, project: 22, age: 5}
	for _, s := range rows {
		c.ticket = max(c.ticket, runeLen(s.Ticket))
	}
	if c.ticket > 0 {
		c.ticket = min(max(c.ticket, 6), 24)
	}
	if width < wideWidth {
		c.what = width - 60
		if c.ticket > 0 {
			c.what -= c.ticket + 2
		}
		c.what = max(c.what, 12)
		return c
	}
	c.wide = true
	c.tab = 3
	longestName, longestPath, longestLabel := 4, 7, 7
	for _, s := range rows {
		longestName = max(longestName, runeLen(workCell(s, titles)))
		longestPath = max(longestPath, runeLen(tilde(s.Project, home)))
		longestLabel = max(longestLabel, runeLen(projectCell(s, home, grouped)))
	}
	c.work = min(max(longestName, 4), maxWorkCol)
	rest := width - (2 + 2) - (9 + 2) - (c.work + 2) - (c.tab + 2) - (c.age + 2)
	if c.ticket > 0 {
		rest -= c.ticket + 2
	}
	if !grouped && longestPath <= rest*4/10 {
		c.paths = true
		c.project = longestPath
	} else {
		c.project = min(max(longestLabel, 7), 40)
	}
	rest -= c.project + 2
	c.note = min(max(rest*45/100, 8), maxNoteCol)
	c.what = max(rest-c.note-2, 12)
	return c
}

// recordOf finds the work record behind a WORK name, for the cells only the
// record knows (tab id, notes).
func (m model) recordOf(name string) (workRecord, bool) {
	if name == "" {
		return workRecord{}, false
	}
	for _, r := range m.work {
		if r.Name == name {
			return r, true
		}
	}
	return workRecord{}, false
}

// takenNames is every work session name in use, for suggestName.
func (m model) takenNames() map[string]bool {
	out := map[string]bool{}
	for _, r := range m.work {
		out[r.Name] = true
	}
	return out
}

// workCell is the WORK column's text: the title when the session has one,
// else the name, or for an unnamed tab its iTerm2 tab title in parentheses
// (Ctrl-R turns that into a name), or "".
func workCell(s session, titles map[string]string) string {
	if s.Work != "" {
		return labelOf(s)
	}
	if t := titleOf(s, titles); t != "" {
		return "(" + t + ")"
	}
	return ""
}

// projectCell is the PROJECT column's text: repo/name (projectLabel), or
// grouped the path inside the row's repository (inRepoLabel).
func projectCell(s session, home string, grouped bool) string {
	if !grouped {
		return projectLabel(s.Project)
	}
	return inRepoLabel(s.Project, home)
}

// sessionLines renders the LOCAL SESSIONS table for the column plan: the
// column header once, then each section (NOW, ACTIVE, LATER) under its own
// header with one styled line per session row. Section membership and order
// come from the records (order.go); grouped, a dim header names each
// repository inside a section (group.go). The hits say, line for line,
// which row or section header each line is, for the mouse (mouse.go).
func (m model) sessionLines(c sessCols, now int64) hitLines {
	var out hitLines
	out.add(noHit, sessionHeader(c))
	recs := recordMap(m.work)
	counts, groupCounts := m.sessionCounts(recs)
	section := -1
	group := ""
	for i, r := range m.rows {
		if r.kind != rowSession {
			continue
		}
		s := r.s
		for sec := sectionOf(s, recs); section < sec; {
			section++
			group = ""
			out.addSection(section, m.sectionLines(section, counts[section]))
		}
		if m.group {
			if root, isRepo := repoRoot(s.Project); root != group || group == "" {
				group = root
				n := groupCounts[[2]string{sectionName(section), root}]
				out.add(noHit, groupHeader(root, isRepo, n, m.home, c.wide))
			}
		}
		rec, hasRec := recs[s.Work]
		style := rowStyleAt(s, now, m.fade)
		if i == m.cursor {
			style = selectedStyle
		}
		out.add(lineHit{row: i, section: -1}, style.Render(m.sessionLine(s, rec, hasRec, c, now)))
	}
	for section < secHistory {
		section++
		out.addSection(section, m.sectionLines(section, counts[section]))
	}
	return out
}

// sessionHeader is the table's column header for the plan.
func sessionHeader(c sessCols) string {
	hdr := pad("", 2) + pad("STATE", 9) + pad("WORK", c.work)
	if c.ticket > 0 {
		hdr += pad("TICKET", c.ticket)
	}
	hdr += pad("PROJECT", c.project)
	if c.wide {
		hdr += pad("TAB", c.tab)
	}
	hdr += pad("AGE", c.age)
	if c.wide {
		hdr += pad("NOTE", c.note)
	}
	return colStyle.Render(hdr + "WHAT")
}

// sessionCounts is the number each header shows: the session rows per
// section, and per (section, repository) for the group headers. HISTORY
// counts every closed session, shown or not (history.go); filtered, the
// matching ones.
func (m model) sessionCounts(recs map[string]workRecord) (counts [secHistory + 1]int, groups map[[2]string]int) {
	groups = map[[2]string]int{} // (section, repo root) -> rows
	for _, r := range m.rows {
		if r.kind == rowSession {
			sec := sectionOf(r.s, recs)
			counts[sec]++
			groups[[2]string{sectionName(sec), groupKey(r.s)}]++
		}
	}
	if m.filter == "" {
		counts[secHistory] = m.countClosed()
	}
	return counts, groups
}

// sessionLine is one session's row, unstyled: its cells for the column
// plan. The state is the hook's, or "polling" while its Claude's progress
// report is live (polling.go).
func (m model) sessionLine(s session, rec workRecord, hasRec bool, c sessCols, now int64) string {
	state, glyph, what := s.State, stateGlyph(s.State), sessionDetail(s)
	if isPolling(s, rec) {
		state, glyph, what = "polling", pollGlyph, pollingDetail(rec.status, now)
	}
	proj := projectCell(s, m.home, m.group)
	if c.paths {
		proj = tilde(s.Project, m.home)
	}
	cell := workCell(s, m.titles)
	if c.work > 2 && runeLen(cell) > c.work {
		if s.Work == "" {
			cell = cutTilde(cell, c.work-1) + ")" // a cut tab title keeps its shape
		} else {
			cell = cutTilde(cell, c.work)
		}
	}
	line := pad(glyph, 2) + pad(state, 9) + pad(cell, c.work)
	if c.ticket > 0 {
		line += linkify(pad(trunc(s.Ticket, c.ticket), c.ticket), s)
	}
	line += pad(proj, c.project)
	if c.wide {
		tab := "-"
		if hasRec && rec.TabID != "" {
			tab = rec.TabID
		}
		line += pad(tab, c.tab)
	}
	line += pad(humanAge(now-s.Updated), c.age)
	if c.wide {
		note := ""
		if hasRec {
			note = rec.lastNote
		}
		line += pad(note, c.note)
		if s.closed() {
			// The note has its own column now; WHAT says what to do.
			what = "offline, no live tab (Enter reopens it)"
		}
	}
	return line + trunc(what, c.what)
}

// addSection adds a section's header lines: the first is the header (a
// click on HISTORY's toggles it), the rest hints.
func (h *hitLines) addSection(sec int, secLines []string) {
	for i, ln := range secLines {
		hit := noHit
		if i == 0 {
			hit = lineHit{row: -1, section: sec}
		}
		h.add(hit, ln)
	}
}

// noteEntry is one `work note` entry: its stamp ("2026-11-03 15:25", the
// heading without " local") and its body, blank lines dropped.
type noteEntry struct {
	stamp, body string
}

// parseNotes splits a notes.md written by `work note` into entries. Text
// before the first heading (or a file with none) is an entry with no stamp.
// noteStampRe is a note's heading as the CLI writes it (append_note). A
// "## " line of another shape, such as a heading inside a reply a `work
// ask` noted, is body.
var noteStampRe = regexp.MustCompile(`^## \d{4}-\d{2}-\d{2} \d{2}:\d{2} local$`)

func parseNotes(text string) []noteEntry {
	var out []noteEntry
	cur := -1
	for _, ln := range strings.Split(text, "\n") {
		if noteStampRe.MatchString(ln) {
			out = append(out, noteEntry{stamp: strings.TrimSuffix(strings.TrimSpace(ln[3:]), " local")})
			cur = len(out) - 1
			continue
		}
		if strings.TrimSpace(ln) == "" {
			continue
		}
		if cur < 0 {
			out = append(out, noteEntry{})
			cur = 0
		}
		if out[cur].body != "" {
			out[cur].body += "\n"
		}
		out[cur].body += strings.TrimRight(ln, " \t")
	}
	return out
}

// shortStamp shows a note's time only when it is from today, else its
// month-day and time ("15:25", "11-03 15:25").
func shortStamp(stamp, today string) string {
	if len(stamp) >= 16 && stamp[:10] == today {
		return stamp[11:16]
	}
	if len(stamp) >= 16 {
		return stamp[5:16]
	}
	return stamp
}

// noteLines renders entries for a column w wide, oldest first: the stamp
// (dim), then the body wrapped beside and under it. Every line fits in w
// visible runes, which padTo relies on.
func noteLines(entries []noteEntry, w int, today string) []string {
	var out []string
	for _, e := range entries {
		stamp := shortStamp(e.stamp, today)
		indent := ""
		if stamp != "" {
			indent = strings.Repeat(" ", runeLen(stamp)+2)
		}
		first := true
		for _, ln := range strings.Split(e.body, "\n") {
			for _, piece := range wrapWords(ln, w-runeLen(indent)) {
				if first && stamp != "" {
					out = append(out, helpStyle.Render(stamp)+"  "+piece)
				} else {
					out = append(out, indent+piece)
				}
				first = false
			}
		}
		if first && stamp != "" { // an entry with an empty body
			out = append(out, helpStyle.Render(stamp))
		}
	}
	return out
}

// wrapWords word-wraps one line to w runes (a word longer than w is split).
// An empty line stays one empty line.
func wrapWords(line string, w int) []string {
	w = max(w, 1)
	words := strings.Fields(line)
	if len(words) == 0 {
		return []string{""}
	}
	var out []string
	cur := ""
	for _, word := range words {
		for runeLen(word) > w {
			if cur != "" {
				out = append(out, cur)
				cur = ""
			}
			r := []rune(word)
			out = append(out, string(r[:w]))
			word = string(r[w:])
		}
		switch {
		case cur == "":
			cur = word
		case runeLen(cur)+1+runeLen(word) <= w:
			cur += " " + word
		default:
			out = append(out, cur)
			cur = word
		}
	}
	return append(out, cur)
}

// wrapAll word-wraps every line of a text to w.
func wrapAll(text string, w int) []string {
	var out []string
	for _, ln := range strings.Split(strings.TrimRight(stripCR(text), "\n"), "\n") {
		out = append(out, wrapWords(ln, w)...)
	}
	return out
}

// ---- the detail panel ---------------------------------------------------------

// panelState is the reply column's fetch: which (name, updated) it is for and
// the CLI's text. seq orphans a fetch that comes back after the row changed.
type panelState struct {
	name    string
	updated int64
	seq     int
	text    string
	have    bool // text is the reply for (name, updated)
}

type panelOutputMsg struct {
	seq  int
	text string
}

// panelOutputCmd fetches the last turn of a work session for the panel.
func panelOutputCmd(bin string, seq int, name string) tea.Cmd {
	args := workArgs("output", name, "--last", "1")
	return func() tea.Msg {
		out, err := run(bin, args...)
		if err != nil {
			return panelOutputMsg{seq: seq, text: cliError(err)}
		}
		return panelOutputMsg{seq: seq, text: string(out)}
	}
}

// updatePanel takes a fetched reply, unless the highlighted row has changed
// since it was asked for.
func (m model) updatePanel(msg panelOutputMsg) (tea.Model, tea.Cmd) {
	if msg.seq == m.panel.seq {
		m.panel.text, m.panel.have = msg.text, true
	}
	return m, nil
}

// refreshPanel points the panel at the highlighted row. A work row whose
// (name, updated) differs from the last fetch gets a new fetch; the old text
// stays up until it lands when the name is the same, so a finished turn
// replaces the reply instead of blanking it. Anything else clears the panel.
func (m *model) refreshPanel() tea.Cmd {
	r, ok := m.currentRow()
	if !ok || !r.isWork() {
		m.panel = panelState{seq: m.panel.seq + 1}
		return nil
	}
	name, upd := r.s.Work, r.s.Updated
	if m.panel.name == name && m.panel.updated == upd {
		return nil
	}
	if m.panel.name != name {
		m.panel.text, m.panel.have = "", false
	}
	m.panel.name, m.panel.updated = name, upd
	m.panel.seq++
	return panelOutputCmd(m.bin, m.panel.seq, name)
}

// replyText is the reply column's text for a work row: the CLI's rendering
// of the last turn with its "## " heading stripped, or what is known so far.
func (m model) replyText(name string) string {
	if m.panel.name != name || (!m.panel.have && m.panel.text == "") {
		return "fetching..."
	}
	text := strings.TrimRight(stripCR(m.panel.text), "\n")
	if strings.HasPrefix(text, "## ") {
		text = text[3:]
	}
	return text
}

// panelContent is what the detail panel says about one row, before it is
// laid out: the first line (the name, bold, and what follows it), the
// prompt line, a status and a summary line when there are any, and one or
// two titled columns. Column text is plain so wrapping and padding can
// count runes; only the titles and note stamps carry a style.
type panelContent struct {
	name, head      string
	prompt          string
	status, summary string
	leftTitle       string
	left            func(w int) []string // the column's lines wrapped to w
	rightTitle      string
	right           func(w int) []string // nil: one column
}

// panelLines renders the detail panel for the highlighted row in exactly h
// lines, w wide. h < minPanel (or a very narrow terminal) renders nothing.
func (m model) panelLines(w, h int, now time.Time) []string {
	if h < minPanel || w < 40 {
		return nil
	}
	r, ok := m.currentRow()
	switch {
	case !ok:
		return nil
	case r.kind == rowTask:
		return renderPanel(taskPanel(r), w, h)
	case r.isWork():
		return renderPanel(m.workPanel(r, now), w, h)
	}
	return renderPanel(m.plainPanel(r), w, h)
}

// taskPanel is the panel for a mesh worker: its last line and any decision
// it is waiting on.
func taskPanel(r row) panelContent {
	t := r.t
	return panelContent{
		name:      r.node + "/" + t.ID,
		head:      fmt.Sprintf("   %s   turn %d   %s", t.State, t.Turns, t.Title),
		prompt:    "worktree  " + t.Worktree,
		leftTitle: "LAST LINE",
		left: func(w int) []string {
			lines := wrapWords(t.LastLine, w)
			if d, pending := pendingDecision(t); pending {
				lines = append(lines, wrapWords(decisionCell(d), w)...)
			}
			return append(lines, "", "Enter (empty) or Tab opens the live view")
		},
	}
}

// workPanel is the panel for a work session: what the record knows on the
// first line, its last (or first) prompt, its progress report and summary,
// then its notes beside the last reply of its Claude.
func (m model) workPanel(r row, now time.Time) panelContent {
	s := r.s
	rec, hasRec := m.recordOf(s.Work)
	polling := isPolling(s, rec)
	c := panelContent{name: s.Work, head: m.workHead(s, rec, hasRec, polling), rightTitle: "LAST REPLY"}
	if s.WorkTitle != "" {
		// The title leads; the name follows, since it is what the CLI takes.
		c.name = s.WorkTitle
		c.head = "   " + s.Work + c.head
	}
	if rec.status.have {
		c.status = statusLine(rec.status, polling, now.Unix())
	}
	if hasRec && rec.Summary != "" {
		c.summary = fmt.Sprintf("summary (%s ago)  %s", humanAge(now.Unix()-rec.SummaryAt),
			strings.Join(strings.Fields(rec.Summary), " "))
	}
	switch {
	case s.LastPrompt != "":
		c.prompt = "last prompt  " + s.LastPrompt
	case hasRec && rec.Prompt != "":
		c.prompt = "first prompt  " + rec.Prompt
	default:
		c.prompt = "no prompt recorded yet"
	}
	var entries []noteEntry
	if hasRec {
		entries = parseNotes(rec.notes)
	}
	today := now.Format("2006-01-02")
	c.leftTitle = fmt.Sprintf("NOTES (%d)", len(entries))
	c.left = func(w int) []string {
		if len(entries) == 0 {
			return []string{"none yet: Ctrl-T adds one"}
		}
		return noteLines(entries, w, today)
	}
	reply := m.replyText(s.Work)
	c.right = func(w int) []string { return wrapAll(reply, w) }
	return c
}

// workHead is what follows a work session's name on the panel's first
// line: its directory, ticket, branch, tab, how it came to be and where it
// sits, then what it is doing.
func (m model) workHead(s session, rec workRecord, hasRec, polling bool) string {
	head := "   " + tilde(s.Project, m.home)
	if s.Ticket != "" {
		head += "   " + linkify(s.Ticket, s)
	}
	if hasRec {
		if rec.Branch != "" {
			head += "   " + rec.Branch
		}
		if rec.TabID != "" {
			head += "   tab " + rec.TabID
		}
		if rec.Adopted {
			head += "   adopted"
		}
		if rec.Priority == prioLater {
			head += "   later"
		}
	}
	if polling {
		return head + "   polling"
	}
	return head + "   " + sessionDetail(s)
}

// plainPanel is the panel for a plain tab: no record, so no notes and no
// reply fetch, only what would give it them.
func (m model) plainPanel(r row) panelContent {
	s := r.s
	c := panelContent{
		name:       projectLabel(s.Project),
		head:       "   " + tilde(s.Project, m.home) + "   " + sessionDetail(s),
		prompt:     "no prompt recorded yet",
		leftTitle:  "NOTES",
		rightTitle: "LAST REPLY",
		left: func(w int) []string {
			return wrapWords("a plain tab has no notes: Ctrl-T names it, then takes one", w)
		},
		right: func(w int) []string {
			return wrapWords("replies show once the tab is named (Ctrl-R; Ctrl-T names it and takes a note); Enter jumps to it", w)
		},
	}
	if s.LastPrompt != "" {
		c.prompt = "last prompt  " + s.LastPrompt
	}
	return c
}

// renderPanel lays a panel out in exactly h lines, w wide: the head lines,
// then the columns side by side from twoColWidth on, else one under the
// other; each column keeps its end (tailFit).
func renderPanel(c panelContent, w, h int) []string {
	out := []string{
		headerStyle.Render(trunc(c.name, w)) + trunc(c.head, w-runeLen(trunc(c.name, w))),
		trunc(c.prompt, w),
	}
	if c.status != "" {
		out = append(out, trunc(c.status, w))
	}
	if c.summary != "" {
		out = append(out, trunc(c.summary, w))
	}
	body := h - len(out) - 1 // the lines so far, then the titles
	if c.right == nil || w < twoColWidth {
		out = append(out, colStyle.Render(trunc(c.leftTitle, w)))
		cols := c.left(w)
		if c.right != nil {
			cols = append(cols, "", c.rightTitle)
			cols = append(cols, c.right(w)...)
		}
		out = append(out, tailFit(cols, body, w)...)
	} else {
		lw := (w - 3) * 45 / 100
		rw := w - 3 - lw
		lcol := tailFit(c.left(lw), body, lw)
		rcol := tailFit(c.right(rw), body, rw)
		out = append(out, colStyle.Render(padTo(c.leftTitle, lw))+" │ "+colStyle.Render(trunc(c.rightTitle, rw)))
		for i := 0; i < body; i++ {
			out = append(out, padTo(lcol[i], lw)+helpStyle.Render(" │ ")+rcol[i])
		}
	}
	for len(out) < h {
		out = append(out, "")
	}
	return out[:h]
}

// tailFit keeps the last n lines of a column (the end is where things are
// now) and pads a short column to n. When lines are dropped the first kept
// line says how many, so a long reply is never mistaken for a short one.
func tailFit(lines []string, n, w int) []string {
	if n < 1 {
		return nil
	}
	if len(lines) > n {
		dropped := len(lines) - n + 1
		lines = append([]string{trunc(fmt.Sprintf("(%d more lines above; Ctrl-O shows all)", dropped), w)},
			lines[len(lines)-n+1:]...)
	}
	out := append([]string{}, lines...)
	for len(out) < n {
		out = append(out, "")
	}
	return out
}

// inputWidth sizes the prompt box to the terminal: the label before it is
// at most about 30 columns.
func inputWidth(width int) int {
	if width <= 0 {
		return 64
	}
	return min(max(width-32, 20), 400)
}

// suggestName proposes a work session name for a plain tab: from its iTerm2
// tab title when there is one (what Claude Code or the owner called it),
// else from its directory's base name; lowered, anything the name rule
// rejects turned into "-", and -2, -3, ... when that name is taken.
func suggestName(project, title string, taken map[string]bool) string {
	name := ""
	for _, src := range []string{title, projectLabel(project)} {
		if name = slugName(src); name != "" {
			break
		}
	}
	if name == "" {
		name = "tab"
	}
	return uniqueName(name, taken)
}

// uniqueName is name, or name-2, name-3, ... when it is taken.
func uniqueName(name string, taken map[string]bool) string {
	if !taken[name] {
		return name
	}
	for i := 2; ; i++ {
		if cand := fmt.Sprintf("%s-%d", name, i); !taken[cand] {
			return cand
		}
	}
}

// nameFor turns the prompt line (or the dialog's Name field) into a work
// session name and, when the text is not itself a name, the title: "Fix the
// parser" is named fix-the-parser and titled as typed; "fix-parser" is just
// the name. A typed name that is taken is refused (the owner meant that
// name); a name derived from words gets -2, -3, ... instead, since the
// words are what they will see.
func nameFor(text string, taken map[string]bool) (name, title, err string) {
	text = strings.TrimSpace(text)
	switch {
	case text == "":
		return "", "", "a name is needed"
	case validWorkName(text):
		if taken[text] {
			return "", "", "work session " + text + " exists; pick another name"
		}
		return text, "", ""
	}
	slug := slugName(text)
	if slug == "" {
		return "", "", "no name can be made from " + text + " (a name needs a letter or digit and is never t-<digits>)"
	}
	return uniqueName(slug, taken), text, ""
}

// nameSuggestion is what the prompt line offers for a plain tab: the tab's
// own title when it reads as one (not a path), so a plain Enter keeps those
// words as the title and derives the name from them; else a name from the
// directory (suggestName).
func nameSuggestion(project, title string, taken map[string]bool) string {
	t := strings.TrimSpace(title)
	if t != "" && !strings.HasPrefix(t, "~") && !strings.HasPrefix(t, "/") && !strings.HasPrefix(t, ".") &&
		!validWorkName(t) && slugName(t) != "" {
		return t
	}
	return suggestName(project, title, taken)
}

// slugName turns free text into a valid work session name, or "" when
// nothing usable remains. Of a path only the last component is used; a run
// of rejected runes becomes one "-".
func slugName(src string) string {
	base := strings.ToLower(strings.TrimRight(strings.TrimSpace(src), "/"))
	if i := strings.LastIndexByte(base, '/'); i >= 0 {
		base = base[i+1:]
	}
	var b strings.Builder
	last := rune(0)
	for _, r := range base {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
		case b.Len() > 0 && (r == '.' || r == '_' || r == '-'):
			b.WriteRune(r)
		case b.Len() > 0 && last != '-':
			r = '-'
			b.WriteRune(r)
		default:
			continue
		}
		last = r
	}
	name := strings.TrimRight(b.String(), ".-_")
	if len(name) > 60 {
		name = strings.TrimRight(name[:60], ".-_")
	}
	if !validWorkName(name) {
		return ""
	}
	return name
}
