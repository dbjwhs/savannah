// savannah-dash: the owner's work board, with the mesh dashboard behind a flag.
//
// A thin Bubble Tea client over the savannah CLI, not a second implementation
// of song's wire protocol. Bare `savannah` execs it, so by default it is the
// WORK board: every work session (work/savannah_work.py: an iTerm2 tab with
// interactive Claude in a git worktree above a shell below) and every other
// open Claude Code tab on this machine (hooks/session-status.py), with the
// tabs that need you floated to the top. Keys open a new task (a tab, a
// worktree, a first prompt), jump to a tab, type into a tab's Claude, read
// what it said back, keep notes, and remove a finished session. Every action
// is a `savannah work <verb>` subprocess (work.go); the dash owns no state.
//
// `--mesh` adds the MESH WORKERS section: the mesh is browsed with `savannah
// ls` on a slow tick (nodes appear and drop as they join and leave), each
// node's `task ls <node> --json` is polled once a second, and the workers
// render under the sessions with a NODE column. Enter on a worker sends a
// prompt (`task send`); an empty Enter or Tab opens the tmux-like live view
// (view.go). A leading node name pins one node (with --addr for a node not
// on mDNS) and implies --mesh. One cursor moves over both sections; the
// header counts everything that needs you (sessions waiting + pending mesh
// decisions) in one number.
//
// Usage:
//
//	savannah-dash                                   # the work board (no savannahd)
//	savannah-dash --mesh [--key FILE]               # plus every node on the mesh
//	savannah-dash <node> [--addr H:P] [--key FILE]  # plus one pinned node
//
// The savannah binary is found via $SAVANNAH_BIN (default "savannah" on
// PATH); iTerm2's it2 via $SAVANNAH_IT2 (default: it2 on PATH, else the copy
// inside iTerm.app).
package main

import (
	"encoding/json"
	"fmt"
	"log/slog"
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
	key   string
	bin   string
	pin   string // non-empty: single-node mode, skip discovery
	addr  string // single-node --addr (a node off mDNS)
	mesh  bool   // --mesh (or a pinned node): discovery + workers; off = the work board alone
	group bool   // rows gathered by repository inside each section (group.go; Ctrl-G)

	// Naming the tabs the owner opened by hand (autoname.go).
	autoname  bool            // on: plain tabs with a prompt are named through `work autoname`
	naming    bool            // one `work autoname` is in flight
	nameTried map[string]bool // hook session ids submitted this run

	nodes    []string          // discovered node names, sorted
	tasks    map[string][]task // node -> its tasks
	errs     map[string]string // node -> last fetch error
	lsErr    string            // last discovery error
	hooks    []session         // last good hook sessions (readSessions), kept across read errors
	work     []workRecord      // last good work records (readWork), kept across read errors
	sessions []session         // hooks joined with work (joinWork) + offline work rows
	sessErr  string            // last session-read error
	workErr  string            // last work-dir read error

	rows   []row
	cursor int
	input  textinput.Model
	status string
	width  int
	height int
	home   string // $HOME, for ~ in paths

	// Work-row actions (work.go).
	pendingRm    string            // work session awaiting the second Ctrl-X
	noteFor      string            // non-empty: the prompt line is a note for this work session
	adoptFor     string            // non-empty: the prompt line names this plain tab (hook session id)
	adoptProject string            // that tab's directory, for the label
	adoptNote    bool              // Ctrl-T named it: the note follows; Ctrl-R: just the name
	titleFor     string            // non-empty: the prompt line is the title of this work session
	titles       map[string]string // iTerm2 pane id -> tab title (order.go), for unnamed tabs
	out          outputState       // the output view (modeOutput)
	nd           newDialog         // the new-task dialog (modeNew)
	panel        panelState        // the detail panel's reply fetch (board.go)
	lg           logState          // the log view (modeLog, logview.go)
	snap         snapState         // the snapshot screen (modeSnapshot, snapshot.go)
	sessTicks    int               // sessions reads so far (the snapshot hint waits for the first)

	// Full-screen worker view (view.go).
	mode     int // modeBoard | modeView | modeOutput | modeNew | modeLog | modeSnapshot
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
		out, err := poll(bin, args...)
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
		_, err := run(bin, args...)
		return sentMsg{node: node, id: id, ok: err == nil}
	}
}

// rmCmd forgets a finished worker (`task rm`); the server refuses a running
// one, so a mistimed Ctrl-X is safe.
func (m model) rmCmd(node, id string) tea.Cmd {
	bin := m.bin
	args := m.taskArgs("rm", node, id)
	return func() tea.Msg {
		_, err := run(bin, args...)
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
	// Local sessions first (the tab-hell focus), in their sections (order.go),
	// then mesh workers.
	ordered := orderSessions(m.sessions, m.work)
	if m.group {
		ordered = groupSessions(ordered, m.work)
	}
	for _, s := range ordered {
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

// applySessions folds one tick's hook sessions and work records into the
// model and rebuilds the rows. A side that failed to read keeps its last good
// list (so a transient sessions-dir error does not flip every live tab to an
// offline work row) and only its error line changes; the join is redone
// every tick from the two kept lists.
func (m *model) applySessions(msg sessionsMsg) {
	m.sessErr = msg.err
	if msg.err == "" {
		m.hooks = msg.hooks
	}
	m.workErr = msg.workErr
	if msg.workErr == "" {
		m.work = msg.work
	}
	before := rowSignature(m.rows)
	m.sessions = joinWork(m.hooks, m.work)
	m.rebuildRows()
	m.sessTicks++
	if after := rowSignature(m.rows); after != before {
		slog.Debug("sessions changed", "rows", len(m.rows), "work", len(m.work),
			"waiting", countSessionAttention(m.sessions), "polling", m.countPolling())
		verbose("rows", "list", after)
	}
}

// rowSignature is one line naming every row and its state, so a changed
// board is logged once (DEBUG) and the ticks in between are not.
func rowSignature(rows []row) string {
	var b strings.Builder
	for _, r := range rows {
		if r.kind != rowSession {
			continue
		}
		who := r.s.Work
		if who == "" {
			who = "#" + r.s.ID
		}
		fmt.Fprintf(&b, "%s=%s ", who, r.s.State)
	}
	return strings.TrimSpace(b.String())
}

// toggleGroup is Ctrl-G: the rows regroup (or return to the plain rank
// order), the cursor stays on the row it was on, and the choice is written
// to the board file for the next run (a failure to write is a status line,
// not a refusal: the toggle still holds for this run).
func (m model) toggleGroup() model {
	before, had := m.currentRow()
	m.group = !m.group
	m.rebuildRows()
	if had {
		for i, r := range m.rows {
			if r.kind == before.kind && r.s.ID == before.s.ID && r.node == before.node && r.t.ID == before.t.ID {
				m.cursor = i
				break
			}
		}
	}
	slog.Info("action", "do", "group", "on", m.group)
	m.status = "rows in your order (Ctrl-G groups them by repository)"
	if m.group {
		m.status = "rows grouped by repository (Ctrl-G returns to your order)"
	}
	prefs := loadBoardPrefs(boardFile())
	prefs.Group = groupPref(m.group)
	if err := saveBoardPrefs(boardFile(), prefs); err != nil {
		slog.Warn("board file", "err", err.Error())
		m.status += warnStyle.Render("; not remembered: " + err.Error())
	}
	return m
}

// currentRow returns the highlighted row, ok=false if there is none.
func (m model) currentRow() (row, bool) {
	if m.cursor < 0 || m.cursor >= len(m.rows) {
		return row{}, false
	}
	return m.rows[m.cursor], true
}

// isWorkRow reports whether a row is a work session's (live, tagged by the
// join, or an offline record row): the rows the work keys act on.
func isWorkRowOf(r row) bool { return r.kind == rowSession && r.s.Work != "" }

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
	cmds := []tea.Cmd{textinput.Blink, fetchTickCmd(), readSessionsCmd(), titlesCmd(), titleTickCmd(),
		m.snapshotsCmd()}
	if m.mesh {
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
	case workOpenMsg, workSaidMsg, workNotedMsg, workRmMsg, workNewMsg, workAdoptMsg, reposMsg,
		workOutputMsg, outputTick:
		return m.updateWork(msg)
	case workMovedMsg, workTitledMsg, titlesMsg, titleTick:
		return m.updateOrder(msg)
	case logReadMsg, logTick:
		return m.updateLog(msg)
	case snapshotsMsg, snapshotDoneMsg, restoreDoneMsg:
		return m.updateSnapshot(msg)
	case panelOutputMsg:
		if msg.seq == m.panel.seq {
			m.panel.text, m.panel.have = msg.text, true
		}
		return m, nil
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.input.Width = inputWidth(m.width)
		switch m.mode {
		case modeView:
			m.refreshViewport()
		case modeOutput:
			m.refreshOutput()
		case modeLog:
			m.refreshLog()
		}
	case discoverTick:
		return m, tea.Batch(m.discoverCmd(), discoverTickCmd())
	case fetchTick:
		// Sessions refresh every tick regardless of mesh; workers only when meshed.
		cmds := []tea.Cmd{readSessionsCmd(), fetchTickCmd()}
		if m.mesh {
			cmds = append(cmds, m.fetchAll())
		}
		return m, tea.Batch(cmds...)
	case sessionsMsg:
		m.applySessions(msg)
		m.snapshotHint()
		return m, tea.Batch(m.refreshPanel(), m.startAutoname())
	case autonameMsg:
		m.naming = false
		if msg.err != "" {
			slog.Warn("autoname failed", "tabs", len(msg.ids), "err", msg.err)
		} else {
			slog.Info("autonamed", "tabs", len(msg.ids), "named", len(msg.out.Named),
				"skipped", len(msg.out.Skipped), "failed", len(msg.out.Failed))
		}
		m.status = autonameStatus(msg)
		return m, readSessionsCmd()
	case jumpMsg:
		if msg.ok {
			slog.Info("jumped", "id", msg.id, "how", msg.how)
			m.status = okStyle.Render("jumped to " + msg.id + " (" + msg.how + ")")
		} else {
			slog.Warn("jump failed", "id", msg.id, "how", msg.how)
			m.status = warnStyle.Render("could not jump to " + msg.id +
				" (" + msg.how + ")")
		}
		return m, nil
	case dismissMsg:
		if msg.ok {
			slog.Info("dismissed", "id", msg.id)
			m.status = okStyle.Render("dismissed session " + msg.id)
			return m, readSessionsCmd()
		}
		slog.Warn("dismiss failed", "id", msg.id)
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
			slog.Info("sent", "node", msg.node, "task", msg.id)
			m.status = okStyle.Render("sent to " + msg.node + "/" + msg.id)
		} else {
			slog.Warn("send failed", "node", msg.node, "task", msg.id)
			m.status = warnStyle.Render("not sent to " + msg.node + "/" + msg.id + " (busy or gone)")
		}
		return m, nil
	case rmMsg:
		if msg.ok {
			slog.Info("task removed", "node", msg.node, "task", msg.id)
			m.status = okStyle.Render("removed " + msg.node + "/" + msg.id)
			// Drop the row now instead of waiting out the poll tick.
			return m, m.fetchCmd(msg.node)
		}
		m.status = warnStyle.Render("not removed " + msg.node + "/" + msg.id +
			" (running? cancel first)")
		return m, nil
	case tea.KeyMsg:
		switch m.mode {
		case modeView:
			return m.updateViewKeys(msg)
		case modeOutput:
			return m.updateOutputKeys(msg)
		case modeNew:
			return m.updateNewKeys(msg)
		case modeLog:
			return m.updateLogKeys(msg)
		case modeSnapshot:
			return m.updateSnapshotKeys(msg)
		}
		return m.updateBoardKeys(msg)
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	return m, cmd
}

// updateBoardKeys is the board's key map. Work rows (a live tab tagged with
// its work session, or an offline record) get the work verbs; a plain hook
// row keeps jump and dismiss; a mesh worker keeps send, view and rm.
func (m model) updateBoardKeys(msg tea.KeyMsg) (tea.Model, tea.Cmd) {
	// A pending Ctrl-X confirm survives exactly one keypress: the next Ctrl-X
	// on the same row. Anything else (a cursor move, a typed character)
	// withdraws it, so a removal is never confirmed by accident.
	pending := m.pendingRm
	m.pendingRm = ""
	r, hasRow := m.currentRow()
	isWork := hasRow && isWorkRowOf(r)
	switch msg.Type {
	case tea.KeyCtrlC:
		return m, tea.Quit
	case tea.KeyEsc:
		if m.adoptFor != "" {
			m.adoptFor, m.adoptProject = "", ""
			m.input.SetValue("")
			m.status = "naming cancelled"
			return m, nil
		}
		if m.titleFor != "" {
			m.titleFor = ""
			m.input.SetValue("")
			m.status = "title cancelled"
			return m, nil
		}
		if m.noteFor != "" {
			m.noteFor = ""
			m.input.SetValue("")
			m.status = "note cancelled"
			return m, nil
		}
		return m, tea.Quit
	case tea.KeyCtrlN:
		cmd := m.enterNew()
		return m, cmd
	case tea.KeyCtrlL:
		return m, m.enterLog()
	case tea.KeyCtrlS:
		return m, m.enterSnapshot()
	case tea.KeyCtrlG:
		return m.toggleGroup(), nil
	case tea.KeyUp:
		if msg.Alt { // Option-Up: a third spelling of the drag (see Shift-Up)
			return m.dragRow(-1)
		}
		if m.cursor > 0 {
			m.cursor--
		}
		return m, m.refreshPanel()
	case tea.KeyDown:
		if msg.Alt {
			return m.dragRow(1)
		}
		if m.cursor < len(m.rows)-1 {
			m.cursor++
		}
		return m, m.refreshPanel()
	// The drag has three spellings, since a terminal or the OS may take one:
	// Shift-arrows (the one the help names), Ctrl-arrows (macOS Mission
	// Control takes these by default) and Option-arrows (KeyUp/Down + Alt).
	case tea.KeyShiftUp, tea.KeyCtrlUp:
		return m.dragRow(-1)
	case tea.KeyShiftDown, tea.KeyCtrlDown:
		return m.dragRow(1)
	case tea.KeyCtrlR:
		return m.startNaming(r, hasRow)
	case tea.KeyTab:
		if !hasRow {
			return m, nil
		}
		if r.kind == rowSession {
			return m, jumpSessionCmd(r.s)
		}
		return m, m.enterView(r.node, r.t.ID)
	case tea.KeyCtrlO:
		if !isWork {
			m.status = warnStyle.Render("Ctrl-O shows a work session's last replies; select one")
			return m, nil
		}
		cmd := m.enterOutput(r.s.Work)
		return m, cmd
	case tea.KeyCtrlT:
		if !isWork {
			if hasRow && r.kind == rowSession {
				// A plain tab has nowhere to keep a note until it is a work
				// session: name it first (adopt), the note follows.
				m.adoptFor, m.adoptProject, m.adoptNote = r.s.ID, r.s.Project, true
				m.noteFor, m.titleFor = "", ""
				m.input.SetValue(nameSuggestion(r.s.Project, titleOf(r.s, m.titles), m.takenNames()))
				m.input.CursorEnd()
				m.status = ""
				return m, nil
			}
			m.status = warnStyle.Render("Ctrl-T notes a session; select one")
			return m, nil
		}
		m.noteFor = r.s.Work
		m.adoptFor, m.adoptProject, m.titleFor = "", "", ""
		m.input.SetValue("")
		m.status = ""
		return m, nil
	case tea.KeyCtrlX:
		if !hasRow {
			return m, nil
		}
		if isWork {
			name := r.s.Work
			if pending == name {
				slog.Info("action", "do", "rm", "name", name)
				m.status = "removing " + name + "..."
				return m, m.workRmCmd(name)
			}
			m.pendingRm = name
			m.status = warnStyle.Render("Ctrl-X again to remove " + name +
				" (closes its tab, removes a clean worktree, keeps the branch)")
			return m, nil
		}
		if r.kind == rowSession {
			slog.Info("action", "do", "dismiss", "id", r.s.ID)
			m.status = "dismissing session " + r.s.ID + "..."
			return m, dismissSessionCmd(r.s)
		}
		if r.t.State == "running" {
			m.status = warnStyle.Render(r.node + "/" + r.t.ID +
				" is running (cancel first)")
			return m, nil
		}
		slog.Info("action", "do", "task rm", "node", r.node, "task", r.t.ID)
		m.status = "removing " + r.node + "/" + r.t.ID + "..."
		return m, m.rmCmd(r.node, r.t.ID)
	case tea.KeyEnter:
		text := strings.TrimSpace(m.input.Value())
		if m.adoptFor != "" {
			// A name as typed, or any words: then the name is derived and
			// the words become the title (board.go nameFor).
			name, title, nerr := nameFor(text, m.takenNames())
			if nerr != "" {
				m.status = warnStyle.Render(nerr)
				return m, nil
			}
			slog.Info("action", "do", "adopt", "name", name, "title", title, "session", m.adoptFor)
			m.status = "naming it " + name + "..."
			return m, m.workAdoptCmd(name, m.adoptFor, title)
		}
		if m.titleFor != "" {
			name := m.titleFor
			rec, _ := m.recordOf(name)
			switch {
			case text == rec.Title && text == "":
				m.status = warnStyle.Render(name + " has no title yet; type one (the name stays " + name + ")")
			case text == rec.Title:
				m.titleFor = ""
				m.input.SetValue("")
				m.status = "title unchanged"
			case text == "":
				slog.Info("action", "do", "title clear", "name", name)
				m.status = "clearing the title of " + name + "..."
				return m, m.workTitleCmd(name, "")
			default:
				slog.Info("action", "do", "title", "name", name, "title", text)
				m.status = "titling " + name + "..."
				return m, m.workTitleCmd(name, text)
			}
			return m, nil
		}
		if m.noteFor != "" {
			name := m.noteFor
			if text == "" {
				m.status = warnStyle.Render("nothing to note for " + name + " (Esc cancels)")
				return m, nil
			}
			m.input.SetValue("")
			m.noteFor = ""
			slog.Info("action", "do", "note", "name", name, "chars", len(text))
			m.status = "noting " + name + "..."
			return m, m.workNoteCmd(name, text)
		}
		if !hasRow {
			return m, nil
		}
		if isWork {
			name := r.s.Work
			if text == "" {
				slog.Info("action", "do", "open", "name", name, "state", r.s.State)
				m.status = "opening " + name + "..."
				return m, m.workOpenCmd(name)
			}
			m.input.SetValue("")
			slog.Info("action", "do", "say", "name", name, "chars", len(text))
			slog.Debug("say text", "name", name, "head", head([]byte(text), 200))
			m.status = "saying to " + labelOf(r.s) + "..."
			return m, m.workSayCmd(name, text)
		}
		if r.kind == rowSession {
			// A plain tab is a person's session: jump to it, never type.
			if text != "" {
				m.status = warnStyle.Render(
					"cannot send to a plain tab (not a work session); jumping instead")
			}
			slog.Info("action", "do", "jump", "id", r.s.ID)
			return m, jumpSessionCmd(r.s)
		}
		if text == "" {
			// Empty prompt: open the full-screen view instead.
			return m, m.enterView(r.node, r.t.ID)
		}
		m.input.SetValue("")
		slog.Info("action", "do", "task send", "node", r.node, "task", r.t.ID, "chars", len(text))
		m.status = "sending to " + r.node + "/" + r.t.ID + "..."
		return m, m.sendCmd(r.node, r.t.ID, text)
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

// inputView renders the prompt line's text box with a placeholder that says
// what Enter will do for the highlighted row.
func (m model) inputView(placeholder string) string {
	m.input.Placeholder = placeholder
	return m.input.View()
}

func (m model) View() string {
	switch m.mode {
	case modeView:
		return m.viewView()
	case modeOutput:
		return m.viewOutput()
	case modeNew:
		return m.viewNew()
	case modeLog:
		return m.viewLog()
	case modeSnapshot:
		return m.viewSnapshot()
	}
	return m.viewBoard()
}

// viewBoard is the board: header, the tables, the detail panel sized to the
// room left (board.go), the prompt line, status and help. It is built as
// lines so the panel can take exactly what the terminal has left; the whole
// thing is never taller than the terminal.
func (m model) viewBoard() string {
	width := m.width
	if width <= 0 {
		width = 100
	}
	nSess, nTask := m.sectionCounts()

	scope := "work"
	if m.mesh {
		scope = "mesh"
		if m.pin != "" {
			scope = m.pin
		}
	}
	counts := fmt.Sprintf("%d session%s", nSess, plural(nSess))
	if m.group {
		counts += "   by repository"
	}
	if p := m.countPolling(); p > 0 {
		counts += fmt.Sprintf("   %d polling", p)
	}
	if m.mesh {
		counts += fmt.Sprintf("   %d worker%s", nTask, plural(nTask))
	}
	// One attention number over both worlds: sessions needing you + pending
	// mesh decisions. This is the whole point: one glance, total.
	attention := countSessionAttention(m.sessions) + countDecisions(m.rows)
	deco := ""
	if attention > 0 {
		deco = "   " + warnStyle.Render(fmt.Sprintf("%d need you", attention))
	}
	top := []string{fmt.Sprintf("%s   %s   %s%s",
		headerStyle.Render("savannah  "+scope), counts,
		time.Now().Format("15:04:05"), deco), ""}
	now := nowUnix()

	// ---- LOCAL SESSIONS (open Claude Code tabs, via the hook) ----
	// The section shows when there are rows OR a read error to report: an
	// error with zero rows must still be visible, not hidden by the guard.
	if nSess > 0 || m.sessErr != "" || m.workErr != "" {
		top = append(top, headerStyle.Render("LOCAL SESSIONS"))
		if nSess > 0 {
			top = append(top, m.sessionLines(sessionColumns(width, m.sessions, m.home, m.titles, m.group), now)...)
		}
		if m.sessErr != "" {
			top = append(top, warnStyle.Render("  sessions: "+m.sessErr+" (showing last good list)"))
		}
		if m.workErr != "" {
			top = append(top, warnStyle.Render("  work: "+m.workErr+" (showing last good list)"))
		}
		top = append(top, "")
	}

	// ---- MESH WORKERS (savannah tasks) ----
	if m.mesh {
		avail := width - 64 // the fixed cells
		if avail < 12 {
			avail = 12
		}
		top = append(top, headerStyle.Render("MESH WORKERS"))
		if m.lsErr != "" {
			top = append(top, warnStyle.Render("  discovery: "+m.lsErr))
		}
		if nTask == 0 {
			if m.pin == "" && len(m.nodes) == 0 {
				top = append(top, helpStyle.Render("  discovering nodes..."))
			} else {
				top = append(top, helpStyle.Render("  (no workers yet)"))
			}
		} else {
			top = append(top, colStyle.Render(
				pad("NODE", 14)+pad("ID", 7)+pad("STATE", 11)+pad("TURN", 4)+
					pad("TITLE", 20)+"LAST LINE"))
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
				top = append(top, line)
			}
		}
		top = append(top, "")
	}

	bottom := m.promptLines(width)
	panelH := 0
	if m.height > 0 {
		panelH = m.height - len(top) - len(bottom)
	}
	lines := top
	lines = append(lines, m.panelLines(width, panelH, time.Now())...)
	lines = append(lines, bottom...)
	return strings.Join(lines, "\n")
}

// promptLines is the bottom of the board: a blank line, the prompt line
// (whose label says what Enter will do for the highlighted row), a blank
// line, the status line (a CLI error can be long) and the key help.
func (m model) promptLines(width int) []string {
	r, ok := m.currentRow()
	room := width - 70
	if room < 8 {
		room = 8
	}
	var prompt string
	switch {
	case m.adoptFor != "":
		ph := "a name, or any words (the name is derived); Enter makes it a work session, Esc cancels"
		if m.adoptNote {
			ph = "a name, or any words; Enter makes it a work session and asks for the note, Esc cancels"
		}
		prompt = headerStyle.Render(trunc("name this tab ("+projectLabel(m.adoptProject)+")", room)) + ":  " +
			m.inputView(ph)
	case m.titleFor != "":
		prompt = headerStyle.Render(trunc("title for "+m.titleFor, room)) + ":  " +
			m.inputView("the words the board shows for it (Enter saves, empty Enter clears, Esc cancels); the name stays")
	case m.noteFor != "":
		prompt = headerStyle.Render(trunc("note for "+m.noteFor, room)) + ":  " +
			m.inputView("what you decided or learned (Enter saves, Esc cancels)")
	case !ok:
		hint := "no rows yet: Ctrl-N opens a work session (a tab, a worktree, a first prompt)"
		if m.mesh {
			hint = "no rows yet: Ctrl-N opens a work session; workers appear as nodes are found"
		}
		prompt = helpStyle.Render(hint)
	case isWorkRowOf(r):
		placeholder := "text for its Claude; empty Enter opens the tab"
		if r.s.State == "offline" {
			placeholder = "empty Enter reopens the tab (claude --continue); text needs a live tab"
		}
		prompt = headerStyle.Render(trunc("say to "+labelOf(r.s), room)) + ":  " + m.inputView(placeholder)
	case r.kind == rowSession:
		hint := "a plain tab (no work session): Enter jumps to it, Ctrl-R names it"
		prompt = headerStyle.Render(fitLabel(projectLabel(r.s.Project), width, hint)) +
			"   " + helpStyle.Render(hint)
	default:
		prompt = "send to " + headerStyle.Render(r.node+"/"+r.t.ID) +
			":  " + m.inputView("prompt for this worker; empty Enter opens the live view")
	}
	// One line, never wrapped (a wrapped help line would push the layout
	// down): hints are dropped from the end until the line fits, so a
	// narrow terminal loses "^C quit" (Esc quits too) before anything else.
	parts := []string{"Shift-up/down move", "Enter say", "^N new", "^R name", "^G group", "^O out",
		"^T note", "^S snap", "^L log", "^X rm", "^C quit"}
	if m.mesh {
		parts[1] = "Enter say/send"
		parts = append(parts[:2], append([]string{"Tab view"}, parts[2:]...)...)
	}
	if m.group {
		parts[indexOf(parts, "^G group")] = "^G ungroup"
	}
	return []string{"", prompt, "", m.status, helpStyle.Render(fitHelp(parts, width))}
}

// fitHelp joins the hints with two spaces, dropping hints from the end
// while the line is wider than the terminal; the first hint always stays
// (cut if even that does not fit).
func fitHelp(parts []string, width int) string {
	for len(parts) > 1 && len([]rune(strings.Join(parts, "  "))) > width {
		parts = parts[:len(parts)-1]
	}
	return trunc(strings.Join(parts, "  "), width)
}

// labelOf is what the board calls a work session in prompts: its title when
// it has one, else its name.
func labelOf(s session) string {
	if s.WorkTitle != "" {
		return s.WorkTitle
	}
	return s.Work
}

func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}

// opts is the command line: work board by default, mesh on request.
type opts struct {
	pin, key, addr string
	mesh, help     bool
	group          int // --group 1, --no-group -1, 0 = as the board file remembers
	autoname       int // --autoname 1, --no-autoname -1, 0 = on unless the board file says "off"
}

// parseArgs reads the argv after the program name. A leading non-flag word
// pins one node (and implies --mesh). --no-mesh and --sessions, the old way
// to get the work board, are accepted and mean nothing now. --group and
// --no-group set the grouping for this run over the remembered choice.
func parseArgs(args []string) (opts, error) {
	var o opts
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		o.pin, o.mesh = args[0], true
		args = args[1:]
	}
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--key", "--addr":
			if i+1 >= len(args) {
				return o, fmt.Errorf("%s needs a value", args[i])
			}
			if args[i] == "--key" {
				o.key = args[i+1]
			} else {
				o.addr = args[i+1]
			}
			i++
		case "--mesh":
			o.mesh = true
		case "--group":
			o.group = 1
		case "--no-group":
			o.group = -1
		case "--autoname":
			o.autoname = 1
		case "--no-autoname":
			o.autoname = -1
		case "--no-mesh", "--sessions", "--work":
			// The work board is the default; kept so old scripts still run.
		case "-h", "--help":
			o.help = true
		default:
			return o, fmt.Errorf("unknown argument %q", args[i])
		}
	}
	return o, nil
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: savannah-dash [--mesh] [<node>] [--addr H:P] [--key FILE] [--group|--no-group] [--no-autoname]")
	fmt.Fprintln(os.Stderr, "  no flags:  the work board: your work sessions and open Claude Code tabs")
	fmt.Fprintln(os.Stderr, "  --mesh:    also discover every node via `savannah ls` and show its workers")
	fmt.Fprintln(os.Stderr, "  <node>:    pin one node (use --addr for a node not on mDNS); implies --mesh")
	fmt.Fprintln(os.Stderr, "  --group:   gather the rows by repository (Ctrl-G toggles it; the choice is")
	fmt.Fprintln(os.Stderr, "             remembered in $SAVANNAH_BOARD_FILE, ~/.claude/savannah/board.json)")
	fmt.Fprintln(os.Stderr, "  --no-autoname:  do not name the tabs you opened by hand (`work autoname`;")
	fmt.Fprintln(os.Stderr, "             \"autoname\": \"off\" in the board file turns it off for good)")
	fmt.Fprintln(os.Stderr, "Sessions come from the session-status hook, work sessions from $SAVANNAH_WORK_DIR")
	fmt.Fprintln(os.Stderr, "(~/.claude/savannah/work). Every action runs `savannah work ...` ($SAVANNAH_BIN).")
}

// newModel is the one constructor, so main and the tests start from the
// same initialised prompt box.
func newModel(o opts, bin string) model {
	ti := textinput.New()
	ti.Focus()
	ti.CharLimit = 4000
	ti.Width = 64
	home, _ := os.UserHomeDir()
	m := model{
		key: o.key, bin: bin, pin: o.pin, addr: o.addr, mesh: o.mesh, group: o.group > 0,
		autoname: o.autoname >= 0,
		tasks:    map[string][]task{}, errs: map[string]string{},
		input: ti, home: home,
	}
	if o.pin != "" {
		m.nodes = []string{o.pin}
	}
	return m
}

func main() {
	o, err := parseArgs(os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, "savannah-dash:", err)
		usage()
		os.Exit(2)
	}
	if o.help {
		usage()
		os.Exit(2)
	}
	bin := os.Getenv("SAVANNAH_BIN")
	if bin == "" {
		bin = "savannah"
	}
	prefs := loadBoardPrefs(boardFile()) // the remembered choices (group.go)
	if o.group == 0 && prefs.Group == "repo" {
		o.group = 1
	}
	if o.autoname == 0 && prefs.Autoname == "off" {
		o.autoname = -1
	}
	openLog()
	slog.Info("board start", "pid", os.Getpid(), "args", os.Args[1:], "bin", bin, "log", logPath(),
		"group", o.group > 0, "autoname", o.autoname >= 0)
	p := tea.NewProgram(newModel(o, bin), tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		slog.Error("board crashed", "err", err.Error())
		fmt.Fprintln(os.Stderr, "savannah-dash:", err)
		os.Exit(1)
	}
	slog.Info("board exit")
}

// fitLabel truncates a footer label so label + 3 spaces + hint fits the
// terminal width (a 64-char work name would otherwise soft-wrap at 80 cols).
func fitLabel(label string, width int, hint string) string {
	if width <= 0 {
		return label
	}
	room := width - 3 - len([]rune(hint))
	if room < 8 {
		room = 8
	}
	return trunc(label, room)
}
