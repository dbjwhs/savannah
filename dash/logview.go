// logview.go: the Ctrl-L screen, savannah's log read back at a level.
//
// The file is always verbose (logging.go); this view is the filter the
// owner asked for: normal (INFO and above), debug (adds DEBUG), verbose
// (every line). It reads the tail of the current file itself, no
// subprocess, every two seconds while open, and follows the end unless you
// scroll up. Lines are shown as written, one per record, so the time, level
// and src stamps stay visible. `savannah work log` is the same reader for
// the shell.
//
// Stated limits: only the current file (the rotated ones are a `less`
// away); the tail is the last 512 KiB; a line without a level stamp (a
// stray write by something else) passes every filter.

package main

import (
	"fmt"
	"io"
	"log/slog"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
)

const (
	logNormal  = iota // INFO and above
	logDebug          // DEBUG and above
	logVerbose        // everything
)

var (
	logChoiceNames  = []string{"normal", "debug", "verbose"}
	logChoiceLevels = []slog.Level{slog.LevelInfo, slog.LevelDebug, LevelVerbose}
	levelRe         = regexp.MustCompile(`(?:^|\s)level=([A-Za-z]+)`)
)

const (
	logTailBytes = 512 << 10
	logRefresh   = 2 * time.Second
	logChrome    = 2 // header + help lines around the viewport
)

type logState struct {
	choice int // logNormal | logDebug | logVerbose
	seq    int // bumped on enter and leave; stale reads and ticks are dropped
	follow bool
	all    []string // the tail as read, every level
	shown  int
	err    string
}

type logReadMsg struct {
	seq   int
	lines []string
	err   string
}
type logTick struct{ seq int }

// readLogTail returns the last lines of the file within maxBytes, the torn
// first line dropped. A missing file is the caller's "no log yet".
func readLogTail(path string, maxBytes int) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil, err
	}
	off := int64(0)
	if st.Size() > int64(maxBytes) {
		off = st.Size() - int64(maxBytes)
	}
	b := make([]byte, st.Size()-off)
	if _, err := f.ReadAt(b, off); err != nil && err != io.EOF {
		return nil, err
	}
	s := string(b)
	if off > 0 {
		if i := strings.IndexByte(s, '\n'); i >= 0 {
			s = s[i+1:]
		}
	}
	s = strings.TrimRight(s, "\n")
	if s == "" {
		return nil, nil
	}
	return strings.Split(s, "\n"), nil
}

// lineLevel reads the level stamp of one line; ok is false without one.
func lineLevel(line string) (slog.Level, bool) {
	m := levelRe.FindStringSubmatch(line)
	if m == nil {
		return 0, false
	}
	return levelFromName(m[1])
}

// filterLog keeps the lines at or above min, and the unstamped ones.
func filterLog(lines []string, min slog.Level) []string {
	var out []string
	for _, ln := range lines {
		if l, ok := lineLevel(ln); !ok || l >= min {
			out = append(out, ln)
		}
	}
	return out
}

func readLogCmd(seq int) tea.Cmd {
	path := logPath()
	return func() tea.Msg {
		lines, err := readLogTail(path, logTailBytes)
		if err != nil {
			if os.IsNotExist(err) {
				return logReadMsg{seq: seq, err: "no log yet at " + path}
			}
			return logReadMsg{seq: seq, err: err.Error()}
		}
		return logReadMsg{seq: seq, lines: lines}
	}
}

func logTickCmd(seq int) tea.Cmd {
	return tea.Tick(logRefresh, func(time.Time) tea.Msg { return logTick{seq: seq} })
}

func (m *model) enterLog() tea.Cmd {
	m.mode = modeLog
	m.lg.seq++
	m.lg.follow, m.lg.err = true, ""
	m.vp = viewport.New(m.width, m.logHeight())
	m.refreshLog()
	m.status = ""
	slog.Debug("log view opened", "level", logChoiceNames[m.lg.choice])
	return tea.Batch(readLogCmd(m.lg.seq), logTickCmd(m.lg.seq))
}

func (m *model) leaveLog() {
	m.mode = modeBoard
	m.lg.seq++
}

func (m model) logHeight() int {
	h := m.height - logChrome
	if h < 3 {
		h = 3
	}
	return h
}

// refreshLog re-renders the tail at the chosen level.
func (m *model) refreshLog() {
	lines := filterLog(m.lg.all, logChoiceLevels[m.lg.choice])
	m.lg.shown = len(lines)
	m.vp.Width, m.vp.Height = m.width, m.logHeight()
	text := strings.Join(lines, "\n")
	switch {
	case m.lg.err != "":
		text = m.lg.err
	case len(lines) == 0:
		text = "(nothing at this level yet)"
	}
	m.vp.SetContent(wrapTo(text, m.width))
	if m.lg.follow {
		m.vp.GotoBottom()
	}
}

func (m model) updateLog(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case logReadMsg:
		if msg.seq == m.lg.seq && m.mode == modeLog {
			m.lg.all, m.lg.err = msg.lines, msg.err
			m.refreshLog()
		}
	case logTick:
		if msg.seq == m.lg.seq && m.mode == modeLog {
			return m, tea.Batch(readLogCmd(msg.seq), logTickCmd(msg.seq))
		}
	}
	return m, nil
}

// setLogChoice picks a level and re-renders; the choice outlives the view,
// so the next Ctrl-L opens where you left it.
func (m *model) setLogChoice(c int) {
	m.lg.choice = ((c % 3) + 3) % 3
	m.refreshLog()
}

func (m model) updateLogKeys(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc, tea.KeyTab:
		m.leaveLog()
		return m, nil
	case tea.KeyLeft:
		m.setLogChoice(m.lg.choice - 1)
		return m, nil
	case tea.KeyRight:
		m.setLogChoice(m.lg.choice + 1)
		return m, nil
	case tea.KeyUp:
		m.lg.follow = false
		m.vp.ScrollUp(1)
	case tea.KeyDown:
		m.vp.ScrollDown(1)
		if m.vp.AtBottom() {
			m.lg.follow = true
		}
	case tea.KeyPgUp:
		m.lg.follow = false
		m.vp.PageUp()
	case tea.KeyPgDown:
		m.vp.PageDown()
		if m.vp.AtBottom() {
			m.lg.follow = true
		}
	case tea.KeyHome:
		m.lg.follow = false
		m.vp.GotoTop()
	case tea.KeyEnd:
		m.lg.follow = true
		m.vp.GotoBottom()
	case tea.KeyRunes:
		switch k.String() {
		case "n", "1":
			m.setLogChoice(logNormal)
		case "d", "2":
			m.setLogChoice(logDebug)
		case "v", "3":
			m.setLogChoice(logVerbose)
		}
	}
	return m, nil
}

func (m model) viewLog() string {
	var b strings.Builder
	// One header line whatever the path's length: the level and the counts
	// come first, the path takes what is left.
	title := "savannah  log"
	rest := fmt.Sprintf("   level %s   %d of %d lines   %s   %s", logChoiceNames[m.lg.choice],
		m.lg.shown, len(m.lg.all), time.Now().Format("15:04:05"), tilde(logPath(), m.home))
	b.WriteString(headerStyle.Render(title) + fit(rest, m.width-len(title)) + "\n")
	b.WriteString(m.vp.View() + "\n")
	b.WriteString(helpStyle.Render(trunc(
		"n normal  d debug  v verbose  Left/Right cycle   Up/Down scroll   End follow   Esc back", m.width)))
	return b.String()
}
