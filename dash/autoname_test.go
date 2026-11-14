package main

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// autonameBoard: one work session, two plain tabs with a prompt, one plain
// tab nothing was typed into, one offline record row.
func autonameBoard(t *testing.T) model {
	t.Helper()
	hooks := []session{
		{ID: "s1", Project: "/r/.savannah-worktrees/live", State: "working", Updated: nowUnix(), ITerm: "w0t0p0:L", LastPrompt: "fix it"},
		{ID: "p1", Project: "/zfs", State: "idle", Updated: nowUnix(), ITerm: "w0t0p0:P1", LastPrompt: "review PR 2353"},
		{ID: "p2", Project: "/net", State: "done", Updated: nowUnix(), ITerm: "w0t0p0:P2", LastPrompt: "why is wifi slow"},
		{ID: "p3", Project: "/fresh", State: "idle", Updated: nowUnix(), ITerm: "w0t0p0:P3"},
	}
	work := []workRecord{
		{Name: "live", Worktree: "/r/.savannah-worktrees/live", ClaudePane: "L"},
		{Name: "parked", Worktree: "/r/.savannah-worktrees/parked", ClaudePane: "P"},
	}
	return testBoard(t, opts{}, withHooks(hooks), withWork(work))
}

func TestPendingAutoname(t *testing.T) {
	m := autonameBoard(t)
	if got := m.pendingAutoname(); !reflect.DeepEqual(got, []string{"p1", "p2"}) {
		t.Fatalf("pending = %v, want the plain tabs with a prompt", got)
	}
	// Off, or a run in flight: nothing.
	off := m
	off.autoname = false
	if got := off.pendingAutoname(); got != nil {
		t.Errorf("pending while off = %v", got)
	}
	busy := m
	busy.naming = true
	if got := busy.pendingAutoname(); got != nil {
		t.Errorf("pending while naming = %v", got)
	}
	// startAutoname submits them once: the second call has nothing, and a
	// tab that gets its first prompt later is picked up.
	cmd := m.startAutoname()
	if cmd == nil || !m.naming || !m.nameTried["p1"] || !m.nameTried["p2"] {
		t.Fatalf("startAutoname: cmd=%v naming=%v tried=%v", cmd != nil, m.naming, m.nameTried)
	}
	if !strings.Contains(m.status, "naming 2 tabs") {
		t.Errorf("status = %q", m.status)
	}
	m.naming = false
	if cmd := m.startAutoname(); cmd != nil {
		t.Errorf("a second startAutoname should have nothing to do")
	}
	m.hooks[3].LastPrompt = "now something"
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	if got := m.pendingAutoname(); !reflect.DeepEqual(got, []string{"p3"}) {
		t.Errorf("pending after p3 got a prompt = %v, want [p3]", got)
	}
	if got := autonameArgs([]string{"a", "b"}); !reflect.DeepEqual(got, []string{"work", "autoname", "--json", "a", "b"}) {
		t.Errorf("autonameArgs = %v", got)
	}
}

func TestSessionsTickStartsAutoname(t *testing.T) {
	m := autonameBoard(t)
	m, cmd := send(m, sessionsMsg{hooks: m.hooks, work: m.work})
	if cmd == nil || !m.naming || len(m.nameTried) != 2 {
		t.Fatalf("the sessions tick should start naming: naming=%v tried=%v", m.naming, m.nameTried)
	}
	// The result: names on the status line, the run released, a re-read asked for.
	res := autonameMsg{ids: []string{"p1", "p2"}, out: autonameOut{Named: []namedTab{
		{Session: "p1", Name: "zfs-send-resume-review", Title: "ZFS Send Resume Review", How: "claude"},
		{Session: "p2", Name: "why-is-wifi-slow", Title: "why is wifi slow", How: "prompt", ClaudeError: "claude exited 1"},
	}}}
	m, cmd = send(m, res)
	if m.naming || cmd == nil {
		t.Errorf("after the result: naming=%v cmd=%v", m.naming, cmd != nil)
	}
	for _, want := range []string{"named 2 tabs", "ZFS Send Resume Review (zfs-send-resume-review)", "why is wifi slow (why-is-wifi-slow)",
		"; 1 from the first"} {
		if !strings.Contains(m.status, want) {
			t.Errorf("status lacks %q: %q", want, m.status)
		}
	}
	// An error from the CLI is a status line, and the tabs stay tried (no
	// retry storm on a broken CLI).
	m, _ = send(m, autonameMsg{ids: []string{"p1"}, err: "work autoname: boom"})
	if !strings.Contains(m.status, "naming tabs: work autoname: boom") || !m.nameTried["p1"] {
		t.Errorf("error status = %q tried=%v", m.status, m.nameTried)
	}
	// Nothing named: the first reason is shown.
	st := autonameStatus(autonameMsg{out: autonameOut{Skipped: []struct {
		Session string `json:"session_id"`
		Why     string `json:"why"`
	}{{"p1", "no prompt yet"}}}})
	if !strings.Contains(st, "no tab named (1 skipped: no prompt yet)") {
		t.Errorf("skipped status = %q", st)
	}
}

func TestAutonameOffByFlagOrFile(t *testing.T) {
	for _, c := range []struct {
		args []string
		want int
	}{{nil, 0}, {[]string{"--no-autoname"}, -1}, {[]string{"--autoname"}, 1}} {
		o, err := parseArgs(c.args)
		if err != nil || o.autoname != c.want {
			t.Errorf("parseArgs(%v) autoname = %d err %v, want %d", c.args, o.autoname, err, c.want)
		}
	}
	if m := newModel(opts{autoname: -1}, "savannah"); m.autoname {
		t.Error("--no-autoname should turn naming off")
	}
	if m := newModel(opts{}, "savannah"); !m.autoname {
		t.Error("naming is on by default")
	}
	// The board file keeps both keys: Ctrl-G rewrites the grouping without
	// losing a hand-written "autoname": "off".
	file := filepath.Join(t.TempDir(), "board.json")
	if err := saveBoardPrefs(file, boardPrefs{Autoname: "off"}); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SAVANNAH_BOARD_FILE", file)
	m := autonameBoard(t)
	m = m.toggleGroup()
	if p := loadBoardPrefs(file); p.Autoname != "off" || p.Group != "repo" {
		t.Errorf("board file after Ctrl-G = %+v, want autoname off kept and group repo", p)
	}
}
