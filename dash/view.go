// view.go: the tmux-like full-screen worker view.
//
// Enter (with an empty prompt) or Tab on a board row opens a live view of
// that worker: transcript replayed, live turn followed, prompt line still
// active at the bottom. Left/Right flips between workers like tmux windows;
// Esc or Tab returns to the board.
//
// It rides the CLI's `task tail` seam. tail replays the transcript, follows
// a live turn until the turn ends, then exits, so the view runs it in a
// respawn loop: every run is a fresh full replay, which makes the loop
// self-healing after disconnects. To keep redraws flicker-free the display
// only swaps from the last completed run's buffer to the current run's once
// the new replay has caught up (the transcript is append-only, so the new
// run is a prefix-superset of the old in the steady state).
//
// Stated tradeoffs: content re-wraps on every append (fine at the 1 MiB
// buffer cap); wrapping counts runes, so double-width glyphs can wrap a
// column early; a vanished task keeps respawning a "no such task" replay
// until you Esc away. Push (song property fan-out) can replace the respawn
// loop underneath the same rendering later.

package main

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
)

const (
	modeBoard = iota
	modeView
)

const (
	maxTailBuf   = 1 << 20 // transcript bytes kept in memory per view
	respawnDelay = 700 * time.Millisecond
	viewChrome   = 3 // header + send + help lines around the viewport
)

// One tail subprocess = one "run", tagged with a sequence number. Bumping
// tailSeq orphans every in-flight message from older runs; the orphaned
// pump still drains its pipe to EOF and reaps the process.
type tailStartedMsg struct {
	seq  int
	r    io.ReadCloser
	proc *exec.Cmd
	err  string
}
type tailDataMsg struct {
	seq  int
	data []byte
	r    io.ReadCloser
	proc *exec.Cmd
}
type tailExitMsg struct{ seq int }
type tailRespawnMsg struct{ seq int }

// tailStartCmd spawns `savannah task tail <node> <id>` with stdout and
// stderr sharing ONE pipe, so TEXT and the [tool]/[result] lines interleave
// in write order, exactly as a terminal would show them.
func (m model) tailStartCmd(seq int, node, id string) tea.Cmd {
	bin := m.bin
	args := m.taskArgs("tail", node, id)
	return func() tea.Msg {
		c := exec.Command(bin, args...)
		r, w, err := os.Pipe()
		if err != nil {
			return tailStartedMsg{seq: seq, err: err.Error()}
		}
		c.Stdout = w
		c.Stderr = w
		if err := c.Start(); err != nil {
			r.Close()
			w.Close()
			return tailStartedMsg{seq: seq, err: err.Error()}
		}
		w.Close() // the child holds its own copies
		return tailStartedMsg{seq: seq, r: r, proc: c}
	}
}

// tailReadCmd pumps one read; Update re-issues it per data message, even for
// stale runs, so an orphaned pipe always drains to EOF, closes, and reaps.
func tailReadCmd(seq int, r io.ReadCloser, proc *exec.Cmd) tea.Cmd {
	return func() tea.Msg {
		buf := make([]byte, 8192)
		n, err := r.Read(buf)
		if n > 0 {
			return tailDataMsg{seq: seq, data: buf[:n], r: r, proc: proc}
		}
		if err != nil {
			r.Close()
			_ = proc.Wait()
			return tailExitMsg{seq: seq}
		}
		return tailDataMsg{seq: seq, r: r, proc: proc}
	}
}

func (m *model) enterView(node, id string) tea.Cmd {
	m.mode = modeView
	m.viewNode, m.viewID = node, id
	m.tailSeq++
	m.killTail()
	m.tailBuf, m.lastBuf = nil, nil
	m.follow = true
	m.vp = viewport.New(m.width, m.vpHeight())
	m.status = ""
	return m.tailStartCmd(m.tailSeq, node, id)
}

func (m *model) leaveView() {
	m.mode = modeBoard
	m.tailSeq++
	m.killTail()
	m.tailBuf, m.lastBuf = nil, nil
}

// killTail signals the current run's process; the pump sees EOF and cleans
// up. Safe to call with no run live.
func (m *model) killTail() {
	if m.tailProc != nil && m.tailProc.Process != nil {
		_ = m.tailProc.Process.Kill()
	}
	m.tailProc = nil
}

func (m model) vpHeight() int {
	h := m.height - viewChrome
	if h < 3 {
		h = 3
	}
	return h
}

// displayBuf picks what to render: the current run once it has caught up
// with the last completed one (the transcript is append-only, so the newer
// replay is a prefix-superset in the steady state), else the completed one.
func displayBuf(cur, last []byte) []byte {
	if len(last) > len(cur) {
		return last
	}
	return cur
}

// refreshViewport re-renders the display buffer into the viewport.
func (m *model) refreshViewport() {
	buf := displayBuf(m.tailBuf, m.lastBuf)
	m.vp.Width = m.width
	m.vp.Height = m.vpHeight()
	s := strings.ReplaceAll(string(buf), "\r", "")
	m.vp.SetContent(wrapTo(s, m.width))
	if m.follow {
		m.vp.GotoBottom()
	}
}

func (m *model) appendTail(data []byte) {
	m.tailBuf = append(m.tailBuf, data...)
	if len(m.tailBuf) > maxTailBuf {
		m.tailBuf = trimFront(m.tailBuf, maxTailBuf)
	}
}

func (m model) updateTail(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tailStartedMsg:
		if msg.seq != m.tailSeq {
			if msg.proc != nil && msg.proc.Process != nil {
				_ = msg.proc.Process.Kill()
				_ = msg.proc.Wait()
			}
			if msg.r != nil {
				msg.r.Close()
			}
			return m, nil
		}
		if msg.err != "" {
			m.tailBuf = append(m.tailBuf, []byte("tail: "+msg.err+"\n")...)
			m.refreshViewport()
			seq := msg.seq
			return m, tea.Tick(respawnDelay,
				func(time.Time) tea.Msg { return tailRespawnMsg{seq: seq} })
		}
		m.tailProc = msg.proc
		m.tailBuf = nil
		return m, tailReadCmd(msg.seq, msg.r, msg.proc)
	case tailDataMsg:
		if msg.seq == m.tailSeq {
			m.appendTail(msg.data)
			m.refreshViewport()
		}
		// Always keep the pump running so stale pipes drain and close.
		return m, tailReadCmd(msg.seq, msg.r, msg.proc)
	case tailExitMsg:
		if msg.seq != m.tailSeq {
			return m, nil
		}
		// A completed run replayed the whole transcript: authoritative.
		if len(m.tailBuf) > 0 {
			m.lastBuf = m.tailBuf
		}
		m.tailBuf = nil
		m.tailProc = nil
		m.refreshViewport()
		seq := msg.seq
		return m, tea.Tick(respawnDelay,
			func(time.Time) tea.Msg { return tailRespawnMsg{seq: seq} })
	case tailRespawnMsg:
		if msg.seq != m.tailSeq || m.mode != modeView {
			return m, nil
		}
		return m, m.tailStartCmd(msg.seq, m.viewNode, m.viewID)
	}
	return m, nil
}

func (m model) updateViewKeys(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.Type {
	case tea.KeyCtrlC:
		m.killTail()
		return m, tea.Quit
	case tea.KeyEsc, tea.KeyTab:
		m.leaveView()
		return m, nil
	case tea.KeyLeft, tea.KeyRight:
		if len(m.rows) == 0 {
			return m, nil
		}
		d := 1
		if k.Type == tea.KeyLeft {
			d = -1
		}
		i := neighborRow(m.rows, m.viewNode, m.viewID, d)
		r := m.rows[i]
		m.cursor = i // keep the board's selection in step
		if r.node == m.viewNode && r.t.ID == m.viewID {
			return m, nil
		}
		return m, m.enterView(r.node, r.t.ID)
	case tea.KeyUp:
		m.follow = false
		m.vp.ScrollUp(1)
		return m, nil
	case tea.KeyDown:
		m.vp.ScrollDown(1)
		if m.vp.AtBottom() {
			m.follow = true
		}
		return m, nil
	case tea.KeyPgUp:
		m.follow = false
		m.vp.PageUp()
		return m, nil
	case tea.KeyPgDown:
		m.vp.PageDown()
		if m.vp.AtBottom() {
			m.follow = true
		}
		return m, nil
	case tea.KeyHome:
		m.follow = false
		m.vp.GotoTop()
		return m, nil
	case tea.KeyEnd:
		m.follow = true
		m.vp.GotoBottom()
		return m, nil
	case tea.KeyEnter:
		text := strings.TrimSpace(m.input.Value())
		if text == "" {
			return m, nil
		}
		m.input.SetValue("")
		m.status = "sending to " + m.viewNode + "/" + m.viewID + "..."
		return m, m.sendCmd(m.viewNode, m.viewID, text)
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(k)
	return m, cmd
}

func (m model) viewView() string {
	state, title := "gone?", ""
	turns := 0
	for _, t := range m.tasks[m.viewNode] {
		if t.ID == m.viewID {
			state, title, turns = t.State, t.Title, t.Turns
			break
		}
	}
	target := m.viewNode + "/" + m.viewID

	var b strings.Builder
	fmt.Fprintf(&b, "%s   %s   turn %d   %s   %s\n",
		headerStyle.Render("savannah-dash  "+target),
		state, turns, trunc(title, 24), time.Now().Format("15:04:05"))
	b.WriteString(m.vp.View() + "\n")
	b.WriteString("send to " + headerStyle.Render(target) + ":  " +
		m.input.View() + "\n")
	help := helpStyle.Render(
		"Left/Right worker   Up/Down scroll   End follow   Esc board   Ctrl-C quit")
	if m.status != "" {
		help += "   " + m.status
	}
	b.WriteString(help)
	return b.String()
}

// neighborRow returns the index delta rows away from (node, id), wrapping
// around; if (node, id) is no longer a row it counts from the top.
func neighborRow(rows []row, node, id string, delta int) int {
	cur := 0
	for i, r := range rows {
		if r.node == node && r.t.ID == id {
			cur = i
			break
		}
	}
	n := len(rows)
	return ((cur+delta)%n + n) % n
}

// wrapTo hard-wraps every line of s at w runes so the viewport never clips
// output horizontally (rune-count wrapping, like the board's pad/trunc).
func wrapTo(s string, w int) string {
	if w <= 0 {
		return s
	}
	var b strings.Builder
	for i, line := range strings.Split(s, "\n") {
		if i > 0 {
			b.WriteByte('\n')
		}
		r := []rune(line)
		for len(r) > w {
			b.WriteString(string(r[:w]))
			b.WriteByte('\n')
			r = r[w:]
		}
		b.WriteString(string(r))
	}
	return b.String()
}

// trimFront keeps at most max trailing bytes, advanced to the next line
// start so the visible top is never a torn line. Copies so the oversized
// backing array is released.
func trimFront(b []byte, max int) []byte {
	if len(b) <= max {
		return b
	}
	b = b[len(b)-max:]
	if i := bytes.IndexByte(b, '\n'); i >= 0 && i+1 < len(b) {
		b = b[i+1:]
	}
	out := make([]byte, len(b))
	copy(out, b)
	return out
}
