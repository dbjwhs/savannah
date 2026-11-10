package main

import (
	"path/filepath"
	"strings"
	"testing"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

func TestParseFade(t *testing.T) {
	for _, c := range []struct {
		in   string
		want time.Duration
		bad  bool
	}{
		{"", fadeDefault, false},
		{"off", 0, false},
		{"0", 0, false},
		{"3h", 3 * time.Hour, false},
		{"90m", 90 * time.Minute, false},
		{"1h30m", 90 * time.Minute, false},
		{"8", 8 * time.Hour, false},
		{"1.5", 90 * time.Minute, false},
		{"-1h", 0, true},
		{"soon", 0, true},
	} {
		got, err := parseFade(c.in)
		if (err != nil) != c.bad || got != c.want {
			t.Errorf("parseFade(%q) = %v, %v; want %v, bad=%v", c.in, got, err, c.want, c.bad)
		}
	}
	for d, want := range map[time.Duration]string{0: "off", time.Hour: "1h", 3 * time.Hour: "3h", 90 * time.Minute: "1h30m0s"} {
		if got := fadeLabel(d); got != want {
			t.Errorf("fadeLabel(%v) = %q, want %q", d, got, want)
		}
	}
	// The cycle: 1h, 3h, 8h, 24h, off, and back to 1h; a value off the
	// list goes to the next step above it.
	want := []time.Duration{3 * time.Hour, 8 * time.Hour, 24 * time.Hour, 0, time.Hour}
	d := time.Hour
	for i, w := range want {
		d = nextFade(d)
		if d != w {
			t.Fatalf("step %d: %v, want %v", i, d, w)
		}
	}
	if got := nextFade(90 * time.Minute); got != 3*time.Hour {
		t.Errorf("nextFade(90m) = %v, want 3h", got)
	}
	if got := nextFade(30 * time.Hour); got != 0 {
		t.Errorf("nextFade(30h) = %v, want off", got)
	}
}

func TestFadedRows(t *testing.T) {
	now := nowUnix()
	old := now - int64(4*time.Hour/time.Second)
	for _, c := range []struct {
		name string
		s    session
		fade time.Duration
		want bool
	}{
		{"idle and old", session{State: "idle", Updated: old}, 3 * time.Hour, true},
		{"done and old", session{State: "done", Updated: old}, 3 * time.Hour, true},
		{"offline and old", session{State: "offline", Updated: old}, 3 * time.Hour, true},
		{"idle and fresh", session{State: "idle", Updated: now - 60}, 3 * time.Hour, false},
		{"old but working", session{State: "working", Updated: old}, 3 * time.Hour, false},
		{"old but waiting", session{State: "waiting", Updated: old}, 3 * time.Hour, false},
		{"no stamp", session{State: "idle"}, 3 * time.Hour, false},
		{"fade off", session{State: "idle", Updated: old}, 0, false},
		{"longer fade", session{State: "idle", Updated: old}, 8 * time.Hour, false},
	} {
		if got := faded(c.s, now, c.fade); got != c.want {
			t.Errorf("%s: faded = %v, want %v", c.name, got, c.want)
		}
	}
	// The faded style is the state colour, faint; an unfaded row is the
	// plain state colour (the test terminal has no colours, so the style's
	// own flags are checked, not the rendered bytes).
	s := session{State: "idle", Updated: old}
	if st := rowStyleAt(s, now, 3*time.Hour); !st.GetFaint() || st.GetForeground() != okStyle.GetForeground() {
		t.Errorf("faded idle row should be faint green: faint=%v fg=%v", st.GetFaint(), st.GetForeground())
	}
	if st := rowStyleAt(s, now, 0); st.GetFaint() || st.GetForeground() != okStyle.GetForeground() {
		t.Errorf("unfaded idle row should be plain green: faint=%v", st.GetFaint())
	}
	if st := rowStyleAt(session{State: "working", Updated: old}, now, time.Hour); st.GetFaint() {
		t.Error("an old working row must not fade")
	}
}

// TestCtrlFCyclesAndRemembers: Ctrl-F moves to the next step, says so, and
// writes the board file; a new board reads it back; --fade wins for a run.
func TestCtrlFCyclesAndRemembers(t *testing.T) {
	file := filepath.Join(t.TempDir(), "board.json")
	t.Setenv("SAVANNAH_BOARD_FILE", file)
	m := boardWith(t, true)
	if m.fade != fadeDefault {
		t.Fatalf("a fresh board fades at %v, want %v", m.fade, fadeDefault)
	}
	m, cmd := press(m, tea.KeyCtrlF)
	if cmd != nil || m.fade != 8*time.Hour || !strings.Contains(m.status, "over 8h are dimmed") || !strings.Contains(m.status, "Ctrl-F: 24h") {
		t.Fatalf("Ctrl-F from 3h: fade=%v status=%q", m.fade, m.status)
	}
	if p := loadBoardPrefs(file); p.Fade != "8h" {
		t.Errorf("board file after Ctrl-F: %+v, want fade 8h", p)
	}
	m, _ = press(m, tea.KeyCtrlF)
	m, _ = press(m, tea.KeyCtrlF)
	if m.fade != 0 || !strings.Contains(m.status, "no row is dimmed") {
		t.Fatalf("two more Ctrl-F: fade=%v status=%q", m.fade, m.status)
	}
	if p := loadBoardPrefs(file); p.Fade != "off" {
		t.Errorf("board file after off: %+v", p)
	}
	// The file is read on start; a flag beats it; a bad flag is refused.
	if o, err := parseArgs([]string{"--fade", "90m"}); err != nil || o.fade != "90m" {
		t.Errorf("--fade 90m: %+v %v", o, err)
	}
	if _, err := parseArgs([]string{"--fade", "soon"}); err == nil {
		t.Error("--fade soon should be refused")
	}
	if _, err := parseArgs([]string{"--fade"}); err == nil {
		t.Error("--fade without a value should be refused")
	}
	if m2 := newModel(opts{fade: "off"}, "savannah"); m2.fade != 0 {
		t.Errorf("newModel with fade off: %v", m2.fade)
	}
	if m2 := newModel(opts{fade: "bad"}, "savannah"); m2.fade != fadeDefault {
		t.Errorf("newModel with a bad fade falls back to the default: %v", m2.fade)
	}
}
