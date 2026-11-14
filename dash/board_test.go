package main

import (
	"reflect"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// wideBoard is boardWith plus the record data the wide layout and the panel
// show: a tab id, a first prompt and two notes on "live", a last prompt on
// its hook session, and a terminal of the given size.
func wideBoard(t *testing.T, width, height int) model {
	t.Helper()
	m := boardWith(t, true)
	m.width, m.height = width, height
	for i := range m.work {
		if m.work[i].Name == "live" {
			m.work[i].TabID = "12"
			m.work[i].Prompt = "fix the parser"
			m.work[i].notes = "## 2026-11-03 15:25 local\nstarted from the board\n\n## 2026-11-03 16:40 local\nparser bug is in lexer.cpp line 40\n\n"
			m.work[i].lastNote = "parser bug is in lexer.cpp line 40"
		}
	}
	for i := range m.hooks {
		if m.hooks[i].ID == "s1" {
			m.hooks[i].LastPrompt = "now add a test for it"
		}
	}
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	return m
}

func TestSessionColumns(t *testing.T) {
	rows := []session{
		{Work: "live", Project: "/Users/x/ng/repo/.savannah-worktrees/live"},
		{Project: "/Users/x/ng/other"},
	}
	compact := sessionColumns(100, rows, "/Users/x", nil, false)
	if compact.wide || compact.note != 0 || compact.what != 40 || compact.project != 22 {
		t.Errorf("compact plan at 100: %+v", compact)
	}
	for _, w := range []int{120, 160, 250} {
		c := sessionColumns(w, rows, "/Users/x", nil, false)
		if !c.wide || c.note < 8 || c.what < 12 {
			t.Errorf("wide plan at %d: %+v", w, c)
		}
		sum := (2 + 2) + (9 + 2) + (c.work + 2) + (c.project + 2) + (c.tab + 2) + (c.age + 2) + (c.note + 2) + c.what
		if sum > w {
			t.Errorf("plan at %d sums to %d: %+v", w, sum, c)
		}
		if !c.paths || c.project != len("~/ng/repo/.savannah-worktrees/live") {
			t.Errorf("at %d PROJECT should hold the longest ~ path: %+v", w, c)
		}
	}
	// A path too long for 40 percent of the room falls back to labels.
	long := []session{{Project: "/" + strings.Repeat("deep/", 30) + "end"}}
	if c := sessionColumns(120, long, "", nil, false); c.paths || c.project > 40 {
		t.Errorf("long path should use labels: %+v", c)
	}
	if c := sessionColumns(0, nil, "", nil, false); c.wide || c.what != 40 {
		t.Errorf("unknown width should plan for 100 columns: %+v", c)
	}
}

func TestWideViewHasNoteAndTabColumns(t *testing.T) {
	m := wideBoard(t, 200, 0) // no height: no panel, the table alone
	cursorTo(t, &m, hookRow("s2"))
	out := m.View()
	hdr := ""
	for _, ln := range strings.Split(out, "\n") {
		if strings.Contains(ln, "STATE") && strings.Contains(ln, "WHAT") {
			hdr = ln
		}
	}
	for _, col := range []string{"WORK", "PROJECT", "TAB", "AGE", "NOTE", "WHAT"} {
		if !strings.Contains(hdr, col) {
			t.Errorf("wide header missing %s: %q", col, hdr)
		}
	}
	for _, want := range []string{"/r/.savannah-worktrees/live", "parser bug is in lexer.cpp line 40", "  12  "} {
		if !strings.Contains(out, want) {
			t.Errorf("wide view missing %q:\n%s", want, out)
		}
	}
	// The offline record's row says what Enter does; its note sits in NOTE.
	if !strings.Contains(out, "offline, no live tab (Enter reopens it)") {
		t.Errorf("offline row should explain Enter:\n%s", out)
	}
	for _, ln := range plainLines(out) {
		if n := len([]rune(ln)); n > 200 {
			t.Errorf("line wider than 200 (%d): %q", n, ln)
		}
	}
	// Narrow terminals keep the compact table: no NOTE column, nothing wider than 100.
	m.width = 100
	out = m.View()
	for _, ln := range plainLines(out) {
		if strings.Contains(ln, "STATE") && strings.Contains(ln, "NOTE") {
			t.Errorf("compact header should have no NOTE column: %q", ln)
		}
		if n := len([]rune(ln)); n > 100 {
			t.Errorf("compact line wider than 100 (%d): %q", n, ln)
		}
	}
}

func TestPanelForWorkRow(t *testing.T) {
	m := wideBoard(t, 140, 40)
	cursorTo(t, &m, workRow("live"))
	cmd := m.refreshPanel()
	if cmd == nil || m.panel.name != "live" {
		t.Fatalf("selecting a work row should fetch its reply: cmd=%v panel=%+v", cmd != nil, m.panel)
	}
	out := m.View()
	for _, want := range []string{"NOTES (2)", "LAST REPLY", "started from the board", "parser bug is in lexer.cpp",
		"15:25", "16:40", "last prompt  now add a test for it", "tab 12", "fetching..."} {
		if !strings.Contains(out, want) {
			t.Errorf("panel missing %q:\n%s", want, out)
		}
	}
	// A reply lands: the CLI heading loses its "## ", the text shows.
	m, _ = send(m, panelOutputMsg{seq: m.panel.seq, text: "## 2026-11-03 16:41 local  you: now add a test\nAdded test_lexer.cpp; it fails on the bug.\n"})
	out = m.View()
	if !strings.Contains(out, "2026-11-03 16:41 local you: now add a test") || strings.Contains(out, "## 2026") ||
		!strings.Contains(out, "Added test_lexer.cpp; it fails on the bug.") || strings.Contains(out, "fetching...") {
		t.Errorf("reply not shown:\n%s", out)
	}
	// A stale fetch (older seq) is ignored.
	next, _ := send(m, panelOutputMsg{seq: m.panel.seq - 1, text: "stale"})
	if strings.Contains(next.View(), "stale") {
		t.Error("stale panel fetch was shown")
	}
	// The same row with a newer hook update refetches, keeping the old text up.
	for i := range m.sessions {
		if m.sessions[i].Work == "live" {
			m.sessions[i].Updated++
		}
	}
	m.rebuildRows()
	if cmd := m.refreshPanel(); cmd == nil || !strings.Contains(m.View(), "Added test_lexer.cpp") {
		t.Errorf("updated row should refetch and keep the last reply: cmd=%v", cmd != nil)
	}
	if cmd := m.refreshPanel(); cmd != nil {
		t.Error("unchanged row should not refetch")
	}
	// Every line fits, and the whole view fits the terminal height.
	lines := strings.Split(m.View(), "\n")
	if len(lines) != 40 {
		t.Errorf("view has %d lines for a 40-line terminal", len(lines))
	}
	for _, ln := range plainLines(m.View()) {
		if n := len([]rune(ln)); n > 140 {
			t.Errorf("line wider than 140 (%d): %q", n, ln)
		}
	}
}

func TestPanelForPlainAndOfflineRows(t *testing.T) {
	m := wideBoard(t, 140, 40)
	cursorTo(t, &m, hookRow("s2"))
	if cmd := m.refreshPanel(); cmd != nil || m.panel.name != "" {
		t.Errorf("a plain tab fetches nothing: cmd=%v panel=%+v", cmd != nil, m.panel)
	}
	out := m.View()
	for _, want := range []string{"a plain tab has no notes: Ctrl-T names it", "replies show once the tab is named"} {
		if !strings.Contains(out, want) {
			t.Errorf("plain panel missing %q:\n%s", want, out)
		}
	}
	cursorTo(t, &m, workRow("parked"))
	if cmd := m.refreshPanel(); cmd == nil {
		t.Error("an offline work row still fetches (the transcript may exist)")
	}
	if out := m.View(); !strings.Contains(out, "NOTES (0)") || !strings.Contains(out, "none yet: Ctrl-T adds one") {
		t.Errorf("offline panel:\n%s", out)
	}
	// Too little height: no panel, the view still fits (the board's minimum
	// is 19 lines here: header, four table headers, NOW's and HISTORY's
	// hints, three rows, the prompt block).
	m.height = 19
	out = m.View()
	if strings.Contains(out, "NOTES (") || len(strings.Split(out, "\n")) > 19 {
		t.Errorf("short terminal should drop the panel and fit:\n%s", out)
	}
}

func TestCtrlTOnPlainRowNamesItFirst(t *testing.T) {
	m := wideBoard(t, 140, 40)
	cursorTo(t, &m, hookRow("s2"))
	m, cmd := press(m, tea.KeyCtrlT)
	if cmd != nil || m.promptFor(promptAdopt) != "s2" || m.promptFor(promptNote) != "" || m.input.Value() != "other" {
		t.Fatalf("Ctrl-T on a plain tab: cmd=%v adoptFor=%q noteFor=%q input=%q", cmd != nil, m.promptFor(promptAdopt), m.promptFor(promptNote), m.input.Value())
	}
	if out := m.View(); !strings.Contains(out, "name this tab (other)") {
		t.Errorf("prompt label:\n%s", out)
	}
	// Unusable text or a taken name is refused on the spot.
	m.input.SetValue("???")
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || m.promptFor(promptAdopt) != "s2" || !strings.Contains(m.status, "no name can be made") {
		t.Errorf("unusable text: cmd=%v status=%q", cmd != nil, m.status)
	}
	m.input.SetValue("live")
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || !strings.Contains(m.status, "exists") {
		t.Errorf("taken name: cmd=%v status=%q", cmd != nil, m.status)
	}
	// Words are a title with a derived name; a taken derived name gets -2.
	m.input.SetValue("Live Tab")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "naming it live-tab") {
		t.Errorf("words: cmd=%v status=%q", cmd != nil, m.status)
	}
	m.input.SetValue("Parked")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || !strings.Contains(m.status, "naming it parked-2") {
		t.Errorf("words over a taken name: cmd=%v status=%q", cmd != nil, m.status)
	}
	m.input.SetValue("other")
	m, cmd = press(m, tea.KeyEnter)
	if cmd == nil || m.promptFor(promptAdopt) != "s2" || !strings.Contains(m.status, "naming it other") {
		t.Errorf("adopt Enter: cmd=%v adoptFor=%q status=%q", cmd != nil, m.promptFor(promptAdopt), m.status)
	}
	// The CLI refuses: the name stays for a one-edit fix.
	m, _ = send(m, workAdoptMsg{name: "other", err: "adopt: no live Claude session s2"})
	if m.promptFor(promptAdopt) != "s2" || m.input.Value() != "other" || !strings.Contains(m.status, "no live Claude session") {
		t.Errorf("refused adopt: adoptFor=%q input=%q status=%q", m.promptFor(promptAdopt), m.input.Value(), m.status)
	}
	// It succeeds: straight into the note for the new name.
	m, cmd = send(m, workAdoptMsg{name: "other"})
	if cmd == nil || m.promptFor(promptAdopt) != "" || m.promptFor(promptNote) != "other" || m.input.Value() != "" || !strings.Contains(m.status, "named it other") {
		t.Errorf("adopted: cmd=%v adoptFor=%q noteFor=%q input=%q status=%q", cmd != nil, m.promptFor(promptAdopt), m.promptFor(promptNote), m.input.Value(), m.status)
	}
	if out := m.View(); !strings.Contains(out, "note for other") {
		t.Errorf("note prompt after adopt:\n%s", out)
	}
	// Esc cancels the naming without quitting.
	cursorTo(t, &m, hookRow("s2"))
	m.prompt = promptState{}
	m, _ = press(m, tea.KeyCtrlT)
	m, cmd = press(m, tea.KeyEsc)
	if cmd != nil || m.promptFor(promptAdopt) != "" || m.input.Value() != "" || !strings.Contains(m.status, "naming cancelled") {
		t.Errorf("Esc while naming: cmd=%v adoptFor=%q input=%q status=%q", cmd != nil, m.promptFor(promptAdopt), m.input.Value(), m.status)
	}
	// Ctrl-T on a mesh worker still has nowhere to go.
	m.rows = append(m.rows, row{kind: rowTask, node: "n", t: task{ID: "t-1"}})
	m.cursor = len(m.rows) - 1
	m, cmd = press(m, tea.KeyCtrlT)
	if cmd != nil || m.promptFor(promptAdopt) != "" || !strings.Contains(m.status, "Ctrl-T") {
		t.Errorf("Ctrl-T on a worker: cmd=%v adoptFor=%q status=%q", cmd != nil, m.promptFor(promptAdopt), m.status)
	}
}

func TestAdoptArgs(t *testing.T) {
	got := adoptArgs("other", "s2", "")
	want := []string{"work", "adopt", "other", "--session=s2", "--json"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("adoptArgs = %q, want %q", got, want)
	}
	got = adoptArgs("other-tab", "s2", "Other Tab")
	want = []string{"work", "adopt", "other-tab", "--session=s2", "--title=Other Tab", "--json"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("adoptArgs with a title = %q, want %q", got, want)
	}
}

func TestNameFor(t *testing.T) {
	taken := map[string]bool{"live": true, "fix-the-parser": true}
	cases := []struct {
		text, name, title, err string
	}{
		{"fix-parser", "fix-parser", "", ""},
		{"  fix-parser  ", "fix-parser", "", ""},
		{"Fix the parser", "fix-the-parser-2", "Fix the parser", ""},
		{"Main Claude Code Session", "main-claude-code-session", "Main Claude Code Session", ""},
		{"live", "", "", "exists"},
		{"Live", "live-2", "Live", ""},
		{"", "", "", "a name is needed"},
		{"???", "", "", "no name can be made"},
		{"t-123", "", "", "no name can be made"}, // the mesh id shape is reserved, and slugs to itself
	}
	for _, c := range cases {
		name, title, err := nameFor(c.text, taken)
		if name != c.name || title != c.title || !strings.Contains(err, c.err) || (c.err == "" && err != "") {
			t.Errorf("nameFor(%q) = %q, %q, %q; want %q, %q, %q", c.text, name, title, err, c.name, c.title, c.err)
		}
	}
}

func TestNameSuggestion(t *testing.T) {
	taken := map[string]bool{"demo": true}
	cases := []struct{ project, title, want string }{
		{"/x/other", "Local testing", "Local testing"}, // words: kept as the title to be
		{"/x/other", "demo", "demo-2"},                 // already a name: the name rule applies
		{"/x/other", "~/ng/dbjwhs", "dbjwhs"},          // a path title is no title: its last part names it
		{"/x/other", "/abs", "abs"},
		{"/x/other", "", "other"},
		{"/x/other", "???", "other"},
	}
	for _, c := range cases {
		if got := nameSuggestion(c.project, c.title, taken); got != c.want {
			t.Errorf("nameSuggestion(%q, %q) = %q, want %q", c.project, c.title, got, c.want)
		}
	}
}

func TestSuggestName(t *testing.T) {
	taken := map[string]bool{"savannah": true, "savannah-2": true}
	cases := []struct{ project, want string }{
		{"/Users/x/ng/dbjwhs/savannah", "savannah-3"},
		{"/Users/x/My Repo", "my-repo"},
		{"/Users/x/ng/repo/.savannah-worktrees/feat", "feat"},
		{"/Users/x/.dotdir", "dotdir"},
		{"/Users/x/t-123", "tab"}, // the mesh id shape is reserved, so the fallback
		{"/", "tab"},
		{"", "tab"},
	}
	for _, c := range cases {
		got := suggestName(c.project, "", taken)
		if got != c.want || !validWorkName(got) || taken[got] {
			t.Errorf("suggestName(%q) = %q, want %q", c.project, got, c.want)
		}
	}
	long := "/x/" + strings.Repeat("abcdefghij", 8)
	if got := suggestName(long, "", nil); len(got) > 60 || !validWorkName(got) {
		t.Errorf("long name not clipped: %q", got)
	}
}

func TestParseNotesAndLines(t *testing.T) {
	notes := "## 2026-11-03 15:25 local\nfirst note\nsecond line\n\n## 2026-11-02 09:00 local\nolder one\n\n"
	got := parseNotes(notes)
	want := []noteEntry{
		{stamp: "2026-11-03 15:25", body: "first note\nsecond line"},
		{stamp: "2026-11-02 09:00", body: "older one"},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("parseNotes = %+v, want %+v", got, want)
	}
	// A "## " line of another shape (a heading inside a noted reply) is body.
	noted := "## 2026-11-10 10:12 local\n## Status\nreviewing\n(asked: what?)\n\n"
	if got := parseNotes(noted); len(got) != 1 || got[0].body != "## Status\nreviewing\n(asked: what?)" {
		t.Errorf("a heading inside a note body should stay in the body: %+v", got)
	}
	if got := parseNotes("no heading at all\n"); len(got) != 1 || got[0].stamp != "" || got[0].body != "no heading at all" {
		t.Errorf("headless notes: %+v", got)
	}
	if got := parseNotes(""); got != nil {
		t.Errorf("empty notes: %+v", got)
	}
	lines := noteLines(want, 30, "2026-11-03")
	joined := strings.Join(lines, "\n")
	for _, s := range []string{"15:25", "11-02 09:00", "first note", "       second line", "older one"} {
		if !strings.Contains(joined, s) {
			t.Errorf("noteLines missing %q:\n%s", s, joined)
		}
	}
	if strings.Contains(joined, "11-03 15:25") {
		t.Errorf("today's stamp should be the time alone:\n%s", joined)
	}
	for _, ln := range lines {
		if visibleLen(ln) > 30 {
			t.Errorf("note line wider than 30: %q", ln)
		}
	}
}

func TestWrapWordsAndTailFit(t *testing.T) {
	if got := wrapWords("the quick brown fox jumps", 10); !reflect.DeepEqual(got, []string{"the quick", "brown fox", "jumps"}) {
		t.Errorf("wrapWords = %q", got)
	}
	if got := wrapWords("abcdefghijkl end", 5); !reflect.DeepEqual(got, []string{"abcde", "fghij", "kl", "end"}) {
		t.Errorf("wrapWords long word = %q", got)
	}
	if got := wrapWords("", 5); !reflect.DeepEqual(got, []string{""}) {
		t.Errorf("wrapWords empty = %q", got)
	}
	if got := wrapAll("a\n\nb c", 1); !reflect.DeepEqual(got, []string{"a", "", "b", "c"}) {
		t.Errorf("wrapAll = %q", got)
	}
	short := tailFit([]string{"x"}, 3, 20)
	if !reflect.DeepEqual(short, []string{"x", "", ""}) {
		t.Errorf("tailFit pads: %q", short)
	}
	long := tailFit([]string{"1", "2", "3", "4", "5"}, 3, 60)
	if len(long) != 3 || !strings.Contains(long[0], "3 more lines above") || long[1] != "4" || long[2] != "5" {
		t.Errorf("tailFit keeps the end: %q", long)
	}
	if got := tailFit([]string{"1"}, 0, 10); got != nil {
		t.Errorf("tailFit with no room: %q", got)
	}
	if got := padTo("ab", 4); got != "ab  " {
		t.Errorf("padTo = %q", got)
	}
	if got := padTo("abcdef", 4); got != "abcd" {
		t.Errorf("padTo cuts plain text: %q", got)
	}
	styled := "\x1b[2mab\x1b[0m"
	if got := padTo(styled, 4); got != styled+"  " || visibleLen(got) != 4 {
		t.Errorf("padTo keeps styled text whole: %q", got)
	}
	if got := shortStamp("2026-11-03 15:25", "2026-11-03"); got != "15:25" {
		t.Errorf("shortStamp today = %q", got)
	}
	if got := shortStamp("2026-11-02 15:25", "2026-11-03"); got != "11-02 15:25" {
		t.Errorf("shortStamp other day = %q", got)
	}
	if got := shortStamp("odd", "2026-11-03"); got != "odd" {
		t.Errorf("shortStamp odd = %q", got)
	}
}

func TestInputWidthFollowsTerminal(t *testing.T) {
	m := wideBoard(t, 100, 30)
	m, _ = send(m, tea.WindowSizeMsg{Width: 220, Height: 60})
	if m.input.Width != 188 || m.width != 220 || m.height != 60 {
		t.Errorf("after resize: input=%d width=%d height=%d", m.input.Width, m.width, m.height)
	}
	if inputWidth(40) != 20 || inputWidth(0) != 64 {
		t.Errorf("inputWidth floors: %d %d", inputWidth(40), inputWidth(0))
	}
}

func TestPanelLinesExactHeight(t *testing.T) {
	m := wideBoard(t, 140, 40)
	cursorTo(t, &m, workRow("live"))
	for _, h := range []int{5, 8, 20} {
		lines := m.panelLines(140, h, time.Date(2026, 11, 3, 22, 0, 0, 0, time.Local))
		if len(lines) != h {
			t.Errorf("panelLines(h=%d) gave %d lines", h, len(lines))
		}
	}
	if lines := m.panelLines(140, minPanel-1, time.Now()); lines != nil {
		t.Errorf("panel below minPanel should be nil, got %d lines", len(lines))
	}
	// One column under twoColWidth: the reply follows the notes.
	narrow := m.panelLines(80, 12, time.Now())
	joined := strings.Join(narrow, "\n")
	if len(narrow) != 12 || !strings.Contains(joined, "NOTES (2)") || !strings.Contains(joined, "LAST REPLY") || strings.Contains(joined, "│") {
		t.Errorf("one-column panel:\n%s", joined)
	}
}
