package main

import (
	"reflect"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
)

func TestRecoverArgs(t *testing.T) {
	if got := recoverArgs(true); !reflect.DeepEqual(got, []string{"work", "recover", "--dry-run", "--json"}) {
		t.Errorf("recoverArgs dry = %v", got)
	}
	if got := recoverArgs(false); !reflect.DeepEqual(got, []string{"work", "recover", "--json"}) {
		t.Errorf("recoverArgs = %v", got)
	}
}

func recoverPlan(dry bool) recoverResult {
	return recoverResult{DryRun: dry,
		Recovered: []recoveredItem{{Name: "alpha", How: "resume", Where: "pane"},
			{Project: "/home/u/proj", How: "resume", Where: "tab"}},
		Skipped: []recoverSkipped{{Name: "live", Why: "running"}, {Name: "beta", Why: "its pane is busy (vim runs there)"}},
		Failed:  []recoverFailed{{Name: "gamma", Error: "it2 run failed"}}}
}

func TestRecoverLines(t *testing.T) {
	got := strings.Join(recoverLines(recoverPlan(true), "/home/u"), "\n")
	for _, want := range []string{"would recover alpha in its pane (resume)", "would recover ~/proj in a new tab (resume)",
		"skipped beta: its pane is busy (vim runs there)", "failed gamma: it2 run failed"} {
		if !strings.Contains(got, want) {
			t.Errorf("plan lacks %q:\n%s", want, got)
		}
	}
	if strings.Contains(got, "live") {
		t.Errorf("a running session has no line of its own:\n%s", got)
	}
	if got := recoverLines(recoverPlan(false), "/home/u"); !strings.HasPrefix(got[0], "recovered alpha in its pane") {
		t.Errorf("done lines = %q", got)
	}
	none := recoverResult{Skipped: []recoverSkipped{{Name: "a", Why: "running"}, {Name: "b", Why: "running"}}}
	if got := recoverLines(none, ""); len(got) != 1 || got[0] != "nothing to recover: 2 sessions running" {
		t.Errorf("nothing to recover = %q", got)
	}
}

func TestRecoverOnSnapshotScreen(t *testing.T) {
	m := boardWith(t, true)
	m.width = 160
	m, _ = press(m, tea.KeyCtrlS)
	if out := m.View(); !strings.Contains(out, "recover: start again every Claude that went down") ||
		!strings.Contains(out, "c check   v recover") {
		t.Errorf("the screen names the recover keys:\n%s", out)
	}
	// c is the plan: lines on the screen, no re-read of the sessions.
	var cmd tea.Cmd
	m = typeText(m, "c")
	if m.snap.busy != "checking..." {
		t.Errorf("c: busy=%q", m.snap.busy)
	}
	m, cmd = send(m, recoverDoneMsg{dry: true, res: recoverPlan(true)})
	if m.snap.busy != "" || cmd != nil || !strings.Contains(m.View(), "would recover alpha in its pane (resume)") {
		t.Errorf("plan: busy=%q cmd=%v\n%s", m.snap.busy, cmd != nil, m.View())
	}
	// v does it, and the board reads its sessions again.
	m = typeText(m, "v")
	if !strings.HasPrefix(m.snap.busy, "recovering") || len(m.snap.lines) != 0 {
		t.Errorf("v: busy=%q lines=%q", m.snap.busy, m.snap.lines)
	}
	m, cmd = send(m, recoverDoneMsg{res: recoverPlan(false)})
	if m.snap.busy != "" || cmd == nil || !strings.Contains(m.View(), "recovered alpha in its pane (resume)") {
		t.Errorf("done: busy=%q cmd=%v\n%s", m.snap.busy, cmd != nil, m.View())
	}
	m, _ = send(m, recoverDoneMsg{err: "it2 not found"})
	if len(m.snap.lines) != 1 || !strings.Contains(m.snap.lines[0], "recover: it2 not found") {
		t.Errorf("error: lines=%q", m.snap.lines)
	}
}

func TestRecoverHint(t *testing.T) {
	m := boardWith(t, true)
	m, cmd := send(m, recoverDoneMsg{dry: true, hint: true, res: recoverPlan(true)})
	if cmd != nil || !strings.Contains(m.status, "2 sessions lost their Claude") ||
		!strings.Contains(m.status, "Ctrl-S then v starts them again") {
		t.Errorf("hint: %q", m.status)
	}
	one := recoverResult{DryRun: true, Recovered: []recoveredItem{{Name: "alpha"}}}
	m, _ = send(m, recoverDoneMsg{dry: true, hint: true, res: one})
	if !strings.Contains(m.status, "1 session lost its Claude") || !strings.Contains(m.status, "starts it again") {
		t.Errorf("hint for one: %q", m.status)
	}
	// Nothing to recover, an error, or another screen in front: no line, and
	// the snapshot screen's own lines are never touched by the start plan.
	for name, msg := range map[string]recoverDoneMsg{
		"nothing": {dry: true, hint: true, res: recoverResult{DryRun: true}},
		"error":   {dry: true, hint: true, err: "boom"},
	} {
		q := boardWith(t, true)
		if q, _ = send(q, msg); q.status != "" || q.snap.lines != nil {
			t.Errorf("%s: status=%q lines=%q", name, q.status, q.snap.lines)
		}
	}
	q := boardWith(t, true)
	q, _ = press(q, tea.KeyCtrlS)
	if q, _ = send(q, recoverDoneMsg{dry: true, hint: true, res: recoverPlan(true)}); q.status != "" || q.snap.lines != nil {
		t.Errorf("hint behind another screen: status=%q lines=%q", q.status, q.snap.lines)
	}
}
