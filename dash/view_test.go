package main

import (
	"reflect"
	"strings"
	"testing"
)

func TestWrapTo(t *testing.T) {
	cases := []struct {
		in, want string
		w        int
	}{
		{"", "", 4},
		{"abc", "abc", 4},
		{"abcd", "abcd", 4},
		{"abcde", "abcd\ne", 4},
		{"abcdefgh", "abcd\nefgh", 4},
		{"ab\ncdefgh", "ab\ncdef\ngh", 4},
		{"trailing\n", "trailing\n", 10},
		{"anything", "anything", 0}, // no width yet: pass through
		{"ééééé", "éééé\né", 4},     // runes, not bytes
	}
	for _, c := range cases {
		if got := wrapTo(c.in, c.w); got != c.want {
			t.Errorf("wrapTo(%q, %d) = %q, want %q", c.in, c.w, got, c.want)
		}
	}
}

func TestTrimFront(t *testing.T) {
	// Under the cap: untouched.
	if got := trimFront([]byte("abc"), 10); string(got) != "abc" {
		t.Errorf("under cap: got %q", got)
	}
	// Over the cap: keeps a tail no bigger than max, starting after a newline.
	b := []byte("line one\nline two\nline three\n")
	got := trimFront(b, 15) // tail 15 = " two\nline three\n" minus torn head
	if len(got) > 15 {
		t.Errorf("tail too big: %d bytes", len(got))
	}
	if string(got) != "line three\n" {
		t.Errorf("got %q, want %q", got, "line three\n")
	}
	// No newline in the tail: keep the raw tail.
	raw := []byte(strings.Repeat("x", 100))
	if got := trimFront(raw, 10); len(got) != 10 {
		t.Errorf("raw tail: got %d bytes, want 10", len(got))
	}
}

func TestNeighborTaskRow(t *testing.T) {
	rows := []row{
		{kind: rowTask, node: "a", t: task{ID: "t1"}},
		{kind: rowTask, node: "a", t: task{ID: "t2"}},
		{kind: rowTask, node: "b", t: task{ID: "t1"}},
	}
	if got, ok := neighborTaskRow(rows, "a", "t2", 1); !ok || got != 2 {
		t.Errorf("next: got %d ok=%v, want 2", got, ok)
	}
	if got, ok := neighborTaskRow(rows, "b", "t1", 1); !ok || got != 0 { // wraps
		t.Errorf("wrap next: got %d ok=%v, want 0", got, ok)
	}
	if got, ok := neighborTaskRow(rows, "a", "t1", -1); !ok || got != 2 { // wraps back
		t.Errorf("wrap prev: got %d ok=%v, want 2", got, ok)
	}
	// A vanished target counts from the top task row.
	if got, ok := neighborTaskRow(rows, "z", "zz", 1); !ok || got != 1 {
		t.Errorf("gone target: got %d ok=%v, want 1", got, ok)
	}

	// Session rows are skipped: switching lands only on task rows, and the
	// returned index is into the full row slice.
	mixed := []row{
		{kind: rowSession, s: session{ID: "s1"}},
		{kind: rowTask, node: "a", t: task{ID: "t1"}},
		{kind: rowSession, s: session{ID: "s2"}},
		{kind: rowTask, node: "b", t: task{ID: "t2"}},
	}
	if got, ok := neighborTaskRow(mixed, "a", "t1", 1); !ok || got != 3 {
		t.Errorf("skip sessions fwd: got %d ok=%v, want 3", got, ok)
	}
	if got, ok := neighborTaskRow(mixed, "a", "t1", -1); !ok || got != 3 { // wraps past sessions
		t.Errorf("skip sessions back: got %d ok=%v, want 3", got, ok)
	}
	// No task rows at all: ok is false.
	if _, ok := neighborTaskRow([]row{{kind: rowSession}}, "x", "y", 1); ok {
		t.Error("no task rows should return ok=false")
	}
}

func TestTaskArgs(t *testing.T) {
	// Mesh mode with a key: no --addr even if set (addr is pin-only).
	m := model{key: "mesh.key", addr: "1.2.3.4:9"}
	got := m.taskArgs("tail", "n1", "t-1")
	want := []string{"task", "tail", "n1", "t-1", "--key", "mesh.key"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("mesh: got %v, want %v", got, want)
	}
	// Pinned node with --addr.
	m = model{pin: "n1", addr: "1.2.3.4:9", key: "k"}
	got = m.taskArgs("ls", "n1", "--json")
	want = []string{"task", "ls", "n1", "--json", "--addr", "1.2.3.4:9", "--key", "k"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("pinned: got %v, want %v", got, want)
	}
}

// Drive the tail state machine with synthetic messages: live data
// accumulates, stale-sequence data is ignored, a completed run is promoted
// to the flicker-free fallback, and a fresh (shorter) replay does not
// replace it on screen until it catches up.
func TestTailRunLifecycle(t *testing.T) {
	m := model{mode: modeView, tailSeq: 1, follow: true, width: 80, height: 24}

	step := func(msg interface{}) {
		next, _ := m.updateTail(msg)
		m = next.(model)
	}

	step(tailDataMsg{seq: 1, data: []byte("hello ")})
	step(tailDataMsg{seq: 1, data: []byte("world\n")})
	if string(m.tailBuf) != "hello world\n" {
		t.Fatalf("tailBuf = %q", m.tailBuf)
	}

	step(tailDataMsg{seq: 0, data: []byte("STALE")}) // old run: ignored
	if string(m.tailBuf) != "hello world\n" {
		t.Fatalf("stale data leaked in: %q", m.tailBuf)
	}

	step(tailExitMsg{seq: 1}) // run completed: promoted, current reset
	if string(m.lastBuf) != "hello world\n" || m.tailBuf != nil {
		t.Fatalf("after exit: lastBuf=%q tailBuf=%q", m.lastBuf, m.tailBuf)
	}

	step(tailDataMsg{seq: 1, data: []byte("hello ")}) // replay catching up
	if got := displayBuf(m.tailBuf, m.lastBuf); string(got) != "hello world\n" {
		t.Fatalf("display flickered to short replay: %q", got)
	}
	step(tailDataMsg{seq: 1, data: []byte("world\nmore\n")}) // caught up
	if got := displayBuf(m.tailBuf, m.lastBuf); string(got) != "hello world\nmore\n" {
		t.Fatalf("display stuck on old run: %q", got)
	}

	step(tailExitMsg{seq: 99}) // stale exit: no promotion
	if string(m.lastBuf) != "hello world\n" {
		t.Fatalf("stale exit promoted: %q", m.lastBuf)
	}
}
