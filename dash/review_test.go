package main

import (
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// reviewBoard is sectionedBoard (live, parked | slow, #s3, #s2) plus two
// live reviews, rev-a before rev-b, one of them opened on a pull request.
func reviewBoard(t *testing.T) model {
	t.Helper()
	m := sectionedBoard(t)
	for ndx, name := range []string{"rev-a", "rev-b"} {
		pane := "R" + name
		m.hooks = append(m.hooks, session{ID: "h-" + name, Project: "/r/.savannah-worktrees/" + name,
			State: "idle", Updated: nowUnix() - 5, ITerm: "w0t0p0:" + pane})
		m.work = append(m.work, workRecord{Name: name, Worktree: "/r/.savannah-worktrees/" + name,
			ClaudePane: pane, Kind: kindReview, Rank: int64(ndx+1) * 10})
	}
	m.work[len(m.work)-2].Target = "2353"
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	return m
}

// TestReviewsSection: reviews sit in their own section between ACTIVE and
// LATER, whatever their priority, and the header and the count say so; a
// board with no review does not draw the section.
func TestReviewsSection(t *testing.T) {
	plain := sectionedBoard(t)
	if out := plain.View(); strings.Contains(out, "REVIEWS") || strings.Contains(out, "review") && !strings.Contains(out, "^Y review") {
		t.Errorf("a board with no review must not draw REVIEWS:\n%s", out)
	}
	m := reviewBoard(t)
	want := []string{"live", "parked", "rev-a", "rev-b", "slow", "#s3", "#s2"}
	if got := rowNames(m); !reflect.DeepEqual(got, want) {
		t.Fatalf("rows = %v, want %v", got, want)
	}
	out := m.View()
	for _, s := range []string{"ACTIVE (2)", "REVIEWS (2)", "LATER (3)", "2 reviews"} {
		if !strings.Contains(out, s) {
			t.Errorf("view lacks %q:\n%s", s, out)
		}
	}
	if a, r, l := lineOf(m, "ACTIVE (2)"), lineOf(m, "REVIEWS (2)"), lineOf(m, "LATER (3)"); !(a < r && r < l) {
		t.Errorf("REVIEWS should be drawn between ACTIVE (%d) and LATER (%d), is at %d", a, l, r)
	}
	// A review with a "now" or "later" priority is still a review.
	setRank(&m, "rev-b", prioNow, 5)
	if got := m.sectionNames(); !reflect.DeepEqual(got[secReviews], []string{"rev-b", "rev-a"}) || len(got[secNow]) != 0 {
		t.Errorf("a review's priority must not move it out of REVIEWS: %v", got)
	}
	// The pull request a review was opened on is its PR.
	rec, _ := m.recordOf("rev-a")
	if id, _ := reviewPR(rec); id != "PR-2353" {
		t.Errorf("reviewPR(%q) = %q, want PR-2353", rec.Target, id)
	}
	rec.Target = "https://github.com/o/r/pull/77/files"
	if id, url := reviewPR(rec); id != "PR-77" || url != "https://github.com/o/r/pull/77" {
		t.Errorf("reviewPR of a URL = %q %q", id, url)
	}
	rec.Target = "feature/x"
	if id, _ := reviewPR(rec); id != "" {
		t.Errorf("a branch target is no PR: %q", id)
	}
}

// TestReviewsDrag: a review moves inside REVIEWS and stops at its edges; a
// drag from ACTIVE to LATER steps over the section; Ctrl-P is refused.
func TestReviewsDrag(t *testing.T) {
	m := reviewBoard(t)
	cursorTo(t, &m, workRow("rev-a"))
	m, cmd := press(m, tea.KeyShiftDown)
	if got := m.sectionNames()[secReviews]; cmd == nil || !reflect.DeepEqual(got, []string{"rev-b", "rev-a"}) {
		t.Fatalf("Shift-Down inside REVIEWS: cmd=%v order=%v", cmd != nil, got)
	}
	rec, _ := m.recordOf("rev-a")
	if rec.Rank != 20 || priorityOf(rec) != prioActive || !isReview(rec) {
		t.Errorf("rev-a should be rank 20 with its priority kept: %+v", rec)
	}
	m, cmd = press(m, tea.KeyShiftDown)
	if cmd != nil || !strings.Contains(m.status, "bottom of REVIEWS") {
		t.Errorf("a drag past the bottom of REVIEWS: cmd=%v status=%q", cmd != nil, m.status)
	}
	cursorTo(t, &m, workRow("rev-b"))
	m, cmd = press(m, tea.KeyShiftUp)
	if cmd != nil || !strings.Contains(m.status, "top of REVIEWS") {
		t.Errorf("a drag past the top of REVIEWS: cmd=%v status=%q", cmd != nil, m.status)
	}
	if _, cmd := press(m, tea.KeyCtrlP); cmd != nil {
		t.Error("Ctrl-P on a review must not move it")
	}
	// parked is the bottom of ACTIVE: down lands at the top of LATER.
	cursorTo(t, &m, workRow("parked"))
	m, _ = press(m, tea.KeyShiftDown)
	rec, _ = m.recordOf("parked")
	if rec.Priority != prioLater || rec.Rank != 10 {
		t.Errorf("parked should step over REVIEWS into LATER: %+v", rec)
	}
	if got := m.sectionNames()[secReviews]; !reflect.DeepEqual(got, []string{"rev-b", "rev-a"}) {
		t.Errorf("REVIEWS must be untouched by that drag: %v", got)
	}
}

// TestCtrlYMode: Ctrl-Y asks the CLI to flip the kind of a work session,
// and refuses a tab with no record.
func TestCtrlYMode(t *testing.T) {
	if got, want := workVerbArgs(verbMode, "live", kindReview), []string{"work", "mode", "live", "review"}; !reflect.DeepEqual(got, want) {
		t.Errorf("mode argv = %q, want %q", got, want)
	}
	m := reviewBoard(t)
	cursorTo(t, &m, workRow("live"))
	m2, cmd := press(m, tea.KeyCtrlY)
	if cmd == nil || !strings.Contains(m2.status, "live a review") {
		t.Errorf("coding row: cmd=%v status=%q", cmd != nil, m2.status)
	}
	cursorTo(t, &m, workRow("rev-a"))
	if m2, cmd := press(m, tea.KeyCtrlY); cmd == nil || !strings.Contains(m2.status, "rev-a a coding") {
		t.Errorf("review row: cmd=%v status=%q", cmd != nil, m2.status)
	}
	cursorTo(t, &m, hookRow("s2"))
	if m2, cmd := press(m, tea.KeyCtrlY); cmd != nil || !strings.Contains(m2.status, "Ctrl-R") {
		t.Errorf("plain tab: cmd=%v status=%q", cmd != nil, m2.status)
	}
	m, cmd = send(m, workDoneMsg{verb: verbMode, name: "live", text: kindReview})
	if cmd == nil || !strings.Contains(m.status, "live is a review") {
		t.Errorf("mode set: reread=%v status=%q", cmd != nil, m.status)
	}
	m, _ = send(m, workDoneMsg{verb: verbMode, name: "rev-a", text: kindCoding})
	if !strings.Contains(m.status, "rev-a is a coding session again") {
		t.Errorf("mode unset: status=%q", m.status)
	}
}

// TestReviewNewArgs: in review mode the dialog's prompt text is the target.
func TestReviewNewArgs(t *testing.T) {
	repos := reposInfo{Dir: "/hub", Repos: []string{"song"}, Scratch: "/hub/.savannah-scratch"}
	got := workNewArgs("pr-2353", "", "song", "2353", true, false, true, repos, "/home/me")
	want := []string{"work", "new", "pr-2353", "--repo=/hub/song", "--mode=review", "--target=2353", "--json"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("review with a target = %q, want %q", got, want)
	}
	got = workNewArgs("look", "", "song", "", false, true, true, repos, "/home/me")
	want = []string{"work", "new", "look", "--repo=/hub/song", "--mode=review", "--no-worktree", "--detach", "--json"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("review with no target = %q, want %q", got, want)
	}
}
