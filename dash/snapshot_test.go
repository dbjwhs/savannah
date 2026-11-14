package main

import (
	"reflect"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

func TestSnapshotArgs(t *testing.T) {
	if got := snapshotsArgs(); !reflect.DeepEqual(got, []string{"work", "snapshots", "--json"}) {
		t.Errorf("snapshotsArgs = %v", got)
	}
	if got := snapshotArgs(); !reflect.DeepEqual(got, []string{"work", "snapshot", "--json"}) {
		t.Errorf("snapshotArgs = %v", got)
	}
	if got := restoreArgs(true); !reflect.DeepEqual(got, []string{"work", "restore", "--dry-run", "--json"}) {
		t.Errorf("restoreArgs dry = %v", got)
	}
	if got := restoreArgs(false); !reflect.DeepEqual(got, []string{"work", "restore", "--json"}) {
		t.Errorf("restoreArgs = %v", got)
	}
}

func TestSnapshotScreen(t *testing.T) {
	m := boardWith(t, true) // live (live), parked (offline), s2 (plain)
	m.width = 160           // the screen cuts every line to the width; the checks want whole lines
	if named, live, plain := m.openNow(); named != 2 || live != 1 || plain != 1 {
		t.Fatalf("openNow = %d %d %d", named, live, plain)
	}
	m, cmd := press(m, tea.KeyCtrlS)
	if m.mode != modeSnapshot || cmd == nil {
		t.Fatalf("Ctrl-S: mode=%d cmd=%v", m.mode, cmd != nil)
	}
	if out := m.View(); !strings.Contains(out, "Open now: 2 work sessions (1 live, 1 offline), 1 plain tab") ||
		!strings.Contains(out, "Snapshots: fetching...") {
		t.Errorf("fresh screen:\n%s", out)
	}
	// r before any snapshot exists is refused on the spot.
	m, _ = send(m, snapshotsMsg{rows: nil})
	if out := m.View(); !strings.Contains(out, "No snapshot yet") {
		t.Errorf("no snapshots:\n%s", out)
	}
	m = typeText(m, "r")
	if m.snap.busy != "" || len(m.snap.lines) != 1 || !strings.Contains(m.snap.lines[0], "no snapshot yet") {
		t.Errorf("r with nothing: busy=%q lines=%q", m.snap.busy, m.snap.lines)
	}
	// s saves; the result refreshes the list.
	m = typeText(m, "s")
	if m.snap.busy != "saving..." {
		t.Errorf("s: busy=%q", m.snap.busy)
	}
	m = typeText(m, "s") // ignored while busy
	m, cmd = send(m, snapshotDoneMsg{path: "/w/snapshots/20261106-083000.json", work: 2, live: 1, plain: 1})
	if cmd == nil || m.snap.busy != "" || !strings.Contains(m.snap.lines[0], "saved /w/snapshots/20261106-083000.json: 2 work sessions (1 live), 1 plain tabs") {
		t.Errorf("after save: cmd=%v busy=%q lines=%q", cmd != nil, m.snap.busy, m.snap.lines)
	}
	rows := []snapshotRow{
		{Stamp: "20261106-083000", Taken: time.Now().Unix(), TakenLocal: "2026-11-06 08:30", Work: 2, Live: 1, Plain: 1, Note: "before reboot"},
		{Stamp: "20261105-220000", Taken: time.Now().Unix() - 40000, TakenLocal: "2026-11-05 22:00", Work: 1},
	}
	m, _ = send(m, snapshotsMsg{rows: rows})
	if out := m.View(); !strings.Contains(out, "Latest snapshot: 20261106-083000 (2026-11-06 08:30, note before reboot): 2 work sessions (1 live), 1 plain tab") ||
		!strings.Contains(out, "Older: 20261105-220000") {
		t.Errorf("with snapshots:\n%s", out)
	}
	// d plans, r restores; both land their lines and only r re-reads sessions.
	m = typeText(m, "d")
	if m.snap.busy != "planning..." {
		t.Errorf("d: busy=%q", m.snap.busy)
	}
	res := restoreResult{Stamp: "20261106-083000", DryRun: true}
	res.Restored = append(res.Restored, struct {
		Name  string `json:"name"`
		How   string `json:"how"`
		TabID string `json:"tab_id"`
	}{Name: "parked", How: "resume"})
	res.Skipped = append(res.Skipped, struct {
		Name    string `json:"name"`
		Project string `json:"project"`
		Why     string `json:"why"`
	}{Name: "live", Why: "open"})
	m, cmd = send(m, restoreDoneMsg{dry: true, res: res})
	if cmd != nil || m.snap.busy != "" {
		t.Errorf("dry run result: cmd=%v busy=%q", cmd != nil, m.snap.busy)
	}
	if out := m.View(); !strings.Contains(out, "would restore parked (resume)") || !strings.Contains(out, "skipped live: open") {
		t.Errorf("dry run lines:\n%s", out)
	}
	m = typeText(m, "r")
	if !strings.HasPrefix(m.snap.busy, "restoring") {
		t.Errorf("r: busy=%q", m.snap.busy)
	}
	res.DryRun = false
	res.Restored[0].TabID = "14"
	res.Failed = append(res.Failed, struct {
		Name    string `json:"name"`
		Project string `json:"project"`
		Error   string `json:"error"`
	}{Project: "/Users/me/x", Error: "it2: no window"})
	m, cmd = send(m, restoreDoneMsg{res: res})
	if cmd == nil {
		t.Error("a real restore should re-read the sessions")
	}
	if out := m.View(); !strings.Contains(out, "restored parked (resume, tab 14)") || !strings.Contains(out, "failed /Users/me/x: it2: no window") {
		t.Errorf("restore lines:\n%s", out)
	}
	// A failure of the command itself.
	m, _ = send(m, restoreDoneMsg{err: "restore: no snapshot latest"})
	if !strings.Contains(m.snap.lines[0], "restore: restore: no snapshot latest") {
		t.Errorf("error line: %q", m.snap.lines)
	}
	if got := restoreLines(restoreResult{}, ""); len(got) != 1 || !strings.Contains(got[0], "nothing to do") {
		t.Errorf("empty result: %q", got)
	}
	m, _ = press(m, tea.KeyEsc)
	if m.mode != modeBoard {
		t.Errorf("Esc: mode=%d", m.mode)
	}
}

func TestSnapshotHintOnce(t *testing.T) {
	m := boardWith(t, true) // parked is offline
	m.sessTicks = 1
	fresh := []snapshotRow{{Stamp: "20261106-083000", Taken: time.Now().Unix() - 3600, Work: 2}}
	m, _ = send(m, snapshotsMsg{rows: fresh})
	if !strings.Contains(m.status, "snapshot 20261106-083000 holds 2 sessions; Ctrl-S restores") || !m.snap.hinted {
		t.Errorf("hint: status=%q hinted=%v", m.status, m.snap.hinted)
	}
	// Decided once: a later list never hints again.
	m.status = ""
	m, _ = send(m, snapshotsMsg{rows: fresh})
	if m.status != "" {
		t.Errorf("second list should not hint: %q", m.status)
	}
	// No hint when nothing is offline, or the snapshot is old, or the
	// sessions have not been read yet.
	for _, c := range []struct {
		name  string
		setup func(m *model)
	}{
		{"all live", func(m *model) {
			// s2 becomes parked's live tab: same worktree, same pane.
			m.hooks[1].Project = "/r/.savannah-worktrees/parked"
			for i := range m.work {
				if m.work[i].Name == "parked" {
					m.work[i].ClaudePane = "UUID-P"
				}
			}
			m.sessions = joinWork(m.hooks, m.work)
			m.rebuildRows()
		}},
		{"old snapshot", func(m *model) { m.snap.rows = nil }},
		{"no sessions yet", func(m *model) { m.sessTicks = 0 }},
	} {
		m := boardWith(t, true)
		m.sessTicks = 1
		rows := fresh
		if c.name == "old snapshot" {
			rows = []snapshotRow{{Stamp: "x", Taken: time.Now().Unix() - 3*24*3600, Work: 2}}
		}
		c.setup(&m)
		m, _ = send(m, snapshotsMsg{rows: rows})
		if m.status != "" {
			t.Errorf("%s: unexpected hint %q", c.name, m.status)
		}
	}
	// The hint waits for the first sessions read and then fires from it.
	m = boardWith(t, true)
	m, _ = send(m, snapshotsMsg{rows: fresh})
	if m.status != "" || m.snap.hinted {
		t.Fatalf("before sessions: status=%q hinted=%v", m.status, m.snap.hinted)
	}
	m, _ = send(m, sessionsMsg{hooks: m.hooks, work: m.work})
	if !strings.Contains(m.status, "Ctrl-S restores") {
		t.Errorf("hint after the first sessions read: %q", m.status)
	}
}
