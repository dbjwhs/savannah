// mesh.go: the mesh half of the board (--mesh, or a pinned node).
//
// The mesh is browsed with `savannah ls` on a slow tick (nodes appear and
// drop as they join and leave) and each node's `task ls <node> --json` is
// polled on the board's one-second tick; the workers render under the
// sessions as MESH WORKERS, with a NODE column. Enter on a worker sends a
// prompt (`task send`), Ctrl-X forgets a finished one (`task rm`), an empty
// Enter or Tab opens the live view (view.go). Nothing here runs without
// --mesh: the work board alone needs no savannahd.

package main

import (
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

type task struct {
	ID       string `json:"id"`
	Title    string `json:"title"`
	State    string `json:"state"`
	Turns    int    `json:"turns"`
	LastLine string `json:"last_line"`
	Worktree string `json:"worktree"`
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

// taskDoneMsg is the result of a `task send` or `task rm` on a mesh worker.
type taskDoneMsg struct {
	verb, node, id string // verb: "send" | "rm" (taskVerbs)
	ok             bool
}

// discoverTick is the slow tick the mesh is browsed on.
type discoverTick time.Time

// meshState is the mesh half of the board: how to reach it and what the
// last polls found.
type meshState struct {
	on    bool              // --mesh (or a pinned node): discovery + workers; off = the work board alone
	pin   string            // non-empty: single-node mode, skip discovery
	addr  string            // single-node --addr (a node off mDNS)
	key   string            // --key FILE, passed to every task command
	nodes []string          // discovered node names, sorted
	tasks map[string][]task // node -> its tasks
	errs  map[string]string // node -> last fetch error
	lsErr string            // last discovery error
}

// discoverCmd browses the mesh and returns the current node names. `savannah
// ls` prints "name host:port" per line; we take the name.
func (m model) discoverCmd() tea.Cmd {
	bin := m.bin
	return func() tea.Msg {
		out, err := poll(bin, "ls", "--timeout-ms", "1500")
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
	if m.mesh.pin != "" && m.mesh.addr != "" {
		args = append(args, "--addr", m.mesh.addr)
	}
	if m.mesh.key != "" {
		args = append(args, "--key", m.mesh.key)
	}
	return args
}

func (m model) fetchCmd(node string) tea.Cmd {
	bin := m.bin
	args := m.taskArgs("ls", node, "--json")
	return func() tea.Msg {
		ts, errText := runJSON[[]task](poll, bin, args, "bad json")
		return tasksMsg{node: node, tasks: ts, err: errText}
	}
}

// taskVerbs is what the board says about a finished `task send` or `task
// rm`: the INFO record and status line of a success, the WARN record and
// status line of a refusal (the CLI's exit is all the board is told, so
// why is the likely reason).
var taskVerbs = map[string]struct{ done, ok, failed, not, why string }{
	"send": {"sent", "sent to ", "send failed", "not sent to ", " (busy or gone)"},
	"rm":   {"task removed", "removed ", "task rm failed", "not removed ", " (running? cancel first)"},
}

// taskCmd runs one verb on a mesh worker: `task send <node> <id> <text>`,
// or `task rm <node> <id>`, which forgets a finished worker (the server
// refuses a running one, so a mistimed Ctrl-X is safe).
func (m model) taskCmd(verb, node, id string, rest ...string) tea.Cmd {
	bin := m.bin
	args := m.taskArgs(verb, node, append([]string{id}, rest...)...)
	return func() tea.Msg {
		_, err := run(bin, args...)
		return taskDoneMsg{verb: verb, node: node, id: id, ok: err == nil}
	}
}

func discoverTickCmd() tea.Cmd {
	return tea.Tick(3*time.Second, func(t time.Time) tea.Msg { return discoverTick(t) })
}

// fetchAll fans out one task-list fetch per known node.
func (m model) fetchAll() tea.Cmd {
	var cmds []tea.Cmd
	for _, n := range m.mesh.nodes {
		cmds = append(cmds, m.fetchCmd(n))
	}
	return tea.Batch(cmds...)
}

// updateMesh folds the mesh's ticks and results into the model.
func (m model) updateMesh(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case discoverTick:
		return m, tea.Batch(m.discoverCmd(), discoverTickCmd())
	case nodesMsg:
		m.applyNodes(msg)
	case tasksMsg:
		m.applyTasks(msg)
	case taskDoneMsg:
		return m.taskDone(msg)
	}
	return m, nil
}

// applyNodes takes one browse of the mesh: the nodes found, with the tasks
// of the nodes that left dropped. A failed browse keeps the last list.
func (m *model) applyNodes(msg nodesMsg) {
	m.mesh.lsErr = msg.err
	if msg.err != "" {
		return
	}
	m.mesh.nodes = msg.nodes
	live := map[string]bool{}
	for _, n := range msg.nodes {
		live[n] = true
	}
	for n := range m.mesh.tasks {
		if !live[n] {
			delete(m.mesh.tasks, n)
			delete(m.mesh.errs, n)
		}
	}
	m.rebuildRows()
}

// applyTasks takes one node's task list; a failed poll keeps its last one.
func (m *model) applyTasks(msg tasksMsg) {
	if msg.err != "" {
		m.mesh.errs[msg.node] = msg.err
	} else {
		delete(m.mesh.errs, msg.node)
		m.mesh.tasks[msg.node] = msg.tasks
	}
	m.rebuildRows()
	// A poll can flip the viewed task's decision banner on or off,
	// which changes the viewport height.
	if m.mode == modeView {
		m.refreshViewport()
	}
}

// taskDone says how a `task send` or `task rm` went (taskVerbs).
func (m model) taskDone(msg taskDoneMsg) (model, tea.Cmd) {
	v, who := taskVerbs[msg.verb], msg.node+"/"+msg.id
	if !msg.ok {
		slog.Warn(v.failed, "node", msg.node, "task", msg.id)
		m.status = warnStyle.Render(v.not + who + v.why)
		return m, nil
	}
	slog.Info(v.done, "node", msg.node, "task", msg.id)
	m.status = okStyle.Render(v.ok + who)
	if msg.verb == "rm" {
		// Drop the row now instead of waiting out the poll tick.
		return m, m.fetchCmd(msg.node)
	}
	return m, nil
}

// meshLines is the MESH WORKERS table (nothing without --mesh): its header,
// a discovery error, the column header and one line per worker, or what
// the board is waiting for, and a closing blank; each line with what the
// mouse hits there.
func (m model) meshLines(width int) hitLines {
	var out hitLines
	if !m.mesh.on {
		return out
	}
	out.add(noHit, headerStyle.Render("MESH WORKERS"))
	if m.mesh.lsErr != "" {
		out.add(noHit, warnStyle.Render("  discovery: "+m.mesh.lsErr))
	}
	_, nTask := m.sectionCounts()
	switch {
	case nTask > 0:
		avail := max(width-64, 12) // what the fixed cells leave
		out.add(noHit, colStyle.Render(
			pad("NODE", 14)+pad("ID", 7)+pad("STATE", 11)+pad("TURN", 4)+
				pad("TITLE", 20)+"LAST LINE"))
		for i, r := range m.rows {
			if r.kind == rowTask {
				out.add(lineHit{row: i, section: -1}, taskLine(r, i == m.cursor, avail))
			}
		}
	case m.mesh.pin == "" && len(m.mesh.nodes) == 0:
		out.add(noHit, helpStyle.Render("  discovering nodes..."))
	default:
		out.add(noHit, helpStyle.Render("  (no workers yet)"))
	}
	out.add(noHit, "")
	return out
}

// taskLine is one worker's row: its last line, or the decision it is
// waiting on (red until it is the highlighted row).
func taskLine(r row, selected bool, avail int) string {
	last := r.t.LastLine
	d, pending := pendingDecision(r.t)
	if pending {
		last = decisionCell(d)
	}
	line := pad(r.node, 14) + pad(r.t.ID, 7) + pad(r.t.State, 11) +
		pad(fmt.Sprintf("%d", r.t.Turns), 4) + pad(r.t.Title, 20) +
		trunc(last, avail)
	switch {
	case selected:
		return selectedStyle.Render(line)
	case pending:
		return needStyle.Render(line)
	}
	return line
}
