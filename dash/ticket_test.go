package main

import (
	"reflect"
	"strings"
	"testing"
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
		if got := ticketFor(c.s, c.rec, c.has); got != c.want {
			t.Errorf("%s: ticketFor = %q, want %q", c.name, got, c.want)
		}
	}
}

// TestTicketColumn: the column appears only when a row has a ticket, in
// both plans, sized to the longest; the panel head carries it.
func TestTicketColumn(t *testing.T) {
	ng := checkoutLayout(t)
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
	if got["a"] != "DLPX-98816" || got["b"] != "PR-940 DP-2817" || got["c"] != "" {
		t.Fatalf("joined tickets: %v", got)
	}
	c := sessionColumns(100, rows, "", nil, false)
	if c.ticket != len("PR-940 DP-2817") || c.what != 40-(c.ticket+2) {
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
	if !strings.Contains(out, "TICKET") || !strings.Contains(out, "DLPX-98816") || !strings.Contains(out, "PR-940 DP-2817") {
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
