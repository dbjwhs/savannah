package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	tea "github.com/charmbracelet/bubbletea"
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
		// A work session's worktree names the repo and the work, not the
		// fixed middle component.
		{"/Users/dbjones/ng/savannah/.savannah-worktrees/feat-x", "savannah/feat-x"},
		{"/.savannah-worktrees/feat-x", ".savannah-worktrees/feat-x"},
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
	// An offline work row says what you were doing (its last note), or that
	// nothing is live.
	off := session{State: "offline", Message: "stuck on the flaky test"}
	if got := sessionDetail(off); got != "stuck on the flaky test" {
		t.Errorf("offline+note detail: %q", got)
	}
	if got := sessionDetail(session{State: "offline"}); got != "offline, no live session" {
		t.Errorf("offline detail: %q", got)
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

// ---- work sessions (work/savannah_work.py records joined at read time) ----

func TestLastNoteFirstLine(t *testing.T) {
	cases := []struct{ in, want string }{
		{"", ""},
		{"\n\n", ""},
		{"## 2026-10-27 09:00 local\nfirst note\nmore\n\n", "first note"},
		{"## 2026-10-27 09:00 local\nold\n\n## 2026-10-27 10:30 local\nnewest line one\nline two\n\n",
			"newest line one"},
		{"## 2026-10-27 10:30 local\n\n  indented after blank  \n", "indented after blank"},
		{"## 2026-10-27 10:30 local\n", ""}, // heading with no body
		{"no heading at all\nsecond", "no heading at all"},
	}
	for _, c := range cases {
		if got := lastNoteFirstLine(c.in); got != c.want {
			t.Errorf("lastNoteFirstLine(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestSamePath(t *testing.T) {
	dir := t.TempDir()
	real := filepath.Join(dir, "real")
	if err := os.Mkdir(real, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(real, link); err != nil {
		t.Skip("symlinks unavailable:", err)
	}
	if !samePath(real, link) {
		t.Error("symlink and target should join (realpath compare)")
	}
	if !samePath(real, real+"/") {
		t.Error("trailing slash should still join")
	}
	// Neither side exists: fall back to a lexical compare so a removed
	// worktree still joins its stale hook file.
	if !samePath("/gone/x/../y", "/gone/y") {
		t.Error("cleaned-path fallback should join")
	}
	if samePath(real, filepath.Join(dir, "other")) {
		t.Error("different paths should not join")
	}
	if samePath("", real) || samePath(real, "") {
		t.Error("empty never joins")
	}
}

func TestPickWorkSession(t *testing.T) {
	rec := workRecord{Name: "feat", Worktree: "/r/.savannah-worktrees/feat", ClaudePane: "UUID-C"}
	none := []session{{ID: "a", Project: "/elsewhere"}}
	if got := pickWorkSession(rec, none); got != -1 {
		t.Errorf("no match: got %d, want -1", got)
	}
	// Single match on project.
	one := []session{{ID: "x", Project: "/elsewhere"}, {ID: "a", Project: "/r/.savannah-worktrees/feat"}}
	if got := pickWorkSession(rec, one); got != 1 {
		t.Errorf("single match: got %d, want 1", got)
	}
	// Several match: the one in the record's Claude pane wins, even if older.
	several := []session{
		{ID: "newer", Project: "/r/.savannah-worktrees/feat", Updated: 900, ITerm: "w0t1p0:UUID-OTHER"},
		{ID: "pane", Project: "/r/.savannah-worktrees/feat", Updated: 100, ITerm: "w0t2p0:UUID-C"},
		{ID: "mid", Project: "/r/.savannah-worktrees/feat", Updated: 500},
	}
	if got := pickWorkSession(rec, several); got != 1 || several[got].ID != "pane" {
		t.Errorf("pane preference: got %d", got)
	}
	// No pane match: most recently updated wins.
	rec.ClaudePane = "UUID-NOT-RUNNING"
	if got := pickWorkSession(rec, several); got != 0 || several[got].ID != "newer" {
		t.Errorf("most-recent fallback: got %d", got)
	}
	// An empty claude_pane never matches an empty ITerm by accident.
	rec.ClaudePane = ""
	several[2].ITerm = ""
	if got := pickWorkSession(rec, several); got != 0 {
		t.Errorf("empty pane must fall back to most recent: got %d", got)
	}
}

func TestJoinWorkTagsAndSynthesizes(t *testing.T) {
	sessions := []session{
		{ID: "s1", Project: "/r/.savannah-worktrees/live", State: "working", Updated: 50, ITerm: "w0t0p0:UUID-L"},
		{ID: "s2", Project: "/other/repo", State: "idle", Updated: 60},
	}
	work := []workRecord{
		{Name: "live", Worktree: "/r/.savannah-worktrees/live", ClaudePane: "UUID-L", Updated: 10},
		{Name: "gone", Worktree: "/r/.savannah-worktrees/gone", ClaudePane: "UUID-G",
			Terminal: "iterm", Updated: 20, lastNote: "left off at the parser"},
	}
	got := joinWork(sessions, work)
	if len(got) != 3 {
		t.Fatalf("rows = %d, want 3 (2 hook + 1 offline)", len(got))
	}
	byID := map[string]session{}
	for _, s := range got {
		byID[s.ID] = s
	}
	if byID["s1"].Work != "live" {
		t.Errorf("s1 not tagged: %+v", byID["s1"])
	}
	if byID["s2"].Work != "" {
		t.Errorf("s2 wrongly tagged: %+v", byID["s2"])
	}
	off, ok := byID["work:gone"]
	if !ok {
		t.Fatalf("no offline row synthesized: %v", got)
	}
	if off.State != "offline" || off.Work != "gone" || off.Project != "/r/.savannah-worktrees/gone" ||
		off.ITerm != "UUID-G" || off.Message != "left off at the parser" || off.Updated != 20 {
		t.Errorf("offline row fields: %+v", off)
	}
	if !isWorkRow(off) || isWorkRow(byID["s1"]) {
		t.Error("isWorkRow should flag only the synthesized row")
	}
	// Offline sorts after everything live, so the tabs you have open come first.
	if got[len(got)-1].ID != "work:gone" {
		t.Errorf("offline row should sort last: %v", got)
	}
	// Inputs are not mutated.
	if sessions[0].Work != "" {
		t.Error("joinWork mutated its input")
	}
}

// Finding (a): two records on one worktree (two `work new --no-worktree` in
// the same repo). A later record's most-recent fallback must not overwrite an
// earlier record's pane match, and each record tags at most one session.
func TestJoinWorkTwoRecordsOneWorktreeLive(t *testing.T) {
	wt := "/r/shared"
	sessions := []session{
		// Newest session, running in b-pane's Claude pane.
		{ID: "sA", Project: wt, State: "working", Updated: 900, ITerm: "w0t0p0:UUID-A"},
		// An older plain tab in the same worktree, no pane match.
		{ID: "sX", Project: wt, State: "idle", Updated: 100},
	}
	// Records arrive sorted by name, so the one needing the fallback comes
	// FIRST: a single pass would hand it sA (newest) and then b-pane's pane
	// match would be stolen or overwritten depending on order.
	work := []workRecord{
		{Name: "a-fallback", Worktree: wt, ClaudePane: "UUID-GONE"},
		{Name: "b-pane", Worktree: wt, ClaudePane: "UUID-A"},
	}
	got := joinWork(sessions, work)
	byID := map[string]session{}
	for _, s := range got {
		byID[s.ID] = s
	}
	if len(got) != 2 {
		t.Fatalf("rows = %d, want 2 (no offline row, both records joined): %+v", len(got), got)
	}
	if byID["sA"].Work != "b-pane" {
		t.Errorf("pane match stolen: sA.Work = %q, want b-pane", byID["sA"].Work)
	}
	if byID["sX"].Work != "a-fallback" {
		t.Errorf("fallback should take the unclaimed session: sX.Work = %q", byID["sX"].Work)
	}

	// One live session, no pane match, two records: the first record (by
	// order) gets it, the second gets its own offline row, never a steal.
	one := []session{{ID: "s1", Project: wt, State: "working", Updated: 5}}
	work2 := []workRecord{{Name: "first", Worktree: wt}, {Name: "second", Worktree: wt}}
	got = joinWork(one, work2)
	if len(got) != 2 {
		t.Fatalf("rows = %d, want 2 (1 tagged + 1 offline): %+v", len(got), got)
	}
	byID = map[string]session{}
	for _, s := range got {
		byID[s.ID] = s
	}
	if byID["s1"].Work != "first" {
		t.Errorf("s1.Work = %q, want first", byID["s1"].Work)
	}
	if off, ok := byID["work:second"]; !ok || off.State != "offline" || off.Work != "second" {
		t.Errorf("second record should get its own offline row: %+v", got)
	}
}

// Finding (a), all tabs closed: the second record must not tag the first
// record's synthesized offline row. Both get their own offline row.
func TestJoinWorkTwoRecordsOneWorktreeAllOffline(t *testing.T) {
	wt := "/r/shared"
	work := []workRecord{
		{Name: "one", Worktree: wt, ClaudePane: "P1", Updated: 1, lastNote: "note one"},
		{Name: "two", Worktree: wt, ClaudePane: "P2", Updated: 2, lastNote: "note two"},
	}
	got := joinWork(nil, work)
	if len(got) != 2 {
		t.Fatalf("rows = %d, want 2 offline rows: %+v", len(got), got)
	}
	byID := map[string]session{}
	for _, s := range got {
		byID[s.ID] = s
	}
	for _, name := range []string{"one", "two"} {
		off, ok := byID["work:"+name]
		if !ok {
			t.Fatalf("missing offline row for %s: %+v", name, got)
		}
		if off.Work != name || off.State != "offline" || off.Message != "note "+name {
			t.Errorf("offline row for %s: %+v", name, off)
		}
	}
	// Re-joining the joined output against the same records is idempotent:
	// synthesized rows are never join candidates, so no duplicates and no
	// cross-tagging.
	again := joinWork(got, work)
	if len(again) != 2 {
		t.Errorf("re-join rows = %d, want 2: %+v", len(again), again)
	}
	// A previously tagged hook session whose record vanished loses its tag.
	stale := []session{{ID: "s1", Project: "/elsewhere", Work: "ghost", State: "idle"}}
	if got := joinWork(stale, nil); got[0].Work != "" {
		t.Errorf("stale WORK tag should be cleared on re-join: %+v", got[0])
	}
}

// Offline work rows are not "need you": they are excluded from the attention
// counter and from the header.
func TestOfflineRowsNotAttention(t *testing.T) {
	offline := []session{
		offlineWorkRow(workRecord{Name: "p1", Worktree: "/r/p1", Updated: nowUnix()}),
		offlineWorkRow(workRecord{Name: "p2", Worktree: "/r/p2", Updated: nowUnix()}),
	}
	if n := countSessionAttention(offline); n != 0 {
		t.Errorf("countSessionAttention(offline rows) = %d, want 0", n)
	}
	m := model{width: 100, noMesh: true, tasks: map[string][]task{}, errs: map[string]string{},
		sessions: offline}
	m.rebuildRows()
	if out := m.View(); strings.Contains(out, "need you") {
		t.Errorf("header must not count offline rows as needing you:\n%s", out)
	}
	// One waiting tab plus offline rows: exactly 1.
	m.sessions = append([]session{{ID: "w", State: "waiting", Updated: nowUnix()}}, offline...)
	m.rebuildRows()
	if out := m.View(); !strings.Contains(out, "1 need you") {
		t.Errorf("header should say 1 need you:\n%s", out)
	}
}

// Finding (b): a sessions-dir read error must not wipe the live rows (every
// work record would flip to offline), and the error line must render even
// with zero rows.
func TestSessionsMsgErrorKeepsLiveRows(t *testing.T) {
	m := model{width: 100, noMesh: true, tasks: map[string][]task{}, errs: map[string]string{}}
	hooks := []session{
		{ID: "s1", Project: "/r/.savannah-worktrees/feat", State: "working", Updated: nowUnix(), ITerm: "w0t0p0:UUID-F"},
		{ID: "s2", Project: "/other", State: "waiting", Updated: nowUnix()},
	}
	work := []workRecord{{Name: "feat", Worktree: "/r/.savannah-worktrees/feat", ClaudePane: "UUID-F"}}
	next, _ := m.Update(sessionsMsg{hooks: hooks, work: work})
	m = next.(model)
	if len(m.rows) != 2 || m.sessions[0].State == "offline" || m.sessions[1].State == "offline" {
		t.Fatalf("populated board: %+v", m.sessions)
	}

	// Now the sessions dir fails to read: hooks nil, err set, work still fresh.
	next, _ = m.Update(sessionsMsg{err: "permission denied", work: work})
	m = next.(model)
	if len(m.rows) != 2 {
		t.Fatalf("rows after error = %d, want 2 (last good list kept): %+v", len(m.rows), m.sessions)
	}
	byID := map[string]session{}
	for _, s := range m.sessions {
		byID[s.ID] = s
	}
	if byID["s1"].State != "working" || byID["s1"].Work != "feat" {
		t.Errorf("s1 should still be live and tagged: %+v", byID["s1"])
	}
	if _, off := byID["work:feat"]; off {
		t.Error("work record must not flip to offline on a sessions read error")
	}
	if byID["s2"].State != "waiting" || countSessionAttention(m.sessions) != 1 {
		t.Errorf("s2 should still be waiting: %+v", byID["s2"])
	}
	out := m.View()
	if !strings.Contains(out, "sessions: permission denied") {
		t.Errorf("error line missing:\n%s", out)
	}
	if !strings.Contains(out, "1 need you") {
		t.Errorf("attention should survive the error:\n%s", out)
	}

	// Fresh work records still re-join against the kept hooks: the record is
	// gone, so the tag clears, no offline row appears.
	next, _ = m.Update(sessionsMsg{err: "permission denied", work: nil})
	m = next.(model)
	if len(m.rows) != 2 {
		t.Errorf("rows = %d, want 2", len(m.rows))
	}
	for _, s := range m.sessions {
		if s.Work != "" || isWorkRow(s) {
			t.Errorf("tag should clear when the record vanishes: %+v", s)
		}
	}

	// A work-dir read error keeps the last good records the same way.
	next, _ = m.Update(sessionsMsg{hooks: hooks, work: work})
	m = next.(model)
	next, _ = m.Update(sessionsMsg{hooks: hooks, workErr: "disk gone"})
	m = next.(model)
	for _, s := range m.sessions {
		if s.ID == "s1" && s.Work != "feat" {
			t.Errorf("work tag should survive a work-dir error: %+v", s)
		}
	}
	if out := m.View(); !strings.Contains(out, "work: disk gone") {
		t.Errorf("work error line missing:\n%s", out)
	}

	// Recovery: a clean read replaces the kept list and clears the line.
	next, _ = m.Update(sessionsMsg{hooks: hooks[:1], work: work})
	m = next.(model)
	if len(m.rows) != 1 || m.sessErr != "" || m.workErr != "" {
		t.Errorf("recovery: rows=%d sessErr=%q workErr=%q", len(m.rows), m.sessErr, m.workErr)
	}

	// Zero rows and an error: the line must still render (it used to sit
	// inside the row-count guard).
	empty := model{width: 100, noMesh: true, tasks: map[string][]task{}, errs: map[string]string{}}
	next, _ = empty.Update(sessionsMsg{err: "boom"})
	empty = next.(model)
	if len(empty.rows) != 0 {
		t.Fatalf("rows = %d, want 0", len(empty.rows))
	}
	if out := empty.View(); !strings.Contains(out, "sessions: boom") || !strings.Contains(out, "LOCAL SESSIONS") {
		t.Errorf("error with zero rows must render:\n%s", out)
	}
	// And with no error and no rows, the section stays hidden as before.
	empty = model{width: 100, noMesh: true, tasks: map[string][]task{}, errs: map[string]string{}}
	if out := empty.View(); strings.Contains(out, "LOCAL SESSIONS") {
		t.Errorf("empty board without error should hide the section:\n%s", out)
	}
}

// Finding (c): the dash applies the CLI's name rule, so both list the same
// directories.
func TestValidWorkName(t *testing.T) {
	good := []string{"a", "0", "feat-x", "feat.x", "feat_x", "t-", "t-x", "t1", "a.b.c",
		strings.Repeat("a", 64)}
	for _, n := range good {
		if !validWorkName(n) {
			t.Errorf("validWorkName(%q) = false, want true", n)
		}
	}
	bad := []string{"", ".", "..", ".hidden", "-x", "_x", "Upper", "has space", "a/b",
		"t-1", "t-123", "x.", "x.lock", ".lock", "a..", "a..b", "a...b", "archive", "ü",
		strings.Repeat("a", 65)}
	for _, n := range bad {
		if validWorkName(n) {
			t.Errorf("validWorkName(%q) = true, want false", n)
		}
	}
}

func TestReadWorkSkipsInvalidNames(t *testing.T) {
	dir := t.TempDir()
	rec := []byte(`{"worktree":"/r","updated":1}`)
	for _, name := range []string{"ok-1", "Upper", "t-42", "x.lock", "x.", "-dash", ".dot",
		strings.Repeat("z", 65)} {
		d := filepath.Join(dir, name)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(d, "record.json"), rec, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	got, err := readWork(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Name != "ok-1" {
		t.Errorf("readWork should list only the valid name: %+v", got)
	}
}

func TestReadWork(t *testing.T) {
	dir := t.TempDir()
	mk := func(name string, rec map[string]any, notes string) {
		d := filepath.Join(dir, name)
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
		if rec != nil {
			b, _ := json.Marshal(rec)
			if err := os.WriteFile(filepath.Join(d, "record.json"), b, 0o644); err != nil {
				t.Fatal(err)
			}
		}
		if notes != "" {
			os.WriteFile(filepath.Join(d, "notes.md"), []byte(notes), 0o644)
		}
	}
	mk("beta", map[string]any{
		"name": "beta", "repo": "/r", "worktree": "/r/.savannah-worktrees/beta",
		"branch": "work/beta", "created": 1, "updated": 2, "terminal": "iterm",
		"tab_id": "TAB-1", "claude_pane": "PANE-C-1", "shell_pane": "PANE-S-1", "prompt": "go"},
		"## 2026-10-27 09:00 local\nfirst\n\n## 2026-10-27 09:05 local\nlast note here\nmore\n\n")
	mk("alpha", map[string]any{"worktree": "/r2", "updated": 5}, "")   // no name: dir name used
	mk("liar", map[string]any{"name": "other", "worktree": "/r3"}, "") // file name ignored
	mk("nowt", map[string]any{"name": "nowt"}, "")                     // no worktree: skipped
	mk("archive", nil, "")                                             // notes graveyard, skipped
	mk("noting", nil, "")                                              // no record.json, skipped
	mk("broken", nil, "")
	os.WriteFile(filepath.Join(dir, "broken", "record.json"), []byte("{nope"), 0o644)
	os.WriteFile(filepath.Join(dir, "stray.json"), []byte("{}"), 0o644) // a file, not a dir

	got, err := readWork(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("got %d records, want 3: %+v", len(got), got)
	}
	if got[2].Name != "liar" || got[2].Worktree != "/r3" {
		t.Errorf("name must come from the directory, not the file: %+v", got[2])
	}
	if got[0].Name != "alpha" || got[0].Worktree != "/r2" || got[0].lastNote != "" {
		t.Errorf("alpha = %+v", got[0])
	}
	b := got[1]
	if b.Name != "beta" || b.Branch != "work/beta" || b.ClaudePane != "PANE-C-1" ||
		b.ShellPane != "PANE-S-1" || b.TabID != "TAB-1" || b.Updated != 2 || b.Prompt != "go" {
		t.Errorf("beta fields: %+v", b)
	}
	if b.lastNote != "last note here" {
		t.Errorf("beta lastNote = %q", b.lastNote)
	}

	// A missing directory is not an error; an empty root means "no work".
	if got, err := readWork(filepath.Join(dir, "nope")); err != nil || got != nil {
		t.Errorf("missing dir: got %v, err %v", got, err)
	}
	if got, err := readWork(""); err != nil || got != nil {
		t.Errorf("empty dir: got %v, err %v", got, err)
	}
}

func TestCtrlXOnOfflineWorkRowKeepsRecord(t *testing.T) {
	wdir := t.TempDir()
	d := filepath.Join(wdir, "keepme")
	os.MkdirAll(d, 0o755)
	recPath := filepath.Join(d, "record.json")
	os.WriteFile(recPath, []byte(`{"name":"keepme","worktree":"/nowhere/keepme","claude_pane":"P"}`), 0o644)
	recs, err := readWork(wdir)
	if err != nil || len(recs) != 1 {
		t.Fatalf("readWork: %v %v", recs, err)
	}
	m := model{tasks: map[string][]task{}, errs: map[string]string{}}
	m.sessions = joinWork(nil, recs)
	m.rebuildRows()
	if len(m.rows) != 1 || !isWorkRow(m.rows[0].s) {
		t.Fatalf("rows = %+v", m.rows)
	}
	next, cmd := m.Update(tea.KeyMsg{Type: tea.KeyCtrlX})
	if cmd != nil {
		t.Error("Ctrl-X on an offline work row must not dispatch a dismiss")
	}
	if st := next.(model).status; !strings.Contains(st, "work rm keepme") {
		t.Errorf("status should point at work rm: %q", st)
	}
	if _, err := os.Stat(recPath); err != nil {
		t.Errorf("record.json must survive Ctrl-X: %v", err)
	}
	// Enter on it jumps via the Claude pane, like any session row.
	if _, cmd := m.Update(tea.KeyMsg{Type: tea.KeyEnter}); cmd == nil {
		t.Error("Enter on an offline work row should produce a jump command")
	}
}

func TestViewHasWorkColumn(t *testing.T) {
	m := model{
		width: 100, tasks: map[string][]task{}, errs: map[string]string{},
		sessions: []session{
			{ID: "s1", State: "working", Project: "/r/.savannah-worktrees/feat", Work: "feat", Updated: nowUnix()},
			offlineWorkRow(workRecord{Name: "parked", Worktree: "/r/.savannah-worktrees/parked",
				lastNote: "resume at step 3 " + strings.Repeat("and keep going ", 8), Updated: nowUnix()}),
		},
		noMesh: true,
	}
	m.rebuildRows()
	out := m.View()
	if !strings.Contains(out, "WORK") || !strings.Contains(out, "PROJECT") {
		t.Errorf("header missing WORK/PROJECT:\n%s", out)
	}
	// The worktree reads as repo/name, the WORK cell as the name, and the
	// offline row's WHAT cell as its last note.
	for _, want := range []string{"feat", "r/feat", "parked", "r/parked", "offline", "resume at step 3"} {
		if !strings.Contains(out, want) {
			t.Errorf("view missing %q:\n%s", want, out)
		}
	}
	// Every session row fits in 100 columns. Styles render as plain text
	// without a tty; skip the width check if escape codes sneak in anyway.
	for _, ln := range strings.Split(out, "\n") {
		if strings.Contains(ln, "\x1b") {
			continue
		}
		if (strings.Contains(ln, "parked") || strings.Contains(ln, "feat")) && len([]rune(ln)) > 100 {
			t.Errorf("row wider than 100 columns (%d): %q", len([]rune(ln)), ln)
		}
	}
}

func TestFitLabel(t *testing.T) {
	hint := "no live session; Enter focuses its Claude pane, `work rm` removes it"
	long := "work " + strings.Repeat("n", 64)
	for _, w := range []int{80, 100, 140} {
		got := fitLabel(long, w, hint)
		if n := len([]rune(got)) + 3 + len([]rune(hint)); n > w && len([]rune(got)) > 8 {
			t.Errorf("width %d: footer is %d runes", w, n)
		}
	}
	if fitLabel("short", 0, hint) != "short" {
		t.Errorf("zero width leaves the label alone")
	}
	if fitLabel("short", 200, hint) != "short" {
		t.Errorf("roomy width leaves the label alone")
	}
}
