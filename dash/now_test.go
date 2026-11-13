package main

import (
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

// Ctrl-P is a toggle: into the bottom of NOW from ACTIVE or LATER, back
// onto the top of ACTIVE from NOW; the records are renumbered and written
// like a drag.
func TestCtrlPTogglesNow(t *testing.T) {
	m := sectionedBoard(t)
	out := m.View()
	if !strings.Contains(out, "NOW (0)") || !strings.Contains(out, "(empty: Ctrl-P puts the session you are pushing on here)") {
		t.Fatalf("empty NOW should head the board with its hint:\n%s", out)
	}
	if strings.Contains(out, " now   ") {
		t.Errorf("header should not count an empty NOW:\n%s", out)
	}
	lines := plainLines(out)
	iN, iA := -1, -1
	for i, ln := range lines {
		switch {
		case strings.HasPrefix(ln, "NOW ("):
			iN = i
		case strings.HasPrefix(ln, "ACTIVE ("):
			iA = i
		}
	}
	if !(iN >= 0 && iN < iA) {
		t.Errorf("NOW should sit above ACTIVE: %d %d", iN, iA)
	}

	// slow (LATER) into NOW: it leaves LATER, lands alone in NOW, rank 10.
	cursorTo(t, &m, workRow("slow"))
	m, cmd := press(m, tea.KeyCtrlP)
	if cmd == nil {
		t.Fatal("Ctrl-P should write the record")
	}
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"slow", "live", "parked", "#s3", "#s2"}) {
		t.Fatalf("after Ctrl-P on slow: %v", got)
	}
	rec, _ := m.recordOf("slow")
	if rec.Priority != "now" || rec.Rank != 10 || m.cursor != 0 {
		t.Fatalf("slow should be now/10 under the cursor: %+v cursor=%d", rec, m.cursor)
	}
	if !strings.Contains(m.status, "slow is in NOW") {
		t.Errorf("status = %q", m.status)
	}
	out = m.View()
	for _, want := range []string{"NOW (1)", "ACTIVE (2)", "LATER (2)", "1 now"} {
		if !strings.Contains(out, want) {
			t.Errorf("view lacks %q:\n%s", want, out)
		}
	}
	if strings.Contains(out, "(empty: Ctrl-P") {
		t.Errorf("the hint goes once NOW has a row:\n%s", out)
	}

	// parked (ACTIVE) joins at the bottom of NOW; the ACTIVE rank order is kept.
	cursorTo(t, &m, workRow("parked"))
	m, _ = press(m, tea.KeyCtrlP)
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"slow", "parked", "live", "#s3", "#s2"}) {
		t.Fatalf("after Ctrl-P on parked: %v", got)
	}
	rec, _ = m.recordOf("parked")
	if rec.Priority != "now" || rec.Rank != 20 {
		t.Fatalf("parked should be now/20: %+v", rec)
	}
	if m.countNow() != 2 || !strings.Contains(m.View(), "2 now") {
		t.Errorf("header should count 2 now")
	}

	// Ctrl-P on a NOW row: back onto the top of ACTIVE, above live.
	cursorTo(t, &m, workRow("slow"))
	m, cmd = press(m, tea.KeyCtrlP)
	if cmd == nil {
		t.Fatal("the way back writes too")
	}
	if got := rowNames(m); !reflect.DeepEqual(got, []string{"parked", "slow", "live", "#s3", "#s2"}) {
		t.Fatalf("after Ctrl-P back: %v", got)
	}
	rec, _ = m.recordOf("slow")
	live, _ := m.recordOf("live")
	if rec.Priority != "active" || rec.Rank != 10 || live.Priority != "active" || live.Rank != 20 {
		t.Fatalf("slow should lead ACTIVE: slow=%+v live=%+v", rec, live)
	}
	if !strings.Contains(m.status, "back at the top of ACTIVE") {
		t.Errorf("status = %q", m.status)
	}
	if args := moveArgs(moveChange{"x", "now", 10}); !reflect.DeepEqual(args, []string{"work", "move", "x", "--now", "--rank=10"}) {
		t.Errorf("moveArgs = %v", args)
	}
}

// Ctrl-P works grouped and filtered (it needs no neighbours on the
// screen), and refuses an unnamed tab and a closed session.
func TestCtrlPAnywhere(t *testing.T) {
	m := sectionedBoard(t)
	m.group = true
	m.rebuildRows()
	cursorTo(t, &m, workRow("live"))
	m, cmd := press(m, tea.KeyCtrlP)
	if cmd == nil || m.countNow() != 1 {
		t.Fatalf("Ctrl-P while grouped: cmd=%v now=%d status=%q", cmd != nil, m.countNow(), m.status)
	}
	m.group = false
	m = m.pressFind()
	m = typeText(m, "parked")
	cursorTo(t, &m, workRow("parked"))
	m, cmd = press(m, tea.KeyCtrlP)
	rec, _ := m.recordOf("parked")
	if cmd == nil || rec.Priority != "now" || rec.Rank != 20 || m.filter != "parked" {
		t.Fatalf("Ctrl-P while filtered: %+v filter=%q status=%q", rec, m.filter, m.status)
	}
	m = m.clearFind()

	// An unnamed tab is named from its tab title and pushed in one go.
	m.titles = map[string]string{"UUID-W": "Slog work"}
	cursorTo(t, &m, hookRow("s3"))
	m, cmd = press(m, tea.KeyCtrlP)
	rec, _ = m.recordOf("slog-work")
	if cmd == nil || rec.Priority != "now" || rec.Title != "Slog work" || !strings.Contains(m.status, "naming it slog-work") {
		t.Errorf("Ctrl-P on an unnamed tab should name and push it: %+v status=%q", rec, m.status)
	}
	if got := m.sectionNames()[secNow]; !reflect.DeepEqual(got, []string{"live", "parked", "slog-work"}) {
		t.Errorf("NOW after the push: %v", got)
	}

	h := groupedBoard(t) // b2 is closed
	cursorTo(t, &h, workRow("b2"))
	h, cmd = press(h, tea.KeyCtrlP)
	if cmd != nil || !strings.Contains(h.status, "HISTORY") {
		t.Errorf("a closed session is refused: cmd=%v status=%q", cmd != nil, h.status)
	}
}

// A drag from the top of ACTIVE walks into NOW, and down out of it again.
func TestDragCrossesIntoNow(t *testing.T) {
	m := sectionedBoard(t)
	cursorTo(t, &m, workRow("live")) // top of ACTIVE
	m, cmd := press(m, tea.KeyShiftUp)
	rec, _ := m.recordOf("live")
	if cmd == nil || rec.Priority != "now" || rec.Rank != 10 || m.cursor != 0 {
		t.Fatalf("Shift-Up from the top of ACTIVE should enter NOW: %+v cursor=%d status=%q", rec, m.cursor, m.status)
	}
	m, _ = press(m, tea.KeyShiftUp)
	if !strings.Contains(m.status, "already at the top") {
		t.Errorf("top of NOW: %q", m.status)
	}
	m, _ = press(m, tea.KeyShiftDown)
	rec, _ = m.recordOf("live")
	if rec.Priority != "active" || rec.Rank != 10 {
		t.Fatalf("Shift-Down from NOW should land at the top of ACTIVE: %+v", rec)
	}
}
