// savannah-dash: an interactive terminal dashboard for the task mesh.
//
// A thin Bubble Tea client over the savannah CLI's JSON seam: it polls
// `savannah task ls <node> --json` once a second to render a live table, and
// on Enter runs `savannah task send <node> <id> <text>` for the highlighted
// task. It never speaks song's wire protocol itself, mirroring how the Python
// MCP shim shells out to the CLI.
//
// Keys: up/down select a row, type a prompt into the field, Enter sends it to
// the selected task, Ctrl-C or Esc quits.
//
// Usage: savannah-dash <node> [--key FILE]
// The savannah binary is found via $SAVANNAH_BIN (default "savannah" on PATH).
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
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

type tasksMsg struct {
	tasks []task
	err   string
}
type sentMsg struct {
	id string
	ok bool
}
type tickMsg time.Time

type model struct {
	node   string
	key    string
	addr   string
	bin    string
	tasks  []task
	cursor int
	input  textinput.Model
	status string
	err    string
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

// cliArgs builds a savannah CLI invocation, appending --addr/--key when set.
// --addr targets a node by address (e.g. a loopback node not on mDNS); without
// it the CLI resolves the node name over the mesh.
func (m model) cliArgs(args ...string) []string {
	if m.addr != "" {
		args = append(args, "--addr", m.addr)
	}
	if m.key != "" {
		args = append(args, "--key", m.key)
	}
	return args
}

func (m model) fetchCmd() tea.Cmd {
	bin, args := m.bin, m.cliArgs("task", "ls", m.node, "--json")
	return func() tea.Msg {
		out, err := exec.Command(bin, args...).Output()
		if err != nil {
			return tasksMsg{err: cliError(err)}
		}
		var ts []task
		if e := json.Unmarshal(out, &ts); e != nil {
			return tasksMsg{err: "bad json from CLI: " + e.Error()}
		}
		return tasksMsg{tasks: ts}
	}
}

func (m model) sendCmd(id, text string) tea.Cmd {
	bin, args := m.bin, m.cliArgs("task", "send", m.node, id, text)
	return func() tea.Msg {
		err := exec.Command(bin, args...).Run()
		return sentMsg{id: id, ok: err == nil}
	}
}

func tickCmd() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg { return tickMsg(t) })
}

func cliError(err error) string {
	if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
		return strings.TrimSpace(string(ee.Stderr))
	}
	return err.Error()
}

func (m model) Init() tea.Cmd {
	return tea.Batch(textinput.Blink, m.fetchCmd(), tickCmd())
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
	case tickMsg:
		return m, tea.Batch(m.fetchCmd(), tickCmd())
	case tasksMsg:
		if msg.err != "" {
			m.err = msg.err
		} else {
			m.err = ""
			m.tasks = msg.tasks
			if m.cursor >= len(m.tasks) {
				m.cursor = len(m.tasks) - 1
			}
			if m.cursor < 0 {
				m.cursor = 0
			}
		}
		return m, nil
	case sentMsg:
		if msg.ok {
			m.status = okStyle.Render("sent to " + msg.id)
		} else {
			m.status = warnStyle.Render("not sent to " + msg.id + " (busy or gone)")
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
			if m.cursor < len(m.tasks)-1 {
				m.cursor++
			}
			return m, nil
		case tea.KeyEnter:
			text := strings.TrimSpace(m.input.Value())
			if text != "" && len(m.tasks) > 0 {
				id := m.tasks[m.cursor].ID
				m.input.SetValue("")
				m.status = "sending to " + id + "..."
				return m, m.sendCmd(id, text)
			}
			return m, nil
		}
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	return m, cmd
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

func flatten(s string) string {
	return strings.Map(func(r rune) rune {
		if r == '\n' || r == '\r' || r == '\t' {
			return ' '
		}
		return r
	}, s)
}

func (m model) selectedID() string {
	if len(m.tasks) == 0 || m.cursor < 0 || m.cursor >= len(m.tasks) {
		return "-"
	}
	return m.tasks[m.cursor].ID
}

func (m model) View() string {
	var b strings.Builder

	fmt.Fprintf(&b, "%s   %d task%s   %s\n\n",
		headerStyle.Render("savannah-dash  "+m.node),
		len(m.tasks), plural(len(m.tasks)), time.Now().Format("15:04:05"))

	// remaining width for the LAST LINE column
	avail := m.width - (7 + 2) - (11 + 2) - (5 + 2) - (24 + 2)
	if avail < 12 {
		avail = 12
	}

	if m.err != "" {
		b.WriteString(warnStyle.Render("  "+m.err) + "\n\n")
	}

	if len(m.tasks) == 0 {
		b.WriteString("  (no tasks yet)\n")
	} else {
		b.WriteString(colStyle.Render(
			pad("ID", 7)+pad("STATE", 11)+pad("TURN", 5)+pad("TITLE", 24)+"LAST LINE") + "\n")
		for i, t := range m.tasks {
			row := pad(t.ID, 7) + pad(t.State, 11) +
				pad(fmt.Sprintf("%d", t.Turns), 5) + pad(t.Title, 24) +
				trunc(t.LastLine, avail)
			if i == m.cursor {
				row = selectedStyle.Render(row)
			}
			b.WriteString(row + "\n")
		}
	}

	b.WriteString("\nsend to " + headerStyle.Render(m.selectedID()) + ":  " + m.input.View() + "\n\n")
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
	if len(os.Args) > 1 && os.Args[1] == "--dump" {
		ti := textinput.New()
		ti.Focus()
		m := model{node: "demo", width: 120, input: ti, tasks: []task{
			{ID: "t-0001", Title: "add auth tests", State: "idle", Turns: 1, LastLine: "did the thing"},
			{ID: "t-0002", Title: "fix parser", State: "running", Turns: 3, LastLine: "building..."},
		}}
		fmt.Print(m.View())
		fmt.Println("---END---")
		return
	}
	if len(os.Args) < 2 || strings.HasPrefix(os.Args[1], "-") {
		fmt.Fprintln(os.Stderr, "usage: savannah-dash <node> [--addr HOST:PORT] [--key FILE]")
		fmt.Fprintln(os.Stderr, "  finds the savannah CLI via $SAVANNAH_BIN (default: savannah on PATH)")
		os.Exit(2)
	}
	node := os.Args[1]
	key, addr := "", ""
	for i := 2; i < len(os.Args); i++ {
		switch os.Args[i] {
		case "--key":
			if i+1 < len(os.Args) {
				key = os.Args[i+1]
				i++
			}
		case "--addr":
			if i+1 < len(os.Args) {
				addr = os.Args[i+1]
				i++
			}
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

	m := model{node: node, key: key, addr: addr, bin: bin, input: ti}
	p := tea.NewProgram(m, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, "savannah-dash:", err)
		os.Exit(1)
	}
}
