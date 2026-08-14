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
// Keys: up/down select a row, type a prompt, Enter sends it to the selected
// worker, Ctrl-C or Esc quits.
//
// Usage:
//   savannah-dash [--key FILE]                 # mesh mode: discover all nodes
//   savannah-dash <node> [--addr H:P] [--key FILE]  # pin one node
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

// One display line: a task and the node it lives on.
type row struct {
	node string
	t    task
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
type fetchTick time.Time
type discoverTick time.Time

type model struct {
	key  string
	bin  string
	pin  string // non-empty: single-node mode, skip discovery
	addr string // single-node --addr (a node off mDNS)

	nodes []string          // discovered node names, sorted
	tasks map[string][]task // node -> its tasks
	errs  map[string]string // node -> last fetch error
	lsErr string            // last discovery error

	rows   []row
	cursor int
	input  textinput.Model
	status string
	width  int
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

func (m model) fetchCmd(node string) tea.Cmd {
	bin := m.bin
	args := []string{"task", "ls", node, "--json"}
	if m.pin != "" && m.addr != "" {
		args = append(args, "--addr", m.addr)
	}
	if m.key != "" {
		args = append(args, "--key", m.key)
	}
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
	args := []string{"task", "send", node, id, text}
	if m.pin != "" && m.addr != "" {
		args = append(args, "--addr", m.addr)
	}
	if m.key != "" {
		args = append(args, "--key", m.key)
	}
	return func() tea.Msg {
		err := exec.Command(bin, args...).Run()
		return sentMsg{node: node, id: id, ok: err == nil}
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
	for _, n := range m.nodes {
		for _, t := range m.tasks[n] {
			rows = append(rows, row{node: n, t: t})
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

func (m model) Init() tea.Cmd {
	cmds := []tea.Cmd{textinput.Blink, fetchTickCmd()}
	if m.pin == "" {
		cmds = append(cmds, m.discoverCmd(), discoverTickCmd())
	}
	cmds = append(cmds, m.fetchAll())
	return tea.Batch(cmds...)
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
	case discoverTick:
		return m, tea.Batch(m.discoverCmd(), discoverTickCmd())
	case fetchTick:
		return m, tea.Batch(m.fetchAll(), fetchTickCmd())
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
		return m, nil
	case sentMsg:
		if msg.ok {
			m.status = okStyle.Render("sent to " + msg.node + "/" + msg.id)
		} else {
			m.status = warnStyle.Render("not sent to " + msg.node + "/" + msg.id + " (busy or gone)")
		}
		return m, nil
	case tea.KeyMsg:
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
		case tea.KeyEnter:
			text := strings.TrimSpace(m.input.Value())
			if text != "" && m.cursor >= 0 && m.cursor < len(m.rows) {
				r := m.rows[m.cursor]
				m.input.SetValue("")
				m.status = "sending to " + r.node + "/" + r.t.ID + "..."
				return m, m.sendCmd(r.node, r.t.ID, text)
			}
			return m, nil
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
	var b strings.Builder

	scope := "mesh"
	if m.pin != "" {
		scope = m.pin
	}
	fmt.Fprintf(&b, "%s   %d node%s   %d task%s   %s\n\n",
		headerStyle.Render("savannah-dash  "+scope),
		len(m.nodes), plural(len(m.nodes)), len(m.rows), plural(len(m.rows)),
		time.Now().Format("15:04:05"))

	if m.lsErr != "" {
		b.WriteString(warnStyle.Render("  discovery: "+m.lsErr) + "\n")
	}

	// NODE(14) ID(7) STATE(11) TURN(4) TITLE(20) then LAST LINE.
	avail := m.width - (14 + 2) - (7 + 2) - (11 + 2) - (4 + 2) - (20 + 2)
	if avail < 12 {
		avail = 12
	}

	if len(m.rows) == 0 {
		if m.pin == "" && len(m.nodes) == 0 {
			b.WriteString("  discovering nodes on the mesh...\n")
		} else {
			b.WriteString("  (no tasks yet)\n")
		}
	} else {
		b.WriteString(colStyle.Render(
			pad("NODE", 14)+pad("ID", 7)+pad("STATE", 11)+pad("TURN", 4)+
				pad("TITLE", 20)+"LAST LINE") + "\n")
		for i, r := range m.rows {
			line := pad(r.node, 14) + pad(r.t.ID, 7) + pad(r.t.State, 11) +
				pad(fmt.Sprintf("%d", r.t.Turns), 4) + pad(r.t.Title, 20) +
				trunc(r.t.LastLine, avail)
			if i == m.cursor {
				line = selectedStyle.Render(line)
			}
			b.WriteString(line + "\n")
		}
	}

	sel := "-"
	if m.cursor >= 0 && m.cursor < len(m.rows) {
		r := m.rows[m.cursor]
		sel = r.node + "/" + r.t.ID
	}
	b.WriteString("\nsend to " + headerStyle.Render(sel) + ":  " + m.input.View() + "\n\n")
	help := helpStyle.Render("up/down select   type a prompt + Enter to send   Ctrl-C quit")
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
		case "-h", "--help":
			fmt.Fprintln(os.Stderr, "usage: savannah-dash [<node>] [--addr H:P] [--key FILE]")
			fmt.Fprintln(os.Stderr, "  no node: mesh mode, discovers every node via `savannah ls`")
			fmt.Fprintln(os.Stderr, "  <node>:  pin one node (use --addr for a node not on mDNS)")
			os.Exit(2)
		}
	}
	bin := os.Getenv("SAVANNAH_BIN")
	if bin == "" {
		bin = "savannah"
	}

	ti := textinput.New()
	ti.Placeholder = "prompt for the selected task..."
	ti.Focus()
	ti.CharLimit = 4000
	ti.Width = 64

	m := model{
		key: key, bin: bin, pin: pin, addr: addr,
		tasks: map[string][]task{}, errs: map[string]string{},
		input: ti,
	}
	if pin != "" {
		m.nodes = []string{pin}
	}
	p := tea.NewProgram(m, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "savannah-dash:", err)
		os.Exit(1)
	}
}
