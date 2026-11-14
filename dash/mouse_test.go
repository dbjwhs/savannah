package main

import (
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// The hits line up with the view: every row line names its row, the
// section headers their section, the rest nothing.
func TestHitsMatchTheView(t *testing.T) {
	m := boardWith(t, true)
	lines := strings.Split(m.View(), "\n")
	for i, r := range m.rows {
		y := -1
		for j, ln := range lines {
			if (r.s.Work != "" && strings.Contains(ln, " "+r.s.Work+" ")) || (r.s.Work == "" && strings.Contains(ln, " "+projectLabel(r.s.Project)+" ")) {
				y = j
				break
			}
		}
		if y < 0 {
			t.Fatalf("row %d (%s) not drawn:\n%s", i, r.s.ID, m.View())
		}
		if hit := m.hitAt(y); hit.row != i {
			t.Errorf("line %d holds row %d, hit says %+v", y, i, hit)
		}
	}
	if hit := m.hitAt(lineOf(m, "HISTORY (")); hit.section != secHistory || hit.row != -1 {
		t.Errorf("HISTORY header hit = %+v", hit)
	}
	if hit := m.hitAt(lineOf(m, "NOW (")); hit.section != secNow {
		t.Errorf("NOW header hit = %+v", hit)
	}
	for _, y := range []int{0, 1, lineOf(m, "LOCAL SESSIONS"), lineOf(m, "STATE"), -1, 999} {
		if hit := m.hitAt(y); hit != noHit {
			t.Errorf("line %d should hit nothing, got %+v", y, hit)
		}
	}
}

// A click selects, a second click on the same row within the window
// opens it, the wheel moves the highlight, a click on HISTORY toggles it.
func TestClicksAndWheel(t *testing.T) {
	m := boardWith(t, true)
	m.cursor = 0
	y := lineOf(m, " parked ")
	m, cmd := click(m, y)
	if cmd != nil && m.cursor != indexOfRow(m, workRow("parked")) {
		t.Fatalf("click should select parked: cursor=%d", m.cursor)
	}
	if m.cursor != indexOfRow(m, workRow("parked")) {
		t.Fatalf("click should select parked: cursor=%d", m.cursor)
	}
	m.click.at = time.Now().Add(-time.Second) // a slow second click: still a select
	m, cmd = click(m, y)
	if cmd != nil || m.cursor != indexOfRow(m, workRow("parked")) {
		t.Errorf("a slow second click only selects: cmd=%v cursor=%d", cmd != nil, m.cursor)
	}
	m, cmd = click(m, y) // and this one, within the window, is the double click
	if cmd == nil || !strings.Contains(m.status, "opening parked") {
		t.Errorf("double click should open parked: cmd=%v status=%q", cmd != nil, m.status)
	}
	if m.click.row != -1 {
		t.Errorf("the double click should reset the click state: %+v", m.click)
	}
	// Double click on a plain tab jumps.
	m.click = clickState{row: -1}
	y = lineOf(m, " other ")
	m, _ = click(m, y)
	m, cmd = click(m, y)
	if cmd == nil || m.cursor != indexOfRow(m, hookRow("s2")) {
		t.Errorf("double click on a plain tab should jump: cmd=%v cursor=%d", cmd != nil, m.cursor)
	}
	// A click off the rows selects nothing and keeps the cursor.
	before := m.cursor
	m, cmd = click(m, 0)
	if cmd != nil || m.cursor != before {
		t.Errorf("a click on the header: cmd=%v cursor=%d (was %d)", cmd != nil, m.cursor, before)
	}
	// The wheel.
	m.cursor = 0
	m = wheel(m, tea.MouseButtonWheelDown)
	m = wheel(m, tea.MouseButtonWheelDown)
	if m.cursor != 2 {
		t.Errorf("two wheel downs: cursor=%d", m.cursor)
	}
	m = wheel(m, tea.MouseButtonWheelUp)
	if m.cursor != 1 {
		t.Errorf("wheel up: cursor=%d", m.cursor)
	}
	for i := 0; i < 10; i++ {
		m = wheel(m, tea.MouseButtonWheelDown)
	}
	if m.cursor != len(m.rows)-1 {
		t.Errorf("the wheel stops at the last row: cursor=%d of %d", m.cursor, len(m.rows))
	}
	// HISTORY's header toggles the section.
	m, _ = click(m, lineOf(m, "HISTORY ("))
	if m.history || !strings.Contains(m.status, "HISTORY closed") {
		t.Errorf("click on HISTORY should close it: history=%v status=%q", m.history, m.status)
	}
	m, _ = click(m, lineOf(m, "HISTORY ("))
	if !m.history {
		t.Error("click on HISTORY should open it again")
	}
	// A pending Ctrl-X is withdrawn by a click elsewhere.
	m.pendingRm = "parked"
	m, _ = click(m, lineOf(m, " live "))
	if m.pendingRm != "" {
		t.Error("a click should withdraw a pending removal")
	}
	// Other screens ignore the mouse.
	m.mode = modeLog
	m.cursor = 0
	m = wheel(m, tea.MouseButtonWheelDown)
	if m.cursor != 0 {
		t.Error("the log view should ignore the wheel")
	}
}

func TestMouseFlags(t *testing.T) {
	o, err := parseArgs([]string{"--no-mouse"})
	if err != nil || o.mouse != -1 {
		t.Errorf("--no-mouse: %+v %v", o, err)
	}
	o, err = parseArgs([]string{"--mouse"})
	if err != nil || o.mouse != 1 {
		t.Errorf("--mouse: %+v %v", o, err)
	}
}

// A click on a MESH WORKERS row selects that worker: every line topLines
// draws there has its hit, so the header lines never shift the rows.
func TestClickMeshWorkerRows(t *testing.T) {
	m := boardWith(t, true)
	m.mesh.on = true
	m.mesh.nodes = []string{"n1"}
	m.mesh.tasks = map[string][]task{"n1": {
		{ID: "t-1", Title: "first", State: "idle"},
		{ID: "t-2", Title: "second", State: "idle"},
		{ID: "t-3", Title: "third", State: "idle"},
	}}
	m.rebuildRows()
	top, hits := m.topLines(m.width, "")
	if len(top) != len(hits) {
		t.Fatalf("topLines: %d lines, %d hits", len(top), len(hits))
	}
	for _, id := range []string{"t-1", "t-2", "t-3"} {
		y := lineOf(m, " "+id+" ")
		if y < 0 {
			y = lineOf(m, id)
		}
		h := m.hitAt(y)
		if h.row < 0 || h.row >= len(m.rows) || m.rows[h.row].kind != rowTask || m.rows[h.row].t.ID != id {
			t.Errorf("click on %s hits row %d", id, h.row)
		}
	}
	if h := m.hitAt(lineOf(m, "MESH WORKERS")); h.row >= 0 {
		t.Errorf("the MESH WORKERS header hits row %d", h.row)
	}
}
