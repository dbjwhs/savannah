package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestHumanAge(t *testing.T) {
	cases := []struct {
		sec  int64
		want string
	}{
		{-5, "0s"}, {0, "0s"}, {45, "45s"}, {59, "59s"},
		{60, "1m"}, {119, "1m"}, {3599, "59m"},
		{3600, "1h"}, {86399, "23h"}, {86400, "1d"}, {200000, "2d"},
	}
	for _, c := range cases {
		if got := humanAge(c.sec); got != c.want {
			t.Errorf("humanAge(%d) = %q, want %q", c.sec, got, c.want)
		}
	}
}

func TestProjectLabel(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", "~"},
		{"/", "~"}, // root trims to empty; never a real project dir
		{"/Users/dbjones/ng/delphix/zfs", "delphix/zfs"},
		{"/Users/dbjones/ng/delphix/zfs/", "delphix/zfs"},
		{"proj", "proj"},
		{"/one", "one"},
		{"/a/b", "a/b"},
	}
	for _, c := range cases {
		if got := projectLabel(c.in); got != c.want {
			t.Errorf("projectLabel(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestSessionDetail(t *testing.T) {
	perm := session{State: "waiting", Notify: "permission_prompt", Message: "use Bash"}
	if got := sessionDetail(perm); got != "needs permission: use Bash" {
		t.Errorf("permission detail: %q", got)
	}
	idle := session{State: "waiting", Notify: "idle_prompt"}
	if got := sessionDetail(idle); got != "idle, waiting for you" {
		t.Errorf("idle detail: %q", got)
	}
	working := session{State: "working", Mode: "acceptEdits"}
	if got := sessionDetail(working); got != "working (acceptEdits)" {
		t.Errorf("working+mode detail: %q", got)
	}
	done := session{State: "done", Mode: "default"}
	if got := sessionDetail(done); got != "done" {
		t.Errorf("done detail: %q", got)
	}
}

func TestSortAndAttention(t *testing.T) {
	ss := []session{
		{ID: "idle1", State: "idle", Updated: 100},
		{ID: "wait-new", State: "waiting", Updated: 500},
		{ID: "done1", State: "done", Updated: 300},
		{ID: "wait-old", State: "waiting", Updated: 200},
		{ID: "work1", State: "working", Updated: 400},
	}
	sortSessions(ss)
	order := []string{}
	for _, s := range ss {
		order = append(order, s.ID)
	}
	// waiting first (longest wait = oldest update on top), then working,
	// then done, then idle.
	want := []string{"wait-old", "wait-new", "work1", "done1", "idle1"}
	for i := range want {
		if order[i] != want[i] {
			t.Errorf("sort order = %v, want %v", order, want)
			break
		}
	}
	if n := countSessionAttention(ss); n != 2 {
		t.Errorf("countSessionAttention = %d, want 2", n)
	}
}

func TestReadSessions(t *testing.T) {
	dir := t.TempDir()
	write := func(name string, obj map[string]any) {
		b, _ := json.Marshal(obj)
		if err := os.WriteFile(filepath.Join(dir, name), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("a.json", map[string]any{
		"session_id": "a", "state": "waiting", "project": "/x/y",
		"updated": 10, "notify_type": "permission_prompt", "message": "use Bash"})
	write("b.json", map[string]any{"session_id": "b", "state": "done", "updated": 20})
	write("bad.json", map[string]any{"state": "waiting"}) // no session_id: skipped
	os.WriteFile(filepath.Join(dir, "garbage.json"), []byte("{not json"), 0o644)
	os.WriteFile(filepath.Join(dir, "ignore.txt"), []byte("x"), 0o644)

	got, err := readSessions(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("got %d sessions, want 2 (bad/garbage/txt skipped)", len(got))
	}
	// waiting sorts before done, and the file path is attached for dismiss.
	if got[0].ID != "a" || got[0].file == "" {
		t.Errorf("first = %+v (want id a with file set)", got[0])
	}
	if got[0].Message != "use Bash" || got[0].Notify != "permission_prompt" {
		t.Errorf("fields not parsed: %+v", got[0])
	}

	// A missing directory is not an error.
	got, err = readSessions(filepath.Join(dir, "does-not-exist"))
	if err != nil || got != nil {
		t.Errorf("missing dir: got %v, err %v", got, err)
	}
}

func TestSessionRowsInRebuild(t *testing.T) {
	m := model{
		tasks:    map[string][]task{"n1": {{ID: "t-1", State: "idle"}}},
		nodes:    []string{"n1"},
		sessions: []session{{ID: "s1", State: "waiting"}},
	}
	m.rebuildRows()
	if len(m.rows) != 2 {
		t.Fatalf("rows = %d, want 2", len(m.rows))
	}
	// Sessions render first.
	if m.rows[0].kind != rowSession || m.rows[0].s.ID != "s1" {
		t.Errorf("row 0 = %+v, want session s1", m.rows[0])
	}
	if m.rows[1].kind != rowTask || m.rows[1].t.ID != "t-1" {
		t.Errorf("row 1 = %+v, want task t-1", m.rows[1])
	}
	nSess, nTask := m.sectionCounts()
	if nSess != 1 || nTask != 1 {
		t.Errorf("counts = %d/%d, want 1/1", nSess, nTask)
	}
}
