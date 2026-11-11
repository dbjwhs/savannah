package main

import (
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func TestTicketsIn(t *testing.T) {
	for _, c := range []struct {
		text string
		slug bool
		want []string
	}{
		{"zfs-DLPX-98816", true, []string{"DLPX-98816"}},
		{"dlpx-qa-gate-DP-2702", true, []string{"DP-2702"}},
		{"dp-2673-mssql-snapsync-throughput-investigation", true, []string{"DP-2673"}},
		{"resume-dlpx-97306-pr-2352-handoff", true, []string{"DLPX-97306", "PR-2352"}},
		{"pr-3141-review-handoff", true, []string{"PR-3141"}},
		{"work/zfs-cp-11610", true, []string{"CP-11610"}},
		{"delphix-release-2026.6.0.0-monitoring", true, nil}, // a seven-letter word is no key
		{"zfs-escl6174-delay-model", true, nil},              // no dash between key and number
		{"resuming-zfs-handoff-session", true, nil},
		{"", true, nil},
		// Free text wants an upper-case key; PR in any case.
		{"TOOL-31974, so the os-upgrade nightly build stops failing. PR #940 (draft)", false, []string{"TOOL-31974", "PR-940"}},
		{"review PR 2353 and DLPX-100202", false, []string{"PR-2353", "DLPX-100202"}},
		{"step-10 of the plan, then dlpx-98816", false, nil},
		{"ESCL-6174 then DP-2817 then DP-2590", false, []string{"ESCL-6174", "DP-2817"}}, // two at most
		{"DP-2817 twice: DP-2817", false, []string{"DP-2817"}},
	} {
		if got := ticketsIn(c.text, c.slug); !reflect.DeepEqual(got, c.want) {
			t.Errorf("ticketsIn(%q, slug=%v) = %v, want %v", c.text, c.slug, got, c.want)
		}
	}
}

// TestTicketFor: the checkout's name first, then the session's name and
// branch, then the title, then the prompt and the summary.
func TestTicketFor(t *testing.T) {
	ng := checkoutLayout(t)
	rec := workRecord{Name: "an-zfs", Branch: "work/an-zfs", Summary: "Working on DP-2817."}
	cases := []struct {
		name string
		s    session
		rec  workRecord
		has  bool
		want string
	}{
		{"checkout beside the repo", session{Project: ng + "/zfs-DLPX-98816/module", Work: "an-zfs", WorkTitle: "DP-1 nope"}, rec, true, "DLPX-98816"},
		{"a clone beside the repo", session{Project: ng + "/zfs-CP-11610"}, workRecord{}, false, "CP-11610"},
		{"main checkout, name", session{Project: ng + "/zfs", Work: "dp-2673-mssql"}, workRecord{Name: "dp-2673-mssql"}, true, "DP-2673"},
		{"main checkout, branch", session{Project: ng + "/zfs", Work: "an-zfs"}, workRecord{Name: "an-zfs", Branch: "work/tool-31974-fix"}, true, "TOOL-31974"},
		{"title", session{Project: ng + "/zfs", Work: "an-zfs", WorkTitle: "TOOL-31974 os-upgrade build"}, rec, true, "TOOL-31974"},
		{"prompt", session{Project: ng + "/zfs", LastPrompt: "review PR #940 please"}, workRecord{}, false, "PR-940"},
		{"summary", session{Project: ng + "/zfs", Work: "an-zfs"}, rec, true, "DP-2817"},
		{"lower-case prompt is no ticket", session{Project: ng + "/notes", LastPrompt: "look at dlpx-98816"}, workRecord{}, false, ""},
		{"nothing", session{Project: ng + "/notes"}, workRecord{}, false, ""},
		{"savannah worktree name", session{Project: ng + "/zfs/.savannah-worktrees/dp-2702-perf", Work: "dp-2702-perf"}, workRecord{Name: "dp-2702-perf"}, true, "DP-2702"},
	}
	for _, c := range cases {
		ticket, _, pr, _ := ticketFor(c.s, c.rec, c.has)
		if got := strings.TrimSpace(ticket + " " + pr); got != c.want {
			t.Errorf("%s: ticketFor = %q, want %q", c.name, got, c.want)
		}
	}
	// The ticket and the pull request are two slots: a checkout named for
	// the ticket and a title naming the PR both show.
	ticket, _, pr, _ := ticketFor(session{Project: ng + "/zfs-DLPX-98816", WorkTitle: "PR #940 review"}, workRecord{}, false)
	if ticket != "DLPX-98816" || pr != "PR-940" {
		t.Errorf("two slots: %q %q", ticket, pr)
	}
}

func TestTicketLinks(t *testing.T) {
	ng := checkoutLayout(t)
	for _, c := range []struct{ remote, want string }{
		{"git@github.com:delphix/zfs.git", "https://github.com/delphix/zfs"},
		{"ssh://git@github.com/delphix/zfs.git", "https://github.com/delphix/zfs"},
		{"https://github.com/delphix/zfs", "https://github.com/delphix/zfs"},
		{"https://github.com/delphix/zfs.git/", "https://github.com/delphix/zfs"},
		{"https://gitlab.com/o/r.git", ""},
		{"", ""},
	} {
		if got := githubWeb(c.remote); got != c.want {
			t.Errorf("githubWeb(%q) = %q, want %q", c.remote, got, c.want)
		}
	}
	for _, c := range []struct{ pattern, want string }{
		{"https://perforce.atlassian.net/browse/{ticket}", "https://perforce.atlassian.net/browse/DLPX-98816"},
		{"https://perforce.atlassian.net/browse/", "https://perforce.atlassian.net/browse/DLPX-98816"},
		{"https://perforce.atlassian.net/browse", "https://perforce.atlassian.net/browse/DLPX-98816"},
	} {
		if got := ticketLink(c.pattern, "DLPX-98816"); got != c.want {
			t.Errorf("ticketLink(%q) = %q, want %q", c.pattern, got, c.want)
		}
	}
	if id, u := prURLIn("see https://github.com/delphix/appliance-build/pull/939 then https://github.com/delphix/appliance-build/pull/940 (draft)"); id != "PR-940" || u != "https://github.com/delphix/appliance-build/pull/940" {
		t.Errorf("prURLIn = %q %q, want the last URL", id, u)
	}
	if id, _ := prURLIn("no url here, PR #940"); id != "" {
		t.Errorf("a number is not a URL: %q", id)
	}

	// The remote is asked once per checkout; a PR by number links through
	// it; a PR by URL in the summary wins over the remote; the ticket links
	// through the pattern when one is set.
	calls := 0
	old, oldPat := gitRemote, ticketURLPattern
	gitRemote = func(dir string) string {
		calls++
		if strings.HasSuffix(dir, "zfs-DLPX-98816") {
			return "git@github.com:delphix/zfs.git"
		}
		return ""
	}
	ticketURLPattern = "https://perforce.atlassian.net/browse/{ticket}"
	repoWebCache.m = map[string]string{}
	t.Cleanup(func() { gitRemote, ticketURLPattern = old, oldPat; repoWebCache.m = map[string]string{} })
	rec := workRecord{Name: "an-zfs", Summary: "Working on it. https://github.com/delphix/zfs/pull/2353"}
	rows := joinWork([]session{
		{ID: "a", Project: ng + "/zfs-DLPX-98816", State: "idle", Updated: nowUnix(), LastPrompt: "review PR #940"},
		{ID: "b", Project: ng + "/zfs/.savannah-worktrees/an-zfs", State: "idle", Updated: nowUnix(), ITerm: "w0t0p0:L"},
		{ID: "c", Project: ng + "/notes", State: "idle", Updated: nowUnix(), LastPrompt: "PR #12 of DP-2817"},
	}, []workRecord{{Name: "an-zfs", Worktree: ng + "/zfs/.savannah-worktrees/an-zfs", ClaudePane: "L", Summary: rec.Summary}})
	by := map[string]session{}
	for _, s := range rows {
		by[s.ID] = s
	}
	a := by["a"]
	if a.Ticket != "DLPX-98816 PR-940" || a.TicketURL != "https://perforce.atlassian.net/browse/DLPX-98816" || a.PRURL != "https://github.com/delphix/zfs/pull/940" {
		t.Errorf("row a: %+v", a)
	}
	if b := by["b"]; b.PR != "PR-2353" || b.PRURL != "https://github.com/delphix/zfs/pull/2353" || b.TicketID != "" {
		t.Errorf("row b (PR by URL from the summary): ticket=%q pr=%q url=%q", b.TicketID, b.PR, b.PRURL)
	}
	if c := by["c"]; c.Ticket != "PR-12 DP-2817" && c.Ticket != "DP-2817 PR-12" || c.PRURL != "" || c.TicketURL == "" {
		t.Errorf("row c (no GitHub origin): %+v", c)
	}
	joinWork(rows, nil)
	joinWork(rows, nil)
	if calls != 2 {
		t.Errorf("git remote asked %d times, want 2: once per checkout naming a PR by number (a and c), cached for the run", calls)
	}
	// The cell carries OSC 8 links around the ids, and the width is the
	// text's; a cut id stays plain.
	cell := linkify(pad("DLPX-98816 PR-940", 20), a)
	if !strings.Contains(cell, osc8("DLPX-98816", a.TicketURL)) || !strings.Contains(cell, osc8("PR-940", a.PRURL)) {
		t.Errorf("linkified cell: %q", cell)
	}
	if got := linkify("DLPX-98~", a); got != "DLPX-98~" {
		t.Errorf("a cut id should stay plain: %q", got)
	}
	m := newModel(opts{autoname: -1}, "savannah")
	m.width, m.height = 160, 30
	m.hooks, m.sessions = rows, rows
	m.rebuildRows()
	if out := m.View(); !strings.Contains(out, "\x1b]8;;https://perforce.atlassian.net/browse/DLPX-98816\x1b\\") {
		t.Errorf("the board should carry the ticket link")
	}
}

// TestCtrlBOpensLinks: Ctrl-B runs the opener for each link of the row,
// says so, and explains a row with nothing to open.
func TestCtrlBOpensLinks(t *testing.T) {
	ng := checkoutLayout(t)
	oldPat, oldOpen := ticketURLPattern, openerArgv
	ticketURLPattern = "https://perforce.atlassian.net/browse/{ticket}"
	openerArgv = func() []string { return []string{"true"} }
	t.Cleanup(func() { ticketURLPattern, openerArgv = oldPat, oldOpen })
	rows := joinWork([]session{
		{ID: "a", Project: ng + "/zfs-DLPX-98816", State: "idle", Updated: nowUnix()},
		{ID: "n", Project: ng + "/notes", State: "idle", Updated: nowUnix()},
	}, nil)
	m := newModel(opts{autoname: -1}, "savannah")
	m.width, m.height = 100, 30
	m.hooks, m.sessions = rows, rows
	m.rebuildRows()
	at := func(id string) {
		for i, r := range m.rows {
			if r.kind == rowSession && r.s.ID == id {
				m.cursor = i
			}
		}
	}
	at("n")
	m, cmd := press(m, tea.KeyCtrlB)
	if cmd != nil || !strings.Contains(m.status, "no ticket on this row") {
		t.Errorf("Ctrl-B on a row without a ticket: cmd=%v status=%q", cmd != nil, m.status)
	}
	at("a")
	m, cmd = press(m, tea.KeyCtrlB)
	if cmd == nil || !strings.Contains(m.status, "opening DLPX-98816") {
		t.Fatalf("Ctrl-B on a ticket row: cmd=%v status=%q", cmd != nil, m.status)
	}
	msg, ok := cmd().(openMsg)
	if !ok || msg.err != "" || msg.what != "DLPX-98816" {
		t.Fatalf("open result: %+v", msg)
	}
	next, _ := m.Update(msg)
	if m = next.(model); !strings.Contains(m.status, "opened DLPX-98816 in the browser") {
		t.Errorf("status after opening: %q", m.status)
	}
	ticketURLPattern = ""
	rows = joinWork(rows, nil)
	m.sessions = rows
	m.rebuildRows()
	at("a")
	m, cmd = press(m, tea.KeyCtrlB)
	if cmd != nil || !strings.Contains(m.status, "--ticket-url") {
		t.Errorf("Ctrl-B with no tracker set should say how to set one: %q", m.status)
	}
	// The board file's value: nothing is the author's Jira, off is none,
	// a pattern is itself.
	if got := resolveTicketURL(""); got != defaultTicketURL || !strings.Contains(got, "perforce.atlassian.net") {
		t.Errorf("no pref: %q", got)
	}
	if got := resolveTicketURL("off"); got != "" {
		t.Errorf("off: %q", got)
	}
	if got := resolveTicketURL("https://x/{ticket}"); got != "https://x/{ticket}" {
		t.Errorf("a pattern: %q", got)
	}
	for _, v := range []string{"off", "default"} {
		if o, err := parseArgs([]string{"--ticket-url", v}); err != nil || o.ticketURL != v {
			t.Errorf("--ticket-url %s: %+v %v", v, o, err)
		}
	}
	// --ticket-url is checked on the command line.
	if _, err := parseArgs([]string{"--ticket-url", "perforce.atlassian.net"}); err == nil {
		t.Error("--ticket-url without a scheme should be refused")
	}
	if o, err := parseArgs([]string{"--ticket-url", "https://perforce.atlassian.net/browse/{ticket}"}); err != nil || o.ticketURL == "" {
		t.Errorf("--ticket-url: %+v %v", o, err)
	}
}

// TestTicketColumn: the column appears only when a row has a ticket, in
// both plans, sized to the longest; the panel head carries it.
func TestTicketColumn(t *testing.T) {
	ng := checkoutLayout(t)
	// Plain cells here: plainLines drops a line with an escape in it, and
	// the links are TestTicketLinks's business.
	oldPat, oldRemote := ticketURLPattern, gitRemote
	ticketURLPattern, gitRemote = "", func(string) string { return "" }
	repoWebCache.m = map[string]string{}
	t.Cleanup(func() { ticketURLPattern, gitRemote = oldPat, oldRemote; repoWebCache.m = map[string]string{} })
	plain := []session{{Project: ng + "/zfs"}, {Project: ng + "/notes"}}
	if c := sessionColumns(100, plain, "", nil, false); c.ticket != 0 || c.what != 40 {
		t.Errorf("no ticket anywhere: %+v", c)
	}
	rows := joinWork([]session{
		{ID: "a", Project: ng + "/zfs-DLPX-98816", State: "idle", Updated: nowUnix()},
		{ID: "b", Project: ng + "/notes", State: "idle", Updated: nowUnix(), LastPrompt: "PR #940 and DP-2817"},
		{ID: "c", Project: ng + "/zfs", State: "idle", Updated: nowUnix()},
	}, nil)
	got := map[string]string{}
	for _, s := range rows {
		got[s.ID] = s.Ticket
	}
	if got["a"] != "DLPX-98816" || got["b"] != "DP-2817 PR-940" || got["c"] != "" {
		t.Fatalf("joined tickets: %v", got)
	}
	c := sessionColumns(100, rows, "", nil, false)
	if c.ticket != len("DP-2817 PR-940") || c.what != 40-(c.ticket+2) {
		t.Errorf("compact plan with tickets: %+v", c)
	}
	w := sessionColumns(160, rows, "", nil, false)
	sum := (2 + 2) + (9 + 2) + (w.work + 2) + (w.ticket + 2) + (w.project + 2) + (w.tab + 2) + (w.age + 2) + (w.note + 2) + w.what
	if w.ticket != c.ticket || sum > 160 {
		t.Errorf("wide plan with tickets sums to %d: %+v", sum, w)
	}
	m := newModel(opts{autoname: -1}, "savannah")
	m.width, m.height = 100, 30
	m.hooks, m.sessions = rows, rows
	m.rebuildRows()
	out := strings.Join(plainLines(m.View()), "\n")
	if !strings.Contains(out, "TICKET") || !strings.Contains(out, "DLPX-98816") || !strings.Contains(out, "DP-2817 PR-940") {
		t.Errorf("the board should show the TICKET column:\n%s", out)
	}
	m.width = 160
	for i, r := range m.rows {
		if r.kind == rowSession && r.s.ID == "a" {
			m.cursor = i
		}
	}
	out = strings.Join(plainLines(m.View()), "\n")
	if !strings.Contains(out, "   DLPX-98816") {
		t.Errorf("the panel head should carry the ticket:\n%s", out)
	}
}
