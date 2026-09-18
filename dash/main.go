// savannah-dash: an interactive terminal dashboard for the task mesh.
//
// A thin Bubble Tea client over the savannah CLI, not a second implementation
// of song's wire protocol. With no node argument it runs in mesh mode:
// browses the mesh with `savannah ls` on a slow tick and discovers every node
// dynamically (systems appear and drop as they join and leave), polls each
// node's `task ls <node> --json` once a second, and renders all workers on one
// board. Enter runs `savannah task send <node> <id> <text>` for the
// highlighted worker. Give a node name to pin a single node instead (with
// optional --addr for a node not on mDNS).
//
// The board has two kinds of row: local Claude Code SESSIONS (your open tabs,
// reported by hooks/session-status.py, see sessions.go) and mesh WORKERS
// (savannah tasks). One cursor moves over both; the header counts everything
// that needs you (sessions waiting + pending mesh decisions) in one number.
//
// Keys: up/down select. On a SESSION row, Enter/Tab jumps to that terminal
// tab and Ctrl-X dismisses a stale entry. On a WORKER row, a typed prompt +
// Enter sends it, an empty Enter or Tab opens the tmux-like full-screen live
// view (see view.go; Left/Right flips workers, Up/Down scrolls, Esc back),
// and Ctrl-X removes a finished worker. Ctrl-C (or Esc on the board) quits.
// `--no-mesh` shows sessions only, needing no savannahd at all.
//
// Usage:
//
//	savannah-dash [--key FILE]                 # mesh mode: discover all nodes
//	savannah-dash <node> [--addr H:P] [--key FILE]  # pin one node
//
// The savannah binary is found via $SAVANNAH_BIN (default "savannah" on PATH).
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

type task struct {
	ID       string `json:"id"`
	Title    string `json:"title"`
	State    string `json:"state"`
	Turns    int    `json:"turns"`
	LastLine string `json:"last_line"`
	Worktree string `json:"worktree"`
}

// Board rows are either a mesh worker (a task on a node) or a local Claude
// Code session (an open tab, via the hook). One cursor moves over both;
// actions dispatch on kind.
const (
	rowTask = iota
	rowSession
)

type row struct {
	kind int
	node string  // mesh task: node name
	t    task    // valid when kind == rowTask
	s    session // valid when kind == rowSession
}

type nodesMsg struct {
	nodes []string
	err   string
}
type tasksMsg struct {
	node  string
	tasks []task
	err   string
}
type sentMsg struct {
	node, id string
	ok       bool
}
type rmMsg struct {
	node, id string
	ok       bool
}
type fetchTick time.Time
type discoverTick time.Time

type model struct {
	key  string
	bin  string
	pin  string // non-empty: single-node mode, skip discovery
	addr string // single-node --addr (a node off mDNS)

	noMesh bool // --no-mesh: skip discovery/fetch, sessions-only (work box)

	nodes    []string          // discovered node names, sorted
	tasks    map[string][]task // node -> its tasks
	errs     map[string]string // node -> last fetch error
	lsErr    string            // last discovery error
	sessions []session         // local Claude Code sessions (the hook)
	sessErr  string            // last session-read error

	rows   []row
	cursor int
	input  textinput.Model
	status string
	width  int
	height int

	// Full-screen worker view (view.go).
	mode     int // modeBoard | modeView
	viewNode string
	viewID   string
	vp       viewport.Model
	follow   bool // stick to the bottom as output arrives
	tailSeq  int  // orphans in-flight tail messages on retarget/leave
	tailProc *exec.Cmd
	tailBuf  []byte // the current tail run
	lastBuf  []byte // the last completed run (flicker-free fallback)
}

var (
	headerStyle   = lipgloss.NewStyle().Bold(true)
	colStyle      = lipgloss.NewStyle().Bold(true).Faint(true)
	selectedStyle = lipgloss.NewStyle().Reverse(true)
	helpStyle     = lipgloss.NewStyle().Faint(true)
	okStyle       = lipgloss.NewStyle().Foreground(lipgloss.Color("2"))
	warnStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("3"))
)

func cliError(err error) string {
	if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
		return strings.TrimSpace(string(ee.Stderr))
	}
	return err.Error()
}

// discoverCmd browses the mesh and returns the current node names. `savannah
// ls` prints "name host:port" per line; we take the name.
func (m model) discoverCmd() tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		out, err := exec.Command(bin, "ls", "--timeout-ms", "1500").Output()
		if err != nil {
			return nodesMsg{err: cliError(err)}
		}
		var nodes []string
		for _, ln := range strings.Split(string(out), "\n") {
			if f := strings.Fields(ln); len(f) >= 1 && f[0] != "" {
				nodes = append(nodes, f[0])
			}
		}
		sort.Strings(nodes)
		return nodesMsg{nodes: nodes}
	}
}

// taskArgs builds `task <sub> <node> ...` argv with the shared --addr/--key
// flags (used by the board's ls/send and the view's tail).
func (m model) taskArgs(sub, node string, rest ...string) []string {
	args := append([]string{"task", sub, node}, rest...)
	if m.pin != "" && m.addr != "" {
		args = append(args, "--addr", m.addr)
	}
	if m.key != "" {
		args = append(args, "--key", m.key)
	}
	return args
}

func (m model) fetchCmd(node string) tea.Cmd {
	bin := m.bin
	args := m.taskArgs("ls", node, "--json")
	return func() tea.Msg {
		out, err := exec.Command(bin, args...).Output()
		if err != nil {
			return tasksMsg{node: node, err: cliError(err)}
		}
		var ts []task
		if e := json.Unmarshal(out, &ts); e != nil {
			return tasksMsg{node: node, err: "bad json: " + e.Error()}
		}
		return tasksMsg{node: node, tasks: ts}
	}
}

func (m model) sendCmd(node, id, text string) tea.Cmd {
	bin := m.bin
	args := m.taskArgs("send", node, id, text)
	return func() tea.Msg {
		err := exec.Command(bin, args...).Run()
		return sentMsg{node: node, id: id, ok: err == nil}
	}
}

// rmCmd forgets a finished worker (`task rm`); the server refuses a running
// one, so a mistimed Ctrl-X is safe.
func (m model) rmCmd(node, id string) tea.Cmd {
	bin := m.bin
	args := m.taskArgs("rm", node, id)
	return func() tea.Msg {
		err := exec.Command(bin, args...).Run()
		return rmMsg{node: node, id: id, ok: err == nil}
	}
}

func fetchTickCmd() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg { return fetchTick(t) })
}
func discoverTickCmd() tea.Cmd {
	return tea.Tick(3*time.Second, func(t time.Time) tea.Msg { return discoverTick(t) })
}

// fetchAll fans out one task-list fetch per known node.
func (m model) fetchAll() tea.Cmd {
	var cmds []tea.Cmd
	for _, n := range m.nodes {
		cmds = append(cmds, m.fetchCmd(n))
	}
	return tea.Batch(cmds...)
}

func (m *model) rebuildRows() {
	var rows []row
	// Local sessions first (the tab-hell focus), then mesh workers.
	for _, s := range m.sessions {
		rows = append(rows, row{kind: rowSession, s: s})
	}
	for _, n := range m.nodes {
		for _, t := range m.tasks[n] {
			rows = append(rows, row{kind: rowTask, node: n, t: t})
		}
	}
	m.rows = rows
	if m.cursor >= len(rows) {
		m.cursor = len(rows) - 1
	}
	if m.cursor < 0 {
		m.cursor = 0
	}
}

// currentRow returns the highlighted row, ok=false if there is none.
func (m model) currentRow() (row, bool) {
	if m.cursor < 0 || m.cursor >= len(m.rows) {
		return row{}, false
	}
	return m.rows[m.cursor], true
}

// sectionCounts counts each kind for the section headers.
func (m model) sectionCounts() (sessions, tasks int) {
	for _, r := range m.rows {
		if r.kind == rowSession {
			sessions++
		} else {
			tasks++
		}
	}
	return
}

func (m model) Init() tea.Cmd {
	cmds := []tea.Cmd{textinput.Blink, fetchTickCmd(), readSessionsCmd()}
	if !m.noMesh {
		if m.pin == "" {
			cmds = append(cmds, m.discoverCmd(), discoverTickCmd())
		}
		cmds = append(cmds, m.fetchAll())
	}
	return tea.Batch(cmds...)
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tailStartedMsg, tailDataMsg, tailExitMsg, tailRespawnMsg:
		return m.updateTail(msg)
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		if m.mode == modeView {
			m.refreshViewport()
		}
	case discoverTick:
		return m, tea.Batch(m.discoverCmd(), discoverTickCmd())
	case fetchTick:
		// Sessions refresh every tick regardless of mesh; workers only when meshed.
		cmds := []tea.Cmd{readSessionsCmd(), fetchTickCmd()}
		if !m.noMesh {
			cmds = append(cmds, m.fetchAll())
		}
		return m, tea.Batch(cmds...)
	case sessionsMsg:
		if msg.err != "" {
			m.sessErr = msg.err
		} else {
			m.sessErr = ""
			m.sessions = msg.sessions
		}
		m.rebuildRows()
		return m, nil
	case jumpMsg:
		if msg.ok {
			m.status = okStyle.Render("jumped to " + msg.id + " (" + msg.how + ")")
		} else {
			m.status = warnStyle.Render("could not jump to " + msg.id +
				" (" + msg.how + ")")
		}
		return m, nil
	case dismissMsg:
		if msg.ok {
			m.status = okStyle.Render("dismissed session " + msg.id)
			return m, readSessionsCmd()
		}
		m.status = warnStyle.Render("could not dismiss " + msg.id)
		return m, nil
	case nodesMsg:
		if msg.err != "" {
			m.lsErr = msg.err
		} else {
			m.lsErr = ""
			m.nodes = msg.nodes
			// Drop tasks for nodes that left the mesh.
			live := map[string]bool{}
			for _, n := range msg.nodes {
				live[n] = true
			}
			for n := range m.tasks {
				if !live[n] {
					delete(m.tasks, n)
					delete(m.errs, n)
				}
			}
			m.rebuildRows()
		}
		return m, nil
	case tasksMsg:
		if msg.err != "" {
			m.errs[msg.node] = msg.err
		} else {
			delete(m.errs, msg.node)
			m.tasks[msg.node] = msg.tasks
		}
		m.rebuildRows()
		// A poll can flip the viewed task's decision banner on or off,
		// which changes the viewport height.
		if m.mode == modeView {
			m.refreshViewport()
		}
		return m, nil
	case sentMsg:
		if msg.ok {
			m.status = okStyle.Render("sent to " + msg.node + "/" + msg.id)
		} else {
			m.status = warnStyle.Render("not sent to " + msg.node + "/" + msg.id + " (busy or gone)")
		}
		return m, nil
	case rmMsg:
		if msg.ok {
			m.status = okStyle.Render("removed " + msg.node + "/" + msg.id)
			// Drop the row now instead of waiting out the poll tick.
			return m, m.fetchCmd(msg.node)
		}
		m.status = warnStyle.Render("not removed " + msg.node + "/" + msg.id +
			" (running? cancel first)")
		return m, nil
	case tea.KeyMsg:
		if m.mode == modeView {
			return m.updateViewKeys(msg)
		}
		switch msg.Type {
		case tea.KeyCtrlC, tea.KeyEsc:
			return m, tea.Quit
		case tea.KeyUp:
			if m.cursor > 0 {
				m.cursor--
			}
			return m, nil
		case tea.KeyDown:
			if m.cursor < len(m.rows)-1 {
				m.cursor++
			}
			return m, nil
		case tea.KeyTab:
			if r, ok := m.currentRow(); ok {
				if r.kind == rowSession {
					return m, jumpSessionCmd(r.s)
				}
				return m, m.enterView(r.node, r.t.ID)
			}
			return m, nil
		case tea.KeyCtrlX:
			if r, ok := m.currentRow(); ok {
				if r.kind == rowSession {
					m.status = "dismissing session " + r.s.ID + "..."
					return m, dismissSessionCmd(r.s)
				}
				if r.t.State == "running" {
					m.status = warnStyle.Render(r.node + "/" + r.t.ID +
						" is running (cancel first)")
					return m, nil
				}
				m.status = "removing " + r.node + "/" + r.t.ID + "..."
				return m, m.rmCmd(r.node, r.t.ID)
			}
			return m, nil
		case tea.KeyEnter:
			text := strings.TrimSpace(m.input.Value())
			r, ok := m.currentRow()
			if !ok {
				return m, nil
			}
			if r.kind == rowSession {
				// A local session is a person's tab: jump to it, never send.
				if text != "" {
					m.status = warnStyle.Render(
						"cannot send to an interactive session; jumping instead")
				}
				return m, jumpSessionCmd(r.s)
			}
			if text == "" {
				// Empty prompt: open the full-screen view instead.
				return m, m.enterView(r.node, r.t.ID)
			}
			m.input.SetValue("")
			m.status = "sending to " + r.node + "/" + r.t.ID + "..."
			return m, m.sendCmd(r.node, r.t.ID, text)
		}
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	return m, cmd
}

func flatten(s string) string {
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\r' || r == '\t' {
			return ' '
		}
		return r
	}, s)
}

func pad(s string, w int) string {
	r := []rune(flatten(s))
	if len(r) > w {
		r = r[:w]
	}
	return string(r) + strings.Repeat(" ", w-len(r)) + "  "
}

func trunc(s string, w int) string {
	r := []rune(flatten(s))
	if len(r) > w {
		return string(r[:w])
	}
	return string(r)
}

func (m model) View() string {
	if m.mode == modeView {
		return m.viewView()
	}
	var b strings.Builder
	nSess, nTask := m.sectionCounts()

	scope := "mesh"
	if m.noMesh {
		scope = "sessions"
	} else if m.pin != "" {
		scope = m.pin
	}
	// One attention number over both worlds: sessions needing you + pending
	// mesh decisions. This is the whole point: one glance, total.
	attention := countSessionAttention(m.sessions) + countDecisions(m.rows)
	deco := ""
	if attention > 0 {
		deco = "   " + warnStyle.Render(fmt.Sprintf("%d need you", attention))
	}
	fmt.Fprintf(&b, "%s   %d session%s   %d worker%s   %s%s\n\n",
		headerStyle.Render("savannah-dash  "+scope),
		nSess, plural(nSess), nTask, plural(nTask),
		time.Now().Format("15:04:05"), deco)

	avail := m.width - (14 + 2) - (7 + 2) - (11 + 2) - (4 + 2) - (20 + 2)
	if avail < 12 {
		avail = 12
	}
	now := nowUnix()

	// ---- LOCAL SESSIONS (open Claude Code tabs, via the hook) ----
	if nSess > 0 {
		b.WriteString(headerStyle.Render("LOCAL SESSIONS") + "\n")
		b.WriteString(colStyle.Render(
			pad("", 2)+pad("STATE", 9)+pad("PROJECT", 26)+pad("AGE", 5)+
				"WHAT") + "\n")
		for i, r := range m.rows {
			if r.kind != rowSession {
				continue
			}
			s := r.s
			line := pad(stateGlyph(s.State), 2) + pad(s.State, 9) +
				pad(projectLabel(s.Project), 26) +
				pad(humanAge(now-s.Updated), 5) + trunc(sessionDetail(s), avail)
			switch {
			case i == m.cursor:
				line = selectedStyle.Render(line)
			case sessionAttention(s):
				line = warnStyle.Render(line)
			case s.State == "done":
				line = okStyle.Render(line)
			case s.State == "idle":
				line = helpStyle.Render(line)
			}
			b.WriteString(line + "\n")
		}
		if m.sessErr != "" {
			b.WriteString(warnStyle.Render("  sessions: "+m.sessErr) + "\n")
		}
		b.WriteString("\n")
	}

	// ---- MESH WORKERS (savannah tasks) ----
	if !m.noMesh {
		b.WriteString(headerStyle.Render("MESH WORKERS") + "\n")
		if m.lsErr != "" {
			b.WriteString(warnStyle.Render("  discovery: "+m.lsErr) + "\n")
		}
		if nTask == 0 {
			if m.pin == "" && len(m.nodes) == 0 {
				b.WriteString(helpStyle.Render("  discovering nodes...") + "\n")
			} else {
				b.WriteString(helpStyle.Render("  (no workers yet)") + "\n")
			}
		} else {
			b.WriteString(colStyle.Render(
				pad("NODE", 14)+pad("ID", 7)+pad("STATE", 11)+pad("TURN", 4)+
					pad("TITLE", 20)+"LAST LINE") + "\n")
			for i, r := range m.rows {
				if r.kind != rowTask {
					continue
				}
				last := r.t.LastLine
				d, pending := pendingDecision(r.t)
				if pending {
					last = decisionCell(d)
				}
				line := pad(r.node, 14) + pad(r.t.ID, 7) + pad(r.t.State, 11) +
					pad(fmt.Sprintf("%d", r.t.Turns), 4) + pad(r.t.Title, 20) +
					trunc(last, avail)
				switch {
				case i == m.cursor:
					line = selectedStyle.Render(line)
				case pending:
					line = warnStyle.Render(line)
				}
				b.WriteString(line + "\n")
			}
		}
	}

	// Selection line adapts to what is highlighted.
	r, ok := m.currentRow()
	switch {
	case !ok:
		b.WriteString("\n" + helpStyle.Render("no rows") + "\n\n")
	case r.kind == rowSession:
		b.WriteString("\n" + headerStyle.Render(projectLabel(r.s.Project)) +
			"   " + helpStyle.Render("Enter jumps to its tab") + "\n\n")
	default:
		b.WriteString("\nsend to " + headerStyle.Render(r.node+"/"+r.t.ID) +
			":  " + m.input.View() + "\n\n")
	}

	help := helpStyle.Render(
		"up/down select   Enter jump/send   Tab view   Ctrl-X rm/dismiss   Ctrl-C quit")
	if m.status != "" {
		help += "   " + m.status
	}
	b.WriteString(help + "\n")
	return b.String()
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

func main() {
	pin, key, addr := "", "", ""
	noMesh := false
	args := os.Args[1:]
	// A leading non-flag positional pins a single node.
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		pin = args[0]
		args = args[1:]
	}
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--key":
			if i+1 < len(args) {
				key = args[i+1]
				i++
			}
		case "--addr":
			if i+1 < len(args) {
				addr = args[i+1]
				i++
			}
		case "--no-mesh", "--sessions":
			noMesh = true
		case "-h", "--help":
			fmt.Fprintln(os.Stderr, "usage: savannah-dash [<node>] [--addr H:P] [--key FILE] [--no-mesh]")
			fmt.Fprintln(os.Stderr, "  no node:   mesh mode, discovers every node via `savannah ls`")
			fmt.Fprintln(os.Stderr, "  <node>:    pin one node (use --addr for a node not on mDNS)")
			fmt.Fprintln(os.Stderr, "  --no-mesh: sessions only, no savannahd needed (a local tab monitor)")
			fmt.Fprintln(os.Stderr, "Local Claude Code sessions always show (via the session-status hook).")
			os.Exit(2)
		}
	}
	bin := os.Getenv("SAVANNAH_BIN")
	if bin == "" {
		bin = "savannah"
	}

	ti := textinput.New()
	ti.Placeholder = "prompt for the selected worker..."
	ti.Focus()
	ti.CharLimit = 4000
	ti.Width = 64

	m := model{
		key: key, bin: bin, pin: pin, addr: addr, noMesh: noMesh,
		tasks: map[string][]task{}, errs: map[string]string{},
		input: ti,
	}
	if pin != "" && !noMesh {
		m.nodes = []string{pin}
	}
	p := tea.NewProgram(m, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "savannah-dash:", err)
		os.Exit(1)
	}
}
