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
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

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

// fetchTick is the board's one-second tick: the sessions are read again,
// and with --mesh every node's workers.
type fetchTick time.Time

type model struct {
	bin     string
	mesh    meshState     // --mesh: the nodes and their workers (mesh.go)
	group   bool          // rows gathered by repository inside each section (group.go; Ctrl-G)
	fade    time.Duration // rows untouched longer than this are dimmed; 0 = never (fade.go; Ctrl-F)
	history bool          // HISTORY (closed work sessions) open on the board (history.go; Ctrl-E)
	filter  string        // the filter in force: only matching rows are on the board (find.go; Ctrl-K)
	prompt  promptState   // what the prompt line is for: the row, the filter, a name, a title, a note

	// Naming the tabs the owner opened by hand (autoname.go).
	autoname  bool            // on: plain tabs with a prompt are named through `work autoname`
	naming    bool            // one `work autoname` is in flight
	nameTried map[string]bool // hook session ids submitted this run

	hooks    []session    // last good hook sessions (readSessions), kept across read errors
	work     []workRecord // last good work records (readWork), kept across read errors
	sessions []session    // hooks joined with work (joinWork) + offline work rows
	sessErr  string       // last session-read error
	workErr  string       // last work-dir read error

	rows   []row
	cursor int
	input  textinput.Model
	status string
	width  int
	height int
	home   string // $HOME, for ~ in paths

	// Work-row actions (work.go).
	pendingRm string            // work session awaiting the second Ctrl-X
	titles    map[string]string // iTerm2 pane id -> tab title (order.go), for unnamed tabs
	out       outputState       // the output view (modeOutput)
	nd        newDialog         // the new-task dialog (modeNew)
	panel     panelState        // the detail panel's reply fetch (board.go)
	lg        logState          // the log view (modeLog, logview.go)
	snap      snapState         // the snapshot screen (modeSnapshot, snapshot.go)
	ask       askState          // Ctrl-A: one question to every tab (modeAsk, ask.go)
	sessTicks int               // sessions reads so far (the snapshot hint waits for the first)

	// The screens (screens, below). vp is the one viewport the four
	// scrolling screens share (the worker view, output, log, ask): only one
	// is up at a time, and each makes it anew on entry.
	mode  int            // modeBoard | modeView | modeOutput | modeNew | modeLog | modeSnapshot | modeAsk
	click clickState     // the last mouse press, for the double click (mouse.go)
	vp    viewport.Model // the screen in front's scrolling body
	tail  tailState      // the full-screen worker view (modeView, view.go)
}

var (
	headerStyle   = lipgloss.NewStyle().Bold(true)
	nowStyle      = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("6")) // cyan: the NOW header, the eye's first stop
	colStyle      = lipgloss.NewStyle().Bold(true).Faint(true)
	selectedStyle = lipgloss.NewStyle().Reverse(true)
	helpStyle     = lipgloss.NewStyle().Faint(true)
	okStyle       = lipgloss.NewStyle().Foreground(lipgloss.Color("2")) // green: idle, done, good news
	warnStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("3")) // yellow: working, refusals, errors
	needStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("1")) // red: needs you
)

func cliError(err error) string {
	if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
		return strings.TrimSpace(string(ee.Stderr))
	}
	return err.Error()
}

func fetchTickCmd() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg { return fetchTick(t) })
}

func (m *model) rebuildRows() {
	var rows []row
	// Local sessions first (the tab-hell focus), in their sections (order.go),
	// then mesh workers.
	ordered := orderSessions(m.sessions, m.work)
	if m.group {
		ordered = groupSessions(ordered, m.work)
	}
	words := filterWords(m.filter)
	recs := recordMap(m.work)
	for _, s := range ordered {
		if s.closed() && !m.history && len(words) == 0 {
			continue // HISTORY closed: its rows are not on the board (history.go); a filter looks there too
		}
		if r := (row{kind: rowSession, s: s}); m.rowMatches(r, words, recs) {
			rows = append(rows, r)
		}
	}
	for _, n := range m.mesh.nodes {
		for _, t := range m.mesh.tasks[n] {
			if r := (row{kind: rowTask, node: n, t: t}); m.rowMatches(r, words, recs) {
				rows = append(rows, r)
			}
		}
	}
	m.rows = rows
	m.cursor = max(min(m.cursor, len(rows)-1), 0)
}

// sameRow reports whether two rows are the same session or the same mesh
// worker, whatever else about them changed between two builds of the rows.
func sameRow(a, b row) bool {
	return a.kind == b.kind && a.s.ID == b.s.ID && a.node == b.node && a.t.ID == b.t.ID
}

// cursorTo puts the cursor on the first row that matches; false, and the
// cursor where it was, when none does.
func (m *model) cursorTo(match func(row) bool) bool {
	for i, r := range m.rows {
		if match(r) {
			m.cursor = i
			return true
		}
	}
	return false
}

// rebuildKeepingCursor rebuilds the rows with the cursor following the row
// it was on. It reports whether that row is still shown; when it is not
// (or there was none) the cursor is where rebuildRows left it.
func (m *model) rebuildKeepingCursor() bool {
	before, had := m.currentRow()
	m.rebuildRows()
	return had && m.cursorTo(func(r row) bool { return sameRow(r, before) })
}

// moveCursor moves the highlight d rows, stopping at either end.
func (m *model) moveCursor(d int) {
	if c := m.cursor + d; c >= 0 && c < len(m.rows) {
		m.cursor = c
	}
}

// defaultWidth is the width the board is laid out for before the terminal
// has said its own (the first WindowSizeMsg).
const defaultWidth = 100

// boardWidth is the terminal's width, or defaultWidth while it is unknown.
func (m model) boardWidth() int {
	if m.width <= 0 {
		return defaultWidth
	}
	return m.width
}

// action logs one thing the owner did (a key, a click) at INFO.
func action(do string, kv ...any) {
	slog.Info("action", append([]any{"do", do}, kv...)...)
}

// act is an action with its status line: the INFO record, then what the
// board says while the command behind it runs.
func (m *model) act(do, status string, kv ...any) {
	action(do, kv...)
	m.status = status
}

// fail is a refused or failed command: a WARN record carrying the error,
// and "<what>: <the error's first line>" on the status line.
func (m *model) fail(logMsg, what, err string, kv ...any) {
	slog.Warn(logMsg, append(kv, "err", err)...)
	m.status = warnStyle.Render(what + ": " + clip(err))
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

// currentRow returns the highlighted row, ok=false if there is none.
func (m model) currentRow() (row, bool) {
	if m.cursor < 0 || m.cursor >= len(m.rows) {
		return row{}, false
	}
	return m.rows[m.cursor], true
}

// isWork reports whether a row is a work session's (live, tagged by the
// join, or an offline record row): the rows the work keys act on.
func (r row) isWork() bool { return r.kind == rowSession && r.s.Work != "" }

// sectionCounts is the header's numbers: open sessions (a closed one is
// counted apart, shown or not) and mesh tasks.
func (m model) sectionCounts() (sessions, tasks int) {
	for _, r := range m.rows {
		switch {
		case r.kind != rowSession:
			tasks++
		case !r.s.closed():
			sessions++
		}
	}
	return
}

func (m model) Init() tea.Cmd {
	cmds := []tea.Cmd{textinput.Blink, fetchTickCmd(), readSessionsCmd(), titlesCmd(), titleTickCmd(),
		m.snapshotsCmd()}
	if m.mesh.on {
		if m.mesh.pin == "" {
			cmds = append(cmds, m.discoverCmd(), discoverTickCmd())
		}
		cmds = append(cmds, m.fetchAll())
	}
	return tea.Batch(cmds...)
}

// screen is one of the board's full-screen modes: its keys, its view and,
// where it has them, what a resize must lay out again and its mouse.
type screen struct {
	keys   func(model, tea.KeyMsg) (tea.Model, tea.Cmd)
	view   func(model) string
	resize func(*model)                                   // nil: nothing of the screen's is sized to the terminal
	mouse  func(model, tea.MouseMsg) (tea.Model, tea.Cmd) // nil: the screen ignores the mouse
}

// screens is the one place a mode is dispatched from, indexed by mode
// (view.go): Update sends keys, resizes and the mouse through it, View
// draws through it. The new-session dialog and the snapshot screen draw
// from the model's width on every frame and so have nothing to resize.
var screens = [...]screen{
	modeBoard:    {keys: model.updateBoardKeys, view: model.viewBoard, mouse: model.updateMouse},
	modeView:     {keys: model.updateViewKeys, view: model.viewView, resize: (*model).refreshViewport},
	modeOutput:   {keys: model.updateOutputKeys, view: model.viewOutput, resize: (*model).refreshOutput},
	modeNew:      {keys: model.updateNewKeys, view: model.viewNew},
	modeLog:      {keys: model.updateLogKeys, view: model.viewLog, resize: (*model).refreshLog},
	modeSnapshot: {keys: model.updateSnapshotKeys, view: model.viewSnapshot},
	modeAsk:      {keys: model.updateAskKeys, view: model.viewAsk, resize: (*model).refreshAsk},
}

// Update hands each message to its owner: a command's result to the file
// whose command it was, a key or the mouse to the screen in front. What
// nobody owns (the cursor's blink, a resize once the screen has laid
// itself out again) ends at the prompt line's text box.
func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tailStartedMsg, tailDataMsg, tailExitMsg, tailRespawnMsg:
		return m.updateTail(msg)
	case workOpenMsg, workDoneMsg, workNewMsg, workAdoptMsg, reposMsg,
		workOutputMsg, outputTick:
		return m.updateWork(msg)
	case workMovedMsg, titlesMsg, titleTick:
		return m.updateOrder(msg)
	case logReadMsg, logTick:
		return m.updateLog(msg)
	case snapshotsMsg, snapshotDoneMsg, restoreDoneMsg:
		return m.updateSnapshot(msg)
	case nodesMsg, tasksMsg, taskDoneMsg, discoverTick:
		return m.updateMesh(msg)
	case jumpMsg, dismissMsg:
		return m.updateSessions(msg)
	case panelOutputMsg:
		return m.updatePanel(msg)
	case autonameMsg:
		return m.updateAutoname(msg)
	case askMsg:
		return m.updateAsk(msg)
	case openMsg:
		return m.updateOpen(msg)
	case fetchTick:
		// Sessions refresh every tick regardless of mesh; workers only when meshed.
		cmds := []tea.Cmd{readSessionsCmd(), fetchTickCmd()}
		if m.mesh.on {
			cmds = append(cmds, m.fetchAll())
		}
		return m, tea.Batch(cmds...)
	case sessionsMsg:
		m.applySessions(msg)
		m.snapshotHint()
		panel, naming := m.refreshPanel(), m.startAutoname()
		return m, tea.Batch(panel, naming)
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		m.input.Width = inputWidth(m.width)
		if resize := screens[m.mode].resize; resize != nil {
			resize(&m)
		}
	case tea.MouseMsg:
		if mouse := screens[m.mode].mouse; mouse != nil {
			return mouse(m, msg)
		}
		return m, nil
	case tea.KeyMsg:
		return screens[m.mode].keys(m, msg)
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(msg)
	return m, cmd
}

func (m model) View() string { return screens[m.mode].view(m) }

// newModel is the one constructor, so main and the tests start from the
// same initialised prompt box.
func newModel(o opts, bin string) model {
	ti := textinput.New()
	ti.Focus()
	ti.CharLimit = 4000
	ti.Width = 64
	home, _ := os.UserHomeDir()
	fade, err := parseFade(o.fade)
	if err != nil {
		fade = fadeDefault
	}
	m := model{
		bin: bin, group: o.group > 0,
		autoname: o.autoname >= 0, fade: fade, history: o.history,
		mesh: meshState{on: o.mesh, pin: o.pin, addr: o.addr, key: o.key,
			tasks: map[string][]task{}, errs: map[string]string{}},
		nameTried: map[string]bool{}, input: ti, home: home,
	}
	if o.pin != "" {
		m.mesh.nodes = []string{o.pin}
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
	prefs := loadBoardPrefs(boardFile()) // the remembered choices (prefs.go)
	o.applyPrefs(prefs)
	ticketURLPattern = resolveTicketURL(o.rememberTicketURL(prefs).TicketURL)
	openLog()
	slog.Info("board start", "pid", os.Getpid(), "args", os.Args[1:], "bin", bin, "log", logPath(),
		"group", o.group > 0, "autoname", o.autoname >= 0, "fade", o.fade, "mouse", o.mouse >= 0)
	popts := []tea.ProgramOption{tea.WithAltScreen()}
	if o.mouse >= 0 {
		popts = append(popts, tea.WithMouseCellMotion()) // clicks and the wheel (mouse.go)
	}
	p := tea.NewProgram(newModel(o, bin), popts...)
	if _, err := p.Run(); err != nil {
		slog.Error("board crashed", "err", err.Error())
		fmt.Fprintln(os.Stderr, "savannah-dash:", err)
		os.Exit(1)
	}
	slog.Info("board exit")
}
