package main

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

// TestMain keeps slog quiet: the board logs through the default logger, and
// a test run is not a board run. It also points the board file at a temp
// directory, so no test ever writes the owner's own board.json (a test that
// wants a particular file still sets SAVANNAH_BOARD_FILE itself).
func TestMain(m *testing.M) {
	slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	dir, err := os.MkdirTemp("", "savannah-dash-test")
	if err != nil {
		panic(err)
	}
	os.Setenv("SAVANNAH_BOARD_FILE", filepath.Join(dir, "board.json"))
	code := m.Run()
	os.RemoveAll(dir)
	os.Exit(code)
}

func TestLevelNames(t *testing.T) {
	if levelName(LevelVerbose) != "VERBOSE" || levelName(slog.LevelDebug) != "DEBUG" ||
		levelName(slog.LevelWarn) != "WARN" || levelName(slog.Level(-20)) != "VERBOSE" {
		t.Errorf("levelName: %s %s %s", levelName(LevelVerbose), levelName(slog.LevelDebug), levelName(slog.LevelWarn))
	}
	cases := map[string]slog.Level{"VERBOSE": LevelVerbose, "verbose": LevelVerbose, "TRACE": LevelVerbose,
		"DEBUG": slog.LevelDebug, "INFO": slog.LevelInfo, "WARN": slog.LevelWarn, "WARNING": slog.LevelWarn,
		"ERROR": slog.LevelError}
	for name, want := range cases {
		if got, ok := levelFromName(name); !ok || got != want {
			t.Errorf("levelFromName(%q) = %v, %v", name, got, ok)
		}
	}
	if _, ok := levelFromName("LOUD"); ok {
		t.Error("LOUD is not a level")
	}
}

func TestFilterLog(t *testing.T) {
	lines := []string{
		`time=t level=VERBOSE msg=output src=dash`,
		`time=t level=DEBUG msg=command src=dash`,
		`time=t level=INFO msg=action src=dash`,
		`time=t level=WARN msg="command failed" src=work`,
		`a stray line with no stamp`,
		`time=t level=ERROR msg=crash src=hook`,
	}
	if got := filterLog(lines, slog.LevelInfo); len(got) != 4 || got[0] != lines[2] || got[1] != lines[3] {
		t.Errorf("normal = %q", got)
	}
	if got := filterLog(lines, slog.LevelDebug); len(got) != 5 || got[0] != lines[1] {
		t.Errorf("debug = %q", got)
	}
	if got := filterLog(lines, LevelVerbose); !reflect.DeepEqual(got, lines) {
		t.Errorf("verbose = %q", got)
	}
	if l, ok := lineLevel(`x level=debug y`); !ok || l != slog.LevelDebug {
		t.Errorf("lineLevel lowercase: %v %v", l, ok)
	}
	if _, ok := lineLevel(`nolevel=INFO`); ok {
		t.Error("nolevel=INFO is not a level stamp")
	}
}

func TestReadLogTail(t *testing.T) {
	path := filepath.Join(t.TempDir(), "savannah.log")
	if _, err := readLogTail(path, 100); !os.IsNotExist(err) {
		t.Errorf("missing file: err=%v", err)
	}
	var b strings.Builder
	for i := 0; i < 50; i++ {
		b.WriteString(strings.Repeat("x", 20) + "\n")
	}
	os.WriteFile(path, []byte(b.String()), 0o644)
	all, err := readLogTail(path, 1<<20)
	if err != nil || len(all) != 50 {
		t.Fatalf("whole file: %d lines, err=%v", len(all), err)
	}
	// A limit that lands mid-line drops the torn first line.
	tail, err := readLogTail(path, 105)
	if err != nil || len(tail) != 4 {
		t.Errorf("tail: %d lines (want 4), err=%v", len(tail), err)
	}
	for _, ln := range tail {
		if len(ln) != 20 {
			t.Errorf("torn line kept: %q", ln)
		}
	}
	os.WriteFile(path, nil, 0o644)
	if lines, err := readLogTail(path, 100); err != nil || lines != nil {
		t.Errorf("empty file: %q %v", lines, err)
	}
}

func TestRotate(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "savannah.log")
	os.WriteFile(path, []byte("small\n"), 0o644)
	rotate(path)
	if _, err := os.Stat(path + ".1"); err == nil {
		t.Error("a small file must not rotate")
	}
	for i := 1; i <= logKeep; i++ {
		os.WriteFile(path+"."+string(rune('0'+i)), []byte{byte('0' + i)}, 0o644)
	}
	f, _ := os.OpenFile(path, os.O_WRONLY, 0o644)
	f.Truncate(logMaxBytes) // sparse: the size is what rotate reads
	f.Close()
	rotate(path)
	if _, err := os.Stat(path); err == nil {
		t.Error("the full file should have moved to .1")
	}
	if b, _ := os.ReadFile(path + ".2"); string(b) != "1" {
		t.Errorf(".1 should have become .2, got %q", b)
	}
	if b, _ := os.ReadFile(path + ".3"); string(b) != "2" {
		t.Errorf(".2 should have become .3, got %q", b)
	}
	if st, err := os.Stat(path + ".1"); err != nil || st.Size() != logMaxBytes {
		t.Errorf(".1 should be the old log: %v", err)
	}
}

func TestLogWriterReopens(t *testing.T) {
	path := filepath.Join(t.TempDir(), "savannah.log")
	w := &logWriter{path: path}
	w.Write([]byte("one\n"))
	os.Rename(path, path+".1") // another writer rotated it away
	w.checked = time.Time{}    // force the next write's check
	w.Write([]byte("two\n"))
	if b, err := os.ReadFile(path); err != nil || string(b) != "two\n" {
		t.Errorf("after a swap the writer should reopen the path: %q %v", b, err)
	}
	if b, _ := os.ReadFile(path + ".1"); string(b) != "one\n" {
		t.Errorf("the rotated file should keep the first line: %q", b)
	}
}

func TestOpenLogWritesSlogText(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("SAVANNAH_LOG_DIR", dir)
	l := openLog()
	defer slog.SetDefault(slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Info("hello", "name", "demo")
	verbose("payload", "head", "x y")
	b, err := os.ReadFile(filepath.Join(dir, "savannah.log"))
	if err != nil {
		t.Fatalf("no log written: %v", err)
	}
	lines := strings.Split(strings.TrimSpace(string(b)), "\n")
	if len(lines) != 2 || !strings.Contains(lines[0], "level=INFO msg=hello src=dash name=demo") ||
		!strings.Contains(lines[1], "level=VERBOSE msg=payload src=dash head=\"x y\"") {
		t.Errorf("log lines = %q", lines)
	}
	if !strings.HasPrefix(lines[0], "time=20") || !strings.Contains(lines[0], "T") {
		t.Errorf("time stamp: %q", lines[0])
	}
	// run() logs the command at DEBUG and its output head at VERBOSE; a
	// failure is WARN with the stderr.
	if _, err := run("sh", "-c", "echo out; echo err >&2; exit 3"); err == nil {
		t.Error("exit 3 should be an error")
	}
	b, _ = os.ReadFile(filepath.Join(dir, "savannah.log"))
	if s := string(b); !strings.Contains(s, "level=WARN msg=\"command failed\"") || !strings.Contains(s, "err=err") ||
		!strings.Contains(s, "level=VERBOSE msg=output") || !strings.Contains(s, "head=out") {
		t.Errorf("run() log:\n%s", s)
	}
	if _, err := poll("sh", "-c", "true"); err != nil {
		t.Error(err)
	}
	b, _ = os.ReadFile(filepath.Join(dir, "savannah.log"))
	if s := string(b); strings.Contains(s, "level=DEBUG msg=command cmd=sh args=\"[-c true]\"") {
		t.Errorf("poll should log a success at VERBOSE, not DEBUG:\n%s", s)
	}
}

func TestLogView(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("SAVANNAH_LOG_DIR", dir)
	m := boardWith(t, false)
	m, cmd := press(m, tea.KeyCtrlL)
	if m.mode != modeLog || cmd == nil || !m.lg.follow {
		t.Fatalf("Ctrl-L: mode=%d cmd=%v follow=%v", m.mode, cmd != nil, m.lg.follow)
	}
	seq := m.lg.seq
	next, _ := m.Update(logReadMsg{seq: seq, err: "no log yet at " + logPath()})
	m = next.(model)
	if out := m.View(); !strings.Contains(out, "no log yet") || !strings.Contains(out, "level normal") {
		t.Errorf("empty log view:\n%s", out)
	}
	lines := []string{
		`time=t level=VERBOSE msg=output src=dash`,
		`time=t level=DEBUG msg=command src=dash`,
		`time=t level=INFO msg=action src=dash do=say`,
		`time=t level=WARN msg="command failed" src=work`,
	}
	next, _ = m.Update(logReadMsg{seq: seq, lines: lines})
	m = next.(model)
	if m.lg.shown != 2 || !strings.Contains(m.View(), "2 of 4 lines") || strings.Contains(m.View(), "msg=command") {
		t.Errorf("normal should show INFO and WARN only: shown=%d\n%s", m.lg.shown, m.View())
	}
	m = typeText(m, "d")
	if m.lg.shown != 3 || !strings.Contains(m.View(), "level debug") {
		t.Errorf("d: shown=%d", m.lg.shown)
	}
	m = typeText(m, "v")
	if m.lg.shown != 4 {
		t.Errorf("v: shown=%d", m.lg.shown)
	}
	m, _ = press(m, tea.KeyRight) // wraps to normal
	if m.lg.choice != logNormal {
		t.Errorf("Right from verbose should wrap to normal, got %d", m.lg.choice)
	}
	m, _ = press(m, tea.KeyLeft)
	if m.lg.choice != logVerbose {
		t.Errorf("Left from normal should wrap to verbose, got %d", m.lg.choice)
	}
	m = typeText(m, "n")
	// A stale read (older seq) is ignored; a tick for the live seq refetches.
	next, _ = m.Update(logReadMsg{seq: seq - 1, lines: nil})
	m = next.(model)
	if m.lg.shown != 2 {
		t.Errorf("stale read applied: shown=%d", m.lg.shown)
	}
	if _, cmd := m.Update(logTick{seq: seq}); cmd == nil {
		t.Error("a live tick should refetch")
	}
	if _, cmd := m.Update(logTick{seq: seq - 1}); cmd != nil {
		t.Error("a stale tick should do nothing")
	}
	m, _ = press(m, tea.KeyUp)
	if m.lg.follow {
		t.Error("Up should stop following")
	}
	m, _ = press(m, tea.KeyEnd)
	if !m.lg.follow {
		t.Error("End should follow again")
	}
	m, _ = press(m, tea.KeyEsc)
	if m.mode != modeBoard {
		t.Errorf("Esc should return to the board, mode=%d", m.mode)
	}
	// The level choice outlives the view.
	m, _ = press(m, tea.KeyCtrlL)
	m = typeText(m, "d")
	m, _ = press(m, tea.KeyEsc)
	m, _ = press(m, tea.KeyCtrlL)
	if m.lg.choice != logDebug {
		t.Errorf("choice should persist, got %d", m.lg.choice)
	}
	// readLogCmd reads the real file at the env path.
	os.WriteFile(filepath.Join(dir, "savannah.log"), []byte("time=t level=INFO msg=x\n"), 0o644)
	msg := readLogCmd(7)()
	if r, ok := msg.(logReadMsg); !ok || r.seq != 7 || len(r.lines) != 1 || r.err != "" {
		t.Errorf("readLogCmd = %+v", msg)
	}
}
