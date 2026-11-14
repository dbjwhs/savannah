package main

import (
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func TestSessionHaystack(t *testing.T) {
	rec := workRecord{Name: "dp-2702-perf", Branch: "work/dp-2702-perf", Summary: "Replication perf, DOSE run #1743.", lastNote: "slog takeover next", Title: "SDD replication perf"}
	s := session{Work: "dp-2702-perf", WorkTitle: "SDD replication perf", Ticket: "DP-2702 PR-940", Project: "/Users/x/ng/delphix/zfs", LastPrompt: "rerun the mirror sync", State: "idle"}
	hay := sessionHaystack(s, rec, true, nil, "/Users/x")
	for _, want := range []string{"dp-2702-perf", "sdd replication", "dp-2702 pr-940", "~/ng/delphix/zfs", "work/dp-2702-perf", "dose run #1743", "slog takeover", "rerun the mirror", "idle"} {
		if !strings.Contains(hay, want) {
			t.Errorf("haystack lacks %q: %q", want, hay)
		}
	}
	plain := session{ID: "p", Project: "/other", ITerm: "w0t0p0:UUID-P", State: "idle"}
	if hay := sessionHaystack(plain, workRecord{}, false, map[string]string{"UUID-P": "Local testing"}, ""); !strings.Contains(hay, "local testing") {
		t.Errorf("a plain tab should be found by its tab title: %q", hay)
	}
	m := model{home: "/Users/x"}
	recs := map[string]workRecord{"dp-2702-perf": rec}
	r := row{kind: rowSession, s: s}
	for _, c := range []struct {
		filter string
		want   bool
	}{
		{"", true}, {"2702", true}, {"DP-2702", true}, {"zfs perf", true}, {"zfs nothere", false}, {"PR-940", true}, {"mirror", true}, {"Mirror SYNC", true},
	} {
		if got := m.rowMatches(r, filterWords(c.filter), recs); got != c.want {
			t.Errorf("rowMatches(%q) = %v, want %v", c.filter, got, c.want)
		}
	}
	task := row{kind: rowTask, node: "devone", t: task{ID: "t-7", Title: "review the parser", Worktree: "/w/t-7"}}
	if !m.rowMatches(task, filterWords("devone parser"), nil) || m.rowMatches(task, filterWords("zfs"), nil) {
		t.Error("a mesh worker matches on node and title")
	}
}

// TestCtrlKFilters: Ctrl-K takes the prompt line, the rows narrow as you
// type (HISTORY searched too), Enter keeps the filter, Esc clears it, a
// drag is refused meanwhile, and the cursor follows the row.
func TestCtrlKFilters(t *testing.T) {
	m := sectionedBoard(t) // live, parked, slow, #s3, #s2
	m.history = false
	m.work = append(m.work, workRecord{Name: "gone", Worktree: "/r/.savannah-worktrees/gone", ClaudePane: "G", Summary: "Was on the zfs slog."})
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"live", "parked", "slow", "#s3", "#s2"}) {
		t.Fatalf("start = %v", got)
	}
	cursorTo(t, &m, workRow("slow"))
	m, cmd := press(m, tea.KeyCtrlK)
	if cmd != nil || !m.finding() || !strings.Contains(m.View(), "find:") {
		t.Fatalf("Ctrl-K should take the prompt line: finding=%v", m.finding())
	}
	m = typeText(m, "slo")
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"slow", "gone"}) {
		t.Fatalf("typing slo: rows = %v, want slow and the closed gone (its summary says slog)", got)
	}
	if m.filter != "slo" || m.cursor != 0 || !strings.Contains(m.View(), `matching "slo"`) || !strings.Contains(m.View(), "HISTORY (1)") {
		t.Errorf("filter=%q cursor=%d (want on slow)", m.filter, m.cursor)
	}
	m = typeText(m, "w")
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"slow"}) {
		t.Fatalf("typing slow: rows = %v", got)
	}
	// Enter keeps the filter; the prompt line is slow's again.
	m, cmd = press(m, tea.KeyEnter)
	if cmd != nil || m.finding() || m.filter != "slow" || m.input.Value() != "" || !strings.Contains(m.status, `1 row matches "slow"`) {
		t.Fatalf("Enter: finding=%v filter=%q input=%q status=%q", m.finding(), m.filter, m.input.Value(), m.status)
	}
	if out := m.View(); !strings.Contains(out, "say to slow") {
		t.Errorf("after Enter the prompt line should be the row's:\n%s", out)
	}
	next, cmd := m.dragRow(-1)
	m = next.(model)
	if cmd != nil || !strings.Contains(m.status, "filtered") {
		t.Errorf("drag while filtered should be refused: cmd=%v status=%q", cmd != nil, m.status)
	}
	// Ctrl-K again edits the same filter; Esc clears everything and the
	// cursor stays on slow.
	m, _ = press(m, tea.KeyCtrlK)
	if !m.finding() || m.input.Value() != "slow" {
		t.Errorf("Ctrl-K again should prefill the filter: finding=%v input=%q", m.finding(), m.input.Value())
	}
	m, cmd = press(m, tea.KeyEsc)
	if cmd != nil || m.finding() || m.filter != "" || !strings.Contains(m.status, "filter cleared") {
		t.Fatalf("Esc: finding=%v filter=%q status=%q", m.finding(), m.filter, m.status)
	}
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"live", "parked", "slow", "#s3", "#s2"}) || m.rows[m.cursor].s.Work != "slow" {
		t.Errorf("after Esc: rows=%v cursor on %q", got, m.rows[m.cursor].s.Work)
	}
	// Esc with no filter and nothing typed still quits.
	if _, cmd := press(m, tea.KeyEsc); cmd == nil {
		t.Error("Esc on a plain board should quit")
	}
	// A filter nothing matches leaves no rows and says so on Enter.
	m, _ = press(m, tea.KeyCtrlK)
	m = typeText(m, "nothing-here")
	m, _ = press(m, tea.KeyEnter)
	if len(m.rows) != 0 || !strings.Contains(m.status, "0 rows match") {
		t.Errorf("no match: rows=%d status=%q", len(m.rows), m.status)
	}
	m, _ = press(m, tea.KeyEsc)
	if len(m.rows) != 5 {
		t.Errorf("after Esc every row is back: %d", len(m.rows))
	}
}
